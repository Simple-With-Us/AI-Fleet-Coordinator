"""Durable, source-qualified inbox exchange for the default-off Muse bridge.

The server exports bounded copies of its own already classified inbox rows over the existing SSH
transport.  A Mac decision can only authorize the server's configured native routine for that same
seat and message.  The server resolves the row again and owns every credential and delivery gate.
No model output can supply a URL, command, bot key, or destination host to this module.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from typing import Any, Mapping

from . import config as C
from . import live as L
from . import wakes as W

MAX_EXPORTED_CONTENT = 6000
MAX_BATCH_PER_SEAT = 8
MAX_INPUT_BYTES = 4096
DECISIONS_FILE = "muse-bridge-decisions.json"


class Refused(ValueError):
    """A definite refusal for one source message, not an outage.

    ``submit`` raises it when the message itself can no longer be decided (stale, paused, loop
    guard, row changed, conflicting kind).  The exchange answers it as ``{"refused": reason}`` so
    the Mac records it and moves on, while a broken ledger or transport still fails the command.
    """


def native_outcome(view: Mapping[str, Any]) -> str:
    """The receipt outcome for one bridge dispatch, from the wake ledger's merged view.

    ``routine_delivered`` is the hosted Worker's integer count, or absent for any other routine,
    and never a bool.  The ledger's ``surfaced`` flag already applies ``routine_surfaced`` (a
    Worker delivery of at least one, or a 2xx from a routine that is not the Worker), so it alone
    decides a delivery.  A Worker answer of zero reached nobody and is held; anything else that
    finished without surfacing (paused, duplicate) stays uncertain for operator review.
    """
    state = view.get("state")
    if state == "done":
        delivered = view.get("routine_delivered")
        if view.get("surfaced") is True:
            return "done"
        if isinstance(delivered, int) and not isinstance(delivered, bool) and delivered == 0:
            return "held"
        return "uncertain"
    if state in ("held_back", "dropped", "skipped"):
        return "held"
    if state == "failed":
        return "failed"
    return "uncertain"


def enabled(cfg: C.Config, seat: str) -> bool:
    row = cfg.seats.get(seat)
    return bool(cfg.muse_bridge_enabled and row and row.enabled and row.muse_bridge)


def _inbox_rows(root: str, seat: str) -> list[dict[str, Any]]:
    paths = L.SeatPaths(root, seat)
    rows = L.read_jsonl(paths.seat_inbox + ".1") + L.read_jsonl(paths.seat_inbox)
    return sorted((row for row in rows if isinstance(row.get("seq"), int) and
                   isinstance(row.get("id"), int)), key=lambda row: row["seq"])


def find_source(root: str, cfg: C.Config, seat: str, message_id: int) -> dict[str, Any] | None:
    if not enabled(cfg, seat) or isinstance(message_id, bool) or not isinstance(message_id, int):
        return None
    return next((row for row in reversed(_inbox_rows(root, seat))
                 if row.get("id") == message_id and row.get("muse_bridge") is True and
                 row.get("wake_worthy") is True and
                 row.get("delivered_to") is None), None)


def source_blocked(root: str, row: Mapping[str, Any]) -> bool:
    key = L.topic_key(str(row.get("channel") or ""), str(row.get("topic") or ""))
    return W.LoopGuard(os.path.join(L.listener_dir(root), "loopguard.json")).blocked(key, time.time())


def source_stale(cfg: C.Config, row: Mapping[str, Any]) -> bool:
    ts = row.get("ts")
    return bool(not row.get("owner") and (row.get("stale") is True or
                not isinstance(ts, (int, float)) or isinstance(ts, bool) or
                time.time() - float(ts) > cfg.stale_after))


def _export_gap(rows: list[dict[str, Any]], cursor: int) -> bool:
    """True when the rows past the cursor are not contiguous from it (a lost rotation).

    The combined rotated-plus-current inbox must read as one unbroken sequence past
    what was already exported; anything else means rows vanished between rotations.
    Duplicate seqs are tolerated, and history at or below the cursor is ignored.
    """
    if not cursor:
        return False
    fresh = [row["seq"] for row in rows if row["seq"] > cursor]
    if not fresh:
        return False
    if fresh[0] > cursor + 1:
        return True
    return any(b > a + 1 for a, b in zip(fresh, fresh[1:]))


def export(root: str, cfg: C.Config, cursors: Mapping[str, Any]) -> dict[str, Any]:
    """Return at most eight rows per enabled seat; a lost rotation is an explicit gap."""
    if cfg.instance != "server" or not cfg.muse_bridge_enabled:
        raise ValueError("server Muse bridge is disabled")
    out: dict[str, Any] = {"v": 1, "seats": {}}
    for seat in sorted(cfg.seats):
        if not enabled(cfg, seat):
            continue
        cursor = cursors.get(seat, 0)
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise ValueError("invalid source cursor")
        rows = _inbox_rows(root, seat)
        oldest = rows[0]["seq"] if rows else None
        if oldest is not None and _export_gap(rows, cursor):
            out["seats"][seat] = {"gap": True, "oldest": oldest, "next": cursor, "items": [],
                                  "budget": cfg.seats[seat].budget}
            continue
        batch = [row for row in rows if row["seq"] > cursor and row.get("muse_bridge") is True and
                 row.get("wake_worthy") is True and row.get("delivered_to") is None][:MAX_BATCH_PER_SEAT]
        items = []
        for row in batch:
            if row.get("muse_bridge") is not True or row.get("wake_worthy") is not True or \
                    row.get("delivered_to") is not None:
                continue
            items.append({"source_seat": seat, "seq": row["seq"], "id": row["id"],
                          "type": row.get("type"), "channel": row.get("channel"),
                          "topic": row.get("topic"), "content": str(row.get("content") or "")[:MAX_EXPORTED_CONTENT],
                          "sender_id": row.get("sender_id"), "sender": row.get("sender"),
                          "is_bot": row.get("is_bot") is True, "owner": row.get("owner") is True,
                          "ts": row.get("ts"), "stale": row.get("stale") is True,
                          "native": cfg.seats[seat].wake == "http", "blocked": source_blocked(root, row),
                          "paused": L.paused(root, seat, wakes=True)})
        out["seats"][seat] = {"gap": False, "oldest": oldest,
                              "next": batch[-1]["seq"] if batch else max(cursor, rows[-1]["seq"] if rows else cursor),
                              "items": items,
                              "budget": cfg.seats[seat].budget}
    return out


def export_local(root: str, cfg: C.Config, cursors: Mapping[str, Any]) -> dict[str, Any]:
    """Use the same row envelope for bridge-enabled seats owned by the Mac listener."""
    if cfg.instance != "mac" or not cfg.muse_bridge_enabled:
        raise ValueError("Mac Muse bridge is disabled")
    out: dict[str, Any] = {"v": 1, "seats": {}}
    for seat in sorted(cfg.seats):
        if not enabled(cfg, seat):
            continue
        cursor = cursors.get(seat, 0)
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise ValueError("invalid source cursor")
        rows = _inbox_rows(root, seat)
        oldest = rows[0]["seq"] if rows else None
        if oldest is not None and _export_gap(rows, cursor):
            out["seats"][seat] = {"gap": True, "oldest": oldest, "next": cursor, "items": [],
                                  "budget": cfg.seats[seat].budget}
            continue
        batch = [row for row in rows if row["seq"] > cursor and row.get("muse_bridge") is True and
                 row.get("wake_worthy") is True and row.get("delivered_to") is None][:MAX_BATCH_PER_SEAT]
        items = []
        for row in batch:
            if row.get("muse_bridge") is not True or row.get("wake_worthy") is not True or \
                    row.get("delivered_to") is not None:
                continue
            items.append({"source_seat": seat, "seq": row["seq"], "id": row["id"],
                          "type": row.get("type"), "channel": row.get("channel"),
                          "topic": row.get("topic"), "content": str(row.get("content") or "")[:MAX_EXPORTED_CONTENT],
                          "sender_id": row.get("sender_id"), "sender": row.get("sender"),
                          "is_bot": row.get("is_bot") is True, "owner": row.get("owner") is True,
                          "ts": row.get("ts"), "stale": row.get("stale") is True,
                          "native": False, "blocked": source_blocked(root, row),
                          "paused": L.paused(root, seat, wakes=True)})
        out["seats"][seat] = {"gap": False, "oldest": oldest,
                              "next": batch[-1]["seq"] if batch else max(cursor, rows[-1]["seq"] if rows else cursor),
                              "items": items,
                              "budget": cfg.seats[seat].budget}
    return out


def _store_path(root: str) -> str:
    parent = L.listener_dir(root)
    L.private_dir(parent)
    return os.path.join(parent, DECISIONS_FILE)


def _decision_key(seat: str, message_id: int) -> str:
    return hashlib.sha256((seat + "\0" + str(message_id)).encode("ascii")).hexdigest()


def native_target_id(seat: str) -> str:
    """A fixed opaque selection ID for the source seat's already configured routine."""
    return hashlib.sha256(("native-routine\0" + seat).encode("ascii")).hexdigest()


def _read_decisions(path: str) -> dict[str, Any]:
    data = L.read_json(path)
    if not isinstance(data, dict) or (os.path.exists(path) and not data):
        raise ValueError("bridge decision ledger is unreadable")
    return data


def submit(root: str, cfg: C.Config, request: Mapping[str, Any]) -> dict[str, Any]:
    """Idempotently record a Mac decision after rechecking the server's own inbox."""
    if cfg.instance != "server" or not cfg.muse_bridge_enabled:
        raise ValueError("server Muse bridge is disabled")
    seat, message_id, kind = request.get("source_seat"), request.get("id"), request.get("kind")
    if not isinstance(seat, str) or not enabled(cfg, seat):
        raise Refused("source seat is not bridge enabled")
    row = find_source(root, cfg, seat, message_id)
    if row is None or row.get("seq") != request.get("seq"):
        raise Refused("source inbox row is missing or changed")
    if kind not in ("native", "chat", "hold"):
        raise Refused("invalid bridge decision")
    if kind == "native" and cfg.seats[seat].wake != "http":
        raise Refused("source seat has no configured native routine")
    if kind != "hold" and source_blocked(root, row):
        raise Refused("source loop guard blocks delivery")
    if kind != "hold" and source_stale(cfg, row):
        raise Refused("source message became stale")
    if kind != "hold" and L.paused(root, seat, wakes=True):
        raise Refused("source seat wakes are paused")
    key, path = _decision_key(seat, message_id), _store_path(root)
    with L.locked(path):
        data = _read_decisions(path)
        prior = data.get(key)
        if prior is not None:
            if prior.get("source_seat") != seat or prior.get("id") != message_id or prior.get("kind") != kind:
                raise Refused("conflicting decision for source message")
            return {"receipt": key, "state": prior.get("state")}
        data[key] = {"source_seat": seat, "id": message_id, "seq": row["seq"], "kind": kind,
                     "state": "queued" if kind == "native" else "chat_selected" if kind == "chat" else "held",
                     "at": time.time()}
        L.write_json(path, data)
    return {"receipt": key, "state": data[key]["state"]}


def claim_native(root: str) -> dict[str, Any] | None:
    """Claim before delivery.  An interrupted claim stays uncertain and is never sent twice."""
    path = _store_path(root)
    with L.locked(path):
        data = _read_decisions(path)
        for key, row in data.items():
            if row.get("kind") == "native" and row.get("state") == "queued":
                row["state"] = "dispatching"
                row["claimed_at"] = time.time()
                L.write_json(path, data)
                return dict(row, receipt=key)
    return None


def finish_native(root: str, receipt: str, state: str) -> None:
    if state not in ("done", "failed", "held", "uncertain"):
        raise ValueError("invalid native decision outcome")
    path = _store_path(root)
    with L.locked(path):
        data = _read_decisions(path)
        row = data.get(receipt)
        if not isinstance(row, dict) or row.get("state") != "dispatching":
            raise ValueError("decision is not dispatching")
        row["state"] = state
        row["finished_at"] = time.time()
        L.write_json(path, data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Private Agent-Sync Muse bridge exchange")
    parser.add_argument("command", choices=("export", "submit"))
    args = parser.parse_args(argv)
    root = L.state_root(os.environ)
    cfg = C.load(root)
    if cfg.errors or cfg.instance != "server":
        raise SystemExit("server bridge configuration is unavailable")
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise SystemExit("bridge input is too large")
    try:
        request = json.loads(raw)
        if not isinstance(request, dict):
            raise ValueError("input must be an object")
        if args.command == "export":
            result = export(root, cfg, request.get("cursors", {}))
        else:
            try:
                result = submit(root, cfg, request)
            except Refused as exc:
                # A definite answer for this one message:  exit 0 so the Mac can tell it from an
                # outage, record it, and move past it instead of retrying the same item forever.
                result = {"refused": str(exc)}
    except (ValueError, TypeError) as exc:
        raise SystemExit(str(exc)) from None
    sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
