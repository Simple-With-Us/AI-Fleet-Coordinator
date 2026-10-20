"""The collector dispatch:  a reserved collector is unsupported, mac runs the check.

The tests here never import the six per-kind check modules, so they pass while those
modules are still being written.  The mac collector is driven with a registry the
test supplies, which is the seam the real caller uses when it already has one.
"""

from __future__ import annotations

import inspect
import tempfile
import unittest
from datetime import UTC, datetime
from unittest import mock

from fleet_config import collectors
from fleet_config.checks import CheckContext, CheckError, CheckRegistry, build_result
from fleet_config.types import (
    CheckSpec,
    CheckState,
    CheckType,
    Collector,
    ConfigKind,
    ExpectedState,
    ObservedState,
    ProofLevel,
    ReasonCode,
    Rule,
)


def make_rule(collector: Collector = Collector.MAC, rule_id: str = "test-rule") -> Rule:
    """A rule for the dispatch tests.  It names no path and no secret."""
    return Rule(
        id=rule_id,
        kind=ConfigKind.HOOKS,
        platforms=("claude",),
        check=CheckSpec(
            type=CheckType.HOOK_REGISTERED,
            target="~/.claude/settings.json",
            detail="PreToolUse",
        ),
        expected=ExpectedState(description="the lane guard is registered"),
        proof_level=ProofLevel.L1,
        owner_seat="OPENCODE",
        autofix_allowed=False,
        seat="OPENCODE",
        collector=collector,
        ttl_seconds=900,
    )


class CollectorCase(unittest.TestCase):
    """A temp home and a context pointed at it."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="fleet-config-test-")
        self.addCleanup(self.tmp.cleanup)
        self.ctx = CheckContext(
            home=self.tmp.name,
            now=datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC),
            timeout=10,
        )


class ReservedCollectorTests(CollectorCase):
    """A collector this phase does not implement yields the pinned UNSUPPORTED result."""

    def test_every_reserved_collector_is_unsupported(self):
        for collector in (Collector.VM, Collector.HETZNER, Collector.API):
            with self.subTest(collector=collector):
                rule = make_rule(collector)
                result = collectors.collect(rule, "claude", self.ctx)
                self.assertIs(result.state, CheckState.UNSUPPORTED)
                self.assertIs(result.reason_code, ReasonCode.UNSUPPORTED)
                self.assertIn(collector.value, result.reason)
                self.assertIs(result.collector, collector)

    def test_an_unsupported_result_is_not_a_pass(self):
        rule = make_rule(Collector.VM)
        result = collectors.collect(rule, "claude", self.ctx)
        self.assertNotEqual(result.state, CheckState.PASS)
        self.assertEqual(result.checked_at, "2026-10-19T12:00:00Z")


class MacCollectorTests(CollectorCase):
    """The mac collector runs the check the registry names for the rule's check type."""

    def _registry(self) -> CheckRegistry:
        registry = CheckRegistry()

        @registry.register(CheckType.HOOK_REGISTERED)
        def fake_check(rule, platform, ctx):
            return build_result(
                rule,
                platform,
                CheckState.PASS,
                "the hook is registered",
                ReasonCode.OK,
                ObservedState(),
                ctx,
            )

        return registry

    def test_the_registered_check_runs(self):
        rule = make_rule()
        result = collectors.mac_collect(rule, "claude", self.ctx, registry=self._registry())
        self.assertIs(result.state, CheckState.PASS)
        self.assertIs(result.reason_code, ReasonCode.OK)
        self.assertEqual(result.reason, "the hook is registered")

    def test_a_check_type_with_no_implementation_is_unsupported(self):
        rule = make_rule()
        result = collectors.mac_collect(rule, "claude", self.ctx, registry=CheckRegistry())
        self.assertIs(result.state, CheckState.UNSUPPORTED)
        self.assertIs(result.reason_code, ReasonCode.UNSUPPORTED)
        self.assertIn(CheckType.HOOK_REGISTERED.value, result.reason)

    def test_collect_dispatches_to_the_mac_collector(self):
        rule = make_rule()
        with mock.patch.object(collectors, "registry_for", return_value=self._registry()):
            result = collectors.collect(rule, "claude", self.ctx)
        self.assertIs(result.state, CheckState.PASS)

    def test_a_missing_check_module_is_a_check_error(self):
        rule = make_rule()
        with (
            mock.patch.object(
                collectors,
                "default_registry",
                side_effect=ImportError("No module named 'fleet_config.checks_hooks'"),
            ),
            self.assertRaises(CheckError) as caught,
        ):
            collectors.mac_collect(rule, "claude", self.ctx)
        self.assertIn("checks_hooks", str(caught.exception))

    def test_the_collector_module_does_not_import_the_check_modules(self):
        """The six per-kind modules are imported lazily, inside the function."""
        source = inspect.getsource(collectors)
        for name in (
            "checks_instructions",
            "checks_hooks",
            "checks_mcp",
            "checks_skills",
            "checks_subagents",
            "checks_permissions",
        ):
            with self.subTest(module=name):
                self.assertNotIn("import " + name, source)

    def test_the_collector_never_calls_the_l2_canary(self):
        """The canary costs a model turn, so it never runs implicitly."""
        source = inspect.getsource(collectors)
        self.assertNotIn("run_l2_canary", source)
        self.assertNotIn("L2_PLATFORMS", source)

    def test_the_collector_never_runs_a_proof(self):
        """The proof machinery lives in proof.py, and the collector never calls it."""
        source = inspect.getsource(collectors)
        for name in ("run_l1_probe", "run_l2_canary", "L1Outcome", "L2Outcome", "L2_PLATFORMS"):
            with self.subTest(name=name):
                self.assertNotIn(name, source)
        self.assertNotIn("from .proof", source)
        self.assertNotIn("import proof", source)


if __name__ == "__main__":
    unittest.main()
