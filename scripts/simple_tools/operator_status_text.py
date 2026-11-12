"""The text of an Operator status message, and the one check that a string is exactly such a text.

Owner ruling, Thu, Oct 29 2026, about 3:35 AM:  the Operator's direct messages to the sender of a
tagged message go one to one, with the owner left out, because they are deterministic and say
nothing about the project.  The text is built here from a fixed vocabulary, and `is_status_text`
is the other half:  it answers True only for a string `build_text` could have produced, character
for character.  `zulip.check_operator_status_send` calls it on the exemption path, so the
owner-in-the-conversation guard (AFC-469) is skipped for these sentences and for no others, even
when a caller holds a client verified as the Operator and passes the exemption flag.

This is a leaf module:  it imports nothing from the package, so `zulip.py` (the client) and
`operator_send.py` (the sender) can both use it without importing each other.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

#: The tag an Operator status message opens with.  The kind is never `wake` or `route`:  the router
#: reads those as seat replies and would count them on the loop guard.
TAG = "[OPERATOR·status]"

KIND_STATUS = "status"
KINDS = (KIND_STATUS,)

#: What a status message can say happened to a wake.  The sentence is fixed;  only the seat tag is
#: filled in.  "Operator" speaks about itself in the third person, because an automated process
#: must not read as a person.
OUTCOMES: Mapping[str, str] = {
    "woken": "{seat} was woken.",
    "alerted": "{seat} was alerted and will see it at its next check.",
    "queued": "It is queued for the next session of {seat}.",
    "held": "It is held back, and a catch-up wake for {seat} will cover it.",
    "dropped": "It was not delivered to {seat}.",
    "paused": "Waking {seat} is paused, so nothing was delivered.",
}

#: Why a wake was held back or dropped.  Fixed sentences again, so a reason cannot carry a detail.
REASONS: Mapping[str, str] = {
    "budget": "The seat is over its wake budget.",
    "quota": "The seat's provider quota is used up.",
    "loop_guard": "The loop guard stopped a repeat wake.",
    "stale": "The message was too old to wake anyone.",
    "paused": "Waking is paused.",
    "no_session": "No live session could take it.",
    "unreachable": "The seat could not be reached.",
}

#: The outcomes that may carry a reason.  A woken seat has nothing to explain, and `paused` says
#: why in its own sentence.
REASON_OUTCOMES = frozenset({"held", "dropped"})

#: A seat tag:  `MM`, `CLAUDE`, `GB-ORACLE`.
SEAT_RE = re.compile(r"^[A-Z][A-Z0-9_-]{0,31}$")

#: Two literal ASCII spaces between sentences:  the Zulip client turns them into the owner's
#: sentence gap on the way out.  The guard in `ZulipClient.request` runs before that conversion, so
#: it sees exactly these two spaces.
GAP = "  "

#: The largest message id a status message names.  A Zulip id is far below this;  the bound keeps
#: `is_status_text` able to read back every text `build_text` writes.
MAX_MESSAGE_ID = 10**18 - 1

_HEAD_RE = re.compile(
    r"\A"
    + re.escape(TAG)
    + r" re=([1-9][0-9]{0,17})\nYour message tagged ([A-Z][A-Z0-9_-]{0,31})\."
)


def seat_tag(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not SEAT_RE.fullmatch(text):
        raise ValueError("not a seat tag")
    return text


def message_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_MESSAGE_ID:
        raise ValueError("not a message id")
    return value


def build_text(
    kind: str,
    *,
    reply_to: int,
    seat: str | None = None,
    outcome: str | None = None,
    reason: str | None = None,
) -> str:
    """The text of one status message, from the fixed vocabulary alone.

    `kind` is `status`.  `reply_to` is the Zulip id of the sender's own message, `seat` the seat tag
    it named, `outcome` what became of the wake, and `reason` (only for a held or dropped wake) why.
    Any value outside the lists raises ValueError."""
    if kind not in KINDS:
        raise ValueError("not an Operator status kind")
    mid = message_id(reply_to)
    who = seat_tag(seat)
    if outcome not in OUTCOMES:
        raise ValueError("not a status outcome")
    sentences = ["Your message tagged %s." % who, OUTCOMES[outcome].format(seat=who)]
    if reason is not None:
        if outcome not in REASON_OUTCOMES:
            raise ValueError("this outcome carries no reason")
        if reason not in REASONS:
            raise ValueError("not a status reason")
        sentences.append(REASONS[reason])
    return "%s re=%d\n%s" % (TAG, mid, GAP.join(sentences))


def is_status_text(content: Any) -> bool:
    """True only for a string `build_text` could have produced, character for character.

    The message id and the seat tag are read from the head, and every outcome and reason is tried:
    a string that differs from what the vocabulary builds for those values, by one character, an
    added sentence or a different space, is not a status text.  No text is parsed leniently, so
    nothing a free-text field could carry gets through."""
    if not isinstance(content, str):
        return False
    head = _HEAD_RE.match(content)
    if head is None:
        return False
    reply_to, seat = int(head.group(1)), head.group(2)
    for outcome in OUTCOMES:
        reasons: list[str | None] = [None]
        if outcome in REASON_OUTCOMES:
            reasons.extend(REASONS)
        for reason in reasons:
            built = build_text(
                KIND_STATUS, reply_to=reply_to, seat=seat, outcome=outcome, reason=reason
            )
            if built == content:
                return True
    return False
