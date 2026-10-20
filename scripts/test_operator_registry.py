"""Tests for agent_sync.operator_registry (AFC-412 PR1).  Run:  python3 scripts/test_operator_registry.py"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_sync import operator_registry as R  # noqa: E402


def ident(seat="MM", platform="zulip", host="mac-1", scope="simplewithus",
          native="native-1", **kw):
    base = dict(seat=seat, platform=platform, verified_host=host, profile_scope=scope,
                native_session_id=native, source="test", title="Thread Title")
    base.update(kw)
    return base


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "registry.sqlite3")
        self.reg = R.SessionRegistry(self.db)

    def tearDown(self):
        self.reg.close_db()
        self.tmp.cleanup()

    def test_register_allocates_A_gen1(self):
        r = self.reg.resolve_or_register(ident())
        self.assertTrue(r["created"])
        self.assertEqual(r["outcome"], "registered")
        self.assertEqual(r["label"], "A")
        self.assertEqual(r["generation"], 1)

    def test_resume_same_identity_returns_same_record_and_label(self):
        first = self.reg.resolve_or_register(ident())
        again = self.reg.resolve_or_register(ident())
        self.assertFalse(again["created"])
        self.assertEqual(again["outcome"], "resumed")
        self.assertEqual(again["record_id"], first["record_id"])
        self.assertEqual(again["label"], "A")

    def test_new_native_id_is_new_registration_new_label(self):
        self.reg.resolve_or_register(ident(native="native-1"))
        other = self.reg.resolve_or_register(ident(native="native-2"))
        self.assertTrue(other["created"])
        self.assertEqual(other["label"], "B")

    def test_seat_mismatch_returns_explicit_outcome_and_changes_nothing(self):
        first = self.reg.resolve_or_register(ident(seat="MM"))
        r = self.reg.resolve_or_register(ident(seat="CODEX"))
        self.assertEqual(r["outcome"], "seat_mismatch")
        self.assertEqual(r["record_id"], first["record_id"])
        row = self.reg._get(first["record_id"])
        self.assertEqual(row["seat"], "MM")  # no rebind
        conn = sqlite3.connect(self.db)
        n = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event = 'resolve' AND outcome = 'seat_mismatch'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)

    def test_recycle_wraps_and_bumps_generation(self):
        a = self.reg.resolve_or_register(ident(native="n1"))
        self.assertEqual(a["label"], "A")
        self.assertTrue(self.reg.close(a["record_id"], actor="MM", source="test"))
        b = self.reg.resolve_or_register(ident(native="n2"))
        self.assertEqual(b["label"], "A")       # wrapped back to A
        self.assertEqual(b["generation"], 2)    # disambiguated by generation
        self.assertEqual(R.cross_seat_ref("MM", b["label"], b["generation"]), "MM A (gen 2)")
        self.assertEqual(R.cross_seat_ref("MM", "B", 1), "MM B")

    def test_lowest_free_letter_wins(self):
        a = self.reg.resolve_or_register(ident(native="n1"))
        b = self.reg.resolve_or_register(ident(native="n2"))
        self.assertEqual((a["label"], b["label"]), ("A", "B"))
        self.reg.close(a["record_id"], actor="MM", source="test")
        c = self.reg.resolve_or_register(ident(native="n3"))
        self.assertEqual(c["label"], "A")

    def test_label_exhaustion(self):
        for i in range(26):
            self.reg.resolve_or_register(ident(native=f"n{i}"))
        with self.assertRaises(R.LabelExhausted):
            self.reg.resolve_or_register(ident(native="n26"))

    def test_close_is_idempotent(self):
        a = self.reg.resolve_or_register(ident())
        self.assertTrue(self.reg.close(a["record_id"], actor="MM", source="test"))
        self.assertFalse(self.reg.close(a["record_id"], actor="MM", source="test"))

    def test_resumed_closed_is_explicit(self):
        a = self.reg.resolve_or_register(ident())
        self.reg.close(a["record_id"], actor="MM", source="test")
        r = self.reg.resolve_or_register(ident())
        self.assertEqual(r["outcome"], "resumed_closed")

    def test_register_writes_event_and_outbox_atomically(self):
        self.reg.resolve_or_register(ident())
        pending = R.pending_outbox(self.db)
        kinds = sorted(p["kind"] for p in pending)
        self.assertEqual(kinds, ["event_append", "session_upsert"])

    def test_mark_exported_then_crash_rerun(self):
        self.reg.resolve_or_register(ident())
        pending = R.pending_outbox(self.db)
        self.assertEqual(R.mark_exported(self.db, [p["outbox_id"] for p in pending]), 2)
        self.assertEqual(R.pending_outbox(self.db), [])

    def test_update_attrs_refuses_immutable_fields(self):
        a = self.reg.resolve_or_register(ident())
        with self.assertRaises(ValueError):
            self.reg.update_attrs(a["record_id"], record_id="nope")
        with self.assertRaises(ValueError):
            self.reg.update_attrs(a["record_id"], label="Z")
        self.reg.update_attrs(a["record_id"], title="New", state="paused")
        row = self.reg._get(a["record_id"])
        self.assertEqual((row["title"], row["state"]), ("New", "paused"))

    def test_display_title_format(self):
        self.assertEqual(R.display_title("H", "Thread Title"), "H: Thread Title")

    def test_missing_identity_fields_raise(self):
        with self.assertRaises(ValueError):
            self.reg.resolve_or_register({"seat": "MM"})

    def test_session_cells_match_sessions_headers(self):
        a = self.reg.resolve_or_register(ident())
        pending = R.pending_outbox(self.db)
        upsert = [p for p in pending if p["kind"] == "session_upsert"][0]
        cells = json.loads(upsert["payload"])["cells"]
        self.assertEqual(len(cells), len(R.SESSIONS_HEADERS))
        self.assertEqual(cells[0], a["record_id"])
        self.assertEqual(cells[1], "A")


if __name__ == "__main__":
    unittest.main()
