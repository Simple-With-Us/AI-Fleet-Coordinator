"""Install code in temporary homes only, with activation explicitly disabled."""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent


class InstallTests(unittest.TestCase):
    def assert_bundle(self, directory):
        for rel in ["service_zulip.py", "simple_tools/secretscan.py", "fleet-apps.json"]:
            self.assertTrue((directory / rel).is_file(), rel)
        spec = importlib.util.spec_from_file_location(
            "installed_service", directory / "service_zulip.py"
        )
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        self.assertIn("claude-bot@simplewithus.zulipchat.com", helper.agent_emails())

    def test_testflight_files_only_does_not_call_launchctl(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            dest = home / "apps/ios-fleet"
            dest.mkdir(parents=True)
            (dest / "asc-api.mjs").write_text("// fixture")
            binary = home / "bin"
            binary.mkdir()
            marker = home / "launchctl-called"
            executable = binary / "launchctl"
            executable.write_text('#!/bin/sh\ntouch "$HOME/launchctl-called"\nexit 1\n')
            executable.chmod(0o755)
            env = dict(
                os.environ,
                HOME=str(home),
                IOS_FLEET_DIR=str(dest),
                PATH=str(binary) + ":" + os.environ["PATH"],
            )
            subprocess.run(
                [
                    "bash",
                    str(ROOT / "scripts/ios-fleet/install-testflight-freshness.sh"),
                    "--no-launchd",
                ],
                env=env,
                check=True,
                capture_output=True,
            )
            self.assert_bundle(home / "apps")
            self.assertFalse(marker.exists())
            self.assertFalse((home / "Library/LaunchAgents").exists())

    def test_reaper_files_only_does_not_call_subprocess_or_write_hooks(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, HOME=tmp):
            spec = importlib.util.spec_from_file_location(
                "reaper_install", ROOT / "scripts/install-sim-idle-reaper.py"
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            with (
                patch.object(sys, "platform", "darwin"),
                patch.object(sys, "argv", ["install", "--skip-launchd", "--skip-hooks"]),
                patch.object(
                    module.subprocess, "run", side_effect=AssertionError("activation forbidden")
                ),
            ):
                self.assertEqual(module.main(), 0)
            self.assert_bundle(Path(tmp) / "apps")
            self.assertFalse((Path(tmp) / ".claude").exists())
            self.assertFalse((Path(tmp) / "Library/LaunchAgents").exists())


if __name__ == "__main__":
    unittest.main()
