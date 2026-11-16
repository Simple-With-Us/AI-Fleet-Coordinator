"""The persistent idempotency rows that stdio writes share (<state>/<SEAT>/mcp-idem.json).

One store for every writer of a seat on this Mac:  the MCP tools (mcp/tools.py) and the direct paths of the
CLI (notes_cli.py) read and write the same rows under the same file lock, so a request id used through the
CLI is remembered by the tools and the other way round.  A row is `pending` while its write is in flight,
`sent` when it finished (with its result), `unknown` when the outcome could not be told (a timeout), and is
dropped when it provably did nothing.  Rows expire.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from . import live as LIVE

PENDING_FRESH = 30.0  # a pending row younger than this is another process's write in flight


class IdemStore:
    def __init__(self, path: str, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock

    def mutate(self, change: Callable[[dict[str, Any], float], Any]) -> Any:
        with LIVE.locked(self.path):
            data = LIVE.read_json(self.path)
            now = self.clock()
            rows = data.get("rows") if isinstance(data.get("rows"), dict) else {}
            rows = {
                k: r
                for k, r in rows.items()
                if isinstance(r, dict)
                and isinstance(r.get("expires"), (int, float))
                and r["expires"] > now
            }
            result = change(rows, now)
            data["rows"] = rows
            LIVE.write_json(self.path, data, durable=True)
            return result

    def peek(self, key: str) -> dict[str, Any] | None:
        row = (LIVE.read_json(self.path).get("rows") or {}).get(key)
        if (
            isinstance(row, dict)
            and row.get("state") == "sent"
            and float(row.get("expires") or 0) > self.clock()
        ):
            return dict(row)
        return None

    def row(self, key: str) -> dict[str, Any] | None:
        """The row for `key` in any state, or None when there is none (or it expired).  A read:  no lock."""
        row = (LIVE.read_json(self.path).get("rows") or {}).get(key)
        if isinstance(row, dict) and float(row.get("expires") or 0) > self.clock():
            return dict(row)
        return None

    def begin(
        self, key: str, ttl: float, fields: Mapping[str, Any]
    ) -> tuple[str, dict[str, Any] | None]:
        """('send', None) for a new key, ('duplicate', row) for a sent one, ('reconcile', row) for an unknown or
        abandoned one, ('busy', row) while another process's write is in flight."""

        def change(rows: dict[str, Any], now: float) -> tuple[str, dict[str, Any] | None]:
            row = rows.get(key)
            if row and row.get("state") in ("sent", "refused"):
                return "duplicate", dict(row)
            if (
                row
                and row.get("state") == "pending"
                and now - float(row.get("claimed_at") or 0) < PENDING_FRESH
            ):
                return "busy", dict(row)
            if row and row.get("state") in ("unknown", "pending"):
                row["state"] = "pending"
                row["claimed_at"] = now
                return "reconcile", dict(row)
            rows[key] = dict(fields, state="pending", claimed_at=now, ts=now, expires=now + ttl)
            return "send", None

        return self.mutate(change)

    def set(self, key: str, **changes: Any) -> None:
        def change(rows: dict[str, Any], now: float) -> None:
            if key in rows:
                rows[key].update(changes)

        self.mutate(change)

    def drop(self, key: str) -> None:
        self.mutate(lambda rows, now: rows.pop(key, None))
