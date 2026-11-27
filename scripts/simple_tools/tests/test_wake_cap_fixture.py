"""AFC-884:  a seat's wake count cap, and the interval counter that reads it.

A seat's wake count caps live in its own `budget` table, and the hourly cap counts the cold wake
rows inside one hour (3600 seconds) of the moment each was accepted.  These tests read the shipped
fixture with the real config loader, so the seat identifier and the cap are the ones a live file
carries, and they check the matching interval counter with a fixed clock:  no wake is sent, nothing
is scheduled, and no live file is read or written.

The cap is a whole number.  The budget compares a whole count against the cap (count >= cap), so a
fractional cap admits ceil(c) wakes per window and behaves exactly like its ceiling;  a decimal rate
needs its own enforcement path, not a decimal value in the table.  The last test is the proof of
that boundary, so the next reader does not have to rediscover it.
"""

from __future__ import annotations

import os
import tomllib
import unittest

from simple_tools import config as C
from simple_tools import wakes as W

FIXTURE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "fixtures", "wake-cap-count-interval.toml"
)

# The clock the counter runs on:  a fixed hour, so nothing in this file depends on the wall clock.
NOW = 1_000_000.0
HOUR = 3600.0


def fixture_text() -> str:
    with open(FIXTURE, "rb") as fh:
        return fh.read().decode("utf-8")


def load(text: str) -> C.Config:
    return C.from_dict(tomllib.loads(text))


class WakeCountCapFixtureTests(unittest.TestCase):
    def test_the_fixture_is_a_real_seat_with_a_whole_cap(self) -> None:
        raw = tomllib.loads(fixture_text())
        seat = raw["seat"]["MM"]
        # The file carries a whole number, not a decimal:  the raw value is an int in TOML.
        self.assertIsInstance(seat["budget"]["wakes_per_hour"], int)

        cfg = load(fixture_text())
        self.assertEqual(cfg.errors, [])
        self.assertIn("MM", cfg.seats)
        budget = cfg.seats["MM"].budget
        self.assertEqual(budget["wakes_per_hour"], 2)
        self.assertEqual(float(budget["wakes_per_hour"]).is_integer(), True)
        # The seat is a waking seat with a routine, the shape the shipped listener carries.
        self.assertEqual(cfg.seats["MM"].wake, "http")
        self.assertIsNotNone(cfg.seats["MM"].routine)

    def test_the_seat_tag_folds_into_one_seat(self) -> None:
        # A lower-case tag is the same seat:  the section name is upper-cased when it is read.
        text = fixture_text().replace("[seat.MM]", "[seat.mm]")
        cfg = load(text)
        self.assertEqual(cfg.errors, [])
        self.assertIn("MM", cfg.seats)
        self.assertNotIn("mm", cfg.seats)

    def view(self, wake_id: str, at: float, *, owner: bool = False, key: str = "k") -> dict:
        return {
            "wake_id": wake_id,
            "state": "done",
            "owner": owner,
            "topic_key": key,
            "cost_usd": 0.0,
            "ts": at,
            "times": {"accepted": at, "started": at, "done": at},
        }

    def cold_wakes(self, count: int, *, age: float = 60.0, key: str = "k") -> dict:
        """`count` cold wake rows, each `age` seconds old, one per topic."""
        return {
            str(i): self.view(str(i), NOW - age - i, key="%s%d" % (key, i))
            for i in range(count)
        }

    def test_the_interval_counter_matches_the_cap(self) -> None:
        cfg = load(fixture_text())
        budget = cfg.seats["MM"].budget
        cap = int(budget["wakes_per_hour"])

        # One fewer than the cap in the window:  the next wake fits.
        self.assertIsNone(
            W.budget_block(self.cold_wakes(cap - 1), budget, owner=False, key="new", now=NOW)
        )
        # The cap in the window:  the next wake is held back, and it says which limit holds it.
        self.assertEqual(
            W.budget_block(self.cold_wakes(cap), budget, owner=False, key="new", now=NOW),
            "wakes_per_hour",
        )

    def test_the_window_is_one_hour_of_accepted_wakes(self) -> None:
        cfg = load(fixture_text())
        budget = cfg.seats["MM"].budget
        cap = int(budget["wakes_per_hour"])

        # The same count, but every row older than the window:  the counter does not see them.
        old = self.cold_wakes(cap, age=HOUR + 1.0)
        self.assertIsNone(W.budget_block(old, budget, owner=False, key="new", now=NOW))

        # Every row inside the window:  the oldest is 3599 seconds old, so the cap bites.
        inside = self.cold_wakes(cap, age=HOUR - cap)
        self.assertEqual(
            W.budget_block(inside, budget, owner=False, key="new", now=NOW), "wakes_per_hour"
        )

        # The window is exclusive at its edge:  a row exactly 3600 seconds old is outside it.
        edge = self.cold_wakes(cap, age=HOUR - (cap - 1))
        self.assertIsNone(W.budget_block(edge, budget, owner=False, key="new", now=NOW))

    def test_a_fractional_cap_would_be_exactly_its_ceiling(self) -> None:
        # The boundary the fixture keeps out of the table:  the budget compares a whole count
        # against the cap, so a fractional cap admits ceil(c) wakes per window and nothing else.
        # A decimal rate needs its own enforcement path;  a decimal value here is not a limit.
        cfg = load(fixture_text())
        whole = dict(cfg.seats["MM"].budget, wakes_per_hour=2)
        fractional = dict(cfg.seats["MM"].budget, wakes_per_hour=1.5)

        for count in range(4):
            views = self.cold_wakes(count)
            self.assertEqual(
                W.budget_block(views, whole, owner=False, key="new", now=NOW),
                W.budget_block(views, fractional, owner=False, key="new", now=NOW),
                "a cap of 1.5 and a cap of 2 differ at %d wakes" % count,
            )
        # And the fixture itself stays whole.
        self.assertEqual(float(cfg.seats["MM"].budget["wakes_per_hour"]).is_integer(), True)


if __name__ == "__main__":
    unittest.main()
