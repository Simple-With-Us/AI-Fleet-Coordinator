"""Shared plumbing for the check modules:  the context they run in and the registry.

Every ``checks_<kind>.py`` module in this package exposes one function with one
signature, so the aggregator can dispatch on the rule's check type without knowing which
module implements what:

    def check(rule: Rule, platform: str, ctx: CheckContext) -> CheckResult

``CheckContext`` carries the home directory the checks read, the clock they stamp
results with, the per-check timeout, and the environment they may pass to a child
process.  ``build_result`` builds a ``CheckResult`` with consistent timestamps and
expiry, so no check module invents its own time handling.

This module is the one piece of the package that every implementer imports, so it is
written once and shared rather than being re-derived per module.  It holds no check
logic of its own:  the vocabulary lives in ``types.py`` and the verdict math lives in
``contract.py``.

Stdlib only, and Python 3.9 safe, matching ``fleet_knobs``.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .types import (
    CheckResult,
    CheckState,
    CheckType,
    Collector,
    ObservedState,
    ProofLevel,
    ReasonCode,
    Rule,
)

__all__ = [
    "CheckContext",
    "CheckContextError",
    "CheckError",
    "CheckRegistry",
    "build_result",
    "expand_target",
    "iso_utc",
    "resolve_home",
    "sha256_text",
    "utc_now",
]


class CheckError(Exception):
    """A check could not produce a verdict.

    ``reason_code`` is the machine token the aggregator records.  A check raises this
    instead of returning a result when it could not read what it needed, so the
    fail-closed rule in ``contract.py`` applies to it unchanged:  an errored check is
    never a pass.
    """

    def __init__(self, message: str, reason_code: ReasonCode = ReasonCode.ERROR) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class CheckContextError(CheckError):
    """The context itself was wrong, such as an empty home directory."""


@dataclass(frozen=True)
class CheckContext:
    """What a check is allowed to touch while it runs.

    ``home`` is the directory the seat's config lives under.  ``now`` is the clock every
    result is stamped from, so one status run has one consistent time.  ``timeout``
    bounds a child process a check spawns.  ``env`` is the environment passed to such a
    child;  it is a mapping of names to values and never carries a credential the
    caller did not put there.
    """

    home: os.PathLike[str]
    now: datetime
    timeout: int = 10
    env: Mapping[str, str] = field(default_factory=dict)

    def child_env(self) -> dict[str, str]:
        """The environment for a child process:  the caller's plus the given overrides.

        A check that spawns a child uses this rather than ``os.environ`` directly, so
        the child sees a known environment and a test can pin it.
        """
        merged = dict(os.environ)
        merged.update(self.env)
        return merged

    def path(self, *parts: str) -> os.PathLike[str]:
        """A path under the home directory."""
        return os.path.join(self.home, *parts)


def resolve_home(home: os.PathLike[str] | None = None) -> os.PathLike[str]:
    """The home directory the checks read, refusing an empty or missing one.

    An empty home would make every check report the same false MISSING, so it is an
    error rather than a default.
    """
    target = home if home is not None else os.environ.get("HOME")
    if not target:
        raise CheckContextError("no home directory to check")
    if not os.path.isdir(target):
        raise CheckContextError("the home directory %s does not exist" % target)
    return target


def utc_now() -> datetime:
    """The current time, timezone-aware and in UTC."""
    return datetime.now(UTC)


def iso_utc(moment: datetime) -> str:
    """A timestamp in the pinned format:  ISO 8601 UTC with a ``Z`` suffix."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_text(text: str) -> str:
    """The lowercase hex SHA-256 of some text.

    This is the hash the manifest's ``expected.sha256`` and the observed value both
    use.  It is computed over the UTF-8 bytes of the text, and the text is the
    canonical block body with its trailing newline removed, so the hash is stable
    across platforms.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def expand_target(target: str, home: os.PathLike[str]) -> str:
    """Expand a manifest target into a real path.

    A target may start with ``~``, which means the home directory the checks are
    running against, and may contain ``$HOME``.  The expansion never consults the
    process environment for anything but ``HOME``, so one run cannot see two homes.
    """
    if not target:
        raise CheckError("a check target is empty", ReasonCode.MISSING)
    text = target.replace("$HOME", str(home))
    if text.startswith("~/"):
        text = os.path.join(str(home), text[2:])
    elif text == "~":
        text = str(home)
    return text


def build_result(
    rule: Rule,
    platform: str,
    state: CheckState,
    reason: str,
    reason_code: ReasonCode,
    observed: ObservedState,
    ctx: CheckContext,
    proof_level_achieved: ProofLevel | None = None,
    collector: Collector | None = None,
) -> CheckResult:
    """Build one checked result with consistent stamps.

    The expiry is the check time plus the rule's TTL.  A check that fails is not
    stamped differently from one that passes;  the state and the reason carry that,
    and ``contract.is_failing`` decides what counts against the run.
    """
    checked_at = ctx.now
    expires_at = checked_at + timedelta(seconds=rule.ttl_seconds)
    return CheckResult(
        rule_id=rule.id,
        seat=rule.seat,
        platform=platform,
        kind=rule.kind,
        state=state,
        reason=reason,
        reason_code=reason_code,
        expected=rule.expected,
        observed=observed,
        proof_level_required=rule.proof_level,
        proof_level_achieved=proof_level_achieved,
        collector=collector if collector is not None else rule.collector,
        check_type=rule.check.type,
        checked_at=iso_utc(checked_at),
        expires_at=iso_utc(expires_at),
        ttl_seconds=rule.ttl_seconds,
        stale=False,
    )


class CheckRegistry:
    """The dispatch table from a check type to the function that runs it.

    The aggregator walks the manifest, looks each rule's check type up here, and calls
    it.  A check type with no registered function is not silently skipped:  it becomes
    an ``UNSUPPORTED`` result with the reason naming the missing implementation, which
    is the fail-closed outcome.
    """

    def __init__(self) -> None:
        self._checks: dict[CheckType, Callable[[Rule, str, CheckContext], CheckResult]] = {}

    def register(
        self, check_type: CheckType
    ) -> Callable[
        [Callable[[Rule, str, CheckContext], CheckResult]],
        Callable[[Rule, str, CheckContext], CheckResult],
    ]:
        """Register the function that implements one check type."""

        def decorate(
            function: Callable[[Rule, str, CheckContext], CheckResult],
        ) -> Callable[[Rule, str, CheckContext], CheckResult]:
            self._checks[check_type] = function
            return function

        return decorate

    def get(self, check_type: CheckType) -> Callable[[Rule, str, CheckContext], CheckResult] | None:
        """The function for one check type, or None when it is not implemented."""
        return self._checks.get(check_type)

    def implemented(self) -> tuple:
        """The check types this registry can run, sorted by name."""
        return tuple(sorted((check.value for check in self._checks), key=str))


def default_registry() -> CheckRegistry:
    """The registry with every implemented check type registered.

    Importing the check modules here rather than in the aggregator keeps the import
    order out of the aggregator's own body, and gives one place to see what exists.
    """
    from . import (
        checks_hooks,
        checks_instructions,
        checks_mcp,
        checks_permissions,
        checks_skills,
        checks_subagents,
    )

    registry = CheckRegistry()
    registry._checks.update(checks_instructions.CHECKS)
    registry._checks.update(checks_hooks.CHECKS)
    registry._checks.update(checks_mcp.CHECKS)
    registry._checks.update(checks_skills.CHECKS)
    registry._checks.update(checks_subagents.CHECKS)
    registry._checks.update(checks_permissions.CHECKS)
    return registry
