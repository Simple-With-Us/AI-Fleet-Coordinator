"""``python3 -m fleet_knobs`` (run from scripts/):  look at knobs, never at secrets.

status [--json]            fetch once and show each knob's source and whether it is set;
                           secrets show only set or empty.  Exit 0, or 3 when Infisical was
                           unreachable (the answers are last-known-good or defaults).
get NAME                   print one NON-secret knob's value (lists and caps as text);
                           "stale" goes to stderr when Infisical was unreachable
inventory [--check F|--write F]
                           print the INFISICAL.md table, or check or rewrite it in file F
set NAME VALUE (--plan|--yes) [--json]
                           change one NON-secret knob in Infisical (setter.py).  --plan shows the
                           write, --yes makes it with the write token.  Exit 0, 1 failed, 2 refused
"""

from __future__ import annotations

import argparse
import json
import sys

from . import inventory, knobs, setter, values
from .registry import BY_NAME, REGISTRY


def _status(args, out) -> int:
    rows = []
    for spec in REGISTRY:
        if spec.secret:
            try:
                knobs.resolve(spec.name)
                rows.append(
                    {
                        "name": spec.name,
                        "owner": spec.owner,
                        "set": True,
                        "source": "infisical",
                        "stale": False,
                    }
                )
            except knobs.SecretUnavailable as exc:
                rows.append(
                    {
                        "name": spec.name,
                        "owner": spec.owner,
                        "set": False,
                        "source": "none",
                        "stale": False,
                        "reason": exc.reason,
                    }
                )
            continue
        k = knobs.resolve(spec.name)
        row = {
            "name": spec.name,
            "owner": spec.owner,
            "set": k.source != "default",
            "source": k.source,
            "stale": k.stale,
        }
        if k.reason:
            row["reason"] = k.reason
        rows.append(row)
    rep = knobs.report()
    if args.json:
        json.dump(
            {"fetch": rep["fetch"], "fetch_reason": rep.get("fetch_reason"), "knobs": rows},
            out,
            indent=1,
        )
        out.write("\n")
    else:
        out.write(
            f"fetch: {rep['fetch']}"
            + (f" ({rep['fetch_reason']})" if rep.get("fetch_reason") else "")
            + "\n"
        )
        for r in rows:
            flag = " stale" if r["stale"] else ""
            out.write(
                f"{r['name']:<32} {r['owner']:<3} {'set' if r['set'] else 'unset':<6} "
                f"{r['source']}{flag}\n"
            )
    return 0 if rep["fetch"] == "ok" else 3


def _get(args, out) -> int:
    spec = BY_NAME.get(args.name)
    if spec is None:
        sys.stderr.write(f"unknown knob {args.name}\n")
        return 2
    if spec.secret:
        sys.stderr.write(f"{args.name} is a secret and is never printed\n")
        return 2
    k = knobs.resolve(args.name)
    if args.json:
        # The JSON form carries the value and where it came from,  so a caller that
        # must know whether a knob was really set (and not only defaulted) can ask.
        json.dump(
            {
                "name": args.name,
                "value": k.value,
                "source": k.source,
                "stale": k.stale,
                "reason": k.reason,
            },
            out,
            sort_keys=True,
        )
        out.write("\n")
    elif k.value is None:
        out.write("unset\n")
    else:
        out.write(values.render(spec, k.value) + "\n")
    if k.stale:
        sys.stderr.write("stale: Infisical was unreachable\n")
    return 0


def _inventory(args, out) -> int:
    if args.check or args.write:
        path = args.check or args.write
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        if args.check:
            if inventory.check(text):
                return 0
            sys.stderr.write(
                f"{path}: the fleet-knobs block is out of date;  run "
                f"`python3 -m fleet_knobs inventory --write {path}` from scripts/\n"
            )
            return 1
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(inventory.splice(text))
        return 0
    out.write(inventory.block() + "\n")
    return 0


def _set(args, out) -> int:
    mode = "plan" if args.plan else "yes" if args.yes else None
    answer, code = setter.set_knob(args.name, args.value, mode)
    if args.json:
        json.dump(answer, out, sort_keys=True)
        out.write("\n")
    else:
        out.write(f"{answer['result']}: {args.name}")
        if answer.get("new") is not None:
            out.write(f" {answer.get('old')} -> {answer['new']}")
        out.write(f" ({answer['detail']})\n" if answer["detail"] else "\n")
    return code


def main(argv: list[str] | None = None, out=None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="fleet_knobs", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("get")
    p.add_argument("name")
    p.add_argument(
        "--json",
        action="store_true",
        help="print the value as JSON with its source, so a caller can tell a set knob "
        "from a defaulted one",
    )
    p = sub.add_parser("inventory")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--check", metavar="FILE")
    group.add_argument("--write", metavar="FILE")
    p = sub.add_parser("set", help="change one non-secret knob (--plan first, then --yes)")
    p.add_argument("name")
    p.add_argument("value")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--plan", action="store_true", help="show the write and change nothing")
    group.add_argument("--yes", action="store_true", help="make the write")
    p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        return {"status": _status, "get": _get, "inventory": _inventory, "set": _set}[args.cmd](
            args, out
        )
    except knobs.KnobError as exc:
        sys.stderr.write(f"fleet_knobs: {exc}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
