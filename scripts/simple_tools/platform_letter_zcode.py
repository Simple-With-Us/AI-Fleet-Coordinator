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
- ZCode runs no hook inside a sub-agent session, so no child of its own ever registers itself:  a lettered
  parent's own turn end enrolls the children the store names for it (`platform_letter.enroll_children`, on the
  Stop pass), and the repair pass lists a lettered parent's children too (`zcode_unlettered_sessions`).  A
  child whose parent holds no letter waits for its parent's own letter, and a refusal (the parent's row is
  full) is the child's failed note, retried no sooner than the library's own retry window.
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

import json
import os
import re
import sqlite3
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import platform_letter as PL
from . import session_ids as SI

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


# The repair pass (`platform_letter.repair_unlettered`) asks for the interactive sessions still showing the "??: "
# placeholder:  at most this many, only ones old enough that their own start hook has finished (a brand-new "??"
# can be its start in flight), never an archived or deleted one, and none older than a week.
UNLETTERED_SCAN = 20
UNLETTERED_MIN_AGE_SECONDS = 900
UNLETTERED_MAX_AGE_SECONDS = 7 * 86400

# The parent-side enrollment pass (`platform_letter.enroll_children`) asks the store for the sub-agent children
# of one lettered chat:  at most CHILD_SCAN rows leave the store, newest first (the pass replays fewer of them
# per turn end), and none older than CHILD_MAX_AGE_SECONDS - a child that old is a thread long over, one the
# repair pass's own window already covers for whatever straggler a live parent's pass misses.
CHILD_SCAN = 24
CHILD_MAX_AGE_SECONDS = 7 * 86400


def zcode_unlettered_sessions(env: Mapping[str, str]) -> list[str] | None:
    """This host's sessions whose own start never landed a letter, newest first, for the repair a session's
    start sets off (`platform_letter.repair_after_start`).  Two kinds belong:  an interactive session whose
    title still carries the "??: " placeholder, and a sub-agent child whose parent's own note holds a letter -
    ZCode runs no hook inside a sub-agent, so no start of a child's own ever comes, and its parent's row is
    where its letter belongs.  A child whose parent holds no letter is left out (its replay would register the
    long-gone parent first), and so is one whose own note already holds a letter, an archived or deleted
    session of either kind, and an id that is not a well-formed session id.  Read only, a short wait for a
    lock, and any doubt (no store, a locked one, a store without the columns this reads) answers None:  no pass
    this time.  Each session's title is read again in its own replayed start before anything changes."""
    path = store_file(env)
    if not os.path.isfile(path):
        return None
    try:
        conn = sqlite3.connect("%s?mode=ro" % Path(path).as_uri(), uri=True, timeout=0.5)
    except (sqlite3.Error, ValueError):
        return None
    try:
        # The store's shape is not one fixed schema (the module docstring's own table and the
        # test fixture disagree on which time columns exist), so the query is built from the
        # columns the store actually has:  a query naming a column that is not there would
        # raise and answer no sessions at all, every time (Kody, PR 2307).
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(session)").fetchall()}
        where = ["title LIKE '??: %'"]
        if "task_type" in cols:
            where.append("task_type = 'interactive'")
        if "time_archived" in cols:
            where.append("time_archived IS NULL")
        if "time_deleted" in cols:
            where.append("time_deleted IS NULL")
        time_col = next((name for name in ("time_created", "time_updated") if name in cols), "")
        # Unknown age is not evidence that a session's own start hook finished.
        # Skip this pass rather than replaying potentially in-flight sessions.
        if not time_col:
            return None
        select = "id, " + time_col
        order = "ORDER BY %s DESC" % time_col
        rows = conn.execute(
            "SELECT %s FROM session WHERE %s %s" % (select, " AND ".join(where), order),
        )
        # Stream candidates until the eligible result cap, rather than truncating before
        # normalized age checks.  Recent starts must not starve older repair candidates.
        now = time.time()
        found: list[tuple[float, str]] = []
        for row in rows:
            try:
                sid = SI.check(row[0] if isinstance(row[0], str) else "", "a stored session id")
            except ValueError:
                continue
            stamp = _stored_seconds(row[1] if len(row) > 1 else None)
            if stamp is None or not (
                UNLETTERED_MIN_AGE_SECONDS <= now - stamp <= UNLETTERED_MAX_AGE_SECONDS
            ):
                continue
            found.append((stamp, sid))
            if len(found) >= UNLETTERED_SCAN:
                break
        found.extend(_unlettered_children(conn, cols, time_col, env, now))
        found.sort(key=lambda found_row: found_row[0], reverse=True)
        return [sid for _stamp, sid in found[:UNLETTERED_SCAN]]
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _note_dict(env: Mapping[str, str], sid: str) -> dict:
    """The chat's own letter note as a dict, or {} when it cannot be read:  a note the hook cannot read holds
    nothing - no letter, no failure, no throttle - so its chat is treated as one that never had one."""
    try:
        with open(PL.note_path(env, SEAT, sid)) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 - a note that cannot be read is one that holds nothing
        return {}


def _unlettered_children(
    conn: sqlite3.Connection, cols: set[str], time_col: str, env: Mapping[str, str], now: float
) -> list[tuple[float, str]]:
    """The sub-agent children a repair pass may enroll, as (age stamp, id), newest first:  young enough for
    the pass's own window, not archived or deleted, and their parent's note holds a letter
    (`platform_letter.note_holds_letter`, read once per parent), so the replay registers the child in its
    parent's row and never a parent nobody ever lettered.  A child whose own note already holds a letter is
    left out (the replay would only mark a dormant child seen again), and so is one whose note holds a
    refusal or failure inside `RETRY_SECONDS`:  a throttled child may not take a replay slot a healthy
    candidate could use.  The scan stops at the first row older than the window (rows come newest first),
    so a store full of long-over children costs no walk and no note read.  At most UNLETTERED_SCAN of them;
    a store without the columns this reads answers none."""
    if "parent_id" not in cols or "task_type" not in cols:
        return []
    where = ["task_type = 'subagent_child'"]
    if "time_archived" in cols:
        where.append("time_archived IS NULL")
    if "time_deleted" in cols:
        where.append("time_deleted IS NULL")
    found: list[tuple[float, str]] = []
    lettered_parents: dict[str, bool] = {}  # one note read per parent, not one per child
    try:
        rows = conn.execute(
            "SELECT id, parent_id, %s FROM session WHERE %s ORDER BY %s DESC"
            % (time_col, " AND ".join(where), time_col)
        )
        for row in rows:
            try:
                sid = SI.check(row[0] if isinstance(row[0], str) else "", "a stored session id")
                parent = SI.check(row[1] if isinstance(row[1], str) else "", "a stored parent id")
            except ValueError:
                continue  # neither id may name a note path, so neither is read unchecked
            stamp = _stored_seconds(row[2] if len(row) > 2 else None)
            if stamp is None:
                continue  # unknown age is not evidence a child is young
            age = now - stamp
            if age > UNLETTERED_MAX_AGE_SECONDS:
                break  # newest first:  every later row is older still, the scan stops here
            if age < UNLETTERED_MIN_AGE_SECONDS:
                continue
            if not parent:
                continue
            if parent not in lettered_parents:
                lettered_parents[parent] = PL.note_holds_letter(env, SEAT, parent)
            if not lettered_parents[parent]:
                continue  # no lettered parent here:  the replay would register the parent first
            note = _note_dict(env, sid)
            if PL.lettered(note):
                continue  # already lettered:  a replay would only mark a dormant child seen
            if now - PL._seconds(note.get("failed_at")) < PL.RETRY_SECONDS:
                continue  # refused or failed this window:  no replay slot until the window passes
            found.append((stamp, sid))
            if len(found) >= UNLETTERED_SCAN:
                break
    except sqlite3.Error:
        return found
    return found


def zcode_child_sessions(env: Mapping[str, str], parent: str) -> list[str] | None:
    """The sub-agent children the store names for `parent`, newest first, for the enrollment a parent's
    turn end makes (`platform_letter.enroll_children`).  Read only, a short wait for a lock, and any doubt
    (no store, a store without the `parent_id`, `task_type` or time columns this reads) answers None:  no
    enrollment this turn.  A row whose age cannot be read is left out - unknown age is not evidence a child
    is young - and the listing stops at the first row older than CHILD_MAX_AGE_SECONDS (rows come newest
    first):  a child that old is a thread long over, one the repair pass's own window already covers.  A
    child whose own note already holds a letter, or holds a refusal or failure inside `RETRY_SECONDS`, is
    read but takes no listing slot:  a lettered or throttled child never crowds an unlettered one out of
    CHILD_SCAN, whatever their ages.  A row whose id is not a session id is left out unread, and after the
    age break the listing examines at most 4 * CHILD_SCAN rows, so a store full of lettered or throttled
    children costs bounded reads, not a walk of the table."""
    if not parent:
        return None
    path = store_file(env)
    if not os.path.isfile(path):
        return None
    try:
        conn = sqlite3.connect("%s?mode=ro" % Path(path).as_uri(), uri=True, timeout=0.5)
    except (sqlite3.Error, ValueError):
        return None
    try:
        cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(session)").fetchall()}
        if "parent_id" not in cols or "task_type" not in cols:
            return None
        time_col = next((name for name in ("time_created", "time_updated") if name in cols), "")
        if not time_col:
            return None
        rows = conn.execute(
            "SELECT id, %s FROM session WHERE task_type = 'subagent_child' AND parent_id = ?"
            " ORDER BY %s DESC" % (time_col, time_col),
            (parent,),
        )
        now = time.time()
        found: list[str] = []
        examined = 0
        for row in rows:
            try:
                sid = SI.check(
                    row[0] if isinstance(row[0], str) else "", "a listed child session id"
                )
            except ValueError:
                continue  # the id names the note path, so it is never read unchecked
            stamp = _stored_seconds(row[1] if len(row) > 1 else None)
            if stamp is None:
                continue  # unknown age is not evidence a child is young
            if now - stamp > CHILD_MAX_AGE_SECONDS:
                break  # newest first:  every later row is older still, the listing stops here
            examined += 1
            if examined > 4 * CHILD_SCAN:
                break  # lettered and throttled rows take no slot, but the note reads still stop here
            note = _note_dict(env, sid)
            if PL.lettered(note) or now - PL._seconds(note.get("failed_at")) < PL.RETRY_SECONDS:
                continue  # costs its one note read, but no listing slot (the enrollment pass
                # re-reads the note inside its own guard, so this is a filter, not the check)
            found.append(sid)
            if len(found) >= CHILD_SCAN:
                break
        return found
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _stored_seconds(value: object) -> float | None:
    """A store timestamp in seconds, or None:  seconds, or milliseconds past 1e12."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    stamp = float(value)
    if stamp > 1e12:
        stamp /= 1000.0
    return stamp if stamp > 0 else None


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
                _record_thread_link(env, sid, record=getattr(request, "record_thread_link", None))
                return True
            return False
        except sqlite3.Error:
            if attempt == 0:
                time.sleep(0.25)
                continue
    return False


def _record_thread_link(env: Mapping[str, str], sid: str, *, record: Any = None) -> None:
    """Put this chat's thread link on its registry row (one update op, failures swallowed).

    The registry's Sessions tab carries a Thread Link column and the Sheets drain exports
    it, so the row is where a reader looks;  the link is the locator convention because
    ZCode mints no share URLs and has no URI scheme (checked live:  zero share_url values
    in the store).  The location is the install's own rule read from the environment
    (SESSION_LETTER_LOCATION, else the platform), the same way the hook command carries it."""
    from . import registry_api as RA

    if record is None and not RA.base_url(env):
        return
    location = str(env.get("SESSION_LETTER_LOCATION") or "").strip() or (
        "mac" if sys.platform == "darwin" else "server"
    )
    try:
        answer = (
            record(THREAD_LINK_FORM.format(sid=sid))
            if record is not None
            else RA.call(
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
    unlettered_sessions=zcode_unlettered_sessions,
    child_sessions=zcode_child_sessions,
    title_writer=zcode_rename,
    locations=("mac", "server"),
    # The wrapper the retired ZCode client left on a host:  `install --check` names it while it is there.
    retired_files=(".local/bin/zc-letter",),
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
