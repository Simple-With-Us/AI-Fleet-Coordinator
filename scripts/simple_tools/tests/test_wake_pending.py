"""Parked wakes for held chats:  the pending spool and the consumed ledger."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from simple_tools import wake_pending as WP

SID = "11111111-2222-4333-8444-555555555555"
SID2 = "22222222-3333-4444-8555-666666666666"


class WakePendingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = os.path.join(self.tmp.name, "state")

    def test_park_then_take_hands_over_and_consumes(self) -> None:
        self.assertTrue(WP.park(self.state, SID, "w-park-1", "[MC wake] hello"))
        self.assertEqual(WP.take(self.state, SID), [("w-park-1", "[MC wake] hello")])
        self.assertEqual(WP.take(self.state, SID), [])
        self.assertTrue(WP.is_consumed(self.state, "w-park-1"))

    def test_repark_of_the_same_wake_keeps_one_file(self) -> None:
        self.assertTrue(WP.park(self.state, SID, "w-park-2", "[MC wake] again"))
        self.assertTrue(WP.park(self.state, SID, "w-park-2", "[MC wake] again"))
        self.assertEqual(len(WP.take(self.state, SID)), 1)

    def test_take_ignores_other_sessions(self) -> None:
        WP.park(self.state, SID, "w-park-3", "[MC wake] mine")
        WP.park(self.state, SID2, "w-park-4", "[MC wake] theirs")
        self.assertEqual(WP.take(self.state, SID), [("w-park-3", "[MC wake] mine")])
        self.assertEqual(WP.take(self.state, SID2), [("w-park-4", "[MC wake] theirs")])

    def test_take_on_an_empty_spool_is_nothing(self) -> None:
        self.assertEqual(WP.take(self.state, SID), [])

    def test_park_refuses_unsafe_names(self) -> None:
        for bad in ("", "../x", "a/b", "a\\b", ".", ".."):
            with self.assertRaises(ValueError):
                WP.park(self.state, bad, "w-park-5", "[MC wake] x")
            with self.assertRaises(ValueError):
                WP.park(self.state, SID, bad, "[MC wake] x")
        with self.assertRaises(ValueError):
            WP.park(self.state, SID, "w-park-6", "")

    def test_take_skips_and_removes_corrupt_files(self) -> None:
        folder = os.path.join(WP.pending_dir(self.state), SID)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "w-bad.json"), "w", encoding="utf-8") as fh:
            fh.write("not json{{")
        WP.park(self.state, SID, "w-good.json".replace(".json", ""), "[MC wake] good")
        self.assertEqual(WP.take(self.state, SID), [("w-good", "[MC wake] good")])
        self.assertEqual(os.listdir(folder), [])
        self.assertFalse(WP.is_consumed(self.state, "w-bad"))

    def test_park_after_consume_is_refused(self) -> None:
        WP.park(self.state, SID, "w-park-7", "[MC wake] once")
        self.assertEqual(WP.take(self.state, SID), [("w-park-7", "[MC wake] once")])
        self.assertFalse(WP.park(self.state, SID, "w-park-7", "[MC wake] once"))

    def test_consumed_entries_expire(self) -> None:
        WP.mark_consumed(self.state, "w-old", now=1000.0)
        self.assertTrue(WP.is_consumed(self.state, "w-old"))
        WP.mark_consumed(self.state, "w-new", now=1000.0 + WP.CONSUMED_TTL_SECONDS + 1.0)
        self.assertFalse(WP.is_consumed(self.state, "w-old"))
        self.assertTrue(WP.is_consumed(self.state, "w-new"))

    def test_state_dir_mirrors_the_adapter_rule(self) -> None:
        home = os.path.join(self.tmp.name, "home")
        self.assertEqual(
            WP.state_dir({"HOME": home}), os.path.join(home, ".local", "state", "muse-wake")
        )
        self.assertEqual(WP.state_dir({"HOME": home, "MUSE_WAKE_STATE": "/x"}), "/x/muse-wake")


if __name__ == "__main__":
    unittest.main()
