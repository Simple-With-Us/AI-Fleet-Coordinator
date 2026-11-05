"""Tests for the Mac coding-priority launcher.

No network, no Infisical, no launchd.  The one live case runs a throwaway
script of this test's own under the clamp and reads the nice value and the
scheduling priority back, so "the control is in place" is measured here
rather than asserted from the source.
"""

import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "mac-coding-priority.py"


def load():
    spec = importlib.util.spec_from_file_location("mac_coding_priority", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


prio = load()


class EntrypointAllowlist(unittest.TestCase):
    def test_coding_build_and_test_tools_are_allowed(self):
        for entry in (
            "npm",
            "npx",
            "pnpm",
            "yarn",
            "xcodebuild",
            "swift",
            "cargo",
            "go",
            "pytest",
            "make",
            "tsc",
        ):
            allowed, reason, _ = prio.check_entrypoint([entry, "--version"])
            self.assertTrue(allowed, f"{entry} must be allowed: {reason}")

    def test_a_bare_runtime_is_not_allowed(self):
        # The review ruled that matching a runtime name cannot tell a coding
        # worker from a protected service, so no bare interpreter is here.
        for entry in ("node", "python", "python3", "deno"):
            allowed, reason, code = prio.check_entrypoint([entry, "-e", "x"])
            self.assertFalse(allowed, f"{entry} must not be allowed")
            self.assertEqual(code, prio.EXIT_NOT_ALLOWED)
            self.assertIn("not an allowlisted", reason)

    def test_a_gui_launch_is_refused(self):
        allowed, reason, code = prio.check_entrypoint(["open", "-a", "Safari"])
        self.assertFalse(allowed)
        self.assertEqual(code, prio.EXIT_INTERACTIVE)
        self.assertIn("foreground app is exempt", reason)

    def test_a_shell_needs_a_script_file(self):
        allowed, reason, code = prio.check_entrypoint(["bash"])
        self.assertFalse(allowed)
        self.assertEqual(code, prio.EXIT_INTERACTIVE)
        with tempfile.NamedTemporaryFile(suffix=".sh") as handle:
            allowed, reason, code = prio.check_entrypoint(["bash", handle.name])
            self.assertTrue(allowed, reason)
            allowed, reason, code = prio.check_entrypoint(["bash", "-c", "echo hi"])
            self.assertFalse(allowed)
            self.assertEqual(code, prio.EXIT_INTERACTIVE)


class ClampCheck(unittest.TestCase):
    def test_the_utility_clamp_is_accepted(self):
        ok, detail = prio.clamp_applied((10, 20), 10, 20)
        self.assertTrue(ok, detail)
        self.assertIn("pri=20", detail)

    def test_a_normal_process_is_refused(self):
        ok, detail = prio.clamp_applied((10, 31), 10, 20)
        self.assertFalse(ok)
        self.assertIn("still normal", detail)

    def test_the_wrong_nice_value_is_refused(self):
        # Less deprioritised than asked for is the direction that has to fail
        # closed: the job would run at MORE priority than the request wanted.
        ok, detail = prio.clamp_applied((0, 20), 10, 20)
        self.assertFalse(ok)
        self.assertIn("nice is 0", detail)

    def test_a_nice_value_further_deprioritised_than_asked_is_accepted(self):
        # The caller may already be nicer than the target, so the job can land
        # at a larger nice value than requested.  That is safe: the job is at
        # least as deprioritised as asked for.
        ok, detail = prio.clamp_applied((20, 20), 10, 20)
        self.assertTrue(ok, detail)

    def test_the_darwin_background_band_is_never_accepted(self):
        # The band is the check, and it is checked on EVERY reading.  An
        # operator who asked for pri=4 must not be able to accept the Darwin
        # background clamp: it is the one that starved
        # com.jay.grok-idle-unload, so pri=4 is refused whatever was expected.
        ok, detail = prio.clamp_applied((10, 4), 10, 4)
        self.assertFalse(ok, "the Darwin background clamp must never be accepted")
        self.assertIn("not the utility clamp", detail)

    def test_a_priority_inside_the_utility_band_is_accepted(self):
        # The band is what decides, so a macOS that reports the utility clamp
        # as a slightly different number still passes, and the difference is
        # recorded rather than hidden.
        ok, detail = prio.clamp_applied((10, 21), 10, 20)
        self.assertTrue(ok, detail)
        self.assertIn("pri=21", detail)

    def test_darwin_background_is_refused(self):
        # pri=4 is the Darwin background flag.  It is the one that starved
        # com.jay.grok-idle-unload, so it must never pass as the utility clamp.
        ok, detail = prio.clamp_applied((10, 4), 10, 20)
        self.assertFalse(ok)
        self.assertIn("not the utility clamp", detail)

    def test_an_unreadable_priority_is_refused(self):
        ok, detail = prio.clamp_applied(None, 10, 20)
        self.assertFalse(ok)
        self.assertIn("could not be read", detail)

    def test_expecting_the_normal_priority_still_fails_closed(self):
        ok, detail = prio.clamp_applied((0, 31), 0, 31)
        self.assertFalse(ok, "an operator must not be able to accept an unclamped process")


class RelativeNice(unittest.TestCase):
    """`nice -n N` is an INCREMENT, so the absolute target has to be computed.

    The helper is meant to be launched by things that are already
    deprioritised -- a coding worker, a launchd job -- so it has to work
    when the caller is not at nice 0.
    """

    def test_the_increment_is_the_difference_to_the_absolute_target(self):
        self.assertEqual(prio.relative_nice_increment(0, 10), 10)
        self.assertEqual(prio.relative_nice_increment(5, 10), 5)
        self.assertEqual(prio.relative_nice_increment(10, 10), 0)

    def test_a_caller_already_nicer_than_the_target_keeps_its_value(self):
        # The target is a floor.  Asking for a smaller increment would RAISE
        # the process's priority, which needs privileges this helper does not
        # have and must not ask for.
        self.assertEqual(prio.relative_nice_increment(20, 10), 0)
        self.assertEqual(prio.relative_nice_increment(15, 10), 0)

    def test_a_nice_request_above_the_host_ceiling_is_refused(self):
        # macOS will not set a nice value above 20, so a request for one
        # cannot be honoured and must be refused rather than silently clamped.
        self.assertEqual(prio.MAX_NICE_VALUE, 20)


class Receipts(unittest.TestCase):
    def test_a_receipt_never_carries_the_command_line(self):
        with tempfile.TemporaryDirectory() as scratch:
            log = Path(scratch) / "receipts.jsonl"
            original = prio.LOG_FILE
            prio.LOG_FILE = log
            try:
                prio.append_receipt({"label": "x", "status": "ran"})
            finally:
                prio.LOG_FILE = original
            text = log.read_text(encoding="utf-8")
            self.assertNotIn("argv", text)
            self.assertNotIn("command", text)
            self.assertEqual(json.loads(text.strip())["status"], "ran")


def code_strings(source):
    """Every string literal in the source that is NOT a docstring.

    A docstring is prose about the rules; a literal in the code is something
    the program can actually do.  The guards below are about the second kind.
    """
    tree = ast.parse(source)
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docstrings.add(doc)
    literals = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value not in docstrings:
                literals.append(node.value)
    return literals


class SourceGuards(unittest.TestCase):
    def setUp(self):
        self.source = SCRIPT.read_text(encoding="utf-8")
        self.literals = code_strings(self.source)

    def test_it_never_scans_the_process_table(self):
        for forbidden in ("pgrep", "ps aux", "ps -A", "ps -e"):
            self.assertNotIn(forbidden, self.source, f"{forbidden} would match by name, not by pid")
        for literal in self.literals:
            self.assertNotIn("pgrep", literal)

    def test_the_priority_read_is_always_for_one_pid(self):
        self.assertIn('"-p", str(pid)', self.source)

    def test_it_never_terminates_anything(self):
        # No literal that names a termination tool, and no call into the
        # process-killing corners of the standard library.
        for literal in self.literals:
            for forbidden in ("pkill", "killall", "launchctl", "SIGTERM", "SIGKILL"):
                self.assertNotIn(forbidden, literal, f"{forbidden} is out of scope for this helper")
        tree = ast.parse(self.source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                self.assertNotIn(
                    node.attr,
                    {"kill", "killpg", "system"},
                    f"{node.attr} would act on a process this helper does not own",
                )
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name, "signal", "signal is out of scope")

    def test_it_never_purges_memory(self):
        for literal in self.literals:
            for forbidden in ("purge", "optimize ram", "killall"):
                self.assertNotIn(forbidden, literal)


class LiveCanary(unittest.TestCase):
    """The clamp has to be measured on this host, not assumed."""

    def setUp(self):
        if not prio._find_tool("taskpolicy") or not prio._find_tool("nice"):
            self.skipTest("taskpolicy or nice is not on this host")

    def test_a_launched_job_runs_at_the_utility_clamp(self):
        with tempfile.TemporaryDirectory() as scratch:
            script = Path(scratch) / "canary.sh"
            script.write_text(
                '#!/bin/bash\n/bin/ps -o ni=,pri= -p $$ | tr -d " " > "$1"\n',
                encoding="utf-8",
            )
            script.chmod(0o755)
            out = Path(scratch) / "observed.txt"
            log = Path(scratch) / "receipts.jsonl"
            env = dict(os.environ, MAC_CODING_PRIORITY_LOG=str(log))
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--label",
                    "canary",
                    "--",
                    "bash",
                    str(script),
                    str(out),
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            observed = out.read_text(encoding="utf-8").strip()
            self.assertEqual(observed, "1020", f"expected ni=10 and pri=20, read {observed}")
            receipt = json.loads(log.read_text(encoding="utf-8").strip().splitlines()[-1])
            self.assertEqual(receipt["status"], "ran")
            self.assertEqual(receipt["exit"], 0)
            self.assertIn("pri=20", receipt["verified"])

    def test_a_launched_job_runs_from_an_already_niced_parent(self):
        """The bug a review found: `nice -n 10` is an INCREMENT.

        The helper is meant to be launched by something that is already
        deprioritised -- a coding worker, a launchd job.  From a parent at
        nice 10, `nice -n 10` produces a child at nice 20, so a fixed
        equality check refused the job and it never ran.  This canary
        launches the helper from an already-niced parent and requires the
        job to RUN.
        """
        nice_bin = prio._find_tool("nice")
        if not nice_bin:
            self.skipTest("nice is not on this host")
        with tempfile.TemporaryDirectory() as scratch:
            script = Path(scratch) / "canary.sh"
            # This one keeps the separator, so the two numbers can be read
            # apart no matter what the nice value turns out to be.
            script.write_text(
                '#!/bin/bash\n/bin/ps -o ni=,pri= -p $$ > "$1"\n',
                encoding="utf-8",
            )
            script.chmod(0o755)
            out = Path(scratch) / "observed.txt"
            log = Path(scratch) / "receipts.jsonl"
            env = dict(os.environ, MAC_CODING_PRIORITY_LOG=str(log))
            # The parent of the launcher is already at nice 10.
            proc = subprocess.run(
                [
                    nice_bin,
                    "-n",
                    "10",
                    sys.executable,
                    str(SCRIPT),
                    "--label",
                    "canary-niced-parent",
                    "--",
                    "bash",
                    str(script),
                    str(out),
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            observed = out.read_text(encoding="utf-8").strip()
            got_nice, got_pri = (int(part) for part in observed.split())
            self.assertEqual(got_pri, 20, f"expected the utility clamp, read {observed}")
            self.assertGreaterEqual(
                got_nice,
                10,
                f"the job must be at least as deprioritised as asked, read {observed}",
            )
            receipt = json.loads(log.read_text(encoding="utf-8").strip().splitlines()[-1])
            self.assertEqual(receipt["status"], "ran")

    def test_the_job_does_not_start_when_the_clamp_is_missing(self):
        with tempfile.TemporaryDirectory() as scratch:
            marker = Path(scratch) / "ran"
            log = Path(scratch) / "receipts.jsonl"
            env = dict(os.environ, MAC_CODING_PRIORITY_LOG=str(log))
            # The inner half, run without the clamp in front of it: the job must
            # not start, because the control is not in place.
            proc = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--inner",
                    "--label",
                    "failclosed",
                    "--",
                    "bash",
                    "-c",
                    f"touch {marker}",
                ],
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )
            self.assertEqual(proc.returncode, prio.EXIT_NO_CLAMP, proc.stderr)
            self.assertFalse(marker.exists(), "the job ran without the clamp")
            receipt = json.loads(log.read_text(encoding="utf-8").strip().splitlines()[-1])
            self.assertEqual(receipt["status"], "refused")


if __name__ == "__main__":
    unittest.main()
