import json
import unittest

try:
    from .test_workflow_verbs import REPO, SHA, Client, Minter, Policy
except ImportError:
    from test_workflow_verbs import REPO, SHA, Client, Minter, Policy
from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import Refused, WorkflowVerbs


class Statuses(unittest.TestCase):
    def test_permission_is_not_inferred_from_contents(self):
        for action in ("wf_commit_statuses", "wf_commit_status_create"):
            with self.assertRaises(Refused):
                WorkflowVerbs.for_action(Client(), Policy(), Minter(), "swu-wise", action, REPO)
        from git_relay.policy import SeatPolicy

        policy = SeatPolicy(
            json.dumps(
                {
                    "swu-wise": {
                        "token": "x" * 32,
                        "app_slug": "swu-wise",
                        "mint_permissions": {"statuses": "write"},
                        "actions": ["wf_commit_status_create"],
                        "repos": ["example/repo"],
                    }
                }
            )
        )
        self.assertIsNone(policy.authorize("swu-wise", "wf_commit_status_create", "example/repo"))
        m = Minter()
        WorkflowVerbs.for_action(
            Client(),
            Policy({"statuses": "write", "metadata": "read"}),
            m,
            "swu-wise",
            "wf_commit_status_create",
            REPO,
        )
        self.assertEqual(m.calls[0][2], {"statuses": "write", "metadata": "read"})

    def test_fixed_route_and_receipt(self):
        row = {
            "id": 1,
            "state": "pending",
            "context": "swu-wise/report",
            "description": "queued",
            "private": "omit",
        }
        for action, args, reply in [
            ("wf_commit_statuses", {}, [row]),
            (
                "wf_commit_status_create",
                {"state": "pending", "context": "swu-wise/report", "description": "queued"},
                row,
            ),
        ]:
            c = Client(reply)
            h = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", action, REPO)
            out = h.run(action, {"repo": REPO, "sha": SHA, **args})
            self.assertNotIn("omit", str(out))
            self.assertEqual(c.calls[0][3], "own")
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(
                    action,
                    {"repo": REPO, "sha": SHA, **args, "target_url": "https://example.invalid"},
                    REPO,
                )

    def test_malformed_receipt_is_unknown(self):
        c = Client({"id": True, "state": "success", "context": "swu-wise/report"})
        h = WorkflowVerbs(
            c, Policy(), "swu-wise", "own", "swu-wise", "wf_commit_status_create", REPO
        )
        with self.assertRaises(RelayGitHubError):
            h.run(
                "wf_commit_status_create",
                {
                    "repo": REPO,
                    "sha": SHA,
                    "state": "success",
                    "context": "swu-wise/report",
                    "description": "done",
                },
            )

    def test_cannot_impersonate_required_ci_or_other_app_context(self):
        for context in ("test", "gitleaks", "swu-echo/report"):
            c = Client()
            h = WorkflowVerbs(
                c, Policy(), "swu-wise", "own", "swu-wise", "wf_commit_status_create", REPO
            )
            with self.assertRaises(Refused):
                h.run(
                    "wf_commit_status_create",
                    {
                        "repo": REPO,
                        "sha": SHA,
                        "state": "success",
                        "context": context,
                        "description": "done",
                    },
                )
            self.assertEqual(c.calls, [])
