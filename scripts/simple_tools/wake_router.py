"""The wake router every multithreaded platform shares (OPS-1048;  owner design, Nov 10, 2026).

The chain.  The listener captures a tagged message to the seat inbox and POSTs one signed
simple-tools-wake/1 body to the platform's router.  The router picks the chat deterministically,
then delivers and wakes it.  A platform supplies only how to wake one of its chats:  a `Platform`
that names its seat, its mention and its `deliver` function.

The pick.  An addressed letter;  then a fragment of a session id, at least 8 hex characters, that
identifies exactly one live chat;  then a Linear issue id from the topic or the excerpt, found in a
chat's registry `issues` (newest `seen_at` first) or, for a record with no `issues`, in its title;
then the topic's words in a chat's title;  then recency.  A tie within a step goes to the most
recent of the tied chats.  An optional decider (`Consult`, off by default) may choose before
recency.  Nobody is asked to clarify and an ambiguous wake is never held:  only `no-live-session`
and an unreadable registry do not deliver, and both are bounded retries, because a chat may register
or the registry come back before `max_attempts` or `max_age_seconds` runs out.  The receiving chat
is always told how it was linked (`Wake.link_note`), and for recency that the router is not sure the
wake is meant for it.

A reply link.  The listener may add `"linked_by": "reply"` when the message answers one a chat
posted, with the letter of that chat.  Any other value is ignored.  It changes no step of the pick:
when that letter picks the chat, the receipt says `picked: "reply"` and `reason: "reply"`, and the
chat is told it was linked by reply.

A session id may carry a `sess_` prefix, in the registry or in the text.  It is not part of the
fragment:  a fragment is compared with the id's hex alone.

The sender is never the target.  A fleet post header carries the sender's own fragment, as in
`[SEAT·4984a346: AA.4.S → OTHER]`.  So a leading header is not searched for a fragment, except the
text after its arrow, which is an addressee, and no step picks a chat whose session id starts with
the fragment in that header.

What is searched.  The listener wraps the message in one marker pair, `BEGIN_UNTRUSTED_… nonce=N`
first and `END_UNTRUSTED_… nonce=N` last (adapters.routine_excerpt), and that frame is unwrapped:
it counts only when its END is the last line.  Any other marked block, nested in the frame or not, is
dropped whole, from its BEGIN line to the END line with the same kind and nonce, or to the end of
the text when it never closes:  nothing inside one is a header, a letter, a fragment or an issue.
Quoted text addresses nobody:  a ```` ```quote ```` (or `~~~quote`) block and a line starting with
`>` are left out of the letter and fragment search, though an issue id there still counts.

A platform whose delivery is just one command needs no `deliver` of its own:  `command_platform`
builds the whole `Platform` from a short spec.  Otherwise, a `deliver` is one plain function:

    def deliver(target: Target, wake: Wake) -> Delivery

`target` is the picked chat (`session_id`, `letter`, `host`, `title`, `thread_link`, `generation`).
`wake` is metadata only (`wake_id`, `message_id`, `channel`, `topic`, `zulip_link`, `sender`, `dm`,
`letter`, `picked`, `reason`, `link_note`), and never holds the message body:  a platform's prompt
names the thread, says to read it and includes `link_note` as it stands.  It runs on a worker
thread, may take as long as it needs, and is never called twice at once for the same `session_id`.
It returns a `Delivery` whose `outcome` is one of:

    sent         handed over, with no proof it was read;  set `ack` to "confirmed" if you have proof
    completed    the chat ran the wake to the end (a headless resume that exited 0)
    busy         the chat cannot take it yet;  the wake waits, and this uses up no attempt
    unreachable  the chat's host did not answer;  the wake waits, and this uses up no attempt
    failed       this try failed and cannot have run the wake;  it is retried up to `max_attempts`
    refused      it can never work, or a retry could run the turn twice;  no retry

Any exception counts as `failed`, so a `deliver` that may already have started the turn must catch
its own errors from that point on and return `refused`.  `route`, `code` and `note` are short fixed
codes for the receipt, never message text and never a credential.  A receipt says `delivered: false`
for a refusal only when its code is in `NEVER_RAN`:  any other refusal may follow a turn that
started, so it says `delivered: null`, because nobody knows.

The answer and the durability.  The listener waits at most its routine timeout, resends with the
same id, and treats any 2xx as accepted, so it never retries after one.  So the POST only checks
and reserves:  the accepted wake is written to `pending/<wake_id>.json` and fsynced BEFORE the 202
goes out, a worker does the pick and the delivery, a restart replays whatever is still pending, and
the entry is dropped only once the final receipt is written.  An id with a final receipt answers
that record;  an id still pending answers 202 again and starts nothing.

A wake that may already have run a turn is never run again:  a duplicate turn is worse than a final,
visible failure, because the sender can see the receipt and send again.  So an in-flight mark is
made durable before `deliver` is called (no mark, no delivery) and cleared when the outcome is
recorded.  A replay that finds the mark writes a `dead` receipt with the code
`interrupted-during-delivery`, whose `delivered` is null because nobody knows, and does not deliver.
A wake the router never started is replayed and delivered, and its receipt says `replayed`.  SIGTERM
stops new work and waits up to `shutdown_wait_seconds` for running deliveries, so a normal restart
interrupts none of them.

A pick can go stale while its wake waits, so the registry is asked again immediately before the
delivery, ahead of the in-flight mark.  If the letter no longer names that session at the generation
picked, nothing is delivered:  a wake addressed to a letter ends in a `dead` receipt with the code
`target-released`, because the letter's new holder is not who the sender meant, and any other wake
goes back through the pick.  A registry that cannot be read is a bounded retry, never a blind
delivery, and so is a live row that does not name a well-formed chat of this seat:  a row with no
seat, or a malformed one, is no target and no proof of release either.  Receipts name the selected
generation as `picked_generation`.

Settings not yet confirmed.  A router whose settings have not come from a successful read in this
process does not know whether delivery is on, and "off" would close every pending wake as `held`.
So until the config says `confirmed`, workers leave wakes pending, use up no attempt and look again;
`max_age_seconds` still bounds the wait, and only a plain dict of settings is confirmed from the
start.

One chat's wakes run in the order they came.  A later wake waits behind an earlier one for the same
chat until that one is settled;  it is not failed as busy.  A wake that still cannot be delivered,
or is older than `max_age_seconds` when its turn comes, ends in a `dead` receipt, visible in the
state directory and at GET /receipt/<id>, and its message is in the seat inbox regardless.

A delivery can run for many minutes, so it does not keep a place in the worker pool:  a worker that
starts one hands its place to a fresh thread and ends once that wake is settled, and a chat that
hangs cannot starve another chat.  `max_deliveries` caps how many run at once, because each may be a
whole headless turn;  a wake over the cap waits and uses up no attempt.

The test flag.  A body with `"test": true` is answered at once with the whole pick and its note (the
fields in `test_answer`), writes a receipt marked as a test and delivers nothing.  It never calls
the decider and never reserves its id.

The answers carry no `ok` field on purpose:  the listener reads a body with a boolean `ok` as the
hosted Worker's settled outcome (adapters.routine_answer), which this is not.

No message body is stored, logged or forwarded.  The excerpt is read for an addressed letter, issue
ids and session-id fragments, and then dropped:  only those validated tokens are kept.
Zulip-authored names pass the secret scanner before they are kept.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import errno
import hmac
import json
import math
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from re import Pattern
from typing import Any

from . import registry_api, secretscan

CONTRACT = "simple-tools-wake/1"
MAX_BODY = 64 * 1024
WAKE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{2,}")
STOP_WORDS = frozenset(["the", "and", "for", "with", "from", "this", "that", "ops", "afc"])
NAME_CHARS = 200
# A Linear issue id as the fleet writes it.  Its own pattern, because STOP_WORDS drops `ops` and
# `afc` and the id must not depend on the topic's words.  The edges keep `P-BF-6` from reading as
# `BF-6`.
ISSUE_ID = r"[A-Z][A-Z0-9]{1,9}-[0-9]{1,7}"
ISSUE_RE = re.compile(r"(?<![A-Za-z0-9_-])%s(?![A-Za-z0-9_-])" % ISSUE_ID)
MAX_WAKE_ISSUES = 8
MAX_ROW_ISSUES = 8
# A piece of a session id:  lower-case hex, hyphens allowed where the id has them, after an optional
# `sess_` prefix that is not kept.
SESSION_PREFIX = "sess_"
FRAGMENT_RE = re.compile(
    r"(?<![0-9A-Za-z_-])(?:%s)?([0-9a-f]+(?:-[0-9a-f]+)*)(?![0-9A-Za-z_-])" % SESSION_PREFIX
)
# A fleet post header's inside:  a seat tag, then its own fragment or one of the marks below.
HEADER_RE = re.compile(r"[A-Z][A-Z0-9_-]*(·(?:%s)?[0-9a-f-]{8,})?(.*)" % SESSION_PREFIX, re.DOTALL)
HEADER_SESSION_RE = re.compile(r":\s*[A-Z]{1,2}\.[0-9]+\.[A-Z]\b")
# An untrusted-text marker line:  BEGIN or END, then the kind (`_ZULIP`, or nothing).
MARKER_LINE_RE = re.compile(r"(BEGIN|END)_UNTRUSTED([A-Z0-9_]*)\b")
# The opening fence of a Zulip quote block.
QUOTE_FENCE_RE = re.compile(r"[ \t]*(`{3,}|~{3,})[ \t]*quote[ \t]*")
FRAGMENT_MIN_HEX = 8
FRAGMENT_MAX_CHARS = 36
# A longer unbroken run of hex is a commit or a key, not a stated session id, and is not kept.
FRAGMENT_MAX_PLAIN = 12
MAX_FRAGMENTS = 8
# The registry's times for a row, best first.  letter_history returns only `assigned_at` today.
RECENCY_FIELDS = ("last_seen_at", "assigned_at")
HISTORY_LIMIT = 200
# Short enough that two reads fit inside the listener's default 15-second wait on a test wake.
TEST_REGISTRY_TIMEOUT = 6
REGISTRY_TIMEOUT = 15
DONE_KEEP_SECONDS = 30 * 86400
# Longer than the listener's routine timeout, so only a client that has stalled is cut off.
REQUEST_TIMEOUT = 30
OUTCOMES = ("sent", "completed", "busy", "unreachable", "failed", "refused")
INTERRUPTED = "interrupted-during-delivery"
EXPIRED = "wake-expired"
RELEASED = "target-released"
# A live registry row the check before delivery cannot read as a chat of this seat.
UNREADABLE_ROW = "registry-row-unreadable"
# A seat tag as the fleet writes one (identity._SEAT_RE).  A registry row must name its seat:  a
# missing one is never read as this platform's.
SEAT_RE = re.compile(r"[A-Z0-9][A-Z0-9_-]{0,31}")
UNCONFIRMED = "settings-unconfirmed"
# The `refused` codes that can only come before anything ran.  It is a list of what is known safe,
# so a new code reads as unknown until someone adds it here.
NEVER_RAN = frozenset(
    {
        RELEASED,
        "bad-request",
        "bad-session-id",
        "command-option-shaped-value",
        "no-ssh-destination",
        "no-transcript",
        "platform-not-installed",
        "session-cwd-gone",
        "unknown-host",
        "unknown-platform",
    }
)
# What the receiving chat is told, by pick reason.  Fixed sentences on purpose:  the only thing that
# can vary is {issue}, and that has matched ISSUE_ID, so no Zulip text reaches a chat's prompt.
UNSURE = "and the router is not sure this wake is meant for this chat."
LINK_NOTES = {
    "letter": "The router linked this wake to this chat because the sender addressed its letter.",
    "reply": "This wake was linked by reply.",
    "session-id": "This wake was linked by session id.",
    "linear-issue": "This wake was linked by Linear issue {issue}.",
    "linear-issue-tie": "This wake was linked by Linear issue {issue}, "
    "as the most recent of several chats on it.",
    "topic-match": "The router linked this wake to this chat because its title matches the topic.",
    "topic-tie": "The router linked this wake to this chat because its title matches the topic, "
    "as the most recent of several equal matches.",
    "consultant-pick": "The router's decider chose this chat for this wake, " + UNSURE,
    "sole-live-chat": "The router could not link this wake to any chat and this is the only "
    "live one, " + UNSURE,
    "most-recent": "The router could not link this wake to any chat and this is the most "
    "recent live one, " + UNSURE,
}

# What a `command_platform` argv may name.  Each value is the router's own or has been validated;
# nothing Zulip-authored (topic, sender, channel, link) has a field, so none can reach an argv.
COMMAND_FIELDS = ("session_id", "wake_id", "message_id", "link_note", "wake_text")
COMMAND_FIELD_RE = re.compile(r"\{([A-Za-z_]+)\}")
COMMAND_TIMEOUT = 900
COMMAND_TEXT = (
    "You were tagged in Zulip (wake id {wake_id}).  {note}Read the message in Zulip before you "
    "act:  zulip_get_message with message_id {message_id}.  Answer in that thread under your "
    "seat's rules."
)

# The settings a router reads and what each one is if nothing sets it.  Their values come from
# Infisical through wake_settings;  fleet_knobs/registry.py holds the same defaults, and a test keeps
# the two equal.
ROUTER_DEFAULTS: dict[str, Any] = {
    # Off until someone arms it:  with this false the router picks and receipts, and delivers nothing.
    "delivery_enabled": False,
    "workers": 4,
    "max_deliveries": 16,
    "max_attempts": 5,
    "retry_backoff": [30, 120, 300, 900],
    "busy_retry_seconds": 30,
    "unreachable_retry_seconds": 300,
    "max_age_seconds": 86400,
    # What a SIGTERM waits for running deliveries.  It covers the longest delivery a default config
    # can start, and the unit's TimeoutStopSec is longer than this.  A delivery still running at the
    # end of it is closed by the replay as interrupted, never run again.
    "shutdown_wait_seconds": 1000,
}


@dataclass(frozen=True)
class Target:
    """One chat a wake can go to."""

    session_id: str
    letter: str = ""
    host: str = ""
    title: str = ""
    thread_link: str = ""
    generation: int = 0  # the registry's generation of `letter`;  0 only when built outside a pick


@dataclass(frozen=True)
class Wake:
    """A wake's metadata.  It has no field for the message body, by design."""

    wake_id: str
    seat: str = ""
    message_id: int = 0
    channel: str = ""
    topic: str = ""
    zulip_link: str = ""
    sender: str = ""
    dm: bool = False
    letter: str = ""
    # The router fills these three just before `deliver`;  they are empty until then.
    picked: str = ""
    reason: str = ""
    link_note: str = ""
    # Tokens read from the excerpt and validated.  The excerpt itself is never kept.
    issue_ids: tuple[str, ...] = ()
    session_fragments: tuple[str, ...] = ()
    sender_fragment: str = ""
    linked_by: str = ""  # "reply" when the listener says the letter came from a reply, else ""


@dataclass(frozen=True)
class Delivery:
    """What a platform's `deliver` did.  See the module docstring for the outcomes."""

    outcome: str
    route: str = ""
    ack: str = "unknown"
    code: str = ""
    note: str = ""


@dataclass(frozen=True)
class Platform:
    """What one platform supplies to the shared router."""

    seat: str
    platform: str
    mention: str
    target_re: Pattern[str]
    deliver: Callable[[Target, Wake], Delivery]
    # The session ids among the candidates that must never be picked (a sub-agent, a headless run).
    exclude: Callable[[list[Target]], set[str]] | None = None
    stop_words: frozenset[str] = frozenset()


#: The seam for an optional decider, off by default.  It receives the candidates and the wake's
#: metadata and returns {"session_id": id} or None.  A missing decider, a None, an exception or an
#: id that is not a candidate all fall through to recency.
Consult = Callable[[list[Target], Wake], Mapping[str, Any] | None]


@dataclass(frozen=True)
class Pick:
    target: Target | None = None
    picked: str = ""  # letter|reply|session-id|linear-issue|topic-match|consultant|most-recent|""
    reason: str = ""
    letter_live: bool = False
    retry: bool = False
    candidates: tuple[str, ...] = ()
    link_note: str = ""


def clean(value: object, limit: int = NAME_CHARS) -> str:
    """Zulip-authored text as it may be kept:  one line, bounded, and withheld if secret-shaped."""
    text = " ".join(str(value or "").split())[:limit]
    return secretscan.WITHHELD if secretscan.scan(text) else text


def letter_of(payload: Mapping[str, Any], mention: str) -> str:
    """The letter the sender addressed, or "".  An explicit field wins, then a real mention anywhere
    in the excerpt (@**Name** AA, with or without Zulip's |id, the letter on its line or the next).
    The plain "Name AA" form counts only at the start of a line:  later in a line it is usually a
    reference to that chat, not an address.  The seat's own tag is not a letter, though a post often
    opens with it under the mention ("ZC, can you").  A silent mention (@_**Name**) addresses
    nobody."""
    explicit = str(payload.get("letter") or "").strip()
    if re.fullmatch(r"[A-Z]{1,2}", explicit):
        return explicit
    excerpt = unquoted(trusted_text(str(payload.get("excerpt") or "")))
    name = re.escape(mention)
    seat = str(payload.get("seat") or "")
    for form in (
        r"@\*\*%s(?:\|\d+)?\*\*\s*([A-Z]{2})\b" % name,
        r"(?m)^[ \t]*%s[ \t]+([A-Z]{2})\b" % name,
    ):
        for found in re.finditer(form, excerpt):
            if found.group(1) != seat:
                return found.group(1)
    return ""


def link_note_ok(text: object) -> bool:
    """Whether this is a sentence the router could have written."""
    for note in LINK_NOTES.values():
        pattern = re.escape(note).replace(re.escape("{issue}"), ISSUE_ID)
        if isinstance(text, str) and re.fullmatch(pattern, text):
            return True
    return False


def _is_post_header(inner: str) -> bool:
    """Whether a leading bracket is a fleet post header and not ordinary text.  It starts with a
    seat tag, then has the sender's fragment (`[SEAT·4984a346]`, with or without more after it), a
    session token (`[SEAT: AA.4.S`), a repo mark or an arrow."""
    found = HEADER_RE.fullmatch(inner.strip())
    if found is None:
        return False
    rest = found.group(2).lstrip()
    return (
        found.group(1) is not None
        or rest.startswith(("\U0001f4c1", "→"))
        or HEADER_SESSION_RE.match(rest) is not None
    )


def _marker(line: str) -> tuple[str, str, str] | None:
    """(BEGIN or END, the marker's kind, its nonce) for an untrusted-text marker line, else None."""
    found = MARKER_LINE_RE.match(line)
    if found is None:
        return None
    nonce = re.search(r"\bnonce=(\S*)", line)
    return found.group(1), found.group(2), nonce.group(1) if nonce else ""


def trusted_text(excerpt: str) -> str:
    """The excerpt without its listener frame and without any other marked block, whole.  A block
    that is opened and never closed runs to the end, so it cannot leak either."""
    lines = excerpt.split("\n")
    first, last = _marker(lines[0]), _marker(lines[-1])
    if (
        len(lines) >= 2
        and first is not None
        and last is not None
        and (first[0], last[0]) == ("BEGIN", "END")
        and first[1:] == last[1:]
    ):
        lines = lines[1:-1]
    kept: list[str] = []
    opened: tuple[str, str] | None = None
    for line in lines:
        mark = _marker(line)
        if opened is not None:
            if mark is not None and mark[0] == "END" and mark[1:] == opened:
                opened = None
            continue
        if mark is not None:
            if mark[0] == "BEGIN":
                opened = mark[1:]
            continue  # a stray END line is dropped too
        kept.append(line)
    return "\n".join(kept)


def unquoted(text: str) -> str:
    """The text without Zulip quotes:  ```` ```quote ```` and `~~~quote` blocks (to their closing
    fence, or to the end when there is none) and lines starting with `>`."""
    kept: list[str] = []
    fence: tuple[str, int] | None = None
    for line in text.split("\n"):
        if fence is not None:
            mark, length = fence
            if re.fullmatch(r"[ \t]*%s{%d,}[ \t]*" % (re.escape(mark), length), line):
                fence = None
            continue
        opening = QUOTE_FENCE_RE.fullmatch(line)
        if opening is not None:
            fence = (opening.group(1)[0], len(opening.group(1)))
            continue
        if not line.lstrip().startswith(">"):
            kept.append(line)
    return "\n".join(kept)


def split_excerpt(excerpt: str) -> tuple[str, str]:
    """(the sender's part of a leading post header, the text that may address a chat).  Only
    trusted text is read (`trusted_text`).  Text after the header's arrow names an addressee, so it
    stays with the body;  the rest of the header is the sender's."""
    body = trusted_text(excerpt).lstrip()
    header = re.match(r"(?:\U0001F440\s*)?\[([^\]\n]*)\]", body)
    if header is None or not _is_post_header(header.group(1)):
        return "", body
    sender, _, addressee = header.group(1).partition("→")
    return sender, addressee + "\n" + body[header.end() :]


def fragments_in(text: str) -> tuple[str, ...]:
    """The tokens in this text shaped like part of a session id, bounded."""
    found: dict[str, None] = {}
    for token in FRAGMENT_RE.findall(text):
        hex_chars = len(token.replace("-", ""))
        if hex_chars < FRAGMENT_MIN_HEX or len(token) > FRAGMENT_MAX_CHARS:
            continue
        if ("-" not in token and hex_chars > FRAGMENT_MAX_PLAIN) or secretscan.scan(token):
            continue
        found[token] = None
        if len(found) == MAX_FRAGMENTS:
            break
    return tuple(found)


def wake_from_payload(payload: Mapping[str, Any], mention: str) -> Wake:
    message_id = payload.get("message_id")
    excerpt = str(payload.get("excerpt") or "")
    sender_part, addressing = split_excerpt(excerpt)
    return Wake(
        wake_id=str(payload.get("wake_id") or ""),
        seat=str(payload.get("seat") or ""),
        message_id=message_id if type(message_id) is int else 0,
        channel=clean(payload.get("channel")),
        topic=clean(payload.get("topic")),
        zulip_link=clean(payload.get("zulip_link"), 500),
        sender=clean(payload.get("sender_full_name")),
        dm=payload.get("dm") is True,
        letter=letter_of(payload, mention),
        issue_ids=tuple(dict.fromkeys(ISSUE_RE.findall(trusted_text(excerpt))))[:MAX_WAKE_ISSUES],
        session_fragments=fragments_in(unquoted(addressing)),
        sender_fragment=next(iter(fragments_in(sender_part)), ""),
        linked_by="reply" if payload.get("linked_by") == "reply" else "",
    )


def private_dir(folder: Path) -> None:
    """Make this private state folder, refusing a symlink that would send its files elsewhere.  An
    OSError, like any other failure to write here, so every caller already handles it."""
    if folder.is_symlink():
        raise OSError(errno.ELOOP, "refusing a symlinked state folder", str(folder))
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)


def atomic_write(path: Path, obj: Any) -> None:
    """One private file, fsynced, then renamed into place and its folder fsynced:  it survives a
    crash whole or not at all."""
    private_dir(path.parent)
    tmp = path.with_name("%s.%d.%d.tmp" % (path.name, os.getpid(), threading.get_ident()))
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def _fsync_dir(folder: Path) -> None:
    fd = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def load_config(
    path: Path, defaults: Mapping[str, Any], *, required: bool = False
) -> dict[str, Any]:
    """The defaults overlaid with a JSON file.  It widens as freely as it narrows, so nothing that
    runs a router reads its settings this way:  wake_settings lets a local file only narrow."""
    return {**defaults, **read_local_config(path, defaults, required=required)}


def read_local_config(
    path: Path, defaults: Mapping[str, Any], *, required: bool = False
) -> dict[str, Any]:
    """Only what a JSON file sets.  A file named explicitly must exist.  A file that is there must
    parse, name only known keys and match each default's type:  an unreadable or mistyped config is
    a refusal that names the path, never a quiet run on defaults."""
    config: dict[str, Any] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if required:
            raise SystemExit("wake-router: the config file %s is not there" % path) from None
        return config
    except OSError as exc:
        raise SystemExit(
            "wake-router: the config file %s is unreadable (%s)" % (path, type(exc).__name__)
        ) from None
    try:
        loaded = json.loads(text)
    except ValueError:
        raise SystemExit("wake-router: the config file %s is not valid JSON" % path) from None
    if not isinstance(loaded, dict):
        raise SystemExit("wake-router: the config file %s is not a JSON object" % path)
    for key, value in loaded.items():
        if key not in defaults:
            raise SystemExit("wake-router: %s: unknown setting %s" % (path, key))
        if not _same_shape(value, defaults[key]):
            raise SystemExit("wake-router: %s: %s has the wrong type" % (path, key))
        config[key] = value
    return config


def _same_shape(value: object, default: object) -> bool:
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return type(value) is int and value > 0
    if isinstance(default, list):
        return (
            isinstance(value, list) and bool(value) and all(type(v) is int and v > 0 for v in value)
        )
    return isinstance(value, str)


def read_key(path: Path) -> str:
    """The routine key, from a 0600 file, on every request.  "" when it is missing, short or open
    to anyone else."""
    try:
        key = path.read_text(encoding="utf-8").strip()
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        return ""
    return "" if mode & 0o077 or len(key) < 16 else key


def command_platform(
    seat: str,
    platform: str,
    mention: str,
    target_re: Pattern[str],
    argv: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    timeout: int = COMMAND_TIMEOUT,
    exclude: Callable[[list[Target]], set[str]] | None = None,
    stop_words: frozenset[str] = frozenset(),
    runner: Callable[..., Any] = subprocess.run,
) -> Platform:
    """A `Platform` whose delivery is one command, run without a shell.  `argv` may name the
    `COMMAND_FIELDS` as {session_id} and so on, anywhere but its first item;  any other name in
    braces is a ValueError here, at startup.  A value that could read as an option is `refused`
    before anything runs.  Exit 0 is `sent` with `ack` unknown, because it proves only that the
    command ran.  A non-zero exit or a timeout is `refused`, because the turn may have started;  an
    OSError at start is `failed`.  The child gets `env`, or else only HOME and PATH:  the router's
    own environment holds its keys."""
    template = [argv] if isinstance(argv, str) else list(argv)
    named = {name for part in template for name in COMMAND_FIELD_RE.findall(part)}
    if len(template) < 2 or COMMAND_FIELD_RE.search(template[0]) or named - set(COMMAND_FIELDS):
        raise ValueError("command_platform: a program, its arguments and only known fields")
    child_env = (
        dict(env)
        if env is not None
        else {name: os.environ[name] for name in ("HOME", "PATH") if name in os.environ}
    )

    def deliver(target: Target, wake: Wake) -> Delivery:
        note = wake.link_note if link_note_ok(wake.link_note) else ""
        values = {
            "session_id": target.session_id,
            "wake_id": wake.wake_id,
            "message_id": str(wake.message_id),
            "link_note": note,
            "wake_text": COMMAND_TEXT.format(
                wake_id=wake.wake_id, note=note + "  " if note else "", message_id=wake.message_id
            ),
        }
        # `target_re` is the platform's own and may allow it, so this does not rely on that.
        if any(values[name].startswith("-") for name in named):
            return Delivery("refused", "command", code="command-option-shaped-value")
        # One pass per item, so a value is never read again as a field.
        command = [COMMAND_FIELD_RE.sub(lambda m: values[m.group(1)], part) for part in template]
        try:
            proc = runner(
                command,
                cwd=cwd,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return Delivery("refused", "command", code="command-timeout")
        except OSError as exc:  # it never started:  the one failure that is safe to try again
            return Delivery("failed", "command", code="command-%s" % type(exc).__name__)
        except Exception as exc:  # noqa: BLE001 - it may have started, so it is final
            return Delivery("refused", "command", code="command-error-%s" % type(exc).__name__)
        if proc.returncode == 0:
            return Delivery("sent", "command")
        return Delivery("refused", "command", code="command-exit-%d" % proc.returncode)

    return Platform(seat, platform, mention, target_re, deliver, exclude, stop_words)


class Router:
    """The pick, the durable queue, the workers and the receipts for one platform."""

    def __init__(
        self,
        platform: Platform,
        env: Mapping[str, str],
        config: Mapping[str, Any],
        state: Path,
        key_file: Path,
        consult: Consult | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.platform = platform
        self.env = env
        self.config = config
        self.state = state
        self.key_file = key_file
        self.consult = consult
        self.clock = clock
        self._entries: dict[str, dict[str, Any]] = {}
        self._active: set[str] = set()
        self._cond = threading.Condition()
        self._stopping = False
        self._threads: list[threading.Thread] = []
        self._retiring: set[threading.Thread] = set()

    # ---- the pick ---------------------------------------------------------------------------

    def _valid(self, row: Mapping[str, Any]) -> Target | None:
        return target_from_row(row, self.platform.seat, self.platform.target_re)

    def _excluded(self, targets: list[Target]) -> set[str]:
        return self.platform.exclude(targets) if self.platform.exclude and targets else set()

    def _words(self, text: str) -> set[str]:
        stop = STOP_WORDS | self.platform.stop_words | {self.platform.platform}
        return {w.lower() for w in WORD_RE.findall(text or "") if w.lower() not in stop}

    def pick(self, wake: Wake, *, consult: bool = True, timeout: int = REGISTRY_TIMEOUT) -> Pick:
        """The rules, in order:  an addressed letter;  a session-id fragment;  a Linear issue;  the
        chat whose title best shares the topic's words;  the optional decider;  recency.  A tie goes
        to the most recent of the tied, and no step picks the sender's own chat.  A registry that
        cannot be read asks for a retry:  it is not "no live chat".  No live chat asks for one too,
        because a chat may register in time."""
        letter_live = False

        def chosen(
            target: Target, picked: str, reason: str, among: Any = (), issue: str = ""
        ) -> Pick:
            return Pick(
                target=target,
                picked=picked,
                reason=reason,
                letter_live=letter_live,
                candidates=tuple(t.letter for t in among),
                link_note=LINK_NOTES[reason].format(issue=issue),
            )

        if wake.letter:
            answer = registry_api.call(
                self.env,
                "resolve",
                {"platform": self.platform.platform, "letter": wake.letter},
                timeout=timeout,
            )
            found = answer.get("found") if isinstance(answer, Mapping) else None
            if type(found) is not bool or "error" in answer:
                # Unreadable, so not a dead letter:  falling back would wake a chat not addressed.
                return Pick(reason="registry-unavailable", retry=True)
            live = found and not _closed(answer)
            target = self._valid(answer) if live else None
            if target is not None:
                letter_live = True
                # An excluded chat is not a place a wake can go, so the chain carries on without it.
                if target.session_id not in self._excluded([target]) and not _is_sender(
                    target, wake
                ):
                    how = "reply" if wake.linked_by == "reply" else "letter"
                    return chosen(target, how, how)
        answer = registry_api.call(
            self.env,
            "history",
            {"platform": self.platform.platform, "limit": HISTORY_LIMIT},
            timeout=timeout,
        )
        if not isinstance(answer, Mapping) or "error" in answer:
            return Pick(reason="registry-unavailable", retry=True)
        live: dict[str, Target] = {}
        seen: dict[str, float | None] = {}
        row_issues: dict[str, list[tuple[str, float | None]]] = {}
        for row in answer.get("items") or []:
            if not isinstance(row, dict):
                continue
            if _closed(row):
                continue
            target = self._valid(row)
            if target is not None and target.session_id not in live:
                live[target.session_id] = target
                seen[target.session_id] = _row_time(row)
                row_issues[target.session_id] = _issues_of(row)
        excluded = self._excluded(list(live.values()))
        candidates = [
            t for sid, t in live.items() if sid not in excluded and not _is_sender(t, wake)
        ]
        if not candidates:
            return Pick(reason="no-live-session", letter_live=letter_live, retry=True)

        named = _named_session(wake, live)
        target = next((t for t in candidates if t.session_id == named), None)
        if target is not None:
            return chosen(target, "session-id", "session-id")

        wanted = list(dict.fromkeys(ISSUE_RE.findall(wake.topic) + list(wake.issue_ids)))
        linked = self._on_issue(candidates, wanted, row_issues, seen)
        if linked is not None:
            target, issue, among = linked
            reason = "linear-issue-tie" if len(among) > 1 else "linear-issue"
            return chosen(target, "linear-issue", reason, among if len(among) > 1 else (), issue)

        topic_words = self._words(wake.topic)
        scored = [(len(topic_words & self._words(t.title)), t) for t in candidates]
        best = max(score for score, _ in scored)
        if best > 0:
            top = [t for score, t in scored if score == best]
            if len(top) == 1:
                return chosen(top[0], "topic-match", "topic-match")
            return chosen(_most_recent(top, seen), "topic-match", "topic-tie", top)

        if len(candidates) == 1:
            return chosen(candidates[0], "most-recent", "sole-live-chat")
        decided = self._decide(candidates, wake) if consult else None
        if decided is not None:
            return chosen(decided, "consultant", "consultant-pick", candidates)
        return chosen(_most_recent(candidates, seen), "most-recent", "most-recent", candidates)

    @staticmethod
    def _on_issue(
        candidates: list[Target],
        wanted: list[str],
        row_issues: Mapping[str, list[tuple[str, float | None]]],
        seen: Mapping[str, float | None],
    ) -> tuple[Target, str, list[Target]] | None:
        """The chat linked by one of these issue ids, the id, and all the chats that matched.  The
        registry's `issues` come first, newest `seen_at` first.  A chat's title is the fallback, and
        only for a record that has no `issues`."""
        if not wanted:
            return None
        newest: dict[str, tuple[float | None, str]] = {}
        for target in candidates:
            hits = [(when, i) for i, when in row_issues[target.session_id] if i in wanted]
            if hits:
                newest[target.session_id] = max(hits, key=lambda h: (h[0] or 0.0, h[1]))
        if newest:
            among = [t for t in candidates if t.session_id in newest]
            best = _most_recent(among, {sid: hit[0] for sid, hit in newest.items()})
            return best, newest[best.session_id][1], among
        titled = {}
        for target in candidates:
            in_title = set(ISSUE_RE.findall(target.title))
            issue = next((i for i in wanted if i in in_title), "")
            if issue and not row_issues[target.session_id]:
                titled[target.session_id] = issue
        if not titled:
            return None
        among = [t for t in candidates if t.session_id in titled]
        best = _most_recent(among, seen)
        return best, titled[best.session_id], among

    def _decide(self, candidates: list[Target], wake: Wake) -> Target | None:
        """The optional decider's choice, or None for anything but one of these candidates."""
        if self.consult is None:
            return None
        try:
            answer = self.consult(list(candidates), wake)
        except Exception as exc:  # noqa: BLE001 - a decider's failure only means recency decides
            self._log("consultant-error-%s" % type(exc).__name__, wake.wake_id)
            return None
        if not isinstance(answer, Mapping):
            return None
        return next((t for t in candidates if t.session_id == answer.get("session_id")), None)

    # ---- accepting --------------------------------------------------------------------------

    def _pending(self, wake_id: str) -> Path:
        return self.state / "pending" / (wake_id + ".json")

    def _done(self, wake_id: str) -> Path:
        return self.state / "done" / (wake_id + ".json")

    def accept(self, wake: Wake) -> tuple[int, dict[str, Any]]:
        """Reserve one wake durably and answer.  A failure to write is a 503, so the listener
        retries it;  a 202 is only ever sent for a wake that is on disk."""
        with self._cond:
            record = _read_json(self._done(wake.wake_id))
            if isinstance(record, dict):
                return 200, record
            held = self._entries.get(wake.wake_id)
            if held is not None:
                return 202, self._accepted(held)
            entry = {
                "wake": asdict(wake),
                "receipt_id": secrets.token_hex(8),
                "accepted_at": self.clock(),
                "next_at": 0.0,
                "attempts": 0,
                "failures": 0,
                "pick": None,
            }
            try:
                atomic_write(self._pending(wake.wake_id), entry)
            except OSError as exc:
                self._log("accept-write-failed-%s" % type(exc).__name__, wake.wake_id)
                return 503, {"error": "the wake could not be recorded", "wake_id": wake.wake_id}
            self._entries[wake.wake_id] = entry
            self._cond.notify_all()
            return 202, self._accepted(entry)

    @staticmethod
    def _accepted(entry: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "wake_id": entry["wake"]["wake_id"],
            "receipt_id": entry["receipt_id"],
            "status": "accepted",
        }

    def test_answer(self, wake: Wake) -> dict[str, Any]:
        """A test wake's answer:  `test`, `wake_id`, `receipt_id`, `picked`, `picked_letter`,
        `picked_generation`, `session_id`, `reason`, `candidates`, `link_note`, `delivered`.  It
        delivers nothing and reserves nothing."""
        pick = self.pick(wake, consult=False, timeout=TEST_REGISTRY_TIMEOUT)
        answer = self._receipt(
            {"wake": asdict(wake), "receipt_id": secrets.token_hex(8), "attempts": 0},
            pick,
            status="test",
            delivered=False,
            result=None,
        )
        answer["test"] = True
        self._append_log(answer)
        return answer

    def receipt(self, wake_id: str) -> dict[str, Any] | None:
        """A wake's record:  the final receipt, or its accepted state while it is pending."""
        with self._cond:
            record = _read_json(self._done(wake_id))
            if isinstance(record, dict):
                return record
            entry = self._entries.get(wake_id)
            return self._accepted(entry) if entry is not None else None

    # ---- the workers ------------------------------------------------------------------------

    def start(self) -> None:
        """Replay what a restart left pending, then start the workers."""
        self.replay()
        with self._cond:
            for _ in range(int(self.config["workers"])):
                self._add_worker()

    def _add_worker(self) -> None:
        thread = threading.Thread(target=self._loop, daemon=True)
        thread.start()
        self._threads.append(thread)

    def _give_up_slot(self) -> None:
        """A delivery may run for many minutes and must not cost another chat a worker, so this
        thread's place in the pool goes to a fresh one, and this thread ends once its wake is
        settled.  One delivery per chat and `max_deliveries` bound what that can start."""
        current = threading.current_thread()
        with self._cond:
            if self._stopping or current not in self._threads:
                return  # no new work is wanted, or this is `run_once` on a caller's own thread
            self._threads = [thread for thread in self._threads if thread.is_alive()]
            try:
                self._add_worker()
            except RuntimeError:
                return  # no thread to be had:  this worker just stays busy, as it would have
            self._retiring.add(current)

    def stop(self, wait: float = 5.0) -> bool:
        """Claim nothing more and wait up to `wait` seconds in all for running deliveries.  Answers
        whether every worker finished."""
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        deadline = time.monotonic() + wait
        for thread in self._threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        return not any(thread.is_alive() for thread in self._threads)

    def replay(self) -> int:
        """Load every pending file.  One with a final receipt finished just before a crash, so it
        is only removed.  One that is not a whole entry stays on disk, set aside and named on
        stderr, and is never run.  One still marked in flight may have run its turn, so it is
        settled as interrupted and only its receipt is left to write."""
        self._prune()
        folder = self.state / "pending"
        loaded = 0
        with self._cond:
            for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
                wake_id = path.name[: -len(".json")]
                if self._done(wake_id).exists():
                    path.unlink(missing_ok=True)
                    continue
                entry = _read_json(path)
                if not _loadable(entry, wake_id):
                    self._log("pending-unreadable", wake_id)
                    continue
                entry["wake"] = asdict(Wake(**entry["wake"]))
                entry["replayed"] = True
                entry["next_at"] = 0.0
                if entry.get("in_flight") and entry.get("receipt") is None:
                    self._log(INTERRUPTED, wake_id)
                    entry["receipt"] = self._interrupted(entry)
                self._entries[wake_id] = entry
                loaded += 1
            self._cond.notify_all()
        return loaded

    def _interrupted(self, entry: Mapping[str, Any]) -> dict[str, Any]:
        """The final receipt of a wake that may have run its turn:  dead, and delivered unknown."""
        record = entry.get("pick")
        pick = _pick_from(record) if record else Pick(reason=INTERRUPTED)
        result = Delivery("failed", code=INTERRUPTED)
        return self._receipt(entry, pick, status="dead", delivered=None, result=result)

    def _prune(self) -> None:
        folder = self.state / "done"
        cutoff = self.clock() - DONE_KEEP_SECONDS
        for path in folder.glob("*.json") if folder.is_dir() else []:
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                pass

    def _claim(self) -> dict[str, Any] | None:
        """The oldest wake that can run now, under the lock.  A wake behind one that is running or
        waiting for the same chat stays behind it, so one chat's wakes run in the order they came.
        A wake whose outcome is settled has only its receipt left, and holds nobody up."""
        now = self.clock()
        blocked: set[str] = set(self._active)
        blocked.update(_session_of(e) for e in self._entries.values() if e.get("running"))
        full = self._full()
        picking = False  # an earlier wake still being picked may turn out to want the same chat
        for entry in sorted(self._entries.values(), key=_order):
            if entry.get("running"):
                picking = picking or entry["pick"] is None
                continue
            session = "" if entry.get("receipt") is not None else _session_of(entry)
            if session and (picking or full or session in blocked):
                continue
            if entry["next_at"] > now:
                if session:
                    blocked.add(session)
                continue
            entry["running"] = True
            return entry
        return None

    def _full(self) -> bool:
        """Whether as many deliveries are running as may run at once."""
        return len(self._active) >= int(self.config["max_deliveries"])

    def _behind(self, entry: Mapping[str, Any], session: str) -> bool:
        """Whether an earlier unsettled wake wants this chat, or is being picked and may."""
        mine = _order(entry)
        for other in self._entries.values():
            if other is entry or other.get("receipt") is not None or _order(other) >= mine:
                continue
            if _session_of(other) == session or (other.get("running") and other["pick"] is None):
                return True
        return False

    def _loop(self) -> None:
        while True:
            try:
                with self._cond:
                    current = threading.current_thread()
                    if self._stopping or current in self._retiring:
                        self._retiring.discard(current)
                        return
                    entry = self._claim()
                    if entry is None:
                        self._cond.wait(timeout=1.0)
                        continue
                self._run(entry)
            except Exception as exc:  # noqa: BLE001 - a dead worker would still look healthy
                self._log("worker-loop-error-%s" % type(exc).__name__, "-")
                time.sleep(1.0)

    def run_once(self) -> bool:
        """Run one claimable wake on this thread, or answer False.  The workers' body, for tests."""
        with self._cond:
            entry = self._claim()
        if entry is None:
            return False
        self._run(entry)
        return True

    def _run(self, entry: dict[str, Any]) -> None:
        try:
            self._process(entry)
        except Exception as exc:  # noqa: BLE001 - one bad wake must not take a worker with it
            code = "worker-error-%s" % type(exc).__name__
            self._log(code, entry["wake"]["wake_id"])
            try:
                if entry.get("in_flight") and entry.get("receipt") is None:
                    # The delivery may have run, and its outcome was lost:  never run it again.
                    entry["receipt"] = self._interrupted(entry)
                    entry.pop("in_flight", None)
                    self._save(entry)
                    self._write_receipt(entry)
                else:
                    self._retry(entry, counted=True, code=code)
            except Exception as again:  # noqa: BLE001 - it stays claimed, so it cannot run twice
                self._log("worker-error-%s" % type(again).__name__, entry["wake"]["wake_id"])

    def _process(self, entry: dict[str, Any]) -> None:
        if entry.get("receipt") is not None:
            self._write_receipt(entry)  # already settled:  only its receipt is outstanding
            return
        if self.clock() - entry["accepted_at"] >= int(self.config["max_age_seconds"]):
            # An outage can outlast a wake:  nobody is waiting on one this old any more.
            old = _pick_from(entry["pick"]) if entry["pick"] else Pick(reason=EXPIRED)
            result = Delivery("failed", code=EXPIRED)
            self._finish(entry, old, status="dead", delivered=False, result=result)
            return
        if not getattr(self.config, "confirmed", True):
            # Before the pick, so a pick is never kept across an outage and delivered stale.
            kept = _pick_from(entry["pick"]) if entry["pick"] else None
            waiting = Delivery("busy", code=UNCONFIRMED)
            self._retry(entry, counted=False, code=UNCONFIRMED, pick=kept, result=waiting)
            return
        wake = Wake(**entry["wake"])
        if entry["pick"] is None:
            pick = self.pick(wake)
            if pick.retry:
                self._retry(entry, counted=True, code=pick.reason, pick=pick)
                return
            entry["pick"] = asdict(pick)
            self._save(entry)
        pick = _pick_from(entry["pick"])
        if pick.target is None:
            self._finish(entry, pick, status="held", delivered=False, result=None)
            return
        if not self.config["delivery_enabled"]:
            held = replace(pick, reason="delivery-disabled")
            self._finish(entry, held, status="held", delivered=False, result=None)
            return
        session = pick.target.session_id
        with self._cond:
            if (
                self._stopping
                or self._full()
                or session in self._active
                or self._behind(entry, session)
            ):
                # Shutting down, at the cap, or another wake has this chat first:  go back with the
                # pick recorded, using no attempt.  Its place in the order is kept by `_claim`.
                entry["running"] = False
                self._cond.notify_all()
                return
            self._active.add(session)
        # The chat stays held until the outcome is settled, so a later wake cannot start between
        # this delivery ending and its result being recorded.
        try:
            stale = self._stale(pick.target)
            if stale == RELEASED:
                self._released(entry, pick, wake)
                return
            if stale:
                self._retry(entry, counted=True, code=stale, pick=pick)
                return
            entry["attempts"] += 1
            entry["in_flight"] = True
            if not self._save(entry):
                # Without the durable mark a crash would run this again, so it does not start.
                entry["attempts"] -= 1
                entry.pop("in_flight", None)
                waiting = Delivery("busy", code="in-flight-mark-failed")
                self._retry(entry, counted=False, code=waiting.code, pick=pick, result=waiting)
                return
            told = replace(wake, picked=pick.picked, reason=pick.reason, link_note=pick.link_note)
            self._give_up_slot()
            try:
                result = self.platform.deliver(pick.target, told)
            except Exception as exc:  # noqa: BLE001 - a platform's failure is a failed try
                result = Delivery("failed", code="deliver-error-%s" % type(exc).__name__)
            self._settle(entry, pick, result)
        finally:
            with self._cond:
                self._active.discard(session)
                self._cond.notify_all()

    def _stale(self, target: Target) -> str:
        """Empty when the registry still has this letter on this session at the generation picked,
        else why not:  `RELEASED` when it says so, or the code of a bounded retry when it cannot be
        read or its row cannot be read as a chat of this seat.  Neither of those is a yes or a no."""
        answer = registry_api.call(
            self.env,
            "resolve",
            {"platform": self.platform.platform, "letter": target.letter},
            timeout=REGISTRY_TIMEOUT,
        )
        if not isinstance(answer, Mapping) or "error" in answer:
            return "registry-unavailable"
        held = holds(answer, target, self.platform.seat, self.platform.target_re)
        if held is None:
            return UNREADABLE_ROW
        return "" if held else RELEASED

    def _released(self, entry: dict[str, Any], pick: Pick, wake: Wake) -> None:
        """The picked chat is gone or re-registered, and nothing was delivered.  A wake addressed
        to a letter ends here.  Any other goes back through the pick, a bounded number of times so
        a registry whose two reads disagree cannot spin a worker."""
        entry["repicks"] = entry.get("repicks", 0) + 1
        if wake.letter or entry["repicks"] > int(self.config["max_attempts"]):
            result = Delivery("refused", code=RELEASED)
            self._finish(entry, pick, status="dead", delivered=False, result=result)
            return
        entry["pick"] = None
        self._save(entry)
        with self._cond:
            entry["running"] = False
            self._cond.notify_all()

    def _settle(self, entry: dict[str, Any], pick: Pick, result: Delivery) -> None:
        outcome = result.outcome if result.outcome in OUTCOMES else "failed"
        if outcome in ("sent", "completed"):
            # A hand-over with no proof is recorded as unknown (null), never as delivered.
            delivered = True if outcome == "completed" or result.ack == "confirmed" else None
            self._finish(entry, pick, status="delivered", delivered=delivered, result=result)
        elif outcome == "refused":
            delivered = False if result.code in NEVER_RAN else None
            self._finish(entry, pick, status="dead", delivered=delivered, result=result)
        elif outcome == "failed":
            self._retry(entry, counted=True, code=result.code or "failed", pick=pick, result=result)
        else:
            self._retry(entry, counted=False, code=result.code or outcome, pick=pick, result=result)

    def _retry(
        self,
        entry: dict[str, Any],
        *,
        counted: bool,
        code: str,
        pick: Pick | None = None,
        result: Delivery | None = None,
    ) -> None:
        now = self.clock()
        if counted:
            entry["failures"] += 1
        too_many = entry["failures"] >= int(self.config["max_attempts"])
        too_old = now - entry["accepted_at"] >= int(self.config["max_age_seconds"])
        if too_many or too_old:
            final = result or Delivery("failed", code=code)
            self._finish(
                entry, pick or Pick(reason=code), status="dead", delivered=False, result=final
            )
            return
        if counted:
            backoff = self.config["retry_backoff"]
            wait = backoff[min(entry["failures"] - 1, len(backoff) - 1)]
        elif result is not None and result.outcome == "busy":
            wait = self.config["busy_retry_seconds"]
        else:
            wait = self.config["unreachable_retry_seconds"]
        entry["next_at"] = now + wait
        entry.pop("in_flight", None)  # the outcome says this try did not run the wake
        # Saved while this worker still owns the entry:  once it is released another worker may
        # finish it, and a late save would bring its pending file back.
        self._save(entry)
        with self._cond:
            entry["running"] = False
            self._cond.notify_all()

    def _save(self, entry: Mapping[str, Any]) -> bool:
        """Keep the pending file current, so a replay keeps the pick, the counts and the in-flight
        mark, and answer whether it was written.  A failure leaves the previous file in place:  the
        wake is already on disk.  Only the in-flight mark has to be written before going on;  a
        mark that cannot then be cleared errs toward a final receipt, never a second delivery."""
        record = {k: v for k, v in entry.items() if k != "running"}
        try:
            atomic_write(self._pending(entry["wake"]["wake_id"]), record)
        except OSError as exc:
            self._log("pending-write-failed-%s" % type(exc).__name__, entry["wake"]["wake_id"])
            return False
        return True

    # ---- the receipt ------------------------------------------------------------------------

    def _receipt(
        self,
        entry: Mapping[str, Any],
        pick: Pick,
        *,
        status: str,
        delivered: bool | None,
        result: Delivery | None,
    ) -> dict[str, Any]:
        wake = entry["wake"]
        target = pick.target
        return {
            "receipt_id": entry["receipt_id"],
            "wake_id": wake["wake_id"],
            "seat": self.platform.seat,
            "at": self.clock(),
            "test": False,
            "status": status,
            "letter": wake["letter"],
            "letter_live": pick.letter_live,
            "picked": pick.picked,
            "picked_letter": target.letter if target else "",
            "picked_generation": target.generation if target else 0,
            "session_id": target.session_id if target else "",
            "host": target.host if target else "",
            "thread_link": target.thread_link if target else "",
            "reason": pick.reason,
            "candidates": list(pick.candidates),
            "link_note": pick.link_note,
            "delivered": delivered,
            "route": result.route if result else "",
            "ack": result.ack if result else "",
            "code": result.code if result else "",
            "note": result.note if result else "",
            "attempts": entry["attempts"],
            "replayed": bool(entry.get("replayed")),
            "channel": wake["channel"],
            "topic": wake["topic"],
            "zulip_link": wake["zulip_link"],
        }

    def _finish(
        self,
        entry: dict[str, Any],
        pick: Pick,
        *,
        status: str,
        delivered: bool | None,
        result: Delivery | None,
    ) -> None:
        """Write the final receipt, then drop the pending entry, in that order:  a crash between
        the two leaves a pending file that replay removes, never a wake with no record."""
        receipt = self._receipt(entry, pick, status=status, delivered=delivered, result=result)
        # The outcome is kept on the entry first:  while this process lives only the receipt is
        # retried.  It reaches a restart only if this save lands.  If the save fails too and the
        # router dies before the receipt is written, the file still has its in-flight mark, so the
        # replay closes it as interrupted;  a delivery that ran is still never run again.
        entry["receipt"] = receipt
        entry.pop("in_flight", None)
        self._save(entry)
        self._write_receipt(entry)

    def _write_receipt(self, entry: dict[str, Any]) -> None:
        wake_id = entry["wake"]["wake_id"]
        receipt = entry["receipt"]
        try:
            atomic_write(self._done(wake_id), receipt)
        except OSError as exc:
            # The wake stays pending and tries to write its receipt again;  it is not lost.
            self._log("receipt-write-failed-%s" % type(exc).__name__, wake_id)
            with self._cond:
                entry["next_at"] = self.clock() + float(self.config["busy_retry_seconds"])
                entry["running"] = False
                self._cond.notify_all()
            return
        self._append_log(receipt)
        with self._cond:
            self._pending(wake_id).unlink(missing_ok=True)
            self._entries.pop(wake_id, None)
            self._cond.notify_all()
        self._log("%s %s" % (receipt["status"], receipt["reason"] or receipt["code"]), wake_id)

    def _append_log(self, receipt: Mapping[str, Any]) -> None:
        path = self.state / "wakes.jsonl"
        try:
            private_dir(path.parent)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(receipt, sort_keys=True) + "\n")
        except OSError as exc:
            self._log("log-write-failed-%s" % type(exc).__name__, str(receipt.get("wake_id")))

    @staticmethod
    def _log(code: str, wake_id: str) -> None:
        """Codes and wake ids only:  no payload, no names, no key."""
        sys.stderr.write("wake-router: %s %s\n" % (wake_id, code))


def _closed(row: Mapping[str, Any]) -> bool:
    return bool(row.get("retired_at") or row.get("closed_at") or row.get("state") == "closed")


def row_seat(row: Mapping[str, Any]) -> str:
    """The seat a registry row names, upper-cased, or "" when it names none or a malformed one."""
    raw = row.get("seat")
    if not isinstance(raw, str) or not raw.isascii():
        return ""
    tag = raw.upper()
    return tag if SEAT_RE.fullmatch(tag) else ""


def target_from_row(row: Mapping[str, Any], seat: str, target_re: Pattern[str]) -> Target | None:
    """The chat a registry row describes, or None if it is not one a wake can go to.  The row has
    to name this seat itself:  a row with no seat is not taken to be this platform's."""
    sid = str(row.get("native_session_id") or "")
    if not target_re.fullmatch(sid):
        return None  # a crafted row can never steer an argv token a child reads as a flag
    if not seat or row_seat(row) != seat:
        return None
    letter = str(row.get("label") or row.get("letter") or "")
    generation = row.get("generation")
    if not letter or type(generation) is not int or generation < 1:
        return None  # the check before delivery needs both, so it could never pass
    return Target(
        session_id=sid,
        letter=letter,
        host=str(row.get("verified_host") or ""),
        title=str(row.get("title") or ""),
        thread_link=str(row.get("thread_link") or ""),
        generation=generation,
    )


def holds(
    answer: Mapping[str, Any], target: Target, seat: str, target_re: Pattern[str]
) -> bool | None:
    """Whether a registry `resolve` answer still has this letter open on this session at this
    generation.  False only when the registry says it does not:  the letter has no live holder, its
    chat is closed, or a well-formed chat of this seat holds it at another session or generation.
    None when a live answer is not a well-formed chat of this seat (no seat, a malformed one,
    another seat's, or a bad id or generation), because a row that cannot be read proves no
    release.  An answer that is not a mapping at all is None too, and so is one whose `found` is not
    exactly True or False (missing, None, 0, 1, a string).  The caller has already ruled out an
    answer that is an error."""
    if not isinstance(answer, Mapping):
        return None
    found = answer.get("found")
    if found is False:
        return False  # only the boolean itself is the registry saying no;  a 0 or a None is not
    if found is not True:
        return None
    if _closed(answer):
        return False
    current = target_from_row(answer, seat, target_re)
    if current is None:
        return None
    return current.session_id == target.session_id and current.generation == target.generation


def _session_of(entry: Mapping[str, Any]) -> str:
    pick = entry.get("pick") or {}
    target = pick.get("target") or {}
    return str(target.get("session_id") or "")


def _order(entry: Mapping[str, Any]) -> tuple[float, str]:
    """Arrival order, with the id breaking a tie so every check agrees on who is first."""
    return entry["accepted_at"], entry["wake"]["wake_id"]


def _loadable(entry: object, wake_id: str) -> bool:
    """Whether a pending file holds a whole entry for this id, with every field a worker reads."""
    if not isinstance(entry, dict):
        return False
    try:
        if entry["wake"]["wake_id"] != wake_id:
            return False
        Wake(**entry["wake"])
        if entry["pick"] is not None:
            _pick_from(entry["pick"])
        return (
            isinstance(entry["receipt_id"], str)
            and type(entry["accepted_at"]) in (int, float)
            and type(entry["next_at"]) in (int, float)
            and type(entry["attempts"]) is int
            and type(entry["failures"]) is int
            and type(entry.get("repicks", 0)) is int
            and isinstance(entry.get("receipt"), (dict, type(None)))
        )
    except (KeyError, TypeError, ValueError):
        return False


def _pick_from(record: Mapping[str, Any]) -> Pick:
    known = {field.name for field in fields(Pick)}  # a file from an older router may have more
    values = {name: value for name, value in dict(record).items() if name in known}
    target = values.get("target")
    values["target"] = Target(**target) if target else None
    values["candidates"] = tuple(values.get("candidates") or ())
    return Pick(**values)


def _instant(value: object) -> float | None:
    """A registry time as epoch seconds, or None if it is not one.  Numbers, numeric strings and
    ISO strings with or without `Z` all occur;  a naive ISO string is UTC."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    except ValueError:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text[-1:] in "Zz" else text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.timestamp()
        except (ValueError, OverflowError, OSError):
            return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number / 1000 if number >= 1e11 else number  # milliseconds, from a JavaScript writer


def _bare_id(session_id: str) -> str:
    """A session id or a fragment of one as they are compared:  no `sess_` prefix, no hyphens."""
    return session_id.removeprefix(SESSION_PREFIX).replace("-", "")


def _is_sender(target: Target, wake: Wake) -> bool:
    bare = _bare_id(wake.sender_fragment)
    return bool(bare) and _bare_id(target.session_id).startswith(bare)


def _named_session(wake: Wake, live: Mapping[str, Target]) -> str:
    """The one live chat the wake's session-id fragments name, or "".  A fragment that fits several
    chats names none, two fragments that name different chats name none, and the sender's own chat
    is never the one named."""
    named: set[str] = set()
    for fragment in wake.session_fragments:
        bare = _bare_id(fragment)
        fits = [sid for sid in live if bare in _bare_id(sid)]
        if len(fits) == 1 and not _is_sender(live[fits[0]], wake):
            named.add(fits[0])
    return named.pop() if len(named) == 1 else ""


def _issues_of(row: Mapping[str, Any]) -> list[tuple[str, float | None]]:
    """A registry record's `issues`, as (id, seen_at).  A record may have none, and a malformed
    item is skipped."""
    value = row.get("issues")
    if not isinstance(value, list):
        return []
    found: list[tuple[str, float | None]] = []
    for item in value[:MAX_ROW_ISSUES]:
        issue = item.get("id") if isinstance(item, Mapping) else None
        if isinstance(issue, str) and ISSUE_RE.fullmatch(issue):
            found.append((issue, _instant(item.get("seen_at"))))
    return found


def _row_time(row: Mapping[str, Any]) -> float | None:
    for name in RECENCY_FIELDS:
        found = _instant(row.get(name))
        if found is not None:
            return found
    return None


def _most_recent(targets: list[Target], seen: Mapping[str, float | None]) -> Target:
    """The newest of these chats.  One with no usable time sorts last;  the id breaks a tie."""

    def key(target: Target) -> tuple[bool, float, str]:
        when = seen.get(target.session_id)
        return when is None, -(when or 0.0), target.session_id

    return min(targets, key=key)


def make_handler(router: Router) -> type[BaseHTTPRequestHandler]:
    seat = router.platform.seat
    wake_path = "/wake/%s" % seat
    limit = REQUEST_TIMEOUT

    class Handler(BaseHTTPRequestHandler):
        timeout = limit  # a client that goes silent cannot hold a thread

        def setup(self) -> None:
            super().setup()
            # The socket timeout restarts on every byte, so a client that dribbles its headers or
            # its body needs one deadline for the whole request.
            self._deadline = threading.Timer(limit, self._cut_off)
            self._deadline.daemon = True
            self._deadline.start()

        def finish(self) -> None:
            self._deadline.cancel()
            super().finish()

        def _cut_off(self) -> None:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        def log_message(self, fmt: str, *args: Any) -> None:  # the default prints paths
            pass

        def _answer(self, code: int, payload: Mapping[str, Any]) -> None:
            body = (json.dumps(payload) + "\n").encode("utf-8")
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                self.close_connection = True  # the client left, or the deadline cut it off

        def _authorized(self) -> bool:
            key = read_key(router.key_file)
            if not key:
                self._answer(503, {"error": "the routine key is missing or unreadable"})
                return False
            given = (self.headers.get("Authorization") or "").encode("utf-8")
            # Constant time:  the bearer is the only thing between the network and a wake.
            if not hmac.compare_digest(given, ("Bearer %s" % key).encode("utf-8")):
                self._answer(401, {"error": "bad bearer"})
                return False
            return True

        def do_GET(self) -> None:
            if self.path == "/health":
                self._answer(200, {"seat": seat, "healthy": True})
                return
            found = re.fullmatch(r"/receipt/([A-Za-z0-9][A-Za-z0-9._-]{0,127})", self.path or "")
            if not found:
                self._answer(404, {"error": "not found"})
                return
            if not self._authorized():
                return
            record = router.receipt(found.group(1))
            if record is None:
                self._answer(404, {"error": "no such wake"})
            else:
                self._answer(200, record)

        def do_POST(self) -> None:
            if not self._authorized():
                return
            if self.path != wake_path:
                self._answer(404, {"error": "not found"})
                return
            raw_length = (self.headers.get("Content-Length") or "").strip()
            if not raw_length.isdigit() or len(raw_length) > 9:
                self._answer(400, {"error": "Content-Length missing or malformed"})
                return
            length = int(raw_length)
            if length <= 0 or length > MAX_BODY:
                self._answer(413, {"error": "body size refused"})
                return
            raw_body = self.rfile.read(length)
            if len(raw_body) != length:
                self.close_connection = True  # cut off part way:  never act on part of a body
                return
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self._answer(400, {"error": "body is not JSON"})
                return
            if not isinstance(payload, dict) or payload.get("contract") != CONTRACT:
                self._answer(400, {"error": "unknown contract"})
                return
            if str(payload.get("seat") or "") != seat:
                self._answer(403, {"error": "seat mismatch"})
                return
            if not WAKE_ID_RE.fullmatch(str(payload.get("wake_id") or "")):
                self._answer(400, {"error": "wake_id missing or malformed"})
                return
            wake = wake_from_payload(payload, router.platform.mention)
            if payload.get("test") is True:
                self._answer(200, router.test_answer(wake))
                return
            status, answer = router.accept(wake)
            self._answer(status, answer)

    return Handler


def serve(router: Router, bind: str, port: int) -> int:
    """Run the router.  It refuses to start without its registry configuration or its key:  a
    router missing either would answer every wake and look healthy doing it.  The port is bound
    before anything is replayed, so a second instance stops there and touches no pending wake.
    SIGTERM closes the port, stops new work and waits for running deliveries."""
    if not registry_api.base_url(router.env):
        raise SystemExit("wake-router: the registry API URL is not set")
    if not read_key(router.key_file):
        raise SystemExit("wake-router: the routine key file is missing, short or group-readable")
    try:
        server = ThreadingHTTPServer((bind, port), make_handler(router))
    except OSError as exc:
        raise SystemExit(
            "wake-router: cannot bind %s:%d (%s)" % (bind, port, type(exc).__name__)
        ) from None
    server.daemon_threads = True

    def on_term(_signum: int, _frame: object) -> None:
        # shutdown() waits for serve_forever, which is this same thread, so it needs its own.
        threading.Thread(target=server.shutdown, daemon=True).start()

    previous = signal.signal(signal.SIGTERM, on_term)
    try:
        router.start()
        print(
            "wake-router listening on %s:%d for seat %s" % (bind, port, router.platform.seat),
            flush=True,
        )
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        wait = router.config.get("shutdown_wait_seconds", ROUTER_DEFAULTS["shutdown_wait_seconds"])
        if not router.stop(float(wait)):
            router._log("shutdown-left-deliveries-running", "-")
        signal.signal(signal.SIGTERM, previous)
    return 0
