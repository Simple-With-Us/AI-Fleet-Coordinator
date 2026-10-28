import unittest

try:
    from .test_workflow_verbs import REPO, Client, Policy
except ImportError:
    from test_workflow_verbs import REPO, Client, Policy

from git_relay.workflow_verbs import Refused, WorkflowVerbs


class ActionsManagement(unittest.TestCase):
    def test_fixed_routes_and_integer_ids(self):
        cases = [
            ("wf_workflow_enable", "workflow_id", "PUT", "actions/workflows/7/enable"),
            ("wf_workflow_disable", "workflow_id", "PUT", "actions/workflows/7/disable"),
            ("wf_run_delete", "run_id", "DELETE", "actions/runs/7"),
            ("wf_artifact_delete", "artifact_id", "DELETE", "actions/artifacts/7"),
        ]
        for action, key, method, path in cases:
            c = Client({})
            h = WorkflowVerbs(c, Policy(), "swu-wise", "private", "swu-wise", action, REPO)
            self.assertEqual(h.run(action, {"repo": REPO, key: 7}), {"completed": True, key: 7})
            self.assertEqual(c.calls, [(method, "/repos/" + REPO + "/" + path, None, "private")])
            for value in [True, 0, -1, "7"]:
                with self.assertRaises(Refused):
                    WorkflowVerbs.check_shape(action, {"repo": REPO, key: value}, REPO)
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(
                    action, {"repo": REPO, key: 7, "url": "https://evil.invalid"}, REPO
                )
