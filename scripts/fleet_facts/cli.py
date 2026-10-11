"""`fleet-facts gather [--part NAME]... [--no-mac] --json`, `publish-mac`, `publish` and `verify`

  gather        run the parts (all of them by default) and print the facts document.  Read-only:  it reads
                GitHub, Linear and local state and writes nothing but the lane report cache.
  --part NAME   github, linear, lanes, autopush, backup, asks, report, needs_owner or consult;  repeat it
                or separate names with commas
  --no-mac      for a cloud host:  every Mac-only part (lanes, autopush, backup, asks, report, needs_owner,
                consult) is marked absent and is not run
  --json        print the document as JSON (without it, one summary line per part)

  publish-mac   one publish cycle:  gather the Mac parts, sign them and write mac-facts.json, gb/mac-facts.json
                and gb/full-list.md to the private fleet-loops-data repository (what the LaunchAgent runs).
                --dry-run writes the three files to --out DIR (default ~/.local/state/fleet-facts/dry-run)
                and sends nothing.
  publish       --path REL --file LOCAL:  sign (a .json file) or scan (anything else) one file and write it to
                REL in the same repository.  REL is mac-facts.json or a file under gb/.
  verify FILE   check a signed file against $FLEET_FACTS_SIGNING_KEY (--path REL: also that it was signed for REL).

Exit status:  gather:  0 the document was built (a part may still be "failed", see its `sources` entry), 2 a usage
error, 3 a safety helper is missing or this Python is too old (nothing is printed), 1 an unexpected error.
publish-mac and publish:  0 published, or skipped on purpose (load, another run), 3 nothing was published because a safety
condition failed (no key, the repository gate, a secret), 4 a write failed or the 20-minute cap was reached, 2 usage.
verify:  0 verified, 1 not verified (unsigned, tampered, wrong key, not JSON), 3 cannot check (no usable key),
2 usage (never a verdict:  the open-loops consumer reads 2 as "this fleet-facts has no verify command").
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from . import safety, signing
from .gather import PARTS, build, normalize_parts

EXIT_USAGE = 2
EXIT_UNSAFE = 3


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fleet-facts", description="Fleet facts for the open-loops digest:  read-only gatherers, and the signed Mac facts publisher.")
    parser.add_argument("--version", action="version", version="fleet-facts " + __version__)
    sub = parser.add_subparsers(dest="command", metavar="{gather,publish-mac,publish,verify}")
    gather = sub.add_parser("gather", help="gather the facts and print them",
                            description="Gather read-only facts.  Parts:  %s." % ", ".join(PARTS))
    gather.add_argument("--part", action="append", metavar="NAME", help="a part to run (repeatable, comma-separated; default all)")
    gather.add_argument("--no-mac", action="store_true", help="cloud host:  skip and mark absent every Mac-only part")
    gather.add_argument("--json", action="store_true", help="print the facts document as JSON")
    cycle = sub.add_parser("publish-mac", help="one publish cycle (what the LaunchAgent runs)",
                           description="Gather the Mac parts, sign them and write them to the private fleet-loops-data repository.")
    cycle.add_argument("--dry-run", action="store_true", help="build and sign everything, write the files to --out, send nothing")
    cycle.add_argument("--out", metavar="DIR", help="with --dry-run:  where to write the files (default ~/.local/state/fleet-facts/dry-run)")
    one = sub.add_parser("publish", help="sign and write one file", description="Sign (a .json file) or scan (any other) and write one file.")
    one.add_argument("--path", required=True, metavar="REL", help="mac-facts.json or a file under gb/")
    one.add_argument("--file", required=True, metavar="LOCAL", help="the local file to write")
    one.add_argument("--dry-run", action="store_true", help="check and sign it, send nothing")
    check = sub.add_parser("verify", help="check a signed file", description="Verify the HMAC signature of a signed file with $%s." % signing.KEY_ENV)
    check.add_argument("file", metavar="FILE", help="the signed JSON file")
    check.add_argument("--path", metavar="REL", help="also require that the file was signed for this repository path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    if sys.version_info < (3, 11):
        sys.stderr.write("fleet-facts needs Python 3.11 or newer\n")
        return EXIT_UNSAFE
    parser = _parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command in ("publish-mac", "publish", "verify"):
        return _signed_commands(args)
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


def _signed_commands(args: argparse.Namespace) -> int:
    from . import publish  # noqa: PLC0415 - the gatherers do not need it
    try:
        if args.command == "verify":
            code, line = publish.verify_file(args.file, os.environ, signed_for=args.path)
            (sys.stdout if code == 0 else sys.stderr).write("fleet-facts: %s\n" % line)
            return code
        if args.command == "publish":
            outcome = publish.publish_file(args.path, args.file, dry_run=args.dry_run)
        else:
            if args.out and not args.dry_run:
                sys.stderr.write("fleet-facts: --out only goes with --dry-run\n")
                return EXIT_USAGE
            folder = (Path(args.out).expanduser() if args.out else publish.state_dir() / "dry-run") if args.dry_run else None
            outcome = publish.run_cycle(dry_run_dir=folder)
    except safety.HelpersMissing as exc:
        sys.stderr.write("fleet-facts: %s;  nothing was published\n" % exc)
        return EXIT_UNSAFE
    except Exception as exc:  # noqa: BLE001 - say what kind, never what it held
        sys.stderr.write("fleet-facts: unexpected error (%s);  nothing more was done\n" % type(exc).__name__)
        return 1
    if args.command == "publish":
        for rel, what in outcome.files.items():
            sys.stdout.write("fleet-facts: %s: %s\n" % (rel, what))
        if outcome.reason:
            sys.stderr.write("fleet-facts: %s:  %s\n" % (outcome.result, outcome.reason))
    elif outcome.exit_code != 0 and outcome.reason:
        sys.stderr.write("fleet-facts: %s:  %s\n" % (outcome.result, outcome.reason))
    return outcome.exit_code
