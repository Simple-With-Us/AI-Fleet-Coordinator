"""``subagents`` checks:  the sub-agent definitions installed for a platform.

The check type is ``SUBAGENTS_INSTALLED``.  The target is the sub-agent definition
directory and the expected names are ``rule.expected.names``.  A definition counts as
installed when the target directory holds an entry with that name, whether that entry
is a file (the usual shape, one Markdown file per definition) or a directory.  A file
named ``<name>.md`` also counts as the definition ``<name>``.

Only the names are read.  The body of a definition is never opened, so a definition
that holds a credential of its own cannot leak into a result.

A rule with no ``expected.names`` is a presence check:  the directory must be there,
and the names it holds are reported.

Stdlib only, and Python 3.9 safe, matching ``fleet_knobs``.
"""

from __future__ import annotations

import os

from .checks import CheckContext, CheckError, build_result, expand_target
from .types import (
    CheckResult,
    CheckState,
    CheckType,
    ObservedState,
    ProofLevel,
    ReasonCode,
    Rule,
)

__all__ = ["CHECKS"]


def installed_names(directory: str) -> tuple[str, ...]:
    """The sub-agent names a directory holds, sorted.

    A directory entry counts under its own name, and a ``.md`` file also counts under
    its stem.  Dot files are not definitions and are left out.
    """
    found: set[str] = set()
    for entry in os.listdir(directory):
        if entry.startswith("."):
            continue
        found.add(entry)
        if entry.endswith(".md"):
            found.add(entry[: -len(".md")])
    return tuple(sorted(found))


def check_subagents_installed(rule: Rule, platform: str, ctx: CheckContext) -> CheckResult:
    """Check that the expected sub-agent definitions are installed in a directory."""
    target = rule.check.target
    try:
        path = expand_target(target, ctx.home)
    except CheckError as exc:
        return build_result(
            rule,
            platform,
            CheckState.MISSING,
            str(exc),
            ReasonCode.MISSING,
            ObservedState(present=False),
            ctx,
        )

    if not os.path.exists(path):
        if rule.expected.present is False:
            return build_result(
                rule,
                platform,
                CheckState.PASS,
                "The sub-agent directory %s is absent, as the rule expects." % target,
                ReasonCode.OK,
                ObservedState(present=False),
                ctx,
                proof_level_achieved=ProofLevel.L0,
            )
        return build_result(
            rule,
            platform,
            CheckState.MISSING,
            "The sub-agent directory %s is missing." % target,
            ReasonCode.MISSING,
            ObservedState(present=False),
            ctx,
        )

    if not os.path.isdir(path):
        return build_result(
            rule,
            platform,
            CheckState.UNKNOWN,
            "The sub-agent target %s is not a directory." % target,
            ReasonCode.ERROR,
            ObservedState(present=None),
            ctx,
        )

    try:
        names = installed_names(path)
    except OSError as exc:
        return build_result(
            rule,
            platform,
            CheckState.UNKNOWN,
            "The sub-agent directory %s cannot be read:  %s"
            % (target, exc.strerror or "unknown error"),
            ReasonCode.ERROR,
            ObservedState(present=None),
            ctx,
        )

    expected_names = tuple(rule.expected.names)
    present = set(names)
    matched = sorted(name for name in expected_names if name in present)
    missing = [name for name in expected_names if name not in present]
    others = len([name for name in names if name not in set(expected_names)])

    if rule.expected.present is False:
        return build_result(
            rule,
            platform,
            CheckState.DRIFT,
            "The sub-agent directory %s exists, which the rule says must be absent." % target,
            ReasonCode.DRIFT,
            ObservedState(present=True, names=tuple(matched)),
            ctx,
        )

    observed = ObservedState(present=True, names=tuple(matched))

    if missing:
        return build_result(
            rule,
            platform,
            CheckState.DRIFT,
            "The sub-agent %s is not installed in %s."
            % (", ".join(repr(name) for name in missing), target),
            ReasonCode.DRIFT,
            observed,
            ctx,
        )

    if not expected_names:
        return build_result(
            rule,
            platform,
            CheckState.PASS,
            "The sub-agent directory %s is present and holds %d definition." % (target, len(names)),
            ReasonCode.OK,
            observed,
            ctx,
            proof_level_achieved=ProofLevel.L0,
        )

    reason = "The sub-agent %s is installed in %s." % (
        ", ".join(repr(name) for name in matched),
        target,
    )
    if others:
        if others == 1:
            reason += "  One other definition is installed there."
        else:
            reason += "  %d other definitions are installed there." % others
    return build_result(
        rule,
        platform,
        CheckState.PASS,
        reason,
        ReasonCode.OK,
        observed,
        ctx,
        proof_level_achieved=ProofLevel.L0,
    )


CHECKS = {
    CheckType.SUBAGENTS_INSTALLED: check_subagents_installed,
}
