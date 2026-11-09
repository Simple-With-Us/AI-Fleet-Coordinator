#!/usr/bin/env python3
"""Unit tests for grok sessionId flush, NDJSON parse, one-job queue.  No live serve."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SEAT = ROOT / "scripts" / "seat-mcp"
sys.path.insert(0, str(SEAT))

from seat_mcp.jobs import status_view  # noqa: E402
from seat_mcp.seats import SeatError, parse_output, parse_progress_line, plan_spawn  # noqa: E402
from seat_mcp.tools import seat_launch  # noqa: E402


class ParseProgressTests(unittest.TestCase):
    def test_session_line(self) -> None:
        fields = parse_progress_line(
            '{"event":"session","sessionId":"01a05123-aaaa","mcpNames":["github"]}'
        )
        self.assertEqual(fields["sessionId"], "01a05123-aaaa")

    def test_tool_line(self) -> None:
        fields = parse_progress_line('{"event":"tool","lastTool":"run_terminal_command"}')
        self.assertEqual(fields["lastTool"], "run_terminal_command")

    def test_noise(self) -> None:
        self.assertEqual(parse_progress_line("not json"), {})
        self.assertEqual(parse_progress_line(""), {})


class ParseOutputTests(unittest.TestCase):
    def test_ndjson_done(self) -> None:
        blob = "\n".join([
            '{"event":"session","sessionId":"sid-1"}',
            '{"event":"tool","lastTool":"echo"}',
            '{"event":"done","sessionId":"sid-1","text":"pong"}',
        ])
        text, sid = parse_output("grok-json", blob)
        self.assertEqual(text, "pong")
        self.assertEqual(sid, "sid-1")

    def test_legacy_indented_json(self) -> None:
        blob = '{\n  "sessionId": "sid-2",\n  "text": "ok"\n}'
        text, sid = parse_output("grok-json", blob)
        self.assertEqual(text, "ok")
        self.assertEqual(sid, "sid-2")

    def test_empty(self) -> None:
        text, sid = parse_output("grok-json", "")
        self.assertEqual(text, "")
        self.assertIsNone(sid)


class PlanSpawnTimeoutTests(unittest.TestCase):
    @patch("seat_mcp.seats.grok_acp_listening", return_value=True)
    def test_timeout_passed_to_client(self, _listen) -> None:
        rec = {
            "seat": "grok",
            "prompt": "hi",
            "cwd": "/Users/jay/apps",
            "opts": {"timeoutSec": 900, "mcpServers": ["github"]},
            "jobId": "job",
        }
        plan = plan_spawn(rec)
        argv = plan["argv"]
        self.assertEqual(plan["timeoutSec"], 900)
        self.assertIn("--timeout", argv)
        self.assertEqual(argv[argv.index("--timeout") + 1], "900")
        self.assertNotIn("session/load", " ".join(argv))
        self.assertEqual(argv[1], "-u")


class OneJobQueueTests(unittest.TestCase):
    def test_rejects_second_grok_job(self) -> None:
        busy = {"jobId": "abc123", "state": "running", "seat": "grok"}
        with patch("seat_mcp.jobs.running_grok_job", return_value=busy):
            with self.assertRaises(SeatError) as ctx:
                seat_launch({
                    "seat": "grok",
                    "prompt": "second",
                    "cwd": "/Users/jay/apps",
                    "opts": {"mcpServers": ["github"], "timeoutSec": 90},
                })
        self.assertIn("abc123", str(ctx.exception))
        self.assertIn("already running", str(ctx.exception))

    def test_prior_job_does_not_auto_resume(self) -> None:
        prior = {
            "jobId": "old",
            "prompt": "first",
            "text": "done",
            "sessionId": "01a05123-dead",
        }
        with patch("seat_mcp.jobs.running_grok_job", return_value=None), \
             patch("seat_mcp.jobs.load_job", return_value=prior), \
             patch("seat_mcp.jobs.new_record") as new_rec, \
             patch("seat_mcp.tools.launch_async"):
            new_rec.return_value = {"jobId": "new"}
            seat_launch({
                "seat": "grok",
                "prompt": "follow",
                "cwd": "/Users/jay/apps",
                "opts": {"priorJobId": "old", "mcpServers": ["github"]},
            })
            opts = new_rec.call_args.kwargs["opts"]
            self.assertNotIn("sessionId", opts)


class StatusViewTests(unittest.TestCase):
    def test_exposes_session_and_bytes(self) -> None:
        rec = {
            "jobId": "j",
            "seat": "grok",
            "state": "running",
            "sessionId": "sid",
            "lastTool": "git status",
            "gitHeadStart": "aaa",
            "cwd": "/Users/jay/apps",
            "startedAt": "2026-08-30T00:00:00+00:00",
            "heartbeatAt": "2026-08-30T00:00:01+00:00",
            "stats": {"elapsedMs": 12, "bytesOut": 40},
            "partialTail": "x",
        }
        with patch("seat_mcp.jobs.git_head", return_value="aaa"), \
             patch("seat_mcp.jobs.pid_alive", return_value=True):
            view = status_view(rec, 15.0)
        self.assertEqual(view["sessionId"], "sid")
        self.assertEqual(view["bytesOut"], 40)
        self.assertEqual(view["lastTool"], "git status")
        self.assertFalse(view["gitMoved"])
        self.assertIn("elapsedMs", view)


if __name__ == "__main__":
    unittest.main()
