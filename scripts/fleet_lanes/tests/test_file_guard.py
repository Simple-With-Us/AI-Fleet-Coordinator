"""The seat-neutral file guard (file_guard.py) and Claude Code's adapter (claude_file_guard_hook.py).

Synthetic paths only:  the guard reads path strings and never opens them.
"""

import copy
import io
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from fleet_lanes import claude_file_guard_hook as claude
from fleet_lanes import file_guard as fg
from fleet_lanes import guard, layout

TOOLS = fg.FileTools(
    kinds={"Look": fg.READ, "Put": fg.WRITE, "Patch": fg.WRITE},
    path_fields=("file_path", "path", "notebook_path"),
)
STRICT = fg.FilePolicy(label="Probe", tools=TOOLS)
READABLE = fg.FilePolicy(label="Probe", tools=TOOLS, deny_integration_reads=False)


class CoreTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = os.path.realpath(tmp.name)
        self.env = {"HOME": self.home}

    def ctx(self, **extra):
        return guard.make_context(dict(self.env, **extra), home=self.home)

    def reason(self, tool, path, policy=STRICT, ctx=None, field="file_path", cwd="home", sg=None):
        payload = {"tool_name": tool, "tool_input": {field: path}}
        if cwd == "home":
            payload["cwd"] = self.home
        elif cwd is not None:
            payload["cwd"] = cwd
        return fg.file_reason(payload, policy, ctx or self.ctx(), sg)

    def test_the_tool_map_rejects_an_unknown_kind_and_no_fields(self):
        with self.assertRaises(ValueError):
            fg.FileTools(kinds={"X": "delete"})
        with self.assertRaises(ValueError):
            fg.FileTools(kinds={"X": fg.READ}, path_fields=())

    def test_a_tool_outside_the_map_is_not_a_file_operation(self):
        self.assertIsNone(self.reason("Read", ".secrets/x"))
        self.assertIsNone(fg.file_reason({"tool_name": "Look"}, STRICT, self.ctx()))
        self.assertIsNone(
            fg.file_reason({"tool_name": "Look", "tool_input": []}, STRICT, self.ctx())
        )
        self.assertIsNone(fg.file_reason({"tool_name": 3, "tool_input": {}}, STRICT, self.ctx()))

    def test_secret_paths_are_refused_for_reads_and_writes(self):
        for tool in ("Look", "Put", "Patch"):
            for path in (".secrets/x", ".env", "credentials.json", "./a/../.secrets/x", ".secrets"):
                with self.subTest(tool=tool, path=path):
                    reason = self.reason(tool, path)
                    self.assertIn("secret-bearing", reason)
                    self.assertTrue(reason.startswith("Probe file tool"))

    def test_every_path_field_is_checked(self):
        for field in ("file_path", "path", "notebook_path"):
            with self.subTest(field=field):
                self.assertIn("secret-bearing", self.reason("Put", ".secrets/x", field=field))
        self.assertIsNone(self.reason("Put", ".secrets/x", field="other_field"))

    def test_the_integration_tree_read_depends_on_the_policy_and_writes_are_refused(self):
        path = "Code/BotFleet/src/file.py"
        self.assertIn("integration tree", self.reason("Look", path, STRICT))
        self.assertIsNone(self.reason("Look", path, READABLE))
        for policy in (STRICT, READABLE):
            for tool in ("Put", "Patch"):
                self.assertIn("integration tree", self.reason(tool, path, policy))
        self.assertIsNone(self.reason("Put", "public/file.py", STRICT))

    def test_a_review_lane_is_readable_and_never_writable(self):
        path = "apps/lanes/BotFleet/review-pr-12/file.py"
        self.assertIsNone(self.reason("Look", path))
        self.assertIn("review lane", self.reason("Put", path))

    def test_another_seats_lane_and_an_unknown_seat_are_refused_for_writes(self):
        own = "apps/lanes/BotFleet/claude-work/file.py"
        other = "apps/lanes/BotFleet/codex-work/file.py"
        seated = self.ctx(AGENT_SEAT="CLAUDE")
        self.assertIsNone(self.reason("Put", own, ctx=seated))
        self.assertIn("another seat", self.reason("Put", other, ctx=seated))
        self.assertIsNone(self.reason("Look", other, ctx=seated))
        self.assertIn("identity is unavailable", self.reason("Put", own))
        launched = self.ctx(AGENT_SEAT="CLAUDE", AGENT_LAUNCHER="BotFleet")
        self.assertIn("identity is unavailable", self.reason("Put", own, ctx=launched))
        assigned = self.ctx(AGENT_SEAT="CLAUDE", AGENT_LAUNCH_SEAT="CODEX")
        self.assertIsNone(self.reason("Put", other, ctx=assigned))
        self.assertIn("another seat", self.reason("Put", own, ctx=assigned))

    def test_trusted_seat_follows_the_launcher_contract(self):
        registry = self.ctx().registry
        self.assertIsNone(fg.trusted_seat({}, registry))
        self.assertIsNone(fg.trusted_seat({"AGENT_LAUNCHER": "x", "AGENT_SEAT": "CLAUDE"}, registry))
        self.assertEqual(fg.trusted_seat({"AGENT_SEAT": "CLAUDE"}, registry).suffix.lower(), "claude")
        self.assertEqual(
            fg.trusted_seat({"AGENT_SEAT": "CLAUDE", "AGENT_LAUNCH_SEAT": "CODEX"}, registry)
            .suffix.lower(),
            "codex",
        )

    def test_a_missing_registry_refuses_and_keeps_the_known_denials(self):
        ctx = self.ctx(FLEET_APPS_JSON=os.path.join(self.home, "missing.json"))
        self.assertEqual(ctx.registry.source, "fallback")
        self.assertIn("FLEET_APPS_JSON", self.reason("Put", "public/file.py", ctx=ctx))
        self.assertIn("FLEET_APPS_JSON", self.reason("Look", "public/file.py", ctx=ctx))
        self.assertIn("secret-bearing", self.reason("Look", ".env", ctx=ctx))
        self.assertIn(
            "review lane", self.reason("Put", "apps/lanes/BotFleet/review-pr-1/f", ctx=ctx)
        )
        empty = copy.copy(self.ctx())
        empty.registry = layout.parse_registry({"apps": []}, source="/synthetic.json")
        self.assertIn("registry is unavailable", self.reason("Look", "public/f", ctx=empty))

    def test_a_missing_secret_table_refuses_and_keeps_the_known_denials(self):
        for sg in (
            SimpleNamespace(),
            SimpleNamespace(SECRET_FILE_PATTERNS="invalid"),
            SimpleNamespace(SECRET_FILE_PATTERNS=["["]),
            SimpleNamespace(SECRET_FILE_PATTERNS=[]),
        ):
            with self.subTest(table=sg):
                self.assertIn("SECRET_FILE_PATTERNS", self.reason("Look", ".env", sg=sg))
                self.assertIn("SECRET_FILE_PATTERNS", self.reason("Put", "public/f", sg=sg))
                self.assertIn("secret-bearing", self.reason("Look", ".secrets/x", sg=sg))
                self.assertIn("integration tree", self.reason("Put", "Code/BotFleet/f", sg=sg))
        with mock.patch.object(fg.fleet, "_load_secret_guard", return_value=None):
            self.assertIn("SECRET_FILE_PATTERNS", self.reason("Look", "public/f"))
        patterns, error = fg.load_secret_patterns()
        self.assertFalse(error)
        self.assertTrue(patterns)

    def test_relative_paths_resolve_against_cwd_or_the_home(self):
        lane = os.path.join(self.home, "Code", "BotFleet")
        self.assertIn("integration tree", self.reason("Put", "src/file.py", cwd=lane))
        self.assertIsNone(self.reason("Put", "src/file.py", cwd=os.path.join(self.home, "pub")))
        # No cwd, a relative cwd and a non-string cwd all resolve against the home.
        for cwd in (None, "relative/dir", 7):
            with self.subTest(cwd=cwd):
                self.assertIn("secret-bearing", self.reason("Look", ".secrets/x", cwd=cwd))
                self.assertIn("integration tree", self.reason("Put", "Code/BotFleet/f", cwd=cwd))

    def test_tilde_paths_resolve_against_the_home(self):
        self.assertIn("secret-bearing", self.reason("Look", "~/.secrets/x", cwd="/elsewhere"))
        self.assertIn("integration tree", self.reason("Put", "~/Code/BotFleet/f", cwd="/elsewhere"))
        self.assertIsNone(self.reason("Put", "~/public/f", cwd="/elsewhere"))

    def test_a_symlink_into_the_integration_tree_or_the_secrets_is_followed(self):
        os.makedirs(os.path.join(self.home, "Code", "BotFleet"))
        os.symlink(os.path.join(self.home, "Code", "BotFleet"), os.path.join(self.home, "bf"))
        self.assertIn("integration tree", self.reason("Put", "bf/src/file.py"))
        self.assertIsNone(self.reason("Look", "bf/src/file.py", READABLE))
        os.symlink(os.path.join(self.home, ".secrets"), os.path.join(self.home, "keys"))
        self.assertIn("secret-bearing", self.reason("Look", "keys/x"))


class ClaudeAdapterTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = os.path.realpath(tmp.name)
        self.ctx = guard.make_context({"HOME": self.home, "AGENT_SEAT": "CLAUDE"}, home=self.home)

    def decide(self, tool, path, field="file_path"):
        payload = {"tool_name": tool, "tool_input": {field: path}, "cwd": self.home}
        return claude.decide(payload, self.ctx)[0]

    def test_the_tool_map_is_claude_codes_file_tools(self):
        self.assertEqual(
            dict(claude.CLAUDE_FILE_TOOLS.kinds),
            {
                "Read": "read",
                "Write": "write",
                "Edit": "write",
                "MultiEdit": "write",
                "NotebookEdit": "write",
            },
        )
        self.assertIn("notebook_path", claude.CLAUDE_FILE_TOOLS.path_fields)
        self.assertFalse(claude.CLAUDE_FILE_POLICY.deny_integration_reads)

    def test_every_file_tool_refuses_the_secrets(self):
        for tool in ("Read", "Write", "Edit", "MultiEdit"):
            with self.subTest(tool=tool):
                reason = self.decide(tool, ".secrets/x")
                self.assertIn("secret-bearing", reason)
                self.assertTrue(reason.startswith("Claude file tool"))
        self.assertIn("secret-bearing", self.decide("NotebookEdit", ".secrets/n.ipynb", "notebook_path"))

    def test_the_integration_tree_is_readable_and_never_writable(self):
        path = "Code/BotFleet/src/file.py"
        self.assertIsNone(self.decide("Read", path))
        for tool in ("Write", "Edit", "MultiEdit"):
            self.assertIn("integration tree", self.decide(tool, path))
        self.assertIn("integration tree", self.decide("NotebookEdit", path, "notebook_path"))

    def test_own_lane_writes_and_other_seats_lanes(self):
        self.assertIsNone(self.decide("Edit", "apps/lanes/BotFleet/claude-work/f.py"))
        self.assertIn("another seat", self.decide("Edit", "apps/lanes/BotFleet/codex-work/f.py"))
        self.assertIn("review lane", self.decide("Write", "apps/lanes/BotFleet/review-pr-3/f.py"))

    def test_shell_and_unknown_tools_are_not_judged_and_crashes_allow(self):
        for payload in (
            {"tool_name": "Bash", "tool_input": {"command": "cat ~/.secrets/x"}},
            {"tool_name": "Glob", "tool_input": {"pattern": "**"}},
            {},
        ):
            self.assertIsNone(claude.decide(payload, self.ctx)[0])
        with mock.patch.object(claude, "file_reason", side_effect=RuntimeError):
            self.assertIsNone(claude.decide({"tool_name": "Read", "tool_input": {}})[0])

    def test_main_writes_the_claude_deny_shape_and_nothing_on_allow(self):
        out = io.StringIO()
        payload = {"tool_name": "Read", "tool_input": {"file_path": "~/.secrets/x"}}
        self.assertEqual(claude.main(stdin=io.StringIO(json.dumps(payload)), stdout=out), 0)
        body = json.loads(out.getvalue())
        self.assertEqual(body["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("Claude file tool", body["hookSpecificOutput"]["permissionDecisionReason"])
        for raw in ("invalid", "[]", "null"):
            out = io.StringIO()
            self.assertEqual(claude.main(stdin=io.StringIO(raw), stdout=out), 0)
            self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
