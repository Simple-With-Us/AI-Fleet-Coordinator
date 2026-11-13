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
        "--hosted",
        action="store_true",
        help="use this bot's own hosted Fleet Tools bearer; never local Google credentials",
    )
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
    resolved = I.resolve_seat(os.environ, flag=ns.acting_seat or ns.as_seat, default=None)
    if resolved.problem:
        raise P.PeerActionsError("invalid_argument", resolved.problem)
    if resolved.seat:
        return resolved.seat
    raise P.PeerActionsError(
        "invalid_argument", "name the acting seat with --acting or set AGENT_LAUNCH_SEAT"
    )


def _hosted_log(entry: P.LogEntry, *, dry_run: bool) -> dict:
    # Reuse the fixed endpoint, secret scan, bounded parser, no-redirect and
    # unknown-outcome handling of the existing supported hosted transport.
    from .cli import RemoteError, _remote_call, _remote_write_identity

    try:
        who = _remote_write_identity(os.environ)
        if I.canonical_seat(who) != entry.acting_seat:
            raise P.PeerActionsError(
                "invalid_argument",
                "the hosted bearer belongs to a different acting seat; nothing was sent",
            )
        result = _remote_call(
            os.environ,
            "peer_actions_log",
            {
                "identity": who,
                "requesting_seat": entry.requesting_seat,
                "request": entry.request_source,
                "action_taken": entry.action_taken,
                "result": entry.result,
                "dry_run": dry_run,
            },
            write=not dry_run,
        )
    except RemoteError as exc:
        # No retry and no direct fallback, including after an ambiguous write.
        raise P.PeerActionsError("hosted_failed", str(exc)) from None
    out = result.get("structuredContent")
    if (
        not isinstance(out, dict)
        or out.get("dry_run") is not dry_run
        or not isinstance(out.get("row"), list)
        or len(out["row"]) != 6
        or out["row"][1:] != entry.row()[1:]
    ):
        raise P.PeerActionsError(
            "hosted_failed",
            "the hosted logger returned an unexpected receipt; do not repeat a possible write",
        )
    return out


def cmd_log(ns: argparse.Namespace) -> int:
    try:
        entry = P.build_entry(
            acting_seat=_acting_seat(ns),
            requesting_seat=ns.requesting_seat,
            request_source=ns.request_source,
            action_taken=ns.action_taken,
            result=ns.result,
        )
        hosted = bool(ns.hosted) or os.environ.get("PEER_ACTIONS_REMOTE", "").strip() == "1"
        out = (
            _hosted_log(entry, dry_run=bool(ns.dry_run))
            if hosted
            else P.log_row(entry, dry_run=bool(ns.dry_run))
        )
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
