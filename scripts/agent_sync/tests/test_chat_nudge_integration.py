from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from agent_sync import live as L
from agent_sync.cli import build_parser
from agent_sync.daemon import set_pause
from agent_sync.live import SeatPaths
from agent_sync.tests.test_daemon import DaemonHarness


class ChatNudgeIntegrationTests(DaemonHarness):
    def _grok_runner(self, daemon):
        base = daemon.seats["CLAUDE"]
        return SimpleNamespace(seat="GROK", cfg=SimpleNamespace(enabled=True, wake_max_usd=0.25,
                              budget=base.cfg.budget), connected=True, me=object(), fatal=False,
                              ledger=base.ledger, paths=base.paths)

    def test_bound_grok_dispatch_is_accounted_and_busy_keeps_pending(self):
        self.write_config()
        daemon = self.daemon()
        target = {"channel": "agent-sync", "topic": "AFC-305", "platform": "grok-leader",
                  "host": "host", "session_id": "existing-1", "cwd": "/repo"}
        daemon.chat_nudges.bind(**target)
        daemon.chat_nudges.enqueue("agent-sync", "AFC-305", 42, self.clock.time())
        runner = self._grok_runner(daemon)
        fake = SimpleNamespace(preflight=lambda binding: "busy",
                               send=lambda binding, text, **kwargs:
                               "accepted" if kwargs["permit"]() else "deferred")
        with mock.patch("agent_sync.daemon.CT.GrokTransport", return_value=fake):
            self.assertEqual(daemon.dispatch_chat_nudge(runner)["status"], "busy")
        self.assertEqual(daemon.chat_nudges.snapshot()["items"][0]["status"], "pending")

        fake.preflight = lambda binding: "ready"
        with mock.patch("agent_sync.daemon.CT.GrokTransport", return_value=fake):
            self.assertEqual(daemon.dispatch_chat_nudge(runner)["status"], "accepted")
        rows = runner.ledger.rows()
        nudge = [row for row in rows if row.get("adapter") == "chat-nudge"]
        self.assertEqual([row["state"] for row in nudge], ["started", "done"])
        self.assertEqual(nudge[0]["topic_key"], L.topic_key("agent-sync", "AFC-305"))
        self.assertEqual(nudge[0]["owner"], False)
        self.assertEqual(nudge[-1]["cost_usd"], 0.0)
        self.assertTrue(nudge[-1]["downstream_cost_unknown"])

    def test_cli_requires_explicit_target_grok_and_has_nudge_actions(self):
        parser = build_parser()
        args = parser.parse_args(["daemon", "chats", "bind", "--target-seat", "GROK",
                                  "--channel", "agent-sync", "--topic", "AFC-305",
                                  "--session-id", "session", "--cwd", "/repo"])
        self.assertEqual(args.target_seat, "GROK")
        cancel = parser.parse_args(["daemon", "chats", "cancel", "--target-seat", "GROK",
                                    "--item-id", "a" * 64])
        self.assertEqual(cancel.item_id, "a" * 64)
        with self.assertRaises(SystemExit):
            parser.parse_args(["daemon", "chats", "status"])

    def test_bound_grok_message_is_in_inbox_and_suppresses_muse_offer(self):
        daemon = self.started()
        runner = daemon.seats["CLAUDE"]
        runner.seat = "GROK"
        runner.paths = SeatPaths(daemon.root, "GROK")
        target = {"channel": "agent-sync", "topic": "AFC-305", "platform": "grok-leader",
                  "host": "host", "session_id": "existing-1", "cwd": "/repo"}
        daemon.chat_nudges.bind(**target)
        offered = []
        daemon.offer_wake = lambda *a, **kw: offered.append((a, kw))
        mid = self.fake.add_message("Cursor", "agent-sync", "AFC-305", "@**Claude** please check")
        daemon._route(runner, dict(self.fake.messages[-1], flags=["mentioned"]))
        self.assertEqual([row["id"] for row in L.read_jsonl(runner.paths.seat_inbox)], [mid])
        self.assertEqual(daemon.chat_nudges.snapshot()["items"][0]["message_id"], str(mid))
        self.assertEqual(offered, [])

    def test_pause_and_budget_gate_pending_delivery(self):
        daemon = self.daemon()
        daemon.chat_nudges.bind("agent-sync", "topic", "grok-leader", "host", "existing", "/repo")
        runner = self._grok_runner(daemon)
        fake = SimpleNamespace(preflight=lambda binding: "ready",
                               send=mock.Mock(return_value="accepted"))
        daemon._chat_transport = fake
        for mid in (41, 42, 43):
            daemon.chat_nudges.enqueue("agent-sync", "topic", mid, self.clock.time())
        set_pause(daemon.root, "GROK", wakes_only=True, by="test", now=self.clock.time())
        self.assertEqual(daemon.dispatch_chat_nudge(runner)["status"], "busy")
        fake.send.assert_not_called()
        from agent_sync.daemon import clear_pause
        clear_pause(daemon.root, "GROK")
        self.assertEqual(daemon.dispatch_chat_nudge(runner)["status"], "accepted")
        self.assertEqual(daemon.dispatch_chat_nudge(runner)["status"], "accepted")
        self.assertEqual(daemon.dispatch_chat_nudge(runner)["status"], "busy")
        self.assertEqual(fake.send.call_count, 2)
        ids = {r["wake_id"] for r in runner.ledger.rows() if r.get("adapter") == "chat-nudge"}
        self.assertEqual(len(ids), 2)

    def test_pause_during_connect_defers_without_duplicate_prompt(self):
        daemon = self.daemon()
        daemon.chat_nudges.bind("agent-sync", "topic", "grok-leader", "host", "existing", "/repo")
        daemon.chat_nudges.enqueue("agent-sync", "topic", 41, self.clock.time())
        runner = self._grok_runner(daemon)
        def send(binding, text, *, permit):
            set_pause(daemon.root, "GROK", wakes_only=True, by="test", now=self.clock.time())
            return "accepted" if permit() else "deferred"
        daemon._chat_transport = SimpleNamespace(preflight=lambda binding: "ready", send=send)
        self.assertEqual(daemon.dispatch_chat_nudge(runner)["status"], "pending")
        self.assertEqual(runner.ledger.rows()[-1]["state"], "skipped")
        self.assertEqual(daemon.chat_nudges.snapshot()["items"][0]["status"], "pending")

    def test_late_saved_receipt_updates_ledger_without_resend(self):
        daemon = self.daemon()
        daemon.chat_nudges.bind("agent-sync", "topic", "grok-leader", "host", "existing", "/repo")
        daemon.chat_nudges.enqueue("agent-sync", "topic", 41, self.clock.time())
        runner = self._grok_runner(daemon)
        fake = SimpleNamespace(preflight=lambda binding: "ready", send=mock.Mock(return_value="unknown"),
                               receipt=mock.Mock(return_value=False))
        daemon._chat_transport = fake
        self.assertEqual(daemon.dispatch_chat_nudge(runner)["status"], "unknown")
        fake.receipt.return_value = True
        self.assertEqual(daemon.dispatch_chat_nudge(runner)["status"], "idle")
        self.assertEqual(fake.send.call_count, 1)
        self.assertEqual(runner.ledger.rows()[-1]["state"], "done")
        self.assertEqual(daemon.chat_nudges.snapshot()["items"][0]["status"], "accepted")


if __name__ == "__main__":
    unittest.main()
