#!/usr/bin/env python3
"""Unload MCP tool processes from Grok chats nobody is using.

session/close unloads that chat's MCP tool processes.  Disk history
(summary.json / updates.jsonl) is not deleted.  Resume via /resume
(or grok --resume ID) — tools come back on the next turn.

A chat is a candidate only when it is LOADED: the shared Grok leader holds
MCP child processes that carry its GROK_SESSION_ID (read from the process
table with sysctl KERN_PROCARGS2; never logged), or a TUI is attached.
Two clocks:

  stub   Never took a real user turn, no attached client.  A client opened
         the session and walked away, yet it holds a full MCP set (seven of
         these cost ~1.6 GiB on 2026-10-08).  Unloads after 30 minutes idle.
         GROK_IDLE_UNLOAD_STUB_MINUTES / --stub-minutes (0 turns it off).
  other  Unloads after 4 hours idle.  GROK_IDLE_UNLOAD_HOURS / --max-age-hours.

Never closes working, needs-input, pendingTool, $GROK_SESSION_ID, or a chat
that still has background processes (carrying its id) outside the leader.
A session whose close failed is terminated directly (SIGTERM on its MCP
children) as a fallback; so are MCP children of sessions whose directory is
gone.  --no-reap-orphans turns both off.

The leader helpers are slow on a loaded Mac (a fresh client needs 15-25s just
to list).  Each helper call gets GROK_IDLE_UNLOAD_HELPER_TIMEOUT_SEC (180s).
On a timeout this prints one JSON line with a timestamp on stderr and exits
75 (EX_TEMPFAIL); other failures exit 1.

On-demand:
  python3 ~/apps/grok-acp-runtime/grok-idle-unload.py --dry-run
  python3 ~/apps/grok-acp-runtime/grok-idle-unload.py

Hourly launchd: com.jay.grok-idle-unload
"""
from __future__ import annotations

import argparse
import collections
import ctypes
import ctypes.util
import json
import os
import re
import signal
import struct
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from session_disk import (  # noqa: E402
    DEFAULT_IDLE_UNLOAD_SEC,
    DEFAULT_STUB_IDLE_SEC,
    annotate_user_turns,
    apply_pid_liveness,
    enrich_sessions,
    find_session_dir,
    idle_age_seconds,
    is_stub_row,
    load_active,
    peek_summary,
    select_idle_unload,
    self_session_id,
    unload_skip_reason,
)

LEADER = HERE / "leader-client.py"
PY = "/usr/bin/python3"
DEFAULT_HELPER_TIMEOUT_SEC = 180.0
DEFAULT_BUDGET_SEC = 600.0
EX_TEMPFAIL = 75
# A process this young may belong to a session whose directory is still being
# created, so it is never reaped for a "missing" directory.
MIN_MISSING_DIR_AGE_SEC = 600.0
SESSION_ID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
SESSION_ENV = "GROK_SESSION_ID"
JsonDict = dict[str, Any]


class HelperTimeout(Exception):
    """A leader helper call did not answer in time."""

    def __init__(self, call: str, timeout: float) -> None:
        super().__init__("helper timed out: %s after %.0fs" % (call, timeout))
        self.call = call
        self.timeout = timeout


class HelperError(Exception):
    """A leader helper call answered with an error."""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= minimum else default


# ---------------------------------------------------------------- leader helper


def _kill_group(proc: subprocess.Popen) -> None:
    """Stop the helper and the grok client it spawned (one process group)."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def _parse_helper_output(proc_out: str, proc_err: str, returncode: int) -> JsonDict:
    blob = (proc_out or "").strip() or (proc_err or "").strip()
    data: JsonDict
    try:
        data = json.loads(blob[blob.find("{") :] if blob.find("{") >= 0 else blob)
    except json.JSONDecodeError:
        data = {"ok": False, "error": blob[:800] or "empty helper output"}
    if not isinstance(data, dict):
        data = {"ok": False, "error": "unexpected helper output"}
    if returncode != 0 and data.get("ok") is not False:
        data = dict(data)
        data["ok"] = False
        data.setdefault("exitCode", returncode)
    return data


def _run_leader(argv: list[str], timeout: float, call: str) -> JsonDict:
    """Run leader-client.py with an outer timeout.

    leader-client gets a total RPC budget a little under `timeout`, so it
    normally reports its own clean "no ACP result" error and exits.  The
    outer timeout (and a process-group kill) is the backstop.
    """
    rpc_budget = max(10.0, timeout - 15.0)
    full = [PY, str(LEADER), "--rpc-timeout", "%.0f" % rpc_budget] + argv
    proc = subprocess.Popen(
        full,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
        raise HelperTimeout(call, timeout)
    data = _parse_helper_output(out, err, proc.returncode)
    if data.get("ok") is False and "no ACP result" in str(data.get("error") or ""):
        raise HelperTimeout(call, rpc_budget)
    return data


def list_leader_sessions(timeout: float) -> list[JsonDict]:
    data = _run_leader(["list"], timeout, "list")
    if data.get("ok") is False:
        raise HelperError(str(data.get("error") or "list failed")[:300])
    return list(data.get("sessions") or [])


def close_session(session_id: str, cwd: str, timeout: float) -> JsonDict:
    argv = ["close", "--session-id", session_id, "--cwd", cwd or "/Users/jay"]
    data = _run_leader(argv, timeout, "close")
    data["diskKept"] = find_session_dir(session_id) is not None
    if not data.get("diskKept"):
        data["ok"] = False
        data["error"] = "session/close removed disk history — unexpected"
    return data


# ---------------------------------------------------------------- process table
# macOS only.  libproc lists pids and parents; sysctl KERN_PROCARGS2 returns a
# process's argv and environment.  Nothing here prints argv or env: the only
# value ever read out is a validated GROK_SESSION_ID.

CTL_KERN = 1
KERN_PROCARGS2 = 49
PROC_PIDTBSDINFO = 3


class _BsdInfo(ctypes.Structure):
    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("status", ctypes.c_uint32),
        ("xstatus", ctypes.c_uint32),
        ("pid", ctypes.c_uint32),
        ("ppid", ctypes.c_uint32),
        ("uid", ctypes.c_uint32),
        ("gid", ctypes.c_uint32),
        ("ruid", ctypes.c_uint32),
        ("rgid", ctypes.c_uint32),
        ("svuid", ctypes.c_uint32),
        ("svgid", ctypes.c_uint32),
        ("rfu1", ctypes.c_uint32),
        ("comm", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        ("nfiles", ctypes.c_uint32),
        ("pgid", ctypes.c_uint32),
        ("pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("nice", ctypes.c_int32),
        ("start_tvsec", ctypes.c_uint64),
        ("start_tvusec", ctypes.c_uint64),
    ]


_LIBS: tuple[Any, Any] | None = None


def _libs() -> tuple[Any, Any]:
    global _LIBS
    if _LIBS is None:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        _LIBS = (libc, libproc)
    return _LIBS


class ProcRow(NamedTuple):
    pid: int
    ppid: int
    comm: str
    started: float  # epoch seconds, 0.0 when unknown
    leader: bool
    session_id: str | None


def parse_procargs(
    raw: bytes, want_env: tuple[str, ...] = (SESSION_ENV,)
) -> tuple[list[str], dict[str, str]]:
    """Split a KERN_PROCARGS2 buffer into (argv, selected env).

    Layout: int32 argc, the executable path (NUL-terminated), NUL padding,
    argc argv strings, then environment strings (NAME=value).  Only the env
    names in `want_env` are returned; the rest of the environment is dropped
    on the floor so it can never reach a log.  A `NAME=` string inside argv is
    NOT environment and is ignored.
    """
    if len(raw) < 5:
        return [], {}
    argc = struct.unpack("i", raw[:4])[0]
    rest = raw[4:]
    exe_end = rest.find(b"\0")
    if exe_end < 0 or argc < 0:
        return [], {}
    i = exe_end
    while i < len(rest) and rest[i] == 0:
        i += 1
    parts = rest[i:].split(b"\0")
    argv = [p.decode("utf-8", "replace") for p in parts[:argc]]
    env: dict[str, str] = {}
    prefixes = {("%s=" % name).encode(): name for name in want_env}
    for item in parts[argc:]:
        for prefix, name in prefixes.items():
            if item.startswith(prefix):
                env[name] = item[len(prefix) :].decode("utf-8", "replace")
    return argv, env


def is_leader_argv(argv: list[str]) -> bool:
    """The shared leader runs `grok agent ... leader ...`.

    Stdio clients run `grok agent --leader stdio` and must not match: the
    subcommand is the bare token `leader`, never `--leader`.
    """
    if len(argv) < 3:
        return False
    if not os.path.basename(argv[0]).startswith("grok"):
        return False
    rest = argv[1:]
    return "agent" in rest and "leader" in rest and "stdio" not in rest


def session_id_from_env(env: dict[str, str]) -> str | None:
    value = (env.get(SESSION_ENV) or "").strip()
    return value if SESSION_ID_RE.match(value) else None


def _read_procargs(pid: int) -> bytes | None:
    libc, _ = _libs()
    mib = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid)
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or size.value <= 0:
        return None
    buf = ctypes.create_string_buffer(size.value + 64)
    size = ctypes.c_size_t(len(buf))
    if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
        return None
    return buf.raw[: size.value]


def _list_pids() -> list[int]:
    _, libproc = _libs()
    count = libproc.proc_listallpids(None, 0)
    if count <= 0:
        return []
    buf = (ctypes.c_int * (count + 256))()
    count = libproc.proc_listallpids(buf, ctypes.sizeof(buf))
    return [buf[i] for i in range(max(0, count)) if buf[i] > 0]


def _bsd_info(pid: int) -> _BsdInfo | None:
    _, libproc = _libs()
    info = _BsdInfo()
    got = libproc.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info))
    return info if got == ctypes.sizeof(info) else None


def scan_processes() -> list[ProcRow]:
    """Every readable process: pid, parent, leader?, GROK_SESSION_ID?"""
    rows: list[ProcRow] = []
    for pid in _list_pids():
        info = _bsd_info(pid)
        if info is None:
            continue
        raw = _read_procargs(pid)
        leader = False
        sid: str | None = None
        if raw:
            argv, env = parse_procargs(raw)
            leader = is_leader_argv(argv)
            sid = session_id_from_env(env)
        rows.append(
            ProcRow(
                pid=int(info.pid),
                ppid=int(info.ppid),
                comm=info.comm.decode("utf-8", "replace"),
                started=float(info.start_tvsec),
                leader=leader,
                session_id=sid,
            )
        )
    return rows


def grok_session_id_from_pid(pid: int) -> str | None:
    """GROK_SESSION_ID of one process via sysctl.  Never logs argv or env."""
    try:
        raw = _read_procargs(pid)
    except OSError:
        return None
    if not raw:
        return None
    return session_id_from_env(parse_procargs(raw)[1])


def leader_pids(procs: list[ProcRow]) -> list[int]:
    return [p.pid for p in procs if p.leader]


def descendants(procs: list[ProcRow], roots: list[int]) -> set[int]:
    by_ppid: dict[int, list[int]] = collections.defaultdict(list)
    for p in procs:
        by_ppid[p.ppid].append(p.pid)
    out: set[int] = set()
    stack = list(roots)
    while stack:
        cur = stack.pop()
        for child in by_ppid.get(cur, []):
            if child in out or child in roots:
                continue
            out.add(child)
            stack.append(child)
    return out


def classify_sessions(
    procs: list[ProcRow], *, self_pid: int | None = None
) -> dict[str, dict[str, list[int]]]:
    """Group session-carrying processes by id.

    "tree"    — under the leader: its MCP servers (the memory we can free).
    "outside" — carrying the id but NOT under the leader (tool-spawned work
                that outlived its parent, dev servers, ...): background work.
    """
    tree_pids = descendants(procs, leader_pids(procs))
    out: dict[str, dict[str, list[int]]] = {}
    for p in procs:
        if not p.session_id or p.pid == self_pid:
            continue
        slot = out.setdefault(p.session_id, {"tree": [], "outside": []})
        slot["tree" if p.pid in tree_pids else "outside"].append(p.pid)
    return out


# ---------------------------------------------------------------- orchestration


def build_rows(
    listed: list[JsonDict],
    smap: dict[str, dict[str, list[int]]],
) -> list[JsonDict]:
    """Merge the leader list, disk state and the process table into rows."""
    listed_ids = {str(s.get("sessionId")) for s in listed}
    extra: list[JsonDict] = []
    for sid, slot in smap.items():
        if not slot["tree"] or sid in listed_ids:
            continue
        peek = peek_summary(sid)
        if not peek.get("ok"):
            continue  # directory gone: the orphan reaper handles it
        extra.append({
            "sessionId": sid,
            "cwd": peek.get("cwd"),
            "title": peek.get("title"),
            "updatedAt": peek.get("updatedAt"),
            "note": "loaded in the leader; beyond the first session/list page",
        })
    rows = enrich_sessions(listed + extra)
    apply_pid_liveness(rows)
    for row in rows:
        slot = smap.get(str(row.get("sessionId")))
        row["loaded"] = bool(slot and slot["tree"])
        row["procs"] = len(slot["tree"]) if slot else 0
        row["backgroundPids"] = len(slot["outside"]) if slot else 0
    annotate_user_turns(rows)
    return rows


def kill_pids(pids: list[int], session_id: str, *, dry_run: bool) -> tuple[bool, str | None]:
    """SIGTERM each pid that STILL carries `session_id` right now.

    The process table was read at the start of the run; a slow close can take
    minutes, children may exit meanwhile and their pids be reused.  Re-read the
    pid's own environment immediately before signalling and skip anything that
    is gone or no longer this session's.
    """
    if dry_run:
        return True, None
    err: str | None = None
    for pid in pids:
        if grok_session_id_from_pid(pid) != session_id:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            err = str(exc)
    return err is None, err


def reap_missing_dir_orphans(
    smap: dict[str, dict[str, list[int]]],
    procs: list[ProcRow],
    *,
    protected_ids: set[str],
    now: float,
    dry_run: bool,
) -> list[JsonDict]:
    """SIGTERM leader-tree MCP children of sessions whose directory is gone."""
    started = {p.pid: p.started for p in procs}
    actions: list[JsonDict] = []
    for sid, slot in smap.items():
        if sid in protected_ids or not slot["tree"]:
            continue
        if find_session_dir(sid) is not None:
            continue
        old = [
            pid
            for pid in slot["tree"]
            if started.get(pid, 0.0) and now - started[pid] >= MIN_MISSING_DIR_AGE_SEC
        ]
        if not old:
            continue
        ok, err = kill_pids(old, sid, dry_run=dry_run)
        rec: JsonDict = {
            "sessionId": sid,
            "pids": len(old),
            "missingSessionDir": True,
            "action": "would_sigterm" if dry_run else "sigterm",
            "ok": ok,
        }
        if err:
            rec["error"] = err
        actions.append(rec)
    return actions


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Unload MCP on Grok chats nobody is using, without deleting them"
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--max-age-hours",
        type=float,
        default=env_float("GROK_IDLE_UNLOAD_HOURS", DEFAULT_IDLE_UNLOAD_SEC / 3600.0, minimum=1e-9),
        help="idle hours before an ordinary chat unloads (env GROK_IDLE_UNLOAD_HOURS)",
    )
    p.add_argument(
        "--stub-minutes",
        type=float,
        default=env_float("GROK_IDLE_UNLOAD_STUB_MINUTES", DEFAULT_STUB_IDLE_SEC / 60.0),
        help="idle minutes before a zero-turn chat unloads; 0 = off (env GROK_IDLE_UNLOAD_STUB_MINUTES)",
    )
    p.add_argument(
        "--helper-timeout",
        type=float,
        default=env_float("GROK_IDLE_UNLOAD_HELPER_TIMEOUT_SEC", DEFAULT_HELPER_TIMEOUT_SEC, minimum=20.0),
        help="seconds per leader helper call (env GROK_IDLE_UNLOAD_HELPER_TIMEOUT_SEC)",
    )
    p.add_argument(
        "--budget",
        type=float,
        default=env_float("GROK_IDLE_UNLOAD_BUDGET_SEC", DEFAULT_BUDGET_SEC, minimum=30.0),
        help="stop starting new closes after this many seconds",
    )
    p.add_argument("--reap-orphans", action="store_true", default=True)
    p.add_argument("--no-reap-orphans", action="store_false", dest="reap_orphans")
    p.add_argument("--verbose", action="store_true", help="list every skipped chat")
    p.add_argument("--json", action="store_true")  # accepted; output is always JSON
    args = p.parse_args(argv)

    started_at = time.time()
    max_idle_sec = float(args.max_age_hours) * 3600.0
    if max_idle_sec <= 0:
        max_idle_sec = float(DEFAULT_IDLE_UNLOAD_SEC)
    stub_idle_sec = max(0.0, float(args.stub_minutes)) * 60.0
    now = started_at
    self_id = self_session_id()
    timeouts: list[JsonDict] = []
    errors: list[str] = []

    try:
        procs = scan_processes()
    except Exception as exc:  # libproc missing, sysctl refused
        procs = []
        errors.append("process scan failed: %s" % type(exc).__name__)
    smap = classify_sessions(procs, self_pid=os.getpid())

    listed: list[JsonDict] = []
    try:
        listed = list_leader_sessions(args.helper_timeout)
    except HelperTimeout as exc:
        timeouts.append({"call": exc.call, "timeoutSec": exc.timeout, "at": now_iso()})
    except HelperError as exc:
        errors.append("list: %s" % exc)

    rows = build_rows(listed, smap)
    skipped = []
    skipped_by_reason: collections.Counter[str] = collections.Counter()
    for row in rows:
        reason = unload_skip_reason(
            row,
            now=now,
            max_idle_sec=max_idle_sec,
            self_id=self_id,
            stub_idle_sec=stub_idle_sec,
        )
        if reason:
            skipped_by_reason[reason] += 1
            skipped.append({
                "sessionId": row.get("sessionId"),
                "reason": reason,
                "loaded": bool(row.get("loaded")),
                "live": bool(row.get("live")),
                "turnState": row.get("turnState"),
                "idleSec": int(idle_age_seconds(row, now)),
            })
    candidates = select_idle_unload(
        rows,
        now=now,
        max_idle_sec=max_idle_sec,
        self_id=self_id,
        stub_idle_sec=stub_idle_sec,
    )

    closed: list[JsonDict] = []
    stop_closing = False
    for row in candidates:
        sid = str(row.get("sessionId"))
        stub = bool(stub_idle_sec) and is_stub_row(row)
        rec: JsonDict = {
            "sessionId": sid,
            "title": row.get("title"),
            "kind": "stub" if stub else "idle",
            "idleSec": int(idle_age_seconds(row, now)),
            "cwd": row.get("cwd"),
            "procs": row.get("procs"),
        }
        if args.dry_run:
            rec["action"] = "would_close"
            rec["ok"] = True
            rec["diskKept"] = find_session_dir(sid) is not None
        elif stop_closing or time.time() - started_at > args.budget:
            rec["action"] = "deferred"
            rec["ok"] = True
            rec["note"] = "helper timed out earlier" if stop_closing else "budget spent"
        else:
            rec["action"] = "session/close"
            try:
                rec.update(close_session(sid, str(row.get("cwd") or "/Users/jay"), args.helper_timeout))
            except HelperTimeout as exc:
                stop_closing = True  # the host is overloaded; do not queue more
                timeouts.append({"call": exc.call, "sessionId": sid, "timeoutSec": exc.timeout, "at": now_iso()})
                rec["ok"] = False
                rec["error"] = "helper timed out"
        closed.append(rec)

    orphans: list[JsonDict] = []
    if args.reap_orphans:
        # Fallback: a candidate whose close failed keeps its MCP children, so
        # terminate them directly.  Only candidates (every skip rule passed).
        by_id = {str(r.get("sessionId")): r for r in candidates}
        for rec in closed:
            if rec.get("ok") is not False:
                continue
            sid = str(rec["sessionId"])
            slot = smap.get(sid)
            if not slot or not slot["tree"] or sid not in by_id:
                continue
            ok, err = kill_pids(slot["tree"], sid, dry_run=args.dry_run)
            orphan: JsonDict = {
                "sessionId": sid,
                "pids": len(slot["tree"]),
                "fallbackAfterCloseFailure": True,
                "action": "would_sigterm" if args.dry_run else "sigterm",
                "ok": ok,
            }
            if err:
                orphan["error"] = err
            orphans.append(orphan)
        protected = {a["sessionId"] for a in load_active()}
        if self_id:
            protected.add(self_id)
        orphans.extend(
            reap_missing_dir_orphans(
                smap, procs, protected_ids=protected, now=now, dry_run=args.dry_run
            )
        )

    out: JsonDict = {
        "ok": all(r.get("ok") is not False for r in closed + orphans) and not errors and not timeouts,
        "at": now_iso(),
        "dryRun": bool(args.dry_run),
        "maxAgeHours": args.max_age_hours,
        "stubMinutes": args.stub_minutes,
        "selfSessionId": self_id or None,
        "leaderPids": len(leader_pids(procs)),
        "loaded": sum(1 for s in smap.values() if s["tree"]),
        "listed": len(listed),
        "rows": len(rows),
        "closed": closed,
        "skippedByReason": dict(skipped_by_reason),
        "orphans": orphans,
    }
    if args.verbose or args.dry_run:
        out["skipped"] = skipped
    if timeouts:
        out["timeouts"] = timeouts
    if errors:
        out["errors"] = errors
    print(json.dumps(out, indent=2))
    if timeouts:
        for t in timeouts:
            print(
                json.dumps({
                    "ok": False,
                    "at": t["at"],
                    "error": "helper timed out",
                    "call": t["call"],
                    "timeoutSec": t["timeoutSec"],
                    "hint": "host overloaded; raise GROK_IDLE_UNLOAD_HELPER_TIMEOUT_SEC or retry next hour",
                }),
                file=sys.stderr,
            )
        return EX_TEMPFAIL
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "at": now_iso(), "error": str(exc)}), file=sys.stderr)
        sys.exit(1)
