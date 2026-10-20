"""AFC-436:  the canonical helper must carry the 900 s AppleEvent timeout around its Notes calls.

A large body takes longer than AppleScript's default two-minute AppleEvent timeout, and the
write fails with "AppleEvent timed out. (-1712)".  The master list is a quarter of a megabyte
of markdown, and it timed out on Oct 20, 2026, before the timeout was added to the live helper
at ~/apps/apple-notes-coding.sh.

That fix was never pushed to this repository, so the canonical copy lacked it and any install
from the repository would have dropped it again.  These tests pin the timeout to the canonical
script, so a later edit that drops it fails here instead of failing on a 183 KB note write.

The timeout lives in one place, the NOTES_APPLE_EVENT_TIMEOUT_S constant, and a caller that
runs the helper must allow more than it:  a cap below the timeout kills the write while
osascript is still working, and the later completion creates a duplicate note.  The last test
holds that relationship.

Run:  python3 -m unittest scripts/test_apple_notes_timeout.py  (or cd scripts && python3 -m unittest test_apple_notes_timeout)
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SCRIPT = SCRIPTS / "apple-notes-coding.sh"
TIMEOUT_DEFAULT_S = 900
#  the name the AppleScript blocks use,  so the two of them cannot drift apart
TIMEOUT_NAME = "NOTES_APPLE_EVENT_TIMEOUT_S"


class AppleEventTimeoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        #  No class-level skip:  a missing or renamed canonical helper has to fail the suite,  not
        #  quietly skip it and leave the timeout unpinned.  A skip here is exactly the failure this
        #  file exists to prevent.
        if not SCRIPT.is_file():
            raise AssertionError(f"the canonical helper is missing: {SCRIPT}")
        cls.text = SCRIPT.read_text()

    def test_the_timeout_is_defined_once_with_its_default(self) -> None:
        pattern = re.compile(rf'^{TIMEOUT_NAME}="\$\{{{TIMEOUT_NAME}:-(\d+)\}}"$', re.MULTILINE)
        found = pattern.findall(self.text)
        self.assertEqual(len(found), 1, f"the timeout constant is not defined exactly once: {found}")
        self.assertEqual(int(found[0]), TIMEOUT_DEFAULT_S)

    def test_both_notes_blocks_use_the_constant(self) -> None:
        self.assertEqual(self.text.count(f"with timeout of ${{{TIMEOUT_NAME}}} seconds"), 2)
        self.assertEqual(self.text.count("end timeout"), 2)
        self.assertNotIn("with timeout of 900 seconds", self.text)

    def test_each_timeout_wraps_a_notes_call(self) -> None:
        #  The timeout must wrap the "tell application \"Notes\"" block,  not sit next to it.
        pattern = re.compile(
            r"with timeout of \$\{[A-Z_]+\} seconds\ntell application \"Notes\"\n"
            r"(?:.*\n)*?end tell\nend timeout\n"
        )
        self.assertEqual(len(pattern.findall(self.text)), 2)

    def test_the_timeout_is_not_wrapped_in_another_timeout(self) -> None:
        #  A nested "with timeout" would reset the outer one when the inner block closes.
        self.assertNotIn("seconds\nwith timeout", self.text)

    def test_the_reason_is_recorded_next_to_the_constant(self) -> None:
        #  The comment is the evidence for the 900 s value;  a bare number would be a guess.
        self.assertIn("AppleEvent timed out. (-1712)", self.text)
        self.assertIn(f"with timeout of {TIMEOUT_NAME} seconds", self.text)

    def test_the_create_and_update_blocks_both_carry_it(self) -> None:
        #  The update block writes the system attribute path,  the create block the $TMP path.
        self.assertIn('set htmlPath to POSIX file (system attribute "TMP")', self.text)
        self.assertIn('set htmlPath to POSIX file "$TMP"', self.text)

    def test_the_caller_allows_more_than_the_script_timeout(self) -> None:
        """A cap below the timeout kills the write while osascript is still working.

        The script may then finish the note anyway,  so a retry writes a second one.  The
        caller's cap has to sit above the script's own timeout,  and this test is what keeps
        the two from drifting apart.
        """
        sys.path.insert(0, str(SCRIPTS))
        from agent_sync import notes

        self.assertGreater(
            notes.SCRIPT_TIMEOUT_S,
            TIMEOUT_DEFAULT_S,
            "the caller's cap is at or below the script's AppleEvent timeout,  so a large write "
            "is killed while osascript is still running",
        )


if __name__ == "__main__":
    unittest.main()
