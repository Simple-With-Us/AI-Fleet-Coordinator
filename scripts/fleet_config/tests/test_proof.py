"""The L1 sentinel probe and the L2 headless canary.

The L1 probe is proved against a fake hook script written into a temporary
directory, so no test touches the real home, a real repo or the installed hook.  The
fake hook answers in the platform's own deny shapes, so the probe's deny detection
is exercised for real:  a Claude-shaped deny, the plain-text exit-2 deny, an allow, a
crash, an unreadable answer and a hang.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from fleet_config import proof
from fleet_config.checks import CheckContext, CheckError
from fleet_config.types import (
    CheckSpec,
    CheckState,
    CheckType,
    ConfigKind,
    ExpectedState,
    ObservedState,
    ProofLevel,
    ReasonCode,
    Rule,
)

from ._fake import make_private_dir

# The real Popen,  captured before any test patches it,  so a stand-in that starts a
# real child does not call itself.
_REAL_POPEN = subprocess.Popen

# The fake hook.  It reads the payload the probe sends, takes the destination the
# payload names, and answers in the shape the mode names.  It never writes anything
# except the record file it is told to write.
FAKE_HOOK = """#!/usr/bin/env python3
import json
import os
import sys
import time

mode = sys.argv[1]
record = sys.argv[2] if len(sys.argv) > 2 else ""
raw = sys.stdin.read()
if record:
    with open(record, "w", encoding="utf-8") as handle:
        handle.write(raw)
try:
    payload = json.loads(raw or "{}")
except ValueError:
    payload = {}
command = ""
tool_input = payload.get("tool_input")
if isinstance(tool_input, dict):
    command = str(tool_input.get("command") or "")
if not command:
    command = str(payload.get("command") or "")
destination = command.split()[-1] if command else ""
reason = "Blocked: fleet-repo checkouts are not allowed in temp directories; " \\
         "this command would put a clone at %s." % destination

if mode == "deny":
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))
    sys.exit(0)
if mode == "allow":
    sys.exit(0)
if mode == "wrong-reason":
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": "deny",
                                             "permissionDecisionReason": "Blocked: something else."}}))
    sys.exit(0)
if mode == "exit2":
    sys.stderr.write(reason + "\\n")
    sys.exit(2)
if mode == "exit2-empty":
    sys.exit(2)
if mode == "garbage":
    sys.stdout.write("this is not json\\n")
    sys.exit(0)
if mode == "crash":
    sys.exit(9)
if mode == "sleep":
    time.sleep(30)
sys.exit(0)
"""


def make_rule(
    rule_id: str = "test-rule",
    proof_level: ProofLevel = ProofLevel.L1,
    collector: str = "mac",
) -> Rule:
    """A rule for the result tests.  It names no path and no secret."""
    from fleet_config.types import Collector

    return Rule(
        id=rule_id,
        kind=ConfigKind.HOOKS,
        platforms=("claude",),
        check=CheckSpec(
            type=CheckType.HOOK_REGISTERED, target="~/.claude/settings.json", detail="PreToolUse"
        ),
        expected=ExpectedState(description="the lane guard is registered"),
        proof_level=proof_level,
        owner_seat="OPENCODE",
        autofix_allowed=False,
        seat="OPENCODE",
        collector=Collector(collector),
        ttl_seconds=900,
    )


class FakeHookCase(unittest.TestCase):
    """A temp dir with a fake hook in it, and a context pointed at that dir."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="fleet-config-test-")
        self.addCleanup(self.tmp.cleanup)
        self.home = self.tmp.name
        self.hook_path = os.path.join(self.home, "fake-hook")
        with open(self.hook_path, "w", encoding="utf-8") as handle:
            handle.write(FAKE_HOOK)
        os.chmod(self.hook_path, os.stat(self.hook_path).st_mode | stat.S_IXUSR)
        self.record = os.path.join(self.home, "payload.json")
        self.ctx = CheckContext(
            home=self.home,
            now=datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc),
            timeout=10,
        )

    def hook(self, mode: str) -> str:
        """The command that runs the fake hook in one mode."""
        return "%s %s %s" % (
            shlex.quote(self.hook_path),
            shlex.quote(mode),
            shlex.quote(self.record),
        )

    def recorded_payload(self) -> dict:
        """The payload the fake hook received."""
        with open(self.record, "r", encoding="utf-8") as handle:
            return json.load(handle)


class L1DenyTests(FakeHookCase):
    """A hook that denies correctly is a pass;  every other answer fails closed."""

    def test_a_deny_naming_the_sentinel_is_a_pass(self):
        outcome = proof.run_l1_probe("claude", self.ctx, hook_command=self.hook("deny"))
        self.assertTrue(outcome.passed)
        self.assertIs(outcome.state, CheckState.PASS)
        self.assertIs(outcome.reason_code, ReasonCode.OK)
        self.assertIs(outcome.achieved, ProofLevel.L1)
        self.assertIs(outcome.level, ProofLevel.L1)

    def test_an_allow_is_drift(self):
        outcome = proof.run_l1_probe("claude", self.ctx, hook_command=self.hook("allow"))
        self.assertFalse(outcome.passed)
        self.assertIs(outcome.state, CheckState.DRIFT)
        self.assertIs(outcome.reason_code, ReasonCode.PROOF_FAILED)

    def test_a_deny_that_does_not_name_the_sentinel_is_drift(self):
        outcome = proof.run_l1_probe("claude", self.ctx, hook_command=self.hook("wrong-reason"))
        self.assertIs(outcome.state, CheckState.DRIFT)
        self.assertIs(outcome.reason_code, ReasonCode.PROOF_FAILED)
        self.assertIn("sentinel", outcome.reason)

    def test_an_unreadable_answer_is_unknown(self):
        outcome = proof.run_l1_probe("claude", self.ctx, hook_command=self.hook("garbage"))
        self.assertIs(outcome.state, CheckState.UNKNOWN)
        self.assertIs(outcome.reason_code, ReasonCode.UNPARSEABLE)

    def test_a_crash_is_unknown(self):
        outcome = proof.run_l1_probe("claude", self.ctx, hook_command=self.hook("crash"))
        self.assertIs(outcome.state, CheckState.UNKNOWN)
        self.assertIs(outcome.reason_code, ReasonCode.ERROR)

    def test_a_missing_hook_is_unknown(self):
        outcome = proof.run_l1_probe(
            "claude",
            self.ctx,
            hook_command="%s --format claude"
            % shlex.quote(os.path.join(self.home, "no-such-hook")),
        )
        self.assertIs(outcome.state, CheckState.UNKNOWN)
        self.assertIs(outcome.reason_code, ReasonCode.ERROR)

    def test_the_exit2_contract_is_a_deny(self):
        outcome = proof.run_l1_probe("claude", self.ctx, hook_command=self.hook("exit2"))
        self.assertTrue(outcome.passed)
        self.assertIs(outcome.state, CheckState.PASS)

    def test_an_exit2_without_a_reason_is_not_a_proven_deny(self):
        outcome = proof.run_l1_probe("claude", self.ctx, hook_command=self.hook("exit2-empty"))
        self.assertFalse(outcome.passed)
        self.assertIs(outcome.state, CheckState.DRIFT)

    def test_the_kimi_format_uses_the_exit2_contract(self):
        outcome = proof.run_l1_probe("kimi", self.ctx, hook_command=self.hook("exit2"))
        self.assertTrue(outcome.passed)
        self.assertIs(outcome.state, CheckState.PASS)

    def test_a_platform_without_an_adapter_is_unsupported(self):
        outcome = proof.run_l1_probe("no-such-platform", self.ctx, hook_command=self.hook("deny"))
        self.assertIs(outcome.state, CheckState.UNSUPPORTED)
        self.assertIs(outcome.reason_code, ReasonCode.UNSUPPORTED)

    def test_the_guard_off_switch_is_named_in_the_reason(self):
        ctx = CheckContext(
            home=self.home,
            now=datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc),
            timeout=10,
            env={"FLEET_LANE_GUARD": "off"},
        )
        outcome = proof.run_l1_probe("claude", ctx, hook_command=self.hook("allow"))
        self.assertIs(outcome.state, CheckState.DRIFT)
        self.assertIn("FLEET_LANE_GUARD", outcome.reason)


class L1TimeoutTests(FakeHookCase):
    """A probe that overruns its budget is a failed proof, never a pass."""

    def test_a_hanging_hook_times_out(self):
        ctx = CheckContext(
            home=self.home,
            now=datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc),
            timeout=1,
        )
        outcome = proof.run_l1_probe("claude", ctx, hook_command=self.hook("sleep"))
        self.assertFalse(outcome.passed)
        self.assertIs(outcome.state, CheckState.UNKNOWN)
        self.assertIs(outcome.reason_code, ReasonCode.TIMEOUT)

    def test_the_timeout_argument_overrides_the_context(self):
        ctx = CheckContext(
            home=self.home,
            now=datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc),
            timeout=600,
        )
        outcome = proof.run_l1_probe("claude", ctx, hook_command=self.hook("sleep"), timeout=1)
        self.assertIs(outcome.reason_code, ReasonCode.TIMEOUT)


class L1SafetyTests(FakeHookCase):
    """The probe must never write anything, and must never aim at a real path."""

    def test_the_probe_writes_nothing(self):
        outcome = proof.run_l1_probe("claude", self.ctx, hook_command=self.hook("allow"))
        self.assertIs(outcome.state, CheckState.DRIFT)
        payload = self.recorded_payload()
        destination = payload["tool_input"]["command"].split()[-1]
        self.assertFalse(os.path.exists(destination))
        self.assertFalse(os.path.exists(os.path.dirname(destination)))

    def test_the_payload_names_a_source_that_does_not_exist(self):
        proof.run_l1_probe("claude", self.ctx, hook_command=self.hook("allow"))
        payload = self.recorded_payload()
        source = payload["tool_input"]["command"].split()[-2]
        self.assertFalse(os.path.exists(source))
        self.assertTrue(source.endswith(proof.SENTINEL_SOURCE_NAME))

    def test_the_destination_is_inside_the_temporary_directory(self):
        proof.run_l1_probe("claude", self.ctx, hook_command=self.hook("allow"))
        payload = self.recorded_payload()
        destination = payload["tool_input"]["command"].split()[-1]
        temp = os.path.realpath(tempfile.gettempdir())
        self.assertTrue(os.path.realpath(destination).startswith(temp + os.sep))

    def test_the_payload_is_the_claude_shape(self):
        adapter = proof.PLATFORM_ADAPTERS["claude"]
        payload = proof.build_payload(adapter, "git clone src dst", "/tmp/work")
        self.assertEqual(payload["hook_event_name"], "PreToolUse")
        self.assertEqual(payload["tool_name"], "Bash")
        self.assertIn("command", payload["tool_input"])
        self.assertIn("cwd", payload)

    def test_the_cursor_payload_is_the_cursor_shape(self):
        adapter = proof.PLATFORM_ADAPTERS["cursor"]
        payload = proof.build_payload(adapter, "git clone src dst", "/tmp/work")
        self.assertEqual(payload["hook_event_name"], "beforeShellExecution")
        self.assertIn("command", payload)
        self.assertIn("workspace_roots", payload)

    def test_the_antigravity_payload_is_the_antigravity_shape(self):
        adapter = proof.PLATFORM_ADAPTERS["antigravity"]
        payload = proof.build_payload(adapter, "git clone src dst", "/tmp/work")
        self.assertEqual(payload["tool_name"], "run_command")
        self.assertIn("CommandLine", payload["tool_input"])

    def test_the_minimax_shell_natives_cover_both_spellings(self):
        self.assertEqual(proof.NATIVE_SHELL_TOOL_NAMES["minimax"], ("bash", "Bash"))

    def test_the_minimax_probe_denies_both_shell_spellings(self):
        outcome = proof.run_l1_probe("minimax", self.ctx, hook_command=self.hook("deny"))
        self.assertTrue(outcome.passed)
        self.assertIs(outcome.state, CheckState.PASS)

    def test_the_file_payload_carries_its_path_field(self):
        adapter = proof.PLATFORM_ADAPTERS["claude"]
        for tool_name, path_field in proof.NATIVE_FILE_TOOLS["claude"]:
            payload = proof.build_file_payload(
                adapter, "/tmp/sentinel/dst", "/tmp/work", tool_name, path_field
            )
            self.assertEqual(payload["tool_name"], tool_name)
            self.assertEqual(payload["tool_input"][path_field], "/tmp/sentinel/dst")

    def test_the_file_tools_are_per_platform(self):
        """A platform is never failed for a tool name its runtime never delivers.

        The MiniMax runtime delivers its native ``write`` and ``edit``,  not the
        capitalised ``Write`` and ``Edit`` of the claude-shaped runtimes,  so the
        capitalised pair must not be probed against it.
        """
        self.assertEqual(
            proof.NATIVE_FILE_TOOLS["minimax"], (("write", "path"), ("edit", "file_path"))
        )
        self.assertNotIn("Write", [name for name, _ in proof.NATIVE_FILE_TOOLS["minimax"]])
        self.assertEqual(proof.NATIVE_FILE_TOOLS["muse"], ())
        self.assertEqual(proof.NATIVE_FILE_TOOLS["cursor"], ())

    def test_the_minimax_edit_tool_carries_the_file_path_field(self):
        self.assertIn(("edit", "file_path"), proof.NATIVE_FILE_TOOLS["minimax"])


class HookResolutionTests(FakeHookCase):
    """The hook under test is the installed one, read from the platform's config."""

    def write_config(self, relative: str, document: dict) -> str:
        """Write a hook config document under the temporary home and return its path."""
        path = os.path.join(self.home, relative)
        make_private_dir(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        return path

    def test_resolve_reads_the_platforms_own_config(self):
        path = self.write_config(
            ".claude/settings.json",
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/opt/lane-tools/lane-guard-hook --format claude",
                                }
                            ],
                        }
                    ]
                }
            },
        )
        self.assertEqual(
            proof.resolve_hook_command("claude", self.ctx),
            "/opt/lane-tools/lane-guard-hook --format claude",
        )
        self.assertTrue(os.path.isfile(path))

    def test_resolve_returns_none_when_no_guard_hook_is_named(self):
        self.write_config(".claude/settings.json", {"hooks": {"PreToolUse": []}})
        self.assertIsNone(proof.resolve_hook_command("claude", self.ctx))

    def test_resolve_returns_none_when_there_is_no_config(self):
        self.assertIsNone(proof.resolve_hook_command("claude", self.ctx))

    def test_a_command_outside_a_hook_entry_is_not_a_command(self):
        self.write_config(
            ".claude/settings.json",
            {
                "sandbox": {
                    "commands": [{"command": "/opt/lane-tools/lane-guard-hook --format claude"}]
                },
                "hooks": {"PreToolUse": []},
            },
        )
        self.assertIsNone(proof.resolve_hook_command("claude", self.ctx))

    def test_an_oversized_config_aborts_naming_the_platform_and_the_path(self):
        path = os.path.join(self.home, ".claude", "settings.json")
        make_private_dir(os.path.dirname(path))
        with open(path, "wb") as handle:
            handle.write(b"{" + b" " * proof.MAX_CONFIG_BYTES + b"}")
        with self.assertRaises(CheckError) as caught:
            proof.resolve_hook_command("claude", self.ctx)
        message = str(caught.exception)
        self.assertIn("claude", message)
        self.assertIn(path, message)

    def test_a_document_nested_past_the_depth_limit_aborts(self):
        node: dict[str, object] = {"command": "/opt/lane-tools/lane-guard-hook"}
        for _ in range(proof.MAX_CONFIG_DEPTH + 1):
            node = {"hooks": node}
        self.write_config(".claude/settings.json", {"hooks": {"PreToolUse": node}})
        with self.assertRaises(CheckError):
            proof.resolve_hook_command("claude", self.ctx)

    def test_a_large_unrelated_section_does_not_abort_the_read(self):
        """Only the section the platform reads is bounded, so a big permissions list is fine."""
        os.makedirs(os.path.join(self.home, ".claude"))
        path = os.path.join(self.home, ".claude", "settings.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "permissions": {
                        "allow": [
                            "Bash(rule %d)" % index for index in range(proof.MAX_CONFIG_ELEMENTS)
                        ]
                    },
                    "hooks": {
                        "PreToolUse": [
                            {
                                "matcher": "Bash",
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "/opt/lane-tools/lane-guard-hook",
                                    }
                                ],
                            }
                        ]
                    },
                },
                handle,
            )
        self.assertEqual(
            proof.resolve_hook_command("claude", self.ctx),
            "/opt/lane-tools/lane-guard-hook",
        )

    def test_a_hooks_section_over_the_element_limit_still_aborts(self):
        os.makedirs(os.path.join(self.home, ".claude"))
        path = os.path.join(self.home, ".claude", "settings.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "hooks": {
                        "PreToolUse": [
                            {"matcher": "Bash(%d)" % index}
                            for index in range(proof.MAX_CONFIG_ELEMENTS + 1)
                        ]
                    }
                },
                handle,
            )
        with self.assertRaises(CheckError):
            proof.resolve_hook_command("claude", self.ctx)

    def test_a_named_group_over_the_element_limit_still_aborts(self):
        os.makedirs(os.path.join(self.home, ".gemini", "config"))
        path = os.path.join(self.home, ".gemini", "config", "hooks.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "fleet-lane-guard": {
                        "PreToolUse": [
                            {"matcher": "run_command(%d)" % index}
                            for index in range(proof.MAX_CONFIG_ELEMENTS + 1)
                        ]
                    }
                },
                handle,
            )
        with self.assertRaises(CheckError):
            proof.resolve_hook_command("antigravity", self.ctx)

    def test_a_hook_list_that_is_not_a_list_aborts(self):
        self.write_config(
            ".claude/settings.json",
            {"hooks": {"PreToolUse": {"command": "/opt/lane-tools/lane-guard-hook"}}},
        )
        with self.assertRaises(CheckError):
            proof.resolve_hook_command("claude", self.ctx)

    def test_a_named_group_config_still_resolves(self):
        self.write_config(
            ".gemini/config/hooks.json",
            {
                "fleet-lane-guard": {
                    "PreToolUse": [
                        {"command": "/opt/lane-tools/fleet-guard-hook --format antigravity"}
                    ]
                }
            },
        )
        self.assertEqual(
            proof.resolve_hook_command("antigravity", self.ctx),
            "/opt/lane-tools/fleet-guard-hook --format antigravity",
        )

    def test_the_default_command_names_the_stable_dir_fleet_hook(self):
        command = proof.default_hook_command("claude", self.ctx)
        self.assertIn(os.path.join("apps", "lane-tools", "fleet-guard-hook"), command)
        self.assertIn("--format claude", command)

    def test_the_format_comes_from_the_command(self):
        self.assertEqual(proof.hook_format("/x/hook --format grok", "claude"), "grok")
        self.assertEqual(proof.hook_format("/x/hook --format=cursor", "claude"), "cursor")
        self.assertEqual(proof.hook_format("/x/hook", "grok"), "grok")


class SiblingEventTests(FakeHookCase):
    """Only the platform's own event may abort a proof;  a sibling may not."""

    def write_config(self, relative: str, document: dict) -> str:
        """Write a hook config document under the temporary home and return its path."""
        path = os.path.join(self.home, relative)
        make_private_dir(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        return path

    def test_a_malformed_sibling_event_does_not_stop_the_own_event(self):
        # A hand-edited PostToolUse list in ~/.claude/settings.json is not this
        # platform's event,  so it is skipped instead of aborting the proof.
        self.write_config(
            ".claude/settings.json",
            {
                "hooks": {
                    "PreToolUse": [
                        {"hooks": [{"command": "/opt/lane-tools/lane-guard-hook --format claude"}]}
                    ],
                    "PostToolUse": {"not": "a list"},
                }
            },
        )
        self.assertEqual(
            proof.resolve_hook_command("claude", self.ctx),
            "/opt/lane-tools/lane-guard-hook --format claude",
        )

    def test_a_malformed_sibling_group_does_not_stop_the_own_group(self):
        # The owner's unrelated moshi-hook group holds a Stop list this package never
        # reads,  so its shape cannot fail the lane guard group's proof.
        self.write_config(
            ".gemini/config/hooks.json",
            {
                "lane-guard": {
                    "PreToolUse": [
                        {"command": "/opt/lane-tools/fleet-guard-hook --format antigravity"}
                    ]
                },
                "moshi-hook": {"Stop": {"not": "a list"}},
            },
        )
        self.assertEqual(
            proof.resolve_hook_command("antigravity", self.ctx),
            "/opt/lane-tools/fleet-guard-hook --format antigravity",
        )

    def test_an_unrelated_section_that_is_large_does_not_stop_the_proof(self):
        # A settings file with thousands of permissions is a healthy seat,  not a
        # broken one:  the section the schema does not read is never bounded.
        self.write_config(
            ".claude/settings.json",
            {
                "permissions": {"allow": ["Bash(git:*)"] * 5000},
                "env": {"A_VARIABLE": "1"},
                "hooks": {
                    "PreToolUse": [
                        {"hooks": [{"command": "/opt/lane-tools/lane-guard-hook --format claude"}]}
                    ]
                },
            },
        )
        self.assertEqual(
            proof.resolve_hook_command("claude", self.ctx),
            "/opt/lane-tools/lane-guard-hook --format claude",
        )

    def test_an_unrelated_section_that_is_deep_does_not_stop_the_proof(self):
        deep: dict[str, object] = {"leaf": True}
        for _ in range(proof.MAX_CONFIG_DEPTH + 2):
            deep = {"deeper": deep}
        self.write_config(
            ".claude/settings.json",
            {
                "mcpServers": {"one": deep},
                "hooks": {
                    "PreToolUse": [
                        {"hooks": [{"command": "/opt/lane-tools/lane-guard-hook --format claude"}]}
                    ]
                },
            },
        )
        self.assertEqual(
            proof.resolve_hook_command("claude", self.ctx),
            "/opt/lane-tools/lane-guard-hook --format claude",
        )

    def test_the_own_event_that_is_malformed_still_aborts(self):
        self.write_config(
            ".claude/settings.json",
            {"hooks": {"PreToolUse": {"not": "a list"}}},
        )
        with self.assertRaises(CheckError) as caught:
            proof.resolve_hook_command("claude", self.ctx)
        self.assertIn("claude", str(caught.exception))

    def test_every_platform_with_a_config_file_has_its_own_schema(self):
        for platform, adapter in proof.PLATFORM_ADAPTERS.items():
            if not adapter.config_rel:
                continue
            with self.subTest(platform=platform):
                self.assertIn(platform, proof.HOOK_CONFIG_SCHEMAS)
                schema = proof.HOOK_CONFIG_SCHEMAS[platform]
                self.assertIn(schema.event, proof.HOOK_EVENTS)


class InterruptionTests(FakeHookCase):
    """An interruption must not leave the hook child running.

    The child is started in its own session,  so the runner can kill the whole group.
    The rule this proves is that an interruption terminates and reaps the child before
    the interruption is propagated,  so a Ctrl-C never leaves a hook behind.
    """

    def test_an_interruption_leaves_no_child_behind(self):
        started: list[int] = []

        class InterruptingPopen:
            """A Popen stand-in that starts a real child and then raises."""

            def __init__(self, argv, **kwargs):
                self._proc = _REAL_POPEN(argv, **kwargs)
                started.append(self._proc.pid)

            @property
            def pid(self):
                return self._proc.pid

            def communicate(self, *args, **kwargs):
                raise KeyboardInterrupt("the probe was interrupted")

            def wait(self, timeout=None):
                return self._proc.wait(timeout=timeout)

            def kill(self):
                self._proc.kill()

            @property
            def stdin(self):
                return self._proc.stdin

            @property
            def stdout(self):
                return self._proc.stdout

            @property
            def stderr(self):
                return self._proc.stderr

        with mock.patch("fleet_config.proof.subprocess.Popen", InterruptingPopen):
            with self.assertRaises(KeyboardInterrupt):
                proof.run_l1_probe("claude", self.ctx, hook_command=self.hook("sleep"))
        self.assertEqual(len(started), 1, "the probe did not start a child")
        with self.assertRaises(ProcessLookupError):
            os.kill(started[0], 0)

    def test_an_interruption_does_not_swallow_the_interruption(self):
        class InterruptingPopen:
            """A Popen stand-in that starts a real child and then raises."""

            def __init__(self, argv, **kwargs):
                self._proc = _REAL_POPEN(argv, **kwargs)

            @property
            def pid(self):
                return self._proc.pid

            def communicate(self, *args, **kwargs):
                raise SystemExit(3)

            def wait(self, timeout=None):
                return self._proc.wait(timeout=timeout)

            def kill(self):
                self._proc.kill()

            @property
            def stdin(self):
                return self._proc.stdin

            @property
            def stdout(self):
                return self._proc.stdout

            @property
            def stderr(self):
                return self._proc.stderr

        with mock.patch("fleet_config.proof.subprocess.Popen", InterruptingPopen):
            with self.assertRaises(SystemExit):
                proof.run_l1_probe("claude", self.ctx, hook_command=self.hook("sleep"))


class L2CanaryTests(FakeHookCase):
    """The canary is opt-in, and a platform that is not installed is unsupported."""

    def test_a_platform_that_is_not_installed_is_unsupported(self):
        with mock.patch("fleet_config.proof.shutil.which", return_value=None):
            outcome = proof.run_l2_canary("claude", self.ctx)
        self.assertIs(outcome.state, CheckState.UNSUPPORTED)
        self.assertIs(outcome.reason_code, ReasonCode.UNSUPPORTED)
        self.assertFalse(outcome.passed)
        self.assertIn("not installed", outcome.reason)

    def test_a_platform_with_no_canary_is_unsupported(self):
        outcome = proof.run_l2_canary("no-such-platform", self.ctx)
        self.assertIs(outcome.state, CheckState.UNSUPPORTED)
        self.assertIs(outcome.reason_code, ReasonCode.UNSUPPORTED)

    def test_the_prompt_is_fixed_and_carries_no_secret(self):
        source = os.path.join(self.home, "Code", proof.SENTINEL_SOURCE_NAME)
        prompt = proof.L2_PROMPT_TEMPLATE.format(source=source, destination="/tmp/x")
        self.assertIn("git clone", prompt)
        self.assertNotIn("KEY", prompt)
        self.assertNotIn("TOKEN", prompt)

    def test_the_canary_defines_a_command_for_each_platform_it_covers(self):
        for platform, spec in proof.L2_PLATFORMS.items():
            with self.subTest(platform=platform):
                self.assertIn("{prompt}", spec.argv)
                self.assertGreater(spec.timeout, 0)

    def test_a_transcript_merely_mentioning_a_denial_is_not_evidence(self):
        # The sentinel destination is echoed in the prompt and in a verbose transcript,
        # and a platform permission rejection, a model refusal or a git error all say
        # "denied".  A transcript with the marker and a deny word but NOT the guard's
        # own answer proves nothing, so it must not accept an L2 proof:  that would
        # pass a canary where the hook never fired at all.
        marker = "fleet-config-l2-sentinel-destination"
        transcript = (
            "The assistant attempted the clone.\n"
            "Error: permission denied while writing to %s\n"
            "fatal: could not read from remote repository\n" % marker
        )
        self.assertFalse(proof._l2_has_deny_evidence(transcript, marker))

    def test_the_guard_own_sentence_is_evidence(self):
        marker = "fleet-config-l2-sentinel-destination"
        transcript = (
            "Blocked: fleet-repo checkouts are not allowed in temp directories, and this "
            "command would put a clone of the fleet repo at %s\n" % marker
        )
        self.assertTrue(proof._l2_has_deny_evidence(transcript, marker))


class L2DenyEvidenceTests(FakeHookCase):
    """The L2 evidence is the guard's own answer,  and the sentinel named in its reason.

    The guard renders one reason in several shapes,  and the reason names the
    destination it refused.  A transcript holds that answer as text,  so the shapes
    are read out of the text,  and the marker is checked against the reason they
    carry.  The marker alone is not evidence,  and a deny word alone is not evidence.
    """

    MARKER = "fleet-config-l2-sentinel-destination"
    DESTINATION = "/tmp/fleet-config-l2-abc123/" + MARKER
    REASON = (
        "Blocked: fleet-repo checkouts are not allowed in temp directories, and this "
        "command would put a clone of Example-Repo at %s." % DESTINATION
    )

    def claude_answer(self, reason: str = "") -> str:
        """The hook's own answer, in the shape the claude family renders."""
        return json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason or self.REASON,
                }
            }
        )

    def test_the_real_guard_output_is_evidence(self) -> None:
        # The shape the guard actually prints,  verbatim,  with the reviewer's advice
        # after the destination.  The destination is the last token before the first
        # full stop,  and the advice that follows is the guard's,  not a second clause.
        reason = (
            "Blocked: fleet-repo checkouts are not allowed in temp directories, and this "
            "command would put a clone of /opt/fixture/x at %s.  Create it under the lanes root "
            "instead, at ~/apps/lanes/<Repo>/<seat>-marker.  For a read-only PR check, use a "
            "detached checkout at ~/apps/lanes/<Repo>/review-pr-<n> instead." % self.DESTINATION
        )
        self.assertTrue(proof._l2_has_deny_evidence(reason, self.MARKER))
        reasons = proof._l2_deny_reasons(reason)
        self.assertEqual(len(reasons), 1)
        self.assertIn(self.MARKER, reasons[0])
        self.assertNotIn("Create it", reasons[0])

    def test_a_multi_sentence_reason_names_the_destination_and_not_the_review_path(self) -> None:
        # The guard's real sentence carries a second clause before the destination,
        # and a reviewer's own text can follow it.  The destination is the LAST token
        # before the sentence's full stop,  so a greedy capture would take the
        # reviewer's text instead and the canary would read as a false DRIFT.
        reason = (
            "Blocked: fleet-repo checkouts are not allowed in temp directories, and this "
            "command would put a clone of Example-Repo at %s.  Set AGENT_SEAT to your seat "
            "tag, the one your launcher gives you, and try again." % self.DESTINATION
        )
        self.assertTrue(proof._l2_has_deny_evidence(reason, self.MARKER))

    def test_the_destination_is_the_last_token_before_the_full_stop(self) -> None:
        reason = (
            "Blocked: fleet-repo checkouts are not allowed in temp directories, and this "
            "command would put a clone of Example-Repo at %s." % self.DESTINATION
        )
        reasons = proof._l2_deny_reasons(reason)
        self.assertEqual(len(reasons), 1)
        self.assertIn(self.MARKER, reasons[0])

    def test_a_model_that_writes_the_deny_shape_cannot_impersonate_the_hook(self) -> None:
        # The transcript holds the model's prose as well as the runtime's record of the
        # hook's answer.  A model that writes the deny shape itself,  or says the
        # command was denied,  is not the hook answering,  and must not be evidence.
        model_text = (
            "I could not run that command.  "
            '{\"hookSpecificOutput\": {\"permissionDecision\": \"deny\", '
            '\"permissionDecisionReason\": \"denied at %s\"}}' % self.DESTINATION
        )
        self.assertFalse(proof._l2_has_deny_evidence(model_text, self.MARKER))

    def test_a_model_that_quotes_the_guard_sentence_cannot_impersonate_the_hook(self) -> None:
        # The guard's own wording,  written by the model,  is still the model's text.
        model_text = (
            "The guard would have said: fleet-repo checkouts are not allowed in temp "
            "directories, and this command would put a clone of Example-Repo at %s."
            % self.DESTINATION
        )
        self.assertFalse(proof._l2_has_deny_evidence(model_text, self.MARKER))

    def test_the_claude_deny_shape_naming_the_sentinel_is_evidence(self):
        self.assertTrue(proof._l2_has_deny_evidence(self.claude_answer(), self.MARKER))

    def test_the_deny_shape_embedded_in_a_transcript_string_is_evidence(self):
        # A runtime puts the hook's answer inside a JSON string of its own, so every
        # quote in the answer is escaped by the time it reaches the transcript.
        event = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "content": [{"type": "text", "text": self.claude_answer()}],
                    }
                ],
            },
        }
        transcript = json.dumps(event) + "\n"
        self.assertTrue(proof._l2_has_deny_evidence(transcript, self.MARKER))

    def test_the_grok_and_cursor_shapes_are_evidence(self):
        grok = json.dumps({"decision": "deny", "reason": self.REASON})
        cursor = json.dumps(
            {
                "permission": "deny",
                "user_message": "Blocked a fleet-repo checkout in a temp directory.",
                "agent_message": self.REASON,
            }
        )
        for answer in (grok, cursor):
            with self.subTest(answer=answer[:20]):
                self.assertTrue(proof._l2_has_deny_evidence(answer, self.MARKER))

    def test_the_plain_text_contract_is_evidence(self):
        # The exit-2 contract puts the reason on stderr with nothing on stdout, so
        # there is no JSON shape to read:  the guard's own sentence is the evidence.
        self.assertTrue(proof._l2_has_deny_evidence(self.REASON + "\n", self.MARKER))

    def test_a_deny_that_does_not_name_the_sentinel_is_not_evidence(self):
        # The guard denied, but the reason names another destination, so the deny
        # cannot be attributed to this probe.  This is the L1 rule, and L2 holds to
        # it:  a deny of somebody else's write is not a proof of this one.
        other = "Blocked: fleet-repo checkouts are not allowed in temp directories, and " \
                "this command would put a clone of Example-Repo at " \
                "/tmp/some-other-run/some-other-destination."
        self.assertFalse(proof._l2_has_deny_evidence(self.claude_answer(other), self.MARKER))

    def test_the_guard_sentence_without_the_sentinel_is_not_evidence(self):
        other = (
            "Blocked: fleet-repo checkouts are not allowed in temp directories, and this "
            "command would put a clone of Example-Repo at /tmp/elsewhere/else.\n"
        )
        self.assertFalse(proof._l2_has_deny_evidence(other, self.MARKER))

    def test_a_canary_whose_transcript_merely_denies_is_drift(self):
        # The whole canary, end to end:  a transcript that names the sentinel and says
        # the call was denied,  but never shows the guard's answer,  is a drift and
        # not a pass.  A pass here would mean a hook that never fired proved itself.
        transcript = (
            "The clone was blocked.\n"
            "Error: permission denied while writing to /tmp/x/%s\n" % self.MARKER
        )
        outcome = self.canary(transcript)
        self.assertFalse(outcome.passed)
        self.assertIs(outcome.state, CheckState.DRIFT)
        self.assertIs(outcome.reason_code, ReasonCode.PROOF_FAILED)

    def test_a_canary_whose_transcript_holds_the_guard_answer_is_a_pass(self):
        outcome = self.canary(self.claude_answer() + "\n")
        self.assertTrue(outcome.passed)
        self.assertIs(outcome.state, CheckState.PASS)
        self.assertIs(outcome.achieved, ProofLevel.L2)

    def canary(self, transcript: str):
        """The L2 outcome for a canary whose transcript is `transcript`."""
        run = mock.Mock()
        run.returncode = 0
        run.stdout = transcript.encode("utf-8")
        run.stderr = b""
        run.error = ""
        run.timed_out = False
        with mock.patch("fleet_config.proof.shutil.which", return_value="/usr/bin/true"):
            with mock.patch("fleet_config.proof._run_argv", return_value=run):
                return proof.run_l2_canary("claude", self.ctx, timeout=5)


class ResultTests(FakeHookCase):
    """A proof outcome becomes a checked result."""

    def test_proof_to_result_maps_a_pass(self):
        rule = make_rule()
        outcome = proof.L1Outcome(
            level=ProofLevel.L1,
            passed=True,
            state=CheckState.PASS,
            reason="the installed hook denied the sentinel write at L1",
            reason_code=ReasonCode.OK,
            detail="",
            achieved=ProofLevel.L1,
        )
        result = proof.proof_to_result(rule, "claude", outcome, self.ctx)
        self.assertIs(result.state, CheckState.PASS)
        self.assertIs(result.reason_code, ReasonCode.OK)
        self.assertIs(result.proof_level_achieved, ProofLevel.L1)
        self.assertIs(result.proof_level_required, ProofLevel.L1)
        self.assertEqual(result.checked_at, "2026-10-19T12:00:00Z")

    def test_proof_to_result_maps_a_failed_proof(self):
        rule = make_rule()
        outcome = proof.L1Outcome(
            level=ProofLevel.L1,
            passed=False,
            state=CheckState.DRIFT,
            reason="the hook did not deny the sentinel write",
            reason_code=ReasonCode.PROOF_FAILED,
        )
        result = proof.proof_to_result(rule, "claude", outcome, self.ctx)
        self.assertIs(result.state, CheckState.DRIFT)
        self.assertIs(result.reason_code, ReasonCode.PROOF_FAILED)
        self.assertIsNone(result.proof_level_achieved)

    def test_apply_proof_folds_a_pass_into_a_passing_l0_result(self):
        base = proof.proof_to_result(
            make_rule(),
            "claude",
            proof.L1Outcome(
                level=ProofLevel.L1,
                passed=True,
                state=CheckState.PASS,
                reason="ok",
                reason_code=ReasonCode.OK,
                achieved=ProofLevel.L1,
            ),
            self.ctx,
        )
        outcome = proof.L1Outcome(
            level=ProofLevel.L1,
            passed=True,
            state=CheckState.PASS,
            reason="the installed hook denied the sentinel write at L1",
            reason_code=ReasonCode.OK,
            achieved=ProofLevel.L1,
        )
        result = proof.apply_proof(base, outcome)
        self.assertIs(result.state, CheckState.PASS)
        self.assertIs(result.proof_level_achieved, ProofLevel.L1)

    def test_apply_proof_keeps_a_failing_l0_result(self):
        rule = make_rule()
        base = proof.build_result(
            rule,
            "claude",
            CheckState.DRIFT,
            "the hook is not registered",
            ReasonCode.DRIFT,
            ObservedState(),
            self.ctx,
        )
        outcome = proof.L1Outcome(
            level=ProofLevel.L1,
            passed=True,
            state=CheckState.PASS,
            reason="the installed hook denied the sentinel write at L1",
            reason_code=ReasonCode.OK,
            achieved=ProofLevel.L1,
        )
        result = proof.apply_proof(base, outcome)
        self.assertIs(result.state, CheckState.DRIFT)
        self.assertIs(result.reason_code, ReasonCode.DRIFT)

    def test_apply_proof_marks_a_proof_that_did_not_run(self):
        rule = make_rule()
        base = proof.build_result(
            rule,
            "claude",
            CheckState.PASS,
            "the hook is registered",
            ReasonCode.OK,
            ObservedState(),
            self.ctx,
        )
        outcome = proof.L2Outcome(
            level=ProofLevel.L2,
            passed=False,
            state=CheckState.UNPROVEN,
            reason="the L2 canary did not run",
            reason_code=ReasonCode.PROOF_NOT_RUN,
        )
        result = proof.apply_proof(base, outcome)
        self.assertIs(result.state, CheckState.UNPROVEN)
        self.assertIs(result.reason_code, ReasonCode.PROOF_NOT_RUN)


if __name__ == "__main__":
    unittest.main()
