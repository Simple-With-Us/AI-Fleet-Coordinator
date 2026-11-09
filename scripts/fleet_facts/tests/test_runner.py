"""The subprocess wrapper:  argv lists only, timeouts kill the process group, a missing program is a result."""
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fleet_facts import runner as R


class Run(unittest.TestCase):
    def test_a_normal_run(self):
        result = R.run([sys.executable, "-c", "import sys; print('hi'); sys.stderr.write('e'); sys.exit(3)"], timeout=30)
        self.assertEqual((result.rc, result.out.strip(), result.err, result.reason, result.ok), (3, "hi", "e", None, False))
        self.assertTrue(R.run([sys.executable, "-c", "pass"], timeout=30).ok)

    def test_a_timeout_kills_the_whole_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "child-alive"
            child = "import time, pathlib; time.sleep(3); pathlib.Path(%r).write_text('x')" % str(marker)
            parent = "\n".join(["import subprocess, sys, time",
                                "subprocess.Popen([sys.executable, '-c', %r])" % child,
                                "time.sleep(60)"])
            start = time.monotonic()
            result = R.run([sys.executable, "-c", parent], timeout=1)
            self.assertLess(time.monotonic() - start, 20)
            self.assertIsNone(result.rc)
            self.assertIn("timed out", result.reason)
            time.sleep(4)
            self.assertFalse(marker.exists(), "the grandchild survived the timeout")

    def test_missing_and_unrunnable_programs_are_results(self):
        self.assertEqual(R.run(["/no/such/program"], timeout=5).reason, "not found")
        with tempfile.TemporaryDirectory() as tmp:
            plain = Path(tmp) / "plain"
            plain.write_text("x")
            self.assertEqual(R.run([str(plain)], timeout=5).reason, "not executable")

    def test_argv_must_be_a_list(self):
        for bad in ("ls -l", b"ls", [], None):
            with self.assertRaises(TypeError):
                R.run(bad, timeout=5)

    def test_no_shell_is_involved(self):
        result = R.run([sys.executable, "-c", "import sys; print(sys.argv[1])", "$(echo injected); `x`"], timeout=30)
        self.assertEqual(result.out.strip(), "$(echo injected); `x`")

    def test_env_is_merged_and_prompts_are_off(self):
        result = R.run([sys.executable, "-c", "import os; print(os.environ['GH_PROMPT_DISABLED'], os.environ['FF_X'])"],
                       timeout=30, env={"FF_X": "1"})
        self.assertEqual(result.out.split(), ["1", "1"])

    def test_stdin_is_closed(self):
        result = R.run([sys.executable, "-c", "import sys; print(repr(sys.stdin.read()))"], timeout=30)
        self.assertEqual(result.out.strip(), "''")


class Which(unittest.TestCase):
    def test_finds_on_path_and_in_the_extra_places(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            tool = home / ".local" / "bin" / "example-tool"
            tool.parent.mkdir(parents=True)
            tool.write_text("#!/bin/sh\n")
            tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
            with mock.patch.dict(os.environ, {"HOME": str(home), "PATH": "/usr/bin:/bin"}):
                self.assertEqual(R.which("example-tool"), str(tool))
                self.assertIsNone(R.which("no-such-tool-xyz"))
                self.assertEqual(R.which(str(tool)), str(tool))
                self.assertIsNone(R.which(str(tool) + "-missing"))


class Shapes(unittest.TestCase):
    def test_the_three_answers(self):
        self.assertEqual(R.ok({"a": 1}), {"status": "ok", "reason": None, "data": {"a": 1}})
        self.assertEqual(R.absent("why"), {"status": "absent", "reason": "why", "data": None})
        self.assertEqual(R.failed("bad", {"p": 1}), {"status": "failed", "reason": "bad", "data": {"p": 1}})


if __name__ == "__main__":
    unittest.main()
