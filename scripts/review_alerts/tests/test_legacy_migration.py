"""Old unknown outcomes are immutable facts, not permission to resend."""

import tempfile
import unittest
from review_alerts import events
from review_alerts.tests import test_review_alerts as fixtures
from review_alerts.tests.test_review_alerts import (
    HEAD,
    REPO,
    FakeRead,
    FakeRouter,
    claimed_owner,
    ci_run,
    pull,
    review,
)

BASE = "b" * 40


class LegacyMigration(unittest.TestCase):
    def setup_service(self, directory, checks=(), reviews=()):
        router = FakeRouter()
        svc = fixtures.ServiceCycleTests._service(
            self,
            directory,
            [claimed_owner()],
            FakeRead(pr=pull(False, "dirty"), checks=list(checks), reviews=list(reviews)),
            router,
        )
        return svc, router

    def legacy(self, svc, kind, event_id, status="unknown"):
        svc.store.append(
            dict(
                repo=REPO,
                number=7,
                kind=kind,
                event_id=event_id,
                status=status,
                reason="transport-unproven",
                head=HEAD,
                current_head=HEAD,
                head_matches=True,
                message_id=643133794,
            )
        )

    def test_same_pair_all_outcomes_suppress_only_the_old_fact(self):
        for status in ("unknown", "delivered", "held", "submitted", "message-posted"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as d:
                svc, router = self.setup_service(d)
                self.legacy(svc, "merge_conflict", HEAD + ":" + BASE, status)
                original = svc.store.get(REPO, 7, "merge_conflict", HEAD + ":" + BASE)
                # Inspect derivation only for unsettled rows: receipt settlement is separate.
                from review_alerts import detect

                found = detect.derive(
                    REPO,
                    7,
                    HEAD,
                    [],
                    [],
                    [],
                    svc.store.cursor(REPO, 7),
                    pull=pull(False, "dirty"),
                )
                self.assertEqual(found, [])
                self.assertEqual(
                    svc.store.get(REPO, 7, "merge_conflict", HEAD + ":" + BASE),
                    original,
                )

    def test_unknown_pair_restart_dedupes_but_new_base_alerts(self):
        with tempfile.TemporaryDirectory() as d:
            svc, router = self.setup_service(d)
            self.legacy(svc, "merge_conflict", HEAD + ":" + BASE)
            svc.store = events.EventStore(svc.store._path)
            self.assertEqual(svc.cycle()["events"], 0)
            svc.gh._pr["base"] = {"sha": "c" * 40}
            self.assertEqual(svc.cycle()["submitted"], 1)
            self.assertEqual(svc.cycle()["events"], 0)
            self.assertEqual(
                svc.store.get(REPO, 7, "merge_conflict", HEAD + ":" + BASE)["status"],
                "unknown",
            )

    def test_new_gate_is_not_hidden_by_legacy_conflict(self):
        with tempfile.TemporaryDirectory() as d:
            svc, router = self.setup_service(d, checks=[ci_run(55, "merge-gate")])
            self.legacy(svc, "merge_conflict", HEAD + ":" + BASE)
            self.assertEqual(svc.cycle()["submitted"], 1)
            self.assertIn("merge gate", router.submitted[0]["topic"])
            self.assertNotIn("merge conflicts", router.submitted[0]["topic"])
            self.assertEqual(svc.cycle()["events"], 0)

    def test_mixed_legacy_and_new_failed_runs_keep_new_run_only(self):
        with tempfile.TemporaryDirectory() as d:
            svc, router = self.setup_service(d, checks=[ci_run(55, "old"), ci_run(56, "new")])
            self.legacy(svc, "merge_conflict", HEAD + ":" + BASE)
            self.legacy(svc, "check_failure", "55")
            self.assertEqual(svc.cycle()["submitted"], 1)
            self.assertIsNotNone(svc.store.get(REPO, 7, "ci_failed", "ci56"))
            self.assertIsNone(svc.store.get(REPO, 7, "ci_failed", "ci55+56"))
            self.assertEqual(svc.store.get(REPO, 7, "check_failure", "55")["status"], "unknown")
            self.assertEqual(svc.cycle()["events"], 0)

    def test_new_review_blocker_is_not_hidden_by_legacy_conflict(self):
        with tempfile.TemporaryDirectory() as d:
            svc, router = self.setup_service(
                d, reviews=[review(88, "someone", "CHANGES_REQUESTED")]
            )
            self.legacy(svc, "merge_conflict", HEAD + ":" + BASE)
            self.assertEqual(svc.cycle()["submitted"], 1)
            self.assertIn("changes requested", router.submitted[0]["topic"])

    def test_legacy_check_unknown_does_not_hide_a_new_lower_id_failure(self):
        with tempfile.TemporaryDirectory() as d:
            svc, router = self.setup_service(d, checks=[ci_run(54, "new"), ci_run(55, "old")])
            self.legacy(svc, "merge_conflict", HEAD + ":" + BASE)
            self.legacy(svc, "check_failure", "55")
            self.assertEqual(svc.cycle()["submitted"], 1)
            self.assertIsNotNone(svc.store.get(REPO, 7, "ci_failed", "ci54"))
            self.assertEqual(svc.cycle()["events"], 0)

    def test_legacy_request_preserved_but_new_request_and_gate_are_not_hidden(self):
        with tempfile.TemporaryDirectory() as d:
            svc, router = self.setup_service(
                d, reviews=[review(88, "someone", "CHANGES_REQUESTED")]
            )
            self.legacy(svc, "merge_conflict", HEAD + ":" + BASE)
            self.legacy(svc, "changes_requested", "88")
            self.assertEqual(svc.cycle()["events"], 0)
            svc.gh._reviews.append(review(89, "someone", "CHANGES_REQUESTED"))
            self.assertEqual(svc.cycle()["submitted"], 1)
            self.assertEqual(svc.store.get(REPO, 7, "changes_requested", "88")["status"], "unknown")

    def test_head_only_2510_unknown_is_not_resent_or_assigned_an_invented_base(self):
        with tempfile.TemporaryDirectory() as d:
            svc, router = self.setup_service(d)
            key = "mb" + HEAD + ":conflicts"
            self.legacy(svc, "merge_blocked", key)
            svc.store = events.EventStore(svc.store._path)
            self.assertEqual(svc.cycle()["events"], 0)
            svc.gh._pr["base"] = {"sha": "c" * 40}
            self.assertEqual(svc.cycle()["events"], 0)  # Old format has no historical base.
            svc.gh._checks = [ci_run(55, "merge-gate")]
            self.assertEqual(svc.cycle()["submitted"], 1)
            self.assertIn("merge gate", router.submitted[0]["topic"])
            self.assertEqual(svc.store.get(REPO, 7, "merge_blocked", key)["status"], "unknown")
            svc.gh._pr["head"] = {"sha": "d" * 40}
            self.assertEqual(svc.cycle()["submitted"], 1)
