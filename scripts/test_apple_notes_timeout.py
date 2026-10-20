"""AFC-436:  the canonical helper must carry the 900 s AppleEvent timeout around its Notes calls.

A large body takes longer than AppleScript's default two-minute AppleEvent timeout, and the
write fails with "AppleEvent timed out. (-1712)".  The master list is a quarter of a megabyte
of markdown, and it timed out on Oct 20, 2026, before the timeout was added to the live helper
at ~/apps/apple-notes-coding.sh.

That fix was never pushed to this repository, so the canonical copy lacked it and any install
from the repository would have dropped it again.  These tests pin the timeout to the canonical
script, so a later edit that drops it fails here instead of failing on a 183 KB note write.

Run:  python3 -m unittest scripts/test_apple_notes_timeout.py  (or cd scripts && python3 -m unittest test_apple_notes_timeout)
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "apple-notes-coding.sh"
TIMEOUT_S = 900


@unittest.skipUnless(SCRIPT.is_file(), "needs the script")
class AppleEventTimeoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.text = SCRIPT.read_text()

    def test_the_timeout_is_present_twice(self) -> None:
        self.assertEqual(self.text.count("with timeout of 900 seconds"), 2)
        self.assertEqual(self.text.count("\nend timeout\n"), 2)

    def test_each_timeout_wraps_a_notes_call(self) -> None:
        #  The timeout must wrap the "tell application \"Notes\"" block,  not sit next to it.
        pattern = re.compile(
            r"with timeout of 900 seconds\ntell application \"Notes\"\n(?:.*\n)*?end tell\nend timeout\n"
        )
        self.assertEqual(len(pattern.findall(self.text)), 2)

    def test_the_timeout_is_not_wrapped_in_another_timeout(self) -> None:
        #  A nested "with timeout" would reset the outer one when the inner block closes.
        self.assertNotIn("with timeout of 900 seconds\nwith timeout", self.text)

    def test_the_reason_is_recorded_next_to_the_first_timeout(self) -> None:
        #  The comment is the evidence for the 900 s value;  a bare number would be a guess.
        self.assertIn("AppleEvent timed out. (-1712)", self.text)
        self.assertIn(f"with timeout of {TIMEOUT_S} seconds", self.text)

    def test_the_create_and_update_blocks_both_carry_it(self) -> None:
        #  The update block writes the system attribute path,  the create block the $TMP path.
        self.assertIn('set htmlPath to POSIX file (system attribute "TMP")', self.text)
        self.assertIn('set htmlPath to POSIX file "$TMP"', self.text)


if __name__ == "__main__":
    unittest.main()
