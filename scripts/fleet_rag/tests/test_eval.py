"""Unit tests for fleet_rag.eval: per-source recall, expect_source, --compare, CLI flags.

    cd scripts && python3 -m unittest fleet_rag.tests.test_eval -v
"""
from __future__ import annotations

import io
import json
import pathlib
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

from fleet_rag import eval as ev
from fleet_rag import recall_api
from fleet_rag.core import FleetRagError

ROWS = [
    {"query": "q-doc", "expect_doc_id_prefix": "doc/a/x.md", "expect_text_contains": ["alpha"]},
    {"query": "q-board", "expect_doc_id_prefix": "board/abc123"},
    {"query": "q-note", "expect_doc_id_prefix": "note/ACC/ICNote/p1"},
    {"query": "q-contrib", "expect_doc_id_prefix": "contrib/GROK/2026-09-02/deadbeef"},
    {"query": "q-source", "expect_source": "agent-contribution", "expect_text_contains": ["lesson"]},
    {"query": "q-any", "expect_text_contains": ["two spaces"]},
]


def hit(doc_id: str, text: str, source: str = "doc") -> dict:
    return {"doc_id": doc_id, "text": text, "source": source}


ANSWERS = {
    # rank 1
    "q-doc": [hit("doc/a/x.md", "ALPHA here"), hit("doc/b", "alpha")],
    # rank 2
    "q-board": [hit("board/zzz", "", "board"), hit("board/abc123", "", "board")],
    # miss
    "q-note": [hit("note/ACC/ICNote/p2", "", "apple-note")],
    # rank 1
    "q-contrib": [hit("contrib/GROK/2026-09-02/deadbeef", "x", "agent-contribution")],
    # source must match: the doc hit with the needle does not count, the contribution at 3 does
    "q-source": [hit("doc/x", "a lesson"), hit("doc/y", "lesson"),
                 hit("contrib/CLAUDE/2026-09-02/1", "the lesson", "agent-contribution")],
    # rank 1
    "q-any": [hit("doc/z", "Two spaces between sentences")],
}


def fake_search(query, limit=5, **kwargs):
    return {"hits": ANSWERS[query][:limit], "mode": "hybrid+rerank" if kwargs.get("rerank", True) else "hybrid"}


class GoldenFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.golden = pathlib.Path(self.tmp.name) / "g.jsonl"
        self.golden.write_text("".join(json.dumps(r) + "\n" for r in ROWS))

    def tearDown(self):
        self.tmp.cleanup()


class ExpectSourceTests(unittest.TestCase):
    def test_expected_source_from_field_prefix_or_any(self):
        self.assertEqual(ev.expected_source({"expect_source": "board", "expect_doc_id_prefix": "doc/x"}), "board")
        self.assertEqual(ev.expected_source({"expect_doc_id_prefix": "doc/AI-Fleet-Coordinator/x.md"}), "doc")
        self.assertEqual(ev.expected_source({"expect_doc_id_prefix": "board/abc"}), "board")
        self.assertEqual(ev.expected_source({"expect_doc_id_prefix": "note/ACC/ICNote/p1"}), "apple-note")
        self.assertEqual(ev.expected_source({"expect_doc_id_prefix": "contrib/GROK/2026-09-02/x"}), "agent-contribution")
        self.assertEqual(ev.expected_source({"expect_doc_id_prefix": "effort-log/ST"}), "effort-log")
        self.assertEqual(ev.expected_source({"expect_text_contains": ["x"]}), "any")

    def test_matches_requires_source_when_present(self):
        row = {"expect_text_contains": ["lesson"], "expect_source": "agent-contribution"}
        self.assertFalse(ev.matches(row, hit("doc/x", "a lesson")))
        self.assertTrue(ev.matches(row, hit("contrib/x/y/z", "a lesson", "agent-contribution")))
        # source alone is never enough (load_golden rejects such rows; matches agrees)
        self.assertFalse(ev.matches({"expect_source": "doc"}, hit("doc/x", "anything")))

    def test_load_golden_validates_expect_source_type(self):
        with tempfile.TemporaryDirectory() as d:
            g = pathlib.Path(d) / "g.jsonl"
            g.write_text(json.dumps({"query": "q", "expect_text_contains": ["x"], "expect_source": 3}) + "\n")
            with self.assertRaisesRegex(FleetRagError, "expect_source"):
                ev.load_golden(g)


class RunEvalTests(GoldenFile):
    def test_scores_by_source_modes_and_ranks(self):
        res = ev.run_eval(k=3, golden=self.golden, search=fake_search)
        self.assertEqual(res["n"], 6)
        self.assertEqual(res["ranks"], [1, 2, None, 1, 3, 1])
        self.assertAlmostEqual(res["recall_at_1"], 3 / 6)
        self.assertAlmostEqual(res["recall_at_k"], 5 / 6)
        self.assertAlmostEqual(res["mrr"], (1 + 0.5 + 0 + 1 + 1 / 3 + 1) / 6)
        self.assertEqual(res["modes"], {"hybrid+rerank": 6})
        by = res["by_source"]
        self.assertEqual(set(by), {"doc", "board", "apple-note", "agent-contribution", "any"})
        self.assertEqual(by["apple-note"], {"n": 1, "recall_at_1": 0.0, "recall_at_k": 0.0, "mrr": 0.0})
        self.assertEqual(by["agent-contribution"]["n"], 2)
        self.assertAlmostEqual(by["agent-contribution"]["recall_at_1"], 0.5)
        self.assertAlmostEqual(by["agent-contribution"]["mrr"], (1 + 1 / 3) / 2)
        self.assertEqual([m["query"] for m in res["misses"]], ["q-note"])
        text = ev.render(res)
        self.assertIn("modes: hybrid+rerank=6", text)
        self.assertIn("apple-note", text)
        self.assertIn("MISS  q-note", text)

    def test_search_kwargs_are_forwarded_only_when_given(self):
        seen = []

        def spy(query, limit, **kw):
            seen.append(kw)
            return {"hits": [], "mode": "hybrid"}

        ev.run_eval(k=2, golden=self.golden, search=spy)
        self.assertTrue(all(kw == {} for kw in seen))
        ev.run_eval(k=2, golden=self.golden, search=spy, search_kwargs={"rerank": False})
        self.assertEqual(seen[-1], {"rerank": False})
        # a plain two-argument callable (older callers) still works when no kwargs are given
        ev.run_eval(k=2, golden=self.golden, search=lambda q, limit: {"hits": []})


class CompareTests(GoldenFile):
    def test_compare_reports_deltas_and_rank_changes(self):
        def search(query, limit=5, prefer_lessons=True, rerank=True):
            hits = list(ANSWERS[query])
            if query == "q-board" and rerank:
                hits.reverse()                     # rerank fixes the board row: 2 -> 1
            if query == "q-note" and prefer_lessons:
                hits = [hit("note/ACC/ICNote/p1", "", "apple-note")] + hits   # lessons fix the miss
            return {"hits": hits[:limit], "mode": "hybrid"}

        res = ev.run_compare(k=3, golden=self.golden, search=search)
        self.assertEqual(res["n"], 6)
        self.assertEqual(set(res["runs"]), {"off/off", "+lessons", "+rerank", "both"})
        base = res["runs"]["off/off"]
        self.assertEqual(base["ranks"], [1, 2, None, 1, 3, 1])
        self.assertEqual(res["runs"]["+lessons"]["ranks"], [1, 2, 1, 1, 3, 1])
        self.assertEqual(res["runs"]["+rerank"]["ranks"], [1, 1, None, 1, 3, 1])
        self.assertEqual(res["runs"]["both"]["ranks"], [1, 1, 1, 1, 3, 1])
        self.assertEqual(res["deltas"]["off/off"], {"recall_at_1": 0.0, "recall_at_k": 0.0, "mrr": 0.0})
        self.assertAlmostEqual(res["deltas"]["both"]["recall_at_1"], 2 / 6)
        self.assertAlmostEqual(res["deltas"]["both"]["recall_at_k"], 1 / 6)
        self.assertAlmostEqual(res["deltas"]["+rerank"]["mrr"], 0.5 / 6)
        self.assertEqual([c["query"] for c in res["changes"]], ["q-board", "q-note"])
        text = ev.render_compare(res)
        self.assertIn("delta vs off/off", text)
        self.assertIn("+lessons", text)
        self.assertIn("rank changes", text)
        self.assertIn(" - ->  1 /  - /  1   q-note", text)

    def test_compare_without_changes_says_so(self):
        res = ev.run_compare(k=3, golden=self.golden, search=fake_search)
        self.assertEqual(res["changes"], [])
        self.assertIn("no per-query rank changes", ev.render_compare(res))


class MainTests(GoldenFile):
    def _run(self, argv):
        out = io.StringIO()
        with mock.patch.object(recall_api, "recall_search", fake_search), redirect_stdout(out):
            rc = ev.main(argv)
        return rc, out.getvalue()

    def test_flags_reach_recall_search(self):
        seen = []

        def spy(query, limit=5, **kw):
            seen.append(dict(kw))
            return fake_search(query, limit, **kw)

        with mock.patch.object(recall_api, "recall_search", spy), redirect_stdout(io.StringIO()):
            ev.main(["--golden", str(self.golden), "--k", "3"])
            self.assertEqual(seen[-1], {})
            ev.main(["--golden", str(self.golden), "--k", "3", "--no-rerank", "--no-lessons"])
            self.assertEqual(seen[-1], {"prefer_lessons": False, "rerank": False})
            seen.clear()
            ev.main(["--golden", str(self.golden), "--k", "3", "--compare"])
        self.assertEqual(len(seen), 4 * len(ROWS))
        self.assertIn({"prefer_lessons": True, "rerank": True}, seen)
        self.assertIn({"prefer_lessons": False, "rerank": False}, seen)

    def test_exit_codes_and_json(self):
        rc, out = self._run(["--golden", str(self.golden), "--k", "3", "--threshold", "0.8"])
        self.assertEqual(rc, 0)
        self.assertIn("Recall@1 0.50", out)
        rc, out = self._run(["--golden", str(self.golden), "--k", "3", "--threshold", "0.9"])
        self.assertEqual(rc, 1)
        rc, out = self._run(["--golden", str(self.golden), "--k", "3", "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertIn("by_source", data)
        self.assertEqual(data["modes"], {"hybrid+rerank": 6})
        rc, out = self._run(["--golden", str(self.golden), "--k", "3", "--compare", "--json"])
        self.assertEqual(rc, 0)
        self.assertIn("deltas", json.loads(out))

    def test_bad_golden_is_exit_2(self):
        self.golden.write_text('{"query": "q"}\n')
        rc, _ = self._run(["--golden", str(self.golden)])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
