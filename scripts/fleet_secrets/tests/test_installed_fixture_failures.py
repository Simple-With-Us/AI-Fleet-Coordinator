"""Classify actual child timeout and missing-executable fixture failures."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

from fleet_secrets.tests.installed_writer_fixture import run_fixture


class InstalledFixtureFailureTests(unittest.TestCase):
    def test_timeout_discards_partial_output_and_reports_124(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_fixture(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    "print('partial', flush=True); import time; time.sleep(5)",
                ],
                cwd=tmp,
                env={"PATH": os.defpath},
                timeout=0.05,
            )
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr), (124, "", "fixture-timeout")
        )

    def test_missing_executable_reports_127(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = run_fixture(
                [str(Path(tmp) / "absent-fixture")], cwd=tmp, env={"PATH": os.defpath}
            )
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr), (127, "", "fixture-executable-missing")
        )


if __name__ == "__main__":
    unittest.main()
