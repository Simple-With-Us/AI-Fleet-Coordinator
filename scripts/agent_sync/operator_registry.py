"""Durable Operator session registry and export outbox (AFC-412 PR1).

Identity is (platform, verified_host, profile_scope, native_session_id) - never the seat, never
the label, never the title.  A resume on the same tuple returns the same immutable record_id and
its live label;  a fork or a new native session id is a new registration.

Labels are per-platform letters A-Z with one namespace across all hosts.  RECYCLE is on (owner
2026-10-20, option B):  allocation takes the lowest letter with no active assignment, wrapping
after Z back to A, and a reused letter gets the next generation.  Cross-seat references to a
generation > 1 letter must carry the generation:  "MM A (gen 2)".

Seat is checked separately from identity:  a resolve under an unexpected seat returns the
existing record with outcome "seat_mismatch", writes an events row, and changes nothing.  No
rebind, and no token or wake authority is derived from a match.

Standard library only, Python 3.11+.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any, Mapping

from .operator_registry_schema import DDL, SCHEMA_VERSION

LETTERS = tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
MUTABLE_ATTRS = ("title", "project", "thread_link", "apple_note_link", "state", "wake_capability")
IDENTITY_FIELDS = ("platform", "verified_host", "profile_scope", "native_session_id")

SESSIONS_HEADERS = (
    "Record ID", "Label", "Seat", "Platform", "Host", "Native Session ID", "Title", "Project",
    "Thread Link", "Apple Note Link", "State", "Wake Capability", "Registered At", "Last Seen At",
    "Closed At", "Source",
)
EVENTS_HEADERS = (
    "Event ID", "Recorded At", "Session Record ID", "Label", "Event", "Actor", "Outcome",
    "Source", "Details",
)


class LabelExhausted(RuntimeError):
    """Every letter A-Z on this platform has an active assignment."""


class UnknownRecord(KeyError):
    """No session with this record_id."""


_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid(now_ms: int | None = None) -> str:
    """A 26-character ULID (48-bit ms time + 80-bit random, Crockford base32).  Minted once per
    record or event and never reused."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    now_ms &= (1 << 48) - 1
    rand = secrets.randbits(80)
    out = []
    for shift in range(45, -1, -5):
        out.append(_CROCKFORD[(now_ms >> shift) & 31])
    for shift in range(75, -1, -5):
        out.append(_CROCKFORD[(rand >> shift) & 31])
    return "".join(out)


def _utcnow() -> str:
    """Canonical UTC instant.  Display formatting (12-hour America/Chicago) is the exporter's job."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def display_title(label: str, title: str) -> str:
    """In-platform display form (owner 2026-10-20):  "H: Title"."""
    return f"{label}: {title}" if label else title


def cross_seat_ref(seat: str, letter: str, generation: int) -> str:
    """Cross-seat reference form.  Generation > 1 (a recycled letter) must be disambiguated."""
    base = f"{seat} {letter}"
    return f"{base} (gen {generation})" if generation > 1 else base


def _session_cells(row: sqlite3.Row, label: str) -> list[str]:
    return [
        row["record_id"], label, row["seat"], row["platform"], row["verified_host"],
        row["native_session_id"], row["title"], row["project"], row["thread_link"],
        row["apple_note_link"], row["state"], row["wake_capability"], row["registered_at"],
        row["last_seen_at"], row["closed_at"] or "", row["source"],
    ]


class SessionRegistry:
    """The one writer of the registry database."""

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(DDL)
        with self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (SCHEMA_VERSION, _utcnow()),
            )

    def close_db(self) -> None:
        self._conn.close()

    # ---- reads -------------------------------------------------

    def _get(self, record_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM sessions WHERE record_id = ?", (record_id,)
        ).fetchone()
        if row is None:
            raise UnknownRecord(record_id)
        return row

    def _get_by_identity(
        self, platform: str, verified_host: str, profile_scope: str, native_session_id: str
    ) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM sessions WHERE platform = ? AND verified_host = ?"
            " AND profile_scope = ? AND native_session_id = ?",
            (platform, verified_host, profile_scope, native_session_id),
        ).fetchone()

    def _active_alias(self, conn: sqlite3.Connection, record_id: str) -> tuple[str, int]:
        row = conn.execute(
            "SELECT letter, generation FROM alias_assignments"
            " WHERE record_id = ? AND retired_at IS NULL"
            " ORDER BY assigned_at DESC LIMIT 1",
            (record_id,),
        ).fetchone()
        return (row["letter"], row["generation"]) if row else ("", 0)

    # ---- writes (each one transaction:  state + events + outbox) ----------------

    def _allocate(self, conn: sqlite3.Connection, platform: str, record_id: str, now: str) -> tuple[str, int]:
        """Lowest letter with no active assignment, wrapping after Z to A (RECYCLE on).  A reused
        letter gets the next generation.  Raises LabelExhausted when all 26 are live."""
        for letter in LETTERS:
            live = conn.execute(
                "SELECT 1 FROM alias_assignments"
                " WHERE platform = ? AND letter = ? AND retired_at IS NULL",
                (platform, letter),
            ).fetchone()
            if live:
                continue
            row = conn.execute(
                "SELECT MAX(generation) AS g FROM alias_assignments WHERE platform = ? AND letter = ?",
                (platform, letter),
            ).fetchone()
            generation = (row["g"] or 0) + 1
            conn.execute(
                "INSERT INTO alias_assignments(platform, letter, generation, record_id, assigned_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (platform, letter, generation, record_id, now),
            )
            return letter, generation
        raise LabelExhausted(f"all {len(LETTERS)} letters are active on platform {platform!r}")

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        record_id: str,
        label: str,
        event: str,
        actor: str,
        outcome: str,
        source: str,
        details: str,
        now: str,
    ) -> str:
        event_id = _ulid()
        conn.execute(
            "INSERT INTO events(event_id, recorded_at, session_record_id, label, event, actor,"
            " outcome, source, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (event_id, now, record_id, label, event, actor, outcome, source, details),
        )
        cells = [event_id, now, record_id, label, event, actor, outcome, source, details]
        conn.execute(
            "INSERT INTO export_outbox(outbox_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (_ulid(), "event_append", json.dumps({"cells": cells}), now),
        )
        return event_id

    def _enqueue_session_upsert(self, conn: sqlite3.Connection, record_id: str, now: str) -> None:
        row = conn.execute("SELECT * FROM sessions WHERE record_id = ?", (record_id,)).fetchone()
        letter, _ = self._active_alias(conn, record_id)
        conn.execute(
            "INSERT INTO export_outbox(outbox_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
            (_ulid(), "session_upsert", json.dumps({"cells": _session_cells(row, letter)}), now),
        )

    def resolve_or_register(self, identity: Mapping[str, Any]) -> dict[str, Any]:
        """Resume an existing registration or create a new one, atomically with its label,
        events row and outbox rows.  Never rebinds identity on a seat change."""
        required = IDENTITY_FIELDS + ("seat", "source")
        missing = [k for k in required if not identity.get(k)]
        if missing:
            raise ValueError(f"missing identity fields: {', '.join(missing)}")
        now = _utcnow()
        with self._conn as conn:
            row = self._get_by_identity(*(identity[k] for k in IDENTITY_FIELDS))
            if row is not None:
                record_id = row["record_id"]
                letter, generation = self._active_alias(conn, record_id)
                if row["seat"] != identity["seat"]:
                    self._insert_event(
                        conn, record_id, letter, "resolve", identity["seat"], "seat_mismatch",
                        identity["source"], f"stored seat is {row['seat']}", now,
                    )
                    return {"record_id": record_id, "label": letter, "generation": generation,
                            "created": False, "outcome": "seat_mismatch"}
                outcome = "resumed_closed" if row["closed_at"] else "resumed"
                self._insert_event(
                    conn, record_id, letter, "resume", identity["seat"], outcome,
                    identity["source"], "", now,
                )
                return {"record_id": record_id, "label": letter, "generation": generation,
                        "created": False, "outcome": outcome}
            record_id = _ulid()
            conn.execute(
                "INSERT INTO sessions(record_id, seat, platform, verified_host, profile_scope,"
                " native_session_id, title, project, thread_link, apple_note_link,"
                " wake_capability, registered_at, last_seen_at, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record_id, identity["seat"], identity["platform"], identity["verified_host"],
                    identity["profile_scope"], identity["native_session_id"],
                    identity.get("title", ""), identity.get("project", ""),
                    identity.get("thread_link", ""), identity.get("apple_note_link", ""),
                    identity.get("wake_capability", "unknown"), now, now, identity["source"],
                ),
            )
            letter, generation = self._allocate(conn, identity["platform"], record_id, now)
            self._insert_event(
                conn, record_id, letter, "register", identity["seat"], "registered",
                identity["source"], "", now,
            )
            self._enqueue_session_upsert(conn, record_id, now)
            return {"record_id": record_id, "label": letter, "generation": generation,
                    "created": True, "outcome": "registered"}

    def mark_seen(self, record_id: str, at: str | None = None) -> None:
        at = at or _utcnow()
        with self._conn as conn:
            self._get(record_id)
            conn.execute("UPDATE sessions SET last_seen_at = ? WHERE record_id = ?", (at, record_id))
            self._enqueue_session_upsert(conn, record_id, at)

    def update_attrs(self, record_id: str, /, **attrs: str) -> None:
        bad = [k for k in attrs if k not in MUTABLE_ATTRS]
        if bad:
            raise ValueError(f"not mutable: {', '.join(bad)}")
        if not attrs:
            return
        now = _utcnow()
        with self._conn as conn:
            self._get(record_id)
            sets = ", ".join(f"{k} = ?" for k in attrs)
            conn.execute(
                f"UPDATE sessions SET {sets} WHERE record_id = ?", (*attrs.values(), record_id)
            )
            self._enqueue_session_upsert(conn, record_id, now)

    def close(self, record_id: str, at: str | None = None, actor: str = "registry",
              source: str = "registry") -> bool:
        """Set closed_at once and retire the live alias.  Closing twice changes nothing."""
        now = at or _utcnow()
        with self._conn as conn:
            row = self._get(record_id)
            if row["closed_at"]:
                return False
            letter, _ = self._active_alias(conn, record_id)
            conn.execute(
                "UPDATE sessions SET closed_at = ?, state = 'closed' WHERE record_id = ?",
                (now, record_id),
            )
            conn.execute(
                "UPDATE alias_assignments SET retired_at = ?"
                " WHERE record_id = ? AND retired_at IS NULL",
                (now, record_id),
            )
            self._insert_event(conn, record_id, letter, "close", actor, "closed", source, "", now)
            self._enqueue_session_upsert(conn, record_id, now)
            return True

    def record_event(self, record_id: str, event: str, actor: str, outcome: str,
                     source: str, details: str = "") -> str:
        now = _utcnow()
        with self._conn as conn:
            self._get(record_id)
            letter, _ = self._active_alias(conn, record_id)
            return self._insert_event(conn, record_id, letter, event, actor, outcome, source,
                                      details, now)


# ---- outbox bridge for the async exporter (AFC-412 PR2) -------------------------

def pending_outbox(db_path: str, limit: int = 50) -> list[dict[str, Any]]:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT outbox_id, kind, payload, created_at FROM export_outbox"
            " WHERE exported_at IS NULL ORDER BY created_at, outbox_id LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_exported(db_path: str, outbox_ids: list[str], exported_at: str | None = None) -> int:
    """Mark outbox rows exported only after the Sheets write is confirmed.  Re-running after a
    crash simply re-exports unmarked rows;  the exporter reconciles by id so that is safe."""
    if not outbox_ids:
        return 0
    at = exported_at or _utcnow()
    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            cur = conn.executemany(
                "UPDATE export_outbox SET exported_at = ? WHERE outbox_id = ?",
                [(at, i) for i in outbox_ids],
            )
        return cur.rowcount
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="operator-registry")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("pending-outbox")
    p.add_argument("--db", required=True)
    p.add_argument("--limit", type=int, default=50)
    m = sub.add_parser("mark-exported")
    m.add_argument("--db", required=True)
    m.add_argument("outbox_ids", nargs="+")
    args = parser.parse_args(argv)
    if args.command == "pending-outbox":
        print(json.dumps(pending_outbox(args.db, args.limit)))
        return 0
    print(json.dumps({"marked": mark_exported(args.db, args.outbox_ids)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
