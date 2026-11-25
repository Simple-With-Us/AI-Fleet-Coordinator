"""Pure qualification-only semantic identity checks. No IO, arming or replay.

Caller supplies a complete, frozen preactivation upstream identity census and a
read-only producer mapping. Detection/acceptance times never prove source age.
"""

import re
import math
import hashlib


SINGLE_SOURCE_KINDS = {
    "review_submitted": "review_floor",
    "review_completed": "check_floor",
}


def valid_boundary(boundary):
    if not isinstance(boundary, dict) or set(boundary) != {
        "repo",
        "number",
        "review_floor",
        "check_floor",
        "complete",
        "epoch",
    }:
        return False
    if (
        type(boundary["epoch"]) not in (int, float)
        or not math.isfinite(boundary["epoch"])
        or boundary["epoch"] <= 0
    ):
        return False
    if boundary["complete"] is not True or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", str(boundary["repo"])
    ):
        return False
    if type(boundary["number"]) is not int or boundary["number"] < 1:
        return False
    if any(
        type(boundary[k]) is not int or boundary[k] < 0 for k in ("review_floor", "check_floor")
    ):
        return False
    return True


def eligible_event(boundary, row, wake_id, message_id):
    """Fail closed. Batch findings require source membership absent from current ledger."""
    if not valid_boundary(boundary):
        return False
    if (
        not isinstance(row, dict)
        or row.get("repo") != boundary["repo"]
        or row.get("number") != boundary["number"]
    ):
        return False
    if type(row.get("number")) is not int:
        return False
    kind = row.get("kind")
    if kind not in SINGLE_SOURCE_KINDS:
        return False
    event_id = row.get("event_id")
    if not isinstance(event_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", event_id):
        return False
    semantic = "%s#%d:%s:%s:e%d" % (row["repo"], row["number"], kind, event_id, 0)
    if wake_id != "ra-" + hashlib.sha256(semantic.encode()).hexdigest()[:24]:
        return False
    return (
        type(row.get("source_epoch")) in (int, float)
        and math.isfinite(row["source_epoch"])
        and row["source_epoch"] > boundary["epoch"]
        and type(row.get("epoch")) is int
        and row["epoch"] == 0
        and row.get("wake_id") == wake_id
        and type(message_id) is int
        and message_id > 0
        and type(row.get("message_id")) is int
        and row["message_id"] == message_id
        and int(event_id) > boundary[SINGLE_SOURCE_KINDS[kind]]
    )


def persisted_event(state, generation, boundary, head):
    from pathlib import Path
    from .native_custody import read_private

    try:
        record = read_private(Path(state) / "pending" / (head.wake_id + ".json"))
        wake = record["wake"]
        if (
            wake.get("credential_scope") != "review-alerts"
            or wake.get("message_id") != head.message_id
        ):
            return False
        row = wake.get("source_event")
        if not isinstance(row, dict):
            return False
        return eligible_event(
            boundary,
            {
                **row,
                "wake_id": wake.get("wake_id"),
                "message_id": wake.get("message_id"),
            },
            head.wake_id,
            head.message_id,
        )
    except (OSError, ValueError, TypeError, KeyError):
        return False
