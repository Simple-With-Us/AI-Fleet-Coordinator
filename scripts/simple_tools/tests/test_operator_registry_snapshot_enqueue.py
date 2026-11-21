"""Local canonical session snapshot enqueue preserves registry state."""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from simple_tools import operator_registry as OR
from simple_tools import registry_api


class SessionSnapshotEnqueueTests(unittest.TestCase):
    TABLES = (
        "schema_migrations",
        "sessions",
        "alias_assignments",
        "events",
        "session_issues",
        "export_outbox",
        "export_attempts",
    )

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "registry.sqlite3")
        OR.SessionRegistry(self.db).close_db()

    def _record(self, *, closed: bool) -> tuple[str, str]:
        native_id = "native-session-fixture-%s" % ("closed" if closed else "active")
        registered = OR.register_session(
            self.db,
            platform="minimax",
            verified_host="server",
            profile_scope="fixture-profile",
            native_session_id=native_id,
            seat="MM",
            source="snapshot-test",
            title="Snapshot fixture",
            project="AFC-859",
            thread_link="https://example.invalid/thread",
            apple_note_link="notes://fixture",
            issuer=OR.ISSUER_OPERATOR,
        )
        record_id = registered["record_id"]
        OR.update_session(
            self.db,
            platform="minimax",
            verified_host="server",
            profile_scope="fixture-profile",
            native_session_id=native_id,
            issues=[{"id": "AFC-859"}],
        )
        registry = OR.SessionRegistry(self.db)
        try:
            registry.mark_seen(record_id, at="2026-11-21T12:00:00.000Z")
            if closed:
                registry.close(
                    record_id,
                    at="2026-11-21T12:01:00.000Z",
                    actor="snapshot-test",
                    source="snapshot-test",
                )
        finally:
            registry.close_db()
        rows = OR.pending_outbox(self.db, 100)
        ids = [row["outbox_id"] for row in rows]
        OR.record_export_attempt(
            self.db,
            ids[0],
            "fixture-destination",
            "Sessions",
            "session_upsert",
            "exported",
        )
        OR.mark_exported(self.db, ids, exported_at="2026-11-21T12:02:00.000Z")
        return record_id, native_id

    def _table_snapshot(self) -> dict[str, list[tuple[object, ...]]]:
        conn = sqlite3.connect(self.db)
        try:
            return {
                table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
                for table in self.TABLES
            }
        finally:
            conn.close()

    def _snapshot_cells(self, record_id: str) -> list[str]:
        registry = OR.SessionRegistry(self.db, read_only=True)
        try:
            row = registry._get(record_id)
            label, _ = registry._active_alias(registry._conn, record_id)
            return OR._session_cells(row, label)
        finally:
            registry.close_db()

    def _cli(self, *args: str) -> tuple[int, dict[str, object], str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = OR.main(list(args), env={})
        raw = output.getvalue()
        return code, json.loads(raw), raw

    def _plan(self, record_id: str) -> tuple[dict[str, object], str]:
        code, answer, raw = self._cli(
            "plan-session-snapshot", "--db", self.db, "--record-id", record_id
        )
        self.assertEqual(code, 0)
        return answer, raw

    def _enqueue(self, record_id: str, digest: str) -> tuple[int, dict[str, object], str]:
        return self._cli(
            "enqueue-session-snapshot",
            "--db",
            self.db,
            "--record-id",
            record_id,
            "--expected-sha256",
            digest,
        )

    def test_plan_and_enqueue_preserve_active_and_closed_registry_rows(self) -> None:
        for closed in (False, True):
            with self.subTest(closed=closed):
                record_id, native_id = self._record(closed=closed)
                before = self._table_snapshot()
                plan, raw_plan = self._plan(record_id)
                self.assertEqual(before, self._table_snapshot(), "planning must not write")
                self.assertEqual(plan["action"], "enqueue")
                self.assertEqual(plan["pending_identical"], 0)
                cells = self._snapshot_cells(record_id)
                digest = OR._session_snapshot_digest(cells)
                self.assertEqual(plan["snapshot_sha256"], digest)
                self.assertNotIn(record_id, raw_plan)
                self.assertNotIn(native_id, raw_plan)
                self.assertNotIn(cells[6], raw_plan)

                code, answer, raw_write = self._enqueue(record_id, digest)
                self.assertEqual(code, 0)
                self.assertEqual(answer["outcome"], "queued")
                self.assertNotIn(record_id, raw_write)
                self.assertNotIn(native_id, raw_write)
                after = self._table_snapshot()
                for table in self.TABLES:
                    if table == "export_outbox":
                        self.assertEqual(after[table][:-1], before[table])
                        self.assertEqual(len(after[table]), len(before[table]) + 1)
                    else:
                        self.assertEqual(after[table], before[table], table)
                new_outbox = after["export_outbox"][-1]
                self.assertEqual(new_outbox[1], "session_upsert")
                self.assertIsNone(new_outbox[4])
                self.assertEqual(json.loads(new_outbox[2]), {"cells": cells})
                self.assertTrue(new_outbox[3])

    def test_repeated_enqueue_is_idempotent_when_identical_snapshot_is_pending(self) -> None:
        record_id, _ = self._record(closed=False)
        plan, _ = self._plan(record_id)
        digest = str(plan["snapshot_sha256"])
        code, first, _ = self._enqueue(record_id, digest)
        self.assertEqual(code, 0)
        self.assertEqual(first["outcome"], "queued")
        after_first = self._table_snapshot()

        repeated_plan, _ = self._plan(record_id)
        self.assertEqual(repeated_plan["action"], "already_pending")
        self.assertEqual(repeated_plan["pending_identical"], 1)
        code, second, _ = self._enqueue(record_id, digest)
        self.assertEqual(code, 0)
        self.assertEqual(second["outcome"], "already_pending")
        self.assertEqual(second["pending_identical"], 1)
        self.assertEqual(self._table_snapshot(), after_first)

    def test_plan_uses_one_wal_snapshot_when_record_closes_between_reads(self) -> None:
        record_id, _ = self._record(closed=False)
        preclose_cells = self._snapshot_cells(record_id)
        preclose_digest = OR._session_snapshot_digest(preclose_cells)
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode = WAL").fetchone()[0], "wal")
        finally:
            conn.close()

        original_get = OR.SessionRegistry._get
        closed_during_plan = False

        def close_after_plan_select(registry: OR.SessionRegistry, selected_id: str):
            nonlocal closed_during_plan
            row = original_get(registry, selected_id)
            if registry.read_only and selected_id == record_id and not closed_during_plan:
                close_registry = OR.SessionRegistry(self.db)
                try:
                    self.assertTrue(
                        close_registry.close(
                            record_id,
                            at="2026-11-21T12:03:00.000Z",
                            actor="snapshot-race-test",
                            source="snapshot-race-test",
                        )
                    )
                    closed_during_plan = True
                finally:
                    close_registry.close_db()
            return row

        with mock.patch.object(OR.SessionRegistry, "_get", new=close_after_plan_select):
            plan, _ = self._plan(record_id)

        self.assertTrue(closed_during_plan)
        self.assertEqual(plan["snapshot_sha256"], preclose_digest)
        self.assertEqual(plan["pending_identical"], 0)
        postclose_digest = OR._session_snapshot_digest(self._snapshot_cells(record_id))
        self.assertNotEqual(postclose_digest, preclose_digest)
        before_stale_enqueue = self._table_snapshot()
        code, stale, _ = self._enqueue(record_id, preclose_digest)
        self.assertEqual(code, 2)
        self.assertEqual(stale["code"], "stale_snapshot")
        self.assertEqual(self._table_snapshot(), before_stale_enqueue)

    def test_enqueue_without_current_migration_marker_does_not_initialize_schema(self) -> None:
        record_id, _ = self._record(closed=True)
        conn = sqlite3.connect(self.db)
        try:
            conn.execute(
                "DELETE FROM schema_migrations WHERE version = ?", (OR.SCHEMA_VERSION,)
            )
            conn.commit()
        finally:
            conn.close()

        before = self._table_snapshot()
        self.assertNotIn(OR.SCHEMA_VERSION, [row[0] for row in before["schema_migrations"]])
        plan, _ = self._plan(record_id)
        self.assertNotIn(OR.SCHEMA_VERSION, [row[0] for row in self._table_snapshot()["schema_migrations"]])
        code, result, _ = self._enqueue(record_id, str(plan["snapshot_sha256"]))
        self.assertEqual(code, 0)
        self.assertEqual(result["outcome"], "queued")

        after = self._table_snapshot()
        for table in self.TABLES:
            if table == "export_outbox":
                self.assertEqual(after[table][:-1], before[table])
                self.assertEqual(len(after[table]), len(before[table]) + 1)
            else:
                self.assertEqual(after[table], before[table], table)
        self.assertNotIn(OR.SCHEMA_VERSION, [row[0] for row in after["schema_migrations"]])

    def test_stale_plan_and_unknown_record_refuse_without_writes_or_identity_output(self) -> None:
        record_id, native_id = self._record(closed=False)
        plan, _ = self._plan(record_id)
        stale_digest = str(plan["snapshot_sha256"])
        OR.update_session(
            self.db,
            platform="minimax",
            verified_host="server",
            profile_scope="fixture-profile",
            native_session_id=native_id,
            title="Changed after plan",
        )
        before_stale_write = self._table_snapshot()
        code, stale, raw_stale = self._enqueue(record_id, stale_digest)
        self.assertEqual(code, 2)
        self.assertEqual(stale["code"], "stale_snapshot")
        self.assertNotIn(record_id, raw_stale)
        self.assertNotIn(native_id, raw_stale)
        self.assertEqual(self._table_snapshot(), before_stale_write)

        unknown = "record-does-not-exist"
        before_unknown = self._table_snapshot()
        code, missing, raw_missing = self._cli(
            "plan-session-snapshot", "--db", self.db, "--record-id", unknown
        )
        self.assertEqual(code, 2)
        self.assertEqual(missing["code"], "unknown_record")
        self.assertNotIn(unknown, raw_missing)
        code, missing, raw_missing = self._enqueue(unknown, "0" * 64)
        self.assertEqual(code, 2)
        self.assertEqual(missing["code"], "unknown_record")
        self.assertNotIn(unknown, raw_missing)
        self.assertEqual(self._table_snapshot(), before_unknown)

    def test_invalid_digest_refuses_without_writes(self) -> None:
        record_id, _ = self._record(closed=False)
        before = self._table_snapshot()
        code, answer, raw = self._enqueue(record_id, "not-a-digest")
        self.assertEqual(code, 2)
        self.assertEqual(answer["code"], "invalid_snapshot_sha256")
        self.assertNotIn(record_id, raw)
        self.assertEqual(self._table_snapshot(), before)

    def test_local_snapshot_commands_do_not_add_or_use_registry_api_operations(self) -> None:
        record_id, _ = self._record(closed=True)
        env = {
            registry_api.URL_ENV: "https://registry.example.invalid",
            registry_api.KEY_VAR: "test-key-not-a-credential",
        }
        with mock.patch.object(registry_api, "call") as api_call:
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                plan_code = OR.main(
                    ["plan-session-snapshot", "--db", self.db, "--record-id", record_id],
                    env=env,
                )
            plan = json.loads(output.getvalue())
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                write_code = OR.main(
                    [
                        "enqueue-session-snapshot",
                        "--db",
                        self.db,
                        "--record-id",
                        record_id,
                        "--expected-sha256",
                        str(plan["snapshot_sha256"]),
                    ],
                    env=env,
                )
            self.assertEqual(plan_code, 0)
            self.assertEqual(write_code, 0)
            api_call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
