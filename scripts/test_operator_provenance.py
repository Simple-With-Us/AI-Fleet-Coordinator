"""Tests for agent_sync.operator_provenance (AFC-412 PR3).  Run:  python3 scripts/test_operator_provenance.py"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_sync import operator_provenance as P
from agent_sync.operator_registry import SessionRegistry


def registry(tmp):
    return SessionRegistry(os.path.join(tmp, "registry.sqlite3"))


class ProvenanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.reg = registry(self.tmp.name)
        self.record = self.reg.resolve_or_register(
            dict(
                seat="MM",
                platform="zulip",
                verified_host="mac-1",
                profile_scope="swu",
                native_session_id="n1",
                source="test",
            )
        )["record_id"]

    def tearDown(self):
        self.reg.close_db()
        self.tmp.cleanup()

    def test_allowlisted_event_writes(self):
        eid = P.emit_provenance_event(
            self.reg, self.record, "token_mint", "OPERATOR", "ok", "broker", {"target_seat": "MM"}
        )
        self.assertTrue(eid)

    def test_unknown_event_refused(self):
        with self.assertRaises(P.UnknownProvenanceEvent):
            P.emit_provenance_event(self.reg, self.record, "steal_creds", "x", "ok", "x", {})

    def test_missing_required_detail_refused(self):
        with self.assertRaises(ValueError):
            P.emit_provenance_event(
                self.reg, self.record, "token_mint", "OPERATOR", "ok", "broker", {}
            )

    def test_secret_key_refused(self):
        with self.assertRaises(P.SecretInDetails):
            P.emit_provenance_event(
                self.reg,
                self.record,
                "token_mint",
                "OPERATOR",
                "ok",
                "broker",
                {"target_seat": "MM", "api_key": "redacted"},
            )

    def test_secret_value_refused(self):
        with self.assertRaises(P.SecretInDetails):
            P.emit_provenance_event(
                self.reg,
                self.record,
                "token_mint",
                "OPERATOR",
                "ok",
                "broker",
                {"target_seat": "MM", "note": "ghp_" + "a" * 30},
            )

    def test_private_key_block_refused(self):
        with self.assertRaises(P.SecretInDetails):
            P.emit_provenance_event(
                self.reg,
                self.record,
                "token_mint",
                "OPERATOR",
                "ok",
                "broker",
                {"target_seat": "MM", "note": "-----BEGIN PRIVATE KEY-----"},
            )


if __name__ == "__main__":
    unittest.main()
