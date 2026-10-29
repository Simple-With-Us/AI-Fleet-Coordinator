"""The listener's per-seat records as plain views (AFC-330, part B):  status, the seat inbox, the owner
queue, the wake ledger, held-back wakes, leases and live inboxes, and chat nudges.

Three readers share these functions:  `simple-chat listener ...` (the CLI), the stdio MCP's listener_*
tools, and the server listener's signed read API (listener_api.py), which the hosted Worker calls.
Every view is for ONE seat, the caller's own:  callers pass the seat their identity resolved (the CLI
and stdio) or the seat a request was signed for (the read API), never a seat taken from arguments.

The files stay the daemon's (live.py, heldback.py, wakes.py, chat_nudges.py).  Reads never move a
cursor;  the few acts (`ack_inbox`, `mark_surfaced`, `clear_held`, `drain`) take the same locks the
daemon takes and only ever move forward.  A seat directory or record file open to group or other
users (mode bits 077) or owned by someone else is refused rather than read:  the listener writes
700 and 600, so anything else was not written by it.

Views carry Zulip-authored strings (bodies, topic names, sender names) as they are stored;  each
surface fences them as untrusted before showing them.  Python 3.11+, standard library only.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping
from typing import Any

from . import heldback as HELD
from . import live as L
from . import wakes as W
from .zulip import UsageError

SEAT_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,31}$")
RECORDS = ("status", "inbox", "owner-queue", "wakes", "held-back")
_LEASE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _check_private(path: str, what: str) -> None:
    """Refuse a path that is open to group or other users, or not owned by this user."""
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise UsageError("cannot read the %s (%s)" % (what, exc.strerror)) from None
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise UsageError("the %s is owned by another user; refusing to read it" % what)
    if info.st_mode & 0o077:
        raise UsageError(
            "the %s is readable by other users (mode %o); the listener writes %s, so refusing to read it "
            "(chmod %s it)"
            % (
                what,
                info.st_mode & 0o777,
                "700" if os.path.isdir(path) else "600",
                "700" if os.path.isdir(path) else "600",
            )
        )


def seat_paths(root: str, seat: str) -> L.SeatPaths:
    """The seat's paths, after the seat name and the directory's mode are checked."""
    if not isinstance(seat, str) or not SEAT_RE.fullmatch(seat):
        raise UsageError("not a seat name")
    paths = L.SeatPaths(root, seat)
    _check_private(paths.seat_dir, "%s state directory" % seat)
    return paths


def _private(path: str, what: str) -> str:
    _check_private(path, what)
    return path


def _rows(path: str, what: str) -> list[dict[str, Any]]:
    return L.read_jsonl(_private(path, what))


# --------------------------------------------------------------------------------------------
# Seat inbox and owner queue
# --------------------------------------------------------------------------------------------


def inbox_cursor(paths: L.SeatPaths) -> int:
    value = L.read_json(_private(paths.local_cursor, "seat-inbox cursor")).get("seq")
    return value if _int(value) is not None else 0


def inbox_item(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "seq": _int(row.get("seq")),
        "id": _int(row.get("id")),
        "kind": row.get("kind", "message"),
        "class": row.get("class"),
        "dm": row.get("type") == "private",
        "channel": row.get("channel"),
        "topic": row.get("topic"),
        "sender_id": _int(row.get("sender_id")),
        "sender": row.get("sender"),
        "is_bot": row.get("is_bot"),
        "owner": bool(row.get("owner")),
        "time": row.get("time"),
        "body": row.get("content"),
    }


def inbox_view(
    root: str,
    seat: str,
    *,
    since_seq: int | None = None,
    limit: int = 20,
    include_delivered: bool = False,
) -> dict[str, Any]:
    """Seat-inbox items after the acknowledged cursor (or after since_seq), oldest first.  Never
    moves the cursor.  `pending` counts every unacknowledged undelivered item, `next_seq` is the last
    seq shown (pass it as since_seq, or ack it)."""
    paths = seat_paths(root, seat)
    cursor = inbox_cursor(paths)
    floor = cursor if since_seq is None else since_seq
    rows = [r for r in _rows(paths.seat_inbox, "seat inbox") if _int(r.get("seq")) is not None]
    pending = [r for r in rows if r["seq"] > cursor and not r.get("delivered_to")]
    items = [
        r for r in rows if r["seq"] > floor and (include_delivered or not r.get("delivered_to"))
    ]
    shown = items[: max(0, limit)]
    return {
        "seat": seat,
        "cursor": cursor,
        "pending": len(pending),
        "items": [inbox_item(r) for r in shown],
        "next_seq": shown[-1]["seq"] if shown else (floor or None),
    }


def ack_inbox(root: str, seat: str, upto: int) -> int:
    """Move the seat-inbox cursor forward to `upto` (never back, never past the newest item)."""
    paths = seat_paths(root, seat)
    newest = max(
        (r["seq"] for r in _rows(paths.seat_inbox, "seat inbox") if _int(r.get("seq")) is not None),
        default=0,
    )
    target = min(int(upto), newest)

    def mutate(data: dict[str, Any]) -> int:
        current = data.get("seq") if _int(data.get("seq")) is not None else 0
        data["seq"] = max(current, target)
        return data["seq"]

    return L.update_json(_private(paths.local_cursor, "seat-inbox cursor"), mutate)


def owner_item(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "seq": _int(row.get("seq")),
        "kind": row.get("kind"),
        "title": row.get("title"),
        "body": row.get("text"),
        "risk": row.get("risk"),
        "note": row.get("note"),
        "owner": bool(row.get("owner")),
        "ts": row.get("ts"),
        "trigger_ids": [i for i in row.get("trigger_ids") or [] if _int(i) is not None]
        if isinstance(row.get("trigger_ids"), list)
        else [],
        "sender_id": _int(row.get("sender_id")),
    }


def owner_queue_view(
    root: str, seat: str, *, all_notes: bool = False, limit: int = 20
) -> dict[str, Any]:
    paths = seat_paths(root, seat)
    surfaced = L.read_json(_private(paths.owner_queue_meta, "owner-queue mark")).get("surfaced")
    surfaced = surfaced if _int(surfaced) is not None else 0
    rows = [r for r in _rows(paths.owner_queue, "owner queue") if _int(r.get("seq")) is not None]
    pending = [r for r in rows if r["seq"] > surfaced]
    shown = (rows if all_notes else pending)[-max(0, limit) :] if limit else []
    return {
        "seat": seat,
        "surfaced": surfaced,
        "pending": len(pending),
        "items": [owner_item(r) for r in shown],
    }


def mark_surfaced(root: str, seat: str, upto: int) -> int:
    paths = seat_paths(root, seat)
    newest = max(
        (
            r["seq"]
            for r in _rows(paths.owner_queue, "owner queue")
            if _int(r.get("seq")) is not None
        ),
        default=0,
    )
    target = min(int(upto), newest)

    def mutate(data: dict[str, Any]) -> int:
        current = data.get("surfaced") if _int(data.get("surfaced")) is not None else 0
        data["surfaced"] = max(current, target)
        return data["surfaced"]

    return L.update_json(_private(paths.owner_queue_meta, "owner-queue mark"), mutate)


# --------------------------------------------------------------------------------------------
# Wakes and held-back wakes
# --------------------------------------------------------------------------------------------


def wake_item(view: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "wake_id": view.get("wake_id"),
        "state": view.get("state"),
        "ts": view.get("ts"),
        "dm": view.get("type") == "private",
        "channel": view.get("channel"),
        "topic": view.get("topic"),
        "trigger_ids": [i for i in view.get("trigger_ids") or [] if _int(i) is not None],
        "owner": bool(view.get("owner")),
        "action": view.get("action"),
        "reason": view.get("reason"),
        "catch_up": bool(view.get("catch_up")),
        "by": view.get("by"),
    }


def wakes_view(
    root: str,
    seat: str,
    *,
    since_hours: float | None = 24,
    state: str | None = None,
    limit: int = 30,
    now: float | None = None,
) -> dict[str, Any]:
    paths = seat_paths(root, seat)
    now = time.time() if now is None else now
    views = W.Ledger(_private(paths.wakes, "wake ledger")).wakes()
    since = now - float(since_hours) * 3600 if since_hours else 0
    rows = sorted(
        (
            v
            for v in views.values()
            if float(v.get("ts") or 0) >= since and (state is None or v.get("state") == state)
        ),
        key=lambda v: float(v.get("ts") or 0),
    )
    by_state: dict[str, int] = {}
    for view in rows:
        by_state[str(view.get("state"))] = by_state.get(str(view.get("state")), 0) + 1
    return {
        "seat": seat,
        "total": len(rows),
        "by_state": dict(sorted(by_state.items())),
        "items": [wake_item(v) for v in (rows[-limit:] if limit else [])],
    }


def held_back_view(
    root: str, seat: str, *, limit: int = 50, now: float | None = None
) -> dict[str, Any]:
    paths = seat_paths(root, seat)
    _check_private(HELD.path_for(paths), "held-back file")
    data = HELD.load(paths)
    entries = data["overflow"] + data["records"]
    summary = HELD.summary(paths, time.time() if now is None else now)
    return {
        "seat": seat,
        "count": len(entries),
        "lost": int(summary.get("evicted") or 0),
        "reasons": summary.get("reasons") or {},
        "items": [HELD.item(e) for e in entries[: max(0, limit)]],
    }


def clear_held(root: str, seat: str, message_id: int) -> int:
    """Clear one held-back entry (either tier) by its message id, as a delivered wake would."""
    paths = seat_paths(root, seat)
    _check_private(HELD.path_for(paths), "held-back file")
    return len(HELD.surface(paths, message_ids=[int(message_id)]))


# --------------------------------------------------------------------------------------------
# Leases, live inboxes, chat nudges
# --------------------------------------------------------------------------------------------


def leases_view(root: str, seat: str, *, now: float | None = None) -> dict[str, Any]:
    paths = seat_paths(root, seat)
    _check_private(paths.leases, "leases directory")
    now = time.time() if now is None else now
    items = []
    for lease_id in L.list_lease_ids(paths):
        lease = L.load_lease(paths, lease_id)
        topics = [
            {"key": key, "expires": entry.get("expires")}
            for key, entry in L.lease_topics(lease, now).items()
        ]
        items.append(
            {
                "lease_id": lease_id,
                "session": lease.get("session_id") or lease.get("session"),
                "pid": _int(lease.get("pid")),
                "topics": topics,
                "pending": len(L.pending_items(paths, lease_id)),
                "watcher_alive": L.watcher_alive(paths, lease_id),
            }
        )
    return {"seat": seat, "items": items}


def _lease(paths: L.SeatPaths, prefix: str) -> str:
    if not _LEASE_RE.fullmatch(prefix or ""):
        raise UsageError("not a lease id")
    found = L.find_lease(paths, prefix)
    if not found:
        raise UsageError("no lease of this seat starts with %r" % prefix)
    _check_private(paths.live_dir(found), "live inbox directory")
    return found


def live_view(root: str, seat: str, lease_prefix: str, *, limit: int = 50) -> dict[str, Any]:
    paths = seat_paths(root, seat)
    lease_id = _lease(paths, lease_prefix)
    pending = L.pending_items(paths, lease_id)
    return {
        "seat": seat,
        "lease_id": lease_id,
        "pending": len(pending),
        "items": [inbox_item(i) for i in pending[: max(0, limit)]],
    }


def drain(root: str, seat: str, lease_prefix: str) -> dict[str, Any]:
    """Take every undelivered item of a live inbox (marked delivered under the daemon's lock, so it
    reaches this caller or the session, never both) and return it."""
    paths = seat_paths(root, seat)
    lease_id = _lease(paths, lease_prefix)
    items = L.release(paths, lease_id, field="drained")
    return {"seat": seat, "lease_id": lease_id, "items": [inbox_item(i) for i in items]}


def nudges_view(root: str, seat: str) -> dict[str, Any]:
    from .chat_nudges import ChatNudges

    paths = seat_paths(root, seat)
    path = os.path.join(paths.seat_dir, "chat-nudges.json")
    _check_private(path, "chat nudges file")
    if not os.path.exists(path):
        return {"seat": seat, "bindings": [], "items": []}
    try:
        data = ChatNudges(root, seat, recover=False).snapshot()
    except ValueError as exc:
        raise UsageError("the chat nudges file cannot be read (%s)" % exc) from None
    binding_rows = list((data.get("bindings") or {}).values())
    items = [
        {
            "id": i.get("id"),
            "status": i.get("status"),
            "message_id": i.get("message_id"),
            "channel": (i.get("binding") or {}).get("channel"),
            "topic": (i.get("binding") or {}).get("topic"),
            "hold_reason": i.get("hold_reason"),
            "updated_at": i.get("updated_at"),
        }
        for i in data.get("items") or []
        if isinstance(i, Mapping)
    ]
    return {
        "seat": seat,
        "bindings": [
            {"channel": b.get("channel"), "topic": b.get("topic"), "platform": b.get("platform")}
            for b in binding_rows
            if isinstance(b, Mapping)
        ],
        "items": items,
    }


# --------------------------------------------------------------------------------------------
# Status
# --------------------------------------------------------------------------------------------


def status_view(root: str, seat: str, *, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    paths = seat_paths(root, seat)
    inbox = inbox_view(root, seat, limit=0)
    owner = owner_queue_view(root, seat, limit=0)
    wakes = wakes_view(root, seat, since_hours=24, limit=0, now=now)
    held = held_back_view(root, seat, limit=0, now=now)
    leases = len(L.list_lease_ids(paths)) if os.path.isdir(paths.leases) else 0
    nudges = nudges_view(root, seat)
    return {
        "seat": seat,
        "inbox_pending": inbox["pending"],
        "inbox_cursor": inbox["cursor"],
        "owner_queue_pending": owner["pending"],
        "held_back": held["count"],
        "held_back_lost": held["lost"],
        "wakes_24h": wakes["total"],
        "wakes_by_state": wakes["by_state"],
        "leases": leases,
        "nudges_pending": sum(
            1 for i in nudges["items"] if i.get("status") in ("pending", "unknown", "dispatching")
        ),
        "paused": L.paused(root, seat),
        "paused_wakes": L.paused(root, seat, wakes=True),
    }


def view(
    root: str, seat: str, record: str, params: Mapping[str, Any], *, now: float | None = None
) -> dict[str, Any]:
    """One read-only record by name (the read API's and the stdio tools' dispatch)."""
    if record == "status":
        return status_view(root, seat, now=now)
    if record == "inbox":
        return inbox_view(
            root, seat, since_seq=params.get("since_seq"), limit=int(params.get("limit", 20))
        )
    if record == "owner-queue":
        return owner_queue_view(
            root, seat, all_notes=bool(params.get("all")), limit=int(params.get("limit", 20))
        )
    if record == "wakes":
        return wakes_view(
            root,
            seat,
            since_hours=params.get("since_hours", 24),
            state=params.get("state"),
            limit=int(params.get("limit", 30)),
            now=now,
        )
    if record == "held-back":
        return held_back_view(root, seat, limit=int(params.get("limit", 50)), now=now)
    raise UsageError("unknown listener record")
