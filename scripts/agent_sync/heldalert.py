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

  - ONE write per window for the whole instance, not one per seat.  Ten seats holding at once are one
    message naming ten seats, so the volume is bounded by the window rather than by the fleet size.
  - Nothing held means no post at all.  An idle instance stays silent, which is what makes the
    signal worth reading when it does fire.
  - Each line names the seat, the hold reasons with their counts, and what is in overflow or lost,
    so the post says which budget to raise rather than blaming a seat for being busy.

ONE standing message, edited in place (owner, Oct 29 2026, AFC-566 and AFC-569).  A new message every
window put 135 posts in the topic, so the digest now keeps one message and rewrites it each window:
the first hold posts it, every later window edits it, and when every hold has cleared it is edited
once more to say so, so the topic never shows a stale list.  An all-clear is only ever an EDIT:  a
fresh post saying nothing is held would be the noise the rule above forbids.

Zulip lets only the bot that sent a message edit it, and the digest is carried by whichever seat can
post, so the standing message is recorded as a `(seat, message_id)` pair and that seat's bot edits
it.  When that seat cannot (it is not running here, its key is refused, or Zulip refuses the edit
because the message is gone), the digest falls back to a fresh post by the next usable seat and that
post becomes the standing one.  An edit that fails without a refusal (a timeout, a gateway error) is
ambiguous rather than refused, so the pair is kept and the edit retried next window:  turning it into
a fresh post would leave two copies of the list.  This realm has no content edit time limit, so a
standing message never ages out of being editable.

The state is the timestamp (`last_post`) and that pair (`standing`), nothing else:  what to report is
read live from each seat's `held-back.json` at post time, so a restart mid-window cannot drop a hold
and there is no copy of the per-seat state to keep in step.  The rate limit is the timestamp.

A failed post or edit is an ATTEMPT, and an attempt consumes the window, so a key that stays broken
costs one attempt per window rather than one per held tick (120 an hour), and the digest is retried
next window with the hold untouched either way.  The one case that does NOT consume the window is
having no seat able to post at all:  nothing was sent, so waiting is free and the digest goes out as
soon as a usable key returns.

One thing this deliberately does NOT do:  it does not mark the entries `told` (heldback.note_digest).
`told_seq` is what lets a record expire, and expiry deletes a hold the owner may not have read yet.
A Mac banner is the owner demonstrably seeing it;  a `#alerts` post the owner has not opened is not
the same promise, so server records keep AFC-253's no-TTL behaviour until the owner says otherwise.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

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
    """Record that a digest was ATTEMPTED, sent or not, which starts the next window.  One locked
    write.  A failure counts as an attempt deliberately:  otherwise the held tick retries every 30
    seconds for as long as a key stays broken, which is a flood rather than an alert."""

    def mutate(data: dict[str, Any]) -> None:
        data["last_post"] = float(now)

    L.update_json(path_for(root), mutate)


def standing(root: str) -> dict[str, Any] | None:
    """The standing message as `{seat, message_id, clear}`, or None when there is none to edit.  A
    malformed record is treated as none, so the worst a damaged file costs is one fresh post."""
    value = load(root).get("standing")
    if not isinstance(value, dict):
        return None
    seat, message_id = value.get("seat"), value.get("message_id")
    if not isinstance(seat, str) or not seat:
        return None
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
        return None
    return {"seat": seat, "message_id": message_id, "clear": value.get("clear") is True}


def note_standing(root: str, seat: str, message_id: int, *, clear: bool) -> None:
    """Record which message is the standing one, whose bot can edit it, and whether it says all-clear."""

    def mutate(data: dict[str, Any]) -> None:
        data["standing"] = {"seat": seat, "message_id": int(message_id), "clear": bool(clear)}

    L.update_json(path_for(root), mutate)


def drop_standing(root: str) -> None:
    """Forget a standing message nobody can edit any more, so the next digest posts a fresh one."""

    def mutate(data: dict[str, Any]) -> None:
        data.pop("standing", None)

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


HEADLINE = "**Held-back wakes on the agent-sync server listener.**"


def updated_line(updated: str | None, *, clear: bool) -> list[str]:
    """The closing line that says when the standing message was last rewritten, so a reader can tell
    a live list from an old one, and that it is edited rather than reposted."""
    if not updated:
        return []
    when = "when a hold appears again" if clear else "each window while anything is held"
    return ["", "_Updated %s.  This message is edited in place %s._" % (updated, when)]


def compose(seats: Sequence[tuple[str, Mapping[str, Any]]], *, updated: str | None = None) -> str:
    """The whole post.  Empty for no holding seat, so the caller writes nothing rather than a fresh
    all-clear (an all-clear post a minute after the last alert teaches the eye to skip the channel);
    a standing message that is already there is rewritten with `compose_clear` instead.

    Two ASCII spaces between sentences:  this is fleet prose, the renderer eats a single one, and the
    client turns each doubled gap into U+00A0 plus a space on the way out."""
    if not seats:
        return ""
    one = len(seats) == 1
    head = (
        "%s  %d seat%s %s;  each one is in that seat's inbox and rides the next wake that can run."
        % (
            HEADLINE,
            len(seats),
            "" if one else "s",
            "has a wake the budget would not run" if one else "have wakes the budget would not run",
        )
    )
    tail = (
        "Nothing was dropped.  To clear a hold sooner, raise that seat's budget in `listener.toml`  "
        "(`[daemon]` `stale_after_minutes` is unrelated;  the limits are the seat's `budget` table),  "
        "and a catch-up wake goes out by itself once the budget has room.  Evictions are ids that "
        "left the file unsurfaced:  a seat never read them and never will."
    )
    return "\n".join(
        [head, ""]
        + [seat_line(seat, held) for seat, held in sorted(seats, key=lambda s: s[0])]
        + ["", tail]
        + updated_line(updated, clear=False)
    )


def compose_clear(*, updated: str | None = None) -> str:
    """The standing message once every hold has cleared:  it says so, so the topic never shows a list
    of holds that no longer exist.  Only ever written as an edit of the standing message."""
    return "\n".join(
        [
            "%s  Nothing is held back now:  every seat's holds have cleared, and no id was lost "
            "past the caps." % HEADLINE
        ]
        + updated_line(updated, clear=True)
    )


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
