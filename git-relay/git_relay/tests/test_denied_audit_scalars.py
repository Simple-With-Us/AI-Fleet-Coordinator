"""A denied workflow call audits the bounded scalar allowlist, never payload text.

This is the anchor the guarded deployment transaction binds a probe to: the
relay's own audit row for the denial carries the allowlisted scalars (path
among them, so a one-time proof id in the probe path is findable), while the
message and edits - which can carry a private payload - are never logged.
"""

import json
import os
import tempfile
import unittest

from git_relay.audit import AuditLog
from git_relay.policy import SeatPolicy
from git_relay.server import Relay
from git_relay.workflow_verbs import Refused

TOKEN = "t" * 32
SEATS = json.dumps(
    {
        "swu-test": {
            "auth": "token",
            "token": TOKEN,
            "identity": "TEST",
            "app_slug": "swu-test",
            "actions": ["wf_file_patch"],
            "repos": ["Simple-With-Us/AI-Fleet-Coordinator"],
            "mint_permissions": {"contents": "write"},
        }
    }
)


class _Client:
    """Never reached: _dispatch is stubbed in every test here."""


class DeniedAuditScalarTests(unittest.TestCase):
    def _relay(self, log):
        return Relay(SeatPolicy(SEATS), _Client(), AuditLog(log), workflow_verbs_enabled=True)

    def _probe_args(self):
        return {
            "repo": "Simple-With-Us/AI-Fleet-Coordinator",
            "branch": "main",
            "path": "deploy-proof/proof-x.probe",
            "message": "private payload text never audited",
            "expected_head_sha": "0" * 40,
            "base_blob_sha": "0" * 40,
            "edits": [{"start": 0, "end": 0, "text": "payload body never audited"}],
        }

    def test_refused_workflow_call_audits_scalars_not_payload(self):
        with tempfile.TemporaryDirectory() as td:
            log = os.path.join(td, "audit.jsonl")
            relay = self._relay(log)

            def boom(seat, action, args, repo):
                raise Refused("default branch is not writable")

            relay._dispatch = boom
            status, _payload = relay.handle(TOKEN, "wf_file_patch", self._probe_args())
            self.assertEqual(status, 400)
            rows = [json.loads(line) for line in open(log)]
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["result"], "bad_request")
            self.assertEqual(row["reason"], "default branch is not writable")
            self.assertEqual(row["path"], "deploy-proof/proof-x.probe")
            self.assertEqual(row["branch"], "main")
            self.assertEqual(row["expected_head_sha"], "0" * 40)
            self.assertEqual(row["seat"], "swu-test")
            self.assertEqual(row["action"], "wf_file_patch")
            self.assertNotIn("message", row)
            self.assertNotIn("edits", row)
            self.assertNotIn("base_blob_sha", row)  # not in the scalar allowlist

    def test_denied_workflow_call_audits_scalars_not_payload(self):
        with tempfile.TemporaryDirectory() as td:
            log = os.path.join(td, "audit.jsonl")
            relay = self._relay(log)

            from git_relay.server import PolicyDenied

            def boom(seat, action, args, repo):
                raise PolicyDenied("seat ceiling does not cover permission 'contents: write'")

            relay._dispatch = boom
            status, _payload = relay.handle(TOKEN, "wf_file_patch", self._probe_args())
            self.assertEqual(status, 403)
            rows = [json.loads(line) for line in open(log)]
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["result"], "denied")
            self.assertEqual(row["path"], "deploy-proof/proof-x.probe")
            self.assertNotIn("message", row)
            self.assertNotIn("edits", row)

    def test_non_workflow_denial_keeps_reason_only(self):
        with tempfile.TemporaryDirectory() as td:
            log = os.path.join(td, "audit.jsonl")
            seats = json.dumps(
                {
                    "swu-test": {
                        "auth": "token",
                        "token": TOKEN,
                        "identity": "TEST",
                        "app_slug": "swu-test",
                        "actions": ["comment_pr"],
                        "repos": ["Simple-With-Us/AI-Fleet-Coordinator"],
                        "mint_permissions": {"pull_requests": "write"},
                    }
                }
            )
            relay = Relay(SeatPolicy(seats), _Client(), AuditLog(log))

            def boom(seat, action, args, repo):
                raise ValueError("no such pull request")

            relay._dispatch = boom
            status, _payload = relay.handle(
                TOKEN,
                "comment_pr",
                {"repo": "Simple-With-Us/AI-Fleet-Coordinator", "number": 1, "body": "hi"},
            )
            self.assertEqual(status, 400)
            rows = [json.loads(line) for line in open(log)]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["reason"], "no such pull request")
            self.assertNotIn("number", rows[0])
            self.assertNotIn("body", rows[0])


if __name__ == "__main__":
    unittest.main()
