import unittest

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
            c = Client({})
            h = WorkflowVerbs(c, Policy(), "swu-wise", "private", "swu-wise", action, REPO)
            h.run(action, {"repo": REPO, **args})
            self.assertEqual(c.calls, [(method, "/repos/" + REPO + "/" + path, payload, "private")])
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(
                    action, {"repo": REPO, **args, "url": "https://evil.invalid"}, REPO
                )

    def test_bad_inputs_refuse_before_network(self):
        for action, args in [
            ("wf_assignees_add", {"number": 1, "assignees": []}),
            ("wf_assignees_add", {"number": 1, "assignees": ["../x"]}),
            ("wf_issue_lock", {"number": 1, "reason": "arbitrary"}),
            ("wf_label_create", {"name": "n", "color": "#ff0000", "description": "d"}),
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(action, {"repo": REPO, **args}, REPO)
