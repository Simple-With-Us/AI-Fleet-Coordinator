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
osascript is still working, and the later completion creates a duplicate note.  The caller also
has to allow more than the write and the pin step together,  or a slow pair outlives the caller
and it reports an unknown outcome for a note that was written.

Three more things are held here, each one a review finding on the same change:

  the override is validated,  so a malformed value cannot abort the write with an AppleScript
  syntax error,  and the value never reaches the AppleScript source at all:  the AppleScript
  reads it back out of the environment with "system attribute".

  the pin Shortcut run is bounded,  because it happens after the write and an unbounded run can
  outlive the caller's cap.

  the reason for the number sits next to the constant,  and not only somewhere in the file.

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
SHORTCUT_TIMEOUT_NAME = "NOTES_SHORTCUT_TIMEOUT_S"
SHORTCUT_DEFAULT_S = 120

#  a stand-in for osascript:  it reports the AppleScript it was given and the timeout value its
#  child process actually saw,  so the test can look at what really reached the interpreter.
#  It keeps everything in memory:  a file written from a stub on a loaded host is not something a
#  test can rely on,  and the script's own stdout already carries the source.
OSASCRIPT_STUB = """#!/bin/bash
source_text="$(cat)"
printf 'APPLESCRIPT-START\\n%s\\nAPPLESCRIPT-END\\n' "$source_text"
printf 'TIMEOUT-SEEN: %s\\n' "${NOTES_APPLE_EVENT_TIMEOUT_S:-unset}"
printf 'updated note id=x-coredata://probe\\n'
exit 0
"""

#  a stand-in for the `shortcuts` command,  so the test never runs a real Shortcut on the host
SHORTCUTS_STUB = """#!/bin/bash
exit 0
"""


class AppleEventTimeoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        #  No class-level skip:  a missing or renamed canonical helper has to fail the suite,  not
        #  quietly skip it and leave the timeout unpinned.  A skip here is exactly the failure this
        #  file exists to prevent.
        if not SCRIPT.is_file():
            raise AssertionError(f"the canonical helper is missing: {SCRIPT}")
        cls.text = SCRIPT.read_text()

    # ---------------------------------------------------------------------------------------------
    # the constant
    # ---------------------------------------------------------------------------------------------

    def test_the_timeout_is_defined_once_with_its_default(self) -> None:
        pattern = re.compile(rf"^{TIMEOUT_NAME}_DEFAULT=(\d+)$", re.MULTILINE)
        found = pattern.findall(self.text)
        self.assertEqual(
            len(found), 1, f"the timeout default is not defined exactly once: {found}"
        )
        self.assertEqual(int(found[0]), TIMEOUT_DEFAULT_S)

    def test_the_timeout_is_resolved_once_through_the_validating_helper(self) -> None:
        """A value that is not a whole number of seconds must be refused,  not interpolated.

        An override that reached the AppleScript as text would abort the write with a syntax
        error,  so the value is checked here and only a number is ever used.
        """
        pattern = re.compile(
            rf'^{TIMEOUT_NAME}="\$\(_seconds_override {TIMEOUT_NAME} "\${TIMEOUT_NAME}_DEFAULT" \d+\)"$',
            re.MULTILINE,
        )
        found = pattern.findall(self.text)
        self.assertEqual(len(found), 1, f"the timeout is not resolved once: {found}")
        self.assertIn("_seconds_override() {", self.text)

    def test_the_reason_is_recorded_next_to_the_constant(self) -> None:
        """The comment is the evidence for the 900 s value;  a bare number would be a guess.

        The reason has to sit next to the constant:  a search over the whole file would still
        find a reason that had been moved far away,  or deleted from here and left in a
        different block.  So the reason is looked for in a window around the constant,  and it
        has to appear exactly once in the file.
        """
        reason = "AppleEvent timed out. (-1712)"
        at = self.text.index(f"{TIMEOUT_NAME}_DEFAULT=")
        window = self.text[max(0, at - 1200) : at + 1200]
        self.assertIn(reason, window, "the reason for the timeout is not next to the constant")
        self.assertEqual(
            self.text.count(reason),
            1,
            "the reason appears more than once,  so the copy next to the constant is not the "
            "one being checked",
        )

    # ---------------------------------------------------------------------------------------------
    # the two Notes blocks
    # ---------------------------------------------------------------------------------------------

    def test_both_notes_blocks_read_the_timeout_from_the_environment(self) -> None:
        """The value must never be interpolated into the AppleScript source.

        The AppleScript reads it back with "system attribute",  so an override can only ever be
        a number the shell already validated,  and nothing from the environment becomes code.
        """
        self.assertEqual(
            self.text.count(f'(system attribute "{TIMEOUT_NAME}") as integer'),
            2,
            "the AppleScript blocks do not both read the timeout from the environment",
        )
        self.assertEqual(self.text.count("with timeout of notesTimeoutSecs seconds"), 2)
        self.assertEqual(self.text.count("end timeout"), 2)
        self.assertNotIn(f"with timeout of ${{{TIMEOUT_NAME}}} seconds", self.text)
        self.assertNotIn("with timeout of 900 seconds", self.text)

    def test_each_timeout_wraps_a_notes_call(self) -> None:
        #  The timeout must wrap the "tell application \"Notes\"" block,  not sit next to it.
        pattern = re.compile(
            r"with timeout of notesTimeoutSecs seconds\ntell application \"Notes\"\n"
            r"(?:.*\n)*?end tell\nend timeout\n"
        )
        self.assertEqual(len(pattern.findall(self.text)), 2)

    def test_the_timeout_is_not_wrapped_in_another_timeout(self) -> None:
        #  A nested "with timeout" would reset the outer one when the inner block closes.
        self.assertNotIn("seconds\nwith timeout", self.text)

    def test_the_create_and_update_blocks_both_carry_it(self) -> None:
        #  The update block writes the system attribute path,  the create block the $TMP path.
        self.assertIn('set htmlPath to POSIX file (system attribute "TMP")', self.text)
        self.assertEqual(self.text.count('set htmlPath to POSIX file (system attribute "TMP")'), 2)

    def test_the_notes_blocks_use_a_quoted_heredoc(self) -> None:
        """Nothing from the environment may become AppleScript source.

        A quoted heredoc hands osascript the text as it is written,  so the only way a value
        could reach the source is the script building it,  and the script reads every value
        through "system attribute" instead.  The pin block is quoted for the same reason.
        """
        self.assertEqual(self.text.count("osascript <<'EOF'"), 3)
        self.assertNotIn("osascript <<EOF", self.text)

    # ---------------------------------------------------------------------------------------------
    # the caller's budget
    # ---------------------------------------------------------------------------------------------

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

    def test_the_caller_allows_more_than_the_write_plus_the_pin(self) -> None:
        """The pin step runs after the write,  so the cap has to cover both.

        A cap that covered only the write lets a slow write and a slow pin together outlive the
        caller,  which then reports an unknown outcome for a note that was in fact written.
        """
        sys.path.insert(0, str(SCRIPTS))
        from agent_sync import notes

        self.assertGreater(
            notes.SCRIPT_TIMEOUT_S,
            TIMEOUT_DEFAULT_S + SHORTCUT_DEFAULT_S,
            "the caller's cap does not cover the AppleEvent timeout and the Shortcut run "
            "together,  so a slow pair outlives the caller",
        )

    def test_the_shortcut_run_is_bounded(self) -> None:
        """The Shortcut run happens after the write,  so it needs a bound of its own."""
        self.assertEqual(
            self.text.count(f'_run_bounded "${SHORTCUT_TIMEOUT_NAME}" shortcuts run'),
            1,
            "the Shortcut run is not bounded",
        )
        self.assertIn(f'{SHORTCUT_TIMEOUT_NAME}_DEFAULT={SHORTCUT_DEFAULT_S}', self.text)
        self.assertIn('_run_bounded() {', self.text)

    # ---------------------------------------------------------------------------------------------
    # end to end,  with a stubbed osascript
    # ---------------------------------------------------------------------------------------------

    def _run_helper(self, **env: str) -> tuple[subprocess.CompletedProcess, str, str]:
        """Run the real helper with a stub osascript,  and return the result, the source it saw
        and the timeout value its child process saw."""
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            bindir.mkdir(mode=0o700)
            stub = bindir / "osascript"
            stub.write_text(OSASCRIPT_STUB)
            stub.chmod(0o700)
            #  the helper pins by default,  so the test switches that off and stubs the
            #  `shortcuts` command as well:  it must never run a real Shortcut on the host
            side = bindir / "shortcuts"
            side.write_text(SHORTCUTS_STUB)
            side.chmod(0o700)
            child = dict(os.environ)
            child["PATH"] = f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}"
            child["APPLE_NOTES_PIN"] = "0"
            child.update(env)
            proc = subprocess.run(
                [
                    "bash",
                    str(SCRIPT),
                    "--update",
                    "--session",
                    "probe-session",
                    "[AFC, MiniMax] timeout probe",
                ],
                input="# probe\n\nOne sentence.  Two sentences.\n",
                capture_output=True,
                text=True,
                env=child,
                timeout=180,
                check=False,
            )
            out = proc.stdout
            source = ""
            if "APPLESCRIPT-START" in out and "APPLESCRIPT-END" in out:
                source = out.split("APPLESCRIPT-START", 1)[1].split("APPLESCRIPT-END", 1)[0]
            value = ""
            for line in out.splitlines():
                if line.startswith("TIMEOUT-SEEN: "):
                    value = line.split("TIMEOUT-SEEN: ", 1)[1].strip()
            return proc, source, value

    def test_a_malformed_override_never_reaches_the_applescript(self) -> None:
        """A bad override is refused,  and the default is used instead.

        The value the child process actually sees is the number,  and the AppleScript it is
        given carries no trace of the string that was passed in.  A malformed value that
        reached the source would abort the write with an AppleScript syntax error.
        """
        hostile = "900; do shell script \"rm -rf /\""
        proc, source, value = self._run_helper(**{TIMEOUT_NAME: hostile})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ignoring %s" % TIMEOUT_NAME, proc.stderr)
        self.assertEqual(value, str(TIMEOUT_DEFAULT_S), "the override was not refused")
        self.assertNotIn("do shell script", source)
        self.assertNotIn(hostile, source)
        self.assertIn(f'(system attribute "{TIMEOUT_NAME}") as integer', source)

    def test_a_valid_override_is_passed_through(self) -> None:
        """A whole number inside the range is honoured,  so the knob works and stays bounded."""
        proc, source, value = self._run_helper(**{TIMEOUT_NAME: "600"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(value, "600", "a valid override was not passed through")
        self.assertNotIn("ignoring %s" % TIMEOUT_NAME, proc.stderr)
        self.assertIn(f'(system attribute "{TIMEOUT_NAME}") as integer', source)

    @unittest.skipUnless(shutil.which("osacompile"), "needs osacompile")
    def test_the_applescript_the_helper_builds_compiles(self) -> None:
        """The source the helper really hands osascript has to compile.

        The source-text assertions above cannot see a broken block,  so the block is compiled
        as it was delivered,  with the environment the helper exports.
        """
        proc, source, value = self._run_helper()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(source, "the stub osascript was never called")
        self.assertEqual(value, str(TIMEOUT_DEFAULT_S))
        compiled = subprocess.run(
            ["osacompile", "-o", os.devnull, "-e", source],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, TIMEOUT_NAME: value},
        )
        self.assertEqual(compiled.returncode, 0, compiled.stderr)

    @unittest.skipUnless(shutil.which("osacompile"), "needs osacompile")
    def test_the_update_block_still_compiles(self) -> None:
        """Compile the rendered AppleScript,  so a broken block fails here and not on a write."""
        block = re.search(r"osascript <<'EOF'\n(.*?)\nEOF\n", self.text, re.DOTALL)
        self.assertIsNotNone(block, "the Notes block is gone")
        rendered = block.group(1)
        self.assertNotIn("$TMP", rendered, "the block still interpolates a shell variable")
        compiled = subprocess.run(
            ["osacompile", "-o", os.devnull, "-e", rendered],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(compiled.returncode, 0, compiled.stderr)


if __name__ == "__main__":
    unittest.main()
