import unittest

try:
    from .test_workflow_verbs import REPO, Client, Policy
except ImportError:
    from test_workflow_verbs import REPO, Client, Policy

from git_relay.workflow_verbs import Refused, WorkflowVerbs


class IssueManagement(unittest.TestCase):
    def test_fixed_routes_and_arguments(self):
        cases = [
            (
                "wf_assignees_add",
                {"number": 2, "assignees": ["swu-wise"]},
                "POST",
                "issues/2/assignees",
                {"assignees": ["swu-wise"]},
            ),
            (
                "wf_assignees_remove",
                {"number": 2, "assignees": ["swu-wise"]},
                "DELETE",
                "issues/2/assignees",
                {"assignees": ["swu-wise"]},
            ),
            (
                "wf_issue_lock",
                {"number": 2, "reason": "resolved"},
                "PUT",
                "issues/2/lock",
                {"lock_reason": "resolved"},
            ),
            ("wf_issue_unlock", {"number": 2}, "DELETE", "issues/2/lock", None),
            (
                "wf_label_create",
                {"name": "needs review", "color": "aabbcc", "description": "Review required"},
                "POST",
                "labels",
                {"name": "needs review", "color": "aabbcc", "description": "Review required"},
            ),
            ("wf_label_delete", {"name": "needs review"}, "DELETE", "labels/needs%20review", None),
        ]
        for action, args, method, path, payload in cases:
            reply = {}
            if action.startswith("wf_assignees_"):
                reply = {
                    "id": 7,
                    "number": 2,
                    "assignees": [{"login": "swu-wise"}] if action.endswith("add") else [],
                    "body": "private",
                }
            if action == "wf_label_create":
                reply = {
                    "id": 8,
                    "name": "needs review",
                    "color": "aabbcc",
                    "description": "Review required",
                }
            c = Client(reply)
            h = WorkflowVerbs(c, Policy(), "swu-wise", "private", "swu-wise", action, REPO)
            result = h.run(action, {"repo": REPO, **args})
            self.assertNotIn("body", result.get("result", {}))
            self.assertEqual(c.calls, [(method, "/repos/" + REPO + "/" + path, payload, "private")])
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(
                    action, {"repo": REPO, **args, "url": "https://evil.invalid"}, REPO
                )

    def test_bad_inputs_refuse_before_network(self):
        for action, args in [
            ("wf_assignees_add", {"number": 1, "assignees": []}),
            ("wf_assignees_add", {"number": 1, "assignees": ["../x"]}),
            ("wf_assignees_add", {"number": 1, "assignees": ["-bad"]}),
            ("wf_assignees_add", {"number": 1, "assignees": ["a" * 40]}),
            ("wf_issue_lock", {"number": 1, "reason": "arbitrary"}),
            ("wf_label_create", {"name": "n", "color": "#ff0000", "description": "d"}),
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(action, {"repo": REPO, **args}, REPO)

    def test_wrong_receipts_fail_closed(self):
        from git_relay.github_client import RelayGitHubError

        for reply in (
            {},
            {"id": 1, "number": 2, "assignees": []},
            {"id": True, "number": 2, "assignees": [{"login": "swu-wise"}]},
        ):
            c = Client(reply)
            h = WorkflowVerbs(
                c, Policy(), "swu-wise", "private", "swu-wise", "wf_assignees_add", REPO
            )
            with self.assertRaises(RelayGitHubError):
                h.run("wf_assignees_add", {"repo": REPO, "number": 2, "assignees": ["swu-wise"]})
