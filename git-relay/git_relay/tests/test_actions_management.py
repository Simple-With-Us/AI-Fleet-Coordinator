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


class ActionsHTTPReceipt(unittest.TestCase):
    def test_only_empty_204_completes(self):
        import io

        from git_relay.github_client import GitHubClient, RelayGitHubError

        class Response(io.BytesIO):
            def __init__(self, status, body):
                super().__init__(body)
                self.status = status

        for status, body in [(200, b"{}"), (202, b""), (204, b"{}"), (201, b"")]:
            c = GitHubClient("token", opener=lambda *a, **k: Response(status, body))
            with self.assertRaises(RelayGitHubError) as caught:
                c._call(
                    "DELETE",
                    "/repos/Example/demo/actions/runs/7",
                    bearer="own",
                    expected_status=204,
                )
            self.assertEqual(caught.exception.status, 502)
        c = GitHubClient("token", opener=lambda *a, **k: Response(204, b""))
        self.assertEqual(
            c._call(
                "DELETE", "/repos/Example/demo/actions/runs/7", bearer="own", expected_status=204
            ),
            {},
        )

    def test_each_actions_wrapper_demands_204(self):
        class Strict(Client):
            def _call(self, *a, expected_status=None, **k):
                self.expected = expected_status
                return super()._call(*a, **k)

        for action, key in [
            ("wf_workflow_enable", "workflow_id"),
            ("wf_workflow_disable", "workflow_id"),
            ("wf_run_delete", "run_id"),
            ("wf_artifact_delete", "artifact_id"),
        ]:
            c = Strict({})
            WorkflowVerbs(c, Policy(), "swu-wise", "private", "swu-wise", action, REPO).run(
                action, {"repo": REPO, key: 7}
            )
            self.assertEqual(c.expected, 204)
