"""Tests for scripts/muster-guard-live-proof.py.

    python3 scripts/test_muster_guard_live_proof.py

The tool exists because a guard can be installed, enabled, parsed, registered and correct
and still protect nothing, so it separates three things a green check tends to blur: the
matcher, the policy, and the live dispatch.  These tests pin the first two against a
temporary plugin directory and a fake hook command, so no MiniMax session and no installed
plugin is touched.

The live dispatch is pinned against the shapes the MiniMax runtime was measured to emit:
a tool call that ran is `status` 2 with an execution record, and a tool call the runtime
refused is `status` 3 with no execution record at all and the hook's reason as the result
text.  Those fixtures are the runtime's own record, so a test never has to trust prose.

The case that matters most is the one that hid for months: a matcher that covers `Bash`
but not `bash` covers half the shell names, and reporting the whole group as missed (or
as covered) would hide the drift in both directions.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


DENY_JSON = (
    '{"hookSpecificOutput": {"hookEventName": "PreToolUse", '
    '"permissionDecision": "deny", "permissionDecisionReason": "the guard refuses this command"}}'
)


def write_plugin(root: Path, matcher: str, command: str) -> Path:
    """A plugin directory shaped like the installed MiniMax one."""
    return write_plugin_groups(root, [(matcher, command)])


def write_plugin_groups(root: Path, groups) -> Path:
    """A plugin directory with one PreToolUse group per (matcher, command) pair."""
    plugin = root / "fleet-guard"
    (plugin / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plugin / "hooks").mkdir(parents=True, exist_ok=True)
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
                        for matcher, command in groups
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


def write_hook(path: Path, body: str) -> str:
    """A hook that can see the payload it was given."""
    path.write_text("#!/bin/sh\nPAYLOAD=$(cat)\n" + body + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def deny_all_body() -> str:
    """A hook body that denies every payload."""
    return "printf '%s' '" + DENY_JSON + "'"


def deny_body_for_tool(tool_name: str) -> str:
    """A hook body that denies only the given tool name."""
    return deny_body_for_tools(tool_name)


def deny_body_for_tools(*tool_names: str) -> str:
    """A hook body that denies only the given tool names."""
    arms = "".join(
        '  *\'"tool_name": "' + name + "\"'*) printf '%s' '" + DENY_JSON + "' ;;\n"
        for name in tool_names
    )
    return 'case "$PAYLOAD" in\n' + arms + "esac\nexit 0"


def tool_call_event(call: dict) -> str:
    """One `item.completed` event carrying a tool call, as the runtime emits it."""
    return json.dumps(
        {
            "schemaVersion": 1,
            "sequence": 1,
            "type": "item.completed",
            "item": {"id": "call_1", "type": "tool_call", "toolCall": call},
        }
    )


def ran_call(command: str) -> dict:
    """A tool call the runtime ran to completion."""
    return {
        "id": "call_1",
        "name": "bash",
        "status": 2,
        "input": {"command": command},
        "output": {
            "content": [{"type": "text", "text": "(no output)"}],
            "details": {
                "execution": {"status": "succeeded", "reason": "exited", "exitCode": 0},
                "processOutput": {"stdout": "", "stderr": "", "exitCode": 0},
            },
        },
    }


def refused_call(command: str, reason: str) -> dict:
    """A tool call the runtime refused, with the hook's reason as the result text."""
    return {
        "id": "call_1",
        "name": "bash",
        "status": 3,
        "input": {"command": command},
        "output": {"content": [{"type": "text", "text": reason}], "details": {}},
    }


class MatcherSemantics(unittest.TestCase):
    """The matcher stage has to answer exactly as the installer answers."""

    def test_case_sensitive(self):
        covered, missed = proof._covered_names("Bash", ("bash", "Bash"))
        self.assertEqual(covered, ["Bash"])
        self.assertEqual(missed, ["bash"])

    def test_alternation_covers_both_spellings(self):
        covered, missed = proof._covered_names("Bash|bash", ("bash", "Bash"))
        self.assertEqual(sorted(covered), ["Bash", "bash"])
        self.assertEqual(missed, [])

    def test_an_empty_matcher_matches_every_tool(self):
        # The installer treats an empty matcher as match-all.  A tool that called it a
        # miss would tell the owner a fully armed plugin was unprotected.
        covered, missed = proof._covered_names("", ("bash", "Bash"))
        self.assertEqual(sorted(covered), ["Bash", "bash"])
        self.assertEqual(missed, [])

    def test_a_star_matcher_matches_every_tool(self):
        covered, missed = proof._covered_names("*", ("bash", "Bash"))
        self.assertEqual(sorted(covered), ["Bash", "bash"])
        self.assertEqual(missed, [])

    def test_a_null_matcher_matches_every_tool(self):
        covered, missed = proof._covered_names(None, ("bash", "Bash"))
        self.assertEqual(sorted(covered), ["Bash", "bash"])
        self.assertEqual(missed, [])

    def test_a_fragment_does_not_cover_the_whole_name(self):
        # The installer uses `re.fullmatch`, so a matcher of `ash` never matches `bash`.
        covered, missed = proof._covered_names("ash", ("bash",))
        self.assertEqual(covered, [])
        self.assertEqual(missed, ["bash"])

    def test_an_invalid_regex_misses_everything(self):
        covered, missed = proof._covered_names("Bash[", ("bash",))
        self.assertEqual(covered, [])
        self.assertEqual(missed, ["bash"])

    def test_a_non_string_matcher_misses_everything(self):
        covered, missed = proof._covered_names(7, ("bash",))
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

    def test_a_shell_only_matcher_passes(self):
        # A guard that only polices shell tools is a valid configuration, not a failed
        # check.  This tool's policy stage drives a shell canary, so a matcher that
        # covers both shell spellings can prove exactly what this tool measures.
        # Demanding the file spellings here failed a perfectly good plugin.
        plugin = write_plugin(self.root, "bash|Bash", "/bin/true")
        report = proof.read_plugin(plugin)
        result = proof.check_matcher(report)
        self.assertTrue(result["ok"])
        self.assertEqual(result["shell_missed"], [])
        # The file coverage is still reported, as information: a guard that also wants
        # to police file tools needs those names, and the reader should be able to see
        # whether it has them.
        self.assertIn("Read", result["file_missed"])

    def test_no_pretooluse_handler(self):
        plugin = self.root / "empty"
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / "hooks").mkdir(parents=True)
        (plugin / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
        (plugin / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {}}), encoding="utf-8")
        result = proof.check_matcher(proof.read_plugin(plugin))
        self.assertFalse(result["ok"])

    def test_coverage_is_the_union_across_groups(self):
        # A plugin that splits its groups - one for the shell tools, one for the file
        # tools - guards the whole surface.  Demanding one group cover the shell names
        # would report a correctly configured plugin as broken.
        plugin = write_plugin_groups(
            self.root,
            [("bash|Bash", "/bin/true"), ("Read|Write|Edit|read|write|edit", "/bin/true")],
        )
        result = proof.check_matcher(proof.read_plugin(plugin))
        self.assertTrue(result["ok"])
        self.assertEqual(result["shell_missed"], [])
        self.assertEqual(result["file_missed"], [])

    def test_two_groups_can_cover_the_shell_names_between_them(self):
        plugin = write_plugin_groups(self.root, [("bash", "/bin/true"), ("Bash", "/bin/true")])
        result = proof.check_matcher(proof.read_plugin(plugin))
        self.assertTrue(result["ok"])
        self.assertEqual(result["shell_missed"], [])

    def test_the_group_detail_is_reported(self):
        plugin = write_plugin_groups(self.root, [("bash", "/bin/true"), ("Read", "/bin/true")])
        result = proof.check_matcher(proof.read_plugin(plugin))
        self.assertEqual(len(result["groups"]), 2)
        self.assertEqual(result["groups"][0]["matcher"], "bash")
        self.assertIn("Bash", result["groups"][0]["shell_missed"])


class ReadPlugin(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def _plugin_with_hooks(self, hooks_doc):
        plugin = self.root / "broken"
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / "hooks").mkdir(parents=True)
        (plugin / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
        (plugin / "hooks" / "hooks.json").write_text(
            hooks_doc if isinstance(hooks_doc, str) else json.dumps(hooks_doc),
            encoding="utf-8",
        )
        return plugin

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

    def test_a_hooks_document_that_is_not_an_object_is_an_error(self):
        report = proof.read_plugin(self._plugin_with_hooks("[1, 2, 3]"))
        self.assertTrue(report["errors"])
        self.assertTrue(any("hooks.json" in error for error in report["errors"]))
        self.assertEqual(report["hooks"], [])

    def test_a_hooks_document_without_the_hooks_section_is_an_error(self):
        report = proof.read_plugin(self._plugin_with_hooks({"other": {}}))
        self.assertTrue(any("hooks" in error for error in report["errors"]))

    def test_a_hooks_section_that_is_not_an_object_is_an_error(self):
        report = proof.read_plugin(self._plugin_with_hooks({"hooks": []}))
        self.assertTrue(any("hooks.json" in error for error in report["errors"]))

    def test_an_event_that_is_not_an_array_is_an_error(self):
        report = proof.read_plugin(self._plugin_with_hooks({"hooks": {"PreToolUse": {}}}))
        self.assertTrue(any("hooks.json" in error for error in report["errors"]))

    def test_an_entry_that_is_not_an_object_is_an_error(self):
        report = proof.read_plugin(self._plugin_with_hooks({"hooks": {"PreToolUse": ["x"]}}))
        self.assertTrue(any("hooks.json" in error for error in report["errors"]))

    def test_a_non_string_matcher_is_an_error(self):
        report = proof.read_plugin(
            self._plugin_with_hooks({"hooks": {"PreToolUse": [{"matcher": 7, "hooks": []}]}})
        )
        self.assertTrue(any("matcher" in error for error in report["errors"]))

    def test_an_entry_without_a_hooks_array_is_an_error(self):
        report = proof.read_plugin(
            self._plugin_with_hooks({"hooks": {"PreToolUse": [{"matcher": "bash"}]}})
        )
        self.assertTrue(any("hooks" in error for error in report["errors"]))

    def test_a_handler_that_is_not_an_object_is_an_error(self):
        report = proof.read_plugin(
            self._plugin_with_hooks({"hooks": {"PreToolUse": [{"hooks": ["x"]}]}})
        )
        self.assertTrue(any("hooks.json" in error for error in report["errors"]))

    def test_a_non_string_command_is_an_error(self):
        report = proof.read_plugin(
            self._plugin_with_hooks(
                {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": 7}]}]}}
            )
        )
        self.assertTrue(any("command" in error for error in report["errors"]))

    def test_a_negative_timeout_is_an_error(self):
        report = proof.read_plugin(
            self._plugin_with_hooks(
                {
                    "hooks": {
                        "PreToolUse": [
                            {"hooks": [{"type": "command", "command": "/bin/true", "timeout": -1}]}
                        ]
                    }
                }
            )
        )
        self.assertTrue(any("timeout" in error for error in report["errors"]))

    def test_a_boolean_timeout_is_an_error(self):
        report = proof.read_plugin(
            self._plugin_with_hooks(
                {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "hooks": [
                                    {"type": "command", "command": "/bin/true", "timeout": True}
                                ]
                            }
                        ]
                    }
                }
            )
        )
        self.assertTrue(any("timeout" in error for error in report["errors"]))

    def test_a_manifest_that_is_not_an_object_is_an_error(self):
        plugin = self.root / "badmanifest"
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / "hooks").mkdir(parents=True)
        (plugin / ".claude-plugin" / "plugin.json").write_text("[]", encoding="utf-8")
        (plugin / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {}}), encoding="utf-8")
        report = proof.read_plugin(plugin)
        self.assertTrue(any("plugin.json" in error for error in report["errors"]))


class CheckPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_a_denying_hook_passes(self):
        command = write_fake_hook(self.root / "deny.sh", deny_all_body())
        plugin = write_plugin(self.root, "Bash|bash", command)
        result = proof.check_policy(proof.read_plugin(plugin))
        self.assertTrue(result["ok"])
        self.assertEqual(result["missed"], [])

    def test_a_failed_hook_retains_the_terminal_error(self):
        diagnostic = "early frame " * 30 + "terminal interpreter failure"
        command = write_fake_hook(
            self.root / "broken.sh", "printf '%s' '" + diagnostic + "' >&2; exit 127"
        )
        plugin = write_plugin(self.root, "Bash|bash", command)
        result = proof.check_policy(proof.read_plugin(plugin))
        self.assertIsNone(result["ok"])
        self.assertEqual(result["status"], "error")
        self.assertIn("terminal interpreter failure", result["why"])
        self.assertNotIn(diagnostic, result["why"])

    def test_a_crashing_handler_does_not_hide_a_later_denial(self):
        """The verdict must not depend on the order the groups are declared in."""
        crashing = write_fake_hook(self.root / "broken.sh", "exit 127")
        denying = write_fake_hook(self.root / "guard.sh", deny_all_body())
        verdicts = []
        for groups in (
            [("Bash|bash", crashing), ("Bash|bash", denying)],
            [("Bash|bash", denying), ("Bash|bash", crashing)],
        ):
            plugin = write_plugin_groups(self.root / str(len(verdicts)), groups)
            result = proof.check_policy(proof.read_plugin(plugin))
            verdicts.append((result["ok"], result["status"]))
        self.assertEqual(verdicts[0], verdicts[1])
        self.assertTrue(verdicts[0][0])

    def test_an_unstartable_handler_does_not_hide_a_later_denial(self):
        missing = str(self.root / "missing-hook")
        denying = write_fake_hook(self.root / "guard.sh", deny_all_body())
        for groups in (
            [("Bash|bash", missing), ("Bash|bash", denying)],
            [("Bash|bash", denying), ("Bash|bash", missing)],
        ):
            plugin = write_plugin_groups(self.root / str(len(groups[0][1])), groups)
            result = proof.check_policy(proof.read_plugin(plugin))
            self.assertTrue(result["ok"])

    def test_an_allowing_hook_fails(self):
        command = write_fake_hook(self.root / "allow.sh", "exit 0")
        plugin = write_plugin(self.root, "Bash|bash", command)
        result = proof.check_policy(proof.read_plugin(plugin))
        self.assertFalse(result["ok"])
        self.assertEqual(sorted(result["missed"]), ["Bash", "bash"])

    def test_a_hook_that_denies_only_one_spelling_is_not_a_pass(self):
        # The failure this tool exists to catch: the guard denies `Bash` and the runtime
        # delivers `bash`, so the seat runs with no protection at all.
        command = write_hook(self.root / "half.sh", deny_body_for_tool("Bash"))
        plugin = write_plugin(self.root, "Bash|bash", command)
        result = proof.check_policy(proof.read_plugin(plugin))
        self.assertFalse(result["ok"])
        self.assertEqual(result["missed"], ["bash"])

    def test_a_name_no_group_covers_is_reported_as_uncovered(self):
        # Coverage is the matcher stage's question.  The hook script is still run with
        # that payload, so the policy question is answered for every name, and the
        # reader is told the name never reaches the hook.
        command = write_fake_hook(self.root / "deny.sh", deny_all_body())
        plugin = write_plugin(self.root, "Bash", command)
        result = proof.check_policy(proof.read_plugin(plugin))
        self.assertTrue(result["ok"])
        self.assertEqual(result["uncovered"], ["bash"])
        self.assertEqual(result["missed"], [])
        self.assertIn("no PreToolUse group covers bash", result["why"])

    def test_the_policy_stage_runs_the_group_that_covers_the_name(self):
        # Two groups: one for the shell tools, one for the file tools.  The shell name
        # must be run against the shell group, not against the first group in the file.
        shell_command = write_hook(self.root / "shell.sh", deny_body_for_tools("bash", "Bash"))
        file_command = write_fake_hook(self.root / "file.sh", "exit 0")
        plugin = write_plugin_groups(
            self.root, [("Read", file_command), ("bash|Bash", shell_command)]
        )
        result = proof.check_policy(proof.read_plugin(plugin))
        self.assertTrue(result["ok"])

    def test_the_denial_reason_and_its_fingerprint_are_captured(self):
        command = write_fake_hook(self.root / "deny.sh", deny_all_body())
        plugin = write_plugin(self.root, "Bash|bash", command)
        result = proof.check_policy(proof.read_plugin(plugin))
        self.assertEqual(result["denial_reason"], "the guard refuses this command")
        self.assertTrue(result["denial_fingerprint"])
        self.assertIn(result["denial_fingerprint"], result["denial_reason"])

    def test_a_denial_longer_than_the_detail_limit_still_yields_a_fingerprint(self):
        # The fleet guard's real denial is a JSON object of about 670 characters.  If the
        # policy stage truncates the hook's output before parsing it, the JSON is cut in
        # half, the parse fails, the reason comes back empty, the fingerprint is empty,
        # and the dispatch stage can never attribute a refusal to this hook - it would
        # report "refused, but not attributable" and exit 2 forever, for a guard that had
        # just refused.
        long_reason = "the guard refuses this command because " + ("x" * 500)
        payload = json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": long_reason,
                }
            }
        )
        command = write_fake_hook(self.root / "long.sh", "printf '%s' '" + payload + "'")
        plugin = write_plugin(self.root, "Bash|bash", command)
        result = proof.check_policy(proof.read_plugin(plugin))
        self.assertTrue(result["ok"])
        self.assertGreater(len(payload), 400)
        self.assertEqual(result["denial_reason"], long_reason)
        self.assertTrue(result["denial_fingerprint"])
        self.assertIn(result["denial_fingerprint"], result["denial_reason"])
        # The reported detail stays short even though the parsed reason is long.
        self.assertLessEqual(len(result["detail"]), 400)

    def test_a_hook_command_that_is_not_parseable_argv_fails(self):
        plugin = write_plugin(self.root, "Bash|bash", "/bin/true 'unclosed")
        result = proof.check_policy(proof.read_plugin(plugin))
        self.assertFalse(result["ok"])
        self.assertIn("argv", result["why"])

    def test_an_empty_hook_command_fails(self):
        plugin = write_plugin(self.root, "Bash|bash", "   ")
        result = proof.check_policy(proof.read_plugin(plugin))
        self.assertFalse(result["ok"])

    def test_a_hook_that_cannot_run_is_an_error_not_a_failure(self):
        plugin = write_plugin(self.root, "Bash|bash", "/nonexistent/hook")
        result = proof.check_policy(proof.read_plugin(plugin))
        self.assertIsNone(result["ok"])
        self.assertEqual(result["status"], proof.STATUS_ERROR)

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


class HookEnvironment(unittest.TestCase):
    """The policy stage must measure the hook the way the runtime runs it."""

    def test_the_kill_switch_cannot_reach_the_hook(self):
        # The lane guard's own kill switch turns that half of the hook off entirely, and
        # it is owner-only precisely because it changes the hook's answer.  MiniMax gives
        # a hook no such variable, so the policy stage must not either.
        with mock.patch.dict(os.environ, {"FLEET_LANE_GUARD": "off"}, clear=False):
            env = proof._hook_env()
        self.assertNotIn("FLEET_LANE_GUARD", env)

    def test_the_operators_shell_is_not_handed_to_the_hook(self):
        with mock.patch.dict(os.environ, {"AGENT_SEAT": "MM", "SOME_OTHER": "x"}, clear=False):
            env = proof._hook_env()
        self.assertNotIn("AGENT_SEAT", env)
        self.assertNotIn("SOME_OTHER", env)

    def test_the_allowlist_and_the_minimal_path_are_kept(self):
        with mock.patch.dict(os.environ, {"LANG": "en_US.UTF-8"}, clear=False):
            env = proof._hook_env()
        self.assertEqual(env["LANG"], "en_US.UTF-8")
        self.assertEqual(env["PATH"], proof.MINIMAL_PATH)
        self.assertTrue(env["HOME"])

    def test_the_policy_stage_runs_the_hook_with_that_environment(self):
        # The finding is about the call, not about a helper: the subprocess that runs the
        # hook has to carry the scrubbed environment.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            command = write_fake_hook(root / "deny.sh", deny_all_body())
            plugin = write_plugin(root, "Bash|bash", command)
            seen = {}
            real_run = subprocess.run

            def spy(cmd, **kwargs):
                seen.update(kwargs)
                return real_run(cmd, **kwargs)

            with mock.patch.object(proof.subprocess, "run", spy):
                result = proof.check_policy(proof.read_plugin(plugin))
            self.assertTrue(result["ok"])
        self.assertIn("env", seen)
        self.assertNotIn("FLEET_LANE_GUARD", seen["env"])
        self.assertEqual(seen["env"]["PATH"], proof.MINIMAL_PATH)


class CountingText(str):
    """A str that counts the line scans made against it."""

    def __new__(cls, value):
        obj = super().__new__(cls, value)
        obj.finds = 0
        return obj

    def find(self, *args, **kwargs):
        self.finds += 1
        return super().find(*args, **kwargs)


class SessionLineScan(unittest.TestCase):
    """The transcript is the runtime's, so the scan has to be bounded while it reads it."""

    def test_the_transcript_is_read_one_line_at_a_time(self):
        # The event cap has to bound the work, so the scan may not materialise every
        # line before it counts them: a 16MB transcript of one-character lines would
        # become millions of string objects, and the cap would only run afterwards.
        text = CountingText("\n".join("x" for _ in range(1000)))
        stream = proof._iter_session_lines(text)
        self.assertEqual(next(stream), "x")
        self.assertEqual(next(stream), "x")
        self.assertEqual(text.finds, 2)

    def test_the_event_cap_aborts_the_scan(self):
        with mock.patch.object(proof, "MAX_SESSION_EVENTS", 3):
            text = "\n".join("x" for _ in range(10))
            with self.assertRaises(ValueError) as ctx:
                list(proof._iter_session_lines(text))
        self.assertIn("event limit", str(ctx.exception))

    def test_a_transcript_over_the_event_cap_is_an_error_not_a_crash(self):
        with (
            mock.patch.object(proof, "MAX_SESSION_EVENTS", 2),
            self.assertRaises(ValueError) as ctx,
        ):
            proof._completed_tool_calls("\n".join("x" for _ in range(10)))
        self.assertIn("event limit", str(ctx.exception))

    def test_a_transcript_over_the_byte_cap_is_an_error(self):
        with (
            mock.patch.object(proof, "MAX_SESSION_BYTES", 10),
            self.assertRaises(ValueError) as ctx,
        ):
            proof._completed_tool_calls("x" * 100)
        self.assertIn("byte limit", str(ctx.exception))

    def test_a_trailing_newline_is_not_an_extra_event(self):
        self.assertEqual(list(proof._iter_session_lines("a\nb\n")), ["a", "b"])


class CompletedToolCalls(unittest.TestCase):
    def test_the_runtime_record_is_parsed(self):
        stream = "\n".join(
            [
                tool_call_event(ran_call("ps > /dev/null")),
                json.dumps({"type": "item.started", "item": {"type": "tool_call"}}),
                json.dumps({"type": "exec.completed", "result": {}}),
                "not json at all",
            ]
        )
        calls = proof._completed_tool_calls(stream)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["name"], "bash")

    def test_a_ran_call_executed(self):
        self.assertTrue(proof._call_executed(ran_call("ps > /dev/null")))

    def test_a_refused_call_did_not_execute(self):
        self.assertFalse(proof._call_executed(refused_call("ps > /dev/null", "no")))

    def test_a_refused_call_is_recognised_only_with_the_hook_fingerprint(self):
        call = refused_call("ps > /dev/null", "the guard refuses this command")
        self.assertTrue(proof._call_was_denied(call, "the guard refuses this command"))
        self.assertFalse(proof._call_was_denied(call, "some other hook's reason"))

    def test_a_refusal_by_another_layer_is_not_attributed_to_the_guard(self):
        # The runtime has other layers that can refuse a command.  A refusal by any of
        # them says nothing about this guard, so it must not read as a proof.
        call = refused_call("ps > /dev/null", "Blocked: dangerous removal target.")
        self.assertFalse(proof._call_was_denied(call, "the guard refuses this command"))

    def test_a_ran_call_is_never_a_denial(self):
        call = ran_call("ps > /dev/null")
        self.assertFalse(proof._call_was_denied(call, ""))


class DispatchVerdict(unittest.TestCase):
    def test_a_canary_that_ran_is_a_negative_proof(self):
        stream = tool_call_event(ran_call("ps > /dev/null"))
        result = proof._dispatch_verdict(stream, 0, "some fingerprint")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], proof.STATUS_FAIL)

    def test_a_session_that_denied_and_also_ran_does_not_claim_the_hook_never_fired(self):
        # The guard refused one attempt and the model ran a variant anyway.  That is
        # still a failure - the canary got through - but the old message said "the hook
        # never fired" in a transcript that carries the hook's own refusal, and sent
        # the operator to re-install a guard that was demonstrably armed.
        stream = "\n".join(
            [
                tool_call_event(refused_call("ps > /dev/null", "the guard refuses this")),
                tool_call_event({**ran_call("ps aux > /dev/null"), "id": "call_2"}),
            ]
        )
        result = proof._dispatch_verdict(stream, 0, "the guard refuses this")
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], proof.STATUS_FAIL)
        self.assertNotIn("never fired", result["why"])
        self.assertIn("refused 1 of 2", result["why"])

    def test_an_unreadable_session_stream_is_an_error_not_a_crash(self):
        # A toolCall that is a string used to raise AttributeError out of the verdict,
        # taking the whole tool down with a traceback instead of a report.
        stream = json.dumps(
            {"type": "item.completed", "item": {"type": "tool_call", "toolCall": "nope"}}
        )
        result = proof._dispatch_verdict(stream, 0, "fingerprint")
        self.assertIsNone(result["ok"])
        self.assertEqual(result["status"], proof.STATUS_ERROR)

    def test_a_deeply_nested_event_is_an_error_not_a_crash(self):
        # json.loads has no depth bound of its own, so a session that nests an event
        # past the interpreter's stack limit raised RecursionError out of the verdict.
        deep = '{"type": "item.completed", "pad": ' + "[" * 50_000 + "]" * 50_000 + "}"
        result = proof._dispatch_verdict(deep, 0, "fingerprint")
        self.assertIsNone(result["ok"])
        self.assertEqual(result["status"], proof.STATUS_ERROR)

    def test_a_canary_refused_by_the_installed_hook_is_a_proof(self):
        stream = tool_call_event(refused_call("ps > /dev/null", "the guard refuses this"))
        result = proof._dispatch_verdict(stream, 0, "the guard refuses this")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], proof.STATUS_OK)

    def test_a_refusal_that_cannot_be_attributed_is_not_a_proof(self):
        stream = tool_call_event(refused_call("ps > /dev/null", "Blocked: something else"))
        result = proof._dispatch_verdict(stream, 0, "the guard refuses this")
        self.assertIsNone(result["ok"])
        self.assertEqual(result["status"], proof.STATUS_ERROR)

    def test_a_session_that_never_ran_the_canary_is_an_error(self):
        stream = tool_call_event(ran_call("echo hello"))
        result = proof._dispatch_verdict(stream, 0, "fingerprint")
        self.assertIsNone(result["ok"])
        self.assertEqual(result["status"], proof.STATUS_ERROR)

    def test_a_command_that_only_mentions_the_canary_is_not_the_canary(self):
        # A model asked to run `ps > /dev/null` will sometimes run
        # `command -v ps; echo hi > /dev/null` instead.  That command ran, so counting
        # it as the canary reports a guard that had just refused the canary as a guard
        # that let it run.  The refusal of the real canary is the proof.
        stream = "\n".join(
            [
                tool_call_event(refused_call("ps > /dev/null", "the guard refuses this")),
                tool_call_event(ran_call('command -v ps; echo hi > /dev/null; echo "x=$?"')),
            ]
        )
        result = proof._dispatch_verdict(stream, 0, "the guard refuses this")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], proof.STATUS_OK)

    def test_the_canary_is_recognised_with_a_wrapped_exit_code_echo(self):
        stream = tool_call_event(refused_call('ps > /dev/null; echo "exit=$?"', "refused"))
        result = proof._dispatch_verdict(stream, 0, "refused")
        self.assertTrue(result["ok"])

    def test_the_canary_is_recognised_without_spaces_around_the_redirect(self):
        stream = tool_call_event(refused_call("ps>/dev/null", "refused"))
        result = proof._dispatch_verdict(stream, 0, "refused")
        self.assertTrue(result["ok"])

    def test_the_canary_is_recognised_with_an_ampersand_redirect(self):
        # `&>` is a redirect operator in bash, not a command separator, and the guard
        # denies it exactly as it denies `ps > /dev/null`.  Reading it as a separator
        # made the tool report a live refusal as "the canary never ran" - the one answer
        # that tells the operator the guard was never exercised.
        for command in ("ps &>/dev/null", "ps &> /dev/null", "ps -ef &>/dev/null"):
            with self.subTest(command=command):
                stream = tool_call_event(refused_call(command, "refused"))
                result = proof._dispatch_verdict(stream, 0, "refused")
                self.assertTrue(result["ok"])
                self.assertEqual(result["status"], proof.STATUS_OK)

    def test_the_canary_is_recognised_with_a_duplicated_stderr_redirect(self):
        # `2>&1` before the redirect is a descriptor duplication, not a separator.
        stream = tool_call_event(refused_call("ps 2>&1 > /dev/null", "refused"))
        result = proof._dispatch_verdict(stream, 0, "refused")
        self.assertTrue(result["ok"])

    def test_an_ampersand_separator_is_still_not_the_canary(self):
        # Widening the redirect spellings must not widen the shape: `&&` and a trailing
        # `&` still separate commands, so a command that merely mentions `ps` is not
        # the canary.
        for command in ("ps && echo x > /dev/null", "ps & echo x > /dev/null"):
            with self.subTest(command=command):
                stream = tool_call_event(ran_call(command))
                result = proof._dispatch_verdict(stream, 0, "refused")
                self.assertIsNone(result["ok"])
                self.assertEqual(result["status"], proof.STATUS_ERROR)

    def test_the_tool_names_the_runtime_used_are_reported(self):
        stream = "\n".join(
            [
                tool_call_event(ran_call("ps > /dev/null")),
                tool_call_event({**ran_call("echo hi"), "name": "read"}),
            ]
        )
        result = proof._dispatch_verdict(stream, 0, "fingerprint")
        self.assertIn("bash", result["tool_names"])


class CheckDispatch(unittest.TestCase):
    """Stage 3 must not report a runtime failure as a negative proof.

    A check that could not run has proved nothing either way.  Reporting it as "the
    guard did not fire" is worse than useless: it tells the operator to fix the guard
    when the guard was never exercised, and it burns the exit code that says "could
    not run" (2) on a false statement (1).
    """

    def test_a_missing_mcode_is_an_error_not_a_failure(self):
        with mock.patch.object(proof.shutil, "which", return_value=None):
            result = proof.check_dispatch("model", 5)
        self.assertIsNone(result["ok"])
        self.assertEqual(result["status"], proof.STATUS_ERROR)
        self.assertIn("mcode", result["why"])

    def test_a_session_that_cannot_start_is_an_error_not_a_failure(self):
        with (
            mock.patch.object(proof.shutil, "which", return_value="/usr/bin/mcode"),
            mock.patch.object(proof.subprocess, "Popen", side_effect=OSError("no such file")),
        ):
            result = proof.check_dispatch("model", 5)
        self.assertIsNone(result["ok"])
        self.assertEqual(result["status"], proof.STATUS_ERROR)

    def test_a_timed_out_session_is_an_error_not_a_failure(self):
        with (
            mock.patch.object(proof.shutil, "which", return_value="/usr/bin/mcode"),
            mock.patch.object(proof.subprocess, "Popen") as popen,
            mock.patch.object(proof, "_kill_group") as kill_group,
        ):
            popen.return_value.communicate.side_effect = proof.subprocess.TimeoutExpired("mcode", 5)
            popen.return_value.pid = 4242
            result = proof.check_dispatch("model", 5)
        self.assertIsNone(result["ok"])
        self.assertEqual(result["status"], proof.STATUS_ERROR)
        self.assertIn("timed out", result["why"])
        # A timeout has to take the session's process tree with it.
        self.assertTrue(kill_group.called)

    def test_a_timeout_kills_the_whole_process_group(self):
        # Killing only the direct child leaves the agent process tree alive with tools
        # enabled after this tool has exited.
        with (
            mock.patch.object(proof.shutil, "which", return_value="/usr/bin/mcode"),
            mock.patch.object(proof.subprocess, "Popen") as popen,
            mock.patch.object(proof.os, "getpgid", return_value=4242),
            mock.patch.object(proof.os, "killpg") as killpg,
        ):
            popen.return_value.communicate.side_effect = proof.subprocess.TimeoutExpired("mcode", 5)
            popen.return_value.pid = 4242
            proof.check_dispatch("model", 5)
        self.assertTrue(killpg.called)

    def test_an_interrupt_kills_the_session_process_group(self):
        # SIGINT and KeyboardInterrupt arrive as a BaseException, not a TimeoutExpired.
        # The old code only cleaned up on the timeout path, so Ctrl-C left the live
        # session's whole process tree running with its tools enabled.
        with (
            mock.patch.object(proof.shutil, "which", return_value="/usr/bin/mcode"),
            mock.patch.object(proof.subprocess, "Popen") as popen,
            mock.patch.object(proof, "_kill_group") as kill_group,
        ):
            popen.return_value.communicate.side_effect = KeyboardInterrupt
            popen.return_value.pid = 4242
            with self.assertRaises(KeyboardInterrupt):
                proof.check_dispatch("model", 5)
        self.assertTrue(kill_group.called)

    def test_the_session_runs_in_its_own_directory_with_a_step_cap(self):
        # `--permission off` means "never ask", not "hold no tools", so the session can
        # still write.  It has to run somewhere inert, or a proof can edit the
        # operator's own checkout.
        with (
            mock.patch.object(proof.shutil, "which", return_value="/usr/bin/mcode"),
            mock.patch.object(proof.subprocess, "Popen") as popen,
        ):
            popen.return_value.communicate.return_value = ("", "")
            popen.return_value.returncode = 0
            proof.check_dispatch("model", 5)
        cmd = popen.call_args[0][0]
        self.assertEqual(cmd[cmd.index("--max-steps") + 1], "8")
        cwd = Path(cmd[cmd.index("--cwd") + 1])
        # The temp directory is removed when the session ends, so it is gone by now;
        # what matters is that it was somewhere other than the operator's checkout.
        self.assertNotEqual(str(cwd), str(Path.cwd()))
        self.assertTrue(str(cwd).startswith(tempfile.gettempdir()))

    def test_the_prompt_goes_on_stdin_and_the_model_is_one_token(self):
        with (
            mock.patch.object(proof.shutil, "which", return_value="/usr/bin/mcode"),
            mock.patch.object(proof.subprocess, "Popen") as popen,
        ):
            popen.return_value.communicate.return_value = ("", "")
            popen.return_value.returncode = 0
            proof.check_dispatch("-evil --permission ask", 5)
        cmd = popen.call_args[0][0]
        self.assertIn("--input", cmd)
        self.assertEqual(cmd[cmd.index("--input") + 1], "-")
        model_tokens = [token for token in cmd if token.startswith("--model")]
        self.assertEqual(model_tokens, ["--model=-evil --permission ask"])
        self.assertTrue(popen.call_args[1]["text"])
        self.assertTrue(popen.call_args[1]["start_new_session"])
        self.assertIn(proof.CANARY, popen.return_value.communicate.call_args[1]["input"])

    def test_a_session_with_no_tool_call_record_is_an_error(self):
        # A mocked session that recorded no tool call never exercised the hook, so the
        # check could not run.  That is not a negative proof of anything.
        with (
            mock.patch.object(proof.shutil, "which", return_value="/usr/bin/mcode"),
            mock.patch.object(proof.subprocess, "Popen") as popen,
        ):
            popen.return_value.communicate.return_value = ("", "")
            popen.return_value.returncode = 0
            result = proof.check_dispatch("model", 5, "fingerprint")
        self.assertIsNone(result["ok"])
        self.assertEqual(result["status"], proof.STATUS_ERROR)


class Main(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_skip_live_reports_incomplete_and_exits_two(self):
        command = write_fake_hook(self.root / "deny.sh", deny_all_body())
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

    def test_an_unreadable_plugin_exits_two_not_one(self):
        # The tool could not evaluate the plugin, so it says nothing about the guard.
        # The documented contract is exit 2 for "could not run the check at all", and
        # exit 1 is a statement that the guard was tested and did not fire.
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = proof.main(["--plugin-dir", str(self.root / "absent"), "--skip-live", "--json"])
        self.assertEqual(code, 2)
        report = json.loads(buf.getvalue())
        self.assertEqual(report["stages"]["dispatch"]["status"], proof.STATUS_ERROR)
        self.assertTrue(report["verdict"].startswith("INCOMPLETE"))

    def test_a_malformed_hooks_document_exits_two_not_one(self):
        import contextlib
        import io

        plugin = self.root / "broken"
        (plugin / ".claude-plugin").mkdir(parents=True)
        (plugin / "hooks").mkdir(parents=True)
        (plugin / ".claude-plugin" / "plugin.json").write_text("{}", encoding="utf-8")
        (plugin / "hooks" / "hooks.json").write_text('{"hooks": 7}', encoding="utf-8")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = proof.main(["--plugin-dir", str(plugin), "--skip-live", "--json"])
        self.assertEqual(code, 2)
        report = json.loads(buf.getvalue())
        self.assertTrue(report["verdict"].startswith("INCOMPLETE"))

    def test_an_empty_model_is_refused(self):
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = proof.main(["--model", "", "--skip-live"])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
