import unittest

try:
    from .test_workflow_verbs import REPO, SHA, Client, Policy
except ImportError:
    from test_workflow_verbs import REPO, SHA, Client, Policy
from git_relay.github_client import RelayGitHubError
from git_relay.server import _REAPPLY_STATE_ACTIONS, PreReadUnavailable
from git_relay.workflow_verbs import Refused, WorkflowVerbs


class Delete(unittest.TestCase):
    def args(self, **kw):
        return {
            "repo": REPO,
            "path": "old/file.txt",
            "branch": "feature/x",
            "message": "Remove obsolete file",
            "prior_sha": SHA,
            **kw,
        }

    def handle(self, c):
        return WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", "wf_file_delete", REPO)

    def test_delete_exact_blob_on_nondefault_branch(self):
        c = Client({"default_branch": "main"}, {"content": None, "commit": {"sha": "b" * 40}})
        out = self.handle(c).run("wf_file_delete", self.args())
        self.assertEqual(out["commit"], "b" * 40)
        self.assertEqual(
            c.calls[-1],
            (
                "DELETE",
                "/repos/" + REPO + "/contents/old/file.txt",
                {"message": "Remove obsolete file", "sha": SHA, "branch": "feature/x"},
                "own",
            ),
        )
        self.assertNotIn("wf_file_delete", _REAPPLY_STATE_ACTIONS)

    def test_default_branch_and_workflow_refused(self):
        c = Client({"default_branch": "main"})
        with self.assertRaises(Refused):
            self.handle(c).run("wf_file_delete", self.args(branch="main"))
        self.assertEqual(len(c.calls), 1)
        for path in [".github", ".github/workflows", ".github/workflows/ci.yml"]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape("wf_file_delete", self.args(path=path), REPO)

    def test_malformed_preread_is_definitely_unsent(self):
        c = Client([])
        with self.assertRaises(PreReadUnavailable):
            self.handle(c).run("wf_file_delete", self.args())
        self.assertEqual(len(c.calls), 1)

    def test_malformed_receipt_is_unknown(self):
        for result in [
            {},
            {"content": {}, "commit": {"sha": SHA}},
            {"content": None, "commit": {"sha": True}},
        ]:
            c = Client({"default_branch": "main"}, result)
            with self.assertRaises(RelayGitHubError) as err:
                self.handle(c).run("wf_file_delete", self.args())
            self.assertEqual(err.exception.status, 502)
            self.assertEqual(len(c.calls), 2)

    def test_closed_and_scanned_args(self):
        for kwargs in [
            {"force": True},
            {"message": " "},
            {"message": "é" * 251},
            {"prior_sha": True},
            {"path": "../x"},
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape("wf_file_delete", self.args(**kwargs), REPO)
