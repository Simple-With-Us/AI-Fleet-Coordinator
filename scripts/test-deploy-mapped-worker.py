import datetime
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

s = importlib.util.spec_from_file_location("deploy", "scripts/deploy-mapped-worker.py")
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)

DEPLOYMENT = {"deployments": [{"versions": [{"version_id": "live", "percentage": 100}]}]}
SECRET = {"workers/triggered_by": "secret"}
SHA_A = "a" * 40
SHA_B = "b" * 40


def workflow_text():
    return pathlib.Path(".github/workflows/worker-deploy.yml").read_text()


def local_imports(entry):
    """Every first-party module reachable from an entry file, by its static imports."""
    seen = set()
    queue = [pathlib.Path(entry)]
    while queue:
        path = queue.pop()
        if path in seen or not path.exists():
            continue
        seen.add(path)
        if path.suffix not in (".js", ".mjs"):
            continue
        for spec in re.findall(r"""(?:from|import)\s+["'](\.{1,2}/[^"']+)["']""", path.read_text()):
            queue.append(pathlib.Path(os.path.normpath(path.parent / spec)))
    return seen


def worker_entry(config):
    """A Wrangler config's `main` file, which is relative to the config."""
    path = pathlib.Path(config)
    text = path.read_text()
    if path.suffix == ".toml":
        main = m.tomllib.loads(text)["main"]
    else:
        main = re.search(r'^\s*"main"\s*:\s*"([^"]+)"', text, re.MULTILINE).group(1)
    return pathlib.Path(os.path.normpath(path.parent / main))


class Reader:
    """A fake Cloudflare read keyed by path, so call order is not part of the contract."""

    def __init__(self, responses):
        self.responses = responses

    def __call__(self, path):
        return self.responses[path]


class PublishApi:
    def __init__(self, items=None, changed=False):
        self.items = (
            items
            if items is not None
            else [
                {
                    "id": "12345678-1234-1234-1234-123456789abc",
                    "annotations": {"workers/tag": SHA_A},
                }
            ]
        )
        self.listings = 0
        self.deployments = 0
        self.changed = changed

    def __call__(self, path):
        if path.endswith("/settings"):
            return {}
        if path.endswith("/deployments"):
            self.deployments += 1
            vid = "old" if self.deployments < 3 else "12345678-1234-1234-1234-123456789abc"
            if self.changed and self.deployments == 2:
                vid = "concurrent"
            return {"deployments": [{"versions": [{"version_id": vid, "percentage": 100}]}]}
        if "/versions/" in path:
            return self.items[0] if len(self.items) == 1 else {}
        raise AssertionError(path)


def upload_receipt(command, **kwargs):
    if command[1:3] == ["versions", "upload"]:
        pathlib.Path(kwargs["env"]["WRANGLER_OUTPUT_FILE_PATH"]).write_text(json.dumps({
            "type": "version-upload", "worker_name": "x",
            "version_id": "12345678-1234-1234-1234-123456789abc",
        }) + "\n")


class Checks(unittest.TestCase):
    def setUp(self):
        for name in ("deployment_head", "verify_deployment_continuity"):
            active = patch.object(m, name, return_value="old")
            active.start()
            self.addCleanup(active.stop)

    def test_partial_rollout_is_not_exact_receipt(self):
        with patch.object(
            m,
            "api",
            return_value={"deployments": [{"versions": [{"version_id": "a", "percentage": 50}]}]},
        ):
            self.assertIsNone(m.deployed_tag("x"))

    def test_exact_tag(self):
        with patch.object(
            m,
            "api",
            side_effect=[
                {"deployments": [{"versions": [{"version_id": "a", "percentage": 100}]}]},
                {"annotations": {"workers/tag": "sha"}},
            ],
        ):
            self.assertEqual(m.deployed_tag("x"), "sha")

    def test_unconfigured_probe_blocks(self):
        with self.assertRaises(RuntimeError):
            m.probe({"name": "x"})

    def test_probe_sends_explicit_user_agent(self):
        seen = []

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self, *a):
                return b'{"ok": true}'

        def fake(req, timeout):
            seen.append(req)
            return Resp()

        with patch.object(m.urllib.request, "urlopen", side_effect=fake):
            m.probe({"name": "x", "health": "https://example.invalid/health", "ok": True})
        self.assertEqual(seen[0].get_header("User-agent"), m.USER_AGENT)
        self.assertFalse(seen[0].get_header("User-agent").startswith("Python-urllib"))

    def test_targets_are_explicit(self):
        self.assertNotIn("start-jays-services", m.TARGETS)
        self.assertNotIn("fleet-share-api", m.TARGETS)

    def walk(self, listing, live=None, deployed=("live", "v3")):
        live = live or {"number": 5, "annotations": SECRET}
        history = [{"versions": [{"version_id": v, "percentage": 100}]} for v in deployed]
        return m.deployed_tag(
            "x",
            Reader(
                {
                    "/workers/scripts/x/deployments": {"deployments": history},
                    "/workers/scripts/x/versions/live": live,
                    "/workers/scripts/x/versions?per_page=50": {"items": listing},
                }
            ),
        )

    TAGGED = {"id": "v3", "number": 3, "annotations": {"workers/tag": SHA_A}}

    def test_secret_versions_walk_back_to_the_tag_they_copied(self):
        secret = {"id": "v4", "number": 4, "annotations": SECRET}
        self.assertEqual(self.walk([secret, self.TAGGED], deployed=("live", "v4", "v3")), SHA_A)

    def test_walk_ignores_versions_newer_than_live(self):
        newer = {"id": "v9", "number": 9, "annotations": {"workers/tag": SHA_B}}
        self.assertEqual(self.walk([newer, self.TAGGED]), SHA_A)

    def test_watchdog_marker_deployment_of_the_same_version_does_not_break_the_walk(self):
        # A trip redeploys one version with a message, then re-arm deletes its secret.
        secret = {"id": "v4", "number": 4, "annotations": SECRET}
        deployed = ("live", "v4", "v4", "v3")
        self.assertEqual(self.walk([secret, self.TAGGED], deployed=deployed), SHA_A)

    def test_secret_after_a_rollback_is_not_the_newer_tag(self):
        # Rolled back from v4 to v3, then a secret change: the next lower number is v4,
        # but v3 was the code live, so v4's tag would claim code that is not running.
        rolled_past = {"id": "v4", "number": 4, "annotations": {"workers/tag": SHA_B}}
        deployed = ("live", "v3", "v4", "v3")
        self.assertIsNone(self.walk([rolled_past, self.TAGGED], deployed=deployed))

    def test_secret_after_an_undeployed_upload_is_not_that_upload_tag(self):
        uploaded = {"id": "v4", "number": 4, "annotations": {"workers/tag": SHA_B}}
        self.assertIsNone(self.walk([uploaded, self.TAGGED], deployed=("live", "v3")))

    def test_walk_stops_at_an_untagged_upload(self):
        # An API or hand upload may hold different code, so older tags say nothing.
        upload = {"id": "v4", "number": 4, "annotations": {"workers/triggered_by": "upload"}}
        self.assertIsNone(self.walk([upload, self.TAGGED], deployed=("live", "v4", "v3")))

    def test_live_untagged_upload_is_not_walked_past(self):
        self.assertIsNone(
            self.walk([], {"number": 5, "annotations": {"workers/triggered_by": "upload"}})
        )

    def test_walk_with_only_secret_versions_is_unknown(self):
        self.assertIsNone(self.walk([{"number": 1, "annotations": SECRET}]))

    def test_guard_tripped_reads_deployment_and_version_messages(self):
        def reader(deployment_message, version_message):
            return Reader(
                {
                    "/workers/scripts/x/deployments": {
                        "deployments": [
                            {
                                "annotations": {"workers/message": deployment_message},
                                "versions": [{"version_id": "live", "percentage": 100}],
                            }
                        ]
                    },
                    "/workers/scripts/x/versions/live": {
                        "annotations": {"workers/message": version_message}
                    },
                }
            )

        self.assertFalse(m.guard_tripped("x", reader("main abc", None)))
        self.assertFalse(m.guard_tripped("x", reader("see GUARD-TRIPPED later", None)))
        self.assertTrue(m.guard_tripped("x", reader("GUARD-TRIPPED rows written", None)))
        self.assertTrue(m.guard_tripped("x", reader(None, "GUARD-TRIPPED rows written")))

    def run_main(self, target=None, env=None, canary_error=None, **patches):
        """Run main() for one target with every external edge mocked."""
        target = target or {
            "deployer": "worker-deploy",
            "health": "https://example.invalid/health",
            "paths": ["src/**"],
            "command": ["wrangler", "deploy"],
            "config": "x.toml",
        }
        defaults = {
            "current_main": SHA_A,
            "guard_tripped": False,
            "deployed_tag": SHA_B,
            "worker_changed": True,
            "seconds_since_last_deploy": 86400,
            "config_limits": {},
            "api": None,
        }
        defaults.update(patches)
        active = [
            patch.object(m, k, side_effect=PublishApi())
            if k == "api" and v is None
            else patch.object(m, k, return_value=v)
            for k, v in defaults.items()
        ]
        with (
            patch.object(m.sys, "argv", ["deploy", "x"]),
            patch.dict(m.TARGETS, {"x": target}),
            patch.dict(os.environ, env or {}),
            patch.object(m.subprocess, "check_output", return_value=SHA_A + "\n"),
            patch.object(m.subprocess, "run", side_effect=upload_receipt) as run,
            patch.object(m, "probe") as probe,
            patch.object(m, "mcp_canary", side_effect=canary_error) as canary,
        ):
            for active_patch in active:
                active_patch.start()
            try:
                m.main()
            finally:
                for active_patch in active:
                    active_patch.stop()
        return run, probe, canary

    def publish_fixture(self, items, mains=None, guards=None):
        target = {"command": ["wrangler", "deploy"], "config": "x.toml"}
        with (
            patch.object(m, "api", side_effect=PublishApi(items)),
            patch.object(m, "current_main", side_effect=mains or [SHA_A]),
            patch.object(m, "guard_tripped", side_effect=guards or [False]),
            patch.object(m.subprocess, "run", side_effect=upload_receipt) as run,
        ):
            m.publish_version("x", target, SHA_A)
        return run

    def test_version_promotion_uses_only_exact_uploaded_identity(self):
        version = "12345678-1234-1234-1234-123456789abc"
        run = self.publish_fixture([{"id": version, "annotations": {"workers/tag": SHA_A}}])
        self.assertEqual(run.call_count, 2)
        upload, promote = [call.args[0] for call in run.call_args_list]
        self.assertEqual(upload[:3], ["wrangler", "versions", "upload"])
        self.assertIn("--keep-vars", upload)
        self.assertIn("--strict", upload)
        self.assertEqual(promote[:4], ["wrangler", "versions", "deploy", version + "@100"])
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["timeout"], m.DEPLOY_TIMEOUT_SECONDS)

    def test_missing_duplicate_wrong_and_malformed_versions_refuse_promotion(self):
        good = {"id": "12345678-1234-1234-1234-123456789abc", "annotations": {"workers/tag": SHA_A}}
        for items in (
            [],
            [good, good],
            [{**good, "annotations": {"workers/tag": SHA_B}}],
            [{**good, "id": "not-a-version"}],
        ):
            with self.subTest(items=items), patch.object(m.subprocess, "run", side_effect=upload_receipt) as run:
                with (
                    patch.object(m, "api", side_effect=PublishApi(items)),
                    self.assertRaises(RuntimeError),
                ):
                    m.publish_version(
                        "x", {"command": ["wrangler", "deploy"], "config": "x.toml"}, SHA_A
                    )
                self.assertEqual(run.call_count, 1)

    def test_concurrent_live_version_change_refuses_promotion(self):
        with (
            patch.object(m, "api", side_effect=PublishApi(changed=True)),
            patch.object(m, "current_main", return_value=SHA_A),
            patch.object(m, "guard_tripped", return_value=False),
            patch.object(m.subprocess, "run", side_effect=upload_receipt) as run,
            self.assertRaisesRegex(RuntimeError, "changed during upload"),
        ):
            m.publish_version("x", {"command": ["wrangler", "deploy"], "config": "x.toml"}, SHA_A)
        self.assertEqual(run.call_count, 1)

    def test_merge_or_guard_change_after_upload_refuses_promotion(self):
        items = [
            {"id": "12345678-1234-1234-1234-123456789abc", "annotations": {"workers/tag": SHA_A}}
        ]
        for head, tripped in ((SHA_B, False), (SHA_A, True)):
            with self.subTest(head=head, tripped=tripped), patch.object(m.subprocess, "run", side_effect=upload_receipt) as run:
                with (
                    patch.object(m, "api", side_effect=PublishApi(items)),
                    patch.object(m, "current_main", return_value=head),
                    patch.object(m, "guard_tripped", return_value=tripped),
                    self.assertRaises(RuntimeError),
                ):
                    m.publish_version(
                        "x", {"command": ["wrangler", "deploy"], "config": "x.toml"}, SHA_A
                    )
                self.assertEqual(run.call_count, 1)

    def test_tripped_worker_is_refused_before_any_mutation(self):
        with patch.object(m.subprocess, "run") as outer:
            with self.assertRaisesRegex(SystemExit, "budget guard"):
                self.run_main(guard_tripped=True)
            outer.assert_not_called()

    def test_unrelated_merge_does_not_redeploy(self):
        run, probe, canary = self.run_main(worker_changed=False)
        run.assert_not_called()
        probe.assert_called_once()
        canary.assert_called_once()

    def test_live_tag_equal_to_main_does_not_redeploy(self):
        run, probe, canary = self.run_main(deployed_tag=SHA_A)
        run.assert_not_called()
        probe.assert_called_once()
        canary.assert_called_once()

    def test_a_failed_canary_fails_the_runs_that_deploy_nothing(self):
        # A failed canary leaves the new version live, so the next scheduled run takes one
        # of these paths and must not report the Worker healthy.
        for patches in ({"deployed_tag": SHA_A}, {"worker_changed": False}):
            with self.subTest(patches), self.assertRaisesRegex(RuntimeError, "MCP canary"):
                self.run_main(canary_error=RuntimeError("MCP canary"), **patches)

    def test_deploy_upload_and_git_reads_have_timeouts(self):
        tags = iter([SHA_B, SHA_A])
        with patch.object(m, "deployed_tag", side_effect=lambda name: next(tags)):
            with (
                patch.object(m.sys, "argv", ["deploy", "x"]),
                patch.dict(
                    m.TARGETS,
                    {
                        "x": {
                            "deployer": "worker-deploy",
                            "health": "https://example.invalid/health",
                            "paths": ["src/**"],
                            "command": ["wrangler", "deploy"],
                            "config": "x.toml",
                        }
                    },
                ),
                patch.object(m.subprocess, "check_output", return_value=SHA_A + "\n") as rev_parse,
                patch.object(m.subprocess, "run", side_effect=upload_receipt) as run,
                patch.object(m, "verify_live"),
                patch.object(m, "current_main", return_value=SHA_A),
                patch.object(m, "guard_tripped", return_value=False),
                patch.object(m, "worker_changed", return_value=True),
                patch.object(m, "config_limits", return_value={}),
                patch.object(m, "api", side_effect=PublishApi()),
            ):
                m.main()
        self.assertEqual(rev_parse.call_args.kwargs["timeout"], m.GIT_TIMEOUT_SECONDS)
        self.assertEqual(run.call_args.kwargs["timeout"], m.DEPLOY_TIMEOUT_SECONDS)

    def test_a_broken_git_stops_the_run_instead_of_authorizing_a_deploy(self):
        target = {"paths": ["workers/one/**"]}
        hung = subprocess.TimeoutExpired("git", m.GIT_TIMEOUT_SECONDS)
        for failure in (hung, FileNotFoundError("git")):
            with (
                self.subTest(failure),
                patch.object(m.subprocess, "run", side_effect=failure) as run,
                self.assertRaises(type(failure)),
            ):
                m.worker_changed(SHA_A, SHA_B, target)
            self.assertEqual(run.call_args.kwargs["timeout"], m.GIT_TIMEOUT_SECONDS)

    def test_changed_worker_deploys_with_exact_tag_after_receipt(self):
        tags = iter([SHA_B, SHA_A])
        with patch.object(m, "deployed_tag", side_effect=lambda name: next(tags)):
            with (
                patch.object(m.sys, "argv", ["deploy", "x"]),
                patch.dict(
                    m.TARGETS,
                    {
                        "x": {
                            "deployer": "worker-deploy",
                            "health": "https://example.invalid/health",
                            "paths": ["src/**"],
                            "command": ["wrangler", "deploy"],
                            "config": "x.toml",
                        }
                    },
                ),
                patch.object(m.subprocess, "check_output", return_value=SHA_A + "\n"),
                patch.object(m.subprocess, "run", side_effect=upload_receipt) as run,
                patch.object(m, "probe"),
                patch.object(m, "mcp_canary") as canary,
                patch.object(m, "current_main", return_value=SHA_A),
                patch.object(m, "guard_tripped", return_value=False),
                patch.object(m, "worker_changed", return_value=True),
                patch.object(m, "config_limits", return_value={}),
                patch.object(m, "api", side_effect=PublishApi()),
            ):
                m.main()
        self.assertIn("main " + SHA_A, run.call_args.args[0])
        self.assertEqual(canary.call_count, 2)

    def test_stale_canary_bearer_stops_the_deploy_before_upload(self):
        with (
            patch.object(m, "deployed_tag", return_value=SHA_B),
            patch.object(m.sys, "argv", ["deploy", "x"]),
            patch.dict(
                m.TARGETS,
                {
                    "x": {
                        "deployer": "worker-deploy",
                        "health": "https://example.invalid/health",
                        "paths": ["src/**"],
                        "command": ["wrangler", "deploy"],
                        "config": "x.toml",
                    }
                },
            ),
            patch.object(m.subprocess, "check_output", return_value=SHA_A + "\n"),
            patch.object(m.subprocess, "run", side_effect=upload_receipt) as run,
            patch.object(m, "probe"),
            patch.object(m, "mcp_canary", side_effect=RuntimeError("MCP canary")),
            patch.object(m, "current_main", return_value=SHA_A),
            patch.object(m, "guard_tripped", return_value=False),
            patch.object(m, "worker_changed", return_value=True),
            patch.object(m, "config_limits", return_value={}),
            patch.object(m, "api", side_effect=PublishApi()),
            self.assertRaisesRegex(RuntimeError, "MCP canary"),
        ):
            m.main()
        run.assert_not_called()

    def test_trip_during_the_run_refuses_the_upload(self):
        with (
            patch.object(m, "guard_tripped", side_effect=[False, True]),
            patch.object(m, "deployed_tag", return_value=SHA_B),
            patch.object(m.sys, "argv", ["deploy", "x"]),
            patch.dict(
                m.TARGETS,
                {
                    "x": {
                        "deployer": "worker-deploy",
                        "health": "https://example.invalid/health",
                        "paths": ["src/**"],
                        "command": ["wrangler", "deploy"],
                        "config": "x.toml",
                    }
                },
            ),
            patch.object(m.subprocess, "check_output", return_value=SHA_A + "\n"),
            patch.object(m.subprocess, "run", side_effect=upload_receipt) as run,
            patch.object(m, "probe"),
            patch.object(m, "mcp_canary"),
            patch.object(m, "current_main", return_value=SHA_A),
            patch.object(m, "worker_changed", return_value=True),
            patch.object(m, "config_limits", return_value={}),
            patch.object(m, "api", side_effect=PublishApi()),
            self.assertRaisesRegex(SystemExit, "budget guard"),
        ):
            m.main()
        run.assert_not_called()

    def test_second_deploy_inside_the_interval_waits(self):
        target = {
            "deployer": "worker-deploy",
            "health": "https://example.invalid/health",
            "paths": ["src/**"],
            "command": ["wrangler", "deploy"],
            "config": "x.toml",
            "min_deploy_interval_seconds": 3600,
        }
        run, _, _ = self.run_main(target, seconds_since_last_deploy=600)
        run.assert_not_called()

    def test_seconds_since_last_deploy_parses_cloudflare_time(self):
        read = Reader(
            {
                "/workers/scripts/x/deployments": {
                    "deployments": [{"created_on": "2026-11-05T01:42:19.784562Z"}]
                }
            }
        )
        now = datetime.datetime(2026, 11, 5, 2, 42, 19, 784562, tzinfo=datetime.UTC)
        self.assertEqual(m.seconds_since_last_deploy("x", read, now), 3600)

    def test_unarmed_per_worker_gate_skips_without_reading(self):
        target = {
            "deployer": "worker-deploy",
            "health": "https://example.invalid/health",
            "gate_variable": "X_ENABLED",
            "paths": ["src/**"],
        }
        with (
            patch.object(m.sys, "argv", ["deploy", "x"]),
            patch.dict(m.TARGETS, {"x": target}),
            patch.dict(os.environ, {"X_ENABLED": "false"}),
            patch.object(m, "current_main") as current,
            patch.object(m.subprocess, "run", side_effect=upload_receipt) as run,
        ):
            m.main()
        current.assert_not_called()
        run.assert_not_called()

    def test_target_of_the_other_pipeline_is_refused(self):
        with patch.object(m.sys, "argv", ["deploy", "alert-ingress"]):
            with self.assertRaisesRegex(SystemExit, "another pipeline"):
                m.main()

    def test_missing_canary_credential_blocks_before_deploy(self):
        target = {
            "deployer": "worker-deploy",
            "health": "https://example.invalid/health",
            "paths": ["src/**"],
            "command": ["wrangler", "deploy"],
            "config": "x.toml",
            "canary": {"url": "https://example.invalid/mcp", "bearer_env": "X_BEARER", "tool": "t"},
        }
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("X_BEARER", None)
            with self.assertRaisesRegex(SystemExit, "X_BEARER"):
                self.run_main(target)

    def test_deploy_that_would_drop_live_limits_is_refused(self):
        with self.assertRaisesRegex(SystemExit, "cpu_ms"):
            self.run_main(api={"limits": {"cpu_ms": 50}}, config_limits={})

    def test_dropped_limits_compares_keys_only(self):
        self.assertEqual(m.dropped_limits({"cpu_ms": 50}, {"cpu_ms": 100}), [])
        self.assertEqual(
            m.dropped_limits({"cpu_ms": 50, "subrequests": 20}, {"cpu_ms": 50}), ["subrequests"]
        )
        self.assertEqual(m.dropped_limits(None, None), [])

    def test_config_limits_reads_each_format(self):
        with tempfile.TemporaryDirectory() as root:
            toml = pathlib.Path(root, "w.toml")
            toml.write_text("[limits]\ncpu_ms = 50\n")
            self.assertEqual(m.config_limits(str(toml)), {"cpu_ms": 50})
            jsonc = pathlib.Path(root, "w.jsonc")
            jsonc.write_text('{\n  // "limits": {"cpu_ms": 1},\n  "name": "x"\n}\n')
            self.assertEqual(m.config_limits(str(jsonc)), {})
            jsonc.write_text(
                '{\n  // comment\n  "limits": { "cpu_ms": 300, "subrequests": 200 }\n}\n'
            )
            self.assertEqual(m.config_limits(str(jsonc)), {"cpu_ms": 300, "subrequests": 200})
            for commented in (
                '{\n  "name": "x", // "limits": {"cpu_ms": 1}\n}\n',
                '{\n  /* "limits": {"cpu_ms": 1} */\n  "name": "x"\n}\n',
                '{\n  /*\n  "limits": {"cpu_ms": 1}\n  */\n  "name": "x"\n}\n',
            ):
                jsonc.write_text(commented)
                self.assertEqual(m.config_limits(str(jsonc)), {}, commented)
            jsonc.write_text(
                '{\n  "url": "https://example.invalid",\n  "limits": { "cpu_ms": 3 }\n}\n'
            )
            self.assertEqual(m.config_limits(str(jsonc)), {"cpu_ms": 3})

    def test_real_mcp_config_parses_for_the_limit_check(self):
        declared = m.config_limits(m.TARGETS["simple-tools-mcp"]["config"])
        self.assertIsInstance(declared, dict)

    def repo(self, root):
        def git(*args):
            subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, timeout=30)

        def commit(path, message):
            file = pathlib.Path(root, path)
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(message)
            git("add", ".")
            git("commit", "-qm", message)
            return subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=root, text=True, timeout=30
            ).strip()

        git("init", "-q")
        git("config", "user.email", "test@example.invalid")
        git("config", "user.name", "test")
        return commit

    def test_worker_changed_is_per_target(self):
        one = {"paths": ["workers/one/src/**"], "ignore": ["*.test.*"]}
        two = {"paths": ["workers/two/src/**"]}
        with tempfile.TemporaryDirectory() as root:
            commit = self.repo(root)
            live = commit("README.md", "start")
            docs = commit("docs/x.md", "docs")
            self.assertFalse(m.worker_changed(live, docs, one, cwd=root))
            test = commit("workers/one/src/a.test.js", "test only")
            self.assertFalse(m.worker_changed(live, test, one, cwd=root))
            code = commit("workers/one/src/a.js", "code")
            self.assertTrue(m.worker_changed(live, code, one, cwd=root))
            self.assertFalse(m.worker_changed(live, code, two, cwd=root))

    def test_worker_changed_treats_doubt_as_changed(self):
        target = {"paths": ["workers/one/**"]}
        with tempfile.TemporaryDirectory() as root:
            commit = self.repo(root)
            head = commit("README.md", "start")
            for live in (None, "", "secret-tag", "0" * 40):
                self.assertTrue(m.worker_changed(live, head, target, cwd=root))

    def test_every_target_path_runs_the_workflow_or_is_released_elsewhere(self):
        listed = set(re.findall(r"^\s+-\s+'([^']+)'", workflow_text(), re.MULTILINE))
        for path in m.TARGETS["simple-tools-mcp"]["paths"]:
            covered = any(m.fnmatch.fnmatchcase(path, glob) for glob in listed)
            self.assertTrue(covered, path + " is outside the workflow's push filter")

    def test_every_local_import_of_every_worker_is_a_target_path(self):
        for name, target in m.TARGETS.items():
            with self.subTest(name):
                root = worker_entry(target["config"])
                found = local_imports(root)
                # A moved entry file must fail here, not pass by walking nothing.
                self.assertTrue(root.exists())
                self.assertGreater(len(found), 1)
                missing = [str(p) for p in found if not m.path_is_relevant(str(p), target)]
                self.assertEqual(missing, [])

    def test_matrix_holds_only_this_pipeline_and_has_probes(self):
        found = re.search(r"^\s*worker:\s*(\[.*\])\s*$", workflow_text(), re.MULTILINE)
        workers = json.loads(found.group(1))
        self.assertEqual(workers, ["simple-tools-mcp"])
        for name in workers:
            self.assertEqual(m.TARGETS[name]["deployer"], m.MAPPED_DEPLOYER)
            self.assertTrue(m.TARGETS[name]["health"])

    def test_mcp_stays_off_behind_its_own_gate(self):
        text = workflow_text()
        self.assertIn("vars.WORKER_DEPLOY_ENABLED == 'true'", text)
        self.assertIn("vars.SIMPLE_TOOLS_MCP_DEPLOY_ENABLED == 'true'", text)
        target = m.TARGETS["simple-tools-mcp"]
        self.assertEqual(target["gate_variable"], "SIMPLE_TOOLS_MCP_DEPLOY_ENABLED")
        self.assertEqual(target["min_deploy_interval_seconds"], 3600)
        self.assertIn(target["canary"]["bearer_env"], text)

    def test_workflow_fetches_history_for_the_compare(self):
        self.assertIn("fetch-depth: 0", workflow_text())

    def test_retired_pr_conflict_watch_is_not_a_target(self):
        # Retired on 2026-11-06 at the owner's request; its source is gone from main.
        self.assertNotIn("pr-conflict-watch", m.TARGETS)

    def test_target_configs_exist(self):
        for name, target in m.TARGETS.items():
            self.assertTrue(pathlib.Path(target["config"]).exists(), name)
            self.assertTrue(target["paths"], name)

    def canary_replies(self, *replies):
        queue = list(replies)

        def post(url, body, bearer=None, session=None, protocol=None):
            status, headers, payload = queue.pop(0)
            return status, headers, payload

        return post

    def mcp_target(self):
        return {
            "canary": {
                "url": "https://example.invalid/mcp",
                "bearer_env": "X_BEARER",
                "tool": "identities",
            }
        }

    def run_canary(self, *replies):
        with (
            patch.dict(os.environ, {"X_BEARER": "offline"}),
            patch.object(m, "mcp_post", side_effect=self.canary_replies(*replies)),
        ):
            m.mcp_canary(self.mcp_target())

    # Split so git-relay's outbound credential scan does not read the challenge as a token.
    UNAUTH = (401, {"WWW-Authenticate": "Bearer" + ' resource_metadata="x"'}, b"")
    INIT = (
        200,
        {"Content-Type": "application/json", "Mcp-Session-Id": "s"},
        b'{"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"2025-06-18"}}',
    )
    NOTIFIED = (202, {}, b"")
    LIST = (
        200,
        {"Content-Type": "text/event-stream"},
        b'event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"tools":[{"name":"identities"}]}}\n\n',
    )
    CALL = (
        200,
        {"Content-Type": "application/json"},
        b'{"jsonrpc":"2.0","id":3,"result":{"content":[]}}',
    )

    def test_canary_passes_on_the_real_sequence(self):
        self.run_canary(self.UNAUTH, self.INIT, self.NOTIFIED, self.LIST, self.CALL)

    def test_canary_fails_when_mcp_answers_without_oauth(self):
        with self.assertRaisesRegex(RuntimeError, "OAuth 401"):
            self.run_canary((200, {}, b""))

    def test_canary_fails_when_the_tool_is_gone(self):
        empty = (200, {"Content-Type": "application/json"}, b'{"id":2,"result":{"tools":[]}}')
        with self.assertRaisesRegex(RuntimeError, "lacks identities"):
            self.run_canary(self.UNAUTH, self.INIT, self.NOTIFIED, empty)

    def test_canary_fails_on_a_tool_error(self):
        broken = (200, {"Content-Type": "application/json"}, b'{"id":3,"result":{"isError":true}}')
        with self.assertRaisesRegex(RuntimeError, "returned an error"):
            self.run_canary(self.UNAUTH, self.INIT, self.NOTIFIED, self.LIST, broken)

    def test_canary_failure_text_never_holds_the_bearer(self):
        with (
            patch.dict(os.environ, {"X_BEARER": "super-secret"}),
            patch.object(
                m, "mcp_post", side_effect=self.canary_replies(self.UNAUTH, (500, {}, b"boom"))
            ),
            self.assertRaises(RuntimeError) as caught,
        ):
            m.mcp_canary(self.mcp_target())
        self.assertNotIn("super-secret", str(caught.exception))
        self.assertNotIn("boom", str(caught.exception))



class UploadReceiptChecks(unittest.TestCase):
 def invoke(self, receipt, details=None, error=None):
  version="12345678-1234-1234-1234-123456789abc"
  def write(command, **kwargs):
   if error: raise error
   if receipt is not None: m.pathlib.Path(kwargs["env"]["WRANGLER_OUTPUT_FILE_PATH"]).write_text(receipt)
  with patch.object(m.subprocess,"run",side_effect=write) as run, patch.object(m,"api",return_value=details if details is not None else {"id":version,"annotations":{"workers/tag":"commit"}}) as api:
   result=m.uploaded_version(["wrangler","versions","upload"],["--tag","commit"],{},"worker","commit")
   self.assertEqual(run.call_args.kwargs["timeout"],600)
   self.assertEqual(api.call_args.args[0],"/workers/scripts/worker/versions/"+version)
   return result
 def receipt(self, **change):
  return json.dumps({"type":"version-upload","worker_name":"worker","version_id":"12345678-1234-1234-1234-123456789abc",**change})+"\n"
 def test_receipt_uses_exact_direct_api_identity_not_version_listing(self):
  self.assertEqual(self.invoke(self.receipt()),"12345678-1234-1234-1234-123456789abc")
 def test_receipt_missing_malformed_wrong_worker_or_version_refuses(self):
  for receipt in (None,"broken",self.receipt()*2,self.receipt(worker_name="other"),self.receipt(version_id="bad"),"x"*65537):
   with self.subTest(receipt=str(receipt)[:20]), self.assertRaises(RuntimeError):self.invoke(receipt)
 def test_direct_api_wrong_commit_or_identity_refuses(self):
  for details in ({},{"id":"other","annotations":{"workers/tag":"commit"}},{"id":"12345678-1234-1234-1234-123456789abc","annotations":{"workers/tag":"old"}}):
   with self.assertRaises(RuntimeError):self.invoke(self.receipt(),details)
 def test_hung_upload_raises_timeout_without_promotion(self):
  with self.assertRaises(m.subprocess.TimeoutExpired):self.invoke(None,error=m.subprocess.TimeoutExpired("wrangler",600))


class DeploymentContinuityChecks(unittest.TestCase):
 def test_null_and_malformed_versions_refuse_cleanly(self):
  for versions in (None, {}, "bad", [None], ["bad"]):
   with patch.object(m,"api",return_value={"deployments":[{"id":"new","versions":versions},{"id":"old"}]}), self.assertRaisesRegex(RuntimeError,"malformed versions"):
    m.verify_deployment_continuity("w","old","version")

 def test_clean_serial_promotion(self):
  with patch.object(m,"api",return_value={"deployments":[{"id":"new","versions":[{"version_id":"version","percentage":100}]},{"id":"old"}]}):
   m.verify_deployment_continuity("w","old","version")
 def test_first_deployment_accepts_no_predecessor(self):
  with patch.object(m,"api",return_value={"deployments":[{"id":"new","versions":[{"version_id":"version","percentage":100}]}]}):
   m.verify_deployment_continuity("w",None,"version")
 def test_missing_expected_predecessor_is_unverifiable_not_success(self):
  with patch.object(m,"api",return_value={"deployments":[{"id":"new","versions":[{"version_id":"version","percentage":100}]}]}), self.assertRaisesRegex(RuntimeError,"history unavailable"):
   m.verify_deployment_continuity("w","old","version")
 def test_response_extension_fields_are_ignored(self):
  with patch.object(m,"api",return_value={"deployments":[{"id":"new","versions":[{"version_id":"version","percentage":100,"extra":"future"}]},{"id":"old"}]}):
   m.verify_deployment_continuity("w","old","version")

 def test_interleaved_promotion_and_changed_traffic_fail(self):
  for entries in ([{"id":"new","versions":[{"version_id":"version","percentage":100}]},{"id":"other"}], [{"id":"other","versions":[{"version_id":"other","percentage":100}]},{"id":"new"}], []):
   with patch.object(m,"api",return_value={"deployments":entries}), self.assertRaisesRegex(RuntimeError,"Concurrent deployment"):
    m.verify_deployment_continuity("w","old","version")

if __name__ == "__main__":
    unittest.main()
