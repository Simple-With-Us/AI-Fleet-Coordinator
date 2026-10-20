"""``python3 -m fleet_config`` (run from scripts/):  the ``fleet-config`` CLI.

    fleet-config status [--seat TAG] [--kind KIND] [--platform ID] [--json] [--prove]
    fleet-config apply  <seat> [kind] [--plan] [--yes]

``status`` is read-only apart from the last-known-good copy in the state directory.  It
prints a human table by default and the pinned JSON envelope with ``--json``.  The JSON
output is exactly ``contract.build_status_envelope(report)``:  no extra field, no wrapper,
no second copy of the results.

``apply`` shows the plan with ``--plan`` and changes nothing.  Without ``--plan`` it
applies, through the existing installers, and only the rules whose manifest entry allows
an automatic fix.  ``--yes`` skips the interactive confirmation;  it never skips the Touch
ID gate,  because a persistence-class write (hooks and plugins) needs a human whether the
caller asked for one or not.

Exit codes:

    0   the verdict is PASS
    1   the verdict is FAIL
    2   the verdict is UNKNOWN
    3   the run was refused (a bad manifest,  or a refused Touch ID confirmation)
    64  a usage error

UNKNOWN is not 0 on purpose.  The verdict is the fail-closed summary of the whole run:
a check that could not produce a trustworthy answer is not a pass,  so a run that holds
one cannot verdict PASS.  Exiting 0 for an UNKNOWN run would let a caller read "nothing
is wrong" from a run that proved nothing,  which is the one thing this engine must never
say.  The design document (section 4) assigns 3 to UNKNOWN and 2 to a refused run;  this
lane's task pins UNKNOWN to 2,  so the two numbers are swapped against that document.
They live in the constants below,  so reconciling them is a one-line change.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from typing import List, Optional, Sequence, TextIO

from . import aggregator, apply as apply_module, cache, contract, manifest as manifest_module, plan as plan_module
from .checks import CheckContext, CheckError, resolve_home, utc_now
from .types import CheckResult, CheckState, ConfigKind, Manifest, OverallVerdict, StatusReport

__all__ = [
    "EXIT_FAIL",
    "EXIT_OK",
    "EXIT_REFUSED",
    "EXIT_UNKNOWN",
    "EXIT_USAGE",
    "main",
    "render_report",
]

# The exit codes, named so a consumer can check them without a magic number.
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_UNKNOWN = 2
EXIT_REFUSED = 3
EXIT_USAGE = 64

# The worst state first,  which is how the human table is ordered.
_SEVERITY = (
    CheckState.MISSING,
    CheckState.DRIFT,
    CheckState.UNKNOWN,
    CheckState.UNPROVEN,
    CheckState.UNSUPPORTED,
    CheckState.PASS,
)


class UsageError(Exception):
    """The caller asked for something the CLI cannot do.  It exits 64."""


class _Parser(argparse.ArgumentParser):
    """An argument parser that raises instead of exiting,  so the exit code is ours."""

    def error(self, message: str) -> None:
        raise UsageError(message)


def _kind_value(raw: str) -> ConfigKind:
    """One config kind by name, or a usage error."""
    try:
        return ConfigKind(raw)
    except ValueError:
        raise UsageError(
            "unknown kind %r;  choose from %s" % (raw, ", ".join(k.value for k in ConfigKind))
        )


def _verdict_exit(verdict: OverallVerdict) -> int:
    """The exit code for a verdict.  Only PASS is zero."""
    if verdict is OverallVerdict.PASS:
        return EXIT_OK
    if verdict is OverallVerdict.FAIL:
        return EXIT_FAIL
    return EXIT_UNKNOWN


def _sorted_results(results: Sequence[CheckResult]) -> List[CheckResult]:
    """The results ordered worst state first,  then by seat, platform and rule id."""
    order = {state: index for index, state in enumerate(_SEVERITY)}
    return sorted(
        results,
        key=lambda result: (
            order.get(result.state, len(_SEVERITY)),
            result.seat,
            result.platform,
            result.rule_id,
        ),
    )


def render_report(report: StatusReport) -> str:
    """The human rendering of a report:  one line per result,  worst state first.

    It adds no fact the JSON envelope lacks.
    """
    lines: List[str] = []
    lines.append("fleet-config status  (%s)" % report.generated_at)
    lines.append("verdict: %s" % report.summary.overall_verdict.value)
    for result in _sorted_results(report.results):
        lines.append(
            "  %-11s %-32s %-12s %-12s %s"
            % (
                result.state.value,
                result.rule_id,
                result.kind.value,
                result.platform,
                result.reason,
            )
        )
    failing = sum(1 for result in report.results if contract.is_failing(result))
    lines.append(
        "summary:  %d check%s,  %d failing,  verdict %s"
        % (
            report.summary.total,
            "" if report.summary.total == 1 else "s",
            failing,
            report.summary.overall_verdict.value,
        )
    )
    for note in report.notes:
        lines.append("note:  %s" % note)
    return "\n".join(lines) + "\n"


def _build_parser() -> argparse.ArgumentParser:
    """The argument parser.  The verbs are exactly ``status`` and ``apply``."""
    parser = _Parser(
        prog="fleet-config",
        description="Check and repair fleet seat tooling config against the manifest.",
    )
    parser.add_argument(
        "--manifest",
        metavar="PATH",
        default=None,
        help="the manifest to read (default: fleet-seats.json at the repo root)",
    )
    parser.add_argument(
        "--registry",
        metavar="PATH",
        default=None,
        help="the fleet registry to read (default: fleet-apps.json at the repo root)",
    )
    parser.add_argument(
        "--home",
        metavar="PATH",
        default=None,
        help="the home directory the checks read (default: $HOME)",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    status = sub.add_parser("status", help="check every rule and report the state")
    status.add_argument("--seat", metavar="TAG", default=None, help="one registry tag")
    status.add_argument("--kind", metavar="KIND", default=None, help="one config kind")
    status.add_argument("--platform", metavar="ID", default=None, help="one platform id")
    status.add_argument("--json", action="store_true", help="print the pinned JSON envelope")
    status.add_argument(
        "--prove",
        nargs="?",
        const="l2",
        choices=("l2",),
        default=None,
        help="run the expensive L2 proof as well (off by default)",
    )
    status.add_argument(
        "--no-doctor",
        action="store_true",
        help="do not fold the MAC level doctors into the run (they run once, not per seat)",
    )

    apply_parser = sub.add_parser("apply", help="apply the plan for one seat")
    apply_parser.add_argument("seat", metavar="SEAT", help="the registry tag to repair")
    apply_parser.add_argument(
        "kind",
        metavar="KIND",
        nargs="?",
        default=None,
        help="one config kind (default: every kind)",
    )
    apply_parser.add_argument(
        "--plan",
        action="store_true",
        help="print the plan and change nothing",
    )
    apply_parser.add_argument(
        "--yes",
        action="store_true",
        help="apply without asking on stdin;  the Touch ID gate still applies",
    )
    return parser


def _load_manifest(args: argparse.Namespace) -> Manifest:
    """Load the manifest, or raise the CLI's own error."""
    path = None
    if args.manifest:
        path = pathlib.Path(os.path.expanduser(args.manifest)).absolute()
    registry = None
    if args.registry:
        registry = pathlib.Path(os.path.expanduser(args.registry)).absolute()
    return manifest_module.load_manifest(path, registry)


def _check_filters(loaded: Manifest, seat: Optional[str], kind: Optional[str], platform: Optional[str]) -> None:
    """Refuse an unknown seat, kind or platform.  An unknown value is a usage error."""
    if seat is not None and loaded.seat(seat) is None:
        raise UsageError(
            "unknown seat %r;  the manifest holds %s"
            % (seat, ", ".join(entry.seat for entry in loaded.seats))
        )
    if platform is not None and platform not in plan_module.known_platforms():
        raise UsageError(
            "unknown platform %r;  choose from %s"
            % (platform, ", ".join(plan_module.known_platforms()))
        )


def _status(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    """The ``status`` verb."""
    kind = _kind_value(args.kind) if args.kind else None
    try:
        loaded = _load_manifest(args)
    except manifest_module.ManifestError as exc:
        return _serve_cached(args, out, err, str(exc))
    _check_filters(loaded, args.seat, args.kind, args.platform)
    ctx = CheckContext(home=resolve_home(args.home), now=utc_now())
    report = aggregator.run_status(
        loaded,
        seat=args.seat,
        kind=kind,
        platform=args.platform,
        ctx=ctx,
        prove=bool(args.prove),
        include_doctors=not args.no_doctor,
    )
    try:
        cache.save_report(report)
    except (OSError, ValueError):
        pass  # the cache is a convenience;  a run that cannot write it still answers
    if args.json:
        json.dump(contract.build_status_envelope(report), out, indent=1)
        out.write("\n")
    else:
        out.write(render_report(report))
    return _verdict_exit(report.summary.overall_verdict)


def _serve_cached(args: argparse.Namespace, out: TextIO, err: TextIO, reason: str) -> int:
    """Serve the last good report,  marked stale,  when a run could not happen.

    The cached copy is never presented as fresh:  every result past its TTL is re-labelled
    ``UNKNOWN`` with the reason code ``stale`` before it reaches the envelope.
    """
    err.write("fleet-config: %s\n" % reason)
    try:
        cached, note = cache.load_report()
    except OSError:
        cached, note = None, "unreadable"
    if cached is None:
        err.write("fleet-config: there is no cached report to serve (%s)\n" % note)
        return EXIT_REFUSED
    stale = aggregator.mark_stale_results(cached, force=True)
    err.write("fleet-config: serving the last good report,  marked stale (%s)\n" % note)
    if args.json:
        json.dump(contract.build_status_envelope(stale), out, indent=1)
        out.write("\n")
    else:
        out.write(render_report(stale))
    return _verdict_exit(stale.summary.overall_verdict)


def _confirm_or_refuse(plan: plan_module.Plan, assume_yes: bool, out: TextIO, err: TextIO) -> bool:
    """Ask the caller to confirm the plan.  False means the run is refused."""
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        err.write(
            "fleet-config: there is no terminal to ask on;  pass --yes to apply without a "
            "prompt,  or --plan to see the plan and change nothing\n"
        )
        return False
    out.write(plan.render())
    out.write("Apply these %d change%s?  [y/N] " % (len(plan.autofixable()), "" if len(plan.autofixable()) == 1 else "s"))
    out.flush()
    answer = sys.stdin.readline().strip().lower()
    return answer in ("y", "yes")


def _apply(args: argparse.Namespace, out: TextIO, err: TextIO) -> int:
    """The ``apply`` verb."""
    kind = _kind_value(args.kind) if args.kind else None
    try:
        loaded = _load_manifest(args)
    except manifest_module.ManifestError as exc:
        err.write("fleet-config: %s\n" % exc)
        return EXIT_REFUSED
    _check_filters(loaded, args.seat, args.kind, None)
    ctx = CheckContext(home=resolve_home(args.home), now=utc_now())
    report = aggregator.run_status(
        loaded,
        seat=args.seat,
        kind=kind,
        ctx=ctx,
    )
    plan = plan_module.build_plan(loaded, report.results, seat=args.seat, kind=kind)
    if args.plan:
        out.write(plan.render())
        return EXIT_OK
    if not _confirm_or_refuse(plan, args.yes, out, err):
        return EXIT_REFUSED
    result = apply_module.apply_plan(
        plan,
        home=str(ctx.home),
        manifest=loaded,
        ctx=ctx,
    )
    out.write(result.render())
    if result.report is not None:
        try:
            cache.save_report(result.report)
        except (OSError, ValueError):
            pass  # the cache is a convenience;  a run that cannot write it still answers
    if result.refused:
        return EXIT_REFUSED
    if result.failed:
        return EXIT_FAIL
    if result.verdict is None:
        return EXIT_UNKNOWN
    return _verdict_exit(result.verdict)


def main(argv: Optional[List[str]] = None, out: Optional[TextIO] = None, err: Optional[TextIO] = None) -> int:
    """Parse the arguments and dispatch.  Returns the exit code."""
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except UsageError as exc:
        err.write("fleet-config: %s\n" % exc)
        return EXIT_USAGE
    try:
        if args.cmd == "status":
            return _status(args, out, err)
        if args.cmd == "apply":
            return _apply(args, out, err)
    except UsageError as exc:
        err.write("fleet-config: %s\n" % exc)
        return EXIT_USAGE
    except manifest_module.ManifestError as exc:
        err.write("fleet-config: %s\n" % exc)
        return EXIT_REFUSED
    except CheckError as exc:
        err.write("fleet-config: %s\n" % exc)
        return EXIT_REFUSED
    err.write("fleet-config: unknown verb %r\n" % args.cmd)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
