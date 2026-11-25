"""The delete path survives a body AppleEvents cannot carry back (AFC-905).

A note the fleet writer wrote can grow past the size AppleEvents will carry out of Notes.
The body then fails to read while the plaintext still reads, and the delete fetch died with
the body, so the one note the tool could not delete was the one it had made too large.

A note can also be one Notes itself refuses:  every body read and every body write fails
with AppleEvent -10000 and the delete fails the same way, so no route through AppleEvents
can remove it.  The script names that refusal in words, so the reader is not left with a
bare exit code and no cause.

These tests are about the script's own JXA and the delete guard it calls.  No note is
deleted and no osascript runs for real:  the JXA is read as text, and the guard is run
against inputs the test builds.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
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


def refusal_reporter() -> str:
    """The shell function that names the Notes-side refusal."""
    text = SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"_notes_report_refusal\(\) \{\n.*?\n\}\n", text, re.S)
    assert match, "the script no longer has the refusal reporter"
    return match.group(0)


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


class RefusalReportTests(unittest.TestCase):
    """A note Notes itself refuses is named,  not reported as a bare exit code."""

    def run_reporter(self, error_text: str) -> subprocess.CompletedProcess:
        body = refusal_reporter()
        with tempfile.TemporaryDirectory() as directory:
            error_file = Path(directory) / "error.txt"
            error_file.write_text(error_text, encoding="utf-8")
            return subprocess.run(
                ["bash", "-c", body + '\n_notes_report_refusal "$1"', "bash", str(error_file)],
                capture_output=True,
                text=True,
                timeout=30,
            )

    def test_a_notes_refusal_is_named(self) -> None:
        result = self.run_reporter("execution error: AppleEvent handler failed. (-10000)\n")
        self.assertIn("NOTES-REFUSED-DELETE", result.stderr)
        self.assertIn("only the Notes app can remove a note in this state", result.stderr)

    def test_another_failure_is_not_reported_as_a_notes_refusal(self) -> None:
        result = self.run_reporter("notes: something else went wrong\n")
        self.assertNotIn("NOTES-REFUSED-DELETE", result.stderr)

    def test_both_delete_stages_report_the_refusal(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        # The fetch stage and the delete stage each capture the AppleEvent error and name it.
        self.assertEqual(text.count('_notes_report_refusal "$_NOTES_ERR"'), 2)
        self.assertEqual(text.count('2>"$_NOTES_ERR"'), 2)


class GuardResolutionTests(unittest.TestCase):
    """The live copy reaches the guard in the managed runtime, where the package lives.

    The guard imports simple_tools.notes from its own directory, so it has to sit next to
    the package.  The tracked copy lives beside simple_tools/, so its sibling guard is the
    right one;  the live copy at ~/apps/ has no package beside it, so it has to reach into
    the managed runtime.
    """

    def test_the_sibling_guard_is_tried_first(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        sibling = '"${_SCRIPTS_ROOT}/notes_delete_guard.py"'
        runtime = "simple-tools-runtime}/scripts/notes_delete_guard.py"
        self.assertIn(sibling, text)
        self.assertIn(runtime, text)
        # The sibling comes first, so the tracked copy always tests its own guard.
        self.assertLess(text.index(sibling), text.index(runtime))

    def test_the_managed_runtime_is_the_fallback(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn(
            "${SIMPLE_TOOLS_RUNTIME:-$HOME/apps/lanes/_managed/fleet/simple-tools-runtime}"
            "/scripts/notes_delete_guard.py",
            text,
        )

    def test_a_missing_guard_is_named_rather_than_left_silent(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("delete guard script missing", text)


if __name__ == "__main__":
    unittest.main()
