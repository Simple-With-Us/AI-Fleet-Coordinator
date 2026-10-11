"""needs-owner:  file a decision that waits on Jay, list the open ones, read one back.

  needs-owner file  --by SEAT --app APP --question TEXT --option TEXT [--option TEXT ...]
                    [--low-stakes N ...] [--recommend N] [--parent ID] [--context TEXT]
                    [--urgent] [--again] [--mirror-of TOPIC_LINK] [--dry-run] [--json]
  needs-owner adopt ID --by SEAT --option TEXT [...] [--question TEXT] [--low-stakes N ...] [--recommend N]
                    [--parent ID] [--context TEXT] [--no-notify] [--json]
  needs-owner list  [--open | --replied | --all] [--seat SEAT] [--json]
  needs-owner status ID [--json]
  needs-owner notify [--dry-run] [--json]       the fallback notifier (CLAUDE only;  the LaunchAgent runs it every 5 minutes)

docs/protocols/needs-owner.md is the contract.
"""
from __future__ import annotations

import argparse
import sys
from typing import Mapping, Sequence, TextIO

from . import __version__
from .errors import NeedsOwnerError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="needs-owner", description="Decisions that wait on Jay:  file them in Linear, "
                                     "announce them from the filing seat's own bot, list and read them back.")
    parser.add_argument("--version", action="version", version="needs-owner " + __version__)
    sub = parser.add_subparsers(dest="verb", metavar="VERB", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--json", action="store_true", help="print one JSON document")

    def request(p: argparse.ArgumentParser) -> None:
        p.add_argument("--by", required=True, metavar="SEAT", help="your seat;  it must equal the seat of this session")
        p.add_argument("--option", action="append", metavar="TEXT", help="an answer Jay can give (1 to 5, repeatable)")
        p.add_argument("--low-stakes", action="append", metavar="N", help="option number a tap on its emoji may answer (repeatable "
                       "or comma separated);  never with --urgent")
        p.add_argument("--recommend", type=int, metavar="N", help="the option you recommend")
        p.add_argument("--parent", metavar="ID", help="the issue you hold that is waiting on this answer")
        p.add_argument("--context", metavar="TEXT", help="a line of background (at most 600 characters)")

    p = sub.add_parser("file", help="file a decision as a Linear issue and announce it from your bot")
    common(p)
    request(p)
    p.add_argument("--app", required=True, metavar="APP", help="the app or team the decision is about")
    p.add_argument("--question", required=True, metavar="TEXT", help="the question for Jay (at most 200 characters)")
    p.add_argument("--urgent", action="store_true", help="file as P1")
    p.add_argument("--again", action="store_true", help="ask again with changed words:  a new version that quotes the earlier item")
    p.add_argument("--mirror-of", metavar="TOPIC_LINK", help="mirror an OWNER topic a Grok Bot persona already opened (no post of its own)")
    p.add_argument("--dry-run", action="store_true", help="check everything and print the plan, change nothing")

    p = sub.add_parser("adopt", help="make an issue filed earlier a needs-owner item, and announce it")
    common(p)
    request(p)
    p.add_argument("id", metavar="ID", help="the Linear identifier of an issue you filed")
    p.add_argument("--question", metavar="TEXT", help="the question (default:  the issue title)")
    p.add_argument("--no-notify", action="store_true", help="adopt only;  post no OWNER topic")
    p.add_argument("--dry-run", action="store_true", help="check everything and print the plan, change nothing")

    p = sub.add_parser("list", help="the items, oldest first")
    common(p)
    group = p.add_mutually_exclusive_group()
    group.add_argument("--open", dest="which", action="store_const", const="open", help="waiting on Jay (the default)")
    group.add_argument("--replied", dest="which", action="store_const", const="replied", help="Jay has replied;  the filer has not closed it")
    group.add_argument("--all", dest="which", action="store_const", const="all", help="every labelled issue, closed ones too")
    p.add_argument("--seat", metavar="SEAT", help="only this seat's items")
    p.set_defaults(which="open")

    p = sub.add_parser("status", help="one item")
    common(p)
    p.add_argument("id", metavar="ID")

    p = sub.add_parser("notify", help="the fallback notifier:  send unsent notices, record answers, mirror Grok Bot requests (CLAUDE only)")
    common(p)
    p.add_argument("--by", metavar="SEAT", default=None, help="only CLAUDE runs it;  this is a check, as in file")
    p.add_argument("--dry-run", action="store_true", help="read everything and say what would be done, change nothing")
    return parser


def main(argv: Sequence[str] | None = None, *, environ: Mapping[str, str] | None = None,
         out: TextIO | None = None, err: TextIO | None = None, knobs: object = None, clock: object = None) -> int:
    import os

    out = out or sys.stdout
    err = err or sys.stderr
    env = os.environ if environ is None else environ
    try:
        # Before argparse:  `--help` and `--version` exit inside parse_args, and the install proof is that they run.
        # Importing verbs loads the F3 helpers by path, so a stable copy that lacks one fails every call, those two included.
        from . import verbs
    except (RuntimeError, ImportError, SyntaxError) as exc:  # F3Missing is a RuntimeError;  the others are a damaged file
        err.write("needs-owner: %s\n" % exc)
        return 1
    args = build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    io = verbs.Streams(env, out, err, knobs, clock)
    handlers = {"file": verbs.run_file, "adopt": verbs.run_adopt, "list": verbs.run_list, "status": verbs.run_status,
                "notify": verbs.run_notify}
    try:
        return handlers[args.verb](args, io)
    except NeedsOwnerError as exc:
        err.write("needs-owner: %s\n" % exc)
        return exc.exit_code
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 - say what failed, never print a traceback (it can carry a filer's words)
        if env.get("NEEDS_OWNER_DEBUG"):
            raise
        err.write("needs-owner: internal error (%s);  rerun with NEEDS_OWNER_DEBUG=1 for the traceback\n" % type(exc).__name__)
        return 1
