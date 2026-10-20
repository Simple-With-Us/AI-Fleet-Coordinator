"""AFC-431:  apple-notes-coding.sh must not leave a coding note unpinned because a Shortcut run raced the
note index, and it must not report one failure as another.

The pin block has four distinct outcomes and one that is not an outcome at all:

  rc 0  the note is pinned
  rc 1  the Shortcut ran but did not complete
  rc 2  the Shortcut is not installed
  rc 3  the Shortcut list could not be read, so it is unknown whether the Shortcut exists
  rc 4  pinning was disabled by APPLE_NOTES_PIN=0, so no run was attempted

The script used to collapse rc 1 and rc 2 into one message that always named the Shortcut as unavailable,
it ran the Shortcut exactly once, so a timing race left the note unpinned, and a failed `shortcuts list`
looked exactly like a Shortcut that was never installed.

The retry is gated on the APPLE_NOTES_PIN_RETRY fleet knob, because a miss can pop a note picker on the
owner's screen and an operator may want a single attempt.  The knob is read once per process through
scripts/fleet_knobs, and APPLE_NOTES_PIN_RETRY in the environment is only a narrowing override.

These tests drive the real source:  the `_run_note_shortcut` function, the pin decision block and the unpin
decision block are cut out of scripts/apple-notes-coding.sh and run in a harness with a stub `shortcuts` on
PATH, so the assertions cover the shipped text and not a copy of it.  Nothing here touches Notes, and it
runs anywhere bash and python3 exist.

AFC-436 adds the Shortcuts-app precondition to the same harness:  `open` and `pgrep` are stubbed as well, so
the test records that the app was asked to open in the background, and never launches the real app on the
host.  The precondition was live-only until AFC-436, and the AFC-431 rewrite of _run_note_shortcut dropped it.

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
  if [ "${STUB_LIST_FAILS:-0}" = "1" ]; then exit 1; fi
  if [ "${STUB_INSTALLED:-1}" = "1" ]; then printf '%s\\n' "${STUB_NAME:-Pin Coding Note}"; fi
  exit 0
fi
n=0
if [ -f "$STUB_COUNTER" ]; then n=$(cat "$STUB_COUNTER"); fi
n=$((n+1))
printf '%s' "$n" > "$STUB_COUNTER"
if [ "${STUB_FAIL_FIRST:-0}" -gt 0 ] && [ "$n" -le "${STUB_FAIL_FIRST:-0}" ]; then exit 1; fi
exit 0
"""

#  a stand-in for `python3 -m fleet_knobs get APPLE_NOTES_PIN_RETRY`, which the script runs in the
#  directory it lives in;  only put on PATH when a test asks for it (STUB_KNOB)
KNOB_STUB = """#!/bin/bash
printf x >> "$STUB_COUNTER.knob"
printf '%s\\n' "${STUB_KNOB}"
"""

#  a stand-in for `pgrep -x Shortcuts`,  which tells the script whether the Shortcuts app is up.
#  Without this stub the test would find the real Shortcuts app on the host and skip the open.
PGREP_STUB = """#!/bin/bash
if [ "${STUB_SHORTCUTS_RUNNING:-0}" = "1" ]; then exit 0; fi
if [ -f "$STUB_COUNTER.running" ]; then exit 0; fi
exit 1
"""

#  a stand-in for `open -g -a Shortcuts`,  which records the request and marks the app as up
OPEN_STUB = """#!/bin/bash
printf '%s\\n' "$*" >> "$STUB_COUNTER.open"
: > "$STUB_COUNTER.running"
exit 0
"""

HARNESS = """set -uo pipefail
TITLE="AFC test note"
MODE="${HARNESS_MODE:-update}"
WANT_PIN="${HARNESS_WANT_PIN:-0}"
APPLE_NOTES_PIN="${HARNESS_APPLE_NOTES_PIN:-1}"
#  the knob's resolved value, as the caller forwards it:  set here so the tests never shell out to
#  fleet_knobs, and so APPLE_NOTES_PIN_RETRY in the environment keeps meaning a narrowing override
APPLE_NOTES_PIN_RETRY="${HARNESS_APPLE_NOTES_PIN_RETRY-1}"
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
    knob = re.search(r"^_PIN_RETRY=\"\"\n(?:.*\n)*?^\}\n", source, re.MULTILINE)
    fn = re.search(r"^_run_note_shortcut\(\) \{[^\n]*\n(?:.*\n)*?^\}\n", source, re.MULTILINE)
    #  the Shortcuts-app precondition is optional:  it was live-only until AFC-436 put it in the
    #  canonical script,  so the extraction still works against a script that does not have it
    ensure = re.search(
        r"^_ensure_shortcuts_running\(\) \{[^\n]*\n(?:.*\n)*?^\}\n", source, re.MULTILINE
    )
    assert fn, "the _run_note_shortcut function is gone from apple-notes-coding.sh"
    assert knob, "the _pin_retry_on knob resolver is gone from apple-notes-coding.sh"
    start = source.index("# Set APPLE_NOTES_PIN=0 to skip pinning entirely.")
    want = source.index('if [[ "$WANT_PIN" != "1" ]]; then')
    end = source.index("\nfi\n", want) + len("\nfi\n")
    parts = knob.group(0)
    if ensure:
        parts += ensure.group(0)
    return parts + fn.group(0), source[start:end]


def _extract_unpin(source: str) -> str:
    """Cut the unpin decision block out of the real script, the same way, so it is covered too."""
    start = source.index('if [[ "$MODE" == "unpin" ]]; then')
    return source[start : source.index("\nfi\n", start) + len("\nfi\n")]


@unittest.skipUnless(SCRIPT.is_file(), "needs scripts/apple-notes-coding.sh")
class PinDecisionTests(unittest.TestCase):
    def run_block(self, **env: str) -> tuple[subprocess.CompletedProcess, int]:
        """Run the real pin block with a stub `shortcuts`, and return the result and the run count."""
        source = SCRIPT.read_text()
        function, block = _extract(source)
        return self._run(function, block, **env)

    def run_unpin_block(self, **env: str) -> tuple[subprocess.CompletedProcess, int]:
        """Run the real unpin block with a stub `shortcuts`, and return the result and the run count."""
        source = SCRIPT.read_text()
        function, _ = _extract(source)
        return self._run(function, _extract_unpin(source), **env)

    def _run(
        self, function: str, block: str, **env: str
    ) -> tuple[subprocess.CompletedProcess, int]:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            bindir.mkdir(mode=0o700)
            stub = bindir / "shortcuts"
            stub.write_text(STUB)
            stub.chmod(0o700)
            #  the script opens the Shortcuts app in the background before the run,  so `open` and
            #  `pgrep` are stubbed too:  the test must never launch the real app on the host
            for name, body in (("pgrep", PGREP_STUB), ("open", OPEN_STUB)):
                side = bindir / name
                side.write_text(body)
                side.chmod(0o700)
            if "STUB_KNOB" in env:
                #  the script reads the knob through the fleet_knobs package next to it and the
                #  python3 that runs it:  both live here, so `python3 -m fleet_knobs get` is the stub
                package = (
                    Path(tmp) / "apps/lanes/_managed/fleet/agent-sync-runtime/scripts/fleet_knobs"
                    if env.get("STUB_MANAGED_KNOBS")
                    else Path(tmp) / "fleet_knobs"
                )
                package.mkdir(mode=0o700, parents=True)
                knob_stub = bindir / "python3"
                knob_stub.write_text(KNOB_STUB)
                knob_stub.chmod(0o700)
            counter = Path(tmp) / "counter"
            script = Path(tmp) / "harness.sh"
            #  a plain replace, because the harness carries shell ${...} braces that str.format would eat
            script.write_text(HARNESS.replace("{function}", function).replace("{block}", block))
            child = dict(os.environ)
            child["PATH"] = f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}"
            child["STUB_COUNTER"] = str(counter)
            child.update(env)
            if env.get("STUB_MANAGED_KNOBS"):
                child["HOME"] = tmp
                child.pop("AGENT_SYNC_RUNTIME", None)
            p = subprocess.run(
                ["bash", str(script)],
                capture_output=True,
                text=True,
                env=child,
                timeout=60,
                check=False,
            )
            runs = int(counter.read_text()) if counter.exists() else 0
            marker = Path(str(counter) + ".knob")
            self.knob_reads = len(marker.read_text()) if marker.exists() else 0
            opened = Path(str(counter) + ".open")
            self.opens = opened.read_text().splitlines() if opened.exists() else []
            return p, runs

    def test_a_working_shortcut_pins_the_note_on_the_first_try(self):
        p, runs = self.run_block()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("pinned: yes", p.stdout)
        self.assertEqual(runs, 1)

    def test_the_shortcuts_app_is_opened_in_the_background_when_it_is_not_running(self):
        """The Find Notes action is an app intent served by the Shortcuts app itself.

        With that app closed the intent cannot be dispatched, and the shortcut fails for every
        title, including one that does not exist.  The app is opened with `open -g`, so it does
        not steal focus.  This precondition was live-only until AFC-436 put it in the canonical
        script, and the AFC-431 rewrite of _run_note_shortcut dropped it.
        """
        p, runs = self.run_block()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("pinned: yes", p.stdout)
        self.assertTrue(self.opens, "the Shortcuts app was never opened")
        self.assertIn("-g", self.opens[0])
        self.assertIn("Shortcuts", self.opens[0])

    def test_the_shortcuts_app_is_not_opened_when_it_is_already_running(self):
        p, runs = self.run_block(STUB_SHORTCUTS_RUNNING="1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("pinned: yes", p.stdout)
        self.assertEqual(self.opens, [], "the app was opened although it was already up")
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

    def test_a_list_that_cannot_be_read_is_not_reported_as_a_missing_shortcut(self):
        """A failed list lookup proves nothing about the Shortcut, so it must not send an operator to rebuild one."""
        p, runs = self.run_block(STUB_LIST_FAILS="1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("could not be read", p.stdout)
        self.assertNotIn("is not installed", p.stdout)
        self.assertIn("could not be read", p.stderr)
        self.assertEqual(runs, 0, "the Shortcut ran even though the list could not be read")

    def test_the_retry_can_be_switched_off(self):
        """APPLE_NOTES_PIN_RETRY=0 keeps the single attempt, for a caller that will not risk a second run."""
        p, runs = self.run_block(STUB_FAIL_FIRST="99", HARNESS_APPLE_NOTES_PIN_RETRY="0")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("shortcut run failed", p.stdout)
        self.assertEqual(runs, 1, "the retry ran even though it was switched off")

    def test_the_knob_turns_the_retry_off(self):
        """The fleet knob (Infisical) owns the switch;  false keeps the single attempt."""
        p, runs = self.run_block(
            STUB_FAIL_FIRST="99", HARNESS_APPLE_NOTES_PIN_RETRY="", STUB_KNOB="false"
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(runs, 1, "the retry ran even though the knob turned it off")

    def test_the_environment_cannot_turn_the_retry_back_on(self):
        """A local value may only narrow:  it can stop the retry, never start one the knob turned off."""
        p, runs = self.run_block(
            STUB_FAIL_FIRST="99", HARNESS_APPLE_NOTES_PIN_RETRY="1", STUB_KNOB="false"
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(runs, 1, "the environment widened the knob back to a retry")

    def test_installed_helper_reads_the_managed_runtime_knob(self):
        result, runs = self.run_block(
            STUB_MANAGED_KNOBS="1", STUB_KNOB="false", STUB_FAIL_FIRST="99"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(runs, 1)
        self.assertEqual(self.knob_reads, 1)

    def test_successful_first_attempt_never_fetches_the_retry_knob(self):
        result, runs = self.run_block(STUB_KNOB="true")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(runs, 1)
        self.assertEqual(self.knob_reads, 0)

    def test_local_disable_narrows_an_enabled_knob(self):
        result, runs = self.run_block(
            STUB_FAIL_FIRST="99", HARNESS_APPLE_NOTES_PIN_RETRY="0", STUB_KNOB="true"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(runs, 1)

    def test_a_local_disable_does_not_reach_the_knob(self):
        """The local off switch is a fast path:  it decides without a lookup that can reach the network."""
        result, runs = self.run_block(
            STUB_FAIL_FIRST="99", HARNESS_APPLE_NOTES_PIN_RETRY="0", STUB_KNOB="true"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(runs, 1)
        self.assertEqual(
            self.knob_reads, 0, "the knob was read even though the local value had already decided"
        )

    def test_the_knob_turns_the_retry_on(self):
        """The knob alone decides when the caller names no override."""
        p, runs = self.run_block(
            STUB_FAIL_FIRST="99", HARNESS_APPLE_NOTES_PIN_RETRY="", STUB_KNOB="true"
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(runs, 2, "the retry did not run even though the knob turned it on")

    def test_the_gui_fallback_is_still_reachable_after_a_failed_run(self):
        """--pin still has to reach the GUI path when the Shortcut fails, so the skip line must not fire."""
        p, _ = self.run_block(HARNESS_WANT_PIN="1", STUB_FAIL_FIRST="99")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("pinned: skipped", p.stdout)
        self.assertIn("ran but did not complete", p.stderr)

    def test_unpin_reports_a_missing_shortcut_as_a_missing_shortcut(self):
        p, runs = self.run_unpin_block(HARNESS_MODE="unpin")
        self.assertEqual(p.returncode, 4, p.stdout)
        self.assertIn("not found", p.stderr)
        self.assertNotIn("run failed", p.stderr)
        self.assertEqual(runs, 0, "the Shortcut ran even though it is not installed")

    def test_unpin_reports_an_unreadable_list_as_an_unreadable_list(self):
        """The twin of the pin case:  a failed list lookup proves nothing, so it must not say 'run failed'.

        The block used to branch on $? after each [[ ]], which made this arm unreachable.
        """
        p, runs = self.run_unpin_block(HARNESS_MODE="unpin", STUB_LIST_FAILS="1")
        self.assertEqual(p.returncode, 4, p.stdout)
        self.assertIn("could not be read", p.stderr)
        self.assertNotIn("run failed", p.stderr)
        self.assertNotIn("not found", p.stderr)
        self.assertEqual(runs, 0, "the Shortcut ran even though the list could not be read")

    def test_unpin_reports_a_failed_run_as_a_failed_run(self):
        p, runs = self.run_unpin_block(
            HARNESS_MODE="unpin", STUB_NAME="Unpin Coding Note", STUB_FAIL_FIRST="99"
        )
        self.assertEqual(p.returncode, 4, p.stdout)
        self.assertIn("run failed", p.stderr)
        self.assertNotIn("not found", p.stderr)
        self.assertEqual(runs, 2)

    def test_unpin_succeeds_headlessly_when_the_shortcut_is_there(self):
        p, runs = self.run_unpin_block(HARNESS_MODE="unpin", STUB_NAME="Unpin Coding Note")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("unpinned: yes", p.stdout)
        self.assertEqual(runs, 1)


if __name__ == "__main__":
    unittest.main()
