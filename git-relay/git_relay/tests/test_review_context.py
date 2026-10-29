import unittest

from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, SHA, Client, Minter, Policy


class ReviewContext(unittest.TestCase):
    def run_action(self, action, replies, **args):
        c = Client(*replies)
        h = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", action, REPO)
        return h.run(action, {"repo": REPO, **args}), c

    def row(self, **changes):
        return {
            "id": 7,
            "commit_id": SHA,
            "user": {"id": 3, "login": "bot", "type": "Bot"},
            "body": "untrusted",
            **changes,
        }

    def test_commit_page_exact_sha_page_projection(self):
        out, c = self.run_action(
            "wf_commit_comments_page",
            [[self.row(url="ignored", extra="ignored")]],
            sha=SHA,
            page=2,
            per_page=1,
        )
        self.assertEqual(
            c.calls,
            [("GET", f"/repos/{REPO}/commits/{SHA}/comments?per_page=1&page=2", None, "own")],
        )
        self.assertTrue(out["has_more"])
        self.assertFalse(out["snapshot"])
        self.assertNotIn("url", out["comments"][0])

    def test_get_correlates_both_comment_id_and_commit(self):
        out, _ = self.run_action("wf_commit_comment_get", [self.row()], sha=SHA, comment_id=7)
        self.assertEqual(out["comment"]["id"], 7)
        for row in [
            self.row(id=8),
            self.row(commit_id="b" * 40),
            self.row(id=True),
            self.row(user=None),
            self.row(body="\ud800"),
        ]:
            with self.subTest(row=row), self.assertRaises(RelayGitHubError):
                self.run_action("wf_commit_comment_get", [row], sha=SHA, comment_id=7)

    def test_pages_refuse_duplicates_overfull_and_cross_commit(self):
        for rows in [
            [self.row(), self.row()],
            [self.row(commit_id="b" * 40)],
            {},
            [self.row(body="x" * 32769)],
        ]:
            with self.subTest(rows=str(rows)[:50]), self.assertRaises((Refused, RelayGitHubError)):
                self.run_action("wf_commit_comments_page", [rows], sha=SHA, per_page=1)

    def test_synthetic_secret_body_withheld(self):
        # Runtime-assembled synthetic scanner shapes only, never credentials.
        for body in ["sk-" + ("a" * 25), "ghp_" + ("b" * 30), "xoxb-" + ("c" * 30)]:
            out, _ = self.run_action(
                "wf_commit_comment_get", [self.row(body=body)], sha=SHA, comment_id=7
            )
            self.assertTrue(out["comment"]["body_withheld"])
            self.assertNotIn(body, str(out))

    def test_serialized_page_limit(self):
        rows = [self.row(id=i + 1, body="x" * 32768) for i in range(9)]
        with self.assertRaises(Refused):
            self.run_action("wf_commit_comments_page", [rows], sha=SHA)

    def test_reviewers_head_readbacks_and_projection(self):
        pull = {"number": 9, "head": {"sha": SHA}}
        row = {
            "users": [self.row()["user"]],
            "teams": [{"id": 4, "slug": "devs", "description": "not returned"}],
        }
        out, c = self.run_action(
            "wf_pr_requested_reviewers", [pull, row, pull], number=9, expected_head_sha=SHA
        )
        self.assertEqual(out["teams"], [{"id": 4, "slug": "devs"}])
        self.assertEqual(len(c.calls), 3)
        self.assertFalse(out["snapshot"])
        self.assertTrue(all(x[0] == "GET" for x in c.calls))

    def test_reviewers_changed_head_duplicates_and_bounds_refuse(self):
        pull = {"number": 9, "head": {"sha": SHA}}
        row = {"users": [], "teams": []}
        for replies in [
            [pull, row, {"number": 9, "head": {"sha": "b" * 40}}],
            [pull, {"users": [self.row()["user"]] * 2, "teams": []}],
            [pull, {"users": [], "teams": [{"id": 4, "slug": "a"}] * 101}],
        ]:
            with (
                self.subTest(replies=str(replies)[:70]),
                self.assertRaises((Refused, RelayGitHubError)),
            ):
                self.run_action(
                    "wf_pr_requested_reviewers", replies, number=9, expected_head_sha=SHA
                )

    def test_shape_before_network_and_own_app_minimal_permissions(self):
        for action, args in [
            ("wf_commit_comments_page", {"sha": SHA}),
            ("wf_commit_comment_get", {"sha": SHA, "comment_id": 7}),
            ("wf_pr_requested_reviewers", {"number": 9, "expected_head_sha": SHA}),
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(
                    action, {"repo": REPO, **args, "url": "https://invalid"}, REPO
                )
            m = Minter()
            WorkflowVerbs.for_action(Client(), Policy(), m, "swu-wise", action, REPO)
            self.assertEqual(m.calls[0][0], "swu-wise")
            self.assertEqual(m.calls[0][1], REPO)
            self.assertEqual(
                m.calls[0][2],
                {
                    ("pull_requests" if "reviewers" in action else "contents"): "read",
                    "metadata": "read",
                },
            )
