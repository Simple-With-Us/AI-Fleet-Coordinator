"""Existing-chat sends must never infer acceptance from a timeout."""
import socket
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from agent_sync.chat_transport import GrokTransport

class TransportTests(unittest.TestCase):
    def transport(self, state="idle", live=True):
        transport = GrokTransport.__new__(GrokTransport)
        transport.disk = SimpleNamespace(load_active=lambda: [{"sessionId": "chat-1", "cwd": "/work", "pid": 123}], turn_state=lambda _: {"turnState": state, "turnEndedAt": 1}, is_self_session=lambda _: False)
        transport._live_pid = lambda _: live
        client = Mock()
        transport.leader = SimpleNamespace(LeaderStdio=lambda: client, _initialize=Mock(), _resume=Mock())
        return transport, {"platform": "grok-leader", "host": socket.gethostname(), "session_id": "chat-1", "cwd": "/work"}, client

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
        for state, live in [("working", True), ("needs-input", True), ("unknown", True), ("idle", False)]:
            transport, binding, client = self.transport(state, live)
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

    def test_pid_owner(self):
        with patch("agent_sync.chat_transport.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="/bin/zsh\n")):
            self.assertFalse(GrokTransport._live_pid(123))
        self.assertFalse(GrokTransport._live_pid(True))

    def test_pause_during_connect_prevents_prompt(self):
        transport, binding, client = self.transport()
        self.assertEqual(transport.send(binding, "nudge", permit=lambda: False), "deferred")
        client.request.assert_not_called()
        client.close.assert_called_once()

    def test_empty_event_history_cannot_prove_idle(self):
        transport, binding, client = self.transport()
        transport.disk.turn_state = lambda _: {"turnState": "idle"}
        self.assertEqual(transport.preflight(binding), "busy")
        self.assertEqual(transport.send(binding, "nudge"), "deferred")
        client.request.assert_not_called()
