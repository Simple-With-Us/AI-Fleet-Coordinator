"""Behavioral MM adapter tests.  Synthetic paths are never opened by the hook."""

import io
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from fleet_lanes import guard
from fleet_lanes import minimax_guard_hook as mm


class MinimaxHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.env = {"HOME": self.home}

    def payload(self, tool, path):
        return {"tool_name": tool, "tool_input": {"file_path": path}, "cwd": self.home}

    def reason(self, tool, path, env=None):
        ctx = guard.make_context(self.env if env is None else env, home=self.home)
        return mm.decide(self.payload(tool, path), ctx)[0]

    def fleet_clone_into_temp(self):
        """A temp clone of a registered fleet repo: the repo comes from the registry, not a literal."""
        owner_repo = sorted(guard.make_context(self.env, home=self.home).owner_repos)[0]
        return f"git clone https://github.com/{owner_repo} /tmp/example-repo"

    def test_secret_paths_all_file_tools_and_relative_paths(self):
        for tool in ("Read", "Write", "Edit"):
            for path in (
                ".secrets/dummy",
                ".env",
                "credentials.json",
                "./keys/../.secrets/dummy",
                ".secrets",
            ):
                with self.subTest(tool=tool, path=path):
                    self.assertIn("secret-bearing", self.reason(tool, path))

    def test_integration_reads_and_writes_block_but_public_files_allow(self):
        for tool in ("Read", "Write", "Edit"):
            self.assertIn("integration tree", self.reason(tool, "Code/BotFleet/src/file.py"))
            self.assertIn(
                "integration tree",
                self.reason(tool, "Code/BotFleet/.claude/worktrees/work/file.py"),
            )
            self.assertIsNone(self.reason(tool, "public/file.py"))

    def test_symlink_to_secret_is_resolved_without_reading(self):
        os.symlink(os.path.join(self.home, ".secrets"), os.path.join(self.home, "alias"))
        self.assertIn("secret-bearing", self.reason("Read", "alias/dummy"))

    def test_secret_named_symlink_stays_secret(self):
        os.symlink(os.path.join(self.home, "public-config"), os.path.join(self.home, ".env"))
        self.assertIn("secret-bearing", self.reason("Read", ".env"))

    def test_case_insensitive_code_root_and_nested_harness(self):
        import dataclasses

        ctx = guard.make_context(self.env, home=self.home)
        original = mm.layout.make_roots
        for insensitive in (True, False):
            roots = dataclasses.replace(
                original(self.home, self.env, registry=ctx.registry), case_insensitive=insensitive
            )
            with mock.patch.object(mm.layout, "make_roots", return_value=roots):
                for suffix in ("src/a.py", ".claude/worktrees/work/src/a.py"):
                    for tool in ("Read", "Write", "Edit"):
                        reason = mm.decide(self.payload(tool, "code/BotFleet/" + suffix), ctx)[0]
                        if insensitive:
                            self.assertIn("integration tree", reason)
                        else:
                            self.assertIsNone(reason)

    def test_review_read_allowed_write_denied(self):
        path = "apps/lanes/BotFleet/review-pr-123/file.py"
        self.assertIsNone(self.reason("Read", path))
        self.assertIn("review lane", self.reason("Edit", path))

    def test_launcher_identity_beats_engine_and_ambient_seat(self):
        env = dict(self.env, AGENT_SEAT="MM", AGENT_LAUNCHER="BotFleet", AGENT_LAUNCH_SEAT="CODEX")
        self.assertIn(
            "another seat", self.reason("Write", "apps/lanes/BotFleet/minimax-work/file", env)
        )
        self.assertIsNone(self.reason("Write", "apps/lanes/BotFleet/codex-work/file", env))
        self.assertIsNone(self.reason("Read", "apps/lanes/BotFleet/minimax-work/file", env))

    def test_no_identity_is_not_inferred_as_mm(self):
        self.assertIn("identity is unavailable", self.reason("Write", "apps/lanes/BotFleet/codex-work/file"))
        env = dict(self.env, AGENT_SEAT="MM", AGENT_LAUNCHER="BotFleet")
        self.assertIn("identity is unavailable", self.reason("Write", "apps/lanes/BotFleet/codex-work/file", env))

    def test_shell_policy_bare_ps_lane_clone_and_allow(self):
        for command in (
            "ps",
            "ps aux",
            "cat ~/.secrets/dummy",
            self.fleet_clone_into_temp(),
        ):
            payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": self.home}
            self.assertIsNotNone(mm.decide(payload)[0], command)
        self.assertIsNone(
            mm.decide({"tool_name": "Bash", "tool_input": {"command": "git status"}})[0]
        )

    def test_content_is_not_shell_and_unknown_payloads_fail_open(self):
        payload = self.payload("Write", "public/file.py")
        payload["tool_input"]["content"] = "ps aux; cat ~/.secrets/dummy"
        self.assertIsNone(mm.decide(payload)[0])
        for payload in (
            {},
            {"tool_name": "Bash", "tool_input": []},
            {"tool_name": "Read", "tool_input": {"file_path": 3}},
        ):
            self.assertIsNone(mm.decide(payload)[0])
        for raw in ("invalid", "[]", "null"):
            out = io.StringIO()
            self.assertEqual(mm.main(stdin=io.StringIO(raw), stdout=out), 0)
            self.assertEqual(out.getvalue(), "")

    def test_maximum_length_command_preserves_shared_guard_denial(self):
        command = self.fleet_clone_into_temp()
        command += " " * (guard.MAX_COMMAND_CHARS - len(command))
        for tool in ("Bash", "bash"):
            payload = {"tool_name": tool, "tool_input": {"command": command}}
            self.assertIsNotNone(mm.decide(payload)[0])

    def test_structurally_empty_registry_refuses_file_tools(self):
        import copy

        ctx = guard.make_context(self.env, home=self.home)
        for data in ({}, {"apps": []}, {"apps": "invalid"}, {"apps": [{}]}):
            registry = mm.layout.parse_registry(data, source="/synthetic/registry.json")
            empty = copy.copy(ctx)
            empty.registry = registry
            for tool in ("Read", "Write", "Edit"):
                reason = mm.decide(self.payload(tool, "Code/ExampleRepo/file.py"), empty)[0]
                self.assertIn("registry is unavailable or invalid", reason)

    def test_shell_alternate_fields_and_trailing_process_command_are_guarded(self):
        for field in ("command", "cmd", "CommandLine", "commandLine", "script"):
            payload = {"tool_name": "bash", "tool_input": {field: self.fleet_clone_into_temp()}}
            self.assertIsNotNone(mm.decide(payload)[0])
        self.assertIsNotNone(mm.decide({"tool_name": "Bash", "command": self.fleet_clone_into_temp()})[0])
        for command in ("true; ps", "true && ps", "true\nps"):
            self.assertIsNotNone(mm.decide({"tool_name": "Bash", "tool_input": {"command": command}})[0])

    def test_code_directory_only_registry_preserves_integration_policy(self):
        import copy
        ctx = copy.copy(guard.make_context(self.env, home=self.home))
        ctx.registry = mm.layout.parse_registry({"apps": [{"codeDir": "ExampleRepo"}]}, source="synthetic")
        reason = mm.decide(self.payload("Write", "Code/ExampleRepo/file.py"), ctx)[0]
        self.assertIn("integration tree", reason)

    def test_native_fallback_names_and_fields(self):
        for tool in ("read", "write", "edit"):
            args = {"file_path" if tool == "edit" else "path": ".secrets/dummy"}
            payload = {"tool_name": tool, "tool_input": args, "cwd": self.home}
            ctx = guard.make_context(self.env, home=self.home)
            self.assertIn("secret-bearing", mm.decide(payload, ctx)[0])
        payload = {"tool_name": "bash", "tool_input": {"command": "ps", "description": "probe"}}
        self.assertIsNotNone(mm.decide(payload)[0])

    def test_a_missing_cwd_does_not_skip_the_file_policy(self):
        for tool, field in (("Read", "file_path"), ("Write", "file_path"), ("read", "path")):
            payload = {"tool_name": tool, "tool_input": {field: ".secrets/dummy"}}
            ctx = guard.make_context(self.env, home=self.home)
            with self.subTest(tool=tool):
                self.assertIn("secret-bearing", mm.decide(payload, ctx)[0])
        payload = {"tool_name": "Write", "tool_input": {"file_path": "Code/BotFleet/file.py"}}
        ctx = guard.make_context(self.env, home=self.home)
        self.assertIn("integration tree", mm.decide(payload, ctx)[0])
        self.assertIsNone(
            mm.decide({"tool_name": "Read", "tool_input": {"file_path": "public/file.py"}}, ctx)[0]
        )

    def test_unavailable_secret_table_preserves_other_path_policies(self):
        ctx = guard.make_context(self.env, home=self.home)
        for sg in (
            SimpleNamespace(),
            SimpleNamespace(SECRET_FILE_PATTERNS="invalid"),
            SimpleNamespace(SECRET_FILE_PATTERNS=["["]),
        ):
            for path, expected in (
                ("Code/BotFleet/file.py", "integration tree"),
                ("apps/lanes/BotFleet/review-pr-1/file.py", "review lane"),
                (".secrets/dummy", "secret-bearing"),
            ):
                with self.subTest(policy=expected, table=sg):
                    self.assertIn(expected, mm.decide(self.payload("Write", path), ctx, sg)[0])
            self.assertIn("SECRET_FILE_PATTERNS", mm.decide(self.payload("Read", ".env"), ctx, sg)[0])
        with mock.patch.object(mm.fleet, "_load_secret_guard", return_value=None):
            self.assertIn("SECRET_FILE_PATTERNS", mm.decide(self.payload("Read", ".env"), ctx)[0])
            self.assertIn(
                "integration tree",
                mm.decide(self.payload("Write", "Code/BotFleet/file.py"), ctx)[0],
            )

    def test_fallback_registry_keeps_secret_and_review_checks(self):
        env = dict(self.env, FLEET_APPS_JSON=os.path.join(self.home, "missing-registry.json"))
        ctx = guard.make_context(env, home=self.home)
        self.assertEqual(ctx.registry.source, "fallback")
        self.assertIn("secret-bearing", mm.decide(self.payload("Read", ".env"), ctx)[0])
        self.assertIn(
            "review lane",
            mm.decide(self.payload("Write", "apps/lanes/BotFleet/review-pr-1/file.py"), ctx)[0],
        )
        self.assertIn("FLEET_APPS_JSON", mm.decide(self.payload("Write", "Code/BotFleet/file.py"), ctx)[0])

    def test_either_path_field_name_is_checked(self):
        ctx = guard.make_context(self.env, home=self.home)
        for tool in ("Read", "Write", "Edit", "read", "write", "edit"):
            for field in ("path", "file_path"):
                payload = {
                    "tool_name": tool,
                    "tool_input": {field: ".secrets/dummy"},
                    "cwd": self.home,
                }
                with self.subTest(tool=tool, field=field):
                    self.assertIn("secret-bearing", mm.decide(payload, ctx)[0])
            payload = {
                "tool_name": tool,
                "tool_input": {
                    "path": "public/other.py",
                    "file_path": "apps/lanes/BotFleet/review-pr-1/f.py",
                },
                "cwd": self.home,
            }
            with self.subTest(tool=tool, field="both"):
                reason = mm.decide(payload, ctx)[0]
                if tool in ("Read", "read"):
                    self.assertIsNone(reason)
                else:
                    self.assertIn("review lane", reason)

    def test_deny_contract_and_policy_failure(self):
        out = io.StringIO()
        payload = {"tool_name": "Bash", "tool_input": {"command": "ps"}}
        self.assertEqual(mm.main(stdin=io.StringIO(json.dumps(payload)), stdout=out), 0)
        self.assertEqual(
            json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"], "deny"
        )
        with mock.patch.object(mm, "file_reason", side_effect=RuntimeError):
            self.assertIsNone(mm.decide(self.payload("Read", ".secrets/dummy"))[0])


if __name__ == "__main__":
    unittest.main()
