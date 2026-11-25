"""The delete path survives a body AppleEvents cannot carry back (AFC-905).

A note the fleet writer wrote can grow past the size AppleEvents will carry out of Notes.
The body then fails to read while the plaintext still reads, and the delete fetch died with
the body, so the one note the tool could not delete was the one it had made too large.

These tests are about the script's own JXA and the delete guard it calls.  No note is
deleted and no osascript runs for real:  the JXA is read as text, and the guard is run
against inputs the test builds.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2]
SCRIPT = SCRIPTS / "apple-notes-coding.sh"
GUARD = SCRIPTS / "notes_delete_guard.py"

TITLE = "[AFC, MM] Fleet-Secrets Retirement (superseded, body not readable)"
# The shape the fleet writer leaves:  a title row, a timestamp row and a session row.
PLAINTEXT = (
    "[AFC, MM] Fleet-Secrets Retirement\n"
    "Mon, Nov 23, 2:28 AM\n"
    "Session: mvs_143c3a57ab6844f99038418e4a03eb5c\n"
    "\n\nProblem\n\nThe body is long enough that AppleEvents will not carry it back.\n"
)


def jxa_source() -> str:
    """The JXA the script runs for a note read."""
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"osascript -l JavaScript <<'JXA'\n(.*?)\nJXA\n", text, re.S)
    assert match, "the script no longer runs its JXA from a heredoc"
    return match.group(1)


def run_guard(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=30,
    )


class DeleteFetchTests(unittest.TestCase):
    """The delete fetch reads what it can;  it never invents a body it could not read."""

    def test_the_fetch_survives_a_body_that_cannot_be_read(self) -> None:
        source = jxa_source()
        self.assertIn("try {", source)
        self.assertIn("body = target.body();", source)
        self.assertIn("plaintext = target.plaintext();", source)
        # A body that fails to read is an empty body,  never a fabricated one.
        self.assertIn("body = '';", source)
        self.assertIn("plaintext = '';", source)

    def test_the_fetch_reads_the_plaintext_even_when_the_body_is_empty(self) -> None:
        source = jxa_source()
        self.assertIn("body: body,", source)
        self.assertIn("plaintext: plaintext", source)

    def test_the_guard_accepts_a_tool_written_note_on_its_plaintext_alone(self) -> None:
        result = run_guard({"title": TITLE, "body": "", "plaintext": PLAINTEXT})
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_guard_refuses_when_neither_body_nor_plaintext_is_readable(self) -> None:
        result = run_guard({"title": TITLE, "body": "", "plaintext": ""})
        self.assertEqual(result.returncode, 5, result.stderr)

    def test_the_guard_refuses_a_note_with_no_fleet_evidence(self) -> None:
        for payload in (
            {"title": TITLE, "body": "", "plaintext": "a person typed this by hand"},
            {"title": "Hand Made", "body": "", "plaintext": PLAINTEXT},
        ):
            with self.subTest(payload=payload):
                result = run_guard(payload)
                self.assertEqual(result.returncode, 5, result.stderr)


if __name__ == "__main__":
    unittest.main()
