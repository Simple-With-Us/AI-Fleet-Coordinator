#!/usr/bin/env python3
"""Unit tests for correlated Grok leader prompt receipts.  No live leader."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
ACP = ROOT / "scripts" / "grok-acp-runtime"
sys.path.insert(0, str(ACP))


def _load_leader():
    path = ACP / "leader-client.py"
    spec = importlib.util.spec_from_file_location("leader_client_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["leader_client_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_disk():
    path = ACP / "session_disk.py"
    spec = importlib.util.spec_from_file_location("session_disk_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["session_disk_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


LEADER = _load_leader()
DISK = _load_disk()


class CorrelatedQueueNotificationTests(unittest.TestCase):
    def test_accepts_matching_session_queue_changed(self):
        msg = {"method": "_x.ai/queue/changed", "params": {"sessionId": "abc"}}
        self.assertTrue(LEADER.correlated_queue_notification(msg, "abc"))

    def test_rejects_other_session(self):
        msg = {"method": "_x.ai/queue/changed", "params": {"sessionId": "other"}}
        self.assertFalse(LEADER.correlated_queue_notification(msg, "abc"))

    def test_rejects_generic_session_update(self):
        msg = {"method": "session/update", "params": {"update": {"sessionUpdate": "agent_message_chunk"}}}
        self.assertFalse(LEADER.correlated_queue_notification(msg, "abc"))


class PromptLiveTests(unittest.TestCase):
    def _client_with_messages(self, messages):
        client = LEADER.LeaderStdio.__new__(LEADER.LeaderStdio)
        client.p = mock.Mock()
        client._id = 0
        client._buf = b""

        def _next():
            client._id += 1
            return client._id

        client._next = _next
        stream = list(messages)

        def _read_msg(_deadline):
            return stream.pop(0) if stream else None

        client._read_msg = _read_msg
        client.p.stdin = mock.Mock()
        return client

    def test_rpc_response_is_correlated_acceptance(self):
        client = self._client_with_messages([
            {"jsonrpc": "2.0", "id": 1, "result": {"queued": True}},
        ])
        out = client.prompt_live("sess-1", "hello", timeout=2.0, wait=False)
        self.assertTrue(out.get("queued"))
        self.assertEqual(out.get("receipt"), "rpc")

    def test_timeout_without_receipt_raises(self):
        client = self._client_with_messages([
            {"method": "session/update", "params": {"update": {"sessionUpdate": "agent_message_chunk"}}},
        ])
        with mock.patch.object(LEADER, "disk_user_receipt", return_value=False):
            with self.assertRaises(TimeoutError):
                client.prompt_live("sess-1", "hello", timeout=0.01, wait=False)

    def test_disk_receipt_after_timeout(self):
        client = self._client_with_messages([])
        with mock.patch.object(LEADER, "disk_user_receipt", return_value=True):
            out = client.prompt_live("sess-1", "hello", timeout=0.01, wait=False)
        self.assertEqual(out.get("receipt"), "disk")
        self.assertTrue(out.get("queued"))


class UserMessageReceiptTests(unittest.TestCase):
    def test_finds_exact_user_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp) / "sessions" / "day" / "sess-1"
            sessions.mkdir(parents=True)
            hist = sessions / "chat_history.jsonl"
            hist.write_text(
                '{"type":"user","content":"ping"}\n'
                '{"type":"user","content":"target text","synthetic_reason":null}\n',
                encoding="utf-8",
            )
            with mock.patch.object(DISK, "SESSIONS_ROOT", Path(tmp) / "sessions"):
                self.assertTrue(DISK.user_message_receipt("sess-1", "target text"))
                self.assertFalse(DISK.user_message_receipt("sess-1", "missing"))


if __name__ == "__main__":
    unittest.main()
