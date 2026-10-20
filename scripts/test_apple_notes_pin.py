"""AFC-431:  apple-notes-coding.sh must not leave a coding note unpinned because a Shortcut run raced the
note index, and it must not report a failed run as a missing Shortcut.

The pin block has three distinct outcomes and one that is not an outcome at all:

  rc 0  the note is pinned
  rc 1  the Shortcut ran but did not complete
  rc 2  the Shortcut is not installed
  rc 3  pinning was disabled by APPLE_NOTES_PIN=0, so no run was attempted

The script used to collapse rc 1 and rc 2 into one message that always named the Shortcut as unavailable,
and it ran the Shortcut exactly once, so a timing race left the note unpinned.

These tests drive the real source:  the `_run_note_shortcut` function and the pin decision block are cut out
of scripts/apple-notes-coding.sh and run in a harness with a stub `shortcuts` on PATH, so the assertions
cover the shipped text and not a copy of it.  Nothing here touches Notes, and it runs anywhere bash and
python3 exist.

Run:  cd scripts && python3 -m unittest test_apple_notes_pin
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "apple-notes-coding.sh"

#  the stub records every `shortcuts run` in this file, so a test can count the attempts
STUB = """#!/bin/bash
if [ "${1:-}" = "list" ]; then
  if [ "${STUB_INSTALLED:-1}" = "1" ]; then printf '%s\\n' "Pin Coding Note"; fi
  exit 0
fi
n=0
if [ -f "$STUB_COUNTER" ]; then n=$(cat "$STUB_COUNTER"); fi
n=$((n+1))
printf '%s' "$n" > "$STUB_COUNTER"
if [ "${STUB_FAIL_FIRST:-0}" -gt 0 ] && [ "$n" -le "${STUB_FAIL_FIRST:-0}" ]; then exit 1; fi
exit 0
"""

HARNESS = """set -uo pipefail
TITLE="AFC test note"
MODE="update"
WANT_PIN="${HARNESS_WANT_PIN:-0}"
APPLE_NOTES_PIN="${HARNESS_APPLE_NOTES_PIN:-1}"
#  the real block waits 2 s for the note index and 3 s between attempts;  the test only needs the shape
sleep() { :; }
{function}
{block}
"""


def _extract(source: str) -> tuple[str, str]:
    """Cut the Shortcut runner and the pin decision block out of the real script.

    The anchors are pre-existing lines, so the same extraction works against the script before the
    AFC-431 fix and the tests fail there on behaviour rather than on a missing marker.
    """
    fn = re.search(r"^_run_note_shortcut\(\) \{[^\n]*\n(?:.*\n)*?^\}\n", source, re.M)
    assert fn, "the _run_note_shortcut function is gone from apple-notes-coding.sh"
    start = source.index("# Set APPLE_NOTES_PIN=0 to skip pinning entirely.")
    want = source.index('if [[ "$WANT_PIN" != "1" ]]; then')
    end = source.index("\nfi\n", want) + len("\nfi\n")
    return fn.group(0), source[start:end]


@unittest.skipUnless(SCRIPT.is_file(), "needs scripts/apple-notes-coding.sh")
class PinDecisionTests(unittest.TestCase):
    def run_block(self, **env: str) -> tuple[subprocess.CompletedProcess, int]:
        """Run the real pin block with a stub `shortcuts`, and return the result and the run count."""
        source = SCRIPT.read_text()
        function, block = _extract(source)
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            stub = bindir / "shortcuts"
            stub.write_text(STUB)
            stub.chmod(0o755)
            counter = Path(tmp) / "counter"
            script = Path(tmp) / "harness.sh"
            #  a plain replace, because the harness carries shell ${...} braces that str.format would eat
            script.write_text(
                HARNESS.replace("{function}", function).replace("{block}", block)
            )
            child = dict(os.environ)
            child["PATH"] = f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}"
            child["STUB_COUNTER"] = str(counter)
            child.update(env)
            p = subprocess.run(
                ["bash", str(script)],
                capture_output=True,
                text=True,
                env=child,
                timeout=60,
                check=False,
            )
            runs = int(counter.read_text()) if counter.exists() else 0
            return p, runs

    def test_a_working_shortcut_pins_the_note_on_the_first_try(self):
        p, runs = self.run_block()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("pinned: yes", p.stdout)
        self.assertEqual(runs, 1)

    def test_a_failed_first_run_is_retried_and_the_note_ends_up_pinned(self):
        """The race that left a note unpinned:  the first run misses the note index and the second finds it."""
        p, runs = self.run_block(STUB_FAIL_FIRST="1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("pinned: yes", p.stdout)
        self.assertEqual(runs, 2, "the failed run was not retried")

    def test_a_run_that_keeps_failing_is_named_as_a_run_failure(self):
        p, runs = self.run_block(STUB_FAIL_FIRST="99")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("shortcut run failed", p.stdout)
        self.assertNotIn("is not installed", p.stdout)
        self.assertIn("ran but did not complete", p.stderr)
        self.assertEqual(runs, 2)

    def test_a_missing_shortcut_is_named_as_a_missing_shortcut(self):
        p, runs = self.run_block(STUB_INSTALLED="0")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("is not installed", p.stdout)
        self.assertNotIn("run failed", p.stdout)
        self.assertIn("is not installed", p.stderr)
        self.assertEqual(runs, 0, "the Shortcut ran even though it is not installed")

    def test_disabling_pinning_is_not_reported_as_a_failure(self):
        """APPLE_NOTES_PIN=0 skips pinning on purpose, so no run was attempted and nothing failed."""
        p, runs = self.run_block(HARNESS_APPLE_NOTES_PIN="0")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("pinning is disabled", p.stdout)
        self.assertNotIn("run failed", p.stdout)
        self.assertNotIn("is not installed", p.stdout)
        self.assertEqual(runs, 0, "the Shortcut ran even though pinning was disabled")

    def test_the_gui_fallback_is_still_reachable_after_a_failed_run(self):
        """--pin still has to reach the GUI path when the Shortcut fails, so the skip line must not fire."""
        p, _ = self.run_block(HARNESS_WANT_PIN="1", STUB_FAIL_FIRST="99")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("pinned: skipped", p.stdout)
        self.assertIn("ran but did not complete", p.stderr)


if __name__ == "__main__":
    unittest.main()
