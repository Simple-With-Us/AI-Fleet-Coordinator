"""Build metadata must fail closed before tools, processes or app bundles are touched."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name("build_and_run.sh")


class BuildNumberTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        (self.bin / "dirname").symlink_to(shutil.which("dirname"))

    def run_script(self, value):
        env = {"PATH": str(self.bin), "HOME": self.tmp.name}
        if value is not None:
            env["MUSTER_BUILD_NUMBER"] = value
        return subprocess.run(["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True)

    def test_missing_or_malformed_build_refuses_before_tools_or_installation(self):
        for value in (None, "", "1001", "2026110312000", "20261103abcd", "202611031200\n"):
            with self.subTest(value=value):
                result = self.run_script(value)
                self.assertEqual(result.returncode, 2)
                self.assertIn("12-digit merge-time build number", result.stderr)
                self.assertNotIn("Required command", result.stderr)

    def test_valid_build_reaches_normal_capability_preflight(self):
        result = self.run_script("202611031200")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Required command is unavailable: xcodebuild", result.stderr)


if __name__ == "__main__":
    unittest.main()
