"""Codex (the CLI and Codex Desktop) on `platform-letter` (AFC-564, owner request Oct 29, 2026): an Operator session
letter, and the title "XX: Title", on every interactive Codex thread, on the Mac and on the server.  The library
(`platform_letter`) does the registering, the note, the retries and the title flow; this file is what is Codex's own.

What Codex gives a hook (read from the schemas the Codex binary embeds, and from live runs of codex-cli 0.163.0).
- A hook gets JSON on stdin: `session_id` (the thread id), `hook_event_name`, `cwd`, `transcript_path`, and `source`
  (startup, resume, clear, compact or fork) on SessionStart, `prompt` and `turn_id` on UserPromptSubmit.  It gets no
  title, no originator and no parent.  Those live in Codex's own store, `$CODEX_HOME/state_<n>.sqlite` (`threads`:
  `originator`, `source`, `thread_source`, `name`, `title`; `thread_spawn_edges`: parent and child), which the hook
  reads read-only.  The thread's row is already there at SessionStart.
- Hook output is context only: `hookSpecificOutput.additionalContext` (a field `sessionTitle` is not in the schema, so
  the spec never prints one).  A title therefore reaches the chat through the app-server: `thread/name/set`.
- A sub-agent's own thread fires none of the session hooks.  Its parent's hooks fire SubagentStart and SubagentStop,
  with `session_id` the PARENT's thread id and `agent_id` the sub-agent's.  So a sub-thread is registered from its
  parent's SubagentStart: it is the chat `agent_id`, its parent is `session_id`, and it takes the pair after its
  parent's (the parent is registered first when it holds no live registration).  SubagentStop fires at the end of
  EVERY turn of a sub-agent, and a follow-up turn (send_input, followup_task) fires no SubagentStart, so a stop never
  closes the sub-agent: its pair stays its own, as its title says, until the 36 hour sweep, and a later sibling takes
  the next pair after the parent's.  A stop is the only event that carries the sub-agent's id after its start, so it
  is also where a registration or a title that failed at the start is tried again, and where a touch is sent when
  none has been for half an hour; it prints nothing, because the SubagentStop output has no `hookSpecificOutput`.
- SessionEnd carries `reason: "other"` for every end (an exit, an idle unload in the desktop app, an archive), so an
  end says nothing about whether a chat is over, and no SessionEnd hook is installed.  An idle chat's letter is freed
  by the library's 36 hour sweep, and a resumed chat keeps its letter.  An archive is read from the store instead:
  `release` (below) frees the letter of each chat the registry holds for Codex whose row Codex has archived.

Naming a thread.  `rename_thread` puts the title in the thread's `name` (what Codex lists, resumes by and shows):
1. through the local app-server daemon, over `codex app-server proxy` (a WebSocket on stdio): about 0.05 seconds; where
   there is no daemon (the Mac has none), or when the daemon fails,
2. through a one-shot `codex app-server` over stdio: the same method, about 0.7 seconds.
Both were checked on a real thread (codex-cli 0.163.0): the name is in `threads.name` of the store afterwards, which is
what `rename_thread` reads back before it says it worked.  Appending to `session_index.jsonl` alone does not rename a
thread (the store keeps the old name), so it is not a route; the app-server writes that file itself on a rename.  A
name set from a second process while the thread's own process is running survives that process's later writes.  What it
does not do is refresh a TUI that is already open: its footer shows the name it last held until the chat is resumed
(the store, the resume picker and the next `codex resume` show the new one at once).

Who takes a letter.  A thread of the store whose originator is one of Codex's own programs (codex-tui, codex_cli_rs,
Codex Desktop, codex_vscode and the like, or none in a store from before the column), that is not an exec or MCP run,
and whose `thread_source` is `user` or empty (or a sub-agent whose parent is such a thread).  Every other originator is
a program that drives Codex as its engine (the Mac store shows Bitrig, editorts_native, botfleet and the MiniMax
bridge), and its threads take none, as do the SDK and exec drivers that carry Codex's name (`DRIVER_ORIGINATORS`).  A
thread the store does not show, or a store that cannot be read, takes none: the hook cannot tell the thread's owner,
so it stays out.  A session a launcher started never does (the library checks AGENT_LAUNCHER and AGENT_LAUNCH_SEAT).

Trust.  Codex runs a new hook only after its hash is recorded in `config.toml` (`[hooks.state."<file>:<event>:<group>:
<hook>"] trusted_hash`, the group counted from the top of the event's list, so the installer only ever appends).  The
fleet's lane-tools installer reports this step and never writes it, so `trust` is this adapter's own: it asks the
app-server for each hook's current hash (`hooks/list`) and records the hash of this adapter's own entries with
`config/batchWrite`, which keeps the file's comments and touches no other hook.  The installer reports the step and
never runs it: `python3 -m simple_tools.platform_letter_codex trust [--check]`.  Until it has run, the hooks are inert
and Codex opens its blocking "Hooks need review" prompt at the next start of a TUI (the unattended `codex resume`
seat included), so every `install` answer says whether this adapter's entries are trusted, `install --check` exits 0
only when they are installed, current and trusted, and any install that changes the command (a new `--location`, a new `--remote`) makes them
`modified` and needs `trust` again.
Each hook command ends in `|| true`.  Codex blocks a prompt when a UserPromptSubmit hook exits 2 with a message on
stderr, and `python3` exits 2 when the script it is asked to run is gone (the server's managed `platform-letter`
wrapper outlives a rollback of the release that shipped the script), so no exit status of ours may reach Codex.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import select
import shutil
import sqlite3
import struct
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO

from . import platform_letter as PL

PLATFORM = "codex"
SEAT = "CODEX"
SOURCE = "codex-letter-hook"

ENV_BIN = "CODEX_LETTER_BIN"  # the codex binary to ask, when it is not on the hook's PATH
ENV_HOME = "CODEX_HOME"
SUBAGENT_EVENTS = ("SubagentStart", "SubagentStop")
# Codex's own programs (the CLI and TUI, Codex Desktop, the VS Code extension) name their originator "codex_cli_rs",
# "codex-tui", "Codex Desktop", "codex_vscode" and the like, and a store from before the column has none.  Any other
# originator is a program that drives Codex as its engine (Bitrig, editorts_native, BotFleet, the MiniMax bridge), and
# its threads are that program's, not the fleet's chats.  The drivers that carry Codex's own name are listed here.
DRIVER_ORIGINATORS = ("codex_exec", "codex_sdk_ts")
# The originators whose threads belong to something else: BotFleet's bots (their own platform letters) and the
# MiniMax Codex bridge.  Exec runs and MCP runs are not chats at all.
SKIP_ORIGINATORS = ("botfleet", "minimax-codex-mcp", *DRIVER_ORIGINATORS)
HEADLESS_SOURCES = ("exec", "mcp")
# The `thread_source` of a chat someone talks to; an older store has none.  Anything else (an automation, a voice
# session, a guardian review) is not a chat.
# `agent_created_thread` is a chat Codex Desktop opened for the user from another chat's tool call (Codex AA on the
# Mac, Nov 10, 2026, is one):  the user talks to it like any other chat.
CHAT_THREAD_SOURCES = ("", "user", "agent_created_thread")

STORE_WAIT = 0.5  # seconds to wait for a locked store before reading nothing
RPC_SECONDS = 6.0  # the whole rename, both routes, inside the hook's own timeout (15 to 20 seconds)
DAEMON_SECONDS = 2.0  # the daemon route's share: it answers in milliseconds or not at all
CHILD_ROW_WAIT = 1.5  # a sub-agent's row is written a moment after its parent's SubagentStart
HOOK_TIMEOUTS = {
    "SessionStart": 20,
    "UserPromptSubmit": 15,
    "SubagentStart": 20,
    "SubagentStop": 15,
}

CLIENT_INFO = {"name": "platform-letter", "title": "Operator Session Letters", "version": "1"}
WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
# Where a Codex binary lives when the hook's PATH does not hold it (the desktop app starts hooks with a short PATH).
BIN_CANDIDATES = (
    ".local/bin/codex",
    "/opt/homebrew/bin/codex",
    "/usr/local/bin/codex",
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
    "/Applications/Codex.app/Contents/Resources/codex",
)


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _emit(stdout: TextIO, payload: Mapping[str, Any], code: int) -> int:
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return code


# --------------------------------------------------------------------------------------------
# Codex's own store (read-only)
# --------------------------------------------------------------------------------------------


def codex_home(env: Mapping[str, str]) -> str:
    return _text(env.get(ENV_HOME)) or os.path.join(
        _text(env.get("HOME")) or os.path.expanduser("~"), ".codex"
    )


def store_file(env: Mapping[str, str]) -> str:
    """The newest `state_<n>.sqlite` in the Codex home (n is the store's schema version), or ""."""
    home = codex_home(env)
    best, found = -1, ""
    try:
        names = os.listdir(home)
    except OSError:
        return ""
    for name in names:
        match = re.fullmatch(r"state_(\d+)\.sqlite", name)
        if match and int(match.group(1)) > best:
            best, found = int(match.group(1)), os.path.join(home, name)
    return found


@dataclass(frozen=True)
class Thread:
    """One row of Codex's `threads` table, as far as the letters need it.  `source` is "cli", "vscode", "exec" or a
    JSON object for a sub-agent; `parent` is the thread that spawned it (the spawn edge, else the source's own)."""

    id: str
    source: str = ""
    originator: str = ""
    thread_source: str = ""
    title: str = ""
    name: str = ""
    first_message: str = ""
    nickname: str = ""
    agent_path: str = ""
    parent: str = ""


def _s(value: object) -> str:
    return value if isinstance(value, str) else ""


def _spawned_by(source: str) -> str:
    """The parent thread named in a sub-agent's `source` JSON ({"subagent": {"thread_spawn": {"parent_thread_id"..."""
    try:
        doc = json.loads(source) if source.startswith("{") else {}
        return _s(doc["subagent"]["thread_spawn"]["parent_thread_id"])
    except (ValueError, KeyError, TypeError):
        return ""


def thread_row(env: Mapping[str, str], sid: str) -> Thread | None:
    """The thread `sid`, or None when the store has no such row or cannot be read.  Read-only (mode=ro), a short wait
    for a lock, and nothing here ever raises: a hook never blocks a chat."""
    path = store_file(env)
    if not path or not sid:
        return None
    try:
        conn = sqlite3.connect(
            Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=STORE_WAIT
        )
    except (sqlite3.Error, OSError, ValueError):
        return None
    conn.row_factory = sqlite3.Row
    try:
        try:
            row = conn.execute(
                "SELECT source, originator, thread_source, title, name, first_user_message, agent_nickname,"
                " agent_path FROM threads WHERE id = ?",
                (sid,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such column" not in str(exc):
                raise  # a locked store is not a store version without one of those columns
            row = conn.execute("SELECT * FROM threads WHERE id = ?", (sid,)).fetchone()
        if row is None:
            return None
        data = {key: row[key] for key in row.keys()}
        edge = ""
        try:
            hit = conn.execute(
                "SELECT parent_thread_id FROM thread_spawn_edges WHERE child_thread_id = ?", (sid,)
            ).fetchone()
            edge = _s(hit[0]) if hit else ""
        except sqlite3.Error:
            pass
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    source = _s(data.get("source"))
    return Thread(
        id=sid,
        source=source,
        originator=_s(data.get("originator")),
        thread_source=_s(data.get("thread_source")),
        title=_s(data.get("title")),
        name=PL.one_line(data.get("name")),
        first_message=_s(data.get("first_user_message")),
        nickname=_s(data.get("agent_nickname")),
        agent_path=_s(data.get("agent_path")),
        parent=edge or _spawned_by(source),
    )


def own_originator(originator: str) -> bool:
    """Whether an originator is one of Codex's own programs (or empty, in a store from before the column) and not a
    program that drives Codex as its engine."""
    name = originator.strip().lower()
    return name not in SKIP_ORIGINATORS and (name == "" or name.startswith("codex"))


def classify(thread: Thread | None) -> str:
    """ "chat", "subagent" or "headless" for a thread of the store.  A thread the store does not show is headless:
    without its row the hook cannot rule out a BotFleet bot or an exec run, so it takes no letter."""
    if thread is None:
        return "headless"
    if not own_originator(thread.originator):
        return "headless"
    if thread.source in HEADLESS_SOURCES:
        return "headless"
    if thread.thread_source not in (*CHAT_THREAD_SOURCES, "subagent"):
        return "headless"  # an automation, a voice session, a guardian review
    if thread.thread_source == "subagent" or thread.source.startswith('{"subagent"'):
        return "subagent"
    return "chat"


def task_name(thread: Thread) -> str:
    """The words a sub-thread stands under: its task (the last part of the path its parent gave it, "/root/pong" is
    "Pong"), else the nickname Codex gave it."""
    task = thread.agent_path.rstrip("/").rsplit("/", 1)[-1]
    words = re.sub(r"[_\-\s]+", " ", task).strip()
    if words and words != "root":
        return PL.clean_title(PL.title_case(words.split(" ")))
    return PL.clean_title(thread.nickname)


# --------------------------------------------------------------------------------------------
# The accessors the library asks
# --------------------------------------------------------------------------------------------


def _event(payload: Mapping[str, Any]) -> str:
    return _text(payload.get("hook_event_name"))


def codex_session_id(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """The chat a hook event is about.  For SubagentStart and SubagentStop that is the sub-agent (`agent_id`), whose
    own thread runs no session hooks; every other event is its thread's (`session_id`, else CODEX_THREAD_ID for
    `platform-letter show` inside a Codex shell)."""
    if _event(payload) in SUBAGENT_EVENTS:
        return _text(payload.get("agent_id"))
    return PL.either(PL.from_payload("session_id"), PL.from_env("CODEX_THREAD_ID"))(payload, env)


def codex_parent(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    if _event(payload) in SUBAGENT_EVENTS:
        return _text(payload.get("session_id"))
    thread = thread_row(env, codex_session_id(payload, env))
    return thread.parent if thread else ""


def codex_kind(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    if _event(payload) in SUBAGENT_EVENTS:
        # Whether the sub-agent is lettered is decided by the chat that started it.  Its own row, when it is already
        # there, can still rule it out (a guardian review is spawned like a sub-agent and is no chat).
        parent = classify(thread_row(env, _text(payload.get("session_id"))))
        if parent not in ("chat", "subagent"):
            return "headless"
        child = thread_row(env, _text(payload.get("agent_id")))
        if child is not None and classify(child) == "headless":
            return "headless"
        return "subagent"
    if payload.get("agent_id") or payload.get("agent_type"):
        return "headless"  # an event in a sub-agent's turn is not its parent chat's own
    return classify(thread_row(env, codex_session_id(payload, env)))


def _wait_for_row(env: Mapping[str, str], sid: str, seconds: float) -> Thread | None:
    end = time.monotonic() + seconds
    while True:
        thread = thread_row(env, sid)
        if thread is not None or time.monotonic() >= end:
            return thread
        time.sleep(0.1)


def codex_current_title(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """The thread's name as Codex shows it now.  A sub-thread has none until it gets one, so its task stands in."""
    sid = codex_session_id(payload, env)
    if not sid:
        return ""  # a malformed event names no thread:  do not spend the hook's time waiting for a row that cannot appear
    thread = (
        _wait_for_row(env, sid, CHILD_ROW_WAIT)
        if _event(payload) == "SubagentStart"
        else thread_row(env, sid)
    )
    if thread is None:
        return ""
    if thread.name:
        return thread.name
    return task_name(thread) if classify(thread) == "subagent" else ""


def codex_stored_title(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """The words of a resumed thread that was never named: its first message, as a short title."""
    thread = thread_row(env, codex_session_id(payload, env))
    if thread is None:
        return ""
    return thread.name or PL.title_from_prompt(thread.first_message or thread.title)


# --------------------------------------------------------------------------------------------
# The app-server (JSON-RPC), for naming a thread and for trust
# --------------------------------------------------------------------------------------------


class RpcError(RuntimeError):
    """The app-server did not answer the way the hook needs.  The message never holds a request's contents."""


class Rpc:
    """One short conversation with a Codex app-server over a child process: `codex app-server proxy` (the daemon's
    socket, which speaks WebSocket) or `codex app-server` (stdio, one JSON object per line).  A hard deadline covers
    the whole conversation.  Messages have no "jsonrpc" field in this protocol."""

    def __init__(
        self,
        argv: list[str],
        env: Mapping[str, str],
        deadline: float,
        *,
        framing: str,
        popen: Any = subprocess.Popen,
    ):
        self.framing, self.deadline, self.buf, self.seq = framing, deadline, b"", 0
        self.proc = popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(env),
        )

    def __enter__(self) -> Rpc:
        try:
            if self.framing == "ws":
                self._handshake()
            self.call("initialize", {"clientInfo": CLIENT_INFO})
            self._send({"method": "initialized"})
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- bytes -----------------------------------------------------------------------------

    def _write(self, data: bytes) -> None:
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise RpcError("the app-server closed its input") from exc

    def _fill(self, need: int) -> None:
        fd = self.proc.stdout.fileno()
        while len(self.buf) < need:
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise RpcError("the app-server did not answer in time")
            ready, _, _ = select.select([fd], [], [], min(left, 0.5))
            if not ready:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                raise RpcError("the app-server closed its output")
            self.buf += chunk

    def _take(self, count: int) -> bytes:
        self._fill(count)
        out, self.buf = self.buf[:count], self.buf[count:]
        return out

    # ---- WebSocket (client side, RFC 6455: masked text frames, no extensions) ---------------

    def _handshake(self) -> None:
        key = base64.b64encode(os.urandom(16))
        self._write(
            b"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Key: " + key + b"\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        while b"\r\n\r\n" not in self.buf:
            self._fill(len(self.buf) + 1)
        head, _, self.buf = self.buf.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        if not lines[0].startswith("HTTP/1.1 101"):
            raise RpcError("the app-server refused the WebSocket upgrade")
        headers = {
            k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])
        }
        accept = base64.b64encode(hashlib.sha1(key + WS_GUID).digest()).decode()
        if headers.get("sec-websocket-accept") != accept:
            raise RpcError("the app-server answered the WebSocket upgrade wrongly")

    def _frame(self, opcode: int, payload: bytes) -> None:
        size = len(payload)
        head = bytes([0x80 | opcode])
        if size < 126:
            head += bytes([0x80 | size])
        elif size < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", size)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", size)
        mask = os.urandom(4)
        self._write(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _ws_message(self) -> bytes:
        message = b""
        while True:
            first, second = self._take(2)
            size = second & 0x7F
            if size == 126:
                size = struct.unpack(">H", self._take(2))[0]
            elif size == 127:
                size = struct.unpack(">Q", self._take(8))[0]
            mask = self._take(4) if second & 0x80 else b""
            body = self._take(size)
            if mask:
                body = bytes(b ^ mask[i % 4] for i, b in enumerate(body))
            opcode = first & 0x0F
            if opcode == 9:  # ping
                self._frame(10, body)
            elif opcode == 8:
                raise RpcError("the app-server closed the connection")
            elif opcode in (0, 1, 2):
                message += body
                if first & 0x80:
                    return message

    # ---- messages --------------------------------------------------------------------------

    def _send(self, obj: Mapping[str, Any]) -> None:
        data = json.dumps(obj).encode("utf-8")
        if self.framing == "ws":
            self._frame(1, data)
        else:
            self._write(data + b"\n")

    def _recv(self) -> dict[str, Any]:
        if self.framing == "ws":
            raw = self._ws_message()
        else:
            while b"\n" not in self.buf:
                self._fill(len(self.buf) + 1)
            raw, _, self.buf = self.buf.partition(b"\n")
        try:
            msg = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise RpcError("the app-server sent something that is not JSON") from exc
        return msg if isinstance(msg, dict) else {}

    def call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """One request, and its result.  Notifications that arrive first are read and dropped, and so is a request of
        the server's own (it has a method, and an id of its own that can equal ours)."""
        self.seq += 1
        want = self.seq
        self._send({"id": want, "method": method, "params": params})
        while True:
            msg = self._recv()
            if msg.get("id") != want or "method" in msg:
                continue
            if "error" in msg:
                error = msg["error"] if isinstance(msg["error"], dict) else {}
                raise RpcError("%s failed (code %s)" % (method, error.get("code")))
            result = msg.get("result")
            return result if isinstance(result, dict) else {}

    def close(self) -> None:
        if self.framing == "ws":
            try:
                self._frame(8, b"")
            except (RpcError, OSError):
                pass
        for stream in (self.proc.stdin, self.proc.stdout):
            try:
                stream.close()
            except (OSError, ValueError):
                pass
        try:
            self.proc.wait(timeout=1.0)
        except (subprocess.TimeoutExpired, OSError):
            self.proc.kill()
            try:
                self.proc.wait(timeout=1.0)
            except (subprocess.TimeoutExpired, OSError):
                pass


def codex_binary(env: Mapping[str, str]) -> str:
    """The codex program to ask: CODEX_LETTER_BIN, else the PATH, else where Codex is usually installed."""
    named = _text(env.get(ENV_BIN))
    if named:
        return named if os.access(named, os.X_OK) else ""
    found = shutil.which("codex", path=env.get("PATH") or os.defpath)
    if found:
        return found
    home = _text(env.get("HOME")) or os.path.expanduser("~")
    for candidate in BIN_CANDIDATES:
        path = candidate if os.path.isabs(candidate) else os.path.join(home, candidate)
        if os.access(path, os.X_OK):
            return path
    return ""


def daemon_socket(env: Mapping[str, str]) -> str:
    """The local app-server daemon's control socket when there is one (the server has it, the Mac does not)."""
    path = os.path.join(codex_home(env), "app-server-control", "app-server-control.sock")
    return path if os.path.exists(path) else ""


def open_rpc(
    env: Mapping[str, str], deadline: float, *, daemon: bool, popen: Any = subprocess.Popen
) -> Rpc:
    """A conversation with the daemon (`daemon`) or with a one-shot app-server of our own, started and not yet
    initialized: `with open_rpc(...) as rpc` connects and initializes it, and closes it at the end."""
    binary = codex_binary(env)
    if not binary:
        raise RpcError("no codex program to ask")
    argv = [binary, "app-server", "proxy"] if daemon else [binary, "app-server"]
    return Rpc(argv, env, deadline, framing="ws" if daemon else "lines", popen=popen)


def rename_thread(
    thread_id: str, name: str, env: Mapping[str, str], *, popen: Any = subprocess.Popen
) -> bool:
    """Set a thread's name (`thread/name/set`), over the daemon when there is one and else, or when that fails,
    through a one-shot app-server.  True once the store shows the name (or, when the store cannot be read, once
    the app-server has accepted it)."""
    title = PL.tidy(name)
    if not thread_id or not title:
        return False
    start = time.monotonic()
    routes: list[tuple[bool, float]] = []
    if daemon_socket(env):
        routes.append((True, DAEMON_SECONDS))
    routes.append((False, RPC_SECONDS))
    for daemon, share in routes:
        left = start + RPC_SECONDS - time.monotonic()
        if left <= 0.2:
            break
        try:
            with open_rpc(
                env, time.monotonic() + min(share, left), daemon=daemon, popen=popen
            ) as rpc:
                rpc.call("thread/name/set", {"threadId": thread_id, "name": title})
        except (RpcError, OSError, ValueError):
            continue
        row = thread_row(env, thread_id)
        if row is None or row.name == title:
            return True
    return False


def codex_rename(req: PL.TitleRequest) -> bool:
    """The spec's title writer: put "XX: Words" on the thread (a sub-thread's own id, not its parent's)."""
    return rename_thread(req.session_id, req.title, req.env)


# --------------------------------------------------------------------------------------------
# The spec and the installer
# --------------------------------------------------------------------------------------------


def codex_chat_payload(sid: str) -> dict[str, str]:
    """A chat's own start event, as a resume sends it: how the library asks the accessors about a sub-thread's parent
    (its parent in turn, its words, whether it is a chat at all)."""
    return {"hook_event_name": "SessionStart", "source": "resume", "session_id": sid}


SPEC = PL.PlatformSpec(
    platform=PLATFORM,
    seat=SEAT,
    source=SOURCE,
    display="Codex",
    session_id=codex_session_id,
    parent_id=codex_parent,
    kind=codex_kind,
    current_title=codex_current_title,
    stored_title=codex_stored_title,
    # SubagentStop fires at the end of every turn of a sub-agent, not once, and a follow-up turn fires no
    # SubagentStart, so it is a sign of life (retry, touch) and never an end.
    events={
        "SessionStart": "start",
        "UserPromptSubmit": "prompt",
        "SubagentStart": "start",
        "SubagentStop": "prompt",
    },
    quiet_events=("SubagentStop",),
    chat_payload=codex_chat_payload,
    title_writer=codex_rename,
)


def codex_entry(command: str, timeout: int, event: str) -> dict[str, Any]:
    """One hook entry.  The command ends in `|| true`: Codex refuses a prompt when a hook exits 2 with a message on
    stderr, and the interpreter exits 2 when the script it is told to run is gone (a server rollback leaves the
    managed wrapper without its target), so no exit status of ours may reach Codex."""
    return PL.command_entry(command + " || true", timeout, event)


INSTALL = PL.JsonHookInstaller(
    path=".codex/hooks.json",
    events=HOOK_TIMEOUTS,
    build=codex_entry,
    trust=(
        "Codex runs a new hook only after its hash is trusted, and until then its next TUI start stops at a "
        "'Hooks need review' prompt: run `python3 -m simple_tools.platform_letter_codex trust` from the scripts folder "
        "of the checkout this host runs right after the install (`trust --check` first, to see the state; it "
        "records only this adapter's own entries)"
    ),
)


# --------------------------------------------------------------------------------------------
# Trust
# --------------------------------------------------------------------------------------------


def hook_states(env: Mapping[str, str], *, popen: Any = subprocess.Popen) -> list[dict[str, Any]]:
    """Every hook the app-server finds for the user's hook file: key, event, trustStatus, currentHash, command."""
    with open_rpc(env, time.monotonic() + 15.0, daemon=False, popen=popen) as rpc:
        result = rpc.call("hooks/list", {"cwds": []})
    hooks: list[dict[str, Any]] = []
    for entry in result.get("data") or []:
        for hook in entry.get("hooks") or []:
            if isinstance(hook, dict):
                hooks.append(hook)
    return hooks


def _ours(hook: Mapping[str, Any], path: str) -> bool:
    """A hook of the user's own hook file (not a plugin's, not a project's) that is this adapter's command."""
    source = _text(hook.get("sourcePath"))
    command = _text(hook.get("command"))
    return (
        bool(command)
        and os.path.realpath(source) == os.path.realpath(path)
        and PL.installed_command(command, PLATFORM) is not None
    )


def trust_state(env: Mapping[str, str], *, popen: Any = subprocess.Popen) -> dict[str, str] | None:
    """The trust status of each of this adapter's hook entries (key to trusted, managed, untrusted or modified), or
    None when the app-server cannot be asked."""
    path = os.path.join(codex_home(env), "hooks.json")
    try:
        mine = [h for h in hook_states(env, popen=popen) if _ours(h, path)]
        return {h["key"]: _text(h.get("trustStatus")) for h in mine}
    except (RpcError, OSError, ValueError, KeyError):
        return None


def install(
    opts: PL.InstallOptions,
    env: Mapping[str, str],
    stdout: TextIO,
    *,
    popen: Any = subprocess.Popen,
) -> int:
    """`platform-letter install --platform codex`: the shared JSON installer, and Codex's trust state beside it.

    An install adds hooks that stay inert until `trust` has run, and Codex stops at a blocking review prompt at the
    next start of a TUI meanwhile, which the installer's own answer cannot tell from a finished install.  So every
    answer that is not an error or a remove also carries `trusted` (true, false, or null when the app-server could not
    be asked or found none of our entries), `trust_status` (key to status) and, unless trusted, the `trust` step to
    run.  `--check` exits 0 only when every hook is current AND trusted.  The installer still never runs the step."""
    buf = io.StringIO()
    code = PL.install_json_hooks(INSTALL, opts, env, buf)
    answer = PL._last_json(buf.getvalue())
    if answer is None or opts.remove or "error" in answer or code != 0:
        stdout.write(buf.getvalue())
        return code
    state = trust_state(env, popen=popen)
    trusted: bool | None = None
    if state:
        trusted = all(v in ("trusted", "managed") for v in state.values())
        answer["trust_status"] = state
    answer["trusted"] = trusted
    if trusted is not True:
        answer["trust"] = INSTALL.trust
    if opts.check and (trusted is not True or not all(answer.get("current", {}).values())):
        code = (
            1  # an install would still change the file, or the hooks are not trusted yet: not done
        )
    return _emit(stdout, answer, code)


def trust_main(
    argv: list[str], env: Mapping[str, str], stdout: TextIO, *, popen: Any = subprocess.Popen
) -> int:
    """`trust [--check]`: record the trust hash of this adapter's hook entries in Codex's config.toml.

    Only entries whose command is this adapter's `platform-letter hook` and whose status is untrusted or modified are
    written (`config/batchWrite`, merged into `hooks.state`), so no other hook's trust changes.  The config file is
    copied first to `config.toml.bak-letter-<UTC stamp>` (0600), like the hook file the installer rewrites.  Exit 0
    when every one of this adapter's entries is trusted afterwards (with --check: already), else 1; 2 for bad
    arguments, a backup that cannot be made (nothing is written then) or an app-server that cannot be reached."""
    check = "--check" in argv
    if any(a not in ("--check",) for a in argv):
        return _emit(stdout, {"error": "usage: trust [--check]"}, 2)
    path = os.path.join(codex_home(env), "hooks.json")
    config = os.path.join(codex_home(env), "config.toml")
    backup = ""
    try:
        mine = [h for h in hook_states(env, popen=popen) if _ours(h, path)]
        pending = [h for h in mine if h.get("trustStatus") in ("untrusted", "modified")]
        written: list[str] = []
        if pending and not check:
            try:
                backup = PL._backup(config) if os.path.isfile(config) else ""
            except OSError as exc:
                return _emit(
                    stdout,
                    {"error": "config.toml could not be backed up (%s)" % type(exc).__name__},
                    2,
                )
            edits = {h["key"]: {"trusted_hash": h["currentHash"]} for h in pending}
            with open_rpc(env, time.monotonic() + 15.0, daemon=False, popen=popen) as rpc:
                rpc.call(
                    "config/batchWrite",
                    {
                        "edits": [
                            {"keyPath": "hooks.state", "mergeStrategy": "upsert", "value": edits}
                        ]
                    },
                )
            written = sorted(edits)
            mine = [h for h in hook_states(env, popen=popen) if _ours(h, path)]
    except (RpcError, OSError, ValueError, KeyError) as exc:
        return _emit(
            stdout, {"error": "the app-server could not be asked (%s)" % type(exc).__name__}, 2
        )
    status = {h["key"]: h.get("trustStatus") for h in mine}
    answer = {"platform": PLATFORM, "path": path, "hooks": status, "written": written}
    if backup:
        answer["backup"] = backup
    ok = bool(mine) and all(v in ("trusted", "managed") for v in status.values())
    return _emit(stdout, answer, 0 if ok else 1)


# --------------------------------------------------------------------------------------------
# Archive release
# --------------------------------------------------------------------------------------------

RELEASE_SOURCE = "codex-archive-release"
# One page of the registry's letter history, newest letter first:  the API pages by limit only and has no live-only
# filter, so a live registration whose letter is older than the newest HISTORY_LIMIT letters of Codex is missed (the
# 36 hour sweep still frees it), never wrongly closed.  A full page is reported as `truncated`.
HISTORY_LIMIT = 5000
ANCESTOR_DEPTH = 8  # how far up a sub-thread's parents are followed to find an archived chat
SID_SHAPE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
USAGE = (
    "usage: python3 -m simple_tools.platform_letter_codex trust [--check]\n"
    "       python3 -m simple_tools.platform_letter_codex release [--apply] [--location mac|server] [--db FILE]"
    " [--store FILE]\n"
)


class StoreUnreadable(RuntimeError):
    """The Codex store could not be read, or has no `archived` column:  nothing may be released on its word."""


def _archive_state(conn: sqlite3.Connection, sid: str) -> tuple[bool, str] | None:
    """(archived, parent) for one thread of the store, or None when the store has no such row."""
    row = conn.execute("SELECT archived, source FROM threads WHERE id = ?", (sid,)).fetchone()
    if row is None:
        return None
    parent = ""
    try:
        hit = conn.execute(
            "SELECT parent_thread_id FROM thread_spawn_edges WHERE child_thread_id = ?", (sid,)
        ).fetchone()
        parent = _s(hit[0]) if hit else ""
    except sqlite3.OperationalError:
        pass  # a store from before the spawn edges:  the source's own parent stands in
    return bool(row[0]), parent or _spawned_by(_s(row[1]))


def archive_reason(conn: sqlite3.Connection, sid: str) -> str:
    """Why the thread `sid` is over, from the store alone:  "archived" when Codex has archived it, "parent archived"
    when it is a sub-thread one of whose parents Codex has archived, "" when it is not archived (or the store does not
    show it, which is no evidence of an archive either).  Raises sqlite3.Error for a store that cannot be read."""
    state = _archive_state(conn, sid)
    if state is None:
        return ""
    archived, parent = state
    if archived:
        return "archived"
    seen = {sid}
    for _ in range(ANCESTOR_DEPTH):
        if not parent or parent in seen:
            return ""
        seen.add(parent)
        up = _archive_state(conn, parent)
        if up is None:
            return ""
        if up[0]:
            return "parent archived"
        parent = up[1]
    return ""


def open_store(path: str) -> sqlite3.Connection:
    """The store, read-only, after checking it has the `archived` column the release reads."""
    if not path or not os.path.isfile(path):
        raise StoreUnreadable("no Codex store at %s" % (path or "the Codex home"))
    try:
        conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
    except sqlite3.Error as exc:
        raise StoreUnreadable(
            "the Codex store could not be read (%s)" % type(exc).__name__
        ) from exc
    if "archived" not in cols:
        conn.close()
        raise StoreUnreadable("the Codex store has no archived column")
    return conn


def live_registrations(
    env: Mapping[str, str], location: str, db: str
) -> tuple[list[dict[str, Any]], str]:
    """The live Codex registrations of this host, newest letter first, and "" (or [] and an error).  When the page of
    history was full, a last row `{"truncated": True}` says older letters were not read.

    The registry API answers when its URL is configured (the server's store, the one store now), else the local
    registry file;  a host with neither has nothing to release."""
    from . import operator_registry as OR
    from . import registry_api as RA

    if RA.base_url(env):
        answer = RA.call(env, "history", {"platform": PLATFORM, "limit": HISTORY_LIMIT})
        items = answer.get("items") if isinstance(answer, dict) else None
        if not isinstance(items, list):
            code = answer.get("code") if isinstance(answer, dict) else ""
            return [], "the registry history could not be read (%s)" % (code or "no items")
    elif os.path.isfile(db):
        try:
            items = OR.letter_history(db, PLATFORM, HISTORY_LIMIT)
        except (sqlite3.Error, OSError, OR.RegistryUnavailable) as exc:
            return [], "the registry could not be read (%s)" % type(exc).__name__
    else:
        return [], "no Operator registry on this host"
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in items:
        row = row if isinstance(row, dict) else {}
        sid = _s(row.get("native_session_id"))
        if (
            SID_SHAPE.fullmatch(sid)
            and sid not in seen
            and _s(row.get("verified_host")) == location
            and row.get("live", row.get("retired_at") is None)
            and not row.get("closed_at")
            and _s(row.get("state")) != "closed"
        ):
            seen.add(sid)
            out.append(row)
    if len(items) >= HISTORY_LIMIT:
        out.append({"truncated": True})  # a full page:  older live registrations were not read
    return out, ""


def _close_note(env: Mapping[str, str], sid: str, reason: str) -> None:
    """Mark the hook's note for a released chat released, so it no longer reads as holding its old letter.  Best effort:  the
    registry is the record, and the chat's next start or prompt registers it again if it is opened."""
    path = PL.note_path(env, SEAT, sid)
    try:
        with open(path, encoding="utf-8") as fh:
            note = json.load(fh)
        if not isinstance(note, dict) or note.get("state") != "open":
            return
        # Not "closed":  a closed note makes the hook ignore the chat's prompts, while any state that is not "open"
        # makes its next prompt (or a sub-agent's next SubagentStop) register it again, which is what a chat that
        # is unarchived and used again needs.
        note["state"] = "released"
        note["released"] = {"reason": reason, "at": time.time()}
        PL._write_private(path, (json.dumps(note, sort_keys=True) + "\n").encode("utf-8"), 0o600)
    except (OSError, ValueError):
        pass


def release_main(argv: list[str], env: Mapping[str, str], stdout: TextIO) -> int:
    """`release [--apply] [--location mac|server] [--db FILE] [--store FILE]`:  free the letter of each chat the
    registry holds for Codex on this host whose thread Codex has archived (`threads.archived`), and of each sub-thread
    one of whose parents it has archived.

    Only the registry's live Codex registrations of this host are looked at, so a thread the registry does not hold
    is never touched, however many archived rows the store has.  A registration whose thread the store does not show
    is kept (that is no evidence of an archive), as is every thread that is not archived.  The store is read again
    right before each close, so a chat unarchived in the meantime keeps its letter.  Without --apply it only reports.
    A registration already closed is the happy no-op, so a rerun is safe.  Exit 0, 1 when a close failed, 2 when the
    store or the registry could not be read (nothing is closed then)."""
    import argparse

    from . import operator_registry as OR
    from . import registry_api as RA

    parser = argparse.ArgumentParser(prog="platform_letter_codex release", add_help=False)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--location", default="", choices=("",) + PL.LOCATIONS)
    parser.add_argument("--db", default="")
    parser.add_argument("--store", default="")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return _emit(stdout, {"error": "bad arguments"}, 2)
    location = args.location or PL.location(env) or ""
    if location not in PL.LOCATIONS:
        return _emit(stdout, {"error": "no host location (pass --location mac or server)"}, 2)
    db = args.db or OR.registry_db_path(PL.L.state_root(env), env)
    if not args.db and not RA.base_url(env) and not os.path.isfile(db):
        env = PL.with_registry_env(env)  # the hook's own rule:  the host's shared registry env file
    try:
        conn = open_store(args.store or store_file(env))
    except StoreUnreadable as exc:
        return _emit(stdout, {"error": str(exc)}, 2)
    try:
        rows, error = live_registrations(env, location, db)
        if error:
            return _emit(stdout, {"error": error}, 2)
        truncated = any(row.get("truncated") for row in rows)
        rows = [row for row in rows if not row.get("truncated")]
        due: list[tuple[str, str]] = []
        for row in rows:
            sid = _s(row.get("native_session_id"))
            reason = archive_reason(conn, sid)
            if reason:
                due.append((sid, reason))
    except sqlite3.Error as exc:
        conn.close()
        return _emit(
            stdout, {"error": "the Codex store could not be read (%s)" % type(exc).__name__}, 2
        )
    released: list[dict[str, Any]] = []
    absent: list[str] = []
    failed: list[str] = []
    kept: list[str] = []
    try:
        for sid, reason in due:
            if not args.apply:
                released.append({"session_id": sid, "reason": reason, "would_release": True})
                continue
            try:
                if archive_reason(conn, sid) != reason:
                    kept.append(
                        sid
                    )  # unarchived (or changed) since the first read:  it keeps its letter
                    continue
            except sqlite3.Error:
                failed.append(sid)
                continue
            buf = io.StringIO()
            argv_close = [
                "close",
                "--platform",
                PLATFORM,
                "--location",
                location,
                f"--session-id={sid}",
                "--source",
                RELEASE_SOURCE,
            ]
            if args.db:
                argv_close += ["--db", args.db]
            code = PL.registry_main(argv_close, env=env, stdout=buf)
            answer = PL._last_json(buf.getvalue()) or {}
            if code == 0 and answer.get("closed"):
                released.append(
                    {
                        "session_id": sid,
                        "reason": reason,
                        "released": True,
                        "label": _s(answer.get("label")),
                    }
                )
                _close_note(env, sid, reason)
            elif answer.get("retired") or answer.get("error") == PL.NO_REGISTRATION:
                absent.append(sid)  # closed already, or never held:  nothing to free
                _close_note(env, sid, reason)
            else:
                failed.append(sid)
    finally:
        conn.close()
    answer = {
        "platform": PLATFORM,
        "location": location,
        "applied": bool(args.apply),
        "registered": len(rows),
        "truncated": truncated,
        "released": released,
        "kept": kept,
        "absent": absent,
        "failed": failed,
    }
    return _emit(stdout, answer, 1 if failed else 0)


def main(
    argv: list[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    stdout = stdout or sys.stdout
    if argv[:1] == ["trust"]:
        return trust_main(argv[1:], env, stdout)
    if argv[:1] == ["release"]:
        return release_main(argv[1:], env, stdout)
    stdout.write(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main())
