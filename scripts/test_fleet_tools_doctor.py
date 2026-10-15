"""Tests for scripts/fleet-tools-doctor.py:  python3 scripts/test_fleet_tools_doctor.py"""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("doctor", HERE / "fleet-tools-doctor.py")
doctor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doctor)


class SeatAndMcpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.patch = mock.patch.multiple(doctor, HOME=self.home, RUNTIME=self.home / "rt", STATE=self.home / "state.json")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def touch(self, rel: str, text: str = "x") -> None:
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_seat_needs_zulip_linear_and_fleetlink(self):
        report = doctor.Report()
        doctor.check_seat(report, "GROK-WEB", {})
        self.assertEqual(report.rows[0]["status"], "FAIL")
        self.assertIn("zulip rc, linear app, fleetlink token", report.rows[0]["detail"])
        self.touch(".secrets/Zulip/Grok-Web-zuliprc")
        self.touch(".secrets/FleetLink/GROK_WEB.env")
        report = doctor.Report()
        doctor.check_seat(report, "GROK-WEB", {"LINEAR_GROK_WEB_CLIENT_ID": "id", "LINEAR_GROK_WEB_CLIENT_SECRET": "s"})
        self.assertEqual(report.rows[0]["status"], "OK")

    def test_mcp_registrations(self):
        self.touch(".codex/config.toml", "[mcp_servers.agent-sync]\ncommand = 'x'\n[mcp_servers.fleetlink]\nurl = 'y'\n")
        self.touch(".claude.json", json.dumps({"mcpServers": {"agent-sync": {}}}))
        report = doctor.Report()
        doctor.check_mcp(report)
        rows = {r["check"]: r for r in report.rows}
        self.assertEqual(rows["mcp:codex"]["status"], "OK")
        self.assertEqual(rows["mcp:claude"]["status"], "WARN")
        self.assertIn("missing fleetlink", rows["mcp:claude"]["detail"])

    def test_post_only_when_the_failing_set_changes(self):
        calls = []
        def runner(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        report = doctor.Report()
        report.add("seat:CLAUDE", "FAIL", "missing fleetlink token")
        self.assertTrue(doctor.post_changes(report, runner))
        self.assertFalse(doctor.post_changes(report, runner))
        self.assertEqual(calls[0][:6], ["agent-sync", "post", "--channel", "agent-sync", "--topic", "fleet tools doctor"])
        self.assertIn("seat:CLAUDE: missing fleetlink token", calls[0][6])
        self.assertTrue(doctor.post_changes(doctor.Report(), runner))
        self.assertIn("every check passes again", calls[1][6])

    def test_runtime_ok_when_no_shipped_file_changed(self):
        self.touch("rt/status.json", json.dumps({"source_commit": "a" * 40, "status": "activated"}))
        def runner(argv, **kw):
            if "ls-remote" in argv or "rev-parse" in argv:
                return subprocess.CompletedProcess(argv, 0, "b" * 40 + "\trefs/heads/main", "")
            if "diff" in argv:
                return subprocess.CompletedProcess(argv, 0, "", "")
            return subprocess.CompletedProcess(argv, 1, "", "")
        with mock.patch.object(doctor, "find_repo", return_value=self.home):
            report = doctor.Report()
            doctor.check_runtime(report, True, runner)
            self.assertEqual(report.rows[0]["status"], "OK")
            def changed(argv, **kw):
                if "diff" in argv:
                    return subprocess.CompletedProcess(argv, 0, "scripts/agent_sync/cli.py\nscripts/linear-board/board", "")
                return runner(argv, **kw)
            report = doctor.Report()
            doctor.check_runtime(report, True, changed)
            self.assertEqual(report.rows[0]["status"], "FAIL")
            self.assertIn("2 shipped files changed", report.rows[0]["detail"])


if __name__ == "__main__":
    unittest.main()
