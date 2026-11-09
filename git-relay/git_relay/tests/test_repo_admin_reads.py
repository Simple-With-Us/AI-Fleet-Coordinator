"""Repository and administration read verbs: shape validation and refusal cases."""

import unittest

try:
    from .test_workflow_verbs import CEILING, REPO, Client, Minter, Policy
except ImportError:
    from test_workflow_verbs import CEILING, REPO, Client, Minter, Policy

from git_relay.workflow_verbs import Refused, RelayGitHubError, WorkflowVerbs

REPO_ITEM = {
    "id": 11,
    "full_name": REPO,
    "private": True,
    "default_branch": "main",
}
INSTALL = {
    "id": 21,
    "app_slug": "swu-echo",
    "account": {"login": "Simple-With-Us"},
}
ITEM = {
    "number": 31,
    "title": "fixture pull request",
    "state": "open",
    "draft": False,
    "updated_at": "2026-01-01T00:00:00Z",
    "html_url": "https://github.com/" + REPO + "/pull/31",
    "repository_url": "https://api.github.com/repos/" + REPO,
    "pull_request": {"url": "https://api.github.com/repos/" + REPO + "/pulls/31"},
}


class RepoAdminReadTests(unittest.TestCase):
    def run_action(self, action, args, *replies):
        client = Client(*replies)
        ceiling = {**CEILING, "metadata": "read", "pull_requests": "read"}
        handle = WorkflowVerbs.for_action(
            client, Policy(ceiling), Minter(), "swu-wise", action, REPO
        )
        return handle.run(action, {"repo": REPO, **args}), client

    def test_repo_list(self):
        out, client = self.run_action(
            "wf_repo_list", {}, {"total_count": 1, "repositories": [REPO_ITEM]}
        )
        self.assertEqual(client.calls[0][1], "/installation/repositories?per_page=30&page=1")
        self.assertEqual(out["repositories"][0]["default_branch"], "main")
        self.assertFalse(out["has_more"])

    def test_repo_list_allows_empty_default_branch(self):
        item = dict(REPO_ITEM, default_branch="")
        out, _ = self.run_action("wf_repo_list", {}, {"total_count": 1, "repositories": [item]})
        self.assertEqual(out["repositories"][0]["default_branch"], "")

    def test_repo_list_refuses_bad_full_name(self):
        item = dict(REPO_ITEM, full_name="not a repo")
        with self.assertRaises(RelayGitHubError):
            self.run_action("wf_repo_list", {}, {"total_count": 1, "repositories": [item]})

    def test_pr_search(self):
        out, client = self.run_action(
            "wf_pr_search",
            {},
            {"total_count": 1, "incomplete_results": False, "items": [ITEM]},
        )
        self.assertIn("org%3ASimple-With-Us%20type%3Apr%20state%3Aopen", client.calls[0][1])
        self.assertEqual(out["pull_requests"][0]["number"], 31)
        self.assertTrue(out["complete"])

    def test_pr_search_state_all_omits_filter(self):
        _, client = self.run_action(
            "wf_pr_search",
            {"state": "all"},
            {"total_count": 0, "incomplete_results": False, "items": []},
        )
        self.assertNotIn("state%3A", client.calls[0][1])

    def test_pr_search_flags_incomplete_results(self):
        out, _ = self.run_action(
            "wf_pr_search",
            {},
            {"total_count": 1, "incomplete_results": True, "items": [ITEM]},
        )
        self.assertFalse(out["complete"])

    def test_pr_search_refuses_issue_without_pull_request(self):
        item = {k: v for k, v in ITEM.items() if k != "pull_request"}
        with self.assertRaises(RelayGitHubError):
            self.run_action(
                "wf_pr_search",
                {},
                {"total_count": 1, "incomplete_results": False, "items": [item]},
            )

    def test_pr_search_rejects_page_over_cap(self):
        with self.assertRaises(Refused):
            self.run_action(
                "wf_pr_search",
                {"page": 21},
                {"total_count": 0, "incomplete_results": False, "items": []},
            )


if __name__ == "__main__":
    unittest.main()
