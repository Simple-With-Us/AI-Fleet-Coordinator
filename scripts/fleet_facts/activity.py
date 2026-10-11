"""The single definition of a claim's activity and idle band.  Pure:  no state, no I/O, no clock.

C2-1 (the digest) and C4-1 (claim expiry) import this module;  there is no second definition anywhere.

A claim's last activity is the latest of these, and never earlier than the launch time of claim expiry:

  branch_push      a push to the claimed branch
  c1_snapshot      a C1 snapshot or auto-commit for it
  pr_update        an update to a same-repo PR from that branch by a fleet author
  linear_comments  any Linear comment on the issue, except the sweepers' own (a text starting
                   `[claim-expiry]` or `[sweep]`, after any `[SEAT]` tag line)
  zulip_topic      a post by the claiming seat in the issue's `<ID> <subject>` Zulip topic

Linear `updated_at` is NEVER an input, because the import and the relabel touched every issue.  Nothing here
reads it, and `test_activity` pins that with a fixture whose `updated_at` is fresh and whose signals are old.

`signals` maps a source name to what the caller found:

    {"ok": True,  "events": [{"at": "2026-10-10T14:00:00Z", ...}]}     # checked;  the list may be empty
    {"ok": False}                                                      # the source could not be read

  * `ok` must be exactly True.  Anything else (False, missing, a string) makes the whole result "unknown",
    never idle:  a source that could not be read is not evidence that nothing happened.
  * `times: [..]` is accepted as a shorthand for events that carry only `at`.
  * linear_comments events: `{"at", "author", "sweeper"}` (the gatherer sets `sweeper` with
    `is_sweeper_text`) or `{"at", "text"}`;  a sweeper comment is skipped, every other comment counts,
    whoever wrote it.
  * pr_update events: `{"at", "same_repo"}`;  an event whose `same_repo` is False is skipped.  (Outside-fork
    PRs never reach the facts at all.)
  * zulip_topic events: `{"at", "by"}`;  with `seat=` given, a post whose `by` names another seat is skipped.
  * A source named in `required` but absent from `signals` is "unknown" too.  A caller that releases claims
    passes `required=SOURCES`.  A source not passed and not required is simply not counted.
  * A time that cannot be parsed, or has no zone, makes its source "unknown".
  * `launch_at` (the claim-expiry launch time) is the floor.  `claimed_at` is the claim's own start and is
    a baseline too.  With no evidence at all (no signal, no floor, no claim time) the answer is "unknown".

`idle_band(item, now)` returns "none" (under 36 hours idle), "36h", "48h", or "unknown".  "unknown" is NOT a
band:  `BANDS` holds the three real ones, and `is_idle_band` is True only for "36h" and "48h".  A caller that
acts on a band (a ping at 36h, a release at 48h) must test for the band it acts on, never "not none".
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

__all__ = ["SOURCES", "BANDS", "UNKNOWN", "BAND_NONE", "BAND_36H", "BAND_48H", "SWEEPER_PREFIXES",
           "parse_time", "is_sweeper_text", "empty_signals", "explain", "activity_at", "idle_hours",
           "idle_band", "is_idle_band"]

UNKNOWN = "unknown"
BAND_NONE, BAND_36H, BAND_48H = "none", "36h", "48h"
BANDS = (BAND_NONE, BAND_36H, BAND_48H)
IDLE_36_HOURS = 36.0
IDLE_48_HOURS = 48.0
SOURCES = ("branch_push", "c1_snapshot", "pr_update", "linear_comments", "zulip_topic")
SWEEPER_PREFIXES = ("[claim-expiry]", "[sweep]")

# A seat tag line as the board writes it:  [CLAUDE] or [CLAUDE·6db9ee77].  Upper case only, so the
# sweepers' own lower-case prefixes are never mistaken for one.
_SEAT_TAG = re.compile(r"^\[[A-Z][A-Z0-9-]*(?:·[0-9A-Za-z-]+)?\]\s*")


class _BadTime(ValueError):
    pass


def parse_time(value: Any) -> datetime | None:
    """A timezone-aware UTC datetime, or None.  Accepts an aware datetime, an ISO 8601 string (a trailing Z
    is UTC) and epoch seconds.  A time with no zone is refused:  it could be any hour."""
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, bool):
        return None
    elif isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text[-1] in "Zz":
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return None
    return dt.astimezone(timezone.utc)


def is_sweeper_text(text: Any) -> bool:
    """True when a Linear comment is one of the sweepers' own:  its text (after one `[SEAT]` tag line, and
    leading space) starts with `[claim-expiry]` or `[sweep]`, in any letter case."""
    if not isinstance(text, str):
        return False
    rest = text.lstrip()
    for _ in range(2):
        if rest.lower().startswith(SWEEPER_PREFIXES):
            return True
        match = _SEAT_TAG.match(rest)
        if not match:
            return False
        rest = rest[match.end():].lstrip()
    return False


def empty_signals(ok: bool = True) -> dict[str, dict]:
    """All five sources, checked and empty (or, with ok=False, all unreadable)."""
    return {name: ({"ok": True, "events": []} if ok else {"ok": False}) for name in SOURCES}


def _event_times(name: str, source: Mapping, seat: str | None) -> list[datetime]:
    events: list[Any] = []
    raw_events = source.get("events", [])
    raw_times = source.get("times", [])
    if not isinstance(raw_events, (list, tuple)) or not isinstance(raw_times, (list, tuple)):
        raise _BadTime("events and times must be lists")
    events.extend(raw_events)
    events.extend({"at": t} for t in raw_times)
    counted: list[datetime] = []
    for event in events:
        if not isinstance(event, Mapping):
            raise _BadTime("an event is not a mapping")
        if name == "linear_comments":
            if event.get("sweeper") is True or is_sweeper_text(event.get("text")):
                continue
        elif name == "pr_update":
            if event.get("same_repo") is False:
                continue
        elif name == "zulip_topic":
            by = event.get("by")
            if seat and isinstance(by, str) and by.strip() and by.strip().upper() != seat.strip().upper():
                continue
        when = parse_time(event.get("at"))
        if when is None:
            raise _BadTime("an event time is missing or has no zone")
        counted.append(when)
    return counted


def explain(signals: Mapping[str, Any] | None, launch_at: Any = None, claimed_at: Any = None, *,
            seat: str | None = None, required: Iterable[str] = ()) -> dict:
    """Everything `activity_at` decides:  {"at": datetime | "unknown", "basis": source or "launch_at" or
    "claimed_at" or None, "unknown_sources": [...]}.  `basis` names the evidence that won."""
    sources = signals if isinstance(signals, Mapping) else {}
    if isinstance(required, str):
        required = (required,)
    bad: list[str] = []
    best: tuple[datetime, str] | None = None

    def consider(when: datetime, basis: str) -> None:
        nonlocal best
        if best is None or when > best[0]:
            best = (when, basis)

    for name in required:
        if name not in sources:
            bad.append(name)
    for name in SOURCES:
        if name not in sources:
            continue
        source = sources[name]
        if not isinstance(source, Mapping) or source.get("ok") is not True:
            bad.append(name)
            continue
        try:
            for when in _event_times(name, source, seat):
                consider(when, name)
        except _BadTime:
            bad.append(name)
    floor = None
    if launch_at is not None:
        floor = parse_time(launch_at)
        if floor is None:
            bad.append("launch_at")
        else:
            consider(floor, "launch_at")
    if claimed_at is not None:
        started = parse_time(claimed_at)
        if started is None:
            bad.append("claimed_at")
        else:
            consider(started, "claimed_at")
    unknown = sorted(set(bad))
    if unknown:
        return {"at": UNKNOWN, "basis": None, "unknown_sources": unknown}
    if best is None:
        return {"at": UNKNOWN, "basis": None, "unknown_sources": ["no_evidence"]}
    return {"at": best[0], "basis": best[1], "unknown_sources": []}


def activity_at(signals: Mapping[str, Any] | None, launch_at: Any = None, claimed_at: Any = None, *,
                seat: str | None = None, required: Iterable[str] = ()) -> datetime | str:
    """max(launch_at, claimed_at, the latest real signal) as an aware UTC datetime, or "unknown"."""
    return explain(signals, launch_at, claimed_at, seat=seat, required=required)["at"]


def idle_hours(item: Mapping[str, Any], now: Any) -> float | str:
    """Hours since the item's last activity (never negative), or "unknown".  `item` keys:  signals,
    launch_at, claimed_at, seat, required.  Any other key (updated_at above all) is ignored."""
    at = activity_at(item.get("signals"), item.get("launch_at"), item.get("claimed_at"),
                     seat=item.get("seat"), required=item.get("required") or ())
    if at == UNKNOWN:
        return UNKNOWN
    current = parse_time(now)
    if current is None:
        return UNKNOWN
    return max(0.0, (current - at).total_seconds() / 3600.0)


def idle_band(item: Mapping[str, Any], now: Any) -> str:
    """"none", "36h", "48h", or "unknown" (see the module docstring)."""
    hours = idle_hours(item, now)
    if hours == UNKNOWN:
        return UNKNOWN
    if hours >= IDLE_48_HOURS:
        return BAND_48H
    if hours >= IDLE_36_HOURS:
        return BAND_36H
    return BAND_NONE


def is_idle_band(band: Any) -> bool:
    return band in (BAND_36H, BAND_48H)
