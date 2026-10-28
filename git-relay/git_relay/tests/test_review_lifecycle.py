import unittest

from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, Client, Policy


class Reviews(unittest.TestCase):
    def test_writes_and_projected_receipts(self):
        for action, args, replies, method, suffix in [
            (
                "wf_review_update",
                {"review_id": 2, "body": "edit"},
                [{"id": 2, "state": "COMMENTED", "body": "edit"}],
                "PUT",
                "reviews/2",
            ),
            (
                "wf_review_dismiss",
                {"review_id": 2, "message": "obsolete"},
                [{"id": 2, "state": "DISMISSED"}],
                "PUT",
                "reviews/2/dismissals",
            ),
            (
                "wf_review_pending_delete",
                {"review_id": 2},
                [{"id": 2, "state": "PENDING"}, {"id": 2, "state": "PENDING"}],
                "DELETE",
                "reviews/2",
            ),
            (
                "wf_review_comment_reply",
                {"comment_id": 2, "body": "reply"},
                [{"id": 3, "in_reply_to_id": 2, "body": "reply"}],
                "POST",
                "comments/2/replies",
            ),
        ]:
            c = Client(*replies)
            h = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", action, REPO)
            out = h.run(action, {"repo": REPO, "number": 1, **args})
            self.assertEqual(c.calls[-1][0], method)
            self.assertTrue(c.calls[-1][1].endswith(suffix))
            self.assertEqual(c.calls[-1][3], "own")
            self.assertNotIn("body", out.get("review", out.get("comment")))
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(
                    action, {"repo": REPO, "number": 1, **args, "force": True}, REPO
                )

    def test_pending_only_and_malformed_receipts(self):
        c = Client({"id": 2, "state": "APPROVED"})
        h = WorkflowVerbs(
            c, Policy(), "swu-wise", "own", "swu-wise", "wf_review_pending_delete", REPO
        )
        with self.assertRaises(Refused):
            h.run("wf_review_pending_delete", {"repo": REPO, "number": 1, "review_id": 2})
        self.assertEqual(len(c.calls), 1)
        c = Client({"id": 2.0, "state": "DISMISSED"})
        h = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", "wf_review_dismiss", REPO)
        with self.assertRaises(RelayGitHubError):
            h.run(
                "wf_review_dismiss",
                {"repo": REPO, "number": 1, "review_id": 2, "message": "obsolete"},
            )
