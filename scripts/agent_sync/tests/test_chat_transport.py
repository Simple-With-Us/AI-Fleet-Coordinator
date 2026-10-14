"""Existing-chat sends must never infer acceptance from a timeout."""
import json
import socket
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_sync.chat_transport import GrokTransport


class TransportTests(unittest.TestCase):
    def transport(self, state="idle", *, ended=1, attached=()):
        transport = GrokTransport.__new__(GrokTransport)
        active = [{"sessionId": sid, "pid": 123} for sid in attached]
        transport.disk = SimpleNamespace(
            load_active=lambda: active,
            turn_state=lambda _: {"turnState": state, "turnEndedAt": ended},
            is_self_session=lambda _: False,
        )
        transport.disk.find_session_dir = lambda _: None
        transport._live_pid = lambda _: True
        client = Mock()
        transport.leader = SimpleNamespace(LeaderStdio=lambda: client, _initialize=Mock(), _resume=Mock())
        transport.discover = Mock(return_value=[{
            "platform": "grok-leader", "host": socket.gethostname(),
            "session_id": "chat-1", "cwd": "/work", "state": state,
            "turn_ended_at": ended, "pending_permission": state == "needs-input",
            "self": False, "wake": state == "idle" and bool(ended),
        }])
        return transport, {"platform": "grok-leader", "host": socket.gethostname(),
                           "session_id": "chat-1", "cwd": "/work"}, client

    @staticmethod
    def leader_result(sessions, *, ok=True, returncode=0):
        return SimpleNamespace(returncode=returncode,
                               stdout=json.dumps({"ok": ok, "sessions": sessions}), stderr="")

    def test_discovery_uses_leader_list_when_active_sessions_is_empty(self):
        transport = GrokTransport.__new__(GrokTransport)
        transport.disk = SimpleNamespace(
            load_active=lambda: [],
            turn_state=lambda _: {"turnState": "idle", "turnEndedAt": 10},
            is_self_session=lambda _: False,
        )
        with patch("agent_sync.chat_transport.subprocess.run", return_value=self.leader_result([
            {"sessionId": "chat-1", "cwd": "/work", "title": "Existing chat", "updatedAt": 9},
            {"sessionId": "chat-no-cwd", "title": "Workspace unavailable"},
        ])) as run:
            rows = transport.discover()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["session_id"], "chat-1")
        self.assertTrue(rows[0]["wake"])
        self.assertFalse(rows[0]["attached"])
        self.assertIsNone(rows[1]["cwd"])
        self.assertFalse(rows[1]["wake"])
        self.assertEqual(run.call_args.args[0][-1], "list")
        self.assertEqual(run.call_args.kwargs["timeout"], 10)

    def test_discovery_fails_closed_on_leader_errors_or_bad_data(self):
        transport = GrokTransport.__new__(GrokTransport)
        transport.disk = SimpleNamespace(load_active=lambda: [], turn_state=lambda _: {},
                                         is_self_session=lambda _: False)
        for result in (self.leader_result([], ok=False, returncode=1),
                       SimpleNamespace(returncode=0, stdout="not-json", stderr="")):
            with self.subTest(result=result), patch("agent_sync.chat_transport.subprocess.run", return_value=result):
                with self.assertRaises(RuntimeError):
                    transport.discover()

    def test_leader_listed_busy_unknown_and_no_history_are_not_wakeable(self):
        for state, ended, expected in (("working", 1, False), ("needs-input", 1, False),
                                       ("unknown", 1, False), ("idle", None, False),
                                       ("idle", 1, True)):
            transport = GrokTransport.__new__(GrokTransport)
            transport.disk = SimpleNamespace(
                load_active=lambda: [],
                turn_state=lambda _, s=state, e=ended: {"turnState": s, "turnEndedAt": e,
                                                          "pendingTool": "permission" if s == "needs-input" else None},
                is_self_session=lambda _: False,
            )
            with self.subTest(state=state, ended=ended), patch(
                    "agent_sync.chat_transport.subprocess.run",
                    return_value=self.leader_result([{"sessionId": "chat-1", "cwd": "/work"}])):
                row = transport.discover()[0]
                self.assertEqual(row["wake"], expected)

    def test_correlated_success(self):
        transport, binding, client = self.transport()
        self.assertEqual(transport.send(binding, "nudge"), "accepted")
        self.assertEqual(client.request.call_args.args[0], "session/prompt")
        self.assertEqual(client.request.call_args.args[1]["sessionId"], "chat-1")
        client.close.assert_called_once()

    def test_timeout_unknown_without_retry(self):
        transport, binding, client = self.transport()
        client.request.side_effect = TimeoutError()
        self.assertEqual(transport.send(binding, "nudge"), "unknown")
        self.assertEqual(client.request.call_count, 1)
        client.close.assert_called_once()

    def test_busy_unknown_dead(self):
        for state, ended in (("working", 1), ("needs-input", 1), ("unknown", 1), ("idle", None)):
            transport, binding, client = self.transport(state, ended=ended)
            self.assertEqual(transport.preflight(binding), "busy")
            self.assertEqual(transport.send(binding, "nudge"), "deferred")
            client.request.assert_not_called()

    def test_target_must_match(self):
        for field, value in [("host", "other-host"), ("platform", "shellular"), ("cwd", "/other"), ("session_id", "other")]:
            transport, binding, client = self.transport()
            binding[field] = value
            self.assertNotEqual(transport.preflight(binding), "ready")
            transport.send(binding, "nudge")
            client.request.assert_not_called()

    def test_failed_resume(self):
        transport, binding, client = self.transport()
        transport.leader._resume.side_effect = RuntimeError("unavailable")
        self.assertEqual(transport.send(binding, "nudge"), "failed")
        client.request.assert_not_called()
        client.close.assert_called_once()

    def test_recheck_after_resume(self):
        transport, binding, client = self.transport()
        transport.preflight = Mock(side_effect=["ready", "busy"])
        self.assertEqual(transport.send(binding, "nudge"), "deferred")
        client.request.assert_not_called()

    def test_pause_during_connect_prevents_prompt(self):
        transport, binding, client = self.transport()
        self.assertEqual(transport.send(binding, "nudge", permit=lambda: False), "deferred")
        client.request.assert_not_called()
        client.close.assert_called_once()

    def test_long_turn_with_saved_user_receipt_is_accepted(self):
        transport, binding, client = self.transport()
        client.request.side_effect = TimeoutError()
        with tempfile.TemporaryDirectory() as directory:
            transport.disk.find_session_dir = lambda _: Path(directory)
            (Path(directory) / "chat_history.jsonl").write_text(json.dumps(
                {"type": "user", "content": [{"type": "text", "text": "nudge"}]}))
            self.assertEqual(transport.send(binding, "nudge"), "accepted")
        self.assertEqual(client.request.call_count, 1)

    def test_assistant_or_synthetic_copy_is_not_receipt(self):
        transport, binding, _ = self.transport()
        with tempfile.TemporaryDirectory() as directory:
            transport.disk.find_session_dir = lambda _: Path(directory)
            for row in ({"type": "assistant", "content": "nudge"},
                        {"type": "user", "content": "nudge", "synthetic_reason": "reminder"},
                        {"type": "user", "content": "another message"}):
                (Path(directory) / "chat_history.jsonl").write_text(json.dumps(row))
                self.assertFalse(transport.receipt(binding, "nudge"))


if __name__ == "__main__":
    unittest.main()
