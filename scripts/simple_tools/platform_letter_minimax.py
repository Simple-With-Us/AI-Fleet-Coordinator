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

Installing.  `platform-letter install --platform minimax [--check | --remove]` adds the two hooks to
`~/.minimax/hooks.json` with the shared JSON installer.

Python 3.11+, standard library only.
"""

from __future__ import annotations

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


def minimax_chat_payload(sid: str) -> Mapping[str, Any]:
    return {"hook_event_name": "SessionStart", "session_id": sid, "source": "startup"}


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
)

INSTALL = PL.JsonHookInstaller(path=os.path.join(".minimax", "hooks.json"), events=HOOK_TIMEOUTS)
