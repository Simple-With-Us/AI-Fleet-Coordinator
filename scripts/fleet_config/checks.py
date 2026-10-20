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

It also owns the ONE runner every child process in this package starts through
(``run_cli``),  so the MAC level doctors,  the secret store listing and the installer
engines the apply side drives all share one allowlist,  one timeout and one kill
discipline.  A command that is not on the allowlist is refused before any process
exists.

Stdlib only, and Python 3.9 safe, matching ``fleet_knobs``.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import tempfile
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

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
    "ALLOWED_COMMANDS",
    "ALLOWED_MODULES",
    "ALLOWED_SCRIPTS",
    "CheckContext",
    "CheckContextError",
    "CheckError",
    "CheckRegistry",
    "CliResult",
    "CommandRefused",
    "MAX_CLI_OUTPUT_BYTES",
    "build_result",
    "check_cli_allowed",
    "expand_target",
    "iso_utc",
    "kill_process_group",
    "parse_bounded_json",
    "resolve_home",
    "run_cli",
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
    caller did not put there.  ``cache`` is the scratch space of one run:  a check that
    asks the same slow question for several rules asks it once and keeps the answer
    there,  so one run never repeats one slow call.
    """

    home: os.PathLike[str]
    now: datetime
    timeout: int = 10
    env: Mapping[str, str] = field(default_factory=dict)
    cache: MutableMapping[str, Any] = field(default_factory=dict)

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
    """The current time, timezone-aware and in timezone.utc."""
    return datetime.now(timezone.utc)


def iso_utc(moment: datetime) -> str:
    """A timestamp in the pinned format:  ISO 8601 timezone.utc with a ``Z`` suffix."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


# --------------------------------------------------------------------------- the one runner

# The programs this package may start.  Every one of them is a fleet tool with a fixed
# shape,  so the allowlist is a closed set:  a command that is not on it is refused
# before any process exists.
ALLOWED_COMMANDS: frozenset[str] = frozenset({"fleet-secrets"})

# The scripts this package may run with the interpreter,  by file name.  The MAC level
# doctor is the only one.
ALLOWED_SCRIPTS: frozenset[str] = frozenset({"fleet-tools-doctor.py"})

# The modules this package may run with ``python3 -m``.  These are the installer engines
# the apply side drives.  They are started as child processes and never imported,  because
# this package has to stay importable with nothing but itself present.
ALLOWED_MODULES: frozenset[str] = frozenset(
    {
        "fleet_lanes.install_mcp",
        "fleet_lanes.install_rules",
        "fleet_lanes.install_tools",
    }
)

# The largest output the runner reads from a child.  A child that writes more is
# truncated,  and the truncation is reported,  so a runaway child cannot exhaust memory.
MAX_CLI_OUTPUT_BYTES = 1024 * 1024

# The seconds a command gets when the caller names no budget of its own.  A caller that
# cares passes its own;  this is only the floor,  so a command is never unbounded.
DEFAULT_CLI_TIMEOUT = 30


class CommandRefused(CheckError):
    """The command is not on the allowlist,  so it was never started.

    The refusal happens before any process exists,  so a path that came out of a config
    file or a manifest can never become a command that runs.
    """


@dataclass(frozen=True)
class CliResult:
    """What one child process did.

    ``returncode`` is the exit code,  or None when the child never ran to completion.
    ``timed_out`` says the budget ran out,  and ``error`` says the child could not be
    started at all.  ``truncated`` says the child wrote more than the cap,  so the bytes
    here are not the whole answer.
    """

    returncode: int | None = None
    stdout: bytes = b""
    stderr: bytes = b""
    timed_out: bool = False
    error: str = ""
    truncated: bool = False


def _is_interpreter(program: str) -> bool:
    """True when the program is a Python interpreter."""
    return os.path.basename(program).startswith("python")


def check_cli_allowed(argv: Sequence[str]) -> str:
    """The name of the allowlisted command,  or raise ``CommandRefused``.

    The allowlist is closed:  a fleet tool by name,  a script by file name,  or an
    installer module run with ``-m``.  Nothing else may start.  The check happens
    before any process exists,  so a refused command leaves nothing behind.
    """
    if not argv or not all(isinstance(part, str) for part in argv):
        raise CommandRefused("the command is empty or holds a value that is not text")
    program = argv[0]
    if _is_interpreter(program):
        rest = list(argv[1:])
        if not rest:
            raise CommandRefused("the interpreter was given nothing to run")
        if rest[0] == "-m":
            module = rest[1] if len(rest) > 1 else ""
            if module not in ALLOWED_MODULES:
                raise CommandRefused("the module %r is not on the allowlist" % module)
            return module
        if rest[0] not in ALLOWED_SCRIPTS:
            raise CommandRefused("the script %r is not on the allowlist" % rest[0])
        return rest[0]
    name = os.path.basename(program)
    if name not in ALLOWED_COMMANDS:
        raise CommandRefused("the command %r is not on the allowlist" % name)
    return name


def kill_process_group(proc: subprocess.Popen) -> None:
    """Kill the child's whole process group,  so nothing it started survives.

    The child is started in its own session,  so the process group id is the child's own
    pid.  A child that spawned helpers takes them down with it.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        try:
            proc.kill()
        except OSError:
            pass


def run_cli(
    argv: Sequence[str],
    *,
    timeout: int,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    max_output_bytes: int = MAX_CLI_OUTPUT_BYTES,
) -> CliResult:
    """Run one allowlisted CLI command,  with an explicit timeout.

    This is the ONE place a child process starts in this package:  the MAC level
    doctors,  the secret store listing and the installer engines the apply side drives
    all come through here,  so every child has the same allowlist,  the same timeout and
    the same kill discipline.  A hung command is a timeout,  never a hang.

    The command is refused before any process exists when it is not on the allowlist.
    The output is written to a temporary file rather than a pipe,  so a child that
    writes without bound cannot exhaust memory,  and the bytes read back are capped.
    The child gets no stdin,  so it can never wait for the caller's terminal.
    """
    check_cli_allowed(argv)
    limit = int(timeout) if int(timeout) > 0 else DEFAULT_CLI_TIMEOUT
    cap = int(max_output_bytes) if int(max_output_bytes) > 0 else MAX_CLI_OUTPUT_BYTES
    try:
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            try:
                proc = subprocess.Popen(
                    list(argv),
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    env=dict(env) if env is not None else None,
                    cwd=cwd,
                    start_new_session=True,
                )
            except OSError as exc:
                return CliResult(error="cannot start %s: %s" % (argv[0], exc))
            try:
                proc.wait(timeout=limit)
            except subprocess.TimeoutExpired:
                kill_process_group(proc)
                _reap(proc)
                return CliResult(timed_out=True)
            except BaseException:
                # An interruption (a Ctrl-C,  a SystemExit) must not leave the child
                # running:  the group is killed and the child is reaped before the
                # interruption is re-raised.
                kill_process_group(proc)
                _reap(proc)
                raise
            out.seek(0)
            err.seek(0)
            stdout = out.read(cap + 1)
            stderr = err.read(cap + 1)
    except OSError as exc:
        return CliResult(error="cannot read the output of %s: %s" % (argv[0], exc))
    truncated = len(stdout) > cap or len(stderr) > cap
    return CliResult(
        returncode=proc.returncode,
        stdout=stdout[:cap],
        stderr=stderr[:cap],
        truncated=truncated,
    )


def _reap(proc: subprocess.Popen) -> None:
    """Wait for a killed child,  so it does not linger as a zombie."""
    try:
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001 - the process is already dead;  reaping is best effort
        pass


def parse_bounded_json(
    raw: bytes,
    *,
    what: str,
    max_bytes: int,
    max_depth: int,
    max_nodes: int,
) -> Any:
    """The JSON document in `raw`,  checked against the byte, depth and node limits.

    The bytes are capped before the parse,  so a huge answer is never turned into a
    tree,  and the parsed tree is walked with a node counter and a depth limit,  so a
    deeply nested or very wide answer cannot exhaust the stack or the memory.

    Raises ``CheckError`` with reason code ``unparseable`` when the answer is refused,
    so the caller turns the refusal into a finding instead of a crash.
    """
    if len(raw) > max_bytes:
        raise CheckError(
            "the %s answered with more than %d bytes" % (what, max_bytes),
            ReasonCode.UNPARSEABLE,
        )
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CheckError(
            "the %s did not answer with JSON:  %s" % (what, exc),
            ReasonCode.UNPARSEABLE,
        ) from None
    _assert_tree_bounded(document, what=what, max_depth=max_depth, max_nodes=max_nodes)
    return document


def _assert_tree_bounded(document: object, *, what: str, max_depth: int, max_nodes: int) -> None:
    """Refuse a JSON tree that nests too deep or holds too many nodes.

    The walk is iterative,  so a deeply nested document cannot exhaust the stack on the
    way to being refused.
    """
    seen = 0
    stack: list[tuple[object, int]] = [(document, 1)]
    while stack:
        node, depth = stack.pop()
        if depth > max_depth:
            raise CheckError(
                "the %s nests deeper than %d levels" % (what, max_depth),
                ReasonCode.UNPARSEABLE,
            )
        seen += 1
        if seen > max_nodes:
            raise CheckError(
                "the %s holds more than %d values" % (what, max_nodes),
                ReasonCode.UNPARSEABLE,
            )
        if isinstance(node, dict):
            for key, value in node.items():
                if not isinstance(key, str):
                    raise CheckError(
                        "the %s has a key that is not text" % what,
                        ReasonCode.UNPARSEABLE,
                    )
                stack.append((value, depth + 1))
        elif isinstance(node, list):
            for item in node:
                stack.append((item, depth + 1))


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
