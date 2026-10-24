#!/usr/bin/env python3
"""Zulip -> Linear relay.  One direction only: nothing in this module ever posts to Zulip.

Any message in a PUBLIC Zulip stream whose TOPIC TITLE or BODY names a Linear issue ID (AFC-123
and the other team keys) is mirrored onto that issue as ONE comment per (issue, topic), written by
a dedicated Linear app identity.  Later hits in the same topic edit that comment in place (a list
of permalinks), at most once per EDIT_INTERVAL seconds per comment.  Direct messages and private
streams are never relayed: the relay bot is a plain member that only joins public streams, and the
worker drops any message whose stream is not in the public set it loaded.

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

Optional: LINEAR_RELAY_STATE (ledger path), LINEAR_RELAY_TEAMS (comma list),
LINEAR_RELAY_DRY_RUN=1 (log what would be written, write nothing to Linear),
LINEAR_RELAY_CHECK_ISSUE (an issue id for the check command).

The module is also importable: RelayWorker.handle_event(event) and RelayWorker.tick(now) are the
two entry points a daemon needs.  Zulip text is untrusted data: it is quoted, trimmed, stripped of
credential-shaped strings, escaped so it cannot break out of the comment, and it never decides what
the relay does beyond which issue ID to name.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .zulip import (
    ApiError,
    Credentials,
    EventQueue,
    NetworkError,
    QueueExpired,
    ZulipClient,
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
MARKER = "<!-- zulip-relay -->"  # first line of every relay comment, so humans can spot them
LIVE_STATE = "~/.agent-sync/linear-relay.json"
DRY_STATE = "~/.agent-sync/linear-relay.dry-run.json"
MAX_ENTRIES = 500  # the ledger is bounded: the oldest entries are pruned
MAX_LEDGER_BYTES = 8 * 1024 * 1024
RETRY_BACKOFF = (1.0, 2.0, 4.0, 8.0)
MAX_ATTEMPTS = 5
MAX_COMMENT_PAGES = 40  # 100 comments a page, so 4000 comments a topic


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


# Credential-shaped strings, redacted before any Zulip text is copied into a Linear comment.
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
_KEYED_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|apikey|access[_-]?token|token|secret|password|passwd|pwd"
    r"|authorization|bearer|credential)\b(\s*[:=]\s*|\s+)(\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
# The markdown that can change the shape of the comment: emphasis, code spans, links, html,
# tables, strikethrough and the quote delimiter.
_MD_SPECIALS = set('\\`*_[]<>|~!"')


def _scrub_secrets(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[redacted]", text)
    return _KEYED_SECRET.sub(lambda m: "%s%s[redacted]" % (m.group(1), m.group(2)), text)


def _escape_markdown(text: str) -> str:
    return "".join("\\" + ch if ch in _MD_SPECIALS else ch for ch in text)


def neutralise(text: str) -> str:
    """Trim, redact and defang untrusted Zulip text for a Linear comment.

    The result cannot open a link, start a code span, close the quoted string, or carry a
    credential into Linear.  The @ is kept but followed by a zero-width space, so a mention
    never becomes a live mention."""
    text = re.sub(r"\s+", " ", text or "").strip()
    text = _scrub_secrets(text)
    text = _escape_markdown(text)
    text = text.replace("@", "@\u200b")
    if len(text) > QUOTE_CHARS:
        text = text[: QUOTE_CHARS - 1] + "\u2026"
    return text


# ------------------------------------------------------------------------------------------
# Ledger
# ------------------------------------------------------------------------------------------
def ledger_key(issue: str, stream_id: int, topic: str) -> str:
    return "%s|%d|%s" % (issue, stream_id, topic)


def split_key(key: str) -> tuple[str, int, str]:
    issue, sid, topic = key.split("|", 2)
    return issue, int(sid), topic


def marker_for(issue: str, root_mid: int) -> str:
    """The first line of a relay comment: the marker plus a key that identifies the conversation.

    The key is the issue and the id of the FIRST message recorded for the topic, not the topic
    name: a topic rename moves the conversation, and the key has to survive that so the relay
    keeps editing the same comment instead of starting a second one."""
    return "%s %s|%d" % (MARKER, issue, root_mid)


class Ledger:
    """JSON file: key "ISSUE|stream_id|topic" -> comment id, links, timing.  Atomic writes under
    an flock so a restart never double-comments."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {"version": 1, "entries": {}}
        self._lock_fh: Any = None
        self.pruned = 0

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
        entries = self.data.get("entries")
        if not isinstance(entries, dict):
            raise SystemExit("the relay ledger %s has no entries table" % self.path)

    def save(self) -> None:
        text = self._render()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(text, "utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

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
        "stream": stream,
        "stream_id": stream_id,
        "topic": topic,
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
    pass


class LinearNotFound(LinearError):
    """The issue or comment is not there.  Never retried: the answer is final."""


class LinearAuthError(LinearError):
    """The token was refused.  Never retried: no amount of waiting changes it."""


class LinearClient:
    def __init__(
        self, token: str, *, dry_run: bool = False, sleep: Callable[[float], None] = time.sleep
    ) -> None:
        self._token = token
        self.dry_run = dry_run
        self.sleep = sleep

    def _gql(self, query: str, variables: Mapping[str, Any]) -> dict[str, Any]:
        body = json.dumps({"query": query, "variables": variables}).encode()
        for attempt in range(MAX_ATTEMPTS):
            req = urllib.request.Request(
                LINEAR_URL,
                data=body,
                method="POST",
                headers={
                    "Authorization": "Bearer " + self._token,
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    try:
                        out = json.loads(resp.read())
                    except ValueError:
                        # A proxy or gateway can answer 200 with a non-JSON body.
                        if attempt < MAX_ATTEMPTS - 1:
                            self.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])
                            continue
                        raise LinearError("linear answered with a non-JSON body") from None
            except urllib.error.HTTPError as exc:
                status = exc.code
                payload = exc.read().decode("utf-8", "replace")[:300]
                if status in (401, 403):
                    raise LinearAuthError("linear refused the token (http %d)" % status) from None
                if status == 404 or "not found" in payload.lower():
                    raise LinearNotFound(payload)
                if status == 429 or status >= 500:
                    if attempt < MAX_ATTEMPTS - 1:
                        self.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])
                        continue
                raise LinearError("linear http %d" % status) from None
            except (urllib.error.URLError, TimeoutError) as exc:
                # A timeout on a write may mean the write landed: the worker reconciles before it
                # creates anything again, so the retry is safe.
                if attempt < MAX_ATTEMPTS - 1:
                    self.sleep(RETRY_BACKOFF[min(attempt, len(RETRY_BACKOFF) - 1)])
                    continue
                raise LinearError("linear network error: %s" % type(exc).__name__) from None
            errors = out.get("errors") or []
            if errors:
                text = json.dumps(errors)[:300]
                low = text.lower()
                if "not found" in low or "entity not found" in low:
                    raise LinearNotFound(text)
                if (
                    "unauthorized" in low
                    or "authentication" in low
                    or "forbidden" in low
                    or "scope" in low
                ):
                    raise LinearAuthError("linear refused the token: %s" % text)
                raise LinearError("linear graphql error: " + text)
            return out["data"]
        raise LinearError("linear gave up after %d attempts" % MAX_ATTEMPTS)

    def viewer(self) -> str:
        """Who the token acts as.  A read that proves the token works without needing an issue."""
        data = self._gql("query{viewer{id name}}", {})
        viewer = data.get("viewer") or {}
        return str(viewer.get("name") or viewer.get("id") or "unknown")

    def issue_id(self, identifier: str) -> str:
        data = self._gql("query($id:String!){issue(id:$id){id identifier}}", {"id": identifier})
        if not data.get("issue"):
            raise LinearNotFound(identifier)
        return data["issue"]["id"]

    def find_comment(self, issue_uuid: str, marker: str) -> str | None:
        """The id of the comment that carries this marker, or None.  Pages through the issue's
        comments: this is how a create that landed while its answer was lost is adopted
        instead of being stacked."""
        after: str | None = None
        for _ in range(MAX_COMMENT_PAGES):
            data = self._gql(
                "query($id:String!,$after:String){issue(id:$id){comments(first:100,after:$after)"
                "{nodes{id body} pageInfo{hasNextPage endCursor}}}}",
                {"id": issue_uuid, "after": after},
            )
            connection = (data.get("issue") or {}).get("comments") or {}
            for node in connection.get("nodes") or []:
                if marker in (node.get("body") or ""):
                    return str(node.get("id"))
            page = connection.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return None
            after = page.get("endCursor")
            if not after:
                return None
        return None

    def create_comment(self, issue_uuid: str, body: str) -> str:
        if self.dry_run:
            raise LinearError("create_comment must not be called in a dry run")
        data = self._gql(
            "mutation($i:CommentCreateInput!){commentCreate(input:$i){success comment{id}}}",
            {"i": {"issueId": issue_uuid, "body": body}},
        )
        res = data["commentCreate"]
        if not res.get("success"):
            raise LinearError("commentCreate failed")
        return res["comment"]["id"]

    def update_comment(self, comment_id: str, body: str) -> None:
        if self.dry_run:
            raise LinearError("update_comment must not be called in a dry run")
        data = self._gql(
            "mutation($id:String!,$i:CommentUpdateInput!){commentUpdate(id:$id,input:$i){success}}",
            {"id": comment_id, "i": {"body": body}},
        )
        if not data["commentUpdate"].get("success"):
            raise LinearNotFound("commentUpdate failed for " + comment_id)


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
        lines.append(
            '- [%s](%s), %s: "%s"'
            % (
                item["when"],
                permalink(realm, int(item["mid"])),
                neutralise(item["sender"]),
                item["quote"],
            )
        )
    lines += ["", "_Mirrored one way from Zulip by the relay. Edit in Zulip, not here._"]
    return "\n".join(lines)


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
        public_streams: set[int] | None = None,
    ) -> None:
        self.realm = zulip_realm
        self.own_email = own_email.lower()
        self.linear = linear
        self.ledger = ledger
        self.regex = regex
        self.clock = clock
        self.log = log or (lambda m: print(m, file=sys.stderr, flush=True))
        self.public_streams: set[int] = set(public_streams or ())
        self._uuid_cache: dict[str, str | None] = {}
        self._missing_issues: set[str] = set()

    # -- the public-stream guard -------------------------------------------------------------
    def _private_stream(self, entry: Mapping[str, Any]) -> bool:
        """True when the entry's stream is known to be private.

        An empty public-stream set means the set has not been loaded yet, so nothing is
        private yet and nothing is dropped on that account.  Once the set is loaded, a stream
        that is not in it is private or archived, and the entry never reaches Linear.
        """
        if not self.public_streams:
            return False
        return int(entry.get("stream_id", 0)) not in self.public_streams

    def _drop_if_private(self, key: str, entry: Mapping[str, Any]) -> bool:
        """Remove an entry whose stream is no longer public.  True when it was dropped."""
        if not self._private_stream(entry):
            return False
        self.ledger.entries.pop(key, None)
        self.log(
            "dropped the relay entry for %s (topic %r): its stream is no longer public"
            % (entry.get("issue", "?"), entry.get("topic", "?"))
        )
        return True

    # -- events ----------------------------------------------------------------------------
    def handle_event(self, event: Mapping[str, Any]) -> None:
        kind = event.get("type")
        if kind == "message":
            self._handle_message(event.get("message") or {})
        elif kind == "update_message":
            self._handle_rename(event)

    def _handle_message(self, msg: Mapping[str, Any]) -> None:
        if msg.get("type") != "stream":
            return  # DMs are never relayed
        sid = msg.get("stream_id")
        if sid is None or int(sid) not in self.public_streams:
            # A private stream the bot was added to, or a stream we have not loaded yet: the
            # relay only ever handles public streams, so this message is dropped.
            return
        if str(msg.get("sender_email", "")).lower() == self.own_email:
            return
        stream = str(msg.get("display_recipient", ""))
        ids = find_issue_ids(self.regex, str(msg.get("subject", "")), str(msg.get("content", "")))
        self._record(
            ids,
            int(sid),
            stream,
            str(msg.get("subject", "")),
            int(msg.get("id", 0)),
            str(msg.get("sender_full_name", "someone")),
            str(msg.get("content", "")),
        )

    def _handle_rename(self, event: Mapping[str, Any]) -> None:
        # A topic rename moves the whole conversation.  The standing comment moves with it, so the
        # topic keeps ONE comment under its new name instead of starting a second one.
        new_topic = event.get("subject")
        old_topic = event.get("orig_subject")
        if not new_topic or new_topic == old_topic:
            return
        sid = event.get("new_stream_id") or event.get("stream_id")
        if sid is None or int(sid) not in self.public_streams:
            return  # private streams are never mirrored, renames included
        stream = str(event.get("stream_name") or sid)
        self._migrate_topic(int(sid), stream, str(old_topic or ""), str(new_topic))
        fresh = [
            i
            for i in find_issue_ids(self.regex, str(new_topic))
            if i not in find_issue_ids(self.regex, str(old_topic or ""))
        ]
        if not fresh:
            return
        mid = int((event.get("message_ids") or [event.get("message_id", 0)])[0])
        self._record(fresh, int(sid), stream, str(new_topic), mid, "topic rename", "")

    def _migrate_topic(self, stream_id: int, stream: str, old_topic: str, new_topic: str) -> None:
        if not old_topic or old_topic == new_topic:
            return
        if stream_id not in self.public_streams:
            # The rename path already refuses a private stream; the guard is repeated here so
            # that a migration can never move a private topic's standing comment, however it
            # is reached.
            return
        moved = False
        for key, entry in list(self.ledger.entries.items()):
            issue, entry_sid, entry_topic = split_key(key)
            if entry_sid != stream_id or entry_topic != old_topic:
                continue
            new_key = ledger_key(issue, stream_id, new_topic)
            target = self.ledger.entries.get(new_key)
            if target is None:
                del self.ledger.entries[key]
                entry["topic"] = new_topic
                entry["dirty"] = True
                entry["last_touch"] = self.clock()
                self.ledger.entries[new_key] = entry
            else:
                target["seen"] = sorted(set(target["seen"]) | set(entry["seen"]))[-MAX_SEEN:]
                target["links"] = (target["links"] + entry["links"])[-MAX_LINKS_SHOWN:]
                target["count"] = max(int(target["count"]), int(entry["count"]))
                target["dirty"] = True
                del self.ledger.entries[key]
            moved = True
        if moved:
            self.log(
                "carried the standing comment over the topic rename %r -> %r"
                % (old_topic, new_topic)
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
                    "sender": sender,
                    "when": time.strftime("%b %d %H:%M UTC", time.gmtime(now)),
                    "quote": neutralise(content),
                }
            )
            entry["links"] = entry["links"][-MAX_LINKS_SHOWN:]
            entry["dirty"] = True
            entry["last_touch"] = now
            self._flush(key, now)  # creates at once; edits wait for the interval
        self.ledger.save()

    # -- writes ----------------------------------------------------------------------------
    def _issue_uuid(self, ident: str) -> str | None:
        if ident not in self._uuid_cache:
            try:
                self._uuid_cache[ident] = self.linear.issue_id(ident)
            except LinearNotFound:
                self._uuid_cache[ident] = None  # not a real issue: skipped, remembered
                self._missing_issues.add(ident)
            except LinearError as exc:
                self.log("linear lookup failed for %s: %s" % (ident, exc))
                return None  # transient: not cached, retried on the next hit
        return self._uuid_cache[ident]

    def _flush(self, key: str, now: float) -> None:
        entry = self.ledger.entries[key]
        if self._drop_if_private(key, entry):
            return  # the stream turned private after the entry was written: it never reaches Linear
        if not entry["dirty"]:
            return
        if now < float(entry.get("retry_after", 0.0)):
            return  # a failed write is backing off
        issue = entry["issue"]
        try:
            uuid = self._issue_uuid(issue)
            if uuid is None:
                if issue in self._missing_issues:
                    entry["dirty"] = False  # unknown issue: drop quietly
                return
            if entry["comment_id"] is None:
                if self.linear.dry_run:
                    self.log(
                        "would create a relay comment for %s (topic %r)" % (issue, entry["topic"])
                    )
                    entry["last_edit"] = now
                    entry["dirty"] = False
                    return
                # A create may have landed while its answer was lost.  Look for the marker
                # before creating anything, so a crash cannot stack a second comment.
                existing = self.linear.find_comment(uuid, entry["marker"])
                if existing:
                    entry["comment_id"] = existing
                    entry["pending"] = False
                    self.log(
                        "adopted an existing relay comment for %s (topic %r)"
                        % (issue, entry["topic"])
                    )
                else:
                    # Write ahead: the intent is on disk before the call, so a crash between the
                    # call and the save is recoverable.
                    entry["pending"] = True
                    self.ledger.save()
                    entry["comment_id"] = self.linear.create_comment(
                        uuid, render_comment(entry, self.realm)
                    )
                    entry["pending"] = False
                    self.log("created a relay comment for %s (topic %r)" % (issue, entry["topic"]))
            else:
                if now - float(entry["last_edit"]) < EDIT_INTERVAL:
                    return  # batched; tick() flushes it later
                try:
                    self.linear.update_comment(
                        entry["comment_id"], render_comment(entry, self.realm)
                    )
                except LinearNotFound:
                    uuid = self._issue_uuid(issue)
                    if uuid is None:
                        return
                    entry["comment_id"] = None
                    entry["pending"] = True
                    self.ledger.save()
                    entry["comment_id"] = self.linear.create_comment(
                        uuid, render_comment(entry, self.realm)
                    )
                    entry["pending"] = False
                self.log("updated the relay comment for %s (topic %r)" % (issue, entry["topic"]))
            entry["last_edit"] = now
            entry["dirty"] = False
            entry["failures"] = 0
            entry["retry_after"] = 0.0
        except LinearError as exc:
            self.log("linear write failed for %s: %s (will retry)" % (issue, exc))
            failures = int(entry.get("failures", 0)) + 1
            entry["failures"] = failures
            entry["retry_after"] = now + min(600.0, 30.0 * (2 ** min(failures, 5)))

    def tick(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        changed = False
        for key in list(self.ledger.entries):
            entry = self.ledger.entries.get(key)
            if entry is None:
                continue
            if self._drop_if_private(key, entry):
                changed = True
                continue
            if entry["dirty"]:
                self._flush(key, now)
                changed = True
        if changed:
            self.ledger.save()

    def reconcile(self) -> None:
        """Adopt the comments of creates whose answer was lost.  Only entries that were written
        ahead are looked at, so the cost is bounded by the number of uncertain writes."""
        recovered = False
        dropped = False
        for key, entry in list(self.ledger.entries.items()):
            if self._drop_if_private(key, entry):
                dropped = True
                continue
            if entry.get("comment_id") or not entry.get("pending"):
                continue
            uuid = self._issue_uuid(entry["issue"])
            if uuid is None:
                continue
            try:
                found = self.linear.find_comment(uuid, entry["marker"])
            except LinearError as exc:
                # A transient Linear failure must not kill the relay; the entry
                # stays pending and is reconciled on the next registration.
                self.log("reconcile lookup failed for %s: %s" % (entry["issue"], exc))
                continue
            if found:
                entry["comment_id"] = found
                entry["pending"] = False
                recovered = True
                self.log(
                    "reconciled a relay comment for %s (topic %r) after an uncertain write"
                    % (entry["issue"], entry["topic"])
                )
        if recovered or dropped:
            self.ledger.save()


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


def public_stream_ids(client: ZulipClient) -> set[int]:
    """Every public stream on the realm.  The relay only ever mirrors these."""
    out: set[int] = set()
    for stream in client.get("streams", {"exclude_archived": True}).get("streams", []):
        if not stream.get("invite_only"):
            out.add(int(stream["stream_id"]))
    return out


def subscribe_public(client: ZulipClient, log: Callable[[str], None]) -> None:
    """Join every public stream the bot is missing (private streams are never joined)."""
    try:
        have = {s["name"] for s in client.get("users/me/subscriptions").get("subscriptions", [])}
        allp = client.get("streams", {"exclude_archived": True}).get("streams", [])
        missing = [
            {"name": s["name"]} for s in allp if not s.get("invite_only") and s["name"] not in have
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
    print("linear auth ok: %s" % linear.viewer())
    ident = (env.get("LINEAR_RELAY_CHECK_ISSUE") or "").strip().upper()
    if not ident:
        print("linear lookup skipped (set LINEAR_RELAY_CHECK_ISSUE to check one issue)")
        return 0
    try:
        linear.issue_id(ident)
    except LinearNotFound:
        print("linear lookup: %s does not exist (the token works; the issue id is wrong)" % ident)
        return 0
    except LinearError as exc:
        print("linear lookup failed for %s: %s" % (ident, exc))
        return 1
    print("linear lookup ok: %s" % ident)
    return 0


def run(env: Mapping[str, str], *, check_only: bool = False) -> int:
    creds = load_zulip_creds(env)
    realm = realm_url(env)
    verify_realm(creds, realm)
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
    types = ["message", "update_message", "stream"]
    while True:
        try:
            subscribe_public(client, worker.log)
            worker.public_streams = public_stream_ids(client)
            queue = EventQueue(client, None, types)
            queue.register()
            worker.reconcile()
            worker.log("relay queue registered%s" % (" (dry run)" if dry else ""))
            while True:
                for event in queue.poll_events(queue.longpoll_timeout or 90):
                    if event.get("type") == "stream":
                        if event.get("op") in ("create", "update"):
                            subscribe_public(client, worker.log)
                            worker.public_streams = public_stream_ids(client)
                        continue
                    worker.handle_event(event)
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
