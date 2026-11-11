"""The proposed interactive Wise policy parses through SeatPolicy and stays inside its scope.

Offline: reads the proposal file only.  Nothing here admits the policy anywhere.
"""

from __future__ import annotations

import json
import pathlib
import unittest

from git_relay.policy import SeatPolicy

PROPOSAL = (
    pathlib.Path(__file__).resolve().parents[2]
    / "proposals"
    / "wise-interactive-policy.proposal.json"
)
SEVEN = {
    "open_pr",
    "edit_pr",
    "reply_thread",
    "comment_pr",
    "merge_pr",
    "workflow_dispatch",
    "resolve_thread",
}

EXPECTED_REPOS = {
    "Simple-With-Us/AI-Fleet-Coordinator",
    "Simple-With-Us/Autorotate",
    "Simple-With-Us/BotFleet",
    "Simple-With-Us/Clutch",
    "Simple-With-Us/CodeCaps",
    "Simple-With-Us/Congress.Trade",
    "Simple-With-Us/ContactLogo",
    "Simple-With-Us/DealDex",
    "Simple-With-Us/FleetLink",
    "Simple-With-Us/HogHunter",
    "Simple-With-Us/Kodus-Config",
    "Simple-With-Us/Personal-Site",
    "Simple-With-Us/Simple-With-Us",
    "Simple-With-Us/Socratic-Trade",
    "Simple-With-Us/Usage-Monitor",
    "Simple-With-Us/congress-trading-shared",
    "Simple-With-Us/homebrew-tap",
    "jaywedgeworth22/Fleet-OPS",
}
EXPECTED_SEAT_KEYS = {"auth", "identity", "actions", "repos", "app_slug", "mint_permissions"}


class InteractivePolicyProposalTest(unittest.TestCase):
    def setUp(self):
        self.raw = PROPOSAL.read_text()
        self.cfg = json.loads(self.raw)["swu-wise"]

    def test_parses_through_seat_policy(self):
        SeatPolicy(self.raw)

    def test_exactly_the_existing_seven_actions(self):
        self.assertEqual(set(self.cfg["actions"]), SEVEN)
        self.assertEqual(len(self.cfg["actions"]), len(set(self.cfg["actions"])))
        for action in self.cfg["actions"]:
            self.assertIn(action, SeatPolicy.ALL_ACTIONS)
            self.assertNotIn(action, SeatPolicy.REGISTERED_ACTIONS)

    def test_one_seat_one_identity_shared_auth(self):
        self.assertEqual(set(self.cfg), EXPECTED_SEAT_KEYS)
        self.assertEqual(list(json.loads(self.raw)), ["swu-wise"])
        self.assertEqual(self.cfg["auth"], "shared")
        self.assertEqual(self.cfg["identity"], "WISE")
        self.assertEqual(self.cfg["app_slug"], "swu-wise")
        self.assertNotIn("token", self.cfg)

    def test_repos_are_the_org_repos_plus_the_one_personal_repo(self):
        repos = self.cfg["repos"]
        self.assertEqual(set(repos), EXPECTED_REPOS)
        self.assertEqual(len(repos), 18)
        self.assertEqual(len(set(repos)), 18)
        personal = [r for r in repos if not r.startswith("Simple-With-Us/")]
        self.assertEqual(personal, ["jaywedgeworth22/Fleet-OPS"])
        org = [r for r in repos if r.startswith("Simple-With-Us/")]
        self.assertEqual(len(org), 17)
        for repo in repos:
            self.assertRegex(repo, r"^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$")
        self.assertNotIn("Simple-With-Us/Fleet-OPS", repos)

    def test_mint_ceiling_has_no_checks_or_admin(self):
        self.assertEqual(
            self.cfg["mint_permissions"],
            {
                "contents": "write",
                "pull_requests": "write",
                "issues": "write",
                "actions": "write",
                "metadata": "read",
            },
        )


if __name__ == "__main__":
    unittest.main()
