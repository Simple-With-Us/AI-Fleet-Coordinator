"""One visible owner digest for the held-back wakes of every seat on an instance (AFC-257).

`heldback.py` keeps each seat's held-back records in `<SEAT>/held-back.json` and the daemon writes
each seat's owner digest to `<SEAT>/owner-queue.jsonl`.  On the Mac something reads that queue (a
banner), so a hold is visible;  on the server nothing read it, so a hold was durable and silent:  a
message sat unread in a seat's inbox with no wake, and an eviction was written to the same unread
place.  A notice nobody reads is not a control.  This module is the reader on the server side.

Owner decision (Jay, Oct 11 2026, 4:44pm):  the surface is a post in `#alerts` (the channel that
already carries watchdogs and CI failures;  no new channel), and the trigger is ANY hold-back, not
only an eviction.  The trigger is deliberately loud:  a seat one under its daily cap posts the same
way as one that has lost a message.

What keeps that loud trigger from being noise:

  - ONE post per window for the whole instance, not one per seat.  Ten seats holding at once are one
    post naming ten seats, so the volume is bounded by the window rather than by the fleet size.
  - Nothing held means no post at all.  An idle instance stays silent, which is what makes the
    signal worth reading when it does fire.
  - Each line names the seat, the hold reasons with their counts, and what is in overflow or lost,
    so the post says which budget to raise rather than blaming a seat for being busy.

The state is one timestamp (`last_post`) and nothing else:  what to report is read live from each
seat's `held-back.json` at post time, so a restart mid-window cannot drop a hold and there is no
copy of the per-seat state to keep in step.  The rate limit is the timestamp.

A post the owner never sees is still a notice nobody reads, so a failed post does not consume the
window:  it is retried at the next window, which bounds the retry to one an hour while keeping the
signal.

One thing this deliberately does NOT do:  it does not mark the entries `told` (heldback.note_digest).
`told_seq` is what lets a record expire, and expiry deletes a hold the owner may not have read yet.
A Mac banner is the owner demonstrably seeing it;  a `#alerts` post the owner has not opened is not
the same promise, so server records keep AFC-253's no-TTL behaviour until the owner says otherwise.

Python 3.11+, standard library only.
"""
from __future__ import annotations

import os
from typing import Any, Iterable, Mapping, Sequence

from . import heldback as HELD
from . import live as L

FILE_NAME = "held-alert.json"
# The channel that already carries watchdogs and CI failures (owner, Oct 11 2026).  Never created by
# this module:  a channel that does not exist is a config error, not something to provision.
CHANNEL = "alerts"
TOPIC = "held-back wakes"


def path_for(root: str) -> str:
    return os.path.join(L.listener_dir(root), FILE_NAME)


def due(root: str, *, every: float, now: float) -> bool:
    """May a digest go now?  `every` <= 0 is off.  True at the first check and then at most once per
    `every` seconds, from `last_post` (absent = never posted, so the first hold is reported at once)."""
    if every <= 0:
        return False
    last = load(root).get("last_post")
    if isinstance(last, bool) or not isinstance(last, (int, float)):
        return True
    return now - float(last) >= every


def load(root: str) -> dict[str, Any]:
    return L.read_json(path_for(root))


def note_post(root: str, now: float) -> None:
    """Record that a digest went out (or was attempted and will be retried after the window), which
    starts the next window.  One locked write."""
    def mutate(data: dict[str, Any]) -> None:
        data["last_post"] = float(now)

    L.update_json(path_for(root), mutate)


def holding(held: Mapping[str, Any]) -> bool:
    """Is this seat's summary worth a line?  Held records and overflow are the holds;  `evicted` is
    ids already gone, still owed, so it stands on its own even when nothing is held any more."""
    return bool(_count(held)) or bool(_evicted(held))


def _count(held: Mapping[str, Any]) -> int:
    value = held.get("count")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _evicted(held: Mapping[str, Any]) -> int:
    value = held.get("evicted")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _overflow(held: Mapping[str, Any]) -> int:
    value = held.get("overflow")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _age(held: Mapping[str, Any]) -> int:
    value = held.get("oldest_age")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def age_text(seconds: int) -> str:
    """An age a human reads at a glance:  `45s`, `12m`, `3h`, `2d`."""
    seconds = max(0, int(seconds))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if seconds >= size:
            return "%d%s" % (seconds // size, unit)
    return "%ds" % seconds


def reason_text(held: Mapping[str, Any]) -> str:
    """The hold reasons with their counts, in a seat's budget names:  `wakes_per_day 4, overflow 1`.

    A reason that is not in `heldback.HOLD_REASONS` is still reported, by its own name, rather than
    dropped or replaced with a placeholder:  a record whose reason this build does not know is still
    a hold somebody owes an answer for, and hiding it would repeat the bug this module exists to
    close."""
    reasons = held.get("reasons")
    parts = []
    for reason, count in sorted((reasons or {}).items()):
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            continue
        parts.append("%s %d" % (str(reason), count))
    return ", ".join(parts) if parts else "unspecified"


def seat_line(seat: str, held: Mapping[str, Any]) -> str:
    """One seat's line.  Names the seat, the count, the reasons and the two states that need a
    decision (overflow, and ids lost past the caps) with the oldest hold's age, so the line says what
    to raise and not only that something happened.

    A seat whose only claim on a post is evicted ids says so plainly, with no "0 held" and no age:
    reporting an empty hold as if it were a hold would be the kind of noise that gets the whole
    channel ignored."""
    count, evicted, overflow = _count(held), _evicted(held), _overflow(held)
    if not count:
        return "**%s**: **%d lost past the caps**" % (seat, evicted)
    bits = ["**%s**: %d held (%s)" % (seat, count, reason_text(held))]
    if overflow:
        bits.append("%d in overflow" % overflow)
    if evicted:
        # Lost ids are gone from the file and nothing can surface them, so they are named apart from
        # the holds:  they are a message the seat never saw and never will.
        bits.append("**%d lost past the caps**" % evicted)
    bits.append("oldest %s" % age_text(_age(held)))
    return ", ".join(bits)


def compose(seats: Sequence[tuple[str, Mapping[str, Any]]]) -> str:
    """The whole post.  Empty for no holding seat, so the caller posts nothing at all rather than an
    all-clear (an all-clear a minute after the last alert teaches the eye to skip the channel).

    Two ASCII spaces between sentences:  this is fleet prose, and the renderer eats a single one."""
    if not seats:
        return ""
    one = len(seats) == 1
    head = "**Held-back wakes on the agent-sync server listener.**  %d seat%s %s a wake the budget would not run;  each one is in that seat's inbox and rides the next wake that can run." % (
        len(seats), "" if one else "s", "has" if one else "have")
    tail = ("Nothing was dropped.  To clear a hold sooner, raise that seat's budget in `listener.toml`  "
            "(`[daemon]` `stale_after_minutes` is unrelated;  the limits are the seat's `budget` table),  "
            "and a catch-up wake goes out by itself once the budget has room.  Evictions are ids that "
            "left the file unsurfaced:  a seat never read them and never will.")
    return "\n".join([head, ""] + [seat_line(seat, held) for seat, held in sorted(seats, key=lambda s: s[0])]
                     + ["", tail])


def collect(root: str, seats: Iterable[str], now: float) -> list[tuple[str, dict[str, Any]]]:
    """The seats worth reporting, read live as `(seat, summary)` for each holding seat.

    Read at post time rather than accumulated, so the post describes the holds that exist now and a
    restart cannot drop one.  A seat with nothing held is simply absent, which is also how the
    "post nothing when nothing is held" rule falls out."""
    out = []
    for seat in sorted(seats):
        held = HELD.summary(L.SeatPaths(root, seat), now)
        if holding(held):
            out.append((seat, held))
    return out