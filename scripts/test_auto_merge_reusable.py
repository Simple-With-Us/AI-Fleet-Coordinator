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


def step_script(step_name: str, workflow: Path = WORKFLOW) -> str:
    """A named step's own `run` script, pulled out of the given workflow."""
    text = workflow.read_text(encoding="utf-8")
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() in (f"- name: {step_name}", f"- id: {step_name}"):
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
        env["EXPECTED_HEAD"] = "1" * 40
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
    def test_merge_is_pinned_to_the_classified_head(self):
        for mode in ("clean_status", "armed_not_ready"):
            _code, out, recorded = run_arm(mode, "true")
            merge_calls = [
                c for c in recorded if c.startswith("pr merge") and "--disable-auto" not in c
            ]
            self.assertTrue(merge_calls, out)
            for call in merge_calls:
                self.assertIn("--match-head-commit " + "1" * 40, call)

    def test_arm_refuses_without_a_classified_head(self):
        script = arm_script()
        self.assertIn('--match-head-commit "$EXPECTED_HEAD"', script)
        self.assertIn("no classified head to pin", script)

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
# check-run statuses for the Kody lookup, raw @json comment lines for the
# combined comments read (the step's own jq reduces them, so the tests
# exercise the real Bot-marker and request-pattern filters), the run status
# for marker-run lookups, and one @json line per active workflow run.
# The compare call fails on purpose:  it bounds the script after the gates, so a
# "compare API failed" skip proves the gates passed.
FAKE_GH_CLASSIFY = """#!/bin/bash
printf '%s\n' "$*" >> "$GH_CALLS"
H="1111111111111111111111111111111111111111"
case "$*" in
  *check-runs*)
    case "$GH_FAKE_MODE" in
      kody_api_fails) echo "gh: api down" >&2; exit 1 ;;
      kody_running) printf '%s\n' '{"id":9,"name":"Kody Code Review","app":"kody-ai","app_id":413034,"status":"in_progress","conclusion":"none","started_at":"2026-10-30T11:55:00Z","completed_at":null,"title":"","summary":""}' ;;
      ack_skipped|request_then_ack_only) printf '%s\n' '{"id":9,"name":"Kody Code Review","app":"kody-ai","app_id":413034,"status":"completed","conclusion":"skipped","started_at":"2026-10-30T11:55:00Z","completed_at":"2026-10-30T11:55:03Z","title":"Code Review Skipped","summary":"Manual mode requires @kody start-review."}' ;;
      check_failed) printf '%s\n' '{"id":9,"name":"Kody Code Review","app":"kody-ai","app_id":413034,"status":"completed","conclusion":"failure","started_at":"2026-10-30T11:55:00Z","completed_at":"2026-10-30T12:00:30Z","title":"Code Review Failed","summary":"Review failed."}' ;;
      neutral_not_receipt) printf '%s\n' '{"id":9,"name":"Kody Code Review","app":"kody-ai","app_id":413034,"status":"completed","conclusion":"neutral","started_at":"2026-10-30T11:55:00Z","completed_at":"2026-10-30T12:00:30Z","title":"Code Review Complete","summary":"Review finished successfully."}' ;;
      forged_name_other_app) printf '%s\n' '{"id":9,"name":"Kody Code Review","app":"some-other-app","app_id":999,"status":"completed","conclusion":"success","started_at":"2026-10-30T11:55:00Z","completed_at":"2026-10-30T12:00:30Z","title":"Code Review Complete","summary":"Review finished successfully."}' ;;
      old_run_started_before_request) printf '%s\n' '{"id":9,"name":"Kody Code Review","app":"kody-ai","app_id":413034,"status":"completed","conclusion":"success","started_at":"2026-10-30T12:59:00Z","completed_at":"2026-10-30T13:05:00Z","title":"Code Review Complete","summary":"Review finished successfully."}' ;;
      fractional_request_order) printf '%s\n' '{"id":9,"name":"Kody Code Review","app":"kody-ai","app_id":413034,"status":"completed","conclusion":"success","started_at":"2026-10-30T13:00:00Z","completed_at":"2026-10-30T13:00:30Z","title":"Code Review Complete","summary":"Review finished successfully."}' ;;
      queued_new_run_old_success)
        printf '%s\n' '{"id":124599873261,"name":"Kody Code Review","app":"kody-ai","app_id":413034,"status":"completed","conclusion":"success","started_at":"2026-10-30T11:55:00Z","completed_at":"2026-10-30T12:00:30Z","title":"Code Review Complete","summary":"Review finished successfully."}'
        printf '%s\n' '{"id":10,"name":"Kody Code Review","app":"kody-ai","app_id":413034,"status":"queued","conclusion":"none","started_at":"2026-10-30T13:10:00Z","completed_at":null,"title":"","summary":""}'
        ;;
      *) printf '%s\n' '{"id":124599873261,"name":"Kody Code Review","app":"kody-ai","app_id":413034,"status":"completed","conclusion":"success","started_at":"2026-10-30T11:55:00Z","completed_at":"2026-10-30T12:00:30Z","title":"Code Review Complete","summary":"Review finished successfully. Suggestions (if any) were posted as PR/file comments."}' ;;
    esac
    exit 0
    ;;
  *issues/1/comments*)
    case "$GH_FAKE_MODE" in
      comments_api_fails|requests_api_fails) echo "gh: api down" >&2; exit 1 ;;
    esac
    # Raw per-comment @json lines, as `gh api ... --jq '.[] | @json'` emits
    # them: the step's own jq pass does the marker/request reduction, so
    # the tests exercise the real filter rather than a pre-reduced answer.
    case "$GH_FAKE_MODE" in
      multiline_request) printf '%s\n' '{"created_at": "2026-10-30T13:00:00Z", "user": {"type": "User"}, "body": "Please review.\\n@kody start-review"}' ;;
      request_after_review|mixed_history|old_run_started_before_request|appr_request_then_ack)
        printf '%s\n' '{"created_at":"2026-10-30T13:00:00Z","user":{"type":"User"},"body":"@kody start-review"}' ;;
      request_before_review|request_then_ack_only)
        printf '%s\n' '{"created_at":"2026-10-30T11:00:00Z","user":{"type":"User"},"body":"@kody start-review"}' ;;
      fractional_request_order)
        printf '%s\n' '{"created_at":"2026-10-30T13:00:00Z","user":{"type":"User"},"body":"@kody start-review"}'
        printf '%s\n' '{"created_at":"2026-10-30T13:00:00.500Z","user":{"type":"User"},"body":"@kody start-review"}' ;;
      fractional_request_older)
        printf '%s\n' '{"created_at":"2026-10-30T11:00:00.500Z","user":{"type":"User"},"body":"@kody start-review"}' ;;
      appr_fractional)
        printf '%s\n' '{"created_at":"2026-10-30T13:00:00.500Z","user":{"type":"User"},"body":"@kody start-review"}' ;;
      appr_fractional_ok)
        printf '%s\n' '{"created_at":"2026-10-30T13:00:00Z","user":{"type":"User"},"body":"@kody start-review"}' ;;
    esac
    case "$GH_FAKE_MODE" in
      marker_run_active|marker_run_done|marker_status_fails|marker_run_404)
        printf '%s\n' '{"created_at":"2026-10-30T10:00:00Z","user":{"type":"Bot"},"body":"swu-autofix-run: bot=x run at https://github.com/x/actions/runs/555"}' ;;
      forged_marker)
        # User-authored: the step's real jq keeps Bot-authored markers only.
        printf '%s\n' '{"created_at":"2026-10-30T10:00:00Z","user":{"type":"User"},"body":"swu-autofix-run: bot=x run at https://github.com/x/actions/runs/555"}' ;;
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
  *pulls/1/reviews*)
    case "$GH_FAKE_MODE" in
      reviews_api_fails) echo "gh: api down" >&2; exit 1 ;;
      multiline_request|eff_commented_current|kody_success_open_findings|request_after_review|request_before_review|threads_api_fails|threads_next_page|requests_api_fails|ack_skipped|check_failed|request_then_ack_only|fractional_request_order|fractional_request_older)
        printf '%s\n' '[{"id":2,"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"'$H'","state":"COMMENTED","submitted_at":"2026-10-30T12:00:00Z"}]' ;;
      old_run_started_before_request|neutral_not_receipt|forged_name_other_app|queued_new_run_old_success)
        printf '%s\n' '[{"id":2,"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"'$H'","state":"COMMENTED","submitted_at":"2026-10-30T12:00:00Z"}]' ;;
      appr_reverse_dismissed)
        printf '%s\n' '[{"id":2,"user":{"login":"kody-ai[bot]"},"commit_id":"'$H'","state":"DISMISSED"}]' '[{"id":1,"user":{"login":"kody-ai[bot]"},"commit_id":"'$H'","state":"APPROVED"}]' ;;
      appr_reverse_approved)
        printf '%s\n' '[{"id":2,"user":{"login":"kody-ai[bot]"},"commit_id":"'$H'","state":"APPROVED"}]' '[{"id":1,"user":{"login":"kody-ai[bot]"},"commit_id":"'$H'","state":"CHANGES_REQUESTED"}]' ;;
      appr_request_then_ack)
        printf '%s\n' '[{"id":1,"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"'$H'","state":"APPROVED","submitted_at":"2026-10-30T12:00:00Z"},{"id":2,"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"'$H'","state":"COMMENTED","submitted_at":"2026-10-30T13:05:00Z"}]' ;;
      appr_fractional)
        printf '%s\n' '[{"id":1,"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"'$H'","state":"APPROVED","submitted_at":"2026-10-30T13:00:00Z"}]' ;;
      appr_fractional_ok)
        printf '%s\n' '[{"id":1,"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"'$H'","state":"APPROVED","submitted_at":"2026-10-30T13:00:00.500Z"}]' ;;
      grace_edge)
        # Submitted 30s after the run completed: inside the 60s pairing grace.
        printf '%s\n' '[{"id":2,"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"'$H'","state":"COMMENTED","submitted_at":"2026-10-30T12:01:00Z"}]' ;;
      ack_outside_window)
        # A lone ack submitted long after the only completed run finished.
        printf '%s\n' '[{"id":2,"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"'$H'","state":"COMMENTED","submitted_at":"2026-10-30T13:05:00Z"}]' ;;
      mixed_history)
        # Genuine paired review at 12:00, then a newer request (13:00), then a
        # bare ack at 13:05 - the AFC #1207 5724125545 scenario.  The old
        # success run must not validate the ack.
        printf '%s\n' '[{"id":1,"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"'$H'","state":"COMMENTED","submitted_at":"2026-10-30T12:00:00Z"},{"id":2,"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"'$H'","state":"COMMENTED","submitted_at":"2026-10-30T13:05:00Z"}]' ;;
      eff_old_head_review)
        printf '%s\n' '[{"id":2,"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"2222222222222222222222222222222222222222","state":"COMMENTED","submitted_at":"2026-10-30T12:00:00Z"}]' ;;
      eff_cr_outstanding)
        # A later COMMENTED never clears an earlier CHANGES_REQUESTED.
        printf '%s\n' '[{"id":1,"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"'$H'","state":"CHANGES_REQUESTED","submitted_at":"2026-10-30T11:00:00Z"},{"id":2,"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"'$H'","state":"COMMENTED","submitted_at":"2026-10-30T12:00:00Z"}]' ;;
      eff_cr_then_approval)
        # Unsorted on purpose: the gate orders by review id, not array order.
        printf '%s\n' '[{"id":3,"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"'$H'","state":"APPROVED","submitted_at":"2026-10-30T12:00:00Z"},{"id":1,"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"'$H'","state":"CHANGES_REQUESTED","submitted_at":"2026-10-30T11:00:00Z"}]' ;;
      eff_cr_other_approval)
        # Another reviewer's approval does not clear the fleet reviewer's request.
        printf '%s\n' '[{"id":1,"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"'$H'","state":"CHANGES_REQUESTED","submitted_at":"2026-10-30T11:00:00Z"},{"id":2,"user":{"id":11,"login":"someone"},"commit_id":"'$H'","state":"APPROVED","submitted_at":"2026-10-30T12:00:00Z"},{"id":3,"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"'$H'","state":"COMMENTED","submitted_at":"2026-10-30T13:00:00Z"}]' ;;
      eff_dismissed_unwinds)
        # The APPROVED review was dismissed: it is no longer decisive, so the
        # earlier CHANGES_REQUESTED by the same reviewer is outstanding again.
        printf '%s\n' '[{"id":1,"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"'$H'","state":"CHANGES_REQUESTED","submitted_at":"2026-10-30T11:00:00Z"},{"id":2,"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"'$H'","state":"DISMISSED","submitted_at":"2026-10-30T12:00:00Z"}]' ;;
      eff_missing_id)
        printf '%s\n' '[{"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"'$H'","state":"APPROVED","submitted_at":"2026-10-30T12:00:00Z"}]' ;;
      eff_missing_user)
        printf '%s\n' '[{"id":1,"commit_id":"'$H'","state":"APPROVED","submitted_at":"2026-10-30T12:00:00Z"}]' ;;
      eff_commented_no_ids)
        # COMMENTED is not decisive: no formal sequence or identity is required.
        printf '%s\n' '[{"user":{"login":"kody-ai[bot]"},"commit_id":"'$H'","state":"COMMENTED","submitted_at":"2026-10-30T12:00:00Z"}]' ;;
      approved_current) printf '%s\n' "[{\\\"user\\\":{\\\"login\\\":\\\"kody-ai[bot]\\\"},\\\"commit_id\\\":\\\"$H\\\",\\\"state\\\":\\\"APPROVED\\\"}]" ;;
      approved_old_head) printf '%s\n' '[{"user":{"login":"kody-ai[bot]"},"commit_id":"2222222222222222222222222222222222222222","state":"APPROVED"}]' ;;
      commented_current) printf '%s\n' "[{\\\"user\\\":{\\\"login\\\":\\\"kody-ai[bot]\\\"},\\\"commit_id\\\":\\\"$H\\\",\\\"state\\\":\\\"COMMENTED\\\"}]" ;;
      changes_requested_current) printf '%s\n' "[{\\\"user\\\":{\\\"login\\\":\\\"kody-ai[bot]\\\"},\\\"commit_id\\\":\\\"$H\\\",\\\"state\\\":\\\"APPROVED\\\"},{\\\"user\\\":{\\\"login\\\":\\\"kody-ai[bot]\\\"},\\\"commit_id\\\":\\\"$H\\\",\\\"state\\\":\\\"CHANGES_REQUESTED\\\"}]" ;;
      approved_then_commented) printf '%s\\n' "[{\\"user\\":{\\"login\\":\\"kody-ai[bot]\\"},\\"commit_id\\":\\"$H\\",\\"state\\":\\"APPROVED\\"},{\\"user\\":{\\"login\\":\\"kody-ai[bot]\\"},\\"commit_id\\":\\"$H\\",\\"state\\":\\"COMMENTED\\"}]" ;;
      approved_by_other) printf '%s\n' "[{\\\"user\\\":{\\\"login\\\":\\\"someone\\\"},\\\"commit_id\\\":\\\"$H\\\",\\\"state\\\":\\\"APPROVED\\\"}]" ;;
      *) printf '%s\n' '[]' ;;
    esac
    exit 0
    ;;
  *graphql*)
    case "$GH_FAKE_MODE" in
      threads_api_fails) echo "gh: api down" >&2; exit 1 ;;
      threads_next_page) printf '%s\n' 'true 0' ;;
      kody_success_open_findings) printf '%s\n' 'false 2' ;;
      *) printf '%s\n' 'false 0' ;;
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


def run_classify_full(
    mode: str,
    review_gate: str = "",
    restrict: str = "",
    require_approval: str = "",
    request_pattern: str = "@kody[ \t]+start-review",
    outputs: dict | None = None,
) -> tuple[int, str, str, list[str]]:
    """Run the classify step with a fake `gh`; return (exit code, output, skip reason, gh calls)."""
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
                "KODY_APP_SLUG": "kody-ai",
                "KODY_APP_ID": "413034",
                "REQUIRE_APPROVAL": require_approval,
                "REVIEW_GATE": review_gate,
                "REVIEW_REQUEST_PATTERN": request_pattern,
                "APPROVAL_REVIEWER": "kody-ai[bot]",
                "RESTRICT": restrict,
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
                if outputs is not None and "=" in line:
                    key, _, value = line.partition("=")
                    outputs[key] = value
        recorded = []
        if calls.exists():
            recorded = [line for line in calls.read_text(encoding="utf-8").splitlines() if line]
        return proc.returncode, proc.stdout + proc.stderr, reason, recorded


def run_classify(mode: str, review_gate: str = "", **kwargs) -> tuple[int, str, str]:
    code, output, reason, _calls = run_classify_full(mode, review_gate, **kwargs)
    return code, output, reason


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
        code, output, reason = run_classify("clear", require_approval="false")
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
        code, output, reason = run_classify("marker_run_done", require_approval="false")
        self.assertEqual(code, 0, output)
        self.assertIn("compare API failed", reason)

    def test_the_marker_status_lookup_fails_closed(self):
        code, output, reason = run_classify("marker_status_fails")
        self.assertEqual(code, 0, output)
        self.assertIn("autofix run status lookup failed", reason)

    def test_a_deleted_marker_run_is_not_in_flight(self):
        code, output, reason = run_classify("marker_run_404", require_approval="false")
        self.assertEqual(code, 0, output)
        self.assertIn("compare API failed", reason)

    def test_a_human_forged_marker_is_ignored(self):
        code, output, reason = run_classify("forged_marker", require_approval="false")
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


class ClassifyCurrentApproval(unittest.TestCase):
    """No arming without an APPROVED review on the current head commit."""

    def run_mode(self, mode):
        code, output, reason = run_classify(mode, require_approval="true")
        self.assertEqual(code, 0, output)
        return reason

    def test_the_gate_is_in_the_classify_step(self):
        self.assertIn("no current-head approval", step_script(CLASSIFY_STEP_NAME))

    def test_a_current_head_approval_passes_the_gate(self):
        self.assertIn("compare API failed", self.run_mode("approved_current"))

    def test_no_review_at_all_blocks_and_disarms(self):
        self.assertIn("no current-head approval", self.run_mode("clear"))

    def test_an_approval_on_an_older_commit_does_not_count(self):
        self.assertIn("no current-head approval", self.run_mode("approved_old_head"))

    def test_a_trailing_comment_does_not_revoke_an_approval(self):
        self.assertIn("compare API failed", self.run_mode("approved_then_commented"))

    def test_the_review_lookup_is_an_explicit_get(self):
        self.assertIn("--method GET", step_script(CLASSIFY_STEP_NAME))

    def test_a_commented_review_does_not_count(self):
        self.assertIn("no current-head approval", self.run_mode("commented_current"))

    def test_an_approval_from_someone_else_does_not_count(self):
        self.assertIn("no current-head approval", self.run_mode("approved_by_other"))

    def test_a_later_changes_request_blocks(self):
        self.assertIn("changes requested", self.run_mode("changes_requested_current"))

    def test_the_review_lookup_fails_closed(self):
        self.assertIn("fail closed", self.run_mode("reviews_api_fails"))

    def test_the_gate_can_be_turned_off_by_the_caller(self):
        code, output, reason = run_classify("clear", require_approval="false")
        self.assertEqual(code, 0, output)
        self.assertIn("compare API failed", reason)

    def test_a_lane_match_does_not_exempt_the_approval_gate(self):
        code, output, reason = run_classify("clear", require_approval="true", restrict="feature")
        self.assertEqual(code, 0, output)
        self.assertIn("no current-head approval", reason)


class ClassifyEffectiveReviewGate(unittest.TestCase):
    """The default review gate: current-head review plus real findings disposition.

    A green Kody check-run alone is never enough (AFC #1207: the check was
    green while a valid HIGH review comment was still open).
    """

    def run_effective(self, mode, **kwargs):
        # Opting into effective also requires require_current_approval: false
        # (the restored default true always forces the strict approval gate).
        kwargs.setdefault("require_approval", "false")
        code, output, reason = run_classify(mode, "effective", **kwargs)
        self.assertEqual(code, 0, output)
        return reason

    def test_the_omitted_inputs_contract_is_unchanged(self):
        # Host review 2026-10-31: no silent default change - omitted inputs
        # still resolve to the legacy strict approval gate, review_gate is an
        # explicit opt-in only, and there is no "off" value.
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("default: true", text)
        self.assertNotIn('default: "effective"', text)
        self.assertNotIn("effective|approval|off", text)
        outputs: dict = {}
        code, output, reason, _ = run_classify_full("clear", outputs=outputs)
        self.assertEqual(code, 0, output)
        self.assertEqual(outputs.get("resolved_gate"), "approval")
        self.assertIn("no current-head approval", reason)

    def test_a_current_head_review_with_resolved_threads_passes(self):
        self.assertIn("compare API failed", self.run_effective("eff_commented_current"))

    def test_a_review_on_an_older_commit_does_not_count(self):
        self.assertIn("no current-head review record", self.run_effective("eff_old_head_review"))

    def test_no_review_at_all_blocks_and_disarms(self):
        self.assertIn("no current-head review record", self.run_effective("clear"))

    def test_an_outstanding_changes_request_blocks(self):
        self.assertIn("outstanding", self.run_effective("eff_cr_outstanding"))

    def test_only_the_same_reviewers_later_approval_clears_a_request(self):
        self.assertIn("compare API failed", self.run_effective("eff_cr_then_approval"))
        self.assertIn("outstanding", self.run_effective("eff_cr_other_approval"))

    def test_a_dismissal_unwinds_the_dismissed_review(self):
        self.assertIn("outstanding", self.run_effective("eff_dismissed_unwinds"))

    def test_a_decisive_review_missing_its_sequence_fails_closed(self):
        self.assertIn("fail closed", self.run_effective("eff_missing_id"))

    def test_a_decisive_review_missing_reviewer_identity_fails_closed(self):
        self.assertIn("fail closed", self.run_effective("eff_missing_user"))

    def test_commented_reviews_need_no_formal_sequence(self):
        self.assertIn("compare API failed", self.run_effective("eff_commented_no_ids"))

    def test_a_successful_kody_check_with_open_findings_does_not_arm(self):
        # The AFC #1207 scenario: the Kody check-run is green (completed) while
        # a valid review finding is still an unresolved thread.
        reason = self.run_effective("kody_success_open_findings")
        self.assertIn("unresolved review thread", reason)

    def test_the_thread_lookup_fails_closed(self):
        self.assertIn("review thread lookup failed", self.run_effective("threads_api_fails"))

    def test_thread_pagination_fails_closed(self):
        self.assertIn("pagination", self.run_effective("threads_next_page"))

    def test_a_review_request_newer_than_the_review_blocks(self):
        self.assertIn(
            "no completed full-review receipt",
            self.run_effective("request_after_review"),
        )

    def test_a_review_request_older_than_the_review_does_not_block(self):
        self.assertIn("compare API failed", self.run_effective("request_before_review"))

    def test_a_fractional_request_newer_than_the_receipt_blocks(self):
        # Kody 4433064857: "13:00:00Z" sorts AFTER "13:00:00.500Z" as text
        # ('Z' > '.'), so a string sort calls the whole-second request the
        # newest and a run started at 13:00:00Z would pass; epochs see the
        # fractional request as the newest and find no receipt started at
        # or after it.
        reason = self.run_effective("fractional_request_order")
        self.assertIn("no completed full-review receipt", reason)

    def test_a_fractional_request_older_than_the_receipt_passes(self):
        self.assertIn("compare API failed", self.run_effective("fractional_request_older"))

    def test_approval_mode_a_fractional_request_needs_a_newer_approval(self):
        # Same misorder in the approval staleness check: the 13:00:00.500Z
        # request is newer than the 13:00:00Z approval, so it must block.
        code, output, reason = run_classify("appr_fractional", require_approval="true")
        self.assertEqual(code, 0, output)
        self.assertIn("no newer APPROVED", reason)

    def test_approval_mode_a_fractional_approval_after_the_request_passes(self):
        # A 13:00:00.500Z APPROVED answers a 13:00:00Z request; a string
        # compare would block it ('Z' > '.'), epochs let it through.
        code, output, reason = run_classify("appr_fractional_ok", require_approval="true")
        self.assertEqual(code, 0, output)
        self.assertIn("compare API failed", reason)

    def test_the_pr_comments_are_read_once(self):
        # Kody 4433065484: the request probe and the marker scan share one
        # paginated read; the old code read the whole comment history twice.
        _code, _output, _reason, calls = run_classify_full(
            "eff_commented_current", "effective", require_approval="false"
        )
        comments_calls = [c for c in calls if "issues/1/comments" in c]
        self.assertEqual(len(comments_calls), 1, calls)

    def test_the_request_lookup_fails_closed(self):
        # One combined comment read now (Kody 4433065484): its failure is
        # the comment lookup failure, with or without a review gate.
        self.assertIn("PR comment lookup failed", self.run_effective("requests_api_fails"))

    def test_a_review_ack_under_a_skipped_check_has_no_receipt(self):
        # AFC #1207 review 5724125545: COMMENTED on the exact head while the
        # Kody check was SKIPPED - a conversation ack, not a whole-code review.
        reason = self.run_effective("ack_skipped")
        self.assertIn("no completed full-review receipt", reason)

    def test_a_failed_review_check_is_not_a_receipt(self):
        reason = self.run_effective("check_failed")
        self.assertIn("no completed full-review receipt", reason)

    def test_an_old_success_check_does_not_validate_an_ack_after_a_new_request(self):
        # Host-confirmed hole: a completed success run from an earlier cycle
        # combined with an ack posted after a new whole-code request.  The
        # receipt must START no earlier than the newest request - blocked.
        reason = self.run_effective("mixed_history")
        self.assertIn("no completed full-review receipt", reason)

    def test_a_request_answered_only_by_an_ack_blocks(self):
        reason = self.run_effective("request_then_ack_only")
        self.assertIn("no completed full-review receipt", reason)

    def test_a_run_started_before_the_request_that_finished_after_it_is_no_receipt(
        self,
    ):
        # Causality, not completion order: the request must precede the start.
        reason = self.run_effective("old_run_started_before_request")
        self.assertIn("no completed full-review receipt", reason)

    def test_a_neutral_run_is_never_a_source_completion(self):
        self.assertIn(
            "no completed full-review receipt",
            self.run_effective("neutral_not_receipt"),
        )

    def test_a_same_named_run_from_another_app_is_not_kody(self):
        # Check-run identity is the authenticated GitHub App, never the label.
        self.assertIn(
            "no completed full-review receipt",
            self.run_effective("forged_name_other_app"),
        )

    def test_a_newer_queued_run_with_an_old_success_blocks(self):
        reason = self.run_effective("queued_new_run_old_success")
        self.assertIn("Kodus review running", reason)

    def test_a_review_record_after_the_receipt_still_passes(self):
        # The record is not provenance; a late conversation reply does not
        # invalidate a valid receipt.
        reason = self.run_effective("grace_edge")
        self.assertIn("compare API failed", reason)

    def test_approval_mode_a_request_then_ack_needs_a_newer_approval(self):
        code, output, reason = run_classify("appr_request_then_ack", require_approval="true")
        self.assertEqual(code, 0, output)
        self.assertIn("no newer APPROVED", reason)

    def test_an_explicit_approval_requirement_is_never_weakened(self):
        code, output, reason = run_classify(
            "commented_current", "effective", require_approval="true"
        )
        self.assertEqual(code, 0, output)
        self.assertIn("no current-head approval", reason)

    def test_an_unknown_gate_fails_closed(self):
        code, output, reason = run_classify("eff_commented_current", "bogus")
        self.assertEqual(code, 0, output)
        self.assertIn("unknown review_gate", reason)

    def test_the_legacy_no_gate_contract_never_touches_the_review_apis(self):
        # require_current_approval: false with review_gate unset is the
        # pre-existing explicit no-gate contract; it stays review-API-free.
        outputs: dict = {}
        code, output, reason, calls = run_classify_full(
            "clear", require_approval="false", outputs=outputs
        )
        self.assertEqual(code, 0, output)
        self.assertEqual(outputs.get("resolved_gate"), "")
        self.assertIn("compare API failed", reason)
        joined = "\n".join(calls)
        self.assertNotIn("reviews?per_page", joined)
        self.assertNotIn("graphql", joined)
        # The request probe is part of the gate too: no "nomatch" scan call.
        self.assertNotIn("nomatch", joined)

    def test_review_gate_off_is_not_a_contract(self):
        # Host review 2026-10-31: no new OFF bypass - "off" now fails closed.
        code, output, reason = run_classify("clear", "off")
        self.assertEqual(code, 0, output)
        self.assertIn("unknown review_gate", reason)


class GateSource(unittest.TestCase):
    """Source pins for the Kody round-3 findings (2026-10-21)."""

    def test_comment_bodies_never_touch_disk(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        # No persisted comment dump: jq reduces each Bot marker comment to its
        # run id inside gh, so user-controlled text never lands on the runner.
        self.assertNotIn("pr-comments.txt", text)
        self.assertIn('select(.user.type == "Bot")', text)
        self.assertIn('contains("swu-autofix-run: bot=")', text)

    def test_the_effective_gate_orders_decisions_by_review_id(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("sort_by(.id)", text)
        self.assertIn("reduce .[] as $r", text)

    def test_the_thread_gate_reads_resolution_state_and_fails_closed_on_pagination(
        self,
    ):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("reviewThreads(first:100)", text)
        self.assertIn("pageInfo{hasNextPage}", text)
        self.assertIn("review thread pagination needs explicit resolution - fail closed", text)

    def test_the_request_probe_matches_any_author(self):
        # A forged request can only delay arming, never permit it: the probe
        # must not trust only Bot-authored comments, and the pattern reaches
        # jq through the environment, never interpolated into the program
        # (Kody 4433065134).
        text = WORKFLOW.read_text(encoding="utf-8")
        probe_lines = [
            line for line in text.splitlines() if "test(env.REVIEW_REQUEST_PATTERN" in line
        ]
        self.assertTrue(probe_lines, "env-fed request probe missing")
        for line in probe_lines:
            self.assertNotIn('user.type == "Bot"', line)
        squashed = text.replace("\\", "")
        self.assertNotIn('test("$REVIEW_REQUEST_PATTERN"', squashed)
        self.assertNotIn("review_request_pattern contains a quote", text)

    def test_the_comment_history_is_read_once_per_job(self):
        # Kody 4433065484: classify and the boundary each paginate the PR
        # comment history exactly once.
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(text.count("issues/$PR_NUMBER/comments"), 2)

    def test_the_kody_lookup_keeps_app_identity_and_verdict_fields(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn(".check_runs[] | {id, name, app:", text)
        self.assertIn("started_at, completed_at", text)
        self.assertIn(".output.title", text)
        self.assertIn(".output.summary", text)

    def test_the_kody_lookup_filters_by_name_and_paginates(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn('-f check_name="$KODY_CHECK_NAME"', text)
        self.assertIn("--paginate", text)

    def test_every_accepted_run_leaves_a_marker(self):
        # Owner/sentry runs post the run-id marker too; only the per-bot cap
        # lookup stays inside the exemption (Kody: the gate could merge while an
        # owner-triggered run was still pushing).
        for name in (
            "pr-autofix-reusable.yml",
            "claude-code-autofix-reusable.yml",
            "codex-autofix-reusable.yml",
        ):
            text = (REPO / ".github" / "workflows" / name).read_text(encoding="utf-8")
            self.assertIn("Owner/sentry request:  exempt from the per-bot cap", text, name)
        for name in ("claude-code-autofix-reusable.yml", "codex-autofix-reusable.yml"):
            text = (REPO / ".github" / "workflows" / name).read_text(encoding="utf-8")
            # The exempt path posts the marker before its early exit.
            exempt = text.index("exempt from the one-run-per-bot rule")
            marker_post = text.index("Owner/sentry request:  exempt from the per-bot cap")
            self.assertLess(exempt, marker_post, name)
        # pr-autofix: the marker write sits after the cap branch, not inside it.
        text = (REPO / ".github" / "workflows" / "pr-autofix-reusable.yml").read_text(
            encoding="utf-8"
        )
        cap = text.index("already used its one autofix run")
        marker_write = text.index("Every accepted run leaves its run-id marker")
        self.assertLess(cap, marker_write)


FAKE_GH_ARM_GUARD = r"""#!/bin/bash
printf '%s\n' "$*" >> "$GH_CALLS"
HEAD_OK="1111111111111111111111111111111111111111"
case "$*" in
  *check-runs/124599873261*)
    SHA="$HEAD_OK"; CONC="success"; STARTED="2026-10-30T13:05:00Z"
    case "$GH_FAKE_MODE" in
      receipt_fetch_fails) echo "gh: api down" >&2; exit 1 ;;
      receipt_gone_neutral) CONC="neutral" ;;
      receipt_head_mismatch) SHA="3333333333333333333333333333333333333333" ;;
      receipt_fractional_started) STARTED="2026-10-30T13:00:00.900Z" ;;
      receipt_fractional_stale) STARTED="2026-10-30T13:00:00Z" ;;
    esac
    printf '{"app":{"slug":"kody-ai","id":413034},"name":"Kody Code Review","head_sha":"%s","status":"completed","conclusion":"%s","started_at":"%s","completed_at":"2026-10-30T13:10:00Z","output":{"title":"Code Review Complete","summary":"Review finished successfully. Suggestions (if any) were posted as PR/file comments."}}\n' "$SHA" "$CONC" "$STARTED"
    exit 0
    ;;
  *reviews*)
    case "$GH_FAKE_MODE" in
      reviews_fail) echo "gh: api down" >&2; exit 1 ;;
      cr_after_classification)
        printf '%s\n' '[{"id":1,"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"'$HEAD_OK'","state":"CHANGES_REQUESTED","submitted_at":"2026-10-30T13:30:00Z"}]' ;;
      approval_dismissed)
        printf '%s\n' '[{"id":1,"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"'$HEAD_OK'","state":"APPROVED","submitted_at":"2026-10-30T13:20:00Z"},{"id":2,"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"'$HEAD_OK'","state":"DISMISSED","submitted_at":"2026-10-30T13:40:00Z"}]' ;;
      approval_other_old_cr)
        printf '%s\n' '[{"id":1,"user":{"id":11,"login":"other"},"commit_id":"2222222222222222222222222222222222222222","state":"CHANGES_REQUESTED"},{"id":2,"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"'$HEAD_OK'","state":"APPROVED"}]' ;;
      approval_legacy_ids)
        printf '%s\n' '[{"user":{"login":"kody-ai[bot]"},"commit_id":"'$HEAD_OK'","state":"APPROVED"}]' ;;
      approval_reverse_dismissed) printf '%s\n' '[{"id": 2, "user": {"id": 10, "login": "kody-ai[bot]"}, "commit_id": "1111111111111111111111111111111111111111", "state": "DISMISSED", "submitted_at": "2026-10-30T13:20:00Z"}]' '[{"id": 1, "user": {"id": 10, "login": "kody-ai[bot]"}, "commit_id": "1111111111111111111111111111111111111111", "state": "APPROVED", "submitted_at": "2026-10-30T13:20:00Z"}]' ;;
      approval_reverse_approved) printf '%s\n' '[{"id": 2, "user": {"id": 10, "login": "kody-ai[bot]"}, "commit_id": "1111111111111111111111111111111111111111", "state": "APPROVED", "submitted_at": "2026-10-30T13:20:00Z"}]' '[{"id": 1, "user": {"id": 10, "login": "kody-ai[bot]"}, "commit_id": "1111111111111111111111111111111111111111", "state": "CHANGES_REQUESTED", "submitted_at": "2026-10-30T13:20:00Z"}]' ;;
      approval_held)
        printf '%s\n' '[{"id":1,"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"'$HEAD_OK'","state":"APPROVED","submitted_at":"2026-10-30T13:20:00Z"}]' ;;
      record_dismissed)
        printf '%s\n' '[{"id":1,"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"'$HEAD_OK'","state":"DISMISSED","submitted_at":"2026-10-30T13:40:00Z"}]' ;;
      commented_record|thread_reopened|threads_next_page|threads_fail)
        printf '%s\n' '[{"id":1,"user":{"id":10,"login":"kody-ai[bot]"},"commit_id":"'$HEAD_OK'","state":"COMMENTED","submitted_at":"2026-10-30T13:20:00Z"}]' ;;
      *) printf '%s\n' '[]' ;;
    esac
    exit 0
    ;;
  *check-runs*)
    case "$GH_FAKE_MODE" in
      kody_started_after) printf '%s\n' '{"app":"kody-ai","app_id":413034,"name":"Kody Code Review","status":"queued"}' ;;
      runs_read_fails) echo "gh: api down" >&2; exit 1 ;;
      *) printf '%s\n' '{"app":"kody-ai","app_id":413034,"name":"Kody Code Review","status":"completed"}' ;;
    esac
    exit 0
    ;;
  *issues/1/comments*)
    case "$GH_FAKE_MODE" in
      requests_fail|marker_comments_fails) echo "gh: api down" >&2; exit 1 ;;
    esac
    # Raw per-comment @json lines, as `gh api ... --jq '.[] | @json'` emits
    # them: the guard's own jq pass does the marker/request reduction, so
    # the tests exercise the real filter rather than a pre-reduced answer.
    case "$GH_FAKE_MODE" in
      multiline_request_drift) printf '%s\n' '{"created_at": "2026-10-30T14:00:00Z", "user": {"type": "User"}, "body": "Please review.\\n@kody start-review"}' ;;
      request_drift) printf '%s\n' '{"created_at":"2026-10-30T14:00:00Z","user":{"type":"User"},"body":"@kody start-review"}' ;;
      *) printf '%s\n' '{"created_at":"2026-10-30T13:00:00Z","user":{"type":"User"},"body":"@kody start-review"}' ;;
    esac
    case "$GH_FAKE_MODE" in
      marker_active|marker_status_fails) printf '%s\n' '{"created_at":"2026-10-30T10:00:00Z","user":{"type":"Bot"},"body":"swu-autofix-run: bot=x run at https://github.com/x/actions/runs/555"}' ;;
    esac
    exit 0
    ;;
  *actions/runs/555*)
    case "$GH_FAKE_MODE" in
      marker_status_fails) echo "gh: api down (HTTP 500)" >&2; exit 1 ;;
      marker_active) printf '%s\n' '{"status":"in_progress"}' ;;
      *) printf '%s\n' '{"status":"completed"}' ;;
    esac
    exit 0
    ;;
  *actions/runs*)
    case "$GH_FAKE_MODE" in
      autofix_linked)
        if [[ "$*" == *status=queued* ]]; then
          printf '%s\n' '{"path":".github/workflows/kody-fleet-autofix.yml","prs":[1]}'
        fi
        ;;
    esac
    exit 0
    ;;
  *pulls/*)
    SHA="$HEAD_OK"
    DRAFT=false
    LABELS='[]'
    case "$GH_FAKE_MODE" in
      pr_read_fails) echo "gh: api down" >&2; exit 1 ;;
      head_drift) SHA="3333333333333333333333333333333333333333" ;;
      became_draft) DRAFT=true ;;
      hold_appeared) LABELS='[{"name":"do-not-automerge"}]' ;;
    esac
    STATE="open"
    [ "$GH_FAKE_MODE" = "not_open" ] && STATE="merged"
    printf '{"state":"%s","head":{"sha":"%s","ref":"feature/x","repo":{"full_name":"Simple-With-Us/Example"}},"draft":%s,"title":"x","labels":%s}\n' "$STATE" "$SHA" "$DRAFT" "$LABELS"
    exit 0
    ;;
  *graphql*)
    case "$GH_FAKE_MODE" in
      threads_fail) echo "gh: api down" >&2; exit 1 ;;
      threads_next_page) printf '%s\n' 'true 0' ;;
      thread_reopened) printf '%s\n' 'false 1' ;;
      *) printf '%s\n' 'false 0' ;;
    esac
    exit 0
    ;;
esac
case "$*" in
  *--disable-auto*) exit 0 ;;
esac
echo "gh: arm-guard fake does not answer: $*" >&2
exit 1
"""

GUARD_STEP_NAME = "Re-verify PR state at the arming boundary"


def run_arm_guard(
    mode: str,
    classified_head: str = "1" * 40,
    classified_request: str = "2026-10-30T13:00:00Z",
    classified_receipt: str = "124599873261",
    review_gate: str = "",
    restrict: str = "",
) -> tuple[int, str, list[str]]:
    """Run the arming-boundary guard step with a fake `gh`; return (exit code, combined output)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        fake = root / "gh"
        fake.write_text(FAKE_GH_ARM_GUARD, encoding="utf-8")
        fake.chmod(0o755)
        script = root / "guard.sh"
        script.write_text(step_script(GUARD_STEP_NAME), encoding="utf-8")
        env = dict(os.environ)
        env["PATH"] = f"{root}:{env.get('PATH', '')}"
        env.update(
            {
                "GH_FAKE_MODE": mode,
                "GH_CALLS": str(root / "calls.txt"),
                "GH_TOKEN": "fake-token",
                "FALLBACK_TOKEN": "fake-token",
                "RUNNER_TEMP": str(root),
                "REPO": "Simple-With-Us/Example",
                "PR_NUMBER": "1",
                "PR_URL": "https://github.com/example/repo/pull/1",
                "CLASSIFIED_HEAD": classified_head,
                "CLASSIFIED_REQUEST": classified_request,
                "CLASSIFIED_RECEIPT": classified_receipt,
                "REVIEW_GATE": review_gate,
                "APPROVAL_REVIEWER": "kody-ai[bot]",
                "KODY_APP_ID": "413034",
                "KODY_CHECK_NAME": "Kody Code Review",
                "KODY_APP_SLUG": "kody-ai",
                "REVIEW_REQUEST_PATTERN": "^[ \t]*@kody[ \t]+start-review([ \t\r\n]|$)",
                "HOLD_LABELS": "do-not-automerge,needs-human-merge",
                "RESTRICT": restrict,
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
        calls = root / "calls.txt"
        recorded = []
        if calls.exists():
            recorded = [line for line in calls.read_text(encoding="utf-8").splitlines() if line]
        return proc.returncode, proc.stdout + proc.stderr, recorded


class ArmBoundaryGuard(unittest.TestCase):
    """Contract item 6: drift between classification and arming aborts the job."""

    def assert_guard_aborts(self, mode: str, fragment: str, **kwargs) -> None:
        code, out, recorded = run_arm_guard(mode, **kwargs)
        self.assertEqual(code, 1, out)
        self.assertIn(fragment, out)
        disarm_calls = [c for c in recorded if "--disable-auto" in c]
        self.assertEqual(
            len(disarm_calls),
            1,
            f"abort must explicitly disarm a prior arm: {recorded}",
        )

    def test_clean_state_passes(self) -> None:
        code, out, recorded = run_arm_guard("clean")
        self.assertEqual(code, 0, out)
        self.assertIn("Arming boundary re-verified", out)
        self.assertFalse([c for c in recorded if "--disable-auto" in c], recorded)

    def test_head_drift_aborts(self) -> None:
        self.assert_guard_aborts("head_drift", "head drifted")

    def test_new_draft_aborts(self) -> None:
        self.assert_guard_aborts("became_draft", "became a draft")

    def test_hold_label_appearing_aborts(self) -> None:
        self.assert_guard_aborts("hold_appeared", "hold label")

    def test_newer_review_request_aborts(self) -> None:
        self.assert_guard_aborts("request_drift", "review request drifted", review_gate="effective")

    def test_kody_run_starting_after_classification_aborts(self) -> None:
        self.assert_guard_aborts("kody_started_after", "started running after classification")

    def test_marker_autofix_run_becoming_active_aborts(self) -> None:
        self.assert_guard_aborts("marker_active", "autofix run 555 in_progress")

    def test_repo_wide_autofix_run_linking_pr_aborts(self) -> None:
        self.assert_guard_aborts("autofix_linked", "autofix run became active for PR #1")

    def test_reopened_thread_aborts(self) -> None:
        self.assert_guard_aborts(
            "thread_reopened", "reopened after classification", review_gate="effective"
        )

    def test_thread_pagination_aborts(self) -> None:
        self.assert_guard_aborts("threads_next_page", "pagination", review_gate="effective")

    def test_pr_closed_or_merged_aborts(self) -> None:
        self.assert_guard_aborts("not_open", "no longer open")

    def test_receipt_invalidated_at_boundary_aborts(self) -> None:
        self.assert_guard_aborts("receipt_gone_neutral", "no longer validates")
        self.assert_guard_aborts("receipt_head_mismatch", "no longer validates")
        self.assert_guard_aborts("receipt_fetch_fails", "receipt check-run re-read failed")

    def test_changes_requested_after_classification_aborts(self) -> None:
        code, out, _ = run_arm_guard("cr_after_classification", review_gate="effective")
        self.assertEqual(code, 1, out)
        self.assertIn("requested changes after classification", out)

    def test_dismissed_head_approval_aborts(self) -> None:
        code, out, _ = run_arm_guard(
            "approval_dismissed", classified_receipt="", review_gate="approval"
        )
        self.assertEqual(code, 1, out)
        self.assertIn("dismissed or superseded", out)

    def test_held_head_approval_passes(self) -> None:
        code, out, _ = run_arm_guard("approval_held", classified_receipt="", review_gate="approval")
        self.assertEqual(code, 0, out)

    def test_require_approval_override_reaches_the_boundary(self) -> None:
        # classify resolves require_current_approval=true to the approval gate
        # and the boundary must re-check under THAT gate, not the raw input:
        # the same dismissal drift passes an effective re-check but must abort
        # under approval.
        outputs: dict = {}
        _code, _output, _reason, _ = run_classify_full(
            "clear", review_gate="effective", require_approval="true", outputs=outputs
        )
        self.assertEqual(outputs.get("resolved_gate"), "approval")
        code, out, _ = run_arm_guard(
            "approval_dismissed", classified_receipt="", review_gate="approval"
        )
        self.assertEqual(code, 1, out)
        self.assertIn("dismissed or superseded", out)
        code, out, _ = run_arm_guard(
            "approval_dismissed", classified_receipt="", review_gate="effective"
        )
        self.assertEqual(code, 0, out)  # the bug class this wiring prevents

    def test_the_legacy_empty_gate_boundary_ignores_a_historical_request(self) -> None:
        # Host probe 2026-10-31: actual classify outputs under
        # require_current_approval: false + review_gate unset are an empty
        # gate, empty request and empty receipt.  A historical request comment
        # (classified none, live present) must not abort the legacy contract.
        code, out, _ = run_arm_guard(
            "request_drift",
            classified_request="",
            classified_receipt="",
            review_gate="",
        )
        self.assertEqual(code, 0, out)
        self.assertIn("Arming boundary re-verified", out)

    def test_the_legacy_empty_gate_boundary_ignores_unresolved_threads(self) -> None:
        code, out, _ = run_arm_guard(
            "thread_reopened",
            classified_request="",
            classified_receipt="",
            review_gate="",
        )
        self.assertEqual(code, 0, out)
        self.assertIn("Arming boundary re-verified", out)

    def test_the_effective_boundary_rederives_the_record_prerequisite(self) -> None:
        # Host probe 2026-10-31: a valid receipt with the qualifying review
        # record gone live returned 0 before the fix; it must abort now.
        code, out, _ = run_arm_guard("clean", review_gate="effective")
        self.assertEqual(code, 1, out)
        self.assertIn("dismissed or removed after classification", out)

    def test_a_dismissed_only_record_fails_the_effective_boundary(self) -> None:
        code, out, _ = run_arm_guard("record_dismissed", review_gate="effective")
        self.assertEqual(code, 1, out)
        self.assertIn("dismissed or removed after classification", out)

    def test_a_current_head_commented_record_passes_the_effective_boundary(
        self,
    ) -> None:
        code, out, _ = run_arm_guard("commented_record", review_gate="effective")
        self.assertEqual(code, 0, out)

    def test_approval_boundary_matches_classify_scope(self):
        for mode in ("approval_other_old_cr", "approval_legacy_ids"):
            code, out, _ = run_arm_guard(mode, classified_receipt="", review_gate="approval")
            self.assertEqual(code, 0, out)

    def test_approval_classifier_orders_review_ids_across_reversed_pages(self):
        for mode, expected in (
            ("appr_reverse_dismissed", "no current-head approval"),
            ("appr_reverse_approved", "compare API failed"),
        ):
            code, out, reason, _ = run_classify_full(
                mode, require_approval="true", request_pattern=""
            )
            self.assertEqual(code, 0, out)
            self.assertIn(expected, reason)

    def test_approval_boundary_orders_review_ids_across_reversed_pages(self):
        code, out, _ = run_arm_guard(
            "approval_reverse_dismissed", classified_receipt="", review_gate="approval"
        )
        self.assertEqual(code, 1, out)
        self.assertIn("dismissed or superseded", out)
        code, out, _ = run_arm_guard(
            "approval_reverse_approved", classified_receipt="", review_gate="approval"
        )
        self.assertEqual(code, 0, out)

    def test_multiline_request_invalidates_classify_and_boundary(self):
        import yaml

        pattern = yaml.safe_load(WORKFLOW.read_text())[True]["workflow_call"]["inputs"][
            "review_request_pattern"
        ]["default"]
        code, out, reason, _ = run_classify_full(
            "multiline_request",
            review_gate="effective",
            require_approval="false",
            request_pattern=pattern,
        )
        self.assertEqual(code, 0, out)
        self.assertIn("no completed full-review receipt", reason)
        code, out, _ = run_arm_guard("multiline_request_drift", review_gate="effective")
        self.assertEqual(code, 1, out)
        self.assertIn("review request", out)

    def test_effective_boundary_keeps_other_reviewers_outstanding_requests(self):
        code, out, _ = run_arm_guard("approval_other_old_cr", review_gate="effective")
        self.assertEqual(code, 1, out)
        self.assertIn("requested changes", out)

    def test_arm_fallback_has_the_reads_the_boundary_uses(self):
        import yaml

        parsed = yaml.safe_load(WORKFLOW.read_text())
        perms = parsed["jobs"]["arm"]["permissions"]
        for scope in ("checks", "actions", "issues"):
            self.assertEqual(perms.get(scope), "read")

    def test_default_request_pattern_ignores_kody_guide_mentions(self):
        import yaml

        parsed = yaml.safe_load(WORKFLOW.read_text())
        # PyYAML1.1 reads on as True; input text itself remains YAML1.2.
        pattern = parsed[True]["workflow_call"]["inputs"]["review_request_pattern"]["default"]
        examples = [
            ("@kody start-review\n\nReview complete head", True),
            ("  @kody start-review", True),
            ("Please check this.\n@kody start-review", True),
            ("Please check this.\r\n\t@kody start-review", True),
            ("Intro\n- use `@kody start-review`", False),
            (
                "# Kody Review Complete\n- Request a Review: use `@kody start-review`",
                False,
            ),
            ("@kody start-review-not-a-command", False),
        ]
        for body, expected in examples:
            proc = subprocess.run(
                [
                    "jq",
                    "-nr",
                    "--arg",
                    "body",
                    body,
                    "--arg",
                    "pattern",
                    pattern,
                    '$body | split("\n") | any(.[]; test($pattern; "i"))',
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(proc.stdout.strip(), str(expected).lower())

    def test_api_uncertainty_aborts(self) -> None:
        for mode, fragment in (
            ("pr_read_fails", "PR re-read failed"),
            ("runs_read_fails", "check-run re-read failed"),
            ("marker_comments_fails", "comment re-read failed"),
            ("marker_status_fails", "autofix run status lookup failed"),
            # One combined comment read now (Kody 4433065484): its failure
            # aborts with the same message with or without a review gate.
            ("requests_fail", "comment re-read failed"),
        ):
            with self.subTest(mode=mode):
                self.assert_guard_aborts(mode, fragment)
        # Thread re-reads exist only under a non-empty gate.
        for mode, fragment in (("threads_fail", "thread re-read failed"),):
            with self.subTest(mode=mode):
                self.assert_guard_aborts(mode, fragment, review_gate="effective")

    def test_receipt_started_fractionally_after_the_request_still_validates(self):
        # Kody 4433064857: "13:00:00.900Z" sorts BEFORE "13:00:00Z" as text
        # ('.' < 'Z'), so a string compare rejects a receipt that actually
        # started after the request; epochs accept it.
        code, out, _ = run_arm_guard(
            "receipt_fractional_started", classified_request="2026-10-30T13:00:00Z"
        )
        self.assertEqual(code, 0, out)
        self.assertIn("Arming boundary re-verified", out)

    def test_receipt_started_before_a_fractional_request_aborts(self):
        # The dangerous direction: a run started at 13:00:00Z answers a
        # 13:00:00.500Z request under a string compare; epochs reject it.
        self.assert_guard_aborts(
            "receipt_fractional_stale",
            "no longer validates",
            classified_request="2026-10-30T13:00:00.500Z",
        )

    def test_a_pr_that_left_the_restricted_lane_aborts(self):
        # Kody 4433065293: classify proved the lane on the event payload;
        # the boundary must re-prove it on the LIVE ref/title.
        self.assert_guard_aborts(
            "clean",
            "left the restricted lane",
            classified_receipt="",
            restrict="dependabot",
        )

    def test_a_pr_still_in_the_restricted_lane_passes(self):
        code, out, _ = run_arm_guard(
            "clean", classified_request="", classified_receipt="", restrict="feature"
        )
        self.assertEqual(code, 0, out)
        self.assertIn("Arming boundary re-verified", out)

    def test_the_pr_comments_are_read_once(self):
        # Kody 4433065484: the request re-check and the marker scan share
        # one paginated read; the old code read the whole comment history
        # twice.
        code, out, recorded = run_arm_guard("commented_record", review_gate="effective")
        self.assertEqual(code, 0, out)
        comments_calls = [c for c in recorded if "issues/1/comments" in c]
        self.assertEqual(len(comments_calls), 1, recorded)


def _gh_api_lines(path: Path):
    """Logical `gh api` invocations (line-continuations joined)."""
    cmds = []
    current = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        s = raw.strip()
        if current is not None:
            current += " " + s.rstrip("\\").strip()
            if not s.endswith("\\"):
                cmds.append(current)
                current = None
            continue
        if "gh api " in s:
            if s.endswith("\\"):
                current = s.rstrip("\\").strip()
            else:
                cmds.append(s)
    if current is not None:
        cmds.append(current)
    return cmds


class HttpMethodSource(unittest.TestCase):
    """gh api defaults to POST when -f fields are present; every REST read in
    the reusable and the caller template must pin --method GET explicitly.
    GraphQL is POST by design and exempt."""

    def check_file(self, path: Path) -> None:
        for cmd in _gh_api_lines(path):
            if "graphql" in cmd:
                continue
            if " -f " in cmd or cmd.rstrip("\\").endswith(" -f"):
                self.assertIn(
                    "--method GET",
                    cmd,
                    f"read with -f fields lacks --method GET: {cmd}",
                )

    def test_reusable_reads_pin_get(self):
        self.check_file(WORKFLOW)


if __name__ == "__main__":
    unittest.main()
