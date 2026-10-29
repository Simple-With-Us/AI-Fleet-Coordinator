import unittest

from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, SHA, Client, Policy


class DetailReads(unittest.TestCase):
    def run_read(self, a, replies, **args):
        c = Client(*replies)
        h = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", a, REPO)
        return h.run(a, {"repo": REPO, **args}), c

    def test_commit_page_bounded_graph_and_history_cap(self):
        pull = {"head": {"sha": SHA}}
        row = {"sha": SHA, "parents": [{"sha": "b" * 40}], "commit": {"message": "private"}}
        out, c = self.run_read(
            "wf_pr_commits_page",
            [pull, [row], pull],
            number=3,
            expected_head_sha=SHA,
            per_page=1,
            page=250,
        )
        self.assertFalse(out["has_more"])
        self.assertFalse(out["history_complete"])
        self.assertNotIn("private", str(out))
        self.assertEqual(out["history_cap"], 250)
        with self.assertRaises(Refused):
            self.run_read(
                "wf_pr_commits_page", [], number=3, expected_head_sha=SHA, per_page=50, page=6
            )

    def test_commit_page_bad_sha_duplicate_and_moving_head(self):
        pull = {"head": {"sha": SHA}}
        for rows, after in [
            ([{"sha": "main", "parents": []}], pull),
            ([{"sha": SHA, "parents": []}] * 2, pull),
            ([], {"head": {"sha": "b" * 40}}),
        ]:
            with self.assertRaises((Refused, RelayGitHubError)):
                self.run_read(
                    "wf_pr_commits_page", [pull, rows, after], number=3, expected_head_sha=SHA
                )

    def test_exact_review_id_and_historical_commit_not_current_approval(self):
        pull = {"head": {"sha": SHA}}
        row = {
            "id": 7,
            "state": "APPROVED",
            "commit_id": "b" * 40,
            "html_url": None,
            "body": "private",
        }
        out, c = self.run_read(
            "wf_pr_review_get", [pull, row, pull], number=3, review_id=7, expected_head_sha=SHA
        )
        self.assertEqual(out["review"]["commit_id"], "b" * 40)
        self.assertNotIn("private", str(out))
        self.assertFalse(out["snapshot"])
        with self.assertRaises(RelayGitHubError):
            self.run_read(
                "wf_pr_review_get",
                [pull, {**row, "id": 8}],
                number=3,
                review_id=7,
                expected_head_sha=SHA,
            )

    def test_release_latest_flags_and_exact_tag_lookup(self):
        row = {
            "id": 7,
            "tag_name": "release/v1",
            "draft": False,
            "prerelease": False,
            "body": "private",
        }
        out, c = self.run_read("wf_release_by_tag", [row], tag="release/v1")
        self.assertTrue(c.calls[0][1].endswith("/releases/tags/release%2Fv1"))
        self.assertNotIn("private", str(out))
        out, c = self.run_read("wf_release_latest", [row])
        self.assertTrue(c.calls[0][1].endswith("/releases/latest"))
        for bad in [
            {**row, "draft": True},
            {**row, "prerelease": True},
            {**row, "tag_name": "sk-" + ("x" * 25)},
            {**row, "id": True},
        ]:
            with self.assertRaises(RelayGitHubError):
                self.run_read("wf_release_latest", [bad])
        with self.assertRaises(RelayGitHubError):
            self.run_read("wf_release_by_tag", [row], tag="v2")
