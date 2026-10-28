import unittest

try:
    from .test_workflow_verbs import REPO, SHA, Client, Policy, Minter
except ImportError:
    from test_workflow_verbs import REPO, SHA, Client, Policy, Minter
from git_relay.workflow_verbs import WorkflowVerbs, Refused
from git_relay.github_client import RelayGitHubError


class Checks(unittest.TestCase):
    def test_no_implicit_check_permissions(self):
        with self.assertRaises(Refused):
            WorkflowVerbs.for_action(
                Client(), Policy(), Minter(), "swu-wise", "wf_check_create", REPO
            )
        m = Minter()
        WorkflowVerbs.for_action(
            Client(), Policy({"checks": "write", "metadata": "read"}), m, "swu-wise", "wf_check_create", REPO
        )
        self.assertEqual(m.calls[0][2], {"checks": "write", "metadata": "read"})

    def test_fixed_create_update_and_head_binding(self):
        for action in ("wf_check_create", "wf_check_update"):
            args = {
                "repo": REPO,
                "name": "build",
                "status": "in_progress",
                "conclusion": "none",
                "title": "Build",
                "summary": "Running",
            }
            args.update(
                {"sha": SHA} if action.endswith("create") else {"check_id": 1, "expected_sha": SHA}
            )
            row = {
                "id": 1,
                "head_sha": SHA,
                "name": "build",
                "status": "in_progress",
                "conclusion": None,
            }
            c = Client(*([row] if action.endswith("create") else [row, row]))
            h = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", action, REPO)
            self.assertEqual(h.run(action, args)["check"]["head_sha"], SHA)
            self.assertEqual(c.calls[-1][3], "own")
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(action, {**args, "status": "completed"}, REPO)

    def test_malformed_write_and_changed_head(self):
        args = {
            "repo": REPO,
            "sha": SHA,
            "name": "build",
            "status": "completed",
            "conclusion": "success",
            "title": "Build",
            "summary": "Done",
        }
        c = Client({})
        h = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", "wf_check_create", REPO)
        with self.assertRaises(RelayGitHubError):
            h.run("wf_check_create", args)
        c = Client({"id": 1, "head_sha": "b" * 40})
        h = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", "wf_check_update", REPO)
        args.pop("sha")
        args.update({"check_id": 1, "expected_sha": SHA})
        with self.assertRaises(Refused):
            h.run("wf_check_update", args)
        self.assertEqual(len(c.calls), 1)
