import unittest
from .test_workflow_verbs import Client, Policy, REPO
from git_relay.workflow_verbs import WorkflowVerbs, Refused, SPECS
from git_relay.server import _REAPPLY_STATE_ACTIONS


class Variables(unittest.TestCase):
    def test_fixed_methods_status_and_no_value_receipt(self):
        class Strict(Client):
            def _call(self, *a, expected_status=None, **k):
                self.expected = expected_status
                return super()._call(*a, **k)

        for action, method, status, path in [
            ("wf_repo_variable_create", "POST", 201, "actions/variables"),
            ("wf_repo_variable_update", "PATCH", 204, "actions/variables/BUILD_MODE"),
            ("wf_repo_variable_delete", "DELETE", 204, "actions/variables/BUILD_MODE"),
        ]:
            c = Strict({})
            args = {"repo": REPO, "name": "BUILD_MODE"}
            if not action.endswith("delete"):
                args["value"] = "release"
            out = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", action, REPO).run(
                action, args
            )
            self.assertEqual(c.calls[0][0:2], (method, f"/repos/{REPO}/{path}"))
            self.assertEqual(c.calls[0][3], "own")
            self.assertEqual(c.expected, status)
            self.assertNotIn("value", out)
            self.assertNotIn(action, _REAPPLY_STATE_ACTIONS)
            self.assertEqual(SPECS[action][0], {"variables": "write"})

    def test_nonsecret_closed_bounded(self):
        a = {"repo": REPO, "name": "BUILD_MODE", "value": "release"}
        for extra in [
            {"name": "GITHUB_TOKEN"},
            {"name": "../evil"},
            {"value": "é" * 4097},
            {"value": "ghp_" + "a" * 36},
            {"secret": True},
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape("wf_repo_variable_create", {**a, **extra}, REPO)

    def test_list_compact_page(self):
        c = Client(
            {
                "total_count": 1,
                "variables": [
                    {"name": "MODE", "value": "dev", "created_at": "x", "updated_at": "x"}
                ],
            }
        )
        a = "wf_repo_variables"
        out = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", a, REPO).run(
            a, {"repo": REPO, "page": 2, "per_page": 1}
        )
        self.assertEqual(out["page"], 2)

    def test_permission_never_assumed_from_shared_ceiling(self):
        from .test_workflow_verbs import Minter

        for a in [
            "wf_repo_variables",
            "wf_repo_variable_get",
            "wf_repo_variable_create",
            "wf_repo_variable_update",
            "wf_repo_variable_delete",
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.for_action(Client(), Policy(), Minter(), "swu-wise", a, REPO)
