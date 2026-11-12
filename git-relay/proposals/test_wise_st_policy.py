"""Offline proposal checks. Does not load or change deployed policy."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from git_relay.policy import PolicyError, SeatPolicy


class WiseSTProposal(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads(Path(__file__).with_name("wise-st-policy.json").read_text())
        self.policy = SeatPolicy(json.dumps(self.raw))

    def test_exact_scope_only(self):
        snapshot = json.loads(Path(__file__).with_name("wise-repo-snapshot.json").read_text())
        repos = [r["full_name"] for r in snapshot["repositories"]]
        self.assertEqual(sorted(self.raw["swu-wise"]["repos"]), sorted(repos))
        self.assertEqual(len(repos), 17)
        for repo in repos:
            self.assertTrue(repo.startswith("Simple-With-Us/"))
            self.assertNotIn("*", repo)
            self.assertIsNone(self.policy.authorize("swu-wise", "mint_token", repo))
        for repo in ["other/Socratic-Trade", "Simple-With-Us/new-unverified-repo"]:
            self.assertIsNotNone(self.policy.authorize("swu-wise", "mint_token", repo))

    def test_no_owner_or_unrelated_action(self):
        for action in ["open_pr", "workflow_dispatch", "merge_pr", "set_branch_protection"]:
            self.assertIsNotNone(
                self.policy.authorize("swu-wise", action, "Simple-With-Us/Socratic-Trade")
            )

    def test_no_other_identity(self):
        self.raw["swu-wise"]["app_slug"] = "swu-echo"
        with self.assertRaises(PolicyError):
            SeatPolicy(json.dumps(self.raw))

    def test_no_static_or_oauth_credential_assumption(self):
        self.assertIsNone(self.policy.authenticate("unprovisioned-placeholder"))
        self.assertNotIn("token", self.raw["swu-wise"])

    def test_checks_read_is_explicit_but_no_workflows_expansion(self):
        self.assertEqual(self.raw["swu-wise"]["mint_permissions"]["checks"], "read")
        self.raw["swu-wise"]["mint_permissions"]["workflows"] = "write"
        with self.assertRaises(PolicyError):
            SeatPolicy(json.dumps(self.raw))


if __name__ == "__main__":
    unittest.main()
