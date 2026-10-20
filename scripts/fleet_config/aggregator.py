"""The engine:  runs a manifest's rules and builds one ``StatusReport``.

``run_status`` walks the manifest, dispatches every rule to its check through the shared
registry (``checks.py``), applies the fail-closed mapping from the design document
(section 4) and returns the report the envelope builder renders.

The rules this module never breaks:

  * A rule is never skipped silently.  A check type with no registered function, a check
    module that is not present, and a collector other than ``mac`` each produce a result
    that says so.
  * A check that raises ``CheckError`` becomes ``UNKNOWN`` with the error's own reason
    code.  It is never a pass, and ``contract.is_failing`` decides what that means.
  * Every result in one report shares one clock.  ``ctx.now`` is the only time source,
    so two results in the same envelope never disagree about when they were checked.
  * A result read after its ``expiresAt`` is re-labelled ``UNKNOWN`` with reason code
    ``stale`` before it reaches the envelope.  The envelope builder does not do this;
    the aggregator owns the clock, as the design document says.

Stdlib only, and Python 3.9 safe, matching ``fleet_knobs``.

The one seam this module expects from another lane is the proof machinery:  it calls
``proof.prove_rule(rule, platform, ctx, prove_l2=<bool>)`` and reads the answer as a
``ProofOutcome``,  a ``(level, reason, reason_code)`` tuple,  or any object with
``level``, ``reason`` and ``reason_code`` attributes.  A missing ``proof.py`` is not an
error;  the rule then reads ``UNPROVEN``,  which is never healthy.
"""
from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import contract
from .checks import (
    CheckContext,
    CheckError,
    CheckRegistry,
    build_result,
    iso_utc,
    resolve_home,
    utc_now,
)
from .types import (
    CheckResult,
    CheckState,
    Collector,
    ConfigKind,
    Manifest,
    ObservedState,
    ProofLevel,
    ReasonCode,
    Rule,
    StatusReport,
)

__all__ = [
    "CHECK_MODULE_BY_KIND",
    "DEFAULT_TIMEOUT",
    "DOCTOR_TIMEOUT",
    "ProofOutcome",
    "load_registry",
    "mark_stale_results",
    "result_expires_at",
    "result_is_expired",
    "run_doctors",
    "run_status",
]

# The seconds a check may take before the aggregator calls it a timeout.  The context
# carries the same number, so a check that spawns a child inherits it.
DEFAULT_TIMEOUT = 10

# The seconds a folded-in doctor may take.  The fast doctor finishes in under two
# seconds;  the lane doctor takes minutes,  so it never runs by default.
DOCTOR_TIMEOUT = 120

# The doctors whose results are folded into a status run.  They are MAC level checks,
# not per-seat ones:  they run once per status run,  never once per seat.
DOCTOR_COMMANDS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("fleet-tools", ("fleet-tools-doctor.py", "--json", "--no-network")),
)

# Which check module implements which kind.  This mirrors the module ownership map in the
# design document (section 10) and lets the aggregator say "the check module
# checks_hooks is not present" instead of a bare "unsupported" when a lane has not landed.
CHECK_MODULE_BY_KIND: Dict[ConfigKind, str] = {
    ConfigKind.INSTRUCTIONS: "checks_instructions",
    ConfigKind.HOOKS: "checks_hooks",
    ConfigKind.MCP: "checks_mcp",
    ConfigKind.SKILLS: "checks_skills",
    ConfigKind.SUBAGENTS: "checks_subagents",
    ConfigKind.PERMISSIONS: "checks_permissions",
}


@dataclass(frozen=True)
class ProofOutcome:
    """What the proof machinery says about one rule.

    ``level`` is the proof level that was actually achieved, or None when no proof ran.
    ``reason_code`` is the machine token:  ``ok`` when the proof passed, ``proof-not-run``
    when it did not run, and ``proof-failed`` when it ran and the hook did not deny.
    """

    level: Optional[ProofLevel]
    reason: str
    reason_code: ReasonCode = ReasonCode.PROOF_NOT_RUN


# A caller-supplied proof function:  (rule, platform, ctx, prove) -> an outcome.
ProofRunner = Callable[[Rule, str, CheckContext, bool], object]

def load_registry() -> Tuple[CheckRegistry, Tuple[str, ...]]:
    """The shared check registry plus the check modules that could not be imported.

    The registry comes from ``checks.default_registry()`` when every check module is
    present.  When one is missing the registry is built module by module instead, so a
    missing module costs only its own kinds instead of the whole run, and the names of
    the missing modules come back so the caller can say which one is absent.
    """
    try:
        from .checks import default_registry

        return default_registry(), ()
    except ImportError:
        pass
    registry = CheckRegistry()
    missing: List[str] = []
    for module_name in sorted(set(CHECK_MODULE_BY_KIND.values())):
        try:
            module = importlib.import_module("." + module_name, __package__)
        except ImportError:
            missing.append(module_name)
            continue
        checks = getattr(module, "CHECKS", None)
        if isinstance(checks, dict):
            for check_type, function in checks.items():
                registry.register(check_type)(function)
    return registry, tuple(missing)


def _unsupported(rule: Rule, platform: str, reason: str, ctx: CheckContext) -> CheckResult:
    """A result that says this platform or collector cannot be checked in this phase.

    The state is ``UNSUPPORTED``, which is never healthy:  it is not a pass, so a run that
    holds one cannot verdict ``PASS``.
    """
    return build_result(
        rule,
        platform,
        CheckState.UNSUPPORTED,
        reason,
        ReasonCode.UNSUPPORTED,
        ObservedState(),
        ctx,
    )


def _unknown(
    rule: Rule,
    platform: str,
    reason: str,
    reason_code: ReasonCode,
    ctx: CheckContext,
) -> CheckResult:
    """A result for a check that could not produce a trustworthy answer.

    The reason code is the failing one (``error``, ``timeout`` or ``unparseable``), so
    ``contract.is_failing`` counts it against the run.  A broken checker never accuses a
    seat of drift it did not observe, and it never reads as healthy either.
    """
    return build_result(
        rule,
        platform,
        CheckState.UNKNOWN,
        reason,
        reason_code,
        ObservedState(),
        ctx,
    )


def _normalize_proof(value: object) -> ProofOutcome:
    """Read the proof machinery's answer in whichever shape it returns.

    ``proof.prove_rule`` may return a ``ProofOutcome``, a ``(level, reason, reason_code)``
    tuple, or any object with ``level``, ``reason`` and ``reason_code`` attributes.  A
    shape the aggregator does not understand reads as "no proof ran", which is fail
    closed rather than an exception in the middle of a run.
    """
    if isinstance(value, ProofOutcome):
        return value
    if isinstance(value, tuple) and value:
        level = value[0] if len(value) > 0 else None
        reason = str(value[1]) if len(value) > 1 else "the proof machinery said nothing"
        code = value[2] if len(value) > 2 else ReasonCode.PROOF_NOT_RUN
        return ProofOutcome(level, reason, code if isinstance(code, ReasonCode) else ReasonCode.PROOF_NOT_RUN)
    level = getattr(value, "level", None)
    reason = getattr(value, "reason", None)
    code = getattr(value, "reason_code", None)
    return ProofOutcome(
        level if isinstance(level, ProofLevel) else None,
        str(reason) if reason else "the proof machinery said nothing",
        code if isinstance(code, ReasonCode) else ReasonCode.PROOF_NOT_RUN,
    )


def _run_proof(
    rule: Rule,
    platform: str,
    ctx: CheckContext,
    prove: bool,
    runner: Optional[ProofRunner] = None,
) -> ProofOutcome:
    """Ask the proof machinery what it can prove about one rule.

    The seam is one function, ``proof.prove_rule(rule, platform, ctx, prove_l2=prove)``,
    so the proof lane can land without the aggregator changing.  A missing ``proof.py``
    or a missing function is not a crash:  the rule then reads ``UNPROVEN`` with reason
    code ``proof-not-run``, which is never healthy.

    ``runner`` replaces the seam with a caller-supplied function, which is how a test
    drives the proof path without a real hook.
    """
    if rule.proof_level is ProofLevel.L0:
        return ProofOutcome(ProofLevel.L0, "no proof is required for this rule", ReasonCode.OK)
    if runner is not None:
        try:
            return _normalize_proof(runner(rule, platform, ctx, prove))
        except CheckError as exc:
            return ProofOutcome(None, str(exc), exc.reason_code)
        except Exception as exc:  # noqa: BLE001 - a proof failure is a result, not a crash
            return ProofOutcome(None, "the proof machinery failed: %s" % exc, ReasonCode.ERROR)
    try:
        from . import proof  # type: ignore[attr-defined]
    except ImportError:
        return ProofOutcome(
            None,
            "the proof machinery (proof.py) is not available in this build",
            ReasonCode.PROOF_NOT_RUN,
        )
    prove_rule = getattr(proof, "prove_rule", None)
    if prove_rule is None:
        return ProofOutcome(
            None,
            "the proof machinery does not expose prove_rule",
            ReasonCode.PROOF_NOT_RUN,
        )
    try:
        outcome = prove_rule(rule, platform, ctx, prove_l2=prove)
    except CheckError as exc:
        return ProofOutcome(None, str(exc), exc.reason_code)
    except Exception as exc:  # noqa: BLE001 - a proof failure is a result, not a crash
        return ProofOutcome(None, "the proof machinery failed: %s" % exc, ReasonCode.ERROR)
    return _normalize_proof(outcome)


def _apply_proof(
    result: CheckResult,
    rule: Rule,
    platform: str,
    ctx: CheckContext,
    prove: bool,
    runner: Optional[ProofRunner] = None,
) -> CheckResult:
    """Fold the proof outcome into a check result.

    A check that did not pass is left alone:  drift is drift, and a proof never hides it.
    A passing check whose required proof was not achieved becomes ``UNPROVEN`` (never
    healthy) or ``DRIFT`` with reason code ``proof-failed`` (failing), as the design
    document's steps 6 and 7 say.
    """
    if result.state is not CheckState.PASS:
        return result
    required = rule.proof_level
    if required is ProofLevel.L0:
        return result
    achieved = result.proof_level_achieved
    if achieved is not None and achieved.at_least(required):
        return result
    outcome = _run_proof(rule, platform, ctx, prove, runner)
    if outcome.level is not None and outcome.level.at_least(required):
        return replace(
            result,
            proof_level_achieved=outcome.level,
            reason="%s  Proof %s achieved." % (result.reason, outcome.level.value),
            reason_code=ReasonCode.OK,
        )
    if outcome.reason_code is ReasonCode.PROOF_FAILED:
        return replace(
            result,
            state=CheckState.DRIFT,
            reason=outcome.reason or "the required proof ran and did not deny the sentinel write",
            reason_code=ReasonCode.PROOF_FAILED,
            proof_level_achieved=outcome.level if outcome.level is not None else achieved,
        )
    if outcome.reason_code in contract.FAILING_REASON_CODES:
        # The proof machinery itself could not answer:  the hook crashed, timed out, or
        # said something unreadable.  That is the design's step 4, and step 4 is FAILING.
        # Folding it into UNPROVEN would make a broken hook look like an unrun proof,
        # and an unrun proof is not counted against the run.
        return replace(
            result,
            state=CheckState.UNKNOWN,
            reason=outcome.reason or "the required proof could not be completed",
            reason_code=outcome.reason_code,
            proof_level_achieved=outcome.level if outcome.level is not None else achieved,
        )
    return replace(
        result,
        state=CheckState.UNPROVEN,
        reason=outcome.reason or "the required proof was not run",
        reason_code=ReasonCode.PROOF_NOT_RUN,
        proof_level_achieved=outcome.level if outcome.level is not None else achieved,
    )


def _run_check(
    rule: Rule,
    platform: str,
    ctx: CheckContext,
    registry: CheckRegistry,
    missing_modules: Tuple[str, ...],
) -> CheckResult:
    """Run one rule's check and return its result, whatever happens.

    Nothing here raises:  a missing module, an unregistered check type, a raised
    ``CheckError``, a timeout and a malformed return value all become results that say
    what went wrong.
    """
    if rule.collector is not Collector.MAC:
        return _unsupported(
            rule,
            platform,
            "the collector %s is not implemented in this phase;  only mac is" % rule.collector.value,
            ctx,
        )
    module_name = CHECK_MODULE_BY_KIND.get(rule.kind)
    if module_name is not None and module_name in missing_modules:
        return _unknown(
            rule,
            platform,
            "the check module %s.py is not available in this build" % module_name,
            ReasonCode.ERROR,
            ctx,
        )
    check = registry.get(rule.check.type)
    if check is None:
        return _unsupported(
            rule,
            platform,
            "no check is registered for the check type %s" % rule.check.type.value,
            ctx,
        )
    try:
        result = check(rule, platform, ctx)
    except CheckError as exc:
        return _unknown(rule, platform, str(exc), exc.reason_code, ctx)
    except subprocess.TimeoutExpired:
        return _unknown(
            rule,
            platform,
            "the check for %s timed out after %d seconds" % (rule.check.type.value, ctx.timeout),
            ReasonCode.TIMEOUT,
            ctx,
        )
    except Exception as exc:  # noqa: BLE001 - a broken check is a result, not a crash
        return _unknown(
            rule,
            platform,
            "the check for %s failed: %s" % (rule.check.type.value, exc),
            ReasonCode.ERROR,
            ctx,
        )
    if not isinstance(result, CheckResult):
        return _unknown(
            rule,
            platform,
            "the check for %s returned %s instead of a CheckResult"
            % (rule.check.type.value, type(result).__name__),
            ReasonCode.UNPARSEABLE,
            ctx,
        )
    return result


def _scripts_dir() -> str:
    """The directory the package's sibling scripts live in."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_doctors(
    ctx: CheckContext,
    *,
    commands: Optional[Sequence[Tuple[str, Tuple[str, ...]]]] = None,
    scripts_dir: Optional[str] = None,
    timeout: int = DOCTOR_TIMEOUT,
) -> Tuple[str, ...]:
    """Run the MAC level doctors once and return their findings as notes.

    The doctors are the fleet's own:  ``fleet-tools-doctor.py`` answers in under two
    seconds with ``--json``,  and the lane doctor takes minutes,  so it is never in this
    list by default.  They are MAC level checks,  so they run once per status run and
    never once per seat.

    Their findings land in the report's notes rather than in its results:  the pinned
    vocabulary has no doctor check type and no seventh config kind,  so a doctor row
    could not be put in the envelope without inventing one.  A doctor that cannot run
    is a note too,  never a crash.

    ``scripts_dir`` names where the doctor scripts live,  and ``commands`` names which
    doctors to run;  both are parameters so a test can point them at a temporary
    directory instead of this checkout.
    """
    selected = commands if commands is not None else DOCTOR_COMMANDS
    notes: List[str] = []
    scripts = scripts_dir if scripts_dir is not None else _scripts_dir()
    for name, argv in selected:
        path = os.path.join(scripts, argv[0])
        if not os.path.isfile(path):
            notes.append("the %s doctor is not available at %s" % (name, path))
            continue
        try:
            result = subprocess.run(
                [sys.executable] + list(argv),
                cwd=scripts,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                env=ctx.child_env(),
                check=False,
            )
        except subprocess.TimeoutExpired:
            notes.append("the %s doctor did not finish within %d seconds" % (name, timeout))
            continue
        except OSError as exc:
            notes.append("the %s doctor could not be started: %s" % (name, exc))
            continue
        if result.returncode != 0:
            detail = (result.stderr or b"").decode("utf-8", "replace").strip()
            notes.append(
                "the %s doctor exited %d:  %s" % (name, result.returncode, detail or "no reason given")
            )
            continue
        try:
            document = json.loads(result.stdout.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            notes.append("the %s doctor did not answer with JSON" % name)
            continue
        rows = document.get("checks") if isinstance(document, dict) else None
        if not isinstance(rows, list):
            notes.append("the %s doctor answered with no checks" % name)
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            notes.append(
                "doctor %s:  %s  %s"
                % (
                    str(row.get("check") or "check"),
                    str(row.get("status") or "unknown"),
                    str(row.get("detail") or ""),
                )
            )
    return tuple(notes)


def run_status(
    manifest: Manifest,
    *,
    seat: Optional[str] = None,
    kind: Optional[ConfigKind] = None,
    platform: Optional[str] = None,
    ctx: Optional[CheckContext] = None,
    prove: bool = False,
    registry: Optional[CheckRegistry] = None,
    home: Optional[str] = None,
    missing_modules: Optional[Sequence[str]] = None,
    include_doctors: bool = False,
    proof_runner: Optional[ProofRunner] = None,
) -> StatusReport:
    """Run every rule in `manifest` and build one ``StatusReport``.

    ``seat``, ``kind`` and ``platform`` narrow the run;  a rule that does not name the
    requested platform is skipped, and a rule of another kind or another seat is
    skipped.  ``ctx`` supplies the home, the clock and the timeout;  when it is absent
    one is built from the environment and one clock is taken for the whole run.
    ``prove`` turns on the expensive proof, which is off by default.

    ``registry`` is the dispatch table;  when it is absent the shared registry is loaded,
    and a check module that is not present costs only its own kinds.  ``missing_modules``
    names the check modules that are known to be absent;  it is discovered by default and
    a caller may set it to say which are missing.  ``include_doctors`` folds the MAC
    level doctors into the report's notes;  it is off by default because it starts a
    child process.  ``proof_runner`` replaces the proof seam with a caller-supplied
    function, which is how a test drives the proof path without a real hook.  The
    report's summary comes from ``contract.summarize``, so the aggregator and the
    envelope share one definition of failing.
    """
    if ctx is None:
        ctx = CheckContext(home=resolve_home(home), now=utc_now(), timeout=DEFAULT_TIMEOUT)
    missing: Tuple[str, ...] = ()
    if registry is None:
        registry, missing = load_registry()
    if missing_modules is not None:
        missing = tuple(missing_modules)
    notes: List[str] = []
    for name in missing:
        notes.append("the check module %s.py is not available in this build" % name)
    results: List[CheckResult] = []
    for seat_manifest in manifest.seats:
        if seat is not None and seat_manifest.seat != seat:
            continue
        for rule in seat_manifest.rules:
            if kind is not None and rule.kind is not kind:
                continue
            if platform is not None and platform not in rule.platforms:
                continue
            for rule_platform in rule.platforms:
                if platform is not None and rule_platform != platform:
                    continue
                result = _run_check(rule, rule_platform, ctx, registry, missing)
                result = _apply_proof(result, rule, rule_platform, ctx, prove, proof_runner)
                results.append(result)
    if include_doctors:
        notes.extend(run_doctors(ctx))
    frozen = tuple(results)
    return StatusReport(
        schema_version=contract.SCHEMA_VERSION,
        generated_at=iso_utc(ctx.now),
        results=frozen,
        summary=contract.summarize(frozen),
        manifest_source=manifest.source_path,
        notes=tuple(notes),
    )


def result_expires_at(result: CheckResult) -> Optional[datetime]:
    """The moment a result goes stale, parsed from its ``expiresAt`` stamp.

    None when the stamp cannot be parsed, which the caller reads as "cannot tell", never
    as "fresh".
    """
    try:
        return datetime.strptime(result.expires_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def result_is_expired(result: CheckResult, now: Optional[datetime] = None) -> bool:
    """True when `result` is past its TTL at `now`.

    A result whose expiry cannot be read counts as expired, so an unreadable stamp never
    reads as fresh.
    """
    moment = now if now is not None else utc_now()
    expires = result_expires_at(result)
    if expires is None:
        return True
    return moment >= expires


def mark_stale_results(
    report: StatusReport,
    now: Optional[datetime] = None,
    force: bool = False,
) -> StatusReport:
    """Re-label every result whose TTL has passed as ``UNKNOWN`` with reason ``stale``.

    The result keeps its original ``checkedAt`` as evidence and its original state in
    the reason text, so a consumer can still see what the check said when it ran.  The
    summary is rebuilt with ``contract.summarize``, so a report holding one stale result
    can no longer verdict ``PASS``.

    ``force`` re-labels every result, expired or not.  It is what the CLI uses when it
    serves the cached copy because a run could not happen:  a copy served in place of a
    failed run is stale whether or not its own TTL has passed, because it is not the
    answer to the question that was asked.
    """
    moment = now if now is not None else utc_now()
    relabelled: List[CheckResult] = []
    for result in report.results:
        if not force and not result_is_expired(result, moment):
            relabelled.append(result)
            continue
        relabelled.append(
            replace(
                result,
                state=CheckState.UNKNOWN,
                reason=(
                    "this result comes from the cached copy and is stale:  %s" % result.reason
                    if force
                    else "the result is past its %d second TTL:  %s"
                    % (result.ttl_seconds, result.reason)
                ),
                reason_code=ReasonCode.STALE,
                stale=True,
            )
        )
    frozen = tuple(relabelled)
    return StatusReport(
        schema_version=report.schema_version,
        generated_at=report.generated_at,
        results=frozen,
        summary=contract.summarize(frozen),
        manifest_source=report.manifest_source,
        notes=report.notes,
    )
