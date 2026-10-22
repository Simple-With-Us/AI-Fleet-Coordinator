"""The thin Mac wake alerter:  AppleScript banners for wake events, with the
listener daemon hosted away (Hetzner) instead of on this Mac.

The hosted listener records every wake in its ledger and serves it through the
signed read API (listener_api.py).  This client polls ONE record -

    GET /listener/v1/<SEAT>/wakes?since_hours=1&limit=50

signed with the seat's routine HMAC key (the same key the daemon and the
Worker already share), remembers the wake ids it has already bannered in a
small state file, and pops one macOS notification per new wake event via the
existing adapters.osascript_argv (bodies never reach a banner).  It holds no
Zulip key, no Infisical access and no listener state:  if it is down, the
events wait in the ledger and are bannered on its return;  if the server is
down, the next poll retries.

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
import json
import os
import sys
import time
import urllib.error
import urllib.request

from .adapters import osascript_argv
from .listener_api import sign

QUERY = "since_hours=1&limit=50"
MAX_SEEN_WAKE_IDS = 500
REQUEST_TIMEOUT_SECONDS = 10


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
) -> int:
    """One poll:  banner every wake event not already in the state file.

    Returns the number of new banners.  Raises nothing for transport or HTTP
    failures - a failed poll simply banners nothing and the next one retries.
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
    for wake_id, item in fresh:
        runner(
            osascript_argv("/usr/bin/osascript", _banner_text(item), "agent-sync wake (%s)" % seat)
        )
        seen.append(wake_id)
    if fresh:
        _save_seen(state_path, seen)
    return len(fresh)


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

    def runner(argv_):
        try:
            subprocess.run(argv_, capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            pass

    if args.loop <= 0:
        poll_once(base_url, seat, key, state_path, runner=runner)
        return 0
    while True:
        poll_once(base_url, seat, key, state_path, runner=runner)
        time.sleep(args.loop)


def main() -> None:
    sys.exit(_run())


if __name__ == "__main__":
    main()
