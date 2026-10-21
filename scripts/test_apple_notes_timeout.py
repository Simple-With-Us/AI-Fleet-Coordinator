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
osascript is still working, and the later completion creates a duplicate note.  The caller must
also allow for the pin phase that follows the write,  whose Shortcut runs are bounded by
NOTES_SHORTCUT_TIMEOUT_S:  a cap that cuts that phase short reports outcome_unknown for a note
that was written.  The wait for the run lock is counted inside the cap as well,  so a request
that waits the lock out and then runs does not outlive it.  The last test holds that
relationship.

Run:  python3 -m unittest scripts/test_apple_notes_timeout.py  (or cd scripts && python3 -m unittest test_apple_notes_timeout)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SCRIPT = SCRIPTS / "apple-notes-coding.sh"
TIMEOUT_DEFAULT_S = 900
#  the name the AppleScript blocks use,  so the two of them cannot drift apart
TIMEOUT_NAME = "NOTES_APPLE_EVENT_TIMEOUT_S"
#  the bound one Shortcuts invocation runs under (NOTES_SHORTCUT_TIMEOUT_S in the helper)
SHORTCUT_TIMEOUT_NAME = "NOTES_SHORTCUT_TIMEOUT_S"
SHORTCUT_TIMEOUT_DEFAULT_S = 30
#  the pin phase that follows the write:  a 2 s settle for the note index,  up to 5 s for the
#  Shortcuts app to start,  one bounded list lookup,  and up to two bounded runs with a 3 s pause
#  between them
PIN_BUDGET_S = 2 + 5 + 3 * SHORTCUT_TIMEOUT_DEFAULT_S + 3


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
        self.assertEqual(
            len(found), 1, f"the timeout constant is not defined exactly once: {found}"
        )
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
        #  It has to sit with the constant it explains:  a reason recorded somewhere else in the
        #  file says nothing about this number.
        self.assertIn(f"with timeout of {TIMEOUT_NAME} seconds", self.text)
        constant = self.text.index(f'{TIMEOUT_NAME}="${{{TIMEOUT_NAME}:-')
        #  the comment block directly above the constant,  and the constant itself
        lines = self.text[:constant].split("\n")
        index = len(lines) - 1
        while index >= 0 and not lines[index].strip():
            index -= 1
        while index >= 0 and lines[index].startswith("#"):
            index -= 1
        window = "\n".join(lines[index + 1 :]) + self.text[constant : constant + 200]
        self.assertIn("AppleEvent timed out. (-1712)", window)

    def test_a_malformed_timeout_override_falls_back_to_the_default(self) -> None:
        """The value goes into the AppleScript source,  so a malformed one must not reach it.

        osascript rejects a program that is not valid AppleScript,  and the helper runs under
        "set -euo pipefail",  so a typo ("900s") aborts the write with an opaque syntax error
        instead of a message about the override.
        """
        lines = self.text.split("\n")
        start = next(
            i
            for i, line in enumerate(lines)
            if line.startswith(f'{TIMEOUT_NAME}="${{{TIMEOUT_NAME}:-')
        )
        check = next(
            (i for i in range(start + 1, start + 12) if "=~" in lines[i] and TIMEOUT_NAME in lines[i]),
            None,
        )
        self.assertIsNotNone(check, "the override is not checked for being a number of seconds")
        snippet = (
            "\n".join(lines[start : check + 1]) + f'\nprintf "%s\\n" "${TIMEOUT_NAME}"\n'
        )
        for value, expected in (("900s", 900), ("", 900), ("450", 450), ('900"; end', 900)):
            with self.subTest(value=value):
                proc = subprocess.run(
                    ["bash", "-c", snippet],
                    env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), TIMEOUT_NAME: value},
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout.strip(), str(expected))

    def test_the_shortcut_run_is_bounded(self) -> None:
        """The Shortcut can wait on a note picker on the owner's screen,  so its run has a bound.

        An unbounded run can outlast the caller's cap:  the note is written and the caller is then
        told the outcome is unknown and refuses the retry of a request that did succeed.
        """
        pattern = re.compile(
            rf'^{SHORTCUT_TIMEOUT_NAME}="\$\{{{SHORTCUT_TIMEOUT_NAME}:-(\d+)\}}"$', re.MULTILINE
        )
        found = pattern.findall(self.text)
        self.assertEqual(
            len(found), 1, f"the shortcut timeout constant is not defined exactly once: {found}"
        )
        self.assertEqual(int(found[0]), SHORTCUT_TIMEOUT_DEFAULT_S)
        #  both the list lookup and the run go through the bound,  so the budget above holds
        self.assertEqual(self.text.count("_run_shortcuts_bounded shortcuts"), 2)
        self.assertIn("alarm shift; exec @ARGV", self.text)

    def test_the_create_and_update_blocks_both_carry_it(self) -> None:
        #  The update block writes the system attribute path,  the create block the $TMP path.
        self.assertIn('set htmlPath to POSIX file (system attribute "TMP")', self.text)
        self.assertIn('set htmlPath to POSIX file "$TMP"', self.text)

    def test_the_caller_allows_more_than_the_write_and_the_pin(self) -> None:
        """The cap has to cover the whole script run,  not only the write.

        A cap above the write timeout alone still cuts a run short while the pin phase is
        running:  the note is written by then,  so the caller reports outcome_unknown and
        refuses the retry of a request that did succeed.  A cap below the write timeout kills the
        write while osascript is still working,  and the later completion creates a second note.
        The wait for the run lock is counted inside the cap as well:  the script gets the cap
        minus that wait,  so a request that waits the lock out and then runs does not outlive the
        cap.  This test is what keeps the two from drifting apart.
        """
        sys.path.insert(0, str(SCRIPTS))
        from agent_sync import notes

        self.assertGreater(
            notes.SCRIPT_TIMEOUT_S,
            TIMEOUT_DEFAULT_S + PIN_BUDGET_S + notes.LOCK_WAIT_S,
            "the caller's cap is not above the write timeout, the pin phase and the lock wait,  so "
            "a request that waits for the lock and then pins after the write can be cut short,  "
            "and the caller then refuses the retry of a note that was written",
        )
        #  and the script itself still gets more than the write and the pin phase need
        self.assertGreater(
            notes.SCRIPT_TIMEOUT_S - notes.LOCK_WAIT_S,
            TIMEOUT_DEFAULT_S + PIN_BUDGET_S,
            "the cap the script runs under is not above the write timeout plus the pin phase,  so "
            "a run that pins after the write can be cut short",
        )

    def test_the_notes_blocks_use_an_unquoted_heredoc(self) -> None:
        """The timeout constant is a shell variable,  so the heredoc has to expand it.

        In a quoted heredoc osascript receives the literal text of the constant and the
        "with timeout of" clause does not compile,  which breaks the write.  The pin block is
        the other way round:  it has no shell variable and stays quoted.
        """
        self.assertIn("osascript <<EOF\n", self.text)
        self.assertNotIn("osascript <<'EOF'\nset titlePath", self.text)
        self.assertEqual(self.text.count("osascript <<EOF"), 2)
        self.assertEqual(self.text.count("osascript <<'EOF'"), 1)

    @unittest.skipUnless(shutil.which("osacompile"), "needs osacompile")
    def test_the_update_block_still_compiles(self) -> None:
        """Compile the rendered AppleScript,  so a broken block fails here and not on a write."""
        block = re.search(r"osascript <<EOF\n(.*?)\nEOF\n", self.text, re.DOTALL)
        self.assertIsNotNone(block, "the unquoted Notes block is gone")
        with tempfile.TemporaryDirectory() as tmp:
            body = Path(tmp) / "body.html"
            body.write_text("<div>test</div>", encoding="utf-8")
            title = Path(tmp) / "title.txt"
            title.write_text("[AFC, MiniMax] compile probe", encoding="utf-8")
            rendered = block.group(1).replace("$TMP", str(body)).replace("$TITLE_FILE", str(title))
            rendered = rendered.replace("${NOTES_APPLE_EVENT_TIMEOUT_S}", str(TIMEOUT_DEFAULT_S))
            proc = subprocess.run(
                ["osacompile", "-o", os.devnull, "-e", rendered],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_the_effective_apple_script_carries_the_number(self) -> None:
        """The AppleScript osascript is given has to carry the number,  not the shell variable.

        The source says "with timeout of ${NOTES_APPLE_EVENT_TIMEOUT_S} seconds",  which only
        reaches osascript when the heredoc expands it.  In a quoted heredoc osascript is handed
        the literal text and the "with timeout of" clause does not compile,  so every update
        fails on a write while the text assertions stay green.  The compile check above only
        runs where osacompile exists,  which is nowhere in CI,  so the helper is run here with
        a stub osascript that records what it was given.
        """
        for flags, mode in (([], "create"), (["--update"], "update")):
            with self.subTest(mode=mode):
                source = self._effective_applescript(flags)
                self.assertIn("with timeout of %d seconds" % TIMEOUT_DEFAULT_S, source)
                self.assertNotIn("${", source)
                self.assertIn('tell application "Notes"', source)

    def _effective_applescript(self, flags: list[str]) -> str:
        """Run the helper with a stub osascript and return the AppleScript it was handed.

        `shortcuts`, `open` and `pgrep` are stubbed too,  so the run never touches the owner's
        Shortcuts app and never lists the shortcuts on it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            bindir.mkdir(mode=0o700)
            captured = Path(tmp) / "captured.applescript"
            stub = bindir / "osascript"
            stub.write_text('#!/bin/bash\ncat > "%s"\necho "x-coredata://stub"\n' % captured)
            stub.chmod(0o700)
            shortcuts = bindir / "shortcuts"
            shortcuts.write_text('#!/bin/bash\necho "the Shortcut list could not be read" >&2\nexit 1\n')
            shortcuts.chmod(0o700)
            for name, body in (("open", "exit 0\n"), ("pgrep", "exit 1\n")):
                side = bindir / name
                side.write_text("#!/bin/bash\n" + body)
                side.chmod(0o700)
            env = dict(os.environ)
            env["PATH"] = f"{bindir}:{env.get('PATH', '/usr/bin:/bin')}"
            proc = subprocess.run(
                [str(SCRIPT), *flags, "[AFC, TEST] notes timeout probe"],
                input="a body\n",
                capture_output=True,
                text=True,
                env=env,
                timeout=120,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(captured.is_file(), "osascript was never called: %s" % proc.stderr)
            return captured.read_text()


if __name__ == "__main__":
    unittest.main()
