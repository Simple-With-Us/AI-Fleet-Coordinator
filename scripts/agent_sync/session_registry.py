"""The Operator's session registry, mirrored to the "Sessions" tab of the Operator
Thread Registry workbook.

Why this exists (owner, Tue, Nov 3, 2026, as corrected):  the "chat threads" the Operator
registers in a sheet are first of all the **lettered agent session threads** — the
Operator's own session registry (operator_registry.py's SQLite store:  sessions,
alias_assignments, events), the same sessions the Operator picks to wake.  The Zulip
stream/topic registry (thread_registry.py) is the second track in the same workbook, on
the "Zulip Threads" tab.  The full design note, with the source of every column, is
docs/operator-session-registry.md.

Shape:

  * The source of truth stays the registry store.  This module only ever opens it
    read-only (SQLite ``mode=ro`` plus ``PRAGMA query_only``):  it is never a second
    writer, never mutates the store, and works against whichever store is canonical
    (the Mac store is the active allocator today) without competing with it.
  * One sync run makes one pass over each table (sessions, alias assignments, events),
    so a run is bounded by the registry's own size — no external history is ever read.
    The sheet writes are the incremental part:  rows whose cells already match are not
    rewritten, so a re-run with no registry change performs zero writes.
  * The upsert key is the session's ``record_id`` (the immutable ULID surrogate), so a
    relabel, a close, or a recycled letter updates the session's one row in place.
  * The Google write itself runs in session_registry_export.mjs, which owns the service
    account auth.  This module owns the semantics — projection, extraction, upsert
    planning — and sync_to_sheet() runs the same plan against any adapter with the
    documented protocol, which is what the tests exercise.

Deterministic only:  Linear Issues are extracted by the same fixed key pattern the
thread registry uses, from the fields the registry itself carries (session title,
thread link, and event details).  Nothing is inferred:  a key is recorded when its
text appears in one of those fields, and never otherwise.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import live as L
from .operator_registry import registry_db_path

__all__ = [
    "HEADERS",
    "SHEET_TAB",
    "SPREADSHEET_ID",
    "SPREADSHEET_URL",
    "SheetHeaderError",
    "extract_linear_issues",
    "open_registry_readonly",
    "plan_upserts",
    "project_rows",
    "session_records",
    "sync_to_sheet",
]

#: The one workbook that holds both Operator registries (created for the thread track,
#: Tue, Nov 3, 2026):  this module's tab is "Sessions";  thread_registry.py's tab is
#: "Zulip Threads".  The id is not a secret;  it is also recorded in
#: docs/operator-session-registry.md and in session_registry_export.mjs, which performs
#: the live Google writes.
SPREADSHEET_ID = "1JEY34a5Gd2GYazbpGZZqq2ZZ80Fp0nenCpeH8AT4wPE"
SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/%s/edit" % SPREADSHEET_ID
SHEET_TAB = "Sessions"

#: Column order is the tab's header row, exactly.  validateHeaders in the exporter and
#: sync_to_sheet both refuse to write when the live header differs.
HEADERS = [
    "Record ID",
    "Letter",
    "Generation",
    "Seat",
    "Platform",
    "Verified Host",
    "Profile Scope",
    "Native Session ID",
    "Title",
    "Thread Link",
    "Status",
    "Wake Capability",
    "Registered At (UTC)",
    "Last Seen At (UTC)",
    "Closed At (UTC)",
    "Event Count",
    "Linear Issues",
    "Registry Updated At (UTC)",
]

COL_KEY = 0
COL_UPDATED = 17

# A Linear issue mention:  two or more capitals/digits, a dash, and a number (AFC-412,
# AFC-565).  The identical pattern to thread_registry.py's, so a key means the same
# thing on both tabs.  This is a mention extractor, not a verifier:  a string shaped
# like an issue id is recorded whether or not the issue exists.
_LINEAR_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d{1,7}\b")


def extract_linear_issues(text: str) -> list[str]:
    return sorted(set(_LINEAR_RE.findall(text or "")))


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------------------------
# Reading the registry store (read-only)
# --------------------------------------------------------------------------------------------


def open_registry_readonly(db_path: str) -> sqlite3.Connection:
    """Open the registry store so that writing is impossible, not just unpromised.

    ``mode=ro`` refuses writes at the SQLite layer and never creates the file;  the
    registry's default rollback journal means a read needs no sidecar files either.
    (The wake path deliberately uses a read-write handle instead — see SessionRegistry's
    read_only comment in operator_registry.py — because it cannot afford a refused read
    against a hot journal.  A sheet sync can simply fail and let the next run reconcile,
    so it takes the strictly read-only side.)  ``PRAGMA query_only`` is the second belt:
    even a future code path in this module cannot write through this handle.
    """
    uri = "%s?mode=ro" % Path(os.path.abspath(db_path)).as_uri()
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def session_records(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One projected record per session, in Record ID order, from one pass per table.

    Column sources (docs/operator-session-registry.md has the full table):
      letter/generation — the session's live alias, or the last one it held once
        retired (SessionRegistry._last_alias's semantics:  active assignment first,
        else the latest assignment;  alias rows are never deleted, so a closed or
        relabelled session still shows the letter it ended on).
      event_count / linear_issues — folded from the session's events:  the count of
        its events rows, and the union of issue keys extracted from event details.
      linear_issues also draws from the session's own title and thread_link.
    """
    sessions = conn.execute("SELECT * FROM sessions ORDER BY record_id").fetchall()

    latest: dict[str, tuple[str, Any]] = {}
    active: dict[str, tuple[str, Any]] = {}
    alias_rows = conn.execute(
        "SELECT record_id, letter, generation, retired_at FROM alias_assignments"
        " ORDER BY assigned_at, generation"
    ).fetchall()
    for row in alias_rows:
        pair = (str(row["letter"] or ""), row["generation"])
        latest[str(row["record_id"])] = pair
        if row["retired_at"] is None:
            active[str(row["record_id"])] = pair

    event_counts: dict[str, int] = {}
    issues: dict[str, set[str]] = {}
    for row in conn.execute("SELECT session_record_id, details FROM events").fetchall():
        record_id = str(row["session_record_id"])
        event_counts[record_id] = event_counts.get(record_id, 0) + 1
        found = extract_linear_issues(str(row["details"] or ""))
        if found:
            issues.setdefault(record_id, set()).update(found)

    records: list[dict[str, Any]] = []
    for row in sessions:
        record_id = str(row["record_id"])
        letter, generation = active.get(record_id) or latest.get(record_id) or ("", "")
        keys = set(issues.get(record_id) or set())
        keys.update(extract_linear_issues(str(row["title"] or "")))
        keys.update(extract_linear_issues(str(row["thread_link"] or "")))
        records.append(
            {
                "record_id": record_id,
                "letter": letter,
                "generation": generation,
                "seat": str(row["seat"] or ""),
                "platform": str(row["platform"] or ""),
                "verified_host": str(row["verified_host"] or ""),
                "profile_scope": str(row["profile_scope"] or ""),
                "native_session_id": str(row["native_session_id"] or ""),
                "title": str(row["title"] or ""),
                "thread_link": str(row["thread_link"] or ""),
                "state": str(row["state"] or ""),
                "wake_capability": str(row["wake_capability"] or ""),
                # The registry stores canonical UTC ISO text (_utcnow);  the sheet
                # carries it verbatim rather than re-formatting it.
                "registered_at": str(row["registered_at"] or ""),
                "last_seen_at": str(row["last_seen_at"] or ""),
                "closed_at": str(row["closed_at"] or ""),
                "event_count": event_counts.get(record_id, 0),
                "linear_issues": sorted(keys),
            }
        )
    return records


def project_rows(conn: sqlite3.Connection, *, now: float | None = None) -> list[list[str]]:
    """The sheet rows for every session in the store behind `conn`, in Record ID order.
    `now` is the sync run's clock:  the Registry Updated At stamp is computed from it,
    so a caller that passes one fixed `now` gets a self-consistent sheet."""
    stamp = _iso(now if now is not None else time.time())
    out: list[list[str]] = []
    for record in session_records(conn):
        generation = record["generation"]
        out.append(
            [
                record["record_id"],
                record["letter"],
                "" if generation in ("", None) else str(generation),
                record["seat"],
                record["platform"],
                record["verified_host"],
                record["profile_scope"],
                record["native_session_id"],
                record["title"],
                record["thread_link"],
                record["state"],
                record["wake_capability"],
                record["registered_at"],
                record["last_seen_at"],
                record["closed_at"],
                str(record["event_count"]),
                ", ".join(record["linear_issues"]),
                stamp,
            ]
        )
    return out


def load_rows(db_path: str, *, now: float | None = None) -> list[list[str]]:
    """project_rows against the store at `db_path`, opened read-only and closed after."""
    conn = open_registry_readonly(db_path)
    try:
        return project_rows(conn, now=now)
    finally:
        conn.close()


# --------------------------------------------------------------------------------------------
# The sheet sync
# --------------------------------------------------------------------------------------------


class SheetHeaderError(RuntimeError):
    """The live sheet's header row is not HEADERS;  the sync writes nothing."""


def _cells_match(existing: list[str], desired: list[str]) -> bool:
    """Equality for idempotency:  every column except the Registry Updated At stamp."""
    if len(existing) < len(desired):
        existing = existing + [""] * (len(desired) - len(existing))
    for idx, want in enumerate(desired):
        if idx == COL_UPDATED:
            continue
        if existing[idx] != want:
            return False
    return True


def plan_upserts(existing: list[dict[str, Any]], desired: list[list[str]]) -> dict[str, Any]:
    """Plan one sync.  `existing` rows carry {rowNumber, cells} (1-based sheet rows, header
    is row 1).  Returns {updates: [(rowNumber, cells)], appends: [cells], unchanged: int,
    duplicate_rows: [rowNumber]}.  When the sheet somehow holds two rows with one Record
    ID, the first is the row of record and the rest are reported as duplicates — never
    written, never deleted by the sync."""
    by_key: dict[str, dict[str, Any]] = {}
    duplicates: list[int] = []
    for row in existing:
        cells = [str(c) for c in (row.get("cells") or [])]
        if not cells or not cells[COL_KEY]:
            continue
        key = cells[COL_KEY]
        if key in by_key:
            duplicates.append(int(row.get("rowNumber") or 0))
        else:
            by_key[key] = row
    updates: list[tuple[int, list[str]]] = []
    appends: list[list[str]] = []
    unchanged = 0
    for cells in desired:
        key = cells[COL_KEY]
        current = by_key.get(key)
        if current is None:
            appends.append(cells)
            continue
        current_cells = [str(c) for c in (current.get("cells") or [])]
        if _cells_match(current_cells, cells):
            unchanged += 1
        else:
            updates.append((int(current.get("rowNumber") or 0), list(cells)))
    return {
        "updates": updates,
        "appends": appends,
        "unchanged": unchanged,
        "duplicate_rows": duplicates,
    }


def sync_to_sheet(db_path: str, adapter: Any, *, now: float | None = None) -> dict[str, Any]:
    """Upsert the registry's rows through `adapter` and report exactly what happened.

    Adapter protocol (the live implementation is session_registry_export.mjs's drain;
    the tests use an in-memory adapter with the same surface):

        validate_headers() -> list[str]   the sheet's live header row
        read_rows() -> [{rowNumber, cells}]
        append_row(cells) -> rowNumber
        update_row(rowNumber, cells) -> None

    The header is validated before any write, so a renamed or reordered column leaves
    the sheet untouched.  Writes stop at the first failure:  rows already written stay
    written, the result says partial=True with the counts so far, and the next run
    reconciles by Record ID — the same discipline as the thread registry's sync."""
    headers = [str(h) for h in adapter.validate_headers()]
    if headers != HEADERS:
        raise SheetHeaderError("sheet header mismatch: expected %r, found %r" % (HEADERS, headers))
    moment = now if now is not None else time.time()
    plan = plan_upserts(adapter.read_rows(), load_rows(db_path, now=moment))
    result: dict[str, Any] = {
        "updated": 0,
        "appended": 0,
        "unchanged": plan["unchanged"],
        "duplicate_rows": plan["duplicate_rows"],
        "failed": 0,
        "partial": False,
        "error": None,
    }
    try:
        for row_number, cells in plan["updates"]:
            adapter.update_row(row_number, cells)
            result["updated"] += 1
        for cells in plan["appends"]:
            adapter.append_row(cells)
            result["appended"] += 1
    except Exception as exc:  # a failed write stops the sync;  nothing is retried blindly
        result["failed"] = 1
        result["partial"] = True
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
    return result


def _main(argv: list[str]) -> int:
    """Operator-side CLI:  `export-rows` writes the desired rows as JSON for the Node drain
    (session_registry_export.mjs);  `stats` prints the store's session/event counts.
    Neither talks to Google — the drain is the only Google writer."""
    import argparse

    parser = argparse.ArgumentParser(prog="session_registry")
    parser.add_argument("command", choices=["export-rows", "stats"])
    parser.add_argument(
        "--registry",
        default=None,
        help="registry store path (default: the AGENT_SYNC_OPERATOR_REGISTRY"
        " override or <state root>/operator/registry.sqlite3)",
    )
    parser.add_argument("--state-root", default=None)
    parser.add_argument("--out", default=None)
    args = parser.parse_args(argv)
    root = args.state_root or L.state_root(os.environ)
    db_path = args.registry or registry_db_path(root, os.environ)
    if args.command == "stats":
        conn = open_registry_readonly(db_path)
        try:
            records = session_records(conn)
        finally:
            conn.close()
        print(
            json.dumps(
                {
                    "sessions": len(records),
                    "events": sum(r["event_count"] for r in records),
                    "registry": db_path,
                }
            )
        )
        return 0
    payload = {
        "spreadsheetId": SPREADSHEET_ID,
        "tab": SHEET_TAB,
        "headers": HEADERS,
        "rows": load_rows(db_path),
    }
    text = json.dumps(payload, indent=1)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main(os.sys.argv[1:]))
