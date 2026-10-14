"""Existing-chat sends must never infer acceptance from a timeout."""
import json
import importlib.util
import io
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from agent_sync import chat_transport as CT
from agent_sync.chat_transport import GrokTransport


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.reset_discovery()

    @staticmethod
    def reset_discovery():
        with CT._DISCOVERY_LOCK:
            CT._DISCOVERY_SESSIONS = None
            CT._DISCOVERY_UPDATED = 0.0
            CT._DISCOVERY_LAST_ATTEMPT = 0.0
            CT._DISCOVERY_ERROR = None
            CT._DISCOVERY_RUNNING = False
            CT._DISCOVERY_CURSOR = None
            CT._DISCOVERY_SEEN_CURSORS = set()
            CT._DISCOVERY_CANDIDATE_SESSIONS = None
            CT._DISCOVERY_COMPLETE = False
            CT._DISCOVERY_DONE.set()

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
    def leader_result(sessions, *, ok=True, returncode=0, next_cursor=None):
        return SimpleNamespace(returncode=returncode,
                               stdout=json.dumps({"ok": ok, "sessions": sessions,
                                                  "nextCursor": next_cursor}), stderr="")

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
            rows = transport.discover(wait=True)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["session_id"], "chat-1")
        self.assertTrue(rows[0]["wake"])
        self.assertFalse(rows[0]["attached"])
        self.assertIsNone(rows[1]["cwd"])
        self.assertFalse(rows[1]["wake"])
        self.assertEqual(run.call_args.args[0][-1], "list")
        self.assertEqual(run.call_args.kwargs["timeout"], 27)
        self.assertIn("25.0", run.call_args.args[0])

    def test_discovery_walks_bounded_cursor_pages(self):
        transport = GrokTransport.__new__(GrokTransport)
        transport.disk = SimpleNamespace(load_active=lambda: [],
                                         turn_state=lambda _: {"turnState": "idle", "turnEndedAt": 10},
                                         is_self_session=lambda _: False)
        responses = [self.leader_result([{"sessionId": "chat-1", "cwd": "/one"}], next_cursor="page-2"),
                     self.leader_result([{"sessionId": "chat-2", "cwd": "/two"}])]
        with patch("agent_sync.chat_transport.subprocess.run", side_effect=responses) as run:
            rows = transport.discover()
        self.assertEqual([row["session_id"] for row in rows], ["chat-1", "chat-2"])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[1].args[0][-2:], ["--cursor", "page-2"])

    def test_discovery_stops_on_repeated_pagination_cursor(self):
        transport = GrokTransport.__new__(GrokTransport)
        transport.disk = SimpleNamespace(load_active=lambda: [], turn_state=lambda _: {},
                                         is_self_session=lambda _: False)
        responses = [self.leader_result([{"sessionId": "chat-1", "cwd": "/one"}], next_cursor="repeat"),
                     self.leader_result([{"sessionId": "chat-2", "cwd": "/two"}], next_cursor="repeat")]
        with patch("agent_sync.chat_transport.subprocess.run", side_effect=responses) as run:
            rows = transport.discover()
        self.assertEqual([row["session_id"] for row in rows], ["chat-1"])
        self.assertEqual(run.call_count, 2)
        self.assertTrue(rows[0]["stale"])

    def test_page_cap_publishes_fetched_rows_but_disables_wake(self):
        transport = GrokTransport.__new__(GrokTransport)
        transport.disk = SimpleNamespace(load_active=lambda: [],
                                         turn_state=lambda _: {"turnState": "idle", "turnEndedAt": 1},
                                         is_self_session=lambda _: False)
        responses = [self.leader_result([{"sessionId": "chat-1", "cwd": "/one"}], next_cursor="page-2"),
                     self.leader_result([{"sessionId": "chat-2", "cwd": "/two"}], next_cursor="page-3")]
        with patch.object(CT, "_DISCOVERY_MAX_PAGES", 2), \
                patch("agent_sync.chat_transport.subprocess.run", side_effect=responses):
            rows = transport.discover()
        self.assertEqual([row["session_id"] for row in rows], ["chat-1", "chat-2"])
        self.assertTrue(all(row["stale"] and not row["wake"] for row in rows))

    def test_leader_client_passes_cursor_and_preserves_next_cursor(self):
        helper_path = CT._RUNTIME / "leader-client.py"
        spec = importlib.util.spec_from_file_location("test_grok_leader_client", helper_path)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        client = SimpleNamespace(request=Mock(return_value={
            "sessions": [{"sessionId": "chat-2", "cwd": "/two"}], "nextCursor": "page-3"
        }), close=Mock())
        helper.LeaderStdio = lambda: client
        helper._initialize = Mock(return_value={})
        stdout = io.StringIO()
        with patch.object(sys, "argv", [str(helper_path), "list", "--cursor", "page-2"]), \
                redirect_stdout(stdout):
            helper.main()
        client.request.assert_called_once_with("session/list", {"cursor": "page-2"}, timeout=20.0)
        self.assertEqual(json.loads(stdout.getvalue())["nextCursor"], "page-3")
        client.close.assert_called_once()

    def test_discovery_fails_closed_on_leader_errors_or_bad_data(self):
        transport = GrokTransport.__new__(GrokTransport)
        transport.disk = SimpleNamespace(load_active=lambda: [], turn_state=lambda _: {},
                                         is_self_session=lambda _: False)
        for result in (self.leader_result([], ok=False, returncode=1),
                       SimpleNamespace(returncode=0, stdout="not-json", stderr="")):
            with self.subTest(result=result), patch("agent_sync.chat_transport.subprocess.run", return_value=result):
                with self.assertRaises(RuntimeError):
                    transport.discover(wait=True, force=True)

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
                self.reset_discovery()
                row = transport.discover(wait=True)[0]
                self.assertEqual(row["wake"], expected)

    def test_async_discovery_returns_pending_then_uses_leader_result(self):
        transport = GrokTransport.__new__(GrokTransport)
        transport.disk = SimpleNamespace(load_active=lambda: [],
                                         turn_state=lambda _: {"turnState": "idle", "turnEndedAt": 1},
                                         is_self_session=lambda _: False)
        with patch("agent_sync.chat_transport.subprocess.run", return_value=self.leader_result([
            {"sessionId": "chat-1", "cwd": "/work"}
        ])):
            with self.assertRaisesRegex(RuntimeError, "pending"):
                transport.discover(wait=False)
            self.assertTrue(CT._DISCOVERY_DONE.wait(1))
            row = transport.discover(wait=False)[0]
        self.assertTrue(row["wake"])

    def test_fresh_shared_snapshot_is_immediate_and_refresh_errors_disable_wake(self):
        sessions = [{"sessionId": "chat-1", "cwd": "/work"}]
        with CT._DISCOVERY_LOCK:
            CT._DISCOVERY_SESSIONS = sessions
            CT._DISCOVERY_UPDATED = time.monotonic()
            CT._DISCOVERY_LAST_ATTEMPT = CT._DISCOVERY_UPDATED
            CT._DISCOVERY_ERROR = None
        transport = GrokTransport.__new__(GrokTransport)
        transport.disk = SimpleNamespace(load_active=lambda: [],
                                         turn_state=lambda _: {"turnState": "idle", "turnEndedAt": 1},
                                         is_self_session=lambda _: False)
        with patch("agent_sync.chat_transport.subprocess.run") as run:
            self.assertTrue(transport.discover(wait=False)[0]["wake"])
            run.assert_not_called()
        with CT._DISCOVERY_LOCK:
            CT._DISCOVERY_ERROR = "leader list unavailable"
        row = transport.discover(wait=False)[0]
        self.assertTrue(row["stale"])
        self.assertFalse(row["wake"])

    def test_completed_inventory_refreshes_and_replaces_old_rows(self):
        transport = GrokTransport.__new__(GrokTransport)
        transport.disk = SimpleNamespace(load_active=lambda: [],
                                         turn_state=lambda _: {"turnState": "idle", "turnEndedAt": 1},
                                         is_self_session=lambda _: False)
        with CT._DISCOVERY_LOCK:
            CT._DISCOVERY_SESSIONS = [{"sessionId": "chat-1", "cwd": "/work"}]
            CT._DISCOVERY_UPDATED = time.monotonic()
            CT._DISCOVERY_LAST_ATTEMPT = 0.0
            CT._DISCOVERY_COMPLETE = True
        with patch("agent_sync.chat_transport.subprocess.run", return_value=self.leader_result([
                {"sessionId": "chat-2", "cwd": "/work"}])) as run:
            rows = transport.discover(wait=True)
        self.assertEqual([row["session_id"] for row in rows], ["chat-2"])
        self.assertTrue(rows[0]["wake"])
        run.assert_called_once()

    def test_discovery_failure_is_retryable_preflight_not_permanent_failure(self):
        transport, binding, client = self.transport()
        transport.discover = GrokTransport.discover.__get__(transport, GrokTransport)
        with patch("agent_sync.chat_transport.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("leader-list", timeout=27)):
            self.assertEqual(transport.preflight(binding), "busy")
            self.assertTrue(CT._DISCOVERY_DONE.wait(1))
            self.assertEqual(transport.send(binding, "nudge"), "deferred")
        client.request.assert_not_called()

    def test_successful_singleflight_refresh_allows_one_later_send(self):
        transport, binding, client = self.transport()
        transport.discover = GrokTransport.discover.__get__(transport, GrokTransport)
        with patch("agent_sync.chat_transport.subprocess.run", return_value=self.leader_result([
                {"sessionId": "chat-1", "cwd": "/work"}])) as run:
            # The shared background list may finish before or after the first preflight reads;
            # either way, its completed fresh snapshot permits one send.
            readiness = transport.preflight(binding)
            self.assertIn(readiness, ("busy", "ready"))
            self.assertTrue(CT._DISCOVERY_DONE.wait(1))
            with CT._DISCOVERY_LOCK:
                self.assertTrue(CT._DISCOVERY_COMPLETE)
            self.assertEqual(transport.send(binding, "nudge"), "accepted")
        self.assertEqual(run.call_count, 1)
        client.request.assert_called_once()

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
