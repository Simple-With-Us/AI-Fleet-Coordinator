"""``python3 -m fleet_config`` (run from scripts/):  the ``fleet-config`` CLI skeleton.

    fleet-config status [--json] [--seat TAG] [--platform ID] [--prove l2]
    fleet-config plan   [--json] [--seat TAG] [--platform ID]
    fleet-config apply  [--json] --only RULE-ID ...

Only argument parsing lives here in the kernel.  Each verb is a stub that says so and
exits 2, until its owning module lands (design doc, section 10).  Exit codes for the
finished CLI are pinned there too:  0 pass, 1 fail, 3 unknown, 2 refused, 64 usage.

A usage error is 64 and never 2.  An unknown verb, an unknown flag, or a ``--seat``
value that is not a registry tag is a mistake in the request, and the caller has to fix
the command line;  2 is a refusal that needs the owner (a bad manifest, a bad registry,
a declined Touch ID confirmation, or a verb that is not implemented yet).  The parser
raises instead of exiting, so argparse's own exit status 2 never leaks out of this CLI.
"""

from __future__ import annotations

import argparse
import sys

from . import manifest as manifest_module

__all__ = [
    "EXIT_FAIL",
    "EXIT_OK",
    "EXIT_REFUSED",
    "EXIT_UNKNOWN",
    "EXIT_USAGE",
    "main",
]

# The exit codes the design document pins (section 4).  They follow the ``fleet_knobs``
# precedent:  3 is the unreachable source,  2 is the refused run,  and 64 is the usage
# error, which is a different thing from a refusal and never shares its code.
EXIT_OK = 0
EXIT_FAIL = 1
EXIT_REFUSED = 2
EXIT_UNKNOWN = 3
EXIT_USAGE = 64


class UsageError(Exception):
    """The caller asked for something the CLI cannot do.  It exits 64."""


class _Parser(argparse.ArgumentParser):
    """An argument parser that raises instead of exiting,  so the exit code is ours."""

    def error(self, message: str) -> None:
        raise UsageError(message)


def _check_filters(args: argparse.Namespace) -> None:
    """Refuse an unknown seat.  An unknown value is a usage error, never an empty pass."""
    seat = args.seat
    if seat is None:
        return
    try:
        roster = manifest_module.registry_seat_tags()
    except manifest_module.ManifestError:
        # A registry that cannot be read is not a usage error:  the run refuses anyway,
        # and the caller has nothing to fix in the command line.
        return
    if seat not in roster:
        raise UsageError("unknown seat %r;  the registry holds %s" % (seat, ", ".join(roster)))


def _not_implemented(args: argparse.Namespace) -> int:
    """A verb whose engine has not landed yet.  It refuses rather than guessing."""
    sys.stderr.write(
        "fleet-config %s is not implemented yet;  see "
        "docs/plans/2026-10-19-fleet-config-design.md\n" % args.cmd
    )
    return EXIT_REFUSED


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch.  ``--help`` is the only finished surface for now."""
    parser = _Parser(
        prog="fleet-config",
        description="Check and repair fleet seat tooling config against the manifest.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for verb in ("status", "plan", "apply"):
        verb_parser = sub.add_parser(verb)
        verb_parser.add_argument("--json", action="store_true")
        verb_parser.add_argument("--seat", metavar="TAG")
        verb_parser.add_argument("--platform", metavar="ID")
        verb_parser.set_defaults(handler=_not_implemented)
    sub.choices["status"].add_argument("--prove", choices=("l2",), metavar="l2")
    sub.choices["apply"].add_argument("--only", action="append", metavar="RULE-ID", default=[])
    try:
        args = parser.parse_args(argv)
        _check_filters(args)
    except UsageError as exc:
        sys.stderr.write("fleet-config: %s\n" % exc)
        return EXIT_USAGE
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
