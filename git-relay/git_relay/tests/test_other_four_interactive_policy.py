import json
import unittest
from pathlib import Path

from git_relay.policy import SeatPolicy

EXPECTED = {
    "swu-jet": "JET",
    "swu-grok-web": "GROK-WEB",
    "swu-cursor": "CURSOR",
    "swu-claude": "CLAUDE",
}
ACTIONS = {
    "open_pr",
    "edit_pr",
    "reply_thread",
    "comment_pr",
    "merge_pr",
    "workflow_dispatch",
    "resolve_thread",
}


class Tests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "proposals"
                / "other-four-interactive-policy.proposal.json"
            ).read_text()
        )

    def test_exact_canonical_bindings_no_aliases(self):
        self.assertEqual(set(self.data), set(EXPECTED))
        for slug, identity in EXPECTED.items():
            self.assertEqual(self.data[slug]["identity"], identity)
            self.assertEqual(self.data[slug]["app_slug"], slug)
            self.assertEqual(self.data[slug]["auth"], "shared")

    def test_existing_seven_only(self):
        for cfg in self.data.values():
            self.assertEqual(set(cfg["actions"]), ACTIONS)

    def test_exact_repo_scope_shared_with_wise(self):
        wise = json.loads(
            (
                Path(__file__).resolve().parents[2]
                / "proposals"
                / "wise-interactive-policy.proposal.json"
            ).read_text()
        )["swu-wise"]
        for cfg in self.data.values():
            self.assertEqual(set(cfg["repos"]), set(wise["repos"]))
            self.assertEqual(len(cfg["repos"]), 18)
            self.assertIn("jaywedgeworth22/Fleet-OPS", cfg["repos"])
            self.assertFalse(any("*" in repo for repo in cfg["repos"]))

    def test_exact_minimum_aggregate_ceiling(self):
        for cfg in self.data.values():
            self.assertEqual(
                cfg["mint_permissions"],
                {
                    "contents": "write",
                    "pull_requests": "write",
                    "issues": "write",
                    "actions": "write",
                    "metadata": "read",
                },
            )
            self.assertNotIn("token", cfg)

    def test_proposal_parses_without_typed_actions(self):
        SeatPolicy(json.dumps(self.data))

    def test_real_policy_resolves_only_verified_identity(self):
        class Verifier:
            identity = None

            def parse_identity(self, token):
                return self.identity

            def resolve(self, token):
                return self.identity

        verifier = Verifier()
        policy = SeatPolicy(json.dumps(self.data), verifier=verifier)
        for slug, identity in EXPECTED.items():
            verifier.identity = identity
            self.assertEqual(policy.authenticate("mock-test-shared-bearer"), slug)
        for identity in ("JET-CHAT", "cursor-cloud", "ECHO", "WISE"):
            verifier.identity = identity
            self.assertIsNone(policy.authenticate("mock-test-shared-bearer"))
