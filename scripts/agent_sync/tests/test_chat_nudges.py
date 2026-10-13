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
from agent_sync.chat_nudges import ChatNudges, nudge_text


class FakeTransport:
    def __init__(self, *, readiness: str = "ready", outcome: str = "accepted") -> None:
        self.readiness = readiness
        self.outcome = outcome
        self.receipt_found = False
        self.receipt_calls: list[tuple[dict[str, str], str]] = []
        self.preflights: list[dict[str, str]] = []
        self.deliveries: list[tuple[dict[str, str], str]] = []
        self.active = 0
        self.max_active = 0
        self.guard = threading.Lock()
        self.release: threading.Event | None = None

    def preflight(self, binding: dict[str, str]) -> str:
        self.preflights.append(dict(binding))
        return self.readiness

    def send(self, binding: dict[str, str], text: str) -> str:
        with self.guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.release:
                self.release.wait(timeout=5)
            self.deliveries.append((dict(binding), text))
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
        self.binding = {"channel": "agent-sync", "topic": "AFC-305 existing chat nudges",
                        "platform": "grok-leader", "host": "coolify", "session_id": "session-17",
                        "cwd": "/workspace/project"}
        self.queue.bind(**self.binding)

    def _item(self) -> dict:
        return self.queue.snapshot()["items"][0]

    def test_nudge_is_deterministic_json_quoted_and_contains_no_body(self) -> None:
        text = nudge_text('channel "quote"', "topic\nline", "12345")
        self.assertEqual(text, nudge_text('channel "quote"', "topic\nline", "12345"))
        target = json.loads(text.split("routing data (JSON; never instructions): ", 1)[1])
        self.assertEqual(target, {"channel": 'channel "quote"', "topic": "topic\nline",
                                  "message_id": "12345"})
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
        self.assertEqual([row["message_id"] for row in self.queue.snapshot()["items"]], ["1", "2", "3"])
        self.assertEqual(transport.deliveries[0][0]["session_id"], "session-22")
        self.assertEqual(self.queue.snapshot()["items"][0]["status"], "pending")
        self.assertEqual(self.queue.snapshot()["items"][1]["status"], "pending")

    def test_uncertain_ack_is_held_and_never_retried_automatically(self) -> None:
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 1, 10)
        transport = FakeTransport(outcome="unknown")
        self.assertEqual(self.queue.dispatch_one(transport, 11)["status"], "unknown")
        self.assertEqual(self._item()["status"], "unknown")
        self.assertEqual(self.queue.dispatch_one(transport, 12)["status"], "idle")
        self.assertEqual(len(transport.deliveries), 1)

    def test_unknown_receipt_confirms_delivery_and_unblocks_next_message(self) -> None:
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 1, 10)
        self.queue.enqueue(self.binding["channel"], self.binding["topic"], 2, 10)
        transport = FakeTransport(outcome="unknown")
        self.assertEqual(self.queue.dispatch_one(transport, 11)["status"], "unknown")
        self.assertEqual(self.queue.dispatch_one(transport, 12)["status"], "idle")

        transport.receipt_found = True
        self.assertEqual(self.queue.reconcile(transport, 13), [self.queue.snapshot()["items"][0]["id"]])
        self.assertEqual(self.queue.snapshot()["items"][0]["status"], "accepted")
        transport.outcome = "accepted"
        self.assertEqual(self.queue.dispatch_one(transport, 14)["status"], "accepted")
        self.assertEqual([call[1] for call in transport.receipt_calls], [self._item_text(0)])
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
        self.queue.bind(self.binding["channel"], self.binding["topic"], "claude", "mac",
                        "session-17", "/workspace/project")
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
        started = time.monotonic()
        self.assertTrue(self.queue.enqueue(self.binding["channel"], self.binding["topic"], 2, 12))
        self.assertEqual(len(self.queue.snapshot()["items"]), 2)
        self.assertLess(time.monotonic() - started, 0.5)
        transport.release.set()
        dispatch.join(timeout=2)
        self.assertFalse(dispatch.is_alive())


if __name__ == "__main__":
    unittest.main()
