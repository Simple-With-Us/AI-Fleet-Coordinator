"""Tests for write-time Apple Note upserts into Fleet RAG (AFC-685)."""
from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from fleet_rag import ingest, note_upsert, sources
from fleet_rag.core import FleetRagError, content_hash
from fleet_rag.tests.test_ingest import FakeQdrant, LONG_A


class NoteUpsertTests(unittest.TestCase):
    def test_note_doc_id_strips_coredata_prefix(self) -> None:
        self.assertEqual(
            note_upsert.note_doc_id("x-coredata://F44FD42D/ICNote/p1"),
            "note/F44FD42D/ICNote/p1",
        )

    def test_build_doc_parses_title_and_identity(self) -> None:
        req = {
            "action": "create",
            "title": "[AFC, Cursor] Fleet RAG upsert",
            "markdown": LONG_A,
            "identity": "CURSOR",
        }
        doc = note_upsert.build_doc(req, "x-coredata://ABC/ICNote/p9")
        self.assertIsNotNone(doc)
        assert doc is not None
        self.assertEqual(doc.doc_id, "note/ABC/ICNote/p9")
        self.assertEqual(doc.source, "apple-note")
        self.assertEqual(doc.seat, "CURSOR")
        self.assertEqual(doc.app, "afc")

    def test_upsert_is_idempotent_when_hash_unchanged(self) -> None:
        req = {
            "action": "update",
            "title": "[AFC, Cursor] Same",
            "markdown": LONG_A,
            "identity": "CURSOR",
        }
        doc = note_upsert.build_doc(req, "ABC/ICNote/p2")
        assert doc is not None
        doc_hash = content_hash(doc.text_markdown)
        with tempfile.TemporaryDirectory() as tmp:
            state_path = pathlib.Path(tmp) / "ingest-state.json"
            state_path.write_text(
                json.dumps({doc.doc_id: {"doc_hash": doc_hash, "chunk_ids": ["pt1"], "source": "apple-note"}}),
                encoding="utf-8",
            )
            qd = FakeQdrant()
            with (
                mock.patch.object(note_upsert, "STATE_PATH", state_path),
                mock.patch.object(note_upsert.core, "load_config", return_value={"x": "y"}),
                mock.patch.object(note_upsert.core, "Qdrant", return_value=qd),
                mock.patch.object(ingest, "process_group") as proc,
            ):
                result = note_upsert.upsert_written_note(req, "ABC/ICNote/p2")
            self.assertTrue(result.get("skipped"))
            proc.assert_not_called()

    def test_upsert_failure_does_not_raise(self) -> None:
        req = {"action": "create", "title": "[AFC, Cursor] X", "markdown": LONG_A}
        with mock.patch.object(
            note_upsert.core, "load_config", side_effect=FleetRagError("missing QDRANT_URL")
        ):
            result = note_upsert.upsert_written_note(req, "ABC/ICNote/p3")
        self.assertFalse(result.get("ok"))
        self.assertIn("QDRANT", result.get("error", ""))


class AppleNotePruneTests(unittest.TestCase):
    def test_nightly_sources_exclude_apple_note(self) -> None:
        self.assertNotIn("apple-note", sources.NIGHTLY_SOURCES)

    def test_explicit_apple_note_prune_is_skipped(self) -> None:
        from fleet_rag.tests.test_ingest import IngestTests

        case = IngestTests()
        case.setUp()
        try:
            with mock.patch.dict(
                ingest.GENERATORS, {"apple-note": lambda limit=None: iter([])}
            ):
                report = ingest.run(
                    "apple-note",
                    prune=True,
                    cfg={"x": "y"},
                    qd=case.qd,
                    state_path=case.state,
                    last_run_path=case.last,
                    log_path=case.logf,
                    log=lambda *a: None,
                )
            self.assertTrue(report["ok"])
            self.assertTrue(
                any("write-time upserts" in w for w in report.get("warnings", []))
            )
            self.assertEqual(report["per_source"]["apple-note"]["docs_pruned"], 0)
        finally:
            case.tearDown()
