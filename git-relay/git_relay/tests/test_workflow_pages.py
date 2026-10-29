import unittest

from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, Client, Minter, Policy


class WorkflowPages(unittest.TestCase):
    def row(self, **kw):
        return {
            "id": 7,
            "path": ".github/workflows/ci.yml",
            "state": "active",
            "name": "not authority",
            "url": "omitted",
            **kw,
        }

    def handle(self, c):
        return WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", "wf_workflows_page", REPO)

    def value(self, rows):
        return {"total_count": len(rows), "workflows": rows}

    def test_numeric_id_path_state_page_only(self):
        c = Client(self.value([self.row()]))
        out = self.handle(c).run("wf_workflows_page", {"repo": REPO, "page": 2, "per_page": 1})
        self.assertEqual(
            out["workflows"], [{"id": 7, "path": ".github/workflows/ci.yml", "state": "active"}]
        )
        self.assertFalse(out["snapshot"])
        self.assertTrue(out["has_more"])
        self.assertEqual(
            c.calls, [("GET", f"/repos/{REPO}/actions/workflows?per_page=1&page=2", None, "own")]
        )
        m = Minter()
        WorkflowVerbs.for_action(c, Policy(), m, "swu-wise", "wf_workflows_page", REPO)
        self.assertEqual(m.calls[0][2], {"actions": "read", "metadata": "read"})

    def test_malformed_duplicate_odd_paths_refuse(self):
        for rows in [
            [self.row(id=True)],
            [self.row(), self.row()],
            [self.row(state="other")],
            [self.row(path="../ci.yml")],
            [self.row(path=".github/workflows/../ci.yml")],
            [self.row(path="dynamic/provider/path")],
        ]:
            with self.assertRaises(RelayGitHubError):
                self.handle(Client(self.value(rows))).run("wf_workflows_page", {"repo": REPO})

    def test_secret_path_refused_not_returned(self):
        with self.assertRaises(Refused):
            self.handle(
                Client(self.value([self.row(path=".github/workflows/ghp_" + "x" * 36 + ".yml")]))
            ).run("wf_workflows_page", {"repo": REPO})

    def test_closed_page_args(self):
        for bad in [{"page": 1001}, {"per_page": 51}, {"raw_query": "x"}]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape("wf_workflows_page", {"repo": REPO, **bad}, REPO)
