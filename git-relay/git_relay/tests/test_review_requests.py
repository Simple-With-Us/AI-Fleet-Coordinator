import unittest

from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, SHA, Client, Minter, Policy


class Requests(unittest.TestCase):
    def run_change(self, operation, receipt, **extra):
        action = "wf_review_requests_" + operation
        c = Client({"number": 3, "head": {"sha": SHA}}, receipt)
        h = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", action, REPO)
        return h.run(action, {"repo": REPO, "number": 3, "expected_head_sha": SHA, **extra}), c

    def receipt(self, **extra):
        return {
            "number": 3,
            "head": {"sha": SHA},
            "requested_reviewers": [],
            "requested_teams": [],
            **extra,
        }

    def test_pending_users_and_teams_removed_no_submitted_review_endpoint(self):
        out, c = self.run_change(
            "remove", self.receipt(), reviewers=["bot"], team_reviewers=["devs"]
        )
        self.assertEqual(
            c.calls[-1],
            (
                "DELETE",
                f"/repos/{REPO}/pulls/3/requested_reviewers",
                {"reviewers": ["bot"], "team_reviewers": ["devs"]},
                "own",
            ),
        )
        self.assertEqual(
            out,
            {
                "number": 3,
                "head_sha": SHA,
                "operation": "remove",
                "reviewers": ["bot"],
                "team_reviewers": ["devs"],
                "atomic": False,
            },
        )
        self.assertNotIn("result", out)

    def test_request_and_rerequest_team_only_supported(self):
        for op in ["add", "rerequest"]:
            out, c = self.run_change(
                op,
                self.receipt(requested_teams=[{"slug": "devs", "description": "private"}]),
                team_reviewers=["devs"],
            )
            self.assertEqual(c.calls[-1][0], "POST")
            self.assertTrue(
                c.calls[-1][1].endswith(
                    "/rerequest" if op == "rerequest" else "/requested_reviewers"
                )
            )
            self.assertNotIn("private", str(out))

    def test_receipt_correlates_head_number_and_membership(self):
        for receipt in [
            self.receipt(number=4),
            self.receipt(head={"sha": "b" * 40}),
            self.receipt(requested_reviewers=[{"login": "bot"}]),
            self.receipt(requested_teams=None),
        ]:
            with self.assertRaises(RelayGitHubError):
                self.run_change("remove", receipt, reviewers=["bot"])
        with self.assertRaises(RelayGitHubError):
            self.run_change("add", self.receipt(), reviewers=["bot"])

    def test_invalid_inputs_no_network(self):
        for values in [
            {},
            {"reviewers": []},
            {"reviewers": ["bot", "BOT"]},
            {"team_reviewers": ["x/y"]},
            {"team_reviewers": ["devs[bot]"]},
            {"reviewers": ["x"] * 51},
            {"reviewers": ["sk-" + ("x" * 25)]},
        ]:
            action = "wf_review_requests_remove"
            c = Client()
            h = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", action, REPO)
            with self.assertRaises(Refused):
                h.run(action, {"repo": REPO, "number": 3, "expected_head_sha": SHA, **values})
            self.assertEqual(c.calls, [])

    def test_wrong_head_refuses_before_mutation(self):
        c = Client({"head": {"sha": "b" * 40}})
        a = "wf_review_requests_remove"
        h = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", a, REPO)
        with self.assertRaises(Refused):
            h.run(a, {"repo": REPO, "number": 3, "expected_head_sha": SHA, "reviewers": ["bot"]})
        self.assertEqual(len(c.calls), 1)

    def test_own_minimal_app_permissions(self):
        for a in ["add", "remove", "rerequest"]:
            m = Minter()
            WorkflowVerbs.for_action(
                Client(), Policy(), m, "swu-wise", "wf_review_requests_" + a, REPO
            )
            self.assertEqual(
                m.calls, [("swu-wise", REPO, {"pull_requests": "write", "metadata": "read"})]
            )
