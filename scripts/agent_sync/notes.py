"""Apple Notes from any host (AFC-486, docs/protocols/operator-notes.md).

Notes live in the Mac's iCloud `Coding` folder and only the Mac can touch them, through
scripts/apple-notes-coding.sh.  This module is the half every caller shares:

  direct    the Mac runs the script itself (`run_direct`):  simple-notes and the stdio MCP on a Mac, and
            the Mac runner (notes_runner.py) for everyone else.  The title, mode and flags go on the
            command line;  the body NEVER does:  markdown goes in on standard input and HTML through a
            mode-600 temporary file named by --html, so no note text appears in a process listing.
  operator  every other host sends a request object to the Operator's notes queue (notes_client.py,
            notes_queue.py), and the Mac runner takes it from there.  No Zulip message is involved.

A request is one JSON object (`v` 2):

    {"v":2,"action":"update","request_id":"nr0123456789abcdef0123","identity":"CLAUDE",
     "title":"[AFC, Claude] Topic","markdown":"...","flags":{"pin":true},"session":"..."}

`check_request` validates one wherever it arrives (the caller, the Operator, the runner) and `make_request`
builds one.  `request_problem` adds the rules that depend on who asks:  a create's title carries the caller's
own agent name, and a create or update carries a session id (AFC-361) unless the caller is a single-thread
identity.  Every other action may name any note in the Coding folder (owner, Oct 22:  agents fix formatting
across every agent's notes), and an update of a note whose title does not carry the caller's name runs with
--existing-only, so it never creates an untagged note.

Every title holds no quote, backslash or control character (the script puts it inside AppleScript source on
its pin and notify paths) and never starts with `-`;  flags are a closed set, and a summary follows the
title's character rule.  The script enforces the note's formatting and logs every run (`read_action_log`).

Python 3.11+, standard library only.
"""

from __future__ import annotations

import hashlib
import html
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import identity as I
from . import session_ids as SID

REQUEST_VERSION = 2
ACTIONS = ("create", "update", "pin", "unpin", "read", "list")
WRITE_ACTIONS = ("create", "update", "pin", "unpin")
BODY_ACTIONS = ("create", "update")
FORMATS = ("html", "text", "raw")
TITLE_MAX = 200
SUMMARY_MAX = 280
PR_MAX = 40
QUERY_MAX = 100
LIST_DEFAULT, LIST_MAX = 100, 500
MARKDOWN_MAX = 500_000  # characters a tool accepts
BODY_TOO_LONG = "the note body is longer than %d characters;  nothing was written"
#  UTF-8 bytes of a note a read returns;  past it the body is cut and `truncated` is true.  Counted in bytes, not
#  characters, so a runner's result (the body as JSON, quotes escaped) always fits the Operator's 4 MiB request body.
READ_MAX = 1_500_000
SCRIPT_NAME = "apple-notes-coding.sh"
#  The helper wraps its Notes calls in a 900 s AppleEvent timeout (NOTES_APPLE_EVENT_TIMEOUT_S in
#  scripts/apple-notes-coding.sh),  because a body the size of the master list takes longer than
#  AppleScript's two-minute default.  This cap has to sit above that timeout:  a cap below it
#  kills the write while osascript is still working,  and the later completion creates a second
#  note.  It has to sit above the pin phase that follows the write as well (about 120 s:  the
#  Shortcut runs are bounded by NOTES_SHORTCUT_TIMEOUT_S, and the helper waits up to
#  NOTES_PIN_LOCK_WAIT_S for its own cross-process pin lock):  a cap that cuts the pin phase short
#  reports outcome_unknown for a note that was written,  and the request id is then refused.
#  The wait for the run lock is counted inside this cap:  a request waits LOCK_WAIT_S for the
#  lock and the script then gets what is left of it,  so the whole request stays inside the cap
#  instead of outliving it by the wait.
#  scripts/test_apple_notes_timeout.py holds the two together (AFC-436).
SCRIPT_TIMEOUT_S = 1200.0
READ_TIMEOUT_S = 180.0  # a read or a list changes nothing, so it is not held to the write cap
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")
SEAT_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,31}$")
FLAG_NAMES = ("pin", "notify", "needs_owner", "summary", "pr", "html")
_TITLE_BAD_RE = re.compile(r'["\\\x00-\x1f\x7f\x85  ]')
_PR_RE = re.compile(r"^[0-9#, ]{1,40}$")
_BRACKET_RE = re.compile(
    r"\[([A-Z0-9]+(?:, [A-Z0-9]+)*), ([A-Za-z][A-Za-z0-9]*(?:[ -][A-Za-z0-9]+)*)\] \S.*"
)

# Script exit codes (scripts/apple-notes-coding.sh header).
EXIT_USAGE, EXIT_NOT_FOUND, EXIT_PIN_FAILED = 2, 3, 4
# The environment the script gets:  nothing that holds a credential.
CHILD_ENV_KEYS = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "USER",
    "LOGNAME",
    "SHELL",
    #  Not a credential:  a local narrowing override for the APPLE_NOTES_PIN_RETRY fleet
    #  knob (whether the script may run the pin Shortcut twice;  a miss can pop a note
    #  picker).  It can only turn the retry off, never on.  Listed here because the
    #  allow-list is otherwise the only thing that decides reachability, and this
    #  caller drives the script under a lock and the SCRIPT_TIMEOUT_S cap (AFC-431).
    "APPLE_NOTES_PIN_RETRY",
    #  Not a credential:  where the script writes its action log (AFC-486), or `off`.
    "APPLE_NOTES_ACTION_LOG",
)


class NotesError(Exception):
    """A refusal with a stable code (invalid_argument, not_found, unavailable, outcome_unknown, internal) and a
    message that never holds the note's body.  `outcome_unknown` means the script may have written the note."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SecretRefused(NotesError):
    """A write whose caller-written text looks like it holds a secret.  Its code is invalid_argument, as the queue and
    simple-notes answer;  the MCP tools answer it as refused_secret, the code their contract names for it."""

    def __init__(self, field: str, kind: str) -> None:
        super().__init__(
            "invalid_argument",
            "refusing to write:  the %s looks like it contains %s" % (field, kind),
        )


# --------------------------------------------------------------------------------------------
# Titles, flags, request ids
# --------------------------------------------------------------------------------------------


def check_title(raw: Any, action: str = "create") -> str:
    """The title as the script will see it, or NotesError.  Any title holds no character that is special inside
    AppleScript source and never starts with `-`, so it can never read as a flag.  A create's title must also read
    `[TEAM_KEY, Agent] Topic`;  every other action names an existing note, whatever its title."""
    if not isinstance(raw, str):
        raise NotesError("invalid_argument", "argument 'title' must be a string")
    title = raw.strip()
    if not title:
        raise NotesError("invalid_argument", "argument 'title' must not be empty")
    if len(title) > TITLE_MAX:
        raise NotesError(
            "invalid_argument",
            "argument 'title' is %d characters; at most %d" % (len(title), TITLE_MAX),
        )
    if _TITLE_BAD_RE.search(title):
        raise NotesError(
            "invalid_argument",
            "argument 'title' must not hold a double quote, a backslash or a control character",
        )
    if title.startswith("-"):
        raise NotesError("invalid_argument", "argument 'title' must not start with -")
    # Every title is sent to the Operator (a read's too), so it is scanned here, before any check that could name a
    # part of it (request_problem names the agent a create's title carries).  A create's refusal is the write scan's.
    from . import secretscan  # lazy, as in withhold_secrets

    kind = secretscan.scan(title)
    if kind and action == "create":
        raise SecretRefused("title", kind)
    if kind:
        raise NotesError(
            "invalid_argument",
            "argument 'title' looks like it contains a secret;  nothing was sent",
        )
    if action == "create" and title_agent(title) is None:
        raise NotesError(
            "invalid_argument",
            'a new note\'s title must read "[TEAM_KEY, Agent] Topic", such as "[AFC, Claude] Topic"',
        )
    return title


def check_flags(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """A closed set of flags, each of its own type.  `pin` defaults to true (the script's own default);
    `activate` is not a flag at all:  a request must never steal the owner's focus.  A flag set to its default
    (pin true;  notify, needs_owner and html false) is left out, so the same request reads the same, and
    request_digest matches, whether a tool's schema filled the default in or simple-notes left it unset."""
    if raw is not None and not isinstance(raw, Mapping):
        raise NotesError("invalid_argument", "argument 'flags' must be an object")
    flags = dict(raw or {})
    unknown = [name for name in flags if name not in FLAG_NAMES]
    if unknown:
        raise NotesError(
            "invalid_argument",
            "unknown flag %s; flags are %s"
            % (
                "'%s'" % unknown[0]
                if re.fullmatch(r"[A-Za-z0-9_]{1,40}", str(unknown[0]))
                else "(name not shown)",
                ", ".join(FLAG_NAMES),
            ),
        )
    out: dict[str, Any] = {}
    for name in ("pin", "notify", "needs_owner", "html"):
        if name in flags:
            if not isinstance(flags[name], bool):
                raise NotesError("invalid_argument", "flag '%s' must be true or false" % name)
            if flags[name] is not (name == "pin"):
                out[name] = flags[name]  # only a value that differs from the default
    if "summary" in flags and flags["summary"] is not None:
        summary = flags["summary"]
        if (
            not isinstance(summary, str)
            or len(summary) > SUMMARY_MAX
            or _TITLE_BAD_RE.search(summary)
        ):
            raise NotesError(
                "invalid_argument",
                "flag 'summary' must be text of at most %d characters with no double "
                "quote, backslash or control character" % SUMMARY_MAX,
            )
        if summary:
            out["summary"] = summary
    if "pr" in flags and flags["pr"] is not None:
        pr = flags["pr"]
        if not isinstance(pr, str) or not _PR_RE.match(pr):
            raise NotesError(
                "invalid_argument",
                'flag \'pr\' must be pull request numbers such as "46" or "46, 47"',
            )
        if pr.strip():
            out["pr"] = pr.strip()
    return out


def check_format(raw: Any) -> str:
    if raw is None:
        return "html"
    if raw not in FORMATS:
        raise NotesError(
            "invalid_argument", "argument 'format' must be one of %s" % ", ".join(FORMATS)
        )
    return str(raw)


def check_query(raw: Any, name: str = "query") -> str:
    """A list query or a log title filter (`name` is the argument a refusal names)."""
    if raw is None:
        return ""
    if (
        not isinstance(raw, str)
        or len(raw) > QUERY_MAX
        or _TITLE_BAD_RE.search(raw)
        or raw.strip().startswith("-")
    ):
        raise NotesError(
            "invalid_argument",
            "argument '%s' must be text of at most %d characters with no double quote, backslash or "
            "control character, and must not start with -" % (name, QUERY_MAX),
        )
    from . import secretscan  # lazy, as in withhold_secrets:  the query is sent to the Operator

    if secretscan.scan(raw):
        raise NotesError(
            "invalid_argument",
            "argument '%s' looks like it contains a secret;  nothing was sent" % name,
        )
    return raw.strip()


def check_limit(raw: Any, default: int = LIST_DEFAULT, maximum: int = LIST_MAX) -> int:
    if raw is None:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= maximum:
        raise NotesError(
            "invalid_argument", "argument 'limit' must be an integer from 1 to %d" % maximum
        )
    return raw


def check_identity(raw: Any) -> str:
    if not isinstance(raw, str) or not SEAT_RE.fullmatch(raw):
        raise NotesError("invalid_argument", "the request names no valid identity")
    return raw


def text_fields(request: Mapping[str, Any]) -> dict[str, str]:
    """Every caller-written text of a write, for the secret scan:  title, body and the text flags.  The
    summary reaches the script on its command line (--summary has no other channel in the script) and the
    owner's notification, so the scan is its guard."""
    flags = request.get("flags") or {}
    fields = {
        "title": str(request.get("title") or ""),
        "markdown": str(request.get("markdown") or ""),
    }
    for name in ("summary", "pr"):
        if flags.get(name):
            fields[name] = str(flags[name])
    return {name: value for name, value in fields.items() if value}


def scan_write(request: Mapping[str, Any]) -> None:
    """SecretRefused when any caller-written text of a write (text_fields) looks like it holds a secret.  The scan
    every path runs when the caller brings none of its own:  notes_client.call, and the Mac runner's second check."""
    from . import secretscan  # lazy, as in withhold_secrets

    for field, text in text_fields(request).items():
        kind = secretscan.scan(text)
        if kind:
            raise SecretRefused(field, kind)


DIRECT_TTL = 24 * 3600.0


def direct_once(
    store: Any, request: Mapping[str, Any], send: Callable[[], dict[str, Any]]
) -> dict[str, Any]:
    """Run `send` (the direct runner) once per request_id, under the persistent idempotency rows every stdio
    writer of this seat shares (idem.IdemStore).  A repeat of a finished request returns its first result with
    duplicate true.  A request that may have written (a timeout, a crash) is never repeated:  it is refused as
    outcome_unknown until the owner checks the note and sends it again under a new request_id.  A failure that
    provably wrote nothing drops its row, so the same id may be tried again."""
    key = "k:notes-direct:%s" % request["request_id"]
    verdict, row = store.begin(key, DIRECT_TTL, {"tool": "notes_direct"})
    if verdict == "busy":
        raise NotesError(
            "unavailable", "the same request is already in flight in another session of this seat"
        )
    if verdict == "duplicate" and row is not None:
        return dict(row.get("result") or {}, duplicate=True)
    if verdict == "reconcile":
        store.set(key, state="unknown")
        raise NotesError(
            "outcome_unknown",
            "an earlier direct %s with this request_id may have been interrupted, so the "
            "note may or may not have been written.  Check the Coding folder, then send it again with a new "
            "request_id." % request["action"],
        )
    try:
        result = send()
    except NotesError as exc:
        if exc.code == "outcome_unknown":
            store.set(key, state="unknown")  # keep the row:  a retry with this id is refused
        else:
            store.drop(key)
        raise
    except BaseException:
        store.set(key, state="unknown")
        raise
    store.set(key, state="sent", id=0, result=result)
    return dict(result, duplicate=False)


def remember_direct_read(store: Any, request: Mapping[str, Any]) -> None:
    """A small row for a read or list this Mac answered directly (its action and done, never the note text or the
    titles), so notes_status answers for it here as it does through the Operator.  A failure here never fails the read."""
    key = "k:notes-read:%s" % request["request_id"]
    result = {"request_id": request["request_id"], "action": request["action"]}

    def change(rows: dict[str, Any], now: float) -> None:
        # One write, finished as it is made:  a read has nothing in flight to guard, so it needs no pending step.
        rows[key] = {
            "tool": "notes_read",
            "state": "sent",
            "id": 0,
            "result": result,
            "claimed_at": now,
            "ts": now,
            "expires": now + DIRECT_TTL,
        }

    try:
        store.mutate(change)
    except (OSError, ValueError):
        pass


def direct_status(store: Any, request_id: str) -> dict[str, Any] | None:
    """What this Mac's idempotency rows say about a request it ran directly:  None when it has no row."""
    row = store.row("k:notes-direct:%s" % request_id) or store.row("k:notes-read:%s" % request_id)
    if not isinstance(row, Mapping):
        return None
    state = row.get("state")
    if state == "sent":
        return dict(
            row.get("result") or {}, request_id=request_id, status="done", transport="direct"
        )
    if state == "unknown":
        return {"request_id": request_id, "status": "unknown", "transport": "direct"}
    return {"request_id": request_id, "status": "running", "transport": "direct"}


def new_request_id() -> str:
    return "nr" + uuid.uuid4().hex[:20]


def check_request_id(raw: Any) -> str:
    if not isinstance(raw, str) or not REQUEST_ID_RE.match(raw):
        raise NotesError(
            "invalid_argument", "argument 'request_id' must be 8 to 64 letters, digits, - or _"
        )
    return raw


# --------------------------------------------------------------------------------------------
# The Seat Tag in a title
# --------------------------------------------------------------------------------------------


def _notes_names(
    identity: str, registry_path: str | None, env: Mapping[str, str] | None
) -> set[str]:
    """Exact canonical display names from the registry, plus the compatible uppercase tag."""
    names = {identity}
    if identity in {"CODEX", "CLAUDE"}:
        names.add(identity.title())
    path = registry_path or I.seat_registry_path(env)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return names
    for row in data.get("seats") or []:
        if (
            isinstance(row, dict)
            and str(row.get("tag") or "").upper() == identity
            and isinstance(row.get("notesName"), str)
        ):
            names.add(row["notesName"].strip())
    return names


def title_agent(title: str) -> tuple[str, str] | None:
    """Return uppercase scope keys and the agent name from the closed title grammar."""
    if _TITLE_BAD_RE.search(title):
        return None
    match = _BRACKET_RE.fullmatch(title)
    return (match.group(1), match.group(2)) if match else None


def title_problem(
    title: str,
    identity: str,
    *,
    registry_path: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """None only when the title carries the identity's canonical display name or compatible uppercase seat tag."""
    parts = title_agent(title)
    if parts is None:
        return 'the title must read "[TEAM_KEY, Agent] Topic" with uppercase team keys and the canonical agent display name'
    if parts[1] not in _notes_names(identity, registry_path, env):
        return 'the title names agent "%s", which is not %s' % (parts[1][:40], identity)
    return None


# --------------------------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------------------------

REQUEST_FIELDS = frozenset(
    {
        "v",
        "action",
        "request_id",
        "identity",
        "title",
        "markdown",
        "flags",
        "session",
        "format",
        "query",
        "limit",
    }
)
_ACTION_FIELDS = {
    "create": frozenset({"title", "markdown", "flags", "session"}),
    "update": frozenset({"title", "markdown", "flags", "session"}),
    "pin": frozenset({"title"}),
    "unpin": frozenset({"title"}),
    "read": frozenset({"title", "format"}),
    "list": frozenset({"query", "limit"}),
}


def check_session(raw: Any) -> str:
    """The request's session id (AFC-361), or NotesError.  The id is not secret:  it goes on the script's command line."""
    try:
        return SID.check(raw, "the request's session")
    except ValueError as exc:
        raise NotesError("invalid_argument", str(exc)) from None


def make_request(
    action: str,
    identity: str,
    *,
    request_id: str,
    title: Any = None,
    markdown: Any = "",
    flags: Mapping[str, Any] | None = None,
    session: Any = None,
    fmt: Any = None,
    query: Any = None,
    limit: Any = None,
) -> dict[str, Any]:
    """The request object for one call, validated.  Only the fields its action uses are set:  `session` (AFC-361) is
    the writer's session id, stamped in the note so a headless resume can find the session, and is left out when None
    (an exempt single-thread identity) and for every action but create and update."""
    if action not in ACTIONS:
        raise NotesError("invalid_argument", "action must be one of %s" % ", ".join(ACTIONS))
    request: dict[str, Any] = {
        "v": REQUEST_VERSION,
        "action": action,
        "request_id": check_request_id(request_id),
        "identity": check_identity(identity),
    }
    if action != "list":
        request["title"] = check_title(title, action)
    if action in BODY_ACTIONS:
        if not isinstance(markdown, str):
            raise NotesError("invalid_argument", "the note body must be text")
        if len(markdown) > MARKDOWN_MAX:
            raise NotesError("invalid_argument", BODY_TOO_LONG % MARKDOWN_MAX)
        if WITHHELD in markdown:
            raise NotesError(
                "invalid_argument",
                "the body holds %s, so it came from a read that withheld a line that looks like a secret;  "
                "writing it back would erase that line from the note.  Edit the note on the Mac instead"
                % WITHHELD,
            )
        request["markdown"] = markdown
        request["flags"] = check_flags(flags)
        if session is not None:
            request["session"] = check_session(session)
    elif markdown or flags:
        raise NotesError("invalid_argument", "only create and update carry a body or flags")
    if action == "read":
        request["format"] = check_format(fmt)
    elif fmt is not None:
        raise NotesError("invalid_argument", "only read takes a format")
    if action == "list":
        request["query"] = check_query(query)
        request["limit"] = check_limit(limit)
    elif query is not None or limit is not None:
        raise NotesError("invalid_argument", "only list takes a query or a limit")
    return request


def check_request(raw: Any) -> dict[str, Any]:
    """A request object that arrived from elsewhere (JSON), validated for shape and for everything make_request
    enforces.  A field this version does not know, or one the action does not use, is refused."""
    if not isinstance(raw, Mapping):
        raise NotesError("invalid_argument", "the request must be a JSON object")
    extra = [key for key in raw if key not in REQUEST_FIELDS]
    if extra:
        raise NotesError("invalid_argument", "the request has a field this version does not know")
    if raw.get("v") != REQUEST_VERSION:
        raise NotesError("invalid_argument", "unsupported request version")
    action = raw.get("action")
    if not isinstance(action, str) or action not in ACTIONS:
        raise NotesError("invalid_argument", "action must be one of %s" % ", ".join(ACTIONS))
    unused = [
        key
        for key in raw
        if key not in {"v", "action", "request_id", "identity"}
        and key not in _ACTION_FIELDS[action]
    ]
    if unused:
        raise NotesError("invalid_argument", "the request has a field %s does not use" % action)
    return make_request(
        action,
        raw.get("identity"),
        request_id=raw.get("request_id"),
        title=raw.get("title"),
        markdown=raw.get("markdown", "") if action in BODY_ACTIONS else "",
        flags=raw.get("flags"),
        session=raw.get("session"),
        fmt=raw.get("format"),
        query=raw.get("query"),
        limit=raw.get("limit"),
    )


def request_problem(
    request: Mapping[str, Any],
    identity: str,
    *,
    env: Mapping[str, str] | None = None,
    registry_path: str | None = None,
) -> str | None:
    """None when `identity` may send this request:  it is the request's own identity, a create's title carries its
    agent name, and a create or update carries a session id unless the identity is a single-thread one."""
    if request.get("identity") != identity:
        return "the request names identity %s, not %s" % (
            str(request.get("identity"))[:32],
            identity,
        )
    if request.get("action") == "create":
        problem = title_problem(
            str(request.get("title") or ""), identity, registry_path=registry_path, env=env
        )
        if problem:
            return (
                "%s;  a new note's title carries the Agent of the identity that creates it (%s)"
                % (
                    problem,
                    identity,
                )
            )
    if (
        request.get("action") in BODY_ACTIONS
        and not request.get("session")
        and not SID.is_exempt(identity)
    ):
        return SID.refusal(identity, "relay")
    return None


def existing_only(
    request: Mapping[str, Any],
    *,
    env: Mapping[str, str] | None = None,
    registry_path: str | None = None,
) -> bool:
    """True for an update of a note whose title does not carry the requester's name:  it may change that note but must
    never create it (owner, Oct 22:  any seat may update any Coding note, and only a create makes a new one)."""
    if request.get("action") != "update":
        return False
    return (
        title_problem(
            str(request.get("title") or ""),
            str(request.get("identity") or ""),
            registry_path=registry_path,
            env=env,
        )
        is not None
    )


def request_digest(request: Mapping[str, Any]) -> str:
    """One request's fingerprint:  a request_id sent again with a different request is a conflict, not a repeat."""
    return hashlib.sha256(
        json.dumps(dict(request), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


# --------------------------------------------------------------------------------------------
# Provenance and the body a read returns
# --------------------------------------------------------------------------------------------

_FOOTER_TEXT = r"(?:Relayed for [^<\n]{1,80}? by the notes relay on|Written for [^<\n]{1,80}? through the Operator on)"
# The footer the way Notes stores it:  its own <div> (italic text, maybe a link), after an <hr> that Notes keeps as a
# <div><br><br></div> and any spacer rows, at the very end of the body.
_HTML_FOOTER_RE = re.compile(
    r"(?:\s*(?:<div>(?:\s*<br\s*/?>)+\s*</div>|<hr\s*/?>))*\s*"
    r"<(div|p)>(?:(?!<(?:div|p)>).){0,200}?" + _FOOTER_TEXT + r"(?:(?!<(?:div|p)>).){0,600}?</\1>"
    r"(?:\s*<div>(?:\s*<br\s*/?>)+\s*</div>)*\s*$",
    re.DOTALL | re.IGNORECASE,
)
_MD_FOOTER_RE = re.compile(r"\n*(?:-{3,}[ \t]*\n+)?\*" + _FOOTER_TEXT + r"[^\n]*\*\s*$")
_TEXT_FOOTER_RE = re.compile(r"(?:\n[ \t￼]*)*\n?" + _FOOTER_TEXT + r"[^\n]*\s*$")
_SPACER_TAIL_RE = re.compile(r"(?:\s*<div>(?:\s*<br\s*/?>)+\s*</div>)+\s*$", re.IGNORECASE)
_STAMP_RE = re.compile(
    r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun), (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d{1,2}, "
    r"\d{1,2}:\d{2}[ap]m(?:\s*[·|—-]\s*(?:PR\s*#?\s*[\d\s,#]+)+)?$",
    re.IGNORECASE,
)
_SESSION_ROW_RE = re.compile(r"^Session:\s*[A-Za-z0-9][A-Za-z0-9._:-]{3,127}(?:\s+-\s+.{0,40})?$")
_BANNER_PREFIXES = ("⚠️ NEEDS OWNER REVIEW", "📌 Mobile Quick View:")
_LEAD_BLOCK_RE = re.compile(
    r"\s*(<(div|p|h[1-6])\b[^>]*>(?:(?!<(?:div|p|h[1-6])\b).)*?</\2>)", re.DOTALL | re.IGNORECASE
)
_TAG_RE = re.compile(r"<[^>]+>")


TAIL_SCAN = (
    4096  # the footer and its spacers are a few hundred characters at the very end of a body
)
_FOOTER_P_RE = re.compile(
    r"(?:\s*<hr\s*/?>)?\s*<p>\s*<i>\s*" + _FOOTER_TEXT + r".{0,600}?</i>\s*</p>\s*$",
    re.DOTALL | re.IGNORECASE,
)


def _on_tail(text: str, *patterns: re.Pattern[str]) -> str:
    """`text` with each end-anchored pattern removed from its last TAIL_SCAN characters only.  Searched over a whole
    body, these patterns try every start position, and a body of many newlines (500,000 characters pass every check)
    then costs minutes of a runner's lease before the write even starts."""
    cut = max(0, len(text) - TAIL_SCAN)
    tail = text[cut:]
    for pattern in patterns:
        tail = pattern.sub("", tail)
    return text[:cut] + tail


def strip_provenance(body: str, is_html: bool) -> str:
    """The body without a trailing provenance line (the Operator's, or the old relay's):  an update replaces it rather
    than piling one on another."""
    if is_html:
        return _on_tail(body, _HTML_FOOTER_RE, _FOOTER_P_RE)
    return _on_tail(body, _MD_FOOTER_RE)


def with_provenance(
    request: Mapping[str, Any], requester: str, *, now: datetime | None = None
) -> dict[str, Any]:
    """The request with a last line saying who asked for the write (owner, Oct 18:  a note written on another host's
    behalf says so).  Apple Notes has no authorship, so a title like `[TEAM_KEY, Agent]` is only text.  Pin, unpin,
    read and list carry no body and are returned unchanged."""
    if request.get("action") not in BODY_ACTIONS:
        return dict(request)
    moment = (now or datetime.now(UTC)).astimezone(ZoneInfo("America/Chicago"))
    when = "%s %d, %d" % (
        moment.strftime("%a, %b"),
        moment.day,
        moment.year,
    )  # no %-d:  not portable
    line = "Written for %s through the Operator on %s (request %s)." % (
        requester,
        when,
        request["request_id"],
    )
    body = str(request.get("markdown") or "")
    if (request.get("flags") or {}).get("html"):
        footer = "<hr><p><i>%s</i></p>" % html.escape(line)
        return dict(request, markdown=strip_provenance(body, True) + footer)
    footer = "\n\n---\n\n*%s*" % re.sub(r"([\\*_\[\]`])", r"\\\1", line)
    return dict(request, markdown=recover_newlines(strip_provenance(body, False)) + footer)


def recover_newlines(text: str) -> str:
    """The Markdown recovery apple-notes-coding.sh applies to a body with few newlines, done here first.

    The script restores a body that arrived with literal `\\n` escapes, and splits a collapsed one-line body
    at its headings and bullets, but only while the body has (almost) no newlines.  The provenance footer adds
    newlines, which would switch that recovery off, so it is applied before the footer is appended."""
    if "\n" not in text and r"\n" in text:
        text = text.replace(r"\n", "\n")
    if text.count("\n") < 3:
        text = re.sub(r"(?<=\S)\s+(#{1,4}\s+)", r"\n\n\1", text)
        text = re.sub(r"(?<=\S)\s+([-\*]\s+)", r"\n\1", text)
    return text


def _block_text(block: str) -> str:
    return " ".join(html.unescape(_TAG_RE.sub("", block)).split())


def content_html(body: str, title: str) -> str:
    """A stored note's HTML without what the script writes on every save:  the title row, the date row, the Session
    row, the needs-owner and quick-view banners, one spacer, and the provenance line at the end.  What is left is the
    note's content, ready to send back with update and html true."""
    rest = body
    seen_title = seen_stamp = seen_session = seen_spacer = False
    for _ in range(10):
        match = _LEAD_BLOCK_RE.match(rest)
        if not match:
            break
        block = match.group(1)
        text = _block_text(block)
        header = not seen_spacer
        if header and not seen_title and not seen_stamp and text == " ".join(title.split()):
            seen_title = True
        elif header and not seen_stamp and _STAMP_RE.match(text):
            seen_stamp = True
        elif header and seen_stamp and not seen_session and _SESSION_ROW_RE.match(text):
            seen_session = True
        elif text.startswith(_BANNER_PREFIXES):
            pass  # the needs-owner and quick-view banners follow the spacer;  the flags write them again
        elif (
            header
            and (seen_title or seen_stamp)
            and not text
            and re.search(r"<br", block, re.IGNORECASE)
        ):
            seen_spacer = True  # the one spacer after the header rows
        else:
            break
        rest = rest[match.end() :]
    rest = _on_tail(strip_provenance(rest, True), _SPACER_TAIL_RE)
    return rest.strip("\n")


def content_text(plaintext: str, title: str) -> str:
    """content_html for the plain-text form."""
    lines = plaintext.replace("\r\n", "\n").split("\n")
    index = 0
    if index < len(lines) and " ".join(lines[index].split()) == " ".join(title.split()):
        index += 1
    if index < len(lines) and _STAMP_RE.match(lines[index].strip()):
        index += 1
        if index < len(lines) and _SESSION_ROW_RE.match(lines[index].strip()):
            index += 1
    if index < len(lines) and not lines[index].strip() and index > 0:
        index += 1
    while index < len(lines) and lines[index].strip().startswith(_BANNER_PREFIXES):
        index += 1
    text = _on_tail("\n".join(lines[index:]), _TEXT_FOOTER_RE)
    return text.rstrip()


WITHHELD = "[withheld: secret-shaped]"
#  A private key's header:  its body lines are base64 that no pattern matches on its own line, so the whole block goes.
_KEY_BEGIN_RE = re.compile(r"-----BEGIN ([A-Z ]*PRIVATE KEY)-----")


def withhold_secrets(body: str) -> tuple[str, int]:
    """`body` with every line the fleet secret scan flags replaced by WITHHELD (a `<div>` line keeps its `<div>`), and
    how many lines that was.  A private key block is withheld whole, from its BEGIN line through its END line (or to
    the end of the body when it has none), since its key lines match no pattern one at a time.  A note can hold a
    credential an agent once pasted into it;  a read must not carry it on to the Operator, a cloud seat or a model.
    make_request refuses a body that still holds the marker, so a read, edited and written back, can never erase the
    withheld line from the real note."""
    from . import secretscan  # lazy:  only a read needs it

    out: list[str] = []
    withheld = 0
    block_end: str | None = None  # the END line of the private key block this line is inside
    for line in body.split("\n"):
        inside = block_end is not None
        if inside:
            if block_end in line:
                block_end = None  # this line ends the block, and goes with it
        else:
            begin = _KEY_BEGIN_RE.search(line)
            if begin and "-----END %s-----" % begin.group(1) not in line[begin.end() :]:
                block_end = "-----END %s-----" % begin.group(1)
        if line and (inside or secretscan.scan(line)):
            withheld += 1
            line = (
                "<div>%s</div>" % WITHHELD if line.lstrip().lower().startswith("<div") else WITHHELD
            )
        out.append(line)
    return "\n".join(out), withheld


def cut_body(body: str, limit: int) -> str:
    """`body` cut to at most `limit` UTF-8 bytes (never inside a character), back to the end of its last whole line,
    so a withheld marker, or the line a secret was on, is never split.  A body with no line break is cut before any
    marker the cut would split."""
    data = body.encode("utf-8")
    if len(data) <= limit:
        return body
    cut = data[:limit].decode("utf-8", "ignore")
    end = cut.rfind("\n")
    if end >= 0:
        return cut[: end + 1]
    at = cut.rfind("[withheld")
    return cut[:at] if at >= 0 and WITHHELD not in cut[at:] else cut


def read_result(raw: Mapping[str, Any], fmt: str, *, limit: int = READ_MAX) -> dict[str, Any]:
    """The `note` a read answers, from the script's --read output, with any secret-shaped line withheld."""
    title = text_safe(str(raw.get("title") or ""))
    if fmt == "raw":
        body = text_safe(str(raw.get("body") or ""))
    elif fmt == "text":
        body = content_text(text_safe(str(raw.get("plaintext") or "")), title)
    else:
        body = content_html(text_safe(str(raw.get("body") or "")), title)
    # Cut first, so the scan covers only what is returned;  then withhold, and cut again in case a marker made a line
    # longer than the secret it replaced.  Each cut ends on a whole line, so a marker is never split.
    whole = len(body.encode("utf-8"))
    marked, _ = withhold_secrets(cut_body(body, limit))
    body = cut_body(marked, limit)
    truncated = whole > limit or body != marked
    withheld = body.count(
        WITHHELD
    )  # the markers the caller actually gets, never one the second cut dropped
    return {
        "title": title,
        "id": text_safe(str(raw.get("id") or "")),
        "created": text_safe(str(raw.get("created") or "")),
        "modified": text_safe(str(raw.get("modified") or "")),
        "format": fmt,
        "body": body,
        "bytes": len(body.encode("utf-8")),
        "truncated": truncated,
        "withheld": withheld,
    }


def list_result(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The answer of a list, from the script's --list output, with every row reduced to its four text fields and a
    title the secret scan flags shown as WITHHELD (a list goes to the Operator and the caller like a read does)."""
    from . import secretscan  # lazy, as in withhold_secrets

    notes = raw.get("notes")
    rows = []
    for row in notes if isinstance(notes, list) else []:
        if isinstance(row, Mapping):
            text = {
                key: text_safe(str(row.get(key) or ""))
                for key in ("title", "id", "created", "modified")
            }
            out = {key: value[:400] for key, value in text.items()}
            if secretscan.scan(text["title"]):  # the whole title, before it is cut
                out["title"] = WITHHELD
            rows.append(out)
    total = raw.get("total")
    return {
        "notes": rows,
        "total": total if isinstance(total, int) and not isinstance(total, bool) else len(rows),
        "truncated": bool(raw.get("truncated")),
    }


# --------------------------------------------------------------------------------------------
# The direct runner (the Mac)
# --------------------------------------------------------------------------------------------

Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]
_RUN_LOCK = (
    threading.Lock()
)  # one osascript write at a time:  Notes.app is a single shared document store
#  How long a request waits for that lock.  The lock is held for the whole write,  and the write
#  budget is SCRIPT_TIMEOUT_S,  so a wedged write would hold it for that long;  the wait has its
#  own bound,  and a request that cannot get the lock is told the notes are busy instead of
#  waiting the whole budget out (AFC-436).  The wait is counted inside SCRIPT_TIMEOUT_S:  the
#  script gets the cap minus this wait,  so a request never outlives the cap by the wait.
LOCK_WAIT_S = 60.0


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """Kill the helper's whole process group.

    The helper leads its own session,  so the group is the helper and everything it started.  A
    killed helper that leaves a stuck `shortcuts` child behind would keep the Shortcuts app busy
    long after the caller gave up and reported an unknown outcome (AFC-448).
    """
    try:
        pgid = os.getpgid(proc.pid)
    except OSError:
        pgid = None
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL)
            return
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:
        pass


def _run_helper(
    argv: list[str], *, runner: Runner | None = None, **kwargs: Any
) -> subprocess.CompletedProcess[bytes]:
    """Start the helper, and take its whole process group down when the cap is reached.

    The keyword arguments are the ones `subprocess.run` takes,  and a caller-supplied runner
    gets exactly those,  so a test that stands in for the helper keeps the same shape.
    """
    if runner is not None:
        return runner(argv, **kwargs)
    kwargs.pop("capture_output", None)
    kwargs.pop("check", None)
    stdin_data = kwargs.pop("input", b"") or b""
    timeout = kwargs.pop("timeout", None)
    env = kwargs.pop("env", None)
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env or {}),
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(input=stdin_data, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _kill_group(proc)
        try:
            #  POSIX:  a second communicate() returns everything the helper wrote,  not only what
            #  came after the timeout,  so it replaces what the timeout captured rather than
            #  adding to it (AFC-448)
            out2, err2 = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            out2, err2 = None, None
        if out2 is not None:
            exc.stdout = out2
        if err2 is not None:
            exc.stderr = err2
        raise
    except BaseException:
        #  Any other interruption,  a signal or a keyboard interrupt,  has to take the group with
        #  it:  the helper leads its own session,  so a signal aimed at the caller never reaches
        #  it,  and the helper and its children would be left running (AFC-448)
        _kill_group(proc)
        raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def script_candidates(env: Mapping[str, str], home: Path) -> list[Path]:
    """Where the script may be:  AGENT_SYNC_NOTES_SCRIPT, next to the package (scripts/), then the live copy."""
    found: list[Path] = []
    override = (env.get("AGENT_SYNC_NOTES_SCRIPT") or "").strip()
    if override:
        found.append(Path(override).expanduser())
    found.append(Path(__file__).resolve().parent.parent / SCRIPT_NAME)
    found.append(home / "apps" / SCRIPT_NAME)
    return found


def find_script(env: Mapping[str, str], home: Path) -> Path | None:
    for path in script_candidates(env, home):
        if path.is_file() and os.access(path, os.X_OK):
            return path
    return None


def direct_available(
    env: Mapping[str, str],
    home: Path,
    *,
    platform: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> Path | None:
    """The script's path when this host can run it:  macOS, osascript on PATH and the script found."""
    if (platform or sys.platform) != "darwin":
        return None
    if which("osascript") is None:
        return None
    return find_script(env, home)


def build_argv(
    script: Path,
    request: Mapping[str, Any],
    html_path: str | None,
    *,
    existing: bool = False,
    caller: str = "cli",
    requester: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    """The command line and the extra environment for one request.  The title, mode, flags and a list's query are the
    only text on the command line;  `html_path` is a file path, never the body."""
    action = request["action"]
    flags = request.get("flags") or {}
    argv = [str(script)]
    extra: dict[str, str] = {
        "AGENT_NOTES_CALLER": caller,
        "AGENT_NOTES_REQUEST_ID": str(request["request_id"]),
    }
    if requester:
        extra["AGENT_NOTES_REQUESTER"] = requester
    if action == "pin":
        argv += ["--pin-only"]
    elif action == "unpin":
        argv += ["--unpin-only"]
    elif action == "read":
        argv += ["--read"]
    elif action == "list":
        argv += ["--list", "--limit", str(request.get("limit") or LIST_DEFAULT)]
        if request.get("query"):
            argv += ["--query", str(request["query"])]
    else:
        if action == "update":
            argv += ["--update"]
            if existing:
                argv += ["--existing-only"]
        if flags.get("notify"):
            argv += ["--notify"]
        if flags.get("needs_owner"):
            argv += ["--needs-owner"]
        if flags.get("summary"):
            argv += ["--summary", flags["summary"]]
        if flags.get("pr"):
            argv += ["--pr", flags["pr"]]
        if html_path:
            argv += ["--html", html_path]
        if flags.get("pin") is False:
            extra["APPLE_NOTES_PIN"] = "0"
    if request.get("session") and action in BODY_ACTIONS:
        argv += [
            "--session",
            str(request["session"]),
        ]  # AFC-361:  not secret, so argv;  the script stamps the Session line
    if request.get("identity"):
        # The identity the request was verified for (the caller's own seat, or the requester a runner acts for):  the
        # script decides its single-thread exemption from this and from nothing the caller typed.  CHILD_ENV_KEYS strips
        # AGENT_SEAT.
        extra["AGENT_SEAT"] = str(request["identity"])
    if action != "list":
        argv += ["--title", request["title"]]
    return argv, extra


def child_env(env: Mapping[str, str], extra: Mapping[str, str]) -> dict[str, str]:
    out = {key: env[key] for key in CHILD_ENV_KEYS if env.get(key)}
    out.update(extra)
    return out


PIN_SKIPPED_RE = re.compile(r"^pin skipped:\s*(.+?)\s*$", re.MULTILINE)


def _pin_skipped(raw_stderr: bytes | None) -> str | None:
    """The reason line of the helper's `pin skipped: ...` warning, or None.

    The helper has no GUI fallback (owner 2026-10-20):  when the Shortcut does not pin, a create or an
    update still exits 0 with the note saved unpinned and says so on stderr.  A caller that only read the
    exit code would report a pinned note, so the warning is carried out in the result (AFC-454)."""
    found = PIN_SKIPPED_RE.search((raw_stderr or b"").decode("utf-8", "replace"))
    return " ".join(found.group(1).split())[:200] if found else None


def _note_id(stdout: str) -> str | None:
    match = re.search(r"\bid=(\S+)", stdout)
    return match.group(1)[:200] if match else None


def run_direct(
    request: Mapping[str, Any],
    script: Path,
    *,
    env: Mapping[str, str],
    runner: Runner | None = None,
    timeout: float | None = None,
    caller: str = "cli",
    requester: str | None = None,
    existing: bool | None = None,
) -> dict[str, Any]:
    """Run the script for `request`.  A write answers {"status": "done", "note_id": ...}, plus "pin_skipped":
    "<reason>" when the note was written but its pin was skipped;  a read answers {"note": {...}} and a list
    {"notes": [...], "total": n, "truncated": bool}.  Anything else raises NotesError.

    `requester` is the seat a runner writes for:  a create or update then carries the provenance line.  `existing`
    defaults to existing_only(request).  The body goes on stdin (markdown) or in a mode-600 temporary file (HTML),
    never on the command line.  The timeout is the budget for the whole request:  a write's script gets it minus the
    wait for the run lock."""
    action = request["action"]
    if existing is None:
        existing = existing_only(request, env=env)
    if action in BODY_ACTIONS:
        is_html = bool((request.get("flags") or {}).get("html"))
        if requester:
            request = with_provenance(request, requester)
        else:
            request = dict(
                request, markdown=strip_provenance(str(request.get("markdown") or ""), is_html)
            )
    flags = request.get("flags") or {}
    markdown = str(request.get("markdown") or "")
    if len(markdown) > MARKDOWN_MAX + 1000:  # the provenance line may add a little to a full body
        raise NotesError("invalid_argument", BODY_TOO_LONG % MARKDOWN_MAX)
    writes = action in WRITE_ACTIONS
    budget = (SCRIPT_TIMEOUT_S if writes else READ_TIMEOUT_S) if timeout is None else timeout
    html_file: str | None = None
    stdin_data: bytes | None = None
    try:
        if action in BODY_ACTIONS:
            if flags.get("html"):
                fd, html_file = tempfile.mkstemp(prefix="agent-sync-note-", suffix=".html")
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as out:
                    out.write(markdown.encode("utf-8"))
                stdin_data = b""
            else:
                stdin_data = markdown.encode("utf-8")
        argv, extra = build_argv(
            script, request, html_file, existing=existing, caller=caller, requester=requester
        )
        #  The lock wait is counted inside the cap:  the script gets the cap minus the wait,  so
        #  the whole request (the wait and the run) stays inside it.  A request that could wait
        #  the lock out and then take the full cap would outlive the budget the timeout test
        #  holds together (AFC-436).  A read takes no lock:  it changes nothing.
        child_timeout = max(1.0, budget - LOCK_WAIT_S) if writes else budget
        try:
            if writes and not _RUN_LOCK.acquire(timeout=LOCK_WAIT_S):
                raise NotesError(
                    "unavailable",
                    "another notes write is still running, so this one was refused as busy; "
                    "try again in a moment",
                )
            try:
                proc = _run_helper(
                    argv,
                    runner=runner,
                    input=stdin_data if stdin_data is not None else b"",
                    capture_output=True,
                    timeout=child_timeout,
                    env=child_env(env, extra),
                    check=False,
                )
            finally:
                if writes:
                    _RUN_LOCK.release()
        except subprocess.TimeoutExpired:
            if not writes:
                raise NotesError(
                    "unavailable",
                    "the notes script did not answer within %d seconds" % int(child_timeout),
                ) from None
            # A timeout does not prove nothing was written (the script may have created the note and then hung),
            # so it is an unknown outcome, never a safe failure that a retry may repeat.
            raise NotesError(
                "outcome_unknown",
                "the notes script did not finish within %d seconds, so the note may or may not "
                "have been written.  Check the Coding folder before sending it again."
                % int(child_timeout),
            ) from None
        except OSError as exc:
            raise NotesError(
                "unavailable",
                "could not start the notes script (%s)" % (exc.strerror or type(exc).__name__),
            ) from None
    finally:
        if html_file:
            try:
                os.unlink(html_file)
            except OSError:
                pass
    code = proc.returncode
    stdout = (proc.stdout or b"").decode("utf-8", "replace")
    stderr = " ".join((proc.stderr or b"").decode("utf-8", "replace").split())[:200]
    if code == 0:
        if action in ("read", "list"):
            try:
                raw = json.loads(stdout)
            except ValueError:
                raise NotesError(
                    "internal", "the notes script's %s answer did not parse" % action
                ) from None
            if not isinstance(raw, Mapping):
                raise NotesError(
                    "internal", "the notes script's %s answer is not an object" % action
                )
            if action == "read":
                return {"note": read_result(raw, str(request.get("format") or "html"))}
            return list_result(raw)
        result: dict[str, Any] = {"status": "done"}
        note_id = _note_id(stdout)
        if note_id:
            result["note_id"] = note_id
        skipped = _pin_skipped(proc.stderr)
        if skipped:
            result["pin_skipped"] = skipped
        return result
    if code == EXIT_USAGE:
        raise NotesError(
            "invalid_argument",
            "the notes script refused the request:  %s" % (stderr or "bad arguments"),
        )
    if code == EXIT_NOT_FOUND:
        if existing:
            raise NotesError(
                "not_found",
                "no note with that title in the Coding folder;  an update creates a note only when its title "
                "carries your own agent name (use create for a new note)",
            )
        raise NotesError("not_found", "no note with that title in the Coding folder")
    if code == EXIT_PIN_FAILED:
        #  The helper has no GUI fallback (owner 2026-10-20):  when the Shortcut does not pin or unpin,
        #  the note is left as it was and this is the whole story.  A create or update never gets here,
        #  because it exits 0 with a stderr warning when only its pin was skipped.
        if action == "pin":
            raise NotesError(
                "unavailable",
                "the note could not be pinned:  the Pin Coding Note shortcut is missing, failed or busy, "
                "and the helper never falls back to the Notes window",
            )
        raise NotesError(
            "unavailable",
            "the note could not be unpinned:  the Unpin Coding Note shortcut is missing, failed or busy",
        )
    detail = "the notes script exited with code %d%s" % (code, (":  " + stderr) if stderr else "")
    if action in ("create", "update"):
        # An exit the script does not plan for is an osascript error (say an AppleEvent timeout after `make new
        # note` was sent), and Notes can still finish the note:  never a plain refusal a retry could write twice.
        raise NotesError(
            "outcome_unknown",
            "%s, so the note may or may not have been written.  Check the Coding folder before sending it again."
            % detail,
        )
    raise NotesError("internal", detail)


# --------------------------------------------------------------------------------------------
# The action log (written by apple-notes-coding.sh, read here)
# --------------------------------------------------------------------------------------------

ACTION_LOG_REL = Path("Library") / "Logs" / "fleet" / "apple-notes-actions.jsonl"
ACTION_LOG_ROTATIONS = 5
LOG_FIELDS = (
    "v",
    "id",
    "ts",
    "host",
    "mode",
    "title",
    "note_id",
    "exit",
    "outcome",
    "duration_ms",
    "seat",
    "session",
    "caller",
    "request_id",
    "requester",
    "input_bytes",
    "body_bytes",
    "body_sha256",
    "reformatted",
    "pin",
    "flags",
    "count",
    "bytes",
    "received_at",
)
LOG_LINE_MAX = 4096
LOG_SINCE_MAX_HOURS = 720


def action_log_path(env: Mapping[str, str], home: Path) -> Path | None:
    """Where the script writes its action log on this host, or None when it is switched off."""
    override = (env.get("APPLE_NOTES_ACTION_LOG") or "").strip()
    if override.lower() == "off":
        return None
    return Path(override).expanduser() if override else home / ACTION_LOG_REL


def clean_log_entry(raw: Any) -> dict[str, Any] | None:
    """One log line reduced to the known fields, each a short scalar (or the two small objects), or None when it is not
    a log line at all (an empty id included:  the id is what the Operator's duplicate check and the runner's cursor go
    by).  The log holds no note text, and nothing that arrives through it can add any."""
    if (
        not isinstance(raw, Mapping)
        or not isinstance(raw.get("id"), str)
        or not raw["id"]
        or not isinstance(raw.get("ts"), str)
    ):
        return None
    from . import (
        secretscan,
    )  # lazy, as in withhold_secrets:  the log goes to the Operator and every seat

    out: dict[str, Any] = {}
    for key in LOG_FIELDS:
        if key not in raw:
            continue
        value = raw[key]
        if key in ("reformatted", "flags"):
            if isinstance(value, Mapping):
                cleaned: dict[str, Any] = {}
                for k, v in list(value.items())[:12]:
                    ck = text_safe(str(k))[:40]
                    if isinstance(v, (bool, int)) or v is None:
                        cleaned[ck] = v
                    elif isinstance(v, Mapping):
                        #  One level of nesting, for the per-kind counts.  A flat dict of small ints
                        #  stays a dict, so nothing is cut off.  Anything deeper is still text.
                        cleaned[ck] = {
                            text_safe(str(k2))[:40]: v2
                            if isinstance(v2, (bool, int)) or v2 is None
                            else text_safe(str(v2))[:80]
                            for k2, v2 in list(v.items())[:12]
                        }
                    else:
                        cleaned[ck] = text_safe(str(v))[:80]
                out[key] = cleaned
            continue
        if key in LOG_COUNTS:
            # A count:  a whole number, else null (a bool, text, or JSON's NaN and Infinity are no count).
            finite = isinstance(value, int) or (isinstance(value, float) and math.isfinite(value))
            out[key] = int(value) if finite and not isinstance(value, bool) else None
        elif isinstance(value, str):
            text = text_safe(value)
            if key == "title" and secretscan.scan(text):  # the whole title, before it is cut
                out[key] = WITHHELD
            else:
                out[key] = text[: 400 if key == "title" else 200]
        elif isinstance(value, bool) or value is None:
            out[key] = value
        # Anything else (a list, an object, a number where text belongs) is no value of this field:  dropped.
    return out


# The log fields that hold a count.  Every other field holds text, a flag or null.
LOG_COUNTS = frozenset({"v", "exit", "duration_ms", "input_bytes", "body_bytes", "count", "bytes"})


def text_safe(value: str) -> str:
    """`value` with anything UTF-8 cannot hold (a lone surrogate, which JSON can spell) replaced, so it can always be
    written to a file and sent on."""
    return value.encode("utf-8", "replace").decode("utf-8")


def _entry_time(entry: Mapping[str, Any]) -> datetime | None:
    try:
        moment = datetime.fromisoformat(str(entry.get("ts") or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def log_filter(
    entries: Iterable[Mapping[str, Any]],
    *,
    since_hours: int = 24,
    limit: int = 50,
    seat: str | None = None,
    action: str | None = None,
    title: str | None = None,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """The entries that match, newest first, at most `limit`."""
    floor = (now or datetime.now(UTC)) - timedelta(hours=since_hours)
    wanted_seat = seat.upper() if seat else None
    needle = title.casefold() if title else None
    picked: list[tuple[datetime, dict[str, Any]]] = []
    for entry in entries:
        moment = _entry_time(entry)
        if moment is None or moment < floor:
            continue
        if wanted_seat and str(entry.get("seat") or "").upper() != wanted_seat:
            continue
        if action and entry.get("mode") != action:
            continue
        if needle and needle not in str(entry.get("title") or "").casefold():
            continue
        picked.append((moment, dict(entry)))
    picked.sort(key=lambda item: item[0], reverse=True)
    return [entry for _, entry in picked[:limit]]


def iter_log_files(path: Path, rotations: int = ACTION_LOG_ROTATIONS) -> Iterable[dict[str, Any]]:
    """Every entry of a log and its rotated copies (`.1` to `.<rotations>`), oldest file first.  A line that does not
    parse is passed over:  the log is a record, and one bad line must not hide the rest."""
    for index in range(rotations, -1, -1):
        candidate = path if index == 0 else path.with_name("%s.%d" % (path.name, index))
        try:
            with open(candidate, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if len(line) > LOG_LINE_MAX * 4:
                        continue
                    try:
                        entry = clean_log_entry(json.loads(line))
                    except ValueError:
                        continue
                    if entry is not None:
                        yield entry
        except OSError:
            continue


def read_action_log(env: Mapping[str, str], home: Path, **filters: Any) -> list[dict[str, Any]]:
    """This host's action log, filtered (log_filter's arguments)."""
    path = action_log_path(env, home)
    if path is None:
        return []
    return log_filter(iter_log_files(path), **filters)


def check_log_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """The filters of a log call, validated (the notes_log tool's arguments)."""
    since = args.get("since_hours", 24)
    if (
        isinstance(since, bool)
        or not isinstance(since, int)
        or not 1 <= since <= LOG_SINCE_MAX_HOURS
    ):
        raise NotesError(
            "invalid_argument",
            "argument 'since_hours' must be an integer from 1 to %d" % LOG_SINCE_MAX_HOURS,
        )
    limit = check_limit(args.get("limit"), default=50, maximum=500)
    seat = args.get("by_seat")
    if seat is not None and (not isinstance(seat, str) or not SEAT_RE.fullmatch(seat.upper())):
        raise NotesError(
            "invalid_argument", "argument 'by_seat' must be a seat name such as CLAUDE"
        )
    if seat is not None:
        from . import (
            secretscan,
        )  # lazy, as in withhold_secrets:  the filter is sent to the Operator

        if secretscan.scan(seat):  # the seat pattern still fits a key's shape (AKIA and 16 more)
            raise NotesError(
                "invalid_argument",
                "argument 'by_seat' looks like it contains a secret;  nothing was sent",
            )
    action = args.get("action")
    if action is not None and action not in ACTIONS:
        raise NotesError(
            "invalid_argument", "argument 'action' must be one of %s" % ", ".join(ACTIONS)
        )
    title = check_query(args.get("title"), "title") or None
    return {
        "since_hours": since,
        "limit": limit,
        "seat": seat.upper() if isinstance(seat, str) else None,
        "action": action,
        "title": title,
    }


def iso_now(clock: Callable[[], float] = time.time) -> str:
    return datetime.fromtimestamp(clock(), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
