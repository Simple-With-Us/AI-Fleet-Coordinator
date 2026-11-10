"""``python3 -m fleet_knobs`` (run from scripts/):  look at knobs, never at secrets.

status [--json]            fetch once and show each knob's source and whether it is set;
                           secrets show only set or empty.  Exit 0, or 3 when Infisical was
                           unreachable (the answers are last-known-good or defaults).
get NAME                   print one NON-secret knob's value (lists and caps as text);
                           "stale" goes to stderr when Infisical was unreachable
status --json --values     the same, plus each NON-secret knob's value, whether a write token
                           is present here (never its value) and the check time:  Muster's
                           Controls tab reads this
inventory [--check F|--write F|--json|--check-json F|--write-json F]
                           print the INFISICAL.md table, or check or rewrite it in file F;
                           the JSON forms are fleet_knobs/registry.json, which Muster reads
unset NAME (--plan|--yes) [--json]
                           delete one NON-secret knob in Infisical so its default applies
set NAME VALUE (--plan|--yes) [--json]
                           change one NON-secret knob in Infisical (setter.py).  --plan shows the
                           write, --yes makes it with the write token.  Exit 0, 1 failed, 2 refused
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from . import inventory, knobs, setter, values
from .registry import BY_NAME, REGISTRY

# The shape of ``status --json``.  Muster reads it and refuses a schema it does not know.
STATUS_SCHEMA = 1


def _status(args, out) -> int:
    rows = []
    for spec in REGISTRY:
        if spec.secret:
            # A secret's row says whether it is set and never carries its value.
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
        if getattr(args, "values", False):
            # Asked for by name:  the value in the engine's canonical text, so a reader such as
            # Muster shows what a job would get without parsing the copy itself.  Non-secret
            # knobs only;  a plain status never prints a value.
            row["value"] = None if k.value is None else values.render(spec, k.value)
        if k.reason:
            row["reason"] = k.reason
        rows.append(row)
    rep = knobs.report()
    if args.json:
        doc: dict = {
            "schema": STATUS_SCHEMA,
            "fetch": rep["fetch"],
            "fetch_reason": rep.get("fetch_reason"),
        }
        if getattr(args, "values", False):
            # Asked for by name, like the values:  when this was read, and whether a write
            # could run here (the token's shape only, never its value).
            doc["checked_epoch"] = int(time.time())
            doc["write_token"] = (
                "present"
                if setter.read_token(dict(os.environ), sys.platform, setter.run)
                else "missing"
            )
        doc["knobs"] = rows
        json.dump(doc, out, indent=1)
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
    if args.json:
        out.write(inventory.json_text())
        return 0
    if args.check_json or args.write_json:
        path = args.check_json or args.write_json
        if args.check_json:
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except OSError:
                text = ""
            if text == inventory.json_text():
                return 0
            sys.stderr.write(
                f"{path}: the knob inventory is out of date;  run "
                f"`python3 -m fleet_knobs inventory --write-json {path}` from scripts/\n"
            )
            return 1
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(inventory.json_text())
        return 0
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


def _unset(args, out) -> int:
    mode = "plan" if args.plan else "yes" if args.yes else None
    answer, code = setter.unset_knob(args.name, mode)
    if args.json:
        json.dump(answer, out, sort_keys=True)
        out.write("\n")
    else:
        out.write(
            f"{answer['result']}: {args.name} ({answer['detail']})\n"
            if answer["detail"]
            else f"{answer['result']}: {args.name}\n"
        )
    return code


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
    p.add_argument(
        "--values",
        action="store_true",
        help="with --json, also carry each NON-secret knob's value (Muster's Controls tab)",
    )
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
    group.add_argument("--json", action="store_true", help="print the inventory Muster reads")
    group.add_argument("--check-json", metavar="FILE")
    group.add_argument("--write-json", metavar="FILE")
    p = sub.add_parser("set", help="change one non-secret knob (--plan first, then --yes)")
    p.add_argument("name")
    p.add_argument("value")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--plan", action="store_true", help="show the write and change nothing")
    group.add_argument("--yes", action="store_true", help="make the write")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser(
        "unset", help="delete one non-secret knob so its default applies (--plan first, then --yes)"
    )
    p.add_argument("name")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--plan", action="store_true", help="show the delete and change nothing")
    group.add_argument("--yes", action="store_true", help="make the delete")
    p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        return {
            "status": _status,
            "get": _get,
            "inventory": _inventory,
            "set": _set,
            "unset": _unset,
        }[args.cmd](args, out)
    except knobs.KnobError as exc:
        sys.stderr.write(f"fleet_knobs: {exc}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
