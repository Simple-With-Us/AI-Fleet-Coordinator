"""`fleet-facts gather [--part NAME]... [--no-mac] --json`

  gather        run the parts (all of them by default) and print the facts document.  Read-only:  it reads
                GitHub, Linear and local state and writes nothing but the lane report cache.
  --part NAME   github, linear, lanes, autopush, backup, asks, report, needs_owner or consult;  repeat it
                or separate names with commas
  --no-mac      for a cloud host:  every Mac-only part (lanes, autopush, backup, asks, report, needs_owner,
                consult) is marked absent and is not run
  --json        print the document as JSON (without it, one summary line per part)

Exit status:  0 the document was built (a part may still be "failed", see its `sources` entry), 2 a usage
error, 3 a safety helper is missing or this Python is too old (nothing is printed), 1 an unexpected error.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from . import __version__
from . import safety
from .gather import PARTS, build, normalize_parts

EXIT_USAGE = 2
EXIT_UNSAFE = 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleet-facts", description="Read-only fleet facts for the open-loops digest.")
    parser.add_argument("--version", action="version", version="fleet-facts " + __version__)
    sub = parser.add_subparsers(dest="command", metavar="{gather}")
    gather = sub.add_parser("gather", help="gather the facts and print them",
                            description="Gather read-only facts.  Parts:  %s." % ", ".join(PARTS))
    gather.add_argument("--part", action="append", metavar="NAME", help="a part to run (repeatable, comma-separated; default all)")
    gather.add_argument("--no-mac", action="store_true", help="cloud host:  skip and mark absent every Mac-only part")
    gather.add_argument("--json", action="store_true", help="print the facts document as JSON")
    sub.add_parser("publish-mac", help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if sys.version_info < (3, 11):
        sys.stderr.write("fleet-facts needs Python 3.11 or newer\n")
        return EXIT_UNSAFE
    parser = _parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "publish-mac":
        sys.stderr.write("fleet-facts: publish-mac is built by open-loops unit F7 and is not in this version\n")
        return EXIT_UNSAFE
    if args.command != "gather":
        parser.print_help(sys.stderr)
        return EXIT_USAGE
    try:
        normalize_parts(args.part)
    except ValueError as exc:
        sys.stderr.write("fleet-facts: %s\n" % exc)
        return EXIT_USAGE
    try:
        document = build(args.part, no_mac=args.no_mac)
    except safety.HelpersMissing as exc:
        sys.stderr.write("fleet-facts: %s;  nothing was printed\n" % exc)
        return EXIT_UNSAFE
    for name, source in document["sources"].items():
        if source["status"] == "failed":
            sys.stderr.write("fleet-facts: %s failed:  %s\n" % (name, source["reason"]))
    if args.json:
        sys.stdout.write(json.dumps(document, indent=2) + "\n")
    else:
        for name, source in document["sources"].items():
            tail = ("  (%s)" % source["reason"]) if source["reason"] else ""
            sys.stdout.write("%s: %s%s\n" % (name, source["status"], tail))
    return 0
