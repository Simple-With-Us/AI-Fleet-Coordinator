#!/usr/bin/env python3
"""The Apple Notes helper reports a secret it could not read as a failed read.

`apple-notes-coding.sh` reads a notification key through the shared scoped reader.
The helper must not report success when the read failed,  so a caller can tell a
key it has from a key it could not fetch.  The caller decides what a missing
notification key means:  the note itself never depends on one,  and the macOS
notification carries the owner instead.

The two functions are read out of the shipped script and run as they are,  so the
test covers the code that ships,  not a copy of it.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "apple-notes-coding.sh"

# A reader that answers with a value,  or refuses.  It is run by python,  the
# way the shipped helper runs it.
FAKE_READER = """#!/usr/bin/env python3
import os, sys
if os.environ.get("FAKE_READER_EXIT") == "0":
    sys.stdout.write(os.environ.get("FAKE_READER_VALUE", ""))
    sys.exit(0)
sys.stderr.write("infisical-scoped:  the scoped token store refused the read\\n")
sys.exit(int(os.environ.get("FAKE_READER_EXIT", "4")))
"""


def function_source(name: str) -> str:
    """One shell function out of the shipped script,  verbatim."""
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"^%s\(\) \{\n.*?^\}\n" % re.escape(name), text, re.DOTALL | re.MULTILINE)
    if not match:
        raise AssertionError("%s is not in %s" % (name, SCRIPT.name))
    return match.group(0)


class PushoverSecretTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="apple-notes-pushover-")
        self.addCleanup(self.tmp.cleanup)
        self.reader = Path(self.tmp.name) / "infisical-scoped.py"
        self.reader.write_text(FAKE_READER, encoding="utf-8")
        self.reader.chmod(0o755)

    def run_helper(self, *args: str, **env_overrides: str):
        """The shipped helper,  with the shipped reader beside it."""
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": self.tmp.name,
            "INFISICAL_SCOPED_READER": str(self.reader),
            "FAKE_READER_VALUE": "a-value-the-reader-answered",
        }
        env.update(env_overrides)
        script = function_source("_scoped_secret") + "\n" + function_source("_pushover_secret")
        return subprocess.run(
            ["bash", "-c", script + '\n_pushover_secret "$@"', "pushover-secret", *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    def test_a_read_that_answers_prints_the_value_and_succeeds(self) -> None:
        result = self.run_helper("PUSHOVER_USER_KEY", FAKE_READER_EXIT="0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "a-value-the-reader-answered")

    def test_a_refused_read_fails_and_names_the_reader_the_account_and_the_folder(self) -> None:
        result = self.run_helper("PUSHOVER_USER_KEY", FAKE_READER_EXIT="4")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("PUSHOVER_USER_KEY", result.stderr)
        self.assertIn("apple-notes-ro", result.stderr)
        self.assertIn("/global-api-keys", result.stderr)
        self.assertIn(str(self.reader), result.stderr)

    def test_a_missing_reader_fails(self) -> None:
        result = self.run_helper(
            "PUSHOVER_USER_KEY", INFISICAL_SCOPED_READER="/nonexistent/infisical-scoped.py"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("PUSHOVER_USER_KEY", result.stderr)

    def test_a_hand_set_environment_value_does_not_replace_a_refused_scoped_read(self) -> None:
        result = self.run_helper(
            "PUSHOVER_USER_KEY",
            PUSHOVER_USER_KEY="the-bound-value",
            FAKE_READER_EXIT="4",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("PUSHOVER_USER_KEY", result.stderr)
        self.assertIn("apple-notes-ro", result.stderr)
        self.assertIn("/global-api-keys", result.stderr)

    def test_a_scoped_read_wins_over_a_hand_set_environment_value(self) -> None:
        result = self.run_helper(
            "PUSHOVER_USER_KEY",
            PUSHOVER_USER_KEY="the-bound-value",
            FAKE_READER_EXIT="0",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "a-value-the-reader-answered")


if __name__ == "__main__":
    unittest.main()
