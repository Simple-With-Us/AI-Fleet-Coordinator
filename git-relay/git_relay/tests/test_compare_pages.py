import unittest
from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import Refused, WorkflowVerbs
from .test_workflow_verbs import REPO, SHA, Client, Policy, Minter

HEAD = "b" * 40


class ComparePages(unittest.TestCase):
    def args(self, **kw):
        return {"repo": REPO, "base_sha": SHA, "head_sha": HEAD, **kw}

    def row(self, **kw):
        return {
            "status": "ahead",
            "base_commit": {"sha": SHA},
            "merge_base_commit": {"sha": SHA},
            "ahead_by": 3,
            "behind_by": 0,
            "total_commits": 3,
            "commits": [{"sha": HEAD, "parents": [{"sha": SHA}], "commit": {"message": "omitted"}}],
            "files": [{"filename": "not included"}],
            **kw,
        }

    def handle(self, c):
        return WorkflowVerbs(
            c, Policy(), "swu-wise", "own", "swu-wise", "wf_compare_commits_page", REPO
        )

    def test_exact_sha_graph_page_and_own_permission(self):
        c = Client(self.row())
        out = self.handle(c).run("wf_compare_commits_page", self.args(page=2, per_page=1))
        self.assertTrue(out["has_more"])
        self.assertEqual(out["merge_base_sha"], SHA)
        self.assertNotIn("files", out)
        self.assertEqual(out["commits"], [{"sha": HEAD, "parents": [SHA]}])
        self.assertEqual(
            c.calls,
            [("GET", f"/repos/{REPO}/compare/{SHA}...{HEAD}?per_page=1&page=2", None, "own")],
        )
        m = Minter()
        WorkflowVerbs.for_action(c, Policy(), m, "swu-wise", "wf_compare_commits_page", REPO)
        self.assertEqual(m.calls[0][2], {"contents": "read", "metadata": "read"})

    def test_malformed_or_mismatched_receipt_refused(self):
        for bad in [
            self.row(base_commit={"sha": HEAD}),
            self.row(merge_base_commit={}),
            self.row(total_commits=True),
            self.row(commits=[{"sha": True, "parents": []}]),
            self.row(commits=[{"sha": HEAD, "parents": []}] * 2),
        ]:
            with self.assertRaises(RelayGitHubError):
                self.handle(Client(bad)).run("wf_compare_commits_page", self.args())

    def test_empty_page_not_false_full_history(self):
        out = self.handle(Client(self.row(commits=[]))).run(
            "wf_compare_commits_page", self.args(page=2)
        )
        self.assertFalse(out["has_more"])
        self.assertEqual(out["total_commits"], 3)

    def test_closed_args(self):
        for bad in [{"page": 1001}, {"per_page": 51}, {"base_sha": "main"}, {"files": True}]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape("wf_compare_commits_page", self.args(**bad), REPO)
