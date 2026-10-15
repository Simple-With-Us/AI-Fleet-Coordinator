#!/usr/bin/env python3
"""Unit tests for Grok TUI drive helpers.  No live model turn."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SEAT = ROOT / "scripts" / "seat-mcp"
LEADER = ROOT / "scripts" / "grok-acp-runtime" / "leader-client.py"
sys.path.insert(0, str(SEAT))

from seat_mcp.auth import access_authenticated, origin_ok  # noqa: E402
from seat_mcp.grok_tui import merge_live  # noqa: E402
from seat_mcp.seats import SeatError, plan_spawn, validate_tui_cwd  # noqa: E402
from seat_mcp.tools import grok_session_prompt, tool_schemas  # noqa: E402


def _load_leader():
    spec = importlib.util.spec_from_file_location("leader_client", LEADER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class OriginTests(unittest.TestCase):
    def test_missing_origin_ok(self) -> None:
        self.assertTrue(origin_ok(""))
        self.assertTrue(origin_ok("null"))

    def test_loopback_ok(self) -> None:
        self.assertTrue(origin_ok("http://127.0.0.1:8793"))
        self.assertFalse(origin_ok("https://evil.example"))

    def test_public_host_ok(self) -> None:
        self.assertTrue(origin_ok("https://agents.jays.services"))

    def test_access_header_skips_origin(self) -> None:
        self.assertTrue(origin_ok("https://cursor.com", access_ok=True))
        self.assertTrue(access_authenticated({"Cf-Access-Jwt-Assertion": "x"}))
        self.assertFalse(access_authenticated({}))


class MergeLiveTests(unittest.TestCase):
    def test_marks_active(self) -> None:
        active = [{
            "sessionId": "abc",
            "cwd": "/Users/jay/Code",
            "pid": 1,
            "openedAt": "t",
            "live": True,
        }]
        with patch("seat_mcp.grok_tui.load_active", return_value=active):
            rows = merge_live([{"sessionId": "abc", "title": "hi", "cwd": "/Users/jay/Code"}])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["live"])
        self.assertEqual(rows[0]["pid"], 1)

    def test_adds_active_missing_from_leader(self) -> None:
        active = [{
            "sessionId": "zzz",
            "cwd": "/Users/jay/apps",
            "pid": 9,
            "openedAt": "t",
            "live": True,
        }]
        with patch("seat_mcp.grok_tui.load_active", return_value=active):
            rows = merge_live([])
        self.assertEqual(rows[0]["sessionId"], "zzz")
        self.assertTrue(rows[0]["live"])


class SchemaTests(unittest.TestCase):
    def test_new_tools_present(self) -> None:
        names = {t["name"] for t in tool_schemas()}
        self.assertIn("grok_sessions_list", names)
        self.assertIn("grok_session_peek", names)
        self.assertIn("grok_session_prompt", names)
        prompt = next(t for t in tool_schemas() if t["name"] == "grok_session_prompt")
        self.assertIn("self", prompt["inputSchema"]["properties"])
        self.assertIn("agents.jays.services", next(
            t for t in tool_schemas() if t["name"] == "grok_sessions_list"
        )["description"])

    @patch("seat_mcp.seats.grok_acp_listening", return_value=True)
    def test_grok_mcp_servers_argv(self, _listen) -> None:
        rec = {
            "seat": "grok",
            "prompt": "hi",
            "cwd": "/Users/jay/apps",
            "opts": {"mcpServers": ["github"]},
            "jobId": "job",
        }
        plan = plan_spawn(rec)
        self.assertIn("--mcp-server", plan["argv"])
        self.assertIn("github", plan["argv"])
        self.assertIn("-u", plan["argv"])
        self.assertIn("--timeout", plan["argv"])
        self.assertEqual(plan["env"].get("PYTHONUNBUFFERED"), "1")

    @patch("seat_mcp.seats.grok_acp_listening", return_value=True)
    def test_grok_empty_mcp_omits_flag(self, _listen) -> None:
        rec = {
            "seat": "grok",
            "prompt": "hi",
            "cwd": "/Users/jay/apps",
            "opts": {},
            "jobId": "job",
        }
        plan = plan_spawn(rec)
        self.assertNotIn("--mcp-server", plan["argv"])

    def test_mcp_servers_rejected_on_tui(self) -> None:
        with self.assertRaises(SeatError):
            plan_spawn({
                "seat": "grok-tui",
                "prompt": "hi",
                "cwd": "/Users/jay/apps",
                "opts": {"sessionId": "sess-1", "mcpServers": ["github"]},
                "jobId": "x",
            })

    def test_grok_tui_requires_session(self) -> None:
        with self.assertRaises(SeatError):
            plan_spawn({
                "seat": "grok-tui",
                "prompt": "hi",
                "cwd": "/Users/jay/apps",
                "opts": {},
                "jobId": "x",
            })

    def test_grok_tui_argv(self) -> None:
        rec = {
            "seat": "grok-tui",
            "prompt": "hello",
            "cwd": "/Users/jay/apps",
            "opts": {"sessionId": "sess-1"},
            "jobId": "job",
        }
        plan = plan_spawn(rec)
        self.assertIn("prompt", plan["argv"])
        self.assertIn("sess-1", plan["argv"])
        self.assertEqual(plan["parse"], "grok-json")
        self.assertNotIn("--self", plan["argv"])

    def test_grok_tui_self_flag(self) -> None:
        rec = {
            "seat": "grok-tui",
            "prompt": "hello",
            "cwd": "/Users/jay/apps",
            "opts": {"sessionId": "sess-1", "self": True},
            "jobId": "job",
        }
        plan = plan_spawn(rec)
        self.assertIn("--self", plan["argv"])

    def test_tui_cwd_under_home(self) -> None:
        self.assertTrue(validate_tui_cwd("/Users/jay/Code").startswith("/Users/jay"))
        with self.assertRaises(SeatError):
            validate_tui_cwd("/tmp")

    def test_prompt_tool_returns_job(self) -> None:
        with patch("seat_mcp.tools.launch_async"):
            out = grok_session_prompt({
                "sessionId": "sess-1",
                "prompt": "ping",
                "cwd": "/Users/jay/apps",
            })
        self.assertIn("jobId", out)


class LeaderExtractTests(unittest.TestCase):
    def test_extract_agent_text(self) -> None:
        mod = _load_leader()
        msg = {
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"text": "hi"},
                }
            },
        }
        self.assertEqual(mod.extract_agent_text(msg), "hi")
        self.assertEqual(mod.extract_agent_text({"method": "ping"}), "")

    def test_peek_from_disk_live_session(self) -> None:
        mod = _load_leader()
        out = mod.peek_from_disk("01a04521-e2e0-7403-9c44-cb8ee340330b")
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out.get("source"), "disk")
        self.assertEqual(out.get("cwd"), "/Users/jay/Code")

    def test_prompt_uses_resume_not_load(self) -> None:
        src = LEADER.read_text(encoding="utf-8")
        self.assertIn("session/resume", src)
        self.assertIn('if args.cmd == "prompt":', src)
        prompt_block = src.split('if args.cmd == "prompt":', 1)[1]
        self.assertIn("_resume", prompt_block)
        self.assertNotIn("session/load", prompt_block.split("return", 1)[0])

    def test_prompt_defaults_to_queued_not_wait(self) -> None:
        src = LEADER.read_text(encoding="utf-8")
        self.assertIn("def prompt_live", src)
        self.assertIn("wait=False", src)
        self.assertIn("--wait", src)
        self.assertIn("_correlated_queue_receipt", src)
        self.assertIn('"uncertain": True', src)


if __name__ == "__main__":
    unittest.main()
