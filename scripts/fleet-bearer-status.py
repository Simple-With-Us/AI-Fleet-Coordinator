#!/usr/bin/env python3
"""Poll the owner bearer status API and raise alarms for Muster.

Every run (the LaunchAgent com.jay.fleet-bearer-status, every 5 minutes) reads
GET https://agents.simplewithus.com/admin/api/bearers with the owner's own Cloudflare Access login
(`cloudflared access token`, run once by the owner with `cloudflared access login`), then writes
~/.local/state/fleet-tools/bearer-status.json (0600, atomic):

  {"schemaVersion": 1, "tool": "fleet-bearer-status", "generatedAt", "ok": bool, "error"?: str,
   "rows": [{"identity","kind","problem","active","expired","expiresAt","mcpAccept","paused",
             "missingRecords","mismatchCount"}], "alarms": [{"key","summary","since","lastAlarm"}],
   "recovered": [{"key","at"}]}

Names, booleans, counts and times only:  the API returns no bearer, hash or secret, and this script
never prints or stores the Access token.  An alarm key is "problem:<identity>:<problem>", the
growth keys "delta:mcp_http_401" and "delta:relay_refusal", and "access:login_needed" when the
Access login is missing or refused.  A key alarms (macOS notification) when it first appears, again
every 6 hours while it stays, and a recovery notice goes out once when it clears.  Python 3.9, stdlib only.

  fleet-bearer-status.py [--write PATH] [--state PATH] [--url URL] [--no-notify] [--dry-run]
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

URL = "https://agents.simplewithus.com/admin/api/bearers"
APP = "https://agents.simplewithus.com/admin"
OUT = os.path.expanduser("~/.local/state/fleet-tools/bearer-status.json")
STATE = os.path.expanduser("~/.local/state/fleet-tools/bearer-alarm-state.json")
REALARM_S = 6 * 3600
CLOUDFLARED = ("/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared")


def access_token():
    for exe in CLOUDFLARED:
        if not os.path.exists(exe):
            continue
        try:
            r = subprocess.run(
                [exe, "access", "token", "-app=" + APP],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        tok = r.stdout.strip()
        if r.returncode == 0 and tok.count(".") == 2:
            return tok
    return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def fetch(url, token):
    req = urllib.request.Request(
        url,
        headers={
            "cf-access-token": token,
            "Accept": "application/json",
            "User-Agent": "fleet-bearer-status/1",
        },
    )
    # Never follow a redirect with the owner's Access token attached:  a 3xx is an answer (Access sends the
    # browser to its login page when the token is no good), not a page to fetch.
    with _OPENER.open(req, timeout=30) as resp:
        return json.loads(resp.read(2_000_000))


def row_summary(r):
    st = r.get("status") or {}
    mm = r.get("mismatches") or {}
    mr = r.get("missingRecords") or {}
    return {
        "identity": r.get("identity"),
        "kind": r.get("kind"),
        "problem": r.get("problem"),
        "active": bool(st.get("active")),
        "expired": bool(st.get("expired")),
        "expiresAt": st.get("expires_at"),
        "mcpAccept": st.get("mcp_accept"),
        "paused": r.get("paused"),
        "missingRecords": mr.get("names") if isinstance(mr, dict) else None,
        "mismatchCount": mm.get("count") if isinstance(mm, dict) else None,
    }


def desired_alarms(doc, last_counts):
    """(alarms {key: summary}, new counts) from one API document and the per-row counts seen on the last poll.

    A diagnostic row is an aggregate:  its `count` is the lifetime total for (kind, reason, seat) and its `ts` is the
    last occurrence, so growth is the difference from the count seen last time.  `last_counts` None means the first
    poll:  it only records the baseline, so history never alarms.  A count below the stored one (the row was evicted
    and began again) counts in full.  Relay refusals are the git relay's own check rows (kinds gr_bearer, gr_assert)."""
    alarms = {}
    for r in doc.get("rows", []):
        p = r.get("problem")
        if p and p != "ok":
            miss = (
                (r.get("missingRecords") or {}).get("names")
                if isinstance(r.get("missingRecords"), dict)
                else None
            )
            extra = f" (missing {', '.join(miss)})" if miss else ""
            alarms[f"problem:{r.get('identity')}:{p}"] = f"{r.get('identity')}: {p}{extra}"
    counts = {}
    n401 = nref = 0
    for d in doc.get("diagnostics") or []:
        c = d.get("count")
        if not isinstance(c, int) or c < 0:
            continue
        kind, reason = str(d.get("kind") or ""), str(d.get("reason") or "")
        key = f"{kind}|{reason}|{d.get('seat') or ''}"
        counts[key] = c
        if last_counts is None:
            continue
        prev = last_counts.get(key, 0)
        grew = c - prev if c >= prev else c
        if reason == "mcp_http_401":
            n401 += grew
        elif kind in ("gr_bearer", "gr_assert"):
            nref += grew
    if n401:
        alarms["delta:mcp_http_401"] = f"{n401} new /mcp 401 responses since the last poll"
    if nref:
        alarms["delta:relay_refusal"] = f"{nref} new relay refusals since the last poll"
    return alarms, counts


def reconcile(state, alarms, now):
    """Update state in place;  return (to_notify [(key, text)], recovered [key])."""
    active = state.setdefault("active", {})
    notify, recovered = [], []
    for key, text in alarms.items():
        cur = active.get(key)
        if cur is None:
            active[key] = {"since": now, "lastAlarm": now, "summary": text}
            notify.append((key, text))
        else:
            cur["summary"] = text
            if now - cur.get("lastAlarm", 0) >= REALARM_S:
                cur["lastAlarm"] = now
                notify.append((key, text))
    for key in [k for k in active if k not in alarms]:
        # Growth keys are edge events:  they clear on the next quiet poll.
        if not key.startswith("delta:"):
            recovered.append(key)
        del active[key]
    return notify, recovered


def notify_mac(title, text):
    safe = lambda s: str(s).replace("\\", " ").replace('"', "'")[:200]
    try:
        subprocess.run(
            [
                "/usr/bin/osascript",
                "-e",
                f'display notification "{safe(text)}" with title "{safe(title)}"',
            ],
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def write_atomic(path, doc):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(doc, f, indent=1)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(os.path.dirname(path), os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def run(args, now=None, token_fn=access_token, fetch_fn=fetch, notify_fn=notify_mac):
    now = time.time() if now is None else now
    try:
        with open(args.state) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    doc, error, alarms = None, None, {}
    token = token_fn()
    if not token:
        error = "access_login_needed"
        alarms["access:login_needed"] = (
            "Muster cannot read bearer status: run  cloudflared access login https://agents.simplewithus.com/admin  on the Mac"
        )
    else:
        try:
            doc = fetch_fn(args.url, token)
        except urllib.error.HTTPError as e:
            error = f"http_{e.code}"
            if e.code in (301, 302, 303, 307, 308, 401, 403):
                alarms["access:login_needed"] = (
                    "The owner Access login was refused or expired: run  cloudflared access login https://agents.simplewithus.com/admin"
                )
            else:
                alarms["access:status_unreachable"] = (
                    f"The bearer status API answered HTTP {e.code}"
                )
        except (OSError, ValueError):
            error = "unreachable"
            alarms["access:status_unreachable"] = "The bearer status API could not be read"
            # A network failure says nothing about the login:  an alarm already raised for it stays.
            prior = (state.get("active") or {}).get("access:login_needed")
            if isinstance(prior, dict):
                alarms.setdefault("access:login_needed", prior.get("summary", "access:login_needed"))
    if doc is not None:
        found, counts = desired_alarms(doc, state.get("diagCounts"))
        alarms.update(found)
        if doc.get("diagnostics") is not None:
            # Merge, never overwrite:  a row that drops out of the API's tail for one poll keeps its baseline,
            # so its lifetime count is not read as growth when it returns.
            merged = dict(state.get("diagCounts") or {})
            merged.update(counts)
            state["diagCounts"] = merged
    elif state.get("active"):
        # An unreadable poll must not clear identity alarms:  carry them over.
        for k, v in state["active"].items():
            if k.startswith("problem:"):
                alarms.setdefault(k, v.get("summary", k))
    to_notify, recovered = reconcile(state, alarms, now)
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
    out = {
        "schemaVersion": 1,
        "tool": "fleet-bearer-status",
        "generatedAt": iso(now),
        "ok": doc is not None,
        "rows": [row_summary(r) for r in (doc or {}).get("rows", [])],
        "alarms": [
            {
                "key": k,
                "summary": v.get("summary"),
                "since": iso(v["since"]),
                "lastAlarm": iso(v["lastAlarm"]),
            }
            for k, v in sorted(state.get("active", {}).items())
        ],
        "recovered": [{"key": k, "at": iso(now)} for k in recovered],
    }
    if error:
        out["error"] = error
    if not args.dry_run:
        write_atomic(args.write, out)
        write_atomic(args.state, state)
        if not args.no_notify:
            for _k, text in to_notify:
                notify_fn("Fleet Tools bearer alarm", text)
            for k in recovered:
                notify_fn("Fleet Tools bearer recovered", k)
    return out


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", default=OUT)
    ap.add_argument("--state", default=STATE)
    ap.add_argument("--url", default=URL)
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    out = run(ap.parse_args(argv))
    print(
        json.dumps(
            {"ok": out["ok"], "alarms": len(out["alarms"]), "recovered": len(out["recovered"])}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
