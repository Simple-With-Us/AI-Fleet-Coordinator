"""The shared command must not import a hostile current-directory reader."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class IsolatedCommandTests(unittest.TestCase):
    def test_absolute_isolated_entry_ignores_hostile_cwd_and_pythonpath(self):
        entry = Path(__file__).resolve().parents[2] / "fleet_infisical_cli.py"
        with tempfile.TemporaryDirectory() as directory:
            hostile = Path(directory)
            for name in ("fleet_infisical_cli.py", "fleet_infisical.py"):
                (hostile / name).write_text("raise AssertionError('foreign reader executed')\n")
            result = subprocess.run(
                [sys.executable, "-I", str(entry), "--help"],
                cwd=hostile,
                env={
                    "HOME": directory,
                    "PATH": os.defpath,
                    "PYTHONPATH": directory,
                    "INFISICAL_TOKEN": "synthetic-fixture",
                },
                capture_output=True,
                text=True,
                timeout=5,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("names", result.stdout)
        self.assertNotIn("foreign reader", result.stdout + result.stderr)
        self.assertNotIn("synthetic-fixture", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
