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
            with patch.object(systemd.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "3.12\n", "")) as run:
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

    def test_symlinked_home_and_links_below_it_are_refused(self):
        with tempfile.TemporaryDirectory() as real:
            home = str(Path(real, "link"))
            os.symlink(real, home)
            for spelling in (home, home + os.sep):
                with self.assertRaises(ValueError):
                    systemd.install({}, spelling, io.StringIO())
            self.assertFalse(Path(systemd.unit_path(home)).exists())
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
            self.assertEqual(systemd.install({}, home, out, python="/not-an-interpreter"), 127)
            self.assertIn("not executable", out.getvalue())
        with self.assertRaises(ValueError):
            systemd.unit_text("/home/bad\nRestart=no", "/usr/bin/python3", "/tool")

    def test_old_interpreter_falls_back_or_is_noted(self):
        with tempfile.TemporaryDirectory() as home:
            out = io.StringIO()
            with patch.object(systemd, "too_old", side_effect=[True, False]):
                self.assertEqual(systemd.install({}, home, out), 0)
            self.assertIn(systemd.quote(sys.executable), Path(systemd.unit_path(home)).read_text())
            # The fallback interpreter was never chosen by the operator, so it is reported.
            self.assertIn(sys.executable, out.getvalue())
            out = io.StringIO()
            with patch.object(systemd, "too_old", return_value=True):
                self.assertEqual(systemd.install({}, home, out, python=sys.executable), 4)
            self.assertIn("older than Python 3.11", out.getvalue())
        with patch.object(
            systemd.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "3.9\n", "")
        ):
            self.assertEqual(systemd.interpreter_version("/usr/bin/python3"), (3, 9))
            self.assertTrue(systemd.too_old("/usr/bin/python3"))
        for error in (OSError("private"), subprocess.TimeoutExpired("python3", 10)):
            with patch.object(systemd.subprocess, "run", side_effect=error):
                with self.assertRaises(type(error)):
                    systemd.interpreter_version("/usr/bin/python3")

    def test_failed_probe_exits_before_the_unit_is_written(self):
        for error, expected_exit_code in (
            (FileNotFoundError("private"), 127),
            (subprocess.TimeoutExpired("python3", 10), 124),
            (OSError("private"), 1),
        ):
            with tempfile.TemporaryDirectory() as home:
                with patch.object(systemd.os, "access", return_value=True):
                    with patch.object(systemd.subprocess, "run", side_effect=error):
                        code = systemd.install({}, home, io.StringIO(), python=sys.executable)
                self.assertEqual(code, expected_exit_code)
                self.assertFalse(Path(systemd.unit_path(home)).exists())
                self.assertFalse(Path(home, ".agent-sync", "listener.toml").exists())

    def test_empty_python_and_unknown_version_fail_closed(self):
        with tempfile.TemporaryDirectory() as home:
            with patch.object(systemd, "too_old", return_value=False):
                self.assertEqual(systemd.install({}, home, io.StringIO(), python=""), 0)
            self.assertNotIn('ExecStart=""', Path(systemd.unit_path(home)).read_text())
        with tempfile.TemporaryDirectory() as home:
            with patch.object(systemd, "interpreter_version", return_value=None):
                self.assertEqual(systemd.install({}, home, io.StringIO(), python=sys.executable), 1)
            self.assertFalse(Path(systemd.unit_path(home)).exists())

    def test_mode_failure_cleans_temp_unit(self):
        with tempfile.TemporaryDirectory() as home:
            err = io.StringIO()
            with patch.object(systemd, "too_old", return_value=False), patch.object(systemd.os, "fchmod", side_effect=OSError("private detail")):
                self.assertEqual(systemd.install({}, home, io.StringIO(), err=err), 1)
            self.assertEqual(list(Path(systemd.unit_path(home)).parent.iterdir()), [])
            self.assertNotIn("private detail", err.getvalue())

    def test_default_interpreter_is_probed_once(self):
        # A second spawn could fail transiently and abort an install whose runtime was just confirmed.
        for explicit in (None, sys.executable):
            probes = []

            def record(argv, **kwargs):
                probes.append(argv[0])
                return subprocess.CompletedProcess([], 0, "3.12\n", "")

            with tempfile.TemporaryDirectory() as home:
                with patch.object(systemd.os, "access", return_value=True):
                    with patch.object(systemd.subprocess, "run", side_effect=record):
                        systemd.install({}, home, io.StringIO(), python=explicit)
            self.assertEqual(probes, [systemd.DEFAULT_PYTHON if explicit is None else explicit])

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

    def test_probe_failure_returns_cli_result_and_stderr(self):
        with patch("agent_sync.listener_cli.sys.platform", "linux"), patch.object(systemd.os, "access", return_value=True), patch.object(systemd.subprocess, "run", side_effect=subprocess.TimeoutExpired("python", 10)):
            result = self.run_cli("daemon", "install", "--python", sys.executable)
        self.assertEqual(result.code, 124)
        self.assertIn("version probe failed", result.err)
        self.assertNotIn("version probe failed", result.out)
        self.assertFalse(Path(systemd.unit_path(str(self.home))).exists())

    def test_unsupported_platform_refuses_without_writes(self):
        with patch("agent_sync.listener_cli.sys.platform", "win32"):
            result = self.run_cli("daemon", "install")
            self.assertNotEqual(result.code, 0)
            self.assertIn("only macOS and Linux", result.err)
            self.assertFalse(Path(systemd.unit_path(str(self.home))).exists())

class RelativeExecutableTests(unittest.TestCase):
    def test_install_absolutizes_relative_overrides_before_probe_and_write(self):
        with tempfile.TemporaryDirectory() as home:
            out = io.StringIO()
            with patch.object(systemd.os, "access", return_value=True), patch.object(systemd, "_confirmed_too_old", return_value=False) as probe:
                result = systemd.install({}, home, out, python="./venv/bin/python", program="scripts/agent-sync")
            self.assertEqual(result, 0)
            self.assertEqual(probe.call_args.args[0], os.path.abspath("./venv/bin/python"))
            self.assertIn(systemd.quote(os.path.abspath("scripts/agent-sync")), Path(systemd.unit_path(home)).read_text())

    def test_timeout_identifies_interpreter(self):
        with patch.object(systemd, "too_old", side_effect=subprocess.TimeoutExpired("probe", 1)):
            with self.assertRaises(systemd.InterpreterProbeFailure) as error:
                systemd._confirmed_too_old("/chosen/python", io.StringIO())
        self.assertIn("/chosen/python", str(error.exception))
        self.assertEqual(error.exception.code, 124)
