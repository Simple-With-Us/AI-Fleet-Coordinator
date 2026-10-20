#!/usr/bin/env python3
"""Tests for the shared auto-merge armer's merge-queue behaviour.

    python3 scripts/test_auto_merge_reusable.py

The armer runs `gh pr merge --auto --squash --delete-branch`.  GitHub refuses
`--delete-branch` when the repository has a merge queue enabled: the flag flips the
repository's delete-branch-on-merge setting, and a queue cannot have both.  GitHub
answers "X Cannot use `-d` or `--delete-branch` when merge queue enabled".

Before AFC-413 that refusal failed the arm step, so the "Auto-merge PRs" check was
red on every PR in a repo with a queue, and a PR that was not already armed never got
armed.  The armer now arms again without the flag when the queue is what refused it.

These tests run the arm step's own shell against a fake `gh`, so the branch that
decides to arm again is exercised rather than described.  Nothing here talks to
GitHub: the fake records its arguments and answers what the test tells it to.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORKFLOW = REPO / ".github" / "workflows" / "auto-merge-reusable.yml"
ARM_STEP_NAME = "Enable auto-merge (squash + delete branch)"

# The message gh prints when a merge queue refuses the flag.  The backticks are
# part of it, so a pattern that ignores them matches both spellings.
QUEUE_REFUSAL = "X Cannot use `-d` or `--delete-branch` when merge queue enabled"

FAKE_GH = """#!/bin/bash
printf '%s\\n' "$*" >> "$GH_CALLS"
case "${GH_FAKE_MODE}" in
  queue)
    if [[ "$*" == *"--delete-branch"* ]]; then
      echo "X Cannot use \\`-d\\` or \\`--delete-branch\\` when merge queue enabled" >&2
      exit 1
    fi
    ;;
  fail)
    echo "gh: some other failure" >&2
    exit 1
    ;;
esac
echo "Auto-merge enabled"
exit 0
"""


def arm_script() -> str:
    """The arm step's own `run` script, pulled out of the reusable workflow."""
    text = WORKFLOW.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == f"- name: {ARM_STEP_NAME}":
            start = index
            break
    if start is None:
        raise AssertionError(f"the workflow has no step named {ARM_STEP_NAME!r}")
    run_at = None
    for index in range(start, len(lines)):
        if lines[index].strip() == "run: |":
            run_at = index
            break
    if run_at is None:
        raise AssertionError("the arm step has no `run: |` block")
    body = []
    for line in lines[run_at + 1 :]:
        if line.strip() and not line.startswith(" " * 10):
            break
        body.append(line)
    if not body:
        raise AssertionError("the arm step's run block is empty")
    indent = min(len(line) - len(line.lstrip()) for line in body if line.strip())
    return "\n".join(line[indent:] if line.strip() else "" for line in body) + "\n"


def run_arm(mode: str, delete_branch: str) -> tuple[int, str, list[str]]:
    """Run the arm step with a fake `gh` and report the exit code and the calls."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        calls = root / "calls.txt"
        fake = root / "gh"
        fake.write_text(FAKE_GH, encoding="utf-8")
        fake.chmod(0o755)
        script = root / "arm.sh"
        script.write_text(arm_script(), encoding="utf-8")
        env = dict(os.environ)
        env["PATH"] = f"{root}:{env.get('PATH', '')}"
        env["GH_FAKE_MODE"] = mode
        env["GH_CALLS"] = str(calls)
        env["GH_TOKEN"] = "fake-token"
        env["PR_URL"] = "https://github.com/example/repo/pull/1"
        env["DELETE_BRANCH"] = delete_branch
        proc = subprocess.run(
            ["bash", str(script)],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )
        recorded = []
        if calls.exists():
            recorded = [line for line in calls.read_text(encoding="utf-8").splitlines() if line]
        return proc.returncode, proc.stdout + proc.stderr, recorded


class ArmStep(unittest.TestCase):
    def test_the_arm_step_is_the_one_under_test(self):
        script = arm_script()
        self.assertIn("gh pr merge", script)
        self.assertIn("--auto", script)

    def test_arming_works_when_the_flag_is_accepted(self):
        code, _out, calls = run_arm("ok", "true")
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn("--delete-branch", calls[0])

    def test_arming_without_the_flag_never_asks_for_it(self):
        code, _out, calls = run_arm("ok", "false")
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--delete-branch", calls[0])

    def test_a_merge_queue_refusal_arms_again_without_the_flag(self):
        # The fix: the queue refuses the flag, the armer asks again without it, and the
        # step succeeds instead of failing every PR in the repo.
        code, out, calls = run_arm("queue", "true")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(calls), 2)
        self.assertIn("--delete-branch", calls[0])
        self.assertNotIn("--delete-branch", calls[1])
        self.assertIn("--auto", calls[1])
        self.assertIn("merge queue", out.lower())

    def test_a_merge_queue_refusal_without_the_flag_needs_no_retry(self):
        code, _out, calls = run_arm("queue", "false")
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("--delete-branch", calls[0])

    def test_any_other_failure_is_still_a_failure(self):
        # The retry is for the queue's refusal and nothing else.  A dead credential or
        # a broken PR must still fail the step loudly.
        code, out, _calls = run_arm("fail", "true")
        self.assertEqual(code, 1)
        self.assertIn("Failed to enable auto-merge", out)

    def test_the_queue_refusal_is_matched_with_its_backticks(self):
        # gh prints the flag in backticks, so a pattern that looks for the bare flag
        # followed by a space would not match the message that is actually out there.
        self.assertRegex(QUEUE_REFUSAL, r"delete-branch.*when merge queue enabled")


if __name__ == "__main__":
    unittest.main()
