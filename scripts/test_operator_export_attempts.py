"""Tests for the OPS-1037 store-side export fence (export_attempts).

Run:  python3 scripts/test_operator_export_attempts.py
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_sync import operator_registry as R


def make_db(tmp):
    db = os.path.join(tmp, "registry.sqlite3")
    R.register_session(
        db, platform="zulip", verified_host="mac-1", profile_scope="simplewithus",
        native_session_id="native-1", seat="MM", source="test", title="t", project="AFC",
        issuer=R.ISSUER_OPERATOR)
    return db


class ExportAttemptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = make_db(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _pending_id(self):
        rows = R.pending_outbox(self.db)
        self.assertTrue(rows)
        return rows[0]["outbox_id"]

    def test_schema_v2_creates_fence_table(self):
        with sqlite3.connect(self.db) as db:
            tables = {r[0] for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            version = db.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        self.assertIn("export_attempts", tables)
        self.assertEqual(version, 2)

    def test_record_and_read_back(self):
        oid = self._pending_id()
        attempt_id = R.record_export_attempt(
            self.db, oid, destination="wb-1", tab="Sessions", kind="session_upsert",
            outcome="exported")
        self.assertTrue(attempt_id)
        rows = R.export_attempts(self.db, outbox_id=oid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["attempt_id"], attempt_id)
        self.assertEqual(rows[0]["outcome"], "exported")
        self.assertEqual(rows[0]["tab"], "Sessions")
        self.assertEqual(rows[0]["destination"], "wb-1")

    def test_all_outcomes_and_invalid_rejected(self):
        oid = self._pending_id()
        for outcome in ("exported", "already_present", "failed", "ambiguous"):
            R.record_export_attempt(self.db, oid, destination="wb-1", tab="Events",
                                    kind="event_append", outcome=outcome, detail="d")
        self.assertEqual(len(R.export_attempts(self.db, outbox_id=oid)), 4)
        with self.assertRaises(ValueError):
            R.record_export_attempt(self.db, oid, destination="wb-1", tab="Events",
                                    kind="event_append", outcome="shipped")

    def test_attempt_is_history_not_state(self):
        # A fence row never clears the outbox row by itself: only mark_exported does.
        oid = self._pending_id()
        R.record_export_attempt(self.db, oid, destination="wb-1", tab="Sessions",
                                kind="session_upsert", outcome="failed", detail="HTTP 503")
        self.assertIn(oid, [r["outbox_id"] for r in R.pending_outbox(self.db)])
        R.mark_exported(self.db, [oid])
        self.assertNotIn(oid, [r["outbox_id"] for r in R.pending_outbox(self.db)])


class ExportAttemptCliTests(unittest.TestCase):
    def test_cli_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = make_db(tmp)
            oid = R.pending_outbox(db)[0]["outbox_id"]
            out = io.StringIO()
            with redirect_stdout(out):
                rc = R.main(["record-export-attempt", "--db", db, "--outbox-id", oid,
                             "--destination", "wb-1", "--tab", "Sessions",
                             "--kind", "session_upsert", "--outcome", "exported"])
            self.assertEqual(rc, 0)
            recorded = json.loads(out.getvalue())
            self.assertTrue(recorded["attempt_id"])
            out = io.StringIO()
            with redirect_stdout(out):
                rc = R.main(["export-attempts", "--db", db, "--outbox-id", oid])
            self.assertEqual(rc, 0)
            rows = json.loads(out.getvalue())
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["outbox_id"], oid)


if __name__ == "__main__":
    unittest.main()
