"""Operator's clarifying-question DMs, and what happens when nobody answers (owner decision, Oct 22 2026).

When a wake's addressee cannot be resolved to one seat, Operator opens **one** DM to the message's
sender asking one clarifying question.  That is the only new DM Operator opens to ask something (its
other DM, since Oct 29 2026, is a one-to-one status message that asks nothing:  operator_send.py),
and it is tracked here so the answer can route the wake — or so the wake can be dropped when it
does not come.

Q11 (Jay, Oct 22 2026):  if the sender never answers, **the wake is dropped** once the timeout
passes.  The drop is logged as a `wake_dropped` entry carrying the message id, the sender and the
reason `no_reply`;  nothing is held back for later and nothing is sent to a default seat.  A reply
that arrives after the drop does **not** revive it:  the entry is already `dropped` and `answer()`
reports `ignored`, so a late, well-meant answer changes nothing.

The queue is one JSON file under the state root, written durably (a mode-600 temp file, fsynced, then
atomically replaced) and only ever through one lock, so a check and the mutation it guards cannot be
separated by another writer.  Every entry is validated on load before any caller reads a field of it.
It holds ids, a sender label and timestamps:  never a message body and never a key, and text that
looks like a secret is refused rather than stored.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Mapping
from typing import Any

from . import live as L
from . import secretscan

FILE_NAME = "clarify.json"
PENDING = "pending"
ROUTED = "routed"
DROPPED = "dropped"
REASON_NO_REPLY = "no_reply"

MAX_SENDER = 64
MAX_QUESTION = 200
MAX_SEAT = 32

_COMMON_FIELDS = frozenset(
    {"message_id", "sender_user_id", "sender", "state", "asked_at", "deadline", "question"}
)
_STATE_FIELDS = {
    PENDING: frozenset(),
    ROUTED: frozenset({"answered_at", "answering_seat"}),
    DROPPED: frozenset({"dropped_at", "reason"}),
}
_REQUIRED = ("message_id", "sender_user_id", "sender", "state", "asked_at", "deadline")


class ClarifyRefused(ValueError):
    """Caller text looks like a secret, so it is not stored and not logged.  `field` names which one."""

    def __init__(self, field: str, kind: str, *, message_id: int) -> None:
        super().__init__(
            "clarify: refusing to store the %s of message %d:  it looks like %s"
            % (field, int(message_id), kind)
        )
        self.field = field
        self.kind = kind
        self.message_id = int(message_id)


def state_path(root: str) -> str:
    return os.path.join(L.listener_dir(root), FILE_NAME)


def _number(value: Any, field: str, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("clarify: entry %s has a non-numeric %s" % (key, field))
    return float(value)


def _text(value: Any, field: str, key: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError("clarify: entry %s has a non-string %s" % (key, field))
    if len(value) > limit:
        raise ValueError(
            "clarify: entry %s has an over-long %s (%d > %d)" % (key, field, len(value), limit)
        )
    return value


def _validate_entry(key: str, entry: Any) -> dict[str, Any]:
    """One entry, checked before any caller reads a field of it.

    Raises ValueError naming the entry and the field, so a hand-edited or half-written file is refused
    here instead of raising KeyError deeper in, or expiring a wake because its deadline went missing."""
    if not isinstance(entry, Mapping):
        raise ValueError("clarify: entry %s is not an object" % key)
    allowed = _COMMON_FIELDS | _STATE_FIELDS.get(str(entry.get("state")), frozenset())
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise ValueError("clarify: entry %s has unknown field %s" % (key, unknown[0]))
    missing = [name for name in _REQUIRED if name not in entry]
    if missing:
        raise ValueError("clarify: entry %s is missing %s" % (key, missing[0]))
    for field in ("message_id", "sender_user_id"):
        value = entry[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("clarify: entry %s has a non-integer %s" % (key, field))
    if entry["message_id"] <= 0:
        raise ValueError("clarify: entry %s has a non-positive message_id" % key)
    if str(entry["message_id"]) != key:
        raise ValueError("clarify: entry %s is keyed for another message" % key)
    _text(entry["sender"], "sender", key, MAX_SENDER)
    if "question" in entry:
        _text(entry["question"], "question", key, MAX_QUESTION)
    state = entry["state"]
    if state not in _STATE_FIELDS:
        raise ValueError("clarify: entry %s has an unknown state %r" % (key, state))
    asked = _number(entry["asked_at"], "asked_at", key)
    deadline = _number(entry["deadline"], "deadline", key)
    if asked < 0 or deadline < asked:
        raise ValueError("clarify: entry %s has a deadline before it was asked" % key)
    if state == ROUTED:
        _number(entry["answered_at"], "answered_at", key)
        _text(entry["answering_seat"], "answering_seat", key, MAX_SEAT)
    if state == DROPPED:
        _number(entry["dropped_at"], "dropped_at", key)
        if entry["reason"] != REASON_NO_REPLY:
            raise ValueError(
                "clarify: entry %s has an unknown drop reason %r" % (key, entry["reason"])
            )
    return dict(entry)


def load(root: str) -> dict[str, Any]:
    """The queue, with every entry validated.

    An **absent** file is an empty queue:  that is the state before the first question is ever
    asked.  A file that exists but cannot be read as a queue raises ValueError instead, because
    discarding it would silently forget every pending clarifying question — and a forgotten wake is
    one that is never routed and never dropped.  A malformed entry raises ValueError naming the
    entry and the field rather than being read as a pending wake."""
    path = state_path(root)
    if not os.path.exists(path):
        return {"entries": {}}
    data = L.read_json(path)
    if not isinstance(data, dict) or not data:
        # `read_json` answers {} for unreadable, corrupt and non-object content alike, so this is
        # exactly the case where the file is present but its contents cannot be trusted.
        raise ValueError("clarify: the queue file is unreadable or malformed, not an empty queue")
    unknown = sorted(set(data) - {"entries"})
    if unknown:
        raise ValueError("clarify: the queue file has an unknown section %s" % unknown[0])
    entries = data.get("entries")
    if not isinstance(entries, Mapping):
        raise ValueError("clarify: the queue file has no entries object")
    return {
        "entries": {str(key): _validate_entry(str(key), value) for key, value in entries.items()}
    }


def save(root: str, data: Mapping[str, Any]) -> None:
    """Write the queue durably:  a unique mode-600 temp file in the same directory, fsynced, then
    `os.replace`, then the directory fsynced.  A crash leaves either the old file or the new one."""
    L.write_json(state_path(root), dict(data), durable=True)


def ask(
    root: str,
    *,
    message_id: int,
    sender_user_id: int,
    sender: str,
    now: float,
    timeout_seconds: float,
    question: str = "",
) -> dict[str, Any]:
    """Record the one clarifying question Operator asked about `message_id`.

    One entry per message:  asking again about the same message replaces the entry rather than
    opening a second DM, which is what "one clarifying question per message" means.

    Both fields are scanned before anything is stored:  a sender or question carrying a credential
    raises ClarifyRefused, and neither value reaches the file or the log."""
    label = sender[:MAX_SENDER] if isinstance(sender, str) else ""
    text = question[:MAX_QUESTION] if isinstance(question, str) else ""
    for field, value in (("sender", label), ("question", text)):
        kind = secretscan.scan(value) if value else None
        if kind:
            raise ClarifyRefused(field, kind, message_id=message_id)
    entry = {
        "message_id": int(message_id),
        "sender_user_id": int(sender_user_id),
        "sender": label,
        "state": PENDING,
        "asked_at": round(float(now), 3),
        "deadline": round(float(now) + float(timeout_seconds), 3),
        "question": text,
    }
    _validate_entry(str(int(message_id)), entry)
    key = str(int(message_id))
    with L.locked(state_path(root)):
        data = load(root)
        data["entries"][key] = entry
        save(root, data)
    return dict(entry)


def due(root: str, now: float) -> list[dict[str, Any]]:
    """Pending entries whose deadline has passed."""
    return [
        dict(entry)
        for entry in load(root)["entries"].values()
        if entry.get("state") == PENDING and float(entry.get("deadline") or 0) <= float(now)
    ]


def expire(root: str, now: float) -> list[dict[str, Any]]:
    """Drop every unanswered clarifying question and return what was dropped.

    The caller logs one `wake_dropped` entry per returned row (message id, sender, reason
    `no_reply`);  this module never logs and never touches the held-back queue, because dropping a
    wake means nothing is held back.  The check, the mutation and the write run under one lock, so a
    reply that lands in the same window is either routed or dropped, never overwritten."""
    dropped: list[dict[str, Any]] = []
    with L.locked(state_path(root)):
        data = load(root)
        for entry in data["entries"].values():
            if entry.get("state") == PENDING and float(entry.get("deadline") or 0) <= float(now):
                entry["state"] = DROPPED
                entry["dropped_at"] = round(float(now), 3)
                entry["reason"] = REASON_NO_REPLY
                dropped.append(dict(entry))
        if dropped:
            save(root, data)
    return dropped


def answer(root: str, *, message_id: int, seat: str, now: float) -> str:
    """Record a reply to a clarifying question.

    Returns `routed` when the reply answers a still-pending question (Operator now routes the wake
    to `seat`), and `ignored` when the entry is unknown or already dropped — a late reply never
    revives a dropped wake.  The state check and the write share one lock, so an expiry racing this
    reply cannot overwrite the answer.

    The answering seat is scanned like every other caller-supplied text, for the same reason
    `ask` scans the sender:  a credential-shaped seat label must not become secret material in
    clarify.json.  The refusal names the field and the message id, never the value."""
    if not isinstance(seat, str):
        raise ValueError("clarify: the answering seat must be a string")
    label = seat[:MAX_SEAT]
    kind = secretscan.scan(label) if label else None
    if kind:
        raise ClarifyRefused("answering_seat", kind, message_id=message_id)
    key = str(int(message_id))
    with L.locked(state_path(root)):
        data = load(root)
        entry = data["entries"].get(key)
        if not isinstance(entry, dict) or entry.get("state") != PENDING:
            return "ignored"
        entry["state"] = ROUTED
        entry["answered_at"] = round(float(now), 3)
        entry["answering_seat"] = label
        save(root, data)
        return "routed"


def pending_entries(root: str) -> list[dict[str, Any]]:
    return [
        dict(entry) for entry in load(root)["entries"].values() if entry.get("state") == PENDING
    ]


def log_dropped(
    log: Callable[..., Any],
    entry: Mapping[str, Any],
) -> None:
    """One `wake_dropped` row:  the message id, the sender, and reason `no_reply`.  No body:  the
    question itself is never logged, and `ask` refused to store one that looked like a secret."""
    log(
        "wake_dropped",
        message_id=entry.get("message_id"),
        sender=entry.get("sender"),
        sender_user_id=entry.get("sender_user_id"),
        reason=REASON_NO_REPLY,
    )
