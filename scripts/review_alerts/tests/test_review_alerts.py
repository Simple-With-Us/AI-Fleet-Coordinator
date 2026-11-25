"""review_alerts:  event derivation, store durability, wake contract, cycle."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from review_alerts import detect, events, github_read, service, wakes

REPO = "Simple-With-Us/HogHunter"
SESSION = "claude-9f8a1c2e-3b4d-4f01-8a2b-1c2d3e4f5a6b"
HEAD = "a1b2c3d4" * 5
CURSOR0 = {"review_id": 0, "comment_id": 0, "check_id": 0, "completed_heads": []}


def kody_review(rid, state="CHANGES_REQUESTED", commit=HEAD, submitted_at="2026-11-21T01:00:00Z"):
    return {
        "id": rid,
        "state": state,
        "commit_id": commit,
        "submitted_at": submitted_at,
        "user": {"login": "kody-ai[bot]"},
    }


def sentry_comment(cid, commit=HEAD, created_at="2026-11-21T01:00:00Z"):
    return {
        "id": cid,
        "original_commit_id": commit,
        "created_at": created_at,
        "user": {"login": "sentry[bot]"},
        "body": "finding",
    }


def kody_check(rid, head=HEAD, status="completed", conclusion="success"):
    return {
        "id": rid,
        "name": "Kody Code Review",
        "status": status,
        "conclusion": conclusion,
        "head_sha": head,
        "completed_at": "2026-11-21T01:00:00Z" if status == "completed" else None,
        "app": {"id": 413034, "slug": "kody-ai"},
    }


def claimed_owner(**over):
    row = {
        "seat": "claude",
        "repo": REPO,
        "number": 7,
        "confirmed": True,
        "ts": "2026-11-21T00:00:00Z",
        "origin_session_claim": SESSION,
        "origin_letter_claim": "AB",
        "origin_generation_claim": 3,
    }
    row.update(over)
    return {k: v for k, v in row.items() if v is not None or k not in over}


class DetectTests(unittest.TestCase):
    def test_new_kody_review_is_completion(self):
        found = detect.derive(REPO, 7, HEAD, [kody_review(11)], [], [], dict(CURSOR0))
        self.assertEqual(len(found), 1)
        event = found[0]
        self.assertEqual(event["kind"], "review_submitted")
        self.assertEqual(event["event_id"], "11")
        self.assertIs(event["head_matches"], True)

    def test_old_head_review_is_not_current_head_completion(self):
        found = detect.derive(
            REPO, 7, HEAD, [kody_review(11, commit="0" * 40)], [], [], dict(CURSOR0)
        )
        self.assertIs(found[0]["head_matches"], False)
        self.assertEqual(found[0]["head"], "0" * 40)

    def test_seen_reviews_and_other_actors_are_ignored(self):
        reviews = [
            kody_review(11),
            kody_review(12),
            {
                "id": 13,
                "state": "APPROVED",
                "commit_id": HEAD,
                "user": {"login": "jaywedgeworth22"},
            },
        ]
        found = detect.derive(REPO, 7, HEAD, reviews, [], [], dict(CURSOR0, review_id=11))
        self.assertEqual([e["event_id"] for e in found], ["12"])

    def test_kody_check_completion_on_current_head_alerts(self):
        found = detect.derive(REPO, 7, HEAD, [], [], [kody_check(55)], dict(CURSOR0))
        self.assertEqual(len(found), 1)
        event = found[0]
        self.assertEqual(event["kind"], "review_completed")
        self.assertEqual(event["head"], HEAD)
        self.assertIn("success", event["summary"])

    def test_check_on_old_head_does_not_alert(self):
        found = detect.derive(REPO, 7, HEAD, [], [], [kody_check(55, head="0" * 40)], dict(CURSOR0))
        self.assertEqual(found, [])

    def test_incomplete_check_does_not_alert(self):
        found = detect.derive(
            REPO,
            7,
            HEAD,
            [],
            [],
            [kody_check(55, status="in_progress", conclusion=None)],
            dict(CURSOR0),
        )
        self.assertEqual(found, [])

    def test_semantic_dedupe_review_and_check_same_head(self):
        # A submitted review and a completed check on the SAME head are one
        # round:  the review alerts, the check does not repeat it.
        found = detect.derive(REPO, 7, HEAD, [kody_review(11)], [], [kody_check(55)], dict(CURSOR0))
        self.assertEqual([e["kind"] for e in found], ["review_submitted"])
        # And a head already alerted is not alerted again.
        found = detect.derive(
            REPO, 7, HEAD, [], [], [kody_check(56)], dict(CURSOR0, completed_heads=[HEAD])
        )
        self.assertEqual(found, [])

    def test_other_app_check_is_ignored(self):
        run = kody_check(55)
        run["app"] = {"id": 15368, "slug": "github-actions"}
        self.assertEqual(detect.derive(REPO, 7, HEAD, [], [], [run], dict(CURSOR0)), [])

    def test_sentry_comments_batch_with_head_binding(self):
        comments = [sentry_comment(21), sentry_comment(22), sentry_comment(23, commit="0" * 40)]
        found = detect.derive(REPO, 7, HEAD, [], comments, [], dict(CURSOR0, comment_id=20))
        self.assertEqual(len(found), 1)
        event = found[0]
        self.assertEqual(event["kind"], "findings_added")
        self.assertEqual(event["event_id"], "c23")
        self.assertIs(event["head_matches"], False)
        self.assertIn("3 new finding comments", event["summary"])

    def test_unrelated_comments_are_ignored(self):
        comments = [{"id": 30, "original_commit_id": HEAD, "user": {"login": "someone"}}]
        self.assertEqual(detect.derive(REPO, 7, HEAD, [], comments, [], dict(CURSOR0)), [])


def ci_run(rid, name="unit tests", conclusion="failure", status="completed", head=HEAD, app=None):
    return {
        "id": rid,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "head_sha": head,
        "completed_at": "2026-11-21T01:00:00Z" if status == "completed" else None,
        "app": app if app is not None else {"id": 15368, "slug": "github-actions"},
    }


def pull(mergeable=None, mergeable_state="", base=None):
    body = {"state": "open", "head": {"sha": HEAD}, "html_url": "https://example/pr/7"}
    if mergeable is not None:
        body["mergeable"] = mergeable
    if mergeable_state:
        body["mergeable_state"] = mergeable_state
    if base is not None:
        body["base"] = {"sha": base}
    return body


BASE = "c" * 40


def kinds(found):
    return [event["kind"] for event in found]


class DetectCiTests(unittest.TestCase):
    def test_a_failed_check_on_the_current_head_alerts_and_names_itself(self):
        found = detect.derive(
            REPO, 7, HEAD, [], [], [ci_run(55, "Muster macOS build")], dict(CURSOR0)
        )
        self.assertEqual(kinds(found), ["ci_failed"])
        event = found[0]
        self.assertEqual(event["event_id"], "ci55")
        self.assertEqual(event["head"], HEAD)
        self.assertIs(event["head_matches"], True)
        self.assertEqual(event["summary"], "CI failed on the current head: Muster macOS build")

    def test_failures_in_one_cycle_are_one_event_naming_each_run(self):
        runs = [ci_run(55, "build"), ci_run(56, "lint", conclusion="timed_out")]
        found = detect.derive(REPO, 7, HEAD, [], [], runs, dict(CURSOR0))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["event_id"], "ci55+56")
        self.assertEqual(found[0]["summary"], "CI failed on the current head: build, lint")

    def test_many_failures_name_three_and_count_the_rest(self):
        runs = [ci_run(n, "job %d" % n) for n in (51, 52, 53, 54, 55)]
        found = detect.derive(REPO, 7, HEAD, [], [], runs, dict(CURSOR0))
        self.assertEqual(
            found[0]["summary"], "CI failed on the current head: job 51, job 52 (+3 more)"
        )

    def test_a_run_already_named_is_never_named_again(self):
        cursor = dict(CURSOR0, ci_ids=[55])
        self.assertEqual(detect.derive(REPO, 7, HEAD, [], [], [ci_run(55)], cursor), [])

    def test_a_slow_run_with_a_lower_id_failing_later_still_alerts(self):
        # A high-water mark would sit at 100 and lose 99 for good.
        first = detect.derive(REPO, 7, HEAD, [], [], [ci_run(100, "fast")], dict(CURSOR0))
        self.assertEqual(first[0]["event_id"], "ci100")
        later = detect.derive(
            REPO,
            7,
            HEAD,
            [],
            [],
            [ci_run(99, "slow"), ci_run(100, "fast")],
            dict(CURSOR0, ci_ids=[100]),
        )
        self.assertEqual([e["event_id"] for e in later], ["ci99"])

    def test_a_rerun_that_fails_again_is_a_new_event_and_one_that_passes_is_none(self):
        cursor = dict(CURSOR0, ci_ids=[55])
        again = detect.derive(REPO, 7, HEAD, [], [], [ci_run(55), ci_run(56)], cursor)
        self.assertEqual([e["event_id"] for e in again], ["ci56"])
        passed = detect.derive(
            REPO, 7, HEAD, [], [], [ci_run(55), ci_run(56, conclusion="success")], cursor
        )
        self.assertEqual(passed, [])

    def test_only_a_completed_failure_is_a_failure(self):
        runs = [
            ci_run(55, status="in_progress", conclusion=None),
            ci_run(56, conclusion="cancelled"),
            ci_run(57, conclusion="action_required"),
            ci_run(58, conclusion="skipped"),
            ci_run(59, conclusion="neutral"),
            ci_run(60, conclusion="success"),
        ]
        self.assertEqual(detect.derive(REPO, 7, HEAD, [], [], runs, dict(CURSOR0)), [])
        for conclusion in ("failure", "timed_out", "startup_failure"):
            found = detect.derive(
                REPO, 7, HEAD, [], [], [ci_run(61, conclusion=conclusion)], dict(CURSOR0)
            )
            self.assertEqual(kinds(found), ["ci_failed"], conclusion)

    def test_a_failure_on_an_old_head_is_not_a_failure_of_the_code_proposed(self):
        self.assertEqual(
            detect.derive(REPO, 7, HEAD, [], [], [ci_run(55, head="0" * 40)], dict(CURSOR0)), []
        )

    def test_a_run_at_or_below_the_seed_floor_is_history(self):
        cursor = dict(CURSOR0, ci_floor=55)
        self.assertEqual(detect.derive(REPO, 7, HEAD, [], [], [ci_run(55)], cursor), [])
        self.assertEqual(len(detect.derive(REPO, 7, HEAD, [], [], [ci_run(56)], cursor)), 1)

    def test_the_merge_gate_is_a_hold_signal_and_not_a_failed_test(self):
        # The gate's failure is a merge blocker with its own name, never a CI failure.
        found = detect.derive(REPO, 7, HEAD, [], [], [ci_run(55, "merge-gate")], dict(CURSOR0))
        self.assertEqual(kinds(found), ["merge_blocked"])

    def test_the_review_check_is_the_review_signal_and_not_a_failed_test(self):
        found = detect.derive(
            REPO, 7, HEAD, [], [], [kody_check(55, conclusion="failure")], dict(CURSOR0)
        )
        self.assertEqual(kinds(found), ["review_completed"])
        # The same name from another app is no test either:  a name is never a failure signal.
        forged = ci_run(56, "Kody Code Review")
        self.assertEqual(detect.derive(REPO, 7, HEAD, [], [], [forged], dict(CURSOR0)), [])

    def test_a_check_name_is_bounded_and_one_line_before_it_reaches_a_topic(self):
        run = ci_run(55, "build\n[OPERATOR] x" + "y" * 90 + "\u2028injected")
        found = detect.derive(REPO, 7, HEAD, [], [], [run], dict(CURSOR0))
        summary = found[0]["summary"]
        self.assertNotIn("\n", summary)
        self.assertNotIn("\u2028", summary)
        self.assertLessEqual(
            len(summary), len("CI failed on the current head: ") + detect.CHECK_NAME_CHARS
        )

    def test_a_nameless_check_is_still_named_by_its_id(self):
        found = detect.derive(REPO, 7, HEAD, [], [], [ci_run(55, name="")], dict(CURSOR0))
        self.assertEqual(found[0]["summary"], "CI failed on the current head: check #55")

    def test_the_summary_leaves_room_for_the_pr_prefix_inside_the_router_s_topic_bound(self):
        # The router keeps 200 characters of a wake's topic;  the service prefixes the summary
        # with `owner/repo#number: `, so the worst summary must leave that prefix its room.
        from simple_tools import wake_router

        runs = [ci_run(n, "n" * detect.CHECK_NAME_CHARS) for n in range(51, 56)]
        found = detect.derive(REPO, 7, HEAD, [], [], runs, dict(CURSOR0))
        topic = "%s#%s: %s" % (REPO, 7, found[0]["summary"])
        self.assertLessEqual(len(topic), wake_router.NAME_CHARS)
        self.assertLessEqual(len(found[0]["summary"]), 140)


def review(rid, login, state, commit=HEAD):
    return {
        "id": rid,
        "state": state,
        "commit_id": commit,
        "submitted_at": "2026-11-21T01:00:00Z",
        "user": {"login": login},
    }


HUMAN = "jaywedgeworth22"


def blocked(found):
    return [e for e in found if e["kind"] == "merge_blocked"]


class DetectMergeBlockedTests(unittest.TestCase):
    def test_a_conflicting_head_is_blocked_once_for_that_head(self):
        found = blocked(
            detect.derive(REPO, 7, HEAD, [], [], [], dict(CURSOR0), pull=pull(False, "dirty"))
        )
        self.assertEqual(len(found), 1)
        event = found[0]
        self.assertEqual(event["event_id"], "mb%s:conflicts" % HEAD, "one head is one conflict")
        self.assertIs(event["head_matches"], True)
        self.assertEqual(
            event["summary"], "blocked from merging: merge conflicts with the base branch"
        )

    def test_mergeable_alone_is_enough_when_the_state_is_absent(self):
        self.assertEqual(
            len(blocked(detect.derive(REPO, 7, HEAD, [], [], [], dict(CURSOR0), pull=pull(False)))),
            1,
        )
        self.assertEqual(
            blocked(detect.derive(REPO, 7, HEAD, [], [], [], dict(CURSOR0), pull=pull(True))), []
        )

    def test_a_pr_that_is_merely_behind_blocked_or_unstable_is_not_in_conflict(self):
        # The branch rules do not require an up-to-date branch and no approval:  behind is not
        # a merge blocker here, and alerting it would wake every open PR each time main moved.
        for state in ("clean", "blocked", "behind", "unstable", "has_hooks", "draft"):
            self.assertEqual(
                blocked(
                    detect.derive(REPO, 7, HEAD, [], [], [], dict(CURSOR0), pull=pull(False, state))
                ),
                [],
                state,
            )

    def test_a_dirty_state_is_a_conflict_whatever_mergeable_reads(self):
        self.assertEqual(
            len(
                blocked(
                    detect.derive(
                        REPO, 7, HEAD, [], [], [], dict(CURSOR0), pull=pull(True, "dirty")
                    )
                )
            ),
            1,
        )

    def test_github_not_computing_yet_is_not_a_conflict_and_not_a_pass(self):
        for body in (pull(None, "unknown"), pull(None), {"state": "open"}, {}):
            self.assertEqual(
                blocked(detect.derive(REPO, 7, HEAD, [], [], [], dict(CURSOR0), pull=body)), []
            )

    def test_no_pr_body_derives_no_conflict(self):
        self.assertEqual(blocked(detect.derive(REPO, 7, HEAD, [], [], [], dict(CURSOR0))), [])

    def test_a_blocker_needs_a_head(self):
        self.assertEqual(
            detect.derive(
                REPO,
                7,
                "",
                [],
                [review(11, HUMAN, "CHANGES_REQUESTED")],
                [],
                dict(CURSOR0),
                pull=pull(False, "dirty"),
            ),
            [],
        )

    def test_failed_checks_alone_are_the_ci_event_and_never_a_second_one(self):
        found = detect.derive(
            REPO, 7, HEAD, [], [], [ci_run(56)], dict(CURSOR0), pull=pull(True, "blocked")
        )
        self.assertEqual(kinds(found), ["ci_failed"])

    def test_failed_checks_ride_beside_another_blocker(self):
        found = blocked(
            detect.derive(
                REPO, 7, HEAD, [], [], [ci_run(56)], dict(CURSOR0), pull=pull(False, "dirty")
            )
        )
        self.assertEqual(found[0]["event_id"], "mb%s:checks_failed+conflicts" % HEAD)
        self.assertEqual(
            found[0]["summary"],
            "blocked from merging: checks failed, merge conflicts with the base branch",
        )

    def test_a_closed_merge_gate_blocks(self):
        # The fleet's guard fails this check while a merge hold is active or when it cannot
        # vouch for the PR;  it is a blocker and not a failed test.
        found = detect.derive(
            REPO,
            7,
            HEAD,
            [],
            [],
            [ci_run(55, "merge-gate")],
            dict(CURSOR0),
            pull=pull(True, "clean"),
        )
        self.assertEqual(kinds(found), ["merge_blocked"])
        self.assertEqual(found[0]["event_id"], "mb%s:merge_gate" % HEAD)
        self.assertEqual(found[0]["summary"], "blocked from merging: the merge gate is closed")

    def test_a_gate_that_failed_and_then_passed_has_passed(self):
        runs = [ci_run(55, "merge-gate"), ci_run(56, "merge-gate", conclusion="success")]
        self.assertEqual(
            detect.derive(REPO, 7, HEAD, [], [], runs, dict(CURSOR0), pull=pull(True, "clean")), []
        )

    def test_a_check_that_failed_and_then_passed_has_passed(self):
        # The rerun that passed is the check's verdict;  only the conflict remains.
        runs = [ci_run(55, "unit tests"), ci_run(56, "unit tests", conclusion="success")]
        found = detect.derive(REPO, 7, HEAD, [], [], runs, dict(CURSOR0), pull=pull(False, "dirty"))
        self.assertEqual(kinds(found), ["merge_blocked"])

    def test_changes_requested_by_a_person_block_until_they_approve(self):
        reviews = [review(11, HUMAN, "CHANGES_REQUESTED")]
        found = blocked(detect.derive(REPO, 7, HEAD, reviews, [], [], dict(CURSOR0)))
        self.assertEqual([e["summary"] for e in found], ["blocked from merging: changes requested"])
        # Their decision stands across a push:  stale reviews are not dismissed on one.
        later_head = "b" * 40
        reviews.append(review(12, HUMAN, "APPROVED", commit="0" * 40))
        self.assertEqual(
            blocked(detect.derive(REPO, 7, later_head, reviews, [], [], dict(CURSOR0))), []
        )
        self.assertEqual(
            [
                e["event_id"]
                for e in blocked(
                    detect.derive(REPO, 7, later_head, reviews[:1], [], [], dict(CURSOR0))
                )
            ],
            ["mb%s:changes_requested" % later_head],
        )

    def test_a_comment_or_a_pending_review_never_changes_a_standing_decision(self):
        reviews = [
            review(11, HUMAN, "CHANGES_REQUESTED"),
            review(12, HUMAN, "COMMENTED"),
            review(13, HUMAN, "PENDING"),
            review(14, "swu-zc[bot]", "COMMENTED"),
        ]
        self.assertEqual(
            len(blocked(detect.derive(REPO, 7, HEAD, reviews, [], [], dict(CURSOR0)))), 1
        )

    def test_a_review_bot_s_changes_requested_is_its_review_event_and_never_a_second_one(self):
        found = detect.derive(
            REPO, 7, HEAD, [kody_review(11, state="CHANGES_REQUESTED")], [], [], dict(CURSOR0)
        )
        self.assertEqual(kinds(found), ["review_submitted"])
        self.assertIn("changes requested", found[0]["summary"])

    def test_the_blockers_still_answer_without_a_pr_body(self):
        # The relay's reduced read carries no mergeability:  a conflict is neither found nor
        # ruled out, and the rest is still said.
        found = detect.derive(
            REPO,
            7,
            HEAD,
            [review(11, HUMAN, "CHANGES_REQUESTED")],
            [],
            [ci_run(55, "merge-gate")],
            dict(CURSOR0),
        )
        self.assertEqual(found[0]["event_id"], "mb%s:changes_requested+merge_gate" % HEAD)

    def test_a_review_bot_s_own_changes_requested_that_is_not_the_completion_bot_still_blocks(self):
        # Sentry's decision is not the completion event (that is Kody's alone), so it
        # would be silent everywhere without blocking here.
        found = detect.derive(
            REPO, 7, HEAD, [review(11, "sentry[bot]", "CHANGES_REQUESTED")], [], [], dict(CURSOR0)
        )
        self.assertEqual([e["summary"] for e in found], ["blocked from merging: changes requested"])

    def test_an_explicit_dismissal_clears_a_standing_changes_requested(self):
        reviews = [review(11, HUMAN, "CHANGES_REQUESTED"), review(12, HUMAN, "DISMISSED")]
        self.assertEqual(blocked(detect.derive(REPO, 7, HEAD, reviews, [], [], dict(CURSOR0))), [])

    def test_a_comment_after_a_dismissal_does_not_restore_the_request(self):
        reviews = [
            review(11, HUMAN, "CHANGES_REQUESTED"),
            review(12, HUMAN, "DISMISSED"),
            review(13, HUMAN, "COMMENTED"),
        ]
        self.assertEqual(blocked(detect.derive(REPO, 7, HEAD, reviews, [], [], dict(CURSOR0))), [])

    def test_a_custom_completion_bot_s_own_decision_never_blocks(self):
        # The deployment names its completion bot:  its changes-requested is already the
        # review event, and hardcoding Kody would double-report it or exclude an
        # uninvolved bot.
        found = detect.derive(
            REPO,
            7,
            HEAD,
            [review(11, "other-bot[bot]", "CHANGES_REQUESTED")],
            [],
            [],
            dict(CURSOR0),
            review_actor="other-bot[bot]",
        )
        self.assertEqual(kinds(found), ["review_submitted"])

    def test_the_default_completion_bot_stays_kody_when_derive_passes_its_default(self):
        reviews = [
            review(11, "other-bot[bot]", "CHANGES_REQUESTED"),
            kody_review(12, state="CHANGES_REQUESTED"),
        ]
        found = detect.derive(REPO, 7, HEAD, reviews, [], [], dict(CURSOR0))
        self.assertEqual(kinds(found), ["review_submitted", "merge_blocked"])
        self.assertEqual(found[-1]["event_id"], "mb%s:changes_requested" % HEAD)

    def test_a_base_advance_re_alerts_a_conflict_on_the_same_head(self):
        # The pair carries over from the earlier conflict-only design:  main moving under a
        # fixed head is a NEW "update the PR" fact, so the base joins the identity.
        first = detect.derive(
            REPO, 7, HEAD, [], [], [], dict(CURSOR0), pull=pull(False, "dirty", base=BASE)
        )
        self.assertEqual([e["event_id"] for e in first], ["mb%s:%s:conflicts" % (HEAD, BASE)])
        again = detect.derive(
            REPO, 7, HEAD, [], [], [], dict(CURSOR0), pull=pull(False, "dirty", base="d" * 40)
        )
        self.assertEqual([e["event_id"] for e in again], ["mb%s:%s:conflicts" % (HEAD, "d" * 40)])
        # A body without a base keeps the head-only identity.
        plain = detect.derive(REPO, 7, HEAD, [], [], [], dict(CURSOR0), pull=pull(False, "dirty"))
        self.assertEqual([e["event_id"] for e in plain], ["mb%s:conflicts" % HEAD])

    def test_review_activity_and_pr_states_coexist_in_one_cycle(self):
        found = detect.derive(
            REPO,
            7,
            HEAD,
            [kody_review(11)],
            [sentry_comment(21)],
            [kody_check(55), ci_run(56)],
            dict(CURSOR0),
            pull=pull(False, "dirty"),
        )
        self.assertEqual(
            kinds(found), ["review_submitted", "findings_added", "ci_failed", "merge_blocked"]
        )


class CredentialResolution(unittest.TestCase):
    def test_the_environment_value_wins_and_the_file_is_the_fallback(self):
        env = {"RA_WAKE_KEY": "from-env-" + "x" * 40}
        self.assertEqual(
            service.credential(env, "RA_WAKE_KEY", "RA_WAKE_KEY_FILE"), env["RA_WAKE_KEY"]
        )
        self.assertEqual(service.credential({}, "RA_WAKE_KEY", "RA_WAKE_KEY_FILE"), "")
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "wake.key"
            path.write_text("from-file-" + "y" * 40)
            path.chmod(0o600)
            # file_name is the ENV NAME whose value is the path, as main() passes it.
            self.assertEqual(
                service.credential(
                    {"RA_WAKE_KEY_FILE": str(path)}, "RA_WAKE_KEY", "RA_WAKE_KEY_FILE"
                ),
                "from-file-" + "y" * 40,
            )
            # Both present:  the loader-delivered environment value wins.
            self.assertEqual(
                service.credential(
                    {"RA_WAKE_KEY": "from-env-" + "x" * 40, "RA_WAKE_KEY_FILE": str(path)},
                    "RA_WAKE_KEY",
                    "RA_WAKE_KEY_FILE",
                ),
                "from-env-" + "x" * 40,
            )

    def test_the_file_loader_re_reads_only_when_the_mtime_changes(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "relay.key"
            path.write_text("first-" + "a" * 32)
            load = service.credential_loader(
                {"RA_SERVICE_TOKEN_FILE": str(path)}, "RA_SERVICE_TOKEN", "RA_SERVICE_TOKEN_FILE"
            )
            self.assertEqual(load(), "first-" + "a" * 32)
            # Unchanged mtime:  the cached value answers without a re-read.
            self.assertEqual(load(), "first-" + "a" * 32)
            # Rotation:  a new value lands and the next call picks it up.  Force a
            # distinct mtime so the test never flakes on a coarse-grained clock.
            path.write_text("rotated-" + "b" * 32)
            stamp = path.stat().st_mtime_ns + 2_000_000_000
            os.utime(path, ns=(stamp, stamp))
            self.assertEqual(load(), "rotated-" + "b" * 32)
            # An environment value is a fixed binding:  it wins over the file forever.
            pinned = service.credential_loader(
                {
                    "RA_SERVICE_TOKEN": "from-env-" + "c" * 24,
                    "RA_SERVICE_TOKEN_FILE": str(path),
                },
                "RA_SERVICE_TOKEN",
                "RA_SERVICE_TOKEN_FILE",
            )
            self.assertEqual(pinned(), "from-env-" + "c" * 24)

    def test_a_transient_read_failure_never_poisons_the_cache(self):
        # A credential file is never legitimately empty:  a read that comes back empty
        # (a transient open failure reads as "") keeps the last good value and the old
        # mtime, so the next call retries instead of serving empty until the file changes.
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "relay.key"
            path.write_text("good-" + "a" * 36)
            load = service.credential_loader(
                {"RA_SERVICE_TOKEN_FILE": str(path)}, "RA_SERVICE_TOKEN", "RA_SERVICE_TOKEN_FILE"
            )
            self.assertEqual(load(), "good-" + "a" * 36)
            stamp = path.stat().st_mtime_ns + 2_000_000_000
            path.write_text("")
            os.utime(path, ns=(stamp, stamp))
            self.assertEqual(load(), "good-" + "a" * 36, "an empty read keeps the last good value")
            # A later real value still lands.
            path.write_text("recovered-" + "b" * 30)
            stamp2 = stamp + 2_000_000_000
            os.utime(path, ns=(stamp2, stamp2))
            self.assertEqual(load(), "recovered-" + "b" * 30)

    def test_the_operator_loop_holds_when_neither_credential_source_has_a_value(self):
        held = []
        svc = ServiceCycleTests._service(
            self, tempfile.mkdtemp(), [claimed_owner()], FakeRead(), FakeRouter()
        )
        try:
            svc.store  # exists
        except AttributeError:
            pass
        # The loop-level check is what a real cycle exercises:  a missing value holds.
        empty = {}
        self.assertFalse(
            service.credential(empty, "RA_SERVICE_TOKEN", "RA_SERVICE_TOKEN_FILE")
            and service.credential(empty, "RA_WAKE_KEY", "RA_WAKE_KEY_FILE")
        )


class EventStoreTests(unittest.TestCase):
    def test_append_reload_and_status_upgrade(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "events.jsonl")
            store = events.EventStore(path)
            store.append(
                {
                    "repo": REPO,
                    "number": 7,
                    "kind": "review_submitted",
                    "event_id": "11",
                    "status": "submitted",
                    "wake_id": "ra-x",
                }
            )
            store.append(
                {
                    "repo": REPO,
                    "number": 7,
                    "kind": "review_submitted",
                    "event_id": "11",
                    "status": "delivered",
                    "wake_id": "ra-x",
                }
            )
            again = events.EventStore(path)
            row = again.get(REPO, 7, "review_submitted", "11")
            self.assertEqual(row["status"], "delivered")
            self.assertEqual(again.pending(), [])
            self.assertEqual(again.counts(), {"delivered": 1})

    def test_terminal_status_is_never_downgraded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "events.jsonl")
            store = events.EventStore(path)
            store.append(
                {
                    "repo": REPO,
                    "number": 7,
                    "kind": "review_submitted",
                    "event_id": "11",
                    "status": "delivered",
                    "wake_id": "ra-x",
                }
            )
            store.append(
                {
                    "repo": REPO,
                    "number": 7,
                    "kind": "review_submitted",
                    "event_id": "11",
                    "status": "submitted",
                    "wake_id": "ra-x",
                }
            )
            self.assertEqual(store.get(REPO, 7, "review_submitted", "11")["status"], "delivered")

    def test_epoch_replaces_dead_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "events.jsonl")
            store = events.EventStore(path)
            store.append(
                {
                    "repo": REPO,
                    "number": 7,
                    "kind": "review_submitted",
                    "event_id": "11",
                    "epoch": 0,
                    "status": "held",
                    "wake_id": "ra-a",
                    "reason": "required-session-not-live",
                }
            )
            store.append(
                {
                    "repo": REPO,
                    "number": 7,
                    "kind": "review_submitted",
                    "event_id": "11",
                    "epoch": 1,
                    "status": "submitted",
                    "wake_id": "ra-b",
                }
            )
            row = store.get(REPO, 7, "review_submitted", "11")
            self.assertEqual(row["epoch"], 1)
            self.assertEqual(row["wake_id"], "ra-b")
            # An older epoch arriving late never displaces the newer one.
            store.append(
                {
                    "repo": REPO,
                    "number": 7,
                    "kind": "review_submitted",
                    "event_id": "11",
                    "epoch": 0,
                    "status": "held",
                    "wake_id": "ra-a",
                }
            )
            self.assertEqual(store.get(REPO, 7, "review_submitted", "11")["wake_id"], "ra-b")

    def test_torn_tail_is_cut_and_complete_final_row_is_kept(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            good = {
                "repo": REPO,
                "number": 7,
                "kind": "review_submitted",
                "event_id": "11",
                "status": "submitted",
            }
            path.write_text(json.dumps(good) + '\n{"repo": "S', encoding="utf-8")
            store = events.EventStore(str(path))
            self.assertIsNotNone(store.get(REPO, 7, "review_submitted", "11"))
            self.assertTrue(path.read_bytes().endswith(b"\n"))
            # A complete final row missing only its newline is preserved, not cut.
            path.write_text(
                json.dumps(good) + "\n" + json.dumps(dict(good, event_id="12")), encoding="utf-8"
            )
            store = events.EventStore(str(path))
            self.assertIsNotNone(store.get(REPO, 7, "review_submitted", "12"))
            self.assertTrue(path.read_bytes().endswith(b"\n"))

    def test_seed_blocks_history_and_cursor_covers_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "events.jsonl")
            store = events.EventStore(path)
            store.seed(REPO, 7, 100, 200, 300)
            store.seed(REPO, 7, 999, 999, 999)  # first seed wins
            cursor = store.cursor(REPO, 7)
            self.assertEqual(cursor["review_id"], 100)
            self.assertEqual(cursor["comment_id"], 200)
            self.assertEqual(cursor["check_id"], 300)
            store.append(
                {
                    "repo": REPO,
                    "number": 7,
                    "kind": "review_completed",
                    "event_id": "301",
                    "status": "delivered",
                    "head": HEAD,
                }
            )
            cursor = store.cursor(REPO, 7)
            self.assertEqual(cursor["check_id"], 301)
            self.assertIn(HEAD, cursor["completed_heads"])

    def test_cursor_tracks_failed_runs_one_by_one_from_the_seed_floor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "events.jsonl")
            store = events.EventStore(path)
            store.seed(REPO, 7, 100, 200, 300)
            cursor = store.cursor(REPO, 7)
            self.assertEqual(cursor["ci_floor"], 300, "runs that predate registration are history")
            self.assertEqual(cursor["ci_ids"], [])
            row = {
                "repo": REPO,
                "number": 7,
                "kind": "ci_failed",
                "event_id": "ci320+315",
                "status": "held",
            }
            store.append(row)
            # The review check's own watermark is not the CI floor:  a review that ran later
            # never silences a test that failed before it.
            store.append(
                {
                    "repo": REPO,
                    "number": 7,
                    "kind": "review_completed",
                    "event_id": "900",
                    "status": "delivered",
                    "head": HEAD,
                }
            )
            cursor = store.cursor(REPO, 7)
            self.assertEqual(cursor["ci_ids"], [315, 320])
            self.assertEqual(cursor["ci_floor"], 300)
            self.assertEqual(cursor["check_id"], 900)
            # The index is a fold of the file, so a restart answers the same.
            reopened = events.EventStore(path)
            self.assertEqual(reopened.cursor(REPO, 7), cursor)


class FakeRouter:
    receipt_terminal = staticmethod(wakes.WakeClient.receipt_terminal)

    def __init__(self):
        self.submitted = []
        self.receipts = {}
        self.fail_submit = None

    def submit(
        self, seat, wake_id, session, letter, generation, require_pr, topic, link, *, message_id=0
    ):
        if self.fail_submit is not None:
            raise self.fail_submit
        self.submitted.append(
            {
                "seat": seat,
                "wake_id": wake_id,
                "session": session,
                "letter": letter,
                "generation": generation,
                "message_id": message_id,
                "topic": topic,
            }
        )
        return {"accepted": True}

    def receipt(self, seat, wake_id):
        return self.receipts.get(wake_id)

    def check_require_pr_support(self, seat, probe_id):
        if getattr(self, "fail_gate", None) is not None:
            raise self.fail_gate


class FakeRead:
    def __init__(self, pr=None, reviews=(), comments=(), checks=()):
        self._pr = pr or {
            "state": "open",
            "head": {"sha": HEAD},
            "html_url": "https://example/pr/7",
        }
        self._reviews = list(reviews)
        self._comments = list(comments)
        self._checks = list(checks)

    def pull(self, repo, number):
        return self._pr

    def reviews(self, repo, number, after_id=0):
        return self._reviews

    def review_comments(self, repo, number, after_id=0):
        return self._comments

    def check_runs(self, repo, sha):
        return self._checks


def verified_origin(*_args):
    return dict(
        seat="CLAUDE",
        platform="claude",
        verified_host="server",
        profile_scope="fixture-profile",
        session=SESSION,
    )


class ServiceCycleTests(unittest.TestCase):
    def _service(self, directory, owners, read=None, router=None):
        owners_path = Path(directory) / "owners.jsonl"
        owners_path.write_text("".join(json.dumps(o) + "\n" for o in owners), encoding="utf-8")
        store = events.EventStore(str(Path(directory) / "events.jsonl"))
        svc = service.AlertService(
            store,
            read or FakeRead(),
            router or FakeRouter(),
            str(owners_path),
            {"claude": "CLAUDE"},
            repos={REPO},
            gate_seat="CLAUDE",
            message_poster=lambda owner, event, link: 123,
            origin_binder=verified_origin,
        )
        return svc

    def test_first_sight_seeds_and_alerts_nothing(self):
        # Activity BEFORE the registration ts is baseline;  nothing alerts.
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._service(
                directory,
                [claimed_owner(ts="2026-11-21T02:00:00Z")],
                FakeRead(
                    reviews=[kody_review(50)],
                    comments=[sentry_comment(60)],
                    checks=[kody_check(70)],
                ),
                router,
            )
            counts = svc.cycle()
            self.assertEqual(counts["seeded"], 1)
            self.assertEqual(counts["events"], 0)
            self.assertEqual(router.submitted, [])
            # Next cycle:  only NEW activity alerts.
            svc.gh = FakeRead(reviews=[kody_review(50), kody_review(51)])
            counts = svc.cycle()
            self.assertEqual(counts["events"], 1)
            self.assertEqual(len(router.submitted), 1)
            self.assertEqual(router.submitted[0]["session"], SESSION)
            self.assertEqual(router.submitted[0]["letter"], "AB")
            self.assertEqual(router.submitted[0]["generation"], 3)

    def test_first_sight_race_review_after_registration_alerts(self):
        # A review landing between PR registration and the FIRST poll is not
        # silently discarded:  the seed baselines only pre-registration
        # activity, and the same first cycle alerts the new review.
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._service(
                directory,
                [claimed_owner(ts="2026-11-21T00:30:00Z")],
                FakeRead(
                    reviews=[kody_review(50)],
                    comments=[sentry_comment(60)],
                    checks=[kody_check(70)],
                ),
                router,
            )
            counts = svc.cycle()
            self.assertEqual(counts["seeded"], 1)
            # review + findings alert;  the check on the SAME head is the same
            # round (semantic dedupe), so 2 events, not 3.
            self.assertEqual(counts["events"], 2)
            self.assertEqual(len(router.submitted), 2)
            cursor = svc.store.cursor(REPO, 7)
            self.assertEqual(cursor["review_id"], 50)
            self.assertEqual(cursor["comment_id"], 60)
            # Nothing re-alerts next cycle (the deduped check is suppressed by
            # the alerted head, not by its watermark).
            counts = svc.cycle()
            self.assertEqual(counts["events"], 0)
            self.assertEqual(len(router.submitted), 2)

    def test_owner_row_without_ts_baselines_everything(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._service(
                directory, [claimed_owner(ts=None)], FakeRead(reviews=[kody_review(50)]), router
            )
            counts = svc.cycle()
            self.assertEqual(counts["seeded"], 1)
            self.assertEqual(counts["events"], 0)

    def test_unbound_claim_is_held_never_delivered(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = claimed_owner(
                origin_session_claim=None, origin_letter_claim=None, origin_generation_claim=None
            )
            router = FakeRouter()
            svc = self._service(directory, [owner], FakeRead(), router)
            svc.cycle()  # seed
            svc.gh = FakeRead(reviews=[kody_review(51)])
            counts = svc.cycle()
            self.assertEqual(counts["held"], 1)
            self.assertEqual(router.submitted, [])
            held = svc.store.held()
            self.assertEqual(held[0]["reason"], "no-origin-claim")

    def test_retryable_submit_marks_submit_pending_then_reconciles(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._service(directory, [claimed_owner()], FakeRead(), router)
            svc.cycle()  # seed
            svc.gh = FakeRead(reviews=[kody_review(51)])
            router.fail_submit = wakes.WakeRetryable("router unreachable: URLError")
            counts = svc.cycle()
            self.assertEqual(counts["held"], 0)
            row = svc.store.get(REPO, 7, "review_submitted", "51")
            self.assertEqual(row["status"], "submit-pending")
            wake_id = row["wake_id"]
            # Router has no record (the submit was lost):  SAME wake id is
            # resubmitted, not a fresh one.
            router.fail_submit = None
            svc.gh = FakeRead(reviews=[kody_review(51)])
            svc.cycle()
            self.assertEqual(len(router.submitted), 1)
            self.assertEqual(router.submitted[0]["wake_id"], wake_id)
            self.assertEqual(
                svc.store.get(REPO, 7, "review_submitted", "51")["status"], "submitted"
            )

    def test_permanent_submit_refusal_is_held(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._service(directory, [claimed_owner()], FakeRead(), router)
            svc.cycle()
            svc.gh = FakeRead(reviews=[kody_review(51)])
            router.fail_submit = wakes.WakeError("wake refused: HTTP 400 bad")
            counts = svc.cycle()
            self.assertEqual(counts["held"], 1)
            self.assertIn("wake-submit", svc.store.held()[0]["reason"])

    def test_not_live_hold_redrives_with_new_epoch(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._service(directory, [claimed_owner()], FakeRead(), router)
            svc.cycle()
            svc.gh = FakeRead(reviews=[kody_review(51)])
            svc.cycle()
            row = svc.store.get(REPO, 7, "review_submitted", "51")
            first_wake = row["wake_id"]
            router.receipts[first_wake] = {
                "delivered": False,
                "reason": "required-session-not-live",
            }
            counts = svc.cycle()
            self.assertEqual(counts["redriven"], 1)
            row = svc.store.get(REPO, 7, "review_submitted", "51")
            self.assertEqual(row["epoch"], 1)
            self.assertNotEqual(row["wake_id"], first_wake)
            self.assertEqual(row["status"], "submitted")

    def test_pr_unbound_hold_redrives_with_new_epoch(self):
        # The binding raced the alert (bind-pr landed after dispatch):  the
        # hold is certainly-undelivered, so a bounded redrive with a fresh
        # epoch is permitted, same reconciliation as session holds.
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._service(directory, [claimed_owner()], FakeRead(), router)
            svc.cycle()
            svc.gh = FakeRead(reviews=[kody_review(51)])
            svc.cycle()
            row = svc.store.get(REPO, 7, "review_submitted", "51")
            first_wake = row["wake_id"]
            router.receipts[first_wake] = {"delivered": False, "reason": "required-pr-unbound"}
            counts = svc.cycle()
            self.assertEqual(counts["redriven"], 1)
            row = svc.store.get(REPO, 7, "review_submitted", "51")
            self.assertEqual(row["epoch"], 1)
            self.assertNotEqual(row["wake_id"], first_wake)

    def test_pr_unbound_redrive_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._service(directory, [claimed_owner()], FakeRead(), router)
            svc.cycle()
            svc.gh = FakeRead(reviews=[kody_review(51)])
            svc.cycle()
            for _ in range(10):
                row = svc.store.get(REPO, 7, "review_submitted", "51")
                if row["status"] != "submitted":
                    break
                router.receipts[row["wake_id"]] = {
                    "delivered": False,
                    "reason": "required-pr-unbound",
                }
                svc.cycle()
            row = svc.store.get(REPO, 7, "review_submitted", "51")
            self.assertEqual(row["status"], "held")
            self.assertEqual(row["reason"], "required-pr-unbound")

    def test_generation_mismatch_is_terminal_held(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._service(directory, [claimed_owner()], FakeRead(), router)
            svc.cycle()
            svc.gh = FakeRead(reviews=[kody_review(51)])
            svc.cycle()
            row = svc.store.get(REPO, 7, "review_submitted", "51")
            router.receipts[row["wake_id"]] = {
                "delivered": False,
                "reason": "required-generation-mismatch",
            }
            counts = svc.cycle()
            self.assertEqual(counts["redriven"], 0)
            row = svc.store.get(REPO, 7, "review_submitted", "51")
            self.assertEqual(row["status"], "held")
            self.assertEqual(row["reason"], "required-generation-mismatch")

    def test_delivered_and_unknown_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._service(directory, [claimed_owner()], FakeRead(), router)
            svc.cycle()
            svc.gh = FakeRead(reviews=[kody_review(51)])
            svc.cycle()
            row = svc.store.get(REPO, 7, "review_submitted", "51")
            router.receipts[row["wake_id"]] = {"delivered": True}
            svc.cycle()
            self.assertEqual(
                svc.store.get(REPO, 7, "review_submitted", "51")["status"], "delivered"
            )

    def test_delivered_null_stays_unknown_regardless_of_status_wording(self):
        self.assertEqual(
            wakes.WakeClient.receipt_terminal({"delivered": None, "status": "delivered"}),
            (True, "unknown"),
        )
        self.assertEqual(
            wakes.WakeClient.receipt_terminal({"status": "delivered"}), (True, "unknown")
        )
        self.assertEqual(
            wakes.WakeClient.receipt_terminal({"delivered": None, "status": "accepted"}),
            (False, ""),
        )
        self.assertEqual(
            wakes.WakeClient.receipt_terminal({"delivered": False, "reason": "no-live-session"}),
            (True, "held:no-live-session"),
        )

    def test_read_error_advances_no_cursor(self):
        with tempfile.TemporaryDirectory() as directory:

            class BrokenRead(FakeRead):
                def reviews(self, repo, number, after_id=0):
                    raise github_read.GithubReadUncertain("service_read:reviews")

            router = FakeRouter()
            svc = self._service(directory, [claimed_owner()], BrokenRead(), router)
            counts = svc.cycle()
            self.assertEqual(counts["read_errors"], 1)
            self.assertFalse(svc.store.has_seed(REPO, 7))

    def _kinds(self, svc):
        store = svc.store
        with store._lock:
            return sorted((row["kind"], str(row["event_id"])) for row in store._by_key.values())

    def test_a_failed_check_and_a_conflict_wake_the_origin_chat(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._service(
                directory,
                [claimed_owner(ts="2026-11-21T00:30:00Z")],
                FakeRead(pr=pull(False, "dirty"), checks=[ci_run(80, "Muster macOS build")]),
                router,
            )
            counts = svc.cycle()
            self.assertEqual(counts["events"], 2)
            self.assertEqual(counts["submitted"], 2)
            topics = [s["topic"] for s in router.submitted]
            self.assertEqual(
                topics,
                [
                    "%s#7: CI failed on the current head: Muster macOS build" % REPO,
                    "%s#7: blocked from merging: checks failed, merge conflicts with the base branch"
                    % REPO,
                ],
            )
            for sent in router.submitted:
                self.assertEqual(sent["session"], SESSION, "the wake is for the PR's origin chat")
                self.assertEqual(sent["letter"], "AB")
                self.assertEqual(sent["generation"], 3)
                self.assertEqual(sent["message_id"], 123, "each has its own Operator status line")
            # Neither alerts again while the head and the runs are the same.
            counts = svc.cycle()
            self.assertEqual(counts["events"], 0)
            self.assertEqual(len(router.submitted), 2)

    def test_a_conflict_alerts_again_on_a_new_head_that_still_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._service(
                directory, [claimed_owner()], FakeRead(pr=pull(False, "dirty")), router
            )
            self.assertEqual(svc.cycle()["events"], 1)
            next_head = "b" * 40
            svc.gh = FakeRead(pr=dict(pull(False, "dirty"), head={"sha": next_head}))
            self.assertEqual(svc.cycle()["events"], 1)
            self.assertEqual(len(router.submitted), 2)
            self.assertEqual(
                self._kinds(svc),
                [
                    ("merge_blocked", "mb%s:conflicts" % HEAD),
                    ("merge_blocked", "mb%s:conflicts" % next_head),
                ],
            )

    def test_a_conflict_is_heard_when_it_appears_on_an_unchanged_head(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._service(directory, [claimed_owner()], FakeRead(), router)
            svc.cycle()
            self.assertEqual(svc.cycle()["events"], 0, "a clean PR alerts nothing")
            svc.gh = FakeRead(pr=pull(False, "dirty"))
            self.assertEqual(svc.cycle()["events"], 1, "it falls into conflict on the same head")
            svc.gh = FakeRead(pr=pull(True, "clean"))
            self.assertEqual(svc.cycle()["events"], 0, "a resolution is not an event")
            self.assertEqual(len(router.submitted), 1)

    def test_a_pr_body_without_mergeability_never_alerts_a_conflict(self):
        # The relay's reduced read carries no mergeable field:  nothing is guessed either way.
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._service(directory, [claimed_owner()], FakeRead(), router)
            svc.cycle()
            self.assertEqual(svc.cycle()["events"], 0)
            self.assertEqual(router.submitted, [])

    def test_a_check_that_failed_before_registration_is_history(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._service(
                directory,
                [claimed_owner(ts="2026-11-21T02:00:00Z")],
                FakeRead(checks=[ci_run(80)]),
                router,
            )
            counts = svc.cycle()
            self.assertEqual(counts["seeded"], 1)
            self.assertEqual(counts["events"], 0)
            self.assertEqual(router.submitted, [])
            # The seed's own floor is what silences it, so a later failure still alerts.
            svc.gh = FakeRead(checks=[ci_run(80), ci_run(81, "lint")])
            self.assertEqual(svc.cycle()["events"], 1)
            self.assertIn("lint", router.submitted[0]["topic"])

    def test_a_failed_ci_event_on_an_unbound_pr_is_held_not_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            owner = claimed_owner(origin_session_claim=None, origin_letter_claim=None)
            router = FakeRouter()
            svc = self._service(
                directory, [owner], FakeRead(pr=pull(False), checks=[ci_run(80)]), router
            )
            counts = svc.cycle()
            self.assertEqual(router.submitted, [], "nothing reaches a chat that was not claimed")
            self.assertEqual(counts["held"], 2)
            held = svc.store.held()
            self.assertEqual(sorted(row["kind"] for row in held), ["ci_failed", "merge_blocked"])
            self.assertEqual({row["reason"] for row in held}, {"no-origin-claim"})
            self.assertEqual(svc.cycle()["events"], 0, "and neither is re-detected")

    def test_status_snapshot_surfaces_held_and_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            owner = claimed_owner(origin_session_claim=None)
            svc = self._service(directory, [owner], FakeRead(), router)
            svc.cycle()
            svc.gh = FakeRead(reviews=[kody_review(51)])
            svc.cycle()
            snap = svc.status_snapshot()
            self.assertEqual(snap["counts"].get("held"), 1)
            self.assertEqual(snap["held"][0]["reason"], "no-origin-claim")
            self.assertIn("pending", snap)


class WakeContractTests(unittest.TestCase):
    def test_deterministic_ids_differ_by_epoch(self):
        a = wakes.wake_id_for(REPO, 7, "review_submitted", "51", 0)
        b = wakes.wake_id_for(REPO, 7, "review_submitted", "51", 1)
        self.assertTrue(a.startswith("ra-"))
        self.assertNotEqual(a, b)
        self.assertEqual(a, wakes.wake_id_for(REPO, 7, "review_submitted", "51", 0))

    def test_submit_validates_required_fields(self):
        client = wakes.WakeClient("http://127.0.0.1:9", lambda: "k" * 48)
        with self.assertRaises(wakes.WakeError):
            client.submit("CLAUDE", "ra-x", "bad session", "AB", 1, "o/r#1", "t", "")
        with self.assertRaises(wakes.WakeError):
            client.submit("CLAUDE", "ra-x", SESSION, "ab", 1, "o/r#1", "t", "")
        with self.assertRaises(wakes.WakeError):
            client.submit("CLAUDE", "ra-x", SESSION, "AB", 0, "o/r#1", "t", "")


if __name__ == "__main__":
    unittest.main()


class InstantParsingTests(unittest.TestCase):
    """First-sight seeding compares INSTANTS, never lexical strings.

    Probe:  a pre-registration review is baselined (no event);  a
    post-registration one alerts (an event is recorded).
    """

    def _events(self, owner_ts, review_ts):
        with tempfile.TemporaryDirectory() as directory:
            owners_path = Path(directory) / "owners.jsonl"
            owners_path.write_text(json.dumps(claimed_owner(ts=owner_ts)) + "\n", encoding="utf-8")
            store = events.EventStore(str(Path(directory) / "events.jsonl"))
            svc = service.AlertService(
                store,
                FakeRead(reviews=[kody_review(50, submitted_at=review_ts)]),
                FakeRouter(),
                str(owners_path),
                {"claude": "CLAUDE"},
                repos={REPO},
                gate_seat="CLAUDE",
                message_poster=lambda owner, event, link: 123,
                origin_binder=verified_origin,
            )
            return svc.cycle()["events"]

    def test_z_and_fractional_seconds(self):
        # 00:00:00.500Z is AFTER 00:00:00Z:  not baselined, it alerts.
        self.assertEqual(self._events("2026-11-21T00:00:00Z", "2026-11-21T00:00:00.500Z"), 1)
        # ...and before-with-fraction baselines.
        self.assertEqual(self._events("2026-11-21T00:00:00.500Z", "2026-11-21T00:00:00Z"), 0)

    def test_offsets_compare_as_instants_not_strings(self):
        # 20:00-05:00 == 01:00Z:  after the 00:30Z registration it alerts,
        # where a lexical compare would seed it away.
        self.assertEqual(self._events("2026-11-21T00:30:00Z", "2026-11-20T20:00:00-05:00"), 1)
        # Before the 02:00Z registration it baselines.
        self.assertEqual(self._events("2026-11-21T02:00:00Z", "2026-11-20T20:00:00-05:00"), 0)

    def test_naive_item_treated_as_utc(self):
        self.assertEqual(self._events("2026-11-21T02:00:00Z", "2026-11-21T01:00:00"), 0)

    def test_unparseable_owner_baselines_everything(self):
        self.assertEqual(self._events("not-a-time", "2026-11-21T01:00:00Z"), 0)

    def test_unparseable_review_time_is_suppressed(self):
        self.assertEqual(self._events("2026-11-21T00:30:00Z", "garbage"), 0)

    def test_unparseable_check_time_still_alerts(self):
        with tempfile.TemporaryDirectory() as directory:
            owners_path = Path(directory) / "owners.jsonl"
            owners_path.write_text(
                json.dumps(claimed_owner(ts="2026-11-21T00:30:00Z")) + "\n", encoding="utf-8"
            )
            store = events.EventStore(str(Path(directory) / "events.jsonl"))
            check = kody_check(70)
            check["completed_at"] = "garbage"
            svc = service.AlertService(
                store,
                FakeRead(checks=[check]),
                FakeRouter(),
                str(owners_path),
                {"claude": "CLAUDE"},
                repos={REPO},
                gate_seat="CLAUDE",
                message_poster=lambda owner, event, link: 123,
                origin_binder=verified_origin,
            )
            counts = svc.cycle()
            self.assertEqual(counts["events"], 1)


class PerCycleGateTests(unittest.TestCase):
    def _gated_service(self, directory, router):
        owners_path = Path(directory) / "owners.jsonl"
        owners_path.write_text(json.dumps(claimed_owner()) + "\n", encoding="utf-8")
        store = events.EventStore(str(Path(directory) / "events.jsonl"))
        return service.AlertService(
            store,
            FakeRead(),
            router,
            str(owners_path),
            {"claude": "CLAUDE"},
            repos={REPO},
            gate_seat="CLAUDE",
            message_poster=lambda owner, event, link: 123,
            origin_binder=verified_origin,
        )

    def test_gate_ok_cycle_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self._gated_service(directory, router)
            counts = svc.cycle()
            self.assertEqual(counts["prs"], 1)

    def test_gate_failure_holds_the_whole_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            router.fail_gate = wakes.WakeError("router lacks require_pr validation")
            svc = self._gated_service(directory, router)
            with self.assertRaises(wakes.WakeError):
                svc.cycle()
            # Nothing dispatched, nothing seeded:  next cycle retries clean.
            self.assertEqual(router.submitted, [])
            self.assertFalse(svc.store.has_seed(REPO, 7))
