"""``python3 -m fleet_agent_config reconcile`` from the scripts directory.

Check-only unless ``--apply`` is present.  Exit 0 when no managed surface
drifts, 1 when one does, and 64 on a usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from fleet_agent_config.reconcile import UsageError, reconcile, render

EXIT_USAGE = 64


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        sys.stderr.write(f"fleet-agent-config: {message}\n")
        raise UsageError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="fleet-agent-config",
        description="Reconcile agent skills, rules blocks, hooks and MCP config from this checkout.",
    )
    sub = parser.add_subparsers(dest="command")
    rec = sub.add_parser("reconcile", help="compare this host with the checkout (default)")
    rec.add_argument("--home", default=None, help="home directory to read (default: $HOME)")
    rec.add_argument("--json", action="store_true", help="print the JSON envelope")
    rec.add_argument(
        "--apply",
        action="store_true",
        help="write managed drift through the existing installers",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0].startswith("-"):
        args.insert(0, "reconcile")
    try:
        ns = _parser().parse_args(args)
        if ns.command != "reconcile":
            raise UsageError("a command is required: reconcile")
        home = ns.home if ns.home is not None else os.path.expanduser("~")
        result = reconcile(home, apply=ns.apply)
    except UsageError as exc:
        sys.stderr.write(f"fleet-agent-config: {exc}\n")
        return EXIT_USAGE
    if ns.json:
        sys.stdout.write(json.dumps(result.as_dict(), indent=2) + "\n")
    else:
        sys.stdout.write(render(result))
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
