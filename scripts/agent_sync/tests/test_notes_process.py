"""AFC-448:  a timeout on the Notes helper has to take its children with it.

The caller starts the helper and, when the cap is reached, kills it.  A killed helper that
leaves a stuck `shortcuts` child behind keeps the Shortcuts app busy long after the caller has
given up and reported an unknown outcome,  so the helper leads its own session and the whole
process group goes down together.

The keyword arguments are the ones `subprocess.run` takes,  so a test that stands in for the
helper keeps the same shape and only the real path changes.

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

#  /bin and /usr/bin both,  because `cat` and `sleep` live in /bin on macOS
PATH = "/usr/bin:/bin"


class ProcessGroupTests(unittest.TestCase):
    """A timeout takes the helper's whole process tree with it."""

    def test_a_timeout_kills_the_children_too(self) -> None:
        """The helper's child must not outlive the cap.

        The helper reports its child's pid,  so the test can check that the child is gone once
        the cap has fired.  Without the group kill the child is orphaned and keeps running.
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
                    input=b"",
                    env={"PATH": PATH},
                    timeout=1.0,
                )
            said = (caught.exception.stdout or b"").decode()
            self.assertTrue(said.strip(), "the helper did not report its child's pid")
            child = int(said.strip().splitlines()[0])
            self.assertTrue(
                _gone(child), "the helper's child was still running after the timeout"
            )

    def test_a_run_that_finishes_is_not_killed(self) -> None:
        """The ordinary path is untouched:  the helper runs,  its output comes back,  and it ends."""
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
                input=b"hello",
                env={"PATH": PATH},
                timeout=30.0,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(out.read_text(), "hello")

    def test_the_timeout_output_is_not_duplicated(self) -> None:
        """A second communicate() returns everything,  so it replaces rather than adds.

        Concatenating it onto what the timeout captured would print the helper's output twice,
        and a caller that reports the helper's own words would report them doubled.
        """
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "helper.sh"
            script.write_text(
                "#!/bin/bash\n"
                "printf 'the-helper-said-this\\n'\n"
                "sleep 300\n",
                encoding="utf-8",
            )
            script.chmod(0o700)
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                N._run_helper(
                    [str(script)],
                    input=b"",
                    env={"PATH": PATH},
                    timeout=1.0,
                )
            said = (caught.exception.stdout or b"").decode()
            self.assertEqual(
                said.count("the-helper-said-this"),
                1,
                "the helper's output was duplicated: %r" % said,
            )

    def test_the_group_is_killed_when_the_run_is_interrupted(self) -> None:
        """The group kill is what an interruption relies on,  so it has to work on its own.

        The helper leads its own session,  so a signal aimed at the caller never reaches it.
        An interruption that did not take the group with it would leave the helper and its
        children running.
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
            proc = subprocess.Popen(
                [str(script)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env={"PATH": PATH},
                start_new_session=True,
            )
            said = proc.stdout.readline().decode().strip()
            self.assertTrue(said, "the helper did not report its child's pid")
            child = int(said)
            N._kill_group(proc)
            proc.wait(timeout=10)
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                if pipe is not None:
                    pipe.close()
            self.assertTrue(
                _gone(child), "the helper's child survived the group kill"
            )

    def test_a_caller_supplied_runner_keeps_the_subprocess_run_shape(self) -> None:
        """The seam the tests stand in for has to keep working,  with the same keywords."""
        seen = {}

        def runner(argv, **kwargs):
            seen.update(kwargs)
            return subprocess.CompletedProcess(argv, 0, b"created note id=x\n", b"")

        proc = N._run_helper(
            ["/fake/apple-notes-coding.sh"],
            runner=runner,
            input=b"body",
            capture_output=True,
            timeout=5.0,
            env={"PATH": PATH},
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(seen["input"], b"body")
        self.assertEqual(seen["timeout"], 5.0)
        self.assertTrue(seen["capture_output"])
        self.assertFalse(seen["check"])


def _gone(pid: int) -> bool:
    """True when the pid is gone.  A killed child can take a moment to be reaped."""
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.1)
    return False


if __name__ == "__main__":
    unittest.main()
