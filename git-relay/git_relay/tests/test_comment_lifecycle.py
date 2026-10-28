import unittest

try:
    from .test_workflow_verbs import REPO, Client, Policy
except ImportError:
    from test_workflow_verbs import REPO, Client, Policy

from git_relay.workflow_verbs import Refused, WorkflowVerbs


class CommentLifecycle(unittest.TestCase):
    def test_fixed_routes_and_bound_identifiers(self):
        cases = [
            (
                "wf_issue_comment_update",
                {"comment_id": 2, "body": "updated"},
                "PATCH",
                "issues/comments/2",
                {"body": "updated"},
                {"id": 2},
            ),
            ("wf_issue_comment_delete", {"comment_id": 2}, "DELETE", "issues/comments/2", None, {}),
            (
                "wf_review_comment_update",
                {"comment_id": 2, "body": "updated"},
                "PATCH",
                "pulls/comments/2",
                {"body": "updated"},
                {"id": 2},
            ),
            ("wf_review_comment_delete", {"comment_id": 2}, "DELETE", "pulls/comments/2", None, {}),
            ("wf_release_list", {}, "GET", "releases?per_page=50", None, []),
            ("wf_release_get", {"release_id": 4}, "GET", "releases/4", None, {"id": 4, "draft": False, "prerelease": False}),
            ("wf_release_delete", {"release_id": 4}, "DELETE", "releases/4", None, {}),
        ]
        for action, args, method, path, payload, reply in cases:
            c = Client(reply)
            h = WorkflowVerbs(c, Policy(), "swu-wise", "private", "swu-wise", action, REPO)
            h.run(action, {"repo": REPO, **args})
            self.assertEqual(c.calls, [(method, "/repos/" + REPO + "/" + path, payload, "private")])
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(
                    action, {"repo": REPO, **args, "url": "https://evil.invalid"}, REPO
                )

    def test_invalid_ids_and_secret_bodies_refuse_before_network(self):
        for value in [True, 0, -1, "4"]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(
                    "wf_issue_comment_delete", {"repo": REPO, "comment_id": value}, REPO
                )
        with self.assertRaises(Refused):
            WorkflowVerbs.check_shape(
                "wf_issue_comment_update",
                {"repo": REPO, "comment_id": 1, "body": "ghp_" + "A" * 36},
                REPO,
            )

    def test_release_projection_and_float_comment_refusal(self):
        from git_relay.github_client import RelayGitHubError

        c = Client([{"id": 2, "draft": False, "prerelease": False, "body": "private"}])
        h = WorkflowVerbs(c, Policy(), "swu-wise", "private", "swu-wise", "wf_release_list", REPO)
        result = h.run("wf_release_list", {"repo": REPO})
        self.assertNotIn("body", result["releases"][0])
        c = Client({"id": 2.0})
        h = WorkflowVerbs(
            c, Policy(), "swu-wise", "private", "swu-wise", "wf_issue_comment_update", REPO
        )
        with self.assertRaises(RelayGitHubError):
            h.run("wf_issue_comment_update", {"repo": REPO, "comment_id": 2, "body": "updated"})
