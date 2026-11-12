"""MiniMax Code on `platform-letter`:  the MiniMax row of the one letter table (owner, Nov 11, 2026:  "one operator
that has one tool/process for all platforms").  The library (`platform_letter`) does the registering, the note, the
retries, the "??" title, the end-of-turn report and the install;  this file is only what is MiniMax Code's own.

- The hooks are Claude Code shaped, in `~/.minimax/hooks.json`:  SessionStart starts and Stop ends a turn (the turn
  end also puts the letter back on a title MiniMax Code regenerated without it).  A SessionStart hook's plain stdout
  is the context the model reads (`output="text"`).  MiniMax Code has no end event, so no hook closes a
  registration:  the assigner's own sweep frees the letter of a chat nobody came back to.
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
import sqlite3
from collections.abc import Mapping
from typing import Any

from . import platform_letter as PL

PLATFORM = "minimax"
SEAT = "MM"
SOURCE = "minimax-letter-hook"
DISPLAY = "MiniMax"
EVENTS = {"SessionStart": "start", "UserPromptSubmit": "prompt", "Stop": "turn"}
HOOK_TIMEOUTS = {"SessionStart": 20, "Stop": 15}
CLOSE_REASONS: tuple[str, ...] = ()
# The retired client's letter cache (`minimax_retired_note`):  the most of one file that is read, and the most
# project scopes looked through for it.
CACHE_BYTES = 64 << 10
CACHE_SCOPES = 200
CHAT_KINDS = ("conversation", "task")


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


def _sid(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    return PL.from_payload("session_id")(payload, env)


def minimax_kind(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    row = _row(env, _sid(payload, env))
    if not row:
        return "chat"
    if row.get("parent_session_id"):
        return "subagent"
    return "chat" if row.get("session_kind") in CHAT_KINDS else "headless"


def minimax_parent(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    return str(_row(env, _sid(payload, env)).get("parent_session_id") or "")


def minimax_title(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    return str(_row(env, _sid(payload, env)).get("title") or "")


def minimax_rename(request: PL.TitleRequest) -> bool:
    """Write the title into the session store's row.  True when the row now carries it."""
    path = store_file(request.env)
    if not request.session_id or not os.path.isfile(path):
        return False
    try:
        conn = sqlite3.connect(path, timeout=4)
        try:
            with conn:
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
        if isinstance(data, dict) and isinstance(data.get("letter"), str) and data["letter"]:
            found.append(
                {
                    "label": data["letter"],
                    "location": data.get("host"),
                    "title": data.get("topic"),
                    "seen": seen,
                }
            )
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
    session_id=PL.from_payload("session_id"),
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
)

INSTALL = PL.JsonHookInstaller(path=os.path.join(".minimax", "hooks.json"), events=HOOK_TIMEOUTS)
