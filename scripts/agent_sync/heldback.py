"""Wakes held back for the seat's budget (owner, Sat, Oct 10:  "surface all").

A wake the budget refuses (a limit in wakes.budget_block, or a full FIFO) is held back instead of
dropped:  the daemon keeps one record per held-back MESSAGE in `<SEAT>/held-back.json`, next to the
ledger, and writes them before the ledger's `held_back` row, so a crash between the two never loses
one.  A coalesced wake that stood for five messages holds all five, each listed and surfaced on its
own (AFC-253:  a record used to keep only the wake's representative id).  Every other refusal (the
pause, the pin, the loop guard, staleness, a seat with no wake adapter) is still a plain `dropped`.

Two tiers, both surfaced, oldest first:
  - `records`:  full metadata (who, where, a link), at most `max`.
  - `overflow`:  past `max` the oldest records move here as compact entries (the id, the reason, the
    time, the DM flag, and what a catch-up needs), at most `overflow_max`.  They are listed in band
    like records, as items with no names (the hosted Worker reduces them to id, reason and time), so
    an overflowed id is still delivered by id, never only counted.
  - `lost`:  only past `overflow_max` does an id leave the file unsurfaced.  It is counted in `lost`,
    which every wake's count carries until a delivered wake reports it (never cleared by a timer), it
    gets an `evicted` ledger row, and the daemon tells the owner at once.

What happens to an entry:
  - surfaced:  a wake that reached the seat listed it, or it was that wake's own trigger.  This is the
    only way an entry leaves the file before it is lost or expired.
  - expired:  `ttl_hours` passed AND the owner was told about it by a digest that was actually shown
    (a Mac banner;  `told_seq`).  On the server nothing shows the owner anything that way:  the
    `#alerts` digest (heldalert.py, AFC-257) is a post the owner has not opened, which is not the
    same promise as a banner, so it does not mark anything told (docs/protocols/agent-sync-server.md,
    The Held-Back Digest on This Instance).  There nothing expires:  an entry stays until it is
    surfaced or lost past the caps.
Each end is also a ledger row for the held-back wake's id, written once the last entry that wake
held has left the file (daemon.py, referenced_wake_ids).

Records hold Zulip metadata only, never a message body:  the id is the reference to its seat-inbox
row, from which a catch-up wake rebuilds the excerpt.  Display copies are escaped and cut like the
wake body's (adapters.routine_body), so every string fits the hosted Worker's `held_back` rules (at
most 200 characters).

Every change is one locked read-modify-write of the file (live.update_json):  holds happen on the
router thread and on the seat's wake worker, and surfacing on the worker.  Everything takes the
time as an argument, so tests drive it with a fake clock.  A file written before AFC-253 (one
record per wake with a `trigger_ids` list, and a bare `evicted` counter) is migrated on read:  each
extra trigger id becomes its own record and the counter becomes `lost`.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from . import live as L

# The refusals that hold a wake back:  every budget wakes.budget_block names, and a full FIFO.
# `usd_per_day` is gone with the dollar axis (owner 2026-10-13):  wake accounting is frequency-only,
# so a dollar total can no longer produce a hold reason.  Kept out of the set deliberately -- a
# reason nothing can emit is worse than no reason at all.
HOLD_REASONS = frozenset(
    {
        "wakes_per_hour",
        "wakes_per_day",
        "per_topic_per_hour",
        "owner_per_day",
        "owner_per_topic_per_hour",
        "overflow",
        "host_busy",
        "host_wake_slots",
    }
)
FILE_NAME = "held-back.json"
NAME_LIMIT = 100  # channel, topic and sender, as in the wake body
LINK_LIMIT = 200  # a longer link is sent as "" (never cut):  the message id is enough to find it
# What an overflow entry keeps:  enough to list it by id and to build a catch-up wake from it.
COMPACT_KEYS = (
    "message_id",
    "seq",
    "at",
    "reason",
    "dm",
    "owner",
    "topic_key",
    "wake_ids",
    "route_tagged",
    "no_primary",
)
# A catch-up wake that surfaced nothing waits before the next one:  15 minutes, doubling up to 4 hours,
# and back to none once anything surfaces.  Bounded retries, on top of the budgets every wake obeys.
CATCH_UP_FIRST_WAIT = 15 * 60.0
CATCH_UP_MAX_WAIT = 4 * 3600.0
TOTAL_KEYS = ("held", "surfaced", "expired", "overflowed", "evicted")
_NEAR_RE = re.compile(r"/near/\d+$")


def path_for(paths: L.SeatPaths) -> str:
    return os.path.join(paths.seat_dir, FILE_NAME)


def is_id(value: Any) -> bool:
    """A Zulip message id:  a positive int (never a bool)."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


_is_id = is_id


def _mark_closing(removed: list[dict[str, Any]], data: Mapping[str, Any]) -> None:
    """Inside the lock that removed them:  tag each removed entry with the wake ids it closes (no entry
    left in the file stands for them), so the daemon writes each wake's closing ledger row exactly
    once, with no second read of the file racing another thread's change."""
    still = {
        w
        for e in list(data.get("records") or []) + list(data.get("overflow") or [])
        for w in wake_ids(e)
    }
    claimed: set[str] = set()
    for entry in removed:
        closes = [w for w in wake_ids(entry) if w not in still and w not in claimed]
        claimed.update(closes)
        entry["closes"] = closes


def _entries(rows: Any) -> list[dict[str, Any]]:
    return (
        [r for r in rows if isinstance(r, dict) and _is_id(r.get("message_id"))]
        if isinstance(rows, list)
        else []
    )


def _int(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _num(data: Mapping[str, Any], key: str) -> float:
    value = data.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _relink(link: str, message_id: int) -> str:
    """The same narrow pointed at another message of the same topic, or "" when it is not one."""
    return _NEAR_RE.sub("/near/%d" % message_id, link) if _NEAR_RE.search(link) else ""


def _normalize(data: dict[str, Any]) -> None:
    """Clean `data` in place:  valid entries only, one per message id, and the pre-AFC-253 shape
    migrated (each extra id of a record's `trigger_ids` becomes its own record;  `evicted` becomes `lost`)."""
    seen: set[int] = set()
    records: list[dict[str, Any]] = []
    for record in _entries(data.get("records")):
        rep = record["message_id"]
        ids = [i for i in record.pop("trigger_ids", None) or [] if _is_id(i)]
        ids = ids if rep in ids else ids + [rep]  # the wake's order, oldest first
        for entry in [
            record
            if i == rep
            else dict(
                record,
                message_id=i,
                owner=False,
                sender_full_name="",
                zulip_link=_relink(str(record.get("zulip_link") or ""), i),
            )
            for i in ids
        ]:
            if entry["message_id"] not in seen:
                seen.add(entry["message_id"])
                records.append(entry)
    overflow = []
    for entry in _entries(data.get("overflow")):
        if entry["message_id"] not in seen:
            seen.add(entry["message_id"])
            overflow.append(entry)
    data["records"], data["overflow"] = records, overflow
    if "evicted" in data:  # the pre-AFC-253 counter:  ids already gone, still owed to the owner
        data["lost"] = _int(data, "lost") + _int(data, "evicted")
        data.pop("evicted", None)
        data.pop("evicted_at", None)


def _totals(data: dict[str, Any]) -> dict[str, int]:
    totals = data.get("totals")
    if not isinstance(totals, dict):
        totals = {}
    clean = {k: _int(totals, k) for k in TOTAL_KEYS}
    data["totals"] = clean
    return clean


def load(paths: L.SeatPaths) -> dict[str, Any]:
    """The seat's state, read without the lock (a status or a choice that a locked call re-checks)."""
    data = L.read_json(path_for(paths))
    _normalize(data)
    return data


def display_link(link: str) -> str:
    """A link for an item:  "" when longer than LINK_LIMIT, never a cut (and broken) URL."""
    return link if len(link) <= LINK_LIMIT else ""


def wake_ids(record: Mapping[str, Any]) -> list[str]:
    """Every held-back wake an entry stands for (one, or more when a message was held back twice)."""
    return [w for w in record.get("wake_ids") or [] if isinstance(w, str)]


def compact(record: Mapping[str, Any]) -> dict[str, Any]:
    """The overflow entry for a record:  its id, reason, time and catch-up facts, no names."""
    entry = {k: record[k] for k in COMPACT_KEYS if k in record}
    entry["overflow"] = True
    return entry


def hold(
    paths: L.SeatPaths,
    records: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    cap: int,
    overflow_cap: int = 1000,
    now: float,
) -> tuple[list[int], list[dict[str, Any]]]:
    """Add records (one per message, oldest first) in one locked write.  Returns (the ids added, the
    entries lost past both caps).  A message already held back is not held twice (a catch-up wake the
    budget refused again, or a requeued trigger):  the existing entry only learns the new wake id, so
    that wake still gets its closing ledger row.  Past `cap` records the oldest move to `overflow`,
    and past `overflow_cap` the oldest overflow entries are lost (counted in `lost`)."""
    new = [records] if isinstance(records, Mapping) else list(records)
    out: dict[str, Any] = {"added": [], "lost": []}

    def mutate(data: dict[str, Any]) -> None:
        _normalize(data)
        totals = _totals(data)
        held, overflow = data["records"], data["overflow"]
        known = {e["message_id"]: e for e in held + overflow}
        for record in new:
            if not _is_id(record.get("message_id")):
                continue
            same = known.get(record["message_id"])
            if same is not None:
                have = wake_ids(same)
                same["wake_ids"] = (have + [w for w in wake_ids(record) if w not in have])[-10:]
                continue
            seq = _int(data, "next_seq") or 1
            entry = {k: v for k, v in record.items() if k != "trigger_ids"}
            entry["seq"] = seq
            held.append(entry)
            known[entry["message_id"]] = entry
            data["next_seq"] = seq + 1
            totals["held"] += 1
            out["added"].append(entry["message_id"])
        excess = len(held) - max(1, int(cap))
        if excess > 0:
            overflow.extend(compact(r) for r in held[:excess])
            del held[:excess]
            totals["overflowed"] += excess
        excess = len(overflow) - max(0, int(overflow_cap))
        if excess > 0:
            out["lost"] = overflow[:excess]
            del overflow[:excess]
            _mark_closing(out["lost"], data)
            data["lost"] = _int(data, "lost") + excess
            data["lost_at"] = now
            totals["evicted"] += excess

    L.update_json(path_for(paths), mutate)
    return out["added"], out["lost"]


def expire(paths: L.SeatPaths, *, ttl: float, now: float) -> list[dict[str, Any]]:
    """Remove the entries held back `ttl` seconds ago or more that the owner was told about (seq at
    most `told_seq`, set only by a digest that was shown), and return them.  Nothing the owner was
    never shown expires, and the `lost` counter never does."""
    out: list[dict[str, Any]] = []
    target = path_for(paths)
    if not os.path.exists(target):
        return out

    def mutate(data: dict[str, Any]) -> None:
        _normalize(data)
        totals = _totals(data)
        told = _int(data, "told_seq")

        def due(entry: Mapping[str, Any]) -> bool:
            return 0 < _int(entry, "seq") <= told and now - _num(entry, "at") >= ttl

        for tier in ("records", "overflow"):
            out.extend(e for e in data[tier] if due(e))
            data[tier] = [e for e in data[tier] if not due(e)]
        _mark_closing(out, data)
        totals["expired"] += len(out)

    L.update_json(target, mutate)
    return out


def wake_summary(
    paths: L.SeatPaths, *, exclude: Iterable[int], limit: int
) -> tuple[dict[str, Any] | None, list[int], int]:
    """What one wake carries:  ({count, items} or None, the listed message ids, the lost count it
    reports).  `exclude` is the wake's own trigger ids:  those entries are the trigger, not held back.
    count is every other entry plus `lost`;  items are the OLDEST `limit` entries, overflow first, so
    each delivered wake moves the backlog forward and nothing starves behind newer holds."""
    skip = set(exclude)
    data = load(paths)
    ordered = [e for e in data["overflow"] + data["records"] if e["message_id"] not in skip]
    lost = _int(data, "lost")
    count = len(ordered) + lost
    if count <= 0:
        return None, [], 0
    listed = ordered[: max(0, int(limit))]
    return (
        {"count": count, "items": [item(e) for e in listed]},
        [e["message_id"] for e in listed],
        lost,
    )


def item(record: Mapping[str, Any]) -> dict[str, Any]:
    """One `held_back` item:  exactly the fields the hosted Worker checks (src/wake.js heldBackProblem).
    An overflow entry has no names:  channel, topic and stream id null, sender and link ""."""
    dm = record.get("dm") is True
    stream_id = record.get("stream_id")
    bare = record.get("overflow") is True
    return {
        "message_id": int(record["message_id"]),
        "dm": dm,
        "channel": None if dm or bare else str(record.get("channel") or "")[:NAME_LIMIT],
        "topic": None if dm or bare else str(record.get("topic") or "")[:NAME_LIMIT],
        "stream_id": (
            stream_id
            if not dm
            and not bare
            and isinstance(stream_id, int)
            and not isinstance(stream_id, bool)
            and stream_id > 0
            else None
        ),
        "sender_full_name": "" if bare else str(record.get("sender_full_name") or "")[:NAME_LIMIT],
        "zulip_link": "" if bare else display_link(str(record.get("zulip_link") or "")),
        "reason": str(record.get("reason") or ""),
        "at": max(0, int(_num(record, "at"))),
    }


def surface(
    paths: L.SeatPaths, *, message_ids: Iterable[int], lost: int = 0
) -> list[dict[str, Any]]:
    """Remove the entries a delivered wake surfaced (either tier) and lower `lost` by what it
    reported.  Returns the removed entries.  Anything surfacing ends a catch-up backoff."""
    wanted = set(message_ids)
    out: list[dict[str, Any]] = []
    target = path_for(paths)
    if not os.path.exists(target):
        return out

    def mutate(data: dict[str, Any]) -> None:
        _normalize(data)
        totals = _totals(data)
        for tier in ("records", "overflow"):
            out.extend(e for e in data[tier] if e["message_id"] in wanted)
            data[tier] = [e for e in data[tier] if e["message_id"] not in wanted]
        _mark_closing(out, data)
        data["lost"] = max(0, _int(data, "lost") - max(0, int(lost)))
        totals["surfaced"] += len(out)
        if out or lost:
            data["catch_up_fails"] = 0

    L.update_json(target, mutate)
    return out


def referenced_wake_ids(paths: L.SeatPaths) -> set[str]:
    """The wake ids some entry still stands for:  a wake's closing ledger row waits until none does."""
    data = load(paths)
    return {w for e in data["records"] + data["overflow"] for w in wake_ids(e)}


def mark_no_primary(paths: L.SeatPaths, message_id: int, why: str) -> None:
    """A catch-up built on this entry cannot work (the routine refused its channel, or its trigger
    is gone):  never choose it as a catch-up trigger again.  It stays held back and listed."""

    def mutate(data: dict[str, Any]) -> None:
        _normalize(data)
        for entry in data["records"] + data["overflow"]:
            if entry["message_id"] == message_id:
                entry["no_primary"] = why

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
        _normalize(data)
        data["catch_up_at"] = now
        data["catch_up_fails"] = _int(data, "catch_up_fails") + 1

    L.update_json(path_for(paths), mutate)


def digest_due(data: Mapping[str, Any], *, every: float, now: float) -> list[dict[str, Any]]:
    """The entries held back since the last digest, when one may go now (at most one per `every`
    seconds), else []."""
    if every <= 0:
        return []
    rows = list(data.get("overflow") or []) + list(data.get("records") or [])
    fresh = sorted(
        (r for r in rows if _int(r, "seq") > _int(data, "digest_seq")), key=lambda r: _int(r, "seq")
    )
    if not fresh or ("digest_at" in data and now - _num(data, "digest_at") < every):
        return []
    return fresh


def note_digest(paths: L.SeatPaths, *, upto: int, now: float, told: bool = False) -> None:
    """A digest went out for every entry up to seq `upto`.  `told` only when the owner was actually
    shown it (notify returned True on an instance with banners):  then those entries may expire."""

    def mutate(data: dict[str, Any]) -> None:
        _normalize(data)
        data["digest_at"] = now
        data["digest_seq"] = max(_int(data, "digest_seq"), int(upto))
        if told:
            data["told_seq"] = max(_int(data, "told_seq"), int(upto))

    L.update_json(path_for(paths), mutate)


def summary(paths: L.SeatPaths, now: float) -> dict[str, Any]:
    """For status and `simple-chat wakes`:  how many are held back now (both tiers), how many of them
    are in overflow, the oldest one's age in seconds, the count per reason, `evicted` (ids lost past
    both caps, still owed) and the lifetime totals."""
    data = load(paths)
    rows = data["records"] + data["overflow"]
    reasons: dict[str, int] = {}
    for row in rows:
        reason = str(row.get("reason") or "?")
        reasons[reason] = reasons.get(reason, 0) + 1
    oldest = min((_num(r, "at") for r in rows), default=None)
    return {
        "count": len(rows),
        "overflow": len(data["overflow"]),
        "evicted": _int(data, "lost"),
        "oldest_age": round(now - oldest) if oldest is not None else None,
        "reasons": dict(sorted(reasons.items())),
        "totals": _totals(dict(data)),
    }
