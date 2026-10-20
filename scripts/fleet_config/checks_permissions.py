"""``permissions`` checks:  the permission identifiers of a settings file and the stat
of a credential file.

Two check types live here, both L0:

  * ``PERMISSIONS_MATCH``  compares the permission identifiers a harness settings file
    holds with ``expected.names``, and compares the file's mode with ``expected.mode``.
    The identifiers are names only:  a tool name with its rule, such as
    ``Bash(git:*)``.  Nothing inside a permission entry is read.
  * ``CREDENTIAL_STAT``  checks a credential file by ``stat`` alone:  existence, mode,
    size and mtime.  The file is never opened, never hashed and never read, so no
    result can carry a secret value.  ``ObservedState`` has no field that could hold
    one either, and the reason string names the credential reference, never its
    contents.

The permission names are compared as sets:  the names a settings file holds must be
exactly the names the rule expects.  An identifier that the rule does not list is a
finding, because a permission that was never asked for is the finding that matters.
A rule that names no permission at all checks the mode alone.

The config shapes that are read are the permission blocks the fleet's platforms write:
a ``permissions`` object with ``allow``, ``deny`` and ``ask`` lists, a ``permissions``
list, and the flat ``allowPermissions``, ``denyPermissions`` and ``askPermissions``
lists.

Stdlib only, and Python 3.9 safe, matching ``fleet_knobs``.
"""
from __future__ import annotations

import os
import stat as stat_module
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set, Tuple

from .checks import CheckContext, CheckError, build_result, expand_target, iso_utc
from .checks_hooks import read_config_document
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

# The permission lists this reader knows, in the order they are read.
_PERMISSION_LIST_KEYS = ("allow", "deny", "ask")
_FLAT_PERMISSION_KEYS = ("allowPermissions", "denyPermissions", "askPermissions")


def permission_names(document: Dict[str, Any]) -> Tuple[str, ...]:
    """The permission identifiers a config document holds, sorted.

    Only strings are read.  A permission entry that is not a string is ignored, because
    a name is all this check compares.
    """
    found: Set[str] = set()
    permissions = document.get("permissions")
    if isinstance(permissions, dict):
        for key in _PERMISSION_LIST_KEYS:
            value = permissions.get(key)
            if isinstance(value, list):
                found.update(item for item in value if isinstance(item, str))
    elif isinstance(permissions, list):
        found.update(item for item in permissions if isinstance(item, str))
    for key in _FLAT_PERMISSION_KEYS:
        value = document.get(key)
        if isinstance(value, list):
            found.update(item for item in value if isinstance(item, str))
    return tuple(sorted(found))


def mode_string(mode: int) -> str:
    """A file mode as four octal digits, such as ``0600``."""
    return "%04o" % stat_module.S_IMODE(mode)


def _expected_mode(expected: Optional[str]) -> Optional[int]:
    """The expected mode as an integer, or None when the rule does not name one."""
    if expected is None:
        return None
    text = expected.strip().lower()
    if text.startswith("0o"):
        text = text[2:]
    if not text:
        return None
    try:
        return int(text, 8)
    except ValueError:
        return None


def check_permissions_match(rule: Rule, platform: str, ctx: CheckContext) -> CheckResult:
    """Check that a settings file grants exactly the expected permissions, with the expected mode."""
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
                "The settings file %s is absent, as the rule expects." % target,
                ReasonCode.OK,
                ObservedState(present=False),
                ctx,
                proof_level_achieved=ProofLevel.L0,
            )
        return build_result(
            rule,
            platform,
            CheckState.MISSING,
            "The settings file %s is missing." % target,
            ReasonCode.MISSING,
            ObservedState(present=False),
            ctx,
        )

    if not os.path.isfile(path):
        return build_result(
            rule,
            platform,
            CheckState.UNKNOWN,
            "The permissions target %s is not a regular file." % target,
            ReasonCode.ERROR,
            ObservedState(present=None),
            ctx,
        )

    try:
        document = read_config_document(path)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return build_result(
            rule,
            platform,
            CheckState.UNKNOWN,
            "The settings file %s cannot be read:  %s" % (target, exc),
            ReasonCode.UNPARSEABLE,
            ObservedState(present=None),
            ctx,
        )

    try:
        info = os.stat(path)
    except OSError as exc:
        return build_result(
            rule,
            platform,
            CheckState.UNKNOWN,
            "The settings file %s cannot be stat'ed:  %s"
            % (target, exc.strerror or "unknown error"),
            ReasonCode.ERROR,
            ObservedState(present=None),
            ctx,
        )

    names = permission_names(document)
    mode = mode_string(info.st_mode)
    observed = ObservedState(present=True, names=names, mode=mode)

    if rule.expected.present is False:
        return build_result(
            rule,
            platform,
            CheckState.DRIFT,
            "The settings file %s exists, which the rule says must be absent." % target,
            ReasonCode.DRIFT,
            observed,
            ctx,
        )

    expected_names = set(rule.expected.names)
    found = set(names)
    missing = sorted(expected_names - found)
    extra = sorted(found - expected_names)
    wanted_mode = _expected_mode(rule.expected.mode)

    if expected_names and missing:
        return build_result(
            rule,
            platform,
            CheckState.DRIFT,
            "The settings file %s does not grant the permission %s."
            % (target, ", ".join(repr(name) for name in missing)),
            ReasonCode.DRIFT,
            observed,
            ctx,
        )

    if expected_names and extra:
        return build_result(
            rule,
            platform,
            CheckState.DRIFT,
            "The settings file %s grants the permission %s, which the rule does not list."
            % (target, ", ".join(repr(name) for name in extra)),
            ReasonCode.DRIFT,
            observed,
            ctx,
        )

    if wanted_mode is not None and mode != mode_string(wanted_mode):
        return build_result(
            rule,
            platform,
            CheckState.DRIFT,
            "The settings file %s has mode %s, not the expected %s."
            % (target, mode, mode_string(wanted_mode)),
            ReasonCode.DRIFT,
            observed,
            ctx,
        )

    if expected_names:
        reason = "The settings file %s grants exactly the expected permissions, with mode %s." % (
            target,
            mode,
        )
    else:
        reason = "The settings file %s has mode %s, as the rule expects." % (target, mode)
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


def check_credential_stat(rule: Rule, platform: str, ctx: CheckContext) -> CheckResult:
    """Check a credential file by stat alone:  presence, mode, size and mtime.

    The file is never opened.  Its contents never reach a result, a reason string or a
    log, and the reason names the credential reference from ``rule.check.detail``.
    """
    reference = rule.check.detail or "the credential file"
    try:
        path = expand_target(rule.check.target, ctx.home)
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

    try:
        info = os.stat(path)
    except FileNotFoundError:
        if rule.expected.present is False:
            return build_result(
                rule,
                platform,
                CheckState.PASS,
                "The credential %s is absent, as the rule expects." % reference,
                ReasonCode.OK,
                ObservedState(present=False),
                ctx,
                proof_level_achieved=ProofLevel.L0,
            )
        return build_result(
            rule,
            platform,
            CheckState.MISSING,
            "The credential %s is missing." % reference,
            ReasonCode.MISSING,
            ObservedState(present=False),
            ctx,
        )
    except OSError as exc:
        return build_result(
            rule,
            platform,
            CheckState.UNKNOWN,
            "The credential %s cannot be stat'ed:  %s"
            % (reference, exc.strerror or "unknown error"),
            ReasonCode.ERROR,
            ObservedState(present=None),
            ctx,
        )

    mode = mode_string(info.st_mode)
    mtime = iso_utc(datetime.fromtimestamp(info.st_mtime, tz=timezone.utc))
    observed = ObservedState(
        present=True,
        mode=mode,
        size_bytes=int(info.st_size),
        mtime=mtime,
    )

    if rule.expected.present is False:
        return build_result(
            rule,
            platform,
            CheckState.DRIFT,
            "The credential %s exists, which the rule says must be absent." % reference,
            ReasonCode.DRIFT,
            observed,
            ctx,
        )

    wanted_mode = _expected_mode(rule.expected.mode)
    if wanted_mode is not None and mode != mode_string(wanted_mode):
        return build_result(
            rule,
            platform,
            CheckState.DRIFT,
            "The credential %s has mode %s, not the expected %s."
            % (reference, mode, mode_string(wanted_mode)),
            ReasonCode.DRIFT,
            observed,
            ctx,
        )

    return build_result(
        rule,
        platform,
        CheckState.PASS,
        "The credential %s is present with mode %s." % (reference, mode),
        ReasonCode.OK,
        observed,
        ctx,
        proof_level_achieved=ProofLevel.L0,
    )


CHECKS = {
    CheckType.PERMISSIONS_MATCH: check_permissions_match,
    CheckType.CREDENTIAL_STAT: check_credential_stat,
}
