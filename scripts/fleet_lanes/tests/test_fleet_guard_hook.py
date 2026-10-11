"""fleet_guard_hook tests: the lane guard and the secret guard behind one hook command.

    cd scripts && python3 -m unittest fleet_lanes.tests.test_fleet_guard_hook -v

The hook is run in-process with StringIO streams, and once as a script in a subprocess (the way a stable copy
runs it).  Nothing here touches a real home.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from fleet_lanes import fleet_guard_hook as FGH
from fleet_lanes import install_tools as T
from fleet_lanes import lane_guard_hook as LGH

SCRIPTS = Path(__file__).resolve().parents[2]
HOOK_FILE = SCRIPTS / "fleet_lanes" / "fleet_guard_hook.py"
PS = T.SECRET_DENY_COMMAND            # "ps aux": the secret guard's rule C
LANE_DENY = T.DENY_COMMAND            # a fleet-repo clone into /tmp


def bash(command: str, tool: str = "Bash") -> dict:
    return {"hook_event_name": "PreToolUse", "tool_name": tool, "tool_input": {"command": command}, "cwd": "/"}


def run(payload, *argv: str):
    """(exit code, stdout, stderr) of the hook for one payload (a dict, or raw text)."""
    stdin = io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
    out, err = io.StringIO(), io.StringIO()
    code = FGH.main(list(argv), stdin=stdin, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


class DecisionTests(unittest.TestCase):
    def test_a_lane_violation_is_denied_in_the_claude_shape(self) -> None:
        code, out, err = run(bash(LANE_DENY), "--format", "claude")
        self.assertEqual((code, err), (0, ""))
        body = json.loads(out)
        self.assertEqual(body["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("hh-verify", body["hookSpecificOutput"]["permissionDecisionReason"])

    def test_a_ps_is_denied_by_the_secret_guard(self) -> None:
        code, out, _ = run(bash(PS), "--format", "claude")
        self.assertEqual(code, 0)
        reason = json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("`ps`", reason)

    def test_the_shell_tool_may_be_spelled_either_way(self) -> None:
        for tool in ("Bash", "bash", "Shell", "shell"):
            with self.subTest(tool=tool):
                self.assertTrue(run(bash(PS, tool), "--format", "claude")[1])

    def test_ordinary_commands_are_allowed_with_no_output(self) -> None:
        for cmd in ("ls -la", "git status", T.ALLOW_COMMAND, "pgrep -c -f node"):
            with self.subTest(cmd=cmd):
                self.assertEqual(run(bash(cmd), "--format", "claude"), (0, "", ""))

    def test_a_tool_that_is_not_a_shell_is_never_judged_as_one(self) -> None:
        payload = {"tool_name": "write", "tool_input": {"file_path": "/tmp/x.md", "content": PS + "; " + LANE_DENY}}
        self.assertEqual(run(payload, "--format", "claude"), (0, "", ""))

    def test_the_lane_guard_goes_first(self) -> None:
        code, out, _ = run(bash(LANE_DENY + "; " + PS), "--format", "claude")
        self.assertIn("hh-verify", json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_the_kimi_contract_is_exit_two_with_the_reason_on_stderr(self) -> None:
        for cmd, needle in ((PS, "`ps`"), (LANE_DENY, "hh-verify")):
            with self.subTest(cmd=cmd):
                code, out, err = run(bash(cmd), "--format", "kimi")
                self.assertEqual((code, out), (2, ""))
                self.assertIn(needle, err)
        self.assertEqual(run(bash("ls"), "--format", "kimi"), (0, "", ""))

    def test_exit2_flag_works_for_any_format(self) -> None:
        code, out, err = run(bash(PS), "--format", "claude", "--exit2")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("`ps`", err)

    def test_every_lane_format_carries_the_secret_deny_too(self) -> None:
        cursor = {"hook_event_name": "beforeShellExecution", "command": PS, "cwd": "/", "workspace_roots": ["/"]}
        self.assertEqual(json.loads(run(cursor, "--format", "cursor")[1])["permission"], "deny")
        for fmt in ("grok", "antigravity"):
            body = json.loads(run(bash(PS), "--format", fmt)[1])
            self.assertEqual(body["decision"], "deny")

    def test_exit2_formats_agree_with_install_tools(self) -> None:
        self.assertEqual(tuple(LGH.EXIT2_FORMATS), tuple(T.EXIT2_FORMATS))
        for fmt in T.EXIT2_FORMATS:
            self.assertIn(fmt, LGH.FORMATS)


class FailOpenTests(unittest.TestCase):
    def test_bad_input_allows(self) -> None:
        for raw in ("", "not json", "[1, 2]", "null", '"ps aux"'):
            with self.subTest(raw=raw):
                self.assertEqual(run(raw, "--format", "claude"), (0, "", ""))

    def test_a_missing_secret_guard_leaves_the_lane_guard_working(self) -> None:
        with mock.patch.object(FGH, "SECRET_GUARD_NAMES", ("no-such-file.py",)):
            self.assertIsNone(FGH._load_secret_guard())
            self.assertEqual(run(bash(PS), "--format", "claude"), (0, "", ""))
            self.assertIn("hh-verify", run(bash(LANE_DENY), "--format", "claude")[1])

    def test_a_crashing_secret_guard_leaves_the_lane_guard_working(self) -> None:
        class Boom:
            @staticmethod
            def extract_command(payload):
                raise RuntimeError("broken on purpose")

            @staticmethod
            def check_command(command):
                raise RuntimeError("broken on purpose")
        reason, _ = FGH.decide(bash(LANE_DENY), secret_guard=Boom)
        self.assertIn("hh-verify", reason)
        self.assertEqual(FGH.decide(bash(PS), secret_guard=Boom), (None, ""))

    def test_a_crashing_lane_guard_leaves_the_secret_guard_working(self) -> None:
        from fleet_lanes import guard
        with mock.patch.object(guard, "evaluate", side_effect=RuntimeError("broken on purpose")):
            reason, _ = FGH.decide(bash(PS))
            self.assertIn("`ps`", reason)
            self.assertEqual(FGH.decide(bash("ls")), (None, ""))

    def test_a_secret_guard_file_that_is_not_a_guard_is_ignored(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            bogus = Path(tmp, "secret_guard.py")
            bogus.write_text("x = 1\n")
            with mock.patch.object(FGH, "SECRET_GUARD_NAMES", (str(bogus),)):
                self.assertIsNone(FGH._load_secret_guard())


class ScriptTests(unittest.TestCase):
    """The hook as a script, the way the stable copy runs it (`python3 -I`)."""

    def run_script(self, payload: dict, *argv: str):
        p = subprocess.run([sys.executable, "-I", str(HOOK_FILE), *argv], input=json.dumps(payload).encode("utf-8"),
                           capture_output=True, timeout=120)
        return p.returncode, p.stdout.decode(), p.stderr.decode()

    def test_the_script_finds_the_secret_guard_beside_the_package(self) -> None:
        code, out, _ = self.run_script(bash(PS), "--format", "claude")
        self.assertEqual(code, 0)
        self.assertIn("`ps`", json.loads(out)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_the_script_allows_an_ordinary_command(self) -> None:
        self.assertEqual(self.run_script(bash("ls"), "--format", "claude"), (0, "", ""))

    def test_the_script_speaks_the_kimi_contract(self) -> None:
        code, out, err = self.run_script(bash(PS), "--format", "kimi")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("`ps`", err)


if __name__ == "__main__":      # pragma: no cover
    unittest.main()
