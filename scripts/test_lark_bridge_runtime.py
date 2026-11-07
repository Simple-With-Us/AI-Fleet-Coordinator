"""test_lark_bridge_runtime.py -- the Lark bridge finds the fleet runtime (AFC-490).

The managed runtime was renamed from agent-sync-runtime to simple-tools-runtime on Nov 2, 2026,
and an installed bridge that only knew the old name crash-looped on `ModuleNotFoundError:
agent_sync`.  These tests pin the order the bridge looks in:  its own scripts folder, then
simple-tools-runtime, then the old name.

Run:  cd scripts && python3 -m unittest test_lark_bridge_runtime -v
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "lark-mm-bridge.py"
_spec = importlib.util.spec_from_file_location("lark_mm_bridge_runtime_probe", SCRIPT)
bridge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bridge)

FLEET = "apps/lanes/_managed/fleet"


class RuntimePathTests(unittest.TestCase):
    def home_with(self, *runtimes: str) -> str:
        home = tempfile.mkdtemp(prefix="lark-runtime-home-")
        self.addCleanup(shutil.rmtree, home, True)
        for name in runtimes:
            os.makedirs(os.path.join(home, FLEET, name, "scripts", "agent_sync"))
        return home

    def test_the_renamed_runtime_is_found(self):
        home = self.home_with("simple-tools-runtime")
        found = bridge._runtime_scripts_dir(home=home, here="/nonexistent")
        self.assertEqual(found, os.path.join(home, FLEET, "simple-tools-runtime", "scripts"))

    def test_the_renamed_runtime_wins_over_the_old_name(self):
        home = self.home_with("simple-tools-runtime", "agent-sync-runtime")
        found = bridge._runtime_scripts_dir(home=home, here="/nonexistent")
        self.assertEqual(found, os.path.join(home, FLEET, "simple-tools-runtime", "scripts"))

    def test_the_old_name_still_works_where_it_is_the_only_one(self):
        home = self.home_with("agent-sync-runtime")
        found = bridge._runtime_scripts_dir(home=home, here="/nonexistent")
        self.assertEqual(found, os.path.join(home, FLEET, "agent-sync-runtime", "scripts"))

    def test_a_checkout_uses_its_own_scripts_folder_first(self):
        home = self.home_with("simple-tools-runtime")
        found = bridge._runtime_scripts_dir(home=home, here=str(SCRIPT.parent))
        self.assertEqual(found, str(SCRIPT.parent))

    def test_no_runtime_is_none_not_a_guess(self):
        home = self.home_with()
        self.assertIsNone(bridge._runtime_scripts_dir(home=home, here="/nonexistent"))


if __name__ == "__main__":
    unittest.main()
