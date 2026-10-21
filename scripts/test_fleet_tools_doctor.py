"""Tests for scripts/fleet-tools-doctor.py:  python3 scripts/test_fleet_tools_doctor.py"""

from __future__ import annotations

import importlib.util
import json
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
        self.patch = mock.patch.multiple(
            doctor, HOME=self.home, RUNTIME=self.home / "rt", STATE=self.home / "state.json"
        )
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def touch(self, rel: str, text: str = "x") -> None:
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_seat_needs_zulip_linear_and_fleetlink(self):
        names: dict[str, frozenset[str] | None] = {
            "/zulip": frozenset({"OWNER_ACCOUNT_ZULIP_KEY"}),
            "/linear": frozenset(),
            "/fleetlink": None,
        }
        # AFC-359:  the mock must honour `project`, so this test actually asserts
        # check_seat queries AI Fleet Coordinator (not Fleet-OPS) for /fleetlink.
        projects = {
            "/zulip": "fleet-ops",
            "/linear": "fleet-ops",
            "/fleetlink": "ai-fleet-coordinator",
        }

        def fake_names(folder, env, project="fleet-ops"):
            self.assertEqual(project, projects[folder], f"wrong Infisical project for {folder}")
            return names[folder]

        with mock.patch.object(doctor, "infisical_names", fake_names):
            report = doctor.Report()
            doctor.check_seat(report, "GROK-WEB", {})
            self.assertEqual(report.rows[0]["status"], "FAIL")
            self.assertIn("zulip bot, linear app, fleetlink token", report.rows[0]["detail"])
            # AFC-359:  files in the retired credentials folder no longer count.
            self.touch(".secrets/Zulip/Grok-Web-zuliprc")
            report = doctor.Report()
            doctor.check_seat(report, "GROK-WEB", {})
            self.assertIn("zulip bot", report.rows[0]["detail"])
            names["/zulip"] = frozenset(
                {"GROK_WEB_ZULIP_EMAIL", "GROK_WEB_ZULIP_KEY", "GROK_WEB_ZULIP_SITE"}
            )
            names["/linear"] = frozenset(
                {"GROK_WEB_LINEAR_CLIENT_ID", "GROK_WEB_LINEAR_CLIENT_SECRET"}
            )
            report = doctor.Report()
            doctor.check_seat(report, "GROK-WEB", {"FLEETLINK_TOKEN_GROK_WEB": "t"})
            self.assertEqual(report.rows[0]["status"], "OK")
            names["/zulip"] = frozenset(
                {"GROK_BUILD_ZULIP_EMAIL", "GROK_BUILD_ZULIP_KEY", "GROK_BUILD_ZULIP_SITE"}
            )
            report = doctor.Report()
            doctor.check_seat(report, "GROK", {})
            self.assertNotIn("zulip bot", report.rows[0]["detail"])
            names["/zulip"] = None
            report = doctor.Report()
            doctor.check_seat(report, "GROK", {})
            self.assertEqual(report.rows[0]["status"], "WARN")

    def test_mcp_registrations(self):
        self.touch(
            ".codex/config.toml",
            "[mcp_servers.agent-sync]\ncommand = 'x'\n[mcp_servers.fleetlink]\nurl = 'y'\n",
        )
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
        self.assertEqual(
            calls[0][:6],
            ["agent-sync", "post", "--channel", "agent-sync", "--topic", "fleet tools doctor"],
        )
        self.assertIn("seat:CLAUDE: missing fleetlink token", calls[0][6])
        self.assertTrue(doctor.post_changes(doctor.Report(), runner))
        self.assertIn("every check passes again", calls[1][6])

    def test_runtime_reads_the_live_release_not_the_stale_receipt(self):
        self.touch("rt/status.json", json.dumps({"source_commit": "b" * 40, "status": "activated"}))
        self.touch("rt/current/release.json", json.dumps({"source_commit": "a" * 40}))
        self.touch("rt/paused", "paused by operator")

        def runner(argv, **kw):
            if "ls-remote" in argv or "rev-parse" in argv:
                return subprocess.CompletedProcess(argv, 0, "b" * 40 + "\trefs/heads/main", "")
            if "diff" in argv:
                return subprocess.CompletedProcess(
                    argv, 0, "scripts/agent_sync/mcp/listener-tools.json", ""
                )
            return subprocess.CompletedProcess(argv, 1, "", "")

        with mock.patch.object(doctor, "find_repo", return_value=self.home):
            report = doctor.Report()
            doctor.check_runtime(report, True, runner)
        rows = {r["check"]: r for r in report.rows}
        self.assertEqual(rows["runtime:updates"]["status"], "WARN")
        self.assertIn("aaaaaaa", rows["runtime"]["detail"])

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
                    return subprocess.CompletedProcess(
                        argv, 0, "scripts/agent_sync/cli.py\nscripts/linear-board/board", ""
                    )
                return runner(argv, **kw)

            report = doctor.Report()
            doctor.check_runtime(report, True, changed)
            self.assertEqual(report.rows[0]["status"], "FAIL")
            self.assertIn("2 shipped files changed", report.rows[0]["detail"])

    def test_runtime_ignores_tests_and_docs_under_shipped_folders(self):
        self.touch("rt/status.json", json.dumps({"source_commit": "a" * 40, "status": "activated"}))

        def runner(argv, **kw):
            if "diff" in argv:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    "scripts/agent_sync/tests/test_x.py\nscripts/linear-board/README.md",
                    "",
                )
            return subprocess.CompletedProcess(argv, 0, "b" * 40 + "\trefs/heads/main", "")

        with mock.patch.object(doctor, "find_repo", return_value=self.home):
            report = doctor.Report()
            doctor.check_runtime(report, True, runner)
            self.assertEqual(report.rows[0]["status"], "OK")

    def test_runtime_warns_when_the_diff_itself_fails(self):
        self.touch("rt/status.json", json.dumps({"source_commit": "a" * 40, "status": "activated"}))

        def runner(argv, **kw):
            if "diff" in argv:
                return subprocess.CompletedProcess(argv, 128, "", "bad object")
            return subprocess.CompletedProcess(argv, 0, "b" * 40 + "\trefs/heads/main", "")

        with mock.patch.object(doctor, "find_repo", return_value=self.home):
            report = doctor.Report()
            doctor.check_runtime(report, True, runner)
            self.assertEqual(report.rows[0]["status"], "WARN")

    def test_runtime_asks_github_when_there_is_no_checkout(self):
        self.touch("rt/status.json", json.dumps({"source_commit": "a" * 40, "status": "activated"}))

        def runner(argv, **kw):
            return subprocess.CompletedProcess(argv, 0, "a" * 40 + "\trefs/heads/main", "")

        with mock.patch.object(doctor, "find_repo", return_value=None):
            report = doctor.Report()
            doctor.check_runtime(report, True, runner)
            self.assertEqual(report.rows[0]["status"], "OK")

    def test_shipped_files_equal_the_receiver_allowlist(self):
        # The doctor's drift test must not disagree with what the receiver ships (AFC-331).
        rel = importlib.util.spec_from_file_location("release", HERE / "fleet-cli-release.py")
        receiver = importlib.util.module_from_spec(rel)
        rel.loader.exec_module(receiver)
        self.assertEqual(doctor.SHIPPED_EXACT, receiver.EXACT)
        for path in receiver.EXACT:
            self.assertTrue(doctor.is_shipped(path), path)
            self.assertTrue(any(path == p or path.startswith(p) for p in doctor.SHIPPED), path)
        for path in (
            "scripts/fleet_rag/eval.py",
            "scripts/fleet_lanes/tests/test_lane.py",
            "scripts/fleet_rag/tests/x.py",
        ):
            self.assertFalse(doctor.is_shipped(path), path)

    def test_the_same_five_tools_on_both_hosts(self):
        self.assertEqual(
            doctor.FLEET_TOOLS, ("agent-sync", "board", "recall", "lane", "fleet-mode")
        )
        self.assertTrue(set(doctor.FLEET_TOOLS) <= set(doctor.COMMANDS))

    def test_commands_accept_the_macs_apps_folder_but_not_a_missing_tool(self):
        apps = self.home / "apps"
        apps.mkdir()
        (apps / "lane").write_text("#!/bin/sh\n")
        (apps / "lane").chmod(0o755)
        (apps / "fleet-mode").write_text("#!/bin/sh\n")  # not executable
        with (
            mock.patch.object(doctor, "COMMAND_FALLBACK_DIRS", (apps,)),
            mock.patch.object(doctor.shutil, "which", return_value=None),
        ):
            report = doctor.Report()
            doctor.check_commands(report)
        rows = {r["check"]: r for r in report.rows}
        self.assertEqual(rows["command:lane"]["status"], "OK")
        self.assertIn("not on PATH", rows["command:lane"]["detail"])
        self.assertEqual(rows["command:fleet-mode"]["status"], "FAIL")
        self.assertEqual(rows["command:recall"]["status"], "FAIL")

    def test_tool_files_must_ship_with_the_release(self):
        for rel in doctor.TOOL_FILES[:-1]:
            self.touch(f"rt/current/{rel}")
        report = doctor.Report()
        doctor.check_tool_files(report)
        rows = {r["check"]: r["status"] for r in report.rows}
        self.assertEqual(rows["tool-file:scripts/recall"], "OK")
        self.assertEqual(rows["tool-file:docs/FLEET-MODE.md"], "FAIL")

    def test_fleet_mode_doc_warns_when_the_mac_copy_differs(self):
        doc = self.home / "apps/FLEET-MODE.md"
        with (
            mock.patch.object(doctor, "MAC_FLEET_MODE_DOC", doc),
            mock.patch.object(doctor, "MAC_RUNTIME", self.home / "published"),
        ):
            report = doctor.Report()
            doctor.check_fleet_mode_doc(report)
            self.assertEqual(report.rows, [])  # no Mac copy (the server):  nothing to compare
            self.touch("apps/FLEET-MODE.md", "mac\n")
            self.touch("published/docs/FLEET-MODE.md", "repo\n")
            doctor.check_fleet_mode_doc(report)
            self.assertEqual(report.rows[-1]["status"], "WARN")
            self.assertIn("differs", report.rows[-1]["detail"])
            self.touch("apps/FLEET-MODE.md", "repo\n")
            doctor.check_fleet_mode_doc(report)
            self.assertEqual(report.rows[-1]["status"], "OK")

    def test_post_survives_a_missing_agent_sync(self):
        def runner(argv, **kw):
            raise FileNotFoundError("agent-sync")

        report = doctor.Report()
        report.add("seat:CLAUDE", "FAIL", "x")
        self.assertFalse(doctor.post_changes(report, runner))

    def test_codex_toml_quoted_and_subtables(self):
        names = doctor.codex_mcp_names(
            '[mcp_servers."agent-sync"]\ncommand = "x"\n[mcp_servers.fleetlink.env]\nA = "b"\n'
        )
        self.assertEqual(sorted(names), ["agent-sync", "fleetlink"])


class ShippedListTests(unittest.TestCase):
    def test_the_fallback_list_is_the_release_exact_list(self):
        rspec = importlib.util.spec_from_file_location(
            "release_for_doctor", HERE / "fleet-cli-release.py"
        )
        release = importlib.util.module_from_spec(rspec)
        rspec.loader.exec_module(release)
        self.assertEqual(doctor.SHIPPED_EXACT, release.EXACT)
        for path in release.EXACT:
            self.assertTrue(doctor.is_shipped(path), path)


class McpStartupTests(unittest.TestCase):
    def test_a_release_that_cannot_list_its_tools_fails(self):
        def broken(argv, **kw):
            return subprocess.CompletedProcess(
                argv,
                1,
                b"",
                b"agent-sync: internal error: FileNotFoundError: listener-tools.json\n",
            )

        def working(argv, **kw):
            reply = {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"tools": [{"name": "whoami"}, {"name": "post"}]},
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(reply).encode() + b"\n", b"")

        with mock.patch.object(doctor.shutil, "which", return_value="/usr/bin/agent-sync"):
            report = doctor.Report()
            doctor.check_mcp_startup(report, "CLAUDE", broken)
            self.assertEqual(report.rows[0]["status"], "FAIL")
            self.assertIn("FileNotFoundError", report.rows[0]["detail"])
            report = doctor.Report()
            doctor.check_mcp_startup(report, "CLAUDE", working)
            self.assertEqual(
                (report.rows[0]["status"], report.rows[0]["detail"]),
                ("OK", "agent-sync mcp serves 2 tools"),
            )


class RegisteredEnvTests(unittest.TestCase):
    def test_the_probe_reads_each_clients_registered_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".claude.json").write_text(
                json.dumps({"mcpServers": {"agent-sync": {"env": {"ZULIP_RC": "/c"}}}})
            )
            (home / ".codex").mkdir()
            (home / ".codex/config.toml").write_text(
                '[mcp_servers.agent-sync]\ncommand = "x"\n\n[mcp_servers.agent-sync.env]\nZULIP_RC = "/x"\n'
            )
            self.assertEqual(doctor.registered_env("CLAUDE", home), {"ZULIP_RC": "/c"})
            self.assertEqual(doctor.registered_env("CODEX", home), {"ZULIP_RC": "/x"})
            self.assertEqual(doctor.registered_env("CURSOR", home), {})
            self.assertEqual(doctor.registered_env("CLAUDE", home / "missing"), {})
            for bad in (
                [],
                {"mcpServers": []},
                {"mcpServers": {"agent-sync": "x"}},
                {"mcpServers": {"agent-sync": {"env": "x"}}},
            ):
                (home / ".claude.json").write_text(json.dumps(bad))
                self.assertEqual(doctor.registered_env("CLAUDE", home), {})

    def test_the_probe_passes_the_registered_env(self):
        seen = {}

        def runner(argv, **kw):
            seen.update(kw.get("env") or {})
            return subprocess.CompletedProcess(argv, 1, b"", b"x\n")

        with (
            mock.patch.object(doctor, "registered_env", return_value={"ZULIP_RC": "/c"}),
            mock.patch.object(doctor.shutil, "which", return_value="/usr/bin/agent-sync"),
        ):
            doctor.check_mcp_startup(doctor.Report(), "CLAUDE", runner)
        self.assertEqual(seen.get("ZULIP_RC"), "/c")


if __name__ == "__main__":
    unittest.main()
