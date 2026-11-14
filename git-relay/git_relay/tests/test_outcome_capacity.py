import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from git_relay import outcomes
from git_relay.outcomes import OutcomeStore, OutcomeStoreError


class OutcomeCapacityTests(unittest.TestCase):
    def test_default_and_module_default_are_resolved_at_construction(self):
        self.assertEqual(OutcomeStore()._max_entries, 5000)
        with patch.object(outcomes, "MAX_ENTRIES", 7):
            self.assertEqual(OutcomeStore()._max_entries, 7)

    def test_explicit_capacity_above_default_survives_reopen_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outcomes.json"
            now = 1_700_000_000.0
            recent_ok = {
                f"{index:064x}": {"state": "ok", "ts": now - 60, "result": {"id": index}}
                for index in range(5001)
            }
            path.write_text(json.dumps(recent_ok), encoding="utf-8")

            store = OutcomeStore(str(path), clock=lambda: now, max_entries=20000)
            self.assertEqual(len(store._rows), 5001)
            key = "f" * 64
            self.assertIsNone(store.begin(key))
            store.finish(key, "ok", {"number": 42})

            reopened = OutcomeStore(str(path), clock=lambda: now, max_entries=20000)
            self.assertEqual(len(reopened._rows), 5002)
            self.assertEqual(reopened.begin(key)["result"], {"number": 42})

    def test_full_held_rows_refuse_new_write(self):
        store = OutcomeStore(max_entries=2)
        store._rows = {
            "a" * 64: {"state": "unknown", "ts": 1.0},
            "b" * 64: {"state": "pending", "ts": 2.0},
        }
        with self.assertRaises(OutcomeStoreError):
            store.begin("c" * 64)
        self.assertEqual(len(store._rows), 2)

    def test_capacity_downgrade_preserves_held_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outcomes.json"
            now = 1_700_000_000.0
            rows = {
                "a" * 64: {"state": "unknown", "ts": now - 100000},
                "b" * 64: {"state": "pending", "ts": now - 90000},
                "c" * 64: {"state": "ok", "ts": now - 60},
            }
            path.write_text(json.dumps(rows), encoding="utf-8")
            store = OutcomeStore(str(path), clock=lambda: now, max_entries=2)
            # With durable archiving (outcome-corr-archive), a downgrade is no
            # longer a hard wall while held unknown rows can move to the
            # archive:  the write is admitted and the archived keys keep
            # refusing exactly as live held rows do.  Refusal still applies
            # once nothing unknown remains to archive.
            self.assertIsNone(store.begin("d" * 64))
            for key in ("a" * 64, "b" * 64):
                held = store.begin(key)
                self.assertIsNotNone(held)
                self.assertTrue(held.get("archived"), key[:4])
            self.assertIn("c" * 64, store._rows)
            archive_lines = (Path(str(path) + ".archive.jsonl")).read_text(encoding="utf-8").splitlines()
            archived_keys = {json.loads(line)["key"] for line in archive_lines}
            self.assertEqual(archived_keys, {"a" * 64, "b" * 64})

    def test_full_recent_ok_rows_remain_replayable_and_refuse_a_fresh_write(self):
        now = 1_700_000_000.0
        store = OutcomeStore(clock=lambda: now, max_entries=2)
        store._rows = {
            "a" * 64: {"state": "ok", "ts": now - 1, "result": {"id": 1}},
            "b" * 64: {"state": "ok", "ts": now - 2, "result": {"id": 2}},
        }
        with self.assertRaises(OutcomeStoreError):
            store.begin("c" * 64)
        self.assertEqual(store.begin("a" * 64)["result"], {"id": 1})
        self.assertEqual(len(store._rows), 2)

    def test_stale_ok_and_oldest_refused_rows_remain_the_only_trim_targets(self):
        now = 1_700_000_000.0
        store = OutcomeStore(clock=lambda: now, max_entries=3)
        store._rows = {
            "a" * 64: {"state": "unknown", "ts": now - 100},
            "b" * 64: {"state": "ok", "ts": now - outcomes.OK_TTL_SECONDS - 1},
            "c" * 64: {"state": "refused", "ts": now - 50},
            "d" * 64: {"state": "refused", "ts": now - 40},
            "e" * 64: {"state": "ok", "ts": now - 60},
        }
        store._trim()
        self.assertNotIn("b" * 64, store._rows)
        self.assertNotIn("c" * 64, store._rows)
        self.assertIn("d" * 64, store._rows)
        self.assertIn("a" * 64, store._rows)
        self.assertIn("e" * 64, store._rows)

    def test_invalid_capacities_are_rejected(self):
        for value in (True, False, 0, -1, 1.5, "5000", outcomes.MAX_CAPACITY + 1):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "max_entries must be a positive integer"):
                    OutcomeStore(max_entries=value)


if __name__ == "__main__":
    unittest.main()
