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
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

from fleet_config import proof
from fleet_config.checks import CheckContext
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
        proof.run_l1_probe("claude", self.ctx, hook_command=self.hook("deny"))
        payload = self.recorded_payload()
        self.assertEqual(payload["hook_event_name"], "PreToolUse")
        self.assertEqual(payload["tool_name"], "Bash")
        self.assertIn("command", payload["tool_input"])
        self.assertIn("cwd", payload)

    def test_the_cursor_payload_is_the_cursor_shape(self):
        proof.run_l1_probe("cursor", self.ctx, hook_command=self.hook("deny"))
        payload = self.recorded_payload()
        self.assertEqual(payload["hook_event_name"], "beforeShellExecution")
        self.assertIn("command", payload)
        self.assertIn("workspace_roots", payload)

    def test_the_antigravity_payload_is_the_antigravity_shape(self):
        proof.run_l1_probe("antigravity", self.ctx, hook_command=self.hook("deny"))
        payload = self.recorded_payload()
        self.assertEqual(payload["tool_name"], "run_command")
        self.assertIn("CommandLine", payload["tool_input"])


class HookResolutionTests(FakeHookCase):
    """The hook under test is the installed one, read from the platform's config."""

    def test_resolve_reads_the_platforms_own_config(self):
        os.makedirs(os.path.join(self.home, ".claude"))
        path = os.path.join(self.home, ".claude", "settings.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
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
                handle,
            )
        self.assertEqual(
            proof.resolve_hook_command("claude", self.ctx),
            "/opt/lane-tools/lane-guard-hook --format claude",
        )

    def test_resolve_returns_none_when_no_guard_hook_is_named(self):
        os.makedirs(os.path.join(self.home, ".claude"))
        path = os.path.join(self.home, ".claude", "settings.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"hooks": {"PreToolUse": []}}, handle)
        self.assertIsNone(proof.resolve_hook_command("claude", self.ctx))

    def test_resolve_returns_none_when_there_is_no_config(self):
        self.assertIsNone(proof.resolve_hook_command("claude", self.ctx))

    def test_the_default_command_names_the_stable_dir_fleet_hook(self):
        command = proof.default_hook_command("claude", self.ctx)
        self.assertIn(os.path.join("apps", "lane-tools", "fleet-guard-hook"), command)
        self.assertIn("--format claude", command)

    def test_the_format_comes_from_the_command(self):
        self.assertEqual(proof.hook_format("/x/hook --format grok", "claude"), "grok")
        self.assertEqual(proof.hook_format("/x/hook --format=cursor", "claude"), "cursor")
        self.assertEqual(proof.hook_format("/x/hook", "grok"), "grok")


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
