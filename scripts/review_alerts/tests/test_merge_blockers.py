"""Merge-blocker transitions use the existing receipt-backed exact-chat path.

changes_requested: a non-review-bot CHANGES_REQUESTED review (the review bot's
own reviews already alert as review_submitted).
check_failure: a completed non-review check run on the CURRENT head with a
failure-class conclusion.  The check and conclusion are named;  whether branch
protection requires the check is not claimed, because the check-runs read does
not say.
"""

import tempfile
import unittest

from review_alerts import events
from review_alerts.tests import test_review_alerts as fixtures
from review_alerts.tests.test_review_alerts import (
    HEAD,
    REPO,
    SESSION,
    FakeRead,
    FakeRouter,
    claimed_owner,
    kody_check,
    kody_review,
)

OTHER = "c" * 40


def human_review(rid, state="CHANGES_REQUESTED", commit=HEAD, login="octocat"):
    return {
        "id": rid,
        "state": state,
        "commit_id": commit,
        "submitted_at": "2026-11-23T01:00:00Z",
        "user": {"login": login},
    }


def ci_check(
    rid, head=HEAD, status="completed", conclusion="failure", name="ci", slug="github-actions"
):
    return {
        "id": rid,
        "head_sha": head,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "completed_at": "2026-11-23T01:00:00Z" if status == "completed" else None,
        "app": {"id": 15368, "slug": slug},
    }


class ChangesRequested(unittest.TestCase):
    make_service = fixtures.ServiceCycleTests._service

    def test_non_bot_changes_requested_alerts_exact_owner_once_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self.make_service(
                directory, [claimed_owner()], FakeRead(reviews=[human_review(61)]), router
            )
            self.assertEqual(svc.cycle()["submitted"], 1)
            self.assertEqual(router.submitted[0]["session"], SESSION)
            self.assertEqual(router.submitted[0]["letter"], "AB")
            self.assertEqual(router.submitted[0]["generation"], 3)
            svc.cycle()
            svc.store = events.EventStore(svc.store._path)
            svc.cycle()
            self.assertEqual(len(router.submitted), 1)
            row = svc.store.get(REPO, 7, "changes_requested", "61")
            self.assertEqual(row["actor"], "octocat")
            self.assertIs(row["head_matches"], True)

    def test_review_bot_changes_requested_does_not_double_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self.make_service(
                directory, [claimed_owner()], FakeRead(reviews=[kody_review(11)]), router
            )
            self.assertEqual(svc.cycle()["submitted"], 1)
            self.assertIsNotNone(svc.store.get(REPO, 7, "review_submitted", "11"))
            self.assertIsNone(svc.store.get(REPO, 7, "changes_requested", "11"))

    def test_other_states_never_alert(self):
        for state in ("APPROVED", "COMMENTED", "DISMISSED", "PENDING"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                router = FakeRouter()
                svc = self.make_service(
                    directory,
                    [claimed_owner()],
                    FakeRead(reviews=[human_review(61, state=state)]),
                    router,
                )
                self.assertEqual(svc.cycle()["events"], 0)
                self.assertEqual(router.submitted, [])

    def test_stale_head_changes_requested_is_flagged_not_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self.make_service(
                directory,
                [claimed_owner()],
                FakeRead(reviews=[human_review(61, commit=OTHER)]),
                router,
            )
            self.assertEqual(svc.cycle()["submitted"], 1)
            row = svc.store.get(REPO, 7, "changes_requested", "61")
            self.assertIs(row["head_matches"], False)

    def test_superseding_approval_emits_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self.make_service(
                directory,
                [claimed_owner()],
                FakeRead(reviews=[human_review(61), human_review(62, state="APPROVED")]),
                router,
            )
            self.assertEqual(svc.cycle()["events"], 0)
            self.assertEqual(router.submitted, [])

    def test_superseding_approval_then_comment_only_emits_nothing(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self.make_service(
                directory,
                [claimed_owner()],
                FakeRead(
                    reviews=[
                        human_review(61),
                        human_review(62, state="APPROVED"),
                        human_review(63, state="COMMENTED"),
                    ]
                ),
                router,
            )
            self.assertEqual(svc.cycle()["events"], 0)
            self.assertEqual(router.submitted, [])

    def test_comment_only_does_not_supersede_request(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self.make_service(
                directory,
                [claimed_owner()],
                FakeRead(reviews=[human_review(61), human_review(62, state="COMMENTED")]),
                router,
            )
            self.assertEqual(svc.cycle()["submitted"], 1)
            self.assertIsNotNone(svc.store.get(REPO, 7, "changes_requested", "61"))

    def test_dismissal_supersedes_request(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self.make_service(
                directory,
                [claimed_owner()],
                FakeRead(reviews=[human_review(61), human_review(62, state="DISMISSED")]),
                router,
            )
            self.assertEqual(svc.cycle()["events"], 0)
            self.assertEqual(router.submitted, [])

    def test_request_after_approval_alerts(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self.make_service(
                directory,
                [claimed_owner()],
                FakeRead(reviews=[human_review(61, state="APPROVED"), human_review(62)]),
                router,
            )
            self.assertEqual(svc.cycle()["submitted"], 1)
            self.assertIsNotNone(svc.store.get(REPO, 7, "changes_requested", "62"))

    def test_approval_after_alerted_request_emits_nothing_new(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self.make_service(
                directory, [claimed_owner()], FakeRead(reviews=[human_review(61)]), router
            )
            self.assertEqual(svc.cycle()["submitted"], 1)
            svc.gh._reviews = [human_review(61), human_review(62, state="APPROVED")]
            self.assertEqual(svc.cycle()["submitted"], 0)
            self.assertEqual(len(router.submitted), 1)


class CheckFailure(unittest.TestCase):
    make_service = fixtures.ServiceCycleTests._service

    def test_ci_failure_on_current_head_alerts_once_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self.make_service(
                directory, [claimed_owner()], FakeRead(checks=[ci_check(51)]), router
            )
            self.assertEqual(svc.cycle()["submitted"], 1)
            svc.cycle()
            svc.store = events.EventStore(svc.store._path)
            svc.cycle()
            self.assertEqual(len(router.submitted), 1)
            row = svc.store.get(REPO, 7, "check_failure", "51")
            self.assertEqual(row["summary"], "check failed: ci (failure)")
            self.assertEqual(row["actor"], "github-actions")

    def test_non_failure_conclusions_and_old_head_never_alert(self):
        for conclusion in ("success", "neutral", "skipped", "cancelled", "stale", None):
            with self.subTest(conclusion=conclusion), tempfile.TemporaryDirectory() as directory:
                router = FakeRouter()
                svc = self.make_service(
                    directory,
                    [claimed_owner()],
                    FakeRead(checks=[ci_check(51, conclusion=conclusion)]),
                    router,
                )
                self.assertEqual(svc.cycle()["events"], 0)
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self.make_service(
                directory, [claimed_owner()], FakeRead(checks=[ci_check(51, head=OTHER)]), router
            )
            self.assertEqual(svc.cycle()["events"], 0)

    def test_in_progress_run_is_not_eaten_by_the_watermark(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self.make_service(
                directory,
                [claimed_owner()],
                FakeRead(checks=[ci_check(51, status="in_progress", conclusion=None)]),
                router,
            )
            self.assertEqual(svc.cycle()["events"], 0)
            svc.gh._checks = [ci_check(51)]
            self.assertEqual(svc.cycle()["submitted"], 1)
            self.assertIsNotNone(svc.store.get(REPO, 7, "check_failure", "51"))

    def test_review_bot_check_failure_alerts_via_review_completed_only(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self.make_service(
                directory,
                [claimed_owner()],
                FakeRead(checks=[kody_check(51, conclusion="failure")]),
                router,
            )
            self.assertEqual(svc.cycle()["submitted"], 1)
            self.assertIsNotNone(svc.store.get(REPO, 7, "review_completed", "51"))
            self.assertIsNone(svc.store.get(REPO, 7, "check_failure", "51"))

    def test_each_failing_run_alerts_once(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self.make_service(
                directory,
                [claimed_owner()],
                FakeRead(
                    checks=[
                        ci_check(51, name="ci"),
                        ci_check(52, name="lint", conclusion="timed_out"),
                    ]
                ),
                router,
            )
            self.assertEqual(svc.cycle()["submitted"], 2)
            svc.cycle()
            self.assertEqual(len(router.submitted), 2)

    def test_higher_id_terminal_failure_does_not_hide_lower_id_in_progress_run(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self.make_service(
                directory,
                [claimed_owner()],
                FakeRead(
                    checks=[
                        ci_check(51, status="in_progress", conclusion=None),
                        ci_check(52, name="lint"),
                    ]
                ),
                router,
            )
            self.assertEqual(svc.cycle()["submitted"], 1)
            self.assertIsNotNone(svc.store.get(REPO, 7, "check_failure", "52"))
            svc.gh._checks = [ci_check(51), ci_check(52, name="lint")]
            self.assertEqual(svc.cycle()["submitted"], 1)
            self.assertIsNotNone(svc.store.get(REPO, 7, "check_failure", "51"))
            svc.cycle()
            self.assertEqual(len(router.submitted), 2)

    def test_lower_id_kody_completion_alongside_higher_failed_check(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self.make_service(
                directory,
                [claimed_owner()],
                FakeRead(checks=[kody_check(70), ci_check(65, name="ci")]),
                router,
            )
            self.assertEqual(svc.cycle()["submitted"], 2)
            self.assertIsNotNone(svc.store.get(REPO, 7, "review_completed", "70"))
            self.assertIsNotNone(svc.store.get(REPO, 7, "check_failure", "65"))
            svc.cycle()
            self.assertEqual(len(router.submitted), 2)

    def test_check_failure_does_not_advance_the_kody_check_watermark(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self.make_service(
                directory, [claimed_owner()], FakeRead(checks=[ci_check(65, name="ci")]), router
            )
            self.assertEqual(svc.cycle()["submitted"], 1)
            svc.gh._checks = [ci_check(65, name="ci"), kody_check(60)]
            self.assertEqual(svc.cycle()["submitted"], 1)
            self.assertIsNotNone(svc.store.get(REPO, 7, "review_completed", "60"))


class BlockerMessage(unittest.TestCase):
    def test_blocker_messages_precede_wakes_and_persist_real_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = fixtures.ServiceCycleTests._service(
                self,
                directory,
                [claimed_owner()],
                FakeRead(reviews=[human_review(61)], checks=[ci_check(51)]),
                router,
            )
            posted = []

            def post(owner, event, link):
                posted.append(event["kind"])
                return 1000 + len(posted)

            svc.message_poster = post
            self.assertEqual(svc.cycle()["submitted"], 2)
            self.assertEqual(sorted(posted), ["changes_requested", "check_failure"])
            ids = {row["message_id"] for row in router.submitted}
            self.assertEqual(ids, {1001, 1002})
            store = events.EventStore(svc.store._path)
            self.assertEqual(store.get(REPO, 7, "changes_requested", "61")["message_id"], 1001)
            self.assertEqual(store.get(REPO, 7, "check_failure", "51")["message_id"], 1002)


if __name__ == "__main__":
    unittest.main()
