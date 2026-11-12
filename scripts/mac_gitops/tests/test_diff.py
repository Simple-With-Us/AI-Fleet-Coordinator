#!/usr/bin/env python3
"""Unit tests for mac_gitops diff logic (no launchctl, no pm2)."""

from __future__ import annotations

import plistlib
import tempfile
import unittest
from pathlib import Path

from mac_gitops.diff import (
    DiffResult,
    DriftKind,
    MacLayout,
    diff_launch_agent,
    diff_manifest,
    diff_pm2,
    parse_pm2_jlist,
)
from mac_gitops.manifest import HelperPair, LaunchAgentSpec, MacManifest, Pm2Spec, load_manifest
from mac_gitops.plist_norm import plist_views_equal, plist_view


REPO_ROOT = Path(__file__).resolve().parents[3]


class PlistNormTests(unittest.TestCase):
    def test_home_normalization_makes_views_equal(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "owner"
            home.mkdir()
            a = home / "a.plist"
            b = home / "b.plist"
            payload = {
                "Label": "com.jay.example",
                "ProgramArguments": ["/bin/bash", "/Users/jay/apps/foo.sh"],
            }
            for path in (a, b):
                with path.open("wb") as handle:
                    plistlib.dump(payload, handle)
            want = plist_view(a, str(home))
            have = plist_view(b, str(home))
            self.assertTrue(plist_views_equal(want, have))


class DiffLaunchAgentTests(unittest.TestCase):
    def test_missing_installed_plist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            (home / "Library" / "LaunchAgents").mkdir(parents=True)
            repo = root / "repo"
            (repo / "scripts/launchd").mkdir(parents=True)
            template = repo / "scripts/launchd/com.jay.test.plist"
            with template.open("wb") as handle:
                plistlib.dump({"Label": "com.jay.test", "RunAtLoad": True}, handle)
            spec = LaunchAgentSpec("com.jay.test", "scripts/launchd/com.jay.test.plist")
            layout = MacLayout(home=home, launch_agents_dir=home / "Library" / "LaunchAgents", repo_root=repo)
            drifts: list = []
            diff_launch_agent(spec, layout, drifts)
            self.assertEqual(len(drifts), 1)
            self.assertEqual(drifts[0].kind, DriftKind.PLIST_MISSING_INSTALLED)

    def test_helper_drift_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            agents = home / "Library" / "LaunchAgents"
            agents.mkdir(parents=True)
            repo = root / "repo"
            (repo / "scripts").mkdir(parents=True)
            (home / "apps").mkdir()
            (repo / "scripts" / "launchd").mkdir(parents=True)
            repo_tpl = repo / "scripts/launchd/com.jay.test.plist"
            installed = agents / "com.jay.test.plist"
            payload = {"Label": "com.jay.test", "RunAtLoad": True}
            for path in (repo_tpl, installed):
                with path.open("wb") as handle:
                    plistlib.dump(payload, handle)
            (repo / "scripts" / "helper.sh").write_text("#!/bin/bash\necho v1\n")
            (home / "apps" / "helper.sh").write_text("#!/bin/bash\necho v2\n")
            spec = LaunchAgentSpec(
                "com.jay.test",
                "scripts/launchd/com.jay.test.plist",
                (HelperPair("scripts/helper.sh", "apps/helper.sh"),),
            )
            layout = MacLayout(home=home, launch_agents_dir=agents, repo_root=repo)
            drifts: list = []
            diff_launch_agent(spec, layout, drifts)
            kinds = {d.kind for d in drifts}
            self.assertIn(DriftKind.HELPER_DRIFT, kinds)


class DiffPm2Tests(unittest.TestCase):
    def test_ecosystem_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            (home / "apps").mkdir(parents=True)
            repo = root / "repo"
            (repo / "scripts").mkdir(parents=True)
            (repo / "scripts" / "pm2-ecosystem.config.cjs").write_text("module.exports = {};\n")
            (home / "apps" / "pm2-ecosystem.config.cjs").write_text("module.exports = {x:1};\n")
            pm2 = Pm2Spec("scripts/pm2-ecosystem.config.cjs", "apps/pm2-ecosystem.config.cjs", ("a",))
            layout = MacLayout(home=home, launch_agents_dir=home / "Library" / "LaunchAgents", repo_root=repo)
            drifts: list = []
            diff_pm2(pm2, layout, drifts)
            self.assertTrue(any(d.kind == DriftKind.PM2_ECOSYSTEM_DRIFT for d in drifts))


class ManifestIntegrationTests(unittest.TestCase):
    def test_repo_manifest_loads(self):
        path = REPO_ROOT / "deploy" / "mac" / "manifest.toml"
        manifest = load_manifest(path)
        self.assertEqual(manifest.issue, "AFC-375")
        self.assertGreaterEqual(len(manifest.launch_agents), 10)
        self.assertGreaterEqual(len(manifest.excluded_launch_agents), 5)
        self.assertIn("com.jay.operator", manifest.excluded_labels)
        self.assertNotIn("com.jay.operator", manifest.desired_labels)

    def test_clean_tree_no_installed_plists_reports_missing(self):
        path = REPO_ROOT / "deploy" / "mac" / "manifest.toml"
        manifest = load_manifest(path)
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / "Library" / "LaunchAgents").mkdir(parents=True)
            layout = MacLayout(
                home=home,
                launch_agents_dir=home / "Library" / "LaunchAgents",
                repo_root=REPO_ROOT,
                pm2_app_names=frozenset(manifest.pm2.app_names),
            )
            result = diff_manifest(manifest, layout, report_excluded=False)
            self.assertFalse(result.ok)
            self.assertTrue(
                all(d.kind == DriftKind.PLIST_MISSING_INSTALLED for d in result.drifts[:3])
                or len(result.drifts) >= len(manifest.launch_agents)
            )


class ParsePm2Tests(unittest.TestCase):
    def test_parse_pm2_jlist_names(self):
        sample = '[{"name":"a","pm2_env":{"name":"a"}},{"name":"b"}]'
        self.assertEqual(parse_pm2_jlist(sample), frozenset({"a", "b"}))


if __name__ == "__main__":
    unittest.main()
