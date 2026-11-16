"""Compatibility entrypoint contract; sync behavior is covered by test_code_main_sync."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[2] / "code-main-keeper.sh"


class KeeperEntrypoint(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name)
        self.entry = self.home / "apps" / "code-main-keeper.sh"
        self.entry.parent.mkdir()
        shutil.copy2(SCRIPT, self.entry)
        self.env = {"HOME": str(self.home), "PATH": os.environ["PATH"],
                    "CODE_ROOT": str(self.home / "legacy-code")}
        self.log = self.home / "apps/logs/code-main-keeper.log"

    def shared(self, installed: bool = False, label: str = "sibling") -> Path:
        base = self.home / ".local/share/code-mirrors/scripts" if installed else self.entry.parent
        path = base / "fleet_lanes/code_main_sync.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "import json, os, sys\n"
            "print(json.dumps({'args': sys.argv[1:], 'root': os.environ['FLEET_CODE_ROOT'], "
            "'relay': os.environ.get('FLEET_SYNC_RELAY_URL'), 'label': " + repr(label) + "}))\n"
            "sys.exit(7 if '--fail' in sys.argv else 0)\n", encoding="utf-8")
        return path

    def keeper(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["/bin/bash", str(self.entry), *args], env=self.env,
                              capture_output=True, text=True, timeout=30)

    def receipt(self) -> dict:
        return json.loads(self.log.read_text(encoding="utf-8").splitlines()[-1])

    def test_checkout_sibling_takes_precedence_and_keeps_existing_log(self) -> None:
        self.shared()
        self.shared(installed=True, label="installed")
        self.log.parent.mkdir(parents=True)
        self.log.write_text("previous log entry\n", encoding="utf-8")
        self.assertEqual(self.keeper("--dry-run").returncode, 0)
        self.assertEqual(self.receipt(), {"args": ["--dry-run"], "root": self.env["CODE_ROOT"],
                                         "relay": None, "label": "sibling"})
        self.assertTrue(self.log.read_text().startswith("previous log entry\n"))

    def test_installed_fallback_maps_short_dry_run_and_preserves_host_configuration(self) -> None:
        self.shared(installed=True, label="installed")
        self.env["FLEET_CODE_ROOT"] = str(self.home / "host-code")
        self.env["FLEET_SYNC_RELAY_URL"] = "https://relay.example.invalid"
        self.env["CODE_MAIN_KEEPER_LOG_DIR"] = str(self.home / "custom-log")
        self.log = self.home / "custom-log/code-main-keeper.log"
        self.assertEqual(self.keeper("-n").returncode, 0)
        self.assertEqual(self.receipt(), {"args": ["--dry-run"], "root": self.env["FLEET_CODE_ROOT"],
                                         "relay": self.env["FLEET_SYNC_RELAY_URL"], "label": "installed"})

    def test_shared_failure_propagates_to_the_existing_scheduler(self) -> None:
        self.shared()
        self.assertEqual(self.keeper("--fail").returncode, 7)

    def test_missing_shared_implementation_fails_without_starting_a_second_sync(self) -> None:
        result = self.keeper()
        self.assertEqual(result.returncode, 1)
        self.assertIn("shared code_main_sync.py is missing", result.stderr)
        self.assertFalse(self.log.exists())
