"""Apple Notes from any transport (AFC-331, docs/protocols/per-call-identity.md section 5).

Notes live in the Mac's iCloud `Coding` folder and only the Mac can write them, through
scripts/apple-notes-coding.sh.  This module is the shared half of the two ways a request gets there:

  direct   the Mac runs the script itself (`run_direct`).  The title, flags and mode go on the command
           line;  the body NEVER does:  markdown goes in on standard input and HTML through a mode-600
           temporary file named by --html, so no note text appears in a process listing.
  relay    everywhere else posts an envelope to `#notes-relay` as the claimed identity, topic = the note
           title (cut to Zulip's 60 characters, the full title is inside the envelope).  The Mac
           listener (notes_relay.py) validates it and runs the same direct path.

An envelope is one fenced JSON block in a message:

    ```json
    {"v":1,"action":"create","title":"[AFC, Claude] topic","markdown":"...","flags":{"pin":true},
     "request_id":"nr0123456789abcdef0123","identity":"CLAUDE"}
    ```

A create or update also carries `"session":"<id>"` (AFC-361, session_ids.py):  the writer's session id, which the script
stamps in the note as a `Session:` line so a headless resume can find the session.  It is left out for the single-thread
identities that need none.

A body too large for one message goes up as an attached `.md` file and the envelope names it
(`markdown_url`, `markdown_bytes`, `markdown_sha256`, `markdown` empty).

What reaches the script is checked here, whoever sent it:  the title must start `[` and hold no quote,
backslash or control character (the script puts it inside AppleScript source on its pin and notify
paths), flags are a closed set, and a summary follows the title's character rule.  The listener adds the
sender rules (a fleet agent bot, `identity` equal to the sender, the `Agent` in the title that sender's
own name).

Python 3.11+, standard library only.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import identity as I
from . import session_ids as SID

RELAY_CHANNEL = "notes-relay"
ENVELOPE_VERSION = 1
ACTIONS = ("create", "update", "pin", "unpin")
TOPIC_MAX = 60  # Zulip's topic limit, in characters
TITLE_MAX = 200
SUMMARY_MAX = 280
PR_MAX = 40
MARKDOWN_MAX = 500_000  # characters a tool accepts;  an upload carries at most UPLOAD_MAX bytes
BODY_TOO_LONG = "the note body is longer than %d characters;  nothing was written"
UPLOAD_MAX = 1024 * 1024
INLINE_JSON_MAX = 8500  # an envelope longer than this (as JSON) travels as an uploaded .md file
ENVELOPE_PARSE_MAX = 20_000  # the longest message body the parser looks at
SCRIPT_NAME = "apple-notes-coding.sh"
#  The helper wraps its Notes calls in a 900 s AppleEvent timeout (NOTES_APPLE_EVENT_TIMEOUT_S in
#  scripts/apple-notes-coding.sh),  because a body the size of the master list takes longer than
#  AppleScript's two-minute default.  This cap has to sit above that timeout:  a cap below it
#  kills the write while osascript is still working,  and the later completion creates a second
#  note.  It has to sit above the pin phase that follows the write as well (about 100 s:  the
#  Shortcut runs are bounded by NOTES_SHORTCUT_TIMEOUT_S):  a cap that cuts the pin phase short
#  reports outcome_unknown for a note that was written,  and the request id is then refused.
#  scripts/test_apple_notes_timeout.py holds the two together (AFC-436).
SCRIPT_TIMEOUT_S = 1080.0
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")
UPLOAD_PATH_RE = re.compile(r"^/user_uploads/[A-Za-z0-9_./-]{1,200}$")
FLAG_NAMES = ("pin", "notify", "needs_owner", "summary", "pr", "html")
_TITLE_BAD_RE = re.compile(r'["\\\x00-\x1f\x7f\x85\u2028\u2029]')
_PR_RE = re.compile(r"^[0-9#, ]{1,40}$")
_BRACKET_RE = re.compile(r"^\[([^\]]+)\] \S")
_APPS_RE = re.compile(r"^[A-Z0-9]+(?:, [A-Z0-9]+)*$")
_FENCE_OPEN = "```json\n"
_FENCE_CLOSE = "\n```"

# Script exit codes (scripts/apple-notes-coding.sh header).
EXIT_USAGE, EXIT_NOT_FOUND, EXIT_UNPIN = 2, 3, 4
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
    #  caller drives the script under a lock and a 120 s timeout (AFC-431).
    "APPLE_NOTES_PIN_RETRY",
)

# Reactions the listener sets and `notes_status` reads (Zulip unicode codes and the names tried to set them).
DONE_EMOJI = ("check", "white_check_mark")
REFUSED_EMOJI = ("cross_mark", "x")
DONE_CODES = frozenset({"2705"})
REFUSED_CODES = frozenset({"274c"})
DONE_NAMES = frozenset({"check", "white_check_mark", "check_mark_button"})
REFUSED_NAMES = frozenset({"cross_mark", "x", "negative_squared_cross_mark"})
REFUSAL_PREFIX = "notes-relay refused "


def refusal_label(request_id: str) -> str:
    """The exact start of a refusal reply:  the prefix, the label and the colon that ends it.  Matching this and not
    the bare prefix keeps request `abcdefgh` from matching the reply for `abcdefgh2`."""
    return "%s%s:" % (REFUSAL_PREFIX, request_id)


class NotesError(Exception):
    """A refusal with a stable code (invalid_argument, not_found, unavailable, outcome_unknown, internal) and a
    message that never holds the note's body.  `outcome_unknown` means the script may have written the note."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------------------------
# Titles, flags, request ids
# --------------------------------------------------------------------------------------------


def check_title(raw: Any) -> str:
    """The title as the script will see it, or NotesError.  It must start `[`, so a title can never read
    as a flag or an empty note name, and holds no character that is special inside AppleScript source."""
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
    if not title.startswith("["):
        raise NotesError(
            "invalid_argument",
            "argument 'title' must start with [APP, Agent], such as \"[AFC, Claude] topic\"",
        )
    if _TITLE_BAD_RE.search(title):
        raise NotesError(
            "invalid_argument",
            "argument 'title' must not hold a double quote, a backslash or a control character",
        )
    return title


def check_flags(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """A closed set of flags, each of its own type.  `pin` defaults to true (the script's own default);
    `activate` is not a flag at all:  a relay request must never steal the owner's focus."""
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
            out[name] = flags[name]
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


def text_fields(envelope: Mapping[str, Any]) -> dict[str, str]:
    """Every caller-written text of an envelope, for the secret scan:  title, body and the text flags.  The
    summary reaches the script on its command line (--summary has no other channel in the script) and the
    owner's notification, so the scan is its guard."""
    flags = envelope.get("flags") or {}
    fields = {
        "title": str(envelope.get("title") or ""),
        "markdown": str(envelope.get("markdown") or ""),
    }
    for name in ("summary", "pr"):
        if flags.get(name):
            fields[name] = str(flags[name])
    return {name: value for name, value in fields.items() if value}


DIRECT_TTL = 24 * 3600.0


def direct_once(
    store: Any, envelope: Mapping[str, Any], send: Callable[[], dict[str, Any]]
) -> dict[str, Any]:
    """Run `send` (the direct runner) once per request_id, under the persistent idempotency rows every stdio
    writer of this seat shares (idem.IdemStore).  A repeat of a finished request returns its first result with
    duplicate true.  A request that may have written (a timeout, a crash) is never repeated:  it is refused as
    outcome_unknown until the owner checks the note and sends it again under a new request_id.  A failure that
    provably wrote nothing drops its row, so the same id may be tried again."""
    key = "k:notes-direct:%s" % envelope["request_id"]
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
            "request_id." % envelope["action"],
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


def new_request_id() -> str:
    return "nr" + uuid.uuid4().hex[:20]


def check_request_id(raw: Any) -> str:
    if not isinstance(raw, str) or not REQUEST_ID_RE.match(raw):
        raise NotesError(
            "invalid_argument", "argument 'request_id' must be 8 to 64 letters, digits, - or _"
        )
    return raw


def topic_for(title: str) -> str:
    """The Zulip topic for a note title:  the title cut to 60 characters (the envelope keeps it whole)."""
    one_line = " ".join(title.split())
    return one_line if len(one_line) <= TOPIC_MAX else one_line[: TOPIC_MAX - 1].rstrip() + "…"


# --------------------------------------------------------------------------------------------
# The relay consumer's seat (a fleet knob)
# --------------------------------------------------------------------------------------------

CONSUMER_KNOB = "NOTES_RELAY_CONSUMER"
DEFAULT_CONSUMER = "CLAUDE"
# Tests replace this with a callable (local_seat, refresh) -> (seat, label, local_ignored);  None means the real knob.
KNOB_RESOLVER: Callable[[str | None, bool], tuple[str, str, bool]] | None = None


def _knob_resolver(local: str | None, refresh: bool) -> tuple[str, str, bool]:
    from fleet_knobs import knobs  # scripts/fleet_knobs:  absent on a host that does not ship it

    if refresh:
        knobs.refresh()
    knob = knobs.resolve(CONSUMER_KNOB, DEFAULT_CONSUMER, "str", local=local)
    if knob.source == "infisical":
        label = "infisical"
    elif knob.source == "lkg":
        label = "last-known-good copy (stale)"
    else:
        label = "default %s (%s)" % (DEFAULT_CONSUMER, knob.reason or "unset")
    return str(knob.value), label, bool(knob.local_ignored)


def relay_consumer(
    local_seat: str | None = None, *, refresh: bool = False
) -> tuple[str, str, bool]:
    """(seat, label, local_ignored):  the seat whose bot is the Notes relay consumer, from the NOTES_RELAY_CONSUMER
    fleet knob (Infisical, through scripts/fleet_knobs).  `local_seat` is the listener.toml `[notes_relay] seat`:
    a str knob cannot be narrowed, so a local seat that differs is ignored (`local_ignored`) and Infisical's wins.
    When Infisical is unreachable with no last-known-good copy, or fleet_knobs is not on this host, the answer is
    the default, CLAUDE, and the label says so (`default CLAUDE (unreachable:...)`)."""
    try:
        return (KNOB_RESOLVER or _knob_resolver)(local_seat, refresh)
    except Exception:  # noqa: BLE001 - a read of a non-secret knob must never stop a tool
        return DEFAULT_CONSUMER, "default %s (fleet_knobs unavailable)" % DEFAULT_CONSUMER, False


# --------------------------------------------------------------------------------------------
# The Agent in a title
# --------------------------------------------------------------------------------------------


def _notes_names(
    identity: str, registry_path: str | None, env: Mapping[str, str] | None
) -> set[str]:
    """Every spelling of `identity`'s Agent name a title may carry, case folded:  its fleet-apps.json
    `notesName` (Claude, Grok Web, Muse Assist), the tag itself (CLAUDE, GROK-WEB, BF-DEPLOYER, GB-FIXER)
    and the tag with spaces for hyphens."""
    names = {identity.casefold(), identity.replace("-", " ").casefold()}
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
            names.add(row["notesName"].strip().casefold())
    return names


def title_agent(title: str) -> tuple[str, str] | None:
    """(apps, agent) from `[APP, APP2, Agent] topic`, or None when the title has no such shape.  The
    apps are the capitalised acronyms before the last comma, the agent is what follows it."""
    match = _BRACKET_RE.match(title)
    if not match:
        return None
    inner = match.group(1)
    if ", " not in inner:
        return None
    apps, agent = inner.rsplit(", ", 1)
    if not _APPS_RE.match(apps) or not agent.strip():
        return None
    return apps, agent.strip()


def title_problem(
    title: str,
    identity: str,
    *,
    registry_path: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """None when the title's Agent is `identity`'s own name, else why not."""
    parts = title_agent(title)
    if parts is None:
        return 'the title must read "[APP, Agent] topic" with the app acronyms in capitals'
    if parts[1].casefold() not in _notes_names(identity, registry_path, env):
        return 'the title names agent "%s", which is not %s' % (parts[1][:40], identity)
    return None


# --------------------------------------------------------------------------------------------
# Envelopes
# --------------------------------------------------------------------------------------------


def check_session(raw: Any) -> str:
    """The envelope's session id (AFC-361), or NotesError.  The id is not secret:  it goes on the script's command line."""
    try:
        return SID.check(raw, "the envelope's session")
    except ValueError as exc:
        raise NotesError("invalid_argument", str(exc)) from None


def make_envelope(
    action: str,
    title: str,
    markdown: str,
    flags: Mapping[str, Any],
    request_id: str,
    identity: str,
    session: str | None = None,
) -> dict[str, Any]:
    """The envelope for one request.  `session` (AFC-361) is the writer's session id, stamped in the note so a
    headless resume can find the session;  it is left out when None (an exempt single-thread identity, or a pin)."""
    if action not in ACTIONS:
        raise NotesError("invalid_argument", "action must be one of %s" % ", ".join(ACTIONS))
    if isinstance(markdown, str) and len(markdown) > MARKDOWN_MAX:
        raise NotesError("invalid_argument", BODY_TOO_LONG % MARKDOWN_MAX)
    envelope = {
        "v": ENVELOPE_VERSION,
        "action": action,
        "title": check_title(title),
        "markdown": markdown,
        "flags": check_flags(flags),
        "request_id": check_request_id(request_id),
        "identity": identity,
    }
    if session is not None:
        envelope["session"] = check_session(session)
    return envelope


def envelope_json(envelope: Mapping[str, Any]) -> str:
    """One line of ASCII JSON, so the closing fence is always the only line break after the opening one."""
    return json.dumps(dict(envelope), ensure_ascii=True, separators=(",", ":"))


def render_message(envelope: Mapping[str, Any]) -> str:
    return _FENCE_OPEN + envelope_json(envelope) + _FENCE_CLOSE


def needs_upload(envelope: Mapping[str, Any]) -> bool:
    return len(envelope_json(envelope)) > INLINE_JSON_MAX


def with_upload(envelope: Mapping[str, Any], *, url: str, data: bytes) -> dict[str, Any]:
    """The envelope with its markdown replaced by a reference to the uploaded file."""
    if not UPLOAD_PATH_RE.match(url):
        raise NotesError("internal", "Zulip returned an upload path this relay does not accept")
    out = dict(envelope)
    out["markdown"] = ""
    out["markdown_url"] = url
    out["markdown_bytes"] = len(data)
    out["markdown_sha256"] = hashlib.sha256(data).hexdigest()
    return out


class EnvelopeError(Exception):
    """A message that is not a well-formed envelope.  The message names the rule, never the content."""


def parse_message(content: Any) -> dict[str, Any]:
    """The envelope inside a relay message body (a tag line may come before the fence), validated for shape
    and for everything check_title and check_flags enforce.  Raises EnvelopeError."""
    if not isinstance(content, str) or not content:
        raise EnvelopeError("the message is empty")
    if len(content) > ENVELOPE_PARSE_MAX:
        raise EnvelopeError("the message is too long to be an envelope")
    start = content.find(_FENCE_OPEN)
    end = content.rfind(_FENCE_CLOSE)
    if start < 0 or end <= start:
        raise EnvelopeError("the message holds no ```json fence")
    try:
        raw = json.loads(content[start + len(_FENCE_OPEN) : end])
    except (ValueError, RecursionError):
        raise EnvelopeError("the fenced JSON does not parse") from None
    if not isinstance(raw, dict):
        raise EnvelopeError("the fenced JSON is not an object")
    allowed = {
        "v",
        "action",
        "title",
        "markdown",
        "flags",
        "request_id",
        "identity",
        "markdown_url",
        "markdown_bytes",
        "markdown_sha256",
        "session",
    }
    extra = [key for key in raw if key not in allowed]
    if extra:
        raise EnvelopeError("the envelope has a field this relay does not know")
    if raw.get("v") != ENVELOPE_VERSION:
        raise EnvelopeError("unsupported envelope version")
    try:
        identity = raw.get("identity")
        if not isinstance(identity, str) or not identity:
            raise NotesError("invalid_argument", "the envelope names no identity")
        markdown = raw.get("markdown", "")
        if not isinstance(markdown, str) or len(markdown) > MARKDOWN_MAX:
            raise NotesError(
                "invalid_argument",
                "the envelope's markdown must be text of at most %d characters" % MARKDOWN_MAX,
            )
        envelope = make_envelope(
            str(raw.get("action")),
            raw.get("title"),
            markdown,
            raw.get("flags") or {},
            raw.get("request_id"),
            identity,
            raw.get("session"),
        )
        if "markdown_url" not in raw and ("markdown_bytes" in raw or "markdown_sha256" in raw):
            raise NotesError(
                "invalid_argument", "markdown_bytes and markdown_sha256 need a markdown_url"
            )
        if "markdown_url" in raw:
            url, size, digest = (
                raw.get("markdown_url"),
                raw.get("markdown_bytes"),
                raw.get("markdown_sha256"),
            )
            if not isinstance(url, str) or not UPLOAD_PATH_RE.match(url) or ".." in url:
                raise NotesError("invalid_argument", "markdown_url must be a /user_uploads/ path")
            if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= UPLOAD_MAX:
                raise NotesError("invalid_argument", "markdown_bytes must be 1 to %d" % UPLOAD_MAX)
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise NotesError("invalid_argument", "markdown_sha256 must be 64 hex characters")
            if markdown:
                raise NotesError(
                    "invalid_argument", "an envelope with markdown_url carries no inline markdown"
                )
            envelope.update(markdown_url=url, markdown_bytes=size, markdown_sha256=digest)
    except NotesError as exc:
        raise EnvelopeError(exc.message) from None
    return envelope


def resolve_upload(envelope: Mapping[str, Any], fetch: Callable[[str], bytes]) -> dict[str, Any]:
    """The envelope with its markdown filled in from the uploaded file (size and digest checked)."""
    if "markdown_url" not in envelope:
        return dict(envelope)
    data = fetch(str(envelope["markdown_url"]))
    if len(data) != envelope.get("markdown_bytes") or len(data) > UPLOAD_MAX:
        raise NotesError(
            "invalid_argument", "the attached markdown is not the size the envelope names"
        )
    if hashlib.sha256(data).hexdigest() != envelope.get("markdown_sha256"):
        raise NotesError(
            "invalid_argument", "the attached markdown does not match the envelope's digest"
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise NotesError("invalid_argument", "the attached markdown is not UTF-8") from None
    out = {key: value for key, value in envelope.items() if not key.startswith("markdown_")}
    out["markdown"] = text
    return out


# --------------------------------------------------------------------------------------------
# The direct runner (the Mac)
# --------------------------------------------------------------------------------------------

Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]
_RUN_LOCK = (
    threading.Lock()
)  # one osascript at a time:  Notes.app is a single shared document store
#  How long a request waits for that lock.  The lock is held for the whole write,  and the write
#  budget is SCRIPT_TIMEOUT_S,  so a wedged write would hold it for that long;  the wait has its
#  own bound,  and a request that cannot get the lock is told the notes are busy instead of
#  waiting the whole budget out (AFC-436).
LOCK_WAIT_S = 60.0


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
    script: Path, envelope: Mapping[str, Any], html_path: str | None
) -> tuple[list[str], dict[str, str]]:
    """The command line and the extra environment for one request.  The title, mode and flags are the only
    text on the command line;  `html_path` is a file path, never the body."""
    action = envelope["action"]
    flags = envelope.get("flags") or {}
    argv = [str(script)]
    extra: dict[str, str] = {}
    if action == "pin":
        argv += ["--pin-only"]
    elif action == "unpin":
        argv += ["--unpin-only"]
    else:
        if action == "update":
            argv += ["--update"]
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
    if envelope.get("session") and action in ("create", "update"):
        argv += [
            "--session",
            str(envelope["session"]),
        ]  # AFC-361:  not secret, so argv;  the script stamps the Session line
    if envelope.get("identity"):
        # The identity the request was verified for (the tool's seat, or the relay sender's bot):  the script decides
        # its single-thread exemption from this and from nothing the caller typed.  CHILD_ENV_KEYS strips AGENT_SEAT.
        extra["AGENT_SEAT"] = str(envelope["identity"])
    argv += ["--title", envelope["title"]]
    return argv, extra


def child_env(env: Mapping[str, str], extra: Mapping[str, str]) -> dict[str, str]:
    out = {key: env[key] for key in CHILD_ENV_KEYS if env.get(key)}
    out.update(extra)
    return out


def _note_id(stdout: str) -> str | None:
    match = re.search(r"\bid=(\S+)", stdout)
    return match.group(1)[:200] if match else None


PROVENANCE_SITE = "https://simplewithus.zulipchat.com"


def with_provenance(
    envelope: dict, identity: str, message_id: int, site: str = PROVENANCE_SITE
) -> dict:
    """The envelope with a footer saying who asked for the note and where (AFC-331, owner 2026-10-18).

    Apple Notes has no authorship, so a title like `[APP, Agent]` is only text.  The relay stamps every note it
    creates or updates with the requesting bot and a permalink to its #notes-relay message, so any relayed note can
    be checked against Zulip, and a note without the footer is visibly unverified.  Pin and unpin carry no body."""
    if envelope.get("action") not in ("create", "update"):
        return envelope
    url = "%s/#narrow/id/%d" % (site.rstrip("/"), int(message_id))
    now = datetime.now(ZoneInfo("America/Chicago"))
    when = "%s %d, %d" % (now.strftime("%a, %b"), now.day, now.year)  # no %-d:  not portable
    if (envelope.get("flags") or {}).get("html"):
        footer = (
            '<hr><p><i>Relayed for %s by the notes relay on %s:  <a href="%s">request message</a>.</i></p>'
            % (html.escape(identity), when, url)
        )
    else:
        footer = "\n\n---\n\n*Relayed for %s by the notes relay on %s:  [request message](%s).*" % (
            re.sub(r"([\\*_\[\]`])", r"\\\1", identity),
            when,
            url,
        )
        return dict(
            envelope, markdown=recover_newlines(str(envelope.get("markdown") or "")) + footer
        )
    return dict(envelope, markdown=str(envelope.get("markdown") or "") + footer)


def recover_newlines(text: str) -> str:
    """The Markdown recovery apple-notes-coding.sh applies to a body with few newlines, done here first.

    The script restores a body that arrived with literal `\\n` escapes, and splits a collapsed one-line body
    at its headings and bullets, but only while the body has (almost) no newlines.  The provenance footer adds
    newlines, which would switch that recovery off, so the relay applies it before appending the footer."""
    if "\n" not in text and r"\n" in text:
        text = text.replace(r"\n", "\n")
    if text.count("\n") < 3:
        text = re.sub(r"(?<=\S)\s+(#{1,4}\s+)", r"\n\n\1", text)
        text = re.sub(r"(?<=\S)\s+([-\*]\s+)", r"\n\1", text)
    return text


def run_direct(
    envelope: Mapping[str, Any],
    script: Path,
    *,
    env: Mapping[str, str],
    runner: Runner | None = None,
    timeout: float = SCRIPT_TIMEOUT_S,
) -> dict[str, Any]:
    """Run the script for `envelope` (markdown already inline).  Returns {"status": "done", "note_id": ...}
    or raises NotesError.  The body goes on stdin (markdown) or in a mode-600 temporary file (HTML);  never
    on the command line."""
    action = envelope["action"]
    flags = envelope.get("flags") or {}
    markdown = str(envelope.get("markdown") or "")
    if len(markdown) > MARKDOWN_MAX:
        raise NotesError("invalid_argument", BODY_TOO_LONG % MARKDOWN_MAX)
    html_file: str | None = None
    stdin_data: bytes | None = None
    try:
        if action in ("create", "update"):
            if flags.get("html"):
                fd, html_file = tempfile.mkstemp(prefix="agent-sync-note-", suffix=".html")
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as out:
                    out.write(markdown.encode("utf-8"))
                stdin_data = b""
            else:
                stdin_data = markdown.encode("utf-8")
        argv, extra = build_argv(script, envelope, html_file)
        try:
            if not _RUN_LOCK.acquire(timeout=LOCK_WAIT_S):
                raise NotesError(
                    "unavailable",
                    "another notes write is still running, so this one was refused as busy; "
                    "try again in a moment",
                )
            try:
                proc = (runner or subprocess.run)(
                    argv,
                    input=stdin_data if stdin_data is not None else b"",
                    capture_output=True,
                    timeout=timeout,
                    env=child_env(env, extra),
                    check=False,
                )
            finally:
                _RUN_LOCK.release()
        except subprocess.TimeoutExpired:
            # A timeout does not prove nothing was written (the script may have created the note and then hung),
            # so it is an unknown outcome, never a safe failure that a retry may repeat.
            raise NotesError(
                "outcome_unknown",
                "the notes script did not finish within %d seconds, so the note may or may not "
                "have been written.  Check the Coding folder before sending it again."
                % int(timeout),
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
        result: dict[str, Any] = {"status": "done"}
        note_id = _note_id(stdout)
        if note_id:
            result["note_id"] = note_id
        return result
    if code == EXIT_USAGE:
        raise NotesError(
            "invalid_argument",
            "the notes script refused the request:  %s" % (stderr or "bad arguments"),
        )
    if code == EXIT_NOT_FOUND:
        raise NotesError("not_found", "no note with that title in the Coding folder")
    if code == EXIT_UNPIN:
        raise NotesError(
            "unavailable",
            "the note could not be unpinned:  the Unpin Coding Note shortcut is missing or failed",
        )
    raise NotesError(
        "internal",
        "the notes script exited with code %d%s" % (code, (":  " + stderr) if stderr else ""),
    )
