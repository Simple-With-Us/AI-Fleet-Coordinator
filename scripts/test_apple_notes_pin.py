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

There is no GUI fallback (owner 2026-10-20):  the helper used to bring Notes to the front and click its
Pin menu item when the Shortcut did not pin, and that is what kept popping Notes open on the owner's screen.
A failed Shortcut now leaves the note saved unpinned with a warning on stderr (exit 0 for create and update,
exit 4 for --pin-only), nothing else touches the Notes window, one pin or unpin Shortcut runs at a time
across processes, and a run that hit its time bound is never repeated.  The end-to-end tests at the bottom
run the whole script with `osascript`, `shortcuts`, `open`, `pgrep` and `sleep` stubbed on PATH and assert on
what reached the stubs, so they never touch Notes.

Run:  cd scripts && python3 -m unittest test_apple_notes_pin
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
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
input_file=""
while [ $# -gt 0 ]; do
  case "$1" in
    -i) input_file="$2"; shift 2 ;;
    *) shift ;;
  esac
done
n=0
if [ -f "$STUB_COUNTER" ]; then n=$(cat "$STUB_COUNTER"); fi
n=$((n+1))
printf '%s' "$n" > "$STUB_COUNTER"
if [ "${STUB_RECORD_INPUT:-0}" = "1" ] && [ -n "$input_file" ]; then
  #  a record-per-line record, so a test can assert attempt N passed input M
  printf '%s\\n' "$(cat "$input_file" 2>/dev/null || printf '')" >> "$STUB_COUNTER.inputs"
fi
if [ -n "${STUB_LOCK_MODE_FILE:-}" ]; then
  #  the lock directory's mode while this run holds it:  drwx------ is owner-only
  ls -ld "$NOTES_PIN_LOCK_DIR" | cut -c1-10 > "$STUB_LOCK_MODE_FILE"
fi
if [ "${STUB_HOLD:-0}" = "1" ]; then
  #  a run that lasts a moment, so two overlapping runs can be seen:  the second one to start finds the marker
  if [ -f "$STUB_COUNTER.active" ]; then printf x >> "$STUB_COUNTER.overlap"; fi
  : > "$STUB_COUNTER.active"
  sleep 1.5
  rm -f "$STUB_COUNTER.active"
fi
if [ "${STUB_HANG:-0}" = "1" ]; then sleep 30; exit 0; fi
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
[ -z "${HARNESS_UMASK:-}" ] || umask "$HARNESS_UMASK"
TITLE="${HARNESS_TITLE:-AFC test note}"
MODE="${HARNESS_MODE:-update}"
APPLE_NOTES_PIN="${HARNESS_APPLE_NOTES_PIN:-1}"
#  the knob's resolved value, as the caller forwards it:  set here so the tests never shell out to
#  fleet_knobs, and so APPLE_NOTES_PIN_RETRY in the environment keeps meaning a narrowing override
APPLE_NOTES_PIN_RETRY="${HARNESS_APPLE_NOTES_PIN_RETRY-1}"
#  the bound one Shortcuts invocation runs under,  as the helper sets it
NOTES_SHORTCUT_TIMEOUT_S="${HARNESS_SHORTCUT_TIMEOUT_S:-30}"
#  the real block waits 2 s for the note index, 3 s between attempts and up to the lock wait for the pin
#  lock;  the test only needs the shape,  except the tests that run two pins at once
[ "${HARNESS_REAL_SLEEP:-0}" = "1" ] || sleep() { :; }
{constants}
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
    #  the bounded Shortcut runner is optional for the same reason:  it came with AFC-436
    bounded = re.search(
        r"^_run_shortcuts_bounded\(\) \{[^\n]*\n(?:.*\n)*?^\}\n", source, re.MULTILINE
    )
    #  the cross-process pin lock is the 2026-10-20 guard:  its constants, its three helpers and the
    #  locked body of the runner that the public runner wraps
    constants = re.search(
        r"^NOTES_PIN_LOCK_DIR=(?:.*\n)*?^_PIN_LOCK_HELD=0\n", source, re.MULTILINE
    )
    assert fn, "the _run_note_shortcut function is gone from apple-notes-coding.sh"
    assert knob, "the _pin_retry_on knob resolver is gone from apple-notes-coding.sh"
    assert constants, "the pin lock constants are gone from apple-notes-coding.sh"
    functions = ""
    for name in (
        "_pin_pid_alive",
        "_pin_lock_stale",
        "_pin_lock_acquire",
        "_pin_lock_release",
        "_run_note_shortcut_locked",
    ):
        found = re.search(rf"^{name}\(\) \{{[^\n]*\n(?:.*\n)*?^\}}\n", source, re.MULTILINE)
        assert found, f"{name} is gone from apple-notes-coding.sh"
        functions += found.group(0)
    start = source.index("# Set APPLE_NOTES_PIN=0 to skip pinning entirely.")
    parts = knob.group(0)
    if ensure:
        parts += ensure.group(0)
    if bounded:
        parts += bounded.group(0)
    #  the pin block is the end of the script:  there is nothing after it to cut off, which is the point
    return constants.group(0) + parts + functions + fn.group(0), source[start:]


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
            script.write_text(
                HARNESS.replace("{constants}", "")
                .replace("{function}", function)
                .replace("{block}", block)
            )
            child = dict(os.environ)
            child["PATH"] = f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}"
            child["STUB_COUNTER"] = str(counter)
            #  the lock lives in the test's own directory:  a test never touches the real ~/Library/Caches
            child["NOTES_PIN_LOCK_DIR"] = str(Path(tmp) / "pin.lock")
            lock_dir = Path(tmp) / "pin.lock"
            holder = None
            pre_lock = env.pop("PRE_LOCK", "")
            if pre_lock:
                #  a lock somebody else holds:  "live" names a process that is still running, "dead" one that
                #  has exited, "foreign" a live process this user may not signal (pid 1, owned by root),
                #  "old" a live process whose lock is older than any run could be, and "aged4" one that is
                #  four minutes old:  stale under the default Shortcut bound but not under a raised one
                if pre_lock == "foreign":
                    holder_pid = 1
                else:
                    holder = subprocess.Popen(["sleep", "60"])
                    if pre_lock == "dead":
                        holder.kill()
                        holder.wait()
                    holder_pid = holder.pid
                lock_dir.mkdir(mode=0o700)
                (lock_dir / "pid").write_text(f"{holder_pid}\n")
                if pre_lock in ("old", "aged4"):
                    age = 600 if pre_lock == "old" else 240
                    stamp = time.time() - age
                    os.utime(lock_dir, (stamp, stamp))
            if env.get("NO_PERL"):
                #  A host without perl:  the bound on a Shortcut run comes from an alarm,  and it
                #  must not depend on perl alone.  Only the tools the pin block needs are linked
                #  in,  and perl is deliberately left out of the PATH.
                plain = Path(tmp) / "plain"
                plain.mkdir(mode=0o700)
                for name in (
                    "bash",
                    "cat",
                    "dirname",
                    "find",
                    "grep",
                    "mkdir",
                    "mktemp",
                    "mv",
                    "python3",
                    "rm",
                    "sed",
                    "sleep",
                ):
                    found = shutil.which(name)
                    self.assertIsNotNone(found, f"{name} is missing on this host")
                    (plain / name).symlink_to(found)
                child["PATH"] = f"{bindir}:{plain}"
            child.update(env)
            if env.get("STUB_MANAGED_KNOBS"):
                child["HOME"] = tmp
                child.pop("AGENT_SYNC_RUNTIME", None)
            try:
                p = subprocess.run(
                    ["bash", str(script)],
                    capture_output=True,
                    text=True,
                    env=child,
                    timeout=60,
                    check=False,
                )
            finally:
                if holder is not None:
                    holder.kill()
                    holder.wait()
            self.lock_left = lock_dir.exists()
            runs = int(counter.read_text()) if counter.exists() else 0
            marker = Path(str(counter) + ".knob")
            self.knob_reads = len(marker.read_text()) if marker.exists() else 0
            opened = Path(str(counter) + ".open")
            self.opens = opened.read_text().splitlines() if opened.exists() else []
            overlap = Path(str(counter) + ".overlap")
            self.overlaps = len(overlap.read_text()) if overlap.exists() else 0
            inputs = Path(str(counter) + ".inputs")
            self.inputs = inputs.read_text().splitlines() if inputs.exists() else []
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
        self.assertIn("pin skipped: Pin Coding Note Shortcut failed; note saved unpinned", p.stderr)
        self.assertEqual(runs, 2)

    def test_a_hanging_shortcut_is_bounded_and_reported_as_a_run_failure(self):
        """The Shortcut can wait on a note picker on the owner's screen.

        An unbounded run can outlast the caller's cap:  the note is written and the caller is
        then told the outcome is unknown and refuses the retry of a request that did succeed.
        The run is bounded instead,  and the bound is reported as a failed run,  not as a
        missing Shortcut (AFC-436).
        """
        p, runs = self.run_block(STUB_HANG="1", HARNESS_SHORTCUT_TIMEOUT_S="1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("pin skipped: Pin Coding Note Shortcut failed; note saved unpinned", p.stderr)
        self.assertIn("shortcut run failed", p.stdout)
        self.assertNotIn("is not installed", p.stdout)
        self.assertEqual(
            runs,
            1,
            "a run that hit its time bound was run again:  a hang is a picker waiting on the owner's "
            "screen, and a second run only opens a second picker",
        )

    def test_a_host_without_perl_bounds_the_run_all_the_same(self):
        """The bound on a Shortcut run is an alarm,  and it must not depend on perl alone.

        macOS ships perl,  but the run was unbounded without it,  and an unbounded run can
        outlast the caller's cap:  the note is written and the caller is then told the outcome
        is unknown and refuses the retry of a request that did succeed (AFC-436).
        """
        p, runs = self.run_block(NO_PERL="1", STUB_HANG="1", HARNESS_SHORTCUT_TIMEOUT_S="1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("pin skipped: Pin Coding Note Shortcut failed; note saved unpinned", p.stderr)
        self.assertIn("shortcut run failed", p.stdout)
        self.assertNotIn("is not installed", p.stdout)
        self.assertEqual(runs, 1, "the run that hit its time bound was run again")

    def test_a_missing_shortcut_is_named_as_a_missing_shortcut(self):
        p, runs = self.run_block(STUB_INSTALLED="0")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("is not installed", p.stdout)
        self.assertNotIn("run failed", p.stdout)
        self.assertIn("pin skipped: the 'Pin Coding Note' Shortcut is not installed", p.stderr)
        self.assertIn("note saved unpinned", p.stderr)
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
        self.assertIn("pin skipped: the Shortcut list could not be read", p.stderr)
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

    def test_a_failed_run_warns_on_stderr_and_succeeds_for_create_and_update(self):
        """The Shortcut did not pin, and there is nothing else to try:  the note is saved unpinned.

        The helper used to bring Notes to the front and click its Pin menu item here, which is what
        kept popping Notes open on the owner's screen (2026-10-20).  Now a create or an update still
        succeeds, says on stderr that the pin was skipped, and touches nothing else.
        """
        for mode in ("create", "update"):
            with self.subTest(mode=mode):
                p, runs = self.run_block(HARNESS_MODE=mode, STUB_FAIL_FIRST="99")
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn(
                    "pin skipped: Pin Coding Note Shortcut failed; note saved unpinned", p.stderr
                )
                self.assertNotIn("pinned: yes", p.stdout)

    def test_a_failed_run_fails_pin_only(self):
        """--pin-only was asked to pin and nothing else, so a pin that did not happen is exit 4."""
        p, runs = self.run_block(HARNESS_MODE="pin", STUB_FAIL_FIRST="99")
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
        self.assertIn("pin skipped: Pin Coding Note Shortcut failed; nothing was pinned", p.stderr)
        self.assertNotIn("note saved unpinned", p.stderr)

    def test_pin_only_exits_4_for_every_way_the_pin_can_fail(self):
        for label, env in (
            ("missing", {"STUB_INSTALLED": "0"}),
            ("list unreadable", {"STUB_LIST_FAILS": "1"}),
            ("disabled", {"HARNESS_APPLE_NOTES_PIN": "0"}),
            ("hang", {"STUB_HANG": "1", "HARNESS_SHORTCUT_TIMEOUT_S": "1"}),
        ):
            with self.subTest(label):
                p, _ = self.run_block(HARNESS_MODE="pin", **env)
                self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
                self.assertIn("pin skipped:", p.stderr)

    def test_pin_only_succeeds_when_the_shortcut_pins(self):
        p, runs = self.run_block(HARNESS_MODE="pin")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("pinned: yes", p.stdout)
        self.assertEqual(runs, 1)

    def test_a_fast_failure_is_still_retried_but_a_hang_is_not(self):
        """The retry exists for the index race, which fails fast.  A run that hit its bound is a hang."""
        fast, fast_runs = self.run_block(STUB_FAIL_FIRST="1")
        self.assertEqual(fast_runs, 2)
        hang, hang_runs = self.run_block(STUB_HANG="1", HARNESS_SHORTCUT_TIMEOUT_S="1")
        self.assertEqual(hang_runs, 1)

    def test_the_lock_is_given_back_after_a_pin_and_after_a_failed_pin(self):
        self.run_block()
        self.assertFalse(self.lock_left, "the lock was left behind after a pin")
        self.run_block(STUB_FAIL_FIRST="99")
        self.assertFalse(self.lock_left, "the lock was left behind after a failed pin")
        self.run_block(STUB_HANG="1", HARNESS_SHORTCUT_TIMEOUT_S="1")
        self.assertFalse(self.lock_left, "the lock was left behind after a hung pin")

    def test_a_pin_that_cannot_get_the_lock_is_skipped_and_never_runs_the_shortcut(self):
        """Another process holds the lock the whole wait:  skip with a clear line, do not stack a second run."""
        p, runs = self.run_block(PRE_LOCK="live", NOTES_PIN_LOCK_WAIT_S="1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("pin skipped: another pin or unpin run held the lock", p.stderr)
        self.assertIn("note saved unpinned", p.stderr)
        self.assertEqual(runs, 0, "the Shortcut ran although another run held the lock")
        self.assertTrue(self.lock_left, "the waiter removed a lock it did not own")

    def test_pin_only_that_cannot_get_the_lock_exits_4(self):
        p, runs = self.run_block(HARNESS_MODE="pin", PRE_LOCK="live", NOTES_PIN_LOCK_WAIT_S="1")
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
        self.assertEqual(runs, 0)

    def test_unpin_that_cannot_get_the_lock_exits_4(self):
        p, runs = self.run_unpin_block(
            HARNESS_MODE="unpin",
            STUB_NAME="Unpin Coding Note",
            PRE_LOCK="live",
            NOTES_PIN_LOCK_WAIT_S="1",
        )
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
        self.assertIn("held the lock", p.stderr)
        self.assertEqual(runs, 0)

    def test_a_lock_whose_holder_is_gone_is_taken_over(self):
        p, runs = self.run_block(PRE_LOCK="dead")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("pinned: yes", p.stdout)
        self.assertEqual(runs, 1)
        self.assertFalse(self.lock_left)

    def test_a_lock_older_than_any_run_could_be_is_taken_over_even_if_its_pid_is_alive(self):
        """A pid can be reused after a crash, so age settles it too."""
        p, runs = self.run_block(PRE_LOCK="old")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("pinned: yes", p.stdout)
        self.assertEqual(runs, 1)

    def test_a_live_lock_owned_by_another_user_is_not_taken_over(self):
        """kill -0 fails with EPERM for a live process this user may not signal, and that is not a dead holder."""
        p, runs = self.run_block(PRE_LOCK="foreign", NOTES_PIN_LOCK_WAIT_S="1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("pin skipped: another pin or unpin run held the lock", p.stderr)
        self.assertEqual(runs, 0, "a live foreign-owned lock was taken over")
        self.assertTrue(self.lock_left, "the live foreign-owned lock was removed")

    def test_the_stale_age_follows_the_shortcut_bound(self):
        """A raised NOTES_SHORTCUT_TIMEOUT_S makes a live run last longer, so the age that calls it stale grows too."""
        taken, taken_runs = self.run_block(PRE_LOCK="aged4")
        self.assertEqual(
            taken_runs, 1, "a four-minute-old lock should be stale under the default bound"
        )
        self.assertIn("pinned: yes", taken.stdout)
        kept, kept_runs = self.run_block(
            PRE_LOCK="aged4", HARNESS_SHORTCUT_TIMEOUT_S="120", NOTES_PIN_LOCK_WAIT_S="1"
        )
        self.assertEqual(kept_runs, 0, "a lock younger than a 120 s bound allows was taken over")
        self.assertIn("pin skipped: another pin or unpin run held the lock", kept.stderr)
        self.assertTrue(self.lock_left)

    def test_every_pin_skipped_reason_fits_the_consumers_cap_with_its_tail(self):
        """agent_sync.notes keeps 200 characters of the reason, and the tail says the note was saved.

        A reason that runs over loses exactly that tail, so a caller could not tell a saved, unpinned note from a
        failed write.  Every `pin skipped:` line of the real script is rendered with the longest outcome, a
        three-digit lock wait and a 100-character lock path (a long HOME), then read back through the consumer."""
        sys.path.insert(0, str(SCRIPT.parent))
        from agent_sync import notes as N

        text = SCRIPT.read_text()
        start = text.index('case "$_pin_rc" in\n  1)')
        block = text[start : text.index("\nesac\n", start)]
        lock_path = (
            "/Users/" + "u" * 20 + "/Library/Caches/apple-notes-coding/" + "p" * 31 + "/pin.lock"
        )
        self.assertGreaterEqual(len(lock_path), 100)
        reasons = re.findall(r'echo "pin skipped: ([^"]*)" >&2', block)
        self.assertGreaterEqual(
            len(reasons), 8, "a pin skipped reason is missing from the case block"
        )
        for template in reasons:
            reason = (
                template.replace("$_pin_outcome", "note saved unpinned")
                .replace("${NOTES_PIN_LOCK_DIR}", lock_path)
                .replace("${NOTES_PIN_LOCK_WAIT_S}", "999")
            )
            with self.subTest(reason=reason[:60]):
                self.assertLessEqual(len(reason), 200, reason)
                self.assertTrue(reason.endswith("note saved unpinned"), reason)
                carried = N._pin_skipped(f"pin skipped: {reason}\n".encode())
                self.assertEqual(carried, reason, "the consumer cut the reason off")

    def test_a_lock_directory_that_cannot_be_made_is_named_as_that(self):
        """Not a busy lock:  the parent of the lock directory is a file, so no run holds anything."""
        for mode, expect in (("update", 0), ("pin", 4)):
            with self.subTest(mode=mode):
                with tempfile.TemporaryDirectory() as tmp:
                    blocker = Path(tmp) / "blocker"
                    blocker.write_text("not a directory")
                    p, runs = self.run_block(
                        HARNESS_MODE=mode, NOTES_PIN_LOCK_DIR=str(blocker / "pin.lock")
                    )
                self.assertEqual(p.returncode, expect, p.stdout + p.stderr)
                self.assertIn("pin skipped: the pin lock directory", p.stderr)
                self.assertIn("could not be made", p.stderr)
                self.assertNotIn("another pin or unpin run held the lock", p.stderr)
                self.assertEqual(runs, 0)

    @unittest.skipIf(os.geteuid() == 0, "root can write into an unwritable directory")
    def test_an_unwritable_lock_parent_is_skipped_at_once_and_named_as_that(self):
        """Not contention:  mkdir fails with no lock there, so waiting the full lock wait would only stall."""
        for mode, expect in (("update", 0), ("pin", 4)):
            with self.subTest(mode=mode):
                with tempfile.TemporaryDirectory() as tmp:
                    parent = Path(tmp) / "readonly"
                    parent.mkdir(mode=0o500)
                    try:
                        started = time.monotonic()
                        #  real sleeps, so a wait that is not skipped would take the whole 20 s
                        p, runs = self.run_block(
                            HARNESS_MODE=mode,
                            HARNESS_REAL_SLEEP="1",
                            NOTES_PIN_LOCK_DIR=str(parent / "pin.lock"),
                        )
                        elapsed = time.monotonic() - started
                    finally:
                        parent.chmod(0o700)
                self.assertEqual(p.returncode, expect, p.stdout + p.stderr)
                self.assertIn("pin skipped: the pin lock directory", p.stderr)
                self.assertIn("could not be made", p.stderr)
                self.assertNotIn("another pin or unpin run held the lock", p.stderr)
                self.assertLess(elapsed, 10, "the lock wait was not skipped")
                self.assertEqual(runs, 0)

    def test_unpin_names_a_lock_directory_that_cannot_be_made(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "blocker"
            blocker.write_text("not a directory")
            p, runs = self.run_unpin_block(
                HARNESS_MODE="unpin",
                STUB_NAME="Unpin Coding Note",
                NOTES_PIN_LOCK_DIR=str(blocker / "pin.lock"),
            )
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
        self.assertIn("could not be made", p.stderr)
        self.assertEqual(runs, 0)

    def test_the_lock_directory_is_private_to_the_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / "lock-mode"
            #  a permissive umask is the case that matters:  mkdir -m must not depend on it
            p, runs = self.run_block(STUB_LOCK_MODE_FILE=str(record), HARNESS_UMASK="000")
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(record.read_text().strip(), "drwx------")

    def test_a_lock_that_keeps_coming_back_stale_is_given_up_on(self):
        """Takeovers take no time, so the wait counter alone never stopped a lock recreated dead every time."""
        source = SCRIPT.read_text()
        function, block = _extract(source)
        #  every lock reads as stale, and moving one aside puts a new one in its place
        function += "\n_pin_lock_stale() { return 0; }\n"
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            bindir.mkdir(mode=0o700)
            real_mv = shutil.which("mv")
            for name, body in (
                ("shortcuts", STUB),
                ("pgrep", PGREP_STUB),
                ("open", OPEN_STUB),
                ("mv", f'#!/bin/bash\n{real_mv} "$@" && mkdir -m 700 -- "$1"\n'),
            ):
                side = bindir / name
                side.write_text(body)
                side.chmod(0o700)
            (Path(tmp) / "pin.lock").mkdir()
            script = Path(tmp) / "harness.sh"
            script.write_text(
                HARNESS.replace("{constants}", "")
                .replace("{function}", function)
                .replace("{block}", block)
            )
            child = dict(os.environ)
            child.update(
                PATH=f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                STUB_COUNTER=str(Path(tmp) / "counter"),
                NOTES_PIN_LOCK_DIR=str(Path(tmp) / "pin.lock"),
                HARNESS_MODE="pin",
            )
            started = time.monotonic()
            p = subprocess.run(
                ["bash", str(script)],
                capture_output=True,
                text=True,
                env=child,
                timeout=30,
                check=False,
            )
            self.assertLess(time.monotonic() - started, 20, "the lock loop did not give up")
            self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
            self.assertIn("pin skipped: the pin lock kept coming back stale", p.stderr)
            self.assertNotIn("another pin or unpin run held the lock", p.stderr)
            self.assertFalse((Path(tmp) / "counter").exists(), "the Shortcut ran without the lock")

    def test_two_pins_at_once_never_overlap(self):
        """The reason for the lock:  overlapping runs fight over the Find Notes picker and fail each other."""
        source = SCRIPT.read_text()
        function, block = _extract(source)
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            bindir.mkdir(mode=0o700)
            for name, body in (("shortcuts", STUB), ("pgrep", PGREP_STUB), ("open", OPEN_STUB)):
                side = bindir / name
                side.write_text(body)
                side.chmod(0o700)
            script = Path(tmp) / "harness.sh"
            script.write_text(
                HARNESS.replace("{constants}", "")
                .replace("{function}", function)
                .replace("{block}", block)
            )
            counter = Path(tmp) / "counter"
            child = dict(os.environ)
            child.update(
                PATH=f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                STUB_COUNTER=str(counter),
                STUB_HOLD="1",
                NOTES_PIN_LOCK_DIR=str(Path(tmp) / "pin.lock"),
                HARNESS_MODE="pin",
                HARNESS_REAL_SLEEP="1",
            )
            procs = [
                subprocess.Popen(
                    ["bash", str(script)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=child,
                )
                for _ in range(2)
            ]
            try:
                results = [proc.communicate(timeout=60) for proc in procs]
            except BaseException:
                #  a timeout or an interrupt must not leave the shells running after the test:  stop and
                #  reap every one, kill the ones that do not stop, then let the original exception go
                for proc in procs:
                    if proc.poll() is None:
                        proc.terminate()
                for proc in procs:
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                raise
            self.assertEqual([proc.returncode for proc in procs], [0, 0], results)
            self.assertTrue(all("pinned: yes" in out for out, _ in results), results)
            self.assertEqual(int(counter.read_text()), 2)
            self.assertFalse(
                Path(str(counter) + ".overlap").exists(), "two Shortcut runs overlapped"
            )
            self.assertFalse((Path(tmp) / "pin.lock").exists())

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

    def test_a_retry_falls_back_to_a_stripped_prefix_when_the_title_starts_with_one(self):
        """A note whose user-given title starts with a non-alphanumeric run (an emoji pin glyph,
        a bullet, a leading space) is the same note once that run is dropped, because Apple
        Notes' contains-match is a substring check.  The first attempt passes the full title;
        the retry rewrites the input to the stripped-prefix form so the Shortcut's Find Notes
        action can return the same note.  This avoids the only path that has worked so far for
        those notes:  --pin, which steals focus and pops a note picker on the owner's screen.
        """
        title = "[FLEET, Oracle] \u2b50\ufe0f Background Jobs Master List"
        p, runs = self.run_block(HARNESS_TITLE=title, STUB_FAIL_FIRST="1", STUB_RECORD_INPUT="1")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("pinned: yes", p.stdout)
        self.assertEqual(runs, 2, "the failed first attempt was not retried")
        self.assertEqual(
            self.inputs,
            [title, "Background Jobs Master List"],
            "attempt 1 should pass the full title and attempt 2 the stripped-prefix form",
        )

    def test_a_retry_keeps_the_full_title_when_there_is_no_leading_non_alnum_run(self):
        """The stripped-prefix rewrite is a no-op when the title starts with an alphanumeric
        character, so the input the Shortcut sees is identical on both attempts and a reviewer
        can read the diff against a title that has no prefix and see no behaviour change.
        """
        p, runs = self.run_block(
            HARNESS_TITLE="AFC test note", STUB_FAIL_FIRST="1", STUB_RECORD_INPUT="1"
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(runs, 2, "the failed first attempt was not retried")
        self.assertEqual(
            self.inputs,
            ["AFC test note", "AFC test note"],
            "a title with no leading non-alnum run should not be rewritten on retry",
        )


#  a stand-in for `osascript`:  it records every call (the arguments, WANT_ACTIVATE and the AppleScript on
#  stdin) and answers with a note id, so the whole script runs without Notes being touched
OSASCRIPT_STUB = """#!/bin/bash
n=0
if [ -f "$STUB_OSA/count" ]; then n=$(cat "$STUB_OSA/count"); fi
n=$((n+1))
printf '%s' "$n" > "$STUB_OSA/count"
{
  printf 'ARGV: %s\\n' "$*"
  printf 'WANT_ACTIVATE=%s\\n' "${WANT_ACTIVATE-unset}"
  cat
} > "$STUB_OSA/call.$n"
printf 'x-coredata://stub/note/1\\n'
"""

#  `sleep` returns at once, so the 2 s settle and the retry pause cost nothing
SLEEP_STUB = """#!/bin/bash
exit 0
"""


@unittest.skipUnless(
    SCRIPT.is_file() and Path("/usr/bin/python3").exists(), "needs the script and /usr/bin/python3"
)
class NoGuiPinTests(unittest.TestCase):
    """The whole script, with `osascript`, `shortcuts`, `open`, `pgrep` and `sleep` stubbed on PATH.

    What the assertions read is what reached the stubs.  `osascript` is the only way this script could
    reach the Notes window, so a count of its calls and a look at their text show whether anything beyond
    the note write and the id lookup was ever attempted.
    """

    def run_script(
        self, *args: str, **env: str
    ) -> tuple[subprocess.CompletedProcess, list[str], int]:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            bindir.mkdir(mode=0o700)
            osa = Path(tmp) / "osa"
            osa.mkdir(mode=0o700)
            for name, body in (
                ("osascript", OSASCRIPT_STUB),
                ("shortcuts", STUB),
                ("pgrep", PGREP_STUB),
                ("open", OPEN_STUB),
                ("sleep", SLEEP_STUB),
            ):
                side = bindir / name
                side.write_text(body)
                side.chmod(0o700)
            counter = Path(tmp) / "counter"
            child = {
                "PATH": f"{bindir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
                "HOME": tmp,
                "LANG": "en_US.UTF-8",
                "AGENT_SEAT": "CLAUDE",
                "STUB_COUNTER": str(counter),
                "STUB_OSA": str(osa),
                "NOTES_PIN_LOCK_DIR": str(Path(tmp) / "pin.lock"),
                #  one attempt unless a test asks for the retry:  the stub fails every run
                "APPLE_NOTES_PIN_RETRY": "0",
            }
            child.update(env)
            p = subprocess.run(
                ["bash", str(SCRIPT), *args],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                env=child,
                timeout=60,
                check=False,
            )
            calls = [
                (osa / f"call.{n}").read_text()
                for n in range(
                    1, (int((osa / "count").read_text()) if (osa / "count").exists() else 0) + 1
                )
            ]
            runs = int(counter.read_text()) if counter.exists() else 0
            return p, calls, runs

    def assert_no_window_automation(self, calls: list[str]) -> None:
        for call in calls:
            for needle in ("System Events", "show note", "click", "menu item", 'process "Notes"'):
                self.assertNotIn(needle, call)

    def test_create_with_a_failing_shortcut_succeeds_unpinned_and_never_touches_the_window(self):
        p, calls, runs = self.run_script(
            "--session",
            "test-session-1234",
            "[AFC, Claude] topic",
            "Body text.",
            STUB_FAIL_FIRST="99",
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("created note id=", p.stdout)
        self.assertIn("pin skipped: Pin Coding Note Shortcut failed; note saved unpinned", p.stderr)
        self.assertEqual(runs, 1, "the Shortcut should have been tried once")
        self.assertEqual(len(calls), 1, "osascript ran more than the one note write: %r" % calls)
        self.assertIn("WANT_ACTIVATE=0", calls[0])
        self.assert_no_window_automation(calls)

    def test_update_with_a_failing_shortcut_succeeds_unpinned_and_never_touches_the_window(self):
        p, calls, _ = self.run_script(
            "--update",
            "--session",
            "test-session-1234",
            "[AFC, Claude] topic",
            "Body text.",
            STUB_FAIL_FIRST="99",
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("updated note id=", p.stdout)
        self.assertIn("pin skipped: Pin Coding Note Shortcut failed; note saved unpinned", p.stderr)
        self.assertEqual(len(calls), 1, "osascript ran more than the one note write: %r" % calls)
        self.assertIn("WANT_ACTIVATE=0", calls[0])
        self.assert_no_window_automation(calls)

    def test_pin_only_with_a_failing_shortcut_exits_4_and_only_looks_the_note_up(self):
        p, calls, _ = self.run_script("--pin-only", "[AFC, Claude] topic", STUB_FAIL_FIRST="99")
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
        self.assertIn("pin skipped: Pin Coding Note Shortcut failed; nothing was pinned", p.stderr)
        self.assertEqual(len(calls), 1, "osascript ran more than the id lookup: %r" % calls)
        self.assertIn("get id of note", calls[0])
        self.assert_no_window_automation(calls)

    def test_pin_only_with_a_missing_shortcut_exits_4_and_never_touches_the_window(self):
        p, calls, runs = self.run_script("--pin-only", "[AFC, Claude] topic", STUB_INSTALLED="0")
        self.assertEqual(p.returncode, 4, p.stdout + p.stderr)
        self.assertEqual(runs, 0)
        self.assertEqual(len(calls), 1, calls)

    def test_pin_only_that_pins_exits_0(self):
        p, calls, runs = self.run_script("--pin-only", "[AFC, Claude] topic")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("pinned: yes", p.stdout)
        self.assertEqual(runs, 1)
        self.assertEqual(len(calls), 1, calls)

    def test_the_old_pin_switches_still_parse_and_no_longer_reach_a_gui_path(self):
        """--pin and APPLE_NOTES_PIN=1 used to switch on the menu-click fallback.  They are accepted and do nothing more."""
        for args, env in (
            (("--pin",), {}),
            ((), {"APPLE_NOTES_PIN": "1"}),
            (("--pin",), {"APPLE_NOTES_PIN": "1"}),
        ):
            with self.subTest(args=args, env=env):
                p, calls, _ = self.run_script(
                    *args,
                    "--session",
                    "test-session-1234",
                    "[AFC, Claude] topic",
                    "Body text.",
                    STUB_FAIL_FIRST="99",
                    **env,
                )
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn("pin skipped:", p.stderr)
                self.assertEqual(
                    len(calls), 1, "a second osascript call reached for the window: %r" % calls
                )
                self.assert_no_window_automation(calls)

    def test_activate_is_still_the_one_explicit_way_to_bring_notes_forward(self):
        p, calls, _ = self.run_script(
            "--activate",
            "--session",
            "test-session-1234",
            "[AFC, Claude] topic",
            "Body text.",
            STUB_FAIL_FIRST="99",
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(calls), 1, calls)
        self.assertIn("WANT_ACTIVATE=1", calls[0])
        env_p, env_calls, _ = self.run_script(
            "--session",
            "test-session-1234",
            "[AFC, Claude] topic",
            "Body text.",
            STUB_FAIL_FIRST="99",
            APPLE_NOTES_ACTIVATE="1",
        )
        self.assertEqual(env_p.returncode, 0, env_p.stderr)
        self.assertIn("WANT_ACTIVATE=1", env_calls[0])

    def test_pinning_disabled_leaves_create_quiet_and_still_exits_0(self):
        p, calls, runs = self.run_script(
            "--session",
            "test-session-1234",
            "[AFC, Claude] topic",
            "Body text.",
            APPLE_NOTES_PIN="0",
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("pinning is disabled", p.stdout)
        self.assertEqual(p.stderr, "")
        self.assertEqual(runs, 0)
        self.assertEqual(len(calls), 1, calls)

    def test_the_script_carries_no_window_automation_on_the_pin_path(self):
        """Read the source, minus comments:  nothing here may reach the Notes window except the opt-in."""
        text = SCRIPT.read_text()
        code = "\n".join(line for line in text.split("\n") if not line.lstrip().startswith("#"))
        for needle in (
            "System Events",
            "show note id",
            "PIN_RESULT",
            "menu item",
            "pin-menu-not-found",
        ):
            self.assertNotIn(needle, code)
        #  the pin block is the end of the script and holds no osascript at all
        pin_block = text[text.index("# Set APPLE_NOTES_PIN=0 to skip pinning entirely.") :]
        self.assertNotIn("osascript", pin_block)
        self.assertNotIn(
            "activate",
            "\n".join(l for l in pin_block.split("\n") if not l.lstrip().startswith("#")),
        )
        #  every show or activate left in the script sits right under the WANT_ACTIVATE opt-in
        lines = code.split("\n")
        found = 0
        for index, line in enumerate(lines):
            if re.match(r"^\s*(show|activate)\b", line):
                found += 1
                window = "\n".join(lines[max(0, index - 3) : index])
                self.assertIn('(system attribute "WANT_ACTIVATE") is "1"', window, line)
        self.assertEqual(
            found, 4, "expected the create and update opt-ins only (a show and an activate in each)"
        )


if __name__ == "__main__":
    unittest.main()
