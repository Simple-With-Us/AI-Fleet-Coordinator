"""User service generation tests.  No live service or credentials are touched."""

import io
import json
import os
import subprocess
import sys
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
        self.assertNotIn("EnvironmentFile", text)
        self.assertIn("WorkingDirectory=/home/a b%%", text)
        self.assertIn('"/a$$b%%c"', systemd.quote("/a$b%c"))
        with self.assertRaises(ValueError):
            systemd.quote("/a\nb")

    def test_install_preserves_config_and_uninstall_keeps_state(self):
        with tempfile.TemporaryDirectory() as home:
            out = io.StringIO()
            with patch.object(systemd.subprocess, "run") as run:
                systemd.install({}, home, out)
                self.assertFalse([c for c in run.call_args_list if "systemctl" in str(c)])
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

    def test_sample_supports_owner_initialization(self):
        from agent_sync.listener_cli import _rewrite_pins
        result = _rewrite_pins(systemd.sample_config(), 42, [11])
        self.assertIn("owner_user_id = 42", result)
        self.assertIn("eligible_user_ids = [11]", result)

    def test_custom_config_and_state_are_pinned_in_service(self):
        with tempfile.TemporaryDirectory() as home:
            state = str(Path(home, "custom state"))
            config = str(Path(home, "config", "listener.toml"))
            systemd.install({"AGENT_SYNC_STATE_DIR": state, "AGENT_SYNC_CONFIG": config}, home, io.StringIO())
            unit = Path(systemd.unit_path(home)).read_text()
            self.assertIn('"AGENT_SYNC_STATE_DIR=' + state + '"', unit)
            self.assertIn('"AGENT_SYNC_CONFIG=' + config + '"', unit)
            self.assertTrue(Path(config).is_file())
            self.assertFalse(Path(home, ".agent-sync/listener.toml").exists())

    def test_service_directory_is_private_and_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as home:
            systemd.install({}, home, io.StringIO())
            directory = Path(systemd.unit_path(home)).parent
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as outside:
            directory = Path(systemd.unit_path(home)).parent
            directory.parent.mkdir(parents=True)
            directory.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                systemd.install({}, home, io.StringIO())
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_symlinked_home_installs_but_a_link_below_it_is_refused(self):
        with tempfile.TemporaryDirectory() as real:
            home = str(Path(real, "link"))
            os.symlink(real, home)
            systemd.install({}, home, io.StringIO())
            self.assertTrue(Path(systemd.unit_path(home)).exists())
        # The guard still covers the directory chain the unit is created below.
        with tempfile.TemporaryDirectory() as real, tempfile.TemporaryDirectory() as outside:
            home = str(Path(real, "link"))
            os.symlink(real, home)
            directory = Path(systemd.unit_path(home)).parent
            directory.parent.mkdir(parents=True)
            directory.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                systemd.install({}, home, io.StringIO())
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_missing_interpreter_falls_back_or_warns_and_control_home_is_rejected(self):
        with tempfile.TemporaryDirectory() as home:
            original = systemd.os.access
            with patch.object(systemd.os, "access", side_effect=lambda path, mode: False if path == systemd.DEFAULT_PYTHON else original(path, mode)):
                systemd.install({}, home, io.StringIO())
            self.assertIn(systemd.quote(sys.executable), Path(systemd.unit_path(home)).read_text())
            out = io.StringIO()
            systemd.install({}, home, out, python="/not-an-interpreter")
            self.assertIn("is not executable", out.getvalue())
        with self.assertRaises(ValueError):
            systemd.unit_text("/home/bad\nRestart=no", "/usr/bin/python3", "/tool")

    def test_old_interpreter_falls_back_or_is_noted(self):
        with tempfile.TemporaryDirectory() as home:
            out = io.StringIO()
            with patch.object(systemd, "too_old", return_value=True):
                systemd.install({}, home, out)
            self.assertIn(systemd.quote(sys.executable), Path(systemd.unit_path(home)).read_text())
            # The fallback interpreter was never chosen by the operator, so it is reported.
            self.assertIn(sys.executable, out.getvalue())
            out = io.StringIO()
            with patch.object(systemd, "too_old", return_value=True):
                systemd.install({}, home, out, python="/usr/bin/python3")
            self.assertIn("older than Python 3.11", out.getvalue())
        with patch.object(
            systemd.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "3.9\n", "")
        ):
            self.assertEqual(systemd.interpreter_version("/usr/bin/python3"), (3, 9))
            self.assertTrue(systemd.too_old("/usr/bin/python3"))
        for error in (OSError("private"), subprocess.TimeoutExpired("python3", 10)):
            with patch.object(systemd.subprocess, "run", side_effect=error):
                self.assertIsNone(systemd.interpreter_version("/usr/bin/python3"))
                self.assertFalse(systemd.too_old("/usr/bin/python3"))

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
        for error, expected in ((FileNotFoundError("private"), 127), (subprocess.TimeoutExpired("systemctl", 3), 124)):
            with patch.object(systemd.subprocess, "run", side_effect=error):
                info = systemd.status("/nonexistent")
                self.assertEqual(info["query_exit_code"], expected)
                self.assertNotIn("private", json.dumps(info))


class DispatchTests(ListenerHarness):
    def test_linux_install_and_status(self):
        with patch("agent_sync.listener_cli.sys.platform", "linux"):
            # The system Python is the default only when this host's copy is 3.11 or newer; a Mac's
            # /usr/bin/python3 is 3.9 and install() falls back deliberately (tested in UnitTests).
            with patch.object(systemd, "too_old", return_value=False):
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
