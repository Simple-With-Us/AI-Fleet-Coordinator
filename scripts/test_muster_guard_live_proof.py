#!/usr/bin/env python3
"""Tests for scripts/muster-guard-live-proof.py.

    python3 scripts/test_muster_guard_live_proof.py

The tool exists because a guard can be installed, enabled, parsed, registered and correct
and still protect nothing, so it separates three things a green check tends to blur: the
matcher, the policy, and the live dispatch.  These tests pin the first two against a
temporary plugin directory and a fake hook command, so no MiniMax session and no installed
plugin is touched.

The case that matters most is the one that hid for months: a matcher that covers `Bash`
but not `bash` covers half the shell names, and reporting the whole group as missed (or
as covered) would hide the drift in both directions.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SCRIPT = SCRIPTS / "muster-guard-live-proof.py"


def _load():
    loader = importlib.machinery.SourceFileLoader("muster_guard_live_proof", str(SCRIPT))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = mod
    loader.exec_module(mod)
    return mod


proof = _load()


def write_plugin(root: Path, matcher: str, command: str) -> Path:
    """A plugin directory shaped like the installed MiniMax one."""
    plugin = root / "fleet-guard"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / "hooks").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "fleet-guard", "version": "1.0.0", "description": "test"}),
        encoding="utf-8",
    )
    (plugin / "hooks" / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": matcher,
                            "hooks": [{"type": "command", "command": command, "timeout": 10}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    return plugin


def write_fake_hook(path: Path, body: str) -> str:
    path.write_text("#!/bin/sh\ncat > /dev/null\n" + body + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


class CoveredNames(unittest.TestCase):
    def test_case_sensitive(self):
        covered, missed = proof._covered_names("Bash", ("bash", "Bash"))
        self.assertEqual(covered, ["Bash"])
        self.assertEqual(missed, ["bash"])

    def test_alternation_covers_both_spellings(self):
        covered, missed = proof._covered_names("Bash|bash", ("bash", "Bash"))
        self.assertEqual(sorted(covered), ["Bash", "bash"])
        self.assertEqual(missed, [])

    def test_empty_matcher_misses_everything(self):
        covered, missed = proof._covered_names("", ("bash", "Bash"))
        self.assertEqual(covered, [])
        self.assertEqual(sorted(missed), ["Bash", "bash"])

    def test_invalid_regex_misses_everything(self):
        covered, missed = proof._covered_names("Bash[", ("bash",))
        self.assertEqual(covered, [])
        self.assertEqual(missed, ["bash"])


class CheckMatcher(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_the_shipped_matcher_is_reported_as_incomplete(self):
        plugin = write_plugin(self.root, "Bash", "/bin/true")
        report = proof.read_plugin(plugin)
        result = proof.check_matcher(report)
        self.assertFalse(result["ok"])
        self.assertIn("bash", result["shell_missed"])
        self.assertNotIn("Bash", result["shell_missed"])
        self.assertIn("Read", result["file_missed"])

    def test_a_full_matcher_passes(self):
        plugin = write_plugin(self.root, "Bash|bash|Read|Write|Edit|read|write|edit", "/bin/true")
        report = proof.read_plugin(plugin)
        result = proof.check_matcher(report)
        self.assertTrue(result["ok"])
        self.assertEqual(result["shell_missed"], [])
        self.assertEqual(result["file_missed"], [])

    def test_no_pretooluse_handler(self):
        plugin = self.root / "empty"
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / "hooks").mkdir(parents=True)
        (plugin / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
        (plugin / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {}}), encoding="utf-8")
        result = proof.check_matcher(proof.read_plugin(plugin))
        self.assertFalse(result["ok"])


class ReadPlugin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_reads_the_manifest_and_the_hook_entries(self):
        plugin = write_plugin(self.root, "Bash|bash", "/bin/true")
        report = proof.read_plugin(plugin)
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["manifest"]["name"], "fleet-guard")
        self.assertEqual(len(report["hooks"]), 1)
        self.assertEqual(report["hooks"][0]["event"], "PreToolUse")
        self.assertEqual(report["hooks"][0]["matcher"], "Bash|bash")

    def test_a_missing_plugin_is_an_error_not_a_crash(self):
        report = proof.read_plugin(self.root / "absent")
        self.assertTrue(report["errors"])


class CheckPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_a_denying_hook_passes(self):
        deny = (
            "printf '%s' "
            '\'{"hookSpecificOutput": {"hookEventName": "PreToolUse", '
            '"permissionDecision": "deny", "permissionDecisionReason": "no"}}\''
        )
        command = write_fake_hook(self.root / "deny.sh", deny)
        plugin = write_plugin(self.root, "Bash|bash", command)
        result = proof.check_policy(proof.read_plugin(plugin))
        self.assertTrue(result["ok"])

    def test_an_allowing_hook_fails(self):
        command = write_fake_hook(self.root / "allow.sh", "exit 0")
        plugin = write_plugin(self.root, "Bash|bash", command)
        result = proof.check_policy(proof.read_plugin(plugin))
        self.assertFalse(result["ok"])

    def test_the_canary_is_silent_when_it_runs(self):
        # `ps > /dev/null` prints nothing, so an inert guard leaks no process list.
        self.assertIn("> /dev/null", proof.CANARY)
        self.assertTrue(proof.CANARY.strip().startswith("ps"))


class CanaryPayload(unittest.TestCase):
    def test_the_payload_carries_both_shell_names(self):
        for tool_name in ("bash", "Bash"):
            payload = json.loads(proof._canary_payload(tool_name))
            self.assertEqual(payload["tool_name"], tool_name)
            self.assertEqual(payload["hook_event_name"], "PreToolUse")
            self.assertIn("command", payload["tool_input"])


class Main(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_skip_live_reports_incomplete_and_exits_two(self):
        deny = (
            "printf '%s' "
            '\'{"hookSpecificOutput": {"hookEventName": "PreToolUse", '
            '"permissionDecision": "deny", "permissionDecisionReason": "no"}}\''
        )
        command = write_fake_hook(self.root / "deny.sh", deny)
        plugin = write_plugin(self.root, "Bash|bash", command)
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            code = proof.main(["--plugin-dir", str(plugin), "--skip-live", "--json"])
        self.assertEqual(code, 2)

    def test_json_output_carries_the_stages(self):
        plugin = write_plugin(self.root, "Bash", "/bin/true")
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            proof.main(["--plugin-dir", str(plugin), "--skip-live", "--json"])
        report = json.loads(buf.getvalue())
        self.assertIn("matcher", report["stages"])
        self.assertIn("policy", report["stages"])
        self.assertIn("dispatch", report["stages"])
        self.assertIn("verdict", report)


if __name__ == "__main__":
    unittest.main()
