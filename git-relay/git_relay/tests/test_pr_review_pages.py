import unittest

from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, SHA, Client, Minter, Policy


class ReviewPages(unittest.TestCase):
    def args(self, **kw):
        return {"repo": REPO, "number": 2, "expected_head_sha": SHA, **kw}

    def pr(self, sha=SHA):
        return {"number": 2, "head": {"sha": sha}}

    def row(self, **kw):
        return {
            "id": 7,
            "user": {"id": 8, "login": "kody-ai[bot]", "type": "Bot"},
            "body": "Untrusted review text",
            "state": "CHANGES_REQUESTED",
            "commit_id": "b" * 40,
            **kw,
        }

    def handle(self, c, a):
        return WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", a, REPO)

    def test_fixed_review_or_issue_conversation_path_and_provenance(self):
        for action, endpoint, key in [
            ("wf_pr_reviews_page", "pulls/2/reviews", "reviews"),
            ("wf_pr_comments_page", "issues/2/comments", "comments"),
        ]:
            c = Client(self.pr(), [self.row()], self.pr())
            out = self.handle(c, action).run(action, self.args(page=2, per_page=1))
            self.assertEqual(
                c.calls[1], ("GET", f"/repos/{REPO}/{endpoint}?per_page=1&page=2", None, "own")
            )
            self.assertTrue(out["has_more"])
            self.assertEqual(out[key][0]["author"]["login"], "kody-ai[bot]")
            self.assertFalse(out[key][0]["body_withheld"])
            if key == "reviews":
                self.assertEqual(out[key][0]["commit_id"], "b" * 40)
            else:
                self.assertNotIn("state", out[key][0])
            m = Minter()
            WorkflowVerbs.for_action(c, Policy(), m, "swu-wise", action, REPO)
            self.assertEqual(m.calls[0][2], {"pull_requests": "read", "metadata": "read"})

    def test_credential_body_withheld_per_item_preserves_records(self):
        # Runtime-only synthetic fixture, never a credential.
        c = Client(self.pr(), [self.row(body="sk-proj-" + "x" * 24), self.row(id=9)], self.pr())
        out = self.handle(c, "wf_pr_reviews_page").run("wf_pr_reviews_page", self.args())
        self.assertTrue(out["reviews"][0]["body_withheld"])
        self.assertEqual(out["reviews"][0]["body"], "<withheld: credential-shaped text>")
        self.assertEqual(out["reviews"][1]["body"], "Untrusted review text")

    def test_bounds_duplicates_head_move_or_malformed_refused(self):
        for row in [self.row(id=True), self.row(commit_id="bad"), self.row(user={})]:
            with self.assertRaises(RelayGitHubError):
                self.handle(Client(self.pr(), [row]), "wf_pr_reviews_page").run(
                    "wf_pr_reviews_page", self.args()
                )
        with self.assertRaises(RelayGitHubError):
            self.handle(Client(self.pr(), [self.row(), self.row()]), "wf_pr_reviews_page").run(
                "wf_pr_reviews_page", self.args()
            )
        with self.assertRaises(Refused):
            self.handle(
                Client(self.pr(), [self.row()], self.pr("c" * 40)), "wf_pr_reviews_page"
            ).run("wf_pr_reviews_page", self.args())
        with self.assertRaises(Refused):
            self.handle(Client(self.pr(), [self.row(body="x" * 32769)]), "wf_pr_reviews_page").run(
                "wf_pr_reviews_page", self.args()
            )

    def test_closed_arg_pages(self):
        for bad in [{"page": 1001}, {"per_page": 51}, {"raw": True}]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape("wf_pr_reviews_page", self.args(**bad), REPO)

    def test_escaped_json_aggregate_bound_refuses_not_truncates(self):
        # Raw UTF-8 total under256KiB; JSON escapes push serialized result over it.
        rows = [self.row(id=i + 1, body="\x01" * 30000) for i in range(2)]
        c = Client(self.pr(), rows, self.pr())
        with self.assertRaises(Refused):
            self.handle(c, "wf_pr_reviews_page").run("wf_pr_reviews_page", self.args())
        self.assertEqual(len(c.calls), 3)
