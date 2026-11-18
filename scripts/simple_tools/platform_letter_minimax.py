"""MiniMax Code on `platform-letter`:  the MiniMax row of the one letter table (owner, Nov 11, 2026:  "one operator
that has one tool/process for all platforms").  The library (`platform_letter`) does the registering, the note, the
retries, the "??" title, the end-of-turn report and the install;  this file is only what is MiniMax Code's own.

- The hooks are Claude Code shaped, in `~/.minimax/hooks.json`:  SessionStart starts, UserPromptSubmit names the
  chat from its first message, SubagentStart gives a subagent its own letter and Stop ends a turn (the turn end
  also puts the letter back on a title MiniMax Code regenerated without it).  The hook does not take a chat's
  title before the chat has a descriptive one:  a brand-new chat keeps the name MiniMax Code gives it, and the
  letter goes on that name at the first prompt.  A SessionStart hook's plain stdout is
  the context the model reads (`output="text"`).  A subagent takes the pair after its parent's (parent CC, child
  CD, the next CE), and MiniMax Code sends the PARENT's session id for a subagent event, so the child's own id is
  read from `childSessionId`.  MiniMax Code has no end event, so no hook closes a registration:  the assigner's own
  sweep frees the letter of a chat nobody came back to.
- The session id is the payload's `session_id` (__MAVIS_RUNTIME_SESSION_ID in the chat's own shell).
- The session store is MiniMax Code's own SQLite table, `local_runtime_sessions` in
  `~/.minimax/v2/sqlite/runtime-state.sqlite` (MINIMAX_DATA_DIR moves `~/.minimax`):  a `conversation` or `task` row
  is a chat, a row with `parent_session_id` is a sub-thread of that chat, and any other kind (a cron run) is not a
  chat.  A session the store does not know yet is a chat.  The title is read from and written to the row's `title`.

- The retired MiniMax client (`mm-session-letter`) kept each chat's letter in a cache of its own, one small file
  per session under MiniMax Code's hook data (`minimax_retired_note`).  A chat that was open when this row took
  over has no note here, so its first hook event takes its note from that cache:  the registration is the same
  one, and the chat keeps its letter.

Installing.  `platform-letter install --platform minimax [--check | --remove]` adds the two hooks to
`~/.minimax/hooks.json` with the shared JSON installer.  It also takes out every hook of the retired client,
the `mm-session-letter hook-pre-tool-use` PreToolUse entry among them (the retired client refused a post that
did not state the chat's letter;  this row has no such gate), and `--check` names the retired wrappers that
are still on the host (`~/.local/bin/mm-session-letter`, `~/.local/bin/mm-registry-env`), which the owner
removes.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Mapping
from typing import Any

from . import platform_letter as PL

PLATFORM = "minimax"
SEAT = "MM"
SOURCE = "minimax-letter-hook"
DISPLAY = "MiniMax"
EVENTS = {
    "SessionStart": "start",
    "UserPromptSubmit": "prompt",
    "SubagentStart": "start",
    "SubagentStop": "turn",
    "Stop": "turn",
}
#: The timeouts of the two hook entries, in seconds.  MiniMax Code's runtime takes a command hook's
#: timeout from 1 to 10 seconds and refuses anything longer with HOOK_SCHEMA_INVALID, so an entry over 10
#: is never run at all:  the hook file reads as installed and no letter note is written and no title is
#: renamed.  The retired client's own entries used 10, 8 and 8, which is why they ran.
#:
#: The budget is the runtime's whole ceiling, so the register chain has to fit inside it:  a register
#: makes at most three registry calls (the parent, the chat, the title), and the shared call timeout is
#: three seconds, so the chain's worst case is nine seconds and the hook finishes before the runtime
#: stops it.
HOOK_TIMEOUTS = {
    "SessionStart": 10,
    "UserPromptSubmit": 10,
    "SubagentStart": 10,
    "SubagentStop": 10,
    "Stop": 10,
}
#: The largest timeout this platform's runtime accepts for one command hook, in seconds:  the installer
#: refuses to write a longer one and `--check` reports a longer installed one as not current.
HOOK_TIMEOUT_CAP = 10
#: How long one registry call from this platform's hook may take, in seconds.  The runtime's ceiling is
#: ten seconds and the chain that has to fit inside it is the chat's own three calls, so the chat's
#: calls are given two seconds each:  three times two is six seconds, and the ancestor walk may spend
#: the four that are left of the ceiling.  Every other platform keeps the shared deadline, which is
#: longer.
CALL_TIMEOUT = 2
PARENT_CALL_TIMEOUT = 1
#: How many seconds this row's hook has before the runtime stops it.  The chat's own chain (three
#: calls at two seconds) is six of them, and the ancestor walk may spend the four that are left.
HOOK_BUDGET = 10
CLOSE_REASONS: tuple[str, ...] = ()
# The retired client's letter cache (`minimax_retired_note`):  the most of one file that is read, and the most
# project scopes looked through for it.
CACHE_BYTES = 64 << 10
CACHE_SCOPES = 200
CHAT_KINDS = ("conversation", "task")
#: The row of the session store where the hook remembers the title it last put on the chat, so a name a person
#: gave is told apart from the hook's own even when the note lost it.  It rides in the store's own extra data.
HOOK_TITLE_KEY = "hookTitle"


def store_file(env: Mapping[str, str]) -> str:
    root = str(env.get("MINIMAX_DATA_DIR") or "") or os.path.join(
        str(env.get("HOME") or os.path.expanduser("~")), ".minimax"
    )
    return os.path.join(root, "v2", "sqlite", "runtime-state.sqlite")


def _row(env: Mapping[str, str], sid: str) -> dict[str, Any]:
    path = store_file(env)
    if not sid or not os.path.isfile(path):
        return {}
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=2)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT title, session_kind, parent_session_id FROM local_runtime_sessions WHERE session_id = ?",
                (sid,),
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return {}
    return dict(row) if row is not None else {}


#: The payload keys the runtime may carry a subagent's OWN session id under.  MiniMax Code sends the
#: PARENT's id in `session_id` for a subagent event, so the child's id has to come from its own key.
CHILD_ID_KEYS = ("childSessionId", "child_session_id", "agentId", "agent_id")


def _event(payload: Mapping[str, Any]) -> str:
    """The runtime event name, whichever key the payload carries it under."""
    return str(payload.get("hook_event_name") or payload.get("event") or "")


def _is_subagent_event(payload: Mapping[str, Any]) -> bool:
    """True for the two events the runtime fires around a subagent."""
    return _event(payload) in ("SubagentStart", "SubagentStop")


def minimax_session_id(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """The chat's own session id:  the payload's for an ordinary chat, and the CHILD's for a subagent
    event, because MiniMax Code sends the parent's id there."""
    if _is_subagent_event(payload):
        for key in CHILD_ID_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        return ""
    return PL.from_payload("session_id")(payload, env)


def _sid(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    return minimax_session_id(payload, env)


def minimax_kind(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    if _is_subagent_event(payload):
        return "subagent"
    row = _row(env, _sid(payload, env))
    if not row:
        return "chat"
    if row.get("parent_session_id"):
        return "subagent"
    return "chat" if row.get("session_kind") in CHAT_KINDS else "headless"


def minimax_parent(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    if _is_subagent_event(payload):
        # The runtime sends the parent's id as the payload's own session id for a subagent event.
        return str(PL.from_payload("session_id")(payload, env) or "")
    return str(_row(env, _sid(payload, env)).get("parent_session_id") or "")


def minimax_title(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    return str(_row(env, _sid(payload, env)).get("title") or "")


def _columns(conn: sqlite3.Connection) -> set[str]:
    """The session table's own column names, so a store without the extra data column still works."""
    try:
        return {str(row[1]) for row in conn.execute("PRAGMA table_info(local_runtime_sessions)")}
    except sqlite3.Error:
        return set()


def _extra(raw: object) -> dict[str, Any]:
    """The session row's extra data as a dict, or an empty one when it is not readable JSON."""
    try:
        value = json.loads(raw) if isinstance(raw, str) and raw else {}
    except ValueError:
        value = {}
    return value if isinstance(value, dict) else {}


def minimax_rename(request: PL.TitleRequest) -> bool:
    """Put the title on the chat, and mark it as the hook's own.

    Three things happen in one write.

    - A chat with no descriptive name yet is left alone.  MiniMax Code names a chat itself, from its
      first message, and the hook took that name away by putting "New Chat" on the thread before the
      runtime had said anything:  the runtime then put its own title over the lettered one and the
      letter was gone.  The letter goes on the chat at its first prompt instead, on the words the chat
      has by then.
    - MiniMax Code's own title service auto-titles a chat whose `titleSource` is still `default` and puts
      its title back over the lettered one, so the row is written with `titleSource` `manual` as well:
      the runtime leaves a title that is not `default` alone.
    - A title a person chose is never overwritten:  the runtime marks a name a person set as `manual`
      too, so a `manual` title that is not the hook's own is left exactly as it is.  The hook's own last
      title is remembered in the row, so a name a person gave is told apart from the hook's own even
      when the note lost it.  When the hook leaves a person's words alone it says so:  it did not write
      the title, and it does not claim that it did.
    """
    path = store_file(request.env)
    if not request.session_id or not os.path.isfile(path):
        return False
    words = str(request.words or "").strip()
    if not words or words == PL.PLACEHOLDER:
        # Nothing descriptive to name the chat with yet:  the runtime's own title service names it, and the
        # hook's letter goes on that name at the chat's first prompt.
        return False
    try:
        conn = sqlite3.connect(path, timeout=4)
        try:
            with conn:
                columns = _columns(conn)
                extra_column = "extra_data_json" in columns
                selected = "title" + (", extra_data_json" if extra_column else "")
                row = conn.execute(
                    f"SELECT {selected} FROM local_runtime_sessions WHERE session_id = ?",
                    (request.session_id,),
                ).fetchone()
                if row is None:
                    return False
                current = str(row[0] or "")
                extra = _extra(row[1]) if extra_column else {}
                own = str(extra.get(HOOK_TITLE_KEY) or "") if extra_column else ""
                shown = str(getattr(request, "shown", "") or "")
                # The title is the hook's own when the row remembers it as such, or when the note remembers it as
                # the hook's last one.  Either record may be gone, and a title the hook cannot claim is a title
                # somebody else put there.
                mine = bool(own and current == own) or bool(shown and current == shown)
                if extra.get("titleSource") == "manual" and current and not mine:
                    # A person named this chat:  their words stay, and the hook does not claim that it wrote
                    # them.  The hook's own last title is in the row, so a name a person gave is still told
                    # apart from the hook's own when the note lost it.
                    return False
                if extra_column:
                    extra["titleSource"] = "manual"
                    extra[HOOK_TITLE_KEY] = str(request.title)
                    cur = conn.execute(
                        "UPDATE local_runtime_sessions SET title = ?, extra_data_json = ?"
                        " WHERE session_id = ?",
                        (
                            request.title,
                            json.dumps(extra, separators=(",", ":")),
                            request.session_id,
                        ),
                    )
                else:
                    cur = conn.execute(
                        "UPDATE local_runtime_sessions SET title = ? WHERE session_id = ?",
                        (request.title, request.session_id),
                    )
            return cur.rowcount == 1
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _data_dir(env: Mapping[str, str]) -> str:
    return str(env.get("MINIMAX_DATA_DIR") or "") or os.path.join(
        str(env.get("HOME") or os.path.expanduser("~")), ".minimax"
    )


def _cache_dirs(env: Mapping[str, str]) -> list[str]:
    """Where the retired client kept its letter cache, in the order it read it:  the scope the runtime names for
    this hook (MINIMAX_HOOKS_DATA), else the user scope, the project scopes and the layout from before those."""
    scope = str(env.get("MINIMAX_HOOKS_DATA") or "")
    if scope:
        return [os.path.join(scope, "session-letters")]
    data = _data_dir(env)
    hooks = os.path.join(data, "v2", "hook-data")
    dirs = [os.path.join(hooks, "user", "session-letters")]
    try:
        names = sorted(os.listdir(os.path.join(hooks, "projects")))[:CACHE_SCOPES]
    except OSError:
        names = []
    dirs += [os.path.join(hooks, "projects", name, "session-letters") for name in names]
    dirs.append(os.path.join(data, "hook-state", "session-letters"))
    return dirs


#: The most characters of each field of the retired cache that are taken (the file itself is read to CACHE_BYTES).
CACHE_FIELD_CHARS = {"letter": 8, "host": 16, "topic": 500, "space": 16}


def _cache_note(data: object, seen: float) -> dict[str, Any] | None:
    """The fields of one retired-cache file that are taken, each checked:  a string within its length.  A field that is
    not (a number, a list, null, a longer string) is ignored, as if it were not there;  a file whose letter is missing or
    not valid is no note at all.  Nothing here raises:  the file is the retired client's, and a hook never fails on it."""
    if not isinstance(data, dict):
        return None
    fields: dict[str, str | None] = {}
    for name, cap in CACHE_FIELD_CHARS.items():
        value = data.get(name)
        fields[name] = value if isinstance(value, str) and 0 < len(value) <= cap else None
    letter = fields["letter"]
    if letter is None or not re.fullmatch(r"[A-Z]{1,2}", letter):
        return None
    return {"label": letter, "location": fields["host"], "title": fields["topic"], "seen": seen}


def minimax_retired_note(env: Mapping[str, str], sid: str) -> dict[str, Any] | None:
    """What the retired MiniMax client cached for a chat it lettered (`<scope>/session-letters/<sid>.json`:  letter,
    topic, host, space), as the library adopts it:  the letter, where the chat is registered, its words and when
    the cache was written.  None when there is no such file.  A cache folder or file that is a symlink is never
    followed, as the retired client never followed one, and only the first CACHE_BYTES of a file are read.

    The cache lives in more than one place (the current layout under `v2/hook-data`, and `hook-state`, the layout from
    before it, which a host still holds files of).  A chat's file may be in several;  the one written last is the one
    that is adopted, and never more than one:  an older layout's file never beats a newer write."""
    found: list[dict[str, Any]] = []
    for folder in _cache_dirs(env):
        path = os.path.join(folder, sid + ".json")
        if os.path.islink(folder) or os.path.islink(path) or not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.loads(fh.read(CACHE_BYTES))
            seen = os.stat(path).st_mtime
        except (OSError, ValueError, RecursionError):
            continue
        note = _cache_note(data, seen)
        if note is not None:
            found.append(note)
    return max(found, key=lambda note: note["seen"]) if found else None


def minimax_chat_payload(sid: str) -> Mapping[str, Any]:
    return {"hook_event_name": "SessionStart", "session_id": sid, "source": "startup"}


#: The wrappers the retired MiniMax client left on a host, under the home folder.  Nothing runs them any more;
#: `install --check` names the ones still there and no installer deletes them.
RETIRED_FILES = (".local/bin/mm-session-letter", ".local/bin/mm-registry-env")

SPEC = PL.PlatformSpec(
    platform=PLATFORM,
    seat=SEAT,
    source=SOURCE,
    display=DISPLAY,
    session_id=minimax_session_id,
    parent_id=minimax_parent,
    kind=minimax_kind,
    current_title=minimax_title,
    stored_title=minimax_title,
    events=EVENTS,
    close_reasons=CLOSE_REASONS,
    title_writer=minimax_rename,
    output="text",
    quiet_events=("Stop",),
    chat_payload=minimax_chat_payload,
    session_env=("__MAVIS_RUNTIME_SESSION_ID",),
    retired_note=minimax_retired_note,
    retired_files=RETIRED_FILES,
    call_timeout=CALL_TIMEOUT,
    parent_call_timeout=PARENT_CALL_TIMEOUT,
    hook_budget=HOOK_BUDGET,
)

INSTALL = PL.JsonHookInstaller(
    path=os.path.join(".minimax", "hooks.json"), events=HOOK_TIMEOUTS, timeout_cap=HOOK_TIMEOUT_CAP
)
