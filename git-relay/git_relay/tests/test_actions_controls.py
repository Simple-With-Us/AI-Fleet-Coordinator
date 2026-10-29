import unittest

from git_relay.workflow_verbs import Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, SHA, Client, Minter, Policy


class ActionsControls(unittest.TestCase):
    def row(self, **changes):
        return {
            "id": 2,
            "run_id": 3,
            "head_sha": SHA,
            "run_attempt": 4,
            "status": "completed",
            "conclusion": "success",
            **changes,
        }

    def call(self, action, row, **extra):
        c = Client(row, {})
        h = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", action, REPO)
        args = {"repo": REPO, "run_id": 3, "head_sha": SHA, "run_attempt": 4, **extra}
        return h.run(action, args), c

    def test_job_read_is_correlated_projection(self):
        out, c = self.call("wf_job_get", self.row(runner_name="private"), job_id=2)
        self.assertEqual(c.calls[0][1], f"/repos/{REPO}/actions/jobs/2")
        self.assertNotIn("private", str(out))

    def test_fixed_request_paths_and_no_debugging(self):
        cases = [
            ("wf_job_rerun", self.row(), {"job_id": 2}, "jobs/2/rerun", "POST"),
            ("wf_run_rerun_all", self.row(id=3), {}, "runs/3/rerun", "POST"),
            (
                "wf_run_force_cancel",
                self.row(id=3, status="in_progress", conclusion=None),
                {},
                "runs/3/force-cancel",
                "POST",
            ),
            ("wf_run_logs_delete", self.row(id=3), {}, "runs/3/logs", "DELETE"),
        ]
        for a, row, args, suffix, method in cases:
            out, c = self.call(a, row, **args)
            self.assertEqual(c.calls[-1][0], method)
            self.assertEqual(c.calls[-1][1], f"/repos/{REPO}/actions/{suffix}")
            self.assertFalse(out["completed"])
            self.assertFalse(out["atomic"])
            if "rerun" in a:
                self.assertEqual(c.calls[-1][2], {"enable_debug_logging": False})

    def test_wrong_id_head_attempt_or_state_refuses_before_write(self):
        for change in [
            {"id": 4},
            {"run_id": 4},
            {"head_sha": "b" * 40},
            {"run_attempt": True},
            {"run_attempt": 5},
            {"status": "invented"},
            {"status": "completed", "conclusion": None},
            {"status": "in_progress", "conclusion": None},
        ]:
            with self.subTest(change=change), self.assertRaises(Refused):
                self.call("wf_job_rerun", self.row(**change), job_id=2)

    def test_exact_completion_status_contract(self):
        class StatusClient(Client):
            def _call(self, *args, **kwargs):
                if args[0] != "GET":
                    self.status = kwargs["expected_status"]
                return super()._call(*args, **kwargs)

        for action, status in [
            ("wf_job_rerun", 201),
            ("wf_run_rerun_all", 201),
            ("wf_run_force_cancel", 202),
            ("wf_run_logs_delete", 204),
        ]:
            job = action.startswith("wf_job_")
            row = self.row(id=2 if job else 3)
            if action.endswith("force_cancel"):
                row.update(status="in_progress", conclusion=None)
            c = StatusClient(row, {})
            h = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", action, REPO)
            h.run(
                action,
                {
                    "repo": REPO,
                    "run_id": 3,
                    "head_sha": SHA,
                    "run_attempt": 4,
                    **({"job_id": 2} if job else {}),
                },
            )
            self.assertEqual(c.status, status)

    def test_own_app_minimal_actions_permissions(self):
        for a in [
            "wf_job_get",
            "wf_job_rerun",
            "wf_run_rerun_all",
            "wf_run_force_cancel",
            "wf_run_logs_delete",
        ]:
            m = Minter()
            WorkflowVerbs.for_action(Client(), Policy(), m, "swu-wise", a, REPO)
            self.assertEqual(
                m.calls[0][2],
                {"actions": "read" if a == "wf_job_get" else "write", "metadata": "read"},
            )


class ReviewFixes(unittest.TestCase):
    def test_empty_json_completion_opt_in_never_relaxes_204(self):
        import io

        from git_relay.github_client import GitHubClient, RelayGitHubError

        class Response(io.BytesIO):
            def __init__(self, raw, status):
                super().__init__(raw)
                self.status = status

        for status in [201, 202]:
            for raw in [b"", b"{}", b" { }\n"]:
                c = GitHubClient(
                    "synthetic",
                    opener=lambda *a, raw=raw, status=status, **k: Response(raw, status),
                )
                self.assertEqual(
                    c._call("POST", "/fixture", expected_status=status, allow_empty_object=True), {}
                )
            for raw in [b"[]", b"null", b'{"extra":1}', b"bad"]:
                c = GitHubClient(
                    "synthetic",
                    opener=lambda *a, raw=raw, status=status, **k: Response(raw, status),
                )
                with self.assertRaises(RelayGitHubError):
                    c._call("POST", "/fixture", expected_status=status, allow_empty_object=True)
        c = GitHubClient("synthetic", opener=lambda *a, **k: Response(b"{}", 204))
        with self.assertRaises(RelayGitHubError):
            c._call("DELETE", "/fixture", expected_status=204, allow_empty_object=True)

    def test_transient_prereads_retryable_before_mutation(self):
        from git_relay.github_client import RelayGitHubError
        from git_relay.server import PreReadUnavailable

        class C(Client):
            def _call(self, *a, **k):
                self.calls.append(a)
                raise RelayGitHubError(503, "synthetic")

        for action, args in [
            (
                "wf_review_requests_remove",
                {"number": 3, "expected_head_sha": SHA, "reviewers": ["bot"]},
            ),
            ("wf_job_rerun", {"job_id": 2, "run_id": 3, "head_sha": SHA, "run_attempt": 4}),
        ]:
            c = C()
            h = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", action, REPO)
            with self.assertRaises(PreReadUnavailable):
                h.run(action, {"repo": REPO, **args})
            self.assertEqual(len(c.calls), 1)
            self.assertEqual(c.calls[0][0], "GET")
