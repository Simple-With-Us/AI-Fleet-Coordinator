#!/usr/bin/env python3
"""Unit tests for leader-client prompt_live receipt correlation."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
LEADER = ROOT / "scripts" / "grok-acp-runtime" / "leader-client.py"


def _load_leader():
    spec = importlib.util.spec_from_file_location("leader_client_prompt_test", LEADER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class PromptLiveReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = _load_leader()

    def _client(self):
        client = self.mod.LeaderStdio.__new__(self.mod.LeaderStdio)
        client._id = 0
        stdin = type("Stdin", (), {"write": lambda self, _data: None, "flush": lambda self: None})()
        client.p = type("Proc", (), {"stdin": stdin})()
        return client

    def test_generic_session_update_is_not_a_receipt(self) -> None:
        client = self._client()
        messages = [
            {"method": "session/update", "params": {"update": {"sessionUpdate": "agent_message_chunk"}}},
            None,
        ]

        def fake_read(_deadline):
            return messages.pop(0) if messages else None

        with patch.object(client, "_read_msg", side_effect=fake_read):
            out = client.prompt_live("sess-1", "wake text", timeout=1.0, wait=False)
        self.assertTrue(out.get("uncertain"))
        self.assertFalse(out.get("queued"))

    def test_correlated_queue_notification_returns_queued(self) -> None:
        client = self._client()
        messages = [
            {
                "method": "_x.ai/queue/changed",
                "params": {"sessionId": "sess-1", "prompt": [{"type": "text", "text": "wake text"}]},
            },
        ]

        def fake_read(_deadline):
            return messages.pop(0) if messages else None

        with patch.object(client, "_read_msg", side_effect=fake_read):
            out = client.prompt_live("sess-1", "wake text", timeout=1.0, wait=False)
        self.assertTrue(out.get("queued"))
        self.assertFalse(out.get("uncertain"))

    def test_json_rpc_response_is_a_receipt(self) -> None:
        client = self._client()
        messages = [
            {"id": 1, "result": {"accepted": True}},
        ]

        def fake_read(_deadline):
            return messages.pop(0) if messages else None

        with patch.object(client, "_next", return_value=1), patch.object(client, "_read_msg", side_effect=fake_read):
            out = client.prompt_live("sess-1", "wake text", timeout=1.0, wait=False)
        self.assertTrue(out.get("queued"))
        self.assertEqual(out.get("result"), {"accepted": True})


if __name__ == "__main__":
    unittest.main()
