import unittest

from test_workflow_verbs import REPO, SHA, Client, Policy

from git_relay.workflow_verbs import Refused, WorkflowVerbs

CASES = [
    ("wf_pr_get", {"number": 4}, "/pulls/4", {"number": 4}, "pull"),
    ("wf_pr_reviews", {"number": 4}, "/pulls/4/reviews?per_page=30", [], "reviews"),
    ("wf_pr_comments", {"number": 4}, "/pulls/4/comments?per_page=30", [], "comments"),
    ("wf_issue_comments", {"number": 4}, "/issues/4/comments?per_page=30", [], "comments"),
    ("wf_commit_get", {"sha": SHA}, "/commits/" + SHA, {}, "commit"),
    (
        "wf_compare",
        {"base": "main", "head": "feature/x"},
        "/compare/main...feature%2Fx",
        {},
        "comparison",
    ),
    ("wf_branch_get", {"branch": "feature/x"}, "/branches/feature%2Fx", {}, "branch"),
    ("wf_repo_get", {}, "", {}, "repository"),
    ("wf_labels", {}, "/labels?per_page=30", [], "labels"),
    ("wf_workflows", {}, "/actions/workflows?per_page=30", {}, "workflows"),
    ("wf_run_get", {"run_id": 9}, "/actions/runs/9", {}, "run"),
    ("wf_run_artifacts", {"run_id": 9}, "/actions/runs/9/artifacts?per_page=30", {}, "artifacts"),
]


class ReadCoverage(unittest.TestCase):
    def test_fixed_endpoints_and_own_bearer(self):
        for action, args, path, reply, key in CASES:
            with self.subTest(action=action):
                client = Client(reply)
                h = WorkflowVerbs(client, Policy(), "swu-wise", "private", "swu-wise", action, REPO)
                out = h.run(action, {"repo": REPO, **args})
                self.assertIn(key, out)
                self.assertEqual(client.calls, [("GET", "/repos/" + REPO + path, None, "private")])

    def test_reject_unknown_arguments_and_bad_refs_before_network(self):
        for action, args, *_ in CASES:
            client = Client()
            h = WorkflowVerbs(client, Policy(), "swu-wise", "private", "swu-wise", action, REPO)
            with self.assertRaises(Refused):
                h.run(action, {"repo": REPO, **args, "url": "https://evil.invalid"})
            self.assertFalse(client.calls)
        with self.assertRaises(Refused):
            WorkflowVerbs.check_shape(
                "wf_compare", {"repo": REPO, "base": "../x", "head": "main"}, REPO
            )
