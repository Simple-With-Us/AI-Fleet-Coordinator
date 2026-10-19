"""``python3 -m fleet_config`` (run from scripts/):  the ``fleet-config`` CLI skeleton.

    fleet-config status [--json] [--seat TAG] [--platform ID] [--prove l2]
    fleet-config plan   [--json] [--seat TAG] [--platform ID]
    fleet-config apply  [--json] --only RULE-ID ...

Only argument parsing lives here in the kernel.  Each verb is a stub that says so and
exits 2, until its owning module lands (design doc, section 10).  Exit codes for the
finished CLI are pinned there too:  0 pass, 1 fail, 3 unknown, 2 refused, 64 usage.
"""
from __future__ import annotations

import argparse
import sys
from typing import List, Optional


def _not_implemented(args: argparse.Namespace) -> int:
    """A verb whose engine has not landed yet.  It refuses rather than guessing."""
    sys.stderr.write(
        "fleet-config %s is not implemented yet;  see "
        "docs/plans/2026-10-19-fleet-config-design.md\n" % args.cmd
    )
    return 2


def main(argv: Optional[List[str]] = None) -> int:
    """Parse arguments and dispatch.  ``--help`` is the only finished surface for now."""
    parser = argparse.ArgumentParser(
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
    args = parser.parse_args(argv)
    return int(args.handler(args))


if __name__ == "__main__":
    sys.exit(main())
