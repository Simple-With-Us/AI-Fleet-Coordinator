import importlib.util
import json
import unittest
from unittest.mock import patch

s = importlib.util.spec_from_file_location("release", "scripts/deploy-alert-workers.py")
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)

# A second alert target for the tests that prove one target cannot block another.
# alert-ingress is the only real one since pr-conflict-watch retired on 2026-11-06.
OTHER = {
    "deployer": "alert-workers-deploy",
    "root": "workers/other/",
    "config": "workers/other/wrangler.toml",
    "paths": ["workers/other/src/**"],
    "ignore": ["*.test.*"],
    "health": "https://other.invalid/health",
    "ok": True,
}


def two_targets():
    return patch.dict(m.TARGETS, {"other-worker": OTHER})


class Tests(unittest.TestCase):
    def test_targets_cannot_expand_to_shared_workers(self):
        self.assertEqual(set(m.TARGETS), {"alert-ingress"})

    def test_retired_worker_is_not_a_target(self):
        self.assertNotIn("pr-conflict-watch", m.TARGETS)
        self.assertNotIn("pr-conflict-watch", m.m.TARGETS)

    def test_no_worker_is_mapped_to_both_pipelines(self):
        import re

        workflow = m.pathlib.Path(".github/workflows/worker-deploy.yml").read_text()
        matrix = json.loads(
            re.search(r"^\s*worker:\s*(\[.*\])\s*$", workflow, re.MULTILINE).group(1)
        )
        self.assertFalse(set(matrix) & set(m.TARGETS))

    def test_tripped_target_is_refused_and_the_other_still_runs(self):
        order = []

        def tripped(name):
            order.append(name)
            return name == "alert-ingress"

        with (
            two_targets(),
            patch.object(m.subprocess, "check_output", return_value="a" * 40),
            patch.object(m.subprocess, "run") as run,
            patch.object(m, "current_main", return_value="a" * 40),
            patch.object(m, "credential_receipt", return_value=True),
            patch.object(
                m,
                "github",
                return_value={
                    "workflow_runs": [
                        {"head_sha": "a" * 40, "status": "completed", "conclusion": "success"}
                    ]
                },
            ),
            patch.object(m, "guard_tripped", side_effect=tripped),
            patch.object(m, "deployed_tag", return_value="a" * 40),
            patch.object(m.m, "worker_changed", return_value=False),
            patch.object(m, "probe"),
        ):
            with self.assertRaisesRegex(RuntimeError, "budget guard tripped: alert-ingress"):
                m.main()
            for call in run.call_args_list:
                self.assertNotIn("deploy", call.args[0])
        self.assertEqual(order, ["alert-ingress", "other-worker"])

    def test_unrelated_merge_does_not_release(self):
        with (
            patch.object(m.subprocess, "check_output", return_value="a" * 40),
            patch.object(m.subprocess, "run") as run,
            patch.object(m, "current_main", return_value="a" * 40),
            patch.object(m, "credential_receipt", return_value=True),
            patch.object(
                m,
                "github",
                return_value={
                    "workflow_runs": [
                        {"head_sha": "a" * 40, "status": "completed", "conclusion": "success"}
                    ]
                },
            ),
            patch.object(m, "guard_tripped", return_value=False),
            patch.object(m, "deployed_tag", return_value="b" * 40),
            patch.object(m.m, "worker_changed", return_value=False),
            patch.object(m, "probe") as probe,
        ):
            m.main()
        for call in run.call_args_list:
            self.assertNotIn("deploy", call.args[0])
        self.assertEqual(probe.call_count, 1)

    def test_release_refuses_to_drop_live_limits(self):
        with (
            patch.object(m.pathlib.Path, "read_text", return_value='name = "x"\n'),
            self.assertRaisesRegex(RuntimeError, "drop live limits"),
        ):
            m.preflight_config({"config": "x"}, {"limits": {"cpu_ms": 200}})

    def test_qualification_reads_every_target(self):
        checked = []
        with (
            patch.dict(m.os.environ, {"CLOUDFLARE_API_TOKEN": "offline", "GH_TOKEN": "offline"}),
            patch.object(m, "current_main"),
            patch.object(m, "guard_tripped", return_value=False),
            patch.object(m, "cloudflare", return_value={}),
            patch.object(m, "preflight_config"),
            patch.object(
                m, "deployed_tag", side_effect=lambda name: checked.append(name) or "a" * 40
            ),
            patch.object(m, "probe"),
        ):
            m.qualify_credentials()
        self.assertEqual(checked, ["alert-ingress"])

    def test_tripped_target_does_not_fail_qualification_for_the_other(self):
        # The qualify step runs first on every trigger, so a raise here would skip the
        # release step and stop the healthy Worker's release too.
        probed = []
        with (
            two_targets(),
            patch.dict(m.os.environ, {"CLOUDFLARE_API_TOKEN": "offline", "GH_TOKEN": "offline"}),
            patch.object(m, "current_main"),
            patch.object(m, "guard_tripped", side_effect=lambda name: name == "alert-ingress"),
            patch.object(m, "cloudflare", return_value={}) as read,
            patch.object(m, "preflight_config") as preflight,
            patch.object(m, "deployed_tag", return_value="a" * 40) as tag,
            patch.object(m, "probe", side_effect=probed.append),
        ):
            m.qualify_credentials()
            names = list(m.TARGETS)
        self.assertEqual(probed, [OTHER])
        # The receipt's proof holds for the stopped Worker too: only the probe is skipped.
        self.assertEqual(preflight.call_count, 2)
        self.assertEqual([c.args[0] for c in tag.call_args_list], names)
        reads = [c.args[0] for c in read.call_args_list]
        self.assertIn("/workers/scripts/alert-ingress/settings", reads)
        self.assertIn("/workers/scripts/other-worker/settings", reads)

    def qualify_with_one_stopped(self, **patches):
        """Qualify with alert-ingress stopped and a second target running; returns the probes."""
        probed = []
        with (
            two_targets(),
            patch.dict(m.os.environ, {"CLOUDFLARE_API_TOKEN": "offline", "GH_TOKEN": "offline"}),
            patch.object(m, "current_main"),
            patch.object(m, "guard_tripped", side_effect=lambda name: name == "alert-ingress"),
            patch.object(m, "preflight_config", patches.get("preflight", lambda *args: None)),
            patch.object(m, "cloudflare", patches.get("read", lambda path: {})),
            patch.object(m, "deployed_tag", patches.get("tag", lambda name: "a" * 40)),
            patch.object(m, "probe", side_effect=probed.append),
        ):
            m.qualify_credentials()
        return probed

    def test_a_stopped_target_still_needs_the_credential_read(self):
        def read(path):
            if "alert-ingress" in path:
                raise RuntimeError("Cloudflare read rejected")
            return {}

        with self.assertRaisesRegex(RuntimeError, "read rejected"):
            self.qualify_with_one_stopped(read=read)

    def test_a_stopped_targets_preconditions_cannot_block_the_other_target(self):
        running = [OTHER]

        def no_baseline(name):
            return None if name == "alert-ingress" else "a" * 40

        def drifted(target, settings):
            if target is m.TARGETS["alert-ingress"]:
                raise RuntimeError("Release would drop live limits")

        self.assertEqual(self.qualify_with_one_stopped(tag=no_baseline), running)
        self.assertEqual(self.qualify_with_one_stopped(preflight=drifted), running)
        # A transport failure or a malformed answer is the same to a target not released.
        for failure in (m.urllib.error.URLError("x"), KeyError("deployments"), ValueError("x")):

            def broken(name, failure=failure):
                if name == "alert-ingress":
                    raise failure
                return "a" * 40

            self.assertEqual(self.qualify_with_one_stopped(tag=broken), running)

    def test_a_running_targets_preconditions_still_fail_qualification(self):
        def no_baseline(name):
            return None if name == "other-worker" else "a" * 40

        def drifted(target, settings):
            if target is OTHER:
                raise RuntimeError("Release would drop live limits")

        with self.assertRaisesRegex(RuntimeError, "Missing qualified live version baseline"):
            self.qualify_with_one_stopped(tag=no_baseline)
        with self.assertRaisesRegex(RuntimeError, "drop live limits"):
            self.qualify_with_one_stopped(preflight=drifted)

    def release(self, guard, tags):
        """Run main() through a real target change, with every external edge mocked."""
        sha = "a" * 40
        with (
            patch.object(m.subprocess, "check_output", return_value=sha),
            patch.object(m.subprocess, "run") as run,
            patch.object(m, "current_main", return_value=sha),
            patch.object(m, "credential_receipt", return_value=True),
            patch.object(
                m,
                "github",
                return_value={
                    "workflow_runs": [
                        {"head_sha": sha, "status": "completed", "conclusion": "success"}
                    ]
                },
            ),
            patch.object(m, "guard_tripped", side_effect=guard),
            patch.object(m, "deployed_tag", side_effect=tags),
            patch.object(m.m, "worker_changed", return_value=True),
            patch.object(m, "git", return_value=""),
            patch.object(m, "reviewed"),
            patch.object(m, "cloudflare", return_value={}),
            patch.object(m, "preflight_config"),
            patch.object(m, "probe"),
        ):
            try:
                m.main()
            except RuntimeError as error:
                return run, error
        return run, None

    def uploads(self, run):
        return [c.args[0] for c in run.call_args_list if "--tag" in c.args[0]]

    def test_changed_target_releases_with_the_exact_tag(self):
        run, error = self.release([False, False], ["b" * 40, "a" * 40])
        self.assertIsNone(error)
        uploads = self.uploads(run)
        self.assertEqual(len(uploads), 1)
        self.assertIn("a" * 40, uploads[0])
        self.assertIn("--keep-vars", uploads[0])

    def test_trip_during_the_release_refuses_the_upload(self):
        run, error = self.release([False, True], ["b" * 40])
        self.assertIn("budget guard tripped: alert-ingress", str(error))
        self.assertEqual(self.uploads(run), [])

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

    def test_review_includes_release_infrastructure(self):
        self.assertIn(".github/workflows/alert-workers-deploy.yml", m.RELEASE_PATHS)
        self.assertIn("scripts/deploy-mapped-worker.py", m.RELEASE_PATHS)

    def test_changed_kv_is_blocked_before_release(self):
        with (
            patch.object(
                m.pathlib.Path,
                "read_text",
                return_value='[[kv_namespaces]]\nbinding="STATE"\nid="changed"\n',
            ),
            self.assertRaises(RuntimeError),
        ):
            m.preflight_config(
                {"config": "x"},
                {"bindings": [{"name": "STATE", "namespace_id": "existing"}]},
            )

    def test_unchanged_kv_passes(self):
        with patch.object(
            m.pathlib.Path,
            "read_text",
            return_value='[[kv_namespaces]]\nbinding="STATE"\nid="same"\n',
        ):
            m.preflight_config(
                {"config": "x"}, {"bindings": [{"name": "STATE", "namespace_id": "same"}]}
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
            patch.object(
                m,
                "github",
                side_effect=[
                    [pr],
                    [
                        {"state": "PENDING", "user": None},
                        {"state": "COMMENTED", "user": "invalid"},
                        review,
                    ],
                ],
            ),
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
            patch.object(
                m,
                "github",
                side_effect=[
                    [pr],
                    [
                        {"state": "PENDING", "user": None},
                        {"state": "COMMENTED", "user": "invalid"},
                        review,
                    ],
                ],
            ),
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
            patch.object(
                m,
                "github",
                side_effect=[
                    [pr],
                    [
                        {"state": "PENDING", "user": None},
                        {"state": "COMMENTED", "user": "invalid"},
                        review,
                    ],
                ],
            ),
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
        self.assertNotIn("simple-tools-mcp/wrangler", text)
        self.assertIn("17,47 * * * *", text)

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
            patch.object(m, "guard_tripped", return_value=False),
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
        receipt = {
            "run_id": 123,
            "source_sha": "a" * 40,
            "nondeployment": True,
            "gate": m.RECEIPT_GATE,
        }
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

    WORKFLOW = """\
jobs:
  deploy:
    steps:
      - uses: actions/setup-python@v7
      - name: Load existing scoped release credential
        env:
          INFISICAL_NAMES: CLOUDFLARE_FLEET_API_TOKEN
        run: node scripts/infisical-fetch.mjs
      - name: Qualify existing credential delivery without deployment
        run: python3 scripts/deploy-alert-workers.py --qualify-only
      - name: Verify reviews and release exact alert targets
        run: python3 scripts/deploy-alert-workers.py
"""

    def check_receipt(self, receipt, run, jobs, changed="", workflow=None):
        import json

        def git(*args):
            if args[0] == "diff":
                self.assertEqual(args[-1], "scripts/infisical-fetch.mjs")
                return changed
            if args[1].startswith("HEAD:"):
                return workflow or self.WORKFLOW
            return self.WORKFLOW

        with (
            patch.object(m.pathlib.Path, "exists", return_value=True),
            patch.object(m.pathlib.Path, "read_text", return_value=json.dumps(receipt)),
            patch.object(m, "github", side_effect=[run, jobs]),
            patch.object(m, "git", side_effect=git),
        ):
            return m.credential_receipt()

    def test_qualified_receipt_passes(self):
        self.assertTrue(self.check_receipt(*self.receipt_fixture()))

    def test_receipt_from_before_this_gate_cannot_arm_a_release(self):
        with self.assertRaisesRegex(RuntimeError, "predates this gate"):
            self.check_receipt(*self.receipt_fixture(gate=None))

    def test_credential_unrelated_workflow_edit_keeps_the_receipt(self):
        bumped = self.WORKFLOW.replace("setup-python@v7", "setup-python@v8")
        bumped = bumped.replace(
            "run: python3 scripts/deploy-alert-workers.py\n",
            "run: python3 scripts/deploy-alert-workers.py # x\n",
        )
        self.assertTrue(self.check_receipt(*self.receipt_fixture(), workflow=bumped))

    def test_credential_step_edit_voids_the_receipt(self):
        changed = self.WORKFLOW.replace("CLOUDFLARE_FLEET_API_TOKEN", "OTHER_TOKEN")
        with self.assertRaisesRegex(RuntimeError, "source changed"):
            self.check_receipt(*self.receipt_fixture(), workflow=changed)

    def test_qualify_step_edit_voids_the_receipt(self):
        changed = self.WORKFLOW.replace("--qualify-only", "--qualify-only --skip")
        with self.assertRaisesRegex(RuntimeError, "source changed"):
            self.check_receipt(*self.receipt_fixture(), workflow=changed)

    def test_missing_credential_step_voids_the_receipt(self):
        changed = self.WORKFLOW.replace("Load existing scoped release credential", "Renamed")
        with self.assertRaisesRegex(RuntimeError, "Credential step not found"):
            self.check_receipt(*self.receipt_fixture(), workflow=changed)

    def test_real_workflow_has_the_credential_steps(self):
        text = m.pathlib.Path(m.CREDENTIAL_WORKFLOW).read_text()
        blocks = m.workflow_credential_steps(text)
        self.assertIn("infisical-fetch.mjs", blocks[0])
        self.assertIn("--qualify-only", blocks[1])
        self.assertNotIn("Verify reviews", blocks[1])

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
            self.check_receipt(*self.receipt_fixture(), changed="scripts/infisical-fetch.mjs")

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
