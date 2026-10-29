import hashlib
import unittest

from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, SHA, Client, Minter, Policy


class CommentWrites(unittest.TestCase):
    def row(self, **changes):
        return {
            "id": 7,
            "commit_id": SHA,
            "user": {"id": 2, "login": "bot", "type": "Bot"},
            "body": "old",
            **changes,
        }

    def args(self, **changes):
        return {
            "repo": REPO,
            "sha": SHA,
            "comment_id": 7,
            "expected_body_sha256": hashlib.sha256(b"old").hexdigest(),
            **changes,
        }

    def call(self, a, replies, args):
        c = Client(*replies)
        h = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", a, REPO)
        return h.run(a, args), c

    def test_create_exact_commit_and_body_receipt(self):
        out, c = self.call(
            "wf_commit_comment_create",
            [{"sha": SHA}, self.row(body="new")],
            {"repo": REPO, "sha": SHA, "body": "new"},
        )
        self.assertEqual(
            c.calls[-1], ("POST", f"/repos/{REPO}/commits/{SHA}/comments", {"body": "new"}, "own")
        )
        self.assertEqual(out["comment"]["body"], "new")

    def test_update_and_delete_digest_id_sha_fenced(self):
        out, c = self.call(
            "wf_commit_comment_update", [self.row(), self.row(body="new")], self.args(body="new")
        )
        self.assertFalse(out["atomic"])
        self.assertEqual(c.calls[-1][0], "PATCH")
        out, c = self.call("wf_commit_comment_delete", [self.row(), {}], self.args())
        self.assertEqual(c.calls[-1], ("DELETE", f"/repos/{REPO}/comments/7", None, "own"))
        self.assertTrue(out["deleted"])

    def test_old_body_identity_changes_refuse_before_write(self):
        for row in [self.row(body="changed"), self.row(id=8), self.row(commit_id="b" * 40)]:
            c = Client(row)
            a = "wf_commit_comment_delete"
            h = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", a, REPO)
            with self.assertRaises((Refused, RelayGitHubError)):
                h.run(a, self.args())
            self.assertEqual(len(c.calls), 1)

    def test_credential_and_oversize_inputs_refuse_before_network(self):
        for body in ["sk-" + ("x" * 25), "ghp_" + ("y" * 30), "x" * 32769, "\ud800", "\x00", "   "]:
            c = Client()
            a = "wf_commit_comment_create"
            h = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", a, REPO)
            with self.assertRaises(Refused):
                h.run(a, {"repo": REPO, "sha": SHA, "body": body})
            self.assertEqual(c.calls, [])

    def test_wrong_create_or_update_receipt_refuses(self):
        for row in [
            self.row(id=8, body="new"),
            self.row(body="wrong"),
            self.row(commit_id="b" * 40, body="new"),
        ]:
            with self.assertRaises(RelayGitHubError):
                self.call("wf_commit_comment_update", [self.row(), row], self.args(body="new"))

    def test_delete_requires_exact_no_content_status(self):
        class C(Client):
            def _call(self, *args, **kwargs):
                if args[0] == "DELETE":
                    self.expected = kwargs["expected_status"]
                return super()._call(*args, **kwargs)

        c = C(self.row(), {})
        a = "wf_commit_comment_delete"
        WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", a, REPO).run(a, self.args())
        self.assertEqual(c.expected, 204)

    def test_own_app_minimum_contents_write(self):
        for a in ["create", "update", "delete"]:
            m = Minter()
            WorkflowVerbs.for_action(
                Client(), Policy(), m, "swu-wise", "wf_commit_comment_" + a, REPO
            )
            self.assertEqual(m.calls[0][2], {"contents": "write", "metadata": "read"})
