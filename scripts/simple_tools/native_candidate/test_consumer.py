import importlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Package registration must precede dynamic package imports, independent of sorter.
fixture = importlib.import_module("test_integration")
B, Driver = fixture.B, fixture.Driver
Consumer = importlib.import_module("native_fixture_package.native_consumer").Consumer
merge = importlib.import_module("native_fixture_package.native_evidence").merge


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/dev/shm")
        self.d = Driver(Path(self.tmp.name), 99, "node", "/fixture")
        self.actions = []
        self.d.journal.transport = lambda req: self.actions.append(req["action"]) or {}

    def tearDown(self):
        self.d.journal.close()
        self.tmp.cleanup()

    def seed(self):
        self.d.journal.verify = lambda _: True
        self.d.journal.deliver(B, "fixture", "frozen original", fresh=True)
        self.actions = []

    def test_later_fetch_preserved_on_partial_read(self):
        self.seed()
        c = Consumer(self.d, lambda _: True)
        proof = {
            "native_input": "persisted",
            "message_id": "m",
            "sourceCommandId": "cmd",
            "turn": "completed",
            "owning_alert": "fetched",
            "alert_id": 123,
            "fetch_message_id": "a",
            "fetch_part_id": "p",
        }
        with patch("native_fixture_package.native_integration.reconcile", return_value=proof):
            r = c.step()[0]
        self.assertEqual(r["owning_alert"], "fetched")
        self.assertEqual(r["handling"], "unverified")
        self.assertEqual(r["source_refs"]["fetch_part_id"], "p")
        with patch(
            "native_fixture_package.native_integration.reconcile",
            return_value={
                "native_input": "unverified",
                "turn": "unverified",
                "owning_alert": "unverified",
            },
        ):
            r = c.step()[0]
        self.assertEqual(r["owning_alert"], "fetched")
        self.assertTrue(r["read_incomplete"])
        self.assertEqual(self.actions, ["query", "query"])

    def test_frozen_intent(self):
        self.seed()
        c = Consumer(self.d, lambda _: True)
        with patch("native_fixture_package.native_integration.reconcile", return_value={}) as read:
            c.step()
        self.assertEqual(read.call_args.args[1], B)
        self.assertEqual(read.call_args.args[3], "frozen original")
        self.assertEqual(self.actions, ["query"])

    def test_evidence_failure_never_reports_stale_as_current(self):
        self.seed()
        c = Consumer(self.d, lambda _: True)
        with patch(
            "native_fixture_package.native_integration.persist",
            side_effect=OSError("full"),
        ):
            r = c.step()[0]
        self.assertEqual(r["reconciliation"], "unverified")
        self.assertEqual(self.actions, ["query"])

    def test_bounded_future_rounds_query_only(self):
        self.seed()
        c = Consumer(self.d, lambda _: True)
        reports = []
        waits = []
        c.run(reports.append, max_rounds=3, interval_seconds=60, wait=waits.append)
        self.assertEqual(len(reports), 3)
        self.assertEqual(waits, [60, 60])
        self.assertEqual(self.actions, ["query"] * 3)

    def test_binding_changes_during_evidence_read(self):
        self.seed()
        current = [True]
        c = Consumer(self.d, lambda _: current[0])
        from native_fixture_package.native_consumer import read_private as actual

        def read(path):
            value = actual(path)
            if path.parent.name == "native-evidence":
                current[0] = False
            return value

        with patch("native_fixture_package.native_consumer.read_private", side_effect=read):
            r = c.step()[0]
        self.assertEqual(r["reconciliation"], "unverified")
        self.assertEqual(self.actions, ["query"])
        self.assertTrue(list((self.d.state / "native-evidence").glob("*.json")))

    def test_stale_binding_no_query(self):
        self.seed()
        Consumer(self.d, lambda _: False).step()
        self.assertEqual(self.actions, [])

    def test_corrupt_journal_no_query(self):
        self.seed()
        next((self.d.state / "native-commands").glob("*.json")).write_text("{}")
        Consumer(self.d, lambda _: True).step()
        self.assertEqual(self.actions, [])

    def test_bounded_step(self):
        self.d.journal.verify = lambda _: True
        for i in range(3):
            self.d.journal.deliver(B, str(i), "fixture", fresh=True)
        self.actions = []
        c = Consumer(self.d, lambda _: True, max_records=1)
        self.assertEqual(len(c.step()), 1)
        self.assertEqual(len(c.step()), 1)
        self.assertEqual(self.actions, ["query", "query"])

    def test_conflicting_positive_refs_flagged_preserved(self):
        old = {
            "version": 1,
            "binding": B,
            "wake_key": "w",
            "evidence": {
                "native_input": "persisted",
                "message_id": "first",
                "owning_alert": "fetched",
            },
        }
        r = merge(
            old,
            {
                **old,
                "evidence": {
                    "native_input": "persisted",
                    "message_id": "different",
                    "owning_alert": "unverified",
                },
            },
        )
        self.assertTrue(r["conflict"])
        self.assertEqual(r["evidence"]["message_id"], "first")
        self.assertEqual(r["evidence"]["owning_alert"], "fetched")


if __name__ == "__main__":
    unittest.main()
