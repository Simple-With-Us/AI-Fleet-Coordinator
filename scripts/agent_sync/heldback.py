"""Wakes held back for the seat's budget (owner, Sat, Oct 10:  "surface all").

A wake the budget refuses (a limit in wakes.budget_block, or a full FIFO) is held back instead of
dropped:  the daemon keeps one record per held-back wake in `<SEAT>/held-back.json`, next to the
ledger, and writes it before the ledger's `held_back` row, so a crash between the two never loses
one.  Every other refusal (the pause, the pin, the loop guard, staleness, a seat with no wake
adapter) is still a plain `dropped`.

What happens to a record:
  - surfaced:  an `http` wake the routine took (adapters.routine_surfaced) listed it in its
    `held_back`, or it was that wake's own trigger (a catch-up wake).  It leaves the file.
  - expired:  `ttl_hours` passed without that.  It leaves the file, counted, and wakes nothing.
  - evicted:  more than `max` records were held;  the oldest leaves the file and the `evicted`
    counter keeps it in the next wake's count until a delivered wake reports it.
Each of these is also a ledger row for the held-back wake's id (daemon.py writes them).

Records hold Zulip metadata only, never a message body:  the trigger's id is the reference to its
seat-inbox row, from which a catch-up wake rebuilds the excerpt.  Display copies are escaped and
cut like the wake body's (adapters.routine_body), so every string fits the hosted Worker's
`held_back` rules (at most 200 characters).

Every change is one locked read-modify-write of the file (live.update_json):  holds happen on the
router thread and on the seat's wake worker, and surfacing on the worker.  Everything takes the
time as an argument, so tests drive it with a fake clock.

Python 3.11+, standard library only.
"""
from __future__ import annotations

import os
from typing import Any, Iterable, Mapping

from . import live as L

# The refusals that hold a wake back:  every budget wakes.budget_block names, and a full FIFO.
HOLD_REASONS = frozenset({"wakes_per_hour", "wakes_per_day", "per_topic_per_hour", "owner_per_day",
                          "owner_per_topic_per_hour", "usd_per_day", "overflow"})
FILE_NAME = "held-back.json"
NAME_LIMIT = 100  # channel, topic and sender, as in the wake body
LINK_LIMIT = 200  # a longer link is sent as "" (never cut):  the message id is enough to find it
# A catch-up wake that surfaced nothing waits before the next one:  15 minutes, doubling up to 4 hours,
# and back to none once anything surfaces.  Bounded retries, on top of the budgets every wake obeys.
CATCH_UP_FIRST_WAIT = 15 * 60.0
CATCH_UP_MAX_WAIT = 4 * 3600.0


def path_for(paths: L.SeatPaths) -> str:
    return os.path.join(paths.seat_dir, FILE_NAME)


def _records(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = data.get("records")
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("message_id"), int)] if isinstance(rows, list) else []


def _int(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _num(data: Mapping[str, Any], key: str) -> float:
    value = data.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _totals(data: dict[str, Any]) -> dict[str, int]:
    totals = data.get("totals")
    if not isinstance(totals, dict):
        totals = {}
    clean = {k: _int(totals, k) for k in ("held", "surfaced", "expired", "evicted")}
    data["totals"] = clean
    return clean


def load(paths: L.SeatPaths) -> dict[str, Any]:
    """The seat's state, read without the lock (a status or a choice that a locked call re-checks)."""
    data = L.read_json(path_for(paths))
    data["records"] = _records(data)
    return data


def display_link(link: str) -> str:
    """A link for an item:  "" when longer than LINK_LIMIT, never a cut (and broken) URL."""
    return link if len(link) <= LINK_LIMIT else ""


def wake_ids(record: Mapping[str, Any]) -> list[str]:
    """Every held-back wake a record stands for (one, or more when a message was held back twice)."""
    return [w for w in record.get("wake_ids") or [] if isinstance(w, str)]


def hold(paths: L.SeatPaths, record: Mapping[str, Any], *, cap: int, now: float) -> tuple[bool, list[dict[str, Any]]]:
    """Add one record, oldest first.  (added, evicted records).  A message already held back is not
    held twice (a catch-up wake the budget refused again, or a requeued trigger):  the existing record
    only learns the new wake id, so that wake still gets its surfaced, expired or evicted row.  Past
    `cap` records the oldest are evicted, and the `evicted` counter keeps them in the next count."""
    out: dict[str, Any] = {"added": False, "evicted": []}

    def mutate(data: dict[str, Any]) -> None:
        records = _records(data)
        totals = _totals(data)
        same = next((r for r in records if r["message_id"] == record["message_id"]), None)
        if same is not None:
            known = wake_ids(same)
            same["wake_ids"] = (known + [w for w in wake_ids(record) if w not in known])[-10:]
            data["records"] = records
            return
        seq = _int(data, "next_seq") or 1
        records.append(dict(record, seq=seq))
        data["next_seq"] = seq + 1
        totals["held"] += 1
        evicted = records[: max(0, len(records) - max(1, int(cap)))]
        if evicted:
            records = records[len(evicted):]
            data["evicted"] = _int(data, "evicted") + len(evicted)
            data["evicted_at"] = now
            totals["evicted"] += len(evicted)
        data["records"] = records
        out["added"], out["evicted"] = True, evicted

    L.update_json(path_for(paths), mutate)
    return out["added"], out["evicted"]


def expire(paths: L.SeatPaths, *, ttl: float, now: float) -> list[dict[str, Any]]:
    """Remove records held back `ttl` seconds ago or more, and return them.  The `evicted` counter
    expires the same way, counted from the last eviction, so it cannot linger after its records would have."""
    out: list[dict[str, Any]] = []
    target = path_for(paths)
    if not os.path.exists(target):
        return out

    def mutate(data: dict[str, Any]) -> None:
        records = _records(data)
        totals = _totals(data)
        keep = [r for r in records if now - _num(r, "at") < ttl]
        gone = [r for r in records if now - _num(r, "at") >= ttl]
        if gone:
            totals["expired"] += len(gone)
        if _int(data, "evicted") and now - _num(data, "evicted_at") >= ttl:
            data["evicted"] = 0
        data["records"] = keep
        out.extend(gone)

    L.update_json(target, mutate)
    return out


def wake_summary(paths: L.SeatPaths, *, exclude: Iterable[int], limit: int) -> tuple[dict[str, Any] | None, list[int], int]:
    """What one wake carries:  ({count, items} or None, the listed message ids, the evicted count it
    reports).  `exclude` is the wake's own trigger ids:  those records are the trigger, not held back.
    count is every other record plus the evicted counter;  items are the newest `limit` records,
    newest first, as metadata only (adapters.routine_body sends them as `held_back`)."""
    skip = set(exclude)
    data = load(paths)
    records = [r for r in data["records"] if r["message_id"] not in skip]
    evicted = _int(data, "evicted")
    count = len(records) + evicted
    if count <= 0:
        return None, [], 0
    listed = list(reversed(records))[: max(0, int(limit))]
    items = [item(r) for r in listed]
    return {"count": count, "items": items}, [r["message_id"] for r in listed], evicted


def item(record: Mapping[str, Any]) -> dict[str, Any]:
    """One `held_back` item:  exactly the fields the hosted Worker checks (src/wake.js heldBackProblem)."""
    dm = record.get("dm") is True
    stream_id = record.get("stream_id")
    return {
        "message_id": int(record["message_id"]),
        "dm": dm,
        "channel": None if dm else str(record.get("channel") or "")[:NAME_LIMIT],
        "topic": None if dm else str(record.get("topic") or "")[:NAME_LIMIT],
        "stream_id": (stream_id if not dm and isinstance(stream_id, int) and not isinstance(stream_id, bool)
                      and stream_id > 0 else None),
        "sender_full_name": str(record.get("sender_full_name") or "")[:NAME_LIMIT],
        "zulip_link": display_link(str(record.get("zulip_link") or "")),
        "reason": str(record.get("reason") or ""),
        "at": max(0, int(_num(record, "at"))),
    }


def surface(paths: L.SeatPaths, *, message_ids: Iterable[int], evicted: int = 0) -> list[dict[str, Any]]:
    """Remove the records a delivered wake surfaced and lower the `evicted` counter by what it
    reported.  Returns the removed records.  Anything surfacing ends a catch-up backoff."""
    wanted = set(message_ids)
    out: list[dict[str, Any]] = []
    target = path_for(paths)
    if not os.path.exists(target):
        return out

    def mutate(data: dict[str, Any]) -> None:
        records = _records(data)
        totals = _totals(data)
        out.extend(r for r in records if r["message_id"] in wanted)
        data["records"] = [r for r in records if r["message_id"] not in wanted]
        data["evicted"] = max(0, _int(data, "evicted") - max(0, int(evicted)))
        totals["surfaced"] += len(out)
        if out or evicted:
            data["catch_up_fails"] = 0

    L.update_json(target, mutate)
    return out


def mark_no_primary(paths: L.SeatPaths, message_id: int, why: str) -> None:
    """A catch-up built on this record cannot work (the routine refused its channel, or its trigger
    is gone):  never choose it as a catch-up trigger again.  It stays held back and listed."""
    def mutate(data: dict[str, Any]) -> None:
        records = _records(data)
        for record in records:
            if record["message_id"] == message_id:
                record["no_primary"] = why
        data["records"] = records

    L.update_json(path_for(paths), mutate)


def catch_up_due(data: Mapping[str, Any], now: float) -> bool:
    """May a catch-up wake start now?  At once the first time, then 15 minutes after a catch-up that
    surfaced nothing, doubling to at most 4 hours (surface() resets it)."""
    fails = _int(data, "catch_up_fails")
    if not fails:
        return True
    wait = min(CATCH_UP_FIRST_WAIT * (2 ** (fails - 1)), CATCH_UP_MAX_WAIT)
    return now - _num(data, "catch_up_at") >= wait


def note_catch_up(paths: L.SeatPaths, now: float) -> None:
    """A catch-up wake was queued:  it counts as a miss until something surfaces."""
    def mutate(data: dict[str, Any]) -> None:
        data["records"] = _records(data)
        data["catch_up_at"] = now
        data["catch_up_fails"] = _int(data, "catch_up_fails") + 1

    L.update_json(path_for(paths), mutate)


def digest_due(data: Mapping[str, Any], *, every: float, now: float) -> list[dict[str, Any]]:
    """The records held back since the last digest, when one may go now (at most one per `every`
    seconds), else []."""
    if every <= 0:
        return []
    fresh = [r for r in data.get("records") or [] if _int(r, "seq") > _int(data, "digest_seq")]
    if not fresh or ("digest_at" in data and now - _num(data, "digest_at") < every):
        return []
    return fresh


def note_digest(paths: L.SeatPaths, *, upto: int, now: float) -> None:
    def mutate(data: dict[str, Any]) -> None:
        data["records"] = _records(data)
        data["digest_at"] = now
        data["digest_seq"] = max(_int(data, "digest_seq"), int(upto))

    L.update_json(path_for(paths), mutate)


def summary(paths: L.SeatPaths, now: float) -> dict[str, Any]:
    """For status and `agent-sync wakes`:  how many are held back now, the oldest one's age in
    seconds, the count per reason, the evicted counter and the lifetime totals."""
    data = load(paths)
    records = data["records"]
    reasons: dict[str, int] = {}
    for record in records:
        reason = str(record.get("reason") or "?")
        reasons[reason] = reasons.get(reason, 0) + 1
    oldest = min((_num(r, "at") for r in records), default=None)
    return {"count": len(records), "evicted": _int(data, "evicted"),
            "oldest_age": round(now - oldest) if oldest is not None else None,
            "reasons": dict(sorted(reasons.items())), "totals": _totals(dict(data))}
