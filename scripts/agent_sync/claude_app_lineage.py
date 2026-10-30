"""Which chat started this one, read from the Claude desktop app's own record of the chat (AFC-564, Oct 29, 2026).

A sub-thread, a chat another chat started, takes the pair after its parent's (`operator_registry.pairs_after`).  Claude
Code gives a hook no field, variable or file that names the parent:  the hook's stdin, its environment and the chat's
transcript were checked against Claude Code 2.1.309 and name only the chat itself.  The desktop app keeps one JSON
record per chat on the Mac, at

    <home>/Library/Application Support/Claude/claude-code-sessions/<account>/<org>/local_<id>.json

The record's file name is the app's own id for the chat, which is `CLAUDE_CODE_HOST_SESSION_ID` in the environment of
the chat's process;  its `cliSessionId` is the chat's Claude Code session UUID, the key the Operator's registry uses;
and a chat another chat started carries `spawnedFrom.sessionId`, the parent's app id.  So the parent is the chat's
record, then `spawnedFrom.sessionId`, then that record's `cliSessionId`.  The app's id is not the Claude Code UUID (it
differs for almost every chat), so the parent's own record is always read.

The record is the app's private, unversioned file, and it also holds the chat's title and MCP wiring.  Only three
fields are read (`cliSessionId`, `spawnedFrom.sessionId`, `spawnedFrom.deletedParent`):  `_project` drops everything
else before a record leaves its file, and only ids come back.  Every read is bounded, every id is checked before it
builds a path, and a surprise of any kind answers "" with a reason, which means no parent is known:  no store (the
server, a terminal `claude` chat), no record, a deleted parent, a parent that never ran, a file caught mid-write.  The
chat is then picked like any other.  A user fork (`forkedFromSessionId`) is not a sub-thread here.

Standard library only, Python 3.11+.
"""

from __future__ import annotations

import json
import os
import re
import time

from . import session_ids as SI

STORE = ("Library", "Application Support", "Claude", "claude-code-sessions")
# The app's id for a chat.  It builds a file name, so nothing but these characters is ever used.
HOST_ID = re.compile(r"local_[A-Za-z0-9-]{1,80}")
RECORD_BYTES = (
    8 << 20
)  # a record is about 0.65 MB (it holds the chat's MCP wiring);  nothing near this is a record
SCAN_RECORDS = 300  # the newest records looked through when the chat's app id is not known
SCAN_BYTES = 128 << 20  # the most a scan reads, counting a file too big to read at its limit
LOAD_ATTEMPTS = 3  # reads of a record the app may be rewriting
RETRY_PAUSE = 0.05  # seconds between them:  longer than a truncate-then-write leaves a file empty, short for a hook

# Why no parent was found, for the registry's answer.  "" means one was.
NO_STORE = "no_store"
NO_RECORD = "no_record"
NOT_STARTED = "not_started_by_chat"
PARENT_GONE = "parent_gone"
PARENT_NEVER_RAN = "parent_never_ran"
UNREADABLE = "unreadable"
SAME_CHAT = "parent_is_self"


def store_root(home: str) -> str:
    return os.path.join(home, *STORE)


def _subdirs(path: str) -> list[str]:
    try:
        with os.scandir(path) as entries:
            return [e.path for e in entries if e.is_dir(follow_symlinks=False)]
    except OSError:
        return []


def _org_dirs(root: str) -> list[str]:
    return [org for account in _subdirs(root) for org in _subdirs(account)]


def _find(root: str, host_id: str) -> str:
    """The path of the record named for an app id, or "" (the id is checked here, so it cannot leave the store)."""
    if not HOST_ID.fullmatch(host_id):
        return ""
    for org in _org_dirs(root):
        path = os.path.join(org, host_id + ".json")
        if os.path.isfile(path) and not os.path.islink(path):
            return path
    return ""


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read(RECORD_BYTES + 1)
    except OSError:
        return None
    return text if len(text) <= RECORD_BYTES else None


def _project(data: object) -> dict | None:
    """The three fields this module reads, and nothing else of the record (its title, its MCP wiring and its message
    text stay in the file), or None when the record is not an object."""
    if not isinstance(data, dict):
        return None
    out: dict = {}
    if isinstance(data.get("cliSessionId"), str):
        out["cliSessionId"] = data["cliSessionId"]
    spawned = data.get("spawnedFrom")
    if isinstance(spawned, dict):
        out["spawnedFrom"] = {
            "sessionId": spawned["sessionId"] if isinstance(spawned.get("sessionId"), str) else "",
            "deletedParent": bool(spawned.get("deletedParent")),
        }
    return out


def _parse(text: str) -> dict | None:
    try:
        return _project(json.loads(text))
    except (ValueError, RecursionError):  # not JSON, or nested past what the parser will follow
        return None


def _load(path: str) -> dict | None:
    """The record's three fields, or None when it cannot be read.  A file the app is rewriting (a truncate, then the
    write) can be caught empty or half written, and a read right after lands in the same window, so it is read up to
    LOAD_ATTEMPTS times with a short pause between."""
    for attempt in range(LOAD_ATTEMPTS):
        if attempt:
            time.sleep(RETRY_PAUSE)
        text = _read(path)
        if text is None:
            return None
        record = _parse(text)
        if record is not None:
            return record
    return None


def _cli_id(record: dict) -> str:
    value = record.get("cliSessionId")
    if not isinstance(value, str):
        return ""
    try:
        return SI.check(value, "the chat's session id")
    except ValueError:
        return ""


def _record_for_cli_id(root: str, cli_session_id: str) -> tuple[dict | None, bool]:
    """(the record whose cliSessionId is this Claude Code session UUID, or None;  whether one that held the UUID could
    not be read), found by looking through the newest records."""
    files: list[tuple[float, int, str]] = []
    for org in _org_dirs(root):
        try:
            with os.scandir(org) as entries:
                for e in entries:
                    try:
                        if (
                            e.name.startswith("local_")
                            and e.name.endswith(".json")
                            and e.is_file(follow_symlinks=False)
                        ):
                            st = e.stat()
                            files.append((st.st_mtime, st.st_size, e.path))
                    except OSError:
                        continue  # one file that vanished, not the rest of the folder
        except OSError:
            continue
    files.sort(reverse=True)
    spent = 0
    unreadable = False
    for _, size, path in files[:SCAN_RECORDS]:
        if size > RECORD_BYTES:
            spent += RECORD_BYTES
        else:
            text = _read(path)
            if text is not None:
                spent += len(text)
                if (
                    cli_session_id in text
                ):  # most records do not hold the id at all, so most are never parsed
                    record = _parse(text)
                    if record is None:
                        record = _load(path)  # caught mid-write:  read it once more
                    if record is None:
                        unreadable = True
                    elif record.get("cliSessionId") == cli_session_id:
                        return record, unreadable
        if spent > SCAN_BYTES:
            break
    return None, unreadable


def lookup(
    home: str, *, host_session_id: str = "", cli_session_id: str = "", allow_lag: bool = False
) -> tuple[str, str]:
    """(the Claude Code session UUID of the chat that started this one, why none): ("<uuid>", "") when one is known,
    ("", reason) otherwise, the reason one of the constants above.

    The chat is found by its app id (`host_session_id`, from CLAUDE_CODE_HOST_SESSION_ID), else by its Claude Code
    session UUID.  Only a chat the app started from another chat (`spawnedFrom`) has a parent.

    A `claude` started inside a chat (a Bash tool, tmux) inherits that chat's app id, so the record the id names can
    be the outer chat's, not this one's.  When that record's own session UUID is valid and is not this chat's, it is
    not used:  this chat is looked for by its session UUID instead, and a nested run, which has no record, has no
    parent.  `allow_lag` lets the app id decide anyway, for the start after a `/clear`, when the app may not have
    written the new session UUID into the record yet."""
    root = store_root(home)
    if not os.path.isdir(root):
        return "", NO_STORE
    path = _find(root, host_session_id) if host_session_id else ""
    record = None
    if path:
        record = _load(path)
        if record is None:
            return "", UNREADABLE
        mine = _cli_id(record)
        if mine and cli_session_id and mine != cli_session_id and not allow_lag:
            record = None  # another chat's record:  this chat is judged by its own session UUID
    if record is None:
        if not cli_session_id:
            return "", NO_RECORD
        record, unreadable = _record_for_cli_id(root, cli_session_id)
        if record is None:
            return "", UNREADABLE if unreadable else NO_RECORD
        host_session_id = (
            ""  # the app id named another chat's record, so it says nothing about this one
        )
    spawned = record.get("spawnedFrom")
    if not spawned or not spawned["sessionId"]:
        return "", NOT_STARTED
    parent_host = spawned["sessionId"]
    if spawned["deletedParent"]:
        return "", PARENT_GONE
    if parent_host == host_session_id:
        return "", SAME_CHAT
    parent_path = _find(root, parent_host)
    if not parent_path:
        return "", PARENT_GONE
    parent_record = _load(parent_path)
    if parent_record is None:
        return "", UNREADABLE
    parent = _cli_id(parent_record)
    if not parent:
        return "", PARENT_NEVER_RAN
    if parent == (_cli_id(record) or cli_session_id):
        return "", SAME_CHAT
    return parent, ""


def parent_of(home: str, *, host_session_id: str = "", cli_session_id: str = "") -> str:
    """The parent's Claude Code session UUID, or "" (see `lookup` for why none)."""
    return lookup(home, host_session_id=host_session_id, cli_session_id=cli_session_id)[0]
