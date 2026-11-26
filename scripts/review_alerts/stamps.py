"""Exact instant comparison for strict RFC3339 canonical stamps.

The canonical contract (comment_evidence._valid_stamp) accepts 1..9 fraction
digits;  datetime/float epoch comparison collapses anything past
microseconds, so a real sub-microsecond edit would compare equal and vanish.
stamp_key returns a totally ordered (epoch_seconds, fraction) tuple computed
with integer and string operations only, or None for a stamp the canonical
rule rejects.  Validation itself stays with comment_evidence._valid_stamp:
this module adds ordering, never a second definition of validity.
"""

from __future__ import annotations

import calendar
import re

from . import comment_evidence

_STAMP_SPLIT = re.compile(
    r"^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]{1,9}))?(Z|[+-][0-9]{2}:[0-9]{2})$"
)


def stamp_key(value) -> tuple | None:
    """(epoch_seconds_utc:int, fraction str right-padded to 9) or None.

    Two keys compare equal exactly when the instants are equal, at the full
    precision the canonical contract supports;  ordering is lexicographic.
    """
    if not comment_evidence._valid_stamp(value):
        return None
    found = _STAMP_SPLIT.fullmatch(value)
    # _valid_stamp already proved the shape and the calendar/time/offset
    # bounds;  a non-match here is unreachable.
    year, month, day, hour, minute, second = (int(found.group(i)) for i in range(1, 7))
    fraction = found.group(7) or ""
    offset = found.group(8)
    epoch = calendar.timegm((year, month, day, hour, minute, second, 0, 0, 0))
    if offset != "Z":
        sign = 1 if offset[0] == "+" else -1
        epoch -= sign * (int(offset[1:3]) * 3600 + int(offset[4:6]) * 60)
    return (epoch, fraction.ljust(9, "0"))
