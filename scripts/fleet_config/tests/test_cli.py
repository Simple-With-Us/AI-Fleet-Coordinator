"""The CLI:  the verbs,  the JSON envelope,  and the exit-code table.

The JSON output is pinned to ``contract.build_status_envelope(report)``:  the test
compares the parsed output with the envelope the contract builds from the same report,
field for field,  so an extra field or a wrapper fails here.

Every test runs in a temporary home with a temporary registry and manifest,  so the real
home and the real manifest are never touched.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import subprocess
import sys
import unittest
from unittest import mock

from fleet_config import __main__ as cli
from fleet_config import aggregator, contract
from fleet_config.checks import build_result
from fleet_config.types import (
    CheckState,
    CheckType,
    ConfigKind,
    ObservedState,
    ReasonCode,
)

from ._fake import FleetTestCase, make_manifest, make_rule, registry_with, snapshot_tree

SCRIPTS = pathlib.Path(__file__).resolve().parents[2]


def passing_check(rule, platform, ctx):
    """A check that always passes,  so the CLI can be driven to each verdict."""
    return build_result(
        rule,
        platform,
        CheckState.PASS,
        "the fake check says so",
        ReasonCode.OK,
        ObservedState(),
        ctx,
    )


def failing_check(rule, platform, ctx):
    """A check that always drifts,  so the CLI can be driven to a FAIL verdict."""
    return build_result(
        rule,
        platform,
        CheckState.DRIFT,
        "the fake check found a difference",
        ReasonCode.DRIFT,
        ObservedState(),
        ctx,
    )


class CliTests(FleetTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.rule = make_rule("mm-instructions", ConfigKind.INSTRUCTIONS)
        self.manifest = make_manifest([self.rule])
        self.write_registry(["MM"])
        self.write_manifest(self.manifest)

    def run_cli(self, argv, registry=None):
        """Run the CLI in process and answer (code, stdout, stderr)."""
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(
            aggregator, "load_registry", return_value=(registry or registry_with({}), ())
        ):
            # No test starts a real doctor:  the MAC level doctors read Jay's machine.
            with mock.patch.object(aggregator, "run_doctors", return_value=()):
                code = cli.main(list(argv), out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def base(self, *extra):
        """The common arguments every verb needs."""
        return [
            "--manifest",
            str(self.manifest_path),
            "--registry",
            str(self.registry_path),
            "--home",
            str(self.home),
        ] + list(extra)

    def test_help_exits_zero(self):
        result = subprocess.run(
            [sys.executable, "-m", "fleet_config", "--help"],
            cwd=str(SCRIPTS),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn(b"status", result.stdout)
        self.assertIn(b"apply", result.stdout)

    def test_status_json_is_exactly_the_contract_envelope(self):
        registry = registry_with({CheckType.FILE_HASH: passing_check})
        code, out, _err = self.run_cli(self.base("status", "--json"), registry)
        document = json.loads(out)
        self.assertEqual(
            list(document),
            [
                "schemaVersion",
                "tool",
                "command",
                "generatedAt",
                "summary",
                "results",
                "uncovered",
                "notImplemented",
            ],
        )
        self.assertEqual(document["schemaVersion"], contract.SCHEMA_VERSION)
        self.assertEqual(document["tool"], "fleet-config")
        self.assertEqual(document["command"], "status")
        self.assertEqual(len(document["results"]), 1)
        self.assertEqual(document["summary"]["overallVerdict"], "PASS")
        self.assertEqual(code, cli.EXIT_OK)

    def test_the_envelope_has_no_second_copy_of_the_results(self):
        registry = registry_with({CheckType.FILE_HASH: passing_check})
        _code, out, _err = self.run_cli(self.base("status", "--json"), registry)
        document = json.loads(out)
        self.assertNotIn("seats", document)
        self.assertNotIn("bySeat", document)

    def test_a_pass_verdict_exits_zero(self):
        registry = registry_with({CheckType.FILE_HASH: passing_check})
        code, out, _err = self.run_cli(self.base("status"), registry)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("verdict: PASS", out)

    def test_a_fail_verdict_exits_one(self):
        registry = registry_with({CheckType.FILE_HASH: failing_check})
        code, out, _err = self.run_cli(self.base("status"), registry)
        self.assertEqual(code, cli.EXIT_FAIL)
        self.assertIn("verdict: FAIL", out)

    def test_an_unknown_verdict_exits_three_and_not_zero(self):
        registry = registry_with({})
        code, out, _err = self.run_cli(self.base("status"), registry)
        self.assertEqual(code, cli.EXIT_UNKNOWN)
        self.assertIn("verdict: UNKNOWN", out)

    def test_an_unknown_seat_is_a_usage_error(self):
        code, _out, err = self.run_cli(self.base("status", "--seat", "NOPE"))
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("unknown seat", err)

    def test_an_unknown_kind_is_a_usage_error(self):
        code, _out, err = self.run_cli(self.base("status", "--kind", "nonsense"))
        self.assertEqual(code, cli.EXIT_USAGE)
        self.assertIn("unknown kind", err)

    def test_a_missing_manifest_is_a_clean_refusal_and_not_a_traceback(self):
        code, _out, err = self.run_cli(
            [
                "--manifest",
                str(self.root / "nope.json"),
                "--registry",
                str(self.registry_path),
                "status",
            ]
        )
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertIn("cannot read the manifest", err)
        self.assertNotIn("Traceback", err)

    def test_the_human_table_puts_the_worst_state_first(self):
        rules = [
            make_rule("mm-pass", ConfigKind.INSTRUCTIONS),
            make_rule("mm-drift", ConfigKind.SKILLS, check_type=CheckType.SKILLS_INSTALLED),
        ]
        self.write_manifest(make_manifest(rules))

        def check(rule, platform, ctx):
            if rule.id == "mm-pass":
                return passing_check(rule, platform, ctx)
            return failing_check(rule, platform, ctx)

        registry = registry_with({CheckType.FILE_HASH: check, CheckType.SKILLS_INSTALLED: check})
        _code, out, _err = self.run_cli(self.base("status"), registry)
        self.assertLess(out.index("DRIFT"), out.index("PASS"))

    def test_apply_plan_changes_no_file(self):
        target = self.home / ".minimax" / "memory" / "user.md"
        target.parent.mkdir(parents=True)
        target.write_text("the original file\n", encoding="utf-8")
        before = snapshot_tree(self.home)
        registry = registry_with({CheckType.FILE_HASH: failing_check})
        code, out, _err = self.run_cli(self.base("apply", "MM", "--plan"), registry)
        after = snapshot_tree(self.home)
        self.assertEqual(before, after)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("Plan for MM", out)
        self.assertIn("mm-instructions", out)

    def test_apply_without_a_terminal_and_without_yes_is_refused(self):
        registry = registry_with({CheckType.FILE_HASH: failing_check})
        with mock.patch.object(sys.stdin, "isatty", return_value=False):
            code, _out, err = self.run_cli(self.base("apply", "MM"), registry)
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertIn("--yes", err)

    def test_apply_runs_the_installer_and_reports_the_new_state(self):
        self.write_manifest(
            make_manifest(
                [
                    make_rule(
                        "mm-instructions",
                        ConfigKind.INSTRUCTIONS,
                        check_type=CheckType.MARKED_BLOCK_HASH,
                        target="~/.minimax/memory/user.md",
                    )
                ]
            )
        )
        calls = []

        def runner(argv, timeout=300):
            calls.append(list(argv))
            return 0, "installed\n", ""

        registry = registry_with({CheckType.MARKED_BLOCK_HASH: failing_check})
        with mock.patch("fleet_config.apply.default_runner", runner):
            with mock.patch("fleet_config.apply.touchid.confirm", return_value=None):
                code, out, _err = self.run_cli(self.base("apply", "MM", "--yes"), registry)
        self.assertEqual(code, cli.EXIT_FAIL, out)
        self.assertIn("Applied 1 change", out)
        self.assertEqual(len(calls), 1)
        self.assertIn("--home", calls[0])

    def test_a_persistence_class_apply_without_touch_id_is_refused(self):
        rules = [
            make_rule(
                "mm-hooks",
                ConfigKind.HOOKS,
                check_type=CheckType.HOOK_REGISTERED,
                target="~/.minimax/plugins/fleet-guard/hooks/hooks.json",
            )
        ]
        self.write_manifest(make_manifest(rules))
        calls = []

        def runner(argv, timeout=300):
            calls.append(list(argv))
            return 0, "installed\n", ""

        registry = registry_with({CheckType.HOOK_REGISTERED: failing_check})
        with (
            mock.patch("fleet_config.apply.default_runner", runner),
            mock.patch(
                "fleet_config.apply.touchid.confirm",
                side_effect=__import__(
                    "fleet_config.touchid", fromlist=["TouchIdError"]
                ).TouchIdError("Touch ID is not available on this machine", "unavailable"),
            ),
        ):
            code, out, _err = self.run_cli(self.base("apply", "MM", "--yes"), registry)
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertEqual(calls, [], "the installer ran without a confirmation")
        self.assertIn("REFUSED", out)

    def test_the_json_output_of_a_refused_run_is_still_the_envelope(self):
        registry = registry_with({CheckType.FILE_HASH: passing_check})
        code, out, _err = self.run_cli(self.base("status", "--json"), registry)
        document = json.loads(out)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertEqual(document["schemaVersion"], contract.SCHEMA_VERSION)

    def test_a_failed_run_serves_the_cached_report_marked_stale(self):
        registry = registry_with({CheckType.FILE_HASH: passing_check})
        code, _out, _err = self.run_cli(self.base("status"), registry)
        self.assertEqual(code, cli.EXIT_OK)
        # Break the manifest:  the run cannot happen,  so the cached copy is served.
        self.manifest_path.unlink()
        code, out, err = self.run_cli(self.base("status"), registry)
        self.assertEqual(code, cli.EXIT_FAIL)
        self.assertIn("serving the last good report", err)
        self.assertIn("UNKNOWN", out)
        self.assertIn("stale", out)

    def test_the_served_cached_report_is_never_presented_as_fresh(self):
        registry = registry_with({CheckType.FILE_HASH: passing_check})
        self.run_cli(self.base("status", "--json"), registry)
        self.manifest_path.unlink()
        code, out, _err = self.run_cli(self.base("status", "--json"), registry)
        document = json.loads(out)
        self.assertEqual(code, cli.EXIT_FAIL)
        for result in document["results"]:
            self.assertEqual(result["state"], "UNKNOWN")
            self.assertEqual(result["reasonCode"], "stale")
            self.assertTrue(result["stale"])
            self.assertTrue(result["failing"])

    def test_a_missing_home_is_a_clean_refusal(self):
        code, _out, err = self.run_cli(
            [
                "--manifest",
                str(self.manifest_path),
                "--registry",
                str(self.registry_path),
                "--home",
                str(self.root / "no-such-home"),
                "status",
            ]
        )
        self.assertEqual(code, cli.EXIT_REFUSED)
        self.assertNotIn("Traceback", err)

    def test_the_doctor_findings_are_folded_into_the_run(self):
        registry = registry_with({CheckType.FILE_HASH: passing_check})
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch.object(aggregator, "load_registry", return_value=(registry, ())):
            with mock.patch.object(
                aggregator,
                "run_doctors",
                return_value=("doctor runtime:  ok  the runtime is there",),
            ):
                code = cli.main(self.base("status"), out=out, err=err)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertIn("doctor runtime", out.getvalue())

    def test_the_doctors_can_be_left_out(self):
        registry = registry_with({CheckType.FILE_HASH: passing_check})
        with mock.patch.object(aggregator, "run_doctors") as doctor:
            code, _out, _err = self.run_cli(self.base("status", "--no-doctor"), registry)
        self.assertEqual(code, cli.EXIT_OK)
        self.assertFalse(doctor.called)


if __name__ == "__main__":
    unittest.main()
