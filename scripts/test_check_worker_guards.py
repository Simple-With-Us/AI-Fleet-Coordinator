#!/usr/bin/env python3
"""Tests for scripts/check-worker-guards.py and scripts/worker-registry.json.

    python3 scripts/test_check_worker_guards.py

Temp git repositories only;  no network and no Cloudflare.

CI: .github/workflows/ci.yml runs this file in the step named
``Worker guard registry and limits``.
"""

from __future__ import annotations

import copy
import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parent
SCRIPT = SCRIPTS / "check-worker-guards.py"
REPO = "Simple-With-Us/AI-Fleet-Coordinator"


def _load():
    loader = importlib.machinery.SourceFileLoader("check_worker_guards", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = mod
    loader.exec_module(mod)
    return mod


guards = _load()


def git(*args):
    subprocess.run(["git", *args], check=True, timeout=30)

GOOD_TOML = """name = "w"
preview_urls = false
[limits]
cpu_ms = 100
subrequests = 40
[observability]
enabled = true
head_sampling_rate = 0.5
"""

REGISTRY = {
    "account_plan": "paid",
    "free_plan_subrequests": 50,
    "workers": {
        "w": {
            "repo": REPO,
            "config": "wrangler.toml",
            "status": "auto",
            "limits": {"cpu_ms": 100, "subrequests": 100},
            "head_sampling_rate": 0.5,
            "health": "probe",
            "deploy": "workflow",
        }
    },
}


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        git("init", "-q", str(self.root))

    def findings(self, config=GOOD_TOML, registry=REGISTRY, path="wrangler.toml", plan=None):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(config, encoding="utf-8")
        git("-C", str(self.root), "add", path)
        registry_file = self.root / "registry.json"
        registry_file.write_text(json.dumps(registry), encoding="utf-8")
        return guards.run(self.root, registry_file, REPO, plan)


class ConfigRules(Base):
    def test_good_config_passes(self):
        self.assertEqual(self.findings(), [])

    def test_missing_cpu_ms_fails(self):
        out = self.findings(GOOD_TOML.replace("cpu_ms = 100\n", ""))
        self.assertTrue(any("limits.cpu_ms is missing" in f for f in out), out)

    def test_missing_subrequests_fails(self):
        out = self.findings(GOOD_TOML.replace("subrequests = 40\n", ""))
        self.assertTrue(any("limits.subrequests is missing" in f for f in out), out)

    def test_value_above_registry_fails(self):
        out = self.findings(GOOD_TOML.replace("cpu_ms = 100", "cpu_ms = 101"))
        self.assertTrue(any("limits.cpu_ms 101 is above" in f for f in out), out)

    def test_boolean_limit_fails(self):
        out = self.findings(GOOD_TOML.replace("cpu_ms = 100", "cpu_ms = true"))
        self.assertTrue(any("limits.cpu_ms must be a positive integer" in f for f in out), out)

    def test_preview_urls_unset_or_true_fails(self):
        for text in (
            GOOD_TOML.replace("preview_urls = false\n", ""),
            GOOD_TOML.replace("preview_urls = false", "preview_urls = true"),
        ):
            out = self.findings(text)
            self.assertTrue(any("preview_urls must be false" in f for f in out), out)

    def test_preview_urls_of_another_type_fails(self):
        # The identity check is the point:  0, null and "false" are not false to Wrangler.
        registry = copy.deepcopy(REGISTRY)
        registry["workers"]["w"]["config"] = "wrangler.jsonc"
        good = (
            '{"name": "w", "preview_urls": false, "limits": {"cpu_ms": 100, "subrequests": 40},'
            ' "observability": {"head_sampling_rate": 0.5}}'
        )
        self.assertEqual(self.findings(good, registry, "wrangler.jsonc"), [])
        for value in ("0", "null", '"false"'):
            text = good.replace('"preview_urls": false', f'"preview_urls": {value}')
            out = self.findings(text, registry, "wrangler.jsonc")
            self.assertTrue(any("preview_urls must be false" in f for f in out), (value, out))

    def test_implicit_sampling_fails(self):
        out = self.findings(GOOD_TOML.replace("head_sampling_rate = 0.5\n", ""))
        self.assertTrue(any("is implicit" in f for f in out), out)
        out = self.findings(GOOD_TOML.split("[observability]")[0])
        self.assertTrue(any("is implicit" in f for f in out), out)

    def test_logs_level_sampling_counts_as_explicit(self):
        text = GOOD_TOML.replace(
            "head_sampling_rate = 0.5\n", "[observability.logs]\nhead_sampling_rate = 0.25\n"
        )
        self.assertEqual(self.findings(text), [])

    def test_sampling_above_registry_fails(self):
        out = self.findings(GOOD_TOML.replace("0.5", "1"))
        self.assertTrue(any("is above the registry" in f for f in out), out)

    def test_unknown_name_fails(self):
        out = self.findings(GOOD_TOML.replace('name = "w"', 'name = "other"'))
        self.assertTrue(any("'other' is not in the worker registry" in f for f in out), out)

    def test_unlisted_path_of_a_known_name_fails(self):
        out = self.findings(path="sub/wrangler.toml")
        self.assertTrue(any("sub/wrangler.toml: not listed" in f for f in out), out)

    def test_extra_config_is_accepted(self):
        registry = copy.deepcopy(REGISTRY)
        registry["workers"]["w"]["extra_configs"] = ["sub/wrangler.toml"]
        self.findings(registry=registry)
        self.assertEqual(self.findings(registry=registry, path="sub/wrangler.toml"), [])

    def test_unparseable_config_fails(self):
        out = self.findings("name = ")
        self.assertTrue(any("cannot parse" in f for f in out), out)

    def test_untracked_config_is_ignored(self):
        self.findings()
        stray = self.root / "scratch"
        stray.mkdir()
        (stray / "wrangler.toml").write_text('name = "nope"\n', encoding="utf-8")
        self.assertEqual(guards.run(self.root, self.root / "registry.json", REPO, None), [])

    def test_jsonc_with_comments_urls_and_trailing_commas(self):
        text = """{
          // a comment
          "name": "w", /* another */
          "url": "https://example.com//not-a-comment",
          "preview_urls": false,
          "observability": { "head_sampling_rate": 0.5, },
          "limits": { "cpu_ms": 100, "subrequests": 40, },
          "list": [1, 2,],
        }"""
        registry = copy.deepcopy(REGISTRY)
        registry["workers"]["w"]["config"] = "wrangler.jsonc"
        self.assertEqual(self.findings(text, registry, "wrangler.jsonc"), [])
        parsed = json.loads(guards.strip_jsonc(text))
        self.assertEqual(parsed["url"], "https://example.com//not-a-comment")
        self.assertEqual(parsed["list"], [1, 2])

    def test_named_config_file_is_checked(self):
        self.findings()
        text = GOOD_TOML.replace("cpu_ms = 100", "cpu_ms = 101")
        out = self.findings(text, path="wrangler.prod.toml")
        self.assertTrue(any(f.startswith("wrangler.prod.toml: ") for f in out), out)

    def test_similar_file_names_are_not_configs(self):
        self.findings()
        for name in ("wrangler.toml.example", "not-wrangler.toml", "wrangler.md"):
            (self.root / name).write_text("junk", encoding="utf-8")
            git("-C", str(self.root), "add", name)
        self.assertEqual(guards.run(self.root, self.root / "registry.json", REPO, None), [])

    def test_non_object_config_is_a_finding_not_a_crash(self):
        registry = copy.deepcopy(REGISTRY)
        registry["workers"]["w"]["config"] = "wrangler.jsonc"
        out = self.findings("[]", registry, "wrangler.jsonc")
        self.assertTrue(any("top level must be an object" in f for f in out), out)

    def test_non_string_name_is_a_finding_not_a_crash(self):
        out = self.findings(GOOD_TOML.replace('name = "w"', 'name = ["w"]'))
        self.assertTrue(any("is not in the worker registry" in f for f in out), out)


class EnvironmentBlocks(Base):
    def registry_with_env_worker(self):
        registry = copy.deepcopy(REGISTRY)
        registry["workers"]["w-prod"] = copy.deepcopy(registry["workers"]["w"])
        return registry

    def test_env_limit_above_registry_fails(self):
        text = GOOD_TOML + "[env.prod.limits]\ncpu_ms = 999\nsubrequests = 40\n"
        out = self.findings(text, self.registry_with_env_worker())
        self.assertTrue(any("[env.prod]: limits.cpu_ms 999 is above" in f for f in out), out)

    def test_env_that_drops_a_limit_fails(self):
        text = GOOD_TOML + "[env.prod.limits]\nsubrequests = 40\n"
        out = self.findings(text, self.registry_with_env_worker())
        self.assertTrue(any("[env.prod]: limits.cpu_ms is missing" in f for f in out), out)

    def test_env_that_turns_preview_urls_on_fails(self):
        text = GOOD_TOML + "[env.prod]\npreview_urls = true\n"
        out = self.findings(text, self.registry_with_env_worker())
        self.assertTrue(any("[env.prod]: preview_urls must be false" in f for f in out), out)

    def test_env_deploys_as_a_worker_that_needs_a_row(self):
        out = self.findings(GOOD_TOML + "[env.prod]\n")
        self.assertTrue(any("[env.prod]: name 'w-prod' is not in" in f for f in out), out)

    def test_env_that_inherits_passes_under_a_registered_name(self):
        out = self.findings(GOOD_TOML + "[env.prod]\n", self.registry_with_env_worker())
        self.assertEqual(out, [])

    def test_env_may_name_its_worker(self):
        text = GOOD_TOML + '[env.prod]\nname = "w"\n'
        self.assertEqual(self.findings(text), [])


class DowngradeSwitch(Base):
    def free_registry(self):
        registry = copy.deepcopy(REGISTRY)
        registry["account_plan"] = "free"
        return registry

    def test_paid_still_requires_cpu_ms(self):
        out = self.findings(GOOD_TOML.replace("cpu_ms = 100\n", ""))
        self.assertTrue(out)

    def test_free_accepts_removing_cpu_ms(self):
        text = GOOD_TOML.replace("cpu_ms = 100\n", "")
        self.assertEqual(self.findings(text, self.free_registry()), [])

    def test_free_caps_subrequests_at_50(self):
        text = GOOD_TOML.replace("subrequests = 40", "subrequests = 51")
        self.assertEqual(self.findings(text), [])
        out = self.findings(text, self.free_registry())
        self.assertTrue(any("limits.subrequests 51 is above the allowed 50" in f for f in out), out)

    def test_free_still_requires_subrequests(self):
        text = GOOD_TOML.replace("subrequests = 40\n", "")
        out = self.findings(text, self.free_registry())
        self.assertTrue(any("limits.subrequests is missing" in f for f in out), out)

    def test_the_downgrade_change_itself_passes(self):
        # Checklist step 8:  the registry keeps its Paid ceiling, the config drops to Free's.
        registry = self.free_registry()
        registry["workers"]["w"]["limits"]["subrequests"] = 200
        text = GOOD_TOML.replace("cpu_ms = 100\n", "")
        text = text.replace("subrequests = 40", "subrequests = 50")
        self.assertEqual(self.findings(text, registry), [])

    def test_plan_flag_overrides_the_file(self):
        text = GOOD_TOML.replace("cpu_ms = 100\n", "")
        self.assertEqual(self.findings(text, plan="free"), [])
        self.assertTrue(self.findings(text, self.free_registry(), plan="paid"))


class RegistryRules(Base):
    def mutated(self, **fields):
        registry = copy.deepcopy(REGISTRY)
        registry["workers"]["w"].update(fields)
        return registry

    def test_bad_status_fails(self):
        out = self.findings(registry=self.mutated(status="live"))
        self.assertTrue(any("status must be one of" in f for f in out), out)

    def test_bad_plan_fails(self):
        registry = copy.deepcopy(REGISTRY)
        registry["account_plan"] = "pro"
        out = self.findings(registry=registry)
        self.assertTrue(any("account_plan" in f for f in out), out)

    def test_null_config_needs_a_reason(self):
        other = {
            "repo": REPO,
            "config": None,
            "status": "retire",
            "limits": {"cpu_ms": 1, "subrequests": 1},
            "head_sampling_rate": 1,
            "health": "x",
            "deploy": "y",
        }
        registry = copy.deepcopy(REGISTRY)
        registry["workers"]["other"] = other
        out = self.findings(registry=registry)
        self.assertTrue(any("needs no_config_reason" in f for f in out), out)
        other["extra_configs"] = "wrangler.toml"
        out = self.findings(registry=registry)
        self.assertTrue(any("extra_configs must be a list" in f for f in out), out)
        del other["extra_configs"]
        other["no_config_reason"] = "removed"
        self.assertEqual(self.findings(registry=registry), [])

    def test_missing_config_of_this_repo_fails(self):
        registry = self.mutated(extra_configs=["gone/wrangler.toml"])
        out = self.findings(registry=registry)
        self.assertTrue(any("gone/wrangler.toml is not a tracked" in f for f in out), out)

    def test_config_in_another_repo_is_not_looked_for(self):
        registry = copy.deepcopy(REGISTRY)
        registry["workers"]["um"] = dict(
            registry["workers"]["w"],
            repo="Simple-With-Us/Usage-Monitor",
            config="workers/um/wrangler.jsonc",
        )
        self.assertEqual(self.findings(registry=registry), [])

    def test_unreadable_registry_fails(self):
        self.findings()
        out = guards.run(self.root, self.root / "absent.json", REPO, None)
        self.assertTrue(any("cannot read" in f for f in out), out)

    def test_no_tracked_config_fails_closed(self):
        registry_file = self.root / "registry.json"
        registry_file.write_text(json.dumps(REGISTRY), encoding="utf-8")
        out = guards.run(self.root, registry_file, REPO, None)
        self.assertTrue(any("no tracked wrangler config" in f for f in out), out)

    def test_not_a_git_tree_fails_closed(self):
        with tempfile.TemporaryDirectory() as plain:
            registry_file = Path(plain) / "registry.json"
            registry_file.write_text(json.dumps(REGISTRY), encoding="utf-8")
            out = guards.run(Path(plain), registry_file, REPO, None)
        self.assertTrue(any("cannot list tracked files" in f for f in out), out)


class ListingFailures(Base):
    """A listing failure never reads as clean, and the exit code names the cause."""

    def exit_code(self, error):
        self.findings()
        with mock.patch.object(guards.subprocess, "run", side_effect=error):
            registry = str(self.root / "registry.json")
            return guards.main(["--root", str(self.root), "--registry", registry])

    def test_timeout_exits_124(self):
        self.assertEqual(self.exit_code(subprocess.TimeoutExpired("git", 60)), 124)

    def test_missing_git_exits_127(self):
        self.assertEqual(self.exit_code(FileNotFoundError("git")), 127)

    def test_other_git_failure_is_an_ordinary_finding(self):
        self.assertEqual(self.exit_code(subprocess.CalledProcessError(128, "git")), 1)

    def test_a_vanished_tracked_config_is_still_a_finding(self):
        self.findings()
        (self.root / "wrangler.toml").unlink()
        out = guards.run(self.root, self.root / "registry.json", REPO, None)
        self.assertTrue(any("cannot parse" in f for f in out), out)


class LiveTree(unittest.TestCase):
    """The repository as committed."""

    def setUp(self):
        self.root = SCRIPTS.parent
        self.registry = json.loads((SCRIPTS / "worker-registry.json").read_text(encoding="utf-8"))

    def test_this_tree_passes(self):
        self.assertEqual(guards.main(["--root", str(self.root)]), 0)

    def test_registry_names_all_nine_workers(self):
        self.assertEqual(
            set(self.registry["workers"]),
            {
                "admin-jays-services",
                "alert-ingress",
                "expenses-jays-services",
                "fleet-share-api",
                "pr-conflict-watch",
                "simple-tools-mcp",
                "start-jays-services",
                "usage-monitor-receipt-inbox",
                "usage-monitor-receipt-lifecycle-auditor",
            },
        )

    def test_admin_records_that_it_has_no_config(self):
        row = self.registry["workers"]["admin-jays-services"]
        self.assertIsNone(row["config"])
        self.assertTrue(row["no_config_reason"].strip())

    def test_free_plan_rehearsal_flags_only_the_configs_above_50(self):
        # Worked out from the tree, so the downgrade change itself still passes this test.
        above = []
        for path in guards.tracked_configs(self.root):
            value = guards.load_config(self.root / path)["limits"]["subrequests"]
            if value > self.registry["free_plan_subrequests"]:
                above.append(f"{path}: limits.subrequests {value} is above the allowed 50")
        out = guards.run(self.root, SCRIPTS / "worker-registry.json", REPO, "free")
        self.assertEqual(len(out), len(above), out)
        for line, expected in zip(sorted(out), sorted(above)):
            self.assertIn(expected, line)

    def test_the_deploy_config_is_named_first_for_pr_conflict_watch(self):
        row = self.registry["workers"]["pr-conflict-watch"]
        self.assertEqual(row["config"], "workers/pr-conflict-watch/zulip-cutover/wrangler.toml")


if __name__ == "__main__":
    unittest.main()
