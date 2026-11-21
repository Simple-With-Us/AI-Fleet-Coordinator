"""The thin Mac wake alerter:  AppleScript banners for wake events, with the
listener daemon hosted away (Hetzner) instead of on this Mac.

The hosted listener records every wake in its ledger and serves it through the
signed read API (listener_api.py).  This client polls ONE record -

    GET /listener/v1/<SEAT>/wakes?since_hours=1&limit=50

signed with the seat's routine HMAC key (the same key the daemon and the
Worker already share), remembers the wake ids it has already handled in a
small state file, and reacts to each new wake event one of two ways:

* default:  pop one macOS notification via the existing
  adapters.osascript_argv (bodies never reach a banner) - the Mac client.
* hook mode (AGENT_SYNC_ALERTER_HOOK set):  run one configured command per
  new wake instead - the headless client for seats hosted off the Mac (for
  example launching Muse Code headlessly on the Hetzner box, see
  docs/agent-sync-listener-hetzner.md).  The hook receives wake metadata
  only ({seat} {channel} {topic} {wake_id} {state} placeholders in the
  command plus AGENT_SYNC_WAKE_* env vars);  bodies never reach it.  A wake
  id is marked seen only after the hook exits 0, so a failed hook retries
  on the next poll.

Either way it holds no Zulip key, no Infisical access and no listener state:
if it is down, the events wait in the ledger and are handled on its return;
if the server is down, the next poll retries.

One-shot by default so launchd owns the cadence (StartInterval);  --loop N
runs forever for a supervisor that prefers a resident process.

Env:
    AGENT_SYNC_SEAT            seat name (for example ECHO)
    AGENT_SYNC_LISTENER_URL    tailnet base URL, for example
                               http://server.boa-roygbiv.ts.net:8788
    <CODE>_ROUTINE_KEY         the seat's routine HMAC key (name overridable
                               with AGENT_SYNC_ROUTINE_KEY_ENV)
    AGENT_SYNC_ALERTER_STATE   state file path (default
                               ~/.local/state/agent-sync/wake-alerter.json)

Python 3.9+ (the Mac's /usr/bin/python3), standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

# The signing and banner helpers below are vendored here (instead of importing
# listener_api.sign and adapters.osascript_argv) so this client runs on the
# Mac's /usr/bin/python3 (3.9) under `python3 -I`:  those packages are Python
# 3.11+ and pull in Zulip and textfmt transitively.  Keep the wire format and
# banner text identical to listener_api.CONTRACT/sign and
# adapters.osascript_argv/clean_banner.
READ_CONTRACT = "agent-sync-read/1"
_NOTIFY_CUT = 120
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def sign(key: str, path: str, query: str, timestamp: str) -> str:
    body = ("%s\nGET\n%s\n%s\n%s" % (READ_CONTRACT, path, query, timestamp)).encode("utf-8")
    return hmac.new(key.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _clean_banner(text: object, limit: int = _NOTIFY_CUT) -> str:
    cleaned = _CONTROL_RE.sub(" ", str(text))
    cleaned = " ".join(cleaned.split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


def osascript_argv(osascript: str, text: str, title: str) -> list[str]:
    return [
        osascript,
        "-e",
        "on run argv",
        "-e",
        "display notification (item 1 of argv) with title (item 2 of argv)",
        "-e",
        "end run",
        "--",
        _clean_banner(text),
        _clean_banner(title),
    ]


QUERY = "since_hours=1&limit=50"
MAX_SEEN_WAKE_IDS = 500
REQUEST_TIMEOUT_SECONDS = 10
HOOK_TIMEOUT_SECONDS = int(os.environ.get("AGENT_SYNC_ALERTER_HOOK_TIMEOUT") or "600")
_HOOK_FIELDS = ("seat", "channel", "topic", "wake_id", "state")


def _hook_argv(template: str, seat: str, item: dict) -> list[str]:
    import shlex

    values = {"seat": seat}
    for field in _HOOK_FIELDS[1:]:
        values[field] = item.get(field)
        if not isinstance(values[field], str):
            values[field] = ""
    quoted = {k: shlex.quote(v) for k, v in values.items()}
    return shlex.split(template.format(**quoted))


def _hook_env(item: dict) -> dict:
    env = dict(os.environ)
    env["AGENT_SYNC_WAKE_ID"] = item.get("wake_id") or ""
    env["AGENT_SYNC_WAKE_CHANNEL"] = item.get("channel") or ""
    env["AGENT_SYNC_WAKE_TOPIC"] = item.get("topic") or ""
    env["AGENT_SYNC_WAKE_STATE"] = item.get("state") or ""
    return env


def _default_state_path() -> str:
    return os.path.expanduser("~/.local/state/agent-sync/wake-alerter.json")


def _load_seen(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    seen = data.get("seen")
    return (
        [w for w in seen if isinstance(w, str)][:MAX_SEEN_WAKE_IDS]
        if isinstance(seen, list)
        else []
    )


def _save_seen(path: str, seen: list[str]) -> None:
    tmp = path + ".tmp"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"seen": seen[-MAX_SEEN_WAKE_IDS:]}, fh)
    os.replace(tmp, path)


def _banner_text(item: dict) -> str:
    where = item.get("channel") or ("DM" if item.get("dm") else "unknown")
    topic = item.get("topic")
    if where and topic:
        return "wake in %s / %s" % (where, topic)
    return "wake in %s" % (where or "unknown")


def poll_once(
    base_url: str,
    seat: str,
    key: str,
    state_path: str,
    *,
    runner,
    clock=time.time,
    opener=urllib.request.urlopen,
    hook: str = "",
) -> int:
    """One poll:  handle every wake event not already in the state file.

    With no hook each new wake gets one macOS banner;  with a hook template
    each new wake runs the hook command instead, and only a hook that exits 0
    marks its wake seen (a failed hook retries on the next poll).  Returns the
    number of new wakes handled.  Raises nothing for transport or HTTP
    failures - a failed poll simply handles nothing and the next one retries.
    """
    path = "/listener/v1/" + seat + "/wakes"
    timestamp = str(int(clock()))
    req = urllib.request.Request(
        base_url.rstrip("/") + path + "?" + QUERY,
        headers={
            "X-Agent-Sync-Timestamp": timestamp,
            "X-Agent-Sync-Signature": sign(key, path, QUERY, timestamp),
        },
    )
    try:
        with opener(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read(1024 * 1024))
    except (urllib.error.URLError, OSError, ValueError):
        return 0
    items = ((payload.get("view") or {}).get("items")) or []
    seen = _load_seen(state_path)
    fresh = []
    for item in items:
        if not isinstance(item, dict):
            continue
        wake_id = item.get("wake_id")
        if isinstance(wake_id, str) and wake_id and wake_id not in seen:
            fresh.append((wake_id, item))
    handled = 0
    for wake_id, item in fresh:
        if hook:
            argv = _hook_argv(hook, seat, item)
            if runner(argv, env=_hook_env(item), timeout=HOOK_TIMEOUT_SECONDS) != 0:
                continue
        else:
            runner(
                osascript_argv(
                    "/usr/bin/osascript", _banner_text(item), "agent-sync wake (%s)" % seat
                )
            )
        seen.append(wake_id)
        handled += 1
    if handled:
        try:
            _save_seen(state_path, seen)
        except OSError as exc:
            print(
                "wake_alerter: state write failed (%s); will retry next poll" % exc, file=sys.stderr
            )
    return handled


def _run(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="wake_alerter")
    parser.add_argument(
        "--loop", type=float, default=0.0, help="poll every N seconds forever instead of one shot"
    )
    args = parser.parse_args(argv)

    seat = (os.environ.get("AGENT_SYNC_SEAT") or "").strip().upper()
    base_url = (os.environ.get("AGENT_SYNC_LISTENER_URL") or "").strip()
    key_env = os.environ.get("AGENT_SYNC_ROUTINE_KEY_ENV") or (
        seat.replace("-", "_") + "_ROUTINE_KEY" if seat else ""
    )
    key = os.environ.get(key_env) if key_env else None
    if not seat or not base_url or not key:
        print(
            "wake_alerter: AGENT_SYNC_SEAT, AGENT_SYNC_LISTENER_URL and "
            "<CODE>_ROUTINE_KEY are required",
            file=sys.stderr,
        )
        return 2
    state_path = os.environ.get("AGENT_SYNC_ALERTER_STATE") or _default_state_path()

    import subprocess

    hook = (os.environ.get("AGENT_SYNC_ALERTER_HOOK") or "").strip()

    def banner_runner(argv_, **_kw):
        try:
            subprocess.run(argv_, capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
        return 0

    def hook_runner(argv_, env=None, timeout=HOOK_TIMEOUT_SECONDS):
        try:
            done = subprocess.run(argv_, capture_output=True, timeout=timeout, check=False, env=env)
        except (OSError, subprocess.SubprocessError):
            return 1
        return done.returncode

    runner = hook_runner if hook else banner_runner
    if args.loop <= 0:
        poll_once(base_url, seat, key, state_path, runner=runner, hook=hook)
        return 0
    while True:
        poll_once(base_url, seat, key, state_path, runner=runner, hook=hook)
        time.sleep(args.loop)


def main() -> None:
    sys.exit(_run())


if __name__ == "__main__":
    main()
