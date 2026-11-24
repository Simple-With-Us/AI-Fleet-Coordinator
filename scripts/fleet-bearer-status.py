#!/usr/bin/env python3
"""Poll the owner bearer status API and raise alarms for Muster.

Every run (the LaunchAgent com.jay.fleet-bearer-status, every 5 minutes) reads
GET https://agents.simplewithus.com/admin/api/bearers with the Access service token "muster-admin-api" when ~/.config/fleet-tools/muster-admin-api.json exists (user-only,
{"clientId", "clientSecret"}), else with the owner's own Cloudflare Access login
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
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

URL = "https://agents.simplewithus.com/admin/api/bearers"
SEAT_TOOLS_URL = "https://agents.simplewithus.com/admin/api/seat-tools"
# (seat, tool) pairs that are known gaps under review:  shown as findings in the output, never notified, never an alarm.
# Remove an entry when its owner decision lands (then a missing state alarms like any other).
OPEN_FINDINGS = frozenset({("WISE", "wake"), ("GROK-WEB", "wake")})
SEAT_TOOL_STATES = frozenset({"ok", "missing", "unavailable", "not_applicable"})
APP = "https://agents.simplewithus.com/admin"
OUT = os.path.expanduser("~/.local/state/fleet-tools/bearer-status.json")
STATE = os.path.expanduser("~/.local/state/fleet-tools/bearer-alarm-state.json")
REALARM_S = 6 * 3600
CLOUDFLARED = ("/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared")


SERVICE_FILE = os.path.expanduser("~/.config/fleet-tools/muster-admin-api.json")


def _header_safe(value):
    """A non-empty string of printable ASCII with no spaces, so it can never split or inject an HTTP header."""
    return isinstance(value, str) and bool(value) and all(0x21 <= ord(c) <= 0x7E for c in value)


def service_token(path=None):
    """(client id, client secret) of the Cloudflare Access service token "muster-admin-api", or None.

    Read from a user-only file ({"clientId", "clientSecret"}).  The file must be a regular file owned by this user
    with no group or other permission bits, so a loose file is ignored rather than trusted.  The Worker accepts
    this token only on /admin/api/* and only for the client ids it lists.  Values are never printed or stored
    anywhere else."""
    path = SERVICE_FILE if path is None else path
    try:
        # Open first (never through a symlink), then check the open descriptor, so the file that is
        # validated is the file that is read.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd) as f:
            st = os.fstat(f.fileno())
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
                return None
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    cid = doc.get("clientId") if isinstance(doc, dict) else None
    sec = doc.get("clientSecret") if isinstance(doc, dict) else None
    if _header_safe(cid) and _header_safe(sec):
        return (cid, sec)
    return None


def credentials():
    """The service token when its file is present, else the owner's Access JWT from cloudflared."""
    return service_token() or access_token()


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
    if isinstance(token, tuple):
        auth = {"CF-Access-Client-Id": token[0], "CF-Access-Client-Secret": token[1]}
    else:
        auth = {"cf-access-token": token}
    req = urllib.request.Request(
        url,
        headers={
            **auth,
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
    counts = {}
    for r in doc.get("rows", []):
        p = r.get("problem")
        ident = r.get("identity")
        if p == "wrong_secret_attempts":
            # The count is a lifetime total with no reset, so a standing alarm could never clear.  Alarm on growth
            # only; a seat with no earlier baseline just records one.
            mm = (r.get("mismatches") or {}).get("count")
            if isinstance(mm, int) and not isinstance(mm, bool) and mm >= 0:
                key = f"mismatch|{ident}"
                counts[key] = mm
                if last_counts is not None and key in last_counts:
                    prev = last_counts[key]
                    grew = mm - prev if mm >= prev else mm
                    if grew:
                        alarms[f"delta:wrong_secret:{ident}"] = (
                            f"{ident}: {grew} new wrong-secret attempts since the last poll"
                        )
                continue
            # No usable count:  fall through to the generic alarm so the fault signal never disappears.
        if p == "no_bearer" and r.get("kind") == "hosted":
            # A hosted seat connects through its OAuth client; a bearer is optional for it, so none is not a fault.
            continue
        if p and p != "ok":
            miss = (
                (r.get("missingRecords") or {}).get("names")
                if isinstance(r.get("missingRecords"), dict)
                else None
            )
            extra = f" (missing {', '.join(miss)})" if miss else ""
            alarms[f"problem:{r.get('identity')}:{p}"] = f"{r.get('identity')}: {p}{extra}"
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


def _dict_items(value):
    """The dict members of a list, or nothing:  a null or odd shape from the API must not abort the poll."""
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


def seat_tool_alarms(doc, prev_unavailable):
    """(alarms {key: summary}, findings [{seat, tool, state}], unavailable [seat|tool]) from GET /admin/api/seat-tools.

    A `missing` tool alarms (credentials absent or tool unconfigured) unless the pair is an open finding.  An
    `unavailable` state means the Worker could not read the store this time, so it alarms only when the previous poll was
    also unavailable (a one-poll blip is not an alarm).  `not_applicable` and unknown states never alarm.  Alarm keys are
    "seat-tool:<seat>:<tool>:<state>";  the reconcile step already de-duplicates and re-alarms every 6 hours."""
    alarms, findings, unavailable = {}, [], []
    prev = set(prev_unavailable or []) if isinstance(prev_unavailable, list) else set()
    for r in _dict_items((doc or {}).get("rows")):
        seat = r.get("seat")
        if not isinstance(seat, str):
            continue
        for t in _dict_items(r.get("tools")):
            tool, state = t.get("tool"), t.get("state")
            if not isinstance(tool, str) or state not in ("missing", "unavailable"):
                continue
            if (seat, tool) in OPEN_FINDINGS:
                findings.append({"seat": seat, "tool": tool, "state": state})
                continue
            if state == "unavailable":
                tag = f"{seat}|{tool}"
                unavailable.append(tag)
                if tag not in prev:
                    continue
            alarms[f"seat-tool:{seat}:{tool}:{state}"] = f"{seat}: {tool} {state}"
    return alarms, findings, unavailable


def seat_tool_rows(doc):
    out = []
    for r in _dict_items((doc or {}).get("rows")):
        if isinstance(r.get("seat"), str):
            out.append(
                {
                    "seat": r["seat"],
                    "problem": r.get("problem"),
                    "tools": {
                        t.get("tool"): t.get("state")
                        if t.get("state") in SEAT_TOOL_STATES
                        else "unknown"
                        for t in _dict_items(r.get("tools"))
                        if isinstance(t.get("tool"), str)
                    },
                }
            )
    return out


def reconcile(state, alarms, now):
    """Update state in place;  return (to_notify [(key, text)], recovered [key])."""
    if not isinstance(state.get("active"), dict):
        state["active"] = {}
    active = state["active"]
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


def run(args, now=None, token_fn=credentials, fetch_fn=fetch, notify_fn=notify_mac):
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
                alarms.setdefault(
                    "access:login_needed", prior.get("summary", "access:login_needed")
                )
    seat_doc, seat_error, findings = None, None, []
    st_url = getattr(args, "seat_tools_url", None)
    if doc is not None and st_url:
        try:
            seat_doc = fetch_fn(st_url, token)
        except (OSError, ValueError) as e:
            seat_error = (
                f"http_{e.code}" if isinstance(e, urllib.error.HTTPError) else "unreachable"
            )
        if seat_doc is not None and not isinstance(seat_doc, dict):
            seat_doc, seat_error = None, "malformed"
        if seat_doc is not None:
            found, findings, unavailable = seat_tool_alarms(
                seat_doc, state.get("seatToolsUnavailable")
            )
            alarms.update(found)
            state["seatToolsUnavailable"] = unavailable
            state["seatToolsLast"] = {
                "seatTools": seat_tool_rows(seat_doc),
                "findings": findings,
            }
        else:
            # An unreadable poll must not clear seat-tool alarms:  carry them over.  It also breaks the run of
            # consecutive `unavailable` polls, so the baseline is reset.
            state["seatToolsUnavailable"] = []
            for k, v in (state.get("active") or {}).items():
                if k.startswith("seat-tool:"):
                    alarms.setdefault(k, v.get("summary", k))
    if doc is not None:
        found, counts = desired_alarms(doc, state.get("diagCounts"))
        alarms.update(found)
        if doc.get("diagnostics") is not None or counts:
            # Merge, never overwrite:  a row that drops out of the API's tail for one poll keeps its baseline,
            # so its lifetime count is not read as growth when it returns.
            merged = dict(state.get("diagCounts") or {})
            merged.update(counts)
            state["diagCounts"] = merged
    elif state.get("active"):
        # An unreadable poll must not clear identity or seat-tool alarms:  carry them over.
        for k, v in state["active"].items():
            if k.startswith(("problem:", "seat-tool:")):
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
    if st_url and doc is not None:
        # An unreadable poll keeps the last good view, so the output never contradicts the alarms it carries.
        last = state.get("seatToolsLast") if isinstance(state.get("seatToolsLast"), dict) else {}
        out["seatTools"] = (
            seat_tool_rows(seat_doc) if seat_doc is not None else last.get("seatTools", [])
        )
        out["findings"] = findings if seat_doc is not None else last.get("findings", [])
        if seat_error:
            out["seatToolsError"] = seat_error
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
    ap.add_argument("--seat-tools-url", default=SEAT_TOOLS_URL)
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
