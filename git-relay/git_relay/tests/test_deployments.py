import unittest

try:
    from .test_workflow_verbs import REPO, SHA, Client, Minter, Policy
except ImportError:
    from test_workflow_verbs import REPO, SHA, Client, Minter, Policy
from git_relay.workflow_verbs import Refused, WorkflowVerbs


class Deployments(unittest.TestCase):
    def test_permission_and_safe_create_flags(self):
        with self.assertRaises(Refused):
            WorkflowVerbs.for_action(
                Client(), Policy(), Minter(), "swu-wise", "wf_deployment_create", REPO
            )
        row = {
            "id": 2,
            "sha": SHA,
            "environment": "staging",
            "transient_environment": True,
            "production_environment": False,
        }
        c = Client(row)
        h = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", "wf_deployment_create", REPO)
        h.run(
            "wf_deployment_create",
            {
                "repo": REPO,
                "sha": SHA,
                "environment": "staging",
                "description": "deploy",
                "transient": True,
                "production": False,
            },
        )
        self.assertEqual(c.calls[0][2]["auto_merge"], False)
        self.assertNotIn("required_contexts", c.calls[0][2])
        self.assertEqual(c.calls[0][3], "own")

    def test_status_does_not_inactivate_others(self):
        c = Client({"id": 2, "state": "pending", "description": "queued"})
        h = WorkflowVerbs(
            c, Policy(), "swu-wise", "own", "swu-wise", "wf_deployment_status_create", REPO
        )
        h.run(
            "wf_deployment_status_create",
            {"repo": REPO, "deployment_id": 1, "state": "pending", "description": "queued"},
        )
        self.assertEqual(c.calls[0][2]["auto_inactive"], False)

    def test_reads_and_delete_shapes(self):
        for action, args, reply in [
            ("wf_deployments", {}, []),
            ("wf_deployment_get", {"deployment_id": 2}, {"id": 2, "sha": SHA}),
            ("wf_deployment_statuses", {"deployment_id": 2}, []),
            ("wf_deployment_delete", {"deployment_id": 2}, {}),
        ]:
            c = Client(reply)
            h = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", action, REPO)
            h.run(action, {"repo": REPO, **args})
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(
                    action, {"repo": REPO, **args, "url": "https://example.invalid"}, REPO
                )
