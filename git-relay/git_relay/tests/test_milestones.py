import unittest

try:
    from .test_workflow_verbs import REPO, Client, Policy
except ImportError:
    from test_workflow_verbs import REPO, Client, Policy
from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import Refused, WorkflowVerbs


class Milestones(unittest.TestCase):
    def test_read_write_routes_receipts(self):
        summary = {"number": 2, "title": "Ship", "state": "open", "body": "private"}
        for action, args, reply, method in [
            ("wf_milestones", {}, [summary], "GET"),
            ("wf_milestone_get", {"milestone": 2}, summary, "GET"),
            (
                "wf_milestone_create",
                {"title": "Ship", "description": "notes", "state": "open"},
                summary,
                "POST",
            ),
            (
                "wf_milestone_update",
                {"milestone": 2, "title": "Ship", "description": "notes", "state": "open"},
                summary,
                "PATCH",
            ),
            ("wf_milestone_delete", {"milestone": 2}, {}, "DELETE"),
        ]:
            c = Client(reply)
            h = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", action, REPO)
            out = h.run(action, {"repo": REPO, **args})
            self.assertEqual(c.calls[0][0], method)
            self.assertEqual(c.calls[0][3], "own")
            self.assertNotIn("private", str(out))
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(
                    action, {"repo": REPO, **args, "url": "https://example.invalid"}, REPO
                )

    def test_bad_receipt_and_text(self):
        c = Client({"number": 2.0, "title": "Ship", "state": "open"})
        h = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", "wf_milestone_get", REPO)
        with self.assertRaises(RelayGitHubError):
            h.run("wf_milestone_get", {"repo": REPO, "milestone": 2})
        with self.assertRaises(Refused):
            WorkflowVerbs.check_shape(
                "wf_milestone_create",
                {"repo": REPO, "title": "Ship", "description": "ghp_" + "A" * 36, "state": "open"},
                REPO,
            )
