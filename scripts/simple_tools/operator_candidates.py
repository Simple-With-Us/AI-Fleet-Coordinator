"""The Operator senders' shared delivery ledger.

The Operator's own messages (the status DM in operator_send.py, the handbook change post in
handbook_service.py) never repeat themselves and never replay a write whose outcome is unknown.
Each keeps a small JSON ledger of its sends, and this module is the ledger they share:  a
reservation goes down before the post, a landed post records its message id, a post that may have
landed is kept as `unknown`, and a definite refusal releases its key.  It also holds the Zulip
member-record check and the masked credential preview those senders use.

The module was named for the candidate list, the direct message that told the sender of a tagged
address which sessions a letter could mean.  That message is gone:  the Operator asks nobody which
session a letter means (owner ruling, Nov 10, 2026:  "forget the operator asking clarification since
overcomplicates for minimal benefit").  The ledger it introduced stayed, because the other senders
use it.

Python 3.11+, standard library only.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import live as L

#: Zulip's generic bot type.  A webhook or integration bot cannot take a direct message.
GENERIC_BOT = 1


# --------------------------------------------------------------------------------------------
# Member records
# --------------------------------------------------------------------------------------------


def _member_ok(member: Any) -> bool:
    """Whether one Zulip member record carries every field the senders consume, with the right
    shape.  Checked before the record is used, so a malformed record is a refusal rather than a
    failure somewhere downstream."""
    if not isinstance(member, Mapping):
        return False
    user_id = member.get("user_id")
    if not isinstance(user_id, int) or isinstance(user_id, bool):
        return False
    for flag in ("is_active", "is_bot"):
        if not isinstance(member.get(flag, True), bool):
            return False
    bot_type = member.get("bot_type", GENERIC_BOT)
    if not isinstance(bot_type, int) or isinstance(bot_type, bool):
        return False
    return all(
        isinstance(member.get(name, ""), (str, type(None)))
        for name in ("full_name", "email", "delivery_email")
    )


# --------------------------------------------------------------------------------------------
# The dedupe state
# --------------------------------------------------------------------------------------------


def _sends(path: str) -> dict[str, Any]:
    data = L.read_json(path)
    sends = data.get("sends")
    return dict(sends) if isinstance(sends, dict) else {}


def _prune(sends: Mapping[str, Any], at: float, window: float) -> dict[str, Any]:
    keep: dict[str, Any] = {}
    for key, row in sends.items():
        when = row.get("at") if isinstance(row, Mapping) else None
        if isinstance(when, (int, float)) and not isinstance(when, bool) and at - when < window:
            keep[key] = dict(row)
    return keep


#: Dedupe outcomes, so a retry never resends an acknowledged message and never blocks a
#: recoverable one:
#:
#:   - `pending`:  a reservation is held and the write is in flight.
#:   - `ok`:       Zulip accepted the message; the row carries its id, and a retry inside the
#:                 window returns that cached result instead of sending again.
#:   - `unknown`:  the write may or may not have reached Zulip.  Replay is refused, because the
#:                 message may already exist.
#:   - `refused`:  a definite refusal.  The key is released, so a clean retry is still possible.
DELIVERY_PENDING = "pending"
DELIVERY_OK = "ok"
DELIVERY_UNKNOWN = "unknown"
DELIVERY_REFUSED = "refused"
#: A delivery that provably never reached Zulip and may be tried again.
#:
#: It is its own state rather than a sibling flag on `refused`, because a
#: terminal refusal and a retryable one look identical to anything that reads
#: `delivery_status` alone.  A caller that wants to retry needs to tell them
#: apart without reading a second field, and the ledger logs one value.
DELIVERY_RETRYABLE = "retryable"


def _reserve(
    path: str, key: str, at: float, window: float, recipient_id: int
) -> tuple[bool, float, dict[str, Any]]:
    """(allowed to send, seconds since the last identical send, the stored row).

    The reservation is written before the message, so two sends at once cannot both go out.  An
    existing `ok` row carries the cached result the caller should return instead of losing the
    message id on a retry;  an `unknown` or `refused` row blocks a replay inside the window,
    because the message may already exist or the credential is still bad.
    """
    with L.locked(path):
        sends = _prune(_sends(path), at, window)
        row = sends.get(key)
        if isinstance(row, Mapping):
            when = row.get("at")
            if isinstance(when, (int, float)) and not isinstance(when, bool):
                return False, max(0.0, at - float(when)), dict(row)
        sends[key] = {
            "at": at,
            "recipient": recipient_id,
            "state": DELIVERY_PENDING,
            "message_id": None,
        }
        L.write_json(path, {"sends": sends}, durable=True)
    return True, 0.0, {}


def _record(
    path: str,
    key: str,
    at: float,
    window: float,
    recipient_id: int,
    message_id: int | None,
    state: str,
) -> None:
    with L.locked(path):
        sends = _prune(_sends(path), at, window)
        sends[key] = {
            "at": at,
            "recipient": recipient_id,
            "state": state,
            "message_id": message_id,
        }
        L.write_json(path, {"sends": sends}, durable=True)


def _release(path: str, key: str, at: float, window: float) -> None:
    """Free a key for a send that certainly did not happen.

    Only a definite refusal calls this.  A write whose outcome is unknown keeps its key, so a
    later retry refuses the replay rather than sending a second copy of a message that may
    already exist.
    """
    with L.locked(path):
        sends = _prune(_sends(path), at, window)
        sends.pop(key, None)
        L.write_json(path, {"sends": sends}, durable=True)


def _masked(key: Any) -> str:
    """A credential preview safe to log and to return to the caller:  the first eight and the last
    four characters only.  A rejected Operator key must be identifiable without ever being
    written out in full."""
    text = str(key or "")
    if not text:
        return "missing"
    if len(text) <= 12:
        return "%s…" % text[:1]
    return "%s…%s" % (text[:8], text[-4:])
