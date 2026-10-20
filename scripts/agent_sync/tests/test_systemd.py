"""User service generation tests.  No live service or credentials are touched."""

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_sync import systemd
from agent_sync.tests.listener_harness import ListenerHarness


class UnitTests(unittest.TestCase):
    def test_quote_paths_and_default_python(self):
        text = systemd.unit_text('/home/a b%"$\\x', "/usr/bin/python3", "/home/a b/tool")
        self.assertIn('ExecStart="/usr/bin/python3" "/home/a b/tool" "daemon" "run"', text)
        self.assertIn('%%\\"$\\\\x', text)
        self.assertIn("AGENT_SYNC_INSTANCE=mac", text)
        self.assertIn("EnvironmentFile=-/home/a b%%", text)
        self.assertIn("WorkingDirectory=/home/a b%%", text)
        self.assertIn('"/a$$b%%c"', systemd.quote("/a$b%c"))
        with self.assertRaises(ValueError):
            systemd.quote("/a\nb")

    def test_install_preserves_config_and_uninstall_keeps_state(self):
        with tempfile.TemporaryDirectory() as home:
            out = io.StringIO()
            with patch.object(systemd.subprocess, "run") as run:
                systemd.install({}, home, out)
                run.assert_not_called()
            config = Path(home, ".agent-sync/listener.toml")
            self.assertIn('instance = "mac"', config.read_text())
            self.assertNotIn("[seat.", config.read_text())
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            self.assertIn('ExecStart="/usr/bin/python3"', Path(systemd.unit_path(home)).read_text())
            self.assertFalse(Path(home, ".agent-sync/listener.env").exists())
            config.write_text("existing")
            systemd.install({}, home, out)
            self.assertEqual(config.read_text(), "existing")
            systemd.uninstall(home, out)
            self.assertFalse(Path(systemd.unit_path(home)).exists())
            self.assertTrue(config.exists())

    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            systemd.install({}, home, io.StringIO(), dry_run=True)
            self.assertEqual(list(Path(home).iterdir()), [])

    def test_status_bounded_and_does_not_echo_errors(self):
        with patch.object(
            systemd.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, "active\n", "private"),
        ) as run:
            self.assertEqual(systemd.status("/nonexistent")["active_state"], "active")
            self.assertEqual(run.call_args.kwargs["timeout"], 3)
        with patch.object(systemd.subprocess, "run", side_effect=OSError("private")):
            self.assertEqual(systemd.status("/nonexistent")["active_state"], "unknown")


class DispatchTests(ListenerHarness):
    def test_linux_install_and_status(self):
        with patch("agent_sync.listener_cli.sys.platform", "linux"):
            result = self.run_cli("daemon", "install", env=self.env(AGENT_SYNC_STATE_DIR=None))
            self.assertEqual(result.code, 0, result.err)
            self.assertTrue(Path(systemd.unit_path(str(self.home))).exists())
            self.assertIn(
                'ExecStart="/usr/bin/python3"', Path(systemd.unit_path(str(self.home))).read_text()
            )
            help_result = self.run_cli("daemon", "install", "--help")
            self.assertIn("/usr/bin/python3", help_result.out)
            self.assertNotIn("/opt/homebrew", help_result.out)
            with patch(
                "agent_sync.systemd.status",
                return_value={"installed": True, "active_state": "inactive"},
            ):
                result = self.run_cli("daemon", "status", "--json")
            self.assertEqual(
                json.loads(result.out)["systemd_user_service"]["active_state"], "inactive"
            )

    def test_unsupported_platform_refuses_without_writes(self):
        with patch("agent_sync.listener_cli.sys.platform", "win32"):
            result = self.run_cli("daemon", "install")
            self.assertNotEqual(result.code, 0)
            self.assertIn("only macOS and Linux", result.err)
            self.assertFalse(Path(systemd.unit_path(str(self.home))).exists())
