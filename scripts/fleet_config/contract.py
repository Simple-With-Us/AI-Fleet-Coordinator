"""The JSON contract for ``fleet-config status``:  the envelope and the verdict math.

This module is the contract, not the engine.  It renders a ``StatusReport`` (types.py)
into the pinned envelope and it owns the fail-closed rules in pure functions, so the
aggregator, the CLI and the contract tests all share one definition of failing.

Pinned rules (design doc, sections 3 and 4):

  * ``SCHEMA_VERSION`` never changes in place.  A breaking envelope change is a new
    version, and the contract test pins the exact integer.
  * The envelope never carries a secret value.  ``ObservedState`` has no field that
    could hold contents, and credential checks report ``stat`` fields only.
  * A stale, errored, timed out or unparseable check counts as failing, never as
    passing.  ``UNKNOWN`` never reads as healthy:  only an all-``PASS`` run is ``PASS``.

Stdlib only, and Python 3.9 safe, matching ``fleet_knobs``.
"""
from __future__ import annotations

from typing import Dict, Optional, Tuple

from .types import (
    CHECK_STATES,
    CheckResult,
    CheckState,
    ExpectedState,
    ObservedState,
    OverallVerdict,
    ReasonCode,
    StatusReport,
    StatusSummary,
)

__all__ = [
    "SCHEMA_VERSION",
    "FAILING_REASON_CODES",
    "is_failing",
    "overall_verdict",
    "summarize",
    "expected_to_dict",
    "observed_to_dict",
    "result_to_dict",
    "build_status_envelope",
]

# The pinned envelope version.  A contract test asserts this exact value, so a breaking
# change must raise it deliberately and update the design doc in the same commit.
SCHEMA_VERSION = 1

# Reason codes that make a result failing even when its state is UNKNOWN.  This set is
# the fail-closed rule written as data.
FAILING_REASON_CODES: Tuple[ReasonCode, ...] = (
    ReasonCode.STALE,
    ReasonCode.ERROR,
    ReasonCode.TIMEOUT,
    ReasonCode.UNPARSEABLE,
    ReasonCode.PROOF_FAILED,
)


def is_failing(result: CheckResult) -> bool:
    """True when `result` must count against the run.  Fail closed, always.

    ``DRIFT`` and ``MISSING`` fail on their state alone.  An ``UNKNOWN`` result fails
    when its reason code is stale, error, timeout, unparseable or a failed proof.
    """
    if result.state in (CheckState.DRIFT, CheckState.MISSING):
        return True
    return result.reason_code in FAILING_REASON_CODES


def overall_verdict(results: Tuple[CheckResult, ...]) -> OverallVerdict:
    """The verdict for a set of results.  An empty run is ``UNKNOWN``, never ``PASS``."""
    if any(is_failing(result) for result in results):
        return OverallVerdict.FAIL
    if results and all(result.state is CheckState.PASS for result in results):
        return OverallVerdict.PASS
    return OverallVerdict.UNKNOWN


def summarize(results: Tuple[CheckResult, ...]) -> StatusSummary:
    """Counts per state (all six keys, always) plus the overall verdict."""
    by_state: Dict[CheckState, int] = {state: 0 for state in CHECK_STATES}
    for result in results:
        by_state[result.state] = by_state.get(result.state, 0) + 1
    return StatusSummary(
        total=len(results),
        by_state=by_state,
        overall_verdict=overall_verdict(results),
    )


def expected_to_dict(expected: ExpectedState) -> Dict[str, object]:
    """The JSON form of an expectation.  Set fields only, and names and booleans only."""
    out: Dict[str, object] = {"description": expected.description}
    if expected.present is not None:
        out["present"] = expected.present
    if expected.sha256 is not None:
        out["sha256"] = expected.sha256
    if expected.names:
        out["names"] = list(expected.names)
    if expected.mode is not None:
        out["mode"] = expected.mode
    if expected.timeout_seconds is not None:
        out["timeoutSeconds"] = expected.timeout_seconds
    return out


def observed_to_dict(observed: ObservedState) -> Dict[str, object]:
    """The JSON form of an observation.  Credential entries carry ``stat`` fields only."""
    out: Dict[str, object] = {}
    if observed.present is not None:
        out["present"] = observed.present
    if observed.sha256 is not None:
        out["sha256"] = observed.sha256
    if observed.names:
        out["names"] = list(observed.names)
    if observed.mode is not None:
        out["mode"] = observed.mode
    if observed.size_bytes is not None:
        out["sizeBytes"] = observed.size_bytes
    if observed.mtime is not None:
        out["mtime"] = observed.mtime
    if observed.timeout_seconds is not None:
        out["timeoutSeconds"] = observed.timeout_seconds
    return out


def result_to_dict(result: CheckResult) -> Dict[str, object]:
    """One result row of the envelope, in the pinned field order."""
    return {
        "ruleId": result.rule_id,
        "seat": result.seat,
        "platform": result.platform,
        "kind": result.kind.value,
        "state": result.state.value,
        "reason": result.reason,
        "reasonCode": result.reason_code.value,
        "expected": expected_to_dict(result.expected),
        "observed": observed_to_dict(result.observed),
        "proofLevelRequired": result.proof_level_required.value,
        "proofLevelAchieved": (
            result.proof_level_achieved.value if result.proof_level_achieved else None
        ),
        "collector": result.collector.value,
        "checkType": result.check_type.value,
        "checkedAt": result.checked_at,
        "expiresAt": result.expires_at,
        "ttlSeconds": result.ttl_seconds,
        "stale": result.stale,
        "failing": is_failing(result),
    }


def build_status_envelope(report: StatusReport) -> Dict[str, object]:
    """The pinned ``fleet-config status --json`` envelope for one report.

    The top-level keys are ``schemaVersion``, ``tool``, ``command``, ``generatedAt``,
    ``summary`` and ``results``, in that order.  Consumers group the flat ``results``
    list by seat, platform and kind;  the envelope deliberately carries no second,
    grouped copy that could disagree with it.
    """
    summary = report.summary
    return {
        "schemaVersion": SCHEMA_VERSION,
        "tool": "fleet-config",
        "command": "status",
        "generatedAt": report.generated_at,
        "summary": {
            "total": summary.total,
            "byState": {state.value: summary.by_state.get(state, 0) for state in CHECK_STATES},
            "overallVerdict": summary.overall_verdict.value,
            "failing": sum(1 for result in report.results if is_failing(result)),
        },
        "results": [result_to_dict(result) for result in report.results],
    }
