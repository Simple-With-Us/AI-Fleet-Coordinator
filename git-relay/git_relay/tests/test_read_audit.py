"""Successful reads leave the relay's audit log (owner ruling 2026-11-06).

A call that ended "ok" and whose action is a known read writes no row.  Every
failure, denial, write and mint_token still writes one.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest import mock

from git_relay.audit import AuditLog
from git_relay.policy import SeatPolicy
from git_relay.server import Relay
from git_relay.tests.test_git_relay import SEAT_TOKEN, SEATS_JSON, StubClient
from git_relay.workflow_verbs import SPECS


def _rows(path):
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


class AuditsTest(unittest.TestCase):
    def test_only_a_successful_read_skips_the_row(self):
        read_wf = next(a for a in SeatPolicy.WORKFLOW_ACTIONS if a in SPECS and not SPECS[a][1])
        write_wf = next(a for a in SeatPolicy.WORKFLOW_ACTIONS if a in SPECS and SPECS[a][1])
        self.assertFalse(SeatPolicy.audits("git_read", "ok"))
        self.assertFalse(SeatPolicy.audits(read_wf, "ok"))
        for outcome in ("denied", "bad_request", "unavailable", "github_terminal", "error"):
            self.assertTrue(SeatPolicy.audits("git_read", outcome), outcome)
            self.assertTrue(SeatPolicy.audits(read_wf, outcome), outcome)
        self.assertTrue(SeatPolicy.audits(write_wf, "ok"))
        self.assertTrue(SeatPolicy.audits("comment_pr", "ok"))
        self.assertTrue(SeatPolicy.audits("open_pr", "ok"))
        self.assertTrue(
            SeatPolicy.audits("mint_token", "ok"), "issuing a credential is always logged"
        )
        self.assertTrue(
            SeatPolicy.audits("no_such_action", "ok"), "an unknown name counts as a write"
        )

    def test_handle_records_writes_and_failures_but_not_ok_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.jsonl")
            relay = Relay(SeatPolicy(SEATS_JSON), StubClient(), AuditLog(path))
            args = {
                "repo": "example/fixture-repo",
                "base": "main",
                "head": "b",
                "title": "t",
                "body": "hello",
            }
            self.assertEqual(relay.handle(SEAT_TOKEN, "open_pr", args)[0], 200)
            self.assertEqual([r["result"] for r in _rows(path)], ["ok"], "a write records")
            # The same wiring with the action classified as a read:  no row on success.
            with mock.patch.object(SeatPolicy, "is_write", classmethod(lambda cls, a: False)):
                self.assertEqual(relay.handle(SEAT_TOKEN, "open_pr", dict(args, head="c"))[0], 200)
                self.assertEqual(len(_rows(path)), 1, "a successful read leaves no row")
                denied = dict(args, repo="elsewhere/repo", head="d")
                self.assertEqual(relay.handle(SEAT_TOKEN, "open_pr", denied)[0], 403)
            self.assertEqual(
                [r["result"] for r in _rows(path)], ["ok", "denied"], "a denied read records"
            )


if __name__ == "__main__":
    unittest.main()
