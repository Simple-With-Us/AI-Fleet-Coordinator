"""The four contract tests the design pins,  and the tests for what AFC-416 changed.

The design doc (section 13) names four contract-level tests and says they are not
optional:  the names are the pin,  because the names are what a reader greps for and
what a future change has to find.

    cd scripts && python3 -m unittest fleet_config.tests.test_contract_pins -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fleet_config import checks_instructions, contract
from fleet_config.checks import CheckContext, sha256_text
from fleet_config.types import (
    CheckResult,
    CheckState,
    CheckType,
    Collector,
    ConfigKind,
    ExpectedState,
    ObservedState,
    OverallVerdict,
    ProofLevel,
    ReasonCode,
    StatusReport,
    StatusSummary,
)

FIXED_NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)


def _result(
    state: CheckState = CheckState.PASS,
    reason_code: ReasonCode = ReasonCode.OK,
    stale: bool = False,
) -> CheckResult:
    """One result row for the verdict tests."""
    return CheckResult(
        rule_id="rule-1",
        seat="CLAUDE",
        platform="claude",
        kind=ConfigKind.INSTRUCTIONS,
        state=state,
        reason="because",
        reason_code=reason_code,
        expected=ExpectedState(description="d"),
        observed=ObservedState(present=True),
        proof_level_required=ProofLevel.L0,
        proof_level_achieved=ProofLevel.L0,
        collector=Collector.MAC,
        check_type=CheckType.FILE_HASH,
        checked_at="2026-10-21T12:00:00Z",
        expires_at="2026-10-21T12:15:00Z",
        ttl_seconds=900,
        stale=stale,
    )


def _report(results: tuple[CheckResult, ...]) -> StatusReport:
    """One report around the given results."""
    return StatusReport(
        schema_version=contract.SCHEMA_VERSION,
        generated_at="2026-10-21T12:00:00Z",
        results=results,
        summary=contract.summarize(results),
    )


class ContractPinTests(unittest.TestCase):
    """The four tests the design names.  Their names are the pin."""

    def test_schema_version_is_pinned(self):
        """The envelope version is an exact integer,  and the envelope carries it.

        The version moved from 1 to 2 when the rule citation and the uncovered list
        were added;  a version never changes in place,  so a bump is a new number and
        this test is the thing that has to be edited with it.
        """
        self.assertEqual(contract.SCHEMA_VERSION, 2)
        envelope = contract.build_status_envelope(_report((_result(),)))
        self.assertEqual(envelope["schemaVersion"], contract.SCHEMA_VERSION)

    def test_envelope_never_carries_a_secret(self):
        """A credential rule's envelope carries stat facts,  and never the contents.

        The fixture writes a known sentinel string as the file's contents and asserts
        that the string is nowhere in the serialized envelope.
        """
        sentinel = "SENTINEL-SECRET-VALUE-0123456789"
        with tempfile.TemporaryDirectory(prefix="fleet-config-pin-") as tmp:
            credential = Path(tmp) / "credential.txt"
            credential.write_text(sentinel, encoding="utf-8")
            os.chmod(credential, 0o600)
            stat_info = credential.stat()
            result = CheckResult(
                rule_id="claude-credential-zulip",
                seat="CLAUDE",
                platform="claude",
                kind=ConfigKind.PERMISSIONS,
                state=CheckState.PASS,
                reason="the credential is present and owner-only",
                reason_code=ReasonCode.OK,
                expected=ExpectedState(description="the credential is present", present=True),
                observed=ObservedState(
                    present=True,
                    mode=stat_info.st_mode & 0o777,
                    size_bytes=stat_info.st_size,
                    mtime=stat_info.st_mtime,
                ),
                proof_level_required=ProofLevel.L0,
                proof_level_achieved=ProofLevel.L0,
                collector=Collector.MAC,
                check_type=CheckType.CREDENTIAL_STAT,
                checked_at="2026-10-21T12:00:00Z",
                expires_at="2026-10-21T12:15:00Z",
                ttl_seconds=900,
            )
            envelope = contract.build_status_envelope(_report((result,)))
        serialized = json.dumps(envelope, sort_keys=True)
        self.assertNotIn(sentinel, serialized)
        row = envelope["results"][0]
        self.assertEqual(row["observed"]["mode"], 0o600)
        self.assertEqual(row["observed"]["sizeBytes"], len(sentinel))
        self.assertIn("mtime", row["observed"])

    def test_fail_closed_mapping(self):
        """Stale, error, timeout and unparseable are failing,  and unknown is not pass.

        An all-UNKNOWN run and an empty run both verdict UNKNOWN,  never PASS.
        """
        for code in (
            ReasonCode.STALE,
            ReasonCode.ERROR,
            ReasonCode.TIMEOUT,
            ReasonCode.UNPARSEABLE,
        ):
            with self.subTest(code=code):
                result = _result(state=CheckState.UNKNOWN, reason_code=code)
                self.assertTrue(contract.is_failing(result))
        # An all-UNKNOWN run whose results carry the bare UNKNOWN reason is not
        # failing,  so its verdict is UNKNOWN.  The same run whose results carry a
        # failing reason is FAIL,  because fail-closed wins over the summary.
        all_unknown = (
            _result(state=CheckState.UNKNOWN, reason_code=ReasonCode.UNKNOWN),
            _result(state=CheckState.UNKNOWN, reason_code=ReasonCode.UNKNOWN),
        )
        self.assertIs(contract.overall_verdict(all_unknown), OverallVerdict.UNKNOWN)
        self.assertIs(contract.overall_verdict(()), OverallVerdict.UNKNOWN)
        failing_unknown = (
            _result(state=CheckState.UNKNOWN, reason_code=ReasonCode.ERROR),
        )
        self.assertIs(contract.overall_verdict(failing_unknown), OverallVerdict.FAIL)
        passing = (_result(),)
        self.assertIs(contract.overall_verdict(passing), OverallVerdict.PASS)
        self.assertIsNot(contract.overall_verdict(all_unknown), OverallVerdict.PASS)

    def test_marked_block_hash_parity(self):
        """The package's marked-block digest and the installer's are the same digest.

        The package may not import ``fleet_lanes`` at runtime,  so this test is the
        only thing that keeps the two implementations agreeing.
        """
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        saved = list(sys.path)
        try:
            sys.path.insert(0, here)
            from fleet_lanes import marked_block
        finally:
            sys.path[:] = saved
        text = (
            "before\n"
            "# fleet:begin fleet-lane-map\n"
            "inside the block\n"
            "# fleet:end fleet-lane-map\n"
            "after\n"
        )
        package_body = checks_instructions.marked_block_body(text, "fleet-lane-map")
        installer_body = marked_block.block_body(text, "fleet-lane-map")
        self.assertIsNotNone(package_body)
        self.assertEqual(package_body, installer_body)
        self.assertEqual(
            checks_instructions.block_body_hash(package_body),
            sha256_text("\n".join(installer_body) + "\n"),
        )


class SourceCitationTests(unittest.TestCase):
    """The rule citation is carried into the envelope,  and it is its own field."""

    def test_the_citation_is_carried_into_the_result(self):
        result = _result()
        cited = CheckResult(
            rule_id=result.rule_id,
            seat=result.seat,
            platform=result.platform,
            kind=result.kind,
            state=result.state,
            reason=result.reason,
            reason_code=result.reason_code,
            expected=result.expected,
            observed=result.observed,
            proof_level_required=result.proof_level_required,
            proof_level_achieved=result.proof_level_achieved,
            collector=result.collector,
            check_type=result.check_type,
            checked_at=result.checked_at,
            expires_at=result.expires_at,
            ttl_seconds=result.ttl_seconds,
            source="AFC-384; docs/plans/2026-10-19-fleet-config-design.md section 6.1",
        )
        envelope = contract.build_status_envelope(_report((cited,)))
        self.assertEqual(
            envelope["results"][0]["source"],
            "AFC-384; docs/plans/2026-10-19-fleet-config-design.md section 6.1",
        )

    def test_a_rule_with_no_citation_carries_an_empty_one(self):
        envelope = contract.build_status_envelope(_report((_result(),)))
        self.assertEqual(envelope["results"][0]["source"], "")

    def test_the_envelope_carries_the_uncovered_surfaces(self):
        from fleet_config.types import UncoveredSurface

        report = StatusReport(
            schema_version=contract.SCHEMA_VERSION,
            generated_at="2026-10-21T12:00:00Z",
            results=(_result(),),
            summary=StatusSummary(
                total=1, by_state={CheckState.PASS: 1}, overall_verdict=OverallVerdict.PASS
            ),
            uncovered=(
                UncoveredSurface(
                    "~/.minimax/config.json",
                    "the channel configuration fits none of the six kinds",
                    "MM",
                ),
            ),
        )
        envelope = contract.build_status_envelope(report)
        self.assertEqual(
            envelope["uncovered"],
            [
                {
                    "seat": "MM",
                    "surface": "~/.minimax/config.json",
                    "reason": "the channel configuration fits none of the six kinds",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
