import json
import tempfile
import unittest
from pathlib import Path
from git_relay.policy import SeatPolicy
from git_relay.server import Relay
from git_relay.audit import AuditLog
from git_relay.github_client import RelayGitHubError

class Client:
    def __init__(self, refused=False):
        self.calls = []
        self.refused = refused
    def open_pr(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.refused:
            raise RelayGitHubError(401, "owner unavailable")
        return {"number": 1, "url": "https://github.com/o/r/pull/1"}
class NoMinter:
    def token_for(self, *args, **kwargs):
        raise AssertionError("App must not be minted for PR creation")
class Tests(unittest.TestCase):
    def test_authorized_legacy_route_is_denied_and_owner_route_never_falls_back(self):
        policy = SeatPolicy(json.dumps({"swu-wise": {"token": "s"*32,
            "actions": ["open_pr", "wf_pr_create"], "repos": ["o/r"],
            "app_slug": "swu-wise", "mint_permissions": {"pull_requests": "write"}}}))
        args = dict(repo="o/r", base="main", head="branch", title="title", body="body")
        for refused in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                client = Client(refused)
                relay = Relay(policy, client, AuditLog(str(Path(directory)/"audit.jsonl")), app_minter=NoMinter(), workflow_verbs_enabled=True)
                status, _ = relay.handle("s"*32, "wf_pr_create", {**args, "expected_head_sha": "a"*40})
                self.assertEqual(status, 400, _)
                self.assertIn("owner-only open_pr", _["error"])
                self.assertEqual(client.calls, [])
                status, _ = relay.handle("s"*32, "open_pr", args)
                self.assertEqual(status, 400 if refused else 200)
                self.assertEqual(len(client.calls), 1)
                self.assertNotIn("bearer", client.calls[0][1])
