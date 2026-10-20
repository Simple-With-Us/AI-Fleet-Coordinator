"""The Mac listener's consumer for `#notes-relay` (AFC-331, docs/protocols/per-call-identity.md section 5).

Agents that cannot run apple-notes-coding.sh (the server, a hosted client, a phone) post a notes envelope
(notes.py) to #notes-relay as their own bot.  One listener seat on the Mac (`[notes_relay] seat`, default
CLAUDE) reads the channel, validates each request and runs the direct runner, then reacts:

    check mark   the request ran
    cross mark   the request was refused, with a reply `notes-relay refused <request_id>: <reason>`

A request runs only when ALL of these hold, checked here and nowhere else:

  1. the sender is a fleet agent bot:  a bot account whose seat tag (identity.seat_tag_for) is a live seat,
     a BF-* bot or an enabled GB-* persona (identities.agent_identity_problem).  The owner's own account,
     the integration bots (Sentry, Datadog, Linear, PagerDuty, BetterStack) and retired seats are not;
  2. the message holds one well-formed envelope (notes.parse_message);
  3. envelope.identity is the sender's own identity (not merely an allowed one);
  4. the title's Agent is the sender's own name (notes.title_problem);
  4b. a create or update carries a session id (envelope `session`, AFC-361) unless the sender is a single-thread
     identity (session_ids.is_exempt:  Grok bots, Rob, Jet, Wise, Echo);
  5. the request_id was never run before for that sender (a duplicate gets the earlier outcome's reaction
     and never runs twice).

Message content never reaches a shell:  the runner takes an argv list and the body goes on standard input.
Requests are handled by a worker thread, so a slow osascript (or a Notes.app dialog) cannot stall wake
delivery in the listener's seat threads.  Acceptance is durable:  `offer` first writes the message to a spool
file (<state>/<SEAT>/notes-relay-spool/<id>.json, mode 600) and only then returns True, which is what lets the
daemon advance its event cursor past the message.  The worker removes a spool file when its request has been
answered, and `resume` (every connect of the reading seat) drains whatever a crash or a Zulip outage left, so
a full queue, a restart or a failed reaction never loses a request.  The processed ids are kept in
<state>/<SEAT>/notes-relay.json (mode 600, the newest 2000).

A refusal reply is posted at most once:  Zulip's send-message API has no idempotency key (only `local_id` and
`queue_id`, which echo a message back to the sender's own queue), so before posting `_answer` looks for this
bot's earlier reply with the same label in the topic, and a crash between the reaction and the reply replays
to exactly one reply.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import queue
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import identities as X
from . import identity as I
from . import notes as N
from . import live as LIVE
from . import secretscan
from . import session_ids as SID
from . import wakes as W
from . import zulip as Z

PROCESSED_LIMIT = 2000
SPOOL_LIMIT = (
    1000  # pending requests kept on disk;  past it `offer` says no and the daemon holds the cursor
)
RELAY_DIR = "notes-relay"  # <state>/notes-relay/:  seat-independent, so a change of consumer seat strands nothing
SPOOL_DIR = "spool"
RETRY_SECONDS = 60.0
STOP_TIMEOUT = 10.0  # how long a reload waits for the old worker before leaving it the owner
REPLY_SCAN_PAGE = 100  # messages per page when looking for a refusal this bot already posted
NO_CONSUMER_REASON = (
    "no Notes relay consumer is running on this Mac, so the note was not written.  "
    "Enable [notes_relay] in listener.toml, then send the request again."
)
NO_SCRIPT_REASON = (
    "this Mac cannot run the notes script, so the note was not written.  "
    "Install apple-notes-coding.sh, then send the request again."
)
REPLY_SCAN_PAGES = 20  # at most this many pages (2000 of the bot's own messages in one topic) before returning an incomplete result
LOCK_FILE = "lock"
REASON_LIMIT = 300
STATE_FILE = "processed.json"
DEFAULT_SEAT = "CLAUDE"


def unknown_emoji(exc: Z.ApiError) -> bool:
    """Zulip's answer to an emoji name it does not know:  HTTP 400 naming the emoji.  Nothing else may fall
    through to the alias."""
    text = ("%s %s" % (exc.msg, exc.data.get("msg") if isinstance(exc.data, dict) else "")).lower()
    return (
        exc.status == 400
        and "emoji" in text
        and ("not exist" in text or "invalid" in text or "unknown" in text)
    )


def retryable(exc: Z.ApiError) -> bool:
    """An API failure a later attempt can fix:  rate limit, server error, timeout, or a credential or permission
    error (the owner can repair those).  Anything else 4xx is a permanent refusal."""
    return exc.status is None or exc.status in (401, 403, 408, 429) or exc.status >= 500


def _fsync_dir(path: str) -> None:
    """Make a rename in `path` durable:  without it a power loss can keep the old directory entry."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class MalformedResponse(ValueError):
    """A Zulip page this listener cannot walk (a message id that is not an id, a missing found_newest, a non-empty
    page with no message after the anchor).  The reply is part of the refusal's result, so a caller treats it as a
    retryable failure:  the request stays held and the reply is published on the next pass rather than lost."""


class Refusal(Exception):
    """A request that must not run.  The text is the reason a sender reads in the reply.  `code` is a fixed category
    (the only part that is logged:  the text can quote what the sender typed).  `request_id` is the envelope's, when
    the message held one that parsed:  the reply is labelled with it, which is what notes_status searches for."""

    def __init__(self, text: str, request_id: str | None = None, code: str = "invalid") -> None:
        super().__init__(text)
        self.request_id = request_id
        self.code = code


def safe_reason(reason: str) -> str:
    """The reason as it may be stored and replied:  text that looks like it holds a secret is withheld, since the
    reasons can quote what the sender typed (an identity, a title)."""
    found = secretscan.scan(reason)
    return "(withheld:  the reason quoted text that looks like %s)" % found if found else reason


def sender_identity(person: Mapping[str, Any] | None, owner_user_id: int = 0) -> str:
    """The fleet agent identity of a Zulip user, or Refusal.  `person` is the realm user record (is_bot,
    email, user_id)."""
    if not person:
        raise Refusal("the sender is not a user this listener knows", code="sender_unknown")
    if owner_user_id and person.get("user_id") == owner_user_id:
        raise Refusal(
            "the owner's own account does not send notes requests;  write the note directly",
            code="sender_owner",
        )
    if not person.get("is_bot"):
        raise Refusal("only a fleet agent bot may send a notes request", code="sender_not_bot")
    tag = I.seat_tag_for(person)
    problem = X.agent_identity_problem(tag)
    if problem:
        raise Refusal(
            "the sender is not a fleet agent identity:  %s" % problem[:120], code="sender_identity"
        )
    return tag


def validate(
    message: Mapping[str, Any],
    person: Mapping[str, Any] | None,
    *,
    owner_user_id: int = 0,
    registry_path: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """(sender identity, envelope) for a request that may run, else Refusal.  A refusal of a message whose envelope
    parses carries that envelope's request_id, whichever check refused it."""
    try:
        parsed: dict[str, Any] | None = N.parse_message(message.get("content"))
        parse_error: N.EnvelopeError | None = None
    except N.EnvelopeError as exc:
        parsed, parse_error = None, exc
    request_id = parsed["request_id"] if parsed is not None else None
    try:
        identity = sender_identity(person, owner_user_id)
    except Refusal as exc:
        raise Refusal(str(exc), request_id, exc.code) from None
    if parsed is None:
        raise Refusal("not a notes request:  %s" % parse_error, code="envelope") from None
    if parsed["identity"] != identity:
        raise Refusal(
            "envelope identity %s is not the sender, %s" % (parsed["identity"][:40], identity),
            request_id,
            "identity_mismatch",
        )
    problem = N.title_problem(parsed["title"], identity, registry_path=registry_path)
    if problem:
        raise Refusal(problem, request_id, "title")
    if parsed["action"] in ("create", "update"):
        # AFC-361:  a write carries the session id of the agent that sent it.  The exemption is judged from the
        # sender's own bot (identity, checked above), never from a field of the envelope, and the listener's own
        # environment is not the sender's, so only the envelope's value counts (env={}).
        try:
            SID.require(identity, parsed.get("session"), {}, "relay")
        except ValueError as exc:  # SessionRequired is a ValueError
            raise Refusal(str(exc), request_id, "session") from None
    return identity, parsed


class Processed:
    """request ids already run, per sender identity, in a mode-600 file.  Thread-safe."""

    def __init__(self, path: str, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock
        self.lock = threading.Lock()

    def _read(self) -> dict[str, Any]:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write(self, rows: Mapping[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            os.fchmod(fh.fileno(), 0o600)
            json.dump({"rows": rows}, fh, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        # The outcome must be on disk before the action it records is acknowledged, or a power loss could keep
        # the spool but roll this record back and run the Notes action again:  sync the directory entry too.
        _fsync_dir(os.path.dirname(self.path))

    @staticmethod
    def key(identity: str, request_id: str) -> str:
        return "%s:%s" % (identity, request_id)

    def begin(self, identity: str, request_id: str, message_id: int) -> str | None:
        """None when this request is new (now marked `started`), else the earlier outcome
        (started, done_pending, done, refused_pending, refused, unknown_pending or unknown)."""
        with self.lock:
            rows = dict(self._read().get("rows") or {})
            key = self.key(identity, request_id)
            if key in rows:
                return str(rows[key].get("outcome") or "started")
            rows[key] = {"outcome": "started", "message_id": message_id, "ts": int(self.clock())}
            if len(rows) > PROCESSED_LIMIT:
                for old in sorted(rows, key=lambda k: rows[k].get("ts", 0))[
                    : len(rows) - PROCESSED_LIMIT
                ]:
                    rows.pop(old, None)
            self._write(rows)
            return None

    def finish(
        self, identity: str, request_id: str, outcome: str, reason: str | None = None
    ) -> None:
        """Record where a request stands.  The outcomes are started, done_pending (the note was written, the
        check mark is not posted yet), done, refused_pending (the reason is recorded, the cross mark and reply are
        not posted yet) and refused.  A `_pending` row is how a retry knows to publish and never to run again."""
        with self.lock:
            rows = dict(self._read().get("rows") or {})
            key = self.key(identity, request_id)
            if key in rows:
                rows[key]["outcome"] = outcome
                if reason is not None:
                    rows[key]["reason"] = reason[:REASON_LIMIT]
                self._write(rows)

    def reason(self, identity: str, request_id: str) -> str | None:
        with self.lock:
            row = (self._read().get("rows") or {}).get(self.key(identity, request_id))
        return (
            row.get("reason")
            if isinstance(row, dict) and isinstance(row.get("reason"), str)
            else None
        )

    def forget(self, identity: str, request_id: str) -> None:
        """Drop a `started` row, for a failure before anything ran (so a retry starts clean)."""
        with self.lock:
            rows = dict(self._read().get("rows") or {})
            key = self.key(identity, request_id)
            if rows.get(key, {}).get("outcome") == "started":
                rows.pop(key)
                self._write(rows)


class Spool:
    """Pending relay messages, one mode-600 JSON file per Zulip message id.  Thread-safe."""

    def __init__(self, path: str, limit: int = SPOOL_LIMIT) -> None:
        self.path = path
        self.limit = limit
        self.lock = threading.Lock()

    def _file(self, message_id: int) -> str:
        return os.path.join(self.path, "%d.json" % message_id)

    def put(self, message: Mapping[str, Any]) -> bool:
        """Write the message durably (fsync, then rename).  False when it cannot be kept."""
        message_id = message.get("id")
        if not isinstance(message_id, int) or isinstance(message_id, bool):
            return False
        with self.lock:
            try:
                os.makedirs(self.path, mode=0o700, exist_ok=True)
                target = self._file(message_id)
                if not os.path.exists(target) and len(self.ids()) >= self.limit:
                    return False
                tmp = target + ".tmp"
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    os.fchmod(fh.fileno(), 0o600)
                    json.dump(dict(message), fh, separators=(",", ":"))
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, target)
                _fsync_dir(self.path)
            except (OSError, TypeError, ValueError):
                return False
        return True

    def ids(self) -> list[int]:
        try:
            names = os.listdir(self.path)
        except OSError:
            return []
        return sorted(int(n[:-5]) for n in names if n.endswith(".json") and n[:-5].isdigit())

    def load(self, message_id: int) -> dict[str, Any] | None:
        try:
            with open(self._file(message_id), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def remove(self, message_id: int) -> None:
        with self.lock:
            try:
                os.unlink(self._file(message_id))
            except OSError:
                pass


def in_relay_channel(message: Mapping[str, Any], channel: str = N.RELAY_CHANNEL) -> bool:
    """Is this a message in the relay channel?  Every seat's runner treats one as consumed (it is not work for
    any session), whichever seat reads it and whether or not a consumer is running.  Without this, a request that
    arrived while the relay was off was routed like any chatter and lost with nothing said (AFC-407)."""
    return (
        message.get("type") == "stream"
        and Z.message_channel(message).casefold() == channel.casefold()
    )


def react(runner: Any, message_id: int, names: tuple[str, ...], log: Callable[..., None]) -> None:
    """Set the first emoji Zulip accepts.  A network failure propagates (the request stays spooled and is
    published on the retry);  an emoji Zulip does not know falls through to the next name."""
    for name in names:
        try:
            runner.client.post("messages/%d/reactions" % message_id, {"emoji_name": name})
            return
        except Z.ApiError as exc:
            if exc.code == "REACTION_ALREADY_EXISTS":
                return
            if unknown_emoji(exc):
                continue  # this name is not an emoji in this realm:  try the alias
            if retryable(exc):
                raise  # 429, 5xx, a permission error:  the request stays spooled and is published on the retry
            log("notes-relay-reaction-refused", id=message_id, status=exc.status)
            return  # a permanent refusal (the message is gone):  nothing a retry could change
    log("notes-relay-no-emoji", id=message_id, names=list(names))


def already_replied(
    runner: Any,
    original: Mapping[str, Any],
    message_id: int,
    prefix: str,
    log: Callable[..., None],
) -> bool:
    """Did this bot already post the refusal?  Reads this bot's own messages in the topic after the request,
    forward in pages until the reply is found or the topic's newest message is reached, so a refusal posted
    after a long backlog is still seen.  The scan is bounded;  past the bound it raises and keeps the request held."""
    me = getattr(runner.me, "user_id", None)
    narrow = json.dumps(
        [
            {"operator": "channel", "operand": original.get("stream_id")},
            {"operator": "topic", "operand": Z.message_topic(original)},
        ]
        + ([{"operator": "sender", "operand": me}] if isinstance(me, int) else [])
    )
    anchor = message_id
    deadline = time.monotonic() + 10.0
    for _ in range(REPLY_SCAN_PAGES):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise MalformedResponse("refusal reply scan deadline exceeded")
        found = runner.client.get(
            "messages",
            {
                "narrow": narrow,
                "anchor": anchor,
                "num_before": 0,
                "num_after": REPLY_SCAN_PAGE,
                "include_anchor": "false",
                "apply_markdown": "false",
            },
            timeout=remaining,
        )
        if not isinstance(found, Mapping) or not isinstance(found.get("messages"), list):
            raise MalformedResponse("Zulip messages response must include a list of messages")
        page = [m for m in found.get("messages") or [] if isinstance(m, dict)]
        if any(
            m.get("sender_id") == me and str(m.get("content") or "").startswith(prefix)
            for m in page
        ):
            return True
        ids: list[int] = []
        for m in page:
            candidate_id = m.get("id")
            if not isinstance(candidate_id, int) or isinstance(candidate_id, bool):
                raise MalformedResponse("Zulip message id must be an integer")
            if candidate_id > anchor:
                ids.append(candidate_id)
        found_newest = found.get("found_newest")
        if not isinstance(found_newest, bool):
            raise MalformedResponse("Zulip messages response must include a boolean found_newest")
        if found_newest:
            return False  # the topic's newest message has been read:  no reply yet
        if not page:
            # Zulip's found_newest is a coverage flag, not a cursor flag:  when the anchor is newer than every message
            # matching the narrow, all of them sit on the before side and num_before 0 returns none.  An empty page is
            # the end of the forward scan, not a malformed answer, so the refusal is still posted -- but only when the
            # server actually sent a message list:  a missing or garbled "messages" payload is malformed, not an empty
            # topic, so the request stays held instead of a second refusal being published.
            if not isinstance(found.get("messages"), list):
                raise MalformedResponse("Zulip messages response must include a list of messages")
            return False
        if not ids:
            raise MalformedResponse("Zulip messages response omitted a valid next anchor")
        anchor = max(ids)
    log("notes-relay-reply-scan-capped", id=message_id, pages=REPLY_SCAN_PAGES)
    raise MalformedResponse("refusal reply scan incomplete")


def is_request(message: Mapping[str, Any]) -> bool:
    """Does this message hold a notes envelope?  A message in the relay channel that is not a request is not work
    for the consumer, and a refusal must not answer it."""
    try:
        N.parse_message(message.get("content"))
    except Exception:  # noqa: BLE001 - a body that does not parse is not a request
        return False
    return True


def _refuse_unconsumed_locked(
    runner: Any,
    message: Mapping[str, Any],
    reason: str,
    log: Callable[..., None],
    row: str,
    unknown: bool,
) -> bool:
    """Refuse a relay request that no consumer on this Mac will write (the relay is off, or this host cannot run
    the notes script).  The requester reads the cross mark and the reply, so `notes_status` answers "refused"
    with the reason instead of "queued" for ever.  The reply relays the request's own id, so that text goes
    through the same credential scan and mention neutralising as the reason.  The log carries the id and a code,
    never the reason text."""
    message_id = message.get("id")
    if not isinstance(message_id, int) or isinstance(message_id, bool):
        return False
    try:
        validate(message, runner.users.get(message.get("sender_id")),
                 owner_user_id=int(runner.daemon.config.owner_user_id or 0))
    except Refusal as exc:
        log("notes-relay-unconsumed-invalid", id=message_id, code=exc.code)
        return False
    request_id: str | None = None
    try:
        parsed = N.parse_message(message.get("content"))
    except Exception:  # noqa: BLE001 - a body that does not parse has no label
        parsed = None
    if isinstance(parsed, dict):
        candidate = parsed.get("request_id")
        request_id = candidate if isinstance(candidate, str) and candidate else None
    label = W.neutralize_mentions(safe_reason(request_id or "(message %d)" % message_id))
    prefix = N.refusal_label(label)
    try:
        react(runner, message_id, N.REFUSED_EMOJI, log)
        response = runner.client.get("messages/%d" % message_id, {"apply_markdown": False})
        original = response.get("message") if isinstance(response, Mapping) else None
        if (not isinstance(original, Mapping)
                or not isinstance(original.get("stream_id"), int)
                or isinstance(original.get("stream_id"), bool)
                or not isinstance(original.get("topic", original.get("subject")), str)):
            raise MalformedResponse("refusal destination must contain stream_id and topic")
        if already_replied(runner, original, message_id, prefix, log):
            LIVE.write_json(row, {"state": "ok"}, durable=True)
            return True
        if unknown:
            raise MalformedResponse("previous refusal outcome is unknown; reconcile before retry")
        LIVE.write_json(row, {"state": "unknown"}, durable=True)
        text = W.neutralize_mentions(safe_reason(" ".join(reason.split())[:REASON_LIMIT]))
        runner.client.post(
            "messages",
            {
                "type": "stream",
                "to": original.get("stream_id"),
                "topic": Z.message_topic(original),
                "content": "%s %s" % (prefix, text),
            },
        )
    except MalformedResponse as exc:
        log("notes-relay-reply-malformed", id=message_id, error=str(exc)[:120])
        raise  # the reply is part of the result:  the request stays held and the next pass publishes it
    except Z.ApiError as exc:
        # A server rejection is definite; a retry may send again after the condition clears.
        LIVE.write_json(row, {"state": "refused"}, durable=True)
        if retryable(exc):
            raise  # the reply is part of the result:  the next pass publishes it
        log("notes-relay-reply-failed", id=message_id, status=exc.status)
        return False
    LIVE.write_json(row, {"state": "ok"}, durable=True)
    log("notes-relay-unconsumed", id=message_id, code="no_consumer")
    return True


def refuse_unconsumed(runner: Any, message: Mapping[str, Any], reason: str,
                     log: Callable[..., None]) -> bool:
    """Serialize reconciliation and sending, retaining ambiguous outcomes across restarts."""
    message_id = message.get("id")
    if not isinstance(message_id, int) or isinstance(message_id, bool):
        return False
    key = hashlib.sha256((str(runner.me.user_id) + ":" + str(message_id)).encode()).hexdigest()
    row = os.path.join(runner.paths.root, RELAY_DIR, "refusals", key + ".json")
    with LIVE.locked(row):
        previous = LIVE.read_json(row)
        if previous.get("state") == "ok":
            return True
        return _refuse_unconsumed_locked(
            runner, message, reason, log, row, previous.get("state") == "unknown"
        )


class Consumer:
    """One per listener.  `offer(runner, message)` is called from the seat thread that routes the message and
    returns at once;  a worker thread does the rest."""

    def __init__(
        self,
        *,
        seat: str,
        state_dir: str,
        script: Callable[[], Path | None],
        env: Mapping[str, str],
        owner_user_id: Callable[[], int] = lambda: 0,
        runner: Callable[..., Any] | None = None,
        log: Callable[..., None] = lambda event, **fields: None,
        clock: Callable[[], float] = time.time,
        registry_path: str | None = None,
        channel: str = N.RELAY_CHANNEL,
    ) -> None:
        self.seat = seat
        self.channel = channel
        self.script = script
        self.env = dict(env)
        self.owner_user_id = owner_user_id
        self.runner = runner
        self.log = log
        self.registry_path = registry_path
        # One relay state directory under the state root, whichever seat consumes:  when the consumer seat changes (the
        # fleet knob or the local config), the new consumer drains the old one's pending requests, whose cursor has
        # already moved on.  The lock file in it is the single owner, across seats and processes.
        self.processed = Processed(os.path.join(state_dir, RELAY_DIR, STATE_FILE), clock)
        self.spool = Spool(os.path.join(state_dir, RELAY_DIR, SPOOL_DIR))
        self.jobs: queue.Queue[bool | None] = queue.Queue(
            1
        )  # a wake-up signal;  the work is in the spool
        self.reader: Any = None  # the reading seat's runner, for the worker
        self.thread: threading.Thread | None = None
        self.stopped = threading.Event()
        self.dropped = 0
        self.retry_seconds = (
            RETRY_SECONDS  # a request whose answer failed is tried again this often
        )

    # ---- the seat thread's side ---------------------------------------------------------------
    def in_channel(self, message: Mapping[str, Any]) -> bool:
        """Is this a message in the relay channel?  Every seat's runner treats one as consumed (it is not work
        for any session), whichever seat reads it."""
        return in_relay_channel(message, self.channel)

    def wants(self, runner: Any, message: Mapping[str, Any]) -> bool:
        """Should this seat's reader take the message?  Only the configured seat.  The reader's own posts are
        taken when they hold a valid envelope (a session of the reading seat that runs elsewhere, such as the
        server, relays as that same bot) and skipped otherwise, so its own refusal replies are never re-read."""
        if runner.seat != self.seat or not self.in_channel(message):
            return False
        if message.get("sender_id") != getattr(runner.me, "user_id", None):
            return True
        content = message.get("content")
        if not isinstance(content, str) or content.lstrip().startswith(N.REFUSAL_PREFIX):
            return False
        try:
            N.parse_message(content)
        except N.EnvelopeError:
            return False
        return True

    def offer(self, runner: Any, message: Mapping[str, Any]) -> bool:
        """Accept a message durably, then wake the worker.  True means the request is on disk and will be
        answered, so the caller may advance its cursor;  False means it could not be kept (the caller holds
        the cursor and the message is logged)."""
        self.reader = runner
        if not self.spool.put(message):
            self.dropped += 1
            self.log(
                "notes-relay-not-accepted",
                id=message.get("id"),
                dropped=self.dropped,
                pending=len(self.spool.ids()),
            )
            return False
        self.start()
        self._signal()
        return True

    def resume(self, runner: Any) -> None:
        """The reading seat (re)connected:  drain whatever an earlier run left in the spool."""
        self.reader = runner
        if self.spool.ids():
            self.start()
            self._signal()

    def _signal(self) -> None:
        try:
            self.jobs.put_nowait(True)
        except queue.Full:
            pass  # a wake-up is already pending

    def start(self) -> None:
        if self.stopped.is_set():
            return  # retiring:  the replacement drains what is spooled
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._work, name="notes-relay", daemon=True)
            self.thread.start()

    def stop(self, timeout: float = STOP_TIMEOUT) -> bool:
        """Ask the worker to finish and wait for it, at most `timeout` seconds.  True when it has exited (or never
        ran).  False means it is still inside a request (a slow script):  the caller must leave this consumer
        the owner of the spool and not start a second one."""
        self.stopped.set()
        try:
            self.jobs.put_nowait(None)
        except queue.Full:
            try:
                self.jobs.get_nowait()
                self.jobs.put_nowait(None)
            except (queue.Empty, queue.Full):
                pass
        thread = self.thread
        if thread is None or thread is threading.current_thread():
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def has_exited(self) -> bool:
        """Has a stopped consumer's worker left?  True once it is safe to hand the spool to a replacement."""
        thread = self.thread
        return thread is None or not thread.is_alive()

    # ---- the worker ---------------------------------------------------------------------------
    def _work(self) -> None:
        while not self.stopped.is_set():
            try:
                if self.jobs.get(timeout=self.retry_seconds) is None:
                    return
            except queue.Empty:
                if not self.spool.ids():
                    continue  # nothing pending:  keep waiting
            try:
                self.drain()
            except Exception as exc:  # noqa: BLE001 - one bad request must not end the worker
                self.log("notes-relay-error", error=type(exc).__name__)

    def drain(self) -> int:
        """Answer every spooled request, oldest first, on the calling thread.  A request whose answer could not
        be sent (Zulip unreachable) stays in the spool for the next wake-up or reconnect.  Returns how many
        were finished."""
        with self._owner_lock() as owned:
            if not owned:
                self.log("notes-relay-busy", reason="another consumer holds the spool")
                return 0
            return self._drain_owned()

    @contextlib.contextmanager
    def _owner_lock(self):
        """An exclusive, non-blocking flock on <state>/<SEAT>/notes-relay.lock for the length of a drain.  It is
        the spool's single owner across processes (two daemons on one state directory) and across consumers (a
        reload's replacement), so no request is claimed twice and no `started` row is mistaken for a crash."""
        path = os.path.join(os.path.dirname(self.spool.path), LOCK_FILE)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _drain_owned(self) -> int:
        finished = 0
        for message_id in self.spool.ids():
            if self.stopped.is_set():
                break  # a reload asked this worker to finish:  leave the rest for its successor
            runner = self.reader
            message = self.spool.load(message_id)
            if message is None:
                self.spool.remove(message_id)
                continue
            if runner is None:
                break
            try:
                self.process(runner, message)
            except Exception as exc:  # noqa: BLE001 - keep the file;  the next wake-up retries
                self.log("notes-relay-retry", id=message_id, error=type(exc).__name__)
                break
            self.spool.remove(message_id)
            finished += 1
        return finished

    def process(self, runner: Any, message: Mapping[str, Any]) -> str:
        """Handle one relay message.  Returns done, refused, duplicate or ignored."""
        message_id = int(message["id"])
        person = runner.users.get(message.get("sender_id"))
        try:
            identity, envelope = validate(
                message,
                person,
                owner_user_id=int(self.owner_user_id() or 0),
                registry_path=self.registry_path,
            )
        except Refusal as exc:
            self.log(
                "notes-relay-refused", id=message_id, code=exc.code
            )  # a category only:  the text quotes the sender
            self._answer(runner, message_id, exc.request_id, refused=str(exc))
            return "refused"
        request_id = envelope["request_id"]
        earlier = self.processed.begin(identity, request_id, message_id)
        if earlier is not None:
            self.log("notes-relay-duplicate", id=message_id, earlier=earlier)
            if earlier in ("done", "done_pending"):
                # The note was written (maybe by an earlier attempt of this very message):  publish the check mark,
                # never write it again.
                self._react(runner, message_id, N.DONE_EMOJI)
                self.processed.finish(identity, request_id, "done")
                return "done" if earlier == "done_pending" else "duplicate"
            unknown = earlier in ("started", "unknown", "unknown_pending")
            if unknown:
                reason = (
                    self.processed.reason(identity, request_id)
                    or "this request id was already started and may have been interrupted;  check the note, "
                    "then send it again with a new request_id"
                )
            else:
                reason = (
                    self.processed.reason(identity, request_id)
                    or "this request id was already refused"
                )
            if earlier in ("refused", "unknown"):
                # Already published:  never downgrade the row.  _answer is a no-op once the reply exists and the
                # reaction is idempotent, so a network failure here leaves the row as it was.
                self._answer(runner, message_id, request_id, refused=reason)
                return "duplicate"
            kind = "unknown" if earlier.startswith("unknown") else "refused"
            self.processed.finish(identity, request_id, kind + "_pending", reason)
            self._answer(runner, message_id, request_id, refused=reason)
            self.processed.finish(identity, request_id, kind)
            return "duplicate"
        try:
            script = self.script()
            if script is None:
                raise N.NotesError("unavailable", "this Mac cannot run the notes script right now")
            try:
                full = N.resolve_upload(envelope, lambda path: self._download(runner, path))
            except Z.NetworkError:
                self.processed.forget(identity, request_id)  # nothing ran:  the retry starts clean
                raise
            # A bot can post a well-formed envelope straight to Zulip, or attach a body, without the tool-side scan:  the
            # consumer scans what it is about to write, after the upload is resolved.  A hit is a refusal that names
            # the field and never quotes any of it.
            for field, text in N.text_fields(full).items():
                if secretscan.scan(text):
                    raise N.NotesError(
                        "secret",
                        "the %s looked like it contains a secret;  nothing was written" % field,
                    )
            creds = getattr(runner, "creds", None)
            full = N.with_provenance(
                full, identity, message_id, getattr(creds, "site", "") or N.PROVENANCE_SITE
            )
            kwargs = {"runner": self.runner} if self.runner is not None else {}
            N.run_direct(full, script, env=self.env, **kwargs)
        except N.NotesError as exc:
            # A timeout (outcome_unknown) is not a definite failure:  the script may have written the note.  It is
            # recorded as `unknown`, so a retry of the same request id is refused as "may have been interrupted"
            # and never runs the action a second time.
            return self._refuse(
                runner,
                message_id,
                identity,
                request_id,
                exc.message,
                "notes-relay-failed",
                exc.code,
                kind="unknown" if exc.code == "outcome_unknown" else "refused",
            )
        except Z.AgentSyncError as exc:
            if isinstance(exc, Z.NetworkError):
                raise
            return self._refuse(
                runner,
                message_id,
                identity,
                request_id,
                "could not fetch the attached body:  %s" % str(exc)[:100],
                "notes-relay-failed",
                "zulip",
            )
        # The action is done.  Record that BEFORE publishing, so a failed publish retries only the acknowledgement.
        self.processed.finish(identity, request_id, "done_pending")
        self.log("notes-relay-done", id=message_id, action=envelope["action"], identity=identity)
        self._react(runner, message_id, N.DONE_EMOJI)
        self.processed.finish(identity, request_id, "done")
        return "done"

    def _refuse(
        self,
        runner: Any,
        message_id: int,
        identity: str,
        request_id: str,
        reason: str,
        event: str,
        code: str,
        kind: str = "refused",
    ) -> str:
        reason = safe_reason(reason)
        self.processed.finish(identity, request_id, kind + "_pending", reason)
        self.log(event, id=message_id, code=code)
        self._answer(runner, message_id, request_id, refused=reason)
        self.processed.finish(identity, request_id, kind)
        return "refused"

    # ---- Zulip --------------------------------------------------------------------------------
    def _react(self, runner: Any, message_id: int, names: tuple[str, ...]) -> None:
        """Set the first emoji Zulip accepts.  A network failure propagates (the request stays spooled and is
        published on the retry);  an emoji Zulip does not know falls through to the next name."""
        react(runner, message_id, names, self.log)

    def _answer(
        self, runner: Any, message_id: int, request_id: str | None, *, refused: str
    ) -> None:
        """Cross mark, then one reply with the reason.  Safe to replay after a crash at any point:  the reaction
        is idempotent and the reply is skipped when this bot already posted one with the same label."""
        self._react(runner, message_id, N.REFUSED_EMOJI)
        reason = W.neutralize_mentions(safe_reason(" ".join(refused.split())[:REASON_LIMIT]))
        label = request_id or "(message %d)" % message_id
        prefix = N.refusal_label(label)
        try:
            original = (
                runner.client.get("messages/%d" % message_id, {"apply_markdown": False}).get(
                    "message"
                )
                or {}
            )
            if self._already_replied(runner, original, message_id, prefix):
                return
            runner.client.post(
                "messages",
                {
                    "type": "stream",
                    "to": original.get("stream_id"),
                    "topic": Z.message_topic(original),
                    "content": "%s %s" % (prefix, reason),
                },
            )
        except MalformedResponse as exc:
            self.log("notes-relay-reply-malformed", id=message_id, error=str(exc)[:120])
            raise  # the reply is part of the result:  keep the request spooled and post it on the retry
        except Z.ApiError as exc:
            if retryable(exc):
                raise  # the reply is part of the result:  keep the request spooled and post it on the retry
            self.log(
                "notes-relay-reply-failed", id=message_id, status=exc.status
            )  # permanent:  not retried

    def _already_replied(
        self, runner: Any, original: Mapping[str, Any], message_id: int, prefix: str
    ) -> bool:
        """Did this bot already post the refusal?  Reads this bot's own messages in the topic after the request,
        forward in pages until the reply is found or the topic's newest message is reached, so a refusal posted
        after a long backlog is still seen.  The scan is bounded;  past the bound it raises and keeps the request held."""
        return already_replied(runner, original, message_id, prefix, self.log)

    def _download(self, runner: Any, path: str) -> bytes:
        """The uploaded .md at /user_uploads/..., through the reading bot's own credential:  Zulip hands back a
        short-lived signed URL for the file, which is then fetched without credentials."""
        if not N.UPLOAD_PATH_RE.match(path) or ".." in path:
            raise N.NotesError(
                "invalid_argument", "the attachment path is not a /user_uploads/ path"
            )
        answer = runner.client.get("user_uploads/" + path[len("/user_uploads/") :])
        url = str(answer.get("url") or "")
        if not url:
            raise N.NotesError("unavailable", "Zulip gave no download URL for the attachment")
        base = urllib.parse.urlparse(runner.client.realm)
        target = urllib.parse.urlparse(urllib.parse.urljoin(runner.client.realm + "/", url))
        if (target.scheme, target.netloc) != (base.scheme, base.netloc):
            raise N.NotesError("unavailable", "the attachment is not on this realm's host")
        return fetch_unauthenticated(runner.client, target.geturl())


def fetch_unauthenticated(client: Any, url: str, limit: int = N.UPLOAD_MAX) -> bytes:
    """GET `url` with no Authorization header (the URL carries its own signature), capped at `limit` bytes."""
    import urllib.request

    request = urllib.request.Request(url, headers={"User-Agent": Z.USER_AGENT})
    try:
        with client._opener.open(request, timeout=client.timeout) as response:
            data = response.read(limit + 1)
    except OSError as exc:
        raise Z.NetworkError("fetching the attachment failed:  %s" % type(exc).__name__) from None
    if len(data) > limit:
        raise N.NotesError("invalid_argument", "the attachment is larger than %d bytes" % limit)
    return data
