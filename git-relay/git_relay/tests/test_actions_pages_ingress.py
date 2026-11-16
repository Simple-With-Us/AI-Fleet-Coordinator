import unittest

from .test_workflow_verbs import REPO, SHA, Client, Minter


class ActionsPageRelayBoundary(unittest.TestCase):
    def relay(self, tmp, *, enabled=True, listed=True, actions=True):
        import json
        import os

        from git_relay.audit import AuditLog
        from git_relay.policy import SeatPolicy
        from git_relay.server import Relay

        perms = {"metadata": "read", **({"actions": "read"} if actions else {})}
        seats = {
            "swu-wise": {
                "token": "w" * 32,
                "actions": ["wf_runs_page", "wf_workflows_page"] if listed else ["wf_runs"],
                "repos": [REPO],
                "app_slug": "swu-wise",
                "mint_permissions": perms,
            }
        }
        return Relay(
            SeatPolicy(json.dumps(seats)),
            Client(),
            AuditLog(os.path.join(tmp, "audit.jsonl")),
            app_minter=Minter(),
            workflow_verbs_enabled=enabled,
        )

    def test_disabled_unlisted_wrong_caller_repo_and_missing_ceiling_never_mint(self):
        import tempfile

        for action in ("wf_runs_page", "wf_workflows_page"):
            for case in ("disabled", "unlisted", "caller", "repo", "ceiling", "badargs"):
                with self.subTest(action=action, case=case), tempfile.TemporaryDirectory() as tmp:
                    r = self.relay(
                        tmp,
                        enabled=case != "disabled",
                        listed=case != "unlisted",
                        actions=case != "ceiling",
                    )
                    args = {"repo": "Other/Repo" if case == "repo" else REPO, "head_sha": SHA}
                    args.pop("head_sha", None)
                    if case == "badargs":
                        args["unexpected"] = True
                    status, _ = r.handle("x" * 32 if case == "caller" else "w" * 32, action, args)
                    self.assertGreaterEqual(status, 400)
                    self.assertEqual(r.client.calls, [])
                    self.assertEqual(r.app_minter.calls, [])

    def test_actual_relay_dispatch_actions_read_and_no_write_or_cache(self):
        import tempfile

        for action in ("wf_runs_page", "wf_workflows_page"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                r = self.relay(tmp)
                payload = (
                    {"total_count": 0, "workflow_runs": []}
                    if action == "wf_runs_page"
                    else {"total_count": 0, "workflows": []}
                )
                r.client = Client(payload, payload)
                args = {"repo": REPO}
                for _ in range(2):
                    status, body = r.handle("w" * 32, action, args)
                    self.assertEqual(status, 200, body)
                self.assertEqual(len(r.client.calls), 2)
                self.assertTrue(
                    all(
                        c[0] == "GET" and c[3] == "opaque-installation-bearer"
                        for c in r.client.calls
                    )
                )
                self.assertTrue(
                    all(c[2] == {"actions": "read", "metadata": "read"} for c in r.app_minter.calls)
                )
