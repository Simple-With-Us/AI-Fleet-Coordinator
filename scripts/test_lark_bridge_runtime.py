"""test_lark_bridge_runtime.py -- the Lark bridge finds the fleet runtime (AFC-490).

The bridge imports `simple_tools.secretscan` from the managed runtime.  These tests pin where it
looks:  its own scripts folder first, then simple-tools-runtime.

Run:  cd scripts && python3 -m unittest test_lark_bridge_runtime -v
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
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
            os.makedirs(os.path.join(home, FLEET, name, "scripts", "simple_tools"))
        return home

    def test_the_runtime_is_found(self):
        home = self.home_with("simple-tools-runtime")
        found = bridge._runtime_scripts_dir(home=home, here="/nonexistent")
        self.assertEqual(found, os.path.join(home, FLEET, "simple-tools-runtime", "scripts"))

    def test_a_checkout_uses_its_own_scripts_folder_first(self):
        home = self.home_with("simple-tools-runtime")
        found = bridge._runtime_scripts_dir(home=home, here=str(SCRIPT.parent))
        self.assertEqual(found, str(SCRIPT.parent))

    def test_no_runtime_is_none_not_a_guess(self):
        home = self.home_with()
        self.assertIsNone(bridge._runtime_scripts_dir(home=home, here="/nonexistent"))

    def test_with_no_runtime_the_bridge_exits_with_a_clear_message(self):
        # Kody, PR 1455:  fail closed, naming where it looked, rather than a bare traceback.
        # A copy of the script outside the checkout, with a home that holds no runtime.
        home = self.home_with()
        copy = Path(home) / "lark-mm-bridge.py"
        shutil.copyfile(SCRIPT, copy)
        result = subprocess.run(
            [sys.executable, "-I", str(copy), "--help"],
            env={"HOME": home, "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("simple_tools.secretscan not found", result.stderr)
        self.assertIn("simple-tools-runtime", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
