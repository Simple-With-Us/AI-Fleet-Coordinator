"""Deterministic read bounds are terminal; uncertain writes remain fenced."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from git_relay.audit import AuditLog
from git_relay.github_client import RelayGitHubError
from git_relay.policy import SeatPolicy
from git_relay.server import Relay


class ResponseBoundRetryTests(unittest.TestCase):
    def handle(self, action, error_code):
        credential = "fixture-auth-value-for-tests"
        policy = SeatPolicy(json.dumps({"swu-echo": {
            "token": credential, "actions": ["wf_pr_get", "open_pr"],
            "repos": ["example/fixture"], "app_slug": "swu-echo",
            "mint_permissions": {"pull_requests": "write", "metadata": "read"},
        }}))
        args = {"repo": "example/fixture", "number": 1} if action == "wf_pr_get" else {
            "repo": "example/fixture", "base": "main", "head": "fixture", "title": "fixture", "body": "fixture"}
        with tempfile.TemporaryDirectory() as tmp:
            relay = Relay(policy, object(), AuditLog(os.path.join(tmp, "audit.jsonl")), workflow_verbs_enabled=True)
            with patch.object(relay, "_dispatch", side_effect=RelayGitHubError(0, "fixture", code=error_code)):
                return relay.handle(credential, action, args)

    def test_bounded_read_is_explicit_nonretryable_502(self):
        status, payload = self.handle("wf_pr_get", "response_too_large")
        self.assertEqual(status, 502)
        self.assertIs(payload["retryable"], False)
        self.assertEqual(payload["github_code"], "response_too_large")
        self.assertNotIn("outcome", payload)

    def test_transient_read_remains_retryable(self):
        status, payload = self.handle("wf_pr_get", None)
        self.assertEqual(status, 502)
        self.assertIs(payload["retryable"], True)

    def test_bounded_write_remains_unknown_nonretryable(self):
        status, payload = self.handle("open_pr", "response_too_large")
        self.assertEqual(status, 502)
        self.assertIs(payload["retryable"], False)
        self.assertEqual(payload["outcome"], "unknown")
