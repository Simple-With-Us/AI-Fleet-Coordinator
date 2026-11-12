"""Collector dispatch for ``fleet_config``:  where a check runs.

A collector says where a check runs.  Only ``mac`` is implemented in this phase, and
a rule that names a reserved collector (``vm``, ``hetzner``, ``api``) yields one
``UNSUPPORTED`` result with the reason naming that collector.  The values are
reserved in ``types.py`` so a later phase adds collectors without a schema change.

The mac collector runs a check by looking its type up in the ``CheckRegistry`` from
``checks.py`` and calling the function that implements it.  The six per-kind check
modules are imported lazily, inside the function that needs them, so this module
loads while they are still being written, and a missing one is a ``CheckError``:
the aggregator turns that into a failing result, never into a silent pass.

The proof machinery is not called from here.  L1 and L2 live in ``proof.py`` and the
aggregator applies them, in the design's step order, after the L0 result exists.

Stdlib only, and Python 3.9 safe, matching ``fleet_knobs``.
"""

from __future__ import annotations

from .checks import CheckContext, CheckError, CheckRegistry, build_result, default_registry
from .types import CheckResult, CheckState, Collector, ObservedState, ReasonCode, Rule

__all__ = [
    "collect",
    "mac_collect",
    "registry_for",
    "unsupported_collector",
]


def registry_for() -> CheckRegistry:
    """The check registry, built on demand.

    The six per-kind check modules are imported here, not when this module is
    imported, so a missing one is a ``CheckError`` at the moment a check is asked
    for, with the reason the aggregator turns into a failing result.  Nothing is
    stubbed and nothing is skipped.
    """
    try:
        return default_registry()
    except ImportError as exc:
        raise CheckError("the fleet_config check modules are not complete yet: %s" % exc) from exc


def unsupported_collector(rule: Rule, platform: str, ctx: CheckContext) -> CheckResult:
    """The pinned result for a collector this phase does not implement.

    It is ``UNSUPPORTED``, which is never healthy:  the run cannot be a pass while a
    rule is unchecked.  The reason names the collector, so the envelope says which
    one is waiting for a later phase.
    """
    reason = (
        "the %s collector is not implemented in this phase, so this rule is not checked here"
        % rule.collector.value
    )
    return build_result(
        rule,
        platform,
        CheckState.UNSUPPORTED,
        reason,
        ReasonCode.UNSUPPORTED,
        ObservedState(),
        ctx,
    )


def mac_collect(
    rule: Rule,
    platform: str,
    ctx: CheckContext,
    registry: CheckRegistry | None = None,
) -> CheckResult:
    """Run a rule's check on the mac collector.

    The check function comes from the registry:  the check type names it, and the
    function has the one signature every check module exposes.  A check type with no
    registered function is not skipped silently:  it is an ``UNSUPPORTED`` result
    naming the missing implementation, which is the fail-closed outcome.

    ``registry`` is a seam for a caller that has one already, and for a test;  the
    default builds the real one, which imports the per-kind check modules.
    """
    table = registry if registry is not None else registry_for()
    function = table.get(rule.check.type)
    if function is None:
        reason = "no check is registered for the check type %s" % rule.check.type.value
        return build_result(
            rule,
            platform,
            CheckState.UNSUPPORTED,
            reason,
            ReasonCode.UNSUPPORTED,
            ObservedState(),
            ctx,
        )
    return function(rule, platform, ctx)


def collect(rule: Rule, platform: str, ctx: CheckContext) -> CheckResult:
    """Run one rule on one platform with the collector the rule names.

    The mac collector runs the check;  a reserved collector yields the pinned
    ``UNSUPPORTED`` result.
    """
    if rule.collector is not Collector.MAC:
        return unsupported_collector(rule, platform, ctx)
    return mac_collect(rule, platform, ctx)
