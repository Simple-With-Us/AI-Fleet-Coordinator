import unittest
from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import Refused, WorkflowVerbs
from .test_workflow_verbs import REPO, SHA, Client, Policy, Minter


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
