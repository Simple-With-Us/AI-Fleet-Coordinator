"""AFC-822:  the export store ages out exported outbox rows and their audit attempts.

The exporter reconciles by id against the sheet itself, so an exported outbox row is never
read again;  the store-side fence (OPS-1037) is an audit record whose old rows are deleted
once they pass the window, and the outbox rows whose attempts are all gone go with them.
Pending rows and their attempts are never touched."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta

from simple_tools import operator_registry as OR
from simple_tools.operator_registry_schema import DDL


def iso(at: datetime) -> str:
    return at.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class TrimTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "registry.sqlite3")
        conn = sqlite3.connect(self.db)
        try:
            conn.executescript(DDL)
        finally:
            conn.close()

    def row(
        self,
        outbox_id: str,
        *,
        exported_at: str | None = None,
        created_at: str | None = None,
        attempts: list[tuple[str, str]] = (),
    ) -> None:
        now = iso(datetime.now(UTC))
        conn = sqlite3.connect(self.db)
        try:
            with conn:
                conn.execute(
                    "INSERT INTO export_outbox(outbox_id, kind, payload, created_at, exported_at)"
                    " VALUES (?, 'session_upsert', '{\"cells\": [\"x\"]}', ?, ?)",
                    (outbox_id, created_at or now, exported_at),
                )
                for outcome, at in attempts:
                    conn.execute(
                        "INSERT INTO export_attempts"
                        " (attempt_id, outbox_id, destination, tab, kind, outcome, detail, attempted_at)"
                        " VALUES (?, ?, 'wb', 'Sessions', 'session_upsert', ?, '', ?)",
                        (f"a-{outbox_id}-{outcome}-{at}", outbox_id, outcome, at),
                    )
        finally:
            conn.close()

    def store(self) -> dict[str, int]:
        conn = sqlite3.connect(self.db)
        try:
            return {
                "outbox": conn.execute("SELECT COUNT(*) FROM export_outbox").fetchone()[0],
                "attempts": conn.execute("SELECT COUNT(*) FROM export_attempts").fetchone()[0],
                "pending": conn.execute(
                    "SELECT COUNT(*) FROM export_outbox WHERE exported_at IS NULL"
                ).fetchone()[0],
                "lost": conn.execute(
                    "SELECT COUNT(*) FROM export_outbox WHERE exported_at IS NOT NULL"
                ).fetchone()[0],
            }
        finally:
            conn.close()

    def test_old_exported_rows_and_their_attempts_age_out(self) -> None:
        old = iso(datetime.now(UTC) - timedelta(days=8))
        recent = iso(datetime.now(UTC) - timedelta(days=1))
        self.row("old-1", exported_at=old, attempts=[("exported", old)])
        self.row("old-2", exported_at=old, attempts=[("failed", old)])
        self.row("new-1", exported_at=recent, attempts=[("exported", recent)])
        self.row("pending-1", attempts=[("failed", recent)])
        counts = OR.trim_export_rows(self.db, keep_seconds=7 * 24 * 3600)
        self.assertEqual(counts, {"attempts": 2, "outbox": 2})
        self.assertEqual(
            self.store(),
            {"outbox": 2, "attempts": 2, "pending": 1, "lost": 1},
            "the recent exported row, its attempt and the pending row with its attempt stay",
        )

    def test_an_old_exported_row_with_a_recent_attempt_stays(self) -> None:
        # A fresh audit entry for an old exported row keeps the row, so the fence's
        # reference is never orphaned and the recent attempt stays auditable.
        old = iso(datetime.now(UTC) - timedelta(days=8))
        recent = iso(datetime.now(UTC) - timedelta(days=1))
        self.row("mixed", exported_at=old, attempts=[("exported", old), ("exported", recent)])
        counts = OR.trim_export_rows(self.db, keep_seconds=7 * 24 * 3600)
        self.assertEqual(counts, {"attempts": 1, "outbox": 0})
        self.assertEqual(self.store(), {"outbox": 1, "attempts": 1, "pending": 0, "lost": 1})

    def test_nothing_to_delete_vacuums_nothing_and_answers_zero(self) -> None:
        recent = iso(datetime.now(UTC) - timedelta(days=1))
        self.row("new-1", exported_at=recent, attempts=[("exported", recent)])
        counts = OR.trim_export_rows(self.db, keep_seconds=7 * 24 * 3600)
        self.assertEqual(counts, {"attempts": 0, "outbox": 0})
        self.assertEqual(self.store(), {"outbox": 1, "attempts": 1, "pending": 0, "lost": 1})

    def test_the_default_window_is_seven_days(self) -> None:
        self.assertEqual(OR.EXPORT_KEEP_SECONDS, 7 * 24 * 3600)

    def test_a_pending_row_is_never_deleted_even_when_old(self) -> None:
        # A row the drain never confirmed stays pending however old it is:  deleting it
        # would drop an event the sheet has never seen.
        ancient = iso(datetime.now(UTC) - timedelta(days=30))
        self.row("stuck", created_at=ancient, attempts=[("failed", ancient)])
        counts = OR.trim_export_rows(self.db, keep_seconds=7 * 24 * 3600)
        self.assertEqual(counts, {"attempts": 1, "outbox": 0})
        self.assertEqual(self.store(), {"outbox": 1, "attempts": 0, "pending": 1, "lost": 0})


if __name__ == "__main__":
    unittest.main()
