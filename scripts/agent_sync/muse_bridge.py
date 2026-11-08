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
import math
import os
import re
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

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
    return sorted(
        (row for row in rows if isinstance(row.get("seq"), int) and isinstance(row.get("id"), int)),
        key=lambda row: row["seq"],
    )


def find_source(root: str, cfg: C.Config, seat: str, message_id: int) -> dict[str, Any] | None:
    if not enabled(cfg, seat) or isinstance(message_id, bool) or not isinstance(message_id, int):
        return None
    return next(
        (
            row
            for row in reversed(_inbox_rows(root, seat))
            if row.get("id") == message_id
            and row.get("muse_bridge") is True
            and row.get("wake_worthy") is True
            and row.get("delivered_to") is None
        ),
        None,
    )


def source_blocked(root: str, cfg: C.Config, row: Mapping[str, Any]) -> bool:
    key = L.topic_key(str(row.get("channel") or ""), str(row.get("topic") or ""))
    return W.LoopGuard(
        os.path.join(L.listener_dir(root), "loopguard.json"),
        enabled=cfg.wake_loop_prevention,
    ).blocked(key, time.time())


def source_stale(cfg: C.Config, row: Mapping[str, Any]) -> bool:
    ts = row.get("ts")
    return bool(
        not row.get("owner")
        and (
            row.get("stale") is True
            or not isinstance(ts, (int, float))
            or isinstance(ts, bool)
            or time.time() - float(ts) > cfg.stale_after
        )
    )


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
            out["seats"][seat] = {
                "gap": True,
                "oldest": oldest,
                "next": cursor,
                "items": [],
                "budget": cfg.seats[seat].budget,
            }
            continue
        batch = [
            row
            for row in rows
            if row["seq"] > cursor
            and row.get("muse_bridge") is True
            and row.get("wake_worthy") is True
            and row.get("delivered_to") is None
        ][:MAX_BATCH_PER_SEAT]
        items = []
        for row in batch:
            if (
                row.get("muse_bridge") is not True
                or row.get("wake_worthy") is not True
                or row.get("delivered_to") is not None
            ):
                continue
            items.append(
                {
                    "source_seat": seat,
                    "seq": row["seq"],
                    "id": row["id"],
                    "type": row.get("type"),
                    "channel": row.get("channel"),
                    "topic": row.get("topic"),
                    "content": str(row.get("content") or "")[:MAX_EXPORTED_CONTENT],
                    "sender_id": row.get("sender_id"),
                    "sender": row.get("sender"),
                    "is_bot": row.get("is_bot") is True,
                    "owner": row.get("owner") is True,
                    "ts": row.get("ts"),
                    "stale": row.get("stale") is True,
                    "native": cfg.seats[seat].wake == "http",
                    "blocked": source_blocked(root, cfg, row),
                    "paused": L.paused(root, seat, wakes=True),
                }
            )
        out["seats"][seat] = {
            "gap": False,
            "oldest": oldest,
            "next": batch[-1]["seq"] if batch else max(cursor, rows[-1]["seq"] if rows else cursor),
            "items": items,
            "budget": cfg.seats[seat].budget,
        }
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
            out["seats"][seat] = {
                "gap": True,
                "oldest": oldest,
                "next": cursor,
                "items": [],
                "budget": cfg.seats[seat].budget,
            }
            continue
        batch = [
            row
            for row in rows
            if row["seq"] > cursor
            and row.get("muse_bridge") is True
            and row.get("wake_worthy") is True
            and row.get("delivered_to") is None
        ][:MAX_BATCH_PER_SEAT]
        items = []
        for row in batch:
            if (
                row.get("muse_bridge") is not True
                or row.get("wake_worthy") is not True
                or row.get("delivered_to") is not None
            ):
                continue
            items.append(
                {
                    "source_seat": seat,
                    "seq": row["seq"],
                    "id": row["id"],
                    "type": row.get("type"),
                    "channel": row.get("channel"),
                    "topic": row.get("topic"),
                    "content": str(row.get("content") or "")[:MAX_EXPORTED_CONTENT],
                    "sender_id": row.get("sender_id"),
                    "sender": row.get("sender"),
                    "is_bot": row.get("is_bot") is True,
                    "owner": row.get("owner") is True,
                    "ts": row.get("ts"),
                    "stale": row.get("stale") is True,
                    "native": False,
                    "blocked": source_blocked(root, cfg, row),
                    "paused": L.paused(root, seat, wakes=True),
                }
            )
        out["seats"][seat] = {
            "gap": False,
            "oldest": oldest,
            "next": batch[-1]["seq"] if batch else max(cursor, rows[-1]["seq"] if rows else cursor),
            "items": items,
            "budget": cfg.seats[seat].budget,
        }
    return out


# ---- AFC-649 known-cap mode: an explicit, reversible bridge consult switch ----------------------
# (Kept inside this module, not a new file: every scripts/agent_sync/*.py ships in the fleet CLI
# release, whose file ceiling needs its own policy review for each added file.)
#
# AFC-649 known-cap mode: an explicit, reversible bridge consult switch.
#
# WHY THIS EXISTS.  On 2026-11-06 the owner reported the primary consult runtime at its
# subscription cap - a then-current observation, not a standing statement about later capacity or
# any reset time - and selected an OpenRouter call for this one inbox-routing consult case,
# supplying the 'operator-inbox-router' preset reference on the existing OPERATOR_OPENROUTER_API_KEY
# (the earlier MiniMax PAYG-direct design is withdrawn).  The tool-less request shape and the gates
# below are the integration's own safety properties.  Automatic cap detection is NOT part of this
# module:  the installed Muse 1.4.14 exec stream does not expose the typed quota signal (the only
# verified embedding is MSP turn/completed params.error.quota, and the bridge's headless exec runs
# never see it), so nothing here tries to infer a cap from error text, exit codes, or record
# shapes.  The mode is entered only by explicit operator declaration and exited only by explicit
# removal or by its own stated review time.
#
# EXPIRY IS A HOLD, NOT A REVERT.  When the declared review time passes, consults HOLD:  pending rows
# stay pending, no cursor advances, no runtime is invoked at all, and a review-due signal is logged.
# The mode never silently outlives its stated condition, and expiry never switches to unverified
# model behavior (neither back to the capped primary nor onward on the backup).
#
# ABSENT VS MALFORMED.  A declaration that was never configured (absent/null) selects the primary,
# i.e. exactly the behavior the bridge has today.  A declaration that IS configured but partial or
# malformed fails CLOSED:  consults hold, pending work is preserved, and a misconfigured signal is
# logged - silently falling back to the primary would spend against the cap the operator declared,
# and silently degrading would consume rows nobody decided.

_PRESET_RE = re.compile(r"^@preset/[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

MUSE = "muse"
OPENROUTER = "openrouter"
HOLD = "backup_review_due"

_REQUIRED_KEYS = ("runtime", "model", "reason", "declared_at", "review_by")


@dataclass(frozen=True)
class BackupDeclaration:
    """One operator declaration that the primary consult runtime is at a known cap.

    runtime:    the approved backup runtime id;  only "openrouter" is recognised.
    model:      the OpenRouter model the backup consult calls;  an `@preset/<slug>` reference
                (the reference the owner supplied for this case) or a plain `provider/name`,
                validated against the consult-backup pattern (seat validation stays preset-free:
                a preset's latest version is always served, so a slug pins nothing for a seat).
    expected_models:  the plain `provider/name` ids the declaration expects the preset to serve.
                REQUIRED with an `@preset/` model and checked against the response's served model
                on every answered consult (a mismatch is a charged hold, never silent acceptance):
                a dashboard edit to the preset's model list changes what runs without changing
                any slug, and this set is the only code-side fingerprint of it.  Optional with a
                plain model, where it defaults to that model itself.
    reason:     the operator's own words for why the mode was entered (logged verbatim).
    declared_at / review_by: epoch seconds.  review_by is REQUIRED - the mode must state the
                condition it lives under, and that condition includes when it is re-examined."""

    runtime: str
    model: str
    reason: str
    declared_at: float
    review_by: float
    expected_models: tuple


def parse_declaration(raw: Any) -> BackupDeclaration | None:
    """The declaration from bridge config, or None when absent or malformed (fail-safe)."""
    if not isinstance(raw, Mapping):
        return None
    if any(k not in raw for k in _REQUIRED_KEYS):
        return None
    runtime = raw["runtime"]
    model = raw["model"]
    reason = raw["reason"]
    declared_at = raw["declared_at"]
    review_by = raw["review_by"]
    if runtime != OPENROUTER:
        return None
    if not isinstance(model, str) or not re.fullmatch(C.OPENROUTER_MODEL_PATTERN, model):
        return None
    expected_raw = raw.get("expected_models")
    if _PRESET_RE.fullmatch(model):
        # A preset's model list lives on the dashboard;  without a declared expectation the
        # code has no fingerprint of what the preset serves, so a preset declaration without a
        # valid expected_models is misconfigured and fails closed.
        if not isinstance(expected_raw, list) or not expected_raw:
            return None
    elif expected_raw is None:
        expected_raw = [model]
    if not isinstance(expected_raw, list) or not expected_raw or any(
        not isinstance(entry, str)
        or _PRESET_RE.fullmatch(entry)
        or not re.fullmatch(C.OPENROUTER_MODEL_PATTERN, entry)
        for entry in expected_raw
    ):
        return None
    if not isinstance(reason, str) or not reason.strip():
        return None
    for value in (declared_at, review_by):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            if not math.isfinite(value) or not math.isfinite(float(value)):
                return None
        except OverflowError:
            return None
    if review_by <= declared_at:
        return None
    return BackupDeclaration(
        runtime=OPENROUTER,
        model=model,
        reason=reason.strip(),
        declared_at=float(declared_at),
        review_by=float(review_by),
        expected_models=tuple(expected_raw),
    )


MISCONFIGURED = "backup_misconfigured"
REVIEW_DUE = "backup_review_due"


def select_consult_runtime(
    raw: Any, *, now: float | None = None
) -> tuple[str, BackupDeclaration | None, str | None]:
    """(which runtime a bridge consult uses, the declaration, why consults hold).

    (MUSE, None, None)          - never enabled: no declaration configured.
    (OPENROUTER, decl, None)    - a complete declaration inside its stated window.
    (HOLD, None, MISCONFIGURED) - a declaration IS configured but partial or malformed: fail
                                  closed, preserve pending work, never silently use the primary.
    (HOLD, decl, REVIEW_DUE)    - the declaration's review time has passed: hold every consult,
                                  preserve pending work, signal for re-examination.  No runtime runs."""
    if raw is None:
        return MUSE, None, None
    decl = parse_declaration(raw)
    if decl is None:
        return HOLD, None, MISCONFIGURED
    moment = time.time() if now is None else now
    if moment >= decl.review_by:
        return HOLD, decl, REVIEW_DUE
    return OPENROUTER, decl, None


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
    if kind != "hold" and source_blocked(root, cfg, row):
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
            if (
                prior.get("source_seat") != seat
                or prior.get("id") != message_id
                or prior.get("kind") != kind
            ):
                raise Refused("conflicting decision for source message")
            return {"receipt": key, "state": prior.get("state")}
        data[key] = {
            "source_seat": seat,
            "id": message_id,
            "seq": row["seq"],
            "kind": kind,
            "state": "queued"
            if kind == "native"
            else "chat_selected"
            if kind == "chat"
            else "held",
            "at": time.time(),
        }
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
