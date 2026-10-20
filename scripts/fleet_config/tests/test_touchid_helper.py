"""The Touch ID helper's build script.

The script takes one argument,  the build configuration,  and forwards it to
``xcodebuild``.  A value that looks like an option would be read by ``xcodebuild`` as an
option,  so the script checks the value against the two it documents before it forwards
anything.

The test runs the script with an option-like value and with the tool it would call
pointed at a path that does not exist,  so the script cannot generate a project, cannot
build anything and cannot touch the source tree.
"""

from __future__ import annotations

import os
import subprocess
import unittest

HELPER_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "touchid_helper"
)
BUILD_SCRIPT = os.path.join(HELPER_DIR, "build.sh")


class BuildScriptTests(unittest.TestCase):
    """The configuration is one of the two documented names,  never an option."""

    def run_script(self, argument: str):
        """Run the build script with `argument` and answer (returncode, stderr)."""
        env = dict(os.environ)
        # The tool the script would call is pointed at a path that does not exist,  so
        # the script can never generate a project or build one.
        env["XCODEGEN"] = os.path.join("/nonexistent", "xcodegen")
        completed = subprocess.run(
            ["/bin/sh", BUILD_SCRIPT, argument],
            capture_output=True,
            env=env,
            check=False,
            timeout=60,
        )
        return completed.returncode, completed.stderr.decode("utf-8", "replace")

    def test_an_option_like_configuration_is_refused(self):
        returncode, stderr = self.run_script("--help")
        self.assertNotEqual(returncode, 0)
        self.assertIn("Release", stderr)
        self.assertIn("Debug", stderr)
        self.assertNotIn("xcodegen", stderr, "the script went on to the tool check")

    def test_another_option_like_configuration_is_refused(self):
        returncode, stderr = self.run_script("-configuration")
        self.assertNotEqual(returncode, 0)
        self.assertIn("Release", stderr)
        self.assertIn("Debug", stderr)

    def test_a_configuration_that_is_not_a_name_is_refused(self):
        returncode, stderr = self.run_script("Production")
        self.assertNotEqual(returncode, 0)
        self.assertIn("Release", stderr)
        self.assertIn("Debug", stderr)

    def test_the_documented_configurations_pass_the_check(self):
        # The check passes,  so the script goes on to the tool check,  which fails
        # because the tool is not there.  That is the proof the value was accepted.
        for argument in ("Release", "Debug"):
            with self.subTest(argument=argument):
                returncode, stderr = self.run_script(argument)
                self.assertNotEqual(returncode, 0)
                self.assertNotIn("must be Release or Debug", stderr)
