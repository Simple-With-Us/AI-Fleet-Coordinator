"""install_mcp tests: the per-harness MCP registrations (fleet-recall, and agent-sync for fixed-seat tools).

    cd scripts && python3 -m unittest fleet_lanes.tests.test_install_mcp -v

Every test works in a throwaway fake home and never touches the real one.  The shapes asserted here were
written by the tools' own `mcp add` commands (OpenCode 2.0.21, Copilot CLI 1.0.68) or copied from an entry the
tool already holds (Muse Code, MiniMax, fx), on a throwaway home, on 2026-10-10.
"""
from __future__ import annotations

import io
import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from fleet_lanes import install_mcp as M
from fleet_lanes import marked_block as MB


class HomeCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.realpath(self._tmp.name)
        self.env = {"FLEET_RAG_HOME": ""}               # force the default <home>/apps/fleet-rag

    def path(self, rel: str) -> str:
        return os.path.join(self.home, rel)

    def mkdir(self, *rels: str) -> None:
        for rel in rels:
            os.makedirs(self.path(rel), exist_ok=True)

    def write(self, rel: str, text: str, mode: int | None = None) -> str:
        p = self.path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        Path(p).write_text(text, encoding="utf-8")
        if mode is not None:
            os.chmod(p, mode)
        return p

    def read(self, rel: str) -> str:
        return Path(self.path(rel)).read_text(encoding="utf-8")

    def load(self, rel: str):
        return json.loads(self.read(rel))

    def run_cli(self, *argv: str, home: bool = True):
        out, err = io.StringIO(), io.StringIO()
        args = list(argv) + (["--home", self.home] if home else [])
        code = M.main(args, out=out, err=err, env=self.env)
        return code, out.getvalue(), err.getvalue()

    def backups(self, rel_dir: str) -> list[str]:
        return sorted(n for n in os.listdir(self.path(rel_dir)) if M.BACKUP_TAG in n)

    @property
    def recall(self) -> str:
        return os.path.join(self.home, "apps", "fleet-rag", "fleet-recall-mcp.py")

    @property
    def agent_sync(self) -> str:
        return os.path.join(self.home, ".local", "bin", "agent-sync")


TOOL_DIRS = {"opencode": ".config/opencode", "kimi": ".kimi-code", "copilot": ".copilot", "muse": ".config/muse",
             "minimax": ".minimax", "fx": ".fx", "clutch": ".clutch/dsh"}


class ShapeTests(HomeCase):
    """The exact native entry each tool gets."""

    def expect(self) -> dict:
        r = self.recall
        return {
            ".config/opencode/opencode.json": {"mcp": {"servers": {"fleet-recall": {"type": "local", "command": ["python3", r]}}}},
            ".kimi-code/mcp.json": {"mcpServers": {"fleet-recall": {"command": "python3", "args": [r]}}},
            ".copilot/mcp-config.json": {"mcpServers": {"fleet-recall": {"tools": ["*"], "type": "local", "command": "python3", "args": [r]}}},
            ".config/muse/settings.json": {"mcpServers": {"fleet-recall": {"type": "stdio", "command": "python3", "args": [r], "mode": "optional"}}},
            ".minimax/mcp.json": {"mcpServers": {"fleet-recall": {"type": "stdio", "enabled": True, "configured": True,
                                                                 "builtin": False, "command": "python3", "args": [r]}}},
            ".fx/mcp.json": {"mcp": {"fleet-recall": {"type": "local", "command": ["python3", r], "enabled": True,
                                                      "startup_timeout_ms": 30000, "operation_timeout_ms": 60000,
                                                      "restart_limit": 1}}},
        }

    def test_apply_writes_each_native_shape_once(self) -> None:
        self.mkdir(*TOOL_DIRS.values())
        keys = [k for k in M.TARGET_KEYS if k != "clutch"]
        code, out, err = self.run_cli("apply", *keys)
        self.assertEqual(code, M.EXIT_OK, out + err)
        for rel, want in self.expect().items():
            with self.subTest(rel=rel):
                self.assertEqual(self.load(rel), want)
                self.assertEqual(stat.S_IMODE(os.stat(self.path(rel)).st_mode), 0o600, "a new config is owner-only")
        self.assertIn("Summary: 6 files changed.", out)
        # a second apply changes nothing and writes no backup
        before = {rel: self.read(rel) for rel in self.expect()}
        code, out, _ = self.run_cli("apply", *keys)
        self.assertEqual(code, M.EXIT_OK)
        self.assertIn("Summary: 0 files changed.", out)
        self.assertEqual({rel: self.read(rel) for rel in self.expect()}, before)
        for rel in self.expect():
            self.assertEqual(self.backups(os.path.dirname(rel)), [])

    def test_server_argv_uses_absolute_paths_and_no_tilde(self) -> None:
        argv = M.server_argv("fleet-recall", self.home, None, self.env)
        self.assertEqual(argv, ["python3", self.recall])
        argv = M.server_argv("agent-sync", self.home, "MC", self.env)
        self.assertEqual(argv, [self.agent_sync, "mcp", "--default-seat", "MC"])
        self.assertFalse(any(a.startswith("~") for a in argv))

    def test_fleet_rag_home_is_honoured(self) -> None:
        self.assertEqual(M.server_argv("fleet-recall", self.home, None, {"FLEET_RAG_HOME": "/opt/rag"}),
                         ["python3", "/opt/rag/fleet-recall-mcp.py"])


class NotInstalledTests(HomeCase):
    def test_a_tool_that_is_not_installed_is_never_given_a_folder(self) -> None:
        code, out, _ = self.run_cli("apply", *M.TARGET_KEYS)
        self.assertEqual(code, M.EXIT_OK)
        self.assertEqual(out.count("SKIPPED-NOT-INSTALLED"), len(M.TARGET_KEYS))
        self.assertEqual(os.listdir(self.home), [])

    def test_plan_is_read_only(self) -> None:
        self.mkdir(*TOOL_DIRS.values())
        code, out, _ = self.run_cli("plan")
        self.assertEqual(code, M.EXIT_OK)
        self.assertIn("would-add", out)
        self.assertEqual(sorted(os.listdir(self.path(".fx"))), [])
        code, out, _ = self.run_cli("apply", "fx", "--dry-run")
        self.assertIn("would-add", out)
        self.assertIn("plan only", out)
        self.assertEqual(os.listdir(self.path(".fx")), [])

    def test_default_command_is_plan(self) -> None:
        self.mkdir(".fx")
        code, out, _ = self.run_cli()
        self.assertEqual(code, M.EXIT_OK)
        self.assertIn("== fx", out)
        self.assertEqual(os.listdir(self.path(".fx")), [])


class ExistingConfigTests(HomeCase):
    def test_other_servers_and_settings_survive_with_a_backup(self) -> None:
        original = json.dumps({"model": "x", "mcpServers": {"coolify": {"type": "stdio", "command": "sh"}},
                               "zeta": [1, 2]}, indent=2) + "\n"
        self.write(".config/muse/settings.json", original, mode=0o640)
        code, out, _ = self.run_cli("apply", "muse")
        self.assertEqual(code, M.EXIT_OK, out)
        doc = self.load(".config/muse/settings.json")
        self.assertEqual(list(doc), ["model", "mcpServers", "zeta"], "key order is kept")
        self.assertEqual(list(doc["mcpServers"]), ["coolify", "fleet-recall"])
        self.assertEqual(doc["zeta"], [1, 2])
        names = self.backups(".config/muse")
        self.assertEqual(len(names), 1)
        self.assertEqual(self.read(".config/muse/" + names[0]), original)
        self.assertEqual(stat.S_IMODE(os.stat(self.path(".config/muse/settings.json")).st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(os.stat(self.path(".config/muse/" + names[0])).st_mode), 0o640)

    def test_four_space_indent_is_kept(self) -> None:
        self.write(".minimax/mcp.json", json.dumps({"mcpServers": {}}, indent=4) + "\n")
        self.run_cli("apply", "minimax")
        self.assertIn('\n    "mcpServers": {\n        "fleet-recall": {', self.read(".minimax/mcp.json"))

    def test_same_command_with_other_keys_is_present_and_untouched(self) -> None:
        entry = {"type": "stdio", "command": "python3", "args": [os.path.join(self.home, "apps/fleet-rag/fleet-recall-mcp.py")],
                 "mode": "required", "env": {"X": "1"}}
        text = json.dumps({"mcpServers": {"fleet-recall": entry}}) + "\n"
        self.write(".config/muse/settings.json", text)
        code, out, _ = self.run_cli("apply", "muse")
        self.assertEqual(code, M.EXIT_OK)
        self.assertIn("present", out)
        self.assertEqual(self.read(".config/muse/settings.json"), text)
        self.assertEqual(self.backups(".config/muse"), [])

    def test_a_pinned_agent_seat_is_reported_never_edited(self) -> None:
        entry = {"type": "stdio", "command": "python3", "args": [self.recall], "mode": "optional", "env": {"AGENT_SEAT": "MC"}}
        text = json.dumps({"mcpServers": {"fleet-recall": entry}}) + "\n"
        self.write(".config/muse/settings.json", text)
        code, out, _ = self.run_cli("apply", "muse")
        self.assertIn("WARNING: the entry pins AGENT_SEAT=MC", out)
        self.assertEqual(self.read(".config/muse/settings.json"), text)
        code, out, _ = self.run_cli("verify", "muse")
        self.assertIn("WARN", out)
        self.assertEqual(code, M.EXIT_OK, "a pin is a warning, not a failure")

    def test_a_different_command_is_foreign_and_left_alone(self) -> None:
        text = json.dumps({"mcpServers": {"fleet-recall": {"command": "node", "args": ["other.js"]}}}) + "\n"
        self.write(".kimi-code/mcp.json", text)
        code, out, _ = self.run_cli("apply", "kimi")
        self.assertEqual(code, M.EXIT_OK)
        self.assertIn("skipped-foreign", out)
        self.assertEqual(self.read(".kimi-code/mcp.json"), text)
        self.assertEqual(self.run_cli("verify", "kimi")[0], M.EXIT_FAILED)

    def test_invalid_json_is_never_rewritten(self) -> None:
        for body in ("{not json", "[1, 2]"):
            with self.subTest(body=body):
                self.write(".copilot/mcp-config.json", body)
                code, out, _ = self.run_cli("apply", "copilot")
                self.assertEqual(code, M.EXIT_OK)
                self.assertIn("skipped-invalid-json", out)
                self.assertEqual(self.read(".copilot/mcp-config.json"), body)

    def test_a_servers_container_that_is_not_an_object_is_left_alone(self) -> None:
        text = json.dumps({"mcpServers": ["oops"]}) + "\n"
        self.write(".minimax/mcp.json", text)
        code, out, _ = self.run_cli("apply", "minimax")
        self.assertIn("skipped-bad-shape", out)
        self.assertEqual(self.read(".minimax/mcp.json"), text)

    def test_opencode_v1_style_entries_are_not_mixed_with_the_v2_level(self) -> None:
        text = json.dumps({"mcp": {"old": {"type": "local", "command": ["x"]}}}) + "\n"
        self.write(".config/opencode/opencode.json", text)
        code, out, _ = self.run_cli("apply", "opencode")
        self.assertIn("skipped-v1-shape", out)
        self.assertEqual(self.read(".config/opencode/opencode.json"), text)

    def test_a_file_that_changed_while_planning_is_not_overwritten(self) -> None:
        self.write(".fx/mcp.json", json.dumps({"mcp": {}}) + "\n")
        plan = M.plan_target("fx", self.home, "add", False, self.env)
        self.assertTrue(plan.will_write)
        Path(self.path(".fx/mcp.json")).write_text('{"mcp": {"mine": {}}}\n')
        with self.assertRaises(OSError):
            M.write_plan(plan)
        self.assertEqual(self.load(".fx/mcp.json"), {"mcp": {"mine": {}}})
        self.assertEqual(self.backups(".fx"), [])

    def test_a_read_only_directory_fails_cleanly(self) -> None:
        self.mkdir(".fx")
        os.chmod(self.path(".fx"), 0o500)
        self.addCleanup(os.chmod, self.path(".fx"), 0o700)
        if os.access(self.path(".fx"), os.W_OK):
            self.skipTest("running as a user that ignores directory modes")
        code, out, _ = self.run_cli("apply", "fx")
        self.assertEqual(code, M.EXIT_FAILED)
        self.assertIn("FAILED", out)


class AgentSyncTests(HomeCase):
    def test_only_fixed_seat_tools_get_agent_sync_and_never_a_pinned_seat(self) -> None:
        self.mkdir(*TOOL_DIRS.values())
        keys = [k for k in M.TARGET_KEYS if k != "clutch"]
        code, out, _ = self.run_cli("apply", *keys, "--with-agent-sync")
        self.assertEqual(code, M.EXIT_OK, out)
        seats = {"muse": ("MC", ".config/muse/settings.json", ("mcpServers",)),
                 "minimax": ("MM", ".minimax/mcp.json", ("mcpServers",)),
                 "fx": ("FX", ".fx/mcp.json", ("mcp",))}
        for key, (seat, rel, path) in seats.items():
            doc = self.load(rel)
            for k in path:
                doc = doc[k]
            with self.subTest(key=key):
                self.assertIn("agent-sync", doc)
                entry = doc["agent-sync"]
                argv = ([entry["command"]] + entry["args"]) if isinstance(entry["command"], str) else entry["command"]
                self.assertEqual(argv, [self.agent_sync, "mcp", "--default-seat", seat])
                self.assertNotIn("env", entry, "never a pinned AGENT_SEAT")
        for key, rel in (("opencode", ".config/opencode/opencode.json"), ("kimi", ".kimi-code/mcp.json"),
                         ("copilot", ".copilot/mcp-config.json")):
            with self.subTest(key=key):
                self.assertNotIn("agent-sync", self.read(rel), "a tool with no default seat has no seat to register")

    def test_without_the_flag_agent_sync_is_never_written(self) -> None:
        self.mkdir(*TOOL_DIRS.values())
        self.run_cli("apply", "muse", "minimax", "fx")
        for rel in (".config/muse/settings.json", ".minimax/mcp.json", ".fx/mcp.json"):
            self.assertNotIn("agent-sync", self.read(rel))

    def test_remove_takes_out_only_what_apply_wrote(self) -> None:
        self.mkdir(".minimax", ".fx")
        hand = {"mcpServers": {"sentry": {"command": "npx"}, "fleet-recall": {"command": "python3", "args": [self.recall],
                                                                                 "type": "stdio", "extra": True}}}
        self.write(".minimax/mcp.json", json.dumps(hand) + "\n")
        self.run_cli("apply", "fx", "--with-agent-sync")
        code, out, _ = self.run_cli("remove", "minimax", "fx", "--with-agent-sync")
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertEqual(self.load(".minimax/mcp.json"), hand, "a hand-registered entry is not ours")
        self.assertIn("skipped-not-ours", out)
        self.assertEqual(self.load(".fx/mcp.json"), {"mcp": {}})

    def test_remove_of_an_absent_entry_is_quiet(self) -> None:
        self.mkdir(".fx")
        code, out, _ = self.run_cli("remove", "fx")
        self.assertEqual(code, M.EXIT_OK)
        self.assertIn("absent", out)
        self.assertEqual(os.listdir(self.path(".fx")), [])


class ClutchTests(HomeCase):
    def patch(self) -> str:
        return ".clutch/dsh/cordis.patch.yml"

    def test_without_the_flag_clutch_gets_nothing(self) -> None:
        self.mkdir(".clutch/dsh")
        code, out, _ = self.run_cli("apply", "clutch")
        self.assertEqual(code, M.EXIT_OK)
        self.assertIn("NOTHING-TO-DO", out)
        self.assertIn("native agent preset", out)
        self.assertEqual(os.listdir(self.path(".clutch/dsh")), [])

    def test_the_agent_sync_block_is_created_with_a_header(self) -> None:
        self.mkdir(".clutch/dsh")
        code, out, _ = self.run_cli("apply", "clutch", "--with-agent-sync")
        self.assertEqual(code, M.EXIT_OK, out)
        text = self.read(self.patch())
        self.assertTrue(text.startswith("# Home-level cordis patch"))
        self.assertIn("# fleet:begin mcp-agent-sync (managed by fleet_lanes.install_mcp)\n- insert:\n", text)
        self.assertIn("      name: '@deepseek-ai/dsh-mcp-client'\n", text)
        self.assertIn('        serverName: agent-sync\n        transport: stdio\n', text)
        self.assertIn('        command: "%s"\n' % self.agent_sync, text)
        self.assertIn('          - "mcp"\n          - "--default-seat"\n          - "CLUTCH"\n', text)
        self.assertIn("        failOnStartupError: false\n# fleet:end mcp-agent-sync\n", text)
        self.assertNotIn("env:", text, "no env, so no pinned seat")
        self.assertEqual(M.MB.block_body(text, "mcp-agent-sync")[0], "- insert:")
        before = text
        code, out, _ = self.run_cli("apply", "clutch", "--with-agent-sync")
        self.assertIn("unchanged", out)
        self.assertEqual(self.read(self.patch()), before)
        self.assertEqual(self.backups(".clutch/dsh"), [])

    def test_the_owners_patch_entries_survive_and_are_backed_up(self) -> None:
        mine = "# my layer\n- id: typert-gateway\n  config:\n    websocketHeartbeatIntervalMs: 15000\n"
        self.write(self.patch(), mine)
        self.run_cli("apply", "clutch", "--with-agent-sync")
        text = self.read(self.patch())
        self.assertTrue(text.startswith(mine + "\n# fleet:begin"))
        names = self.backups(".clutch/dsh")
        self.assertEqual(self.read(".clutch/dsh/" + names[0]), mine)

    def test_a_changed_command_replaces_the_block_in_place(self) -> None:
        self.mkdir(".clutch/dsh")
        self.run_cli("apply", "clutch", "--with-agent-sync")
        text = self.read(self.patch()).replace(self.agent_sync, "/old/agent-sync")
        self.write(self.patch(), text + "- id: tail\n")
        code, out, _ = self.run_cli("apply", "clutch", "--with-agent-sync")
        self.assertIn("block replaced in place", out)
        new = self.read(self.patch())
        self.assertIn(self.agent_sync, new)
        self.assertNotIn("/old/agent-sync", new)
        self.assertTrue(new.endswith("- id: tail\n"))

    def test_remove_deletes_only_the_block(self) -> None:
        mine = "- id: typert-gateway\n  config:\n    a: 1\n"
        self.write(self.patch(), mine)
        self.run_cli("apply", "clutch", "--with-agent-sync")
        code, out, _ = self.run_cli("remove", "clutch", "--with-agent-sync")
        self.assertIn("removed", out)
        self.assertEqual(self.read(self.patch()), mine)

    def test_removing_the_last_block_deletes_the_file_because_a_comment_only_patch_crashes_the_engine(self) -> None:
        self.mkdir(".clutch/dsh")
        self.run_cli("apply", "clutch", "--with-agent-sync")
        code, out, _ = self.run_cli("remove", "clutch", "--with-agent-sync")
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertIn("so the file is removed", out)
        self.assertFalse(os.path.exists(self.path(self.patch())))
        names = self.backups(".clutch/dsh")
        self.assertEqual(len(names), 1)
        self.assertIn("# fleet:begin mcp-agent-sync", self.read(".clutch/dsh/" + names[0]))

    def test_another_installers_block_in_the_same_patch_is_untouched(self) -> None:
        from fleet_lanes import cordis_patch as CP
        self.mkdir(".clutch/dsh")
        self.run_cli("apply", "clutch", "--with-agent-sync")
        text, _ = CP.upsert(self.read(self.patch()), "hooks-fleet-guards", "other", ["- id: fleet-hooks-guards"])
        self.write(self.patch(), text)
        self.run_cli("remove", "clutch", "--with-agent-sync")
        left = self.read(self.patch())
        self.assertIn("# fleet:begin hooks-fleet-guards (managed by other)", left)
        self.assertNotIn("mcp-agent-sync", left)
        self.run_cli("apply", "clutch", "--with-agent-sync")
        self.assertIn("# fleet:begin hooks-fleet-guards", self.read(self.patch()))
        self.assertIn("# fleet:begin mcp-agent-sync", self.read(self.patch()))

    def test_a_patch_that_is_not_a_list_is_left_alone(self) -> None:
        text = "plugins:\n  - a\n"
        self.write(self.patch(), text)
        code, out, _ = self.run_cli("apply", "clutch", "--with-agent-sync")
        self.assertIn("skipped-bad-shape", out)
        self.assertEqual(self.read(self.patch()), text)

    def test_broken_markers_are_refused(self) -> None:
        text = "- id: a\n# fleet:begin mcp-agent-sync (managed by x)\n- insert: []\n"
        self.write(self.patch(), text)
        code, out, _ = self.run_cli("apply", "clutch", "--with-agent-sync")
        self.assertIn("skipped-bad-markers", out)
        self.assertEqual(self.read(self.patch()), text)

    def test_odd_paths_are_quoted_as_yaml_strings(self) -> None:
        body = M.clutch_block_body(['/Users/o\'brien/a b/agent-sync', "mcp", "--default-seat", "CLUTCH"])
        self.assertIn('        command: "/Users/o\'brien/a b/agent-sync"', body)


class VerifyTests(HomeCase):
    def test_verify_fails_before_and_passes_after_apply(self) -> None:
        self.mkdir(".fx", ".copilot")
        code, out, _ = self.run_cli("verify", "fx", "copilot")
        self.assertEqual(code, M.EXIT_FAILED)
        self.assertIn("FAIL", out)
        self.run_cli("apply", "fx", "copilot")
        code, out, _ = self.run_cli("verify", "fx", "copilot")
        self.assertEqual(code, M.EXIT_OK, out)
        self.assertEqual(out.count("PASS"), 2)

    def test_a_tool_that_is_not_installed_is_a_skip_not_a_failure(self) -> None:
        code, out, _ = self.run_cli("verify", "kimi")
        self.assertEqual(code, M.EXIT_OK)
        self.assertIn("SKIP", out)

    def test_verify_never_writes(self) -> None:
        self.mkdir(".fx")
        self.run_cli("verify", "fx")
        self.assertEqual(os.listdir(self.path(".fx")), [])


class ProbeTests(unittest.TestCase):
    def script(self, body: str) -> list[str]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name, "server.py")
        p.write_text(textwrap.dedent(body))
        return [sys.executable, str(p)]

    GOOD = """
        import json, sys
        for line in sys.stdin:
            m = json.loads(line)
            if m.get("method") == "initialize":
                print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "x", "version": "1"}}}), flush=True)
            elif m.get("method") == "tools/list":
                print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {"tools": [{"name": "a"}, {"name": "b"}]}}), flush=True)
    """

    def test_a_server_that_answers_passes(self) -> None:
        ok, detail = M.probe_stdio(self.script(self.GOOD), timeout=10)
        self.assertTrue(ok, detail)
        self.assertIn("2 tools listed", detail)

    def test_a_server_that_exits_fails(self) -> None:
        ok, detail = M.probe_stdio(self.script("import sys; sys.exit(3)"), timeout=5)
        self.assertFalse(ok)
        self.assertIn("no initialize result", detail)

    def test_a_missing_command_fails(self) -> None:
        ok, detail = M.probe_stdio(["/nonexistent/server"], timeout=2)
        self.assertFalse(ok)
        self.assertIn("cannot start", detail)

    def test_a_silent_server_times_out(self) -> None:
        ok, detail = M.probe_stdio(self.script("import time; time.sleep(30)"), timeout=1.5)
        self.assertFalse(ok)
        self.assertIn("timeout", detail)

    def test_verify_probe_runs_the_registered_command(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        home = os.path.realpath(tmp.name)
        os.makedirs(os.path.join(home, ".fx"))
        rag = os.path.join(home, "rag")
        os.makedirs(rag)
        Path(rag, "fleet-recall-mcp.py").write_text(textwrap.dedent(self.GOOD))
        env = {"FLEET_RAG_HOME": rag}
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(M.main(["apply", "fx", "--home", home], out=out, err=err, env=env), M.EXIT_OK)
        out = io.StringIO()
        code = M.main(["verify", "fx", "--probe", "--home", home], out=out, err=err, env=env)
        self.assertEqual(code, M.EXIT_OK, out.getvalue())
        self.assertIn("probe: initialize ok, 2 tools listed", out.getvalue())


class CliTests(HomeCase):
    def test_apply_needs_a_target(self) -> None:
        code, _, err = self.run_cli("apply")
        self.assertEqual(code, M.EXIT_USAGE)
        self.assertIn("name at least one target", err)

    def test_unknown_target_is_a_usage_error(self) -> None:
        code, _, err = self.run_cli("apply", "emacs")
        self.assertEqual(code, M.EXIT_USAGE)
        self.assertIn("unknown target", err)

    def test_empty_home_is_a_usage_error_never_the_real_home(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(M.main(["plan", "--home", ""], out=out, err=err), M.EXIT_USAGE)
        self.assertIn("--home must not be empty", err.getvalue())
        self.assertEqual(M.main(["plan", "--home", "/nonexistent-dir-xyz"], out=out, err=err), M.EXIT_USAGE)

    def test_aliases(self) -> None:
        self.assertEqual(M._resolve_targets(["muse-code", "mm", "kimi-code", "copilot-cli", "dsh"], True),
                         ["muse", "minimax", "kimi", "copilot", "clutch"])


class MarkedBlockTests(unittest.TestCase):
    def test_upsert_replace_remove_round_trip(self) -> None:
        base = "a: 1\n"
        t1, act = MB.upsert_block(base, "x", "tool", ["- one"])
        self.assertEqual(act, "append")
        t2, act = MB.upsert_block(t1, "x", "tool", ["- one"])
        self.assertEqual((t2, act), (t1, "none"))
        t3, act = MB.upsert_block(t2, "x", "tool", ["- two"])
        self.assertEqual(act, "replace")
        self.assertIn("- two", t3)
        t4, act = MB.remove_block(t3, "x")
        self.assertEqual((t4, act), (base, "remove"))

    def test_other_names_are_ignored_and_malformed_raise(self) -> None:
        text = "# fleet:begin a (managed by t)\n1\n# fleet:end a\n"
        self.assertIsNone(MB.find_block(text, "b"))
        for bad in ("# fleet:begin a\n", "# fleet:end a\n", "# fleet:end a\n# fleet:begin a\n",
                    "# fleet:begin a\n# fleet:end a\n# fleet:begin a\n# fleet:end a\n"):
            with self.subTest(bad=bad), self.assertRaises(MB.BlockError):
                MB.find_block(bad, "a")

    def test_crlf_files_keep_their_line_endings(self) -> None:
        t1, _ = MB.upsert_block("a: 1\r\n", "x", "tool", ["- one"])
        self.assertNotIn("\n", t1.replace("\r\n", ""))
        t2, _ = MB.upsert_block(t1, "x", "tool", ["- two"])
        self.assertNotIn("\n", t2.replace("\r\n", ""))


if __name__ == "__main__":      # pragma: no cover
    unittest.main()
