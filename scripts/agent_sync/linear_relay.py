#!/usr/bin/env python3
"""Zulip -> Linear relay.  One direction only: nothing in this module ever posts to Zulip.

Any message in a PUBLIC Zulip stream whose TOPIC TITLE or BODY names a Linear issue ID (AFC-123
and the other team keys) is mirrored onto that issue as ONE comment per (issue, topic), written by
a dedicated Linear app identity.  Later hits in the same topic edit that comment in place (a list
of permalinks), at most once per EDIT_INTERVAL seconds per comment.  Direct messages and private
streams are never relayed: the relay bot is a plain member that only joins public streams, and the
worker drops any message whose stream is not in the public set it loaded.  Until that set has
loaded, the worker relays, writes and drops nothing.

Owner decisions (Jay, 2026-10-21 and 2026-10-23): human and bot posts both count; Linear stays the
record; no #issues stream; comments are NOT posted as the Wise app but as a new dedicated relay
app; one updating comment per (issue, topic) with 10 minute batching; DMs and private streams are
excluded.

What this build does NOT claim (Echo's reviewer addendum, 2026-10-23):

  - NOT exactly-once.  The write-ahead ledger and the marker reconciliation narrow duplicates, they
    do not eliminate them.  A lost ledger file, a second relay process on another host, or an
    uncertain write outcome (a create that landed while its answer was lost) can each produce a
    second comment.  The relay reconciles what it can see and says so in the log.
  - Comment-edit permission under the Linear scopes `read` + `comments:create` is UNVERIFIED.  The
    relay is written for it; the sandbox test that proves it comes later, after Jay approves the
    app.

Run (server instance, stdlib only, Python 3.11+):

    python3 -m agent_sync.linear_relay run        # long-running loop
    python3 -m agent_sync.linear_relay check      # credentials + Linear auth, no writes

Credentials (environment, loaded by the container from Infisical, never logged):

    ZULIP_RELAY_EMAIL, ZULIP_RELAY_API_KEY   a member (not admin) Zulip bot; site from ZULIP_SITE
    LINEAR_RELAY_TOKEN                       OAuth app token of the dedicated Linear relay app

The realm comes from AGENT_SYNC_REALM (else the fleet realm); ZULIP_SITE must name the same host.

Optional: LINEAR_RELAY_STATE (ledger path), LINEAR_RELAY_TEAMS (comma list),
LINEAR_RELAY_DRY_RUN=1 (log what would be written, write nothing to Linear),
LINEAR_RELAY_CHECK_ISSUE (an issue id for the check command).

The module is also importable.  A daemon needs these entry points: RelayWorker.handle_event(event)
for message and update_message events; RelayWorker.start_cycle() before each poll batch and
RelayWorker.tick(now) after it, so the batch's writes share one budget; and
RelayWorker.set_public_streams(...) whenever a `stream` event arrives (None first, then the
reloaded id -> name map, as run() does).  The worker holds no Zulip client, so it cannot reload the
set itself, and until it is given one it relays and writes nothing.  Zulip text is untrusted data:
a field that carries anything credential-shaped is withheld whole, never partly redacted, and is
not logged; every other field is trimmed and escaped so it cannot break out of the comment.  It
never decides what the relay does beyond which issue ID to name.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from . import secretscan
from .zulip import (
    ApiError,
    CredentialError,
    Credentials,
    EventQueue,
    NetworkError,
    QueueExpired,
    ZulipClient,
    _NoRedirect,
    masked_credential,
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
MAX_ISSUES_PER_MESSAGE = 5
QUOTE_CHARS = 280
LINEAR_URL = "https://api.linear.app/graphql"
USER_AGENT = "agent-sync-linear-relay/1 (fleet)"
MARKER = "<!-- zulip-relay -->"  # first line of every relay comment, so humans can spot them
LIVE_STATE = "~/.agent-sync/linear-relay.json"
DRY_STATE = "~/.agent-sync/linear-relay.dry-run.json"
LEDGER_VERSION = 2  # 2: the ledger key carries a digest of the topic, never the topic itself
MAX_ENTRIES = 500  # the ledger is bounded: the oldest entries are pruned
MAX_LEDGER_BYTES = 8 * 1024 * 1024
RETRY_BACKOFF = (1.0, 2.0, 4.0, 8.0)
MAX_ATTEMPTS = 5
REQUEST_TIMEOUT = 30.0  # one HTTP request
CALL_BUDGET = 60.0  # one Linear call, every retry included
SCAN_BUDGET = 90.0  # one marker scan, every page and every retry included
MAX_COMMENT_PAGES = 40  # 100 comments a page, so 4000 comments an issue
MISSING_TTL = 3600.0  # an issue id Linear does not have is looked up again after an hour
TICK_BUDGET = 120.0  # one tick's writes; the rest wait for the next cycle so Zulip keeps polling
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
    """Trim, withhold and defang untrusted Zulip text for a Linear comment.

    The result cannot open a link, start a code span, close the quoted string, or carry a
    credential into Linear.  The @ is kept but followed by a zero-width space, so a mention
    never becomes a live mention."""
    text = _escape_markdown(safe_text(text))
    text = text.replace("@", "@\u200b")
    if len(text) > QUOTE_CHARS:
        text = text[: QUOTE_CHARS - 1] + "\u2026"
    return text


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
    )


class Ledger:
    """JSON file: key "ISSUE|stream_id|topic digest" -> comment id, links, timing.  Atomic,
    owner-only writes under an flock so a restart never double-comments."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {"version": LEDGER_VERSION, "entries": {}}
        self._lock_fh: Any = None
        self.pruned = 0
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

    def mark_dirty(self) -> None:
        """Ask the next tick() to write the file, so one cycle costs one write."""
        self.dirty = True

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
        self._prune_entries()
        text = json.dumps(self.data, indent=1, sort_keys=True)
        while len(text.encode("utf-8")) > MAX_LEDGER_BYTES and self.data["entries"]:
            self._drop_oldest()
            text = json.dumps(self.data, indent=1, sort_keys=True)
        return text

    def _prune_entries(self) -> None:
        entries = self.data["entries"]
        excess = len(entries) - MAX_ENTRIES
        if excess <= 0:
            return
        order = sorted(entries.items(), key=lambda kv: float(kv[1].get("last_touch", 0.0)))
        for key, _ in order[:excess]:
            entries.pop(key, None)
            self.pruned += 1

    def _drop_oldest(self) -> None:
        entries = self.data["entries"]
        if not entries:
            return
        oldest = min(entries.items(), key=lambda kv: float(kv[1].get("last_touch", 0.0)))[0]
        entries.pop(oldest, None)
        self.pruned += 1

    @property
    def entries(self) -> dict[str, Any]:
        return self.data["entries"]


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
    ) -> None:
        self._token = token
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
                    "Authorization": "Bearer " + self._token,
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
                # The fleet's preview of a refused key (zulip.masked_credential): the first eight
                # and last four characters of a long key, one character of a short one, so the
                # operator can tell which key was refused without it ever being written out.
                raise LinearAuthError("%s; key %s" % (last, masked_credential(self._token)))
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
def render_comment(entry: Mapping[str, Any], realm: str) -> str:
    links = entry["links"][-MAX_LINKS_SHOWN:]
    earlier = max(0, int(entry["count"]) - len(links))
    lines = [
        entry["marker"],
        "**Zulip discussion:** #%s > %s"
        % (neutralise(entry["stream"]), neutralise(entry["topic"])),
        "",
    ]
    if earlier > 0:
        lines.append("_%d earlier message(s) not listed._" % earlier)
    for item in links:
        # The URL is built here, from the message id, so no text that came out of Zulip can
        # reach it.  The quote was neutralised when it was stored, so it is not escaped twice.
        # A message the relay never read (one that came in by a topic rename) has no quote, and
        # its bullet shows none rather than an empty pair of quotation marks.
        head = "- [%s](%s), %s" % (
            item["when"],
            permalink(realm, int(item["mid"])),
            neutralise(item["sender"]),
        )
        lines.append('%s: "%s"' % (head, item["quote"]) if item["quote"] else head)
    # Rendered copy: the sentence gap is a no-break space plus a space.
    lines += ["", "_Mirrored one way from Zulip by the relay.\u00a0 Edit in Zulip, not here._"]
    return "\n".join(lines)


def _stop_on_auth(exc: LinearAuthError) -> SystemExit:
    """The relay's exit on a refused token.  The message is the client's, which names the
    variable and carries only the masked preview of the key."""
    return SystemExit("%s; the relay stops rather than retry a token that cannot work" % exc)


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
        log: Callable[[str], None] | None = None,
        public_streams: Mapping[int, str] | Iterable[int] | None = None,
    ) -> None:
        self.realm = zulip_realm
        self.own_email = own_email.lower()
        self.linear = linear
        self.ledger = ledger
        self.regex = regex
        self.clock = clock
        self.log = log or (lambda m: print(m, file=sys.stderr, flush=True))
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
        self._cycle_deadline = self.clock() + TICK_BUDGET

    def _budget_spent(self) -> bool:
        return self._cycle_deadline is not None and self.clock() > self._cycle_deadline

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

    @staticmethod
    def _label(entry: Mapping[str, Any]) -> str:
        """How the log names an entry: the issue and the conversation's first message id, never
        topic text, which is untrusted and can carry a credential."""
        return "%s (conversation %s)" % (entry.get("issue", "?"), entry.get("root", "?"))

    # -- events ----------------------------------------------------------------------------
    def handle_event(self, event: Mapping[str, Any]) -> None:
        kind = event.get("type")
        if kind == "message":
            self._handle_message(event.get("message") or {})
        elif kind == "update_message":
            self._handle_move(event)
        elif kind == "stream":
            # The worker holds no Zulip client, so it cannot reload the set itself.  run() never
            # routes a stream event here; a daemon that does has missed set_public_streams.
            self.log("a stream event reached handle_event: reload the set with set_public_streams")

    def _handle_message(self, msg: Mapping[str, Any]) -> None:
        if msg.get("type") != "stream":
            return  # DMs are never relayed
        sid = msg.get("stream_id")
        if sid is None or self._is_public(sid) is not True:
            # A private stream the bot was added to, or a stream set that has not loaded yet:
            # the relay only ever handles known public streams, so this message is dropped.
            return
        if str(msg.get("sender_email", "")).lower() == self.own_email:
            return
        topic = str(msg.get("subject", ""))
        content = str(msg.get("content", ""))
        sender = str(msg.get("sender_full_name", "someone"))
        ids = find_issue_ids(self.regex, topic, content)
        if not ids:
            return
        withheld = [
            name
            for name, value in (("topic", topic), ("content", content), ("sender", sender))
            if has_secret(value)
        ]
        if withheld:
            # The field names and the message id only: the text itself never reaches the log.
            self.log(
                "message %s: withheld %s (credential-shaped text)"
                % (int(msg.get("id", 0)), ", ".join(withheld))
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
        when the moved messages went to a private stream, they also leave its list."""
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

    def _detach_messages(self, stream_id: int, topic: str, moved: list[int]) -> None:
        """Some messages left the topic for a private stream (change_one or change_later): the
        topic's comment stays standing, and the moved messages leave its list at the next edit,
        so it stops linking to messages that now live where the relay may not look."""
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
            else:
                target["seen"] = sorted(set(target["seen"]) | set(entry["seen"]))[-MAX_SEEN:]
                target["links"] = (target["links"] + entry["links"])[-MAX_LINKS_SHOWN:]
                # Two conversations become one: a message belongs to one topic, so the counts add.
                target["count"] = int(target["count"]) + int(entry["count"])
                target["dirty"] = True
                target["last_touch"] = now
                del self.ledger.entries[key]
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
        for ident in ids:
            key = ledger_key(ident, stream_id, topic)
            entry = self.ledger.entries.get(key)
            if entry is None:
                entry = new_entry(ident, stream_id, stream, topic, mid)
                self.ledger.entries[key] = entry
            if mid in entry["seen"]:
                continue
            entry["seen"] = (entry["seen"] + [mid])[-MAX_SEEN:]
            entry["count"] = int(entry["count"]) + 1
            entry["links"].append(
                {
                    "mid": mid,
                    "sender": safe_text(sender),
                    "when": time.strftime("%b %d %H:%M UTC", time.gmtime(now)),
                    "quote": neutralise(content),
                }
            )
            entry["links"] = entry["links"][-MAX_LINKS_SHOWN:]
            entry["dirty"] = True
            entry["last_touch"] = now
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
                uuid, render_comment(entry, self.realm)
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
            self.linear.update_comment(entry["comment_id"], render_comment(entry, self.realm))
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
            return  # this cycle's write budget is spent: the entry stays dirty for the next one
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
            else:
                if now - float(entry["last_edit"]) < EDIT_INTERVAL:
                    return  # batched; tick() flushes it later
                done = self._edit(uuid, entry)
            self.log("%s relay comment for %s" % (done, label))
            entry["last_edit"] = now
            entry["dirty"] = False
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
            self._save_quietly()
        finally:
            self._cycle_deadline = None

    def _save_quietly(self) -> None:
        """Write the ledger if anything changed.  A failed write (a full or broken volume) is
        logged and tried again next cycle instead of stopping the relay; a create still needs
        its own write-ahead save to succeed, so nothing is created while the volume fails."""
        if not self.ledger.dirty:
            return
        try:
            self.ledger.save()
        except OSError as exc:
            self.log(
                "could not write the relay ledger (%s); trying again next cycle"
                % type(exc).__name__
            )

    def reconcile(self) -> None:
        """Adopt the comments of creates whose answer was lost.  Only entries that were written
        ahead are looked at, so the cost is bounded by the number of uncertain writes."""
        if self.public_streams is None:
            return  # nothing is known about any stream yet: look up nothing, drop nothing
        for key, entry in list(self.ledger.entries.items()):
            if self._drop_if_private(key, entry):
                continue
            if entry.get("comment_id") or not entry.get("pending"):
                continue
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
    """Where the ledger lives.  A dry run never touches the live ledger: the two would fight
    over the same comment ids."""
    raw = (env.get("LINEAR_RELAY_STATE") or "").strip()
    live = Path(LIVE_STATE).expanduser()
    if dry_run:
        chosen = Path(raw).expanduser() if raw else Path(DRY_STATE).expanduser()
        if chosen == live:
            raise SystemExit(
                "LINEAR_RELAY_DRY_RUN=1 refuses to use the live ledger %s; point LINEAR_RELAY_STATE "
                "at a dry-run file" % live
            )
        return chosen
    return Path(raw).expanduser() if raw else live


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
    """Credentials and Linear auth, with no writes.  A missing issue is not an auth failure."""
    print("zulip bot ok (member, not admin); realm %s" % realm)
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
            "%s.  The relay's realm is %s (AGENT_SYNC_REALM, else the fleet realm), and ZULIP_SITE "
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
            worker.reconcile()
            worker.log("relay queue registered%s" % (" (dry run)" if dry else ""))
            while True:
                events = queue.poll_events(queue.longpoll_timeout or 90)
                worker.start_cycle()  # this batch's creates and the tick share one budget
                for event in events:
                    if event.get("type") == "stream":
                        if event.get("op") in ("create", "update", "delete"):
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
