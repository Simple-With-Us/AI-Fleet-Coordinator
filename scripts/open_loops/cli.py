"""`open-loops gather|render|lint|send`.

  gather [--no-mac] [--mac-facts PATH|URL] [--launch-at TIME] --json [--out PATH]
      Run the read-only gatherers and print the facts (schema 1).  --out writes them to a 0600 file instead.
      --no-mac is for a cloud host (no lanes, no C1, no C6);  --mac-facts reads the signed Mac facts file there
      (it is ignored, and reported as Missing, unless `fleet-facts verify` accepts it).
  render --facts PATH --markdown [--brief] [--out PATH]
      The facts as the full Markdown list (--brief: five items per list and "+ N more").
  lint --dm-file PATH --facts PATH
      Any seat:  run the same checks `send` runs on a DM and print the problems (exit 4 if any);  sends nothing, needs
      no simple-chat and no C2 id, so the author of a routine text can check a DM before BF-Deployer exists.
  send --dm-file PATH --facts PATH [--c2-id ID] [--dry-run] [--again]
      BF-Deployer only:  lint the DM, skip a duplicate, send it to the owner, post the heartbeat.

Exit status:  gather and render:  0 done, 2 usage, 3 an F3 helper is missing or Python is too old (nothing is
printed), 1 an unexpected error.  lint:  0 passes, 4 a problem, 2 and 3 as above.  send:  see send.py.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from fleet_facts import safety

from . import __version__
from . import lint as lintmod
from .facts import LAUNCH_ENV, build_facts
from .files import EXIT_USAGE, Refusal, load_facts, read_private_file, write_private_file
from .render import BRIEF_LIMIT, render_markdown
from .send import EXIT_LINT, EXIT_REFUSED, run_send

C2_ENV = "OPEN_LOOPS_C2_ID"
MAC_FACTS_ENV = "OPEN_LOOPS_MAC_FACTS_URL"


def _c2_file() -> Path:
    return Path(__file__).resolve().parent.parent / "c2-id"


def resolve_c2_id(flag: str | None, environ=os.environ) -> str | None:
    """--c2-id, else $OPEN_LOOPS_C2_ID, else the `c2-id` file install.sh --c2-id wrote next to the package."""
    for value in (flag, environ.get(C2_ENV)):
        if value and value.strip():
            return value.strip()
    try:
        text = _c2_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="open-loops", description="The fleet's daily open-loops digest:  gather, render, send."
    )
    parser.add_argument("--version", action="version", version="open-loops " + __version__)
    sub = parser.add_subparsers(dest="command", metavar="{gather,render,lint,send}")
    g = sub.add_parser(
        "gather", help="gather the facts", description="Gather read-only facts (schema 1)."
    )
    g.add_argument("--no-mac", action="store_true", help="cloud host:  skip the Mac-only parts")
    g.add_argument(
        "--mac-facts",
        metavar="PATH|URL",
        help="the signed gb/mac-facts.json (a path, or an https URL on api.github.com or raw.githubusercontent.com);  "
        "with --no-mac, defaults to $%s;  verified with `fleet-facts verify` using $FLEET_FACTS_SIGNING_KEY, else ignored"
        % MAC_FACTS_ENV,
    )
    g.add_argument(
        "--launch-at",
        metavar="TIME",
        help="when the claim clock starts (ISO 8601 with a zone;  default $%s)" % LAUNCH_ENV,
    )
    g.add_argument("--json", action="store_true", help="print the facts as JSON")
    g.add_argument(
        "--out",
        metavar="PATH",
        help="write the facts to this file (mode 0600) instead of printing them",
    )
    r = sub.add_parser(
        "render",
        help="render the facts as Markdown",
        description="The full list, every item of every section.",
    )
    r.add_argument(
        "--facts", required=True, metavar="PATH", help="a facts file written by `gather --out`"
    )
    r.add_argument("--markdown", action="store_true", help="Markdown (the only format)")
    r.add_argument(
        "--brief",
        action="store_true",
        help='at most 5 items per list and "+ N more":  what a routine reads to write the DM',
    )
    r.add_argument(
        "--out", metavar="PATH", help="write to this file (mode 0600) instead of printing"
    )
    k = sub.add_parser(
        "lint",
        help="check a DM against its facts (any seat;  sends nothing)",
        description="The checks `send` runs, alone.",
    )
    k.add_argument("--dm-file", required=True, metavar="PATH", help="the DM the routine wrote")
    k.add_argument(
        "--facts", required=True, metavar="PATH", help="the facts file it was written from"
    )
    s = sub.add_parser(
        "send",
        help="lint, dedupe and send the digest DM (BF-Deployer only)",
        description="See send.py.",
    )
    s.add_argument("--dm-file", required=True, metavar="PATH", help="the DM the routine wrote")
    s.add_argument(
        "--facts", required=True, metavar="PATH", help="the facts file it was written from"
    )
    s.add_argument(
        "--c2-id",
        metavar="ID",
        help="the C2 umbrella issue id for the heartbeat topic (default $%s, then the c2-id file)"
        % C2_ENV,
    )
    s.add_argument("--dry-run", action="store_true", help="check everything, send nothing")
    s.add_argument(
        "--again",
        action="store_true",
        help="send even though another digest went out today (the same facts id is never resent)",
    )
    return parser


def _emit(text: str) -> None:
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.buffer.flush()


def main(argv: Sequence[str] | None = None) -> int:
    if sys.version_info < (3, 11):
        sys.stderr.write("open-loops needs Python 3.11 or newer\n")
        return EXIT_REFUSED
    parser = _parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "gather":
            return _gather(args)
        if args.command == "render":
            return _render(args)
        if args.command == "lint":
            return _lint(args)
        if args.command == "send":
            return run_send(
                args.dm_file,
                args.facts,
                c2_id=resolve_c2_id(args.c2_id),
                dry_run=args.dry_run,
                again=args.again,
            )
    except Refusal as refusal:
        sys.stderr.write("open-loops: %s\n" % refusal)
        return refusal.code
    except safety.HelpersMissing as exc:
        sys.stderr.write("open-loops: %s;  nothing was printed\n" % exc)
        return EXIT_REFUSED
    parser.print_help(sys.stderr)
    return EXIT_USAGE


def _gather(args: argparse.Namespace) -> int:
    launch = args.launch_at or os.environ.get(LAUNCH_ENV) or None
    mac_facts = args.mac_facts
    if mac_facts is None and args.no_mac:
        mac_facts = os.environ.get(MAC_FACTS_ENV) or None
    facts = build_facts(no_mac=args.no_mac, mac_facts=mac_facts, launch_at=launch)
    for item in facts["missing"]:
        sys.stderr.write("open-loops: Missing:  %s (%s)\n" % (item["source"], item["reason"]))
    text = json.dumps(facts, indent=2) + "\n"
    if args.out:
        path = write_private_file(args.out, text)
        ok = sum(1 for s in facts["sources"].values() if s["status"] == "ok")
        sys.stderr.write(
            "open-loops: wrote %s to %s (%d of %d sources ok, %d missing)\n"
            % (facts["facts_id"], path, ok, len(facts["sources"]), len(facts["missing"]))
        )
        return 0
    if args.json:
        _emit(text)
    else:
        for name, source in facts["sources"].items():
            tail = ("  (%s)" % source["reason"]) if source["reason"] else ""
            sys.stdout.write("%s: %s%s\n" % (name, source["status"], tail))
    return 0


def _lint(args: argparse.Namespace) -> int:
    facts = load_facts(args.facts)
    text = read_private_file(args.dm_file, "the DM file")
    problems = lintmod.lint(text, facts)
    for problem in problems:
        sys.stdout.write("lint:  %s\n" % problem)
    if problems:
        sys.stderr.write(
            "open-loops: the DM did not pass %d check%s\n"
            % (len(problems), "" if len(problems) == 1 else "s")
        )
        return EXIT_LINT
    sys.stdout.write(
        "the DM passes the checks (%d characters)\n" % len(lintmod.prepare(text).strip())
    )
    return 0


def _render(args: argparse.Namespace) -> int:
    facts = load_facts(args.facts)
    text = render_markdown(facts, limit=BRIEF_LIMIT if args.brief else None)
    if args.out:
        write_private_file(args.out, text)
    else:
        _emit(text)
    return 0
