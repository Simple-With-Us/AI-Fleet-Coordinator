import unittest

from .test_workflow_verbs import REPO, SHA, Client, Minter


class PrFilePageRelayBoundary(unittest.TestCase):
    def relay(self, tmp, *, enabled=True, listed=True, pull_requests=True):
        import json
        import os

        from git_relay.audit import AuditLog
        from git_relay.policy import SeatPolicy
        from git_relay.server import Relay

        perms = {"metadata": "read", **({"pull_requests": "read"} if pull_requests else {})}
        seats = {
            "swu-wise": {
                "token": "w" * 32,
                "actions": ["wf_pr_files_page"] if listed else ["wf_runs"],
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

        for action in ("wf_pr_files_page",):
            for case in ("disabled", "unlisted", "caller", "repo", "ceiling", "badargs"):
                with self.subTest(action=action, case=case), tempfile.TemporaryDirectory() as tmp:
                    r = self.relay(
                        tmp,
                        enabled=case != "disabled",
                        listed=case != "unlisted",
                        pull_requests=case != "ceiling",
                    )
                    args = {"repo": "Other/Repo" if case == "repo" else REPO, "head_sha": SHA}
                    args.pop("head_sha", None)
                    args.update({"number": 2, "expected_head_sha": SHA})
                    if case == "badargs":
                        args["unexpected"] = True
                    status, _ = r.handle("x" * 32 if case == "caller" else "w" * 32, action, args)
                    self.assertGreaterEqual(status, 400)
                    self.assertEqual(r.client.calls, [])
                    self.assertEqual(r.app_minter.calls, [])

    def test_actual_relay_dispatch_pull_requests_read_and_no_write_or_cache(self):
        import tempfile

        for action in ("wf_pr_files_page",):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                r = self.relay(tmp)
                pr = {"number": 2, "head": {"sha": SHA}}
                r.client = Client(pr, [], pr, pr, [], pr)
                args = {"repo": REPO, "number": 2, "expected_head_sha": SHA}
                for _ in range(2):
                    status, body = r.handle("w" * 32, action, args)
                    self.assertEqual(status, 200, body)
                self.assertEqual(len(r.client.calls), 6)
                self.assertTrue(
                    all(
                        c[0] == "GET" and c[3] == "opaque-installation-bearer"
                        for c in r.client.calls
                    )
                )
                self.assertTrue(
                    all(
                        c[2] == {"pull_requests": "read", "metadata": "read"}
                        for c in r.app_minter.calls
                    )
                )
