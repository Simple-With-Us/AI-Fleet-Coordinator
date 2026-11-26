"""Regression coverage for the shared router's ineligible-target repick path."""

from __future__ import annotations

import json
import threading
import time
from unittest import mock

from simple_tools.tests.test_wake_router import (
    Base as _RouterBase,
    NEWER,
    OLDER,
    SID_A,
    SID_B,
    SID_C,
    WR,
    row,
)


class IneligibleRepicks(_RouterBase):
    def setUp(self) -> None:
        super().setUp()
        self.registry.rows = [
            row(SID_A, "AA", "First chat", assigned_at=NEWER),
            row(SID_B, "BB", "Second chat", assigned_at=OLDER),
            row(SID_C, "CC", "Third chat", assigned_at="2026-10-30T00:00:00Z"),
        ]

    def router(self, *args, **config) -> WR.Router:
        config.setdefault("retry_backoff", [0])
        router = super().router(*args, **config)
        router.never_ran = router.never_ran | {"session-subagent"}
        return router

    def refusal(self) -> WR.Delivery:
        return WR.Delivery("refused", "wake", code="session-subagent")

    def test_an_unaddressed_never_run_subagent_pick_reaches_another_principal(self) -> None:
        calls: list[str] = []

        def consult(candidates, wake):
            calls.append(wake.wake_id)
            return {"session_id": SID_A if len(calls) == 1 else SID_C}

        self.results = [self.refusal(), WR.Delivery("completed", "wake")]
        router = self.router(consult=consult, max_attempts=3)
        router.accept(self.wake(topic="unrelated"))
        self.drain(router)

        receipt = self.done()
        self.assertEqual(self.delivered, [(SID_A, "w-1"), (SID_B, "w-1")])
        self.assertEqual(calls, ["w-1"], "the alternate pick must not call the decider again")
        self.assertEqual((receipt["status"], receipt["delivered"], receipt["attempts"]),
                         ("delivered", True, 2))
        self.assertEqual(receipt["rejected_targets"], [{
            "host": "server", "session_id": SID_A, "letter": "AA", "generation": 1,
            "code": "session-subagent",
        }])

    def test_the_rejection_is_durable_across_restart_before_the_alternate_pick(self) -> None:
        self.results = [self.refusal(), WR.Delivery("completed", "wake")]
        first = self.router(max_attempts=3)
        first.accept(self.wake())
        self.assertTrue(first.run_once())
        saved = json.loads(self.pending().read_text(encoding="utf-8"))
        self.assertIsNone(saved["pick"])
        self.assertNotIn("in_flight", saved)
        self.assertEqual(saved["failures"], 1)
        self.assertEqual(saved["rejected_targets"][0]["session_id"], SID_A)

        restarted = self.router(max_attempts=3)
        self.assertEqual(restarted.replay(), 1)
        self.drain(restarted)
        self.assertEqual(self.delivered, [(SID_A, "w-1"), (SID_B, "w-1")])
        self.assertEqual(self.done()["status"], "delivered")

    def test_a_new_generation_of_the_same_chat_is_a_different_binding(self) -> None:
        self.results = [self.refusal(), WR.Delivery("completed", "wake")]
        router = self.router(max_attempts=3)
        router.accept(self.wake())
        self.assertTrue(router.run_once())
        self.registry.rows = [
            row(SID_A, "AA", "First chat", assigned_at=NEWER, generation=4),
            row(SID_B, "BB", "Second chat", assigned_at=OLDER),
        ]
        self.drain(router)
        receipt = self.done()
        self.assertEqual(self.delivered, [(SID_A, "w-1"), (SID_A, "w-1")])
        self.assertEqual(receipt["picked_generation"], 4)
        self.assertEqual(receipt["rejected_targets"][0]["generation"], 1)

    def test_each_eligible_rejection_uses_the_existing_attempt_cap(self) -> None:
        self.results = [self.refusal(), self.refusal()]
        router = self.router(max_attempts=2)
        router.accept(self.wake())
        self.drain(router)
        receipt = self.done()
        self.assertEqual(self.delivered, [(SID_A, "w-1"), (SID_B, "w-1")])
        self.assertEqual((receipt["status"], receipt["delivered"], receipt["attempts"]),
                         ("dead", False, 2))
        self.assertEqual([item["session_id"] for item in receipt["rejected_targets"]],
                         [SID_A, SID_B])

    def test_a_subagent_refusal_and_later_failures_share_the_failure_cap(self) -> None:
        self.results = [
            self.refusal(),
            WR.Delivery("failed", "wake", code="retryable"),
            WR.Delivery("failed", "wake", code="retryable"),
            WR.Delivery("failed", "wake", code="retryable"),
        ]
        router = self.router(max_attempts=3)
        router.accept(self.wake())
        self.drain(router)
        receipt = self.done()
        self.assertEqual(len(self.delivered), 3)
        self.assertEqual([session for session, _ in self.delivered], [SID_A, SID_B, SID_B])
        self.assertEqual((receipt["status"], receipt["code"], receipt["attempts"]),
                         ("dead", "retryable", 3))
        self.assertEqual(receipt["rejected_targets"][0]["session_id"], SID_A)

    def test_a_prior_failure_and_ineligible_refusal_at_cap_do_not_pick_again(self) -> None:
        self.results = [
            WR.Delivery("failed", "wake", code="retryable"),
            self.refusal(),
            WR.Delivery("completed", "wake"),
        ]
        router = self.router(max_attempts=2)
        router.accept(self.wake())
        self.drain(router)
        receipt = self.done()
        self.assertEqual(self.delivered, [(SID_A, "w-1"), (SID_A, "w-1")])
        self.assertEqual((receipt["status"], receipt["code"], receipt["attempts"]),
                         ("dead", "session-subagent", 2))
        self.assertEqual(receipt["rejected_targets"][0]["session_id"], SID_A)

    def test_a_only_candidate_refusal_retries_no_live_session_then_ends_at_cap(self) -> None:
        self.registry.rows = [row(SID_A, "AA", "Only chat")]
        self.results = [self.refusal()]
        router = self.router(max_attempts=2)
        router.accept(self.wake())
        self.drain(router)
        receipt = self.done()
        self.assertEqual(self.delivered, [(SID_A, "w-1")])
        self.assertEqual((receipt["status"], receipt["code"], receipt["delivered"]),
                         ("dead", "no-live-session", False))
        self.assertEqual(receipt["rejected_targets"][0]["session_id"], SID_A)

    def test_repick_keeps_same_chat_serialization_for_two_wakes(self) -> None:
        started_a = threading.Event()
        started_b = threading.Event()
        release_a = threading.Event()
        release_b = threading.Event()
        second_a = threading.Event()
        active: set[str] = set()
        overlaps: list[str] = []
        order: list[tuple[str, str]] = []
        lock = threading.Lock()

        def deliver(target, wake, host=""):
            with lock:
                if target.session_id in active:
                    overlaps.append(target.session_id)
                active.add(target.session_id)
                order.append((wake.wake_id, target.session_id))
            if wake.wake_id == "w-1" and target.session_id == SID_A:
                started_a.set()
                release_a.wait(timeout=10)
                result = self.refusal()
            elif wake.wake_id == "w-2" and target.session_id == SID_B:
                started_b.set()
                release_b.wait(timeout=10)
                result = WR.Delivery("completed", "wake")
            else:
                second_a.set()
                result = WR.Delivery("completed", "wake")
            with lock:
                active.remove(target.session_id)
            return result

        def consult(candidates, wake):
            return {"session_id": SID_A}

        router = self.router(deliver=deliver, consult=consult, workers=2, max_attempts=3)
        router.start()
        self.addCleanup(router.stop)
        router.accept(self.wake("w-1"))
        self.assertTrue(started_a.wait(timeout=10))
        router.accept(self.wake("w-2", letter="BB"))
        self.assertTrue(started_b.wait(timeout=10))
        release_a.set()
        time.sleep(0.2)
        self.assertFalse(second_a.is_set(), "the repick must wait while w-2 owns the same chat")
        release_b.set()
        deadline = time.time() + 10
        while time.time() < deadline and not second_a.is_set():
            time.sleep(0.02)
        self.assertTrue(second_a.is_set())
        self.assertEqual(overlaps, [])
        self.assertEqual(order, [("w-1", SID_A), ("w-2", SID_B), ("w-1", SID_B)])

    def test_a_failed_rejection_write_does_not_open_a_second_delivery(self) -> None:
        self.results = [self.refusal(), WR.Delivery("completed", "wake")]
        router = self.router(max_attempts=3)
        router.accept(self.wake())
        save = router._save
        failed = False

        def save_rejection_once(entry):
            nonlocal failed
            if (not failed and entry.get("rejected_targets") and entry.get("pick") is None
                    and not entry.get("in_flight")):
                failed = True
                return False
            return save(entry)

        with mock.patch.object(router, "_save", side_effect=save_rejection_once):
            self.drain(router)
        receipt = self.done()
        self.assertEqual(self.delivered, [(SID_A, "w-1")])
        self.assertEqual((receipt["status"], receipt["code"], receipt["delivered"]),
                         ("dead", "session-subagent", False))

    def test_rejected_bindings_are_shape_checked_and_bounded_on_replay(self) -> None:
        router = self.router()
        router.accept(self.wake())
        entry = json.loads(self.pending().read_text(encoding="utf-8"))
        binding = {
            "host": "server", "session_id": SID_A, "letter": "AA", "generation": 1,
            "code": "session-subagent",
        }
        entry["rejected_targets"] = [binding]
        self.assertTrue(WR._loadable(entry, "w-1"))

        for bad in (
            {**binding, "host": ["server"]},
            {**binding, "session_id": "-option"},
            {**binding, "letter": "AAA"},
            {**binding, "generation": True},
            {**binding, "code": "unknown-refusal"},
            {**binding, "extra": "field"},
        ):
            with self.subTest(bad=bad):
                entry["rejected_targets"] = [bad]
                self.assertFalse(WR._loadable(entry, "w-1"))
        entry["rejected_targets"] = [binding] * (WR.MAX_REJECTED_TARGETS + 1)
        self.assertFalse(WR._loadable(entry, "w-1"))

    def test_explicit_letter_reply_or_native_fragment_never_redirects(self) -> None:
        self.registry.letters = {"AA": row(SID_A, "AA", "First chat")}
        wakes = (
            self.wake("letter", letter="AA"),
            self.wake("reply", letter="AA", linked_by="reply"),
            self.wake("fragment", session_fragments=("aaaaaaaa",)),
        )
        for wake in wakes:
            with self.subTest(wake_id=wake.wake_id):
                self.results = [self.refusal()]
                router = self.router(max_attempts=3)
                router.accept(wake)
                self.drain(router)
                receipt = self.done(wake.wake_id)
                self.assertEqual(receipt["status"], "dead")
                self.assertEqual(receipt["attempts"], 1)
                self.assertNotIn("rejected_targets", receipt)
        self.assertEqual(self.delivered, [(SID_A, "letter"), (SID_A, "reply"), (SID_A, "fragment")])

    def test_unknown_and_other_never_run_refusals_do_not_repick(self) -> None:
        router = self.router(max_attempts=3)
        self.results = [WR.Delivery("unknown", "wake", code="session-subagent")]
        router.accept(self.wake("unknown"))
        self.drain(router)
        self.assertEqual(self.done("unknown")["status"], "unknown")

        router = self.router(max_attempts=3)
        router.never_ran = router.never_ran | {"session-not-a-chat"}
        self.results = [WR.Delivery("refused", "wake", code="session-not-a-chat")]
        router.accept(self.wake("other-refusal"))
        self.drain(router)
        receipt = self.done("other-refusal")
        self.assertEqual((receipt["status"], receipt["delivered"]), ("dead", False))
        self.assertEqual(self.delivered, [(SID_A, "unknown"), (SID_A, "other-refusal")])
        self.assertNotIn("rejected_targets", receipt)


del _RouterBase
