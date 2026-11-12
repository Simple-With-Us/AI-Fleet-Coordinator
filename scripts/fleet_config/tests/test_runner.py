"""The one runner every child process in this package starts through.

The runner is the safety seam of the package:  a command that is not on the allowlist
is refused before any process exists,  every command has an explicit timeout,  and the
output is capped.  These tests use a fake ``fleet-secrets`` in a temporary directory,  so
no test starts a real fleet tool,  no test writes outside its own temporary tree and no
test reads a secret.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

from fleet_config import apply as apply_module
from fleet_config.checks import (
    CommandRefused,
    check_cli_allowed,
    run_cli,
)


def write_script(directory: str, name: str, body: str) -> str:
    """Write an executable shell script and return its path."""
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\n" + body + "\n")
    os.chmod(path, 0o755)
    return path


class AllowlistTests(unittest.TestCase):
    """Only the commands on the allowlist may start."""

    def test_a_command_that_is_not_on_the_allowlist_is_refused(self):
        with self.assertRaises(CommandRefused):
            check_cli_allowed(["/bin/sh", "-c", "echo hello"])

    def test_the_fleet_secrets_command_is_allowed(self):
        self.assertEqual(
            check_cli_allowed(["fleet-secrets", "names", "--path", "/zulip"]),
            "fleet-secrets",
        )

    def test_the_fleet_secrets_command_is_allowed_by_path(self):
        self.assertEqual(
            check_cli_allowed(["/opt/fleet/bin/fleet-secrets", "names"]),
            "fleet-secrets",
        )

    def test_an_installer_module_is_allowed(self):
        self.assertEqual(
            check_cli_allowed(
                [sys.executable, "-m", "fleet_lanes.install_rules", "apply", "claude"]
            ),
            "fleet_lanes.install_rules",
        )

    def test_an_unknown_module_is_refused(self):
        with self.assertRaises(CommandRefused):
            check_cli_allowed([sys.executable, "-m", "os"])

    def test_the_doctor_script_is_allowed(self):
        self.assertEqual(
            check_cli_allowed([sys.executable, "fleet-tools-doctor.py", "--json"]),
            "fleet-tools-doctor.py",
        )

    def test_an_unknown_script_is_refused(self):
        with self.assertRaises(CommandRefused):
            check_cli_allowed([sys.executable, "not-a-fleet-script.py"])

    def test_an_empty_command_is_refused(self):
        with self.assertRaises(CommandRefused):
            check_cli_allowed([])

    def test_a_refused_command_never_starts(self):
        with tempfile.TemporaryDirectory() as temp:
            marker = os.path.join(temp, "ran")
            script = write_script(temp, "not-a-fleet-tool", "touch %s" % marker)
            with self.assertRaises(CommandRefused):
                run_cli([script, "--not-allowed"], timeout=10)
            self.assertFalse(os.path.exists(marker), "the refused command ran")


class RunnerTests(unittest.TestCase):
    """The runner starts an allowlisted command,  with a timeout and a cap."""

    def test_an_allowlisted_command_runs_and_answers(self):
        with tempfile.TemporaryDirectory() as temp:
            script = write_script(temp, "fleet-secrets", "echo CLAUDE_ZULIP_KEY")
            result = run_cli([script, "names"], timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertIn("CLAUDE_ZULIP_KEY", result.stdout.decode("utf-8"))
        self.assertFalse(result.timed_out)

    def test_a_command_that_overruns_its_budget_is_a_timeout(self):
        with tempfile.TemporaryDirectory() as temp:
            script = write_script(temp, "fleet-secrets", "sleep 30")
            result = run_cli([script, "names"], timeout=1)
        self.assertTrue(result.timed_out)
        self.assertIsNone(result.returncode)

    def test_the_output_is_capped(self):
        with tempfile.TemporaryDirectory() as temp:
            script = write_script(temp, "fleet-secrets", "printf 'x%.0s' $(seq 1 5000)")
            result = run_cli([script, "names"], timeout=10, max_output_bytes=100)
        self.assertTrue(result.truncated)
        self.assertLessEqual(len(result.stdout), 100)

    def test_a_command_that_cannot_start_is_an_error_and_not_a_crash(self):
        with tempfile.TemporaryDirectory() as temp:
            missing = os.path.join(temp, "fleet-secrets")
            result = run_cli([missing, "names"], timeout=10)
        self.assertTrue(result.error)
        self.assertIsNone(result.returncode)


class ApplyRunnerTests(unittest.TestCase):
    """The apply side starts its installers through the same runner."""

    def test_the_apply_runner_refuses_a_command_that_is_not_on_the_allowlist(self):
        code, stdout, stderr = apply_module.default_runner(["/bin/sh", "-c", "echo hello"])
        self.assertEqual(code, 127)
        self.assertIn("allowlist", stderr)
        self.assertEqual(stdout, "")

    def test_the_apply_runner_has_a_timeout(self):
        # The runner is called with a budget,  and a command that overruns it is a
        # failure,  never a hang.
        self.assertGreater(apply_module.DEFAULT_TIMEOUT, 0)
