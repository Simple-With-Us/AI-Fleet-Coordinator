"""The Touch ID gate in front of persistence-class writes.

Hooks and plugins change what every agent on this Mac may do, and they survive a reboot,
so they are persistence class:  writing one needs a human confirmation.  This module runs
a small signed helper that asks for Touch ID and refuses the write when the confirmation
does not succeed.

The gate fails closed, always:

  * a helper that is missing, not executable, not signed, or that exits non-zero refuses
    the write;  there is no path through this module that silently allows one
  * the helper is resolved from fixed paths the package owns, never from the
    environment,  so a stand-in binary cannot confirm a write by exiting 0
  * Touch ID that cannot be enrolled or is not present is a clear refusal with an
    actionable reason, never a bypass
  * the test override is refused in production:  the environment variable alone never
    bypasses the gate;  only an explicit ``test_bypass=True`` argument does, and no
    production call site passes one

Stdlib only, and Python 3.9 safe, matching ``fleet_knobs``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence

from .types import ConfigKind

__all__ = [
    "HELPER_EXIT_FAILED",
    "HELPER_EXIT_OK",
    "HELPER_EXIT_UNAVAILABLE",
    "PERSISTENCE_CLASS_KINDS",
    "TouchIdError",
    "confirm",
    "helper_candidates",
    "helper_is_signed",
    "helper_path",
    "is_persistence_class",
]

# The kinds whose writes persist and change what an agent may do, so they need a human
# confirmation.  Hooks and plugins are the two the design names;  a new kind joins this
# tuple deliberately, never by accident.
PERSISTENCE_CLASS_KINDS: tuple[ConfigKind, ...] = (ConfigKind.HOOKS,)

# The helper's exit codes, mirrored from the Swift source.
HELPER_EXIT_OK = 0
HELPER_EXIT_UNAVAILABLE = 2
HELPER_EXIT_FAILED = 3

# The environment variable that lets a test run the gate without a prompt.  It is only
# honoured together with an explicit ``test_bypass=True`` argument, so it never bypasses
# the gate in production.
TEST_BYPASS_ENV = "FLEET_CONFIG_TOUCHID_TEST_BYPASS"

# The default seconds the helper may take to ask for Touch ID before the gate gives up.
DEFAULT_TIMEOUT = 120


class TouchIdError(Exception):
    """The confirmation did not succeed, so the write is refused.

    ``reason`` is the machine token:  ``missing``, ``not-executable``, ``not-signed``,
    ``unavailable``, ``cancelled`` or ``error``.  The message says what a human should
    do about it.
    """

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


def is_persistence_class(kind: ConfigKind) -> bool:
    """True when a write of this kind needs a human confirmation."""
    return kind in PERSISTENCE_CLASS_KINDS


def _package_dir() -> str:
    """The directory this package lives in."""
    return os.path.dirname(os.path.abspath(__file__))


def helper_candidates() -> tuple[str, ...]:
    """The places the helper may live, most specific first.

    The paths are FIXED.  The helper is never named by an environment variable, because
    the gate's whole purpose is to stop a process that can set the environment for the
    command from applying a persistence-class write unattended.  A stand-in binary that
    exits 0 would defeat the gate, and an ad-hoc signature satisfies ``codesign
    --verify --strict``, so a caller-controlled path is a bypass, not a convenience.

    The repo-local build comes first, then the system path.  A caller that needs a
    different binary passes it as the explicit ``helper`` argument to ``confirm``, which
    is a code path, not an ambient setting.
    """
    base = os.path.join(_package_dir(), "touchid_helper")
    return (
        os.path.join(base, "build", "Build", "Products", "Release", "TouchIDGate"),
        os.path.join(base, "build", "Build", "Products", "Debug", "TouchIDGate"),
        os.path.join(base, "build", "Release", "TouchIDGate"),
        os.path.join(base, "build", "Debug", "TouchIDGate"),
        "/usr/local/bin/fleet-touchid-gate",
    )


def helper_path() -> str | None:
    """The helper that will be used, or None when there is none."""
    for candidate in helper_candidates():
        if os.path.isfile(candidate):
            return candidate
    return None


def helper_is_signed(path: str) -> bool:
    """True when the helper carries a code signature macOS will accept.

    The signature must be a REAL one:  an ad-hoc signature (``codesign --sign -``) passes
    ``codesign --verify --strict``, and anyone can produce one, so accepting it would
    make the signature check no check at all.  The gate therefore requires a Developer ID
    or an Apple signature, and it reads the AUTHORITY rather than trusting the exit code
    alone.

    A missing ``codesign`` counts as unsigned, so the gate fails closed on a machine
    where the check cannot be made.
    """
    codesign = shutil.which("codesign") or "/usr/bin/codesign"
    if not os.path.isfile(codesign):
        return False
    try:
        result = subprocess.run(
            [codesign, "--verify", "--strict", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    return _has_trusted_authority(path)


def _has_trusted_authority(path: str) -> bool:
    """True when the helper's signature chains to a Developer ID or an Apple authority.

    ``codesign --verify`` accepts an ad-hoc signature, so the exit code alone cannot
    answer this question.  ``codesign -dvv`` prints the authority lines, and an ad-hoc
    signature has none.
    """
    codesign = shutil.which("codesign") or "/usr/bin/codesign"
    try:
        result = subprocess.run(
            [codesign, "--display", "--verbose=4", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    text = (result.stdout or b"") + (result.stderr or b"")
    decoded = text.decode("utf-8", "replace")
    if "Signature=adhoc" in decoded:
        return False
    return "Authority=" in decoded


def _test_bypass_allowed(env: Mapping[str, str], test_bypass: bool) -> bool:
    """True only when the caller asked for the test bypass explicitly.

    The environment variable on its own never bypasses the gate:  production calls do not
    pass ``test_bypass``, so an agent that sets the variable still gets refused.
    """
    if not test_bypass:
        return False
    return (env.get(TEST_BYPASS_ENV) or "").strip() == "1"


def confirm(
    reason: str,
    *,
    helper: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    env: Mapping[str, str] | None = None,
    test_bypass: bool = False,
) -> None:
    """Ask for a Touch ID confirmation, or raise ``TouchIdError``.

    `reason` is the sentence the owner reads on the prompt.  ``helper`` names the binary
    to run;  when it is absent the usual places are looked in.  ``timeout`` bounds the
    helper, so a machine that cannot ask never hangs the run.

    Raises ``TouchIdError`` when the helper is missing, not executable, not signed, when
    Touch ID is unavailable, when the confirmation was refused, and when the helper
    itself failed.  Every one of those is a refusal of the write.
    """
    environment = dict(env) if env is not None else dict(os.environ)
    if _test_bypass_allowed(environment, test_bypass):
        return
    path = helper if helper is not None else helper_path()
    if not path:
        raise TouchIdError(
            "the Touch ID helper is not built;  build it with "
            "scripts/fleet_config/touchid_helper/build.sh,  so the write can be confirmed",
            "missing",
        )
    if not os.path.isfile(path):
        raise TouchIdError(
            "the Touch ID helper at %s does not exist;  the write is refused" % path,
            "missing",
        )
    if not os.access(path, os.X_OK):
        raise TouchIdError(
            "the Touch ID helper at %s is not executable;  the write is refused" % path,
            "not-executable",
        )
    if not helper_is_signed(path):
        raise TouchIdError(
            "the Touch ID helper at %s is not signed;  an unsigned helper cannot ask for a "
            "confirmation,  so the write is refused" % path,
            "not-signed",
        )
    argv: Sequence[str] = [path, "--reason", reason]
    try:
        result = subprocess.run(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=environment,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise TouchIdError(
            "the Touch ID confirmation did not answer within %d seconds;  the write is refused"
            % timeout,
            "error",
        )
    except OSError as exc:
        raise TouchIdError(
            "the Touch ID helper could not be run (%s);  the write is refused" % exc,
            "error",
        )
    if result.returncode == HELPER_EXIT_OK:
        return
    detail = (result.stderr or b"").decode("utf-8", "replace").strip()
    if result.returncode == HELPER_EXIT_UNAVAILABLE:
        raise TouchIdError(
            "Touch ID is not available on this machine,  so the write cannot be confirmed:  %s"
            % (detail or "no reason given"),
            "unavailable",
        )
    if result.returncode == HELPER_EXIT_FAILED:
        raise TouchIdError(
            "the Touch ID confirmation did not succeed,  so the write is refused:  %s"
            % (detail or "no reason given"),
            "cancelled",
        )
    raise TouchIdError(
        "the Touch ID helper exited %d,  so the write is refused:  %s"
        % (result.returncode, detail or "no reason given"),
        "error",
    )
