#!/usr/bin/env python3
"""Zulip -> Linear relay.  One direction only: nothing in this module ever posts to Zulip.

Any message in a PUBLIC Zulip stream whose TOPIC TITLE or BODY names a Linear issue ID (AFC-123
and the other team keys) is mirrored onto that issue as ONE comment per (issue, topic), written by
a dedicated Linear app identity.  Later hits in the same topic edit that comment in place (every
post of the topic, each under its permalink), at most once per EDIT_INTERVAL seconds per
comment.  Once a topic has a standing
comment, every later message in the topic joins it, key or not (the whole-topic scope the owner
ruled on 2026-11-03, with the backfill in operator_linear.py's hosting).  Direct messages and private
streams are never relayed: the relay bot is a plain member that only joins public streams, and the
worker drops any message whose stream is not in the public set it loaded.  Until that set has
loaded, the worker relays, writes and drops nothing.

Owner decisions (Jay, 2026-10-21 and 2026-10-23): human and bot posts both count; Linear stays the
record; no #issues stream; comments are NOT posted as the Wise app but as a new dedicated relay
app; one updating comment per (issue, topic) with 10 minute batching; DMs and private streams are
excluded.

The comment carries the whole post, as readable text (AFC-847).  A Zulip message is converted
to Markdown (scripts/simple_tools/zulip_markdown.py:  Zulip's rendered HTML and its Markdown
source both become Markdown) and shown with the line structure it was sent with, up to
MESSAGE_CHARS characters a message and COMMENT_CHARS characters a comment.  A cut is always
said in the comment itself:  "N characters of this message are not shown" and "N characters of
this topic are not shown".  The credential scan runs over the converted text and not over the
markup, so a Zulip attribute cannot trip it by itself;  a link's URL stays in the text, so a
credential inside one is still caught.  A comment an older build wrote holds quotes that were
cut at 280 characters:  its quotes are read from Zulip again and the comment is edited with the
whole posts (the repair, in _refresh).

What this build does NOT claim (Echo's reviewer addendum, 2026-10-23):

  - NOT exactly-once.  The write-ahead ledger and the marker reconciliation narrow duplicates, they
    do not eliminate them.  A lost ledger file, an archive record dropped at its bound (below), a
    second relay process on another host, or an uncertain write outcome (a create that landed
    while its answer was lost) can each produce a second comment.  So can a topic rename or a
    stream move the worker never saw (one made while it was not running, or while its
    public-stream set was unloaded): the topic's entry, or its archived record, stays under the
    old name, and a mention under the new name starts afresh.  The relay reconciles what it can
    see and says so in the log.
  - Comment-edit permission under the Linear scopes `read` + `comments:create` is UNVERIFIED.  The
    relay is written for it; the sandbox test that proves it comes later, after Jay approves the
    app.

Operator hosting (2026-11-03):  the listener daemon can host this worker instead of the
standalone process (operator_linear.py).  Hosted, the worker is fed the messages the listener
already routes, writes under the Operator's own Linear credential, and is given a topic-history
reader:  when a topic's first mention creates an entry, the topic's earlier messages are pulled
into it, so the comment covers the whole topic and not only the thread from the mention on
(owner ruling, 2026-11-03:  the poster often did not know an issue existed).  The standalone
process passes no history reader, so it gets no backfill;  the whole-topic continuation above
applies to both.

The comment map outlives the ledger's bound (AFC-821).  The ledger keeps MAX_ENTRIES live entries
and prunes the oldest.  A pruned entry that has a comment, or a create whose outcome is unknown,
leaves a compact record in the ledger's `archive` table under the same key: the conversation's
first message id, the comment id, the pending flag and when it was last touched, and no topic
text, stream name, link or quote.  The next message in that topic, whether or not it names the
issue, restores the entry from that record with its ORIGINAL first message id, so the marker is
the old comment's own and the old comment is edited instead of a second one being created.  A
record whose create was never confirmed is looked up by that marker before anything is created,
and a comment Linear no longer has is recreated once, under that marker.  Hosted, the restored
entry is refilled from the topic's history first, as a new entry is; when that read fails, the
record stays in the archive and the old comment is left as it is, and a message at least
CATCH_UP_RETRY seconds later tries again (the messages in between are in the history it reads).
Standalone there is no history to read, so a restored entry restarts from the live message and
its comment is rewritten to list only what follows.  A rename that newly names the issue does
not restore its comment; the topic's next message does.  Archived records follow a whole-topic
rename or move, and are dropped with a stream that is not public, by the rules live entries
obey.  Still NOT guaranteed: a lost ledger file loses the archive with it; the archive is
bounded at MAX_ARCHIVE records and by the ledger's byte bound, its oldest records are dropped
first, and the log says how many; a partial move of some of a topic's messages to a private
stream cannot take them out of an archived comment, which lists no message, so that comment
keeps their links and quotes until the topic's next message restores and refills it; and the
build before this one reads a ledger that has an archive, keeps the table as it found it, and
neither adds to it nor restores from it.

Catch-up (AFC-821, hosted only).  The host replays only part of what it missed while it was
down (direct messages, mentions, owner posts, leased and followed topics), so a plain message
posted in the gap never reaches the worker; the same holds for the gap while its event queues
register again after an expiry, and for a message dropped while the public-stream set was
unloaded.  Given a second reader (history_since), the worker closes that gap itself: the first
message that reaches an entry after a restart, after the host reports a queue registered again
(reopen_catch_up), or after its topic lost a message to an unloaded set, reads the topic's
messages newer than the newest one the entry has seen, and records the missed ones before the
live one.  An entry is settled only by a read that reaches the live message: a read cut short
of it (at its MAX_BACKFILL cap, or by a reader that stopped early), or one that shows the live
message is not the topic's newest (the host replaying an older one), is continued from where it
stopped by the topic's next message, and the whole-topic read of a new or restored entry is
judged the same way.  A read that brings nothing new MAX_SHORT_READS times in a row (a live
message that left its topic after it was routed) settles the entry, in the log.  Each read is
taken at MAX_BACKFILL messages at most, and a history message with a text field over
MAX_HISTORY_FIELD_BYTES is skipped and counted as a malformed one is.  A read that fails is
logged and tried again by a message at least CATCH_UP_RETRY seconds later; the same message fed
again for another seat costs no second read.  Still NOT guaranteed: a topic is caught up only
when its next message arrives, so a topic that stays silent keeps its gap until then; the point
a failed read retries from lives in memory only, so a restart before a retry succeeds loses the
gap before the messages recorded meanwhile; a catch-up still open after MAX_BACKFILL further
messages gives up and says so in the log; a message moved into a public topic from a private
stream while the worker was not running is recorded like any other message the topic holds,
as the whole-topic backfill records it; and the standalone process passes no such reader, so it
catches nothing up.

Run (server instance, stdlib only, Python 3.11+):

    python3 -m simple_tools.linear_relay run        # long-running loop
    python3 -m simple_tools.linear_relay check      # credentials, public streams, Linear auth; no writes

Credentials (environment, loaded by the container from Infisical, never logged):

    ZULIP_RELAY_EMAIL, ZULIP_RELAY_API_KEY   a member (not admin) Zulip bot; site from ZULIP_SITE
    LINEAR_RELAY_TOKEN                       OAuth app token of the dedicated Linear relay app

The realm comes from SIMPLE_TOOLS_REALM (else the fleet realm); ZULIP_SITE must name the same host.

Optional: LINEAR_RELAY_STATE (ledger path), LINEAR_RELAY_TEAMS (comma list),
LINEAR_RELAY_DRY_RUN=1 (log what would be written, write nothing to Linear),
LINEAR_RELAY_CHECK_ISSUE (an issue id for the check command).

The module is also importable.  A daemon needs these entry points: RelayWorker.handle_event(event)
for message and update_message events; RelayWorker.start_cycle() before each poll batch and
RelayWorker.tick(now) after it, so the batch's writes share one budget; and
RelayWorker.set_public_streams(...) whenever a `stream` event arrives (None first, then the
reloaded id -> name map, as run() does).  The worker holds no Zulip client, so it cannot reload the
set itself, and until it is given one it relays and writes nothing.  A host that gives the worker
a catch-up reader also calls RelayWorker.reopen_catch_up() whenever one of its event queues
registers again, so the gap the old queue left is read.  Zulip text is untrusted data:
a field that carries anything credential-shaped is withheld whole, never partly redacted, and is
not logged; every other field is trimmed and escaped so it cannot break out of the comment.  It
never decides what the relay does beyond which issue ID to name.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import heapq
import http.client
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from . import secretscan, zulip_markdown
from .zulip import (
    ApiError,
    CredentialError,
    Credentials,
    EventQueue,
    NetworkError,
    QueueExpired,
    ZulipClient,
    _NoRedirect,
    realm_url,
    verify_realm,
)

# Keep in step with LINEAR_TEAMS in scripts/zulip_linkifiers.py (that script's --check compares it
# with Linear).  Override with LINEAR_RELAY_TEAMS.
DEFAULT_TEAMS = (
    "AFC",
    "CTS",
    "OPS",
    "SWU",
    "AR",
    "BF",
    "CC",
    "CL",
    "CT",
    "DD",
    "FL",
    "HH",
    "PS",
    "ST",
    "UM",
    "C",
)
EDIT_INTERVAL = 600  # seconds between edits of one comment (owner: 10 minute batching)
MAX_LINKS_SHOWN = 20
MAX_SEEN = 200  # message ids remembered per comment, for idempotency
MAX_BACKFILL = 1000  # topic messages pulled into a new entry (whole-topic scope, owner 2026-11-03)
CATCH_UP_RETRY = 60.0  # seconds before a history read that failed is tried again for an entry
MAX_SHORT_READS = 3  # reads in a row that bring nothing past where they began, then a catch-up ends
# One text field of a history message, in UTF-8 bytes.  Zulip refuses a message over 10,000
# characters, so a field past this bound is not one Zulip sent as it is.
MAX_HISTORY_FIELD_BYTES = 64 * 1024
MAX_ISSUES_PER_MESSAGE = 5
# The text of one message in the comment:  a post is shown whole, up to this many
# characters.  Zulip itself refuses a message over 10,000 characters, so this bound cuts
# no post Zulip sends, and the guard stays for a host that delivers more.
MESSAGE_CHARS = 10000
# The whole comment body.  The messages that do not fit in it are left out, and the
# comment says how many characters of the topic they were.  It is well inside what
# Linear takes for one comment.
COMMENT_CHARS = 50000
# The text one ledger entry holds.  It is a little more than a comment shows, so the
# comment's own bound is what the reader sees, and the ledger does not hold text no
# comment can show.
MAX_ENTRY_TEXT = 60000
# A label (the stream, the topic, the sender):  a name, not a post.
LABEL_CHARS = 280
# The render format of an entry's quotes.  An entry written by an older build carries no
# version, and its quotes are read from Zulip again before its comment is edited.
RENDER_VERSION = 2
# How many standing comments are re-rendered from Zulip in one cycle, and how long an
# entry whose topic could not be read waits before it is tried again.
REFRESH_PER_CYCLE = 1
REFRESH_RETRY = 900.0
LINEAR_URL = "https://api.linear.app/graphql"
USER_AGENT = "simple-tools-linear-relay/1 (fleet)"
MARKER = "<!-- zulip-relay -->"  # first line of every relay comment, so humans can spot them
LIVE_STATE = "~/.simple-tools/linear-relay.json"
DRY_STATE = "~/.simple-tools/linear-relay.dry-run.json"
LEDGER_VERSION = 2  # 2: the ledger key carries a digest of the topic, never the topic itself
MAX_ENTRIES = 500  # the ledger is bounded: the oldest entries are pruned
MAX_ARCHIVE = 20000  # what pruned entries leave behind (root, comment id) is bounded too
# The ledger holds the rendered text of every message it records, so it is larger than it
# was when a quote was cut at 280 characters:  the bound is raised with the text.
MAX_LEDGER_BYTES = 48 * 1024 * 1024
RETRY_BACKOFF = (1.0, 2.0, 4.0, 8.0)
MAX_ATTEMPTS = 5
REQUEST_TIMEOUT = 30.0  # one HTTP request
CALL_BUDGET = 60.0  # one Linear call, every retry included
SCAN_BUDGET = 90.0  # one marker scan, every page and every retry included
MAX_COMMENT_PAGES = 40  # 100 comments a page, so 4000 comments an issue
MISSING_TTL = 3600.0  # an issue id Linear does not have is looked up again after an hour
TICK_BUDGET = 120.0  # one tick's writes; the rest wait for the next cycle so Zulip keeps polling
STREAM_RELOAD_OPS = ("create", "update", "delete")  # stream-event ops that reload the public set
# What a field that looked like it carried a credential shows instead.  It names no secret
# keyword, so it never matches the patterns below itself.
WITHHELD = "(withheld: it looked like it carried a credential)"


# ------------------------------------------------------------------------------------------
# Issue ids, links, untrusted text
# ------------------------------------------------------------------------------------------
def build_regex(teams: tuple[str, ...]) -> re.Pattern[str]:
    # Longest key first so CC-1 is never read as C-1; \b stops ABC-12 matching C-12.
    keys = "|".join(re.escape(t) for t in sorted(set(teams), key=lambda t: (-len(t), t)))
    return re.compile(r"\b(?:%s)-\d{1,6}\b" % keys)


def find_issue_ids(regex: re.Pattern[str], *texts: str) -> list[str]:
    seen: list[str] = []
    for text in texts:
        for match in regex.findall(text or ""):
            ident = match.upper()
            if ident not in seen:
                seen.append(ident)
    return seen[:MAX_ISSUES_PER_MESSAGE]


def permalink(realm: str, mid: int) -> str:
    """The link to one Zulip message, carrying no topic text.

    The topic is untrusted and can hold a credential, so it never goes into a URL: a
    percent-encoded topic is still the topic, and a reader of the Linear comment could
    decode it.  Zulip's `near` narrow operator resolves a message id on its own, and the
    server answers that narrow with the message and the topic it lives in, so the link
    names the message and nothing else.
    """
    return "%s/#narrow/near/%d" % (realm.rstrip("/"), int(mid))


# Credential-shaped strings.  A Zulip field that matches any of them is withheld whole.
_SECRET_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{5,}"),
    re.compile(r"\b[0-9a-fA-F]{32,}\b"),
    re.compile(r"\b(?=[A-Za-z0-9+/]*[0-9])(?=[A-Za-z0-9+/]*[A-Za-z])[A-Za-z0-9+/]{40,}={0,2}\b"),
)
# A keyword counts only as an assignment (`password: x`, `token=x`), so prose such as "token
# refresh" or "password reset" is not withheld; a bare `Bearer` needs a token-shaped value.
_KEYED_SECRET = re.compile(
    r"(?i)\b(?:api[_-]?key|apikey|access[_-]?token|token|secret|password|passwd|pwd"
    r"|authorization|credential)\b\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
    r"|\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"
)
# The markdown that can change the shape of the comment: emphasis, code spans, links, html,
# tables, strikethrough and the quote delimiter.
_MD_SPECIALS = set('\\`*_[]<>|~!"')


def has_secret(text: str) -> bool:
    """True when the text carries anything credential-shaped: the fleet's canonical scanner
    (`secretscan`, which knows Linear, Sentry, Infisical, Cloudflare and the other service
    prefixes) plus the relay's own wider rows (long hex and base64 runs, keyed assignments)."""
    text = text or ""
    return (
        secretscan.scan(text) is not None
        or any(p.search(text) for p in _SECRET_PATTERNS)
        or bool(_KEYED_SECRET.search(text))
    )


def safe_text(text: str) -> str:
    """Untrusted Zulip text on one line, or WITHHELD when it carries anything credential-shaped.

    A hit withholds the whole field instead of cutting the match out of it: a pattern can find
    where a credential starts but not always where it ends (a passphrase after `password:` runs
    on past the first word), so a partial redaction can leak the rest.  This is the form the
    ledger stores and the relay renders; the raw text is never persisted, logged or relayed."""
    text = re.sub(r"\s+", " ", text or "").strip()
    return WITHHELD if has_secret(text) else text


def _escape_markdown(text: str) -> str:
    return "".join("\\" + ch if ch in _MD_SPECIALS else ch for ch in text)


def neutralise(text: str) -> str:
    """Trim, withhold and defang an untrusted Zulip label for a Linear comment.

    The result cannot open a link, start a code span, close the quoted string, or carry a
    credential into Linear.  The @ is kept but followed by a zero-width space, so a mention
    never becomes a live mention.  A label is a name, not a post:  a post is rendered by
    post_text, which keeps the line structure the label throws away."""
    text = _escape_markdown(safe_text(text))
    text = text.replace("@", "@\u200b")
    if len(text) > LABEL_CHARS:
        text = text[: LABEL_CHARS - 1] + "\u2026"
    return text


# A link opener in the converted text:  a bracket a link follows.  The brackets of the
# text around it are already escaped by the conversion.
_LINK_OPENER = re.compile(r"(?P<slashes>\\*)(?P<bracket>\[)(?=[^\]\n]*\]\()")


def _escape_link_openers(text: str) -> str:
    """Escape the bracket that opens a link, so the link is shown and never live.

    The backslashes in front of it decide how it reads:  an odd count escapes the
    bracket already, and an even count does not, so a bracket behind two backslashes is
    a live link again unless it is escaped here (Sentry, PR 2340)."""

    def replace(match: re.Match[str]) -> str:
        slashes = match.group("slashes")
        if len(slashes) % 2:
            return slashes + match.group("bracket")
        return slashes + "\\" + match.group("bracket")

    return _LINK_OPENER.sub(replace, text)


def defang(text: str) -> str:
    """Markdown that a Linear comment can hold:  its links are shown, not live, and an at
    sign never starts a live mention.

    Only the link openers are escaped.  The text around them is the post's own Markdown,
    and a reader should see it as the sender wrote it;  a link is shown as the text and
    the URL, never as a link that can be clicked.  A bracket that opens no link (one in a
    code span, say) is left alone."""
    text = _escape_link_openers(text)
    return text.replace("@", "@\u200b")


def post_text(content: str) -> str:
    """One Zulip post as the text of a Linear comment:  readable Markdown with its line
    structure kept, or the withheld placeholder when it carries anything
    credential-shaped.

    The post is converted first (Zulip's rendered HTML and its Markdown source both
    become Markdown) and the credential scan runs over the converted text, so a markup
    attribute can never trip it by itself.  A link's URL stays in the text, so a
    credential inside one is still caught.  A post longer than MESSAGE_CHARS is cut, and
    the cut says how many characters of it are not shown."""
    converted = zulip_markdown.to_markdown(content)
    if has_secret(converted):
        return WITHHELD
    text = defang(converted)
    if len(text) > MESSAGE_CHARS:
        left_out = len(text) - MESSAGE_CHARS
        # The cut must not leave a code fence open:  the rest of the comment would
        # render as code (Sentry, PR 2340).
        cut = zulip_markdown.close_code_fence(text[:MESSAGE_CHARS].rstrip())
        text = "%s\n\n_%d characters of this message are not shown._" % (cut, left_out)
    return text


def _field_is_secret(name: str, value: str) -> bool:
    """Whether one field of a Zulip message carries anything credential-shaped.

    A message's text is scanned as the text the comment shows:  the converted post, so a
    markup attribute cannot trip the scanner by itself, and a link's URL stays in the
    text, so a credential inside one is still caught.  The other fields are plain text
    and are scanned as they are."""
    if name == "content":
        value = zulip_markdown.to_markdown(value)
    return has_secret(value)


# ------------------------------------------------------------------------------------------
# Ledger
# ------------------------------------------------------------------------------------------
def topic_digest(topic: str) -> str:
    """The ledger's name for a topic.  The topic is untrusted and can carry a credential, so the
    ledger keys on a digest of it and never stores the raw title."""
    return hashlib.sha256((topic or "").encode("utf-8")).hexdigest()[:32]


def ledger_key(issue: str, stream_id: int, topic: str) -> str:
    return "%s|%d|%s" % (issue, stream_id, topic_digest(topic))


def split_key(key: str) -> tuple[str, int, str]:
    """(issue, stream id, topic digest)."""
    issue, sid, digest = key.split("|", 2)
    return issue, int(sid), digest


def marker_for(issue: str, root_mid: int) -> str:
    """The first line of a relay comment: the marker plus a key that identifies the conversation.

    The key is the issue and the id of the FIRST message recorded for the topic, not the topic
    name: a topic rename moves the conversation, and the key has to survive that so the relay
    keeps editing the same comment instead of starting a second one."""
    return "%s %s|%d" % (MARKER, issue, root_mid)


def carries_marker(body: Any, marker: str) -> bool:
    """True when one line of the body is exactly the marker.  A substring test would let the
    marker of root 100 match the comment of root 1000."""
    if not isinstance(body, str):
        return False
    return any(line.strip() == marker for line in body.splitlines())


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_finite(value: Any) -> bool:
    """A number that can stand for a time: not NaN, not an infinity, and not an integer too large
    for a float, which would make the archive's oldest-first ordering raise."""
    if not _is_number(value):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _valid_link(item: Any) -> bool:
    return (
        isinstance(item, dict)
        and _is_int(item.get("mid"))
        and isinstance(item.get("sender"), str)
        and isinstance(item.get("when"), str)
        and isinstance(item.get("quote"), str)
    )


def valid_entry(key: Any, entry: Any) -> bool:
    """The whole shape of one ledger entry: every field, its type, the list elements, the bounds,
    and a key that agrees with the entry."""
    if not isinstance(key, str) or not isinstance(entry, dict):
        return False
    try:
        issue, sid, digest = split_key(key)
    except ValueError:
        return False
    return (
        isinstance(entry.get("issue"), str)
        and entry["issue"] == issue
        and _is_int(entry.get("stream_id"))
        and entry["stream_id"] == sid
        and re.fullmatch(r"[0-9a-f]{32}", digest) is not None
        and isinstance(entry.get("stream"), str)
        and isinstance(entry.get("topic"), str)
        and _is_int(entry.get("root"))
        and entry.get("marker") == marker_for(issue, entry["root"])
        and (entry.get("comment_id") is None or isinstance(entry.get("comment_id"), str))
        and isinstance(entry.get("links"), list)
        and len(entry["links"]) <= MAX_LINKS_SHOWN
        and all(_valid_link(item) for item in entry["links"])
        and isinstance(entry.get("seen"), list)
        and len(entry["seen"]) <= MAX_SEEN
        and all(_is_int(mid) for mid in entry["seen"])
        and _is_int(entry.get("count"))
        and entry["count"] >= 0
        and _is_number(entry.get("last_edit"))
        and _is_number(entry.get("last_touch"))
        and isinstance(entry.get("dirty"), bool)
        and isinstance(entry.get("pending"), bool)
        and _is_int(entry.get("failures"))
        and entry["failures"] >= 0
        and _is_number(entry.get("retry_after"))
        and (entry.get("format") is None or _is_int(entry.get("format")))
        and (entry.get("refreshed") is None or isinstance(entry.get("refreshed"), str))
    )


def valid_archived(key: Any, record: Any) -> bool:
    """The whole shape of one archive record: a ledger key exactly as ledger_key writes it, and
    the four fields a pruned entry leaves behind (its root message id, its comment id, its
    pending flag, its last touch), each of them present.  The key is held to its written form
    because the archive is looked up by key: a key that only parses to the same stream id would
    never be found.  The last touch must be a finite time, because the bounds order the archive
    by it."""
    if not isinstance(key, str) or not isinstance(record, dict):
        return False
    try:
        issue, sid, digest = split_key(key)
    except ValueError:
        return False
    comment_id = record.get("comment_id")
    return (
        key == "%s|%d|%s" % (issue, sid, digest)
        and re.fullmatch(r"[0-9a-f]{32}", digest) is not None
        and _is_int(record.get("root"))
        and "comment_id" in record
        and (comment_id is None or (isinstance(comment_id, str) and comment_id != ""))
        and isinstance(record.get("pending"), bool)
        and _is_finite(record.get("at"))
    )


def _member_bytes(key: str, value: Any) -> int:
    """The bytes one member of the ledger's entries or archive table takes in the file
    Ledger._render writes, its separator included: its key and ": ", its value as json.dumps
    renders it alone, the two more spaces each of the value's lines is indented by inside its
    table, and the comma, newline and two spaces between it and its neighbour.  A table's last
    member takes one byte more.  The file is ASCII (json.dumps escapes the rest), so its
    characters are its bytes."""
    text = json.dumps(value, indent=1, sort_keys=True)
    return len(json.dumps(key)) + 2 + len(text) + 2 * text.count("\n") + 4


class Ledger:
    """JSON file: key "ISSUE|stream_id|topic digest" -> comment id, links, timing.  Atomic,
    owner-only writes under an flock so a restart never double-comments.

    The live table is bounded (MAX_ENTRIES), and the comment map outlives that bound (AFC-821):
    a pruned entry that has a comment, or a create whose outcome is unknown, leaves a compact
    record in the optional `archive` table under the same key: {"root", "comment_id", "pending",
    "at"}, and no topic text, stream name, link or quote.  The archive is bounded as well
    (MAX_ARCHIVE, and the file's byte bound); `archive_dropped` counts the records those bounds
    cost, each one a standing comment the relay can no longer find.  A full archive renders to
    about 3.7 MB, a little under half of MAX_LEDGER_BYTES, so from then on the byte bound rather
    than MAX_ENTRIES can be what limits the live table, and an entry it retires pushes the
    oldest archive record out."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {"version": LEDGER_VERSION, "entries": {}}
        self._lock_fh: Any = None
        self.pruned = 0
        self.archive_dropped = 0
        # stream id -> topic digest -> the archived keys of that topic.  Built at open and kept
        # in step by archive_put and archive_pop, so finding one topic's archived issues, or one
        # stream's archived records, never scans the whole archive.
        self._archive_index: dict[int, dict[str, set[str]]] = {}
        self.dirty = False

    def open(self) -> None:
        parent = self.path.parent
        if parent.is_symlink():
            raise SystemExit("refusing a symlinked relay state directory: %s" % parent)
        # A new state directory is owner-only; an existing one keeps the mode it has.
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._lock_fh = open(str(self.path) + ".lock", "w")
        try:
            fcntl.flock(self._lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise SystemExit("another linear_relay already holds %s" % self.path)
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text("utf-8"))
            except (OSError, ValueError) as exc:
                # Fail closed: a ledger that cannot be read is unknown state, and a fresh one
                # would stack a second comment on every existing topic.
                raise SystemExit(
                    "the relay ledger %s is unreadable (%s); move it aside to start over, and know "
                    "that a fresh ledger can stack duplicate comments"
                    % (self.path, type(exc).__name__)
                ) from None
        self._validate()
        self._index_archive()

    def _validate(self) -> None:
        """Fail closed on a ledger whose shape is wrong, before any entry is read or written."""
        if not isinstance(self.data, dict):
            raise SystemExit("the relay ledger %s must contain a JSON object" % self.path)
        if self.data.get("version") != LEDGER_VERSION:
            raise SystemExit(
                "the relay ledger %s is not a version %d ledger; move it aside to start over"
                % (self.path, LEDGER_VERSION)
            )
        entries = self.data.get("entries")
        if not isinstance(entries, dict):
            raise SystemExit("the relay ledger %s has no entries table" % self.path)
        for number, (key, entry) in enumerate(entries.items()):
            if not valid_entry(key, entry):
                # The entry's position, never its key or fields: those name the issue and
                # stream, and nothing from the file needs to reach the log.
                raise SystemExit(
                    "the relay ledger %s has a malformed entry (number %d); move the file aside "
                    "to start over" % (self.path, number + 1)
                )
        if "archive" not in self.data:
            return  # optional: a ledger written before the archive existed opens as it always did
        archive = self.data["archive"]
        if not isinstance(archive, dict):
            raise SystemExit(
                "the relay ledger %s has an archive that is not a table; move the file aside to "
                "start over" % self.path
            )
        for number, (key, record) in enumerate(archive.items()):
            if not valid_archived(key, record):
                # As for an entry: the record's position, never its key or fields.
                raise SystemExit(
                    "the relay ledger %s has a malformed archive record (number %d); move the "
                    "file aside to start over" % (self.path, number + 1)
                )

    def mark_dirty(self) -> None:
        """Ask the next tick() to write the file, so one cycle costs one write."""
        self.dirty = True

    def close(self) -> None:
        """Release the ledger flock.  Entries dirtied since the last save are saved
        first (every save is atomic and fsynced), so closing loses nothing;  a host
        that rebuilds its worker on a credential change closes the old ledger before
        opening the new one."""
        fh, self._lock_fh = self._lock_fh, None
        if fh is not None:
            try:
                if self.dirty:
                    self.save()
            finally:
                fh.close()

    def save(self) -> None:
        text = self._render()
        tmp = self.path.with_suffix(".tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(str(tmp), flags, 0o600)
        try:
            # A temp file a crash left behind keeps its old mode under O_CREAT, so the mode is
            # set before a byte is written.
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fd = -1
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
        finally:
            if fd >= 0:
                os.close(fd)
        os.replace(tmp, self.path)
        # The rename is durable only once the directory entry is.
        dir_fd = os.open(str(self.path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        self.dirty = False

    def _render(self) -> str:
        """The file's text, within MAX_LEDGER_BYTES.  Over the bound, the oldest entries leave
        for the archive, where each record is a fraction of its entry's size, so the file still
        shrinks; with no entry left, the oldest archive records go.  Each round drops, oldest
        first, as many as the bytes they free say the excess needs, and renders the whole
        ledger once, where it used to render it again for every one dropped (Kody, PR 2298).
        What a drop frees is never counted under what the file loses (_member_bytes), so a
        round stops at or before the first drop that brings the file under the bound, and the
        same ones go as when they were dropped one at a time."""
        self._prune_entries()
        self._prune_archive()
        text = json.dumps(self.data, indent=1, sort_keys=True)
        size = len(text.encode("utf-8"))
        while size > MAX_LEDGER_BYTES:
            if self.data["entries"]:
                self._shed_entries(size - MAX_LEDGER_BYTES)
            elif self.archive:
                self._shed_archived(size - MAX_LEDGER_BYTES)  # the map itself gives way
            else:
                break
            text = json.dumps(self.data, indent=1, sort_keys=True)
            size = len(text.encode("utf-8"))
        return text

    def _prune_entries(self) -> None:
        entries = self.data["entries"]
        excess = len(entries) - MAX_ENTRIES
        if excess <= 0:
            return
        order = sorted(entries.items(), key=lambda kv: float(kv[1].get("last_touch", 0.0)))
        for key, _ in order[:excess]:
            self._retire(key)

    def _shed_entries(self, excess: int) -> None:
        """Retire the oldest entries, oldest first, until the bytes they free cover `excess`:
        each entry's own and those of the archive records it replaces or pushes past
        MAX_ARCHIVE, less those of the record it leaves.  A member that goes is counted with
        a byte to spare, and the record left with none, so the count is never under what the
        file loses; the render that follows says whether another round is needed."""
        entries = self.data["entries"]
        order = sorted(entries.items(), key=lambda kv: float(kv[1].get("last_touch", 0.0)))
        freed = 0
        for key, entry in order:
            if freed >= excess:
                return
            filed = "%s|%d|%s" % split_key(key)  # the key _retire files the record under
            held = self.archive.get(filed)
            self._retire(key)
            pushed = [(name, record) for name, record in self._prune_archive() if name != filed]
            left = self.archive.get(filed)
            freed += _member_bytes(key, entry) + 1
            freed += sum(_member_bytes(name, record) + 1 for name, record in pushed)
            if held is not None:
                freed += _member_bytes(filed, held) + 1
            if left is not None:
                freed -= _member_bytes(filed, left)

    def _retire(self, key: str) -> None:
        """Take one entry out of the live table to keep the ledger bounded.  An entry that has
        a comment, or a create whose outcome is unknown, leaves a compact record in the
        archive, so a later message in its topic finds that comment again instead of starting
        a second one.  An entry with neither has no comment to find, and is simply dropped."""
        entry = self.data["entries"].pop(key, None)
        if entry is None:
            return
        self.pruned += 1
        comment_id = entry.get("comment_id") or None
        pending = entry.get("pending") is True
        if comment_id is None and not pending:
            return
        record = {
            "root": entry["root"],
            "comment_id": comment_id,
            "pending": pending,
            "at": entry["last_touch"],
        }
        # Filed under the key as ledger_key writes it, which is the form the archive is looked
        # up by and the only form it accepts when the file is read back.
        self.archive_put("%s|%d|%s" % split_key(key), record)

    def _prune_archive(self) -> list[tuple[str, dict[str, Any]]]:
        """Drop the oldest archive records past MAX_ARCHIVE, and answer them, oldest first.
        heapq.nsmallest is sorted()[:excess], ties included, at a cost that follows the excess:
        an entry retired into a full archive pushes one record out, and the archive is not
        sorted whole for it."""
        excess = len(self.archive) - MAX_ARCHIVE
        if excess <= 0:
            return []
        oldest = heapq.nsmallest(
            excess, self.archive.items(), key=lambda kv: float(kv[1].get("at", 0.0))
        )
        for key, _ in oldest:
            self.archive_pop(key)
            self.archive_dropped += 1
        return oldest

    def _shed_archived(self, excess: int) -> None:
        """With no entry left, drop the oldest archive records until the bytes they free cover
        `excess`, counted as _shed_entries counts them."""
        order = sorted(self.archive.items(), key=lambda kv: float(kv[1].get("at", 0.0)))
        freed = 0
        for key, record in order:
            if freed >= excess:
                return
            freed += _member_bytes(key, record) + 1
            self.archive_pop(key)
            self.archive_dropped += 1

    @property
    def entries(self) -> dict[str, Any]:
        return self.data["entries"]

    # -- the archive -----------------------------------------------------------------------
    @property
    def archive(self) -> dict[str, Any]:
        """The archived records, by ledger key.  For reading: archive_put and archive_pop are
        the only writers, records and keys alike, because they keep the index in step with the
        table."""
        return self.data.get("archive") or {}

    def _index_archive(self) -> None:
        self._archive_index = {}
        for key in self.archive:
            _issue, sid, digest = split_key(key)
            self._archive_index.setdefault(sid, {}).setdefault(digest, set()).add(key)

    def archive_put(self, key: str, record: dict[str, Any]) -> None:
        _issue, sid, digest = split_key(key)
        self.data.setdefault("archive", {})[key] = record
        self._archive_index.setdefault(sid, {}).setdefault(digest, set()).add(key)

    def archive_pop(self, key: str) -> dict[str, Any] | None:
        """Take one record out of the archive; None when the archive has none for the key."""
        table = self.data.get("archive")
        record = table.pop(key, None) if table else None
        if record is None:
            return None
        if not table:
            del self.data["archive"]  # an empty archive is no table, as before the first prune
        _issue, sid, digest = split_key(key)
        topics = self._archive_index.get(sid, {})
        keys = topics.get(digest, set())
        keys.discard(key)
        if not keys:
            topics.pop(digest, None)
        if not topics:
            self._archive_index.pop(sid, None)
        return record

    def archived_for(self, stream_id: int, digest: str) -> list[str]:
        """The archived keys of one topic (stream id, topic digest), one per issue."""
        return sorted(self._archive_index.get(stream_id, {}).get(digest, ()))

    def archived_streams(self) -> list[int]:
        """The ids of the streams the archive holds a record for."""
        return list(self._archive_index)

    def archived_in_stream(self, stream_id: int) -> list[str]:
        """Every archived key of one stream."""
        topics = self._archive_index.get(stream_id, {})
        return sorted(key for keys in topics.values() for key in keys)


def new_entry(issue: str, stream_id: int, stream: str, topic: str, root_mid: int) -> dict[str, Any]:
    return {
        "issue": issue,
        "stream": safe_text(stream),
        "stream_id": stream_id,
        "topic": safe_text(topic),
        "root": root_mid,
        "marker": marker_for(issue, root_mid),
        "comment_id": None,
        "links": [],
        "seen": [],
        "count": 0,
        "last_edit": 0.0,
        "last_touch": 0.0,
        "dirty": False,
        "pending": False,
        "failures": 0,
        "retry_after": 0.0,
        # The render format of the quotes below.  An entry written by an older build has
        # none, and its quotes are read from Zulip again before its comment is edited.
        "format": RENDER_VERSION,
        # When the quotes were last re-rendered from Zulip, for the comment to say so.
        "refreshed": None,
    }


# ------------------------------------------------------------------------------------------
# Linear
# ------------------------------------------------------------------------------------------
class LinearError(Exception):
    """A Linear call failed.  The message carries only allow-listed fields (an HTTP status,
    Linear's error codes, a fixed description) and never the response body, so it can be
    logged as it is."""


class LinearNotFound(LinearError):
    """The issue or comment is not there.  Never retried: the answer is final."""


class LinearAuthError(LinearError):
    """The token was refused.  Never retried: no amount of waiting changes it."""


class LinearPermanentError(LinearError):
    """Linear refused the request for a reason that waiting does not change (forbidden, invalid
    input, a definite `success: false` on a create).  Never retried as it is."""


class LinearRejected(LinearError):
    """Linear answered an edit with `success: false`.  The comment may still be there, so this is
    never read as "the comment is gone"."""


class LinearUncertain(LinearError):
    """A create whose outcome is unknown: it may have landed.  It is never sent again blind; the
    next attempt reads the issue's comments for the marker first."""


class LinearRateLimited(LinearError):
    """Linear rate limited the request (HTTP 429 or RATELIMITED).  The request was not carried
    out, so it is a definite answer even for a create: retried later, never read as unknown."""


# Matched against Linear's codes and types after underscores become spaces, so INVALID_INPUT and
# "invalid input" are the same hint.
_PERMANENT_HINTS = (
    "forbidden",
    "invalid input",
    "input error",
    "user error",
    "validation",
    "bad user input",
    "feature not accessible",
    "usage limit",
)


def _error_codes(errors: list[Any]) -> list[str]:
    """Linear's error codes and types, cleaned to a short allow-listed form for the log."""
    codes: list[str] = []
    for err in errors:
        ext = err.get("extensions") if isinstance(err, dict) else None
        if not isinstance(ext, dict):
            continue
        for field in ("code", "type"):
            value = ext.get(field)
            if isinstance(value, str):
                clean = re.sub(r"[^A-Za-z0-9 _-]", "", value)[:40]
                if clean and clean not in codes:
                    codes.append(clean)
    return codes[:4]


def classify_errors(errors: list[Any]) -> LinearError:
    """The exception for a GraphQL `errors` array.  The messages are read to classify and never
    copied into the exception."""
    messages = [str(e.get("message") or "").strip().lower() for e in errors if isinstance(e, dict)]
    codes = _error_codes(errors)
    low = " ".join(codes).lower().replace("_", " ")
    label = ", ".join(codes) or "no code"
    if "authentication" in low or any(m.startswith("authentication") for m in messages):
        return LinearAuthError("linear refused LINEAR_RELAY_TOKEN (%s)" % label)
    if any(m.startswith("entity not found") for m in messages):
        return LinearNotFound("linear: not found (%s)" % label)
    if any(hint in low for hint in _PERMANENT_HINTS) or any(
        m.startswith("forbidden") for m in messages
    ):
        return LinearPermanentError("linear refused the request (%s)" % label)
    if "ratelimited" in low or "rate limited" in low:
        return LinearRateLimited("linear rate limited the request (%s)" % label)
    return LinearError("linear graphql error (%s)" % label)


def _retryable(exc: LinearError) -> bool:
    return not isinstance(exc, (LinearNotFound, LinearAuthError, LinearPermanentError))


class LinearClient:
    def __init__(
        self,
        token: str,
        *,
        dry_run: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        opener: Any = None,
        auth_scheme: str = "bearer",
    ) -> None:
        # "bearer" sends `Authorization: Bearer <token>` (an OAuth access token, the
        # standalone relay's form);  "api_key" sends the token bare, Linear's form for
        # a personal API key (the Operator-hosted engine's developer token).
        if auth_scheme not in ("bearer", "api_key"):
            raise ValueError("unknown Linear auth scheme %r" % (auth_scheme,))
        self._token = token
        self.auth_scheme = auth_scheme
        self.dry_run = dry_run
        self.sleep = sleep
        self.monotonic = monotonic
        # Never follow a redirect: urllib would re-send the bearer token to the new host.
        self._opener = opener or urllib.request.build_opener(_NoRedirect())

    def _http_error(self, exc: urllib.error.HTTPError) -> LinearError:
        status = exc.code
        try:
            raw = exc.read()
        except (OSError, http.client.HTTPException):
            raw = b""
        finally:
            exc.close()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = None
        errors = payload.get("errors") if isinstance(payload, dict) else None
        if isinstance(errors, list) and errors:
            return classify_errors(errors)
        if status == 401:
            return LinearAuthError("linear refused LINEAR_RELAY_TOKEN (http 401)")
        if status == 403:
            # A valid token can still be refused one write (an edit the app may not make), so a
            # 403 refuses this request, not the token: only a 401 or an authentication error
            # stops the relay.
            return LinearPermanentError("linear refused the request (http 403)")
        if status == 404:
            return LinearNotFound("linear: not found (http 404)")
        if status == 429:
            return LinearRateLimited("linear rate limited the request (http 429)")
        if status >= 500:
            return LinearError("linear http %d" % status)
        return LinearPermanentError("linear http %d" % status)

    def _gql(
        self,
        query: str,
        variables: Mapping[str, Any],
        *,
        idempotent: bool = True,
        deadline: float | None = None,
    ) -> dict[str, Any]:
        """One GraphQL call.  Reads and edits retry a transient failure with backoff, all inside
        one deadline.  A create (idempotent=False) is sent once: a timeout, a network error, a 5xx,
        an unreadable answer or an unmapped GraphQL error may mean it landed, so it raises
        LinearUncertain and the worker reads the issue for its marker before it ever creates
        again.  A rate limit is the one transient answer that is definite, so it is raised as it
        is."""
        if deadline is None:
            deadline = self.monotonic() + CALL_BUDGET
        body = json.dumps({"query": query, "variables": variables}).encode()
        # Invariant: commentCreate is the only mutation a replay could duplicate, so it is the only
        # call sent with idempotent=False; commentUpdate sets the whole body, so a replay converges.
        attempts = MAX_ATTEMPTS if idempotent else 1
        last: LinearError = LinearError("linear call ran out of its time budget")
        for attempt in range(attempts):
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                last = LinearError("linear call ran out of its time budget")
                break
            req = urllib.request.Request(
                LINEAR_URL,
                data=body,
                method="POST",
                headers={
                    "Authorization": (
                        self._token if self.auth_scheme == "api_key" else "Bearer " + self._token
                    ),
                    "Content-Type": "application/json",
                    "User-Agent": USER_AGENT,
                },
            )
            try:
                with self._opener.open(req, timeout=min(REQUEST_TIMEOUT, remaining)) as resp:
                    raw = resp.read()
            except urllib.error.HTTPError as exc:
                last = self._http_error(exc)
            except (urllib.error.URLError, OSError, http.client.HTTPException) as exc:
                last = LinearError("linear network error: %s" % type(exc).__name__)
            else:
                last = self._answer(raw)
                if not isinstance(last, LinearError):
                    return last
            if isinstance(last, LinearAuthError):
                raise last
            if not _retryable(last):
                raise last
            if not idempotent:
                if isinstance(last, LinearRateLimited):
                    raise last  # a rate limit is a definite "not done", even for a create
                raise LinearUncertain("linear create outcome unknown (%s)" % last) from None
            if attempt < attempts - 1:
                pause = RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)]
                self.sleep(min(pause, max(0.0, deadline - self.monotonic())))
        # The retries are spent, or the deadline passed before a request went out.  A create
        # never gets here after sending: it raised LinearUncertain above.
        raise last

    @staticmethod
    def _answer(raw: bytes) -> dict[str, Any] | LinearError:
        """The `data` object of a 200 answer, or the error it amounts to."""
        try:
            out = json.loads(raw)
        except ValueError:
            # A proxy or gateway can answer 200 with a non-JSON body.
            return LinearError("linear answered with a non-JSON body")
        if not isinstance(out, dict):
            return LinearError("linear answered with a non-object body")
        errors = out.get("errors")
        if isinstance(errors, list) and errors:
            return classify_errors(errors)
        data = out.get("data")
        if not isinstance(data, dict):
            return LinearError("linear answered without a data object")
        return data

    def viewer(self) -> str:
        """Who the token acts as.  A read that proves the token works without needing an issue."""
        data = self._gql("query{viewer{id name}}", {})
        viewer = data.get("viewer")
        if not isinstance(viewer, dict):
            raise LinearError("linear answered without a viewer")
        return str(viewer.get("name") or viewer.get("id") or "unknown")

    def issue_id(self, identifier: str) -> str:
        data = self._gql("query($id:String!){issue(id:$id){id identifier}}", {"id": identifier})
        issue = data.get("issue")
        if not isinstance(issue, dict):
            raise LinearNotFound("linear: not found (no issue)")
        uuid = issue.get("id")
        if not isinstance(uuid, str) or not uuid:
            raise LinearError("linear answered an issue without an id")
        return uuid

    def find_comment(self, issue_uuid: str, marker: str) -> str | None:
        """The id of the comment that carries this marker, or None once every page has been read.

        Pages through the issue's comments: this is how a create that landed while its answer
        was lost is adopted instead of being stacked.  The whole scan shares one deadline.  A
        scan that cannot finish (a malformed page, a bad cursor, the page cap, the deadline)
        raises LinearError instead of answering None, because None means "create one"."""
        deadline = self.monotonic() + SCAN_BUDGET
        after: str | None = None
        for _ in range(MAX_COMMENT_PAGES):
            data = self._gql(
                "query($id:String!,$after:String){issue(id:$id){comments(first:100,after:$after)"
                "{nodes{id body} pageInfo{hasNextPage endCursor}}}}",
                {"id": issue_uuid, "after": after},
                deadline=deadline,
            )
            issue = data.get("issue")
            if not isinstance(issue, dict):
                raise LinearNotFound("linear: not found (no issue)")
            connection = issue.get("comments")
            nodes = connection.get("nodes") if isinstance(connection, dict) else None
            page = connection.get("pageInfo") if isinstance(connection, dict) else None
            if not isinstance(nodes, list) or not isinstance(page, dict):
                raise LinearError("linear answered a malformed comments page")
            for node in nodes:
                if isinstance(node, dict) and carries_marker(node.get("body"), marker):
                    cid = node.get("id")
                    if isinstance(cid, str) and cid:
                        return cid
            has_next = page.get("hasNextPage")
            if has_next is False:
                return None
            if has_next is not True:
                raise LinearError("linear answered a comments page without hasNextPage")
            cursor = page.get("endCursor")
            if not isinstance(cursor, str) or not cursor:
                raise LinearError("linear returned an invalid comment cursor")
            after = cursor
        raise LinearError("the comment scan reached %d pages without the end" % MAX_COMMENT_PAGES)

    def create_comment(self, issue_uuid: str, body: str) -> str:
        if self.dry_run:
            raise LinearError("create_comment must not be called in a dry run")
        data = self._gql(
            "mutation($i:CommentCreateInput!){commentCreate(input:$i){success comment{id}}}",
            {"i": {"issueId": issue_uuid, "body": body}},
            idempotent=False,
        )
        res = data.get("commentCreate")
        if not isinstance(res, dict):
            raise LinearUncertain("linear create outcome unknown (no commentCreate result)")
        if res.get("success") is False:
            raise LinearPermanentError("linear refused the create (success false)")
        comment = res.get("comment")
        cid = comment.get("id") if isinstance(comment, dict) else None
        if not isinstance(cid, str) or not cid:
            raise LinearUncertain("linear create outcome unknown (no comment id)")
        return cid

    def update_comment(self, comment_id: str, body: str) -> None:
        if self.dry_run:
            raise LinearError("update_comment must not be called in a dry run")
        data = self._gql(
            "mutation($id:String!,$i:CommentUpdateInput!){commentUpdate(id:$id,input:$i){success}}",
            {"id": comment_id, "i": {"body": body}},
        )
        res = data.get("commentUpdate")
        if not isinstance(res, dict):
            raise LinearError("linear answered without a commentUpdate result")
        if res.get("success") is not True:
            raise LinearRejected("linear refused the edit (success false)")


# ------------------------------------------------------------------------------------------
# Worker
# ------------------------------------------------------------------------------------------
def _message_block(item: Mapping[str, Any], realm: str) -> str:
    """One message of the comment:  its permalink and its sender, then its text.

    The link is built here, from the message id, so no text that came out of Zulip can
    reach it.  The sender is a label, escaped whole.  A message the relay never read (one
    that came in by a topic rename) has no text, and its block shows none."""
    head = "**[%s](%s) \u2014 %s**" % (
        item["when"],
        permalink(realm, int(item["mid"])),
        neutralise(item["sender"]),
    )
    if not item["quote"]:
        return head
    return "%s\n\n%s" % (head, item["quote"])


def render_comment(entry: Mapping[str, Any], realm: str, byline: str = "the relay") -> str:
    """The body of the relay comment:  the marker, the heading, every recorded message as
    its own block, and the footer.

    The body is bounded and nothing is ever cut silently.  A message whose text is longer
    than MESSAGE_CHARS was cut when it was stored, and it says so;  the messages that do
    not fit in COMMENT_CHARS are left out here, and the comment says how many characters
    of the topic they were."""
    links = entry["links"][-MAX_LINKS_SHOWN:]
    earlier = max(0, int(entry["count"]) - len(links))
    heading = "**Zulip discussion:** #%s > %s" % (
        neutralise(entry["stream"]),
        neutralise(entry["topic"]),
    )
    lines = [entry["marker"], heading, ""]
    if entry.get("refreshed"):
        lines += ["_Re-rendered from Zulip on %s._" % entry["refreshed"], ""]
    if earlier > 0:
        lines += ["_%d earlier message(s) not listed._" % earlier, ""]
    # What the messages share of the body:  the marker, the heading, the notes and the
    # footer are outside it, and the note about what was left out has room in it.
    budget = COMMENT_CHARS - len("\n".join(lines)) - 200
    blocks: list[str] = []
    used = 0
    left_out = 0
    for item in reversed(links):
        block = _message_block(item, realm)
        if blocks and used + len(block) > budget:
            # The newest messages are the ones that fit:  what is left out is the oldest.
            left_out += len(item["quote"])
            continue
        blocks.append(block)
        used += len(block)
    blocks.reverse()
    if left_out:
        lines += ["_%d characters of this topic are not shown._" % left_out, ""]
    for block in blocks:
        lines += [block, ""]
    # Rendered copy: the sentence gap is a no-break space plus a space.
    lines += ["_Mirrored one way from Zulip by %s.\u00a0 Edit in Zulip, not here._" % byline]
    return "\n".join(lines)


def _stop_on_auth(exc: LinearAuthError) -> SystemExit:
    """The relay's exit on a refused token, naming the variable without key-derived text."""
    return SystemExit("%s; the relay stops rather than retry a token that cannot work" % exc)


class _CatchUp:
    """An entry whose catch-up is still open (AFC-821), in memory only.  What its topic got after
    `start` is unknown to the entry, except the ids in `recorded`: the next read starts there.
    `short` counts the reads in a row that brought nothing past where they began (_judge)."""

    __slots__ = ("recorded", "short", "start")

    def __init__(self, start: int, recorded: Iterable[int] = (), short: int = 0) -> None:
        self.start = int(start)
        # The ids after `start` the entry has recorded since, so a later read never counts one
        # twice, even once it has left the entry's bounded `seen` list.
        self.recorded: set[int] = {int(mid) for mid in recorded if int(mid) > self.start}
        self.short = int(short)


# The text fields of a history message the worker reads, besides its id and its timestamp.
_HISTORY_TEXT_FIELDS = ("sender_email", "sender_full_name", "content")
# The latest timestamp a history message may carry: the last second of 9999 UTC, well inside
# what time.gmtime renders.  A larger one (or NaN, an infinity) made the append raise.
MAX_TIMESTAMP = 253402300799


def _too_long(text: str) -> bool:
    """True when one text field of a history message is over MAX_HISTORY_FIELD_BYTES in UTF-8.
    The length in characters is looked at first, so a field far past the bound is refused
    without being encoded; a lone surrogate, which JSON can carry, is measured, never raised on."""
    if len(text) > MAX_HISTORY_FIELD_BYTES:
        return True
    return len(text.encode("utf-8", "surrogatepass")) > MAX_HISTORY_FIELD_BYTES


def _checked_message(message: Any) -> dict[str, Any] | None:
    """One message of a history read, rebuilt from the fields the worker reads, each checked
    against the shape Zulip's messages API gives it, or None when any is missing or of the wrong
    type: id an integer, sender_email, sender_full_name and content strings of at most
    MAX_HISTORY_FIELD_BYTES each, and timestamp whole seconds since the epoch, no later than
    MAX_TIMESTAMP.  The size bound comes before any scan of the text, so an oversized field
    costs no credential scan, no quote and no ledger space (Kody, PR 2298)."""
    if not isinstance(message, Mapping) or not _is_int(message.get("id")):
        return None
    if not all(isinstance(message.get(name), str) for name in _HISTORY_TEXT_FIELDS):
        return None
    if any(_too_long(message[name]) for name in _HISTORY_TEXT_FIELDS):
        return None
    stamp = message.get("timestamp")
    if not _is_int(stamp) or not 0 <= stamp <= MAX_TIMESTAMP:
        return None
    checked = {name: message[name] for name in _HISTORY_TEXT_FIELDS}
    return {"id": int(message["id"]), "timestamp": int(stamp), **checked}


def _by_id(messages: Any) -> tuple[list[dict[str, Any]], int, int]:
    """The messages a history reader returned, once each by id, oldest first, how many of them
    were malformed, and how many past the read's bound were left for a later read (AFC-821).

    An answer that is not a list (or a tuple) fails the read with ValueError, None and an
    empty mapping or string included: read as no messages, any of them would settle a
    catch-up with its gap still open, or restore an archived entry without its history and
    rewrite its standing comment down to the live message.  Each message is checked
    (_checked_message) and rebuilt from its checked fields, so nothing downstream reads a field
    that was not checked.

    A malformed message is skipped and counted, not failed: a failed read is tried again from
    the same point, so one message that never parses would hold its topic's catch-up open, or
    keep an archived comment from being restored, for good.  One whose id is good keeps its
    place as {"id": id, "malformed": True}, which _append_history never records, so the read
    still shows how far it went and _judge still sees a read that stopped at its cap.  One
    without an integer id has no place in the topic and is left out.

    A read is taken at MAX_BACKFILL items at most, the most a reader is ever asked for (Kody, PR
    2298): the items past it are not looked at, and are counted, not failed, for the reason a
    malformed message is.  Readers answer oldest first, so the items kept are the oldest, and
    _judge has the topic's next message read on from the newest of them."""
    if not isinstance(messages, (list, tuple)):
        raise ValueError("a topic history read must answer a list of messages")
    over = max(0, len(messages) - MAX_BACKFILL)
    found: dict[int, dict[str, Any]] = {}
    malformed = 0
    for message in messages[:MAX_BACKFILL]:
        checked = _checked_message(message)
        if checked is None:
            malformed += 1
            if not (isinstance(message, Mapping) and _is_int(message.get("id"))):
                continue
            checked = {"id": int(message["id"]), "malformed": True}
        found.setdefault(checked["id"], checked)
    return [found[mid] for mid in sorted(found)], malformed, over


class RelayWorker:
    def __init__(
        self,
        zulip_realm: str,
        own_email: str,
        linear: LinearClient,
        ledger: Ledger,
        regex: re.Pattern[str],
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        log: Callable[[str], None] | None = None,
        public_streams: Mapping[int, str] | Iterable[int] | None = None,
        history: Callable[[int, str], list[Mapping[str, Any]]] | None = None,
        history_since: Callable[[int, str, int], list[Mapping[str, Any]]] | None = None,
        byline: str = "the relay",
    ) -> None:
        self.realm = zulip_realm
        self.own_email = own_email.lower()
        self.linear = linear
        self.ledger = ledger
        self.regex = regex
        self.clock = clock  # wall time, for the timestamps the ledger keeps
        self.monotonic = monotonic  # for the cycle budget, which a clock step must not move
        self.log = log or (lambda m: print(m, file=sys.stderr, flush=True))
        # A topic-history reader, given by the Operator hosting (operator_linear.py): called
        # with (stream id, topic) when a first mention creates an entry, or a message restores
        # an archived one, so the topic's earlier messages join the comment.  Each reader
        # answers a list (anything else fails the read: _by_id) or raises.  None in the
        # standalone process: no backfill there.
        self.history = history
        # A second reader, given by the same hosting: called with (stream id, topic, after id)
        # for a topic's messages newer than that id, oldest first.  It is how an entry catches
        # up on what its topic got while the worker was not receiving it (_catch_up).  None in
        # the standalone process: no catch-up there.
        self.history_since = history_since
        # The catch-up's state, in memory only.  _caught_up: the ledger keys whose entries hold
        # their topic up to the newest message this worker has handled for it.  _catching: the
        # keys whose catch-up is open, and where the next read starts.  _read_retry: for a key
        # whose history read failed, the monotonic time before which it is not read for again.
        # A key in none of them (an entry read from the ledger at start) opens its catch-up at
        # its topic's next message.
        self._caught_up: set[str] = set()
        self._catching: dict[str, _CatchUp] = {}
        self._read_retry: dict[str, float] = {}
        # For a key whose topic could not be read to re-render its comment, the monotonic
        # time before which it is not read for again.
        self._refresh_retry: dict[str, float] = {}
        self._archive_drops_logged = 0  # how much of ledger.archive_dropped the log has said
        # Whose name the comment footer carries: "the relay" standalone, "the Operator" hosted.
        # The marker line itself never changes, so a hosted worker adopts the comments the
        # standalone relay created instead of stacking new ones.
        self.byline = byline
        # None until the public-stream set has loaded: nothing is known about any stream yet.
        self.public_streams: set[int] | None = None
        self.stream_names: dict[int, str] = {}
        self.set_public_streams(public_streams)
        self._uuid_cache: dict[str, str | None] = {}
        self._missing_issues: dict[str, float] = {}  # issue id -> when to look it up again
        # The end of the current poll cycle's write budget (start_cycle), or None outside one.
        self._cycle_deadline: float | None = None

    def start_cycle(self) -> None:
        """Open one poll cycle's write budget.  Every write that starts in the cycle, the creates
        made while handling a message included, starts before this deadline or waits, dirty, for
        the next cycle, so a slow Linear cannot keep the Zulip poll loop from polling."""
        self._cycle_deadline = self.monotonic() + TICK_BUDGET

    def _budget_spent(self) -> bool:
        return self._cycle_deadline is not None and self.monotonic() > self._cycle_deadline

    def set_public_streams(self, streams: Mapping[int, str] | Iterable[int] | None) -> None:
        """Load the public-stream set (a stream id -> name map, or bare ids), or None to forget
        it.  An empty set is a loaded set: a realm with no public stream relays nothing."""
        if streams is None:
            self.public_streams = None
            self.stream_names = {}
            return
        if isinstance(streams, Mapping):
            self.public_streams = {int(sid) for sid in streams}
            self.stream_names = {int(sid): str(name) for sid, name in streams.items() if name}
        else:
            self.public_streams = {int(sid) for sid in streams}
            self.stream_names = {}

    def reopen_catch_up(self) -> None:
        """Forget which entries are caught up (AFC-821).  The host calls this when one of its
        event queues registers again: what the realm got before the new queue existed may never
        reach the worker (the host's own backfill replays only part of it), so the next message
        of each topic reads from the newest message its entry holds, as after a restart.  A
        catch-up still open keeps its point, which is still true.  The cost is one read per
        topic, at the topic's next message."""
        self._caught_up.clear()

    # -- the public-stream guard -------------------------------------------------------------
    def _is_public(self, sid: Any) -> bool | None:
        """True or False once the public-stream set has loaded; None before, when nothing is
        known.  A stream that is not in a loaded set is private, and never reaches Linear."""
        if self.public_streams is None:
            return None
        try:
            return int(sid) in self.public_streams
        except (TypeError, ValueError):
            return False

    def _drop_if_private(self, key: str, entry: Mapping[str, Any]) -> bool:
        """Remove an entry whose stream is known to be private now.  True when it was dropped.
        An unloaded set proves nothing about a stream, so nothing is dropped on that account."""
        if self._is_public(entry.get("stream_id")) is not False:
            return False
        self.ledger.entries.pop(key, None)
        self.ledger.mark_dirty()
        self.log(
            "dropped the relay entry for %s: its stream is no longer public" % self._label(entry)
        )
        return True

    def _may_write(self, key: str, entry: Mapping[str, Any]) -> bool:
        """The gate every write passes.  Unloaded set: defer (write nothing, drop nothing, the
        entry keeps its state).  Loaded, stream absent: drop.  Loaded, stream present: write."""
        state = self._is_public(entry.get("stream_id"))
        if state is None:
            return False
        if state is False:
            self._drop_if_private(key, entry)
            return False
        return True

    def _drop_private_archive(self) -> None:
        """The archive's side of _drop_if_private.  An archived record obeys the rule its live
        entry would: once its stream is known to be private, the record is dropped, so the
        relay never restores an entry for it, and never edits its comment again, even if the
        stream is made public later.  An unloaded set proves nothing, so nothing is dropped."""
        if self.public_streams is None:
            return
        for sid in self.ledger.archived_streams():
            if sid in self.public_streams:
                continue
            keys = self.ledger.archived_in_stream(sid)
            for key in keys:
                self.ledger.archive_pop(key)
            self.ledger.mark_dirty()
            self.log(
                "dropped %d archived relay record(s): their stream is no longer public" % len(keys)
            )

    @staticmethod
    def _label(entry: Mapping[str, Any]) -> str:
        """How the log names an entry: the issue and the conversation's first message id, never
        topic text, which is untrusted and can carry a credential."""
        return "%s (conversation %s)" % (entry.get("issue", "?"), entry.get("root", "?"))

    def _archived_label(self, key: str, record: Mapping[str, Any]) -> str:
        """How the log names an archived record: as _label names the entry it came from."""
        return self._label({"issue": split_key(key)[0], "root": record.get("root", "?")})

    # -- events ----------------------------------------------------------------------------
    def handle_event(self, event: Mapping[str, Any]) -> None:
        kind = event.get("type")
        if kind == "message":
            self._handle_message(event.get("message") or {})
        elif kind == "update_message":
            self._handle_move(event)
        elif kind == "stream" and event.get("op") in STREAM_RELOAD_OPS:
            # The worker holds no Zulip client, so it cannot reload the set itself.  run() never
            # routes a stream event here; a daemon that does has missed set_public_streams.  Only
            # the ops run() reloads on are named, so the advice matches what run() does.
            self.log("a stream event reached handle_event: reload the set with set_public_streams")

    def _handle_message(self, msg: Mapping[str, Any]) -> None:
        if msg.get("type") != "stream":
            return  # DMs are never relayed
        sid = msg.get("stream_id")
        if sid is None or self._is_public(sid) is not True:
            # A private stream the bot was added to, or a stream set that has not loaded yet:
            # the relay only ever handles known public streams, so this message is dropped.
            if sid is not None and self._is_public(sid) is None:
                # Unloaded, the stream may well be public, and then this message is a gap in
                # its topic's entries: the topic's next message reads for it (AFC-821).
                self._reopen_topic(sid, str(msg.get("subject", "")))
            return
        if str(msg.get("sender_email", "")).lower() == self.own_email:
            return
        topic = str(msg.get("subject", ""))
        content = str(msg.get("content", ""))
        sender = str(msg.get("sender_full_name", "someone"))
        ids = find_issue_ids(self.regex, topic, content)
        # Whole-topic scope (owner ruling, 2026-11-03):  once a topic has a standing comment
        # for an issue, every later message in the topic belongs to that comment, whether or
        # not it repeats the key, so an edit reflects the whole current thread and not only
        # its mentioning messages.  A topic with no standing entry still records nothing
        # until a real mention creates one.
        for _key, standing in self._entries_for(int(sid), topic):
            if standing["issue"] not in ids:
                ids.append(standing["issue"])
        # A standing comment whose entry was pruned still stands: its issue is named by the
        # archive, so this message restores the entry and updates that comment (AFC-821).
        for key in self.ledger.archived_for(int(sid), topic_digest(topic)):
            issue = split_key(key)[0]
            if issue not in ids:
                ids.append(issue)
        if not ids:
            return
        self._log_withheld(
            int(msg.get("id", 0)), (("topic", topic), ("content", content), ("sender", sender))
        )
        self._record(
            ids,
            int(sid),
            str(msg.get("display_recipient", "")),
            topic,
            int(msg.get("id", 0)),
            sender,
            content,
        )

    def _log_withheld(self, mid: int, fields: Iterable[tuple[str, str]]) -> None:
        """Say which fields of one message carry credential-shaped text, and so show WITHHELD in
        the comment (_append withholds each such field whole, on the live path and the history
        path alike).  The field names and the message id only: the text never reaches the log."""
        withheld = [name for name, value in fields if _field_is_secret(name, value)]
        if withheld:
            self.log(
                "message %d: withheld %s (credential-shaped text)" % (mid, ", ".join(withheld))
            )

    def _stream_name(self, sid: int, event: Mapping[str, Any] | None = None) -> str | None:
        """A stream's display name from the loaded set, by id.  An update_message event's
        `stream_name` names the PRE-edit stream, so it is used only when the message did not
        change stream.  None when neither source names it: the name is never guessed."""
        name = self.stream_names.get(sid)
        if name:
            return name
        if (
            event is not None
            and event.get("new_stream_id") is None
            and event.get("stream_id") is not None
            and int(event["stream_id"]) == sid
            and event.get("stream_name")
        ):
            return str(event["stream_name"])
        return None

    def _handle_move(self, event: Mapping[str, Any]) -> None:
        """A topic rename or a stream move (Zulip's update_message event).

        Per the get-events schema: `stream_id` is the PRE-edit stream and `new_stream_id` the
        POST-edit one (present only when the messages changed stream); `orig_subject` is the
        PRE-edit topic and `subject` the POST-edit one (absent when the topic did not change);
        `propagate_mode` is change_one, change_later or change_all, and `message_ids` lists only
        the messages that moved.  A content-only edit carries none of these and is ignored.

        Both ends must be public before anything moves or is recorded: nothing that lived in a
        private stream is ever named in Linear, whatever stream it moves to.  Only change_all
        moves the standing comment.  A partial move leaves the old topic's comment standing;
        when the moved messages went to a private stream, they also leave its list.

        An archived record (the comment of a pruned entry) obeys the same rules, each where
        the live rule is implemented: _migrate_topic carries it, _close_topic drops it, and a
        partial move leaves it with the old topic.  A rename that newly names an issue whose
        comment stands archived restores nothing: the topic's next message restores it."""
        if event.get("stream_id") is None:
            return  # not a stream message
        old_topic = event.get("orig_subject", event.get("orig_topic"))
        if old_topic is None:
            return  # nothing moved: a content-only edit
        old_sid = int(event["stream_id"])
        new_sid_raw = event.get("new_stream_id")
        new_sid = int(new_sid_raw) if new_sid_raw is not None else old_sid
        old_topic = str(old_topic)
        new_topic_raw = event.get("subject", event.get("topic"))
        new_topic = str(new_topic_raw) if new_topic_raw else old_topic
        if new_sid == old_sid and new_topic == old_topic:
            return
        old_public = self._is_public(old_sid)
        new_public = self._is_public(new_sid)
        if old_public is not True:
            # A private (or not yet known) source: nothing of it was mirrored, and nothing of it
            # may be named now, so a private -> public move records nothing.
            return
        moved = [int(m) for m in event.get("message_ids") or [] if _is_int(m)]
        if not moved and _is_int(event.get("message_id")):
            moved = [int(event["message_id"])]  # the oldest message moved, per the schema
        whole = event.get("propagate_mode") == "change_all"
        if new_public is not True:
            # public -> private: the moved messages now live where the relay may not look.
            if whole:
                self._close_topic(old_sid, old_topic)
            else:
                self._detach_messages(old_sid, old_topic, moved)
            return
        stream = self._stream_name(new_sid, event)
        if whole:
            self._migrate_topic(old_sid, new_sid, stream, old_topic, new_topic)
        fresh = [
            i
            for i in find_issue_ids(self.regex, new_topic)
            if i not in find_issue_ids(self.regex, old_topic)
            # A comment that stands archived for the topic waits for the topic's next message,
            # which restores it with the topic's whole history.  Restored here, by the first
            # moved message, it would have no history before that message, and its edit would
            # cut the comment down to one bullet.
            and ledger_key(i, new_sid, new_topic) not in self.ledger.archive
        ]
        if not fresh or not moved:
            return
        self._record(
            fresh, new_sid, stream or str(new_sid), new_topic, moved[0], "topic rename", ""
        )

    def _entries_for(self, stream_id: int, topic: str) -> list[tuple[str, dict[str, Any]]]:
        digest = topic_digest(topic)
        out = []
        for key, entry in list(self.ledger.entries.items()):
            _, entry_sid, entry_digest = split_key(key)
            if entry_sid == stream_id and entry_digest == digest:
                out.append((key, entry))
        return out

    def _reopen_topic(self, stream_id: Any, topic: str) -> None:
        """A message of this topic was dropped while the public-stream set was unloaded: the
        topic's entries are no longer caught up, so its next message reads for what was lost.
        Only memory changes; nothing is recorded, written or dropped."""
        try:
            sid = int(stream_id)
        except (TypeError, ValueError):
            return
        for key, _entry in self._entries_for(sid, topic):
            self._caught_up.discard(key)

    def _close_topic(self, stream_id: int, topic: str) -> None:
        """The whole topic left for a private stream: its entries leave the ledger, so the
        standing comment is never edited again and no link to the moved messages is added."""
        for key, entry in self._entries_for(stream_id, topic):
            self.ledger.entries.pop(key, None)
            self.ledger.mark_dirty()
            self.log(
                "closed the relay entry for %s: the topic moved to a stream that is not public"
                % self._label(entry)
            )
        # An archived record obeys the same rule: the topic left for a stream that is not
        # public, so its record leaves the archive, and no later message restores the entry
        # or edits the comment.
        for key in self.ledger.archived_for(stream_id, topic_digest(topic)):
            record = self.ledger.archive_pop(key)
            if record is None:
                continue
            self.ledger.mark_dirty()
            self.log(
                "closed the archived relay record for %s: the topic moved to a stream that is "
                "not public" % self._archived_label(key, record)
            )

    def _detach_messages(self, stream_id: int, topic: str, moved: list[int]) -> None:
        """Some messages left the topic for a private stream (change_one or change_later): the
        topic's comment stays standing, and the moved messages leave its list at the next edit,
        so it stops linking to messages that now live where the relay may not look.

        An archived record stays with the old topic too, as the live entry does.  It lists no
        message, so nothing can be taken out of it here, and its comment is not edited until the
        topic's next message restores it: until then the comment keeps the moved messages'
        links and quotes, as a pruned topic's comment kept everything before AFC-821.  The
        entry restored from it is refilled from the topic's history, which no longer holds the
        moved messages, so its first edit drops them."""
        if not moved:
            return
        gone = set(moved)
        for _key, entry in self._entries_for(stream_id, topic):
            left = [item for item in entry["links"] if int(item["mid"]) not in gone]
            removed = sum(1 for mid in entry["seen"] if mid in gone)
            if not removed and len(left) == len(entry["links"]):
                continue
            entry["links"] = left
            entry["count"] = max(len(left), int(entry["count"]) - removed)
            entry["dirty"] = True
            entry["last_touch"] = self.clock()
            self.ledger.mark_dirty()

    def _migrate_topic(
        self,
        old_sid: int,
        new_sid: int,
        stream: str | None,
        old_topic: str,
        new_topic: str,
    ) -> None:
        """The whole topic moved (change_all), to a new name, a new stream, or both.  Its entries
        are re-keyed from the OLD stream id and topic to the NEW ones, so the standing comment
        moves with the conversation instead of a second one starting."""
        if self._is_public(old_sid) is not True or self._is_public(new_sid) is not True:
            # The move path already requires both ends public; the guard is repeated here so a
            # migration can never carry a private topic's comment, however it is reached.
            return
        if old_sid == new_sid and old_topic == new_topic:
            return
        now = self.clock()
        moved = 0
        for key, entry in self._entries_for(old_sid, old_topic):
            issue = entry["issue"]
            new_key = ledger_key(issue, new_sid, new_topic)
            if new_key == key:
                continue
            target = self.ledger.entries.get(new_key)
            if target is None:
                del self.ledger.entries[key]
                if stream is not None:
                    entry["stream"] = safe_text(stream)
                elif new_sid != old_sid:
                    entry["stream"] = str(new_sid)  # an unnamed stream is shown by its id
                entry["stream_id"] = new_sid
                entry["topic"] = safe_text(new_topic)
                entry["dirty"] = True
                entry["last_touch"] = now
                self.ledger.entries[new_key] = entry
                self._carry_catch_up(key, new_key)
                # One record per (issue, stream, topic): the destination topic can have an
                # archived record of its own for this issue.  Here the mover stands, where a
                # live target would stand and take the mover in (below), and that difference
                # is deliberate: the record holds no message while the mover holds them, so
                # the mover's comment carries on with its list whole.  Letting the record stand
                # would take a history read inside the move, or an edit that cuts its comment
                # down to the mover's messages.  Either way one comment is left as it was,
                # because the app cannot delete one; here it is the archived record's.
                stale = self.ledger.archive_pop(new_key)
                if stale is not None:
                    self.log(
                        "merged the archived %s into %s; its older comment is left as it was"
                        % (self._archived_label(new_key, stale), self._label(entry))
                    )
            else:
                # The survivor now holds both conversations, so it is complete only as far as
                # the less complete of the two: its catch-up goes on from the earlier point.
                # The points are taken before the lists merge, since an unknown point is read
                # from an entry's own `seen`.
                points = [
                    point
                    for point in (self._point(new_key, target), self._point(key, entry))
                    if point is not None
                ]
                recorded = self._recorded(new_key) | self._recorded(key)
                target["seen"] = sorted(set(target["seen"]) | set(entry["seen"]))[-MAX_SEEN:]
                target["links"] = (target["links"] + entry["links"])[-MAX_LINKS_SHOWN:]
                # Two conversations become one: a message belongs to one topic, so the counts add.
                target["count"] = int(target["count"]) + int(entry["count"])
                target["dirty"] = True
                target["last_touch"] = now
                del self.ledger.entries[key]
                self._forget_catch_up(key)
                if points:
                    self._open(new_key, min(points), recorded | set(target["seen"]))
                else:
                    self._mark_caught_up(new_key)
                if entry.get("comment_id"):
                    # The app holds no delete scope, so the moved topic's own comment stays as
                    # it was; the surviving comment now lists both topics' messages.
                    self.log(
                        "merged %s into %s; its older comment is left as it was"
                        % (self._label(entry), self._label(target))
                    )
            moved += 1
        if moved:
            self.ledger.mark_dirty()
            self.log("carried %d standing comment(s) over a topic move" % moved)
        self._migrate_archived(old_sid, new_sid, old_topic, new_topic, now)

    def _carry_catch_up(self, old_key: str, new_key: str) -> None:
        """What is known about how far an entry is caught up belongs to the entry, so it moves
        with it to the entry's new key, and nothing a former holder of that key left applies.
        A whole-topic move keeps every message id, so the point still holds."""
        caught_up = old_key in self._caught_up
        state = self._catching.get(old_key)
        retry = self._read_retry.get(old_key)
        self._forget_catch_up(old_key)
        self._forget_catch_up(new_key)
        if caught_up:
            self._caught_up.add(new_key)
        if state is not None:
            self._catching[new_key] = state
        if retry is not None:
            self._read_retry[new_key] = retry

    def _point(self, key: str, entry: Mapping[str, Any]) -> int | None:
        """How far an entry is known to hold its topic: None when it is caught up (or nothing
        says where its history stops), else the message id its next catch-up read starts
        after, which for an entry not yet looked at is the newest message it has seen."""
        if key in self._caught_up:
            return None
        state = self._catching.get(key)
        if state is not None:
            return state.start
        return max(entry["seen"]) if entry["seen"] else None

    def _recorded(self, key: str) -> set[int]:
        state = self._catching.get(key)
        return set(state.recorded) if state is not None else set()

    def _migrate_archived(
        self, old_sid: int, new_sid: int, old_topic: str, new_topic: str, now: float
    ) -> None:
        """The archive's side of a whole-topic move (_migrate_topic has checked that both ends
        are public).  An archived record obeys the rule its live entry would: it is re-keyed
        to the new stream id and topic digest, so the comment of a pruned topic is still found
        under the topic's new name or in its new stream.

        One record survives per (issue, stream, topic).  A live entry at the destination
        stands, as a live target does in _migrate_topic.  Between two archived records the
        destination's stands, for the same reason: it is the topic the conversation now lives
        in.  (A live entry that moves onto an archived record stands over it instead, for the
        reason given in _migrate_topic: a live entry stands over a record, and between two of
        a kind the destination stands.)  The record that goes names a comment the app cannot
        delete, so that comment is left as it was."""
        carried = 0
        for key in self.ledger.archived_for(old_sid, topic_digest(old_topic)):
            new_key = ledger_key(split_key(key)[0], new_sid, new_topic)
            if new_key == key:
                continue
            record = self.ledger.archive_pop(key)
            if record is None:
                continue
            self._read_retry.pop(key, None)
            self.ledger.mark_dirty()
            moved = self._archived_label(key, record)
            target = self.ledger.entries.get(new_key)
            standing = self.ledger.archive.get(new_key)
            if target is not None:
                self.log(
                    "merged the archived %s into %s; its older comment is left as it was"
                    % (moved, self._label(target))
                )
            elif standing is not None:
                self.ledger.archive_put(new_key, dict(standing, at=now))
                self.log(
                    "merged the archived %s into the archived %s; its older comment is left as "
                    "it was" % (moved, self._archived_label(new_key, standing))
                )
            else:
                record["at"] = now
                self.ledger.archive_put(new_key, record)
                carried += 1
        if carried:
            self.log("carried %d archived comment record(s) over a topic move" % carried)

    def _append(
        self, entry: dict[str, Any], mid: int, sender: str, content: str, when: float
    ) -> bool:
        """Record one message on an entry.  False when the message is already there: a replay
        (the same message routed for several seats, or delivered twice) counts once."""
        if mid in entry["seen"]:
            return False
        entry["seen"] = (entry["seen"] + [mid])[-MAX_SEEN:]
        entry["count"] = int(entry["count"]) + 1
        entry["links"].append(
            {
                "mid": mid,
                "sender": safe_text(sender),
                "when": time.strftime("%b %d %H:%M UTC", time.gmtime(when)),
                "quote": post_text(content),
            }
        )
        entry["links"] = entry["links"][-MAX_LINKS_SHOWN:]
        # The text an entry holds is bounded as well:  the oldest messages leave the list
        # when the entry holds more text than a comment can show (the count of the
        # messages that are not listed says so).
        held = sum(len(item["quote"]) for item in entry["links"])
        while len(entry["links"]) > 1 and held > MAX_ENTRY_TEXT:
            held -= len(entry["links"][0]["quote"])
            entry["links"].pop(0)
        entry["dirty"] = True
        entry["last_touch"] = self.clock()
        return True

    def _read_topic(
        self, stream_id: int, topic: str, cache: dict[tuple[int, str], Any]
    ) -> tuple[list[Mapping[str, Any]] | None, str]:
        """The whole topic from the history reader, oldest first, read once per message for
        every entry the message makes or restores (the caller's cache).  (messages, "") when the
        read succeeded, (None, the error's type name) when it failed, and (None, "") when there
        is no reader to read with (the standalone process)."""
        if self.history is None:
            return None, ""
        cache_key = (stream_id, topic)
        if cache_key not in cache:
            try:
                read, malformed, over = _by_id(self.history(stream_id, topic))
            except Exception as exc:  # noqa: BLE001 - history must never stop a record
                cache[cache_key] = (None, type(exc).__name__)
            else:
                cache[cache_key] = (read, "")
                self._log_malformed(malformed, over)
        return cache[cache_key]

    def _log_malformed(self, count: int, over: int = 0) -> None:
        """Say how many malformed messages a history read answered, and how many past its bound
        it left for a later read (_by_id): the counts alone, since their fields are unchecked
        Zulip text."""
        if count:
            self.log(
                "a topic history read answered %d malformed message(s); they are left out of "
                "the comment" % count
            )
        if over:
            self.log(
                "a topic history read answered %d message(s) past the %d it takes; a later read "
                "goes on from the newest one taken" % (over, MAX_BACKFILL)
            )

    def _enter(
        self,
        key: str,
        issue: str,
        stream_id: int,
        stream: str,
        topic: str,
        mid: int,
        cache: dict[tuple[int, str], Any],
    ) -> dict[str, Any] | None:
        """The entry the message `mid` makes for a key that has none: restored from the archive
        when the topic's comment stands there (AFC-821), brand new otherwise.  Either way the
        topic's messages older than `mid` are pulled into it first, so the comment covers the
        whole topic and not only the thread from the message on (owner ruling, 2026-11-03).
        Only older messages are taken (later ones arrive live), the writer's own posts are
        skipped as on the live path, and one read serves every entry the message makes.

        A create is never held for history: a brand-new entry whose read fails starts at `mid`,
        and its catch-up starts at the topic's beginning, so a later message fills in the rest.
        A restore is held: an entry restored without its history would rewrite the standing
        comment down to the live message.  So when the read fails, now or a moment ago, the
        record stays archived, the comment stays as it is, and None is returned; a message at
        least CATCH_UP_RETRY seconds later tries again, and this one is in the history it reads.
        The standalone process has no reader, so it restores at once, from `mid` on."""
        archived = key in self.ledger.archive
        if (
            archived
            and self.history is not None
            and self.monotonic() < self._read_retry.get(key, 0.0)
        ):
            return None  # its read failed a moment ago
        read, error = self._read_topic(stream_id, topic, cache)
        if error:
            self._read_retry[key] = self.monotonic() + CATCH_UP_RETRY
        entry = None
        if archived:
            if error:
                self.log(
                    "topic history for %s could not be read (%s); its archived comment is left "
                    "as it is until a later message in the topic"
                    % (self._archived_label(key, self.ledger.archive[key]), error)
                )
                return None
            entry = self._restore(key, issue, stream_id, stream, topic)
        if entry is None:
            entry = new_entry(issue, stream_id, stream, topic, mid)
            self.ledger.entries[key] = entry
            if error:
                self.log(
                    "topic history for %s could not be read (%s); its comment starts at the "
                    "first mention%s"
                    % (
                        self._label(entry),
                        error,
                        "; a later message in the topic fills in the rest"
                        if self.history_since is not None
                        else "",
                    )
                )
        if read is None:
            if error:
                self._open(key, 0, ())  # the catch-up reads the topic from its beginning
            else:
                self._mark_caught_up(key)  # no reader: nothing to read now, or later
            return entry
        self._read_retry.pop(key, None)
        self._catching.pop(key, None)  # nothing an earlier life of the key left applies
        earlier = [message for message in read if int(message["id"]) < mid]
        taken = earlier[:MAX_BACKFILL]
        for message in taken:
            self._append_history(entry, message)
        if len(earlier) > MAX_BACKFILL:
            self._open(key, int(taken[-1]["id"]), ())  # cut short here: the catch-up goes on
        else:
            self._judge(key, read, 0, mid)
        return entry

    def _needs_refresh(self, entry: Mapping[str, Any]) -> bool:
        """True when the entry's quotes were rendered by an older build.

        An entry written before this build holds quotes that were cut at 280 characters
        and escaped, and the cut text is gone:  only Zulip holds the whole post."""
        try:
            return int(entry.get("format", 0)) < RENDER_VERSION
        except (TypeError, ValueError):
            return True

    def _refresh(self, entry: dict[str, Any], read: list[Mapping[str, Any]]) -> bool:
        """Render the entry's recorded quotes again from the messages the topic holds.

        The quotes are replaced only for the messages the read actually holds, so a read
        of another topic (the stored topic is the whitespace-collapsed form, and a topic
        whose title carried a credential is stored withheld) changes nothing.  The entry
        counts as rendered in this format once the read covered one of its messages;  a
        message the read does not hold (one older than the read's cap, or one that was
        deleted) keeps the text it had."""
        by_id: dict[int, Mapping[str, Any]] = {}
        for message in read:
            try:
                by_id[int(message["id"])] = message
            except (KeyError, TypeError, ValueError):
                continue
        changed = 0
        covered = 0
        for item in entry["links"]:
            message = by_id.get(int(item["mid"]))
            if message is None:
                continue
            covered += 1
            quote = post_text(str(message.get("content", "")))
            if quote != item["quote"]:
                item["quote"] = quote
                changed += 1
        if not covered:
            # The read is of another topic, or it holds none of this entry's messages:
            # the entry keeps the format it had and waits for a later try.
            return False
        entry["format"] = RENDER_VERSION
        if changed:
            entry["refreshed"] = time.strftime("%b %d %H:%M UTC", time.gmtime(self.clock()))
            entry["dirty"] = True
            self.log("re-rendered the quotes of %s from Zulip" % self._label(entry))
        return True

    def _refresh_stale(self) -> None:
        """Re-render the standing comments an older build wrote.

        A comment written before this build holds quotes that were cut at 280
        characters;  the whole post is only in Zulip, so the topic is read again and the
        comment is edited with the whole text.  One comment is refreshed per cycle, so a
        realm full of old comments is repaired over a few cycles instead of in one
        burst, and an entry whose topic cannot be read waits REFRESH_RETRY seconds
        before it is tried again."""
        if self.history is None:
            return  # no reader: the standalone process cannot read a topic back
        now = self.monotonic()
        attempts = 0
        for key, entry in sorted(
            self.ledger.entries.items(),
            key=lambda item: float(item[1].get("last_touch", 0.0)),
        ):
            if attempts >= REFRESH_PER_CYCLE:
                break
            if not self._needs_refresh(entry) or not entry.get("comment_id"):
                continue
            if self._is_public(int(entry["stream_id"])) is not True:
                continue  # a stream that turned private is dropped, not repaired
            if entry["topic"] == WITHHELD:
                continue  # a topic whose title carried a credential cannot be read back
            if now < self._refresh_retry.get(key, 0.0):
                continue  # its topic could not be read a moment ago
            # Every read counts, not every repair:  an entry whose topic cannot be read
            # must not make the cycle read every stale entry, every cycle (Sentry, PR 2340).
            attempts += 1
            read, _error = self._read_topic(int(entry["stream_id"]), entry["topic"], {})
            if read is None:
                self._refresh_retry[key] = now + REFRESH_RETRY
                continue
            if not self._refresh(entry, read):
                # The read held none of this entry's messages:  the entry waits before it
                # is tried again, so a topic that cannot be read is not read every cycle.
                self._refresh_retry[key] = now + REFRESH_RETRY

    def _append_history(self, entry: dict[str, Any], message: Mapping[str, Any]) -> bool:
        """Record one message read from Zulip's history, as _by_id checked it, the way the live
        path would have recorded it: the writer's own posts are skipped, a malformed message is
        never recorded, the message keeps its own timestamp, and a credential-shaped field is
        withheld whole by _append, with the same log line the live path writes (Kody, PR
        2298)."""
        if message.get("malformed") is True:
            return False
        if message["sender_email"].lower() == self.own_email:
            return False
        added = self._append(
            entry,
            message["id"],
            message["sender_full_name"],
            message["content"],
            float(message["timestamp"]),
        )
        if added:
            self._log_withheld(
                message["id"],
                (("content", message["content"]), ("sender", message["sender_full_name"])),
            )
        return added

    def _restore(
        self, key: str, issue: str, stream_id: int, stream: str, topic: str
    ) -> dict[str, Any] | None:
        """The entry of a pruned topic, rebuilt from its archive record (which leaves the
        archive) and put in the live table, or None when the archive has none for the key
        (AFC-821).

        The entry is built on the ARCHIVED root, not on the message that brought it back, so
        its marker is the old comment's own, byte for byte.  With a comment id, the next
        flush EDITS that comment, and if Linear no longer has it the edit path scans for the
        marker and recreates exactly one.  Without one (a create whose outcome was never
        learned), the flush looks for that marker before it creates anything."""
        record = self.ledger.archive_pop(key)
        if record is None:
            return None
        entry = new_entry(issue, stream_id, stream, topic, int(record["root"]))
        entry["comment_id"] = record.get("comment_id") or None
        entry["pending"] = record.get("pending") is True
        # In the live table before anything else runs, the log line included, so the record
        # never leaves the archive without its entry taking its place.
        self.ledger.entries[key] = entry
        self.log("restored the relay entry for %s from the archive" % self._label(entry))
        return entry

    def _catch_up(self, keys: list[str], stream_id: int, topic: str, mid: int) -> None:
        """Record what a topic got while the worker was not receiving it, before the live
        message `mid` is recorded (AFC-821).

        A restart loses nothing the ledger holds, but a message posted in the gap may never
        reach the worker: the host replays only part of what it missed, after a restart and
        after an event queue registers again (reopen_catch_up), and a message that arrives
        while the public-stream set is unloaded is dropped (_reopen_topic).  So the next
        message that reaches an entry which is not caught up reads the topic's messages newer
        than the entry's point (the newest message it has seen, unless a catch-up is already
        open), and the ones older than the live message are recorded first, in id order, as a
        backfill records them.  One read serves every entry of the topic: it starts from the
        oldest point any of them needs, and each entry takes only what is newer than its own
        point and not recorded already.

        _judge settles the entry, or moves its point to where the next read starts: only a
        read that reaches the live message settles it, and one cut short of it (at its cap,
        or by a reader that stopped early) or one that shows the live message is not the
        topic's newest (the host replaying an older one) does not.  A read that fails is logged,
        does not stop the record, and is not tried again for CATCH_UP_RETRY seconds.  A live
        message no newer than the entry's point, or one the entry has recorded already (the
        same message fed again for another seat), shows nothing new about the gap and costs
        no read."""
        if self.history_since is None:
            return
        now = self.monotonic()
        waiting: list[tuple[str, dict[str, Any], _CatchUp]] = []
        for key in keys:
            entry = self.ledger.entries.get(key)
            if entry is None or key in self._caught_up:
                continue  # no entry yet: _enter makes or restores it, and reads the whole topic
            state = self._catching.get(key)
            if state is None:
                if not entry["seen"]:
                    self._mark_caught_up(key)  # nothing says where its history stops
                    continue
                state = self._open(key, max(entry["seen"]), ())
            if state.start >= mid or mid in entry["seen"] or mid in state.recorded:
                continue
            if now < self._read_retry.get(key, 0.0):
                continue  # a read for it failed a moment ago: a later message tries again
            waiting.append((key, entry, state))
        if not waiting:
            return
        oldest = min(state.start for _key, _entry, state in waiting)
        try:
            found, malformed, over = _by_id(self.history_since(stream_id, topic, oldest))
        except Exception as exc:  # noqa: BLE001 - a catch-up must never stop a record
            for key, _entry, _state in waiting:
                self._read_retry[key] = now + CATCH_UP_RETRY
            self.log(
                "the messages a topic got while the relay was not receiving it could not be "
                "read (%s); a message at least %d seconds later tries again"
                % (type(exc).__name__, CATCH_UP_RETRY)
            )
            return
        self._log_malformed(malformed, over)
        for key, entry, state in waiting:
            self._read_retry.pop(key, None)
            added = 0
            for message in found:
                ident = int(message["id"])
                if (
                    state.start < ident < mid
                    and ident not in state.recorded
                    and self._append_history(entry, message)
                ):
                    state.recorded.add(ident)
                    added += 1
            if added:
                # A read that comes after live messages were recorded (a retry after a failed
                # read, a continued one) records older messages after newer ones, so the lists
                # go back into id order.
                entry["seen"].sort()
                entry["links"].sort(key=lambda item: int(item["mid"]))
                self.log("caught up %d missed message(s) for %s" % (added, self._label(entry)))
            self._judge(key, found, state.start, mid, origin=oldest)

    def _judge(
        self,
        key: str,
        found: list[Mapping[str, Any]],
        start: int,
        mid: int,
        *,
        origin: int | None = None,
    ) -> None:
        """Settle an entry's catch-up, or say where its next read starts, after a read of its
        topic (`found`, oldest first) made after `origin` (the entry's own point `start`, unless
        one read served several entries), on the way to recording the live message `mid`.

        Only a read that reached `mid` settles the entry.  One that went past `mid` shows `mid`
        is not the topic's newest (the host replaying an older message): once `mid` is recorded
        the entry holds the topic up to it, and the next read starts there.  One that stopped
        short of `mid` did not reach the live message, though `mid` was routed in this topic:
        it stopped at its cap (MAX_BACKFILL), or the reader stopped early (a page the API
        repeated, an anchor that would not move), or `mid` left the topic after it was routed.
        So nothing is known past its newest message, and the next read starts there (Kody, PR
        2298: such a read used to settle the entry, and the messages between its newest and
        `mid` were never read).  A read that brought nothing past `origin` cannot be continued
        from anywhere new: after MAX_SHORT_READS of those in a row the entry is settled and the
        log says so, so a live message that is no longer in its topic never keeps the topic
        reading at every message.  A read that went past `origin` but not past the entry's own
        `start` is not one of those: it moved on for an entry behind this one, the next read
        starts nearer this one's point, and counted it would give the entry up before any read
        had reached it (Kody asked for that count on PR 2298).  With no catch-up reader nothing
        can continue a read, and the entry is settled."""
        newest = int(found[-1]["id"]) if found else None
        state = self._catching.get(key)
        recorded = state.recorded if state is not None else ()
        if self.history_since is None or newest == mid:
            self._mark_caught_up(key)
        elif newest is not None and newest > mid:
            self._open(key, mid, recorded)
        elif newest is not None and newest > (start if origin is None else origin):
            self._open(key, max(start, newest), recorded)
        else:
            short = (state.short if state is not None else 0) + 1
            if short < MAX_SHORT_READS:
                self._open(key, start, recorded, short)
                return
            self._mark_caught_up(key)
            self.log(
                "gave up catching up %s: %d reads in a row stopped short of the live message "
                "and brought nothing new; what lies between stays missing"
                % (self._label(self.ledger.entries.get(key) or {}), short)
            )

    def _open(self, key: str, start: int, recorded: Iterable[int], short: int = 0) -> _CatchUp:
        """Open (or move) an entry's catch-up: the next read for it starts after `start`.
        `short` carries the count of reads in a row that brought nothing new (_judge)."""
        state = _CatchUp(start, recorded, short)
        self._catching[key] = state
        self._caught_up.discard(key)
        return state

    def _mark_caught_up(self, key: str) -> None:
        self._caught_up.add(key)
        self._catching.pop(key, None)
        self._read_retry.pop(key, None)

    def _forget_catch_up(self, key: str) -> None:
        self._caught_up.discard(key)
        self._catching.pop(key, None)
        self._read_retry.pop(key, None)

    def _note_recorded(self, key: str, mid: int) -> None:
        """Remember a live message recorded on an entry whose catch-up is open, so the read
        that closes the gap does not count it again.  The memory is bounded: a catch-up still
        open after MAX_BACKFILL such messages gives up, and the gap it leaves is logged."""
        state = self._catching.get(key)
        if state is None or mid <= state.start:
            return
        state.recorded.add(mid)
        if len(state.recorded) <= MAX_BACKFILL:
            return
        self._mark_caught_up(key)
        self.log(
            "gave up catching up %s after %d later messages; what its topic got while the "
            "relay was not receiving it stays missing"
            % (self._label(self.ledger.entries.get(key) or {}), MAX_BACKFILL)
        )

    def _record(
        self,
        ids: list[str],
        stream_id: int,
        stream: str,
        topic: str,
        mid: int,
        sender: str,
        content: str,
    ) -> None:
        if not ids:
            return
        now = self.clock()
        reads: dict[tuple[int, str], Any] = {}  # one whole-topic read for the whole message
        keys = [ledger_key(ident, stream_id, topic) for ident in ids]
        self._catch_up(keys, stream_id, topic, mid)
        for ident, key in zip(ids, keys):
            entry = self.ledger.entries.get(key)
            if entry is None:
                entry = self._enter(key, ident, stream_id, stream, topic, mid, reads)
                if entry is None:
                    continue  # an archived comment whose history could not be read waits
            elif self._needs_refresh(entry):
                # A comment an older build wrote:  its quotes are read from Zulip again, so
                # the next edit of it carries the whole posts.
                read, _error = self._read_topic(stream_id, topic, reads)
                if read is not None:
                    self._refresh(entry, read)
            if self._append(entry, mid, sender, content, now):
                self._note_recorded(key, mid)
                self._flush(key, now)  # creates at once; edits wait for the interval
        # tick() writes the whole file once per cycle, not once per message.  A create still
        # writes its intent ahead of the call, inside _flush.
        self.ledger.mark_dirty()

    # -- writes ----------------------------------------------------------------------------
    def _mark_missing(self, ident: str) -> None:
        """Remember that an issue id is not one the relay can write, for MISSING_TTL seconds
        only: an id named before its issue exists is looked up again later, not dropped for
        good."""
        self._uuid_cache.pop(ident, None)
        self._missing_issues[ident] = self.clock() + MISSING_TTL

    def _issue_uuid(self, ident: str) -> str | None:
        until = self._missing_issues.get(ident)
        if until is not None:
            if self.clock() < until:
                return None  # known missing, recently: not asked again yet
            del self._missing_issues[ident]
        if ident not in self._uuid_cache:
            try:
                self._uuid_cache[ident] = self.linear.issue_id(ident)
            except LinearNotFound as exc:
                self.log("linear has no issue %s (%s); asked again in an hour" % (ident, exc))
                self._mark_missing(ident)
                return None
            except LinearPermanentError as exc:
                # A refused read (a 403, an issue the app may not see) is not a missing issue:
                # say so, so a scope problem never looks like a quiet relay.
                self.log(
                    "linear refused the lookup of %s (%s); asked again in an hour" % (ident, exc)
                )
                self._mark_missing(ident)
                return None
            except LinearAuthError:
                raise
            except LinearError as exc:
                self.log("linear lookup failed for %s: %s" % (ident, exc))
                return None  # transient: not cached, retried on the next hit
        return self._uuid_cache[ident]

    def _create(self, uuid: str, entry: dict[str, Any]) -> None:
        """Write ahead, then create.  The intent is on disk before the call, so a crash or an
        unknown outcome leaves the entry pending, and a pending entry is reconciled by marker
        before it is ever created again."""
        entry["pending"] = True
        self.ledger.save()
        try:
            entry["comment_id"] = self.linear.create_comment(
                uuid, render_comment(entry, self.realm, self.byline)
            )
        except LinearUncertain:
            raise  # the outcome is unknown: the entry stays pending
        except LinearError:
            entry["pending"] = False  # a definite refusal: nothing landed
            raise
        entry["pending"] = False

    def _adopt_or_create(self, uuid: str, entry: dict[str, Any]) -> str:
        # A create may have landed while its answer was lost.  Look for the marker before
        # creating anything, so a crash cannot stack a second comment.
        existing = self.linear.find_comment(uuid, entry["marker"])
        if existing:
            entry["comment_id"] = existing
            entry["pending"] = False
            return "adopted an existing"
        self._create(uuid, entry)
        return "created a"

    def _edit(self, uuid: str, entry: dict[str, Any]) -> str:
        try:
            self.linear.update_comment(
                entry["comment_id"], render_comment(entry, self.realm, self.byline)
            )
            return "updated the"
        except (LinearNotFound, LinearRejected, LinearPermanentError) as exc:
            refusal = exc
        refused = type(refusal).__name__
        # The edit did not land.  Only a comment that is really gone is created again: the marker
        # scan confirms it first, and a comment that is still there is kept.
        found = self.linear.find_comment(uuid, entry["marker"])
        if found:
            entry["comment_id"] = found
            if isinstance(refusal, LinearNotFound):
                # The stored id missed but the scan found the comment: the next edit goes to the
                # id the scan found, so this one is worth retrying.
                raise LinearError(
                    "linear did not find the comment by its stored id; the scan found it, so the "
                    "edit is retried"
                )
            # A refused edit (success false, forbidden, invalid input) is a definite answer:
            # the comment is kept, and the edit is not retried until a new message gives the
            # entry new content.
            raise LinearPermanentError(
                "linear refused the edit (%s); the comment is kept, and the edit is not retried"
                % refused
            )
        entry["comment_id"] = None
        self._create(uuid, entry)
        return "recreated the"

    def _flush(self, key: str, now: float) -> None:
        entry = self.ledger.entries.get(key)
        if entry is None or not self._may_write(key, entry):
            return  # not loaded yet (deferred), or the stream is private now (dropped)
        if not entry["dirty"]:
            return
        if now < float(entry.get("retry_after", 0.0)):
            return  # a failed write is backing off
        if self._budget_spent():
            # This cycle's write budget is spent: the entry stays dirty for the next one, and the
            # log says why a handled message has no comment yet.
            self.log(
                "this cycle's write budget is spent; %s waits for the next cycle"
                % self._label(entry)
            )
            return
        issue = entry["issue"]
        label = self._label(entry)
        try:
            uuid = self._issue_uuid(issue)
            if uuid is None:
                if issue in self._missing_issues:
                    entry["dirty"] = False  # unknown issue: drop quietly
                    self.ledger.mark_dirty()
                return
            if entry["comment_id"] is None:
                if self.linear.dry_run:
                    self.log("would create a relay comment for %s" % label)
                    entry["last_edit"] = now
                    entry["dirty"] = False
                    self.ledger.mark_dirty()
                    return
                done = self._adopt_or_create(uuid, entry)
                if done == "adopted an existing":
                    # The adopted comment shows what the lost create wrote, not what the entry
                    # holds now (a message since, or a restored entry's refill), so the entry
                    # stays dirty and keeps its last edit time: the next tick edits it.
                    self.log("%s relay comment for %s" % (done, label))
                    entry["failures"] = 0
                    entry["retry_after"] = 0.0
                    self.ledger.mark_dirty()
                    return
            else:
                if now - float(entry["last_edit"]) < EDIT_INTERVAL:
                    return  # batched; tick() flushes it later
                done = self._edit(uuid, entry)
            self.log("%s relay comment for %s" % (done, label))
            entry["last_edit"] = now
            entry["dirty"] = False
            entry["refreshed"] = None  # the comment says so once
            entry["failures"] = 0
            entry["retry_after"] = 0.0
        except LinearAuthError as exc:
            raise _stop_on_auth(exc) from None
        except LinearNotFound as exc:
            # The issue is gone: waiting does not bring it back.
            self._mark_missing(issue)
            entry["dirty"] = False
            self.log("linear write for %s not retried: %s" % (label, exc))
        except LinearPermanentError as exc:
            # Refused for a reason waiting does not change: not retried until a new message
            # in the topic gives the entry new content.
            entry["dirty"] = False
            self.log("linear write for %s not retried: %s" % (label, exc))
        except LinearError as exc:
            self.log("linear write failed for %s: %s (will retry)" % (label, exc))
            failures = int(entry.get("failures", 0)) + 1
            entry["failures"] = failures
            entry["retry_after"] = now + min(600.0, 30.0 * (2 ** min(failures, 5)))
        self.ledger.mark_dirty()

    def tick(self, now: float | None = None) -> None:
        """Flush what is dirty, inside the cycle's write budget, then close the cycle."""
        now = self.clock() if now is None else now
        if self._cycle_deadline is None:
            self.start_cycle()  # a tick on its own is one cycle
        try:
            if self.public_streams is None:
                return  # nothing is known about any stream yet: write nothing, drop nothing
            self._drop_private_archive()
            self._refresh_stale()
            for key in list(self.ledger.entries):
                entry = self.ledger.entries.get(key)
                if entry is None:
                    continue
                if self._drop_if_private(key, entry):
                    continue
                if not entry["dirty"]:
                    continue
                if self._budget_spent():
                    # A slow Linear must not stall the Zulip poll loop: the rest stay dirty and
                    # are written next cycle.
                    self.log("this cycle's write budget is spent; the rest wait for the next cycle")
                    break
                try:
                    self._flush(key, now)
                except Exception as exc:  # noqa: BLE001 - one bad entry must not stop the others
                    self.log(
                        "the relay entry for %s failed: %s"
                        % (self._label(entry), type(exc).__name__)
                    )
            # A create's write-ahead save in the loop can retire an entry of a stream that has
            # just turned private, into the archive, after the sweep above.  It goes in this
            # tick, as the live rule drops the entry, not at the next one.
            self._drop_private_archive()
            self._save_quietly()
        finally:
            self._cycle_deadline = None

    def _save_quietly(self) -> None:
        """Write the ledger if anything changed.  A failed write (a full or broken volume) is
        logged and tried again next cycle instead of stopping the relay; a create still needs
        its own write-ahead save to succeed, so nothing is created while the volume fails."""
        if self.ledger.dirty:
            try:
                self.ledger.save()
            except OSError as exc:
                self.log(
                    "could not write the relay ledger (%s); trying again next cycle"
                    % type(exc).__name__
                )
        self._report_archive_drops()

    def _report_archive_drops(self) -> None:
        """Say so when the archive itself was truncated.  Every record a bound drops is a
        standing comment the relay can no longer find, so a later message that names the
        issue in that topic starts a second comment: the one loss the archive exists to
        prevent, and the log must show when it happens.  A count only, never a key."""
        dropped = self.ledger.archive_dropped - self._archive_drops_logged
        if dropped <= 0:
            return
        self._archive_drops_logged = self.ledger.archive_dropped
        self.log(
            "the relay ledger dropped %d archived record(s), oldest first, to stay inside its "
            "bounds; a topic that lost its record starts a new comment when next named" % dropped
        )

    def reconcile(self) -> None:
        """Adopt the comments of creates whose answer was lost.  Only entries that were written
        ahead are looked at, so the cost is bounded by the number of uncertain writes, and the
        scans share the open cycle's budget (run() opens one first), so a restart after a Linear
        outage cannot hold off the first Zulip poll.  An entry the budget does not reach stays
        pending, and its next write scans for the marker before it creates anything."""
        if self.public_streams is None:
            return  # nothing is known about any stream yet: look up nothing, drop nothing
        self._drop_private_archive()
        for key, entry in list(self.ledger.entries.items()):
            if self._drop_if_private(key, entry):
                continue
            if entry.get("comment_id") or not entry.get("pending"):
                continue
            if self._budget_spent():
                self.log(
                    "the reconcile budget is spent; the rest are checked before their next write"
                )
                break
            try:
                uuid = self._issue_uuid(entry["issue"])
                if uuid is None:
                    continue
                found = self.linear.find_comment(uuid, entry["marker"])
            except LinearAuthError as exc:
                raise _stop_on_auth(exc) from None
            except (LinearNotFound, LinearPermanentError) as exc:
                # The issue is gone or refused for good: there is nothing to adopt and nothing
                # to write, so the entry is released instead of being probed at every start.
                self._mark_missing(entry["issue"])
                entry["pending"] = False
                entry["dirty"] = False
                self.ledger.mark_dirty()
                self.log("reconcile for %s not retried: %s" % (self._label(entry), exc))
                continue
            except LinearError as exc:
                # A transient Linear failure must not kill the relay; the entry
                # stays pending and is reconciled on the next registration.
                self.log("reconcile lookup failed for %s: %s" % (self._label(entry), exc))
                continue
            if found:
                entry["comment_id"] = found
                entry["pending"] = False
                self.ledger.mark_dirty()
                self.log(
                    "reconciled a relay comment for %s after an uncertain write"
                    % self._label(entry)
                )
        self._save_quietly()


# ------------------------------------------------------------------------------------------
# Process
# ------------------------------------------------------------------------------------------
def load_zulip_creds(env: Mapping[str, str]) -> Credentials:
    email = (env.get("ZULIP_RELAY_EMAIL") or "").strip()
    key = (env.get("ZULIP_RELAY_API_KEY") or "").strip()
    site = (env.get("ZULIP_SITE") or "").strip().rstrip("/")
    if not (email and key and site):
        raise SystemExit("missing ZULIP_RELAY_EMAIL, ZULIP_RELAY_API_KEY or ZULIP_SITE")
    return Credentials(email=email, key=key, site=site, source="env")


def state_path(env: Mapping[str, str], dry_run: bool) -> Path:
    """Where the ledger lives.  A dry run never touches the live ledger: it writes a sibling of
    the configured file (`<name>.dry-run.json`), whatever LINEAR_RELAY_STATE names, because a
    dry run marks topics written that the live relay would then never create a comment for."""
    raw = (env.get("LINEAR_RELAY_STATE") or "").strip()
    live = Path(raw).expanduser() if raw else Path(LIVE_STATE).expanduser()
    if not live.name or live.name in (".", ".."):
        raise SystemExit("LINEAR_RELAY_STATE must name a ledger file, not %s" % live)
    if dry_run:
        return live.with_name(live.stem + ".dry-run" + (live.suffix or ".json"))
    return live


def load_public_streams(client: ZulipClient) -> dict[int, str]:
    """Every public stream on the realm, by id, with its name.  The relay only ever mirrors these.

    Archived streams are included: an archived stream is still public, and only `invite_only`
    makes a stream private.  Leaving it out would read it as private and drop its entries."""
    response = client.get("streams", {"exclude_archived": False})
    streams = response.get("streams") if isinstance(response, Mapping) else None
    if not isinstance(streams, list):
        # A missing list is not "no public streams": the load fails, and nothing is relayed.
        raise ValueError("the streams answer carries no streams list")
    out: dict[int, str] = {}
    for stream in streams:
        if (
            not isinstance(stream, Mapping)
            or not _is_int(stream.get("stream_id"))
            or stream["stream_id"] <= 0
            or not isinstance(stream.get("name"), str)
            or not stream["name"].strip()
            or not isinstance(stream.get("invite_only"), bool)
        ):
            raise ValueError("the streams answer carries a malformed stream")
        if not stream["invite_only"]:
            out[stream["stream_id"]] = stream["name"]
    return out


def subscribe_public(client: ZulipClient, log: Callable[[str], None]) -> None:
    """Join every public stream the bot is missing (private streams are never joined).  Archived
    streams are not joined: a subscription only brings new messages, and they take none."""
    try:
        have = {s["name"] for s in client.get("users/me/subscriptions").get("subscriptions", [])}
        allp = client.get("streams", {"exclude_archived": True}).get("streams", [])
        missing = [
            {"name": s["name"]}
            for s in allp
            if not s.get("invite_only") and not s.get("is_archived") and s["name"] not in have
        ]
        for i in range(0, len(missing), 20):
            client.post("users/me/subscriptions", {"subscriptions": missing[i : i + 20]})
        if missing:
            log("subscribed to %d public stream(s)" % len(missing))
    except (ApiError, NetworkError) as exc:
        log("auto-subscribe error: %s" % type(exc).__name__)


def run_check(client: ZulipClient, linear: LinearClient, env: Mapping[str, str], realm: str) -> int:
    """Credentials, the public-stream set and Linear auth, with no writes.  A missing issue is not
    an auth failure."""
    print("zulip bot ok (member, not admin); realm %s" % realm)
    try:
        streams = load_public_streams(client)
    except (ApiError, NetworkError, ValueError) as exc:
        # run() relays nothing until this set loads, so a set that cannot load fails the check.
        # The name and the message together say which of the three it is: the client scrubs
        # the key and the auth token out of its own messages (zulip.py:1231 and zulip.py:1415),
        # and a ValueError is one of this module's own fixed strings, so nothing credential-
        # shaped reaches the operator's terminal here.
        detail = " ".join(str(exc).split())[:200] or "no detail"
        print(
            "zulip public-stream set failed to load (%s: %s); the relay would relay nothing"
            % (type(exc).__name__, detail)
        )
        return 1
    print("zulip public-stream set ok: %d public stream(s)" % len(streams))
    try:
        print("linear auth ok: %s" % linear.viewer())
    except LinearError as exc:
        print("linear auth failed: %s" % exc)
        return 1
    ident = (env.get("LINEAR_RELAY_CHECK_ISSUE") or "").strip().upper()
    if not ident:
        print("linear lookup skipped (set LINEAR_RELAY_CHECK_ISSUE to check one issue)")
        return 0
    try:
        linear.issue_id(ident)
    except LinearNotFound:
        print("linear lookup: %s does not exist (the token works; the issue id is wrong)" % ident)
        return 0
    except LinearPermanentError as exc:
        # A malformed id, or an issue the app may not read: an issue problem, not an auth one.
        print(
            "linear lookup: %s was refused (%s); the token works, and the id is malformed or the "
            "app cannot see that issue" % (ident, exc)
        )
        return 0
    except LinearError as exc:
        print("linear lookup failed for %s: %s" % (ident, exc))
        return 1
    print("linear lookup ok: %s" % ident)
    return 0


def run(env: Mapping[str, str], *, check_only: bool = False) -> int:
    creds = load_zulip_creds(env)
    realm = realm_url(env)
    try:
        verify_realm(creds, realm)
    except CredentialError as exc:
        raise SystemExit(
            "%s.  The relay's realm is %s (SIMPLE_TOOLS_REALM, else the fleet realm), and ZULIP_SITE "
            "must name the same host" % (exc, realm)
        ) from None
    client = ZulipClient(creds, realm)
    me = client.get("users/me")
    if me.get("is_admin") or me.get("is_owner") or not me.get("is_bot"):
        raise SystemExit("the relay bot must be a non-admin bot (member role)")
    if str(me.get("email", "")).lower() != creds.email.lower():
        raise SystemExit("users/me does not match the configured relay bot")
    token = (env.get("LINEAR_RELAY_TOKEN") or "").strip()
    if not token:
        raise SystemExit("missing LINEAR_RELAY_TOKEN")
    dry = env.get("LINEAR_RELAY_DRY_RUN") == "1"
    linear = LinearClient(token, dry_run=dry)
    teams = (
        tuple(
            t.strip().upper() for t in (env.get("LINEAR_RELAY_TEAMS") or "").split(",") if t.strip()
        )
        or DEFAULT_TEAMS
    )
    regex = build_regex(teams)
    if check_only:
        return run_check(client, linear, env, realm)
    ledger = Ledger(state_path(env, dry))
    ledger.open()
    worker = RelayWorker(realm, creds.email, linear, ledger, regex)

    def load_streams() -> bool:
        """Reload the public-stream set.  The old set is forgotten first, so a reload that fails
        leaves the worker knowing nothing (it relays, writes and drops nothing) instead of
        trusting a set that may be stale.  True when the set loaded."""
        worker.set_public_streams(None)
        try:
            streams = load_public_streams(client)
        except (ApiError, NetworkError, ValueError) as exc:
            worker.log(
                "could not load the public-stream set (%s); relaying nothing until it loads"
                % type(exc).__name__
            )
            return False
        worker.set_public_streams(streams)
        return True

    types = ["message", "update_message", "stream"]
    while True:
        try:
            subscribe_public(client, worker.log)
            if not load_streams():
                time.sleep(30)  # no queue is registered until the set loads
                continue
            queue = EventQueue(client, None, types)
            queue.register()
            worker.start_cycle()  # the startup reconcile gets a cycle's budget, then polling starts
            worker.reconcile()
            worker.log("relay queue registered%s" % (" (dry run)" if dry else ""))
            while True:
                events = queue.poll_events(queue.longpoll_timeout or 90)
                worker.start_cycle()  # this batch's creates and the tick share one budget
                for event in events:
                    if event.get("type") == "stream":
                        if event.get("op") in STREAM_RELOAD_OPS:
                            # A failed reload keeps the queue: the set is unknown, so nothing
                            # is written, and the reload is tried again after this batch.
                            subscribe_public(client, worker.log)
                            load_streams()
                        continue
                    try:
                        worker.handle_event(event)
                    except Exception as exc:  # noqa: BLE001 - one bad event must not stop the relay
                        worker.log("a relay event failed: %s" % type(exc).__name__)
                if worker.public_streams is None:
                    load_streams()
                worker.tick()
        except QueueExpired:
            worker.log("queue expired, re-registering (messages sent in the gap are not replayed)")
        except (ApiError, NetworkError) as exc:
            worker.log("zulip error %s, retrying in 30s" % type(exc).__name__)
            time.sleep(30)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="linear_relay")
    parser.add_argument("command", choices=["run", "check"])
    args = parser.parse_args(argv)
    return run(os.environ, check_only=args.command == "check")


if __name__ == "__main__":
    raise SystemExit(main())
