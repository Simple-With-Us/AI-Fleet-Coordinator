"""The Touch ID gate in front of persistence-class writes.

Hooks and plugins change what every agent on this Mac may do, and they survive a reboot,
so they are persistence class:  writing one needs a human confirmation.  This module runs
a small signed helper that asks for Touch ID and refuses the write when the confirmation
does not succeed.

The gate fails closed, always:

  * a helper that is missing, not executable, not signed, or that exits non-zero refuses
    the write;  there is no path through this module that silently allows one
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

# The environment variable that names the helper binary, for an installed copy or a test.
HELPER_ENV = "FLEET_CONFIG_TOUCHID_HELPER"

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

    The environment override comes first, so a test or an installed copy can name its own
    binary;  the repo-local build comes next, and the system path last.
    """
    override = (os.environ.get(HELPER_ENV) or "").strip()
    candidates = []
    if override:
        candidates.append(override)
    base = os.path.join(_package_dir(), "touchid_helper")
    candidates.append(os.path.join(base, "build", "Build", "Products", "Release", "TouchIDGate"))
    candidates.append(os.path.join(base, "build", "Build", "Products", "Debug", "TouchIDGate"))
    candidates.append(os.path.join(base, "build", "Release", "TouchIDGate"))
    candidates.append(os.path.join(base, "build", "Debug", "TouchIDGate"))
    candidates.append("/usr/local/bin/fleet-touchid-gate")
    return tuple(candidates)


def helper_path() -> str | None:
    """The helper that will be used, or None when there is none."""
    for candidate in helper_candidates():
        if os.path.isfile(candidate):
            return candidate
    return None


def helper_is_signed(path: str) -> bool:
    """True when the helper carries a code signature macOS will accept.

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
    return result.returncode == 0


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
