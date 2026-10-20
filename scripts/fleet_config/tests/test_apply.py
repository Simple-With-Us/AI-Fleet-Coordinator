"""The write side:  only through the existing installers,  and never without a human.

These tests cover the AFC-386 done-when:

  * ``apply --plan`` changes no file,  proved by an mtime and hash snapshot of the tree
  * a second apply is a no-op
  * a persistence-class apply from an agent shell without Touch ID is refused
  * an apply restores a removed guard plugin to PASS with a fresh deny

The installer and the check are simulated:  the runner is injected,  so no test ever
starts a real installer,  and the guard plugin is a fixture file in a temporary home,  so
no test ever touches Jay's Mac.
"""

from __future__ import annotations

import os
import subprocess
import unittest
from datetime import UTC, datetime

from fleet_config import aggregator, touchid
from fleet_config import apply as apply_module
from fleet_config import plan as plan_module
from fleet_config.checks import CheckContext, build_result
from fleet_config.types import (
    CheckState,
    CheckType,
    ConfigKind,
    ObservedState,
    ProofLevel,
    ReasonCode,
)

from ._fake import FleetTestCase, make_manifest, make_rule, registry_with, snapshot_tree

MOMENT = datetime(2026, 10, 19, 14, 30, 0, tzinfo=UTC)

GUARD_REL = ".minimax/plugins/fleet-guard/hooks/hooks.json"
GUARD_MARKER = "deny the sentinel write"


def context(home) -> CheckContext:
    """A context with a fixed clock,  pointing at the temporary home."""
    return CheckContext(home=str(home), now=MOMENT)


def guard_check(rule, platform, ctx):
    """A check that reads the guard plugin fixture and answers like the real one.

    It is a stand-in for ``checks_hooks``:  the plugin file holds the deny marker,  so the
    check passes at L1 with a fresh deny;  when the file is gone the check reports the
    plugin as missing.
    """
    path = os.path.join(str(ctx.home), GUARD_REL)
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        if GUARD_MARKER in body:
            return build_result(
                rule,
                platform,
                CheckState.PASS,
                "the guard plugin is installed and denied the sentinel write",
                ReasonCode.OK,
                ObservedState(present=True),
                ctx,
                proof_level_achieved=ProofLevel.L1,
            )
    return build_result(
        rule,
        platform,
        CheckState.MISSING,
        "the guard plugin is not installed",
        ReasonCode.MISSING,
        ObservedState(present=False),
        ctx,
    )


class RecordingRunner:
    """A fake installer that records its argv and writes the guard plugin fixture."""

    def __init__(self, home, fail: bool = False) -> None:
        self.home = str(home)
        self.fail = fail
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if self.fail:
            return 1, "", "the fake installer refused"
        target = os.path.join(self.home, GUARD_REL)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write('{"hooks": "%s"}\n' % GUARD_MARKER)
        return 0, "installed the guard plugin\n", ""


def allowing_gate(reason):
    """A gate that confirms without a prompt,  so a test can reach the write path."""
    return


class ApplyTests(FleetTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.rule = make_rule(
            "mm-hooks-lane-guard",
            ConfigKind.HOOKS,
            check_type=CheckType.HOOK_REGISTERED,
            target="~/.minimax/plugins/fleet-guard/hooks/hooks.json",
            proof=ProofLevel.L1,
            autofix=True,
        )
        self.manifest = make_manifest([self.rule])
        self.registry = registry_with({CheckType.HOOK_REGISTERED: guard_check})

    def status(self, home):
        """A status run over the temporary home."""
        return aggregator.run_status(
            self.manifest,
            seat="MM",
            ctx=context(home),
            registry=self.registry,
        )

    def test_a_missing_guard_plugin_is_reported_and_repaired_to_pass(self):
        home = self.home
        report = self.status(home)
        self.assertIs(report.results[0].state, CheckState.MISSING)
        built = plan_module.build_plan(self.manifest, report.results, seat="MM")
        self.assertEqual(len(built.autofixable()), 1)
        runner = RecordingRunner(home)
        result = apply_module.apply_plan(
            built,
            home=str(home),
            runner=runner,
            touchid_gate=allowing_gate,
            manifest=self.manifest,
            registry=self.registry,
            ctx=context(home),
        )
        self.assertEqual(len(runner.calls), 1)
        self.assertIn("fleet_lanes.install_tools", " ".join(runner.calls[0]))
        self.assertIn("--home", runner.calls[0])
        self.assertEqual(runner.calls[0][-1], str(home))
        self.assertIsNotNone(result.report)
        self.assertIs(result.report.results[0].state, CheckState.PASS)
        self.assertIs(result.report.results[0].proof_level_achieved, ProofLevel.L1)
        self.assertIs(result.verdict, result.report.summary.overall_verdict)

    def test_a_second_apply_is_a_no_op(self):
        home = self.home
        runner = RecordingRunner(home)
        first = apply_module.apply_plan(
            plan_module.build_plan(self.manifest, self.status(home).results, seat="MM"),
            home=str(home),
            runner=runner,
            touchid_gate=allowing_gate,
            manifest=self.manifest,
            registry=self.registry,
            ctx=context(home),
        )
        self.assertEqual(len(first.applied), 1)
        second_report = self.status(home)
        self.assertIs(second_report.results[0].state, CheckState.PASS)
        second = apply_module.apply_plan(
            plan_module.build_plan(self.manifest, second_report.results, seat="MM"),
            home=str(home),
            runner=runner,
            touchid_gate=allowing_gate,
            manifest=self.manifest,
            registry=self.registry,
            ctx=context(home),
        )
        self.assertEqual(len(runner.calls), 1, "the second apply started an installer")
        self.assertEqual(second.steps, ())
        self.assertEqual(len(second.applied), 0)

    def test_the_plan_changes_no_file(self):
        tree = self.home
        target = tree / GUARD_REL
        target.parent.mkdir(parents=True)
        target.write_text("{}\n", encoding="utf-8")
        before = snapshot_tree(tree)
        built = plan_module.build_plan(self.manifest, self.status(tree).results, seat="MM")
        built.render()
        after = snapshot_tree(tree)
        self.assertEqual(before, after)
        self.assertNotEqual(before, {})

    def test_a_persistence_class_apply_without_touch_id_is_refused(self):
        home = self.home
        runner = RecordingRunner(home)

        def refusing_gate(reason):
            raise touchid.TouchIdError("Touch ID is not available on this machine", "unavailable")

        result = apply_module.apply_plan(
            plan_module.build_plan(self.manifest, self.status(home).results, seat="MM"),
            home=str(home),
            runner=runner,
            touchid_gate=refusing_gate,
            manifest=self.manifest,
            registry=self.registry,
            ctx=context(home),
        )
        self.assertTrue(result.refused)
        self.assertIn("Touch ID", result.refusal_reason)
        self.assertEqual(runner.calls, [], "the installer ran without a confirmation")
        self.assertFalse(os.path.exists(os.path.join(str(home), GUARD_REL)))
        self.assertEqual(len(result.failed), 1)
        self.assertIs(result.steps[0].outcome, apply_module.OUTCOME_REFUSED)

    def test_a_rule_that_may_not_be_fixed_is_skipped(self):
        rule = make_rule(
            "mm-hooks-lane-guard",
            ConfigKind.HOOKS,
            check_type=CheckType.HOOK_REGISTERED,
            target="~/.minimax/plugins/fleet-guard/hooks/hooks.json",
            autofix=False,
        )
        manifest = make_manifest([rule])
        home = self.home
        runner = RecordingRunner(home)
        report = aggregator.run_status(
            manifest, seat="MM", ctx=context(home), registry=self.registry
        )
        result = apply_module.apply_plan(
            plan_module.build_plan(manifest, report.results, seat="MM"),
            home=str(home),
            runner=runner,
            touchid_gate=allowing_gate,
            manifest=manifest,
            registry=self.registry,
            ctx=context(home),
        )
        self.assertEqual(runner.calls, [])
        self.assertEqual(len(result.skipped), 1)
        self.assertIn("does not allow an automatic fix", result.steps[0].detail)

    def test_a_failing_installer_is_reported_as_a_failure(self):
        home = self.home
        runner = RecordingRunner(home, fail=True)
        result = apply_module.apply_plan(
            plan_module.build_plan(self.manifest, self.status(home).results, seat="MM"),
            home=str(home),
            runner=runner,
            touchid_gate=allowing_gate,
            manifest=self.manifest,
            registry=self.registry,
            ctx=context(home),
        )
        self.assertEqual(len(result.failed), 1)
        self.assertIs(result.steps[0].outcome, apply_module.OUTCOME_FAILED)
        self.assertIn("the fake installer refused", result.steps[0].detail)

    def test_a_target_outside_the_home_is_refused(self):
        home = self.home
        outside = self.root / "outside.json"
        outside.write_text("{}\n", encoding="utf-8")
        rule = make_rule(
            "mm-hooks-outside",
            ConfigKind.HOOKS,
            check_type=CheckType.HOOK_REGISTERED,
            target="~/.minimax/plugins/fleet-guard/hooks/hooks.json",
        )
        manifest = make_manifest([rule])
        entry = plan_module.PlanEntry(
            rule_id=rule.id,
            seat="MM",
            platform="minimax",
            kind=ConfigKind.HOOKS,
            state=CheckState.DRIFT,
            reason="it differs",
            change="it would change",
            installer=plan_module.Installer(
                key="install_tools",
                argv=("python3", "-m", "fleet_lanes.install_tools", "apply", "minimax"),
                files=(str(outside),),
            ),
            files=(str(outside),),
            autofix_allowed=True,
            persistence_class=True,
            owner_seat="MM",
            proof_level_required=ProofLevel.L0,
            auto_fixable=True,
            blocked_reason="",
        )
        built = plan_module.Plan(
            seat="MM",
            kind=None,
            platform=None,
            generated_at="2026-10-19T14:30:00Z",
            entries=(entry,),
        )
        runner = RecordingRunner(home)
        result = apply_module.apply_plan(
            built,
            home=str(home),
            runner=runner,
            touchid_gate=allowing_gate,
            manifest=manifest,
            registry=self.registry,
            ctx=context(home),
        )
        self.assertEqual(runner.calls, [])
        self.assertTrue(result.refused)
        self.assertIs(result.steps[0].outcome, apply_module.OUTCOME_REFUSED)


class TouchIdGateTests(FleetTestCase):
    """The gate itself:  fail closed,  and the test override is refused in production."""

    def fake_helper(self, body: str, sign: bool = True) -> str:
        """A fake helper script that exits with the code `body` asks for.

        The helper is signed ad hoc when codesign is available, because the gate
        refuses an unsigned helper.  codesign is a macOS tool, so on another platform
        the helper is left unsigned and the signing-dependent test skips;  the gate
        still refuses it, which is the same outcome.
        """
        path = self.root / "fake-gate.sh"
        path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        path.chmod(0o755)
        if sign and os.path.exists("/usr/bin/codesign"):
            subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(path)], check=True)
        return str(path)

    @unittest.skipUnless(os.path.exists("/usr/bin/codesign"), "codesign is a macOS tool")
    def test_a_missing_helper_refuses(self):
        with self.assertRaises(touchid.TouchIdError) as caught:
            touchid.confirm("apply the change", helper=str(self.root / "nope"))
        self.assertEqual(caught.exception.reason, "missing")

    def test_an_unsigned_helper_refuses(self):
        path = self.fake_helper("exit 0", sign=False)
        with self.assertRaises(touchid.TouchIdError) as caught:
            touchid.confirm("apply the change", helper=path)
        self.assertEqual(caught.exception.reason, "not-signed")

    @unittest.skipUnless(os.path.exists("/usr/bin/codesign"), "codesign is a macOS tool")
    def test_a_helper_that_fails_refuses(self):
        path = self.fake_helper("echo 'the owner cancelled' >&2\nexit 3")
        with self.assertRaises(touchid.TouchIdError) as caught:
            touchid.confirm("apply the change", helper=path)
        self.assertEqual(caught.exception.reason, "cancelled")
        self.assertIn("the owner cancelled", str(caught.exception))

    @unittest.skipUnless(os.path.exists("/usr/bin/codesign"), "codesign is a macOS tool")
    def test_an_unavailable_touch_id_refuses_with_an_actionable_reason(self):
        path = self.fake_helper("echo 'Touch ID is not enrolled on this Mac' >&2\nexit 2")
        with self.assertRaises(touchid.TouchIdError) as caught:
            touchid.confirm("apply the change", helper=path)
        self.assertEqual(caught.exception.reason, "unavailable")
        self.assertIn("not enrolled", str(caught.exception))

    @unittest.skipUnless(os.path.exists("/usr/bin/codesign"), "codesign is a macOS tool")
    def test_a_successful_helper_allows_the_write(self):
        path = self.fake_helper("exit 0")
        self.assertIsNone(touchid.confirm("apply the change", helper=path))

    @unittest.skipUnless(os.path.exists("/usr/bin/codesign"), "codesign is a macOS tool")
    def test_the_environment_override_alone_never_bypasses_the_gate(self):
        path = self.fake_helper("echo 'no' >&2\nexit 3")
        env = {touchid.TEST_BYPASS_ENV: "1"}
        with self.assertRaises(touchid.TouchIdError):
            touchid.confirm("apply the change", helper=path, env=env)

    @unittest.skipUnless(os.path.exists("/usr/bin/codesign"), "codesign is a macOS tool")
    def test_the_test_bypass_works_only_when_the_caller_asks_for_it(self):
        path = self.fake_helper("echo 'no' >&2\nexit 3")
        env = {touchid.TEST_BYPASS_ENV: "1"}
        self.assertIsNone(
            touchid.confirm("apply the change", helper=path, env=env, test_bypass=True)
        )

    def test_the_hooks_kind_is_persistence_class(self):
        self.assertTrue(touchid.is_persistence_class(ConfigKind.HOOKS))
        self.assertFalse(touchid.is_persistence_class(ConfigKind.INSTRUCTIONS))


if __name__ == "__main__":
    unittest.main()
