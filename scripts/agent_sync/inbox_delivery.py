"""Native Zulip inbox delivery for an owner channel broadcast (AFC-938).

The owner asked for two things about a channel-wide mention:  the message must be UNREAD in
every fleet bot's own Zulip inbox, and `@**everyone**` must wake nobody while `@**all**`
keeps the wake path it already has.  Zulip delivers a channel message to a subscriber's
inbox and marks it read once a listener sees it, so the listener would otherwise empty the
very inbox the owner wants filled.

The Zulip invariant:  a message can only be unread for a user subscribed to its channel.
So every bot, with that bot's own credentials, is subscribed (quiet) to the message's
channel and then has the message's read flag removed.  A private channel is never joined:
it is skipped and logged.

Credentials come from Infisical only (`zulip.infisical_seats`, `zulip.prefetch_infisical`,
`zulip.infisical_credentials`).  There is no environment fallback, no zuliprc fallback and
no config override:  when Infisical is unreachable or empty, the delivery fails loudly with
an `inbox-delivery-failed` event instead of pretending to have delivered.

Every log line carries the seat tag, the message id and an error type:  never an email, a
key, or the message content.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from . import zulip as Z
from .subscribe_all import QUIET

#: The transport:  http(method, path, payload) -> the parsed answer.  Injectable, so the whole
#: delivery runs offline in a test.
Http = Callable[[str, str, Mapping[str, Any] | None], Mapping[str, Any]]

#: The log:  log(event, **fields).
Log = Callable[..., None]

#: The transport factory:  credentials -> that bot's transport.
HttpFactory = Callable[[Z.Credentials], Http]


class DeliveryError(Exception):
    """The delivery could not be completed:  no credentials, or one bot refused the message."""


def zulip_http(creds: Z.Credentials, realm: str | None = None) -> Http:
    """A transport bound to one bot's own credentials and site.

    The bot's own site comes from its own credentials, so no realm is read from the
    environment here:  a bot is never addressed on somebody else's server."""
    client = Z.ZulipClient(creds, realm or creds.site)

    def call(method: str, path: str, payload: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
        if method == "GET":
            return client.get(path, payload)
        return client.post(path, payload)

    return call


def infisical_targets(
    *,
    env: Mapping[str, str],
    home: Path,
    log: Log | None = None,
) -> list[tuple[str, Z.Credentials]]:
    """Every fleet bot's seat tag and credentials, from Infisical only.

    A seat whose keys are missing is skipped and logged:  one bot's missing key never stops
    the others.  When no bot at all can be read, this raises:  the caller logs the failure
    rather than delivering with the wrong identity.  The caller passes the daemon's own env and
    home:  nothing here reads the process environment or guesses a default."""
    seats = sorted(Z.infisical_seats(env, home))
    if not seats:
        raise DeliveryError("Infisical returned no fleet bot credentials")
    Z.prefetch_infisical(seats, env, home)
    targets: list[tuple[str, Z.Credentials]] = []
    for seat in seats:
        try:
            targets.append((seat, Z.infisical_credentials(seat, env=env, home=home)))
        except Z.CredentialError as exc:
            if log is not None:
                log("inbox-delivery-failed", seat=seat, error=type(exc).__name__)
    if not targets:
        raise DeliveryError("Infisical returned no fleet bot credentials")
    return targets


def channel_state(http: Http, channel: str) -> tuple[bool, int | None]:
    """(the channel is public, its stream id) for `channel`.

    A channel that is private, or that the bot cannot see at all, is not public:  the
    delivery never joins it and never creates it, because the add endpoint takes names and
    would make a channel that does not exist."""
    answer = http("GET", "streams", {"include_public": "true", "include_subscribed": "true"})
    wanted = channel.casefold()
    for stream in answer.get("streams") or []:
        if not isinstance(stream, dict):
            continue
        if str(stream.get("name") or "").casefold() != wanted:
            continue
        if stream.get("invite_only"):
            return False, None
        stream_id = stream.get("stream_id")
        return True, stream_id if isinstance(stream_id, int) else None
    return False, None


def ensure_subscribed(http: Http, channel: str, stream_id: int | None) -> None:
    """Subscribe this bot to a public channel, quiet.

    The subscription is idempotent (Zulip answers `already_subscribed` for a bot that is
    already there), and the quiet settings from `subscribe_all` are applied every time, so a
    bot never starts notifying a phone because of this path."""
    http("POST", "users/me/subscriptions", {"subscriptions": [{"name": channel}]})
    if stream_id is None:
        return
    data = [
        {"stream_id": stream_id, "property": prop, "value": value}
        for prop, value in sorted(QUIET.items())
    ]
    http("POST", "users/me/subscriptions/properties", {"subscription_data": data})


def mark_unread(http: Http, message_id: int) -> None:
    """Remove the read flag, so the message is unread for this bot.

    This also covers a bot whose own listener already marked the message read:  the flag is
    taken off again.  Zulip ignores the message for a user that is not subscribed to the
    channel, and says so;  that bot failed."""
    answer = http(
        "POST",
        "messages/flags",
        {"messages": [message_id], "op": "remove", "flag": "read"},
    )
    ignored = answer.get("ignored_because_not_subscribed_channels")
    if ignored:
        raise DeliveryError("Zulip ignored the message: the bot is not subscribed")


def deliver_to_bot(
    http: Http,
    message_id: int,
    channel: str,
    *,
    seat: str,
    log: Log,
) -> bool:
    """One bot:  subscribe (quiet) to the channel, then make the message unread.

    Returns False when the channel is private or unknown to this bot:  the delivery never
    joins a private channel, and the skip is logged."""
    public, stream_id = channel_state(http, channel)
    if not public:
        log(
            "inbox-delivery-skipped",
            seat=seat,
            id=message_id,
            reason="private-or-unknown-channel",
        )
        return False
    ensure_subscribed(http, channel, stream_id)
    mark_unread(http, message_id)
    return True


def deliver(
    message: Mapping[str, Any],
    *,
    targets: Iterable[tuple[str, Z.Credentials]],
    http_factory: HttpFactory,
    log: Log,
    skip_emails: Iterable[str] = (),
) -> dict[str, Any]:
    """Put one message in every bot's own Zulip inbox, unread.

    Each bot is isolated:  one bot's failure is logged (seat tag, message id and error type
    only) and the rest of the fleet still gets the message.  The human owner and the sender
    are never targeted:  a bot does not mark its own message unread, and the owner reads
    the channel anyway."""
    message_id = message.get("id")
    channel = str(message.get("display_recipient") or "")
    sender_email = str(message.get("sender_email") or "").casefold()
    skipped_emails = {str(e).casefold() for e in skip_emails if e}
    result: dict[str, Any] = {"delivered": 0, "failed": 0, "skipped": 0}
    if not isinstance(message_id, int) or not channel:
        return result
    for seat, creds in targets:
        email = str(creds.email or "").casefold()
        if email and email == sender_email:
            result["skipped"] += 1
            log("inbox-delivery-skipped", seat=seat, id=message_id, reason="sender")
            continue
        if email and email in skipped_emails:
            result["skipped"] += 1
            log("inbox-delivery-skipped", seat=seat, id=message_id, reason="owner")
            continue
        try:
            http = http_factory(creds)
            if deliver_to_bot(http, message_id, channel, seat=seat, log=log):
                result["delivered"] += 1
            else:
                result["skipped"] += 1
        except Exception as exc:  # noqa: BLE001 - one bot must not stop the others
            result["failed"] += 1
            log("inbox-delivery-failed", seat=seat, id=message_id, error=type(exc).__name__)
    return result
