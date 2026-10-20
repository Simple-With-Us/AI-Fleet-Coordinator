"""``permissions`` checks:  the permission identifiers of a settings file, the stat
of a credential file and the reachability of a credential in the secret store.

Three check types live here, all L0:

  * ``PERMISSIONS_MATCH``  compares the permission identifiers a harness settings file
    holds with ``expected.names``, and compares the file's mode with ``expected.mode``.
    The identifiers are names only:  a tool name with its rule, such as
    ``Bash(git:*)``.  Nothing inside a permission entry is read.
  * ``CREDENTIAL_STAT``  checks a credential file by ``stat`` alone:  existence, mode,
    size and mtime.  The file is never opened, never hashed and never read, so no
    result can carry a secret value.  ``ObservedState`` has no field that could hold
    one either, and the reason string names the credential reference, never its
    contents.
  * ``CREDENTIAL_KEY_PRESENT``  asks the fleet's ``fleet-secrets`` CLI for the KEY
    NAMES in one secret-store folder and reports which of the rule's names are there.
    It is how the fleet reaches a credential today (AFC-359):  the command it runs is
    fixed by this module,  the ``names`` verb only,  and that verb prints names and
    never values.  A value cannot reach a result,  a reason or a log.

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
import re
import shutil
import stat as stat_module
import subprocess
from datetime import datetime, timezone
from typing import Any

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

# The secret-store folder a credential rule may name.  The pattern is the one the
# fleet-secrets CLI itself accepts,  so a manifest can never turn the check into a
# request for something else.
_FOLDER_RE = re.compile(r"^/[A-Za-z0-9._/-]*$")

# The shape of a key name in a listing.  Anything else is unreadable output,  which is
# a failing UNKNOWN and never a pass.
_KEY_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,254}$")

# The CLI the check asks for key names.  The name is also the one the fleet's other
# tools use,  so the same binary answers for the whole fleet.
_FLEET_SECRETS = "fleet-secrets"

# The environment variable that names the binary explicitly,  as the fleet's own
# doctor does.
_FLEET_SECRETS_BIN = "FLEET_SECRETS_BIN"


def permission_names(document: dict[str, Any]) -> tuple[str, ...]:
    """The permission identifiers a config document holds, sorted.

    Only strings are read.  A permission entry that is not a string is ignored, because
    a name is all this check compares.
    """
    found: set[str] = set()
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


def _expected_mode(expected: str | None) -> int | None:
    """The expected mode as an integer, or None when the rule does not name one."""
    if expected is None:
        return None
    text = expected.strip().lower()
    text = text.removeprefix("0o")
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


def _secret_store_binary(ctx: CheckContext) -> str | None:
    """The ``fleet-secrets`` binary to ask, or None when there is none.

    The fleet's own ``FLEET_SECRETS_BIN`` override wins, so a caller can name the
    binary explicitly.  Otherwise the binary is looked up on the PATH the check was
    given, not the process PATH, so a run that was pointed at a different PATH asks
    that one and a test never reaches the real secret store by accident.
    """
    override = (ctx.env.get(_FLEET_SECRETS_BIN) or os.environ.get(_FLEET_SECRETS_BIN) or "").strip()
    if override:
        return override
    path = ctx.env.get("PATH") or os.environ.get("PATH", "")
    return shutil.which(_FLEET_SECRETS, path=path)


def _secret_store_names(ctx: CheckContext, folder: str) -> frozenset[str]:
    """The key NAMES in one secret-store folder, or a CheckError.

    The command is the ``names`` verb, which prints names and never values, so a
    secret cannot reach this module, a result, a reason or a log.  The listing is
    cached on the context, so one status run asks the secret store once per folder
    rather than once per rule.
    """
    cache_key = "fleet-secrets:names:%s" % folder
    cached = ctx.cache.get(cache_key)
    if cached is not None:
        return cached

    binary = _secret_store_binary(ctx)
    if not binary:
        raise CheckError(
            "the fleet-secrets command is not on PATH, so the secret store cannot be reached",
            ReasonCode.ERROR,
        )
    argv = [binary, "names", "--path", folder]
    try:
        completed = subprocess.run(
            argv,
            capture_output=True,
            timeout=ctx.timeout or 10,
            env=ctx.child_env(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise CheckError(
            "the fleet-secrets command did not answer within %ds" % (ctx.timeout or 10),
            ReasonCode.TIMEOUT,
        ) from None
    except OSError as exc:
        raise CheckError(
            "the fleet-secrets command could not be started:  %s" % (exc.strerror or exc),
            ReasonCode.ERROR,
        ) from None
    if completed.returncode != 0:
        # The error text names keys and reason tokens, never values, but it is not
        # needed here, so only the exit code is reported.
        raise CheckError(
            "the fleet-secrets command exited %d, so the secret store could not be read"
            % completed.returncode,
            ReasonCode.ERROR,
        )
    names = set()
    for line in completed.stdout.decode("utf-8", "replace").splitlines():
        text = line.strip()
        if not text:
            continue
        if not _KEY_NAME_RE.match(text):
            raise CheckError(
                "the fleet-secrets command printed a line that is not a key name",
                ReasonCode.UNPARSEABLE,
            )
        names.add(text)
    frozen = frozenset(names)
    ctx.cache[cache_key] = frozen
    return frozen


def check_credential_key_present(rule: Rule, platform: str, ctx: CheckContext) -> CheckResult:
    """Check that a credential is reachable in the secret store, by key NAME.

    This is how the fleet reaches a credential now that ``~/.secrets`` is retired
    (AFC-359):  the seat's credential lives in the secret store under a key name, and
    the check asks the ``fleet-secrets`` CLI whether that name is there.

    The check never sees a value.  The ``names`` verb prints names only, the observed
    state carries the names that were found, and the reason string names the rule's
    expected names.  A key that is absent is ``MISSING``;  a secret store that cannot
    be reached is a failing ``UNKNOWN``;  neither is ever a pass.
    """
    folder = (rule.check.target or "").strip()
    if not _FOLDER_RE.match(folder):
        return build_result(
            rule,
            platform,
            CheckState.UNKNOWN,
            "The secret-store folder %r is not a path the check can ask for." % folder,
            ReasonCode.UNPARSEABLE,
            ObservedState(present=None),
            ctx,
        )
    wanted = tuple(rule.expected.names)
    if not wanted:
        return build_result(
            rule,
            platform,
            CheckState.UNKNOWN,
            "The rule names no key to look for, so the secret store cannot be checked.",
            ReasonCode.UNPARSEABLE,
            ObservedState(present=None),
            ctx,
        )
    try:
        available = _secret_store_names(ctx, folder)
    except CheckError as exc:
        return build_result(
            rule,
            platform,
            CheckState.UNKNOWN,
            str(exc),
            exc.reason_code,
            ObservedState(present=None),
            ctx,
        )
    found = tuple(name for name in wanted if name in available)
    observed = ObservedState(present=bool(found), names=found)
    if len(found) == len(wanted):
        return build_result(
            rule,
            platform,
            CheckState.PASS,
            "The credential key %s is in the secret store at %s." % (", ".join(found), folder),
            ReasonCode.OK,
            observed,
            ctx,
            proof_level_achieved=ProofLevel.L0,
        )
    absent = tuple(name for name in wanted if name not in available)
    return build_result(
        rule,
        platform,
        CheckState.MISSING,
        "The credential key %s is not in the secret store at %s." % (", ".join(absent), folder),
        ReasonCode.MISSING,
        observed,
        ctx,
    )


CHECKS = {
    CheckType.PERMISSIONS_MATCH: check_permissions_match,
    CheckType.CREDENTIAL_STAT: check_credential_stat,
    CheckType.CREDENTIAL_KEY_PRESENT: check_credential_key_present,
}
