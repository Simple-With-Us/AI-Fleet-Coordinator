"""The owner's clock for the publisher's log and status file: Central, 12-hour, spaced AM/PM, never a zone name.

    stamp(utc)   "Sun, Oct 11, 7:00 AM"

Daylight saving comes from the system time zone database.  If that is missing, the United States rule is applied
by hand:  daylight time runs from the second Sunday of March (08:00 UTC) to the first Sunday of November (07:00 UTC).
(`open_loops.central` has the same rule for the digest;  this package cannot import it, because open_loops imports
this one.)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

__all__ = ["stamp"]

_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _nth_sunday(year: int, month: int, n: int) -> int:
    first = datetime(year, month, 1)
    return 1 + (6 - first.weekday()) % 7 + 7 * (n - 1)


def _local(utc: datetime, use_zoneinfo: bool) -> datetime:
    if use_zoneinfo:
        try:
            from zoneinfo import ZoneInfo

            return utc.astimezone(ZoneInfo("America/Chicago"))
        except Exception:  # no tz database
            pass
    start = datetime(utc.year, 3, _nth_sunday(utc.year, 3, 2), 8, tzinfo=UTC)
    end = datetime(utc.year, 11, _nth_sunday(utc.year, 11, 1), 7, tzinfo=UTC)
    return utc.astimezone(timezone(timedelta(hours=-5 if start <= utc < end else -6)))


def stamp(value: datetime, *, use_zoneinfo: bool = True) -> str:
    if value.tzinfo is None:
        raise ValueError("a time with no zone could be any hour")
    local = _local(value.astimezone(UTC), use_zoneinfo)
    hour = local.hour % 12 or 12
    return "%s, %s %d, %d:%02d%s" % (
        _DAYS[local.weekday()],
        _MONTHS[local.month - 1],
        local.day,
        hour,
        local.minute,
        " AM" if local.hour < 12 else " PM",
    )
