"""Tests for scripts/fleet-tools-doctor.py:  python3 scripts/test_fleet_tools_doctor.py"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("doctor", HERE / "fleet-tools-doctor.py")
doctor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(doctor)


class RootSearchPathTests(unittest.TestCase):
    """As root, the doctor never searches a directory another user can write."""

    def test_root_skips_a_directory_not_owned_by_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            planted = Path(tmp) / "bin"
            planted.mkdir()
            fake = mock.Mock(st_uid=1000, st_mode=stat.S_IFDIR | 0o755)
            real_stat = os.stat
            with (
                mock.patch.object(doctor, "TOOL_PATH_DIRS", (str(planted), "/usr/bin")),
                mock.patch.dict(os.environ, {"PATH": ""}),
                mock.patch.object(
                    doctor.os,
                    "stat",
                    side_effect=lambda p, *a, **k: (
                        fake if p == str(planted) else real_stat(p, *a, **k)
                    ),
                ),
            ):
                self.assertNotIn(str(planted), doctor.tool_path_dirs(euid=0))
                # Not root:  the caller's own directories are searched as before.
                self.assertIn(str(planted), doctor.tool_path_dirs(euid=1000))

    def test_root_skips_a_group_or_world_writable_root_directory(self):
        for mode in (0o775, 0o757):
            fake = mock.Mock(st_uid=0, st_mode=stat.S_IFDIR | mode)
            with mock.patch.object(doctor.os, "stat", return_value=fake):
                self.assertFalse(doctor.dir_is_root_safe("/some/dir"))
        safe = mock.Mock(st_uid=0, st_mode=stat.S_IFDIR | 0o755)
        with mock.patch.object(doctor.os, "stat", return_value=safe):
            self.assertTrue(doctor.dir_is_root_safe("/usr/bin"))

    def test_a_missing_directory_is_kept(self):
        self.assertTrue(doctor.dir_is_root_safe("/no/such/dir/for/the/doctor"))


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
            "[mcp_servers.simple-chat]\ncommand = 'x'\n[mcp_servers.fleetlink]\nurl = 'y'\n",
        )
        self.touch(".claude.json", json.dumps({"mcpServers": {"simple-chat": {}}}))
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
            [
                "simple-chat",
                "post",
                "--channel",
                "Work-In-Progress",
                "--topic",
                "fleet tools doctor",
            ],
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
                    argv, 0, "scripts/agent_sync/mcp/eavesdropper-tools.json", ""
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

    def test_the_same_six_tools_on_both_hosts(self):
        self.assertEqual(
            doctor.FLEET_TOOLS,
            ("simple-chat", "simple-notes", "simple-board", "recall", "lane", "fleet-mode"),
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

    def test_elapsed_minutes_do_not_repost_a_standing_failure(self):
        # Sentry 4339724488:  a timer row's "last fired N minutes ago" ticks every run.
        calls = []

        def runner(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        def report_at(minutes):
            report = doctor.Report()
            report.add(
                "updater:fleet-cli-git-update.timer",
                "FAIL",
                f"fleet-cli-git-update.timer last fired {minutes} minutes ago",
            )
            return report

        self.assertTrue(doctor.post_changes(report_at(80), runner))
        self.assertFalse(doctor.post_changes(report_at(140), runner))
        self.assertEqual(len(calls), 1)
        report = report_at(200)
        report.add("tool:gitleaks", "FAIL", "not installed;  required on the server")
        self.assertTrue(doctor.post_changes(report, runner))  # a real change still posts

    def test_post_survives_a_missing_agent_sync(self):
        def runner(argv, **kw):
            raise FileNotFoundError("simple-chat")

        report = doctor.Report()
        report.add("seat:CLAUDE", "FAIL", "x")
        self.assertFalse(doctor.post_changes(report, runner))

    def test_codex_toml_quoted_and_subtables(self):
        names = doctor.codex_mcp_names(
            '[mcp_servers."simple-chat"]\ncommand = "x"\n[mcp_servers.fleetlink.env]\nA = "b"\n'
        )
        self.assertEqual(sorted(names), ["fleetlink", "simple-chat"])


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
                b"simple-chat: internal error: FileNotFoundError: eavesdropper-tools.json\n",
            )

        def working(argv, **kw):
            reply = {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"tools": [{"name": "whoami"}, {"name": "post"}]},
            }
            return subprocess.CompletedProcess(argv, 0, json.dumps(reply).encode() + b"\n", b"")

        with mock.patch.object(doctor.shutil, "which", return_value="/usr/bin/simple-chat"):
            report = doctor.Report()
            doctor.check_mcp_startup(report, "CLAUDE", broken)
            self.assertEqual(report.rows[0]["status"], "FAIL")
            self.assertIn("FileNotFoundError", report.rows[0]["detail"])
            report = doctor.Report()
            doctor.check_mcp_startup(report, "CLAUDE", working)
            self.assertEqual(
                (report.rows[0]["status"], report.rows[0]["detail"]),
                ("OK", "simple-chat mcp serves 2 tools"),
            )


class RegisteredEnvTests(unittest.TestCase):
    def test_the_probe_reads_each_clients_registered_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".claude.json").write_text(
                json.dumps({"mcpServers": {"simple-chat": {"env": {"ZULIP_RC": "/c"}}}})
            )
            (home / ".codex").mkdir()
            (home / ".codex/config.toml").write_text(
                '[mcp_servers.simple-chat]\ncommand = "x"\n\n[mcp_servers.simple-chat.env]\nZULIP_RC = "/x"\n'
            )
            self.assertEqual(doctor.registered_env("CLAUDE", home), {})
            self.assertEqual(doctor.registered_env("CODEX", home), {})
            self.assertEqual(doctor.registered_env("CURSOR", home), {})
            self.assertEqual(doctor.registered_env("CLAUDE", home / "missing"), {})
            for bad in (
                [],
                {"mcpServers": []},
                {"mcpServers": {"simple-chat": "x"}},
                {"mcpServers": {"simple-chat": {"env": "x"}}},
            ):
                (home / ".claude.json").write_text(json.dumps(bad))
                with self.assertRaises(doctor.ClientConfigError):
                    doctor.registered_env("CLAUDE", home)

    def test_codex_config_without_tomllib_falls_back_to_table_headers(self):
        # Python < 3.11 has no tomllib:  names come from the headers, env is never read, nothing fails.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".codex").mkdir()
            (home / ".codex/config.toml").write_text(
                '[mcp_servers.simple-chat]\ncommand = "x"\n\n[mcp_servers.simple-chat.env]\nLANG = "C"\n\n'
                '[mcp_servers."fleetlink"]\ncommand = "y"\n'
            )
            with mock.patch.dict(sys.modules, {"tomllib": None}):
                self.assertEqual(
                    sorted(doctor.client_servers("CODEX", home)), ["fleetlink", "simple-chat"]
                )
                self.assertEqual(doctor.registered_env("CODEX", home), {})
                report = doctor.Report()
                with mock.patch.object(doctor, "HOME", home):
                    doctor.check_mcp(report)
        codex = [row for row in report.rows if row["check"] == "mcp:codex"]
        self.assertEqual(codex[0]["status"], "OK")

    def test_client_secrets_and_lookup_redirects_are_never_forwarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".claude.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "simple-chat": {
                                "env": {
                                    "INFISICAL_TOKEN": "synthetic-file-token",
                                    "ZULIP_KEY": "synthetic-key",
                                    "ZULIP_RC": "/synthetic/alternate",
                                    "AGENT_SEAT": "OTHER",
                                    "LANG": "C",
                                }
                            }
                        }
                    }
                )
            )
            self.assertEqual(doctor.registered_env("CLAUDE", home), {"LANG": "C"})

    def test_malformed_source_fails_without_starting_probe_or_leaking_contents(self):
        runner = mock.Mock()
        with (
            mock.patch.object(
                doctor,
                "registered_env",
                side_effect=doctor.ClientConfigError("Claude client JSON unavailable or malformed"),
            ),
            mock.patch.object(doctor.shutil, "which", return_value="simple-chat"),
        ):
            report = doctor.Report()
            doctor.check_mcp_startup(report, "CLAUDE", runner)
        runner.assert_not_called()
        self.assertEqual(report.rows[0]["status"], "FAIL")
        self.assertIn("Claude client JSON", report.rows[0]["detail"])

    def test_the_probe_passes_the_registered_env(self):
        seen = {}

        def runner(argv, **kw):
            seen.update(kw.get("env") or {})
            return subprocess.CompletedProcess(argv, 1, b"", b"x\n")

        with (
            mock.patch.object(doctor, "registered_env", return_value={"LANG": "file-locale"}),
            mock.patch.object(doctor.shutil, "which", return_value="/usr/bin/simple-chat"),
            mock.patch.dict(
                os.environ, {"LANG": "process-locale", "INFISICAL_TOKEN": "synthetic-process-token"}
            ),
        ):
            doctor.check_mcp_startup(doctor.Report(), "CLAUDE", runner)
        self.assertEqual(seen.get("LANG"), "process-locale")
        self.assertEqual(seen.get("INFISICAL_TOKEN"), "synthetic-process-token")


# --- AFC-461:  per-host install and currency -----------------------------------------------------

TIP = "c" * 40
OLD = "a" * 40


def fake_runner(rules, calls=None):
    """A subprocess.run stand-in:  the first rule whose words all appear in argv answers.  An answer is
    (returncode, stdout[, stderr]) or an exception to raise."""

    def runner(argv, **kw):
        if calls is not None:
            calls.append(list(argv))
        for words, answer in rules:
            if all(w in argv for w in words):
                if isinstance(answer, BaseException):
                    raise answer
                err = answer[2] if len(answer) > 2 else ""
                return subprocess.CompletedProcess(argv, answer[0], answer[1], err)
        return subprocess.CompletedProcess(argv, 1, "", "no rule")

    return runner


class HomeCase(unittest.TestCase):
    """A temporary HOME with the doctor's paths pointed into it.  PATH is emptied so the registry
    finds only the fake tools in HOME/bin;  a case that runs real git keeps it."""

    BLANK_PATH = True

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.rt_dir = self.home / "apps/lanes/_managed/fleet/agent-sync-runtime"
        self.patch = mock.patch.multiple(
            doctor,
            HOME=self.home,
            RUNTIME=self.home / ".local/share/fleet-cli",
            STATE=self.home / "state.json",
            MAC_RUNTIME=self.rt_dir,
            MAC_FLEET_MODE_DOC=self.home / "apps/FLEET-MODE.md",
            COMMAND_FALLBACK_DIRS=(self.home / "apps",),
            TOOL_PATH_DIRS=(str(self.home / "bin"),),
        )
        self.patch.start()
        self.env = mock.patch.dict(os.environ, {"PATH": ""} if self.BLANK_PATH else {})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.patch.stop()
        self.tmp.cleanup()

    def touch(self, rel, text="x", mode=None):
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        if mode is not None:
            path.chmod(mode)
        return path


class VersionTests(unittest.TestCase):
    def test_first_version_token(self):
        cases = {
            "2.1.303 (Claude Code)": "2.1.303",
            "codex-cli 0.163.0": "0.163.0",
            "v26.11.1": "26.11.1",
            "jq-1.7.1-apple": "1.7.1",
            "0.1.5-rc.3": "0.1.5-rc.3",
            "GitHub Copilot CLI 1.0.101.\nRun 'copilot update' to check for updates.": "1.0.101",
            "cloudflared version 2026.10.0 (built 2026-10-05T15:39:13Z)": "2026.10.0",
            "1.4.11-R6667.1": "1.4.11",
            "gh version 2.102.0 (2026-09-30)\nhttps://github.com/cli/cli/releases/tag/v2.102.0": "2.102.0",
            "Docker version 29.8.2, build 7fc2dff": "29.8.2",
        }
        for text, want in cases.items():
            self.assertEqual(doctor.parse_version(text)["text"], want, text)
        for text in ("no version here", "build 7", "", None):
            self.assertIsNone(doctor.parse_version(text), text)

    def test_compare(self):
        v = doctor.parse_version
        self.assertEqual(doctor.compare_versions(v("2.1.303"), v("2.1.306")), -1)
        self.assertEqual(doctor.compare_versions(v("26.12.0"), v("26.11.1")), 1)
        self.assertEqual(doctor.compare_versions(v("1.0"), v("1.0.0")), 0)
        self.assertEqual(doctor.compare_versions(v("0.1.5-rc.3"), v("0.1.5")), -1)
        self.assertEqual(doctor.compare_versions(v("1.4.11-R6667.1"), v("1.4.11")), 0)


class RegistryRowTests(unittest.TestCase):
    TOOL = {
        "name": "claude",
        "kind": "platform-cli",
        "mac": "required",
        "server": "optional",
        "npm": "x",
    }

    def row(self, host="mac", path="/bin/claude", probe=None, latest=None):
        return doctor.registry_row(self.TOOL, host, path, probe, latest)

    def probe(self, text):
        return {"version": doctor.parse_version(text), "raw": text, "error": None}

    def test_absent_optional_is_informational_but_missing_required_fails(self):
        row = self.row(host="server", path=None)
        self.assertEqual((row["state"], row["status"]), ("absent", "OK"))
        row = self.row(host="mac", path=None)
        self.assertEqual((row["state"], row["status"]), ("missing", "FAIL"))
        self.assertTrue(row["required"])

    def test_probe_failures_are_unknown_never_a_failure(self):
        row = self.row(
            probe={
                "version": None,
                "raw": None,
                "error": "--version did not answer within 5 seconds",
            }
        )
        self.assertEqual((row["state"], row["status"]), ("unknown", "WARN"))
        row = self.row(probe={"version": None, "raw": "garbage", "error": None})
        self.assertEqual((row["state"], row["status"]), ("unknown", "WARN"))
        self.assertIn("not recognised", row["detail"])

    def test_latest_compare(self):
        latest = {"source": "npm", "version": doctor.parse_version("2.1.306"), "error": None}
        row = self.row(probe=self.probe("2.1.303"), latest=latest)
        self.assertEqual((row["state"], row["status"], row["latest"]), ("stale", "WARN", "2.1.306"))
        self.assertIn("behind npm's latest 2.1.306", row["detail"])
        row = self.row(probe=self.probe("2.1.306"), latest=latest)
        self.assertEqual((row["state"], row["basis"]), ("current", "latest"))
        row = self.row(probe=self.probe("2.2.0"), latest=latest)
        self.assertEqual(row["state"], "current")
        self.assertIn("ahead of", row["detail"])

    def test_policy_is_one_constant(self):
        latest = {"source": "npm", "version": doctor.parse_version("2.1.306"), "error": None}
        with mock.patch.object(doctor, "VENDOR_CURRENCY_POLICY", "skew"):
            row = self.row(probe=self.probe("2.1.303"), latest=latest)
        self.assertEqual((row["state"], row["basis"]), ("current", "skew"))
        self.assertIn("behind npm's latest", row["reason"])

    def test_no_source_is_presence_only_and_a_source_not_read_is_unknown(self):
        row = self.row(probe=self.probe("1.0.0"), latest=None)
        self.assertEqual((row["state"], row["basis"], row["latest"]), ("current", "presence", None))
        row = self.row(
            probe=self.probe("1.0.0"),
            latest={"source": "npm", "version": None, "error": "not read (--no-network)"},
        )
        self.assertEqual((row["state"], row["latest"]), ("unknown", None))


class RegistryCheckTests(HomeCase):
    def setUp(self):
        super().setUp()
        for name in ("claude", "gitleaks", "muse", "fx", "npm"):
            self.touch(f"bin/{name}", "#!/bin/sh\n", 0o755)
        self.registry = (
            {
                "name": "claude",
                "kind": "platform-cli",
                "mac": "required",
                "npm": "@anthropic-ai/claude-code",
            },
            {
                "name": "gitleaks",
                "kind": "dev-tool",
                "mac": "required",
                "github": "gitleaks/gitleaks",
            },
            {
                "name": "muse",
                "kind": "platform-cli",
                "mac": "required",
                "version_file": ".muse-version",
            },
            {"name": "fx", "kind": "platform-cli", "mac": "required"},
            {"name": "droid", "kind": "platform-cli", "mac": "optional"},
            {"name": "docker", "kind": "dev-tool", "server": "required"},
            {"name": "npm", "kind": "dev-tool", "mac": "optional", "npm": "npm"},
        )
        self.touch(".muse-version", "1.4.11-R6667.1\n")

    def run_registry(self, network, rules, fetch=None):
        calls = []
        runner = fake_runner(rules, calls)
        report = doctor.Report()
        with mock.patch.object(doctor, "TOOL_REGISTRY", self.registry):
            doctor.check_registry(
                report,
                "mac",
                network,
                runner,
                fetch or (lambda url, timeout=8: b'{"tag_name": "v8.30.1"}'),
            )
        return {r["check"]: r for r in report.rows}, calls

    def test_no_network_reads_no_latest_and_never_calls_a_source_row_current(self):
        rows, calls = self.run_registry(
            False,
            [(("--version",), (0, "2.1.306 (Claude Code)\n")), (("view",), (0, "9.9.9\n"))],
            fetch=lambda url, timeout=8: self.fail("GitHub read with --no-network"),
        )
        self.assertFalse([c for c in calls if "view" in c])
        for name in ("claude", "gitleaks"):
            self.assertIsNone(rows[f"tool:{name}"]["latest"])
            self.assertEqual(rows[f"tool:{name}"]["state"], "unknown", name)
        self.assertEqual(rows["tool:fx"]["state"], "current")  # no latest source:  presence only
        self.assertEqual(rows["tool:droid"]["state"], "absent")
        self.assertNotIn("tool:docker", rows)  # a server-only tool is not checked on the Mac

    def test_network_reads_npm_and_github_and_the_muse_file_without_running_muse(self):
        rows, calls = self.run_registry(
            True,
            [
                (("view", "@anthropic-ai/claude-code"), (0, "2.1.306\n")),
                (("--version",), (0, "2.1.303 (Claude Code)\n")),
            ],
        )
        self.assertEqual(rows["tool:claude"]["state"], "stale")
        self.assertEqual(rows["tool:claude"]["latest"], "2.1.306")
        self.assertEqual(rows["tool:muse"]["version"], "1.4.11")
        self.assertFalse([c for c in calls if c and c[0].endswith("/muse")])
        self.assertEqual(rows["tool:gitleaks"]["latestSource"], "github")

    def test_one_tools_unexpected_error_does_not_hide_the_others(self):
        def answer(argv, **kw):
            if Path(argv[0]).name == "fx":
                raise RuntimeError("boom")
            return subprocess.CompletedProcess(argv, 0, "1.0.0\n", "")

        report = doctor.Report()
        with mock.patch.object(doctor, "TOOL_REGISTRY", self.registry):
            doctor.check_registry(report, "mac", False, answer)
        self.assertEqual(report.row("tool:fx")["state"], "unknown")
        self.assertIn("RuntimeError", report.row("tool:fx")["detail"])
        self.assertEqual(report.row("tool:muse")["state"], "current")

    def test_a_probe_timeout_is_unknown(self):
        rows, _ = self.run_registry(
            False, [(("--version",), subprocess.TimeoutExpired(["x", "--version"], 5))]
        )
        self.assertEqual(rows["tool:fx"]["state"], "unknown")
        self.assertEqual(rows["tool:fx"]["status"], "WARN")
        self.assertIn("did not answer", rows["tool:fx"]["detail"])

    def test_a_probe_or_npm_lookup_that_timed_out_gets_one_more_try_alone(self):
        tries: dict = {}

        def answer(argv, **kw):
            key = "view" if "view" in argv else Path(argv[0]).name
            tries[key] = tries.get(key, 0) + 1
            if tries[key] == 1:
                raise subprocess.TimeoutExpired(argv, 5)
            out = "2.1.306\n" if key == "view" else "2.1.306 (Claude Code)\n"
            return subprocess.CompletedProcess(argv, 0, out, "")

        report = doctor.Report()
        with mock.patch.object(doctor, "TOOL_REGISTRY", self.registry[:1]):
            doctor.check_registry(report, "mac", True, answer)
        row = report.row("tool:claude")
        self.assertEqual(
            (row["state"], row["version"], row["latest"]), ("current", "2.1.306", "2.1.306")
        )
        self.assertEqual(tries, {"claude": 2, "view": 2})
        self.assertEqual(doctor.REGISTRY_WORKERS, 4)

    def test_brew_is_the_source_for_a_cellar_install(self):
        cellar = self.home / "opt/homebrew/Cellar/gitleaks/8.29.0/bin"
        cellar.mkdir(parents=True)
        (cellar / "gitleaks").write_text("#!/bin/sh\n")
        (cellar / "gitleaks").chmod(0o755)
        (self.home / "bin/gitleaks").unlink()
        (self.home / "bin/gitleaks").symlink_to(cellar / "gitleaks")
        self.touch("bin/brew", "#!/bin/sh\n", 0o755)
        brew = json.dumps({"formulae": [{"name": "gitleaks", "versions": {"stable": "8.30.1"}}]})
        rows, calls = self.run_registry(
            True,
            [
                (("info", "--json=v2"), (0, brew)),
                (("--version",), (0, "gitleaks version 8.29.0\n")),
            ],
            fetch=lambda url, timeout=8: self.fail("GitHub read for a brew install"),
        )
        self.assertEqual(rows["tool:gitleaks"]["latestSource"], "brew")
        self.assertEqual(rows["tool:gitleaks"]["state"], "stale")


class SkewTests(unittest.TestCase):
    def doc(self, host, version, state="current", tool="claude"):
        return {
            "host": host,
            "reachable": True,
            "checks": [
                {
                    "check": f"tool:{tool}",
                    "tool": tool,
                    "category": "platform-cli",
                    "version": version,
                    "state": state,
                    "status": "OK",
                    "detail": "d",
                }
            ],
        }

    def test_the_older_host_is_stale(self):
        mac, server = self.doc("mac", "2.1.300"), self.doc("server", "2.1.306")
        doctor.apply_skew([mac, server])
        self.assertEqual(mac["checks"][0]["state"], "stale")
        self.assertIn("older than the server", mac["checks"][0]["reason"])
        self.assertEqual(server["checks"][0]["state"], "current")

    def test_no_skew_without_both_hosts_or_for_os_tools(self):
        mac, server = self.doc("mac", "2.1.300"), self.doc("server", "2.1.306")
        server["reachable"] = False
        doctor.apply_skew([mac, server])
        self.assertEqual(mac["checks"][0]["state"], "current")
        mac, server = (
            self.doc("mac", "3.15.0", tool="python3"),
            self.doc("server", "3.12.3", tool="python3"),
        )
        doctor.apply_skew([mac, server])
        self.assertEqual(server["checks"][0]["state"], "current")


class TipTests(unittest.TestCase):
    def test_tip_comes_from_github_or_not_at_all(self):
        self.assertIsNone(doctor.read_tip(False)["rev"])
        good = fake_runner([(("ls-remote",), (0, TIP + "\trefs/heads/main\n"))])
        self.assertEqual(doctor.read_tip(True, good)["rev"], TIP)
        bad = fake_runner([(("ls-remote",), (128, "", "fatal"))])
        tip = doctor.read_tip(True, bad)
        self.assertIsNone(tip["rev"])
        self.assertIn("exit 128", tip["error"])


class MacRuntimeTests(HomeCase):
    HEALTHY = {"loaded": True, "runs": 10, "lastExit": "0", "paused": False, "advanced": True}

    def runtime(
        self, head=OLD, tip=TIP, present=True, tracked="", updater=None, previous=None, error=None
    ):
        runner = fake_runner(
            [
                (("rev-parse", "HEAD"), (0, head + "\n")),
                (("cat-file",), (0 if present else 1, "")),
                (("rev-list",), (0, "3\n")),
                (("status",), (0, tracked)),
            ]
        )
        report = doctor.Report()
        rt = doctor.check_mac_runtime(
            report, runner, {"rev": tip, "error": error}, dict(updater or self.HEALTHY), previous
        )
        return rt, report.row("runtime")

    def test_current_only_at_the_tip(self):
        rt, row = self.runtime(head=TIP)
        self.assertEqual((row["state"], row["status"]), ("current", "OK"))

    def test_behind_counts_when_the_tip_is_here_and_is_null_when_not(self):
        _, row = self.runtime()
        self.assertEqual((row["state"], row["behindCommits"], row["status"]), ("stale", 3, "WARN"))
        self.assertTrue(row["grace"])
        _, row = self.runtime(present=False)
        self.assertEqual((row["state"], row["behindCommits"]), ("stale", None))
        self.assertIn("count unknown", row["detail"])

    def test_never_current_without_a_remote_read(self):
        # The runtime HEAD equals the local origin/main ref, but GitHub was not read:  unknown, not OK.
        _, row = self.runtime(head=TIP, tip=None, error="not read (--no-network)")
        self.assertEqual((row["state"], row["status"]), ("unknown", "WARN"))

    def test_reasons(self):
        self.touch(".agent-sync/runtime-sync.pause", "")
        _, row = self.runtime(updater=dict(self.HEALTHY, paused=True))
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("paused", row["reason"])
        (self.home / ".agent-sync/runtime-sync.pause").unlink()
        self.touch(".agent-sync/runtime-sync.bad-sha", TIP + "\n")
        _, row = self.runtime(updater=dict(self.HEALTHY, advanced=None))
        self.assertEqual(row["status"], "WARN")  # amber:  the updater waits for main to move
        self.assertIn("smoke test", row["reason"])
        (self.home / ".agent-sync/runtime-sync.bad-sha").unlink()
        _, row = self.runtime(tracked=" M scripts/simple-chat\n")
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("tracked changes", row["reason"])
        _, row = self.runtime(updater=dict(self.HEALTHY, advanced=False))
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("run counter", row["reason"])
        _, row = self.runtime(updater=dict(self.HEALTHY, loaded=False))
        self.assertEqual(row["status"], "FAIL")

    def test_behind_the_same_tip_twice_is_red(self):
        previous = {
            "hosts": [
                {
                    "host": "mac",
                    "checks": [
                        {"check": "runtime", "state": "stale", "installed": OLD, "expectedRev": TIP}
                    ],
                }
            ]
        }
        _, row = self.runtime(previous=previous)
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("same tip", row["reason"])
        # A previous report too recent to judge the counter is no evidence of a stuck updater.
        _, row = self.runtime(previous=previous, updater=dict(self.HEALTHY, advanced=None))
        self.assertEqual((row["state"], row["status"], row["grace"]), ("stale", "WARN", None))

    def test_a_first_run_behind_is_amber_without_claiming_grace(self):
        # Sentry 4339724496:  no previous report (advanced is None) must not be red.
        _, row = self.runtime(updater=dict(self.HEALTHY, advanced=None))
        self.assertEqual((row["state"], row["status"], row["grace"]), ("stale", "WARN", None))
        self.assertIn("no previous report", row["reason"])


class MacUpdaterTests(HomeCase):
    PRINT = "gui/501/com.jay.agent-sync-runtime-sync = {\n\tactive count = 0\n\tstate = not running\n\truns = 413\n\tlast exit code = 0\n\tevent triggers = {\n\t\tstate = active\n\t}\n}\n"

    def previous(self, runs, minutes_ago):
        return {
            "generatedAt": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - minutes_ago * 60)
            ),
            "hosts": [{"host": "mac", "updater": {"runs": runs}}],
        }

    def updater(self, previous=None, answer=None):
        report = doctor.Report()
        runner = fake_runner([(("launchctl", "print"), answer or (0, self.PRINT))])
        info = doctor.check_mac_updater(report, runner, previous)
        return info, report.row("updater:com.jay.agent-sync-runtime-sync")

    def test_counter(self):
        info, row = self.updater(self.previous(400, 15))
        self.assertEqual((info["runs"], info["advanced"], row["state"]), (413, True, "current"))
        info, row = self.updater(self.previous(413, 15))
        self.assertEqual((info["advanced"], row["state"], row["status"]), (False, "stale", "FAIL"))
        info, row = self.updater(self.previous(413, 1))  # too soon to judge
        self.assertEqual((info["advanced"], row["state"]), (None, "unknown"))
        info, row = self.updater(None)
        self.assertEqual(row["state"], "unknown")

    def test_not_loaded_is_missing(self):
        _, row = self.updater(answer=(113, "", "Could not find service"))
        self.assertEqual((row["state"], row["status"]), ("missing", "FAIL"))


def git_repo(root: Path, commits):
    """A real repository:  `commits` is a list of {path: text} applied in order;  returns their shas."""

    def g(*args):
        return subprocess.run(
            ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        ).stdout.strip()

    root.mkdir(parents=True, exist_ok=True)
    g("init", "-q", "-b", "main")
    shas = []
    for files in commits:
        for rel, text in files.items():
            path = root / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        g("add", "-A")
        g("commit", "-q", "-m", "c")
        shas.append(g("rev-parse", "HEAD"))
    return shas


class CopyTests(HomeCase):
    BLANK_PATH = False

    @classmethod
    def setUpClass(cls):
        cls.repo_tmp = tempfile.TemporaryDirectory()
        cls.repo = Path(cls.repo_tmp.name) / "repo"
        cls.shas = git_repo(
            cls.repo,
            [
                {
                    "scripts/recall": "r1",
                    "scripts/fleet_rag/core.py": "v1",
                    "scripts/fleet_rag/tests/test_x.py": "t",
                },
                {"scripts/fleet_rag/core.py": "v2"},
                {"scripts/fleet_rag/core.py": "v3", "scripts/other.py": "o"},
            ],
        )
        cls.tip = cls.shas[-1]

    @classmethod
    def tearDownClass(cls):
        cls.repo_tmp.cleanup()

    def install(self, core="v3", recall="r1"):
        if recall is not None:
            self.touch("apps/fleet-rag/recall", recall)
        self.touch("apps/fleet-rag/fleet_rag/core.py", core)

    def compare(self, tip="tip", head=None):
        rt = {"tip": self.tip if tip == "tip" else tip, "head": head or self.tip}
        return doctor.compare_copy("recall", doctor.COPY_SETS["recall"], self.repo, rt)

    def test_same_blob_is_current_and_tests_are_not_expected(self):
        self.install()
        row = self.compare()
        self.assertEqual((row["state"], row["status"]), ("current", "OK"), row)

    def test_an_older_blob_is_stale_with_the_later_commits(self):
        self.install(core="v1")
        row = self.compare()
        self.assertEqual((row["state"], row["status"], row["behindCommits"]), ("stale", "FAIL", 2))
        self.assertEqual(row["installed"], self.shas[0][:7])

    def test_a_blob_main_never_had_is_a_local_edit(self):
        self.install(core="mine")
        row = self.compare()
        self.assertEqual(
            (row["state"], row["installed"], row["behindCommits"]), ("stale", "local edit", None)
        )

    def test_a_missing_entry_is_missing_and_an_extra_file_is_stale(self):
        self.install(recall=None)
        self.assertEqual(self.compare()["state"], "missing")
        self.install()
        self.touch("apps/fleet-rag/fleet_rag/old.py", "gone")
        row = self.compare()
        self.assertEqual(row["state"], "stale")
        self.assertIn("extra", row["detail"])

    def test_equal_to_the_runtime_without_the_tip_is_unknown(self):
        self.install()
        row = self.compare(tip=None)
        self.assertEqual((row["state"], row["status"]), ("unknown", "WARN"))


class LaneTests(HomeCase):
    PLAN = (
        "plan (read-only)  home=/h  stable=/h/apps/lane-tools  source=/s  sha={sha}\n\n"
        "== tools  stable dir /h/apps/lane-tools  [{state}]\n{lines}"
        "   25 file(s) already identical; the whole dir is replaced atomically (stage, probe, swap)\n"
        "   shim /h/apps/lane [same]:\n"
    )

    def setUp(self):
        super().setUp()
        self.touch(
            "apps/lanes/_managed/fleet/agent-sync-runtime/scripts/fleet_lanes/install_tools.py"
        )
        self.touch("apps/lane-tools/VERSION", f"sha={OLD}\npackage=0.1.0\n")

    def lane(self, state, lines="", rc=0, rt_state="current", text=None):
        out = text if text is not None else self.PLAN.format(sha=TIP, state=state, lines=lines)
        runner = fake_runner(
            [(("plan", "tools"), (rc, out)), (("cat-file",), (0, "")), (("rev-list",), (0, "27\n"))]
        )
        report = doctor.Report()
        rt = {
            "state": rt_state,
            "status": doctor.STATUS_FOR_STATE[rt_state],
            "tip": TIP,
            "head": TIP,
        }
        doctor.check_lane_tools(report, rt, runner)
        return report.row("tool:lane")

    def test_plan_states(self):
        row = self.lane("stale", "   changed  VERSION\n   changed  fleet_lanes/lane.py\n")
        self.assertEqual((row["state"], row["status"], row["behindCommits"]), ("stale", "FAIL", 27))
        self.assertEqual(row["installed"], OLD[:7])
        self.assertEqual(self.lane("current")["state"], "current")
        self.assertEqual(self.lane("current", rt_state="unknown")["state"], "unknown")
        self.assertEqual(self.lane("missing")["state"], "missing")

    def test_only_version_changed_is_as_current_as_the_runtime(self):
        self.assertEqual(self.lane("stale", "   changed  VERSION\n")["state"], "current")

    def test_unrecognised_text_or_an_exit_is_unknown(self):
        self.assertEqual(self.lane("", text="something else entirely\n")["state"], "unknown")
        self.assertEqual(self.lane("current", rc=2)["state"], "unknown")
        self.assertEqual(self.lane("foreign")["state"], "unknown")


class ServerTests(HomeCase):
    def setUp(self):
        super().setUp()
        self.fleet_cli = self.home / ".local/share/fleet-cli"
        (self.fleet_cli / "repo/.git").mkdir(parents=True)
        (self.fleet_cli / "current").symlink_to(self.fleet_cli / "repo")

    def timer(self, active="active", minutes=10, load="loaded"):
        last = f"@{int(time.time()) - minutes * 60}" if minutes is not None else "n/a"
        return (
            0,
            f"LoadState={load}\nActiveState={active}\nLastTriggerUSec={last}\nNextElapseUSecRealtime=\n",
        )

    def runtime(self, timer):
        runner = fake_runner(
            [
                (("rev-parse", "HEAD"), (0, OLD + "\n")),
                (("cat-file",), (0, "")),
                (("rev-list",), (0, "2\n")),
                (("systemctl",), timer),
            ]
        )
        report = doctor.Report()
        doctor.check_runtime(report, True, runner, {"rev": TIP})
        return report.row("runtime")

    def test_grace_window(self):
        row = self.runtime(self.timer(minutes=10))
        self.assertEqual((row["state"], row["status"], row["behindCommits"]), ("stale", "WARN", 2))
        row = self.runtime(self.timer(minutes=200))
        self.assertEqual(row["status"], "FAIL")
        row = self.runtime(self.timer(active="inactive"))
        self.assertEqual(row["status"], "FAIL")
        (self.fleet_cli / "git-state.json").write_text(
            json.dumps({"bad": TIP, "bad_reason": "smoke"})
        )
        row = self.runtime(self.timer(minutes=10))
        self.assertEqual(row["status"], "FAIL")
        self.assertIn("smoke test", row["reason"])

    def test_current_must_point_at_repo(self):
        (self.fleet_cli / "current").unlink()
        (self.fleet_cli / "old").mkdir()
        (self.fleet_cli / "current").symlink_to(self.fleet_cli / "old")
        row = self.runtime(self.timer())
        self.assertEqual((row["state"], row["status"]), ("stale", "FAIL"))

    def test_timer_rows(self):
        for answer, want in (
            (self.timer(active="inactive"), ("stale", "FAIL")),
            (self.timer(load="not-found", active="inactive"), ("missing", "FAIL")),
            (self.timer(minutes=5), ("current", "OK")),
            (self.timer(minutes=500), ("stale", "FAIL")),
        ):
            report = doctor.Report()
            doctor.check_server_timers(report, fake_runner([(("systemctl",), answer)]))
            row = report.row("updater:fleet-cli-git-update.timer")
            self.assertEqual((row["state"], row["status"]), want, answer)

    def test_service_run_newer_than_the_timer_counts_as_a_run(self):
        now = int(time.time())
        out = (
            "LoadState=loaded\nActiveState=active\nLastTriggerUSec=@%d\n"
            "NextElapseUSecRealtime=\nExecMainStartTimestamp=\n\n"
            "LoadState=loaded\nActiveState=inactive\nLastTriggerUSec=\n"
            "NextElapseUSecRealtime=\nExecMainStartTimestamp=@%d\n"
        ) % (now - 300 * 60, now - 20 * 60)
        report = doctor.Report()
        doctor.check_server_timers(report, fake_runner([(("systemctl",), (0, out))]))
        row = report.row("updater:fleet-cli-git-update.timer")
        self.assertEqual((row["state"], row["status"]), ("current", "OK"))
        self.assertIn("fired 20 minutes ago", row["reason"])

    def test_wrappers_and_the_fleet_secrets_link(self):
        current = str(self.fleet_cli / "current")
        self.touch(
            ".local/bin/simple-chat",
            f"release = Path({current!r})\nargv = [str(release / 'scripts/simple-chat')]\n",
        )
        self.touch(".local/bin/simple-board", "exec /usr/bin/python3 /somewhere/else/board\n")
        self.touch("elsewhere/fleet-secrets", "#!/bin/sh\n")
        (self.home / ".local/bin/fleet-secrets").symlink_to(self.home / "elsewhere/fleet-secrets")
        report = doctor.Report()
        rt = {"state": "current", "status": "OK", "head": TIP, "tip": TIP}
        doctor.check_server_tools(report, rt, {"FleetLink": "current"}, fake_runner([]))
        self.assertEqual(report.row("tool:simple-chat")["state"], "current")
        self.assertEqual(report.row("tool:simple-board")["state"], "missing")  # points elsewhere
        self.assertEqual(report.row("tool:recall")["state"], "missing")  # absent
        self.assertEqual(report.row("tool:fleet-secrets")["state"], "stale")  # wrong link
        self.assertEqual(report.row("tool:fleetlink")["state"], "missing")


class RemoteTests(unittest.TestCase):
    CONFIGURED = (("ssh", "-G"), (0, "user root\nhostname 192.0.2.10\n"))

    def doc(self, **overrides):
        doc = {"ok": True, "checks": [], "schemaVersion": 1, "host": "server"}
        doc.update(overrides)
        return json.dumps(doc).encode()

    def fetch(self, *rules):
        return doctor.fetch_remote("fleet-tools-ro", fake_runner(list(rules)))

    def test_no_alias_means_no_route(self):
        doc = self.fetch((("ssh", "-G"), (0, "user jay\nhostname fleet-tools-ro\n")))
        self.assertFalse(doc["reachable"])
        self.assertIn(doctor.NO_ROUTE, doc["error"])
        rows = {r["check"]: r for r in doc["checks"]}
        self.assertTrue(
            {"runtime", "tool:simple-chat", "tool:gitleaks", "tool:claude"} <= set(rows)
        )
        self.assertTrue(all(r["state"] == "unknown" for r in rows.values()))
        self.assertTrue(rows["tool:gitleaks"]["required"])
        for key in ("path", "version", "latest", "latestSource", "basis"):
            self.assertIn(key, rows["tool:gitleaks"])  # the same shape as a reachable registry row

    def test_an_alias_that_looks_like_an_option_is_refused_before_ssh_runs(self):
        calls = []
        for alias in ("-oProxyCommand=touch /tmp/x", "", "two words"):
            doc = doctor.fetch_remote(alias, fake_runner([self.CONFIGURED], calls))
            self.assertFalse(doc["reachable"])
            self.assertIn("not a plain host alias", doc["error"])
        self.assertEqual(calls, [])

    def test_the_ssh_call_is_read_only_and_batch(self):
        calls = []
        doctor.fetch_remote(
            "fleet-tools-ro",
            fake_runner([self.CONFIGURED, (("BatchMode=yes",), (0, self.doc()))], calls),
        )
        ssh = calls[-1]
        for option in ("BatchMode=yes", "ConnectTimeout=8", "StrictHostKeyChecking=yes"):
            self.assertIn(option, ssh)
        self.assertEqual(
            ssh[-1], "fleet-tools-ro"
        )  # no remote command:  the forced command decides

    def test_answers(self):
        self.assertTrue(
            self.fetch(self.CONFIGURED, (("BatchMode=yes",), (1, self.doc(ok=False))))["reachable"]
        )
        for answer, why in (
            (subprocess.TimeoutExpired(["ssh"], 150), "did not answer"),
            ((255, b"", b"Permission denied (publickey).\n"), "refused the key"),
            ((0, b"not json"), "not JSON"),
            ((0, json.dumps({"ok": True, "checks": []}).encode()), "predates schemaVersion"),
            ((0, self.doc(schemaVersion=2)), "schemaVersion 2"),
            ((0, self.doc(host="mac")), "not the server"),
            ((0, b"[" * 50000 + b"]" * 50000), "nested too deeply"),
            (
                (0, self.doc(checks=[{"check": "x", "status": 1, "detail": "d"}])),
                "malformed check row",
            ),
            ((0, self.doc(checks=["not a row"])), "malformed check row"),
        ):
            doc = self.fetch(self.CONFIGURED, (("BatchMode=yes",), answer))
            self.assertFalse(doc["reachable"], why)
            self.assertIn(why, doc["error"])
            self.assertTrue(all(r["state"] == "unknown" for r in doc["checks"]))

    def test_ssh_error_text_never_reaches_the_report(self):
        stderr = b"ssh: connect to host 192.0.2.77 port 22: Operation timed out\n"
        doc = self.fetch(self.CONFIGURED, (("BatchMode=yes",), (255, b"", stderr)))
        self.assertEqual(doc["error"], "ssh fleet-tools-ro exited 255 (the connection timed out)")
        doc = self.fetch(self.CONFIGURED, (("BatchMode=yes",), (255, b"", b"something new\n")))
        self.assertEqual(doc["error"], "ssh fleet-tools-ro exited 255")

    def test_depth_and_node_limits(self):
        self.assertTrue(doctor.json_within_limits({"a": [1, 2, {"b": 3}]}))
        deep = []
        for _ in range(40):
            deep = [deep]
        self.assertFalse(doctor.json_within_limits(deep))
        self.assertFalse(doctor.json_within_limits(list(range(10)), max_nodes=5))


class ReportAndRunTests(HomeCase):
    def run_doctor(self, argv, rules=(), host="server"):
        out = io.StringIO()
        registry = (
            {"name": "gitleaks", "kind": "dev-tool", "mac": "required", "server": "required"},
        )
        with (
            mock.patch.object(doctor, "host_kind", return_value=host),
            mock.patch.object(doctor, "find_repo", return_value=None),
            mock.patch.object(doctor, "TOOL_REGISTRY", registry),
            mock.patch.object(doctor, "doctor_commit", return_value=TIP),
            contextlib.redirect_stdout(out),
        ):
            _, code = doctor.run(
                argv, env={}, runner=fake_runner(list(rules)), fetch=lambda u, timeout=8: b"{}"
            )
        return out.getvalue(), code

    def test_json_keeps_the_original_contract_and_adds_fields(self):
        text, code = self.run_doctor(["--json", "--no-network"])
        doc = json.loads(text)
        self.assertIn("ok", doc)
        self.assertEqual(code, 0 if doc["ok"] else 1)
        self.assertTrue(doc["checks"])
        for row in doc["checks"]:
            for key in ("check", "status", "detail"):
                self.assertIsInstance(row[key], str, row)
            for key in doctor.ROW_FIELDS:
                self.assertIn(key, row)
        self.assertEqual((doc["schemaVersion"], doc["host"]), (1, "server"))
        self.assertIsNone(doc["expected"]["rev"])
        self.assertEqual(doc["doctorCommit"], TIP)
        rows = {r["check"]: r for r in doc["checks"]}
        self.assertEqual(rows["tool:gitleaks"]["state"], "missing")  # required on both hosts
        self.assertEqual(rows["tool:gitleaks"]["status"], "FAIL")

    def test_no_network_never_calls_a_fleet_tool_current(self):
        (self.rt_dir).mkdir(parents=True)
        (self.rt_dir / ".git").write_text("gitdir: x\n")
        self.touch("apps/lanes/_managed/fleet/agent-sync-runtime/scripts/simple-chat", "x")
        (self.home / ".local/bin").mkdir(parents=True)
        (self.home / ".local/bin/simple-chat").symlink_to(self.rt_dir / "scripts/simple-chat")
        rules = [
            (("rev-parse", "HEAD"), (0, TIP + "\n")),
            (
                ("rev-parse", "origin/main"),
                (0, TIP + "\n"),
            ),  # the local ref agrees, and still is not enough
            (("launchctl",), (113, "")),
        ]
        text, _ = self.run_doctor(["--json", "--no-network"], rules, host="mac")
        rows = {r["check"]: r for r in json.loads(text)["checks"]}
        self.assertEqual(rows["runtime"]["state"], "unknown")
        self.assertEqual(rows["tool:simple-chat"]["state"], "unknown")
        current = [
            r["check"]
            for r in rows.values()
            if r["state"] == "current"
            and (
                r["check"] == "runtime"
                or r["check"].startswith(("tool:agent", "tool:board", "tool:lane", "tool:recall"))
            )
        ]
        self.assertEqual(current, [])

    def test_write_is_atomic_private_and_versioned(self):
        path = self.home / ".local/state/fleet-tools/report.json"
        path.parent.mkdir(parents=True)
        path.write_text("old")
        path.chmod(0o644)
        rules = [(("ssh", "-G"), (0, "hostname fleet-tools-ro\n"))]
        text, _ = self.run_doctor(
            ["--no-network", "--remote", "fleet-tools-ro", "--write", str(path)], rules, host="mac"
        )
        self.assertEqual(len(text.strip().splitlines()), 1)
        self.assertIn("server unreachable:", text)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["report.json"])
        report = json.loads(path.read_text())
        self.assertEqual((report["schemaVersion"], report["tool"]), (1, "fleet-tools-doctor"))
        self.assertEqual([h["host"] for h in report["hosts"]], ["mac", "server"])
        self.assertTrue(report["hosts"][0]["reachable"])
        self.assertFalse(report["hosts"][1]["reachable"])
        self.assertIn(doctor.NO_ROUTE, report["hosts"][1]["error"])

    def test_write_dash_prints_the_report(self):
        text, _ = self.run_doctor(["--no-network", "--write", "-"], host="server")
        self.assertEqual(json.loads(text)["hosts"][0]["host"], "server")

    def test_a_symlinked_report_folder_is_refused(self):
        real = self.home / "elsewhere"
        real.mkdir()
        (self.home / "state").mkdir()
        (self.home / "state/fleet-tools").symlink_to(real)
        with self.assertRaises(ValueError):
            doctor.write_report(self.home / "state/fleet-tools/report.json", {"schemaVersion": 1})
        self.assertEqual(list(real.iterdir()), [])

    def test_no_registry_skips_the_version_probes(self):
        text, _ = self.run_doctor(["--json", "--no-network", "--no-registry"])
        checks = [r["check"] for r in json.loads(text)["checks"]]
        self.assertNotIn("tool:gitleaks", checks)

    def test_presence_rows_are_their_own_category_and_an_absent_checkout_is_unknown(self):
        text, _ = self.run_doctor(["--json", "--no-network"])
        rows = {r["check"]: r for r in json.loads(text)["checks"]}
        self.assertEqual(rows["command:simple-chat"]["category"], "presence")
        self.assertEqual(rows["tool-file:scripts/recall"]["category"], "presence")
        self.assertIn("presence", doctor.VERDICT_EXCLUDED)
        self.assertEqual(rows["checkout:FleetLink"]["state"], "unknown")  # find_repo finds none

    def test_the_mac_judges_its_tools_by_the_managed_runtime_even_with_a_release_receipt(self):
        self.rt_dir.mkdir(parents=True)
        (self.rt_dir / ".git").write_text("gitdir: x\n")
        self.touch(".local/share/fleet-cli/status.json", json.dumps({"source_commit": OLD}))
        rules = [(("rev-parse", "HEAD"), (0, TIP + "\n")), (("launchctl",), (113, ""))]
        text, _ = self.run_doctor(["--json", "--no-network"], rules, host="mac")
        runtime = {r["check"]: r for r in json.loads(text)["checks"]}["runtime"]
        self.assertEqual(runtime["installed"], TIP)  # the managed runtime's HEAD, not the release's
        self.assertNotIn("release", runtime["detail"])


class ServerStatusTests(HomeCase):
    """The server's doctor carries the collector's report, and the Mac keeps it beside its own."""

    run_doctor = ReportAndRunTests.run_doctor
    CONFIGURED = (("ssh", "-G"), (0, "user root\nhostname 192.0.2.10\n"))

    def status(self, **overrides):
        status = {
            "schemaVersion": 1,
            "tool": "fleet-server-status",
            "generatedAt": "2026-10-28T15:00:00Z",
            "host": "server",
            "coolify": [],
            "processes": [],
            "files": [],
            "hostMetrics": {"cpuCount": 4, "disks": []},
            "services": [],
            "filesSummary": {"found": 0, "limit": 200, "truncated": False},
        }
        status.update(overrides)
        return status

    def status_path(self):
        return self.home / doctor.SERVER_STATUS_RELATIVE

    def write_status(self, text):
        path = self.status_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text if isinstance(text, str) else json.dumps(text))
        return path

    def server_answer(self, status):
        doc = {"ok": True, "checks": [], "schemaVersion": 1, "host": "server"}
        if status is not None:
            doc["serverStatus"] = status
        return (("BatchMode=yes",), (0, json.dumps(doc).encode()))

    def test_the_server_json_carries_a_valid_status(self):
        self.write_status(self.status())
        text, _ = self.run_doctor(["--json", "--no-network", "--no-registry"])
        doc = json.loads(text)
        self.assertEqual(doc["serverStatus"], self.status())
        # The doctor's own contract is unchanged beside it.
        self.assertEqual((doc["schemaVersion"], doc["host"]), (1, "server"))
        self.assertIsInstance(doc["checks"], list)

    def test_the_server_json_leaves_out_a_status_that_is_not_one(self):
        too_big = self.status(files=[{"path": "x" * 1024}] * 600)
        for text in (
            None,  # no file at all
            "not json",
            "[1, 2]",
            json.dumps(self.status(tool="something-else")),
            json.dumps({"schemaVersion": 1}),
            json.dumps(too_big),
        ):
            if text is None:
                self.assertFalse(self.status_path().exists())
            else:
                self.write_status(text)
            out, _ = self.run_doctor(["--json", "--no-network", "--no-registry"])
            self.assertNotIn("serverStatus", json.loads(out), (text or "")[:40])

    def test_only_the_server_and_only_json_carry_it(self):
        self.write_status(self.status())
        text, _ = self.run_doctor(["--json", "--no-network", "--no-registry"], host="mac")
        self.assertNotIn("serverStatus", json.loads(text))
        text, _ = self.run_doctor(["--no-network", "--no-registry", "--write", "-"])
        self.assertNotIn("serverStatus", json.loads(text)["hosts"][0])

    def test_the_mac_writes_a_valid_status_privately_and_drops_it_from_the_report(self):
        report = self.home / ".local/state/fleet-tools/report.json"
        rules = [self.CONFIGURED, self.server_answer(self.status())]
        self.run_doctor(
            ["--no-network", "--no-registry", "--remote", "fleet-tools-ro", "--write", str(report)],
            rules,
            host="mac",
        )
        combined = json.loads(report.read_text())
        server = combined["hosts"][1]
        self.assertTrue(server["reachable"])
        self.assertNotIn("serverStatus", server)
        path = self.status_path()
        self.assertEqual(json.loads(path.read_text()), self.status())
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        # Atomic:  no temporary file is left beside the two reports.
        self.assertEqual(
            sorted(p.name for p in path.parent.iterdir()), ["report.json", "server-status.json"]
        )

    def test_the_mac_drops_an_invalid_status_and_keeps_the_previous_file(self):
        report = self.home / ".local/state/fleet-tools/report.json"
        previous = self.write_status(self.status(host="before"))
        for status in (
            self.status(tool="other"),
            ["not", "an", "object"],
            "a string",
            self.status(files=[{"path": "x" * 1024}] * 600),
        ):
            rules = [self.CONFIGURED, self.server_answer(status)]
            self.run_doctor(
                [
                    "--no-network",
                    "--no-registry",
                    "--remote",
                    "fleet-tools-ro",
                    "--write",
                    str(report),
                ],
                rules,
                host="mac",
            )
            server = json.loads(report.read_text())["hosts"][1]
            self.assertTrue(server["reachable"])
            self.assertNotIn("serverStatus", server)
            self.assertEqual(json.loads(previous.read_text())["host"], "before")

    def test_printing_the_report_strips_the_status_and_writes_nothing(self):
        rules = [self.CONFIGURED, self.server_answer(self.status())]
        text, _ = self.run_doctor(
            ["--no-network", "--no-registry", "--remote", "fleet-tools-ro", "--write", "-"],
            rules,
            host="mac",
        )
        self.assertNotIn("serverStatus", json.loads(text)["hosts"][1])
        self.assertFalse(self.status_path().exists())

    def test_an_unreachable_server_writes_nothing(self):
        server = doctor.unreachable_server("no route")
        server["serverStatus"] = self.status()
        self.assertFalse(doctor.keep_server_status(server, self.status_path()))
        self.assertNotIn("serverStatus", server)
        self.assertFalse(self.status_path().exists())
        self.assertFalse(doctor.keep_server_status(None, self.status_path()))
        self.assertFalse(doctor.keep_server_status({"reachable": True}, self.status_path()))

    def test_a_write_that_fails_never_fails_the_doctor(self):
        blocked = self.home / "blocked"
        blocked.write_text("a file where the folder should be")
        server = {"reachable": True, "serverStatus": self.status()}
        self.assertFalse(doctor.keep_server_status(server, blocked / "server-status.json"))

    def test_the_answer_bound_leaves_room_for_the_status(self):
        self.assertGreaterEqual(
            doctor.MAX_REMOTE_BYTES, 1024 * 1024 + 2 * doctor.MAX_SERVER_STATUS_BYTES
        )
        self.assertEqual(doctor.MAX_SERVER_STATUS_BYTES, 512 * 1024)

    def test_a_status_with_too_many_nodes_is_refused(self):
        many = self.status(processes=[{"pid": n} for n in range(doctor.MAX_REMOTE_NODES // 2)])
        self.assertFalse(doctor.server_status_valid(many))
        self.assertTrue(doctor.server_status_valid(self.status()))

    def test_a_deeply_nested_status_file_is_left_out_without_a_crash(self):
        # Within 512 KiB, but past the parser's recursion limit:  --json still prints a document.
        self.write_status("[" * 200000)
        out, _ = self.run_doctor(["--json", "--no-network", "--no-registry"])
        doc = json.loads(out)
        self.assertNotIn("serverStatus", doc)
        self.assertIsInstance(doc["checks"], list)
        self.assertIsNone(doctor.read_server_status(self.status_path()))

    def timer_row(self, minutes_old, linger, user="root"):
        linger_dir = self.home / "linger"
        linger_dir.mkdir(exist_ok=True)
        if linger:
            (linger_dir / user).touch()
        stamp = doctor.parse_utc(self.status()["generatedAt"])
        report = doctor.Report()
        doctor.check_server_status_timer(
            report,
            self.status_path(),
            clock=lambda: stamp + minutes_old * 60,
            user=user,
            linger_dir=linger_dir,
        )
        return report.row("updater:fleet-server-status.timer")

    def test_the_collector_timer_row(self):
        row = self.timer_row(10, linger=True)
        self.assertEqual((row["state"], row["status"]), ("absent", "OK"))
        self.write_status(self.status())
        row = self.timer_row(10, linger=True)
        self.assertEqual((row["state"], row["status"]), ("current", "OK"))
        # Fresh, but the timer stops when the last session closes.
        (self.home / "linger/root").unlink()
        row = self.timer_row(10, linger=False)
        self.assertEqual((row["state"], row["status"]), ("unknown", "WARN"))
        self.assertIn("loginctl enable-linger root", row["reason"])
        # Past Muster's window:  stale, and the linger hint when it is the likely cause.
        row = self.timer_row(60, linger=False)
        self.assertEqual((row["state"], row["status"]), ("stale", "FAIL"))
        self.assertIn("enable-linger", row["reason"])
        row = self.timer_row(60, linger=True)
        self.assertEqual((row["state"], row["status"]), ("stale", "FAIL"))
        self.assertNotIn("enable-linger", row["reason"])
        self.write_status("[" * 5000)
        row = self.timer_row(10, linger=True)
        self.assertEqual((row["state"], row["status"]), ("stale", "FAIL"))


class NotesServiceTests(unittest.TestCase):
    """AFC-486:  the Notes rows Muster shows (category `service`)."""

    PRINT = "gui/501/com.jay.operator-notes-runner = {\n\tstate = running\n\truns = 3\n\tlast exit code = 0\n}\n"
    NOW = 1_800_000_000.0

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.state = root / "notes-runner.json"
        self.log = root / "apple-notes-actions.jsonl"
        for name, value in (("NOTES_RUNNER_STATE", self.state), ("NOTES_ACTION_LOG", self.log)):
            patcher = mock.patch.object(doctor, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def iso(self, seconds_ago):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.NOW - seconds_ago))

    def rows(self, host, answer=(0, PRINT), health=None, network=True):
        report = doctor.Report()
        doctor.check_notes(
            report,
            host,
            {},
            fake_runner([(("launchctl", "print"), answer)]),
            network=network,
            health=health or (lambda env: {}),
            clock=lambda: self.NOW,
        )
        return {row["check"]: row for row in report.rows}

    def test_a_runner_that_is_not_loaded_fails(self):
        rows = self.rows("mac", answer=(113, ""))
        self.assertEqual(
            (rows["notes:runner"]["status"], rows["notes:runner"]["category"]), ("FAIL", "service")
        )

    def test_a_loaded_runner_that_polls_is_current_and_a_silent_one_is_stale(self):
        self.state.write_text(json.dumps({"last_poll_at": self.iso(30), "handled": 4}))
        self.assertEqual(self.rows("mac")["notes:runner"]["state"], "current")
        self.state.write_text(
            json.dumps({"last_poll_at": self.iso(3600), "last_error": "unavailable: x"})
        )
        row = self.rows("mac")["notes:runner"]
        self.assertEqual((row["status"], row["state"]), ("WARN", "stale"))
        self.assertIn("last error", row["detail"])

    def test_the_action_log_row_reports_unsent_lines(self):
        self.assertEqual(self.rows("mac")["notes:action-log"]["state"], "unknown")  # no log yet
        self.log.write_text('{"id":"a","ts":"2026-10-22T10:00:00Z"}\n')
        self.state.write_text(
            json.dumps(
                {
                    "last_poll_at": self.iso(10),
                    "log": {"offset": 0},
                    "log_shipped_at": self.iso(7200),
                }
            )
        )
        self.assertEqual(self.rows("mac")["notes:action-log"]["state"], "stale")
        size = self.log.stat().st_size
        self.state.write_text(
            json.dumps(
                {
                    "last_poll_at": self.iso(10),
                    "log": {"inode": self.log.stat().st_ino, "offset": size},
                    "log_shipped_at": self.iso(60),
                }
            )
        )
        self.assertEqual(self.rows("mac")["notes:action-log"]["state"], "current")

    def test_unsent_bytes_are_counted_from_the_file_the_cursor_names(self):
        # The cursor can sit in a rotated copy (a send that failed after a rotation):  the rest of that copy and the
        # whole newer log are unsent, however small the new log is.
        rotated = self.log.with_name(self.log.name + ".1")
        rotated.write_text("x" * 3999 + "\n")
        self.log.write_text("y" * 99 + "\n")
        cursor = {"inode": rotated.stat().st_ino, "offset": 1000}
        self.state.write_text(
            json.dumps(
                {
                    "last_poll_at": self.iso(10),
                    "log": cursor,
                    "log_shipped_at": self.iso(7200),
                    "last_error": "log:  unavailable: the Operator notes API could not be reached",
                    "last_error_at": self.iso(60),
                }
            )
        )
        row = self.rows("mac")["notes:action-log"]
        self.assertEqual(row["state"], "stale")
        self.assertIn("%d bytes not yet sent" % (4000 - 1000 + 100), row["detail"])
        self.assertIn("last error (60 s ago)", row["detail"])
        # A cursor that names no kept file:  the runner sends every kept file again, so all of it counts.
        self.state.write_text(
            json.dumps({"log": {"inode": -1, "offset": 50}, "log_shipped_at": self.iso(7200)})
        )
        self.assertIn("4100 bytes not yet sent", self.rows("mac")["notes:action-log"]["detail"])
        # A file cut short in place (shorter than the cursor):  the runner finds its place again, so all of it counts.
        self.state.write_text(
            json.dumps(
                {
                    "log": {"inode": self.log.stat().st_ino, "offset": 500},
                    "log_shipped_at": self.iso(7200),
                }
            )
        )
        self.assertIn("4100 bytes not yet sent", self.rows("mac")["notes:action-log"]["detail"])

    def test_a_torn_tail_and_an_old_or_unrelated_error_are_not_reported_as_waiting(self):
        self.log.write_text(
            '{"id":"a","ts":"2026-10-22T10:00:00Z"}\n{"id":"b","ts":'
        )  # the last line torn
        whole = len('{"id":"a","ts":"2026-10-22T10:00:00Z"}\n')
        state = {
            "last_poll_at": self.iso(10),
            "log": {"inode": self.log.stat().st_ino, "offset": whole},
            "log_shipped_at": self.iso(7200),
            "last_error": "log:  unavailable: x",
            "last_error_at": self.iso(9000),  # before the last send
        }
        self.state.write_text(json.dumps(state))
        self.assertEqual(self.rows("mac")["notes:action-log"]["state"], "current")
        self.log.write_text(self.log.read_text() + "1}\n")  # finished:  now one line waits
        self.state.write_text(json.dumps(state))
        row = self.rows("mac")["notes:action-log"]
        self.assertEqual(row["state"], "stale")
        self.assertNotIn("last error", row["detail"])  # the only error is older than the last send
        self.state.write_text(
            json.dumps(dict(state, last_error="internal: a claim", last_error_at=self.iso(60)))
        )
        self.assertNotIn(
            "last error", self.rows("mac")["notes:action-log"]["detail"]
        )  # not from shipping the log
        # A torn tail longer than the doctor's first look back (64 KiB) is still left out.
        self.log.write_text('{"id":"a","ts":"2026-10-22T10:00:00Z"}\n' + "z" * 100_000)
        self.state.write_text(
            json.dumps(dict(state, log={"inode": self.log.stat().st_ino, "offset": whole}))
        )
        self.assertEqual(self.rows("mac")["notes:action-log"]["state"], "current")

    def test_a_line_written_moments_ago_is_not_late_however_long_ago_the_last_send(self):
        # A quiet Mac sends nothing for hours;  a line it writes now waits only for the runner's next pass (25 s).
        self.log.write_text('{"id":"a","ts":"2026-10-22T10:00:00Z"}\n')
        os.utime(self.log, (self.NOW - 20, self.NOW - 20))
        self.state.write_text(
            json.dumps(
                {
                    "last_poll_at": self.iso(5),
                    "log": {"offset": 0},
                    "log_shipped_at": self.iso(7200),
                }
            )
        )
        self.assertEqual(self.rows("mac")["notes:action-log"]["state"], "current")
        os.utime(
            self.log, (self.NOW - 600, self.NOW - 600)
        )  # written ten minutes ago and still not sent
        self.assertEqual(self.rows("mac")["notes:action-log"]["state"], "stale")

    def test_the_doctor_counts_the_copies_the_script_keeps(self):
        from agent_sync import notes as N

        self.assertEqual(doctor.NOTES_LOG_ROTATIONS, N.ACTION_LOG_ROTATIONS)

    def test_the_server_reads_the_queue_health(self):
        fresh = {
            "queued": 1,
            "running": 0,
            "oldest_queued_seconds": 20,
            "runner_polled_at": self.iso(10),
        }
        self.assertEqual(
            self.rows("server", health=lambda env: fresh)["notes:queue"]["status"], "OK"
        )
        silent = dict(fresh, runner_polled_at=None)
        self.assertEqual(
            self.rows("server", health=lambda env: silent)["notes:queue"]["state"], "stale"
        )
        backlog = dict(fresh, oldest_queued_seconds=4000)
        self.assertIn(
            "waited", self.rows("server", health=lambda env: backlog)["notes:queue"]["detail"]
        )

    def test_a_failed_probe_is_a_row_never_a_crash(self):
        class Refused(Exception):
            def __init__(self, code):
                super().__init__(code)
                self.code, self.message = code, "x"

        def raises(code):
            def health(env):
                raise Refused(code)

            return health

        self.assertEqual(
            self.rows("server", health=raises("not_configured"))["notes:queue"]["status"], "WARN"
        )
        self.assertEqual(
            self.rows("server", health=raises("unavailable"))["notes:queue"]["status"], "FAIL"
        )
        self.assertEqual(self.rows("server", network=False)["notes:queue"]["state"], "unknown")


class CodeRootTests(unittest.TestCase):
    """The doctor finds the integration checkouts through the canonical roots.

    The owner removed the /root aliases on 2026-10-25, so the doctor reads the
    roots the fleet names in the environment and keeps the copy under the home
    as the fallback.
    """

    def make_repo(self, root: Path, name: str) -> Path:
        repo = root / name
        (repo / ".git").mkdir(parents=True)
        return repo

    def test_a_configured_root_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "configured"
            repo = self.make_repo(root, "Fixture-Repo")
            with mock.patch.dict(
                os.environ, {"FLEET_CODE_ROOT": str(root), "FLEET_SERVER_VOLUME": ""}
            ):
                self.assertEqual(doctor.find_repo("Fixture-Repo"), repo)

    def test_the_volume_the_host_names_is_used_next(self):
        with tempfile.TemporaryDirectory() as tmp:
            volume = Path(tmp) / "volume"
            repo = self.make_repo(volume / "Code", "Fixture-Repo-2")
            with (
                mock.patch.dict(
                    os.environ, {"FLEET_CODE_ROOT": "", "FLEET_SERVER_VOLUME": str(volume)}
                ),
                mock.patch.object(doctor, "SERVER_CODE_ROOTS", ()),
                mock.patch.object(doctor, "HOME", Path(tmp) / "home"),
            ):
                self.assertEqual(doctor.find_repo("Fixture-Repo-2"), repo)

    def test_the_copy_under_the_home_is_the_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            repo = self.make_repo(home / "Code", "Fixture-Repo")
            with (
                mock.patch.dict(os.environ, {"FLEET_CODE_ROOT": "", "FLEET_SERVER_VOLUME": ""}),
                mock.patch.object(doctor, "SERVER_CODE_ROOTS", ()),
                mock.patch.object(doctor, "HOME", home),
            ):
                self.assertEqual(doctor.find_repo("Fixture-Repo"), repo)

    def test_a_host_with_no_root_and_no_alias_finds_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            with (
                mock.patch.dict(os.environ, {"FLEET_CODE_ROOT": "", "FLEET_SERVER_VOLUME": ""}),
                mock.patch.object(doctor, "SERVER_CODE_ROOTS", ()),
                mock.patch.object(doctor, "HOME", Path(tmp) / "home"),
            ):
                self.assertIsNone(doctor.find_repo("Fixture-Repo"))

    def test_a_root_named_with_a_tilde_is_expanded_against_the_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            repo = self.make_repo(home / "trees", "Fixture-Repo")
            with (
                mock.patch.dict(
                    os.environ, {"FLEET_CODE_ROOT": "~/trees", "FLEET_SERVER_VOLUME": ""}
                ),
                mock.patch.object(doctor, "SERVER_CODE_ROOTS", ()),
                mock.patch.object(doctor, "HOME", home),
            ):
                self.assertEqual(doctor.find_repo("Fixture-Repo"), repo)

    def test_a_relative_root_is_joined_onto_the_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            repo = self.make_repo(home / "trees", "Fixture-Repo")
            with (
                mock.patch.dict(
                    os.environ, {"FLEET_CODE_ROOT": "trees", "FLEET_SERVER_VOLUME": ""}
                ),
                mock.patch.object(doctor, "SERVER_CODE_ROOTS", ()),
                mock.patch.object(doctor, "HOME", home),
            ):
                self.assertEqual(doctor.find_repo("Fixture-Repo"), repo)

    def test_the_volume_may_be_named_with_a_tilde(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            repo = self.make_repo(home / "vol" / "Code", "Fixture-Repo-2")
            with (
                mock.patch.dict(
                    os.environ, {"FLEET_CODE_ROOT": "", "FLEET_SERVER_VOLUME": "~/vol"}
                ),
                mock.patch.object(doctor, "SERVER_CODE_ROOTS", ()),
                mock.patch.object(doctor, "HOME", home),
            ):
                self.assertEqual(doctor.find_repo("Fixture-Repo-2"), repo)

    def test_a_relative_volume_is_joined_onto_the_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            repo = self.make_repo(home / "vol" / "Code", "Fixture-Repo-2")
            with (
                mock.patch.dict(os.environ, {"FLEET_CODE_ROOT": "", "FLEET_SERVER_VOLUME": "vol"}),
                mock.patch.object(doctor, "SERVER_CODE_ROOTS", ()),
                mock.patch.object(doctor, "HOME", home),
            ):
                self.assertEqual(doctor.find_repo("Fixture-Repo-2"), repo)

    def test_as_root_a_root_another_user_can_write_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "planted"
            repo = self.make_repo(root, "Fixture-Repo")
            fake = mock.Mock(st_uid=1000, st_mode=stat.S_IFDIR | 0o755)
            real_stat = os.stat
            with (
                mock.patch.dict(
                    os.environ, {"FLEET_CODE_ROOT": str(root), "FLEET_SERVER_VOLUME": ""}
                ),
                mock.patch.object(doctor, "SERVER_CODE_ROOTS", ()),
                mock.patch.object(
                    doctor.os,
                    "stat",
                    side_effect=lambda p, *a, **k: (
                        fake if p == str(root) else real_stat(p, *a, **k)
                    ),
                ),
            ):
                # As root the doctor runs nothing out of a tree another user can write.
                self.assertIsNone(doctor.find_repo("Fixture-Repo", euid=0))
                # As that user, the tree is its own and is searched.
                self.assertEqual(doctor.find_repo("Fixture-Repo", euid=1000), repo)

    def test_the_warning_names_every_root_that_was_searched(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            report = doctor.Report()
            with (
                mock.patch.dict(
                    os.environ, {"FLEET_CODE_ROOT": str(home), "FLEET_SERVER_VOLUME": ""}
                ),
                mock.patch.object(doctor, "SERVER_CODE_ROOTS", ()),
                mock.patch.object(doctor, "HOME", home),
            ):
                doctor.check_checkouts(report, network=False, host="server")
            detail = report.row(f"checkout:{doctor.REPOS[0]}")["detail"]
            self.assertIn(str(home), detail)
            self.assertIn(str(home / "Code"), detail)

    def test_the_roots_are_reported_once_each(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            with (
                mock.patch.dict(
                    os.environ,
                    {"FLEET_CODE_ROOT": str(home / "Code"), "FLEET_SERVER_VOLUME": ""},
                ),
                mock.patch.object(doctor, "SERVER_CODE_ROOTS", ()),
                mock.patch.object(doctor, "HOME", home),
            ):
                roots = doctor.code_roots()
            self.assertEqual(roots, [home / "Code"])


if __name__ == "__main__":
    unittest.main()
