import unittest

try:
    from .test_workflow_verbs import REPO, Client, Policy
except ImportError:
    from test_workflow_verbs import REPO, Client, Policy
from git_relay.workflow_verbs import WorkflowVerbs, Refused
from git_relay.github_client import RelayGitHubError


class Threads(unittest.TestCase):
    def test_membership_then_fixed_mutation(self):
        for resolved in (True, False):
            action = "wf_review_thread_resolve" if resolved else "wf_review_thread_unresolve"
            field = "resolveReviewThread" if resolved else "unresolveReviewThread"
            c = Client(
                {
                    "data": {
                        "repository": {
                            "pullRequest": {"reviewThreads": {"nodes": [{"id": "PRRT_fixture"}]}}
                        }
                    }
                },
                {"data": {field: {"thread": {"id": "PRRT_fixture", "isResolved": resolved}}}},
            )
            h = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", action, REPO)
            out = h.run(action, {"repo": REPO, "number": 1, "thread_id": "PRRT_fixture"})
            self.assertEqual(out, {"thread": {"id": "PRRT_fixture", "isResolved": resolved}})
            self.assertEqual(c.calls[0][2]["variables"]["number"], 1)
            self.assertEqual(c.calls[1][2]["variables"], {"id": "PRRT_fixture"})
            self.assertEqual(c.calls[1][3], "own")

    def test_foreign_thread_refused_before_mutation(self):
        c = Client({"data": {"repository": {"pullRequest": {"reviewThreads": {"nodes": []}}}}})
        h = WorkflowVerbs(
            c, Policy(), "swu-wise", "own", "swu-wise", "wf_review_thread_resolve", REPO
        )
        with self.assertRaises(Refused):
            h.run(
                "wf_review_thread_resolve", {"repo": REPO, "number": 1, "thread_id": "PRRT_foreign"}
            )
        self.assertEqual(len(c.calls), 1)

    def test_graphql_mutation_errors_unknown(self):
        c = Client(
            {
                "data": {
                    "repository": {
                        "pullRequest": {"reviewThreads": {"nodes": [{"id": "PRRT_fixture"}]}}
                    }
                }
            },
            {"errors": [{"message": "private"}]},
        )
        h = WorkflowVerbs(
            c, Policy(), "swu-wise", "own", "swu-wise", "wf_review_thread_resolve", REPO
        )
        with self.assertRaises(RelayGitHubError) as error:
            h.run(
                "wf_review_thread_resolve", {"repo": REPO, "number": 1, "thread_id": "PRRT_fixture"}
            )
        self.assertEqual(error.exception.status, 502)

    def test_required_permissions_do_not_widen_seat(self):
        try:
            from .test_workflow_verbs import Minter
        except ImportError:
            from test_workflow_verbs import Minter
        for action in ("wf_review_thread_resolve", "wf_review_thread_unresolve"):
            with self.assertRaises(Refused):
                WorkflowVerbs.for_action(
                    Client(),
                    Policy({"pull_requests": "write", "contents": "read"}),
                    Minter(),
                    "swu-wise",
                    action,
                    REPO,
                )
            m = Minter()
            WorkflowVerbs.for_action(Client(), Policy(), m, "swu-wise", action, REPO)
            self.assertEqual(
                m.calls[0][2], {"pull_requests": "write", "contents": "write", "metadata": "read"}
            )
