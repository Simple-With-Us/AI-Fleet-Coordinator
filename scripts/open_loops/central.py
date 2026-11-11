"""Central time, the owner's clock:  12-hour, am or pm in lower case, and never a zone name.

    stamp(utc)   "Sun, Oct 11, 7:00am"
    clock(utc)   "7:00am"
    id_stamp(utc)   ("20261011", "0700")

Daylight saving comes from the system time zone database (zoneinfo).  If that database is missing (a
minimal container), the United States rule is applied by hand:  daylight time runs from the second Sunday
of March at 2:00am standard time to the first Sunday of November at 2:00am daylight time, so the change
instants are 08:00 UTC and 07:00 UTC.  A test compares the two across the whole of 2026.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

__all__ = ["CENTRAL", "central", "stamp", "clock", "id_stamp", "age_text"]

CENTRAL = "America/Chicago"
_DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _nth_sunday(year: int, month: int, n: int) -> int:
    first = datetime(year, month, 1)
    return 1 + (6 - first.weekday()) % 7 + 7 * (n - 1)


def _manual_offset_hours(utc: datetime) -> int:
    year = utc.year
    start = datetime(year, 3, _nth_sunday(year, 3, 2), 8, tzinfo=timezone.utc)
    end = datetime(year, 11, _nth_sunday(year, 11, 1), 7, tzinfo=timezone.utc)
    return -5 if start <= utc < end else -6


def central(value: datetime, *, use_zoneinfo: bool = True) -> datetime:
    """`value` (an aware datetime) as Central wall-clock time, in a datetime that carries that time's offset."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("a time with no zone could be any hour")
    utc = value.astimezone(timezone.utc)
    if use_zoneinfo:
        try:
            from zoneinfo import ZoneInfo
            return utc.astimezone(ZoneInfo(CENTRAL))
        except Exception:  # no tz database
            pass
    return utc.astimezone(timezone(timedelta(hours=_manual_offset_hours(utc))))


def clock(value: datetime, *, use_zoneinfo: bool = True) -> str:
    local = central(value, use_zoneinfo=use_zoneinfo)
    hour = local.hour % 12 or 12
    return "%d:%02d%s" % (hour, local.minute, "am" if local.hour < 12 else "pm")


def stamp(value: datetime, *, use_zoneinfo: bool = True) -> str:
    local = central(value, use_zoneinfo=use_zoneinfo)
    return "%s, %s %d, %s" % (_DAYS[local.weekday()], _MONTHS[local.month - 1], local.day, clock(value, use_zoneinfo=use_zoneinfo))


def id_stamp(value: datetime, *, use_zoneinfo: bool = True) -> tuple[str, str]:
    local = central(value, use_zoneinfo=use_zoneinfo)
    return local.strftime("%Y%m%d"), local.strftime("%H%M")


def age_text(hours: float | int | None) -> str:
    """"under an hour", "5 hours", "3 days" (a day is 24 hours;  from 48 hours on it is days)."""
    if hours is None:
        return "age unknown"
    if hours < 1:
        return "under an hour"
    if hours < 48:
        n = int(hours)
        return "%d hour%s" % (n, "" if n == 1 else "s")
    n = int(hours // 24)
    return "%d days" % n
