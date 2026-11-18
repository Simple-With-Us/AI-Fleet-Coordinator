"""Wake bookkeeping for the listener daemon:  the ledger, budgets, coalescing, the loop guard,
the model gate's record, and the mention rules for the Operator's own text.

Everything here is deterministic and takes the time as an argument, so tests drive it with a
fake clock.

Ledger (`<SEAT>/wakes.jsonl`), one JSON line per state change, written before the thing it
guards:  `queued` before the cursor moves past the trigger, `accepted` when the job enters the
seat's FIFO, `started` before the routine is called, then `done` or `failed`.  `dropped`,
`skipped` and `held_back` end a wake that never ran.  A held-back wake (refused for its budget,
heldback.py) later gets one more row:  `surfaced`, `expired` or `evicted`.  On restart, queued
and accepted wakes are reloaded; a `started` with no outcome is marked failed and never re-run.

For an `http` wake, `done` means only that the routine answered 2xx.  When the routine is the one
wake router (config.routes_to_router), that 2xx says the router stored the wake, nothing more:
the `done` row carries `receipt_awaited`, the daemon reads the router's receipt later
(Daemon.receipt_pass), and the terminal outcome arrives as its own row (AFC-824):  `delivered`,
`undelivered` (the router's `dead` or `held`) or `unknown` (the router could not tell,
or no terminal receipt was read within RECEIPT_DEADLINE of the 2xx).  That row carries only the
receipt fields RECEIPT_FIELDS lets through, and it does not move the wake's `ts` in the merged
view (Ledger.wakes).

Model gate (phase 1, record only):  a wake records why it was started.  The listener starts no
model (owner ruling, Nov 10, 2026), so the only start it records today is the routine forward
(`det_routine_forward`);  the other purposes and codes stay for rows already in the ledger and a
follow-up trims them.  `model_gate_verdict` is the pure rule table, `gate_counts` reads the rows
back, and the `gate*` row fields are listed in docs/protocols/simple-tools-listener.md.  Nothing
here blocks or reroutes a start.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import errno
import json
import math
import os
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any, NamedTuple

from . import live as L
from . import secretscan

LEDGER_KEEP_SECONDS = 90 * 86400
DAY = 86400.0
HOUR = 3600.0
FIFO_LIMIT = 5
LIMITS = {"owner_note": 500}  # the escalation note's quoted text (Daemon.owner_dm_text)
OPEN_STATES = ("queued", "accepted")
SPENT_STATES = ("started", "done", "failed")
# The router's terminal outcome of a `done` wake it accepted (AFC-824):  one more row, after `done`.
DELIVERED = "delivered"
OUTCOME_STATES = (DELIVERED, "undelivered", "unknown")
FINAL_STATES = (
    "done",
    "failed",
    "dropped",
    "skipped",
    "held_back",
    "surfaced",
    "expired",
    "evicted",
    # A wake whose triggers were absorbed by the other wake for the same topic
    # (Coalescer.restore):  closed, never re-offered by a crash recovery.
    "merged",
    *OUTCOME_STATES,
)


# --------------------------------------------------------------------------------------------
# Ledger
# --------------------------------------------------------------------------------------------


class Ledger:
    def __init__(self, path: str) -> None:
        self.path = path
        self.lock = threading.Lock()
        # rows() runs on every status write (every 2 s) for every seat; re-parse the file only
        # when it changed.  Keyed on inode, size and mtime, so an append or a prune's
        # os.replace always misses the cache.
        self._cache_sig: tuple[int, int, int] | None = None
        self._cache_rows: list[dict[str, Any]] = []

    def _sig(self) -> tuple[int, int, int] | None:
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return (st.st_ino, st.st_size, st.st_mtime_ns)

    def append(self, row: Mapping[str, Any]) -> None:
        with self.lock:
            L.append_jsonl(self.path, [row])
            self._cache_sig = None

    def rows(self) -> list[dict[str, Any]]:
        with self.lock:
            sig = self._sig()
            if sig is None or sig != self._cache_sig:
                self._cache_rows = L.read_jsonl(self.path)
                self._cache_sig = sig
            return [dict(r) for r in self._cache_rows]

    def wakes(self) -> dict[str, dict[str, Any]]:
        """wake id -> the merged view:  the latest state, the first time each state was reached,
        and the fields of every row (later rows win).  The one exception is the router's outcome
        row (OUTCOME_STATES, AFC-824), which can land up to RECEIPT_DEADLINE after the wake ran:
        it does not move `ts`, so `ts` stays the time the wake itself last moved, and what is
        measured from it (the stale-once day, the 24-hour wakes view) is measured from the run.
        The outcome's own time is `times[<outcome>]`, and the outcome row itself is kept whole as
        `outcome_row`, so what the router said (outcome_fields) is read from that row alone and
        never from a field an earlier row of the wake carried."""
        merged: dict[str, dict[str, Any]] = {}
        for row in self.rows():
            wake_id = row.get("wake_id")
            if not isinstance(wake_id, str):
                continue
            view = merged.setdefault(wake_id, {"times": {}})
            outcome = row.get("state") in OUTCOME_STATES
            skip = ("times", "outcome_row", "ts") if outcome else ("times", "outcome_row")
            view.update({k: v for k, v in row.items() if k not in skip})
            view["times"].setdefault(str(row.get("state")), row.get("ts"))
            if outcome:
                view["outcome_row"] = dict(row)
        return merged

    def prune(self, now: float) -> None:
        """Drop wakes older than 90 days (by their first row)."""
        with self.lock:
            rows = L.read_jsonl(self.path)
            first: dict[str, float] = {}
            for row in rows:
                if isinstance(row.get("ts"), (int, float)):
                    first.setdefault(str(row.get("wake_id")), float(row["ts"]))
            keep = [
                r for r in rows if now - first.get(str(r.get("wake_id")), now) < LEDGER_KEEP_SECONDS
            ]
            if len(keep) != len(rows):
                tmp = self.path + ".prune"
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    for row in keep:
                        fh.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")
                os.replace(tmp, self.path)
                self._cache_sig = None

    def recover(self, now: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """After a restart:  (wakes to reload, wakes that were started and never finished).  The
        second list is marked failed here and is never re-run."""
        reload: list[dict[str, Any]] = []
        crashed: list[dict[str, Any]] = []
        for wake_id, view in self.wakes().items():
            state = view.get("state")
            if state in OPEN_STATES:
                reload.append(view)
            elif state == "started":
                crashed.append(view)
                self.append(
                    {
                        "ts": now,
                        "wake_id": wake_id,
                        "seat": view.get("seat"),
                        "state": "failed",
                        "reason": "daemon stopped during the run",
                    }
                )
        return reload, crashed


def at_cap(count: float, budget: Mapping[str, float], name: str) -> bool:
    """Whether `count` has reached the cap `name` in a seat's budget.  A cap that is absent from the
    budget is NO LIMIT (owner, Nov 10, 2026) and is never reached;  `0` is reached at once, which is how
    a seat is paused for wakes;  a positive number is a cap.  The five wake count caps have no
    default (config.WAKE_CAP_KEYS), so a budget without one is the normal case, not a damaged one."""
    cap = budget.get(name)
    return cap is not None and count >= cap


def budget_block(
    views: Mapping[str, Mapping[str, Any]],
    budget: Mapping[str, float],
    *,
    owner: bool,
    key: str,
    now: float,
    exclude: str | None = None,
    run_max: float = 0.0,
) -> str | None:
    """None when one more wake fits the seat's budget, else the name of the limit it hits.
    Wakes count from the moment they are accepted into the FIFO.  the COUNT caps are the whole budget (owner 2026-10-13):  there is deliberately no dollar axis
    here.  It never measured anything real -- a subscription-backed wake is 0.0 and a metered
    one's per-run maximum is approximate -- while what costs downstream is the FAN-OUT, which a
    dollar ceiling does not measure and a frequency cap does.  `exclude` is the wake
    being checked, so a re-check just before its run never counts it twice.  A wake whose router
    outcome row followed its `done` (OUTCOME_STATES) ran, and counts like the `done` it was."""
    others = {wake_id: view for wake_id, view in views.items() if wake_id != exclude}
    counted: list[Mapping[str, Any]] = []
    for view in others.values():
        times = view.get("times") or {}
        at = times.get("accepted", times.get("started"))
        if (
            view.get("state") in ("accepted", "started", "done", "failed", *OUTCOME_STATES)
            and isinstance(at, (int, float))
            and now - at < DAY
        ):
            counted.append(view)

    def times_of(view: Mapping[str, Any]) -> float:
        times = view.get("times") or {}
        return float(times.get("accepted", times.get("started")))

    same = [v for v in counted if bool(v.get("owner")) == owner]
    topic = [v for v in same if v.get("topic_key") == key and now - times_of(v) < HOUR]
    if owner:
        if at_cap(len(same), budget, "owner_per_day"):
            return "owner_per_day"
        if at_cap(len(topic), budget, "owner_per_topic_per_hour"):
            return "owner_per_topic_per_hour"
        return None
    if at_cap(sum(1 for v in same if now - times_of(v) < HOUR), budget, "wakes_per_hour"):
        return "wakes_per_hour"
    if at_cap(len(same), budget, "wakes_per_day"):
        return "wakes_per_day"
    if at_cap(len(topic), budget, "per_topic_per_hour"):
        return "per_topic_per_hour"
    return None


# --------------------------------------------------------------------------------------------
# Coalescing
# --------------------------------------------------------------------------------------------


class Pending:
    """A wake waiting out its quiet window."""

    def __init__(
        self,
        wake_id: str,
        seat: str,
        item: Mapping[str, Any],
        *,
        owner: bool,
        now: float,
        due: float,
    ) -> None:
        self.wake_id = wake_id
        self.seat = seat
        self.channel = str(item.get("channel") or "")
        self.topic = str(item.get("topic") or "")
        self.key = L.topic_key(self.channel, self.topic)
        self.type = str(item.get("type") or "stream")
        self.recipients = list(item.get("recipients") or [])
        self.stream_id = item.get("stream_id")
        self.trigger_ids: list[int] = []
        self.owner_ids: list[int] = []
        self.sender_id: int | None = None  # who asked first; see add()
        self.stale = bool(item.get("stale"))
        self.owner = owner
        self.first = now
        self.due = due
        # Leases a trigger came back from (grace release or reap).  The takeover never hands the
        # wake back to them, so a deaf lease cannot swallow the trigger again.
        self.skip_leases: list[str] = []
        self.trigger_ts: float | None = (
            None  # the newest trigger's message time, for the stale check
        )
        self.route_tagged = False  # a trigger is a `·route` post:  this wake never routes again
        # The triggers that reached the seat because they answer one of its chats (router class
        # `reply_to_chat`, owner Nov 10 2026), each with the session letter of that chat ("" when the post
        # it answers carried none).  The wake row says so (`linked_by`, `reply_target_letter`), so a wrong
        # pick can be read back.
        self.reply_links: dict[int, str] = {}
        # A catch-up wake (daemon._catch_up):  built from a held-back record, with no trigger_ts, because
        # the record's TTL bounds its age instead of the stale rule.
        self.catch_up = False
        # The model gate's record of this wake's model starts (phase 1, record only:  see the Model
        # gate section below).  Set by the daemon just before the start, so every row written after
        # it carries the fields.  None until a start was decided, which is also what every row from
        # before the gate looks like.
        self.gate: str | None = None
        self.gate_avoidable: bool | None = None
        self.gate_fallback: str | None = None
        self.gate_fallback_reason: str | None = None
        self.gate_inspect: str | None = None
        # What was learned about a tagged session letter, for the gate.  Nothing sets them since the
        # headless wake path went with the retired wake types (owner ruling, Nov 10, 2026), so the
        # gate leaves those facts out;  kept with the gate, which a follow-up trims.  Not written
        # to the ledger.
        self.letter_candidates: int | None = None
        self.letter_holder_on_seat: bool | None = None

    def note_gate(self, verdict: GateVerdict) -> None:
        """The primary start's verdict."""
        self.gate = verdict.code
        self.gate_avoidable = verdict.avoidable

    def note_gate_fallback(self, verdict: GateVerdict, reason: str) -> None:
        """The fallback start's verdict and why the fallback was needed.  The primary's `gate` is
        left as it was:  a fallback row keeps the primary's code and adds its own reason."""
        self.gate_fallback = verdict.code
        self.gate_fallback_reason = reason

    def note_gate_inspect(self, verdict: GateVerdict) -> None:
        """The second Muse call's verdict (the chat inspection)."""
        self.gate_inspect = verdict.code

    def add(self, item: Mapping[str, Any], owner: bool) -> None:
        message_id = item.get("id")
        if isinstance(message_id, int) and message_id not in self.trigger_ids:
            self.trigger_ids.append(message_id)
            if owner:
                self.owner_ids.append(message_id)
        for field in ("released_from", "returned_from"):
            lease_id = item.get(field)
            if isinstance(lease_id, str) and lease_id and lease_id not in self.skip_leases:
                self.skip_leases.append(lease_id)
        ts = item.get("ts")
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            self.trigger_ts = (
                float(ts) if self.trigger_ts is None else max(self.trigger_ts, float(ts))
            )
        if "route_tag" in (item.get("classes") or []):
            self.route_tagged = True
        if isinstance(message_id, int) and "reply" in (item.get("classes") or []):
            self.reply_links[message_id] = str(item.get("reply_target_letter") or "")
        sender = item.get("sender_id")
        if isinstance(sender, int) and not isinstance(sender, bool) and self.sender_id is None:
            # The FIRST trigger's sender, which is the one whose question this wake answers.  Kept on
            # the pending so a held wake can tell that person or bot why it got no reply (owner
            # 2026-10-13) without re-reading the inbox to find out who asked.
            self.sender_id = sender
        self.owner = self.owner or owner

    def merge(self, other: Pending) -> None:
        """Absorb a NEWER pending for the same topic:  add() semantics, pending-to-pending.

        The other's trigger ids come after this wake's own (they arrived later);  owner,
        stale, route_tagged and skip_leases union;  the newest trigger time;  the first
        asker kept when this wake knows one, else the other's;  first/due keep the
        earlier bound, so no trigger ever fires later than its own window."""
        seen = set(self.trigger_ids)
        for message_id in other.trigger_ids:
            if message_id not in seen:
                seen.add(message_id)
                self.trigger_ids.append(message_id)
        owners = set(self.owner_ids)
        for message_id in other.owner_ids:
            if message_id not in owners:
                owners.add(message_id)
                self.owner_ids.append(message_id)
        for lease_id in other.skip_leases:
            if lease_id not in self.skip_leases:
                self.skip_leases.append(lease_id)
        if other.trigger_ts is not None:
            self.trigger_ts = (
                other.trigger_ts
                if self.trigger_ts is None
                else max(self.trigger_ts, other.trigger_ts)
            )
        self.route_tagged = self.route_tagged or other.route_tagged
        for message_id, letter in other.reply_links.items():
            self.reply_links.setdefault(message_id, letter)
        self.stale = self.stale or other.stale
        self.owner = self.owner or other.owner
        if self.sender_id is None:
            self.sender_id = other.sender_id
        self.first = min(self.first, other.first)
        self.due = min(self.due, other.due)
        if other.catch_up and self.trigger_ts is None:
            self.catch_up = True

    def reply_link(self) -> tuple[str, str] | None:
        """("reply", the chat's session letter) when the trigger this wake is about answers one of the
        seat's chats, else None.  The trigger is the one the routine body is built from:  the newest
        owner trigger when the owner is among them, else the newest trigger."""
        ids = self.owner_ids or self.trigger_ids
        letter = self.reply_links.get(ids[-1]) if ids else None
        return None if letter is None else ("reply", letter)

    def row(self, state: str, now: float, **extra: Any) -> dict[str, Any]:
        row = {
            "ts": now,
            "wake_id": self.wake_id,
            "seat": self.seat,
            "state": state,
            "channel": self.channel,
            "topic": self.topic,
            "topic_key": self.key,
            "type": self.type,
            "trigger_ids": list(self.trigger_ids),
            "owner_ids": list(self.owner_ids),
            "owner": self.owner,
            "due": self.due,
            "stale": self.stale,
            "recipients": list(self.recipients),
            "stream_id": self.stream_id,
            "skip_leases": list(self.skip_leases),
            "trigger_ts": self.trigger_ts,
            "route_tagged": self.route_tagged,
            # The FIRST trigger's sender, kept in the row so a wake reloaded after a daemon
            # restart still knows who asked.
            "sender_id": self.sender_id,
        }
        if self.catch_up:
            row["catch_up"] = True
        link = self.reply_link()
        if link is not None:
            row["linked_by"], row["reply_target_letter"] = link
        # The model gate's record, on every row written after the start was decided (a row from before
        # it, or from a daemon that predates it, has none of these keys).
        if self.gate is not None:
            row["gate"] = self.gate
            row["gate_avoidable"] = self.gate_avoidable
        if self.gate_fallback is not None:
            row["gate_fallback"] = self.gate_fallback
            row["gate_fallback_reason"] = self.gate_fallback_reason
        if self.gate_inspect is not None:
            row["gate_inspect"] = self.gate_inspect
        row.update(extra)
        return row

    @classmethod
    def from_view(cls, view: Mapping[str, Any]) -> Pending:
        item = {
            "channel": view.get("channel"),
            "topic": view.get("topic"),
            "type": view.get("type"),
            "recipients": view.get("recipients"),
            "stream_id": view.get("stream_id"),
            "stale": view.get("stale"),
        }
        pending = cls(
            str(view["wake_id"]),
            str(view.get("seat")),
            item,
            owner=bool(view.get("owner")),
            now=float(view.get("ts") or 0),
            due=float(view.get("due") or 0),
        )
        pending.trigger_ids = [i for i in view.get("trigger_ids") or [] if isinstance(i, int)]
        pending.owner_ids = [i for i in view.get("owner_ids") or [] if isinstance(i, int)]
        pending.skip_leases = [i for i in view.get("skip_leases") or [] if isinstance(i, str)]
        ts = view.get("trigger_ts")
        pending.trigger_ts = (
            float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) else None
        )
        # sender_id is excluded when it is a bool, exactly as trigger_ts is:  a reloaded wake must
        # keep the person who asked.
        sender = view.get("sender_id")
        pending.sender_id = (
            int(sender) if isinstance(sender, int) and not isinstance(sender, bool) else None
        )
        pending.route_tagged = view.get("route_tagged") is True
        if view.get("linked_by") == "reply":
            ids = pending.owner_ids or pending.trigger_ids
            if ids:
                pending.reply_links[ids[-1]] = str(view.get("reply_target_letter") or "")
        pending.catch_up = view.get("catch_up") is True
        # Restoring the gate record is for a wake that is restored AFTER its start was decided.  Today
        # only `queued` and `accepted` wakes are reloaded (a `started` one is marked failed and never
        # re-run), and those rows carry no gate yet, so in production this finds nothing;  it is here so
        # a future restore keeps the record, and it is tested.  A token or nothing, never a guess.
        for name in ("gate", "gate_fallback", "gate_fallback_reason", "gate_inspect"):
            value = view.get(name)
            if isinstance(value, str) and _GATE_TOKEN_RE.fullmatch(value):
                setattr(pending, name, value)
        avoidable = view.get("gate_avoidable")
        pending.gate_avoidable = avoidable if isinstance(avoidable, bool) else None
        return pending


class Coalescer:
    """A wake-worthy item opens a quiet window per (seat, topic):  20 seconds, extended by each new
    item up to 90 seconds from the first.  Owner items wait 5 seconds."""

    def __init__(
        self, *, window: float, max_window: float, owner_window: float, new_id: Callable[[], str]
    ) -> None:
        self.window = window
        self.max_window = max_window
        self.owner_window = owner_window
        self.new_id = new_id
        self.pending: dict[tuple[str, str], Pending] = {}

    def offer(self, seat: str, item: Mapping[str, Any], owner: bool, now: float) -> Pending:
        key = (seat, L.topic_key(str(item.get("channel") or ""), str(item.get("topic") or "")))
        pending = self.pending.get(key)
        wait = self.owner_window if owner else self.window
        if pending is None:
            pending = Pending(self.new_id(), seat, item, owner=owner, now=now, due=now + wait)
            self.pending[key] = pending
        elif owner:
            pending.due = min(pending.due, now + self.owner_window)
        elif not pending.owner:  # a peer item never delays an owner wake
            pending.due = min(max(pending.due, now + self.window), pending.first + self.max_window)
        pending.add(item, owner)
        return pending

    def restore(self, pending: Pending) -> tuple[Pending, Pending | None]:
        """Re-arm a deferred wake;  returns (survivor, absorbed).

        A newer offer for the same (seat, topic) may already occupy the slot:  the
        deferred wake was popped when it went due, so offer() built a fresh pending
        with its own trigger ids and its own queued ledger row.  Overwriting the slot
        would drop those trigger ids and strand that row, so the OLDER pending absorbs
        the newer one's triggers (Pending.merge) and the caller closes the absorbed
        wake's ledger row and refreshes the survivor's:  no trigger id is lost and a
        later crash recovery re-offers nothing twice."""
        key = (pending.seat, pending.key)
        existing = self.pending.get(key)
        if existing is None or existing is pending:
            self.pending[key] = pending
            return pending, None
        if existing.first <= pending.first:
            existing.merge(pending)
            return existing, pending
        pending.merge(existing)
        self.pending[key] = pending
        return pending, existing

    def take_due(self, now: float) -> list[Pending]:
        ready = [p for p in self.pending.values() if p.due <= now]
        for pending in ready:
            self.pending.pop((pending.seat, pending.key), None)
        return sorted(ready, key=lambda p: (not p.owner, p.due))


# --------------------------------------------------------------------------------------------
# Loop guard
# --------------------------------------------------------------------------------------------


class LoopGuard:
    """After 3 wake replies in a topic with no owner post in between, the topic stops waking.  Only
    a post by the owner (owner_user_id from a human Zulip app) resets it; a content tag never does.

    A DM thread the owner is not in (bots only, `rolling`) can never get an owner post, so a fixed
    count would block that pair for good.  There the count is rolling:  3 wake replies within
    DM_WINDOW stop the thread waking, and it wakes again once the oldest of them is DM_WINDOW old.
    That bounds bot-to-bot DM ping-pong (owner 2026-10-09 made bot DMs wake) without a permanent
    block.  What counts:  `·wake`-tagged replies and `·route` posts (a wake seat paging another seat
    on the owner's behalf, owner 2026-10-10), never other posts."""

    LIMIT = 3
    DM_WINDOW = 6 * HOUR

    def __init__(self, path: str, *, enabled: bool = False) -> None:
        # Off unless the caller turns it on:  the listener passes `[daemon] wake_loop_prevention`,
        # which is off unless the config says true (owner, Nov 10, 2026:  wakes are liberal, nothing
        # throttles them).
        self.enabled = enabled
        self.path = path
        self.lock = threading.Lock()

    @staticmethod
    def _recent(value: Any, now: float) -> list[float]:
        if not isinstance(value, list):
            return []
        return [
            float(t)
            for t in value
            if isinstance(t, (int, float))
            and not isinstance(t, bool)
            and 0 <= now - float(t) < LoopGuard.DM_WINDOW
        ]

    def note(
        self,
        key: str,
        *,
        wake_reply: bool,
        owner: bool,
        rolling: bool = False,
        now: float | None = None,
    ) -> None:
        if not wake_reply and not owner:
            return
        at = time.time() if now is None else float(now)
        with self.lock:

            def mutate(data: dict[str, Any]) -> None:
                if owner:
                    data.pop(key, None)
                elif rolling:
                    data[key] = (self._recent(data.get(key), at) + [at])[-self.LIMIT * 4 :]
                else:
                    value = data.get(key)
                    count = len(value) if isinstance(value, list) else int(value or 0)
                    data[key] = count + 1

            L.update_json(self.path, mutate)

    def count(self, key: str, now: float | None = None) -> int:
        """The replies the guard is counting in this topic:  the running total, or for a rolling bot DM
        the ones still inside DM_WINDOW."""
        return self.counted(L.read_json(self.path).get(key), now)

    @classmethod
    def counted(cls, value: Any, now: float | None = None) -> int:
        if isinstance(value, list):
            return len(cls._recent(value, time.time() if now is None else float(now)))
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    def blocked(self, key: str, now: float | None = None) -> bool:
        return self.enabled and self.count(key, now) >= self.LIMIT

    def holding(self, now: float | None = None) -> list[dict[str, Any]]:
        """The topics at their limit, for `simple-chat status` (AFC-255):  the channel, the topic, the
        count that holds each and whether that count rolls (a bot DM, where it ages out) or waits for
        the owner however long that takes.  A guard that blocks silently looked like a working one, so
        status names every topic it is holding and why it is still holding it."""
        if not self.enabled:
            return []
        at = time.time() if now is None else float(now)
        out = []
        for key, value in L.read_json(self.path).items():
            count = self.counted(value, at)
            if count < self.LIMIT:
                continue
            channel, _, topic = key.partition("\u0000")
            out.append(
                {
                    "channel": channel,
                    "topic": topic,
                    "count": count,
                    "rolling": isinstance(value, list),
                }
            )
        return sorted(out, key=lambda e: (e["channel"], e["topic"]))


# --------------------------------------------------------------------------------------------
# Model gate (phase 1:  record only)
# --------------------------------------------------------------------------------------------
#
# Owner model:  a seat wake is a deterministic delivery, and the only model run is the Operator's
# own consultant, sometimes, only when the deterministic code cannot decide.  So every model start
# records WHY it was needed, and the counts show how often the consultant fires and which rule to
# make deterministic next.  This phase only records:  `model_gate_verdict` is a pure rule table, the
# daemon stores its answer next to the start, and nothing is blocked, removed or
# rerouted.  The row fields and the code vocabulary are in docs/protocols/simple-tools-listener.md.

GATE_PURPOSES = (
    "select_destination",  # the model is asked which listed chat or session should take the message
    "compose_reply",  # a seat wake:  the model composes the reply the Operator posts
    "routine",  # an http routine wake (a Grok Bot routine, for one)
    "fallback",  # the second runtime after the primary failed or could not run
    "inspect",  # the second Muse call that reads one listed chat's excerpt
)

#: Every token a record can carry, with the rule it names.  `det_*` means the facts the caller held
#: satisfied a deterministic rule that Phase 2 could act on before a model starts (the exact conditions
#: are in `model_gate_verdict`, and each says what it proves and what it does not); `model_*` means the
#: code could not decide; `gate_error` marks a start whose verdict could not be computed (the wake still ran).
#: `det_routine_forward` is the odd `det_*` one: an `http` routine forward is a deterministic delivery
#: by design, so it is never `avoidable` and is counted as a forward, never as a model start.
GATE_RULES: dict[str, str] = {
    "det_eyes": "no_wake_marker",
    "det_live_lease": "live_lease_takeover",
    "det_bound_chat": "bound_chat_nudge",
    "det_tagged_letter_single": "tagged_letter_one_candidate",
    "det_exact_chat_id": "exact_chat_id_in_text",
    "det_routine_forward": "routine_forward_is_delivery",
    "model_letter_ambiguous": "tagged_letter_two_or_more_candidates",
    "model_select_destination": "addressee_unresolved",
    "model_compose_reply": "reply_composition_is_the_model_job",
    "model_fallback": "primary_failed",
    "gate_error": "gate_failed",
}
GATE_CODES = tuple(GATE_RULES)
#: The `det_*` codes that are a delivery and not a rule a model start could have been replaced by.
GATE_DELIVERY_CODES = ("det_routine_forward",)
#: Why a fallback was needed, the rule of a `model_fallback` verdict and the `gate_fallback_reason` field.
GATE_FALLBACK_REASONS = ("primary_failed", "primary_invalid_result", "primary_unavailable")
_GATE_TOKEN_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")


class GateVerdict(NamedTuple):
    """What the gate says about one model start.  Immutable, and all three fields are bounded tokens."""

    code: str  # one of GATE_CODES
    avoidable: (
        bool  # a `det_*` rule applied (a routine forward excepted) and the model started anyway
    )
    rule: str  # the deterministic rule that applied, or the reason none could


def _gate_count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def model_gate_verdict(purpose: str, facts: Mapping[str, Any] | None = None) -> GateVerdict:
    """Why a model start was needed, from the facts the caller passes in.  This function reads nothing
    and changes nothing, and an unknown purpose is the only input that raises.  (The caller that
    gathers the facts does read, the daemon looking at the chat-nudges file for `bound_chat`, for one.)

    Facts (every key optional, and a missing or malformed one counts as not known, never as false):

    - `eyes`:  every trigger the gate could read carries the no-wake marker, stripped the way the
      router strips it.  A routine start reads its one representative trigger, and the daemon sees only
      the triggers still inside the history window.  The router offers no wake for such a message, so a
      hit is a leak.
    - `live_lease`:  a live session could take the wake.  The daemon states it only at the first start
      (False, from the check just made) and does not probe again for a fallback or an inspection.
    - `bound_chat`:  the topic is bound to an existing chat (a GROK, CURSOR or CODEX seat on a channel
      topic, with no broadcast token in a trigger).  The router nudges a bound chat and offers no wake,
      so a wake that reaches a start with a binding present is a router gap that the gate surfaces: a
      lease release or reap that was requeued straight to a wake, or a binding made after the routing.  A
      broadcast the fanout owns is left out, because the router skips the binding for it on purpose.
    - `letter`, `letter_candidates` and `letter_holder_on_seat`:  the tagged session letter, how many
      live sessions hold it on any platform, and whether the one holder is on the tagged seat itself.
    - `exact_chat_ids`:  how many listed chats are legal destinations (wake-capable, not stale, seat
      enabled, and for a private message the same seat) and have their id in the trigger text.
    - `risk_screen_needed`:  the model's risk answer would still decide whether the message may be
      routed (a peer-triggered message).  False only when none is needed.
    - `fallback_reason`:  one of GATE_FALLBACK_REASONS, used by the `fallback` purpose.

    The first `det_*` rule that applies wins, in the order above; with none, the purpose decides, in this
    order: a routine forward, a fallback, then an ambiguous letter, a reply, a destination.  What each `det_*` proves: `det_eyes` that no wake should have started; `det_live_lease`
    that a session was there to take it; `det_bound_chat` that the router's own binding rule applied and
    did not stop the wake; `det_tagged_letter_single` that the address names one live session of the
    tagged seat (not that routing the request is safe); `det_exact_chat_id` that one legal destination
    exists and no risk answer is needed for it.  A routine forward is a delivery (`det_routine_forward`, never avoidable).  `avoidable` is true for the `det_*` codes except
    the delivery."""
    if purpose not in GATE_PURPOSES:
        raise ValueError("unknown model gate purpose")
    f: Mapping[str, Any] = facts if isinstance(facts, Mapping) else {}
    letter = f.get("letter")
    tagged = isinstance(letter, str) and bool(letter.strip())
    candidates = _gate_count(f.get("letter_candidates"))
    no_risk_screen = f.get("risk_screen_needed") is False
    if f.get("eyes") is True:
        code = "det_eyes"
    elif f.get("live_lease") is True:
        code = "det_live_lease"
    elif f.get("bound_chat") is True:
        code = "det_bound_chat"
    elif tagged and candidates == 1 and f.get("letter_holder_on_seat") is True:
        code = "det_tagged_letter_single"
    elif _gate_count(f.get("exact_chat_ids")) == 1 and no_risk_screen:
        code = "det_exact_chat_id"
    elif purpose == "routine":
        code = "det_routine_forward"
    elif purpose == "fallback":
        code = "model_fallback"
    elif tagged and candidates is not None and candidates >= 2:
        code = "model_letter_ambiguous"
    elif purpose == "compose_reply":
        code = "model_compose_reply"
    else:
        code = "model_select_destination"
    rule = GATE_RULES[code]
    if code == "model_fallback" and f.get("fallback_reason") in GATE_FALLBACK_REASONS:
        rule = str(f["fallback_reason"])
    return GateVerdict(code, code.startswith("det_") and code not in GATE_DELIVERY_CODES, rule)


def gate_verdict_safe(purpose: str, facts: Mapping[str, Any] | None = None) -> GateVerdict:
    """`model_gate_verdict` that never raises: a failure is a `gate_error` verdict, so a bug in the
    gate can be recorded and can never stop the wake it describes."""
    try:
        return model_gate_verdict(purpose, facts)
    except Exception:  # noqa: BLE001 - the gate must never block a wake
        return GateVerdict("gate_error", False, GATE_RULES["gate_error"])


def gate_fields(verdict: GateVerdict) -> dict[str, Any]:
    """The two fields a start's record persists."""
    return {"gate": verdict.code, "gate_avoidable": verdict.avoidable}


def _gate_number(value: Any) -> float | None:
    """A finite float for a real number, else None: a bool, a string, NaN, an infinity and an integer
    too large for a float are all None, so a bad timestamp in a row can never raise."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _gate_token(value: Any) -> str | None:
    return value if isinstance(value, str) and _GATE_TOKEN_RE.fullmatch(value) else None


def rows_since(
    path: str, since: float, *, chunk: int = 1 << 16, slack: float = HOUR
) -> list[dict[str, Any]] | None:
    """The rows of a time-ordered JSONL ledger that are not older than `since`, read from the END of
    the file so a status call costs the window and not the 90 days a ledger keeps.

    Rows are appended as they happen, so `ts` only runs forward; the read stops at the first row older
    than `since - slack` (an hour of slack for a clock that stepped back), and the caller still filters
    by its own window.  A line that cannot be parsed (not JSON, not an object, nested too deep), a partial
    last line and a row with no `ts` are skipped or kept without stopping the read.

    A ledger that is not there is no rows (`[]`, a real zero).  A ledger that is there and cannot be read
    (no permission, a directory, an I/O error) is `None`, so a caller never mistakes it for an empty
    day, and an unusable `since` is `None` too."""
    floor = _gate_number(since)
    if floor is None:
        return None
    floor -= _gate_number(slack) or 0.0
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        return [] if exc.errno in (errno.ENOENT, errno.ENOTDIR) else None
    newest_first: list[dict[str, Any]] = []
    try:
        pos = os.fstat(fd).st_size
        carry = b""
        stop = False
        while pos > 0 and not stop:
            size = min(chunk, pos)
            pos -= size
            lines = (os.pread(fd, size, pos) + carry).split(b"\n")
            carry = (
                lines[0] if pos > 0 else b""
            )  # the front of a line that began in an earlier chunk
            for raw in reversed(lines[1:] if pos > 0 else lines):
                try:
                    row = json.loads(raw)
                except (ValueError, RecursionError):
                    continue
                if not isinstance(row, dict):
                    continue
                ts = _gate_number(row.get("ts"))
                if ts is not None and ts < floor:
                    stop = True
                    break
                newest_first.append(row)
    except OSError:
        return None
    finally:
        os.close(fd)
    newest_first.reverse()
    return newest_first


def gate_counts(
    rows: Iterable[Mapping[str, Any]], now: float, window: float = DAY
) -> dict[str, dict[str, Any]]:
    """Model starts per seat over the last `window` seconds, read back from the rows.

    A start is a wake-ledger `started` row that carries `gate`; a row without the field (every row
    from before the gate) is ignored, and so is a row with no `state`.  A routine forward (an `http`
    wake, or the code `det_routine_forward`) is a delivery: it is counted under `forwards`, never under
    `starts`.  A fallback and a chat inspection are counted apart, from
    the `gate_fallback` and `gate_inspect` fields of the wake's closing row and in the window of THAT
    row's time, so a wake is one start however many runs it took.  Per seat: `starts`, `avoidable`
    (a `det_*` rule applied and the model started anyway), `codes` (a count per gate code), `fallbacks`,
    `inspects`, `forwards` and `forwards_avoidable`."""
    out: dict[str, dict[str, Any]] = {}
    now_f, window_f = _gate_number(now), _gate_number(window)
    if now_f is None or window_f is None:
        return out

    def entry(seat: str) -> dict[str, Any]:
        return out.setdefault(
            seat,
            {
                "starts": 0,
                "avoidable": 0,
                "codes": {},
                "fallbacks": 0,
                "inspects": 0,
                "forwards": 0,
                "forwards_avoidable": 0,
            },
        )

    for row in rows:
        if not isinstance(row, Mapping):
            continue
        state = row.get("state")
        if state is None:
            continue
        seat, at = row.get("seat"), _gate_number(row.get("ts"))
        code = _gate_token(row.get("gate")) if state == "started" else None
        forward = code is not None and (code in GATE_DELIVERY_CODES or row.get("adapter") == "http")
        if not isinstance(seat, str) or not seat or at is None or not 0 <= now_f - at < window_f:
            continue
        if code is not None:
            counts = entry(seat)
            if forward:
                counts["forwards"] += 1
                counts["forwards_avoidable"] += 1 if row.get("gate_avoidable") is True else 0
            else:
                counts["starts"] += 1
                counts["avoidable"] += 1 if row.get("gate_avoidable") is True else 0
                counts["codes"][code] = counts["codes"].get(code, 0) + 1
        if state in ("done", "failed"):
            if _gate_token(row.get("gate_fallback")):
                entry(seat)["fallbacks"] += 1
            if _gate_token(row.get("gate_inspect")):
                entry(seat)["inspects"] += 1
    return out


def gate_line(counts: Mapping[str, Any] | None, hours: int = 24) -> str:
    """One status line from one seat's `gate_counts` entry: `model starts 24h 5 (avoidable 2)`, with
    the fallbacks, inspections and routine forwards added only when there were any."""
    counts = counts or {}
    line = "model starts %dh %d (avoidable %d)" % (
        hours,
        int(counts.get("starts") or 0),
        int(counts.get("avoidable") or 0),
    )
    extra = [
        "%s %d" % (label, int(counts.get(key) or 0))
        for key, label in (
            ("fallbacks", "fallbacks"),
            ("inspects", "inspections"),
            ("forwards", "routine forwards"),
        )
        if counts.get(key)
    ]
    return line + (", " + ", ".join(extra) if extra else "")


# --------------------------------------------------------------------------------------------
# The router's receipt (AFC-824)
# --------------------------------------------------------------------------------------------
#
# A wake the one wake router answered 2xx is only stored there:  the router picks a chat and
# delivers later, retrying a busy or unreachable chat for up to a day, and its receipt
# (GET /receipt/<SEAT>/<wake_id>) holds the outcome.  The daemon reads that receipt on a schedule
# (Daemon.receipt_pass) and appends one outcome row when it settles.  Everything here is pure.

#: The router's terminal receipt statuses (wake_router.Router._finish) and the ledger state each
#: one becomes.  `accepted` (still pending there) and any word not listed are not terminal:  the
#: receipt is read again later.  `refused` is not a status the router writes today;  it is listed
#: so that a router that starts writing it is read as undelivered rather than as pending.
#: `delivered` is the router's word for a hand-over with no proof too (wake_router.Router._settle):
#: its receipt's `delivered` is true only with proof and null without, and the outcome row keeps
#: that field, so the two stay apart in the ledger.
RECEIPT_TERMINAL = {
    "delivered": DELIVERED,
    "dead": "undelivered",
    "held": "undelivered",
    "refused": "undelivered",
    "unknown": "unknown",
}
#: When the receipt is read:  30 seconds after the 2xx, then 2 minutes after that, then 10 minutes
#: after that, then every 30 minutes, until a terminal receipt or the deadline.  The router retries
#: a busy or unreachable delivery for up to 24 hours (its max_age_seconds), so the deadline is two
#: hours past that;  a wake with no terminal receipt by then is recorded `unknown`, never delivered.
RECEIPT_BACKOFF = (30.0, 120.0, 600.0)
RECEIPT_EVERY = 1800.0
RECEIPT_DEADLINE = 26 * HOUR
#: How long the router keeps a final receipt (wake_router.DONE_KEEP_SECONDS):  a `done` wake from
#: before the listener read receipts is still read once while the router can answer it
#: (awaiting_receipts with `backfill`).
RECEIPT_BACKFILL = 30 * DAY

_RECEIPT_CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
_RECEIPT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}")
_RECEIPT_SESSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_RECEIPT_LETTER_RE = re.compile(r"[A-Z]{1,8}")
#: A word the daemon itself writes as `receipt_last`:  what the last read answered when a wake
#: reaches the deadline with no terminal receipt (`accepted`, `http_401`, `timeout`, ...).
RECEIPT_LAST_RE = re.compile(r"[a-z0-9_]{1,32}")


def _receipt_word(shape: re.Pattern[str]) -> Callable[[Any], bool]:
    return lambda value: isinstance(value, str) and shape.fullmatch(value) is not None


def _receipt_int(limit: int) -> Callable[[Any], bool]:
    return lambda value: type(value) is int and 0 <= value <= limit


def _receipt_status(value: Any) -> bool:
    return isinstance(value, str) and value in RECEIPT_TERMINAL


#: The receipt fields an outcome row may carry, as (the receipt's name, the row's name, the shape
#: the value must have).  Nothing else in a receipt is ever copied:  `topic`, `channel`,
#: `zulip_link`, `thread_link`, `link_note`, `note`, `candidates` and the rest are free text or
#: lists the router took from Zulip or a chat, and a receipt is read as untrusted data.
RECEIPT_FIELDS: tuple[tuple[str, str, Callable[[Any], bool]], ...] = (
    ("status", "receipt_status", _receipt_status),
    ("code", "receipt_code", _receipt_word(_RECEIPT_CODE_RE)),
    ("receipt_id", "receipt_id", _receipt_word(_RECEIPT_ID_RE)),
    ("picked_letter", "picked_letter", _receipt_word(_RECEIPT_LETTER_RE)),
    ("picked_generation", "picked_generation", _receipt_int(10**9)),
    ("host", "host", _receipt_word(_RECEIPT_CODE_RE)),
    ("route", "route", _receipt_word(_RECEIPT_CODE_RE)),
    ("ack", "ack", _receipt_word(_RECEIPT_CODE_RE)),
    ("attempts", "attempts", _receipt_int(10**6)),
    # True, false or null:  the router writes null when nobody knows whether the chat ran it.
    ("delivered", "delivered", lambda value: value is None or isinstance(value, bool)),
    ("reason", "reason", _receipt_word(_RECEIPT_CODE_RE)),
    ("session_id", "session_id", _receipt_word(_RECEIPT_SESSION_RE)),
    ("at", "receipt_at", lambda value: _gate_number(value) is not None),
)


def _receipt_value(
    source: Mapping[str, Any], name: str, check: Callable[[Any], bool], known: Iterable[str]
) -> tuple[bool, Any]:
    """(kept, value) for one field:  present, of its shape, and not secret-shaped."""
    if name not in source:
        return False, None
    value = source[name]
    if not check(value):
        return False, None
    if isinstance(value, str) and secretscan.scan(value, known):
        return False, None
    return True, value


def receipt_row_fields(receipt: Mapping[str, Any], known: Iterable[str] = ()) -> dict[str, Any]:
    """The fields of a router receipt an outcome row carries, under the row's names
    (RECEIPT_FIELDS):  each one present, of its shape and not secret-shaped (`known` are values the
    caller holds as secrets), and every other field dropped.  An empty string (the router's "none")
    is dropped."""
    known = list(known)
    out: dict[str, Any] = {}
    for name, row_name, check in RECEIPT_FIELDS:
        kept, value = _receipt_value(receipt, name, check, known)
        if kept:
            out[row_name] = value
    return out


def outcome_fields(view: Mapping[str, Any]) -> dict[str, Any]:
    """The receipt fields of a merged ledger view's own outcome row (`outcome_row`, Ledger.wakes),
    read back under the same shapes, plus the daemon's own `receipt_last`:  what listener_records
    shows for an outcome.  Only that row is read, never the merged fields:  a field the receipt
    did not have, such as `attempts`, is then absent, and not the `done` row's count of POSTs
    shown as the router's.  An outcome the deadline wrote holds `receipt_last` alone."""
    row = view.get("outcome_row")
    if not isinstance(row, Mapping):
        return {}
    out: dict[str, Any] = {}
    for _name, row_name, check in RECEIPT_FIELDS:
        kept, value = _receipt_value(row, row_name, check, ())
        if kept:
            out[row_name] = value
    last = row.get("receipt_last")
    if isinstance(last, str) and RECEIPT_LAST_RE.fullmatch(last):
        out["receipt_last"] = last
    return out


def receipt_outcome(
    status: int | None,
    body: Any,
    *,
    seat: str,
    wake_id: str,
    known: Iterable[str] = (),
) -> tuple[str, dict[str, Any]] | None:
    """(the outcome state, the row's receipt fields) when the router answered this wake's receipt
    with a terminal status, else None (read it again later).  Only a 200 whose JSON object names
    this wake and this seat and is not a test receipt counts."""
    if status != 200 or not isinstance(body, Mapping) or body.get("test") is True:
        return None
    if body.get("wake_id") != wake_id or body.get("seat") != seat:
        return None
    word = body.get("status")
    state = RECEIPT_TERMINAL.get(word) if isinstance(word, str) else None
    if state is None:
        return None
    return state, receipt_row_fields(body, known)


def receipt_read_word(status: int | None, body: Any, error: str | None) -> str:
    """What one read that settled nothing answered, as a RECEIPT_LAST_RE word from a fixed set:
    `accepted` (the router still holds the wake), `other` (a 200 with any other status that is not
    terminal), `mismatched` (a terminal receipt that names another wake or seat, or a test one),
    `malformed` (a 200 that is no receipt), `http_<code>`, `no_answer`, or the read's own error
    word (A.ReceiptRead).  Nothing the router wrote is copied:  the word reaches the ledger, the
    log and the read API."""
    if error:
        word = error
    elif status == 200:
        found = body.get("status") if isinstance(body, Mapping) else None
        if not isinstance(found, str):
            word = "malformed"
        elif found in RECEIPT_TERMINAL:
            word = "mismatched"
        elif found == "accepted":
            word = "accepted"
        else:
            word = "other"
    elif status is None:
        word = "no_answer"
    else:
        word = "http_%d" % status
    return word if RECEIPT_LAST_RE.fullmatch(word) else "malformed"


def receipt_next_read(done_at: float, read_at: float | None) -> float:
    """When the receipt of a wake answered 2xx at `done_at` is next due, after a read at `read_at`
    (None before the first):  the first slot of the schedule (RECEIPT_BACKOFF, then every
    RECEIPT_EVERY) past the last read, and never later than the deadline.  It is a function of the
    two times alone, so a restarted daemon that knows only the 2xx time picks the schedule up.
    A read at or past the deadline that still settled nothing (its outcome row could not be
    written) is followed by one every RECEIPT_EVERY, so a ledger that cannot be written is not
    read again on every pass."""
    if read_at is not None and read_at >= done_at + RECEIPT_DEADLINE:
        return read_at + RECEIPT_EVERY
    after = -math.inf if read_at is None else read_at - done_at
    at = 0.0
    for gap in RECEIPT_BACKOFF:
        at += gap
        if at > after:
            return done_at + min(at, RECEIPT_DEADLINE)
    steps = math.floor((after - at) / RECEIPT_EVERY) + 1
    return done_at + min(at + steps * RECEIPT_EVERY, RECEIPT_DEADLINE)


def awaiting_receipts(
    views: Mapping[str, Mapping[str, Any]], *, backfill: bool = False, now: float | None = None
) -> dict[str, float]:
    """wake id -> the time of its `done` row, for every wake still waiting on the router's receipt:
    its `done` row carries `receipt_awaited` and no outcome row followed.  This is how a restart
    picks the reads up again:  the ledger is their only record.

    With `backfill` (the caller's seat routes to the router by its config now), a `done` wake from
    before the listener read receipts counts too, while the router still keeps its receipt (done
    less than RECEIPT_BACKFILL before `now`):  an `http` wake the routine answered 202, the
    router's answer to a wake it stored, with no answer of the hosted Worker on its row
    (`routine_delivered` and `routine_outcome`, adapters.routine_answer).  One that is past the
    deadline gets one last read (Daemon.receipt_pass), so each is read at most once more.  That
    202 is a guess, not a record:  a Worker's (or another routine's) 202 whose body was not the
    Worker's answer reads the same, so the daemon settles such a wake only on a terminal receipt
    that names it, which the router writes only for a wake it stored (Daemon._receipt_final)."""
    out: dict[str, float] = {}
    for wake_id, view in views.items():
        if view.get("state") != "done":
            continue
        at = _gate_number((view.get("times") or {}).get("done"))
        if at is None:
            continue
        if view.get("receipt_awaited") is True or (
            backfill and now is not None and now - at < RECEIPT_BACKFILL and _router_stored(view)
        ):
            out[wake_id] = at
    return out


def _router_stored(view: Mapping[str, Any]) -> bool:
    """Whether a `done` view is an `http` wake answered the way the router answers a wake it
    stored (wake_router.Router.accept:  202), and not by the hosted Worker with its answer.  It
    is a guess:  any routine's 202 that left no Worker answer on the row looks the same."""
    status = view.get("http_status")
    return (
        view.get("adapter") == "http"
        and type(status) is int
        and status == 202
        and "routine_delivered" not in view
        and "routine_outcome" not in view
    )


def outcome_counts(views: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """How many of these merged views are at each router outcome, plus `awaiting`:  a `done` wake
    still waiting on the router's receipt."""
    counts = {state: 0 for state in OUTCOME_STATES}
    counts["awaiting"] = 0
    for view in views:
        state = view.get("state")
        if state in OUTCOME_STATES:
            counts[state] += 1
        elif state == "done" and view.get("receipt_awaited") is True:
            counts["awaiting"] += 1
    return counts


# --------------------------------------------------------------------------------------------
# Route suggestion caps (no caller since the retired wake types went;  a follow-up removes them)
# --------------------------------------------------------------------------------------------


def _route_time(view: Mapping[str, Any]) -> float | None:
    """When a wake posted its route (the `done` row), or None when it posted none."""
    route = view.get("route")
    if not isinstance(route, dict) or route.get("status") != "posted":
        return None
    at = (view.get("times") or {}).get("done")
    return float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else None


def routes_in(views: Iterable[Mapping[str, Any]], now: float, window: float = DAY) -> int:
    """Routes posted in the last `window` seconds."""
    return sum(1 for v in views if (t := _route_time(v)) is not None and 0 <= now - t < window)


def route_budget_block(
    views: Mapping[str, Mapping[str, Any]], budget: Mapping[str, float], *, key: str, now: float
) -> str | None:
    """None when one more route fits the routing seat's caps, else the cap it hits:
    routes_per_hour, routes_per_day, or route_topic_minutes (the time since the last route in this
    topic).  Counted from the routing seat's own ledger, owner triggers included:  every route
    needs one.  A cap the budget does not carry is no limit (they have no default, like the wake
    count caps).  `0` is reached at once for routes_per_hour and routes_per_day, and means no
    spacing for route_topic_minutes."""
    posted = [
        (t, v) for v in views.values() if (t := _route_time(v)) is not None and 0 <= now - t < DAY
    ]
    if at_cap(sum(1 for t, _ in posted if now - t < HOUR), budget, "routes_per_hour"):
        return "routes_per_hour"
    if at_cap(len(posted), budget, "routes_per_day"):
        return "routes_per_day"
    spacing = float(budget.get("route_topic_minutes", 0)) * 60
    if any(v.get("topic_key") == key and now - t < spacing for t, v in posted):
        return "route_topic_minutes"
    return None


# --------------------------------------------------------------------------------------------
# Mentions in the Operator's own text
# --------------------------------------------------------------------------------------------


_MENTION_RE = re.compile(r"@(?!_)\*\*")
_GROUP_RE = re.compile(r"@_?\*(?!\*)[^*\n]{1,100}\*")
_LOUD_RE = re.compile(r"@(?!_)\*")
_LOUD_SINGLE_STAR_RE = re.compile(r"@(?!_)\*(?!\*)")


def neutralize_mentions(text: str) -> str:
    """@**X** becomes @_**X** (a silent reference) and group mentions are removed, so a wake
    reply notifies no one.  Both rewrites repeat until the text stops changing, because removing
    a group mention can join an `@` to a following `**name**` (`@@*x***Bob**`).  Any `@*` left
    after that (an unclosed or over-long group name) is made silent too."""
    while True:
        changed = _MENTION_RE.sub("@_**", _GROUP_RE.sub("", text))
        if changed == text:
            break
        text = changed
    return _LOUD_RE.sub("@_*", text)


def sanitize_compose_mentions(text: str) -> str:
    """Remove group mentions and silence stray @* tokens; every @** mention stays real.

    A typed @**Name** notifies and can wake that seat, and a typed wildcard (@**all** and the
    like) stays real too (owner, Nov 10 2026, AFC-689)."""
    while True:
        changed = _GROUP_RE.sub("", text)
        if changed == text:
            break
        text = changed
    return _LOUD_SINGLE_STAR_RE.sub("@_*", text)


def loud_mention(text: str) -> bool:
    """True when text still holds an @-mention that would notify someone."""
    return bool(_LOUD_RE.search(text))


#: What the Operator's own notes call themselves in their first-line tag (owner ruling, Oct 30 2026:
#: "I want it to be the operator's", not the seat's or the runtime's).  The seat the note is about is in
#: the ledger and in the body of the note, not in the tag.
OPERATOR_TAG = "OPERATOR"
