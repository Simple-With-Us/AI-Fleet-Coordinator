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

    def test_state_dir_is_a_pure_function_of_home(self) -> None:
        home = os.path.join(self.tmp.name, "home")
        self.assertEqual(
            WP.state_dir(home), os.path.join(home, ".local", "state", "muse-wake")
        )

    def test_peek_shows_without_consuming_and_consume_removes(self) -> None:
        WP.park(self.state, SID, "w-peek-1", "[MC wake] one")
        self.assertEqual(WP.peek(self.state, SID), [("w-peek-1", "[MC wake] one")])
        self.assertEqual(WP.peek(self.state, SID), [("w-peek-1", "[MC wake] one")])
        self.assertFalse(WP.is_consumed(self.state, "w-peek-1"))
        WP.consume(self.state, SID, ["w-peek-1"])
        self.assertEqual(WP.peek(self.state, SID), [])
        self.assertTrue(WP.is_consumed(self.state, "w-peek-1"))

    def test_consume_ignores_unknown_ids(self) -> None:
        WP.consume(self.state, SID, ["w-never-parked"])
        self.assertFalse(WP.is_consumed(self.state, "w-never-parked"))

    def test_park_refuses_a_symlinked_spool(self) -> None:
        real = os.path.join(self.tmp.name, "real")
        os.makedirs(real)
        link = os.path.join(self.tmp.name, "linked-state")
        os.symlink(real, link)
        with self.assertRaises(OSError):
            WP.park(link, SID, "w-link-1", "[MC wake] x")

    def test_concurrent_marks_keep_every_wake_id(self) -> None:
        import threading

        ids = ["w-race-%02d" % n for n in range(20)]

        def mark_some(part: list[str]) -> None:
            for wake_id in part:
                WP.mark_consumed(self.state, wake_id)

        threads = [threading.Thread(target=mark_some, args=(ids[i::4],)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        for wake_id in ids:
            self.assertTrue(WP.is_consumed(self.state, wake_id))

    def test_mark_delivered_without_a_pending_file_still_marks(self) -> None:
        self.assertFalse(WP.mark_delivered(self.state, SID, "w-fresh-1"))
        self.assertTrue(WP.is_consumed(self.state, "w-fresh-1"))


if __name__ == "__main__":
    unittest.main()
