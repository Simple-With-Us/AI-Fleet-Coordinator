"""Grok (the `grok` TUI and Grok Build, one binary, seat GROK) on `platform-letter` (AFC-564, owner request Oct 29,
2026): an Operator session letter, and the title "XX: Title", on every interactive Grok chat on the Mac.  The library
(`platform_letter`) does the registering, the note, the retries and the title flow; this file is what is Grok's own.
The server has no Grok, so there is nothing to install there.

The platform name is `grok-leader`, not `grok`: that is the name the chat catalog (`chat_catalog._scan_grok`), the
transport and the Operator's letter lookup (`daemon` resolves a letter in the namespace of the catalog rows it holds)
already give Grok chats, and `PLATFORM_NAMESPACES` in `operator_registry` does not fold it into anything.  Letters
issued here therefore resolve to the chats the catalog lists, with no change to the registry.  (Folding
`'grok-leader': 'grok'` there is optional, would have to be done before the first letter is issued, and is that file's
owner's to decide.)  The adapter file is `platform_letter_grok_leader.py` because the library finds an adapter by the
platform name with dashes made underscores.

What Grok gives a hook (read from live runs of grok 1.0.70 on the Mac, in a private GROK_HOME with its own leader).
- A hook gets one line of JSON on stdin, closed at once (EOF), and runs with `GROK_HOOK_EVENT`, `GROK_HOOK_NAME`,
  `GROK_SESSION_ID`, `GROK_WORKSPACE_ROOT`, `CLAUDE_PROJECT_DIR` and, when the leader hosts the chat,
  `GROK_LEADER_SOCKET`.  The payload has `hook_event_name` (PascalCase), `sessionId` and `session_id`, `cwd` and
  `permissionMode`; SessionStart adds `source` (`new` or `load`), UserPromptSubmit adds `prompt`, `promptId` and
  `transcriptPath`, SessionEnd adds `reason` (`shutdown` is the only one seen).  There is no title, no originator and
  no parent in a payload, and no CLAUDE_CODE_ENTRYPOINT in the environment.
- Hook output does not reach the model: SessionStart reads only a top-level `systemMessage` (shown to the owner as a
  hook note) and UserPromptSubmit discards the stdout of an allowing hook.  A probe that returned `additionalContext`
  from both left the model answering UNKNOWN to "what is this chat's letter".  The hook still prints the library's
  usual `additionalContext` (a future Grok may read it), but the letter lives in the chat TITLE, and the model is not
  told its letter.  There is no `sessionTitle` output field either.
- The chat's own store is `$GROK_HOME/sessions/<cwd, URL-encoded>/<session id>/summary.json` (GROK_HOME defaults to
  `~/.grok`).  It exists before SessionStart fires.  `session_kind` is absent for an ordinary chat, `worktree` for a
  chat started with `-w`, `headless` for `grok -p`, `subagent` for a sub-agent and `subagent_resume` for a resumed one
  (709 summaries on the Mac: 638 plain, 60 subagent, 9 headless, 1 each of the other two); `generated_title` is the
  title Grok shows, and `title_is_manual` is true once someone, or the call below, named it.

Naming a chat.  `rename_session` puts the title in the chat (what the TUI shows, the picker lists and `--resume` finds
by title) with the request Grok's own `/rename` uses, `_x.ai/session/rename`, sent over ACP to the shared leader that
hosts the chat: a short `grok agent --leader stdio` client of our own (about 0.15 seconds to connect, 0.1 to rename),
pointed at GROK_LEADER_SOCKET (set for every hook of a leader-hosted chat in the live runs, for a leader started
without --leader-socket too), else at the default socket under the Grok home.  The leader updates `summary.json`
(`generated_title`, `title_is_manual: true`).
Two consequences.  A title set this way is manual, so Grok's own title generation leaves the chat alone: the words
after the letter are the library's (the first prompt's), not Grok's.  And the owner's own `/rename` drops the letter
until the next prompt puts it back.  Grok's own help says a manual title also shows in the prompt border.  The leader refuses a title over 100 characters
(code points, in any script), so a longer one is cut at a word end.  Writing `summary.json` directly is not a route:
the running leader owns the file.

The client is only ever pointed at a socket something is listening on: asked for a socket that is not there, `grok agent
--leader` starts a leader of its own (seen live, with the cloud relay flags), which a hook must never do.  A chat that
no leader hosts (`grok --no-leader`) cannot be renamed from outside, and gets its letter in the registry only.

Who takes a letter.  An interactive chat on the Mac that Grok's own hook runner started: no `session_kind`, `worktree` (a
chat started with -w) or `fork` (a chat made with /fork).  The environment must carry GROK_SESSION_ID and
GROK_HOOK_EVENT, and the payload's session must be the same one, so this hook run by a Claude or Cursor session finds
neither and does nothing, and a payload from anywhere else cannot register a Grok session.  (The other direction is open
from this side: see "The Claude hook in Grok" below.)  A headless run, a sub-agent and a session a launcher started
(AGENT_LAUNCHER) take none, and so does a session whose `summary.json` cannot be read or whose kind this adapter has not
seen: the hook cannot tell those from a headless run.

Whose environment.  A hook gets the environment of the Grok process that hosts the chat.  For a chat the shared leader
hosts (the Mac's TUI runs with `use_leader = true`, and the leader is a pm2 child) that is the LEADER's environment, not
that of the process that asked for the chat.  So AGENT_LAUNCHER, AGENT_LAUNCH_SEAT, GROK_LETTER=0 and SESSION_LETTER=0
reach the hook only when they are in the leader's own environment, or the chat runs in its own process (`grok
--no-leader`).  Seen live on the Mac (grok 1.0.70, a private leader, with the TUI and with an ACP client): each of them
set only in the client was absent from the hook and the chat took a pair, and the same GROK_LETTER=0 in the leader's own
environment kept the hook out.  Three consequences.  A chat a launcher asks the shared leader for takes a letter (a
launcher that wants none runs its own GROK_HOME or `--no-leader`, as the ACP runtime and Conductor do).
`GROK_LETTER=0 grok` in a terminal does not stop the hook of a leader-hosted chat.  And a leader started from a launcher
session would keep every chat on it from taking a letter.  The switch that reaches every chat on the leader at once is a
file, `$GROK_HOME/session-letter.off`: `touch` it to pause the hook and delete it to resume (a paused hook neither
registers, titles, touches nor closes, and `install --check` says so).  `install --remove` is the other way.

Forks.  `/fork` in the TUI and the ACP request `_x.ai/session/fork` copy a chat into a parallel one.  Seen live: the
fork's `summary.json` has `session_kind: fork`, `parent_session_id` and `forked_at`, its SessionStart `source` is `load`
(not `fork`), and its title starts as the source chat's, letter and all.  A fork is a chat another chat started, so it is
a sub-thread: the Operator gives it the pair after its source chat's (parent CC, fork CD), or places it as a main chat
when the source is not live there.  (Claude's own hook leaves a user fork as a main chat because the app's record does
not name its source; Grok's store does.)  The fork keeps the words of that title and not the letter: until the fork holds
a letter of its own, `grok_current_title` reads the title without a letter in front of it.

The Claude hook in Grok.  Grok imports the hooks of `~/.claude/settings.json` while `[compat.claude] hooks` is on in its
config (the default is on; the Mac's `~/.grok/config.toml` says false today, and something else has rewritten that file
before).  `claude_letter` gates on the environment alone (CLAUDE_CODE_ENTRYPOINT) and takes the session id from the
payload, which Grok's payload carries too, so a Grok run that inherits CLAUDE_CODE_ENTRYPOINT (started from a Claude Code
Bash tool, or under a leader that was) registers as a Claude chat once the Claude letter hook is installed.  Seen live
with a stand-in for that hook: with `hooks = true` and the entrypoint set all three events ran and a Claude row was made,
with `hooks = false` none ran.  The guard belongs in `claude_letter` (refuse when GROK_HOOK_EVENT or GROK_SESSION_ID is
set), which is another change's file; here `install` and `install --check` warn while the cell is not false.

Sub-agents take no letter.  A sub-agent's session (the explore and plan workers a chat starts) is not a chat the leader
hosts: asked to rename one, the leader answered "session not found" on every one of about 90 tries over the 36 seconds
it ran, in two runs, and again after it ended; and the leader's own session list, which is what the chat catalog reads,
held the chat that started it and not the sub-agent.  So it cannot be titled, listed or woken, and a letter in the
registry alone would be invisible to the owner and would spend a pair.  Its hook events (its UserPromptSubmit and
SessionEnd carry `subagentType`, and it fires no SessionStart) find it out of scope and do nothing, and no SubagentStart
or SubagentStop hook is installed.

Closing.  SessionEnd arrives with `reason: shutdown` for a quit, and nothing says an end is final (a quit chat is
resumed with `--resume`), so no end closes a registration: the reason is kept in the note, and the library's 36 hour
sweep frees the pair of a chat nobody came back to.

Not covered.  Chats the ACP runtime (`grok-acp-runtime/start.sh`, `--no-leader serve`) and Conductor start use their own
GROK_HOME, so they never load `~/.grok/hooks`; they are driven by agents, not typed to by the owner.  `--fork-session`
was not run, and neither was a restored chat (Grok's own comment says `parent_session_id` also names the source of a
restore): a chat with a source and no `session_kind` is treated as a fork is.  A `--no-leader` TUI was not run either:
its hooks should run in its own process (the hooks guide calls that the shell route), so its own environment is the one
the gates would read.  The pause file was exercised in process, not against a live leader.

Trust.  `~/.grok/hooks/*.json` runs once the folder is trusted, as the owner's lane-guard.json and seat-session.json
already do; nothing extra to approve.  A new session loads the new file, and `/hooks` reloads it in a running one.  The
hook command is `~/.local/bin/platform-letter`, which `scripts/agent_sync/install.sh` links: `install` refuses while it
is not there, and `install --check` fails.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import dataclasses
import glob
import io
import json
import os
import re
import select
import shutil
import socket
import subprocess
import time
import tomllib
from collections.abc import Mapping
from typing import Any, TextIO

from . import platform_letter as PL

PLATFORM = "grok-leader"
SEAT = "GROK"
SOURCE = "grok-letter-hook"
ENV_OFF = "GROK_LETTER"
ENV_BIN = "GROK_LETTER_BIN"  # the grok binary to ask, when it is not on the hook's PATH
ENV_HOME = "GROK_HOME"
ENV_SOCKET = "GROK_LEADER_SOCKET"
HOOK_FILE = "session-letter.json"
OFF_FILE = "session-letter.off"  # under the Grok home: while it exists the hook does nothing, for every chat
CONFIG_FILE = "config.toml"

RENAME_METHOD = "_x.ai/session/rename"
RPC_SECONDS = 6.0  # the whole rename, inside the hook's own timeout (15 to 20 seconds)
MAX_TITLE = 100  # characters (code points); the leader answers "title too long" above it
LISTEN_SECONDS = 0.5  # the most the "is a leader listening" check waits
SUMMARY_LIMIT = 1 << 20

#: `session_kind` of a chat someone talks to: absent for a normal chat, `worktree` for one started with -w, `fork` for one
#: made with /fork (seen live; it names its source chat in `parent_session_id`).
CHAT_KINDS = ("", "worktree", "fork")
#: The hook entries and their timeouts in seconds.  UserPromptSubmit holds the prompt while it runs, so every network
#: step inside it has a deadline well under its timeout.
HOOK_TIMEOUTS = {
    "SessionStart": 20,
    "UserPromptSubmit": 15,
    "SessionEnd": 15,
}
# Where grok lives when the hook's PATH does not hold it.
BIN_CANDIDATES = (".grok/bin/grok", "/opt/homebrew/bin/grok", "/usr/local/bin/grok")
# What a hook inherits from the chat that the rename client must not: it is a client of the leader, not a hook of a chat.
SCRUB_NAMES = ("GROK_SESSION_ID", "GROK_WORKSPACE_ROOT", "CLAUDE_PROJECT_DIR")
SCRUB_PREFIXES = ("GROK_HOOK_", "GROK_PLUGIN_", "CLAUDE_PLUGIN_", "CURSOR_PLUGIN_")
# The shape of a Grok session id (a UUID), checked before it is used in a path.
SID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{3,127}")


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _emit(stdout: TextIO, payload: Mapping[str, Any], code: int) -> int:
    stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return code


def _last_json(text: str) -> dict[str, Any] | None:
    """The last line of `text` that is a JSON object, as the library's installer prints its answer."""
    for line in reversed(text.splitlines()):
        if line.strip().startswith("{"):
            try:
                value = json.loads(line)
            except ValueError:
                continue
            return value if isinstance(value, dict) else None
    return None


# --------------------------------------------------------------------------------------------
# Grok's own store (read-only)
# --------------------------------------------------------------------------------------------


def grok_home(env: Mapping[str, str]) -> str:
    named = _text(env.get(ENV_HOME))
    if named:
        return os.path.expanduser(named)
    return os.path.join(_text(env.get("HOME")) or os.path.expanduser("~"), ".grok")


def summary_of(env: Mapping[str, str], sid: str) -> dict[str, Any] | None:
    """The session's `summary.json`, or None when the id is not a session id, the file is not there or cannot be read.
    Nothing here raises: a hook never blocks a chat."""
    if not SID_RE.fullmatch(sid or ""):
        return None
    pattern = os.path.join(
        glob.escape(grok_home(env)), "sessions", "*", glob.escape(sid), "summary.json"
    )
    for path in glob.glob(pattern):
        try:
            with open(path, "rb") as fh:
                doc = json.loads(fh.read(SUMMARY_LIMIT).decode("utf-8", "replace"))
        except (OSError, ValueError, RecursionError):
            return None
        return doc if isinstance(doc, dict) else None
    return None


def classify(summary: Mapping[str, Any] | None) -> str:
    """ "chat", "subagent" or "headless" for a session of the store.  A session the store does not show is headless:
    without its summary the hook cannot rule out a `grok -p` run, so it takes no letter, and neither does a kind this
    adapter has not seen."""
    if summary is None:
        return "headless"
    kind = _text(summary.get("session_kind"))
    if kind.startswith("subagent"):
        return "subagent"
    return "chat" if kind in CHAT_KINDS else "headless"


# --------------------------------------------------------------------------------------------
# The accessors the library asks
# --------------------------------------------------------------------------------------------


def grok_session_id(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """The chat a hook event is about, or "" when Grok's hook runner did not start this process.

    The gate is Grok-only: the hook environment must carry GROK_SESSION_ID and GROK_HOOK_EVENT, and the payload's
    session must be that session.  With no payload (`platform-letter show` inside a Grok shell) it is GROK_SESSION_ID."""
    env_sid = _text(env.get("GROK_SESSION_ID"))
    if not payload:
        return env_sid
    if not env_sid or not _text(env.get("GROK_HOOK_EVENT")):
        return ""
    said = _text(payload.get("sessionId")) or _text(payload.get("session_id"))
    if said and said != env_sid:
        return ""
    return env_sid


def switched_off(env: Mapping[str, str]) -> bool:
    """Whether the Grok home holds the off file.  A hook of a leader-hosted chat runs in the leader's environment, so
    GROK_LETTER=0 in a terminal never reaches it; a file under the Grok home does."""
    try:
        return os.path.lexists(os.path.join(grok_home(env), OFF_FILE))
    except (OSError, ValueError):
        return False


def grok_kind(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    sid = grok_session_id(payload, env)
    if not sid:
        return "headless"  # not a Grok hook run: never a chat of ours
    if switched_off(env):
        return "headless"  # paused: no chat takes part, whatever its environment says
    if _text(payload.get("subagentType")):
        return (
            "subagent"  # an event in a sub-agent's own session, which may not have its summary yet
        )
    return classify(summary_of(env, sid))


def grok_parent(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """The chat a fork was made from: `parent_session_id` of a chat's summary, else "".  Only a chat has a parent that
    counts (a sub-agent takes no letter, so its source is nobody's parent), and a source that is not a session id, or is
    the chat itself, is no parent."""
    sid = grok_session_id(payload, env)
    summary = summary_of(env, sid) if sid else None
    if summary is None or classify(summary) != "chat":
        return ""
    parent = _text(summary.get("parent_session_id"))
    return parent if parent != sid and SID_RE.fullmatch(parent) else ""


def own_label(env: Mapping[str, str], sid: str) -> str:
    """The letter this chat holds in its note, or "" (no note, or none open)."""
    try:
        with open(PL.note_path(env, SEAT, sid), encoding="utf-8") as fh:
            note = json.load(fh)
    except (OSError, ValueError):
        return ""
    label = note.get("label") if isinstance(note, dict) else None
    return label if isinstance(label, str) and PL.LABEL_RE.fullmatch(label) else ""


def grok_current_title(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """The chat's title as Grok shows it now (`generated_title`; Grok's own `session_summary` is not updated by a
    rename).  A fork starts with its source chat's title, source letter and all, and that letter is not the fork's: for
    a chat that has a source, a letter in front of the title that is not the chat's own comes off, so the library sees
    the words alone and puts the fork's own letter on them."""
    sid = grok_session_id(payload, env)
    summary = summary_of(env, sid)
    if not summary:
        return ""
    title = PL.one_line(summary.get("generated_title"))
    if title and _text(summary.get("parent_session_id")):
        mine = own_label(env, sid)
        if not (mine and title.startswith(mine + ": ")):
            return PL.bare(title)
    return title


# --------------------------------------------------------------------------------------------
# The leader (ACP over a child process), for naming a chat
# --------------------------------------------------------------------------------------------


class RpcError(RuntimeError):
    """The leader did not answer the way the hook needs.  The message never holds a request's contents."""

    def __init__(self, message: str, *, code: int | None = None, data: str = ""):
        super().__init__(message)
        self.code, self.data = code, data


class Rpc:
    """One short conversation with the leader through `grok agent --leader stdio`: one JSON object per line, a hard
    deadline over the whole of it.  Notifications and requests of the leader's own are read and dropped."""

    def __init__(
        self,
        argv: list[str],
        env: Mapping[str, str],
        deadline: float,
        *,
        popen: Any = subprocess.Popen,
    ):
        self.deadline, self.buf, self.seq = deadline, b"", 0
        self.proc = popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=dict(env),
        )

    def __enter__(self) -> Rpc:
        try:
            self.call(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientInfo": {"name": "platform-letter", "version": "1"},
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    },
                },
            )
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _send(self, obj: Mapping[str, Any]) -> None:
        try:
            self.proc.stdin.write(json.dumps(obj).encode("utf-8") + b"\n")
            self.proc.stdin.flush()
        except (OSError, ValueError) as exc:
            raise RpcError("the leader client closed its input") from exc

    def _recv(self) -> dict[str, Any]:
        fd = self.proc.stdout.fileno()
        while b"\n" not in self.buf:
            left = self.deadline - time.monotonic()
            if left <= 0:
                raise RpcError("the leader did not answer in time")
            ready, _, _ = select.select([fd], [], [], min(left, 0.5))
            if not ready:
                continue
            chunk = os.read(fd, 65536)
            if not chunk:
                raise RpcError("the leader client closed its output")
            self.buf += chunk
        raw, _, self.buf = self.buf.partition(b"\n")
        try:
            msg = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise RpcError("the leader client sent something that is not JSON") from exc
        return msg if isinstance(msg, dict) else {}

    def call(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """One request, and its result; a JSON-RPC error is an RpcError with its code and its short data text."""
        self.seq += 1
        want = self.seq
        self._send({"jsonrpc": "2.0", "id": want, "method": method, "params": params})
        while True:
            msg = self._recv()
            if msg.get("id") != want or "method" in msg:
                continue
            if "error" in msg:
                error = msg["error"] if isinstance(msg["error"], dict) else {}
                raise RpcError(
                    "%s failed (code %s)" % (method, error.get("code")),
                    code=error.get("code") if isinstance(error.get("code"), int) else None,
                    data=_text(error.get("data"))[:120],
                )
            result = msg.get("result")
            return result if isinstance(result, dict) else {}

    def close(self) -> None:
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


def grok_binary(env: Mapping[str, str]) -> str:
    """The grok program to ask: GROK_LETTER_BIN, else the PATH, else where Grok is usually installed."""
    named = _text(env.get(ENV_BIN))
    if named:
        return named if os.access(named, os.X_OK) else ""
    found = shutil.which("grok", path=env.get("PATH") or os.defpath)
    if found:
        return found
    home = _text(env.get("HOME")) or os.path.expanduser("~")
    for candidate in BIN_CANDIDATES:
        path = candidate if os.path.isabs(candidate) else os.path.join(home, candidate)
        if os.access(path, os.X_OK):
            return path
    return ""


def listening(path: str) -> bool:
    """Whether something accepts connections on this unix socket.  A leader that died leaves its socket file behind,
    and `grok agent --leader` pointed at a socket nobody listens on starts a leader of its own, so the file being there
    is not enough."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(LISTEN_SECONDS)
    try:
        probe.connect(path)
        return True
    except (
        OSError,
        ValueError,
    ):  # ValueError: a path with a NUL, or one too long for the socket address
        return False
    finally:
        probe.close()


def leader_socket(env: Mapping[str, str]) -> str:
    """The socket of the leader that hosts this chat: GROK_LEADER_SOCKET (Grok sets it for a hook), else the default
    under the Grok home.  "" when no leader listens there: nothing hosts the chat, so it cannot be named from outside,
    and a client of ours must not start a leader of its own."""
    path = _text(env.get(ENV_SOCKET)) or os.path.join(grok_home(env), "leader.sock")
    return path if listening(path) else ""


def client_env(env: Mapping[str, str]) -> dict[str, str]:
    """The environment of the rename client: the hook's own, without what marks the process as a hook of a chat."""
    return {
        k: v for k, v in env.items() if k not in SCRUB_NAMES and not k.startswith(SCRUB_PREFIXES)
    }


def fit(title: str) -> str:
    """The title cut to the most Grok accepts, at a word end when that keeps at least half of it.  The library's own
    words are short (60 characters); a chat the owner named with 97 characters or more is the case this is for."""
    if len(title) <= MAX_TITLE:
        return title
    cut = title[:MAX_TITLE]
    head = cut.rsplit(" ", 1)[0].rstrip()
    return head if len(head) >= MAX_TITLE // 2 else cut


def rename_session(
    session_id: str, name: str, env: Mapping[str, str], *, popen: Any = subprocess.Popen
) -> bool:
    """Set a chat's title (`_x.ai/session/rename`) through the leader that hosts it.  True once the leader has accepted
    it.  One try: the leader answers "session not found" for a session it does not host, and waiting does not change that."""
    title = fit(PL.tidy(name))
    binary = grok_binary(env)
    if not (SID_RE.fullmatch(session_id or "") and title and binary):
        return False
    sock = leader_socket(env)
    if not sock:
        return False
    argv = [binary, "agent", "--leader", "--leader-socket", sock, "stdio"]
    deadline = time.monotonic() + RPC_SECONDS
    try:
        with Rpc(argv, client_env(env), deadline, popen=popen) as rpc:
            return (
                rpc.call(RENAME_METHOD, {"sessionId": session_id, "title": title}).get("success")
                is True
            )
    except (RpcError, OSError, ValueError):
        return False


def grok_rename(req: PL.TitleRequest) -> bool:
    """The spec's title writer: put "XX: Words" on the chat."""
    return rename_session(req.session_id, req.title, req.env)


# --------------------------------------------------------------------------------------------
# The spec and the installer
# --------------------------------------------------------------------------------------------

SPEC = PL.PlatformSpec(
    platform=PLATFORM,
    seat=SEAT,
    source=SOURCE,
    display="Grok",
    session_id=grok_session_id,
    parent_id=grok_parent,  # a fork's source chat; a sub-agent is out of scope whoever started it (grok_kind)
    kind=grok_kind,
    current_title=grok_current_title,
    events={
        "SessionStart": "start",
        "UserPromptSubmit": "prompt",
        "SessionEnd": "end",
    },
    # No end closes: SessionEnd always has a reason (`shutdown`), and a main chat is resumed after a quit, so its
    # letter is freed by the 36 hour sweep.
    close_reasons=(),
    title_writer=grok_rename,
    locations=("mac",),
    env_off=ENV_OFF,
)


def hook_file(env: Mapping[str, str]) -> str:
    """The hook file Grok loads for every session: `$GROK_HOME/hooks/session-letter.json`, a file of this adapter's own."""
    return os.path.join(grok_home(env), "hooks", HOOK_FILE)


def installer(env: Mapping[str, str]) -> PL.JsonHookInstaller:
    return PL.JsonHookInstaller(
        path=hook_file(env),
        events=HOOK_TIMEOUTS,
        whole_file=True,
        trust=(
            "nothing to approve: hooks in ~/.grok/hooks run once the folder is trusted, as the existing ones do; a "
            "new session loads the file, and /hooks reloads it in a running one; the hook runs "
            "~/.local/bin/platform-letter, which scripts/agent_sync/install.sh links"
        ),
    )


def program_path(env: Mapping[str, str]) -> str:
    """The program the hook file runs: this host's own `platform-letter` link, where `PL.command_for` puts it."""
    return os.path.join(str(env.get("HOME") or os.path.expanduser("~")), ".local", "bin", PL.ENTRY)


def program_ready(env: Mapping[str, str]) -> bool:
    """Whether that link is there and runs (a link to a file that is gone is not).  A hook file that names a program that
    is not there makes Grok print a failure line on every event and gives no chat a letter."""
    path = program_path(env)
    return os.path.isfile(path) and os.access(path, os.X_OK)


def imports_claude_hooks(env: Mapping[str, str]) -> bool:
    """Whether Grok loads the hooks of `~/.claude/settings.json`: `[compat.claude] hooks` of its user config, on unless
    the file says false.  Grok's default is on, so a config that is missing or cannot be read counts as on."""
    try:
        with open(os.path.join(grok_home(env), CONFIG_FILE), "rb") as fh:
            doc = tomllib.load(fh)
    except (OSError, ValueError):
        return True
    compat = doc.get("compat")
    claude = compat.get("claude") if isinstance(compat, dict) else None
    return not (isinstance(claude, dict) and claude.get("hooks") is False)


def host_warnings(env: Mapping[str, str]) -> list[str]:
    """What an owner running `install` or `install --check` should know about this host."""
    found = []
    if switched_off(env):
        found.append(
            "paused: %s exists, so no chat takes a letter until it is deleted"
            % os.path.join(grok_home(env), OFF_FILE)
        )
    if imports_claude_hooks(env):
        found.append(
            "Grok imports the hooks of ~/.claude/settings.json ([compat.claude] hooks is not false in %s): a Grok run "
            "that inherits CLAUDE_CODE_ENTRYPOINT would also register as a Claude chat once the Claude letter hook is "
            "installed; set hooks = false under [compat.claude]"
            % os.path.join(grok_home(env), CONFIG_FILE)
        )
    return found


def install(opts: PL.InstallOptions, env: Mapping[str, str], stdout: TextIO) -> int:
    """`install --platform grok-leader [--check | --remove]`: add (or take out, or check) this adapter's hooks in
    `~/.grok/hooks/session-letter.json`.  Grok runs on the Mac only, and the Mac registers in process, so a location
    or a registry host is refused, and so is an install on a host with no Grok home (it would leave a stray folder) and
    one where the program the hooks run is not there.  The answer of an install or a check carries that program, whether
    it is ready (a check that finds it missing exits 1), and the `host_warnings`."""
    if opts.location == "server" or opts.remote:
        return _emit(
            stdout,
            {"error": "Grok runs on the Mac only; there is nothing to install on a server"},
            2,
        )
    home = grok_home(env)
    if not (opts.check or opts.remove or os.path.isdir(home)):
        return _emit(
            stdout, {"error": "no Grok home at %s: Grok has not run on this host" % home}, 2
        )
    program, ready = program_path(env), program_ready(env)
    if not (opts.check or opts.remove or ready):
        return _emit(
            stdout,
            {
                "error": "%s is not an executable file: run scripts/agent_sync/install.sh, which links it"
                % program
            },
            2,
        )
    buf = io.StringIO()
    code = PL.install_json_hooks(installer(env), dataclasses.replace(opts, location=""), env, buf)
    answer = _last_json(buf.getvalue())
    if answer is None:
        stdout.write(buf.getvalue())
        return code
    if not opts.remove and "error" not in answer:
        answer["program"], answer["program_ready"] = program, ready
        found = host_warnings(env)
        if found:
            answer["warnings"] = found
        if opts.check and not ready:
            code = code or 1
    return _emit(stdout, answer, code)
