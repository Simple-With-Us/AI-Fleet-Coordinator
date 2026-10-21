#!/usr/bin/env python3
"""Tests for the shared auto-merge armer's merge-queue behaviour.

    python3 scripts/test_auto_merge_reusable.py

The armer runs `gh pr merge --auto --squash --delete-branch`.  GitHub refuses
`--delete-branch` when the repository has a merge queue enabled: the flag asks
GitHub to delete the head branch when the pull request merges, and a merge queue
cannot accept that request on the auto-merge.  GitHub answers "X Cannot use `-d`
or `--delete-branch` when merge queue enabled".

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
  not_ready|armed_not_ready|clean_status|clean_status_armed)
    if [[ "$*" == "pr view "* ]]; then
      # Answer only the field the armer asks for, so a query for anything else fails
      # here instead of being answered with a plausible-looking null.
      if [[ "$*" != *"--json autoMergeRequest"* ]]; then
        echo "gh: fake gh answers only --json autoMergeRequest, got: $*" >&2
        exit 1
      fi
      case "$GH_FAKE_MODE" in
        armed_not_ready|clean_status_armed) echo '{"enabledAt":"2026-10-20T00:00:00Z"}' ;;
        *) echo "null" ;;
      esac
      exit 0
    fi
    if [[ "$GH_FAKE_MODE" == clean_status* ]]; then
      echo "Pull request is in clean status" >&2
      exit 1
    fi
    echo "gh: private-path not mergeable" >&2
    exit 1
    ;;
  queue|queue_then_fail)
    if [[ "$*" == *"--delete-branch"* ]]; then
      echo "X Cannot use \\`-d\\` or \\`--delete-branch\\` when merge queue enabled" >&2
      exit 1
    fi
    if [[ "$GH_FAKE_MODE" == "queue_then_fail" ]]; then
      echo "gh: some other failure" >&2
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


def step_script(step_name: str) -> str:
    """A named step's own `run` script, pulled out of the reusable workflow."""
    text = WORKFLOW.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == f"- name: {step_name}":
            start = index
            break
    if start is None:
        raise AssertionError(f"the workflow has no step named {step_name!r}")
    run_at = None
    for index in range(start, len(lines)):
        if lines[index].strip() == "run: |":
            run_at = index
            break
        if index > start and lines[index].strip() and not lines[index].startswith(" " * 6):
            break
    if run_at is None:
        raise AssertionError(f"the {step_name!r} step has no `run: |` block")
    body = []
    for line in lines[run_at + 1 :]:
        if line.strip() and not line.startswith(" " * 10):
            break
        body.append(line)
    if not body:
        raise AssertionError(f"the {step_name!r} step's run block is empty")
    indent = min(len(line) - len(line.lstrip()) for line in body if line.strip())
    return "\n".join(line[indent:] if line.strip() else "" for line in body) + "\n"


def arm_script() -> str:
    return step_script(ARM_STEP_NAME)


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
        # a broken PR must still fail the step loudly -- and exactly one gh call may be
        # made, or a broadened retry condition would slip past this test.
        code, out, calls = run_arm("fail", "true")
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn("Failed to enable auto-merge", out)

    def test_external_failure_details_are_not_logged(self):
        code, out, calls = run_arm("fail", "true")
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 1)
        self.assertNotIn("gh: some other failure", out)
        self.assertIn("status=failed", out)
        self.assertIn("code=outcome_unknown", out)

    def test_retry_failure_details_are_not_logged(self):
        code, out, calls = run_arm("queue_then_fail", "true")
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 2)
        self.assertNotIn("--delete-branch", calls[1])
        self.assertNotIn("gh: some other failure", out)
        self.assertIn("status=failed code=outcome_unknown", out)

    def test_not_ready_notice_omits_the_external_reason(self):
        code, out, calls = run_arm("not_ready", "true")
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertIn("Auto-merge not armed: the PR is not ready", out)
        self.assertNotIn("private-path", out)

    def test_an_armed_not_ready_pr_reports_the_armed_outcome(self):
        # A not-mergeable PR that IS armed is armed, not "not ready": the armer must
        # read the autoMergeRequest field to tell the two apart.  Asking for any other
        # field would make the fake answer null and this test would fail.
        code, out, calls = run_arm("armed_not_ready", "true")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(calls), 2)
        self.assertIn("Auto-merge armed despite pending checks.", out)
        self.assertNotIn("the PR is not ready", out)

    def test_a_clean_status_pr_with_no_auto_merge_is_an_error(self):
        # The clean-status branch must not report a PR armed when it is not: clean
        # status plus a null autoMergeRequest is an inconsistency, not an arm.
        code, out, calls = run_arm("clean_status", "true")
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), 2)
        self.assertIn("clean status but auto-merge is not armed", out)

    def test_an_armed_clean_status_pr_reports_the_armed_outcome(self):
        code, out, calls = run_arm("clean_status_armed", "true")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(calls), 2)
        self.assertIn("Auto-merge armed (clean status).", out)

    def test_armed_state_is_checked_for_pending_and_clean_prs(self):
        for mode, message in (
            ("armed_not_ready", "Auto-merge armed despite pending checks."),
            ("clean_status_armed", "Auto-merge armed (clean status)."),
        ):
            with self.subTest(mode=mode):
                code, out, calls = run_arm(mode, "true")
                self.assertEqual(code, 0)
                self.assertEqual(len(calls), 2)
                self.assertIn("--json autoMergeRequest --jq .autoMergeRequest", calls[1])
                self.assertIn(message, out)
                self.assertNotIn("private-path", out)

    def test_the_queue_refusal_is_matched_with_its_backticks(self):
        # gh prints the flag in backticks, so a pattern that looks for the bare flag
        # followed by a space would not match the message that is actually out there.
        self.assertRegex(QUEUE_REFUSAL, r"delete-branch.*when merge queue enabled")


CLASSIFY_STEP_NAME = "Classify PR for auto-merge eligibility"

# The fake answers each API call with the --jq-filtered lines the step expects:
# check-run statuses for the Kody lookup, one @json line per PR comment, the run
# status for marker-run lookups, and one @json line per active workflow run.
# The compare call fails on purpose:  it bounds the script after the gates, so a
# "compare API failed" skip proves the gates passed.
FAKE_GH_CLASSIFY = """#!/bin/bash
printf '%s\n' "$*" >> "$GH_CALLS"
case "$*" in
  *check-runs*)
    case "$GH_FAKE_MODE" in
      kody_api_fails) echo "gh: api down" >&2; exit 1 ;;
      kody_running) printf '%s\n' '{"check_runs":[{"name":"Kody Code Review","status":"in_progress"}]}' ;;
      *) printf '%s\n' '{"check_runs":[{"name":"Kody Code Review","status":"completed"}]}' ;;
    esac
    exit 0
    ;;
  *issues/1/comments*)
    case "$GH_FAKE_MODE" in
      comments_api_fails) echo "gh: api down" >&2; exit 1 ;;
      marker_run_active|marker_run_done|marker_status_fails|marker_run_404)
        printf '%s\n' '{"body":"<!-- swu-autofix-run: bot=swu-echo --> Autofix run https://example.com/o/r/actions/runs/555 started","user_type":"Bot"}'
        ;;
      forged_marker)
        printf '%s\n' '{"body":"<!-- swu-autofix-run: bot=swu-echo --> Autofix run https://example.com/o/r/actions/runs/999 started","user_type":"User"}'
        ;;
    esac
    exit 0
    ;;
  *actions/runs/555*)
    case "$GH_FAKE_MODE" in
      marker_status_fails) echo "gh: api down (HTTP 500)" >&2; exit 1 ;;
      marker_run_404) echo "gh: Not Found (HTTP 404)" >&2; exit 1 ;;
      marker_run_active) printf '%s\n' '{"status":"in_progress"}' ;;
      *) printf '%s\n' '{"status":"completed"}' ;;
    esac
    exit 0
    ;;
  *actions/runs*)
    case "$GH_FAKE_MODE" in
      runs_api_fails) echo "gh: api down" >&2; exit 1 ;;
      pr_linked_run)
        if [[ "$*" == *status=queued* ]]; then
          printf '%s\n' '{"path":".github/workflows/kody-fleet-autofix.yml","prs":[1]}'
        fi
        ;;
      *)
        if [[ "$*" == *status=queued* ]]; then
          printf '%s\n' '{"path":".github/workflows/ci.yml","prs":[1]}'
        fi
        ;;
    esac
    exit 0
    ;;
  *compare*)
    echo "gh: compare exploded" >&2
    exit 1
    ;;
esac
echo "gh: fake gh answers only check-runs, comments, actions/runs and compare, got: $*" >&2
exit 1
"""


def run_classify(mode: str) -> tuple[int, str, str]:
    """Run the classify step with a fake `gh`; return (exit code, output, skip reason)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        calls = root / "calls.txt"
        out = root / "gh_output.txt"
        fake = root / "gh"
        fake.write_text(FAKE_GH_CLASSIFY, encoding="utf-8")
        fake.chmod(0o755)
        script = root / "classify.sh"
        script.write_text(step_script(CLASSIFY_STEP_NAME), encoding="utf-8")
        env = dict(os.environ)
        env["PATH"] = f"{root}:{env.get('PATH', '')}"
        env.update(
            {
                "GH_FAKE_MODE": mode,
                "GH_CALLS": str(calls),
                "GH_TOKEN": "fake-token",
                "MERGE_TOKEN": "",
                "GITHUB_OUTPUT": str(out),
                "RUNNER_TEMP": str(root),
                "BASE": "0" * 40,
                "HEAD": "1" * 40,
                "HEAD_REF": "feature/x",
                "TITLE": "x",
                "REPO": "Simple-With-Us/Example",
                "PR_NUMBER": "1",
                "KODY_CHECK_NAME": "Kody Code Review",
                "RESTRICT": "",
                "SELF_GUARD": "",
                "HOLD_LABELS": "do-not-automerge,needs-human-merge",
                "LABELS_JSON": "[]",
                "REQUIRE_GATE": "false",
                "GATE_SCRIPT": "",
            }
        )
        proc = subprocess.run(
            ["bash", str(script)],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
            check=False,
        )
        reason = ""
        if out.exists():
            for line in out.read_text(encoding="utf-8").splitlines():
                if line.startswith("reason="):
                    reason = line[len("reason=") :]
        return proc.returncode, proc.stdout + proc.stderr, reason


class ClassifyInFlightGates(unittest.TestCase):
    """The owner 2026-10-21 rule: no merge while Kodus reviews or autofix runs."""

    def test_the_classify_step_is_the_one_under_test(self):
        self.assertIn("Kodus review running", step_script(CLASSIFY_STEP_NAME))
        self.assertIn("autofix run", step_script(CLASSIFY_STEP_NAME))

    def test_a_running_kodus_review_blocks_and_disarms(self):
        code, output, reason = run_classify("kody_running")
        self.assertEqual(code, 0, output)
        self.assertIn("Kodus review running", reason)

    def test_a_completed_kodus_review_passes_the_gate(self):
        code, output, reason = run_classify("clear")
        self.assertEqual(code, 0, output)
        self.assertIn("compare API failed", reason)

    def test_the_kody_lookup_fails_closed(self):
        code, output, reason = run_classify("kody_api_fails")
        self.assertEqual(code, 0, output)
        self.assertIn("fail closed", reason)

    def test_an_active_marker_run_blocks_and_disarms(self):
        code, output, reason = run_classify("marker_run_active")
        self.assertEqual(code, 0, output)
        self.assertIn("autofix run 555 in_progress", reason)

    def test_a_completed_marker_run_passes(self):
        code, output, reason = run_classify("marker_run_done")
        self.assertEqual(code, 0, output)
        self.assertIn("compare API failed", reason)

    def test_the_marker_status_lookup_fails_closed(self):
        code, output, reason = run_classify("marker_status_fails")
        self.assertEqual(code, 0, output)
        self.assertIn("autofix run status lookup failed", reason)

    def test_a_deleted_marker_run_is_not_in_flight(self):
        code, output, reason = run_classify("marker_run_404")
        self.assertEqual(code, 0, output)
        self.assertIn("compare API failed", reason)

    def test_a_human_forged_marker_is_ignored(self):
        code, output, reason = run_classify("forged_marker")
        self.assertEqual(code, 0, output)
        self.assertIn("compare API failed", reason)

    def test_a_pr_linked_autofix_run_blocks_and_disarms(self):
        code, output, reason = run_classify("pr_linked_run")
        self.assertEqual(code, 0, output)
        self.assertIn("autofix run queued or in progress for PR #1", reason)

    def test_the_comment_lookup_fails_closed(self):
        code, output, reason = run_classify("comments_api_fails")
        self.assertEqual(code, 0, output)
        self.assertIn("fail closed", reason)

    def test_the_runs_lookup_fails_closed(self):
        code, output, reason = run_classify("runs_api_fails")
        self.assertEqual(code, 0, output)
        self.assertIn("fail closed", reason)


if __name__ == "__main__":
    unittest.main()
