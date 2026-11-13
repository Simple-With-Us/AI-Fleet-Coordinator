"""Repository and administration read verbs: shape validation and refusal cases."""

import unittest

try:
    from .test_workflow_verbs import CEILING, REPO, Client, Minter, Policy
except ImportError:
    from test_workflow_verbs import CEILING, REPO, Client, Minter, Policy

from git_relay import workflow_verbs
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
    def setUp(self):
        workflow_verbs._INSTALLATION_REPOS_CACHE.clear()

    def test_check_shape_accepts_valid_new_reads(self):
        # Regression (Seer HIGH): check_shape runs each verb against a probe
        # whose policy is a stand-in;  wf_pr_search consults the seat scope
        # before any upstream call and must stop at that probe, not crash.
        for action in ("wf_pr_search", "wf_repo_list"):
            self.assertIsNone(WorkflowVerbs.check_shape(action, {"repo": REPO}, REPO))

    def test_check_shape_still_refuses_bad_new_read_args(self):
        with self.assertRaises(Refused):
            WorkflowVerbs.check_shape("wf_pr_search", {"repo": REPO, "state": "bogus"}, REPO)
        with self.assertRaises(Refused):
            WorkflowVerbs.check_shape("wf_pr_search", {"repo": REPO, "unexpected": 1}, REPO)
        with self.assertRaises(Refused):
            WorkflowVerbs.check_shape("wf_repo_list", {"repo": REPO, "page": 0}, REPO)

    def test_installation_repos_cache_serialized_under_threads(self):
        # Regression (Seer LOW): the relay serves on a threading server;  with
        # an instantly-stale TTL every call sweeps and stores, so concurrent
        # access without the cache lock raises RuntimeError/KeyError here.
        import threading

        old_ttl = workflow_verbs._INSTALLATION_REPOS_TTL_S
        workflow_verbs._INSTALLATION_REPOS_TTL_S = 0.0
        handle = WorkflowVerbs.__new__(WorkflowVerbs)
        handle.bearer = "probe-bearer"
        fetches = []
        errors = []

        def fake_get(path):
            fetches.append(path)
            return {"total_count": 1, "repositories": [REPO_ITEM]}

        handle._get = fake_get

        def hammer():
            for _ in range(50):
                try:
                    handle._installation_repositories()
                except Exception as exc:  # record any race failure
                    errors.append(exc)

        try:
            threads = [threading.Thread(target=hammer) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        finally:
            workflow_verbs._INSTALLATION_REPOS_TTL_S = old_ttl
        self.assertEqual(errors, [])
        self.assertGreaterEqual(len(fetches), 1)

    def run_action(self, action, args, *replies, repos=(REPO,)):
        client = Client(*replies)
        ceiling = {**CEILING, "metadata": "read", "pull_requests": "read"}
        handle = WorkflowVerbs.for_action(
            client, Policy(ceiling, repos=repos), Minter(), "swu-wise", action, REPO
        )
        return handle.run(action, {"repo": REPO, **args}), client

    def test_repo_list(self):
        out, client = self.run_action(
            "wf_repo_list", {}, {"total_count": 1, "repositories": [REPO_ITEM]}
        )
        self.assertEqual(client.calls[0][1], "/installation/repositories?per_page=50&page=1")
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
        self.assertIn(
            "repo%3ASimple-With-Us%2FAI-Fleet-Coordinator%20type%3Apr%20state%3Aopen",
            client.calls[0][1],
        )
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

    def test_repo_list_filters_to_seat_allowlist(self):
        foreign = dict(REPO_ITEM, id=12, full_name="Simple-With-Us/Other-Repo")
        out, _ = self.run_action(
            "wf_repo_list", {}, {"total_count": 2, "repositories": [REPO_ITEM, foreign]}
        )
        self.assertEqual([r["full_name"] for r in out["repositories"]], [REPO])
        self.assertEqual(out["total_count"], 1)

    def test_repo_list_wildcard_seat_keeps_org_repos(self):
        foreign = dict(REPO_ITEM, id=12, full_name="Simple-With-Us/Other-Repo")
        out, _ = self.run_action(
            "wf_repo_list",
            {},
            {"total_count": 2, "repositories": [REPO_ITEM, foreign]},
            repos=("Simple-With-Us/*",),
        )
        self.assertEqual(len(out["repositories"]), 2)
        self.assertEqual(out["total_count"], 2)

    def test_pr_search_wildcard_seat_uses_org_scope(self):
        _, client = self.run_action(
            "wf_pr_search",
            {},
            {"total_count": 0, "incomplete_results": False, "items": []},
            repos=("Simple-With-Us/*",),
        )
        self.assertIn("org%3ASimple-With-Us%20type%3Apr", client.calls[0][1])

    def test_pr_search_refuses_item_outside_seat_scope(self):
        foreign = dict(
            ITEM,
            html_url="https://github.com/Simple-With-Us/Other-Repo/pull/31",
            repository_url="https://api.github.com/repos/Simple-With-Us/Other-Repo",
        )
        with self.assertRaises(RelayGitHubError):
            self.run_action(
                "wf_pr_search",
                {},
                {"total_count": 1, "incomplete_results": False, "items": [foreign]},
            )

    def run_action_shared(self, client, action, args, repos=(REPO,)):
        ceiling = {**CEILING, "metadata": "read", "pull_requests": "read"}
        handle = WorkflowVerbs.for_action(
            client, Policy(ceiling, repos=repos), Minter(), "swu-wise", action, REPO
        )
        return handle.run(action, {"repo": REPO, **args})

    def test_repo_list_caches_installation_scan(self):
        client = Client({"total_count": 1, "repositories": [REPO_ITEM]})
        first = self.run_action_shared(client, "wf_repo_list", {})
        second = self.run_action_shared(client, "wf_repo_list", {})
        scans = [c for c in client.calls if c[1].startswith("/installation/repositories")]
        self.assertEqual(len(scans), 1)
        self.assertEqual(second, first)

    def test_repo_list_cache_expires(self):
        reply = {"total_count": 1, "repositories": [REPO_ITEM]}
        client = Client(reply, dict(reply))
        self.run_action_shared(client, "wf_repo_list", {})
        for key, (_, rows) in list(workflow_verbs._INSTALLATION_REPOS_CACHE.items()):
            workflow_verbs._INSTALLATION_REPOS_CACHE[key] = (0.0, rows)
        self.run_action_shared(client, "wf_repo_list", {})
        scans = [c for c in client.calls if c[1].startswith("/installation/repositories")]
        self.assertEqual(len(scans), 2)

    def test_repo_list_policy_filter_runs_per_call(self):
        other = dict(REPO_ITEM, id=12, full_name="Simple-With-Us/other-repo")
        client = Client({"total_count": 2, "repositories": [REPO_ITEM, other]})
        out = self.run_action_shared(client, "wf_repo_list", {})
        self.assertEqual([r["full_name"] for r in out["repositories"]], [REPO])
        out2 = self.run_action_shared(client, "wf_repo_list", {})
        self.assertEqual([r["full_name"] for r in out2["repositories"]], [REPO])


if __name__ == "__main__":
    unittest.main()
