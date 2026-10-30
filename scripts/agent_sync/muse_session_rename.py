"""Rename a stored Muse Code session once the process that holds it has let go (AFC-564, the Muse Code adapter).

Why a helper at all.  Muse names a chat by its session name (`session/rename` on the Muse Session Protocol, MSP), and
the one process that holds a session's writer lease may rename it.  A hook runs as a child of that process, so a
`muse serve` host it starts is refused with `sessionInUse` (-32021) for as long as the chat is open:  Muse 1.4.14
exposes no control socket that takes a rename (the socket a chat publishes under /tmp/tbh-<uid>-rt/muse speaks the
peer-message hello and nothing else), and a hook's output has no title field.  So the title is queued, and this helper
applies it as soon as the lease is free, which for a chat in a terminal is the moment the chat exits.

    muse_session_rename.py run --spool FILE      wait for the lease, rename the session, record the outcome

The queue is one small JSON file per session (the spool record, 0600) that the hook writes and this helper reads and
updates; it holds the wanted name, the chat's letter, the names this helper already set (`applied`), the names it set
around someone's own words (`wrapped`), and where Muse and the session live.  The helper is started detached by the
hook (`start_worker`), waits for the lease by reading the `.session.lock` file the holder keeps in the session's
folder (its `pid=` line names the holder, and the holder empties the file when it lets go), then runs one `muse serve`
host (`--disable-shell --disable-write`, no workspace trust) to resume the session, read its name, rename it and leave.

The host runs the session's own provider and model.  A host whose provider differs from the one a session was recorded
with writes its own into the session when it resumes it, and `muse resume` keeps what is recorded: a chat renamed
through an echo host came back on the echo provider.  So `default_host` first reads the recorded provider and model
(`session/read` on a throw-away echo host, which takes no lease and records nothing), then starts the host with
`--provider` and `--model` to match.  A provider it has no name for ends the job as `failed`, never a guess.

What it never does.  It never writes to the session store itself, only through MSP.  It never overwrites a name
someone gave the chat by hand: a custom name that is not one this helper set keeps its words, whole, and only gains the
letter in front.  A name this helper set around someone's words stays theirs: it is kept in `wrapped`, not in
`applied`, so the stale title a resumed chat queues again never replaces it.  The runtime lower-cases a name when it
settles it ("AA: Fix Login" is stored as "aa: fix login"),
and two sessions may hold the same name:  the name authority (session-names.db, one per account, not per HOME) keeps
one canonical claim for a name and turns the earlier claimant's into a tombstone, without an error, so a letter that
comes back to a new chat takes the name over from the old chat that last carried it.  An error that cannot be
retried (`name_invalid`) or a host that will not start ends the job as `failed`, and the Operator keeps the letter
either way.

The host this helper starts fires the plugin's own SessionEnd hook when it exits.  The helper marks its environment
(MUSE_LETTER_HELPER=1), and the adapter reads that mark from the parent process, so that hook takes no part.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import glob
import json
import os
import re
import select
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

SESSION_ID_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z-]{7,63}")
#: The parent environment variables the adapter reads, and nothing else of that environment is ever kept.
IDENTITY_KEYS = (
    "AGENT_LAUNCHER",
    "AGENT_LAUNCH_SEAT",
    "SESSION_LETTER",
    "MUSE_LETTER",
    "MUSE_LETTER_HELPER",
)
HELPER_MARK = "MUSE_LETTER_HELPER"
MUSE_NAME = re.compile(r"^(muse|muse-bin[0-9A-Za-z._-]*|Muse)$")
#: How far up from the hook's parent to look for the Muse process (an `env` or `sh` wrapper may sit between).
ANCESTOR_DEPTH = 4

LEASE_POLL = 2.0  # seconds between looks at the session's lock file
WAIT_MAX = 7 * 86400  # a chat that is still open after a week is not waited for any longer
REQUEST_TIMEOUT = 20.0  # one MSP request;  the host's start-up is inside `initialize`
ATTEMPTS = 4  # tries at the rename itself, after the lease is free
RETRY_PAUSE = 3.0
MAX_BUSY = (
    60  # "in use" answers in a row (two minutes) while the lock file says the session is free
)
#: The `--provider` name the rename host takes for each provider id a stored session reports (`session/read` says
#: "muse" for a session Muse's own default host started and "meta" for one a chat started; both are the meta provider).
HOST_PROVIDERS = {"": "echo", "echo": "echo", "meta": "meta", "muse": "meta", "local": "local"}
MODEL_RE = re.compile(
    r"[0-9A-Za-z][0-9A-Za-z._:/@+-]{0,127}"
)  # a recorded model id this helper will pass to `--model`
LETTER_PREFIX = re.compile(
    r"[A-Za-z]{1,2}: "
)  # the letter this helper put in front of a name it wrapped
CTL_KERN, KERN_PROCARGS2 = (
    1,
    49,
)  # <sys/sysctl.h>: the kernel's copy of a process's arguments and environment
RECORD_KEYS = (
    "session_id",
    "name",
    "label",
    "applied",
    "wrapped",
    "cwd",
    "home",
    "exe",
    "xdg_data_home",
    "holder_pid",
    "queued_at",
    "state",
    "attempts",
    "error",
    "worker_pid",
    "done_at",
    "result",
)

Clock = Callable[[], float]
Sleep = Callable[[float], None]


# --------------------------------------------------------------------------------------------
# Processes
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Proc:
    """What the adapter needs to know about a process:  its pid and parent, its command words, its executable, and
    the few identity variables of its environment (IDENTITY_KEYS only)."""

    pid: int
    ppid: int
    argv: tuple[str, ...]
    exe: str
    env: Mapping[str, str] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return os.path.basename(self.argv[0]) if self.argv else os.path.basename(self.exe)


def default_proc_root() -> str | None:
    """/proc where there is one (Linux);  None means ask `ps` (macOS)."""
    return "/proc" if os.path.isdir("/proc/self") else None


def _read_bytes(path: str, limit: int = 1 << 20) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read(limit)
    except OSError:
        return b""


def _identity_env(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in pairs:
        key, eq, value = item.partition("=")
        if eq and key in IDENTITY_KEYS:
            out[key] = value.strip()
    return out


def read_proc(
    pid: int, proc_root: str | None = None, runner: Callable[..., Any] = subprocess.run
) -> Proc | None:
    """The process `pid`, or None when it is gone or cannot be read.  Linux reads /proc/<pid>; macOS asks the kernel
    for the arguments and the environment (`kernel_args`, each argument whole) and `ps` for the parent and the program,
    and where the kernel will not say, `ps` alone (`ps eww` appends the environment to the command line, of which only
    IDENTITY_KEYS are taken)."""
    if pid <= 1:
        return None
    root = proc_root if proc_root is not None else default_proc_root()
    if root is not None:
        base = os.path.join(root, str(pid))
        raw = _read_bytes(os.path.join(base, "cmdline"))
        stat = _read_bytes(os.path.join(base, "stat")).decode("utf-8", "replace")
        if not stat:
            return None
        try:
            fields = stat.rsplit(")", 1)[1].split()
            state, ppid = fields[0], int(fields[1])
        except (IndexError, ValueError):
            return None
        if state in ("Z", "X"):
            return None  # exited, not yet reaped
        argv = tuple(w.decode("utf-8", "replace") for w in raw.split(b"\0") if w)
        try:
            exe = os.readlink(os.path.join(base, "exe"))
        except OSError:
            exe = argv[0] if argv else ""
        env = _identity_env(
            [
                w.decode("utf-8", "replace")
                for w in _read_bytes(os.path.join(base, "environ")).split(b"\0")
                if w
            ]
        )
        return Proc(pid, ppid, argv, exe, env)
    try:
        listing = runner(
            ["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        head, _, exe = listing.partition(" ")
        if not head.strip().isdigit():
            return None
        kernel = kernel_args(pid)
        if kernel is not None:
            path, argv, strings = kernel
            return Proc(pid, int(head), argv, exe.strip() or path, _identity_env(strings))
        command = runner(
            ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5
        ).stdout.strip()
        full = runner(
            ["ps", "eww", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if not command:
        return None
    # The fallback: `ps` joins the arguments with spaces, so a quoted prompt looks like words.
    tail = full.strip()[len(command) :] if full.strip().startswith(command) else ""
    pairs = re.findall(r"(?:^|\s)(%s)=(\S*)" % "|".join(IDENTITY_KEYS), " " + tail)
    return Proc(pid, int(head), tuple(command.split()), exe.strip(), {k: v for k, v in pairs})


def parse_procargs(raw: bytes) -> tuple[str, tuple[str, ...], list[str]] | None:
    """Split a KERN_PROCARGS2 buffer into (the executable path, the arguments, the environment strings).  The buffer is
    argc as a native int, the executable path, NUL padding, argc arguments, then the environment, all NUL terminated."""
    if len(raw) < 8:
        return None
    argc = int.from_bytes(raw[:4], sys.byteorder)
    body = raw[4:]
    end = body.find(b"\0")
    if end < 0 or not 0 < argc <= 4096:
        return None
    start = end
    while start < len(body) and body[start] == 0:
        start += 1
    words = body[start:].split(b"\0")
    if len(words) < argc:
        return None
    return (
        body[:end].decode("utf-8", "replace"),
        tuple(w.decode("utf-8", "replace") for w in words[:argc]),
        [w.decode("utf-8", "replace") for w in words[argc:] if w],
    )


def kernel_args(pid: int) -> tuple[str, tuple[str, ...], list[str]] | None:
    """macOS: the arguments and the environment of a process of this user, each argument whole, from the kernel.  `ps`
    joins the arguments with spaces, so `muse "login page"` and `muse login page` look the same there.  None where the
    call does not exist (Linux) and where the kernel refuses it (another user's process), and the caller asks `ps`."""
    if sys.platform != "darwin":
        return None
    try:
        import ctypes
        import ctypes.util

        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib", use_errno=True)
        call = libc.sysctl
        call.restype = ctypes.c_int
        call.argtypes = [
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p,
            ctypes.c_size_t,
        ]
        mib = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, int(pid))
        size = ctypes.c_size_t(0)
        if call(mib, 3, None, ctypes.byref(size), None, 0) != 0 or size.value < 8:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if call(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            return None
        return parse_procargs(buf.raw[: size.value])
    except Exception:  # noqa: BLE001 - no libc symbol, no ctypes or a refusal: the caller falls back to `ps`
        return None


def muse_ancestor(
    start: int, proc_root: str | None = None, runner: Callable[..., Any] = subprocess.run
) -> Proc | None:
    """The nearest process from `start` upwards that is Muse (`muse`, `muse-bin-<version>` or the desktop `Muse`)."""
    pid = start
    for _ in range(ANCESTOR_DEPTH):
        proc = read_proc(pid, proc_root, runner)
        if proc is None:
            return None
        if MUSE_NAME.match(proc.name) or MUSE_NAME.match(os.path.basename(proc.exe)):
            return proc
        pid = proc.ppid
    return None


#: The `muse` options that take a value as the next word, so that value is not taken for a subcommand.
VALUE_FLAGS = frozenset(
    [
        "--agents",
        "--provider",
        "--preset",
        "--model",
        "--contributor-mode",
        "-c",
        "--config",
        "--reasoning-effort",
        "--effort",
        "--base-url",
        "--image",
        "--workspace",
        "--worktree-base",
        "--worktree-existing",
        "--approval-mode",
        "--permission-profile",
        "--approval-judge",
        "--sandbox-network",
        "--echo-delay-ms",
        "--prompt-file",
        "--output-schema",
        "--session-id",
        "--max-model-steps",
    ]
)
# `acp` is not in `muse --help` (a build without it answers "not available in this build"), but the Mac runs one as a
# host some program drives, so it is a host, never a chat.
SUBCOMMANDS = frozenset(
    [
        "resume",
        "exec",
        "config",
        "export",
        "trace",
        "model-profile",
        "skills",
        "voice",
        "plugins",
        "sandbox",
        "schema",
        "serve",
        "session-message",
        "mcp",
        "auth",
        "login",
        "logout",
        "usage",
        "init",
        "acp",
    ]
)
WORKTREE_MODES = ("off", "create", "existing")


def subcommand(argv: tuple[str, ...]) -> str:
    """The `muse` subcommand in a command line, or "" for the chat itself (`muse`, `muse "a prompt"`)."""
    i = 1
    while i < len(argv):
        word = argv[i]
        if word.startswith("-"):
            if (
                "=" not in word
                and word in VALUE_FLAGS
                or (
                    word in ("-w", "--worktree")
                    and i + 1 < len(argv)
                    and argv[i + 1] in WORKTREE_MODES
                )
            ):
                i += 1
            i += 1
            continue
        return word if word in SUBCOMMANDS else ""
    return ""


# --------------------------------------------------------------------------------------------
# The session store
# --------------------------------------------------------------------------------------------


def data_root(home: str, xdg_data_home: str = "") -> str:
    """Muse's data root:  $XDG_DATA_HOME/muse, else $HOME/.local/share/muse."""
    base = xdg_data_home if xdg_data_home else os.path.join(home, ".local", "share")
    return os.path.join(base, "muse")


def find_session_dir(root: str, sid: str) -> str | None:
    """The folder of a stored session (sessions/YYYY/MM/DD/<id>), or None.  The id is checked first, so it can never
    be a path."""
    if not SESSION_ID_RE.fullmatch(sid or ""):
        return None
    found = sorted(
        glob.glob(os.path.join(glob.escape(root), "sessions", "[0-9]*", "[0-9]*", "[0-9]*", sid))
    )
    return found[-1] if found else None


def is_subagent(root: str, sid: str) -> bool:
    """Whether `sid` is a sub-agent's session:  Muse stores one under its parent chat (sessions/Y/M/D/<chat>/subagent/
    <id>), never at the top level.  False for an id with a top-level folder, and for one the store does not hold."""
    if not SESSION_ID_RE.fullmatch(sid or "") or find_session_dir(root, sid) is not None:
        return False
    nested = os.path.join(
        glob.escape(root), "sessions", "[0-9]*", "[0-9]*", "[0-9]*", "*", "subagent", sid
    )
    return bool(glob.glob(nested))


def lock_holder(session_dir: str) -> tuple[int | None, str]:
    """(the pid in the session's `.session.lock`, the role it names).  The holder writes `pid=`, `host=` and, for a
    serve host, `role=serve`, and empties the file when it lets go, so an empty or missing file is no holder."""
    text = _read_bytes(os.path.join(session_dir, ".session.lock"), 4096).decode("utf-8", "replace")
    pid = re.search(r"^pid=(\d+)\s*$", text, re.MULTILINE)
    role = re.search(r"^role=(\S+)\s*$", text, re.MULTILINE)
    return (int(pid.group(1)) if pid else None, role.group(1) if role else "")


def pid_alive(
    pid: int, proc_root: str | None = None, runner: Callable[..., Any] = subprocess.run
) -> bool:
    return read_proc(pid, proc_root, runner) is not None


def lease_held(
    session_dir: str | None, fallback_pid: int | None, proc_root: str | None = None
) -> bool:
    """Whether a process still holds the session.  The lock file decides;  a store this helper cannot find falls back
    to the pid the hook recorded."""
    if session_dir is not None:
        pid, _ = lock_holder(session_dir)
        return pid is not None and pid_alive(pid, proc_root)
    return bool(fallback_pid) and pid_alive(int(fallback_pid or 0), proc_root)


# --------------------------------------------------------------------------------------------
# The Muse Session Protocol, over a `muse serve` host on stdio
# --------------------------------------------------------------------------------------------


def uuid7() -> str:
    """A version 7 UUID (the idempotency handle every MSP command carries)."""
    ms = int(time.time() * 1000)
    rand = uuid.uuid4().int
    value = (
        (ms << 80)
        | (0x7 << 76)
        | (((rand >> 64) & 0xFFF) << 64)
        | (0b10 << 62)
        | (rand & ((1 << 62) - 1))
    )
    return str(uuid.UUID(int=value))


FINAL_REASONS = ("name_invalid", "name_taken", "naming_unavailable", "unsupported_provider")


class HostError(RuntimeError):
    """The host could not be started, died, or stopped answering."""


class Refused(HostError):
    """The host answered with an error.  `kind` is its `data.kind`, `reason` its `data.reason` (for a rejected
    command), `code` the JSON-RPC code."""

    def __init__(self, code: int, kind: str, reason: str, message: str):
        super().__init__("%s %s %s" % (code, kind, reason or message[:80]))
        self.code, self.kind, self.reason = code, kind, reason

    @property
    def in_use(self) -> bool:
        return self.kind == "sessionInUse"

    @property
    def final(self) -> bool:
        """An answer that asking again cannot change."""
        return self.reason in FINAL_REASONS or self.kind in (
            "sessionNotFound",
            "capabilityRequired",
        )


class MspHost:
    """A `muse serve` host and the newline-delimited JSON-RPC conversation with it.  Standard library only:  the
    reads use `select`, so a host that goes quiet costs REQUEST_TIMEOUT, not a hang."""

    def __init__(
        self,
        argv: list[str],
        env: Mapping[str, str],
        cwd: str,
        *,
        timeout: float = REQUEST_TIMEOUT,
        popen: Callable[..., Any] = subprocess.Popen,
    ):
        self.timeout, self.next_id, self.buffer = timeout, 0, b""
        try:
            self.proc = popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=dict(env),
                cwd=cwd,
                close_fds=True,
            )
        except OSError as exc:
            raise HostError("the host did not start (%s)" % type(exc).__name__) from exc

    def _send(self, doc: Mapping[str, Any]) -> None:
        try:
            self.proc.stdin.write((json.dumps(doc) + "\n").encode("utf-8"))
            self.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise HostError("the host closed its input (%s)" % type(exc).__name__) from exc

    def _line(self, deadline: float) -> bytes:
        while b"\n" not in self.buffer:
            left = deadline - time.monotonic()
            if left <= 0:
                raise HostError("the host did not answer in time")
            ready, _, _ = select.select([self.proc.stdout], [], [], min(left, 1.0))
            if not ready:
                if self.proc.poll() is not None:
                    raise HostError("the host exited")
                continue
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise HostError("the host closed its output")
            self.buffer += chunk
        line, _, self.buffer = self.buffer.partition(b"\n")
        return line

    def request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """One request and its result;  notifications that arrive first are skipped.  An error answer raises Refused."""
        self.next_id += 1
        rid = self.next_id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": dict(params)})
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                doc = json.loads(self._line(deadline))
            except ValueError:
                continue
            if not isinstance(doc, dict) or doc.get("id") != rid:
                continue
            error = doc.get("error")
            if isinstance(error, dict):
                data = error.get("data") if isinstance(error.get("data"), dict) else {}
                raise Refused(
                    int(error.get("code") or 0),
                    str(data.get("kind") or ""),
                    str(data.get("reason") or ""),
                    str(error.get("message") or ""),
                )
            result = doc.get("result")
            return result if isinstance(result, dict) else {}

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            self.proc.wait(timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            try:
                self.proc.kill()
            except OSError:
                pass


HostFactory = Callable[[Mapping[str, Any]], Any]


def host_env(rec: Mapping[str, Any], base: Mapping[str, str]) -> dict[str, str]:
    """The environment of the helper's host:  the chat's HOME and data root, the helper mark, and a UTF-8 locale.
    Nothing else of the caller's environment is passed on."""
    env = {
        "HOME": str(rec.get("home") or base.get("HOME") or ""),
        "PATH": str(base.get("PATH") or "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        HELPER_MARK: "1",
        "MUSE_NO_AUTO_UPDATE": "1",
        "MUSE_LOGIN": "0",
    }
    if rec.get("xdg_data_home"):
        env["XDG_DATA_HOME"] = str(rec["xdg_data_home"])
    if base.get("AGENT_SYNC_STATE_DIR"):
        env["AGENT_SYNC_STATE_DIR"] = str(base["AGENT_SYNC_STATE_DIR"])
    return env


def read_stored_provider(host: Any, sid: str) -> tuple[str, str]:
    """(the `--provider` name, the model id or "") a host needs to carry the stored session as it was recorded.  Reads
    the session on `host` without resuming it, which takes no lease and records nothing.  Raises Refused
    (`unsupported_provider`, a final answer) for a provider this helper has no name for and for a model id of a shape
    it will not pass on."""
    host.request("initialize", {"clientInfo": {"name": "platform_letter", "version": "1"}})
    host.notify("initialized")
    session = host.request("session/read", {"sessionId": sid}).get("session")
    session = session if isinstance(session, dict) else {}
    recorded = str(session.get("providerId") or "").strip().lower()
    provider = HOST_PROVIDERS.get(recorded)
    if provider is None:
        raise Refused(
            0,
            "commandRejected",
            "unsupported_provider",
            "no host for the provider %s" % recorded[:40],
        )
    model = "" if provider == "echo" else str(session.get("modelId") or "").strip()
    if model and not MODEL_RE.fullmatch(model):
        raise Refused(
            0, "commandRejected", "unsupported_provider", "a model id this helper will not pass on"
        )
    return provider, model


def default_host(rec: Mapping[str, Any], base: Mapping[str, str] | None = None) -> MspHost:
    """The host that renames the session: one that runs the provider and model the session was recorded with (see the
    module docstring), with no shell, no workspace writes and no workspace trust.  A session recorded on the echo
    provider (no login, no network) gets an echo host."""
    exe = str(rec.get("exe") or "")
    if not exe or not os.access(exe, os.X_OK):
        exe = shutil.which("muse") or ""
    if not exe:
        raise HostError("no muse executable to run")
    cwd = str(rec.get("cwd") or "")
    cwd = cwd if os.path.isdir(cwd) else "/"
    env = host_env(rec, os.environ if base is None else base)
    inert = ["--disable-shell", "--disable-write"]
    probe = MspHost([exe, "serve", "--provider", "echo", *inert], env, cwd)
    try:
        provider, model = read_stored_provider(probe, str(rec.get("session_id") or ""))
    finally:
        probe.close()
    argv = [exe, "serve", "--provider", provider]
    if model:
        argv.append("--model=" + model)
    return MspHost([*argv, *inert], env, cwd)


def plan_name(
    label: str, want: str, applied: list[str], wrapped: list[str], current: str, origin: str
) -> tuple[str | None, bool]:
    """(the name to ask for, or None when the session already carries it; whether it wraps someone's own words).

    A session with no name, or one Muse named itself (`automatic`), takes `want`.  A custom name that is one this
    helper set earlier (`applied`) is replaced by `want`.  A custom name someone gave the chat by hand keeps their
    words, whole, and only gains the letter in front when it has none; a name this helper set around such words
    (`wrapped`) is theirs still, and a letter that changed is put in place of the old one."""
    wanted = " ".join(want.split())
    cur = current or ""
    low = cur.lower()
    around = low in {w.lower() for w in wrapped}
    if cur and origin == "custom" and (around or low not in {a.lower() for a in applied}):
        words = " ".join((LETTER_PREFIX.sub("", cur, count=1) if around else cur).split())
        if words:
            if label and not low.startswith(label.lower() + ": "):
                return label + ": " + words, True
            return None, False
    return (None if low == wanted.lower() else wanted), False


def choose_name(
    label: str,
    want: str,
    applied: list[str],
    current: str,
    origin: str,
    wrapped: list[str] | tuple[str, ...] = (),
) -> str | None:
    """The name to ask for, or None when the session already carries it (see `plan_name`)."""
    return plan_name(label, want, applied, list(wrapped), current, origin)[0]


def remember(
    applied: list[str], wrapped: list[str], name: str, hand: bool
) -> tuple[list[str], list[str]]:
    """File a name this helper just set: under `wrapped` when it carries someone's words, else under `applied`.  A name
    is in one list only, and the newest is last."""
    low = name.lower()
    applied = [a for a in applied if a.lower() != low]
    wrapped = [w for w in wrapped if w.lower() != low]
    (wrapped if hand else applied).append(name)
    return applied, wrapped


def rename_session(
    host: Any,
    sid: str,
    label: str,
    want: str,
    applied: list[str],
    wrapped: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    """Resume the stored session on `host`, read its name, rename it when it needs it, and leave.  Raises Refused
    (`in_use` while a chat still holds it) and HostError;  returns {"name": the name it carries now, "set": whether
    this call renamed it, "hand": whether the name it set wraps someone's own words}."""
    host.request("initialize", {"clientInfo": {"name": "platform_letter", "version": "1"}})
    host.notify("initialized")
    host.request("session/resume", {"commandId": uuid7(), "sessionId": sid, "excludeItems": True})
    try:
        session = host.request("session/read", {"sessionId": sid}).get("session")
        session = session if isinstance(session, dict) else {}
        current, origin = str(session.get("name") or ""), str(session.get("nameOrigin") or "")
        target, hand = plan_name(label, want, applied, list(wrapped), current, origin)
        if target is None:
            return {"name": current, "set": False, "hand": False}
        settled = host.request(
            "session/rename", {"commandId": uuid7(), "sessionId": sid, "name": target}
        )
        return {"name": str(settled.get("name") or target.lower()), "set": True, "hand": hand}
    finally:
        try:
            host.request("session/leave", {"commandId": uuid7(), "sessionId": sid})
        except HostError:
            pass


# --------------------------------------------------------------------------------------------
# The spool record
# --------------------------------------------------------------------------------------------


def read_record(path: str) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_record(path: str, rec: Mapping[str, Any]) -> bool:
    """Save the record (0600, atomically);  False when it cannot be saved."""
    body = {k: rec[k] for k in RECORD_KEYS if k in rec}
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(body, fh, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


def worker_alive(rec: Mapping[str, Any], proc_root: str | None = None) -> bool:
    """Whether the helper named in the record is still running (and is a helper, not a reused pid)."""
    pid = int(rec.get("worker_pid") or 0)
    proc = read_proc(pid, proc_root) if pid else None
    return proc is not None and any(
        os.path.basename(w) == os.path.basename(__file__) for w in proc.argv
    )


def start_worker(
    spool: str, home: str, base: Mapping[str, str], popen: Callable[..., Any] = subprocess.Popen
) -> int:
    """Start the helper detached (its own session, no inherited pipes, so the hook that starts it can exit and Muse
    does not wait on it).  Returns its pid, or 0 when it could not be started."""
    env = {
        "HOME": home,
        "PATH": str(base.get("PATH") or "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
    }
    if base.get("AGENT_SYNC_STATE_DIR"):
        env["AGENT_SYNC_STATE_DIR"] = str(base["AGENT_SYNC_STATE_DIR"])
    try:
        proc = popen(
            [sys.executable, os.path.abspath(__file__), "run", "--spool", spool],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd="/",
            close_fds=True,
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError):
        return 0
    return int(proc.pid)


# --------------------------------------------------------------------------------------------
# The helper
# --------------------------------------------------------------------------------------------


def _close_host(host: Any) -> None:
    close = getattr(host, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 - a host that will not close must not stop the job
            pass


def _settle(spool: str, state: str, now: float, **extra: Any) -> str:
    """Record the end of the job on the freshest record, so a title queued meanwhile is kept."""
    rec = read_record(spool)
    rec.update(state=state, worker_pid=0, done_at=now, **extra)
    write_record(spool, rec)
    return state


def run_worker(
    spool: str,
    *,
    now: Clock = time.time,
    sleep: Sleep = time.sleep,
    host_factory: Callable[[Mapping[str, Any]], Any] | None = None,
    proc_root: str | None = None,
    pid: int | None = None,
) -> str:
    """Wait for the lease, rename the session, record the outcome.  Returns the final state:  `done`, `failed` or
    `stale` (the record is gone, finished, or another helper has it).  Safe to run twice:  a second copy finds the
    first alive and leaves."""
    me = os.getpid() if pid is None else pid
    make_host = host_factory or default_host
    rec = read_record(spool)
    if not rec or rec.get("state") in ("done", "failed"):
        return "stale"
    other = int(rec.get("worker_pid") or 0)
    if other and other != me and worker_alive(rec, proc_root):
        return "stale"
    rec.update(worker_pid=me, state="waiting")
    write_record(spool, rec)
    sid = str(rec.get("session_id") or "")
    root = data_root(str(rec.get("home") or ""), str(rec.get("xdg_data_home") or ""))
    busy = 0  # answers of "in use" in a row while the lock file said the session was free
    while True:
        rec = read_record(spool)
        if int(rec.get("worker_pid") or 0) != me:
            return "stale"  # the record is gone, or another helper took the job
        t = now()
        if t - float(rec.get("queued_at") or t) > WAIT_MAX:
            return _settle(spool, "failed", t, error="the chat was still open after a week")
        sdir = find_session_dir(root, sid)
        if lease_held(sdir, rec.get("holder_pid"), proc_root):
            busy = 0
            sleep(LEASE_POLL)
            continue
        if not SESSION_ID_RE.fullmatch(sid):
            return _settle(spool, "failed", t, error="not a Muse session id")
        host = None
        try:
            host = make_host(rec)
            result = rename_session(
                host,
                sid,
                str(rec.get("label") or ""),
                str(rec.get("name") or ""),
                list(rec.get("applied") or []),
                list(rec.get("wrapped") or []),
            )
        except Refused as exc:
            if exc.in_use:
                busy += 1
                if busy > MAX_BUSY:
                    return _settle(spool, "failed", now(), error="the session stayed in use")
                sleep(LEASE_POLL)
                continue
            tried = _tried(spool, rec)
            if exc.final or tried >= ATTEMPTS:
                return _settle(spool, "failed", now(), attempts=tried, error=str(exc)[:200])
            sleep(RETRY_PAUSE)
            continue
        except HostError as exc:
            tried = _tried(spool, rec)
            if tried >= ATTEMPTS:
                return _settle(spool, "failed", now(), attempts=tried, error=str(exc)[:200])
            sleep(RETRY_PAUSE)
            continue
        finally:
            _close_host(host)
        latest = read_record(spool)
        applied, wrapped = list(latest.get("applied") or []), list(latest.get("wrapped") or [])
        if result["set"]:
            applied, wrapped = remember(applied, wrapped, result["name"], bool(result.get("hand")))
        latest.update(
            applied=applied[-8:],
            wrapped=wrapped[-8:],
            result={"name": result["name"], "set": result["set"]},
        )
        if latest.get("name") != rec.get("name"):
            latest.update(
                state="waiting", worker_pid=me
            )  # a newer title was queued while this one was applied
            write_record(spool, latest)
            continue
        write_record(spool, latest)
        return _settle(spool, "done", now(), error="")


def _tried(spool: str, rec: Mapping[str, Any]) -> int:
    """Count one failed try in the record and return the count."""
    fresh = read_record(spool) or dict(rec)
    fresh["attempts"] = int(fresh.get("attempts") or 0) + 1
    write_record(spool, fresh)
    return int(fresh["attempts"])


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 3 and args[0] == "run" and args[1] == "--spool":
        try:
            run_worker(args[2])
        except Exception:  # noqa: BLE001 - a detached helper has nobody to tell
            return 1
        return 0
    sys.stderr.write("usage: muse_session_rename.py run --spool FILE\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
