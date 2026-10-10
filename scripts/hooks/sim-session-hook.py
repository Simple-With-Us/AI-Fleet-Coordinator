#!/usr/bin/env python3
"""Claude Code hook: shut down the iOS Simulators a session booted when its turn ends.

Owner rule (2026-10-09):  never end a turn with a simulator still booted.  This hook enforces it
for Claude Code;  the LaunchAgent com.jay.sim-idle-reaper (scripts/sim-idle-reaper.py) is the
backstop for every seat.

A simulator boots implicitly as often as by `simctl boot` (`xcodebuild test -destination`, the
desktop app's iOS Simulator MCP tools, ios-debug.sh), so ownership comes from a booted-set diff
around each tool call, never from parsing a boot command:

  PreToolUse           a Bash command that can boot a simulator (BOOTISH), or any iOS Simulator
                       MCP tool:  snapshot the booted set to pre/<session>__<tool_use_id>.json.
                       The set is read from each device.plist `state` (milliseconds), not from
                       `simctl list`, which takes 0.5-1.5s on this Mac under load.
  PostToolUse          re-read, diff, and record every NEW device as owned by (session_id,
  PostToolUseFailure   agent_id or "main") in sessions/<session_id>.json.  The first claim
                       wins:  a device another session claimed after this snapshot is not taken.
                       An iOS Simulator MCP `control` call with action attach or launch marks
                       the session watched (the owner is looking at the live panel).
  Stop                 shut down the devices the MAIN agent owns.  Subagent-owned devices are
                       left alone, because a background worker may still be using one.
  SubagentStop         the same, for the devices that subagent (agent_id) owns.
  SessionEnd           shut down everything the session owns, watched or not;  delete its file.

Stop and SubagentStop do nothing for a watched session, and the detached worker defers while
an xcodebuild or xctest runs or Simulator.app is open;  SessionEnd and the reaper catch those.
Shutdowns run in a detached worker (`--worker`), so the hook itself returns at once.

Never `simctl shutdown all`, never erase or delete.  Fails open:  any error exits 0, stdout is
never written, and SIM_REAPER_HOOKS=0 disables it.  Stdlib only.  Installed to ~/.claude/hooks/
by scripts/install-sim-idle-reaper.py, behind the shell prefilter sim-session-hook.sh.
Log:  ~/Library/Logs/sim-session-hook.log.
"""
from __future__ import annotations

import fcntl
import json
import os
import plistlib
import re
import subprocess
import sys
import time

HOME = os.path.expanduser("~")
STATE_DIR = os.environ.get("SIM_REAPER_STATE_DIR") or os.path.join(
    HOME, "Library", "Application Support", "sim-idle-reaper")
DEVICES_DIR = os.environ.get("SIM_REAPER_DEVICES_DIR") or os.path.join(
    HOME, "Library", "Developer", "CoreSimulator", "Devices")
LOG_PATH = os.environ.get("SIM_REAPER_HOOK_LOG") or os.path.join(
    HOME, "Library", "Logs", "sim-session-hook.log")
XCRUN = os.environ.get("SIM_REAPER_XCRUN") or "/usr/bin/xcrun"
PGREP = "/usr/bin/pgrep"

MCP_PREFIX = "mcp__Claude_Code_iOS_Simulator__"
WATCH_ACTIONS = ("attach", "launch")          # `launch` re-attaches the live panel too
# Bash commands that can boot a simulator.  Read-only simctl calls (list, listapps, io, spawn,
# shutdown) do not match, so a concurrent `simctl list` in another session cannot claim a boot.
BOOTISH = re.compile(r"xcodebuild|\bboot(?:status)?\b|\bSimulator\b|ios-debug|run-ios|run:ios|"
                     r"fastlane|xctest")
BOOTED_STATES = (2, 3)                         # device.plist state: 1 Shutdown, 2 Booting, 3 Booted
PRE_MAX_AGE = 3600                             # stale PreToolUse snapshots are pruned after this
LOCK_WAIT = 0.5
LOG_MAX = 512 * 1024
_SAFE = re.compile(r"[^A-Za-z0-9._-]")
_UDID = re.compile(r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")


def safe(value) -> str:
    return _SAFE.sub("_", str(value or ""))[:128] or "unknown"


def sessions_dir() -> str:
    return os.path.join(STATE_DIR, "sessions")


def session_path(session_id) -> str:
    return os.path.join(sessions_dir(), safe(session_id) + ".json")


def pre_path(session_id, tool_use_id) -> str:
    return os.path.join(STATE_DIR, "pre", f"{safe(session_id)}__{safe(tool_use_id)}.json")


def log(line: str) -> None:
    try:
        os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
        try:
            if os.path.getsize(LOG_PATH) > LOG_MAX:
                os.replace(LOG_PATH, LOG_PATH + ".1")
        except OSError:
            pass
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {line}\n")
    except Exception:
        pass


def load_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        return doc if isinstance(doc, type(default)) or default is None else default
    except Exception:
        return default


def write_json(path: str, doc) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


class StateLock:
    """One flock for every read-modify-write of session files (hook, worker and reaper)."""

    def __init__(self, wait: float = LOCK_WAIT):
        self.wait = wait
        self.fh = None

    def __enter__(self):
        os.makedirs(STATE_DIR, exist_ok=True)
        self.fh = open(os.path.join(STATE_DIR, "sessions.lock"), "a")
        deadline = time.monotonic() + self.wait
        while True:
            try:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError:
                if time.monotonic() >= deadline:
                    self.fh.close()
                    raise TimeoutError("sessions.lock busy")
                time.sleep(0.02)

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fh.fileno(), fcntl.LOCK_UN)
        finally:
            self.fh.close()
        return False


def booted_devices(devices_dir: str | None = None) -> dict:
    """{udid: name} for every device whose device.plist says Booting or Booted."""
    root = devices_dir or DEVICES_DIR
    out: dict = {}
    try:
        names = os.listdir(root)
    except OSError:
        return out
    for name in names:
        if not _UDID.match(name):
            continue
        try:
            with open(os.path.join(root, name, "device.plist"), "rb") as fh:
                pl = plistlib.load(fh)
        except Exception:
            continue
        if pl.get("state") in BOOTED_STATES and not pl.get("isDeleted"):
            out[name] = str(pl.get("name") or "")
    return out


def relevant(payload: dict) -> bool:
    tool = str(payload.get("tool_name") or "")
    if tool.startswith(MCP_PREFIX):
        return True
    if tool == "Bash":
        cmd = (payload.get("tool_input") or {}).get("command")
        return isinstance(cmd, str) and bool(BOOTISH.search(cmd))
    return False


def agent_of(payload: dict) -> str:
    return str(payload.get("agent_id") or "") or "main"


def newest_other_claim(udid: str, me: str):
    """Newest claim time on udid by any session other than `me`, or None."""
    best = None
    mine = safe(me) + ".json"
    try:
        names = os.listdir(sessions_dir())
    except OSError:
        return None
    for fn in names:
        if not fn.endswith(".json") or fn == mine:
            continue
        ent = (load_json(os.path.join(sessions_dir(), fn), {}).get("udids") or {}).get(udid)
        if isinstance(ent, dict):
            try:
                t = float(ent.get("t") or 0)
            except (TypeError, ValueError):
                continue
            best = t if best is None else max(best, t)
    return best


def prune_pre(now: float) -> None:
    d = os.path.join(STATE_DIR, "pre")
    try:
        for fn in os.listdir(d):
            p = os.path.join(d, fn)
            try:
                if now - os.path.getmtime(p) > PRE_MAX_AGE:
                    os.remove(p)
            except OSError:
                pass
    except OSError:
        pass


def on_pre(payload: dict) -> None:
    if not relevant(payload):
        return
    sid, tuid = payload.get("session_id"), payload.get("tool_use_id")
    if not sid or not tuid:
        return
    write_json(pre_path(sid, tuid), {"t": time.time(), "booted": sorted(booted_devices())})


def on_post(payload: dict) -> None:
    sid, tuid = payload.get("session_id"), payload.get("tool_use_id")
    if not sid or not tuid:
        return
    path = pre_path(sid, tuid)
    if not os.path.exists(path):
        return                                   # no snapshot: attribute nothing
    snap = load_json(path, {})
    try:
        os.remove(path)
    except OSError:
        pass
    now = time.time()
    prune_pre(now)
    try:
        pre_t = float(snap.get("t"))
    except (TypeError, ValueError):
        return
    before = set(snap.get("booted") or [])
    booted = booted_devices()
    new = [u for u in sorted(booted) if u not in before]

    tool = str(payload.get("tool_name") or "")
    tin = payload.get("tool_input") or {}
    watch: list = []
    if tool == MCP_PREFIX + "control" and str(tin.get("action") or "") in WATCH_ACTIONS:
        target = str(tin.get("device") or tin.get("udid") or "")
        watch = [u for u, n in booted.items() if target and (u.lower() == target.lower() or n == target)]
        watch = watch or sorted(booted)          # unknown target: treat every booted device as watched
    if not new and not watch:
        return

    agent = agent_of(payload)
    with StateLock():
        spath = session_path(sid)
        doc = load_json(spath, {})
        udids = doc.setdefault("udids", {})
        claimed = 0
        for u in new:
            other = newest_other_claim(u, sid)
            if other is not None and other >= pre_t:
                log(f"skip-claim {u} session={safe(sid)[:8]} agent={agent}: another session claimed it first")
                continue
            udids[u] = {"agent": agent, "t": now, "name": booted.get(u, ""), "tool": tool}
            claimed += 1
            log(f"own {u} \"{booted.get(u, '')}\" session={safe(sid)[:8]} agent={agent} tool={tool}")
        if not claimed and not watch:
            return
        if watch:
            w = doc.setdefault("watched", {})
            for u in watch:
                w[u] = now
            log(f"watched {','.join(watch)} session={safe(sid)[:8]} ({tin.get('action')})")
        doc["session_id"] = str(sid)
        doc["updated"] = now
        write_json(spath, doc)


def spawn_worker(event: str, sid: str, agent: str) -> None:
    args = [sys.executable or "python3", os.path.abspath(__file__), "--worker", event, str(sid), agent]
    with open(os.devnull, "rb") as dn_in, open(os.devnull, "wb") as dn_out:
        subprocess.Popen(args, stdin=dn_in, stdout=dn_out, stderr=dn_out,
                         start_new_session=True, close_fds=True)


def on_stop(payload: dict, event: str) -> None:
    sid = payload.get("session_id")
    if not sid:
        return
    path = session_path(sid)
    if not os.path.exists(path):
        return
    if event == "SessionEnd":
        spawn_worker(event, sid, "*")
        return
    doc = load_json(path, {})
    if doc.get("watched"):
        return                                   # owner is watching: SessionEnd and the reaper
    agent = "main" if event == "Stop" else str(payload.get("agent_id") or "")
    if not agent:
        return
    if any(isinstance(e, dict) and e.get("agent") == agent for e in (doc.get("udids") or {}).values()):
        spawn_worker(event, sid, agent)


def running(*names: str) -> list:
    out = []
    for n in names:
        try:
            if subprocess.run([PGREP, "-x", n], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                              timeout=5).returncode == 0:
                out.append(n)
        except Exception:
            out.append(n)                         # cannot tell: assume busy
    return out


def shutdown(udid: str) -> int:
    try:
        return subprocess.run([XCRUN, "simctl", "shutdown", udid], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120).returncode
    except Exception:
        return -1


def run_worker(event: str, sid: str, agent: str) -> None:
    path = session_path(sid)
    tag = f"{event} session={safe(sid)[:8]} agent={agent}"
    with StateLock(wait=10):
        doc = load_json(path, {})
    udids = doc.get("udids") or {}
    targets = {u: e for u, e in udids.items()
               if isinstance(e, dict) and (agent == "*" or e.get("agent") == agent)}
    if event != "SessionEnd":
        if doc.get("watched") or not targets:
            return
        busy = running("xcodebuild", "xctest", "Simulator")
        if busy:
            log(f"defer {tag} {len(targets)} device(s): {'/'.join(busy)} running")
            return
    booted = booted_devices()
    done = []
    for u, e in sorted(targets.items()):
        if u not in booted:
            done.append(u)
            log(f"gone {u} {tag}: already shut down")
            continue
        other = newest_other_claim(u, sid)
        try:
            mine_t = float(e.get("t") or 0)
        except (TypeError, ValueError):
            mine_t = 0.0
        if other is not None and other > mine_t:
            done.append(u)
            log(f"release {u} {tag}: re-booted and claimed by another session")
            continue
        rc = shutdown(u)
        log(f"shutdown {u} \"{e.get('name', '')}\" {tag} rc={rc}")
        if rc == 0:
            done.append(u)
    with StateLock(wait=10):
        if event == "SessionEnd":
            try:
                os.remove(path)
            except OSError:
                pass
            return
        doc = load_json(path, {})
        cur = doc.get("udids") or {}
        for u in done:
            if u in cur and cur[u] == targets.get(u):
                del cur[u]
        if cur or doc.get("watched"):
            doc["udids"] = cur
            doc["updated"] = time.time()
            write_json(path, doc)
        else:
            try:
                os.remove(path)
            except OSError:
                pass


def main(argv: list) -> int:
    if os.environ.get("SIM_REAPER_HOOKS", "1") == "0":
        return 0
    if len(argv) >= 5 and argv[1] == "--worker":
        try:
            run_worker(argv[2], argv[3], argv[4])
        except Exception as exc:                  # never raise; leave a trace for diagnosis
            log(f"worker error {type(exc).__name__}")
        return 0
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0
    if not isinstance(payload, dict):
        return 0
    event = str(payload.get("hook_event_name") or "")
    try:
        if event == "PreToolUse":
            on_pre(payload)
        elif event in ("PostToolUse", "PostToolUseFailure"):
            on_post(payload)
        elif event in ("Stop", "SubagentStop", "SessionEnd"):
            on_stop(payload, event)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        main(sys.argv)
    except BaseException:
        pass
    sys.exit(0)
