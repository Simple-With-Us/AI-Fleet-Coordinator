import unittest
from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import Refused, SPECS, WorkflowVerbs
from .test_workflow_verbs import REPO, SHA, Client, Policy, Minter


class FileHistory(unittest.TestCase):
    def args(self, **kw):
        return {"repo": REPO, "path": "docs/a+b.txt", "head_sha": SHA, **kw}

    def handle(self, c):
        return WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", "wf_file_history", REPO)

    def test_exact_pinned_encoded_path_bounded_metadata(self):
        c = Client(
            [
                {
                    "sha": "b" * 40,
                    "parents": [{"sha": "c" * 40}],
                    "commit": {"message": "not returned"},
                    "author": {"email": "not returned"},
                }
            ]
        )
        out = self.handle(c).run("wf_file_history", self.args(page=2, per_page=1))
        self.assertEqual(
            out,
            {
                "commits": [{"sha": "b" * 40, "parents": ["c" * 40]}],
                "path": "docs/a+b.txt",
                "head_sha": SHA,
                "page": 2,
                "has_more": True,
            },
        )
        self.assertEqual(
            c.calls,
            [
                (
                    "GET",
                    f"/repos/{REPO}/commits?sha={SHA}&path=docs%2Fa%2Bb.txt&per_page=1&page=2",
                    None,
                    "own",
                )
            ],
        )
        self.assertEqual(SPECS["wf_file_history"], ({"contents": "read"}, False))
        m = Minter()
        WorkflowVerbs.for_action(c, Policy(), m, "swu-wise", "wf_file_history", REPO)
        self.assertEqual(m.calls[0][2], {"contents": "read", "metadata": "read"})

    def test_pages_not_false_complete(self):
        for values in [[], [{"sha": "b" * 40, "parents": []}]]:
            out = self.handle(Client(values)).run("wf_file_history", self.args(per_page=2))
            self.assertFalse(out["has_more"])

    def test_closed_args(self):
        for bad in [
            {"path": "../x"},
            {"page": 0},
            {"per_page": 51},
            {"head_sha": True},
            {"follow": True},
            {"path": "a/" * 17 + "x"},
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape("wf_file_history", self.args(**bad), REPO)

    def test_malformed_upstream_refused_not_silently_skipped(self):
        for values in [
            {},
            [{}],
            [{"sha": True, "parents": []}],
            [{"sha": "b" * 40, "parents": [{}]}],
            [{"sha": "b" * 40, "parents": []}] * 31,
        ]:
            with self.assertRaises(RelayGitHubError):
                self.handle(Client(values)).run("wf_file_history", self.args())
