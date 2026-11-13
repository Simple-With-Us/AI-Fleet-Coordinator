"""`simple-peer-actions log` — append one Peer-Actions Registry row (AFC-745)."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import identity as I
from . import peer_actions as P


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="simple-peer-actions", description="Peer-Actions Registry logging"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="print one JSON object")
    log = sub.add_parser("log", parents=[common], help="append one registry row")
    log.add_argument("--acting", "--acting-seat", dest="acting_seat", metavar="SEAT")
    log.add_argument(
        "--requesting", "--requesting-seat", dest="requesting_seat", required=True, metavar="SEAT"
    )
    log.add_argument(
        "--request", "--request-source", dest="request_source", required=True, metavar="TEXT"
    )
    log.add_argument(
        "--action", "--action-taken", dest="action_taken", required=True, metavar="TEXT"
    )
    log.add_argument("--result", required=True, metavar="TEXT")
    log.add_argument(
        "--dry-run", action="store_true", help="validate and print the row without writing"
    )
    log.add_argument(
        "--as",
        dest="as_seat",
        metavar="NAME",
        help="same as --acting; follows simple-tools identity rules",
    )
    return parser


def _acting_seat(ns: argparse.Namespace) -> str:
    raw = ns.acting_seat or ns.as_seat
    if raw:
        return I.canonical_seat(raw)
    resolved = I.resolve_seat(os.environ, flag=None, default=None)
    if resolved.seat:
        return resolved.seat
    raise P.PeerActionsError(
        "invalid_argument", "name the acting seat with --acting or set AGENT_LAUNCH_SEAT"
    )


def cmd_log(ns: argparse.Namespace) -> int:
    try:
        entry = P.build_entry(
            acting_seat=_acting_seat(ns),
            requesting_seat=ns.requesting_seat,
            request_source=ns.request_source,
            action_taken=ns.action_taken,
            result=ns.result,
            known=(os.environ.get(P.ENV_INFISICAL_TOKEN, ""), os.environ.get(P.ENV_SA_JSON, "")),
        )
        out = P.log_row(entry, dry_run=bool(ns.dry_run))
    except P.PeerActionsError as exc:
        if ns.json:
            print(
                json.dumps(
                    {"ok": False, "error": exc.code, "message": exc.message}, ensure_ascii=False
                )
            )
        else:
            print(f"simple-peer-actions: {exc.message}", file=sys.stderr)
        if exc.code in {"not_configured", "invalid_argument", "invalid_secret"}:
            return 3 if exc.code == "not_configured" else 2
        return 6 if exc.retryable else 5
    if ns.json:
        print(json.dumps({"ok": True, **out}, ensure_ascii=False))
        return 0
    if out.get("dry_run"):
        print("dry-run: would append", " | ".join(out["row"]))
    else:
        print("appended", out.get("updated_range") or TAB_TITLE)
    return 0


TAB_TITLE = P.TAB_TITLE


def main(argv: list[str] | None = None) -> int:
    ns = build_parser().parse_args(argv)
    if ns.command == "log":
        return cmd_log(ns)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
