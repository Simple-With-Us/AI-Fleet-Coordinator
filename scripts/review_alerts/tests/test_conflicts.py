"""Conflict signals use the existing receipt-backed exact-chat wake path."""

import tempfile
import unittest
from unittest import mock

from review_alerts import detect, events, wakes
from review_alerts.tests import test_review_alerts as fixtures
from review_alerts.tests.test_review_alerts import (
    HEAD,
    REPO,
    SESSION,
    FakeRead,
    FakeRouter,
    claimed_owner,
    kody_review,
)

BASE = "b" * 40


def pull(**changes):
    row = dict(
        state="open",
        head={"sha": HEAD},
        base={"sha": BASE},
        mergeable=False,
        mergeable_state="dirty",
        html_url="https://example/pr/7",
    )
    row.update(changes)
    return row


class ConflictDetection(unittest.TestCase):
    def test_positive_pair_identity_and_summary(self):
        (event,) = detect.merge_conflict(REPO, 7, pull())
        self.assertEqual(event["kind"], "merge_conflict")
        self.assertEqual(event["event_id"], HEAD + ":" + BASE)
        self.assertIs(event["head_matches"], True)
        self.assertNotIn("dirty", event["summary"])

    def test_unknown_clean_blocked_and_malformed_never_claim_conflict(self):
        for changes in (
            {"mergeable": None},
            {"mergeable": True},
            {"mergeable": 0},
            {"mergeable": "false"},
            {"mergeable_state": "unknown"},
            {"mergeable_state": "blocked"},
            {"mergeable_state": "unstable"},
            {"mergeable_state": None},
            {"state": "closed"},
            {"head": {}},
            {"base": {}},
            {"head": "bad"},
            {"base": None},
            {"head": {"sha": "bad"}},
            {"base": {"sha": "a" * 39}},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(detect.merge_conflict(REPO, 7, pull(**changes)), [])

    def test_new_head_or_base_is_distinct(self):
        first = detect.merge_conflict(REPO, 7, pull())[0]["event_id"]
        self.assertNotEqual(
            first,
            detect.merge_conflict(REPO, 7, pull(base={"sha": "c" * 40}))[0]["event_id"],
        )
        self.assertNotEqual(
            first,
            detect.merge_conflict(REPO, 7, pull(head={"sha": "c" * 40}))[0]["event_id"],
        )


class ConflictCycle(unittest.TestCase):
    make_service = fixtures.ServiceCycleTests._service

    def test_first_observed_conflict_alerts_exact_owner_once_across_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self.make_service(directory, [claimed_owner()], FakeRead(pr=pull()), router)
            self.assertEqual(svc.cycle()["submitted"], 1)
            self.assertEqual(router.submitted[0]["session"], SESSION)
            self.assertEqual(router.submitted[0]["letter"], "AB")
            self.assertEqual(router.submitted[0]["generation"], 3)
            svc.cycle()
            svc.store = events.EventStore(svc.store._path)
            svc.cycle()
            self.assertEqual(len(router.submitted), 1)

    def test_base_advance_alerts_new_pair_not_every_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self.make_service(directory, [claimed_owner()], FakeRead(pr=pull()), router)
            svc.cycle()
            svc.gh._pr = pull(base={"sha": "c" * 40})
            svc.cycle()
            svc.cycle()
            self.assertEqual(len(router.submitted), 2)

    def test_unknown_does_not_consume_future_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self.make_service(
                directory, [claimed_owner()], FakeRead(pr=pull(mergeable=None)), router
            )
            self.assertEqual(svc.cycle()["events"], 0)
            svc.gh._pr = pull()
            self.assertEqual(svc.cycle()["submitted"], 1)

    def test_review_and_conflict_both_use_existing_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self.make_service(
                directory,
                [claimed_owner()],
                FakeRead(pr=pull(), reviews=[kody_review(11)]),
                router,
            )
            self.assertEqual(svc.cycle()["submitted"], 2)
            self.assertEqual(svc.store.cursor(REPO, 7)["review_id"], 11)
            self.assertEqual(svc.store.cursor(REPO, 7)["completed_heads"], [HEAD])

    def test_binding_failure_preserves_conflict_until_recovered(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self.make_service(directory, [claimed_owner()], FakeRead(pr=pull()), router)
            svc.origin_binder = mock.Mock(side_effect=RuntimeError("held"))
            self.assertEqual(svc.cycle()["submitted"], 0)
            self.assertEqual(len(svc.store), 0)
            svc.origin_binder.side_effect = fixtures.verified_origin
            self.assertEqual(svc.cycle()["submitted"], 1)

    def test_lost_submit_reconciles_same_wake_id(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            router.fail_submit = wakes.WakeRetryable("lost answer")
            svc = self.make_service(directory, [claimed_owner()], FakeRead(pr=pull()), router)
            svc.cycle()
            (pending,) = svc.store.pending()
            router.fail_submit = None
            svc.cycle()
            self.assertEqual(router.submitted[0]["wake_id"], pending["wake_id"])
            self.assertEqual(len(svc.store), 1)

    def test_closed_pr_never_alerts(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = self.make_service(
                directory, [claimed_owner()], FakeRead(pr=pull(state="closed")), router
            )
            self.assertEqual(svc.cycle()["events"], 0)
            self.assertEqual(router.submitted, [])


class ConflictMessage(unittest.TestCase):
    def test_conflict_message_precedes_wake_and_persists_real_id(self):
        with tempfile.TemporaryDirectory() as directory:
            router = FakeRouter()
            svc = fixtures.ServiceCycleTests._service(
                self, directory, [claimed_owner()], FakeRead(pr=pull()), router
            )
            posted = []

            def post(owner, event, link):
                posted.append(dict(event))
                return 432

            svc.message_poster = post
            self.assertEqual(svc.cycle()["submitted"], 1)
            self.assertEqual(posted[0]["kind"], "merge_blocked")
            self.assertEqual(router.submitted[0]["message_id"], 432)
            record = events.EventStore(svc.store._path).get(
                REPO, 7, "merge_blocked", "mb" + HEAD + ":" + BASE + ":conflicts"
            )
            self.assertEqual(record["message_id"], 432)


if __name__ == "__main__":
    unittest.main()
