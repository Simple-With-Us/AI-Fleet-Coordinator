import unittest

try:
    from .test_workflow_verbs import REPO, SHA, Client, Policy
except ImportError:
    from test_workflow_verbs import REPO, SHA, Client, Policy

from git_relay.workflow_verbs import Refused, WorkflowVerbs

COMMIT = {"sha": SHA, "commit": {"message": "fixture"}, "parents": []}
PULL = {"id": 4, "number": 4, "state": "open", "head": {"sha": SHA}, "base": {"sha": SHA}}
CASES = [
    ("wf_pr_get", {"number": 4}, "/pulls/4", PULL, "pull"),
    ("wf_pr_reviews", {"number": 4}, "/pulls/4/reviews?per_page=30", [], "reviews"),
    ("wf_pr_comments", {"number": 4}, "/pulls/4/comments?per_page=30", [], "comments"),
    ("wf_issue_comments", {"number": 4}, "/issues/4/comments?per_page=30", [], "comments"),
    ("wf_commit_get", {"sha": SHA}, "/commits/" + SHA, COMMIT, "commit"),
    (
        "wf_compare",
        {"base": "main", "head": "feature/x"},
        "/compare/main...feature%2Fx",
        {"status": "ahead", "ahead_by": 0, "behind_by": 0, "total_commits": 0, "commits": []},
        "comparison",
    ),
    ("wf_branch_get", {"branch": "feature/x"}, "/branches/feature%2Fx", {"name":"feature/x","commit":{"sha":SHA}}, "branch"),
    ("wf_repo_get", {}, "", {"id":1,"name":"fixture","full_name":"fixture-org/fixture","default_branch":"main","private":True}, "repository"),
    ("wf_labels", {}, "/labels?per_page=30", [], "labels"),
    ("wf_workflows", {}, "/actions/workflows?per_page=30", {"total_count":0,"workflows":[]}, "workflows"),
    ("wf_run_get", {"run_id": 9}, "/actions/runs/9", {"id":9,"head_sha":SHA,"status":"completed","conclusion":"success"}, "run"),
    ("wf_run_artifacts", {"run_id": 9}, "/actions/runs/9/artifacts?per_page=30", {"total_count":0,"artifacts":[]}, "artifacts"),
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

    def test_new_reads_reject_empty_and_nested_invalid_receipts(self):
        from git_relay.workflow_verbs import _validate_read
        from git_relay.github_client import RelayGitHubError

        for action, result in [("wf_pr_get", {"pull": {}}), ("wf_pr_reviews", {"reviews": [None], "complete": False}), ("wf_workflows", {"workflows": {"arbitrary": True}}), ("wf_run_artifacts", {"artifacts": {"total_count": 1, "artifacts": [{}]}})]:
            with self.assertRaises(RelayGitHubError):
                _validate_read(action, result)
