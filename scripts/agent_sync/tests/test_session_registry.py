"""Tests for the Operator's session registry sheet mirror (session_registry.py).

Hermetic:  the registry store is a temp SQLite file built from
operator_registry_schema.DDL with hand-written rows, and the sheet is an in-memory
adapter with the documented protocol, so no Google call and no live store is touched.
The cases pin the promises the design note makes:  one row per session keyed by
record_id, alias semantics that match the registry's own last-alias rule, deterministic
Linear-issue extraction that accumulates and dedupes, idempotent upserts, status and
relabel transitions updating the row in place, duplicate-key handling that never
deletes, header validation before any write, a failed write that stops the sync
partial, and a store the sync provably never writes.

Run:  cd scripts && python3 -m unittest agent_sync.tests.test_session_registry
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from agent_sync import session_registry as SR  # noqa: E402
from agent_sync.operator_registry_schema import DDL  # noqa: E402

T0 = 1_763_000_000.0  # a fixed clock so the Registry Updated At stamp is deterministic


def rid(n: int) -> str:
    return "01" + "A" * 22 + "%02d" % n  # ULID-shaped, sorts in n order


def make_store(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(DDL)
    return conn


def add_session(conn, n, *, seat="MA", platform="claude", host="mac-jay",
                scope="default", native=None, title="", thread_link="",
                state="active", wake="unknown", registered="2026-11-01T10:00:00.000Z",
                last_seen="2026-11-03T01:00:00.000Z", closed_at=None, source="hook"):
    conn.execute(
        "INSERT INTO sessions(record_id, seat, platform, verified_host, profile_scope,"
        " native_session_id, title, project, thread_link, apple_note_link, state,"
        " wake_capability, registered_at, last_seen_at, closed_at, source)"
        " VALUES (?,?,?,?,?,?,?,'',?,'',?,?,?,?,?,?)",
        (rid(n), seat, platform, host, scope, native or "sess-%03d" % n, title,
         thread_link, state, wake, registered, last_seen, closed_at, source),
    )


def add_alias(conn, n, letter, generation, *, platform="claude",
              assigned="2026-11-01T10:00:00.000Z", retired=None):
    conn.execute(
        "INSERT INTO alias_assignments(platform, letter, generation, record_id,"
        " assigned_at, retired_at) VALUES (?,?,?,?,?,?)",
        (platform, letter, generation, rid(n), assigned, retired),
    )


def add_event(conn, eid, n, *, event="touch", outcome="ok", details="",
              recorded="2026-11-02T10:00:00.000Z", label="A"):
    conn.execute(
        "INSERT INTO events(event_id, recorded_at, session_record_id, label, event,"
        " actor, outcome, source, details) VALUES (?,?,?,?,?,?,?,?,?)",
        (eid, recorded, rid(n), label, event, "OPERATOR", outcome, "test", details),
    )


def base_store(path: str) -> str:
    """Four sessions covering the shapes the projection must get right."""
    conn = make_store(path)
    # 1: active, live alias, issues in title + thread link + event details.
    add_session(conn, 1, title="AFC-412 letters work",
                thread_link="https://simplewithus.zulipchat.com/#narrow/topic/AFC-593",
                wake="direct")
    add_alias(conn, 1, "A", 1)
    add_event(conn, "E1", 1, event="register", details="issued letter A for AFC-412")
    add_event(conn, "E2", 1, event="touch")
    add_event(conn, "E3", 1, event="notes_request",
              details='{"note_id": "n1", "ref": "AFC-486"}')
    # 2: closed; its letter was retired at close and later recycled to session 3.
    add_session(conn, 2, seat="CODEX", platform="codex", host="hetzner",
                title="plain title", state="closed",
                registered="2026-10-30T09:00:00.000Z",
                last_seen="2026-11-01T12:00:00.000Z",
                closed_at="2026-11-01T12:30:00.000Z", source="cli")
    add_alias(conn, 2, "A", 1, platform="codex", assigned="2026-10-30T09:00:00.000Z",
              retired="2026-11-01T12:30:00.000Z")
    add_event(conn, "E4", 2, event="register", label="A")
    add_event(conn, "E5", 2, event="close", outcome="closed",
              details="closed by operator", label="A")
    # 3: active holder of the recycled letter, generation 2.
    add_session(conn, 3, seat="CODEX", platform="codex", host="hetzner",
                title="second codex", registered="2026-11-02T08:00:00.000Z",
                last_seen="2026-11-02T09:00:00.000Z", source="cli")
    add_alias(conn, 3, "A", 2, platform="codex", assigned="2026-11-02T08:00:00.000Z")
    add_event(conn, "E6", 3, event="register", label="A")
    # 4: registered but never lettered, no events.
    add_session(conn, 4, title="unlettered")
    conn.commit()
    conn.close()
    return path


class FakeAdapter:
    """In-memory sheet:  rows are 1-based with the header at row 1, like the real tab."""

    def __init__(self, headers=None, rows=None, fail_on_write=None):
        self.headers = list(SR.HEADERS) if headers is None else headers
        self.rows = [list(r) for r in (rows or [])]  # data rows only; rowNumber = index + 2
        self.fail_on_write = fail_on_write  # 1-based write-call number that raises
        self.writes = 0

    def _write(self):
        self.writes += 1
        if self.fail_on_write is not None and self.writes == self.fail_on_write:
            raise RuntimeError("write blew up")

    def validate_headers(self):
        return list(self.headers)

    def read_rows(self):
        return [
            {"rowNumber": i + 2, "cells": list(cells)} for i, cells in enumerate(self.rows)
        ]

    def append_row(self, cells):
        self._write()
        self.rows.append(list(cells))
        return len(self.rows) + 1

    def update_row(self, row_number, cells):
        self._write()
        self.rows[row_number - 2] = list(cells)


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = base_store(os.path.join(self.tmp.name, "registry.sqlite3"))

    def rows(self, now=T0):
        return SR.load_rows(self.db, now=now)

    def row_for(self, n, now=T0):
        for row in self.rows(now=now):
            if row[0] == rid(n):
                return row
        self.fail("no row for session %d" % n)


class ProjectionTest(StoreCase):
    def test_row_per_session_in_record_id_order(self):
        rows = self.rows()
        self.assertEqual([r[0] for r in rows], [rid(1), rid(2), rid(3), rid(4)])
        self.assertTrue(all(len(r) == len(SR.HEADERS) for r in rows))

    def test_active_session_cells(self):
        row = self.row_for(1)
        self.assertEqual(
            row[:17],
            [
                rid(1), "A", "1", "MA", "claude", "mac-jay", "default", "sess-001",
                "AFC-412 letters work",
                "https://simplewithus.zulipchat.com/#narrow/topic/AFC-593",
                "active", "direct",
                "2026-11-01T10:00:00.000Z", "2026-11-03T01:00:00.000Z", "",
                "3", "AFC-412, AFC-486, AFC-593",
            ],
        )
        self.assertEqual(row[17], SR._iso(T0))

    def test_closed_session_keeps_last_held_letter(self):
        row = self.row_for(2)
        self.assertEqual(row[1:3], ["A", "1"])  # retired alias still shown
        self.assertEqual(row[10], "closed")
        self.assertEqual(row[14], "2026-11-01T12:30:00.000Z")
        self.assertEqual(row[15], "2")
        self.assertEqual(row[16], "")

    def test_recycled_letter_generation(self):
        row = self.row_for(3)
        self.assertEqual(row[1:3], ["A", "2"])

    def test_unlettered_session_blank_letter_and_zero_events(self):
        row = self.row_for(4)
        self.assertEqual(row[1:3], ["", ""])
        self.assertEqual(row[15], "0")
        self.assertEqual(row[16], "")


class LinearIssuesTest(StoreCase):
    def test_extract_pattern(self):
        self.assertEqual(
            SR.extract_linear_issues("see AFC-412 and OPS-7, not afc-412 or A-1"),
            ["AFC-412", "OPS-7"],
        )
        self.assertEqual(SR.extract_linear_issues(""), [])
        self.assertEqual(SR.extract_linear_issues("no keys here"), [])

    def test_accumulates_and_dedupes_over_time(self):
        self.assertEqual(self.row_for(1)[16], "AFC-412, AFC-486, AFC-593")
        conn = sqlite3.connect(self.db)
        add_event(conn, "E7", 1, event="notes_result",
                  details='{"note_id": "n2", "follow_up": "CT-2442 and AFC-412 again"}')
        conn.commit()
        conn.close()
        # The new key joins the list;  the repeated key does not duplicate.
        self.assertEqual(self.row_for(1)[16], "AFC-412, AFC-486, AFC-593, CT-2442")
        self.assertEqual(self.row_for(1)[15], "4")


class ReadOnlyTest(StoreCase):
    def _digest(self):
        with open(self.db, "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()

    def test_store_bytes_and_directory_unchanged(self):
        before = self._digest()
        listing_before = sorted(os.listdir(self.tmp.name))
        adapter = FakeAdapter()
        SR.sync_to_sheet(self.db, adapter, now=T0)
        SR.load_rows(self.db, now=T0)
        self.assertEqual(self._digest(), before)
        self.assertEqual(sorted(os.listdir(self.tmp.name)), listing_before)

    def test_handle_refuses_writes(self):
        conn = SR.open_registry_readonly(self.db)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("INSERT INTO sessions(record_id) VALUES ('x')")
        finally:
            conn.close()

    def test_missing_store_is_an_error_not_a_creation(self):
        missing = os.path.join(self.tmp.name, "nope.sqlite3")
        with self.assertRaises(sqlite3.OperationalError):
            SR.load_rows(missing)
        self.assertFalse(os.path.exists(missing))


class SyncTest(StoreCase):
    def test_first_sync_appends_all_then_idempotent(self):
        adapter = FakeAdapter()
        result = SR.sync_to_sheet(self.db, adapter, now=T0)
        self.assertEqual(
            (result["appended"], result["updated"], result["unchanged"],
             result["failed"], result["partial"]),
            (4, 0, 0, 0, False),
        )
        self.assertEqual(adapter.rows, self.rows())
        # A later run, even with a different clock, writes nothing:  only the
        # Registry Updated At stamp differs, and it is excluded from matching.
        again = SR.sync_to_sheet(self.db, adapter, now=T0 + 3600)
        self.assertEqual((again["appended"], again["updated"], again["unchanged"]),
                         (0, 0, 4))
        self.assertEqual(adapter.writes, 4)

    def test_status_transition_updates_in_place(self):
        adapter = FakeAdapter()
        SR.sync_to_sheet(self.db, adapter, now=T0)
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE sessions SET state='closed',"
                     " closed_at='2026-11-03T02:00:00.000Z',"
                     " last_seen_at='2026-11-03T02:00:00.000Z' WHERE record_id=?",
                     (rid(1),))
        conn.execute("UPDATE alias_assignments SET retired_at='2026-11-03T02:00:00.000Z'"
                     " WHERE record_id=?", (rid(1),))
        conn.commit()
        conn.close()
        result = SR.sync_to_sheet(self.db, adapter, now=T0 + 60)
        self.assertEqual((result["appended"], result["updated"]), (0, 1))
        self.assertEqual(len(adapter.rows), 4)
        row = adapter.rows[0]
        self.assertEqual(row[0], rid(1))
        self.assertEqual(row[10], "closed")
        self.assertEqual(row[14], "2026-11-03T02:00:00.000Z")
        self.assertEqual(row[1], "A")  # last held letter survives the close

    def test_relabel_updates_letter_and_generation(self):
        adapter = FakeAdapter()
        SR.sync_to_sheet(self.db, adapter, now=T0)
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE alias_assignments SET retired_at='2026-11-03T02:00:00.000Z'"
                     " WHERE record_id=? AND retired_at IS NULL", (rid(3),))
        add_alias(conn, 3, "B", 1, platform="codex", assigned="2026-11-03T02:00:00.000Z")
        conn.commit()
        conn.close()
        result = SR.sync_to_sheet(self.db, adapter, now=T0 + 60)
        self.assertEqual((result["appended"], result["updated"]), (0, 1))
        row = [r for r in adapter.rows if r[0] == rid(3)][0]
        self.assertEqual(row[1:3], ["B", "1"])

    def test_new_session_appends(self):
        adapter = FakeAdapter()
        SR.sync_to_sheet(self.db, adapter, now=T0)
        conn = sqlite3.connect(self.db)
        add_session(conn, 5, title="late arrival")
        add_alias(conn, 5, "B", 1)
        conn.commit()
        conn.close()
        result = SR.sync_to_sheet(self.db, adapter, now=T0 + 60)
        self.assertEqual((result["appended"], result["updated"]), (1, 0))
        self.assertEqual(adapter.rows[-1][0], rid(5))

    def test_duplicate_record_id_rows_reported_never_deleted(self):
        adapter = FakeAdapter()
        SR.sync_to_sheet(self.db, adapter, now=T0)
        adapter.rows.append(list(adapter.rows[0]))  # a second row with Record ID 1
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE sessions SET last_seen_at='2026-11-03T03:00:00.000Z'"
                     " WHERE record_id=?", (rid(1),))
        conn.commit()
        conn.close()
        result = SR.sync_to_sheet(self.db, adapter, now=T0 + 60)
        self.assertEqual(result["duplicate_rows"], [6])
        self.assertEqual(result["updated"], 1)
        self.assertEqual(len(adapter.rows), 5)  # the duplicate is left in place
        self.assertEqual(adapter.rows[0][13], "2026-11-03T03:00:00.000Z")
        self.assertNotEqual(adapter.rows[4][13], "2026-11-03T03:00:00.000Z")

    def test_header_mismatch_writes_nothing(self):
        adapter = FakeAdapter(headers=["Record ID", "Wrong"] + SR.HEADERS[2:])
        with self.assertRaises(SR.SheetHeaderError):
            SR.sync_to_sheet(self.db, adapter, now=T0)
        self.assertEqual(adapter.writes, 0)
        self.assertEqual(adapter.rows, [])

    def test_failed_write_stops_sync_partial_then_reconciles(self):
        adapter = FakeAdapter(fail_on_write=2)  # second append raises
        result = SR.sync_to_sheet(self.db, adapter, now=T0)
        self.assertEqual(
            (result["appended"], result["failed"], result["partial"]),
            (1, 1, True),
        )
        self.assertIn("RuntimeError", result["error"])
        self.assertEqual(len(adapter.rows), 1)
        adapter.fail_on_write = None
        healed = SR.sync_to_sheet(self.db, adapter, now=T0 + 60)
        self.assertEqual((healed["appended"], healed["unchanged"]), (3, 1))
        self.assertEqual([r[0] for r in adapter.rows],
                         [rid(1), rid(2), rid(3), rid(4)])


class CliTest(StoreCase):
    def test_export_rows_payload(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = SR._main(["export-rows", "--registry", self.db])
        self.assertEqual(rc, 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["spreadsheetId"], SR.SPREADSHEET_ID)
        self.assertEqual(payload["tab"], "Sessions")
        self.assertEqual(payload["headers"], SR.HEADERS)
        self.assertEqual([r[0] for r in payload["rows"]],
                         [rid(1), rid(2), rid(3), rid(4)])

    def test_stats(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = SR._main(["stats", "--registry", self.db])
        self.assertEqual(rc, 0)
        stats = json.loads(out.getvalue())
        self.assertEqual(stats["sessions"], 4)
        self.assertEqual(stats["events"], 6)


if __name__ == "__main__":
    unittest.main()
