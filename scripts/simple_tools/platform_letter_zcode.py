"""ZCode on `platform-letter` (OPS-1048, owner request Nov 7, 2026):  an Operator session letter, and the title
"XX: Title", on every ZCode chat, on the server (the ZCode server the desktop app drives) and on the Mac
(the desktop app's local chats).  The library (`platform_letter`) does the registering, the note, the retries and
the title flow; this file is what is ZCode's own.

How it fits together.
- ZCode keeps Claude-style hooks in `~/.zcode/cli/config.json` under `hooks.events`, and its payloads carry the
  Claude field names beside the native ones (`session_id` beside `sessionId`), so the library's default session-id
  accessor works as it is.  The events map onto the library's names:  `SessionStart` starts (its `source` values
  are the library's own:  startup, resume, clear, compact), `UserPromptSubmit` prompts, and `Stop` ends a turn
  (`turn`:  the end-of-turn report, a retry for a chat with no letter, and the letter put back on a title the app
  retitled).  ZCode has no `SessionEnd`, so no hook closes a registration:  the assigner's own sweep frees the
  letter of a chat nobody came back to.
- The session store is ZCode's own SQLite table, `~/.zcode/cli/db/db.sqlite` (`session`:  id, parent_id, title,
  title_source, task_type, directory, time_archived, time_deleted).  The parent and the kind are read from it:
  a `subagent_child` row is a sub-thread of the row its `parent_id` names, and takes the pair after its parent's
  in the parent's row (parent CC, child CD), exactly as every other platform places them.  A session the store
  does not know yet registers as a chat and is named at its next prompt.
- The title is written straight into the store (`zcode_rename`), and never over a name a person gave:  a title is
  the hook's own when it is what the hook last put on the chat (`TitleRequest.shown`), carries the "??" mark, or
  starts with a letter the chat holds or held.

Who takes a letter.  Every ZCode session the hooks see, except one a launcher started (`AGENT_LAUNCHER`, the
wake runs among them) and one SESSION_LETTER=0 turns off.

Installing.  `platform-letter install --platform zcode [--location server|mac] [--check | --remove]` adds this
adapter's entries to `~/.zcode/cli/config.json` `hooks.events` with the shared JSON installer, idempotently, with
the file backed up first.  The entries run `~/.local/bin/platform-letter`, the same command every platform runs;
an entry an earlier install pointed at the retired `zc-letter` wrapper is replaced in place.  Nothing else in the
config is touched, and there is no trust step:  ZCode runs the hooks a config file lists.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
from collections.abc import Mapping
from typing import Any

from . import platform_letter as PL

PLATFORM = "zcode"
SEAT = "ZC"
DISPLAY = "ZCode"
SOURCE = "the ZC seat's session-letter hook"

STORE_SUBPATH = os.path.join(".zcode", "cli", "db", "db.sqlite")

#: ZCode has no public thread URL today, so the link the registry records is the stable
#: locator convention the fleet reads:  the session id under the platform's own scheme.
#: One form, replaced wholesale the day the app grows a real link.
THREAD_LINK_FORM = "zcode://session/{sid}"

#: ZCode has no SessionEnd:  Stop ends a turn, not a chat, so it never closes a registration.
EVENTS = {"SessionStart": "start", "UserPromptSubmit": "prompt", "Stop": "turn"}
HOOK_TIMEOUTS = {"SessionStart": 20, "UserPromptSubmit": 15, "Stop": 15}
CLOSE_REASONS: tuple[str, ...] = ()

_TASK_KINDS = {"interactive": "chat", "subagent_child": "subagent"}


def store_file(env: Mapping[str, str]) -> str:
    """The session store on this host, under the home directory."""
    home = str(env.get("HOME") or os.path.expanduser("~"))
    return os.path.join(home, STORE_SUBPATH)


class _Store:
    """One read-only connection to the session store for one lookup, closed by the context
    manager:  a hook run opens a handful of short-lived readers and leaves no descriptor
    behind.  Errors are the caller\'s empty answers, never exceptions a chat could see."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self.path = store_file(env)
        self._conn: sqlite3.Connection | None = None

    def __enter__(self) -> _Store:
        return self

    def __exit__(self, *exc: object) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    def _connect(self) -> bool:
        if self._conn is not None:
            return True
        if not os.path.isfile(self.path):
            return False
        try:
            self._conn = sqlite3.connect("file:%s?mode=ro" % self.path, uri=True, timeout=5)
            return True
        except sqlite3.Error:
            return False

    def row(self, sid: str) -> dict[str, Any] | None:
        if not sid or not self._connect():
            return None
        try:
            cur = self._conn.execute(
                "SELECT id, parent_id, title, title_source, task_type, directory,"
                " time_archived, time_deleted FROM session WHERE id = ?",
                (sid,),
            )
            fetched = cur.fetchone()
        except sqlite3.Error:
            return None
        if fetched is None:
            return None
        keys = (
            "id",
            "parent_id",
            "title",
            "title_source",
            "task_type",
            "directory",
            "time_archived",
            "time_deleted",
        )
        return dict(zip(keys, fetched))

    def subagent_ids(self, sids: list[str]) -> set[str]:
        """The ids among `sids` the store calls sub-agent children, in one query."""
        if not sids or not self._connect():
            return set()
        placeholders = ",".join("?" * len(sids))
        try:
            rows = self._conn.execute(
                "SELECT id FROM session WHERE task_type = 'subagent_child'"
                " AND id IN (%s)" % placeholders,
                sids,
            ).fetchall()
        except sqlite3.Error:
            return set()
        return {str(r[0]) for r in rows if r and r[0]}


def _sid_of(payload: Mapping[str, Any]) -> str:
    for key in ("session_id", "sessionId"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def zcode_session_id(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    return _sid_of(payload)


def zcode_parent(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """The parent chat's native id, from the store's `parent_id` (a sub-agent's row names it)."""
    with _Store(env) as store:
        row = store.row(_sid_of(payload))
    if row and isinstance(row.get("parent_id"), str):
        return row["parent_id"]
    return ""


def zcode_kind(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """chat, subagent or headless, from the store's `task_type`; an unknown session is a chat."""
    with _Store(env) as store:
        row = store.row(_sid_of(payload))
    if row:
        return _TASK_KINDS.get(str(row.get("task_type") or ""), "headless")
    return "chat"


def zcode_stored_title(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    with _Store(env) as store:
        row = store.row(_sid_of(payload))
    if row and isinstance(row.get("title"), str):
        return row["title"]
    return ""


def zcode_current_title(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """The title as the chat shows it now:  what the payload says, else what the store holds."""
    title = payload.get("title")
    if isinstance(title, str) and title.strip():
        return title
    return zcode_stored_title(payload, env)


def zcode_chat_payload(sid: str) -> Mapping[str, Any]:
    """The start payload a chat's own SessionStart would carry, so the library can enroll a
    parent that holds no live registration.  The store row is not read here:  a parent the
    store does not know yet is still enrolled, titleless, and named at its next prompt."""
    return {
        "hook_event_name": "SessionStart",
        "session_id": sid,
        "source": "startup",
        "cwd": "",
    }


def zcode_rename(request: Any) -> bool:
    """Put the lettered title on the chat:  one UPDATE in the store, `title_source` `custom`.

    `custom`, not `generated`:  the app model-retitles a thread whose title it considers its
    own (`generated`) and wipes the letter prefix off it (seen live — a thread titled
    "DD: work on ..." came back as plain "Merge and Close Draft PRs"), while a custom title
    is a user rename the app leaves alone.  A custom title is replaced only when it is the
    hook's own (`hook_owned`):  what the hook last put on the chat, the "??" mark, or a
    letter the chat holds or held.  Anything else is a name a person chose, so the chat
    keeps the user's words and the registry keeps the letter.

    The write stays inside the hook's own budget while still beating a busy store:  two
    short attempts (4 s each, a quarter second apart), not one long one — a busy timeout
    alone was seen losing title writes under the live app (titles landed hours late, on a
    much later event's retry)."""
    env = request.env
    sid = request.session_id
    title = str(request.title or "").strip()
    if not sid or not title:
        return False
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", sid):
        return False
    path = store_file(env)
    if not os.path.isfile(path):
        return False
    with _Store(env) as store:
        row = store.row(sid)
    if row and row.get("title_source") == "custom":
        current = str(row.get("title") or "")
        if current and not hook_owned(request, current):
            return True  # a person's rename:  the words stay
    for attempt in range(2):
        try:
            conn = sqlite3.connect(path, timeout=4)
            try:
                with conn:
                    cur = conn.execute(
                        "UPDATE session SET title = ?, title_source = 'custom',"
                        " time_title_updated = ? WHERE id = ?",
                        (title, int(time.time()), sid),
                    )
                    done = cur.rowcount == 1
            finally:
                conn.close()
            if done:
                _record_thread_link(env, sid)
                return True
            return False
        except sqlite3.Error:
            if attempt == 0:
                time.sleep(0.25)
                continue
    return False


def _record_thread_link(env: Mapping[str, str], sid: str) -> None:
    """Put this chat's thread link on its registry row (one update op, failures swallowed).

    The registry's Sessions tab carries a Thread Link column and the Sheets drain exports
    it, so the row is where a reader looks;  the link is the locator convention because
    ZCode mints no share URLs and has no URI scheme (checked live:  zero share_url values
    in the store).  The location is the install's own rule read from the environment
    (SESSION_LETTER_LOCATION, else the platform), the same way the hook command carries it."""
    from . import registry_api as RA

    if not RA.base_url(env):
        return
    location = str(env.get("SESSION_LETTER_LOCATION") or "").strip() or (
        "mac" if sys.platform == "darwin" else "server"
    )
    try:
        answer = RA.call(
            env,
            "update",
            {
                "platform": PLATFORM,
                "location": location,
                "scope": "default",
                "session_id": sid,
                "thread_link": THREAD_LINK_FORM.format(sid=sid),
            },
            timeout=2,
        )
    except Exception as exc:  # noqa: BLE001 - a link that cannot be recorded never blocks a chat
        print(
            "platform-letter: thread-link update failed (%s)" % type(exc).__name__, file=sys.stderr
        )
        return
    if isinstance(answer, dict) and answer.get("error"):
        # swallowed no longer:  one stderr line names it, the chat is never blocked
        print(
            "platform-letter: thread-link update refused (%s)" % str(answer.get("error"))[:120],
            file=sys.stderr,
        )


def hook_owned(request: Any, current: str) -> bool:
    """True when the store's `custom` title is the hook's own, so a new title may replace it:  it is what the hook
    last put on the chat (the library passes it as `shown`), it carries the "??" mark the hook puts on a chat with no
    letter, or it starts with the letter being written or one the chat holds or held.  Anything else is a person's
    name, which the hook never overwrites."""
    if current == str(getattr(request, "shown", "") or "") or current.startswith(
        PL.UNLETTERED + ":"
    ):
        return True
    letters = {str(getattr(request, "label", "") or ""), *getattr(request, "held", ())}
    return any(label and current.startswith(label + ":") for label in letters)


SPEC = PL.PlatformSpec(
    platform=PLATFORM,
    seat=SEAT,
    source=SOURCE,
    display=DISPLAY,
    session_id=PL.from_payload("session_id"),
    parent_id=zcode_parent,
    kind=zcode_kind,
    current_title=zcode_current_title,
    stored_title=zcode_stored_title,
    events=EVENTS,
    close_reasons=CLOSE_REASONS,
    quiet_events=("Stop",),
    chat_payload=zcode_chat_payload,
    title_writer=zcode_rename,
    locations=("mac", "server"),
)


# --------------------------------------------------------------------------------------------
# Install
# --------------------------------------------------------------------------------------------


def _locate_events(doc: dict[str, Any]) -> dict[str, Any]:
    """Where ZCode keeps its hook events:  `hooks.events`, created when absent.  The `enabled`
    flag beside it belongs to the user's config and is never touched here."""
    hooks = doc.get("hooks")
    if hooks is None:
        hooks = doc["hooks"] = {}
    if not isinstance(hooks, dict):
        raise ValueError("the hooks field is not an object")
    events = hooks.get("events")
    if events is None:
        events = hooks["events"] = {}
    if not isinstance(events, dict):
        raise ValueError("the hooks.events field is not an object")
    return events


INSTALL = PL.JsonHookInstaller(
    path=".zcode/cli/config.json",
    events=HOOK_TIMEOUTS,
    locate=_locate_events,
)
