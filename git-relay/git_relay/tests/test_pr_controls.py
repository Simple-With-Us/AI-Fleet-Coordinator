import unittest

try:
    from .test_workflow_verbs import REPO, Client, Policy
except ImportError:
    from test_workflow_verbs import REPO, Client, Policy
from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import Refused, WorkflowVerbs


class PrControls(unittest.TestCase):
    def test_fixed_graphql_and_repo_preread(self):
        for action, field in [
            ("wf_pr_draft", "convertPullRequestToDraft"),
            ("wf_pr_ready", "markPullRequestReadyForReview"),
            ("wf_automerge_disable", "disablePullRequestAutoMerge"),
            ("wf_automerge_enable", "enablePullRequestAutoMerge"),
        ]:
            pull = {
                "id": "PR_abc",
                "isDraft": action == "wf_pr_draft",
                "autoMergeRequest": {"enabledAt": "now"}
                if action == "wf_automerge_enable"
                else None,
            }
            c = Client({"node_id": "PR_abc"}, {"data": {field: {"pullRequest": pull}}})
            h = WorkflowVerbs(c, Policy(), "swu-wise", "private", "swu-wise", action, REPO)
            args = {
                "repo": REPO,
                "number": 1,
                **({"method": "SQUASH"} if action == "wf_automerge_enable" else {}),
            }
            self.assertEqual(h.run(action, args), {"pull": pull})
            self.assertEqual(c.calls[0][1], "/repos/" + REPO + "/pulls/1")
            self.assertEqual(c.calls[1][1], "/graphql")
            self.assertEqual(c.calls[1][2]["variables"]["id"], "PR_abc")
            self.assertEqual(c.calls[1][3], "private")
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(action, {**args, "query": "arbitrary"}, REPO)

    def test_graphql_errors_are_unknown_not_definitely_unsent(self):
        c = Client({"node_id": "PR_abc"}, {"data": {}, "errors": [{"message": "private"}]})
        h = WorkflowVerbs(c, Policy(), "swu-wise", "private", "swu-wise", "wf_pr_ready", REPO)
        with self.assertRaises(RelayGitHubError) as error:
            h.run("wf_pr_ready", {"repo": REPO, "number": 1})
        self.assertEqual(error.exception.status, 502)

    def test_threads_query_fixed_and_read_only(self):
        c = Client(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {"nodes": [], "pageInfo": {"hasNextPage": False}}
                        }
                    }
                }
            }
        )
        h = WorkflowVerbs(
            c, Policy(), "swu-wise", "private", "swu-wise", "wf_review_threads", "Example/demo"
        )
        self.assertIn("threads", h.run("wf_review_threads", {"repo": "Example/demo", "number": 1}))
        self.assertEqual(
            c.calls[0][2]["variables"],
            {"owner": "Example", "name": "demo", "number": 1, "cursor": None},
        )

    def test_invalid_method_refused_before_preread(self):
        c = Client()
        h = WorkflowVerbs(
            c, Policy(), "swu-wise", "private", "swu-wise", "wf_automerge_enable", REPO
        )
        with self.assertRaises(Refused):
            h.run("wf_automerge_enable", {"repo": REPO, "number": 1, "method": "INVALID"})
        self.assertEqual(c.calls, [])

    def test_base64_node_slash_and_wrong_draft_receipt(self):
        c = Client(
            {"node_id": "PR/abc="},
            {
                "data": {
                    "markPullRequestReadyForReview": {
                        "pullRequest": {"id": "PR/abc=", "isDraft": True}
                    }
                }
            },
        )
        h = WorkflowVerbs(c, Policy(), "swu-wise", "private", "swu-wise", "wf_pr_ready", REPO)
        with self.assertRaises(RelayGitHubError):
            h.run("wf_pr_ready", {"repo": REPO, "number": 1})
        self.assertEqual(len(c.calls), 2)
