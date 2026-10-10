"""Time formatting: America/Chicago, 12-hour, lowercase am/pm, never a zone abbreviation."""
from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from agent_sync import cli


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> float:
    return dt.datetime(year, month, day, hour, minute, tzinfo=dt.timezone.utc).timestamp()


CASES = (
    # (utc timestamp, expected)
    (utc(2026, 10, 7, 23, 41), "Wed, Oct 7, 6:41pm"),      # daylight time, UTC-5
    (utc(2026, 10, 7, 17, 0), "Wed, Oct 7, 12:00pm"),      # noon
    (utc(2026, 10, 7, 5, 0), "Wed, Oct 7, 12:00am"),       # midnight
    (utc(2026, 10, 7, 5, 9), "Wed, Oct 7, 12:09am"),
    (utc(2026, 1, 15, 3, 5), "Wed, Jan 14, 9:05pm"),       # winter, UTC-6, previous local day
    (utc(2026, 3, 8, 7, 59), "Sun, Mar 8, 1:59am"),        # just before the spring change (2am local)
    (utc(2026, 3, 8, 8, 0), "Sun, Mar 8, 3:00am"),         # just after
    (utc(2026, 11, 1, 6, 59), "Sun, Nov 1, 1:59am"),       # daylight, before the fall change
    (utc(2026, 11, 1, 7, 0), "Sun, Nov 1, 1:00am"),        # the repeated hour, now standard time
    (utc(2026, 12, 31, 23, 59), "Thu, Dec 31, 5:59pm"),
)


class TimeFormatTests(unittest.TestCase):
    def test_known_instants(self) -> None:
        for stamp, expected in CASES:
            with self.subTest(expected=expected):
                self.assertEqual(cli.format_time(stamp), expected)

    def test_no_zone_abbreviation_and_lowercase_suffix(self) -> None:
        for stamp, _ in CASES:
            text = cli.format_time(stamp)
            self.assertRegex(text, r"^[A-Z][a-z]{2}, [A-Z][a-z]{2} \d{1,2}, \d{1,2}:\d{2}(am|pm)$")
            for banned in ("CDT", "CST", "CT", "UTC", "AM", "PM"):
                self.assertNotIn(banned, text)

    def test_fallback_rule_matches_zoneinfo_when_the_tz_database_is_missing(self) -> None:
        with mock.patch.object(cli, "_central_zone", return_value=None):
            for stamp, expected in CASES:
                with self.subTest(expected=expected):
                    self.assertEqual(cli.format_time(stamp), expected)

    def test_fallback_rule_across_several_years(self) -> None:
        for year in range(2024, 2031):
            for month, day in ((1, 15), (3, 20), (7, 4), (11, 20)):
                stamp = utc(year, month, day, 18, 0)
                with mock.patch.object(cli, "_central_zone", return_value=None):
                    fallback = cli.format_time(stamp)
                self.assertEqual(fallback, cli.format_time(stamp), (year, month, day))


if __name__ == "__main__":
    unittest.main()
