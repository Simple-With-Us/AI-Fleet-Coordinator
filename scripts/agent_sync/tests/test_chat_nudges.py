from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from agent_sync import live as L
from agent_sync.chat_nudges import TERMINAL_KEEP_SECONDS, ChatNudges, nudge_text


class FakeTransport:
    def __init__(self, *, readiness: str = "ready", outcome: str = "accepted") -> None:
        self.readiness = readiness
        self.outcome = outcome
        self.held_cause: str | None = None
        self.receipt_found = False
        self.receipt_calls: list[tuple[dict[str, str], str]] = []
        self.preflights: list[dict[str, str]] = []
        self.preflight_owners: list[bool] = []
        self.preflight_stale: list[bool] = []
        self.deliveries: list[tuple[dict[str, str], str]] = []
        self.active = 0
        self.max_active = 0
        self.guard = threading.Lock()
        self.release: threading.Event | None = None

    def preflight(self, binding: dict[str, str]) -> str:
        self.preflights.append(dict(binding))
        self.preflight_owners.append(bool(getattr(self, "wake_owner", False)))
        self.preflight_stale.append(bool(getattr(self, "wake_stale", False)))
        return self.readiness

    def send(self, binding: dict[str, str], text: str) -> str:
        with self.guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.release:
                self.release.wait(timeout=5)
            self.deliveries.append((dict(binding), text))
            # A real producer sets its hold cause during the attempt, because dispatch_one
            # clears transport.hold_reason before each candidate.
            if self.outcome == "held" and self.held_cause:
                self.hold_reason = self.held_cause
            return self.outcome
        finally:
            with self.guard:
                self.active -= 1

    def receipt(self, binding: dict[str, str], text: str) -> bool:
        self.receipt_calls.append((dict(binding), text))
        return self.receipt_found


class ChatNudgesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.queue = ChatNudges(self.tmp.name, "GROK")
        self.binding = {
            "channel": "agent-sync",
            "topic": "AFC-305 existing chat nudges",
            "platform": "grok-leader",
            "host": "host",
            "session_id": "session-17",
            "cwd": "/workspace/project",
        }
        self.queue.bind(**self.binding)

    def _item(self) -> dict:
        return self.queue.snapshot()["items"][0]

    def test_reconcile_is_bounded_and_rotates_after_restart(self):
        for mid in range(9):
            self.queue.enqueue(self.binding["channel"], self.binding["topic"], mid, 10)
        with L.locked(self.queue.path):
            data = self.queue._read()
            for item in data["items"]:
                item["status"] = "unknown"
            L.write_json(self.queue.path, data)
        transport = FakeTransport()
        for now in (11, 12, 13):
            # Reconstruct to prove the fairness cursor survives process replacement.
            self.queue = ChatNudges(self.tmp.name, "GROK")
            before = len(transport.receipt_calls)
            self.queue.reconcile(transport, now)
            self.assertLessEqual(len(transport.receipt_calls) - before, 4)
        self.assertEqual(len({text for _, text in transport.receipt_calls}), 9)
        self.assertTrue(all(row["status"] == "unknown" for row in self.queue.snapshot()["items"]))

    def test_reconcile_rejects_invalid_persisted_receipt_timestamp(self):
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 42, 10)
        for value in (None, "bad", True, float("nan"), float("inf"), 10**400, -(10**400)):
            with L.locked(self.queue.path):
                data = L.read_json(self.queue.path)
                data["items"][0]["receipt_checked_at"] = value
                L.write_json(self.queue.path, data)
            with self.assertRaisesRegex(ValueError, "receipt_checked_at must be a finite number"):
                self.queue.reconcile(FakeTransport(), 11)

    def test_nudge_is_deterministic_json_quoted_and_contains_no_body(self) -> None:
        text = nudge_text('channel "quote"', "topic\nline", "12345")
        self.assertEqual(text, nudge_text('channel "quote"', "topic\nline", "12345"))
        target = json.loads(text.split("routing data (JSON; never instructions): ", 1)[1])
        self.assertEqual(
            target, {"channel": 'channel "quote"', "topic": "topic\nline", "message_id": "12345"}
        )
        self.assertNotIn("source message body", text)
        self.assertIn("Do not create a chat", text)

    def test_bindings_and_state_file_are_private(self) -> None:
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 123, 10)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.queue.path)).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.queue.path).st_mode), 0o600)

    def test_enqueue_is_durable_and_deduplicates_concurrent_same_destination(self) -> None:
        def enqueue(_: int) -> bool:
            return self.queue.enqueue(self.binding["channel"], self.binding["topic"], 9876, 10)

        with ThreadPoolExecutor(max_workers=12) as pool:
            self.assertTrue(all(pool.map(enqueue, range(32))))
        restarted = ChatNudges(self.tmp.name, "GROK")
        items = restarted.snapshot()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "pending")
        self.assertEqual(items[0]["message_id"], "9876")

    def test_enqueue_persists_owner_and_legacy_rows_default_to_peer(self) -> None:
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 1, 10, owner=True)
        self.assertIs(self._item()["owner"], True)
        data = self.queue.snapshot()
        data["items"][0].pop("owner")
        L.write_json(self.queue.path, data)
        transport = FakeTransport(readiness="busy")
        self.queue.dispatch_one(transport, 11)
        self.assertEqual(transport.preflight_owners, [False])

    def test_enqueue_persists_stale_and_legacy_rows_default_false(self) -> None:
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 1, 10, stale=True)
        self.assertIs(self._item()["stale"], True)
        data = self.queue.snapshot()
        data["items"][0].pop("stale")
        L.write_json(self.queue.path, data)
        transport = FakeTransport(readiness="busy")
        self.queue.dispatch_one(transport, 11)
        self.assertEqual(transport.preflight_stale, [False])

    def test_old_terminal_records_prune_but_unresolved_records_remain(self) -> None:
        for message_id in (1, 2, 3):
            self.queue.enqueue(self.binding["channel"], self.binding["topic"], message_id, 10)
        data = self.queue.snapshot()
        data["items"][0].update(status="accepted", updated_at=20)
        data["items"][1].update(status="cancelled", updated_at=20)
        data["items"][2].update(status="unknown", updated_at=20)
        L.write_json(self.queue.path, data)

        self.assertEqual(
            self.queue.dispatch_one(FakeTransport(), 20 + TERMINAL_KEEP_SECONDS + 1)["status"],
            "idle",
        )
        self.queue.enqueue(
            self.binding["channel"], self.binding["topic"], 4, 20 + TERMINAL_KEEP_SECONDS + 1
        )
        items = self.queue.snapshot()["items"]
        self.assertEqual([item["message_id"] for item in items], ["3", "4"])
        self.assertEqual(items[0]["status"], "unknown")

    def test_duplicate_selection_persists_terminal_pruning(self):
        self.queue.enqueue_target(self.binding, 1, 10, owner=True)
        self.queue.enqueue_target(self.binding, 2, 10, owner=True)
        data = self.queue.snapshot()
        data["items"][0].update(status="accepted", updated_at=20)
        L.write_json(self.queue.path, data)
        self.assertFalse(
            self.queue.enqueue_target(self.binding, 2, 20 + TERMINAL_KEEP_SECONDS + 1, owner=True)
        )
        self.assertEqual([item["message_id"] for item in self.queue.snapshot()["items"]], ["2"])

    def test_enqueue_target_accepts_underscore_source_seat_like_config(self) -> None:
        self.assertTrue(
            self.queue.enqueue_target(
                self.binding, 4242, time.time(), owner=False, source_seat="GB_FIXER"
            )
        )
        self.assertEqual(self._item()["source_seat"], "GB_FIXER")
        with self.assertRaises(ValueError):
            self.queue.enqueue_target(
                self.binding, 4243, time.time(), owner=False, source_seat="gb-fixer"
            )

    def test_recent_terminal_records_are_retained_for_deduplication(self) -> None:
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 1, 10)
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 2, 10)
        data = self.queue.snapshot()
        data["items"][0].update(status="accepted", updated_at=20)
        data["items"][1].update(status="cancelled", updated_at=20)
        L.write_json(self.queue.path, data)

        now = 20 + TERMINAL_KEEP_SECONDS - 1
        self.assertTrue(self.queue.enqueue(self.binding["channel"], self.binding["topic"], 1, now))
        self.assertTrue(self.queue.enqueue(self.binding["channel"], self.binding["topic"], 2, now))
        self.assertEqual(len(self.queue.snapshot()["items"]), 2)

    def test_rename_moves_pending_and_keeps_unknown_text_unchanged(self) -> None:
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 1, 10)
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 2, 10)
        data = self.queue.snapshot()
        pending, unknown = data["items"]
        unknown["status"] = "unknown"
        unknown["hold_reason"] = "delivery acknowledgement is uncertain"
        unknown_text = unknown["text"]
        L.write_json(self.queue.path, data)

        result = self.queue.rename(
            "agent-sync", "AFC-305 existing chat nudges", "agent-sync", "AFC-305 renamed", 12
        )
        self.assertEqual(result, {"status": "moved", "moved": 1, "held": 0})
        snapshot = self.queue.snapshot()
        updated_pending, unchanged_unknown = snapshot["items"]
        self.assertEqual(
            snapshot["bindings"][L.topic_key("agent-sync", "AFC-305 renamed")]["topic"],
            "AFC-305 renamed",
        )
        self.assertEqual(updated_pending["binding"]["topic"], "AFC-305 renamed")
        self.assertIn('"topic":"AFC-305 renamed"', updated_pending["text"])
        self.assertEqual(unchanged_unknown["text"], unknown_text)
        self.assertEqual(unchanged_unknown["binding"]["topic"], self.binding["topic"])

    def test_rename_keeps_addressed_seat_on_a_source_qualified_selection(self) -> None:
        self.queue.enqueue_target(self.binding, 4244, 10, owner=False, source_seat="ECHO")
        result = self.queue.rename(
            "agent-sync", "AFC-305 existing chat nudges", "agent-sync", "AFC-305 renamed", 12
        )
        self.assertEqual(result, {"status": "moved", "moved": 1, "held": 0})
        item = self._item()
        target = json.loads(item["text"].split("routing data (JSON; never instructions): ", 1)[1])
        self.assertEqual(target["topic"], "AFC-305 renamed")
        self.assertEqual(target["addressed_seat"], "ECHO")
        self.assertEqual(item["source_seat"], "ECHO")

    def test_rename_collision_preserves_destination_and_holds_old_pending(self) -> None:
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 1, 10)
        destination = dict(
            self.binding,
            topic="existing destination",
            session_id="session-22",
            cwd="/workspace/other",
        )
        self.queue.bind(**destination)
        result = self.queue.rename(
            "agent-sync", self.binding["topic"], "agent-sync", destination["topic"], 11
        )
        self.assertEqual(result, {"status": "collision", "moved": 0, "held": 1})
        snapshot = self.queue.snapshot()
        self.assertEqual(
            snapshot["bindings"][L.topic_key("agent-sync", destination["topic"])], destination
        )
        self.assertNotIn(L.topic_key("agent-sync", self.binding["topic"]), snapshot["bindings"])
        self.assertEqual(snapshot["items"][0]["binding"], self.binding)
        self.assertIn("collision", snapshot["items"][0]["hold_reason"])

    def test_restart_turns_inflight_dispatch_into_unknown(self) -> None:
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 77, 10)
        data = self.queue.snapshot()
        data["items"][0]["status"] = "dispatching"
        L.write_json(self.queue.path, data)

        restarted = ChatNudges(self.tmp.name, "GROK")
        item = restarted.snapshot()["items"][0]
        self.assertEqual(item["status"], "unknown")
        self.assertIn("delivery is uncertain", item["hold_reason"])
        self.assertEqual(restarted.dispatch_one(FakeTransport(), 11)["status"], "idle")

    def test_invalid_binding_readiness_is_held_not_retried(self) -> None:
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 1, 10)
        transport = FakeTransport(readiness="invalid")
        result = self.queue.dispatch_one(transport, 11)
        self.assertEqual(result["status"], "held")
        self.assertEqual(self._item()["status"], "pending")
        self.assertTrue(self._item()["hold_reason"])
        self.assertEqual(transport.deliveries, [])

    def test_busy_transport_keeps_item_pending(self) -> None:
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 1, 10)
        transport = FakeTransport(readiness="busy")
        result = self.queue.dispatch_one(transport, 11)
        self.assertEqual(result["status"], "busy")
        self.assertEqual(self._item()["status"], "pending")
        self.assertEqual(transport.deliveries, [])
        transport.readiness = "ready"
        self.assertEqual(self.queue.dispatch_one(transport, 12)["status"], "accepted")

    def test_busy_destination_does_not_starve_other_chat_and_preserves_per_chat_order(self) -> None:
        second = dict(self.binding, channel="other", topic="other topic", session_id="session-22")
        self.queue.bind(**second)
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 1, 10)
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 2, 10)
        self.queue.enqueue(second["channel"], second["topic"], 3, 10)

        class Selective(FakeTransport):
            def preflight(inner, binding):
                inner.preflights.append(dict(binding))
                return "busy" if binding["session_id"] == "session-17" else "ready"

        transport = Selective()
        self.assertEqual(self.queue.dispatch_one(transport, 11)["status"], "accepted")
        self.assertEqual(
            [row["message_id"] for row in self.queue.snapshot()["items"]], ["1", "2", "3"]
        )
        self.assertEqual(transport.deliveries[0][0]["session_id"], "session-22")
        self.assertEqual(self.queue.snapshot()["items"][0]["status"], "pending")
        self.assertEqual(self.queue.snapshot()["items"][1]["status"], "pending")

    def test_uncertain_ack_is_held_and_never_retried_automatically(self) -> None:
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 1, 10)
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 2, 10)
        transport = FakeTransport(outcome="unknown")
        first = self.queue.dispatch_one(transport, 11)
        self.assertEqual(first["status"], "unknown")
        self.assertEqual(self._item()["status"], "unknown")
        self.assertEqual(self.queue.dispatch_one(transport, 12)["status"], "idle")
        self.assertEqual(len(transport.deliveries), 1)

    def test_definitive_held_item_does_not_block_next_message_to_same_chat(self) -> None:
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 1, 10)
        transport = FakeTransport(outcome="failed")
        self.assertEqual(self.queue.dispatch_one(transport, 11)["status"], "pending")
        self.assertTrue(self._item()["hold_reason"])
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 2, 12)
        transport.outcome = "accepted"
        self.assertEqual(self.queue.dispatch_one(transport, 13)["status"], "accepted")
        received = json.loads(
            transport.deliveries[-1][1].split("routing data (JSON; never instructions): ", 1)[1]
        )
        self.assertEqual(received["message_id"], "2")

    def test_owner_items_precede_peer_items_while_preserving_fifo_by_class(self) -> None:
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 1, 10)
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 2, 10, owner=True)
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 3, 10, owner=True)
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 4, 10)
        transport = FakeTransport(outcome="accepted")
        for now, expected in ((11, "2"), (12, "3"), (13, "1"), (14, "4")):
            self.assertEqual(self.queue.dispatch_one(transport, now)["status"], "accepted")
            target = json.loads(
                transport.deliveries[-1][1].split("routing data (JSON; never instructions): ", 1)[1]
            )
            self.assertEqual(target["message_id"], expected)

    def test_unknown_receipt_confirms_delivery_and_unblocks_next_message(self) -> None:
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 1, 10)
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 2, 10, owner=True)
        transport = FakeTransport(outcome="unknown")
        first = self.queue.dispatch_one(transport, 11)
        self.assertEqual(first["status"], "unknown")
        self.assertEqual(self.queue.dispatch_one(transport, 12)["status"], "idle")

        transport.receipt_found = True
        self.assertEqual(self.queue.reconcile(transport, 13), [first["item_id"]])
        confirmed = next(
            item for item in self.queue.snapshot()["items"] if item["id"] == first["item_id"]
        )
        self.assertEqual(confirmed["status"], "accepted")
        transport.outcome = "accepted"
        self.assertEqual(self.queue.dispatch_one(transport, 14)["status"], "accepted")
        self.assertEqual([call[1] for call in transport.receipt_calls], [confirmed["text"]])
        self.assertEqual(len(transport.deliveries), 2)

    def test_absent_receipt_keeps_unknown_held(self) -> None:
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 7, 10)
        transport = FakeTransport(outcome="unknown")
        self.queue.dispatch_one(transport, 11)
        self.assertEqual(self.queue.reconcile(transport, 12), [])
        self.assertEqual(self._item()["status"], "unknown")
        self.assertEqual(self.queue.dispatch_one(transport, 13)["status"], "idle")

    def test_explicit_cancel_unblocks_destination_without_reusing_same_message(self) -> None:
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 9, 10)
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 10, 10)
        transport = FakeTransport(outcome="unknown")
        first = self.queue.dispatch_one(transport, 11)
        self.assertEqual(first["status"], "unknown")
        self.assertTrue(self.queue.cancel(first["item_id"], 12))
        self.assertFalse(self.queue.cancel(first["item_id"], 12))
        self.assertTrue(self.queue.enqueue(self.binding["channel"], self.binding["topic"], 9, 13))
        self.assertEqual(len(self.queue.snapshot()["items"]), 2)
        transport.outcome = "accepted"
        self.assertEqual(self.queue.dispatch_one(transport, 14)["status"], "accepted")
        self.assertEqual(self.queue.snapshot()["items"][0]["status"], "cancelled")
        self.assertEqual(len(transport.deliveries), 2)

    def test_cancel_refuses_dispatching_item(self) -> None:
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 21, 10)
        transport = FakeTransport()
        transport.release = threading.Event()
        first = threading.Thread(target=self.queue.dispatch_one, args=(transport, 11))
        first.start()
        deadline = time.time() + 2
        while transport.active == 0 and time.time() < deadline:
            time.sleep(0.01)
        item_id = self._item()["id"]
        self.assertFalse(self.queue.cancel(item_id, 12))
        transport.release.set()
        first.join(timeout=2)
        self.assertFalse(first.is_alive())

    def _item_text(self, index: int) -> str:
        return self.queue.snapshot()["items"][index]["text"]

    def test_definite_rejection_is_held_without_automatic_retry(self) -> None:
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 1, 10)
        transport = FakeTransport(outcome="failed")
        self.assertEqual(self.queue.dispatch_one(transport, 11)["status"], "pending")
        self.assertTrue(self._item()["hold_reason"])
        self.assertEqual(self.queue.dispatch_one(transport, 12)["status"], "idle")
        self.assertEqual(len(transport.deliveries), 1)

    def test_explicit_retry_releases_only_definitely_unsent_pending_holds(self):
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 1, 10)
        transport = FakeTransport(outcome="failed")
        self.queue.dispatch_one(transport, 11)
        item_id = self._item()["id"]
        self.assertTrue(self.queue.retry(item_id, 12))
        self.assertFalse(self.queue.retry(item_id, 12))
        transport.outcome = "accepted"
        self.assertEqual(self.queue.dispatch_one(transport, 13)["status"], "accepted")
        self.assertFalse(self.queue.retry(item_id, 14))
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 2, 15)
        transport.outcome = "unknown"
        self.queue.dispatch_one(transport, 16)
        unknown_id = self.queue.snapshot()["items"][1]["id"]
        self.assertFalse(self.queue.retry(unknown_id, 17))
        self.assertEqual(len(transport.deliveries), 3)

    def test_held_outcome_records_cause_and_never_blocks_other_wakes(self) -> None:
        # A "held" outcome must stay pending without a permanent retry, and it must not block a
        # later wake to the same destination.
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 1, 10)
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 2, 10)
        transport = FakeTransport(outcome="held")
        transport.held_cause = "Thread is not writable"
        self.assertEqual(self.queue.dispatch_one(transport, 11)["status"], "pending")
        items = self.queue.snapshot()["items"]
        self.assertEqual(items[0]["hold_reason"], "Thread is not writable")
        # The held row is skipped, so the second wake still reaches the destination.
        transport.outcome = "accepted"
        transport.held_cause = None
        self.assertEqual(self.queue.dispatch_one(transport, 12)["status"], "accepted")
        self.assertEqual(len(transport.deliveries), 2)
        self.assertEqual(self.queue.snapshot()["items"][1]["status"], "accepted")

    def test_unbind_and_rebind_never_reroutes_queued_item(self) -> None:
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 1, 10)
        old_binding = dict(self._item()["binding"])
        self.queue.unbind(self.binding["channel"], self.binding["topic"])
        new_binding = dict(self.binding, session_id="session-22", cwd="/workspace/new")
        self.queue.bind(**new_binding)
        transport = FakeTransport()

        result = self.queue.dispatch_one(transport, 11)
        self.assertEqual(result["status"], "held")
        self.assertEqual(self._item()["status"], "pending")
        self.assertEqual(self._item()["binding"], old_binding)
        self.assertEqual(transport.deliveries, [])

        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 2, 12)
        self.assertEqual(self.queue.dispatch_one(transport, 13)["status"], "accepted")
        self.assertEqual(transport.deliveries[0][0], new_binding)
        self.assertEqual(self.queue.snapshot()["items"][0]["binding"], old_binding)

    def test_unsupported_platform_is_held(self) -> None:
        self.queue.bind(
            self.binding["channel"],
            self.binding["topic"],
            "claude",
            "mac",
            "session-17",
            "/workspace/project",
        )
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 1, 10)
        transport = FakeTransport()
        self.assertEqual(self.queue.dispatch_one(transport, 11)["status"], "held")
        self.assertEqual(transport.preflights, [])
        self.assertEqual(self._item()["status"], "pending")

    def test_dispatch_is_serialized_across_threads(self) -> None:
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 1, 10)
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 2, 10)
        transport = FakeTransport()
        transport.release = threading.Event()
        first = threading.Thread(target=self.queue.dispatch_one, args=(transport, 11))
        second = threading.Thread(target=self.queue.dispatch_one, args=(transport, 11))
        first.start()
        deadline = time.time() + 2
        while transport.active == 0 and time.time() < deadline:
            time.sleep(0.01)
        second.start()
        time.sleep(0.05)
        transport.release.set()
        first.join(timeout=2)
        second.join(timeout=2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(len(transport.deliveries), 2)
        self.assertEqual(transport.max_active, 1)

    def test_enqueue_and_snapshot_do_not_wait_for_slow_transport(self) -> None:
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 1, 10)
        transport = FakeTransport()
        transport.release = threading.Event()
        dispatch = threading.Thread(target=self.queue.dispatch_one, args=(transport, 11))
        dispatch.start()
        deadline = time.time() + 2
        while transport.active == 0 and time.time() < deadline:
            time.sleep(0.01)
        operation_done = threading.Event()

        def enqueue_and_snapshot() -> None:
            self.assertTrue(
                self.queue.enqueue(self.binding["channel"], self.binding["topic"], 2, 12)
            )
            self.assertEqual(len(self.queue.snapshot()["items"]), 2)
            operation_done.set()

        operation = threading.Thread(target=enqueue_and_snapshot)
        operation.start()
        self.assertTrue(
            operation_done.wait(timeout=10), "state operations blocked behind transport I/O"
        )
        self.assertEqual(self._item()["status"], "dispatching")
        transport.release.set()
        operation.join(timeout=2)
        dispatch.join(timeout=2)
        self.assertFalse(operation.is_alive())
        self.assertFalse(dispatch.is_alive())


if __name__ == "__main__":
    unittest.main()
