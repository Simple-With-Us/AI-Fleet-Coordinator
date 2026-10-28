"""The peer-broadcast audit:  a fleet bot's `@all` must be explained to the owner (Oct 27 2026).

Jay's rule (owner, Oct 27 2026):  `@all` is mostly Jay's.  A bot uses it when Jay asks or
explicitly approves, or on its own judgment in an extremely serious situation (an ongoing
targeted attack, a fleet-wide outage of about twenty minutes).  A bot that uses it without
explicit approval DMs Jay why.  A fleet bot's `@all` therefore wakes like the owner's, so the
listener keeps the book:  ten minutes after such a broadcast, if nobody explained it to Jay,
the held-alert seat DMs Jay one short line naming the seat, the place and the message.

The audit is a read-only check plus one DM.  It never quotes the message, and it never carries
a credential:  the text names the seat, the channel, the topic and the permalink, and nothing
else.  The channel and the topic go through the same escaping the rest of the listener uses, so
a topic that carries a key cannot smuggle it into a DM.

Two ways a broadcast counts as explained:

  * `owner_context`:  Jay himself posted in the same channel and topic in the sixty minutes
    BEFORE the `@all`.  That is the ordinary case (Jay asked for the broadcast), so nothing is
    sent.  The window is deliberately one-sided:  a post after the `@all` is Jay reacting to it,
    not asking for it.
  * `explained`:  the sender bot DMed Jay after the `@all`.  The check reads the SENDER bot's
    own Zulip account, so the answer is the bot's own DM history and nobody else's.

Credentials come from Infisical only, through the same path the inbox delivery uses
(`zulip.infisical_credentials`).  There is no environment fallback and no zuliprc fallback:  when
Infisical cannot be read, the check did not run, and the reminder says so in its second line
instead of pretending the bot explained itself.  A failure is never a silent pass.

The decision logic here is pure and offline:  the caller injects the transport and the clock, so
every outcome (owner context, explained, reminded, could-not-check, rate limited) is unit-tested
without a network.

A restart drops the pending audits:  they live in memory only, and a dropped audit costs one
unexplained broadcast, never a wrong wake.  That is logged at the start of the daemon.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from . import live as L
from . import zulip as Z
from .identity import seat_tag_for

#: The transport:  http(method, path, payload) -> the parsed answer.  Injectable, so the whole
#: audit runs offline in a test.
Http = Callable[[str, str, Mapping[str, Any] | None], Mapping[str, Any]]

#: The log:  log(event, **fields).
Log = Callable[..., None]

#: The outcomes of one audit.
OWNER_CONTEXT = "owner_context"
EXPLAINED = "explained"
#: The audit decided that the owner must be reminded.  The caller sends the DM and turns this
#: into `reminded` (it went out) or `reminder_failed` (the scanner refused it, or the post
#: failed).
REMIND = "remind"
REMINDED = "reminded"
REMINDER_FAILED = "reminder_failed"
RATE_LIMITED = "rate_limited"

#: How long the owner has to be in the conversation before the `@all` is assumed to be asked for.
OWNER_CONTEXT_MINUTES = 60

#: How long the sender has to explain itself before the reminder goes out.
REMINDER_MINUTES = 10

#: At most this many reminders for one seat in one hour:  a seat that broadcasts in a loop is
#: capped rather than allowed to flood the owner's DMs.
REMINDERS_PER_HOUR = 3

#: The window the cap counts in.
RATE_WINDOW_SECONDS = 3600.0

#: The tag every reminder carries, so the router never takes the DM for a wake reply.
NOTE_TAG = "note"

#: A real no-break space and a normal space between sentences (owner, Oct 23 2026:  never two
#: plain spaces, which collapse on a rendered surface, and never the `&nbsp;` entity).
SENTENCE_GAP = "\u00a0 "

#: A permalink is the realm and the message id only:  the topic is untrusted text and never
#: goes into a URL.
_PERMALINK_RE = re.compile(r"^https?://[^\s]+$")


class AuditError(Exception):
    """The check could not run:  a malformed answer, or a transport that failed."""


@dataclass(frozen=True)
class Broadcast:
    """One fleet bot's channel-wide `@all`, as the audit sees it.

    `sender_seat` is the seat tag the bot signs its posts with; `sender_email` is only used to
    find that seat.  Neither the content nor any credential is here."""

    message_id: int
    sender_seat: str
    sender_email: str
    channel: str
    topic: str
    timestamp: float

    def permalink(self, realm: str) -> str:
        """The link to the broadcast:  the realm and the message id, nothing else."""
        return "%s/#narrow/near/%d" % (str(realm or "").rstrip("/"), int(self.message_id))


@dataclass(frozen=True)
class Verdict:
    """What the audit decided.

    `outcome` is one of the outcome names.  `reason` is set only when a check could not run:  it
    is a short phrase with no secret in it, and it becomes the second line of the reminder.
    `text` is the reminder to send, empty when there is nothing to send."""

    outcome: str
    reason: str = ""
    text: str = ""


@dataclass
class RateLimiter:
    """At most `limit` reminders for one seat per window.

    A slot is taken when the audit decides to remind, not when the DM lands:  a failed send is
    still an attempt, and an attempt that repeats every ten minutes would be the flood this
    cap exists to stop."""

    limit: int = REMINDERS_PER_HOUR
    window: float = RATE_WINDOW_SECONDS
    _sent: dict[str, list[float]] = field(default_factory=dict)

    def allow(self, seat: str, now: float) -> bool:
        """True when this seat may be reminded now.  A refused seat keeps its slot free."""
        key = str(seat or "")
        times = [t for t in self._sent.get(key, []) if float(now) - t < self.window]
        if len(times) >= self.limit:
            self._sent[key] = times
            return False
        times.append(float(now))
        self._sent[key] = times
        return True


def topic_narrow(channel: str, topic: str) -> list[dict[str, Any]]:
    """The narrow for one channel and topic."""
    return [
        {"operator": "stream", "operand": str(channel)},
        {"operator": "topic", "operand": str(topic)},
    ]


def dm_narrow(owner_user_id: int) -> list[dict[str, Any]]:
    """The narrow for the bot's own DMs with the owner.

    The sender is "me":  the credentials are the sender bot's own, so the answer can only ever be
    that bot's own DM history."""
    return [
        {"operator": "dm", "operand": [int(owner_user_id)]},
        {"operator": "sender", "operand": "me"},
    ]


def fetch_messages(
    http: Http,
    narrow: list[dict[str, Any]],
    *,
    anchor: str,
    before: int = 20,
    after: int = 0,
) -> list[Mapping[str, Any]]:
    """One page of messages for a narrow.

    The answer is checked at the boundary:  a malformed answer is a failure, never an empty
    result, because "the bot did not explain itself" and "the check did not run" are different
    answers."""
    answer = http(
        "GET",
        "messages",
        {
            "narrow": narrow,
            "anchor": anchor,
            "num_before": int(before),
            "num_after": int(after),
            "apply_markdown": False,
        },
    )
    if not isinstance(answer, Mapping):
        raise AuditError("malformed messages answer")
    messages = answer.get("messages")
    if not isinstance(messages, list):
        raise AuditError("malformed messages answer")
    return [m for m in messages if isinstance(m, Mapping)]


def _place(message: Mapping[str, Any]) -> tuple[str, str] | None:
    """(channel, topic) of a stream message, or None for a DM."""
    if message.get("type") == "private":
        return None
    channel = message.get("display_recipient")
    topic = message.get("subject") if message.get("subject") is not None else message.get("topic")
    if not isinstance(channel, str):
        return None
    return (channel.casefold(), str(topic or "").casefold())


def owner_context(
    messages: Iterable[Mapping[str, Any]],
    *,
    owner_user_id: int,
    channel: str,
    topic: str,
    broadcast_id: int,
    broadcast_ts: float,
    window_minutes: float = OWNER_CONTEXT_MINUTES,
) -> bool:
    """True when the owner was in the conversation just before the broadcast.

    "Just before" is the sixty minutes before the `@all`, in the same channel and topic:  that is
    the shape of "Jay asked for this".  A message after the broadcast is Jay reacting to it, so
    it does not count, and a DM never counts."""
    limit = float(window_minutes) * 60.0
    wanted = (str(channel or "").casefold(), str(topic or "").casefold())
    for message in messages:
        mid = message.get("id")
        if not isinstance(mid, int) or isinstance(mid, bool):
            continue
        if mid >= int(broadcast_id):
            continue  # only what came BEFORE the broadcast counts
        if message.get("sender_id") != int(owner_user_id):
            continue
        if _place(message) != wanted:
            continue
        ts = message.get("timestamp")
        if not isinstance(ts, (int, float)) or isinstance(ts, bool):
            continue
        age = float(broadcast_ts) - float(ts)
        if 0.0 <= age <= limit:
            return True
    return False


def explained(messages: Iterable[Mapping[str, Any]], broadcast_id: int) -> bool:
    """True when the sender wrote the owner a message after the `@all`.

    Any message after it counts:  the check cannot read the content (and must not), so it asks
    only about the order."""
    for message in messages:
        mid = message.get("id")
        if isinstance(mid, int) and not isinstance(mid, bool) and mid > int(broadcast_id):
            return True
    return False


def reminder_text(
    alert_seat: str,
    sender_seat: str,
    channel: str,
    topic: str,
    permalink: str,
    *,
    minutes: int = REMINDER_MINUTES,
    reason: str = "",
) -> str:
    """The one short message the alert seat sends the owner.

    It never carries the message content:  the seat, the place and the link are enough for the
    owner to look.  The channel and the topic go through the same escaping the rest of the
    listener uses (`live.escape_line`), so a topic that holds a key cannot carry it into a DM.

    When the check could not run, the reason follows as a second line, in the same no-break
    space convention as every other sentence the fleet writes."""
    first = (
        "[%s\u00b7%s] %s used @all in #%s > %s (%s) and has not DMed you a reason within %d minutes."
        % (
            alert_seat,
            NOTE_TAG,
            sender_seat,
            L.escape_line(str(channel or ""), 60),
            L.escape_line(str(topic or ""), 80),
            permalink,
            int(minutes),
        )
    )
    if not reason:
        return first
    return "%s%s\n(Could not check its DMs: %s.)" % (first, SENTENCE_GAP, reason)


def evaluate(
    broadcast: Broadcast,
    *,
    owner_user_id: int,
    http: Http,
    permalink: str,
    rate: RateLimiter,
    now: float,
    alert_seat: str = "",
    minutes: int = REMINDER_MINUTES,
    window_minutes: float = OWNER_CONTEXT_MINUTES,
) -> Verdict:
    """Decide what the audit says about one broadcast.

    The order is the owner's:  Jay's own words first (he asked for it), then the bot's own
    explanation (it told him why), and only then the reminder.  A check that could not run is
    not a pass:  it produces the reminder with the reason, so the owner hears about it anyway.

    The transport and the clock are injected, so the whole decision runs offline in a test."""
    reason = ""
    try:
        topic_messages = fetch_messages(
            http,
            topic_narrow(broadcast.channel, broadcast.topic),
            anchor=str(int(broadcast.message_id)),
            before=20,
            after=0,
        )
    except Exception as exc:  # noqa: BLE001 - a failed check is a reason, not a pass
        topic_messages = []
        reason = "its topic history could not be read (%s)" % type(exc).__name__
    if owner_context(
        topic_messages,
        owner_user_id=owner_user_id,
        channel=broadcast.channel,
        topic=broadcast.topic,
        broadcast_id=broadcast.message_id,
        broadcast_ts=broadcast.timestamp,
        window_minutes=window_minutes,
    ):
        return Verdict(OWNER_CONTEXT)
    try:
        dm_messages = fetch_messages(
            http,
            dm_narrow(owner_user_id),
            anchor="newest",
            before=20,
            after=0,
        )
    except Exception as exc:  # noqa: BLE001 - a failed check is a reason, not a pass
        dm_messages = []
        reason = reason or "its DMs could not be read (%s)" % type(exc).__name__
    if explained(dm_messages, broadcast.message_id):
        return Verdict(EXPLAINED)
    if not rate.allow(broadcast.sender_seat, now):
        return Verdict(RATE_LIMITED)
    text = reminder_text(
        alert_seat,
        broadcast.sender_seat,
        broadcast.channel,
        broadcast.topic,
        permalink,
        minutes=minutes,
        reason=reason,
    )
    return Verdict(REMIND, reason=reason, text=text)


def sender_seat_for(email: str, seats: Iterable[str] | None = None) -> str:
    """The seat tag a bot's email maps to, or an empty string when it maps to none.

    With `seats` (the fleet's seat tags) the answer is only a seat the fleet knows:  a mail from
    outside the fleet is not a seat, so its `@all` cannot be checked against a seat's own Zulip
    account, and the caller says so in the reminder instead of guessing."""
    if not email:
        return ""
    tag = seat_tag_for({"is_bot": True, "email": str(email)}) or ""
    if not tag:
        return ""
    if seats is not None and tag not in {str(s).upper() for s in seats}:
        return ""
    return tag


def realm_of(creds: Z.Credentials, fallback: str = "") -> str:
    """The realm a credential belongs to, with the caller's realm as the fallback."""
    site = str(getattr(creds, "site", "") or "").strip()
    return site or str(fallback or "")
