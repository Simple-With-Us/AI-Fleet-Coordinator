#!/usr/bin/env python3
"""Tests for scripts/check-fleet-ops-project-id.py."""

from __future__ import annotations

import hashlib
import importlib.machinery
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "check-fleet-ops-project-id.py"
REPO_ROOT = SCRIPT.parent.parent
FAKE_SAFE = "00000000-0000-4000-8000-000000000099"
FAKE_BANNED = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
FAKE_BANNED_DIGEST = hashlib.sha256(FAKE_BANNED.lower().encode()).hexdigest()


def _load():
    loader = importlib.machinery.SourceFileLoader("check_fleet_ops_project_id", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = mod
    loader.exec_module(mod)
    return mod


lint = _load()


class FleetOpsProjectIdLintTests(unittest.TestCase):
    def test_safe_uuid_passes_against_fake_digest(self):
        self.assertEqual(
            lint.scan_text("x.txt", f"project = {FAKE_SAFE}\n", banned_digest=FAKE_BANNED_DIGEST),
            [],
        )

    def test_matching_digest_is_flagged(self):
        found = lint.scan_text(
            "bad.py", f'X = "{FAKE_BANNED}"\n', banned_digest=FAKE_BANNED_DIGEST
        )
        self.assertEqual(len(found), 1)
        self.assertIn("INFISICAL_FLEET_OPS_PROJECT_ID", found[0].message)

    def test_scan_tree_in_temp_dir_with_custom_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok.txt").write_text(FAKE_SAFE + "\n", encoding="utf-8")
            (root / "leak.py").write_text(f'id = "{FAKE_BANNED}"\n', encoding="utf-8")
            findings = lint.scan_tree(root, banned_digest=FAKE_BANNED_DIGEST)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].path, "leak.py")

    def test_repo_clean_under_default_digest(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
