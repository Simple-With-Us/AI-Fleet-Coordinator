import importlib.util
import unittest
from unittest.mock import patch

s = importlib.util.spec_from_file_location("release", "scripts/deploy-alert-workers.py")
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)


class Tests(unittest.TestCase):
    def test_targets_cannot_expand_to_shared_workers(self):
        self.assertEqual(set(m.TARGETS), {"alert-ingress", "pr-conflict-watch"})
        self.assertIn("zulip-cutover", m.TARGETS["pr-conflict-watch"]["config"])

    def test_no_review_fails_closed(self):
        with patch.object(m, "github", return_value=[]), self.assertRaises(RuntimeError):
            m.reviewed("a" * 40)

    def test_unreviewed_merge_fails(self):
        with (
            patch.object(
                m,
                "github",
                side_effect=[[{"number": 1, "merged_at": "now", "merge_commit_sha": "a" * 40}], []],
            ),
            self.assertRaises(RuntimeError),
        ):
            m.reviewed("a" * 40)

    def test_no_exact_main_ci_fails(self):
        with (
            patch.object(m.subprocess, "check_output", return_value="a" * 40),
            patch.object(m, "current_main", return_value="a" * 40),
            patch.object(m, "github", return_value={"workflow_runs": []}),
            patch.object(m, "credential_receipt", return_value=True),
            self.assertRaises(RuntimeError),
        ):
            m.main()

    def test_superseded_does_not_release(self):
        with (
            patch.object(m.subprocess, "check_output", return_value="a" * 40),
            patch.object(m, "current_main", return_value="b" * 40) as current,
            patch.object(m, "credential_receipt", return_value=True),
            patch.object(m.subprocess, "run") as run,
        ):
            m.main()
            run.assert_not_called()
            current.assert_called_once()

    def test_missing_cutover_proof_skips_only_that_target(self):
        with patch.object(m.pathlib.Path, "exists", return_value=False):
            self.assertFalse(m.cutover_receipt())

    def test_synthetic_cutover_proof_fails(self):
        import json

        receipt = {
            "sender_id": 1212033,
            "channel_id": 642233,
            "source_sha": "a" * 40,
            "version_id": "verified",
            "message_id": 123,
            "message_url": "https://simplewithus.zulipchat.com/#narrow/id/123",
            "real_conflict_event": False,
        }
        with (
            patch.object(m.pathlib.Path, "exists", return_value=True),
            patch.object(m.pathlib.Path, "read_text", return_value=json.dumps(receipt)),
            self.assertRaises(RuntimeError),
        ):
            m.cutover_receipt()

    def test_review_includes_release_infrastructure(self):
        self.assertIn(".github/workflows/alert-workers-deploy.yml", m.RELEASE_PATHS)
        self.assertIn("scripts/deploy-mapped-worker.py", m.RELEASE_PATHS)

    def test_changed_kv_is_blocked_before_release(self):
        with (
            patch.object(
                m.pathlib.Path,
                "read_text",
                return_value='[[kv_namespaces]]\nbinding="PR_STATE"\nid="changed"\n',
            ),
            self.assertRaises(RuntimeError),
        ):
            m.preflight_config(
                {"config": "x"},
                {"bindings": [{"name": "PR_STATE", "namespace_id": "existing"}]},
            )

    def test_unchanged_kv_passes(self):
        with patch.object(
            m.pathlib.Path,
            "read_text",
            return_value='[[kv_namespaces]]\nbinding="PR_STATE"\nid="same"\n',
        ):
            m.preflight_config(
                {"config": "x"}, {"bindings": [{"name": "PR_STATE", "namespace_id": "same"}]}
            )

    def test_old_review_cannot_cover_changed_release_source(self):
        pr = {
            "number": 1,
            "merged_at": "now",
            "merge_commit_sha": "a" * 40,
            "head": {"sha": "b" * 40},
        }
        review = {
            "id": 1,
            "user": {"id": 10, "login": "kody-ai[bot]"},
            "state": "APPROVED",
            "commit_id": "c" * 40,
        }
        with (
            patch.object(m, "github", side_effect=[[pr], [review]]),
            patch.object(m, "git", side_effect=AssertionError("No old comparison allowed")),
            self.assertRaises(RuntimeError),
        ):
            m.reviewed("a" * 40)

    def test_review_with_unresolved_thread_fails(self):
        import json
        from unittest.mock import MagicMock

        pr = {
            "number": 1,
            "merged_at": "now",
            "merge_commit_sha": "a" * 40,
            "head": {"sha": "b" * 40},
        }
        review = {
            "id": 1,
            "user": {"id": 10, "login": "kody-ai[bot]"},
            "state": "APPROVED",
            "commit_id": "b" * 40,
        }
        response = MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [{"isResolved": False}],
                            }
                        }
                    }
                }
            }
        ).encode()
        with (
            patch.object(m, "github", side_effect=[[pr], [review]]),
            patch.dict(m.os.environ, {"GITHUB_REPOSITORY": "test/repo", "GH_TOKEN": "offline"}),
            patch.object(m.urllib.request, "build_opener") as opener,
        ):
            opener.return_value.open.return_value = response
            with self.assertRaises(RuntimeError):
                m.reviewed("a" * 40)

    def test_exact_review_and_resolved_threads_pass(self):
        import json
        from unittest.mock import MagicMock

        pr = {
            "number": 1,
            "merged_at": "now",
            "merge_commit_sha": "a" * 40,
            "head": {"sha": "b" * 40},
        }
        review = {
            "id": 1,
            "user": {"id": 10, "login": "kody-ai[bot]"},
            "state": "COMMENTED",
            "commit_id": "b" * 40,
        }
        response = MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "pageInfo": {"hasNextPage": False},
                                "nodes": [{"isResolved": True}],
                            }
                        }
                    }
                }
            }
        ).encode()
        with (
            patch.object(m, "github", side_effect=[[pr], [review]]),
            patch.dict(m.os.environ, {"GITHUB_REPOSITORY": "test/repo", "GH_TOKEN": "offline"}),
            patch.object(m.urllib.request, "build_opener") as opener,
        ):
            opener.return_value.open.return_value = response
            m.reviewed("a" * 40)

    def test_workflow_is_main_ci_and_catchup_without_broad_gate(self):
        text = m.pathlib.Path(".github/workflows/alert-workers-deploy.yml").read_text()
        self.assertIn("workflow_run:", text)
        self.assertIn("workflows: [CI]", text)
        self.assertNotIn("WORKER_DEPLOY_ENABLED", text)
        self.assertNotIn("agent-sync-mcp/wrangler", text)
        self.assertIn("17,47 * * * *", text)

    def test_numeric_receipt_not_boolean(self):
        import json

        receipt = {
            "sender_id": 1212033,
            "channel_id": 642233,
            "source_sha": "a" * 40,
            "version_id": "verified",
            "message_id": 123,
            "message_url": "https://simplewithus.zulipchat.com/#narrow/id/123",
            "real_conflict_event": 1,
        }
        with (
            patch.object(m.pathlib.Path, "exists", return_value=True),
            patch.object(m.pathlib.Path, "read_text", return_value=json.dumps(receipt)),
            self.assertRaises(RuntimeError),
        ):
            m.cutover_receipt()

    def test_redirect_handler_refuses_forward(self):
        self.assertIsNone(
            m.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://other.invalid")
        )

    def test_health_oversize_fails(self):
        from unittest.mock import MagicMock

        response = MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = b"x" * 65537
        with patch.object(m.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value = response
            with self.assertRaises(RuntimeError):
                m.probe({"health": "https://example.invalid", "ok": True})

    def test_numeric_health_not_boolean(self):
        from unittest.mock import MagicMock

        response = MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read.return_value = b'{"ok":1}'
        with patch.object(m.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value = response
            with self.assertRaises(RuntimeError):
                m.probe({"health": "https://example.invalid", "ok": True})

    def test_null_annotations_fail_baseline(self):
        with patch.object(
            m,
            "cloudflare",
            side_effect=[
                {"deployments": [{"versions": [{"version_id": "x", "percentage": 100}]}]},
                {"annotations": None},
            ],
        ):
            self.assertIsNone(m.deployed_tag("alert-ingress"))

    def test_api_message_is_controlled_failure(self):
        with (
            patch.dict(m.os.environ, {"GITHUB_REPOSITORY": "test/repo", "GH_TOKEN": "offline"}),
            patch.object(m, "request_json", return_value={"message": "refused"}),
            self.assertRaises(RuntimeError),
        ):
            m.github("/anything")

    def test_qualification_cannot_deploy(self):
        with (
            patch.dict(m.os.environ, {"CLOUDFLARE_API_TOKEN": "offline", "GH_TOKEN": "offline"}),
            patch.object(m, "current_main"),
            patch.object(m, "cloudflare", return_value={}),
            patch.object(m, "preflight_config"),
            patch.object(m, "deployed_tag", return_value="a" * 40),
            patch.object(m, "probe"),
            patch.object(m.subprocess, "run") as run,
        ):
            m.qualify_credentials()
            run.assert_not_called()

    def test_missing_qualification_credential_fails(self):
        with patch.dict(m.os.environ, {}, clear=True), self.assertRaises(RuntimeError):
            m.qualify_credentials()

    def test_first_automatic_run_cannot_deploy(self):
        with (
            patch.object(m, "credential_receipt", return_value=False),
            patch.object(m.subprocess, "run") as run,
            patch.object(m, "current_main") as current,
        ):
            with self.assertRaises(RuntimeError):
                m.main()
            run.assert_not_called()
            current.assert_not_called()

    def receipt_fixture(self, **changes):
        receipt = {"run_id": 123, "source_sha": "a" * 40, "nondeployment": True}
        receipt.update(changes)
        run = {
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
            "head_sha": "a" * 40,
            "head_branch": "main",
            "path": ".github/workflows/alert-workers-deploy.yml",
        }
        jobs = {
            "total_count": 1,
            "jobs": [
                {
                    "steps": [
                        {
                            "name": "Qualify existing credential delivery without deployment",
                            "conclusion": "success",
                        },
                        {
                            "name": "Verify reviews and release exact alert targets",
                            "conclusion": "skipped",
                        },
                    ]
                }
            ],
        }
        return receipt, run, jobs

    def check_receipt(self, receipt, run, jobs, changed=""):
        import json

        with (
            patch.object(m.pathlib.Path, "exists", return_value=True),
            patch.object(m.pathlib.Path, "read_text", return_value=json.dumps(receipt)),
            patch.object(m, "github", side_effect=[run, jobs]),
            patch.object(m, "git", return_value=changed) as git,
        ):
            result = m.credential_receipt()
            self.assertIn("scripts/worker-targets.json", git.call_args.args)
            self.assertIn("scripts/agent-sync-mcp/package-lock.json", git.call_args.args)
            return result

    def test_qualified_receipt_passes(self):
        self.assertTrue(self.check_receipt(*self.receipt_fixture()))

    def test_numeric_nondeployment_receipt_fails(self):
        with self.assertRaises(RuntimeError):
            self.check_receipt(*self.receipt_fixture(nondeployment=1))

    def test_missing_jobs_fails_controlled(self):
        receipt, run, _ = self.receipt_fixture()
        with self.assertRaises(RuntimeError):
            self.check_receipt(receipt, run, {"total_count": 0})

    def test_executed_release_invalidates_qualification(self):
        receipt, run, jobs = self.receipt_fixture()
        jobs["jobs"][0]["steps"][1]["conclusion"] = "success"
        with self.assertRaises(RuntimeError):
            self.check_receipt(receipt, run, jobs)

    def test_changed_release_source_invalidates_receipt(self):
        with self.assertRaises(RuntimeError):
            self.check_receipt(*self.receipt_fixture(), changed="scripts/worker-targets.json")

    def test_effective_request_superseded_only_by_same_reviewer_approval(self):
        request = {"id": 1, "user": {"id": 10}, "state": "CHANGES_REQUESTED"}
        approved = {"id": 2, "user": {"id": 10}, "state": "APPROVED"}
        self.assertFalse(m.outstanding_requests([approved, request]))
        self.assertTrue(m.outstanding_requests([approved, {**request, "id": 3}]))
        self.assertTrue(m.outstanding_requests([request, {**approved, "user": {"id": 11}}]))
        self.assertTrue(m.outstanding_requests([request, {**approved, "state": "COMMENTED"}]))

    def test_missing_formal_reviewer_fails(self):
        with self.assertRaises(RuntimeError):
            m.outstanding_requests([{"id": 1, "state": "APPROVED"}])

    def test_irrelevant_reviews_do_not_require_sequence(self):
        self.assertFalse(
            m.outstanding_requests([{"state": "COMMENTED"}, {"state": "PENDING", "user": None}])
        )
        self.assertTrue(
            m.outstanding_requests(
                [
                    {"state": "COMMENTED"},
                    {"id": 1, "state": "CHANGES_REQUESTED", "user": {"id": 10}},
                ]
            )
        )

    def test_null_formal_user_is_controlled_failure(self):
        for user in [None, False, "invalid", {}, {"id": True}]:
            with self.subTest(user=user), self.assertRaises(RuntimeError):
                m.outstanding_requests([{"id": 1, "state": "APPROVED", "user": user}])


if __name__ == "__main__":
    unittest.main()
