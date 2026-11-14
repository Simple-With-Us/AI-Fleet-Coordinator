"""Offline checks for the shared Cursor Cloud letter unit, its timer and its installer (owner ruling, Nov 12, 2026:
one Operator unit, no seat's own copy).

The unit and timer must name no seat, no ssh remote and no shared registry env file, and the unit must run the sweep
with --api-only and --no-dm.  The installer runs under a temporary test root (CURSOR_LETTERS_INSTALL_TEST_ROOT):  no
real home, no systemctl and no network.  Its runtime is a synthetic fleet-cli folder whose simple_tools is this
checkout's, beside stub fleet_knobs and fleet_secrets packages that answer from a fixture file, so the installer's
check runs the real `cursor_cloud_letters check`.  Fixture tokens are built in parts at run time, so no
credential-shaped literal sits in this file, and every run's output is checked for them.

Run:  cd scripts && python3 -m unittest simple_tools.tests.test_cursor_cloud_letters_unit -v
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2]
SERVER = SCRIPTS / "simple_tools" / "server"
INSTALL = SERVER / "install-cursor-cloud-letters.sh"
SERVICE = SERVER / "afc-cursor-cloud-letters.service"
TIMER = SERVER / "afc-cursor-cloud-letters.timer"
KNOBS_TOKEN = "st" + "." + "fixture" + "-knobs-not-a-real-token-" + "0123456789"
CURSOR_TOKEN = "st" + "." + "fixture" + "-cursor-not-a-real-token-" + "0123456789"
MARKER = "# Installed by install-cursor-cloud-letters.sh"

# Every seat tag a unit might have been named for.  CURSOR is the platform this unit serves, and appears only as the
# word Cursor in prose;  the check below is for a seat as an identity (AGENT_SEAT, a tag in upper case, a token name).
SEAT_TAGS = ("CLAUDE", "CODEX", "MINIMAX", "GROK", "ANTIGRAVITY", "OPENCODE", "ZCODE", "MUSE", "CLUTCH", "OPERATOR")

KNOBS_STUB = '''
import json, os


def get(name):
    with open(os.path.join(os.environ["HOME"], "knobs.json")) as handle:
        data = json.load(handle)
    if os.environ.get("FLEET_KNOBS_INFISICAL_TOKEN") != data["token"]:
        error = RuntimeError("unauthorized")
        error.reason = "service-token-required"
        raise error
    return data["values"].get(name, "")
'''
REGISTRY_STUB = '''
BY_NAME = {"SIMPLE_TOOLS_OPERATOR_REGISTRY_URL": object(), "SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY": object()}
'''
SECRETS_STUB = '''
import json, os


class SecretsError(Exception):
    pass


def home_dir(env):
    return env.get("HOME", "")


def infisical_project_id(env, home):
    return "00000000-0000-4000-8000-000000000000"


def fetch_project(project, path, env, token=None, names=None, timeout=None):
    with open(os.path.join(os.environ["HOME"], "cursor.json")) as handle:
        data = json.load(handle)
    if token != data["token"] or path != "/cursor-cloud-letters" or list(names) != ["CURSOR_SYNC_API_KEY"]:
        raise SecretsError("token-scope-mismatch")
    return {"CURSOR_SYNC_API_KEY": data["value"]}
'''
SYSTEMCTL_STUB = """#!/bin/sh
here="$(dirname "$0")/.."
echo "systemctl $*" >> "$here/systemctl.log"
if [ -z "$XDG_RUNTIME_DIR" ]; then echo "Failed to connect to user scope bus" >&2; exit 1; fi
if [ "$2" = "is-active" ]; then
  if [ -f "$here/active-$3" ]; then echo active; exit 0; fi
  echo inactive; exit 3
fi
exit 0
"""
LOGINCTL_STUB = """#!/bin/sh
cat "$(dirname "$0")/../linger"
"""


class UnitTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = SERVICE.read_text(encoding="utf-8")
        self.timer = TIMER.read_text(encoding="utf-8")

    def directives(self, text: str) -> list[str]:
        return [line for line in text.splitlines() if line and not line.startswith("#")]

    def test_the_units_name_no_seat_no_remote_and_no_registry_env_file(self) -> None:
        for name, text in (("service", self.service), ("timer", self.timer)):
            for banned in ("AGENT_SEAT", "operator-registry-env", "mm-registry-env", "SESSION_LETTER_REMOTE",
                           "--remote", "LETTER_REMOTE", "simple-chat dm", "libexec", "fleet-secrets/"):
                self.assertNotIn(banned, text, "%s names %s" % (name, banned))
            for tag in SEAT_TAGS:
                self.assertIsNone(re.search(r"\b%s\b" % tag, text), "%s names the seat %s" % (name, tag))
            self.assertNotIn("claude", text.lower(), "%s names a seat" % name)
            self.assertNotIn("CURSOR_SYNC_API_KEY=", text)

    def test_the_service_runs_the_shared_sweep_from_the_runtime(self) -> None:
        lines = self.directives(self.service)
        self.assertIn("Type=oneshot", lines)
        self.assertIn("EnvironmentFile=-%h/.config/afc-cursor-cloud-letters/env", lines)
        self.assertIn("WorkingDirectory=%h/.local/share/fleet-cli/current/scripts", lines)
        exec_start = [line for line in lines if line.startswith("ExecStart=")]
        self.assertEqual(exec_start, [
            "ExecStart=/usr/bin/python3 -E -s -m simple_tools.cursor_cloud_letters sweep --api-only --no-dm"])
        self.assertTrue(any(line.startswith("TimeoutStartSec=") for line in lines))
        self.assertIn("NoNewPrivileges=yes", lines)
        self.assertNotIn("[Install]", lines)  # the timer starts it
        # No other Cursor key or Infisical login reaches the sweep, whatever the user manager passes on.
        unset = [line for line in lines if line.startswith("UnsetEnvironment=")]
        self.assertEqual(len(unset), 1)
        self.assertEqual(set(unset[0].split("=", 1)[1].split()), {
            "CURSOR_API_KEY", "CURSOR_SYNC_API_KEY", "INFISICAL_TOKEN", "INFISICAL_AUTOMATION_CLIENT_ID",
            "INFISICAL_AUTOMATION_CLIENT_SECRET"})

    def test_the_timer_runs_every_ten_minutes_after_the_last_run(self) -> None:
        lines = self.directives(self.timer)
        self.assertIn("OnUnitInactiveSec=10min", lines)
        self.assertIn("Unit=afc-cursor-cloud-letters.service", lines)
        self.assertIn("WantedBy=timers.target", lines)


class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        (self.root / "bin").mkdir()
        for name, text in (("systemctl", SYSTEMCTL_STUB), ("loginctl", LOGINCTL_STUB)):
            path = self.root / "bin" / name
            path.write_text(text)
            path.chmod(0o755)
        (self.root / "linger").write_text("yes\n")
        runtime = self.home / ".local" / "share" / "fleet-cli" / "current" / "scripts"
        runtime.mkdir(parents=True)
        (runtime / "simple_tools").symlink_to(SCRIPTS / "simple_tools")
        (runtime / "fleet_infisical.py").write_text(SECRETS_STUB)
        for package, files in (("fleet_knobs", {"knobs.py": KNOBS_STUB, "registry.py": REGISTRY_STUB}),
                               ("fleet_secrets", {"core.py": SECRETS_STUB})):
            (runtime / package).mkdir()
            (runtime / package / "__init__.py").write_text("")
            for name, text in files.items():
                (runtime / package / name).write_text(text)
        (self.home / "knobs.json").write_text(json.dumps({"token": KNOBS_TOKEN, "values": {
            "SIMPLE_TOOLS_OPERATOR_REGISTRY_URL": "https://registry.fake.invalid",
            "SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY": "fixture-registry-key-" + "0123456789abcdef"}}))
        (self.home / "cursor.json").write_text(json.dumps({"token": CURSOR_TOKEN, "value": "fixture-cursor-key"}))
        self.staging = self.home / ".config" / "infisical-staging"
        self.staging.mkdir(parents=True)
        self.knobs_staged = self.staging / "afc-cursor-cloud-letters-knobs.env"
        self.cursor_staged = self.staging / "afc-cursor-cloud-letters-cursor.env"
        self.stage(self.knobs_staged, "FLEET_KNOBS_INFISICAL_TOKEN=" + KNOBS_TOKEN)
        self.stage(self.cursor_staged, CURSOR_TOKEN)  # a bare token is accepted too
        self.units = self.home / ".config" / "systemd" / "user"
        self.env_file = self.home / ".config" / "afc-cursor-cloud-letters" / "env"

    def stage(self, path: Path, line: str) -> None:
        path.write_text(line + "\n")
        path.chmod(0o600)

    def run_installer(self, *args: str) -> subprocess.CompletedProcess:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "CURSOR_LETTERS_INSTALL_TEST_ROOT": str(self.root),
            "XDG_RUNTIME_DIR": str(self.root / "run"),
            "LANG": "C.UTF-8",
        }
        proc = subprocess.run(["bash", str(INSTALL), *args], env=env, capture_output=True, text=True, timeout=120)
        for secret in (KNOBS_TOKEN, CURSOR_TOKEN, "fixture-cursor-key", "fixture-registry-key"):
            self.assertNotIn(secret, proc.stdout + proc.stderr)
        return proc

    def systemctl_log(self) -> str:
        path = self.root / "systemctl.log"
        return path.read_text() if path.exists() else ""

    def test_a_dry_run_checks_the_reads_and_changes_nothing(self) -> None:
        proc = self.run_installer("--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        for key in ("registry_url", "registry_key", "cursor_key"):
            self.assertIn("%s: present" % key, proc.stdout)
        self.assertIn("dry run:  a real run would move both staged tokens", proc.stdout)
        self.assertFalse(self.units.exists())
        self.assertFalse(self.env_file.exists())
        self.assertTrue(self.knobs_staged.exists() and self.cursor_staged.exists())
        self.assertEqual(self.systemctl_log(), "")

    def test_a_dry_run_with_a_wrong_token_names_the_read_and_exits_three(self) -> None:
        self.stage(self.cursor_staged, "CURSOR_LETTERS_INFISICAL_TOKEN=st." + "fixture-other-token-0123456789")
        proc = self.run_installer("--dry-run")
        self.assertEqual(proc.returncode, 3, proc.stdout + proc.stderr)
        self.assertIn("cursor_key cannot be read (token-scope-mismatch)", proc.stdout)
        self.assertIn("registry_key: present", proc.stdout)

    def test_missing_tokens_and_a_loose_file_are_refused(self) -> None:
        self.cursor_staged.unlink()
        self.knobs_staged.chmod(0o644)
        proc = self.run_installer("--dry-run")
        self.assertEqual(proc.returncode, 3)
        self.assertIn("no CURSOR_LETTERS_INFISICAL_TOKEN", proc.stdout)
        self.assertIn("mode 0600", proc.stdout)

    def test_install_moves_the_tokens_writes_the_units_and_enables_the_timer(self) -> None:
        proc = self.run_installer()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse(self.knobs_staged.exists() or self.cursor_staged.exists())
        self.assertEqual(self.env_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.env_file.parent.stat().st_mode & 0o777, 0o700)
        body = [line for line in self.env_file.read_text().splitlines() if line and not line.startswith("#")]
        self.assertEqual(body, ["FLEET_KNOBS_INFISICAL_TOKEN=" + KNOBS_TOKEN,
                                "CURSOR_LETTERS_INFISICAL_TOKEN=" + CURSOR_TOKEN])
        for unit, template in (("afc-cursor-cloud-letters.service", SERVICE),
                               ("afc-cursor-cloud-letters.timer", TIMER)):
            text = (self.units / unit).read_text()
            self.assertTrue(text.startswith(MARKER))
            self.assertTrue(text.endswith(template.read_text()))
        log = self.systemctl_log()
        self.assertIn("systemctl --user daemon-reload", log)
        self.assertIn("systemctl --user enable afc-cursor-cloud-letters.timer", log)
        self.assertNotIn("start", log)
        # The one route offered to start it is --start, which checks the retired per-seat timer first.
        self.assertIn("not started;  run again with --start", proc.stdout)
        self.assertNotIn("systemctl --user start", proc.stdout)
        again = self.run_installer("--dry-run")  # the installed env file is kept, and checked
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertIn("keep the installed env file", again.stdout)

    def test_start_is_refused_while_the_retired_per_seat_timer_runs(self) -> None:
        self.units.mkdir(parents=True)
        (self.units / "claude-cursor-cloud-letters.timer").write_text("[Timer]\n")
        (self.root / "active-claude-cursor-cloud-letters.timer").write_text("")
        proc = self.run_installer("--start")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("--start is refused while claude-cursor-cloud-letters.timer is active", proc.stdout)
        self.assertIn('(README, "Retiring the per-seat unit")', proc.stdout)
        readme = (SCRIPTS / "simple_tools" / "README.md").read_text(encoding="utf-8")
        self.assertIn("- **Retiring the per-seat unit.**", readme)  # the bullet the installer names exists
        self.assertFalse(self.env_file.exists())
        self.assertTrue((self.units / "claude-cursor-cloud-letters.timer").exists())  # never touched

    def test_a_unit_it_did_not_write_is_never_overwritten(self) -> None:
        self.units.mkdir(parents=True)
        (self.units / "afc-cursor-cloud-letters.service").write_text("[Service]\n")
        proc = self.run_installer()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("was not written by this script", proc.stdout)
        self.assertEqual((self.units / "afc-cursor-cloud-letters.service").read_text(), "[Service]\n")

    def test_uninstall_removes_its_units_and_moves_the_tokens_back(self) -> None:
        self.assertEqual(self.run_installer().returncode, 0)
        proc = self.run_installer("--uninstall")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertFalse((self.units / "afc-cursor-cloud-letters.service").exists())
        self.assertFalse((self.units / "afc-cursor-cloud-letters.timer").exists())
        self.assertFalse(self.env_file.exists())
        self.assertEqual(self.knobs_staged.read_text(), "FLEET_KNOBS_INFISICAL_TOKEN=%s\n" % KNOBS_TOKEN)
        self.assertEqual(self.cursor_staged.read_text(), "CURSOR_LETTERS_INFISICAL_TOKEN=%s\n" % CURSOR_TOKEN)
        self.assertEqual(self.cursor_staged.stat().st_mode & 0o777, 0o600)
        self.assertIn("systemctl --user disable --now afc-cursor-cloud-letters.timer", self.systemctl_log())

    def test_help_prints_the_usage(self) -> None:
        proc = subprocess.run(["bash", str(INSTALL), "--help"], capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertIn("FLEET_KNOBS_INFISICAL_TOKEN", proc.stdout)
        self.assertIn("CURSOR_LETTERS_INFISICAL_TOKEN", proc.stdout)


if __name__ == "__main__":
    unittest.main()
