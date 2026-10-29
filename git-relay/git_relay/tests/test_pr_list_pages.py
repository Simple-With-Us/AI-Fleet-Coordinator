import unittest

from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, SHA, Client, Minter, Policy


class PrListPages(unittest.TestCase):
    def row(self, **kw):
        return {
            "number": 2,
            "state": "open",
            "draft": False,
            "head": {"ref": "release/a", "sha": SHA, "label": REPO.split("/")[0] + ":release/a"},
            "base": {"ref": "main", "sha": "b" * 40},
            "title": "not returned",
            **kw,
        }

    def handle(self, c):
        return WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", "wf_pr_list_page", REPO)

    def test_fixed_paging_owner_filter_and_metadata_only(self):
        c = Client([self.row()])
        out = self.handle(c).run(
            "wf_pr_list_page",
            {"repo": REPO, "head": "release/a", "base": "main", "page": 2, "per_page": 1},
        )
        self.assertFalse(out["snapshot"])
        self.assertTrue(out["has_more"])
        self.assertNotIn("title", out["pulls"][0])
        self.assertEqual(
            c.calls[0],
            (
                "GET",
                f"/repos/{REPO}/pulls?state=open&per_page=1&page=2&sort=created&direction=asc&head="
                + REPO.split("/")[0]
                + "%3Arelease%2Fa&base=main",
                None,
                "own",
            ),
        )
        m = Minter()
        WorkflowVerbs.for_action(c, Policy(), m, "swu-wise", "wf_pr_list_page", REPO)
        self.assertEqual(m.calls[0][2], {"pull_requests": "read", "metadata": "read"})

    def test_malformed_duplicate_or_filter_mismatch_refused(self):
        for value, args in [
            ([self.row(number=True)], {}),
            ([self.row(), self.row()], {}),
            ([self.row(state="closed")], {}),
            (
                [self.row(head={"ref": "release/a", "sha": SHA, "label": "other:release/a"})],
                {"head": "release/a"},
            ),
            ([self.row()], {"base": "other"}),
        ]:
            with self.assertRaises(RelayGitHubError):
                self.handle(Client(value)).run("wf_pr_list_page", {"repo": REPO, **args})

    def test_closed_bounds_and_no_fork_expression(self):
        for bad in [
            {"head": "other:branch"},
            {"page": 1001},
            {"per_page": 51},
            {"sort": "updated"},
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape("wf_pr_list_page", {"repo": REPO, **bad}, REPO)

    def test_provider_branch_metadata_does_not_use_write_ref_grammar(self):
        for name in ("deps/pkg@1.2+build#2", "feature/é"):
            row = self.row(head={"ref": name, "sha": SHA})
            out = self.handle(Client([row])).run("wf_pr_list_page", {"repo": REPO})
            self.assertEqual(out["pulls"][0]["head_ref"], name)
        for name in ("a..b", "a@{b", "a\n", "a/.bad", "a.lock"):
            with self.assertRaises(RelayGitHubError):
                self.handle(Client([self.row(head={"ref": name, "sha": SHA})])).run(
                    "wf_pr_list_page", {"repo": REPO}
                )
