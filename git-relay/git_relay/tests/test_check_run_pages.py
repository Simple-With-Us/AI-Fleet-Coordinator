import unittest

from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, SHA, Client, Minter, Policy


class CheckPages(unittest.TestCase):
    def args(self, **kw):
        return {"repo": REPO, "head_sha": SHA, "check_name": "Kody Code Review", **kw}

    def row(self, **kw):
        return {
            "id": 7,
            "head_sha": SHA,
            "name": "Kody Code Review",
            "status": "completed",
            "conclusion": "success",
            "app": {"id": 8},
            **kw,
        }

    def handle(self, c, a):
        return WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", a, REPO)

    def test_exact_name_pages_all_attempts_no_output_and_permission(self):
        a = "wf_check_runs_page"
        c = Client(
            {"total_count": 3, "check_runs": [self.row(output={"text": "untrusted omitted"})]}
        )
        out = self.handle(c, a).run(a, self.args(page=2, per_page=1))
        self.assertTrue(out["has_more"])
        self.assertEqual(out["checks"][0]["app_id"], 8)
        self.assertNotIn("output", out["checks"][0])
        self.assertEqual(
            c.calls,
            [
                (
                    "GET",
                    f"/repos/{REPO}/commits/{SHA}/check-runs?check_name=Kody%20Code%20Review&filter=all&per_page=1&page=2",
                    None,
                    "own",
                )
            ],
        )
        m = Minter()
        WorkflowVerbs.for_action(
            c, Policy(ceiling={"checks": "read", "metadata": "read"}), m, "swu-wise", a, REPO
        )
        self.assertEqual(m.calls[0][2], {"checks": "read", "metadata": "read"})

    def test_exact_run_head_correlation(self):
        a = "wf_check_run_get"
        args = {"repo": REPO, "check_run_id": 7, "head_sha": SHA}
        c = Client(self.row())
        out = self.handle(c, a).run(a, args)
        self.assertEqual(out["check"]["id"], 7)
        for row in [self.row(id=8), self.row(head_sha="b" * 40)]:
            with self.assertRaises(RelayGitHubError):
                self.handle(Client(row), a).run(a, args)

    def test_malformed_or_scope_mismatched_page_not_skipped(self):
        for row in [
            self.row(name="other"),
            self.row(head_sha="b" * 40),
            self.row(id=True),
            self.row(status="in_progress", conclusion="success"),
            self.row(app={}),
        ]:
            with self.assertRaises(RelayGitHubError):
                self.handle(
                    Client({"total_count": 1, "check_runs": [row]}), "wf_check_runs_page"
                ).run("wf_check_runs_page", self.args())
        with self.assertRaises(RelayGitHubError):
            self.handle(
                Client({"total_count": 51, "check_runs": [self.row()] * 51}), "wf_check_runs_page"
            ).run("wf_check_runs_page", self.args())

    def test_closed_bounded_arguments_and_untrusted_name_scan(self):
        for bad in [
            {"page": 0},
            {"per_page": 51},
            {"check_name": " "},
            {"check_name": "\ud800"},
            {"url": "https://evil.invalid"},
        ]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape("wf_check_runs_page", self.args(**bad), REPO)
        # Synthetic credential fixture only, never a real token.
        with self.assertRaises(Refused):
            self.handle(Client(self.row(name="ghp_" + "x" * 36)), "wf_check_run_get").run(
                "wf_check_run_get", {"repo": REPO, "check_run_id": 7, "head_sha": SHA}
            )

    def test_missing_checks_ceiling_denied_without_mint(self):
        for action in ["wf_check_runs_page", "wf_check_run_get"]:
            m = Minter()
            with self.assertRaises(Refused):
                WorkflowVerbs.for_action(Client(), Policy(), m, "swu-wise", action, REPO)
            self.assertEqual(m.calls, [])

    def test_name_queries_are_encoded_and_secrets_refused_before_upstream(self):
        args = self.args(check_name="build / lint?x=1&y=2")
        client = Client({"total_count": 0, "check_runs": []})
        result = self.handle(client, "wf_check_runs_page").run("wf_check_runs_page", args)
        self.assertFalse(result["has_more"])
        self.assertIn(
            "check_name=build%20%2F%20lint%3Fx%3D1%26y%3D2&filter=all", client.calls[0][1]
        )
        # Synthetic runtime-generated patterns only, never credentials.
        for name in ("ghp_" + "x" * 36, "sk-proj-" + "x" * 24):
            client = Client({})
            with self.assertRaises(Refused):
                self.handle(client, "wf_check_runs_page").run(
                    "wf_check_runs_page", self.args(check_name=name)
                )
            self.assertEqual(client.calls, [])

    def test_pending_empty_and_invalid_page_shapes(self):
        for status in ("queued", "in_progress", "waiting", "requested", "pending"):
            row = self.row(status=status, conclusion=None)
            got = self.handle(Client(row), "wf_check_run_get").run(
                "wf_check_run_get", {"repo": REPO, "check_run_id": 7, "head_sha": SHA}
            )
            self.assertEqual(got["check"]["status"], status)
        for value in (
            [],
            {"total_count": True, "check_runs": []},
            {"total_count": 0, "check_runs": [self.row()]},
            {"total_count": 1, "check_runs": [None]},
        ):
            with self.assertRaises(RelayGitHubError):
                self.handle(Client(value), "wf_check_runs_page").run(
                    "wf_check_runs_page", self.args()
                )


class CheckPageRelayBoundary(unittest.TestCase):
    def relay(self, tmp, *, enabled=True, listed=True, checks=True):
        import json
        import os

        from git_relay.audit import AuditLog
        from git_relay.policy import SeatPolicy
        from git_relay.server import Relay

        perms = {"metadata": "read", **({"checks": "read"} if checks else {})}
        seats = {
            "swu-wise": {
                "token": "w" * 32,
                "actions": ["wf_check_runs_page", "wf_check_run_get"] if listed else ["wf_runs"],
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

        for action in ("wf_check_runs_page", "wf_check_run_get"):
            for case in ("disabled", "unlisted", "caller", "repo", "ceiling", "badargs"):
                with self.subTest(action=action, case=case), tempfile.TemporaryDirectory() as tmp:
                    r = self.relay(
                        tmp,
                        enabled=case != "disabled",
                        listed=case != "unlisted",
                        checks=case != "ceiling",
                    )
                    args = {"repo": "Other/Repo" if case == "repo" else REPO, "head_sha": SHA}
                    args.update(
                        {"check_name": "test"}
                        if action == "wf_check_runs_page"
                        else {"check_run_id": 7}
                    )
                    if case == "badargs":
                        args["unexpected"] = True
                    status, _ = r.handle("x" * 32 if case == "caller" else "w" * 32, action, args)
                    self.assertGreaterEqual(status, 400)
                    self.assertEqual(r.client.calls, [])
                    self.assertEqual(r.app_minter.calls, [])

    def test_actual_relay_dispatch_checks_read_and_no_write_or_cache(self):
        import tempfile

        for action in ("wf_check_runs_page", "wf_check_run_get"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp:
                r = self.relay(tmp)
                row = {
                    "id": 7,
                    "head_sha": SHA,
                    "name": "test",
                    "status": "completed",
                    "conclusion": "success",
                    "app": {"id": 8},
                }
                payload = (
                    {"total_count": 1, "check_runs": [row]}
                    if action == "wf_check_runs_page"
                    else row
                )
                r.client = Client(payload, payload)
                args = {
                    "repo": REPO,
                    "head_sha": SHA,
                    **(
                        {"check_name": "test"}
                        if action == "wf_check_runs_page"
                        else {"check_run_id": 7}
                    ),
                }
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
                    all(c[2] == {"checks": "read", "metadata": "read"} for c in r.app_minter.calls)
                )
