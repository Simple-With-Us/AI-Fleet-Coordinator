"""AFC-436:  the Notes caller must not turn one large write into a stalled fleet,  and a timeout
must take the whole process tree with it.

Two things changed on the caller side of the Notes helper,  and both are about what happens when
a note is slow rather than about what happens when it is quick.

The lock.  Notes.app is a single shared document store,  so the helper runs one at a time.  The
cap on a run is long,  because a body the size of the master list needs minutes,  and a second
request that queued behind it would wait for the whole run before it was even started.  The
wait is now bounded,  and a request that cannot get the lock in time is told the Mac is busy.

The process group.  A timeout used to kill the helper and leave its children running:  a stuck
`shortcuts` child would keep the Shortcuts app busy long after the caller had given up and
reported an unknown outcome.  The helper now leads its own session,  and the timeout takes the
whole group with it.

Run:  cd scripts && python3 -m unittest agent_sync.tests.test_notes_process
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from agent_sync import notes as N

TITLE = "[AFC, Claude] process check"
SCRIPT = Path("/fake/apple-notes-coding.sh")
ENV = {
    "HOME": "/h",
    "PATH": "/usr/bin",
    "AGENT_SYNC_STATE_DIR": "/s",
}


def envelope(action="create", title=TITLE, markdown="body"):
    return N.make_envelope(action, title, markdown, {}, "requestid-0001", "CLAUDE")


class LockBoundTests(unittest.TestCase):
    """A queued request waits for a bound and is then refused as busy."""

    def test_a_request_that_cannot_get_the_lock_is_refused_as_busy(self) -> None:
        held = N._RUN_LOCK.acquire(blocking=False)
        self.assertTrue(held, "the lock was already held by another test")
        original = N.LOCK_WAIT_S
        N.LOCK_WAIT_S = 0.2
        try:
            with self.assertRaises(N.NotesError) as caught:
                N.run_direct(envelope(), SCRIPT, env=ENV, runner=_never_runs)
        finally:
            N.LOCK_WAIT_S = original
            N._RUN_LOCK.release()
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertIn("busy", caught.exception.message)

    def test_the_refusal_comes_quickly_and_not_after_the_whole_cap(self) -> None:
        """The wait is bounded by LOCK_WAIT_S,  not by the run that holds the lock.

        A cap of 1080 s would otherwise be the wait,  and a caller would sit there for eighteen
        minutes before it was told anything.
        """
        held = N._RUN_LOCK.acquire(blocking=False)
        self.assertTrue(held)
        original = N.LOCK_WAIT_S
        N.LOCK_WAIT_S = 0.3
        try:
            started = time.monotonic()
            with self.assertRaises(N.NotesError):
                N.run_direct(envelope(), SCRIPT, env=ENV, runner=_never_runs)
            elapsed = time.monotonic() - started
        finally:
            N.LOCK_WAIT_S = original
            N._RUN_LOCK.release()
        self.assertLess(elapsed, 5.0, "the refusal waited far longer than the bound")

    def test_the_lock_is_released_after_a_run(self) -> None:
        """A failed run must not leave the lock held,  or every later request is refused."""
        with self.assertRaises(N.NotesError):
            N.run_direct(
                envelope(),
                SCRIPT,
                env=ENV,
                runner=_fails,
            )
        self.assertFalse(
            N._RUN_LOCK.locked(), "the lock was still held after the run finished"
        )

    def test_the_lock_is_released_after_a_timeout(self) -> None:
        """A timeout is an unknown outcome,  but it still has to give the lock back."""
        with self.assertRaises(N.NotesError) as caught:
            N.run_direct(envelope(), SCRIPT, env=ENV, runner=_times_out)
        self.assertEqual(caught.exception.code, "outcome_unknown")
        self.assertFalse(
            N._RUN_LOCK.locked(), "the lock was still held after the timeout"
        )


class ProcessGroupTests(unittest.TestCase):
    """A timeout takes the helper's whole process tree with it."""

    def test_a_timeout_kills_the_children_too(self) -> None:
        """A stuck `shortcuts` child must not outlive the caller.

        The helper leads its own session,  so the group is the helper and everything it started.
        The child reports its own pid,  so the test can check that it is gone.
        """
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "helper.sh"
            script.write_text(
                "#!/bin/bash\n"
                "sleep 300 &\n"
                'printf "%s\\n" "$!"\n'
                "sleep 300\n",
                encoding="utf-8",
            )
            script.chmod(0o700)
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                N._run_helper(
                    [str(script)],
                    stdin_data=b"",
                    env={**ENV, "PATH": "/usr/bin:/bin"},
                    timeout=1.0,
                    runner=None,
                )
            said = (caught.exception.stdout or b"").decode()
            child = int(said.strip().splitlines()[0])
            self.assertTrue(
                _gone(child), "the helper's child was still running after the timeout"
            )

    def test_a_run_that_finishes_is_not_killed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            script = Path(tmp) / "helper.sh"
            script.write_text(
                "#!/bin/bash\n"
                'cat > "%s"\n' % out,
                encoding="utf-8",
            )
            script.chmod(0o700)
            proc = N._run_helper(
                [str(script)],
                stdin_data=b"hello",
                env={**ENV, "PATH": "/usr/bin:/bin"},
                timeout=30.0,
                runner=None,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(out.read_text(), "hello")


def _gone(pid: int) -> bool:
    """True when the pid is gone.  A killed child can take a moment to be reaped."""
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.1)
    return False


def _never_runs(argv, **kwargs):
    raise AssertionError("the helper must not run when the lock could not be taken")


def _fails(argv, **kwargs):
    return subprocess.CompletedProcess(argv, 9, b"", b"boom")


def _times_out(argv, **kwargs):
    raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 1))


if __name__ == "__main__":
    unittest.main()
