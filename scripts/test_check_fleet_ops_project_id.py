#!/usr/bin/env python3
"""Tests for scripts/check-fleet-ops-project-id.py."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "check-fleet-ops-project-id.py"
FAKE_OPS = "00000000-0000-4000-8000-000000000099"
BANNED = "8dae6d03-8f12-48b2-adf3-e5c1b881c8c5"


def _load():
    loader = importlib.machinery.SourceFileLoader("check_fleet_ops_project_id", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = mod
    loader.exec_module(mod)
    return mod


lint = _load()


class FleetOpsProjectIdLintTests(unittest.TestCase):
    def test_fake_uuid_passes(self):
        self.assertEqual(lint.scan_text("x.txt", f"project = {FAKE_OPS}\n"), [])

    def test_banned_uuid_is_flagged(self):
        found = lint.scan_text("bad.py", f'X = "{BANNED}"\n')
        self.assertEqual(len(found), 1)
        self.assertIn("INFISICAL_FLEET_OPS_PROJECT_ID", found[0].message)

    def test_scan_tree_in_temp_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok.txt").write_text(FAKE_OPS + "\n", encoding="utf-8")
            (root / "leak.py").write_text('id = "' + BANNED + '"\n', encoding="utf-8")
            findings = lint.scan_tree(root)
            self.assertEqual(len(findings), 1)
            self.assertEqual(findings[0].path, "leak.py")


if __name__ == "__main__":
    unittest.main()
