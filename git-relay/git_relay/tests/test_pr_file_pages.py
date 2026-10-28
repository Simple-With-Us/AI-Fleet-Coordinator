import unittest
from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import Refused, WorkflowVerbs
from .test_workflow_verbs import REPO, SHA, Client, Policy, Minter


class Files(unittest.TestCase):
    def args(self, **kw):
        return {"repo": REPO, "number": 2, "expected_head_sha": SHA, **kw}

    def pr(self, sha=SHA):
        return {"number": 2, "head": {"sha": sha}}

    def row(self, **kw):
        return {
            "sha": "b" * 40,
            "filename": "src/new.txt",
            "previous_filename": "src/old.txt",
            "status": "renamed",
            "additions": 1,
            "deletions": 1,
            "changes": 2,
            "patch": "untrusted omitted",
            "raw_url": "omitted",
            **kw,
        }

    def handle(self, c):
        return WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", "wf_pr_files_page", REPO)

    def test_metadata_page_before_after_head_and_permission(self):
        c = Client(self.pr(), [self.row()], self.pr())
        out = self.handle(c).run("wf_pr_files_page", self.args(page=2, per_page=1))
        self.assertTrue(out["has_more"])
        self.assertFalse(out["history_complete"])
        self.assertNotIn("patch", out["files"][0])
        self.assertEqual(
            c.calls[1], ("GET", f"/repos/{REPO}/pulls/2/files?per_page=1&page=2", None, "own")
        )
        m = Minter()
        WorkflowVerbs.for_action(c, Policy(), m, "swu-wise", "wf_pr_files_page", REPO)
        self.assertEqual(m.calls[0][2], {"pull_requests": "read", "metadata": "read"})

    def test_head_moves_or_malformed_rows_refuse(self):
        c = Client(self.pr(), [self.row()], self.pr("c" * 40))
        with self.assertRaises(Refused):
            self.handle(c).run("wf_pr_files_page", self.args())
        for row in [
            self.row(sha=True),
            self.row(additions=-1),
            self.row(previous_filename=None),
            self.row(status="unknown"),
        ]:
            with self.assertRaises(RelayGitHubError):
                self.handle(Client(self.pr(), [row], self.pr())).run(
                    "wf_pr_files_page", self.args()
                )

    def test_closed_page_bounds(self):
        for bad in [{"page": 101}, {"per_page": 51}, {"patch": True}]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape("wf_pr_files_page", self.args(**bad), REPO)

    def test_path_credential_refused_and_empty_page_not_history_complete(self):
        # Synthetic assembled fixture only, never a credential.
        with self.assertRaises(Refused):
            self.handle(Client(self.pr(), [self.row(filename="ghp_" + "x" * 36)])).run(
                "wf_pr_files_page", self.args()
            )
        out = self.handle(Client(self.pr(), [], self.pr())).run("wf_pr_files_page", self.args())
        self.assertFalse(out["has_more"])
        self.assertFalse(out["history_complete"])
