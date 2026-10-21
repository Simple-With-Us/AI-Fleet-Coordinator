"""``python3 -m fleet_view`` (run from scripts/):  the Fleet View collector.

    fleet-view status [--json] [--lanes-root PATH] [--registry PATH]
                      [--expiry-hours N] [--github-max N] [--linear-max N]

``status`` is read only.  It prints a human table by default and the pinned
JSON envelope with ``--json``.  The Swift Muster app runs this verb as a
subprocess by absolute path with a pinned interpreter and an explicit
environment, decodes ``schemaVersion``, and shows UNKNOWN — never the last
green — when the run fails.

Exit codes mirror ``fleet_config``:  0 PASS, 3 UNKNOWN, 2 refused, 64 usage.
The claim expiry ceiling comes from ``CLAIM_EXPIRY_HOURS`` when set, else 72
hours;  the resolved value rides in the envelope notes so a reader can see
which ceiling flagged a claim.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from datetime import UTC, datetime
from typing import TextIO

from . import collectors, contract
from .types import FleetReport, RequestBudget

__all__ = ["EXIT_OK", "EXIT_REFUSED", "EXIT_UNKNOWN", "EXIT_USAGE", "main"]

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_UNKNOWN = 3
EXIT_USAGE = 64

_DEFAULT_EXPIRY_HOURS = 72


class UsageError(Exception):
    """The caller asked for something the CLI cannot do.  It exits 64."""


class _Parser(argparse.ArgumentParser):
    """An argument parser that raises instead of exiting."""

    def error(self, message: str) -> None:
        raise UsageError(message)


def _repo_root() -> pathlib.Path:
    """The repo root:  two levels above this package's ``scripts/`` home."""
    return pathlib.Path(__file__).resolve().parent.parent.parent


def _load_registry(path):
    """Read fleet-apps.json into (SeatIndex, repo names).  Raises ValueError."""
    data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    seats = data.get("seats") or []
    owner = data.get("owner") or "Simple-With-Us"
    repos = []
    for app in data.get("apps") or []:
        repo = app.get("repo")
        if repo:
            repos.append("%s/%s" % (owner, repo))
    return collectors.SeatIndex(seats), repos


def _expiry_hours(args) -> int:
    if args.expiry_hours is not None:
        return args.expiry_hours
    raw = os.environ.get("CLAIM_EXPIRY_HOURS", "")
    try:
        return int(raw) if raw else _DEFAULT_EXPIRY_HOURS
    except ValueError:
        return _DEFAULT_EXPIRY_HOURS


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="fleet-view", description="Collect lanes, PRs and claims for Muster.")
    parser.add_argument("--lanes-root", default=None, help="default: ~/apps/lanes")
    parser.add_argument(
        "--registry", default=None, help="default: fleet-apps.json at the repo root"
    )
    parser.add_argument(
        "--expiry-hours", type=int, default=None, help="claim expiry ceiling in hours"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    status = sub.add_parser("status", help="collect once and report")
    status.add_argument("--json", action="store_true", help="print the pinned JSON envelope")
    status.add_argument("--github-max", type=int, default=60)
    status.add_argument("--linear-max", type=int, default=30)
    return parser


def _render(report: FleetReport) -> str:
    lines = ["fleet-view status  (%s)" % report.generated_at]
    lines.append("verdict: %s" % contract.overall_verdict(report))
    for name, section in (
        ("lanes", report.lanes),
        ("pullRequests", report.pull_requests),
        ("claims", report.claims),
    ):
        lines.append("  %-12s %-8s %s" % (name, section.state, section.reason))
    lines.append(
        "requests: github %d/%d, linear %d/%d"
        % (
            report.budget.github_used,
            report.budget.github_max,
            report.budget.linear_used,
            report.budget.linear_max,
        )
    )
    for note in report.notes:
        lines.append("note:  %s" % note)
    return "\n".join(lines) + "\n"


def _status(args, out: TextIO, err: TextIO) -> int:
    root = _repo_root()
    registry_path = pathlib.Path(args.registry) if args.registry else root / "fleet-apps.json"
    try:
        index, repos = _load_registry(registry_path)
    except (OSError, ValueError) as exc:
        err.write("fleet-view: registry unreadable: %s\n" % exc)
        return EXIT_REFUSED
    lanes_root = (
        pathlib.Path(args.lanes_root) if args.lanes_root else pathlib.Path.home() / "apps" / "lanes"
    )
    now = datetime.now(UTC)
    budget = RequestBudget(github_max=args.github_max, linear_max=args.linear_max)
    runner = collectors.make_runner()
    expiry = _expiry_hours(args)
    report = FleetReport(
        generated_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        lanes=collectors.collect_lanes(lanes_root, index, runner, now),
        pull_requests=collectors.collect_pull_requests(repos, index, runner, budget),
        claims=collectors.collect_claims(runner, budget, now, expiry),
        budget=budget,
        notes=("claim expiry ceiling: %d hour(s) (CLAIM_EXPIRY_HOURS)" % expiry,),
    )
    if args.json:
        json.dump(contract.build_envelope(report), out, indent=1)
        out.write("\n")
    else:
        out.write(_render(report))
    return EXIT_OK if contract.overall_verdict(report) == "PASS" else EXIT_UNKNOWN


def main(argv=None, out: TextIO = None, err: TextIO = None) -> int:
    """Parse the arguments and dispatch.  Returns the exit code."""
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except UsageError as exc:
        err.write("fleet-view: %s\n" % exc)
        return EXIT_USAGE
    try:
        if args.cmd == "status":
            return _status(args, out, err)
    except UsageError as exc:
        err.write("fleet-view: %s\n" % exc)
        return EXIT_USAGE
    err.write("fleet-view: unknown verb %r\n" % args.cmd)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
