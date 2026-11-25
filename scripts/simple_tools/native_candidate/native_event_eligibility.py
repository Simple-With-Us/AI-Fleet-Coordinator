"""Pure qualification-only semantic identity checks. No IO, arming or replay.

Caller supplies a complete, frozen preactivation upstream identity census and a
read-only producer mapping. Detection/acceptance times never prove source age.
"""

import hashlib
import math
import re
from datetime import datetime

SINGLE_SOURCE_KINDS = {
    "review_submitted": "review_floor",
    "review_completed": "check_floor",
}

FINDINGS_KIND = "findings_added"
#: Same bound as the producer's persistence contract (review_alerts.events):
#: a batch larger than this carries no membership at all.
FINDINGS_MEMBER_LIMIT = 100

_BASE_BOUNDARY_KEYS = {"repo", "number", "review_floor", "check_floor", "complete", "epoch"}


def _epoch_of(text):
    # ISO-8601 as an instant, normalising trailing Z like the producer does.
    if not isinstance(text, str) or not text.strip():
        return None
    raw = text.strip()
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def valid_boundary(boundary):
    # comment_floor rides along once the census includes finding comments;
    # older six-field boundaries remain valid for the single-source kinds.
    if not isinstance(boundary, dict) or not (
        set(boundary) == _BASE_BOUNDARY_KEYS
        or set(boundary) == _BASE_BOUNDARY_KEYS | {"comment_floor"}
    ):
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
    floors = ["review_floor", "check_floor"]
    if "comment_floor" in boundary:
        floors.append("comment_floor")
    if any(type(boundary[k]) is not int or boundary[k] < 0 for k in floors):
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
    if kind == FINDINGS_KIND:
        return _eligible_findings(boundary, row, wake_id, message_id)
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


def _eligible_findings(boundary, row, wake_id, message_id):
    # A findings batch is a set of source comments, not one source:  every
    # member must postdate the frozen census individually, the event id names
    # the newest member, the batch carries ONE exact head (never the lossy
    # max aggregate), and source_epoch is the newest member's own time.
    if "comment_floor" not in boundary:
        return False
    members = row.get("members")
    if not isinstance(members, list) or not 1 <= len(members) <= FINDINGS_MEMBER_LIMIT:
        return False
    ids, instants, heads = [], [], set()
    for member in members:
        if not isinstance(member, dict) or set(member) != {"id", "at", "head"}:
            return False
        if type(member["id"]) is not int or member["id"] < 1:
            return False
        if (
            not isinstance(member["at"], str)
            or not isinstance(member["head"], str)
            or not re.fullmatch(r"[0-9a-f]{40}", member["head"])
        ):
            return False
        stamp = _epoch_of(member["at"])
        if stamp is None:
            return False
        ids.append(member["id"])
        instants.append(stamp)
        heads.add(member["head"])
    if any(b <= a for a, b in zip(ids, ids[1:])):
        return False
    event_id = row.get("event_id")
    if event_id != "c%d" % ids[-1]:
        return False
    if len(heads) != 1:
        return False
    if ids[0] <= boundary["comment_floor"] or min(instants) <= boundary["epoch"]:
        return False
    source_epoch = row.get("source_epoch")
    if (
        type(source_epoch) not in (int, float)
        or not math.isfinite(source_epoch)
        or source_epoch != max(instants)
    ):
        return False
    semantic = "%s#%d:%s:%s:e%d" % (row["repo"], row["number"], FINDINGS_KIND, event_id, 0)
    return (
        wake_id == "ra-" + hashlib.sha256(semantic.encode()).hexdigest()[:24]
        and type(row.get("epoch")) is int
        and row["epoch"] == 0
        and row.get("wake_id") == wake_id
        and type(message_id) is int
        and message_id > 0
        and type(row.get("message_id")) is int
        and row["message_id"] == message_id
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
