import unittest

from git_relay.github_client import RelayGitHubError
from git_relay.workflow_verbs import Refused, WorkflowVerbs

from .test_workflow_verbs import REPO, SHA, Client, Minter, Policy

ACTIONS = [
    "wf_workflow_get",
    "wf_artifact_get",
    "wf_artifacts_page",
    "wf_caches_page",
    "wf_cache_usage",
    "wf_run_attempt_get",
    "wf_run_attempt_jobs",
    "wf_run_pending_deployments",
]


class Inventory(unittest.TestCase):
    def call(self, a, replies, **kw):
        c = Client(*replies)
        h = WorkflowVerbs(c, Policy(), "swu-wise", "own", "swu-wise", a, REPO)
        return h.run(a, {"repo": REPO, **kw}), c

    def artifact(self, **kw):
        return dict(
            id=3,
            name="build",
            size_in_bytes=9,
            expired=False,
            workflow_run={"id": 4, "head_sha": SHA},
            **kw,
        )

    def runrow(self, **kw):
        row = dict(id=4, head_sha=SHA, run_attempt=2, status="completed", conclusion="success")
        row.update(kw)
        return row

    def identity(self):
        return dict(run_id=4, head_sha=SHA, run_attempt=2)

    def test_fixed_paths_exact_id_projection(self):
        a = self.artifact(url="private", archive_download_url="private")
        out, c = self.call("wf_artifact_get", [a], artifact_id=3)
        self.assertEqual(c.calls[0][1], f"/repos/{REPO}/actions/artifacts/3")
        self.assertNotIn("private", str(out))
        out, c = self.call(
            "wf_workflow_get",
            [dict(id=7, path="ruby.yaml", state="active", url="private")],
            workflow_id=7,
        )
        self.assertEqual(out, {"workflow": {"id": 7, "path": "ruby.yaml", "state": "active"}})
        with self.assertRaises(RelayGitHubError):
            self.call("wf_artifact_get", [a], artifact_id=8)

    def test_pages_mutable_bounded_unique(self):
        a = self.artifact()
        out, c = self.call(
            "wf_artifacts_page", [{"total_count": 1, "artifacts": [a]}], page=2, per_page=1
        )
        self.assertTrue(out["has_more"])
        self.assertFalse(out["snapshot"])
        self.assertIn("per_page=1&page=2", c.calls[0][1])
        cache = dict(
            id=2, key="build-key", ref="refs/heads/main", size_in_bytes=4, version="private"
        )
        out, _ = self.call("wf_caches_page", [{"total_count": 1, "actions_caches": [cache]}])
        self.assertNotIn("private", str(out))
        for rows, total in [([a, a], 2), ([a], 0), ([a], True)]:
            with self.subTest(rows=rows, total=total), self.assertRaises(RelayGitHubError):
                self.call("wf_artifacts_page", [{"total_count": total, "artifacts": rows}])

    def test_malformed_bounds_secret_text_refused(self):
        for field, value in [
            ("id", True),
            ("size_in_bytes", -1),
            ("expired", 1),
            ("name", "ghp_" + "a" * 30),
            ("workflow_run", {"id": 4, "head_sha": "b" * 39}),
        ]:
            a = self.artifact()
            a[field] = value
            with self.subTest(field=field), self.assertRaises((Refused, RelayGitHubError)):
                self.call("wf_artifact_get", [a], artifact_id=3)
        for value in [
            None,
            [],
            {"id": None, "name": "x"},
            {"id": 1, "name": "x", "state": "invented"},
        ]:
            with self.assertRaises(RelayGitHubError):
                self.call("wf_workflow_get", [value], workflow_id=1)

    def test_attempts_exact_run_head_and_attempt(self):
        out, c = self.call("wf_run_attempt_get", [self.runrow()], **self.identity())
        self.assertEqual(out["run"]["id"], 4)
        self.assertTrue(c.calls[0][1].endswith("/runs/4/attempts/2"))
        for delta in [
            dict(id=5),
            dict(head_sha="b" * 40),
            dict(run_attempt=True),
            dict(run_attempt=3),
            dict(status="completed", conclusion=None),
            dict(status="in_progress", conclusion="success"),
        ]:
            with self.subTest(delta=delta), self.assertRaises(RelayGitHubError):
                self.call("wf_run_attempt_get", [self.runrow(**delta)], **self.identity())
        job = self.runrow(id=8, run_id=4, runner_name="private")
        out, c = self.call(
            "wf_run_attempt_jobs",
            [{"total_count": 1, "jobs": [job]}],
            **self.identity(),
            per_page=1,
        )
        self.assertTrue(out["has_more"])
        self.assertNotIn("private", str(out))
        self.assertTrue(c.calls[0][1].endswith("/attempts/2/jobs?per_page=1&page=1"))

    def test_pending_two_run_observations_no_approval(self):
        env = dict(
            environment={"id": 9, "name": "prod", "url": "private"},
            wait_timer=10,
            current_user_can_approve=False,
            reviewers=["private"],
        )
        out, c = self.call(
            "wf_run_pending_deployments", [self.runrow(), [env], self.runrow()], **self.identity()
        )
        self.assertFalse(out["snapshot"])
        self.assertNotIn("private", str(out))
        self.assertEqual(len(c.calls), 3)
        self.assertTrue(all(x[0] == "GET" for x in c.calls))
        with self.assertRaises(Refused):
            self.call(
                "wf_run_pending_deployments",
                [self.runrow(), [env], self.runrow(run_attempt=3)],
                **self.identity(),
            )

    def test_cache_usage_repo_correlated(self):
        row = dict(full_name=REPO, active_caches_count=2, active_caches_size_in_bytes=4)
        out, _ = self.call("wf_cache_usage", [row])
        self.assertEqual(out["repo"], REPO)
        row["full_name"] = "wrong/repo"
        with self.assertRaises(RelayGitHubError):
            self.call("wf_cache_usage", [row])

    def test_own_app_read_only_ceiling_and_shape(self):
        args = {
            "wf_workflow_get": dict(workflow_id=1),
            "wf_artifact_get": dict(artifact_id=1),
            "wf_artifacts_page": {},
            "wf_caches_page": {},
            "wf_cache_usage": {},
            "wf_run_attempt_get": self.identity(),
            "wf_run_attempt_jobs": self.identity(),
            "wf_run_pending_deployments": self.identity(),
        }
        for a in ACTIONS:
            m = Minter()
            WorkflowVerbs.for_action(Client(), Policy(), m, "swu-wise", a, REPO)
            self.assertEqual(m.calls[0][2], {"actions": "read", "metadata": "read"})
            WorkflowVerbs.check_shape(a, dict(repo=REPO, **args[a]), REPO)
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(a, dict(repo=REPO, url="bad", **args[a]), REPO)

    def test_real_relay_dispatch_and_refusal_before_mint(self):
        import json
        import tempfile

        from git_relay.audit import AuditLog
        from git_relay.policy import SeatPolicy
        from git_relay.server import Relay

        token = "w" * 32
        for action, args, row in [
            (
                "wf_cache_usage",
                {},
                dict(full_name=REPO, active_caches_count=1, active_caches_size_in_bytes=2),
            ),
            ("wf_run_attempt_get", self.identity(), self.runrow()),
            ("wf_artifacts_page", {}, dict(total_count=0, artifacts=[])),
        ]:
            with tempfile.TemporaryDirectory() as tmp:
                config = {
                    "swu-wise": {
                        "token": token,
                        "actions": [action],
                        "repos": [REPO],
                        "app_slug": "swu-wise",
                        "mint_permissions": {"actions": "read", "metadata": "read"},
                    }
                }
                c = Client(row)
                m = Minter()
                r = Relay(
                    SeatPolicy(json.dumps(config)),
                    c,
                    AuditLog(tmp + "/audit"),
                    app_minter=m,
                    workflow_verbs_enabled=True,
                )
                self.assertEqual(r.handle(token, action, dict(repo=REPO, **args))[0], 200)
                self.assertEqual(len(m.calls), 1)
                self.assertEqual(c.calls[0][3], "opaque-installation-bearer")
                for bearer, request, expected in [
                    (token, dict(repo=REPO, url="bad", **args), 400),
                    ("wrong", dict(repo=REPO, **args), 401),
                    (token, dict(repo="wrong/repo", **args), 403),
                ]:
                    self.assertEqual(r.handle(bearer, action, request)[0], expected)
                self.assertEqual(len(m.calls), 1)
