"""Operator's clarifying-question DMs, and what happens when nobody answers (owner decision, Oct 22 2026).

When a wake's addressee cannot be resolved to one seat, Operator opens **one** DM to the message's
sender asking one clarifying question.  That is the only new DM Operator ever opens, and it is
tracked here so the answer can route the wake — or so the wake can be dropped when it does not come.

Q11 (Jay, Oct 22 2026):  if the sender never answers, **the wake is dropped** once the timeout
passes.  The drop is logged as a `wake_dropped` entry carrying the message id, the sender and the
reason `no_reply`;  nothing is held back for later and nothing is sent to a default seat.  A reply
that arrives after the drop does **not** revive it:  the entry is already `dropped` and `answer()`
reports `ignored`, so a late, well-meant answer changes nothing.

The queue is one JSON file under the state root, written atomically.  It holds ids, a sender label
and timestamps:  never a message body and never a key.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from typing import Any

from . import live as L

FILE_NAME = "clarify.json"
PENDING = "pending"
ROUTED = "routed"
DROPPED = "dropped"
REASON_NO_REPLY = "no_reply"


def state_path(root: str) -> str:
    return os.path.join(L.listener_dir(root), FILE_NAME)


def load(root: str) -> dict[str, Any]:
    """The queue, or an empty one.  A file that cannot be read is empty, never an error."""
    path = state_path(root)
    try:
        with open(path, encoding="utf-8") as fh:
            import json

            data = json.load(fh)
    except (OSError, ValueError):
        return {"entries": {}}
    entries = data.get("entries") if isinstance(data, dict) else None
    return {"entries": entries} if isinstance(entries, dict) else {"entries": {}}


def save(root: str, data: Mapping[str, Any]) -> None:
    import json

    path = state_path(root)
    L.private_dir(os.path.dirname(path))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, sort_keys=True, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


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
    opening a second DM, which is what "one clarifying question per message" means."""
    data = load(root)
    entries = data["entries"]
    key = str(int(message_id))
    entry = {
        "message_id": int(message_id),
        "sender_user_id": int(sender_user_id),
        "sender": sender[:64],
        "state": PENDING,
        "asked_at": round(float(now), 3),
        "deadline": round(float(now) + float(timeout_seconds), 3),
        "question": question[:200],
    }
    entries[key] = entry
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
    wake means nothing is held back."""
    data = load(root)
    dropped = []
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
    revives a dropped wake."""
    data = load(root)
    entry = data["entries"].get(str(int(message_id)))
    if not isinstance(entry, dict) or entry.get("state") != PENDING:
        return "ignored"
    entry["state"] = ROUTED
    entry["answered_at"] = round(float(now), 3)
    entry["answering_seat"] = seat[:32]
    save(root, data)
    return "routed"


def pending_entries(root: str) -> list[dict[str, Any]]:
    return [
        dict(entry)
        for entry in load(root)["entries"].values()
        if entry.get("state") == PENDING
    ]


def log_dropped(
    log: Callable[..., Any],
    entry: Mapping[str, Any],
) -> None:
    """One `wake_dropped` row:  the message id, the sender, and reason `no_reply`.  No body."""
    log(
        "wake_dropped",
        message_id=entry.get("message_id"),
        sender=entry.get("sender"),
        sender_user_id=entry.get("sender_user_id"),
        reason=REASON_NO_REPLY,
        question=entry.get("question", ""),
    )