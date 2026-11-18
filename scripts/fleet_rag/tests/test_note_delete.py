"""Tests for write-time Apple Note deletes from Fleet RAG (AFC-728)."""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from fleet_rag import ingest, note_delete, note_upsert
from fleet_rag.core import FleetRagError
from fleet_rag.tests.test_ingest import FakeQdrant, LONG_A


class NoteDeleteTests(unittest.TestCase):
    def test_delete_removes_state_and_qdrant_points(self) -> None:
        raw_id = "x-coredata://ABC/ICNote/p7"
        doc_id = note_upsert.note_doc_id(raw_id)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = pathlib.Path(tmp) / "ingest-state.json"
            state_path.write_text(
                json.dumps(
                    {
                        doc_id: {"doc_hash": "abc", "chunk_ids": ["c1", "c2"], "source": "apple-note"},
                        "note/OTHER/ICNote/p1": {"chunk_ids": ["c2", "c3"], "source": "apple-note"},
                    }
                ),
                encoding="utf-8",
            )
            qd = FakeQdrant()
            with (
                mock.patch.object(note_delete, "STATE_PATH", state_path),
                mock.patch.object(note_delete.core, "load_config", return_value={"x": "y"}),
                mock.patch.object(note_delete.core, "Qdrant", return_value=qd),
            ):
                result = note_delete.delete_written_note(raw_id)
            self.assertTrue(result.get("ok"))
            self.assertEqual(result.get("chunks_deleted"), 1)
            st = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertNotIn(doc_id, st)
            self.assertEqual(qd.deleted, ["c1"])

    def test_delete_is_idempotent_without_state_row(self) -> None:
        with (
            mock.patch.object(note_delete.core, "load_config", return_value={"x": "y"}),
            mock.patch.object(note_delete.core, "Qdrant", return_value=FakeQdrant()),
        ):
            result = note_delete.delete_written_note("x-coredata://ABC/ICNote/p9")
        self.assertTrue(result.get("skipped"))

    def test_delete_failure_does_not_raise(self) -> None:
        with mock.patch.object(
            note_delete.core, "load_config", side_effect=FleetRagError("missing QDRANT_URL")
        ):
            result = note_delete.delete_written_note("x-coredata://ABC/ICNote/p3")
        self.assertFalse(result.get("ok"))


class ToolCreatedNoteTests(unittest.TestCase):
    def test_bracket_title_with_session_row(self) -> None:
        from simple_tools import notes as N

        title = "[AFC, Cursor] Smoke"
        body = "<p>Mon, Nov 10, 3:05pm</p><p><span>Session: abc12345-def6</span></p><p>Body.</p>"
        self.assertTrue(N.is_tool_created_note(title, body))

    def test_static_note_refused(self) -> None:
        from simple_tools import notes as N

        self.assertFalse(
            N.is_tool_created_note("⭐️ Background Jobs Master List", "<p>Process list</p>", "Process list")
        )

    def test_marker_html(self) -> None:
        from simple_tools import notes as N

        self.assertTrue(
            N.is_tool_created_note("Manual title", "<p>x</p><!-- fleet-notes-tool:v1 -->", "")
        )
