#!/usr/bin/env python3
"""Unit tests for dsh-acp and cursor_acp_cloud_bridge.  No network, no secrets."""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cursor_acp_cloud_bridge as cacp
dsh_acp = __import__("dsh-acp")


class CursorAcpCloudBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bridge = cacp.AcpBridge(api_key="test-key")

    def test_initialize_returns_auth_methods(self) -> None:
        replies = []
        with patch.object(self.bridge, "reply", side_effect=lambda req_id, res: replies.append(res)):
            self.bridge.handle_initialize(1, {"protocolVersion": 1})
        self.assertEqual(len(replies), 1)
        res = replies[0]
        self.assertIn("authMethods", res)
        self.assertEqual(res["authMethods"], [])

    def test_mirror_conversation_filters_baseline(self) -> None:
        emitted = []
        with patch.object(self.bridge, "emit_text", side_effect=lambda sid, text: emitted.append(text)):
            with patch("cursor_acp_cloud_bridge.get_conversation") as mock_get_convo:
                with patch("cursor_acp_cloud_bridge.get_cloud_agent") as mock_get_agent:
                    mock_get_convo.return_value = {
                        "messages": [
                            {"role": "assistant", "content": "Baseline message 1\n"},
                            {"role": "assistant", "content": "New response for turn 2\n"},
                        ]
                    }
                    mock_get_agent.return_value = {"status": "IDLE"}
                    baseline = {"Baseline message 1\n"}
                    self.bridge._mirror_conversation("sess-1", "agent-1", {}, baseline_texts=baseline)

        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0], "New response for turn 2\n")

    def test_mirror_conversation_emits_repeated_identical_assistant_messages(self) -> None:
        emitted = []
        with patch.object(self.bridge, "emit_text", side_effect=lambda sid, text: emitted.append(text)):
            with patch("cursor_acp_cloud_bridge.get_conversation") as mock_get_convo:
                with patch("cursor_acp_cloud_bridge.get_cloud_agent") as mock_get_agent:
                    mock_get_convo.return_value = {
                        "messages": [
                            {"id": "msg-1", "role": "assistant", "content": "OK\n"},
                            {"id": "msg-2", "role": "assistant", "content": "OK\n"},
                        ]
                    }
                    mock_get_agent.return_value = {"status": "IDLE"}
                    baseline_ids = {"msg-1"}
                    self.bridge._mirror_conversation(
                        "sess-1",
                        "agent-1",
                        {},
                        baseline_ids=baseline_ids,
                        baseline_count=1,
                    )

        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0], "OK\n")

    def test_cancel_session_sets_flag(self) -> None:
        self.bridge.sessions["sess-1"] = {"cwd": "/tmp"}
        self.bridge.handle_session_cancel({"sessionId": "sess-1"})
        self.assertTrue(self.bridge.sessions["sess-1"].get("cancel"))

    def test_handle_session_prompt_resets_cancel_flag(self) -> None:
        self.bridge.sessions["sess-1"] = {"cwd": "/tmp", "cancel": True}
        with patch("cursor_acp_cloud_bridge.create_cloud_agent") as mock_create:
            with patch.object(self.bridge, "_mirror_conversation"):
                with patch.object(self.bridge, "reply"):
                    with patch.object(self.bridge, "emit_text"):
                        mock_create.return_value = {"agent": {"id": "ag-1", "url": "https://cursor.com/agents/ag-1"}}
                        self.bridge.handle_session_prompt(1, {"sessionId": "sess-1", "prompt": [{"type": "text", "text": "hello"}]})

        self.assertFalse(self.bridge.sessions["sess-1"].get("cancel"))
        self.assertIsNotNone(self.bridge.sessions["sess-1"].get("turn_id"))


class DshAcpTests(unittest.TestCase):
    def test_modes_block_only_advertises_agent(self) -> None:
        modes = dsh_acp.modes_block()
        self.assertEqual(modes["currentModeId"], "agent")
        self.assertEqual(len(modes["availableModes"]), 1)
        self.assertEqual(modes["availableModes"][0], {"id": "agent", "name": "Agent"})

    def test_build_dsh_cmd_resumes_harness_session_ids(self) -> None:
        cmd = dsh_acp.build_dsh_cmd("session-abc", "Say OK")
        self.assertIn("--resume", cmd)
        self.assertIn("session-abc", cmd)
        self.assertEqual(cmd[-1], "Say OK")
        new_cmd = dsh_acp.build_dsh_cmd("plain-uuid", "Say OK")
        self.assertNotIn("--resume", new_cmd)

    def test_extract_prompt_text_accepts_content_field(self) -> None:
        text = dsh_acp.extract_prompt_text(
            [{"type": "text", "content": "hello from phone"}]
        )
        self.assertEqual(text, "hello from phone")

    def test_handle_prompt_detaches_stdin_and_process_group(self) -> None:
        import subprocess

        mock_proc = MagicMock()
        mock_proc.stdout = io.StringIO("ok\n")
        mock_proc.stderr = None
        mock_proc.returncode = 0
        mock_proc.pid = 12345
        mock_proc.poll.return_value = 0
        mock_proc.wait.return_value = 0

        replies = []
        with patch.object(dsh_acp, "emit_text"):
            with patch.object(dsh_acp, "emit_thought"):
                with patch.object(dsh_acp, "reply", side_effect=lambda req_id, res: replies.append(res)):
                    with patch("subprocess.Popen", return_value=mock_proc) as popen:
                        dsh_acp.handle_prompt(1, "sess-test", "Say OK", "/tmp")

        kwargs = popen.call_args.kwargs
        self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0], {"stopReason": "endTurn"})

    def test_handle_prompt_watchdog_kills_hanging_proc(self) -> None:
        mock_proc = MagicMock()
        mock_proc.stdout = io.StringIO("")  # empty stdout
        mock_proc.stderr = None
        mock_proc.returncode = 0
        mock_proc.pid = 99
        mock_proc.poll.return_value = 0
        mock_proc.wait.return_value = 0

        replies = []
        emits = []
        with patch.object(dsh_acp, "DEFAULT_TIMEOUT_SEC", 0.1):
            with patch.object(dsh_acp, "emit_text", side_effect=lambda sid, text: emits.append(text)):
                with patch.object(dsh_acp, "emit_thought"):
                    with patch.object(dsh_acp, "reply", side_effect=lambda req_id, res: replies.append(res)):
                        with patch("subprocess.Popen", return_value=mock_proc):
                            dsh_acp.handle_prompt(1, "sess-test", "test prompt", "/tmp")

        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0], {"stopReason": "endTurn"})
        self.assertTrue(any("DeepSeek started" in str(x) for x in emits))


if __name__ == "__main__":
    unittest.main()
