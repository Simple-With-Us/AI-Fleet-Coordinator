#!/usr/bin/env python3
"""Unload MCP tool processes from Grok chats nobody is using.

session/close unloads that chat's MCP tool processes.  Disk history
(summary.json / updates.jsonl) is not deleted.  Resume via /resume
(or grok --resume ID) — tools come back on the next turn.

A chat is a candidate only when it is LOADED: the shared Grok leader holds
MCP child processes that carry its GROK_SESSION_ID (read from the process
table with sysctl KERN_PROCARGS2; never logged).  Two clocks:

  stub   Never took a real user turn and no client is attached.  A client
         opened the session and walked away, yet it holds a full MCP set
         (seven of these cost ~1.6 GiB on 2026-10-08).  Unloads after 30
         minutes idle.  GROK_IDLE_UNLOAD_STUB_MINUTES / --stub-minutes (0 = off).
  other  Unloads after 4 hours idle.  GROK_IDLE_UNLOAD_HOURS / --max-age-hours.

"Idle" counts from the newest of summary updated_at, the last turn start or
end, and the last time the chat was LOADED (newest mcp_config_resolved /
mcp_init_completed event, and the start of its current MCP processes), so a
chat resumed today after a day of silence is not unloaded before its first turn.

"No client attached" means no grok process (TUI, stdio client) has the chat's
working directory as its own; the leader exposes no connection list.  A cwd
that cannot be read counts as attached.

MCP versus work: a process under the leader that carries the chat's id is an
MCP server only when its branch root (the leader's child) is not a shell and was
forked inside the chat's MCP load window.  Everything else (a shell, a watcher,
a dev server, a process forked later) is work a tool started: it protects the
chat and is never signalled.  So does any process carrying the id outside the
leader.

Never closes working, needs-input, pendingTool, $GROK_SESSION_ID, or a chat with
background work.  Right before each close every rule is re-checked on fresh
data; a chat that changed since selection is skipped.

When session/close answers closeOutcome "notResident" (the leader does not hold
the chat) yet its MCP processes still run under the leader, they are terminated
directly after one more re-check.  Never after a timeout, an RPC error or a
refusal.  MCP processes of sessions whose directory is gone are reaped too, at
most 5 sessions a run, and only with exactly one leader, a readable sessions
directory and a successful list.  --no-reap-orphans turns both off.

The leader helpers are slow on a loaded Mac (a fresh client needed 15-25s just
to list, and 60-90s at load 280).  Candidates are found from the process table
and disk, so session/list is not called unless --use-list (or
GROK_IDLE_UNLOAD_USE_LIST=1) asks for it; only session/close is, and only for a
chat that is about to be unloaded.  Each helper call gets
GROK_IDLE_UNLOAD_HELPER_TIMEOUT_SEC (180s).
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
import session_disk as _sd  # noqa: E402
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
    mcp_load_marks,
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
# At most this many sessions are reaped for a "missing" directory per run.
MAX_REAP_PER_RUN = 5
# A process is an MCP server of a session only when it was forked inside that
# session's load window: from just before mcp_config_resolved to a grace after
# mcp_init_completed (a slow server was seen starting ~4 minutes after the
# first).  Anything else under the leader carrying the session id is work a
# tool started (a shell, a watcher, a dev server), and protects the chat.
MCP_WINDOW_LEAD_SEC = 30.0
MCP_WINDOW_GRACE_SEC = 300.0
MCP_WINDOW_NO_INIT_SEC = 600.0
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh"})
LEADER_LOCK = Path.home() / ".grok" / "leader.lock"
CLOSE_OUTCOME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,40}$")
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


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


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
    grok_client: bool = False  # a grok process that is not the leader


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
        client = False
        sid: str | None = None
        if raw:
            argv, env = parse_procargs(raw)
            leader = is_leader_argv(argv)
            client = (not leader) and bool(argv) and os.path.basename(argv[0]).startswith("grok")
            sid = session_id_from_env(env)
        rows.append(
            ProcRow(
                pid=int(info.pid),
                ppid=int(info.ppid),
                comm=info.comm.decode("utf-8", "replace"),
                started=float(info.start_tvsec),
                leader=leader,
                session_id=sid,
                grok_client=client,
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


def lock_pid() -> int | None:
    """The pid ~/.grok/leader.lock names, when readable."""
    try:
        raw = LEADER_LOCK.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


def pinned_leaders(procs: list[ProcRow]) -> list[int]:
    """The leader whose tree we act on: the pid in leader.lock when that process
    is a leader, else every process that looks like one."""
    found = leader_pids(procs)
    pid = lock_pid()
    if pid is not None and pid in found:
        return [pid]
    return found


def single_leader(procs: list[ProcRow]) -> bool:
    """Exactly one leader, and leader.lock (when readable) agrees with it."""
    found = leader_pids(procs)
    if len(found) != 1:
        return False
    pid = lock_pid()
    return pid is None or pid == found[0]


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


# ---------------------------------------------------------------- attached clients
# The leader exposes no list of connected clients (session/list carries only id,
# cwd, title, updatedAt and a kind/facets meta), and active_sessions.json is empty
# in leader mode.  Stdio clients (Shellular, BotFleet, grok-drive, seat-mcp) and
# TUIs run as their own `grok` processes, and a client works in its chat's
# directory, so a grok process whose working directory equals a chat's cwd
# counts as attached to it.  A client whose cwd cannot be read means we cannot
# tell, so every chat counts as attached for that run (fail safe).

PROC_PIDVNODEPATHINFO = 9


class _VNodePathInfo(ctypes.Structure):
    # struct proc_vnodepathinfo: two vnode_info_path, each a 152-byte
    # vnode_info followed by a MAXPATHLEN (1024) path.  Only the cwd is read.
    _fields_ = [
        ("cdir_info", ctypes.c_char * 152),
        ("cdir_path", ctypes.c_char * 1024),
        ("rdir_info", ctypes.c_char * 152),
        ("rdir_path", ctypes.c_char * 1024),
    ]


def proc_cwd(pid: int) -> str | None:
    """Working directory of one process through libproc, or None."""
    try:
        _, libproc = _libs()
        info = _VNodePathInfo()
        got = libproc.proc_pidinfo(
            pid, PROC_PIDVNODEPATHINFO, 0, ctypes.byref(info), ctypes.sizeof(info)
        )
    except Exception:
        return None
    if got != ctypes.sizeof(info):
        return None
    path = info.cdir_path.decode("utf-8", "replace")
    return path or None


def _norm_path(path: str) -> str:
    return os.path.realpath(path)


def cwd_probe_works() -> bool:
    """proc_cwd(self) must equal os.getcwd(); otherwise the struct layout is wrong."""
    got = proc_cwd(os.getpid())
    return got is not None and _norm_path(got) == _norm_path(os.getcwd())


def client_cwds(
    procs: list[ProcRow], *, own_pid: int, leaders: list[int]
) -> set[str] | None:
    """Working directories of connected grok clients, or None when unknown.

    Not clients: the leader's own descendants and this job's descendants (its
    helper runs `grok agent --leader stdio`).
    """
    skip = descendants(procs, leaders) | descendants(procs, [own_pid]) | {own_pid}
    out: set[str] = set()
    for p in procs:
        if not p.grok_client or p.pid in skip:
            continue
        cwd = proc_cwd(p.pid)
        if cwd is None:
            return None
        out.add(_norm_path(cwd))
    if out and not cwd_probe_works():
        return None
    return out


def is_attached(cwd: Any, clients: set[str] | None) -> bool:
    if clients is None:
        return True  # cannot tell: assume somebody is attached
    if not clients:
        return False
    if not cwd:
        return True  # no cwd to compare, and a client exists
    return _norm_path(str(cwd)) in clients


# ---------------------------------------------------------------- MCP versus work


def mcp_window(marks: JsonDict | None) -> tuple[float, float] | None:
    """(earliest, latest) start time of this session's current MCP processes."""
    if not marks:
        return None
    cfg = marks.get("configAt")
    init = marks.get("initAt")
    if not cfg and not init:
        return None
    if cfg:
        lo = float(cfg) - MCP_WINDOW_LEAD_SEC
    else:
        lo = float(init) - MCP_WINDOW_NO_INIT_SEC
    if init and (not cfg or float(init) >= float(cfg)):
        hi = float(init) + MCP_WINDOW_GRACE_SEC
    else:
        hi = float(cfg) + MCP_WINDOW_NO_INIT_SEC + MCP_WINDOW_GRACE_SEC
    return lo, hi


def branch_root(pid: int, by_pid: dict[int, ProcRow], leaders: set[int]) -> ProcRow | None:
    """The ancestor of `pid` whose parent is the leader (pid itself if it is one)."""
    cur = pid
    for _ in range(64):
        proc = by_pid.get(cur)
        if proc is None:
            return None
        if proc.ppid in leaders:
            return proc
        cur = proc.ppid
    return None


def classify_sessions(
    procs: list[ProcRow],
    *,
    self_pid: int | None = None,
    marks_for: Any = None,
) -> dict[str, dict[str, Any]]:
    """Group session-carrying processes by id.

    "tree"    all of them that sit under the leader.
    "mcp"     the part of "tree" that is the chat's MCP servers (the memory we
              can free): branches whose leader-child root is not a shell and was
              forked inside the session's MCP load window (see mcp_window).
    "task"    the rest of "tree": a shell, or anything forked outside the window.
              That is work a tool started; it protects the chat and is never
              signalled by this job.  When unsure, a process is a task.
    "nonshell" "tree" pids whose branch root is not a shell (used only to reap
              sessions whose directory is gone, where no window exists).
    "outside" carrying the id but NOT under the leader (reparented tool work,
              dev servers, ...): background work.
    "mcpStarted" earliest start among "mcp" (0.0 when none): when the chat's
              current MCP set was loaded.
    """
    leaders = pinned_leaders(procs)
    leader_set = set(leaders)
    tree_pids = descendants(procs, leaders)
    by_pid = {p.pid: p for p in procs}
    lookup = marks_for if marks_for is not None else mcp_load_marks
    out: dict[str, dict[str, Any]] = {}
    for p in procs:
        if not p.session_id or p.pid == self_pid:
            continue
        slot = out.setdefault(
            p.session_id,
            {
                "tree": [], "mcp": [], "task": [], "nonshell": [], "outside": [],
                "mcpStarted": 0.0, "marks": {}, "taskRoots": [],
            },
        )
        if p.pid not in tree_pids:
            slot["outside"].append(p.pid)
            continue
        slot["tree"].append(p.pid)
    for sid, slot in out.items():
        if not slot["tree"]:
            continue
        slot["marks"] = lookup(sid) or {}
        window = mcp_window(slot["marks"])
        for pid in slot["tree"]:
            root = branch_root(pid, by_pid, leader_set)
            shell_root = root is None or root.comm.lower() in SHELLS
            if not shell_root:
                slot["nonshell"].append(pid)
            is_mcp = (
                not shell_root
                and window is not None
                and root.started > 0
                and window[0] <= root.started <= window[1]
            )
            slot["mcp" if is_mcp else "task"].append(pid)
            if not is_mcp and root is not None and root.comm not in slot["taskRoots"]:
                slot["taskRoots"].append(root.comm)  # comm only, for the log
        starts = [by_pid[pid].started for pid in slot["mcp"] if by_pid[pid].started > 0]
        slot["mcpStarted"] = min(starts) if starts else 0.0
    return out


# ---------------------------------------------------------------- orchestration


def build_rows(
    listed: list[JsonDict],
    smap: dict[str, dict[str, Any]],
    clients: set[str] | None = None,
    *,
    include_unlisted: bool = True,
) -> list[JsonDict]:
    """Merge the leader list, disk state and the process table into rows."""
    listed_ids = {str(s.get("sessionId")) for s in listed}
    extra: list[JsonDict] = []
    for sid, slot in smap.items():
        if not include_unlisted or not slot["tree"] or sid in listed_ids:
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
        sid = str(row.get("sessionId"))
        slot = smap.get(sid)
        row["loaded"] = bool(slot and slot["tree"])
        row["procs"] = len(slot["mcp"]) if slot else 0
        row["backgroundPids"] = (len(slot["outside"]) + len(slot["task"])) if slot else 0
        row["attached"] = is_attached(row.get("cwd"), clients)
        marks = slot["marks"] if slot else {}
        row["loadedAt"] = max(
            float(marks.get("configAt") or 0.0),
            float(marks.get("initAt") or 0.0),
            float(slot["mcpStarted"]) if slot else 0.0,
        )
        row["taskRoots"] = slot["taskRoots"] if slot else []
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


def sessions_root_healthy() -> bool:
    """~/.grok/sessions is a readable directory that holds at least one session.

    When it is not, find_session_dir() returns None for EVERY session, and every
    loaded chat would look like one whose directory is gone.
    """
    root = _sd.SESSIONS_ROOT
    try:
        if not root.is_dir():
            return False
        return any(child.is_dir() for group in root.iterdir() if group.is_dir() for child in group.iterdir())
    except OSError:
        return False


def reap_missing_dir_orphans(
    smap: dict[str, dict[str, Any]],
    procs: list[ProcRow],
    *,
    protected_ids: set[str],
    now: float,
    dry_run: bool,
    max_sessions: int = MAX_REAP_PER_RUN,
) -> list[JsonDict]:
    """SIGTERM non-shell leader-tree children of sessions whose directory is gone.

    No window exists for such a session (its events.jsonl is gone with it), so
    anything whose branch root is a shell is left alone, and at most
    `max_sessions` sessions are reaped per run.
    """
    started = {p.pid: p.started for p in procs}
    actions: list[JsonDict] = []
    for sid in sorted(smap):
        if len(actions) >= max_sessions:
            break
        slot = smap[sid]
        if sid in protected_ids or not slot["tree"]:
            continue
        if find_session_dir(sid) is not None:
            continue
        old = [
            pid
            for pid in slot["nonshell"]
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


def fresh_row(row: JsonDict, *, own_pid: int) -> tuple[JsonDict, dict[str, dict[str, Any]]]:
    """Rebuild ONE row from the process table and disk as they are right now."""
    procs = scan_processes()
    smap = classify_sessions(procs, self_pid=own_pid)
    clients = client_cwds(procs, own_pid=own_pid, leaders=pinned_leaders(procs))
    sid = str(row.get("sessionId"))
    base: JsonDict = {k: row[k] for k in ("sessionId", "cwd", "title", "note") if row.get(k) is not None}
    peek = peek_summary(sid)
    base["updatedAt"] = peek.get("updatedAt") or row.get("updatedAt")
    rows = build_rows([base], smap, clients, include_unlisted=False)
    return rows[0], smap


def still_eligible(
    row: JsonDict,
    *,
    own_pid: int,
    max_idle_sec: float,
    self_id: str,
    stub_idle_sec: float,
) -> tuple[str | None, dict[str, dict[str, Any]] | None]:
    """(None, smap) when the chat is STILL a candidate right now, else (reason, None).

    Eligibility is decided once at the start of the run, but closes run one after
    another and can start minutes later.  Re-run every rule on fresh data
    immediately before acting.  A failure to re-check counts as "no".
    """
    try:
        fresh, smap = fresh_row(row, own_pid=own_pid)
    except Exception as exc:
        return "recheck_failed:%s" % type(exc).__name__, None
    why = unload_skip_reason(
        fresh,
        now=time.time(),
        max_idle_sec=max_idle_sec,
        self_id=self_id,
        stub_idle_sec=stub_idle_sec,
    )
    return (why, None) if why is not None else (None, smap)


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
    p.add_argument(
        "--use-list",
        action="store_true",
        default=env_flag("GROK_IDLE_UNLOAD_USE_LIST"),
        help=(
            "also call the leader's session/list (env GROK_IDLE_UNLOAD_USE_LIST=1).  Off by default: "
            "it took 14-90s on a loaded Mac and only adds chats that are not loaded, which are never candidates"
        ),
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
    own_pid = os.getpid()
    self_id = self_session_id()
    timeouts: list[JsonDict] = []
    errors: list[str] = []

    clients: set[str] | None = None
    try:
        procs = scan_processes()
        clients = client_cwds(procs, own_pid=own_pid, leaders=pinned_leaders(procs))
    except Exception as exc:  # libproc missing, sysctl refused
        procs = []
        errors.append("process scan failed: %s" % type(exc).__name__)
    smap = classify_sessions(procs, self_pid=own_pid)

    # Loaded chats come from the process table and disk (build_rows adds every
    # chat that holds MCP processes), so the leader's session/list adds nothing a
    # candidate needs.  It is the slowest and most failure-prone call here, so it
    # runs only on request.
    listed: list[JsonDict] = []
    list_ok = True
    if args.use_list:
        try:
            listed = list_leader_sessions(args.helper_timeout)
        except HelperTimeout as exc:
            list_ok = False
            timeouts.append({"call": exc.call, "timeoutSec": exc.timeout, "at": now_iso()})
        except HelperError as exc:
            list_ok = False
            errors.append("list: %s" % exc)

    rows = build_rows(listed, smap, clients)
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
            rec_skip: JsonDict = {
                "sessionId": row.get("sessionId"),
                "reason": reason,
                "loaded": bool(row.get("loaded")),
                "live": bool(row.get("live")),
                "attached": bool(row.get("attached")),
                "turnState": row.get("turnState"),
                "idleSec": int(idle_age_seconds(row, now)),
            }
            if row.get("taskRoots"):
                rec_skip["taskRoots"] = row.get("taskRoots")
            skipped.append(rec_skip)
    candidates = select_idle_unload(
        rows,
        now=now,
        max_idle_sec=max_idle_sec,
        self_id=self_id,
        stub_idle_sec=stub_idle_sec,
    )

    closed: list[JsonDict] = []
    fallback_ids: list[str] = []
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
            # Decided at the start of the run; a turn, a resume or a new client
            # may have arrived since.  Look again before touching it.
            why, _ = still_eligible(
                row,
                own_pid=own_pid,
                max_idle_sec=max_idle_sec,
                self_id=self_id,
                stub_idle_sec=stub_idle_sec,
            )
            if why is not None:
                rec["action"] = "skipped"
                rec["ok"] = True
                rec["reason"] = "changed_since_selection:%s" % why
                skipped_by_reason["changed_since_selection"] += 1
                closed.append(rec)
                continue
            rec["action"] = "session/close"
            try:
                data = close_session(sid, str(row.get("cwd") or "/Users/jay"), args.helper_timeout)
                outcome = str(data.get("closeOutcome") or "")
                if outcome and CLOSE_OUTCOME_RE.match(outcome):
                    rec["closeOutcome"] = outcome
                rec.update({k: v for k, v in data.items() if k != "closeOutcome"})
                if rec.get("closeOutcome") == "notResident" and data.get("ok") is not False:
                    # The leader says it does not hold this chat, yet MCP
                    # processes carrying its id sit under the leader: leaked.
                    fallback_ids.append(sid)
            except HelperTimeout as exc:
                stop_closing = True  # the host is overloaded; do not queue more
                timeouts.append({"call": exc.call, "sessionId": sid, "timeoutSec": exc.timeout, "at": now_iso()})
                rec["ok"] = False
                rec["error"] = "helper timed out"
        closed.append(rec)

    orphans: list[JsonDict] = []
    orphans_skipped: str | None = None
    if args.reap_orphans:
        # Fallback: only when the leader itself answered "notResident" for a
        # chat whose MCP processes are still running under it.  Never after a
        # timeout, an RPC error or a refusal -- the leader may still be
        # working on the close, or may have refused because a turn began.
        by_id = {str(r.get("sessionId")): r for r in candidates}
        for sid in fallback_ids:
            if sid not in by_id:
                continue
            why, fresh_map = still_eligible(
                by_id[sid],
                own_pid=own_pid,
                max_idle_sec=max_idle_sec,
                self_id=self_id,
                stub_idle_sec=stub_idle_sec,
            )
            orphan: JsonDict = {
                "sessionId": sid,
                "fallbackAfterNotResident": True,
                "action": "would_sigterm" if args.dry_run else "sigterm",
            }
            slot = (fresh_map or {}).get(sid)
            if why is not None or not slot or not slot["mcp"]:
                orphan["action"] = "skipped"
                orphan["ok"] = True
                orphan["reason"] = why or "no_mcp_processes"
                orphans.append(orphan)
                continue
            ok, err = kill_pids(slot["mcp"], sid, dry_run=args.dry_run)
            orphan["pids"] = len(slot["mcp"])
            orphan["ok"] = ok
            if err:
                orphan["error"] = err
            orphans.append(orphan)
        if not list_ok:
            orphans_skipped = "leader list failed"
        elif not single_leader(procs):
            orphans_skipped = "not exactly one leader process"
        elif not sessions_root_healthy():
            orphans_skipped = "sessions directory unreadable or empty"
        else:
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
        "clientCwds": None if clients is None else len(clients),
        "loaded": sum(1 for s in smap.values() if s["tree"]),
        "listed": len(listed),
        "rows": len(rows),
        "closed": closed,
        "skippedByReason": dict(skipped_by_reason),
        "orphans": orphans,
    }
    if orphans_skipped:
        out["orphansSkipped"] = orphans_skipped
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
