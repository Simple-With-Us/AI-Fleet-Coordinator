"""Native Zulip inbox delivery for an owner channel broadcast (AFC-938).

The owner asked for two things about a channel-wide mention:  the message must be UNREAD in
every fleet bot's own Zulip inbox, and `@**everyone**` must wake nobody while `@**all**`
keeps the wake path it already has.  Zulip delivers a channel message to a subscriber's
inbox and marks it read once a listener sees it, so the listener would otherwise empty the
very inbox the owner wants filled.

The Zulip invariant:  a message can only be unread for a user subscribed to its channel.
So every bot, with that bot's own credentials, is subscribed (quiet) to the message's
channel and then has the message's read flag removed.  A private channel is never joined:
it is skipped and logged.

Credentials come from Infisical only (`zulip.infisical_roster`, `zulip.prefetch_infisical`,
`zulip.infisical_credentials`).  There is no environment fallback, no zuliprc fallback and
no config override:  when Infisical is unreachable or empty, the delivery fails loudly with
an `inbox-delivery-failed` event instead of pretending to have delivered.  The roster, not
the seat list, is the enumeration boundary:  a seat whose key is missing is named as
`missing-credentials` instead of vanishing from the fleet without a word.

A bot is only addressed on the realm the message came from:  a credential whose own site is
another host is skipped (`inbox-delivery-skipped`, reason `other-realm`), so a key is never
sent to a server it does not belong to.

The delivery is a set of small steps per bot (`subscribe`, `quiet`, `unread`), each one
idempotent.  The caller's `done` map says which steps a bot already completed, so a retry
runs only the steps that are still missing:  a bot that already got the message never runs
again, and a bot that failed half way does not repeat the half that worked.

Every log line carries the seat tag, the message id, an error type and an outcome:  never an
email, a key, or the message content.  A key may appear only as a masked preview (the first
eight and the last four characters), so a refused key can be told apart without ever being
written out in full.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import zulip as Z
from .subscribe_all import QUIET

#: The transport:  http(method, path, payload) -> the parsed answer.  Injectable, so the whole
#: delivery runs offline in a test.
Http = Callable[[str, str, Mapping[str, Any] | None], Mapping[str, Any]]

#: The log:  log(event, **fields).
Log = Callable[..., None]

#: The transport factory:  credentials, source realm -> that bot's transport.  The realm is part
#: of the call, so a bot is addressed on the server the message came from and never on another.
HttpFactory = Callable[[Z.Credentials, str], Http]

#: The outcome of one bot's delivery.
DELIVERED = "delivered"
SKIPPED = "skipped"
RETRYABLE = "retryable"
TERMINAL = "terminal"
UNKNOWN = "unknown"
MISSING_CREDENTIALS = "missing-credentials"

#: Every outcome, so the result always carries a count for each of them.
OUTCOMES = (DELIVERED, SKIPPED, RETRYABLE, TERMINAL, UNKNOWN, MISSING_CREDENTIALS)

#: The outcomes that a later attempt may still fix:  the seat stays on the retry queue.
PENDING_OUTCOMES = (RETRYABLE, UNKNOWN, MISSING_CREDENTIALS)

#: One bot's delivery, as steps:  subscribe to the channel, make the subscription quiet, then
#: take the read flag off.  Every step is idempotent, so a step that may have been applied can
#: be run again without harm.
STEPS = ("subscribe", "quiet", "unread")


#: A variable or secret name:  upper case, with at least one underscore.  A value never
#: matches, so a message that quotes one cannot put it in the log.
_NAME_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")


class DeliveryError(Exception):
    """The delivery could not be completed:  no credentials, or one bot refused the message."""


@dataclass(frozen=True)
class Targets:
    """The fleet bots the delivery may address, and the ones it may not.

    `targets` holds (seat tag, credentials) pairs.  `skipped` holds the seat tags whose
    credentials are missing, mapped to the names of the variables or secrets that are missing:
    a name, never a value.  A skipped seat is not a silent gap:  the caller reports it and
    keeps it on the retry queue, so a repaired key is picked up by a later attempt."""

    targets: list[tuple[str, Z.Credentials]] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)


class _Step(Exception):
    """One write of a bot's delivery failed.

    `write` says whether the failing request could have changed anything:  a write that
    timed out may have been applied, a read never was.  The classifier needs that to say
    `retryable` or `unknown`."""

    def __init__(self, cause: Exception) -> None:
        super().__init__(str(cause))
        self.cause = cause


def zulip_http(creds: Z.Credentials, realm: str | None = None) -> Http:
    """A transport bound to one bot's own credentials and site.

    The bot's own site comes from its own credentials, so no realm is read from the
    environment here:  a bot is never addressed on somebody else's server."""
    client = Z.ZulipClient(creds, realm or creds.site)

    def call(method: str, path: str, payload: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        if method == "GET":
            return client.get(path, payload)
        return client.post(path, payload)

    # The bound client, so a caller (and a test) can see which realm is addressed.
    call.client = client  # type: ignore[attr-defined]
    return call


def missing_credential_name(exc: Exception) -> str:
    """The name of the variable or secret that is missing, never a value.

    `Z.CredentialError` carries no attribute for it, so the name comes from the message:
    only upper-case identifiers (a variable or key name) are kept, so a value can never
    reach the log through it.  The message's tail after "not set:" is preferred, because
    that is where the reader puts the name that is missing."""
    for attr in ("name", "variable", "secret", "secret_name", "key_name"):
        value = getattr(exc, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    text = str(exc)
    tail = text.rsplit("not set:", 1)[-1] if "not set:" in text else text
    names = list(dict.fromkeys(_NAME_RE.findall(tail)))
    return ", ".join(names) if names else "unknown"


def key_preview(key: Any) -> str:
    """A masked preview of an API key:  the first eight and the last four characters only.

    A short key gets one leading character instead, because eight plus four of a value
    under 24 characters would disclose most of it.  The full key never reaches a log."""
    return Z.masked_credential(key)


def infisical_targets(
    *,
    env: Mapping[str, str],
    home: Path,
    log: Log | None = None,
) -> Targets:
    """Every fleet bot's seat tag and credentials, from Infisical only.

    The roster is the enumeration boundary:  a seat that has only some of its three names is
    named and skipped, mapped to the names that are missing, so it never vanishes from the
    fleet without a word.  A seat whose names are all there but whose credentials cannot be
    read is skipped the same way.  One bot's missing key never stops the others.

    When Infisical cannot be read at all, or holds no fleet bot names, this raises:  the
    caller logs the failure and retries later rather than delivering with the wrong identity.
    The caller passes the daemon's own env and home:  nothing here reads the process
    environment or guesses a default."""
    roster = Z.infisical_roster(env, home)
    if not roster:
        raise DeliveryError("Infisical returned no fleet bot credentials")
    skipped: dict[str, str] = {}
    complete: list[str] = []
    for seat, missing in sorted(roster.items()):
        if missing:
            skipped[seat] = ", ".join(missing)
        else:
            complete.append(seat)
    if complete:
        Z.prefetch_infisical(complete, env, home)
    targets: list[tuple[str, Z.Credentials]] = []
    for seat in complete:
        try:
            targets.append((seat, Z.infisical_credentials(seat, env=env, home=home)))
        except Z.CredentialError as exc:
            missing = missing_credential_name(exc)
            skipped[seat] = missing
            if log is not None:
                log(
                    "inbox-delivery-failed",
                    seat=seat,
                    error=type(exc).__name__,
                    missing=missing,
                )
    return Targets(targets=targets, skipped=skipped)


def fetch_streams(http: Http) -> list[Mapping[str, Any]]:
    """The realm's streams, as Zulip answered them.

    The answer is checked at the boundary:  a malformed answer is a failure, never a silent
    "no such channel", because the delivery must not subscribe a bot to a channel it could
    not read."""
    answer = http("GET", "streams", {"include_public": "true", "include_subscribed": "true"})
    if not isinstance(answer, Mapping):
        raise DeliveryError("malformed streams answer")
    streams = answer.get("streams")
    if not isinstance(streams, list):
        raise DeliveryError("malformed streams answer")
    out: list[Mapping[str, Any]] = []
    for stream in streams:
        if not isinstance(stream, Mapping):
            raise DeliveryError("malformed streams answer")
        out.append(stream)
    return out


def channel_state(streams: Sequence[Mapping[str, Any]], channel: str) -> tuple[bool, int]:
    """(the channel is public, its stream id) in a fetched streams list.

    A channel that is private, or that the bot cannot see at all, is not public:  the
    delivery never joins it and never creates it, because the add endpoint takes names and
    would make a channel that does not exist.

    A matching stream must carry a name, an `invite_only` flag and a stream id:  anything
    else is a malformed answer, and the delivery refuses it rather than guessing."""
    wanted = channel.casefold()
    for stream in streams:
        name = stream.get("name")
        if not isinstance(name, str) or name.casefold() != wanted:
            continue
        invite_only = stream.get("invite_only")
        stream_id = stream.get("stream_id")
        if (
            not isinstance(invite_only, bool)
            or not isinstance(stream_id, int)
            or isinstance(stream_id, bool)
        ):
            raise DeliveryError("malformed streams answer")
        if invite_only:
            return False, 0
        return True, stream_id
    return False, 0


def subscribe(http: Http, channel: str) -> None:
    """Subscribe this bot to a public channel.

    The subscription is idempotent (Zulip answers `already_subscribed` for a bot that is
    already there), so a retry may run it again without a second copy of the channel."""
    answer = http("POST", "users/me/subscriptions", {"subscriptions": [{"name": channel}]})
    if not isinstance(answer, Mapping):
        raise DeliveryError("malformed subscribe answer")


def quiet(http: Http, stream_id: int) -> None:
    """Apply the quiet settings to the subscription, so the bot never notifies a phone.

    The stream id is required:  the properties endpoint needs it, and a bot left un-quiet
    would notify a phone the moment the owner's note arrived."""
    data = [
        {"stream_id": stream_id, "property": prop, "value": value}
        for prop, value in sorted(QUIET.items())
    ]
    answer = http("POST", "users/me/subscriptions/properties", {"subscription_data": data})
    if not isinstance(answer, Mapping):
        raise DeliveryError("malformed subscribe properties answer")


def ensure_subscribed(http: Http, channel: str, stream_id: int) -> None:
    """Subscribe this bot to a public channel, quiet.

    The two writes are the `subscribe` and `quiet` steps;  a caller that retries one step at
    a time calls them directly."""
    subscribe(http, channel)
    quiet(http, stream_id)


def mark_unread(http: Http, message_id: int) -> None:
    """Remove the read flag, so the message is unread for this bot.

    This also covers a bot whose own listener already marked the message read:  the flag is
    taken off again.  Zulip ignores the message for a user that is not subscribed to the
    channel, and says so;  that bot failed."""
    answer = http(
        "POST",
        "messages/flags",
        {"messages": [message_id], "op": "remove", "flag": "read"},
    )
    if not isinstance(answer, Mapping):
        raise DeliveryError("malformed messages/flags answer")
    ignored = answer.get("ignored_because_not_subscribed_channels")
    if ignored:
        raise DeliveryError("Zulip ignored the message: the bot is not subscribed")


def deliver_to_bot(
    http: Http,
    message_id: int,
    channel: str,
    streams: Sequence[Mapping[str, Any]] | None,
    *,
    seat: str,
    log: Log,
    done: set[str] | None = None,
) -> str:
    """One bot:  subscribe (quiet) to the channel, then make the message unread.

    `done` holds the steps this bot already completed;  it is mutated in place, so a retry
    runs only the steps that are still missing and a bot that already got the message runs
    nothing at all.

    Returns the outcome:  `skipped` when the channel is private or unknown to this bot (the
    delivery never joins a private channel, and the skip is logged), `delivered` when the
    message is unread for the bot.  A failed write is raised as a `_Step`, so the caller can
    tell a write that may have been applied from one that certainly was not."""
    completed = done if done is not None else set()
    # `unread` is never a frozen done-step: a listener may re-read the message
    # after one attempt, so every attempt reapplies it (it is idempotent).
    completed.discard("unread")
    if streams is None:
        # Only the read flag is missing: no channel lookup is needed.
        try:
            mark_unread(http, message_id)
        except Exception as exc:  # noqa: BLE001 - the classifier decides
            raise _Step(exc) from None
        completed.discard("unread")
        return DELIVERED
    public, stream_id = channel_state(streams, channel)
    if not public:
        log(
            "inbox-delivery-skipped",
            seat=seat,
            id=message_id,
            reason="private-or-unknown-channel",
        )
        return SKIPPED
    try:
        if "subscribe" not in completed:
            subscribe(http, channel)
            completed.add("subscribe")
        if "quiet" not in completed:
            quiet(http, stream_id)
            completed.add("quiet")
        mark_unread(http, message_id)
        completed.discard("unread")
    except Exception as exc:  # noqa: BLE001 - the classifier decides what the failure means
        raise _Step(exc) from None
    return DELIVERED


def classify(exc: Exception, *, write: bool) -> str:
    """The outcome of a bot whose delivery failed.

    `retryable` means the request was never sent or provably not applied, so a later try
    can still succeed:  a refused connection or a DNS failure before the send, a rate
    limit, or a Zulip error that came before any write.  `terminal` means the request will
    never work as it stands:  an HTTP 4xx other than a rate limit, a refused delivery (a
    message Zulip ignored because the bot is not subscribed), or a malformed answer.
    `unknown` means the request may have been applied and nothing is known:  a timeout, a
    dropped connection, or a 5xx on a write.

    Both writes are idempotent (a subscription and a read flag), so an `unknown` is safe to
    retry:  the worst a retry does is subscribe a bot twice and take a read flag off twice.
    """
    if isinstance(exc, DeliveryError):
        return TERMINAL
    if isinstance(exc, Z.CredentialError):
        return TERMINAL
    if isinstance(exc, Z.ApiError):
        status = exc.status
        if status == 429:
            return RETRYABLE
        if status is not None and 400 <= status < 500:
            return TERMINAL
        return UNKNOWN if write else RETRYABLE
    if isinstance(exc, Z.NetworkError):
        return UNKNOWN if exc.maybe_sent else RETRYABLE
    return UNKNOWN


def _log_failure(
    log: Log,
    seat: str,
    message_id: Any,
    outcome: str,
    exc: Exception,
    creds: Z.Credentials,
) -> None:
    """Log one bot's failure:  seat, message id, error type and outcome, never a value.

    A key the realm refused (401 or 403) is logged as a masked preview, so the reader can
    tell which key was refused without the key ever being written out in full."""
    fields: dict[str, Any] = {
        "seat": seat,
        "id": message_id,
        "error": type(exc).__name__,
        "outcome": outcome,
    }
    if getattr(exc, "status", None) in (401, 403):
        fields["key_preview"] = key_preview(creds.key)
    log("inbox-delivery-failed", **fields)


def deliver(
    message: Mapping[str, Any],
    *,
    targets: Iterable[tuple[str, Z.Credentials]] | Targets,
    http_factory: HttpFactory,
    log: Log,
    skip_emails: Iterable[str] = (),
    realm: str = "",
    only_seats: Iterable[str] | None = None,
    done: dict[str, set[str]] | None = None,
) -> dict[str, Any]:
    """Put one message in every bot's own Zulip inbox, unread.

    Each bot is isolated:  one bot's failure is logged (seat tag, message id, error type and
    outcome only) and the rest of the fleet still gets the message.  The human owner and the
    sender are never targeted:  a bot does not mark its own message unread, and the owner reads
    the channel anyway.

    `realm` is the realm the message came from:  a bot whose own credential site is another
    host is skipped (`other-realm`), so a key is never sent to a server it does not belong to,
    and the transport is addressed at that realm.

    `only_seats` narrows the attempt to the seats that are still pending, and `done` says which
    steps each seat already completed (it is mutated in place).  A retry therefore runs only
    the pending seats and only their missing steps:  a delivered seat never runs again.

    The result carries the outcome of every bot:  `outcomes` maps a seat to `delivered`,
    `skipped`, `retryable`, `terminal`, `unknown` or `missing-credentials`, and `counts` counts
    each of them.  A `retryable`, an `unknown` or a `missing-credentials` bot may still be
    delivered by a later attempt, so the caller retries it;  every write is idempotent, so that
    retry is safe."""
    if isinstance(targets, Targets):
        pairs = list(targets.targets)
        missing_seats = dict(targets.skipped)
    else:
        pairs = list(targets)
        missing_seats = {}
    wanted = None if only_seats is None else {str(seat) for seat in only_seats}
    steps = done if done is not None else {}
    message_id = message.get("id")
    channel = str(message.get("display_recipient") or "")
    sender_email = str(message.get("sender_email") or "").casefold()
    skipped_emails = {str(e).casefold() for e in skip_emails if e}
    result: dict[str, Any] = {
        "delivered": 0,
        "failed": 0,
        "skipped": 0,
        "outcomes": {},
        "counts": {name: 0 for name in OUTCOMES},
    }
    if not isinstance(message_id, int) or not channel:
        return result

    def record(seat: str, outcome: str) -> None:
        result["outcomes"][seat] = outcome
        result["counts"][outcome] = result["counts"].get(outcome, 0) + 1
        if outcome == DELIVERED:
            result["delivered"] += 1
        elif outcome == SKIPPED:
            result["skipped"] += 1
        else:
            result["failed"] += 1

    # A seat without credentials is part of the result:  it is named, counted and never silent.
    for seat in sorted(missing_seats):
        if wanted is not None and seat not in wanted:
            continue
        record(seat, MISSING_CREDENTIALS)
    # The streams list is fetched once per realm, not once per bot:  only public streams
    # matter here, so every bot on the same site shares one view of them.  The cache lives in
    # this call only:  a later delivery fetches a fresh list, and a failed fetch is not
    # cached, so the next bot on that site tries with its own key.
    streams_by_site: dict[str, list[Mapping[str, Any]]] = {}
    for seat, creds in pairs:
        if wanted is not None and seat not in wanted:
            continue
        outcome = ""
        try:
            if realm:
                # The bot is addressed on the realm the message came from, and never on the
                # server its key belongs to.
                try:
                    Z.verify_realm(creds, realm)
                except Z.RealmMismatch:
                    outcome = SKIPPED
                    log("inbox-delivery-skipped", seat=seat, id=message_id, reason="other-realm")
                else:
                    outcome = _deliver_one(
                        seat,
                        creds,
                        message_id,
                        channel,
                        streams_by_site,
                        http_factory,
                        log,
                        realm,
                        steps,
                        sender_email,
                        skipped_emails,
                    )
            else:
                outcome = _deliver_one(
                    seat,
                    creds,
                    message_id,
                    channel,
                    streams_by_site,
                    http_factory,
                    log,
                    realm,
                    steps,
                    sender_email,
                    skipped_emails,
                )
        except _Step as step:
            outcome = classify(step.cause, write=True)
            _log_failure(log, seat, message_id, outcome, step.cause, creds)
        except Exception as exc:  # noqa: BLE001 - one bot must not stop the others
            outcome = classify(exc, write=False)
            _log_failure(log, seat, message_id, outcome, exc, creds)
        record(seat, outcome)
    return result


def _deliver_one(
    seat: str,
    creds: Z.Credentials,
    message_id: int,
    channel: str,
    streams_by_site: dict[str, list[Mapping[str, Any]]],
    http_factory: HttpFactory,
    log: Log,
    realm: str,
    steps: dict[str, set[str]],
    sender_email: str,
    skipped_emails: set[str],
) -> str:
    """One bot's delivery, apart from the classification of its failure.

    The sender and the owner are skipped by their email;  everybody else is subscribed (quiet)
    and has the message's read flag taken off."""
    email = str(creds.email or "").casefold()
    if email and email == sender_email:
        log("inbox-delivery-skipped", seat=seat, id=message_id, reason="sender")
        return SKIPPED
    if email and email in skipped_emails:
        log("inbox-delivery-skipped", seat=seat, id=message_id, reason="owner")
        return SKIPPED
    completed = steps.setdefault(seat, set())
    completed.discard("unread")
    http = http_factory(creds, realm)
    site = str(creds.site or "")
    streams: list[Mapping[str, Any]] | None = None
    if not {"subscribe", "quiet"} <= completed:
        if site not in streams_by_site:
            streams_by_site[site] = fetch_streams(http)
        streams = streams_by_site[site]
    return deliver_to_bot(
        http,
        message_id,
        channel,
        streams,
        seat=seat,
        log=log,
        done=completed,
    )
