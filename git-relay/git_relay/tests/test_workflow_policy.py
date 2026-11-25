import json
import unittest

from git_relay.policy import PolicyError, SeatPolicy
from git_relay.server import _validate_args
from git_relay.workflow_verbs import SPECS

TOKEN = "w" * 32


def seats(actions, **extra):
    cfg = {"token": TOKEN, "actions": actions, "repos": ["example/*"], **extra}
    return json.dumps({"swu-wise": cfg})


class WorkflowPolicyTests(unittest.TestCase):
    def test_every_workflow_spec_has_a_policy_action_name(self):
        self.assertEqual(set(SPECS), set(SeatPolicy.WORKFLOW_ACTIONS))
        self.assertEqual(len(SeatPolicy.WORKFLOW_ACTIONS), len(set(SeatPolicy.WORKFLOW_ACTIONS)))
        self.assertTrue(all(a.startswith("wf_") for a in SeatPolicy.WORKFLOW_ACTIONS))

    def test_a_seat_may_list_workflow_actions_only_with_its_own_app(self):
        with self.assertRaises(PolicyError):
            SeatPolicy(seats(["wf_checks"]))
        policy = SeatPolicy(
            seats(["wf_checks"], app_slug="swu-wise", mint_permissions={"checks": "read"})
        )
        self.assertIsNone(policy.authorize("swu-wise", "wf_checks", "example/r"))
        # The retired per-seat allowlist no longer gates unlisted actions.
        self.assertIsNone(policy.authorize("swu-wise", "wf_files_put", "example/r"))

    def test_other_bots_app_still_refused_for_workflow_actions(self):
        with self.assertRaises(PolicyError):
            SeatPolicy(
                seats(["wf_checks"], app_slug="swu-echo", mint_permissions={"checks": "read"})
            )

    def test_unknown_workflow_name_is_refused_at_config_load(self):
        with self.assertRaises(PolicyError):
            SeatPolicy(seats(["wf_nope"], app_slug="swu-wise"))

    def test_an_actions_less_seat_may_run_every_action_through_its_own_bearer(self):
        policy = SeatPolicy(
            json.dumps(
                {
                    "swu-wise": {
                        "token": TOKEN,
                        "repos": ["example/*"],
                        "app_slug": "swu-wise",
                        "mint_permissions": {"checks": "read"},
                    }
                }
            ),
            rate_per_hour=100000,
            write_rate_per_hour=100000,
        )
        for action in sorted(SeatPolicy.ALL_ACTIONS):
            if action == "wf_pr_create":
                continue  # owner-only open_pr rule
            if action == "create_repo":
                continue  # still an explicit per-seat entitlement (namespace mint)
            with self.subTest(action=action):
                self.assertIsNone(policy.authorize("swu-wise", action, "example/r"))

    def test_workflow_actions_stay_unwired_in_the_http_server(self):
        for action in SeatPolicy.WORKFLOW_ACTIONS:
            with self.assertRaises(ValueError, msg=action):
                _validate_args(action, {"repo": "example/r"})


if __name__ == "__main__":
    unittest.main()
