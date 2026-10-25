"""The fleet-wide compliance checks:  rules about the fleet,  not about one seat.

A compliance rule answers a question about the fleet rather than about one seat's config
surface:  is a certificate about to expire,  is a secret sitting in a flat key file,  did
a backup stop.  The rules live in their own list in the manifest and carry the same
vocabulary the seat rules carry,  so one registry holds both.

Three rules hold for every check in this module:

  * A rule that cannot be decided reads ``UNKNOWN``.  A timeout,  a missing binary,  an
    unreadable answer and a threshold nobody set are all unknown,  and the engine's
    fail-closed rule makes them count against the run.
  * A reason is a short sentence about shapes and counts.  It never carries a secret
    value,  a certificate body or a line of a file that was scanned.
  * A threshold comes from a fleet knob (Infisical through ``scripts/fleet_knobs``),  never
    from a literal in the manifest.  A knob that is unset or out of range leaves the rule
    undecided,  and an undecided rule never reads as a pass.

The knob is read through the one allowlisted runner,  so a knob read has a timeout and
cannot hang a status run,  and the answer is cached on the run context so one run reads a
knob once.

Stdlib only,  and Python 3.9 safe,  matching ``fleet_knobs``.
"""

from __future__ import annotations

import glob
import os
import re
import shlex
import sys
import time
from dataclasses import dataclass

from .certs import CertificateExpiry, ExpiryUnknown, keychain_expiries, profile_expiry
from .checks import (
    CheckContext,
    CheckError,
    build_result,
    expand_target,
    parse_bounded_json,
    run_cli,
)
from .types import (
    CheckResult,
    CheckState,
    CheckType,
    ObservedState,
    ReasonCode,
    Rule,
)

__all__ = [
    "CHECKS",
    "KNOB_TIMEOUT_SECONDS",
    "KnobAnswer",
    "ThresholdReading",
    "check_certificate_expiry",
    "check_command",
    "check_secrets_hygiene",
    "read_knob",
    "read_threshold",
]

# The seconds a knob read may take.  The read goes to Infisical,  so it needs more than a
# file read gets;  it still has a budget,  so an unreachable Infisical is a timeout rather
# than a hang.
KNOB_TIMEOUT_SECONDS = 30

# The seconds a command check may take.  A fleet tool that answers in a second gets a
# minute;  a hung one is a timeout.
COMMAND_TIMEOUT_SECONDS = 60

# The seconds a certificate read may take.  Reading the keychain can block on a locked
# keychain,  so the budget is short.
CERTIFICATE_TIMEOUT_SECONDS = 20

# The largest answer the knob reader parses.  The JSON is one small object,  so the cap
# is generous;  it is there so a wrong program cannot fill the memory.
MAX_KNOB_BYTES = 64 * 1024

# The largest published file the leak scan reads.  A workflow is a few kilobytes;  a
# megabyte is far past anything the scan should read.
MAX_PUBLISHED_BYTES = 1024 * 1024

# The most file names one result carries.  A machine can hold hundreds of flat key
# files,  and the report names a few of them and counts the rest.
MAX_REPORTED_NAMES = 10

# How deep a published directory is walked.  The template trees keep their workflow
# files one directory down (``github-workflows-template/workflows/``),  so two levels
# below the surface cover them while a surface still stays a small,  known set.
MAX_PUBLISHED_DEPTH = 3

# The most provisioning profiles one certificate rule decodes,  and the time all of them
# together may take.  Each one is a child process,  so without a cap a machine with many
# profiles could hold one rule far past the run's own deadline.  The profiles past the cap
# are reported as unread,  so the rule reads UNKNOWN rather than a clean bill of health.
MAX_PROFILES = 25
PROFILE_BUDGET_SECONDS = 60

# The subcommands a ``command`` rule may run,  per allowlisted program.  The allowlist in
# checks.py pins the program's NAME;  this pins what it is asked to do,  so a manifest
# edit cannot turn a read into a write.  ``security`` has none:  the keychain is read
# only by the certificate check,  with its own fixed,  read-only arguments.
COMMAND_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "fleet-secrets": frozenset({"doctor", "names", "status", "get"}),
    "security": frozenset(),
}


@dataclass(frozen=True)
class KnobAnswer:
    """What the knob registry said about one knob.

    ``value`` is None when the knob has no value from any source:  that is the unset
    case,  and a rule that reads an unset knob reads UNKNOWN.  ``source`` says where the
    value came from:  ``infisical``,  ``lkg`` (the last-known-good copy) or ``default``
    (the registry default).  ``reason`` is a short token,  never a value.
    """

    name: str
    value: int | None
    source: str
    stale: bool
    reason: str


@dataclass(frozen=True)
class ThresholdReading:
    """The number a rule decided on,  and where the number came from."""

    value: int
    source: str
    stale: bool


def _scripts_dir() -> str:
    """The directory the package's sibling scripts live in."""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _unknown(
    rule: Rule,
    platform: str,
    reason: str,
    reason_code: ReasonCode,
    ctx: CheckContext,
) -> CheckResult:
    """A result for a check that could not produce a trustworthy answer.

    The state is ``UNKNOWN`` and the reason code is a failing one,  so the run counts
    it against the verdict.  A broken check never accuses anybody of drift it did not
    observe,  and it never reads as healthy either.
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


def _repo_root(ctx: CheckContext) -> str | None:
    """The checkout the published surfaces are resolved against.

    The manifest names the checkout the engine is working on,  so the surfaces a rule
    declares are read from the same tree the manifest came from.  Without a manifest
    path the package's own checkout is used,  and without one either the rule cannot
    say what it scanned.
    """
    root = ctx.cache.get("repoRoot")
    if isinstance(root, str) and root:
        return root
    fallback = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if os.path.isdir(os.path.join(fallback, ".git")) or os.path.isfile(
        os.path.join(fallback, "fleet-seats.json")
    ):
        return fallback
    return None


def read_knob(name: str, ctx: CheckContext, *, timeout: int = KNOB_TIMEOUT_SECONDS) -> KnobAnswer:
    """One knob's value,  read through the fleet's own knob registry.

    The read goes through the one allowlisted runner,  so it has a timeout and cannot
    hang a status run.  The answer is cached on the run context,  so one status run reads
    a knob once and not once per rule.
    """
    key = "knob:" + name
    cached = ctx.cache.get(key)
    if isinstance(cached, KnobAnswer):
        return cached
    answer = _read_knob_now(name, ctx, timeout)
    ctx.cache[key] = answer
    return answer


def _read_knob_now(name: str, ctx: CheckContext, timeout: int) -> KnobAnswer:
    """The knob read itself,  without the cache."""
    argv = [sys.executable, "-m", "fleet_knobs", "get", "--json", name]
    try:
        result = run_cli(argv, timeout=timeout, env=ctx.child_env(), cwd=_scripts_dir())
    except CheckError:
        return KnobAnswer(name, None, "none", False, "refused")
    if result.error:
        return KnobAnswer(name, None, "none", False, "not-started")
    if result.timed_out:
        return KnobAnswer(name, None, "none", False, "timeout")
    if result.returncode != 0:
        return KnobAnswer(name, None, "none", False, "exit-%d" % (result.returncode or 0))
    try:
        document = parse_bounded_json(
            result.stdout,
            what="the %s knob" % name,
            max_bytes=MAX_KNOB_BYTES,
            max_depth=8,
            max_nodes=64,
        )
    except CheckError:
        return KnobAnswer(name, None, "none", False, "unreadable")
    if not isinstance(document, dict):
        return KnobAnswer(name, None, "none", False, "unreadable")
    value = document.get("value")
    source = str(document.get("source") or "none")
    stale = bool(document.get("stale"))
    reason = str(document.get("reason") or "")
    if value is None:
        return KnobAnswer(name, None, source, stale, reason or "unset")
    if isinstance(value, bool) or not isinstance(value, int):
        return KnobAnswer(name, None, source, stale, "not-a-number")
    return KnobAnswer(name, value, source, stale, reason)


def read_threshold(
    rule: Rule, ctx: CheckContext
) -> tuple[ThresholdReading | None, str]:
    """The rule's threshold number,  and the reason there is none.

    Every compliance check that reads a threshold needs one to decide,  so a rule that
    declares none reads UNKNOWN,  and the reason says so.  A rule whose knob is unset,  or
    whose value is outside the range the rule accepts,  has no threshold either,  and the
    reason says which of the two it was.  The reason is never empty.

    ``unset`` is the fleet's own word for it:  a knob that still holds only its registry
    default has not been set by anybody,  and a threshold nobody set is not a threshold.
    The rule reads UNKNOWN rather than deciding on the default.
    """
    if rule.threshold is None:
        return None, "the rule declares no threshold,  so it cannot decide"
    answer = read_knob(rule.threshold.knob, ctx)
    if answer.value is None:
        return None, (
            "the threshold knob %s has no value,  so the rule cannot decide"
            % rule.threshold.knob
        )
    if answer.source == "default":
        return None, (
            "the threshold knob %s is not set,  so the rule cannot decide:  a threshold "
            "nobody set is not a threshold" % rule.threshold.knob
        )
    if answer.value < rule.threshold.minimum or answer.value > rule.threshold.maximum:
        return None, (
            "the threshold knob %s answered %d,  which is outside the %d to %d the rule "
            "accepts,  so the rule cannot decide"
            % (
                rule.threshold.knob,
                answer.value,
                rule.threshold.minimum,
                rule.threshold.maximum,
            )
        )
    return ThresholdReading(value=answer.value, source=answer.source, stale=answer.stale), ""


def _threshold_note(reading: ThresholdReading) -> str:
    """A short note about where the threshold came from,  for the reason text."""
    if reading.source == "infisical":
        return "the threshold came from Infisical"
    if reading.source == "lkg":
        return "the threshold came from the last-known-good copy"
    return "the threshold came from the registry default"


def check_command(rule: Rule, platform: str, ctx: CheckContext) -> CheckResult:
    """Run the rule's command and read its exit code.

    The program is looked up in the one allowlist every child process in this package
    goes through,  so a command that is not on it is refused before any process exists.
    A missing binary,  a timeout and an unreadable answer all read ``UNKNOWN`` with a
    failing reason code,  exactly like the per-seat checks do.

    The command's output never reaches the result.  A rule that wants the answer read
    says which JSON keys it wants in ``expected.names``,  and only the KEYS are
    reported:  a key name is an identifier,  and a value could be a secret.
    """
    program = rule.check.target.strip()
    if not program:
        return _unknown(
            rule,
            platform,
            "the rule names no command to run",
            ReasonCode.MISSING,
            ctx,
        )
    # The program is a bare name,  found on the child's own PATH:  a path would pin the
    # file name the allowlist checks but run whatever binary sits at that path.
    if "/" in program or os.sep in program:
        return _unknown(
            rule,
            platform,
            "the command %s is a path,  and a command rule names a program,  never a file"
            % program,
            ReasonCode.ERROR,
            ctx,
        )
    try:
        arguments = shlex.split(rule.check.detail)
    except ValueError:
        # Running the program bare would answer a different question than the rule asks.
        return _unknown(
            rule,
            platform,
            "the arguments the rule declares for %s cannot be read,  so the command is not run"
            % program,
            ReasonCode.UNPARSEABLE,
            ctx,
        )
    allowed = COMMAND_SUBCOMMANDS.get(program)
    if allowed is not None and (not arguments or arguments[0] not in allowed):
        return _unknown(
            rule,
            platform,
            "the command %s is not allowed to run %s from a command rule"
            % (program, arguments[0] if arguments else "with no subcommand"),
            ReasonCode.ERROR,
            ctx,
        )
    timeout = rule.expected.timeout_seconds or COMMAND_TIMEOUT_SECONDS
    try:
        result = run_cli(
            [program] + arguments,
            timeout=timeout,
            env=ctx.child_env(),
            cwd=None,
        )
    except CheckError as exc:
        return _unknown(
            rule,
            platform,
            "the command %s is not allowed to run here:  %s" % (program, exc),
            ReasonCode.ERROR,
            ctx,
        )
    if result.error:
        return _unknown(
            rule,
            platform,
            "the command %s could not be started:  %s" % (program, result.error),
            ReasonCode.ERROR,
            ctx,
        )
    if result.timed_out:
        return _unknown(
            rule,
            platform,
            "the command %s did not finish within %d seconds" % (program, timeout),
            ReasonCode.TIMEOUT,
            ctx,
        )
    wanted = tuple(rule.expected.names)
    if wanted:
        try:
            document = parse_bounded_json(
                result.stdout,
                what="the %s command" % program,
                max_bytes=MAX_KNOB_BYTES,
                max_depth=16,
                max_nodes=4096,
            )
        except CheckError as exc:
            return _unknown(rule, platform, str(exc), ReasonCode.UNPARSEABLE, ctx)
        if not isinstance(document, dict):
            return _unknown(
                rule,
                platform,
                "the %s command did not answer with a JSON object" % program,
                ReasonCode.UNPARSEABLE,
                ctx,
            )
        absent = [name for name in wanted if name not in document]
        if absent:
            return build_result(
                rule,
                platform,
                CheckState.DRIFT,
                "the %s command answered without %s" % (program, ", ".join(absent)),
                ReasonCode.DRIFT,
                ObservedState(names=tuple(name for name in wanted if name in document)),
                ctx,
            )
    if result.returncode != 0:
        return build_result(
            rule,
            platform,
            CheckState.DRIFT,
            "the command %s exited with code %d" % (program, result.returncode or 0),
            ReasonCode.DRIFT,
            ObservedState(),
            ctx,
        )
    return build_result(
        rule,
        platform,
        CheckState.PASS,
        "the command %s answered as expected" % program,
        ReasonCode.OK,
        ObservedState(names=wanted),
        ctx,
    )


# The shapes that say "this published file names an internal asset".  They match SHAPES,
# never the fleet's own values:  a rule that carried the values would put them in a
# public repository,  which is the very thing it checks for.
LEAK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "an internal host address",
        re.compile(
            r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
            r"|192\.168\.\d{1,3}\.\d{1,3}"
            r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
            r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})\b"
        ),
    ),
    ("a tailnet host name", re.compile(r"\b[a-z0-9-]+\.(?:ts\.net|tailscale)\b")),
    ("a private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "an internal project or container id",
        re.compile(
            r"(?im)^\s*[A-Z0-9_]*(?:PROJECT|CONTAINER)[A-Z0-9_]*(?:ID|UUID)"
            r"\s*[:=]\s*[\"']?[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
        ),
    ),
)


def _published_files(path: str, depth: int = MAX_PUBLISHED_DEPTH) -> list[str]:
    """The files a declared published surface holds,  sorted.

    A surface is a file or a directory.  A directory is walked ``MAX_PUBLISHED_DEPTH``
    levels deep,  so the workflow files a template keeps in a subdirectory are scanned
    while a surface still stays a small,  known set rather than a whole checkout.  A
    symlinked directory is not followed.
    """
    if os.path.isfile(path):
        return [path]
    if not os.path.isdir(path) or depth <= 0:
        return []
    out: list[str] = []
    for name in sorted(os.listdir(path)):
        child = os.path.join(path, name)
        if os.path.isfile(child):
            out.append(child)
        elif os.path.isdir(child) and not os.path.islink(child):
            out.extend(_published_files(child, depth - 1))
    return out


def _scan_published(
    surfaces: tuple[str, ...], repo_root: str
) -> tuple[list[tuple[str, str]], list[str]]:
    """The published surfaces that name an internal asset,  and the surfaces missing.

    Each finding is the file and the SHAPE of what it names,  never the value:  a
    finding that carried the value would put the value in the report.
    """
    findings: list[tuple[str, str]] = []
    absent: list[str] = []
    for surface in surfaces:
        path = surface if os.path.isabs(surface) else os.path.join(repo_root, surface)
        if not os.path.exists(path):
            absent.append(surface)
            continue
        for file_path in _published_files(path):
            try:
                with open(file_path, "rb") as handle:
                    raw = handle.read(MAX_PUBLISHED_BYTES + 1)
            except OSError:
                absent.append(os.path.relpath(file_path, repo_root))
                continue
            if len(raw) > MAX_PUBLISHED_BYTES:
                # A file too large to read whole is a file the rule never inspected,  so it
                # is unreadable,  and the rule reads UNKNOWN rather than PASS.
                absent.append(os.path.relpath(file_path, repo_root))
                continue
            text = raw.decode("utf-8", "replace")
            for label, pattern in LEAK_PATTERNS:
                if pattern.search(text):
                    findings.append((os.path.relpath(file_path, repo_root), label))
    return findings, absent


def _flat_key_files(directory: str) -> list[str]:
    """The regular files directly under the retired flat key directory,  sorted.

    Only the names are read,  and only the names are returned:  a credential file is
    never opened,  so no value can reach the report.  The directory is read one level
    deep,  because a flat key file is a file at the top of it.
    """
    if not os.path.isdir(directory):
        return []
    out: list[str] = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    for name in names:
        path = os.path.join(directory, name)
        try:
            if os.path.isfile(path) and not os.path.islink(path):
                out.append(name)
        except OSError:
            continue
    return out


def _leak_files(leaks: list[tuple[str, str]]) -> int:
    """How many distinct files the findings name.  One file can match several shapes."""
    return len({path for path, _shape in leaks})


def _finding_summary(
    directory_present: bool, file_count: int, leaks: list[tuple[str, str]]
) -> str:
    """One short phrase saying what the findings are,  with counts and no values."""
    parts: list[str] = []
    if directory_present:
        parts.append(
            "the retired secrets directory is still on disk,  holding %d file%s"
            % (file_count, "" if file_count == 1 else "s")
        )
    if leaks:
        shapes: list[str] = []
        for _path, shape in leaks:
            if shape not in shapes:
                shapes.append(shape)
        files = _leak_files(leaks)
        parts.append(
            "%d published file%s name%s %s"
            % (
                files,
                "" if files == 1 else "s",
                "s" if files == 1 else "",
                ",  ".join(shapes),
            )
        )
    if not parts:
        return "there is nothing to report"
    return ",  and ".join(parts)


def check_secrets_hygiene(rule: Rule, platform: str, ctx: CheckContext) -> CheckResult:
    """The secrets hygiene of this Mac,  as OPS-818 and Kody rules 37 and 60 describe it.

    Two kinds of finding count.  The first is the retired flat key directory:  it is the
    place a credential was written as a plain key file instead of through the secret
    store,  and the fleet's own cutover gates on retiring it,  so the directory being
    there is the finding and the files in it are the evidence.  The second is a published
    file that names an internal asset,  which is what the attack map holds and the public
    workflows must not.

    The rule declares the retired flat key directory in ``check.target`` and the published
    surfaces in ``expected.names``.  The number of findings the rule tolerates is a fleet
    knob,  and a knob that is unset leaves the rule undecided.

    Nothing here reads a credential:  the flat key directory is counted by name,  and the
    published files are scanned for shapes.  No value is ever put in a result or a reason.
    """
    reading, reason = read_threshold(rule, ctx)
    if reading is None:
        return _unknown(rule, platform, reason, ReasonCode.ERROR, ctx)
    directory = expand_target(rule.check.target or "~/.secrets", ctx.home)
    directory_present = os.path.isdir(directory)
    flat_files = _flat_key_files(directory)
    repo_root = _repo_root(ctx)
    if repo_root is None:
        return _unknown(
            rule,
            platform,
            "there is no checkout to scan,  so the published surfaces cannot be read",
            ReasonCode.ERROR,
            ctx,
        )
    surfaces = tuple(rule.expected.names)
    if not surfaces:
        return _unknown(
            rule,
            platform,
            "the rule declares no published surface,  so it cannot check what is published",
            ReasonCode.ERROR,
            ctx,
        )
    paths: list[str] = []
    for surface in surfaces:
        if not surface.startswith("published:"):
            return _unknown(
                rule,
                platform,
                "the declared published surface %s is not one this check knows" % surface,
                ReasonCode.UNPARSEABLE,
                ctx,
            )
        paths.append(surface.split(":", 1)[1])
    leaks, absent = _scan_published(tuple(paths), repo_root)
    if absent:
        return _unknown(
            rule,
            platform,
            "the declared published surface %s is not there,  so the rule cannot check it"
            % ", ".join(absent),
            ReasonCode.MISSING,
            ctx,
        )
    # A finding is a FILE that names an internal asset (or the retired directory),  not a
    # shape match:  one file naming two shapes is one finding against the threshold.
    findings = _leak_files(leaks) + (1 if directory_present else 0)
    names = tuple(
        "%s names %s" % (path, shape) for path, shape in leaks[:MAX_REPORTED_NAMES]
    )
    if findings > reading.value:
        return build_result(
            rule,
            platform,
            CheckState.DRIFT,
            "%d secrets-hygiene finding%s,  and the rule tolerates %d:  %s"
            % (
                findings,
                "" if findings == 1 else "s",
                reading.value,
                _finding_summary(directory_present, len(flat_files), leaks),
            ),
            ReasonCode.DRIFT,
            ObservedState(names=names),
            ctx,
        )
    return build_result(
        rule,
        platform,
        CheckState.PASS,
        "no secrets-hygiene finding,  and %s" % _threshold_note(reading),
        ReasonCode.OK,
        ObservedState(names=names),
        ctx,
    )


def _keychain_expiries(name: str, ctx: CheckContext) -> tuple[list[CertificateExpiry], list[str]]:
    """The expiry of every keychain certificate whose common name holds `name`.

    The second value is the reasons some of them could not be read:  a certificate the
    keychain would not give is a certificate the rule cannot check,  and the rule reads
    unknown rather than a clean bill of health.
    """
    try:
        result = run_cli(
            ["security", "find-certificate", "-a", "-c", name, "-p"],
            timeout=CERTIFICATE_TIMEOUT_SECONDS,
            env=ctx.child_env(),
            cwd=None,
        )
    except CheckError:
        return [], ["the keychain could not be read"]
    if result.error:
        return [], ["the keychain could not be read"]
    if result.timed_out:
        return [], ["the keychain did not answer in time"]
    if result.returncode != 0:
        return [], ["the keychain answered with code %d" % (result.returncode or 0)]
    text = result.stdout.decode("utf-8", "replace")
    unreadable: list[str] = []
    found = keychain_expiries(text, "keychain:%s" % name, unreadable)
    # One certificate that cannot be read is one the rule cannot check:  it is named,  so
    # the rule reads UNKNOWN with that reason instead of failing as a whole.
    return found, unreadable[:1]


def _profile_expiries(
    pattern: str, ctx: CheckContext
) -> tuple[list[CertificateExpiry], list[str]]:
    """The expiry of every provisioning profile the pattern matches.

    A profile that cannot be decoded is named in the second value,  so the rule reads
    unknown rather than pretending the profile is not there.
    """
    expanded = expand_target(pattern, ctx.home)
    out: list[CertificateExpiry] = []
    problems: list[str] = []
    paths = [path for path in sorted(glob.glob(expanded)) if os.path.isfile(path)]
    if len(paths) > MAX_PROFILES:
        # The profiles past the cap are not read,  and the rule says so instead of
        # reporting only the ones it reached.
        problems.append(
            "%d provisioning profiles match,  and the rule reads at most %d"
            % (len(paths), MAX_PROFILES)
        )
        paths = paths[:MAX_PROFILES]
    deadline = time.monotonic() + PROFILE_BUDGET_SECONDS
    for path in paths:
        remaining = int(deadline - time.monotonic())
        if remaining <= 0:
            problems.append("the provisioning profiles could not all be read in time")
            break
        try:
            result = run_cli(
                ["security", "cms", "-D", "-i", path],
                timeout=min(CERTIFICATE_TIMEOUT_SECONDS, remaining),
                env=ctx.child_env(),
                cwd=None,
            )
        except CheckError:
            problems.append("a profile could not be read")
            continue
        if result.error or result.timed_out:
            problems.append("a profile could not be read in time")
            continue
        if result.returncode != 0:
            problems.append("a profile could not be decoded")
            continue
        try:
            expires_at = profile_expiry(result.stdout)
        except ExpiryUnknown:
            problems.append("a profile carries no readable expiry")
            continue
        out.append(
            CertificateExpiry(
                label="profile:%s" % os.path.basename(path), expires_at=expires_at
            )
        )
    return out, problems


def check_certificate_expiry(rule: Rule, platform: str, ctx: CheckContext) -> CheckResult:
    """The Apple signing certificates the fleet uses,  and the day each one expires.

    The rule declares the inventory in ``expected.names``.  A source is either
    ``keychain:<name>``,  which is every certificate in the login keychain whose common
    name holds ``<name>``,  or ``profiles:<glob>``,  which is every provisioning profile
    the glob matches.  A rule that declares no inventory has nothing to check,  so it
    reads UNKNOWN:  an empty list is not a clean bill of health.

    The expiry window is a fleet knob.  A certificate that expires inside the window is
    a finding;  one that expires outside it is not.  A knob that is unset leaves the rule
    undecided.
    """
    reading, reason = read_threshold(rule, ctx)
    if reading is None:
        return _unknown(rule, platform, reason, ReasonCode.ERROR, ctx)
    sources = tuple(rule.expected.names)
    if not sources:
        return _unknown(
            rule,
            platform,
            "the rule declares no certificate inventory,  so it cannot check one",
            ReasonCode.ERROR,
            ctx,
        )
    expiries: list[CertificateExpiry] = []
    empty: list[str] = []
    problems: list[str] = []
    for source in sources:
        if source.startswith("keychain:"):
            found, issues = _keychain_expiries(source.split(":", 1)[1], ctx)
        elif source.startswith("profiles:"):
            found, issues = _profile_expiries(source.split(":", 1)[1], ctx)
        else:
            return _unknown(
                rule,
                platform,
                "the declared certificate source %s is not one this check knows" % source,
                ReasonCode.UNPARSEABLE,
                ctx,
            )
        if not found:
            empty.append(source)
        problems.extend(issues)
        expiries.extend(found)
    if problems:
        return _unknown(
            rule,
            platform,
            "%s,  so the rule cannot check every certificate" % problems[0],
            ReasonCode.UNPARSEABLE,
            ctx,
        )
    if empty:
        return _unknown(
            rule,
            platform,
            "the declared certificate source %s has no certificate on this machine,  so the "
            "rule cannot check it" % ", ".join(empty),
            ReasonCode.MISSING,
            ctx,
        )
    earliest = min(expiries, key=lambda item: item.expires_at)
    days = earliest.days_left(ctx.now)
    if days < reading.value:
        return build_result(
            rule,
            platform,
            CheckState.DRIFT,
            "the earliest expiry among %d certificate%s is in %d day%s,  and the window is %d "
            "day%s"
            % (
                len(expiries),
                "" if len(expiries) == 1 else "s",
                days,
                "" if days == 1 else "s",
                reading.value,
                "" if reading.value == 1 else "s",
            ),
            ReasonCode.DRIFT,
            ObservedState(names=tuple(item.label for item in expiries)),
            ctx,
        )
    return build_result(
        rule,
        platform,
        CheckState.PASS,
        "the earliest expiry among %d certificate%s is in %d day%s,  and the window is %d day%s"
        % (
            len(expiries),
            "" if len(expiries) == 1 else "s",
            days,
            "" if days == 1 else "s",
            reading.value,
            "" if reading.value == 1 else "s",
        ),
        ReasonCode.OK,
        ObservedState(names=tuple(item.label for item in expiries)),
        ctx,
    )


# The check types this module implements.  A compliance check type with no entry here is
# a check nobody wrote,  and the engine says so rather than skipping the rule.
CHECKS = {
    CheckType.COMMAND: check_command,
    CheckType.SECRETS_HYGIENE: check_secrets_hygiene,
    CheckType.CERTIFICATE_EXPIRY: check_certificate_expiry,
}
