"""The aggregator:  every rule produces a result,  and no result is quietly healthy.

The rules under test,  from the design document's section 4:

  * an unimplemented check type is ``UNSUPPORTED``,  never a skip
  * a missing check module is a failing result,  never a silent skip
  * a raised check is ``UNKNOWN`` with the error's own reason code,  never a pass
  * a reserved collector is ``UNSUPPORTED``
  * every result in one report shares one clock
  * a result past its TTL is re-labelled ``UNKNOWN`` with the reason code ``stale``
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from fleet_config import aggregator, contract
from fleet_config.checks import CheckContext, CheckError, CheckRegistry
from fleet_config.types import (
    CheckState,
    CheckType,
    Collector,
    ConfigKind,
    ExpectedState,
    ObservedState,
    OverallVerdict,
    ProofLevel,
    ReasonCode,
)

from ._fake import make_manifest, make_rule, registry_with, result_check

MOMENT = datetime(2026, 10, 19, 14, 30, 0, tzinfo=timezone.utc)


def context(home="/tmp/fleet-config-home", timeout: int = 10) -> CheckContext:
    """A context with a fixed clock,  so every test sees the same stamps."""
    return CheckContext(home=home, now=MOMENT, timeout=timeout)


class RunDeadlineTests(unittest.TestCase):
    """A run has an overall budget:  the rules left over are failing timeouts."""

    class Clock:
        """A monotonic clock that returns the values it is given,  one call at a time."""

        def __init__(self, values):
            self._values = list(values)
            self._index = 0

        def __call__(self):
            value = self._values[min(self._index, len(self._values) - 1)]
            self._index += 1
            return value

    def test_the_rules_left_over_after_the_budget_are_failing_timeouts(self):
        rules = [
            make_rule("mm-a", ConfigKind.INSTRUCTIONS),
            make_rule("mm-b", ConfigKind.INSTRUCTIONS),
            make_rule("mm-c", ConfigKind.INSTRUCTIONS),
        ]
        manifest = make_manifest(rules)
        checked = []

        def check(rule, platform, ctx):
            from fleet_config.checks import build_result

            checked.append(rule.id)
            return build_result(
                rule, platform, CheckState.PASS, "fine", ReasonCode.OK, ObservedState(), ctx
            )

        report = aggregator.run_status(
            manifest,
            registry=registry_with({CheckType.FILE_HASH: check}),
            ctx=context(),
            deadline_seconds=60,
            clock=self.Clock([0.0, 0.0, 1000.0, 1000.0, 1000.0]),
        )
        self.assertEqual(checked, ["mm-a"], "a rule ran after the budget was spent")
        self.assertEqual(len(report.results), 3)
        for result in report.results[1:]:
            self.assertIs(result.state, CheckState.UNKNOWN)
            self.assertIs(result.reason_code, ReasonCode.TIMEOUT)
            self.assertTrue(contract.is_failing(result))
        self.assertIs(report.summary.overall_verdict, OverallVerdict.FAIL)

    def test_a_run_with_a_budget_to_spare_checks_every_rule(self):
        rules = [
            make_rule("mm-a", ConfigKind.INSTRUCTIONS),
            make_rule("mm-b", ConfigKind.INSTRUCTIONS),
        ]
        manifest = make_manifest(rules)
        registry = registry_with({CheckType.FILE_HASH: result_check(CheckState.PASS)})
        report = aggregator.run_status(
            manifest,
            registry=registry,
            ctx=context(),
            deadline_seconds=60,
            clock=self.Clock([0.0, 1.0, 2.0, 3.0]),
        )
        self.assertEqual(len(report.results), 2)
        for result in report.results:
            self.assertIs(result.state, CheckState.PASS)
        self.assertIs(report.summary.overall_verdict, OverallVerdict.PASS)

    def test_the_budget_is_the_default_and_it_is_positive(self):
        self.assertGreater(aggregator.RUN_DEADLINE_SECONDS, 0)


class DoctorBoundsTests(unittest.TestCase):
    """A doctor's answer is bounded:  too big,  too deep or too wide is a note."""

    def doctor_notes(self, body: str):
        """Run a fake doctor that prints `body` and return its notes."""
        import pathlib
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            scripts = pathlib.Path(tmp)
            doctor = scripts / "fleet-tools-doctor.py"
            doctor.write_text(body, encoding="utf-8")
            return list(
                aggregator.run_doctors(
                    context(),
                    commands=(("fake", ("fleet-tools-doctor.py", "--json")),),
                    scripts_dir=str(scripts),
                )
            )

    def test_a_doctor_that_answers_with_too_many_bytes_is_refused(self):
        notes = self.doctor_notes(
            "import sys\nsys.stdout.write('x' * %d)\n" % (aggregator.MAX_DOCTOR_BYTES + 10)
        )
        self.assertEqual(len(notes), 1)
        self.assertIn("more than", notes[0])

    def test_a_doctor_that_answers_with_a_deep_tree_is_refused(self):
        depth = aggregator.MAX_DOCTOR_DEPTH + 2
        notes = self.doctor_notes(
            "import json\nprint(json.dumps(" + "[" * depth + "]" * depth + "))\n"
        )
        self.assertEqual(len(notes), 1)
        self.assertIn("deeper than", notes[0])

    def test_a_doctor_that_answers_with_too_many_nodes_is_refused(self):
        notes = self.doctor_notes(
            "import json\nprint(json.dumps(['x'] * %d))\n"
            % (aggregator.MAX_DOCTOR_NODES + 10)
        )
        self.assertEqual(len(notes), 1)
        self.assertIn("more than", notes[0])

    def test_a_doctor_that_answers_with_no_json_is_refused(self):
        notes = self.doctor_notes("print('this is not json')\n")
        self.assertEqual(len(notes), 1)
        self.assertIn("JSON", notes[0])

    def test_a_refused_doctor_is_a_note_and_not_a_crash(self):
        import pathlib
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            scripts = pathlib.Path(tmp)
            doctor = scripts / "fleet-tools-doctor.py"
            doctor.write_text("print('x' * 10)\n", encoding="utf-8")
            report = aggregator.run_status(
                make_manifest([make_rule("mm-a", ConfigKind.INSTRUCTIONS)]),
                registry=registry_with({CheckType.FILE_HASH: result_check(CheckState.PASS)}),
                ctx=context(),
                include_doctors=True,
            )
        self.assertIsNotNone(report)
        self.assertEqual(len(report.results), 1)


class AggregatorTests(unittest.TestCase):
    def test_a_passing_rule_gives_a_pass_and_a_pass_verdict(self):
        rule = make_rule("mm-instructions", ConfigKind.INSTRUCTIONS)
        manifest = make_manifest([rule])
        registry = registry_with({CheckType.FILE_HASH: result_check(CheckState.PASS)})
        report = aggregator.run_status(manifest, registry=registry, ctx=context())
        self.assertEqual(len(report.results), 1)
        self.assertIs(report.results[0].state, CheckState.PASS)
        self.assertIs(report.summary.overall_verdict, OverallVerdict.PASS)

    def test_an_unimplemented_check_type_is_unsupported_and_never_a_pass(self):
        rule = make_rule("mm-skills", ConfigKind.SKILLS, check_type=CheckType.SKILLS_INSTALLED)
        manifest = make_manifest([rule])
        report = aggregator.run_status(manifest, registry=registry_with({}), ctx=context())
        result = report.results[0]
        self.assertIs(result.state, CheckState.UNSUPPORTED)
        self.assertIs(result.reason_code, ReasonCode.UNSUPPORTED)
        self.assertIn("skills_installed", result.reason)
        self.assertIsNot(report.summary.overall_verdict, OverallVerdict.PASS)

    def test_a_missing_check_module_is_a_failing_result(self):
        rule = make_rule("mm-hooks", ConfigKind.HOOKS, check_type=CheckType.HOOK_REGISTERED)
        manifest = make_manifest([rule])
        report = aggregator.run_status(
            manifest,
            registry=registry_with({}),
            missing_modules=["checks_hooks"],
            ctx=context(),
        )
        result = report.results[0]
        self.assertIs(result.state, CheckState.UNKNOWN)
        self.assertIs(result.reason_code, ReasonCode.ERROR)
        self.assertTrue(contract.is_failing(result))
        self.assertIn("checks_hooks", result.reason)
        self.assertIs(report.summary.overall_verdict, OverallVerdict.FAIL)

    def test_a_raised_check_is_unknown_with_the_errors_reason_code(self):
        def boom(rule, platform, ctx):
            raise CheckError("the settings file is locked", ReasonCode.TIMEOUT)

        rule = make_rule("mm-hooks", ConfigKind.HOOKS, check_type=CheckType.HOOK_REGISTERED)
        manifest = make_manifest([rule])
        registry = registry_with({CheckType.HOOK_REGISTERED: boom})
        report = aggregator.run_status(manifest, registry=registry, ctx=context())
        result = report.results[0]
        self.assertIs(result.state, CheckState.UNKNOWN)
        self.assertIs(result.reason_code, ReasonCode.TIMEOUT)
        self.assertTrue(contract.is_failing(result))
        self.assertIsNot(result.state, CheckState.PASS)

    def test_an_unexpected_exception_is_unknown_and_not_a_pass(self):
        def boom(rule, platform, ctx):
            raise ValueError("something nobody expected")

        rule = make_rule("mm-hooks", ConfigKind.HOOKS, check_type=CheckType.HOOK_REGISTERED)
        manifest = make_manifest([rule])
        registry = registry_with({CheckType.HOOK_REGISTERED: boom})
        report = aggregator.run_status(manifest, registry=registry, ctx=context())
        self.assertIs(report.results[0].state, CheckState.UNKNOWN)
        self.assertIs(report.results[0].reason_code, ReasonCode.ERROR)

    def test_a_check_that_returns_the_wrong_shape_is_unparseable(self):
        rule = make_rule("mm-hooks", ConfigKind.HOOKS, check_type=CheckType.HOOK_REGISTERED)
        manifest = make_manifest([rule])
        registry = registry_with(
            {CheckType.HOOK_REGISTERED: lambda rule, platform, ctx: "not a result"}
        )
        report = aggregator.run_status(manifest, registry=registry, ctx=context())
        result = report.results[0]
        self.assertIs(result.state, CheckState.UNKNOWN)
        self.assertIs(result.reason_code, ReasonCode.UNPARSEABLE)
        self.assertTrue(contract.is_failing(result))

    def test_a_reserved_collector_is_unsupported(self):
        rule = make_rule(
            "mm-instructions",
            ConfigKind.INSTRUCTIONS,
            collector=Collector.VM,
        )
        manifest = make_manifest([rule])
        registry = registry_with({CheckType.FILE_HASH: result_check(CheckState.PASS)})
        report = aggregator.run_status(manifest, registry=registry, ctx=context())
        result = report.results[0]
        self.assertIs(result.state, CheckState.UNSUPPORTED)
        self.assertIn("vm", result.reason)
        self.assertIsNot(report.summary.overall_verdict, OverallVerdict.PASS)

    def test_every_result_shares_one_clock(self):
        rules = [
            make_rule("mm-a", ConfigKind.INSTRUCTIONS, ttl=900),
            make_rule("mm-b", ConfigKind.SKILLS, check_type=CheckType.SKILLS_INSTALLED, ttl=60),
        ]
        manifest = make_manifest(rules)
        registry = registry_with(
            {
                CheckType.FILE_HASH: result_check(CheckState.PASS),
                CheckType.SKILLS_INSTALLED: result_check(CheckState.PASS),
            }
        )
        report = aggregator.run_status(manifest, registry=registry, ctx=context())
        self.assertEqual(report.generated_at, "2026-10-19T14:30:00Z")
        for result in report.results:
            self.assertEqual(result.checked_at, "2026-10-19T14:30:00Z")
        expiries = sorted(result.expires_at for result in report.results)
        self.assertEqual(expiries, ["2026-10-19T14:31:00Z", "2026-10-19T14:45:00Z"])

    def test_the_filters_narrow_the_run(self):
        rules = [
            make_rule("mm-a", ConfigKind.INSTRUCTIONS),
            make_rule("mm-b", ConfigKind.SKILLS, check_type=CheckType.SKILLS_INSTALLED),
            make_rule(
                "mm-c",
                ConfigKind.MCP,
                check_type=CheckType.MCP_SERVERS_REGISTERED,
                platform="codex",
            ),
        ]
        manifest = make_manifest(rules)
        registry = registry_with(
            {
                CheckType.FILE_HASH: result_check(CheckState.PASS),
                CheckType.SKILLS_INSTALLED: result_check(CheckState.PASS),
                CheckType.MCP_SERVERS_REGISTERED: result_check(CheckState.PASS),
            }
        )
        by_kind = aggregator.run_status(
            manifest, kind=ConfigKind.SKILLS, registry=registry, ctx=context()
        )
        self.assertEqual([result.rule_id for result in by_kind.results], ["mm-b"])
        by_platform = aggregator.run_status(
            manifest, platform="codex", registry=registry, ctx=context()
        )
        self.assertEqual([result.rule_id for result in by_platform.results], ["mm-c"])
        by_seat = aggregator.run_status(manifest, seat="CLAUDE", registry=registry, ctx=context())
        self.assertEqual(by_seat.results, ())

    def test_an_unproven_rule_is_never_a_pass(self):
        rule = make_rule(
            "mm-hooks",
            ConfigKind.HOOKS,
            check_type=CheckType.HOOK_REGISTERED,
            proof=ProofLevel.L1,
        )
        manifest = make_manifest([rule])
        registry = registry_with({CheckType.HOOK_REGISTERED: result_check(CheckState.PASS)})
        report = aggregator.run_status(
            manifest,
            registry=registry,
            ctx=context(),
            proof_runner=lambda rule, platform, ctx, prove: aggregator.ProofOutcome(
                None, "the proof did not run", ReasonCode.PROOF_NOT_RUN
            ),
        )
        result = report.results[0]
        self.assertIs(result.state, CheckState.UNPROVEN)
        self.assertIs(result.reason_code, ReasonCode.PROOF_NOT_RUN)
        self.assertIsNot(report.summary.overall_verdict, OverallVerdict.PASS)

    def test_a_proof_that_could_not_complete_is_a_failing_unknown(self):
        # The design's step 4:  a check that raised, timed out or returned unparseable
        # output is UNKNOWN and it counts against the run.  A proof that cannot start is
        # such a check, so it must NOT be folded into the non-failing UNPROVEN.
        rule = make_rule(
            "mm-hooks",
            ConfigKind.HOOKS,
            check_type=CheckType.HOOK_REGISTERED,
            proof=ProofLevel.L1,
        )
        manifest = make_manifest([rule])
        registry = registry_with({CheckType.HOOK_REGISTERED: result_check(CheckState.PASS)})
        report = aggregator.run_status(
            manifest,
            registry=registry,
            ctx=context(),
            proof_runner=lambda rule, platform, ctx, prove: aggregator.ProofOutcome(
                None, "the hook could not be started", ReasonCode.ERROR
            ),
        )
        result = report.results[0]
        self.assertIs(result.state, CheckState.UNKNOWN)
        self.assertIs(result.reason_code, ReasonCode.ERROR)
        self.assertTrue(contract.is_failing(result))
        self.assertIs(report.summary.overall_verdict, OverallVerdict.FAIL)

    def test_a_proof_that_ran_and_did_not_deny_is_drift(self):
        # The design's step 7:  the hook ran and let the sentinel write through.
        rule = make_rule(
            "mm-hooks",
            ConfigKind.HOOKS,
            check_type=CheckType.HOOK_REGISTERED,
            proof=ProofLevel.L1,
        )
        manifest = make_manifest([rule])
        registry = registry_with({CheckType.HOOK_REGISTERED: result_check(CheckState.PASS)})
        report = aggregator.run_status(
            manifest,
            registry=registry,
            ctx=context(),
            proof_runner=lambda rule, platform, ctx, prove: aggregator.ProofOutcome(
                None, "the hook did not deny the sentinel write", ReasonCode.PROOF_FAILED
            ),
        )
        result = report.results[0]
        self.assertIs(result.state, CheckState.DRIFT)
        self.assertIs(result.reason_code, ReasonCode.PROOF_FAILED)
        self.assertTrue(contract.is_failing(result))

    def test_a_proof_that_achieved_its_level_passes_the_rule(self):
        # The design's step 8.
        rule = make_rule(
            "mm-hooks",
            ConfigKind.HOOKS,
            check_type=CheckType.HOOK_REGISTERED,
            proof=ProofLevel.L1,
        )
        manifest = make_manifest([rule])
        registry = registry_with({CheckType.HOOK_REGISTERED: result_check(CheckState.PASS)})
        report = aggregator.run_status(
            manifest,
            registry=registry,
            ctx=context(),
            proof_runner=lambda rule, platform, ctx, prove: aggregator.ProofOutcome(
                ProofLevel.L1, "the hook denied the sentinel write", ReasonCode.OK
            ),
        )
        result = report.results[0]
        self.assertIs(result.state, CheckState.PASS)
        self.assertIs(result.proof_level_achieved, ProofLevel.L1)
        self.assertIs(report.summary.overall_verdict, OverallVerdict.PASS)

    def test_a_rule_that_needs_no_proof_stays_a_pass(self):
        rule = make_rule("mm-instructions", ConfigKind.INSTRUCTIONS)
        manifest = make_manifest([rule])
        registry = registry_with({CheckType.FILE_HASH: result_check(CheckState.PASS)})
        report = aggregator.run_status(manifest, registry=registry, ctx=context())
        self.assertIs(report.results[0].state, CheckState.PASS)

    def test_a_stale_result_is_relabelled_unknown_with_the_stale_reason(self):
        rule = make_rule("mm-instructions", ConfigKind.INSTRUCTIONS, ttl=900)
        manifest = make_manifest([rule])
        registry = registry_with({CheckType.FILE_HASH: result_check(CheckState.PASS)})
        report = aggregator.run_status(manifest, registry=registry, ctx=context())
        self.assertIs(report.summary.overall_verdict, OverallVerdict.PASS)
        later = MOMENT + timedelta(seconds=901)
        stale = aggregator.mark_stale_results(report, later)
        result = stale.results[0]
        self.assertIs(result.state, CheckState.UNKNOWN)
        self.assertIs(result.reason_code, ReasonCode.STALE)
        self.assertTrue(result.stale)
        self.assertEqual(result.checked_at, "2026-10-19T14:30:00Z")
        self.assertTrue(contract.is_failing(result))
        self.assertIs(stale.summary.overall_verdict, OverallVerdict.FAIL)

    def test_a_fresh_result_is_not_relabelled(self):
        rule = make_rule("mm-instructions", ConfigKind.INSTRUCTIONS, ttl=900)
        manifest = make_manifest([rule])
        registry = registry_with({CheckType.FILE_HASH: result_check(CheckState.PASS)})
        report = aggregator.run_status(manifest, registry=registry, ctx=context())
        fresh = aggregator.mark_stale_results(report, MOMENT + timedelta(seconds=10))
        self.assertIs(fresh.results[0].state, CheckState.PASS)
        self.assertFalse(fresh.results[0].stale)

    def test_an_empty_run_is_unknown_and_never_a_pass(self):
        manifest = make_manifest([])
        report = aggregator.run_status(manifest, registry=registry_with({}), ctx=context())
        self.assertEqual(report.results, ())
        self.assertIs(report.summary.overall_verdict, OverallVerdict.UNKNOWN)

    def test_the_summary_comes_from_the_shared_contract_math(self):
        rules = [
            make_rule("mm-a", ConfigKind.INSTRUCTIONS),
            make_rule("mm-b", ConfigKind.SKILLS, check_type=CheckType.SKILLS_INSTALLED),
        ]
        manifest = make_manifest(rules)

        def state_for(rule, platform, ctx):
            from fleet_config.checks import build_result

            if rule.id == "mm-a":
                return build_result(
                    rule, platform, CheckState.PASS, "fine", ReasonCode.OK, ObservedState(), ctx
                )
            return build_result(
                rule,
                platform,
                CheckState.DRIFT,
                "it differs",
                ReasonCode.DRIFT,
                ObservedState(),
                ctx,
            )

        registry = registry_with(
            {CheckType.FILE_HASH: state_for, CheckType.SKILLS_INSTALLED: state_for}
        )
        report = aggregator.run_status(manifest, registry=registry, ctx=context())
        self.assertEqual(report.summary, contract.summarize(report.results))
        self.assertIs(report.summary.overall_verdict, OverallVerdict.FAIL)

    def test_the_registry_loads_and_names_the_modules_it_could_not_import(self):
        registry, missing = aggregator.load_registry()
        self.assertIsInstance(registry, CheckRegistry)
        self.assertIsInstance(missing, tuple)
        for name in missing:
            self.assertIn(name, set(aggregator.CHECK_MODULE_BY_KIND.values()))

    def test_the_doctor_findings_are_folded_in_as_notes(self):
        import pathlib
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            scripts = pathlib.Path(tmp)
            doctor = scripts / "fleet-tools-doctor.py"
            doctor.write_text(
                "import json\nprint(json.dumps({'ok': True, 'checks': "
                "[{'check': 'runtime', 'status': 'ok', 'detail': 'the runtime is there'}]}))\n",
                encoding="utf-8",
            )
            notes = aggregator.run_doctors(
                context(),
                commands=(("fake", ("fleet-tools-doctor.py", "--json")),),
                scripts_dir=str(scripts),
            )
        self.assertEqual(len(notes), 1)
        self.assertIn("runtime", notes[0])
        self.assertIn("ok", notes[0])

    def test_a_doctor_that_cannot_run_is_a_note_and_not_a_crash(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            notes = aggregator.run_doctors(
                context(),
                commands=(("missing", ("no-such-doctor.py", "--json")),),
                scripts_dir=tmp,
            )
        self.assertEqual(len(notes), 1)
        self.assertIn("not available", notes[0])

    def test_a_check_target_is_never_read_by_the_aggregator(self):
        rule = make_rule(
            "mm-credential",
            ConfigKind.PERMISSIONS,
            check_type=CheckType.CREDENTIAL_STAT,
            expected=ExpectedState(
                description="the credential file is present", present=True, mode="0600"
            ),
        )
        manifest = make_manifest([rule])
        registry = registry_with(
            {
                CheckType.CREDENTIAL_STAT: result_check(
                    CheckState.PASS,
                    observed=ObservedState(present=True, mode="0600", size_bytes=42),
                )
            }
        )
        report = aggregator.run_status(manifest, registry=registry, ctx=context())
        observed = report.results[0].observed
        self.assertTrue(observed.present)
        self.assertEqual(observed.size_bytes, 42)
        self.assertIsNone(getattr(observed, "contents", None))


if __name__ == "__main__":
    unittest.main()
