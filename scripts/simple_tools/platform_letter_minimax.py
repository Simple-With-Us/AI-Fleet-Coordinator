"""MiniMax Code on `platform-letter`:  the MiniMax row of the one letter table (owner, Nov 11, 2026:  "one operator
that has one tool/process for all platforms").  The library (`platform_letter`) does the registering, the note, the
retries, the "??" title, the end-of-turn report and the install;  this file is only what is MiniMax Code's own.

- The hooks are Claude Code shaped, in `~/.minimax/hooks.json`:  SessionStart starts, UserPromptSubmit names the
  chat from its first message, SubagentStart gives a subagent its own letter and Stop ends a turn (the turn end
  also puts the letter back on a title MiniMax Code regenerated without it).  The hook does not take a chat's
  title before the chat has a descriptive one:  a brand-new chat keeps the name MiniMax Code gives it, and the
  letter goes on that name at the first prompt.  A SessionStart hook's plain stdout is
  the context the model reads (`output="text"`).  A subagent takes the pair after its parent's (parent CC, child
  CD, the next CE), and MiniMax Code sends the CHILD's own session id as a subagent event's session id, so the
  parent is read from the session store's own parent column and never from the payload.  MiniMax Code has no end event, so no hook closes a registration:  the assigner's own
  sweep frees the letter of a chat nobody came back to.
- Archiving a chat reaches no hook either, and the archive flag is in the session store's own
  `archived` column.  This row reads it (`minimax_gone`, the spec's `gone_sessions`), so a chat's
  start reports the archived chats and the assigner closes their rows.
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
from datetime import UTC, datetime
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
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


def _read_only(path: str) -> sqlite3.Connection:
    """The session store opened read-only, its path percent-encoded for the URI.

    A path with a `?` or a `#` in it would otherwise end the file name early or carry
    parameters of its own, so the URI would name another file, or none, and `mode=ro`
    would not be the only thing in it.

    A path that cannot be resolved at all is an unreadable store, which is what the callers
    answer for, so the path's own failure (an `OSError`, a `ValueError`, a `RuntimeError`)
    is turned into the store's own error and never out of the reader."""
    try:
        uri = Path(path).resolve().as_uri() + "?mode=ro"
    except (OSError, ValueError, RuntimeError) as exc:
        raise sqlite3.OperationalError(str(exc)) from exc
    return sqlite3.connect(uri, uri=True, timeout=2)


def _row(env: Mapping[str, str], sid: str) -> dict[str, Any]:
    path = store_file(env)
    if not sid or not os.path.isfile(path):
        return {}
    try:
        conn = _read_only(path)
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


#: How long a hook waits for a subagent's own row to appear in the session store.  The runtime writes
#: that row a moment AFTER the event that announces the child, so a hook that read the store straight
#: away could see no row at all, leave the child with no parent, and drop the child out of the letter's
#: scope:  it never took a letter.  The wait is short and bounded, and it is spent on the store, not on
#: the registry's own calls, so the hook's budget for those is untouched.
CHILD_ROW_WAIT = 1.5


def _wait_for_parent(env: Mapping[str, str], sid: str, seconds: float) -> str:
    """The child's parent session id, waiting briefly for the child's own row to appear.

    The session store writes a child's row a moment after the event that announces it.  A hook that read
    the row straight away found nothing, left the child with no parent, and the child never took a letter.
    The wait is bounded:  a row that never appears is not waited for forever.
    """
    end = time.monotonic() + seconds
    while True:
        parent = str(_row(env, sid).get("parent_session_id") or "")
        if parent or time.monotonic() >= end:
            return parent
        time.sleep(0.1)


#: The payload keys the runtime may spell a subagent's OWN session id out under.  MiniMax Code builds the
#: user hook payload with `session_id: event.sessionId`, and a subagent event's session id IS the child's own
#: id, so the payload's `session_id` is the child's.  A runtime that spells the child out under its own key is
#: read too.  `agent_id` is deliberately not here:  it is the AGENT's id (the seat's worker name), not a
#: session id, and reading it as one points the row at a session that does not exist.
CHILD_ID_KEYS = ("childSessionId", "child_session_id")
#: The payload keys the runtime may carry a subagent's PARENT session id under.  MiniMax Code's user hook
#: payload does not carry the parent at all today:  the parent lives in the event's `subagentContext`, which
#: the payload builder does not copy into the payload.  The session store has the parent on the child's own
#: row, so that is where the parent is read from; these keys are a fallback for a runtime that does carry it.
#: `parentId` and `parent_id` are deliberately not here:  they name an AGENT (the seat's worker name), not
#: a session, and reading one as a session id points the letter at a session that does not exist.
PARENT_ID_KEYS = ("parentSessionId", "parent_session_id")


def _event(payload: Mapping[str, Any]) -> str:
    """The runtime event name, whichever key the payload carries it under."""
    return str(payload.get("hook_event_name") or payload.get("event") or "")


def _is_subagent_event(payload: Mapping[str, Any]) -> bool:
    """True for the two events the runtime fires around a subagent."""
    return _event(payload) in ("SubagentStart", "SubagentStop")


def minimax_session_id(payload: Mapping[str, Any], env: Mapping[str, str]) -> str:
    """The chat's own session id:  the payload's for an ordinary chat, and the CHILD's for a subagent
    event, because MiniMax Code sends the child's own id there."""
    if _is_subagent_event(payload):
        for key in CHILD_ID_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
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
    sid = _sid(payload, env)
    if _is_subagent_event(payload):
        # The parent's id is NOT in MiniMax Code's user hook payload:  it lives in the event's
        # `subagentContext`, which the payload builder does not copy, and the payload's own session id is the
        # CHILD's own id.  Reading the parent's id out of the payload therefore made a subagent its own
        # parent, and the child took a letter from the wrong place in the tree.  The session store has the
        # parent on the child's own row, so it is read from there, after a short wait for that row:  the
        # store writes it a moment after the event, and a child read too early had no parent at all.
        parent = _wait_for_parent(env, sid, CHILD_ROW_WAIT)
        if parent and parent != sid:
            return parent
        for key in PARENT_ID_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value and value != sid:
                return value
        return ""
    parent = str(_row(env, sid).get("parent_session_id") or "")
    # A chat is never its own parent.  A row that says so is a row the store has not finished writing, and
    # a child that reads itself as its parent takes a MAIN pair instead of the pair after its parent's:  it
    # takes a pair a main chat is owed, and its own family never learns of the child.  The parent is
    # unknown here, and an unknown parent is no parent:  the chat waits for the next event, which reads the
    # row once the store has written it.  The repair scan leaves such rows out for the same reason.
    return "" if parent == sid else parent


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


# --------------------------------------------------------------------------------------------
# Which chats the app has archived (AFC-855)
# --------------------------------------------------------------------------------------------
#
# Archiving a MiniMax Code chat reaches no hook event:  the runtime's hook list has no archive
# event, and its `SessionEnd` fires when a chat ends, not when it is filed away.  The archive is
# in the session store's own `archived` column, the same one `minimax_wake.py` reads before it
# wakes a chat, so that is where this row learns that a chat is gone.  The library's gone report,
# made on a chat's start, then has the assigner close the row and free the pair.

#: The most session ids one query names (SQLite's own limit on the variables of a statement is 999).
GONE_BATCH = 200


def archived_sessions(env: Mapping[str, str], session_ids: Iterable[str]) -> set[str]:
    """The chats of these ids the session store knows are archived, by the store's own `archived` column.

    A chat the store does not know is not named:  it may be a chat of another host's store, or one
    the store has not written yet.  A store with no `archived` column (one from before the runtime
    wrote it) names none, and a store that cannot be read names none either.  Ids only."""
    path = store_file(env)
    if not path or not os.path.isfile(path):
        return set()
    wanted = sorted({sid for sid in session_ids if isinstance(sid, str) and sid})
    if not wanted:
        return set()
    gone: set[str] = set()
    try:
        conn = _read_only(path)
        try:
            if "archived" not in _columns(conn):
                return set()
            for start in range(0, len(wanted), GONE_BATCH):
                chunk = wanted[start : start + GONE_BATCH]
                rows = conn.execute(
                    "SELECT session_id FROM local_runtime_sessions"
                    " WHERE archived = 1 AND session_id IN (%s)" % ",".join("?" * len(chunk)),
                    chunk,
                ).fetchall()
                gone.update(str(row[0]) for row in rows)
        finally:
            conn.close()
    except sqlite3.Error:
        return set()
    return gone


#: The most chats one report may name.  It is the registry's own limit
#: (`operator_registry.MAX_LIVENESS_IDS`),  and the two are pinned equal by a test.
MAX_LIVENESS_IDS = 2000


def minimax_live(env: Mapping[str, str]) -> dict[str, str] | None:
    """The spec's `live_chats`:  the chats this host's store still holds open,  with the store's own
    last-activity time for each.

    The session store is the truth about this host's chats:  it holds every chat MiniMax Code made,
    it says which ones are archived,  and it carries the time each chat was last touched.  The
    registry's sweep cannot read any host's store,  so the host reports its own view here (AFC-878):
    a chat the store still holds keeps its letter,  and a chat it has not touched for longer than
    the window is one the sweep may recycle.

    None means the host cannot answer,  and it is not the same as an empty answer:  a store that
    cannot be read,  or one that holds more chats than one report may name,  says nothing at all,
    and the registry then treats the host as unverified and keeps its letters.  A partial report
    would be worse than none,  because a chat the report leaves out is a chat the host no longer
    holds."""
    path = store_file(env)
    if not path or not os.path.isfile(path):
        return None
    live: dict[str, str] = {}
    try:
        conn = _read_only(path)
        try:
            columns = _columns(conn)
            if "archived" not in columns:
                return None
            stamp = "updated_at_ms" if "updated_at_ms" in columns else ""
            if not stamp:
                return None
            # The query is bounded:  one more row than a report may name is enough to know that
            # this host holds more chats than one report can carry,  and the host then does not
            # report at all.
            rows = conn.execute(
                "SELECT session_id, %s FROM local_runtime_sessions WHERE archived = 0"
                " ORDER BY %s DESC LIMIT ?" % (stamp, stamp),
                (MAX_LIVENESS_IDS + 1,),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if len(rows) > MAX_LIVENESS_IDS:
        return None
    for row in rows:
        sid = str(row[0] or "").strip()
        if not sid or len(sid) > 200:
            continue
        try:
            millis = int(row[1])
        except (TypeError, ValueError):
            continue
        if millis <= 0:
            continue
        moment = datetime.fromtimestamp(millis / 1000.0, UTC)
        live[sid] = moment.isoformat().replace("+00:00", "Z")
    return live


def minimax_gone(env: Mapping[str, str], session_ids: Iterable[str]) -> set[str]:
    """The spec's `gone_sessions`:  which of these chats MiniMax Code has archived.

    The archive flag is the session store's own `archived` column, the same one `minimax_wake.py`
    reads before it wakes a chat.  The library asks this adapter on a chat's start, and a report
    that names an archived chat has the assigner close its row and free the letter.  Ids only."""
    return archived_sessions(env, session_ids)


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


# --------------------------------------------------------------------------------------------
# The chats a repair pass may name (AFC-857)
# --------------------------------------------------------------------------------------------
#
# The repair pass (`repair_unlettered`) replays the start of the chats this row names, so a chat
# whose letter never landed is lettered and titled.  Two kinds of chat are named here, and one kind is
# deliberately not.
#
# - A sub-thread the hook never lettered:  a task row with a parent whose title carries no letter.
#   The hook refused it at its start when the store did not hold its row yet, and a sub-thread that
#   ends never prompts again, so nothing else ever asks for its letter.  The pass replays its start,
#   the store holds its row by then, and the parent is known:  the chat takes the pair after its
#   parent's and its title carries it.
#
# - A chat whose note holds a letter and whose title no longer carries it:  the app's own title
#   service puts its title back over the lettered one.  The note still holds the letter, so the
#   letter goes back where it was, with no registry call:  a letter the registry has since retired
#   is never handed out a second time (AFC-858).
#
# A chat still titled "??: " is NOT named.  It holds no letter, which is the state the owner defined for
# a chat that asked for one and got none (owner, Nov 11, 2026:  "a chat with no letter titles itself
# '??: Title', retries later by itself"), and on a host the "?:" chats are mostly the app's own runs:
# a wake runner, a bot session.  Lettering them would take the seat's pairs (there are twenty-six of
# them) from the chats the fleet addresses, one run at a time, and the owner did not ask for those
# chats to be renamed.  A chat that runs again asks for its letter itself.

#: The most chats one scan names, and the age window a candidate may have:  a chat that is starting
#: now runs its own hook, and one older than the window is history the fleet does not address.
UNLETTERED_SCAN = 20
UNLETTERED_MIN_AGE_SECONDS = 900
UNLETTERED_MAX_AGE_SECONDS = 7 * 86400
#: How many rows one query reads at a time, and how many notes one scan reads.  The query leaves out
#: the chats that carry a letter on their title and the chats the repair pass would skip, so the rows
#: it returns are candidates:  the bound counts candidates and not rows, and a busy host's lettered
#: chats cannot spend it.
UNLETTERED_BATCH = 200
UNLETTERED_NOTES = 500
#: Single-fetch cap for noteless top-level chats, large enough that already-lettered
#: rows cannot starve a later candidate without re-issuing OFFSET pages.
UNLETTERED_READ = 2000
#: One absolute budget for both unlettered-session reads.
UNLETTERED_DEADLINE_SECONDS = 2.0
#: How long a read of the session store waits for a locked one before it reads nothing.
STORE_WAIT = 0.5
#: Age window for top-level chats that predate the hook (wider than the sub-thread window).
CHAT_UNLETTERED_MAX_AGE_SECONDS = 90 * 86400
#: The hook's own no-letter mark, as a SQL pattern.  LIKE reads only % and _ as wildcards, so the two
#: question marks are the mark itself.
UNLETTERED_MARK = PL.UNLETTERED + ": %"
#: The prefixes a chat that holds a letter carries:  one capital letter or two, then a colon and a
#: space.  A GLOB pattern is case sensitive, so a lower-case word is never taken for a letter.
LETTER_GLOB_ONE = "[A-Z]: *"
LETTER_GLOB_TWO = "[A-Z][A-Z]: *"


def _note_sids(env: Mapping[str, str]) -> tuple[dict[str, str], set[str]]:
    """(the letter each chat that holds one holds, keyed by session id, the session ids the pass will
    not touch).

    The pass reads a chat's note and skips the chat when the note says closed or mismatch, or when
    the note holds a letter (`lettered`).  The scan leaves out exactly those chats, so every candidate
    it names is a chat the pass will really work on, and no slot of the twenty is spent on a chat the
    pass skips.

    The letter is in the note and the note is a file, so the lettered chats are read from the
    folder:  a listing and one read per note, at most UNLETTERED_NOTES of them.  The ids come from
    the listing and are used as query values only, so a name the store holds can never place a read
    outside the folder.

    A name is an id only when the library's own check takes it, and only when it is the file the
    library reads for that id:  the library reads `<id>.json` for the trimmed id, so a name with
    spaces around it is no note of any chat and decides nothing.  The store may write the same
    chat's id with spaces around it; the query and the check below read the id trimmed, so such a
    chat is left out as well."""
    folder = os.path.dirname(PL.note_path(env, SEAT, ""))
    try:
        names = os.listdir(folder)
    except OSError:
        return {}, set()
    notes: list[tuple[float, str, str]] = []
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(folder, name)
        try:
            notes.append((os.stat(path).st_mtime, path, name[:-5]))
        except OSError:
            continue
    notes.sort(reverse=True)
    lettered: dict[str, str] = {}
    skipped: set[str] = set()
    for _, path, sid in notes[:UNLETTERED_NOTES]:
        try:
            ident = PL.SI.check(sid, "a listed session id")
        except ValueError:
            continue  # a name that is not a session id names no note
        if ident != sid:
            continue  # the library reads `<id>.json`:  a padded name is no note of any chat
        try:
            with open(path, encoding="utf-8") as fh:
                note = json.load(fh)
        except (OSError, ValueError):
            continue
        if not isinstance(note, dict):
            continue
        if note.get("state") in ("closed", "mismatch"):
            skipped.add(ident)  # the pass skips this chat:  a closed or a mismatched note
        elif PL.lettered(note):
            skipped.add(ident)  # the pass holds this chat's letter:  it is not replayed
            labels = PL.held(note)
            if labels:
                # A real letter, so a title that lost it can be repaired:  the letter is kept, because
                # the title is compared with the letter this chat holds, not with any letter.
                lettered[ident] = labels[0]
    return lettered, skipped


def _unlettered_children(conn: sqlite3.Connection, now: float, skipped: set[str]) -> list[str]:
    """The sub-threads of this host that hold no letter, newest first, and how many rows of the
    store holds.

    The chats the repair pass would skip are left out in the query, so the bound counts the
    candidates and not the rows:  a host whose newest sub-threads are all lettered cannot spend the
    scan's bound on them and starve a genuinely unlettered older row.  A row that names no session
    id raises ValueError:  a corrupt row must not read as a chat that was considered and left
    alone, and only a well-formed id is ever named (the library's own check), so a name the store
    holds can never place anything outside the notes folder.

    The ids are read trimmed on both sides:  the note of a chat is the file of its trimmed id, so a
    store that writes the id with spaces around it names the same chat, and a chat that holds a
    letter is left out whichever way its id is written."""
    clause = ""
    params: list[Any] = []
    if skipped:
        # A row with no id at all is let through:  a NULL never matches the list, and a row that
        # cannot be named must be counted below and not vanish from the count.
        clause = " AND (TRIM(session_id) IS NULL OR TRIM(session_id) NOT IN (%s))" % ",".join(
            "?" * len(skipped)
        )
        params.extend(sorted(skipped))
    rows = conn.execute(
        "SELECT session_id FROM local_runtime_sessions"
        " WHERE session_kind = ?"
        " AND parent_session_id IS NOT NULL AND parent_session_id != ''"
        " AND (session_id IS NULL OR parent_session_id != session_id)"
        " AND (archived IS NULL OR archived = 0)"
        " AND (created_at_ms IS NULL OR (created_at_ms >= ? AND created_at_ms <= ?))"
        " AND title IS NOT NULL AND title != ''"
        " AND title NOT LIKE ?"
        " AND title NOT GLOB ?"
        " AND title NOT GLOB ?" + clause + " ORDER BY updated_at_ms DESC LIMIT ?",
        [
            "task",
            (now - UNLETTERED_MAX_AGE_SECONDS) * 1000,
            (now - UNLETTERED_MIN_AGE_SECONDS) * 1000,
            UNLETTERED_MARK,
            LETTER_GLOB_ONE,
            LETTER_GLOB_TWO,
            *params,
            UNLETTERED_BATCH,
        ],
    ).fetchall()
    found: list[str] = []
    for (sid,) in rows:
        if not isinstance(sid, str) or not sid:
            raise ValueError("session_id must be a non-empty string, got %s" % type(sid).__name__)
        checked = PL.SI.check(sid, "a listed session id")
        if checked in skipped:
            continue  # a chat the pass skips, whose id the store writes with spaces around it
        found.append(checked)
    return found


def _expired(deadline: float):
    def handler() -> int:
        return 1 if time.monotonic() >= deadline else 0

    return handler


def _unlettered_subthreads(
    env: Mapping[str, str], deadline: float, skipped: set[str]
) -> list[str] | None:
    """This host's MiniMax sub-threads that hold no letter on their title, newest first."""
    path = store_file(env)
    if not path or not os.path.isfile(path):
        return None
    if time.monotonic() >= deadline:
        return None
    now = time.time()
    found: list[str] = []
    try:
        conn = _read_only(path)
        try:
            if hasattr(conn, "set_progress_handler"):
                conn.set_progress_handler(_expired(deadline), 1000)
            children = _unlettered_children(conn, now, skipped)
            for sid in children:
                if time.monotonic() >= deadline:
                    return None
                if sid not in found:
                    found.append(sid)
                if len(found) >= UNLETTERED_SCAN:
                    break
        finally:
            conn.close()
    except sqlite3.Error:
        return None  # a store this old cannot say what it holds:  no pass
    return found[:UNLETTERED_SCAN]


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


def _unlettered_noteless_chats(env: Mapping[str, str], deadline: float) -> list[str] | None:
    """Top-level chats that hold no letter note: unknown-age rows first, then dated newest first."""
    path = store_file(env)
    if not path or not os.path.isfile(path):
        return None
    if time.monotonic() >= deadline:
        return None
    now = time.time()
    try:
        conn = sqlite3.connect(
            Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=STORE_WAIT
        )
    except (sqlite3.Error, OSError, ValueError):
        return None
    try:
        if hasattr(conn, "set_progress_handler"):
            conn.set_progress_handler(_expired(deadline), 1000)
        rows = conn.execute(
            "SELECT session_id FROM local_runtime_sessions"
            " WHERE session_kind IN (?, ?)"
            " AND (parent_session_id IS NULL OR parent_session_id = '')"
            " AND (archived IS NULL OR archived = 0)"
            " AND (created_at_ms IS NULL"
            " OR (created_at_ms >= ? AND created_at_ms <= ?))"
            " AND (title IS NULL OR (title NOT LIKE ? AND title NOT GLOB ? AND title NOT GLOB ?))"
            " ORDER BY created_at_ms IS NULL DESC, created_at_ms DESC"
            " LIMIT ?",
            (
                *CHAT_KINDS,
                (now - CHAT_UNLETTERED_MAX_AGE_SECONDS) * 1000,
                (now - UNLETTERED_MIN_AGE_SECONDS) * 1000,
                UNLETTERED_MARK,
                LETTER_GLOB_ONE,
                LETTER_GLOB_TWO,
                UNLETTERED_READ,
            ),
        ).fetchall()
        found: list[str] = []
        for (sid,) in rows:
            if time.monotonic() >= deadline:
                return None
            if not isinstance(sid, str) or not sid:
                raise ValueError(
                    "session_id must be a non-empty string, got %s" % type(sid).__name__
                )
            ident = PL.SI.check(sid, "a listed session id")
            if os.path.isfile(PL.note_path(env, SEAT, ident)):
                continue  # lettered already:  the pass has nothing to do for it
            found.append(ident)
            if len(found) >= UNLETTERED_SCAN:
                break
        return found
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _letter_lost_chats(
    env: Mapping[str, str], deadline: float, lettered: Mapping[str, str]
) -> list[str] | None:
    """Top-level chats whose own note holds a letter and whose title no longer carries it.

    The letter is in the note and the title is in the store, so the chats whose title lost the letter
    are the rows where the two disagree:  the note says which letter the chat holds,  and the store says
    the title does not carry it.  They are named so the pass can put the letter back,  with no registry
    call:  a letter the registry has since retired is never handed out a second time (AFC-858).

    The title is read with the chat's own letter in hand and the library's own check decides:  a title
    that carries another letter is named too (the chat was renamed with a letter of its own), and a title
    that carries this chat's letter is not (there is nothing to restore).  A title on the no-letter mark
    ("??: ") belongs to another rule, and the check leaves it out.

    The age window is the one the chats that hold no letter note use,  so a chat that predates the hook
    and a chat the app renamed are read the same way.  A sub-thread is in this group too:  a sub-thread
    is a lettered chat in its own right,  so its title can lose its letter the same way."""
    if not lettered:
        return []
    path = store_file(env)
    if not path or not os.path.isfile(path):
        return None
    if time.monotonic() >= deadline:
        return None
    now = time.time()
    try:
        conn = sqlite3.connect(
            Path(path).resolve().as_uri() + "?mode=ro", uri=True, timeout=STORE_WAIT
        )
    except (sqlite3.Error, OSError, ValueError):
        return None
    try:
        if hasattr(conn, "set_progress_handler"):
            conn.set_progress_handler(_expired(deadline), 1000)
        rows = conn.execute(
            "SELECT session_id, title FROM local_runtime_sessions"
            " WHERE session_kind IN (?, ?)"
            " AND (archived IS NULL OR archived = 0)"
            " AND (created_at_ms IS NULL"
            " OR (created_at_ms >= ? AND created_at_ms <= ?))"
            " AND TRIM(session_id) IN (%s)" % ",".join("?" * len(lettered)),
            (
                *CHAT_KINDS,
                (now - CHAT_UNLETTERED_MAX_AGE_SECONDS) * 1000,
                (now - UNLETTERED_MIN_AGE_SECONDS) * 1000,
                *sorted(lettered),
            ),
        ).fetchall()
        found: list[str] = []
        for sid, title in rows:
            if time.monotonic() >= deadline:
                return None
            if not isinstance(sid, str) or not sid:
                raise ValueError(
                    "session_id must be a non-empty string, got %s" % type(sid).__name__
                )
            ident = PL.SI.check(sid, "a listed session id")
            label = lettered.get(ident)
            if not label:
                continue  # the store writes the id with spaces around it
            if not PL.letter_lost({"state": "open", "label": label}, title):
                continue  # the title carries the letter this chat holds, or the no-letter mark
            found.append(ident)
            if len(found) >= UNLETTERED_SCAN:
                break
        return found
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def _interleave(*groups: list[str], limit: int) -> list[str]:
    """The groups in turn, so one busy group cannot starve the others, and every id once."""
    found: list[str] = []
    seen: set[str] = set()
    index = 0
    while len(found) < limit:
        added = False
        for group in groups:
            if index >= len(group):
                continue
            added = True
            sid = group[index]
            if sid not in seen:
                seen.add(sid)
                found.append(sid)
                if len(found) >= limit:
                    break
        if not added:
            break
        index += 1
    return found


def minimax_unlettered_sessions(env: Mapping[str, str]) -> list[str] | None:
    """This host's MiniMax chats whose title carries no letter, for the repair pass
    (`repair_unlettered`).

    Three groups are named, then capped at UNLETTERED_SCAN: sub-threads whose title carries no
    letter (newest first by updated_at_ms), then top-level chats that hold no letter note, with
    `created_at_ms IS NULL` rows before dated rows ordered newest first, then the chats whose own
    note holds a letter and whose title no longer carries it (AFC-858:  the letter goes back where
    it was, with no registry call).

    The groups are taken in turn, so one busy group cannot starve the others.

    Read-only, a short wait for a lock, and any doubt (no store, a locked one, a store without the
    columns it needs) answers None:  no pass this time.  A row whose session_id is malformed
    raises ValueError naming the field and type rather than returning a partial inventory.
    All three reads share one absolute deadline; an expired deadline discards partial results and
    answers None.  Eligible top-level rows are fetched once (no OFFSET pages).  A chat's note
    or title is read again in its own replayed start before anything changes."""
    deadline = time.monotonic() + UNLETTERED_DEADLINE_SECONDS
    lettered, skipped = _note_sids(env)
    children = _unlettered_subthreads(env, deadline, skipped)
    chats = _unlettered_noteless_chats(env, deadline)
    lost = _letter_lost_chats(env, deadline, lettered)
    if children is None or chats is None or lost is None:
        # One half could not be read, so this is not a complete inventory:  the pass answers None
        # and the half that failed is repaired on the next pass, rather than a list that hides it.
        return None
    if time.monotonic() >= deadline:
        return None
    return _interleave(children, chats, lost, limit=UNLETTERED_SCAN)


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
    unlettered_sessions=minimax_unlettered_sessions,
    session_env=("__MAVIS_RUNTIME_SESSION_ID",),
    gone_sessions=minimax_gone,
    live_chats=minimax_live,
    retired_note=minimax_retired_note,
    retired_files=RETIRED_FILES,
    call_timeout=CALL_TIMEOUT,
    parent_call_timeout=PARENT_CALL_TIMEOUT,
    hook_budget=HOOK_BUDGET,
)

INSTALL = PL.JsonHookInstaller(
    path=os.path.join(".minimax", "hooks.json"), events=HOOK_TIMEOUTS, timeout_cap=HOOK_TIMEOUT_CAP
)
