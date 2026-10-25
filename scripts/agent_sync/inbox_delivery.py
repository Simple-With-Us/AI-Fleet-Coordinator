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

Credentials come from Infisical only (`zulip.infisical_seats`, `zulip.prefetch_infisical`,
`zulip.infisical_credentials`).  There is no environment fallback, no zuliprc fallback and
no config override:  when Infisical is unreachable or empty, the delivery fails loudly with
an `inbox-delivery-failed` event instead of pretending to have delivered.

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

#: The transport factory:  credentials -> that bot's transport.
HttpFactory = Callable[[Z.Credentials], Http]

#: The outcome of one bot's delivery.
DELIVERED = "delivered"
SKIPPED = "skipped"
RETRYABLE = "retryable"
TERMINAL = "terminal"
UNKNOWN = "unknown"

#: Every outcome, so the result always carries a count for each of them.
OUTCOMES = (DELIVERED, SKIPPED, RETRYABLE, TERMINAL, UNKNOWN)

#: A variable or secret name:  upper case, with at least one underscore.  A value never
#: matches, so a message that quotes one cannot put it in the log.
_NAME_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")


class DeliveryError(Exception):
    """The delivery could not be completed:  no credentials, or one bot refused the message."""


@dataclass(frozen=True)
class Targets:
    """The fleet bots the delivery may address, and the ones it may not.

    `targets` holds (seat tag, credentials) pairs.  `skipped` holds the seat tags whose
    credentials are missing, mapped to the name of the variable or secret that is missing:
    a name, never a value."""

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

    A seat whose keys are missing is skipped and logged:  one bot's missing key never stops
    the others.  When no bot at all can be read, this raises:  the caller logs the failure
    rather than delivering with the wrong identity.  The caller passes the daemon's own env and
    home:  nothing here reads the process environment or guesses a default."""
    seats = sorted(Z.infisical_seats(env, home))
    if not seats:
        raise DeliveryError("Infisical returned no fleet bot credentials")
    Z.prefetch_infisical(seats, env, home)
    targets: list[tuple[str, Z.Credentials]] = []
    skipped: dict[str, str] = {}
    for seat in seats:
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
    if not targets:
        raise DeliveryError("Infisical returned no fleet bot credentials")
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


def ensure_subscribed(http: Http, channel: str, stream_id: int) -> None:
    """Subscribe this bot to a public channel, quiet.

    The subscription is idempotent (Zulip answers `already_subscribed` for a bot that is
    already there), and the quiet settings from `subscribe_all` are applied every time, so a
    bot never starts notifying a phone because of this path.

    The stream id is required:  the properties endpoint needs it, and a bot left un-quiet
    would notify a phone the moment the owner's note arrived."""
    answer = http("POST", "users/me/subscriptions", {"subscriptions": [{"name": channel}]})
    if not isinstance(answer, Mapping):
        raise DeliveryError("malformed subscribe answer")
    data = [
        {"stream_id": stream_id, "property": prop, "value": value}
        for prop, value in sorted(QUIET.items())
    ]
    answer = http("POST", "users/me/subscriptions/properties", {"subscription_data": data})
    if not isinstance(answer, Mapping):
        raise DeliveryError("malformed subscribe properties answer")


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
    streams: Sequence[Mapping[str, Any]],
    *,
    seat: str,
    log: Log,
) -> str:
    """One bot:  subscribe (quiet) to the channel, then make the message unread.

    Returns the outcome:  `skipped` when the channel is private or unknown to this bot (the
    delivery never joins a private channel, and the skip is logged), `delivered` when the
    message is unread for the bot.  A failed write is raised as a `_Step`, so the caller can
    tell a write that may have been applied from one that certainly was not."""
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
        ensure_subscribed(http, channel, stream_id)
        mark_unread(http, message_id)
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
) -> dict[str, Any]:
    """Put one message in every bot's own Zulip inbox, unread.

    Each bot is isolated:  one bot's failure is logged (seat tag, message id, error type and
    outcome only) and the rest of the fleet still gets the message.  The human owner and the
    sender are never targeted:  a bot does not mark its own message unread, and the owner reads
    the channel anyway.

    The result carries the outcome of every bot:  `outcomes` maps a seat to `delivered`,
    `skipped`, `retryable`, `terminal` or `unknown`, and `counts` counts each of them.  A
    `retryable` or an `unknown` bot may still be delivered by a later attempt, so the caller
    retries;  both writes are idempotent, so that retry is safe."""
    if isinstance(targets, Targets):
        pairs = list(targets.targets)
    else:
        pairs = list(targets)
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
    # The streams list is fetched once per realm, not once per bot:  only public streams
    # matter here, so every bot on the same site shares one view of them.  The cache lives in
    # this call only:  a later delivery fetches a fresh list, and a failed fetch is not
    # cached, so the next bot on that site tries with its own key.
    streams_by_site: dict[str, list[Mapping[str, Any]]] = {}
    for seat, creds in pairs:
        outcome = ""
        try:
            email = str(creds.email or "").casefold()
            if email and email == sender_email:
                outcome = SKIPPED
                log("inbox-delivery-skipped", seat=seat, id=message_id, reason="sender")
            elif email and email in skipped_emails:
                outcome = SKIPPED
                log("inbox-delivery-skipped", seat=seat, id=message_id, reason="owner")
            else:
                http = http_factory(creds)
                site = str(creds.site or "")
                if site not in streams_by_site:
                    streams_by_site[site] = fetch_streams(http)
                outcome = deliver_to_bot(
                    http, message_id, channel, streams_by_site[site], seat=seat, log=log
                )
        except _Step as step:
            outcome = classify(step.cause, write=True)
            _log_failure(log, seat, message_id, outcome, step.cause, creds)
        except Exception as exc:  # noqa: BLE001 - one bot must not stop the others
            outcome = classify(exc, write=False)
            _log_failure(log, seat, message_id, outcome, exc, creds)
        result["outcomes"][seat] = outcome
        result["counts"][outcome] = result["counts"].get(outcome, 0) + 1
        if outcome == DELIVERED:
            result["delivered"] += 1
        elif outcome == SKIPPED:
            result["skipped"] += 1
        else:
            result["failed"] += 1
    return result
