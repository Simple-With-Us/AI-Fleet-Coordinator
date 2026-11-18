"""Compact recall_search presentation (token-thrift defaults)."""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from fleet_rag import recall_presentation as presentation
from fleet_rag.core import FleetRagError


def _long_hit(doc_id: str, score: float, chunk_index: int = 0) -> dict:
    return {
        "score": score,
        "text": ("lesson " * 400) + doc_id,
        "title": "title " + doc_id,
        "doc_id": doc_id,
        "url": f"https://example.test/{doc_id}",
        "source": "doc",
        "app": "fleet",
        "category": "lesson",
        "chunk_index": chunk_index,
    }


class CompactPresentationTests(unittest.TestCase):
    def test_default_limit_when_omitted(self) -> None:
        args, pres = presentation.split_search_arguments({"query": "pm2"})
        self.assertEqual(3, args["limit"])
        self.assertFalse(pres.full)

    def test_explicit_limit_is_preserved(self) -> None:
        args, _ = presentation.split_search_arguments({"query": "x", "limit": 7})
        self.assertEqual(7, args["limit"])

    def test_collapses_duplicate_doc_and_truncates_text(self) -> None:
        raw = {
            "mode": "hybrid",
            "hits": [
                _long_hit("doc/a", 0.9, 0),
                _long_hit("doc/a", 0.8, 1),
                _long_hit("doc/b", 0.7, 0),
            ],
        }
        compact = presentation.present_search_result(raw, presentation.SearchPresentation())
        self.assertEqual(2, len(compact["hits"]))
        self.assertEqual("doc/a", compact["hits"][0]["doc_id"])
        text = compact["hits"][0]["text"]
        self.assertLess(len(text), len(raw["hits"][0]["text"]))
        self.assertTrue(compact["hits"][0].get("text_truncated"))

    def test_full_returns_raw_payload(self) -> None:
        raw = {"mode": "dense", "hits": [_long_hit("doc/z", 0.5)]}
        full = presentation.present_search_result(raw, presentation.SearchPresentation(full=True))
        self.assertEqual(raw, full)

    def test_fields_selector(self) -> None:
        raw = {"mode": "dense", "hits": [_long_hit("doc/z", 0.5)]}
        compact = presentation.present_search_result(
            raw,
            presentation.SearchPresentation(fields=("doc_id", "score")),
        )
        hit = compact["hits"][0]
        self.assertEqual({"doc_id": "doc/z", "score": 0.5}, hit)

    def test_snippet_env_override(self) -> None:
        with mock.patch.dict(os.environ, {presentation.SNIPPET_ENV: "120"}):
            raw = {"mode": "dense", "hits": [_long_hit("doc/z", 0.5)]}
            compact = presentation.present_search_result(raw, presentation.SearchPresentation())
            self.assertLessEqual(len(compact["hits"][0]["text"]), 123)

    def test_invalid_fields_rejected(self) -> None:
        with self.assertRaises(FleetRagError):
            presentation.parse_fields(["not-a-field"])

    def test_token_estimate_drops_for_compact(self) -> None:
        raw = {
            "mode": "hybrid",
            "hits": [_long_hit(f"doc/{i}", 0.9 - i * 0.01, i % 2) for i in range(5)],
        }
        verbose = presentation.present_search_result(
            raw, presentation.SearchPresentation(full=True)
        )
        compact = presentation.present_search_result(raw, presentation.SearchPresentation())
        verbose_t = presentation.approximate_tokens(verbose)
        compact_t = presentation.approximate_tokens(compact)
        self.assertGreater(verbose_t, compact_t)
        ratio = 1 - (compact_t / verbose_t)
        self.assertGreater(ratio, 0.5)

    def test_mcp_envelope_uses_presented_json(self) -> None:
        raw = {"mode": "dense", "hits": [_long_hit("doc/z", 0.5)]}
        compact = presentation.present_search_result(raw, presentation.SearchPresentation())
        wrapped = presentation.mcp_result(compact)
        line = wrapped["content"][0]["text"].splitlines()[2]
        parsed = json.loads(line)
        self.assertEqual(compact, parsed)
        self.assertEqual(compact, wrapped["structuredContent"])
