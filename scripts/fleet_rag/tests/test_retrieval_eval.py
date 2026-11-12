"""Retrieval-quality eval: golden docs, offline BM25 fixture, recall@1/5/10, compare.

    cd scripts && python3 -m unittest fleet_rag.tests.test_retrieval_eval -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from fleet_rag import retrieval_eval as rq
from fleet_rag.chunk import chunk_markdown
from fleet_rag.core import FleetRagError
from fleet_rag.tests._hermetic import HermeticTestCase

REPO = pathlib.Path(__file__).resolve().parents[3]
CLI = pathlib.Path(__file__).resolve().parents[2] / "recall"


def _load_cli():
    loader = importlib.machinery.SourceFileLoader("recall_cli_retrieval_eval_test", str(CLI))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class ScoreTests(unittest.TestCase):
    def test_recall_and_mrr(self):
        scored = rq.score_ranks([1, 5, 10, None])
        self.assertEqual(scored["n"], 4)
        self.assertEqual(scored["recall_at_1"], 0.25)
        self.assertEqual(scored["recall_at_5"], 0.5)
        self.assertEqual(scored["recall_at_10"], 0.75)
        self.assertAlmostEqual(scored["mrr"], (1 + 0.2 + 0.1 + 0) / 4)

    def test_prefix_match_is_the_first_hit(self):
        hits = [
            {"doc_id": "doc/AI-Fleet-Coordinator/README.md"},
            {"doc_id": "doc/AI-Fleet-Coordinator/AGENTS.md"},
        ]
        rank, doc_id = rq.first_rank(hits, ["doc/AI-Fleet-Coordinator/AGENTS.md"])
        self.assertEqual((rank, doc_id), (2, "doc/AI-Fleet-Coordinator/AGENTS.md"))
        self.assertIsNone(rq.first_rank(hits, ["doc/AI-Fleet-Coordinator/docs/NOPE.md"])[0])


class GoldenFileTests(HermeticTestCase):
    def test_shipped_set_is_repo_docs_only(self):
        golden = rq.load_golden()
        queries = golden["queries"]
        self.assertGreaterEqual(len(queries), 25)
        self.assertLessEqual(len(queries), 40)
        ids = [row["id"] for row in queries]
        self.assertEqual(len(ids), len(set(ids)))
        for row in queries:
            for prefix in row["expect_doc_id_prefix"]:
                rel = rq.rel_from_prefix(prefix)
                self.assertTrue((REPO / rel).is_file(), rel)
        for rel in golden["extra_paths"]:
            self.assertTrue((REPO / rel).is_file(), rel)

    def test_unsafe_prefix_is_refused(self):
        with self.assertRaises(FleetRagError):
            rq.rel_from_prefix("doc/AI-Fleet-Coordinator/../../etc/passwd")
        with self.assertRaises(FleetRagError):
            rq.rel_from_prefix("board/secret")

    def test_bad_golden_shapes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "g.json"
            path.write_text("{}", encoding="utf-8")
            with self.assertRaises(FleetRagError):
                rq.load_golden(path)
            path.write_text(json.dumps({"queries": [{"id": "a", "query": "q"}]}), encoding="utf-8")
            with self.assertRaises(FleetRagError):
                rq.load_golden(path)
            path.write_text(
                json.dumps({
                    "queries": [
                        {"id": "a", "query": "q", "expect_doc_id_prefix": ["doc/AI-Fleet-Coordinator/A.md"]},
                        {"id": "a", "query": "q2", "expect_doc_id_prefix": ["doc/AI-Fleet-Coordinator/B.md"]},
                    ]
                }),
                encoding="utf-8",
            )
            with self.assertRaises(FleetRagError):
                rq.load_golden(path)


class OfflineTests(HermeticTestCase):
    def _golden(self, root: pathlib.Path) -> pathlib.Path:
        (root / "docs").mkdir()
        (root / "docs" / "runners.md").write_text(
            "# Runners\n\ntrading-live-mac-ci is permanently banned.\n",
            encoding="utf-8",
        )
        (root / "docs" / "lanes.md").write_text(
            "# Lanes\n\nlayout v2 puts every checkout under apps/lanes.\n",
            encoding="utf-8",
        )
        (root / "README.md").write_text("# Readme\n\nA distractor about nothing in particular.\n", encoding="utf-8")
        path = root / "golden.json"
        path.write_text(
            json.dumps({
                "extra_paths": ["README.md"],
                "queries": [
                    {
                        "id": "runners",
                        "query": "trading-live-mac-ci permanently banned",
                        "expect_doc_id_prefix": ["doc/AI-Fleet-Coordinator/docs/runners.md"],
                    },
                    {
                        "id": "lanes",
                        "query": "layout v2 apps/lanes checkout",
                        "expect_doc_id_prefix": ["doc/AI-Fleet-Coordinator/docs/lanes.md"],
                    },
                ],
            }),
            encoding="utf-8",
        )
        return path

    def test_fixture_ranks_the_named_doc(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            golden = self._golden(root)
            report = rq.run_offline(golden_path=golden, repo=root)
            self.assertEqual(report["mode"], "offline")
            self.assertEqual(report["target_collection"], "QDRANT_FLEET_COLLECTION")
            self.assertEqual(report["recall_at_1"], 1.0)
            self.assertEqual(report["recall_at_10"], 1.0)
            self.assertEqual(report["docs"], 3)
            self.assertGreaterEqual(report["chunks"], 2)
            by_id = {row["id"]: row for row in report["queries"]}
            self.assertEqual(by_id["runners"]["rank"], 1)
            self.assertTrue(by_id["runners"]["hit_doc_id"].endswith("docs/runners.md"))

    def test_source_filter_drops_fixture_docs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            golden = self._golden(root)
            report = rq.run_offline(golden_path=golden, repo=root, source="board")
            self.assertEqual(report["recall_at_10"], 0.0)
            self.assertTrue(all(row["rank"] is None for row in report["queries"]))

    def test_missing_fixture_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            path = root / "golden.json"
            path.write_text(
                json.dumps({
                    "queries": [{
                        "id": "missing",
                        "query": "anything",
                        "expect_doc_id_prefix": ["doc/AI-Fleet-Coordinator/docs/nope.md"],
                    }]
                }),
                encoding="utf-8",
            )
            with self.assertRaises(FleetRagError):
                rq.run_offline(golden_path=path, repo=root)

    def test_shipped_offline_clears_the_ci_gate(self):
        report = rq.run_offline()
        self.assertGreaterEqual(report["recall_at_1"], 0.9)
        self.assertGreaterEqual(report["recall_at_5"], 0.9)
        self.assertGreaterEqual(report["recall_at_10"], 0.9)
        self.assertGreaterEqual(report["mrr"], 0.9)
        self.assertEqual(report["n"], len(rq.load_golden()["queries"]))

    def test_smaller_chunks_still_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            golden = self._golden(root)
            wide = rq.run_offline(golden_path=golden, repo=root, chunk_target=400, chunk_hard_max=600)
            narrow = rq.run_offline(golden_path=golden, repo=root, chunk_target=80, chunk_overlap=10, chunk_hard_max=120)
            self.assertGreaterEqual(narrow["chunks"], wide["chunks"])
            self.assertEqual(narrow["chunk"]["target"], 80)
            diff = rq.compare_runs(wide, narrow)
            self.assertIn("recall_at_1", diff["deltas"])
            self.assertEqual(diff["chunk_before"]["target"], 400)
            self.assertEqual(diff["chunk_after"]["target"], 80)


class CompareAndCliTests(HermeticTestCase):
    def test_compare_reports_a_rank_change(self):
        before = {
            "schema_version": 1,
            "suite": "retrieval-quality",
            "mode": "offline",
            "n": 1,
            "recall_at_1": 1.0,
            "recall_at_5": 1.0,
            "recall_at_10": 1.0,
            "mrr": 1.0,
            "chunk": {"target": 1600, "overlap": 200, "hard_max": 2400},
            "filters": {"source": None},
            "queries": [{"id": "runners", "query": "q", "rank": 1, "hit_doc_id": "doc/a"}],
        }
        after = json.loads(json.dumps(before))
        after["recall_at_1"] = 0.0
        after["mrr"] = 0.0
        after["queries"][0]["rank"] = None
        after["queries"][0]["hit_doc_id"] = None
        diff = rq.compare_runs(before, after)
        self.assertAlmostEqual(diff["deltas"]["recall_at_1"], -1.0)
        self.assertEqual(diff["changes"][0]["id"], "runners")
        text = rq.render_compare(diff)
        self.assertIn("recall@1", text)
        self.assertIn("runners", text)
        same = rq.compare_runs(before, before)
        self.assertEqual(same["changes"], [])
        self.assertIn("no per-query rank changes", rq.render_compare(same))

    def test_module_cli_offline_json_and_compare(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            out = root / "run.json"
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = rq.main(["--offline", "--json", "--out", str(out), "--threshold", "0.9"])
            self.assertEqual(rc, 0)
            report = json.loads(buf.getvalue())
            self.assertEqual(report["mode"], "offline")
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["n"], report["n"])
            other = root / "other.json"
            other.write_text(out.read_text(encoding="utf-8"), encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = rq.main(["--compare", str(out), str(other)])
            self.assertEqual(rc, 0)
            self.assertIn("no per-query rank changes", buf.getvalue())
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = rq.main(["--offline", "--threshold", "1.01"])
            self.assertEqual(rc, 1)
            self.assertIn("recall@10", buf.getvalue())

    def test_live_search_is_scored_without_qdrant(self):
        def fake(query, limit=10, **kwargs):
            golden = rq.load_golden()
            row = next(item for item in golden["queries"] if item["query"] == query)
            return {
                "hits": [{"doc_id": row["expect_doc_id_prefix"][0], "text": "x", "source": "doc"}],
                "mode": "hybrid" if kwargs.get("rerank", True) else "dense",
            }

        with mock.patch.object(rq, "recall_search", fake):
            report = rq.run_live(source="doc", rerank=False, prefer_lessons=False)
        self.assertEqual(report["mode"], "live")
        self.assertEqual(report["recall_at_1"], 1.0)
        self.assertEqual(report["filters"]["source"], "doc")
        self.assertFalse(report["filters"]["rerank"])
        self.assertEqual(report["modes"], {"dense": report["n"]})

    def test_chunk_flags_are_refused_on_a_live_run(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            rc = rq.main(["--chunk-target", "800", "--threshold", "0"])
        self.assertEqual(rc, 2)
        self.assertIn("offline", buf.getvalue())

    def test_abbreviated_threshold_reaches_retrieval_eval(self):
        cli = _load_cli()
        seen: dict[str, float | None] = {}

        def capture(args):
            seen["threshold"] = args.threshold
            return 0

        with mock.patch("fleet_rag.retrieval_eval.run_from_namespace", capture):
            rc = cli.main(["eval", "--offline", "--thresh", "0.5"])
        self.assertEqual(rc, 0)
        self.assertEqual(seen["threshold"], 0.5)
        seen.clear()
        with mock.patch("fleet_rag.retrieval_eval.run_from_namespace", capture):
            rc = cli.main(["eval", "--offline"])
        self.assertEqual(rc, 0)
        self.assertIsNone(seen["threshold"])

    def test_recall_cli_offline_and_saved_compare(self):
        cli = _load_cli()
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / "run.json"
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["eval", "--offline", "--json", "--out", str(out)])
            self.assertEqual(rc, 0, buf.getvalue())
            report = json.loads(buf.getvalue())
            self.assertGreaterEqual(report["recall_at_10"], 0.9)
            other = pathlib.Path(tmp) / "other.json"
            saved = json.loads(out.read_text(encoding="utf-8"))
            saved["queries"][0]["rank"] = None
            saved["recall_at_1"] = 0.0
            other.write_text(json.dumps(saved), encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["eval", "--compare", str(out), str(other)])
            self.assertEqual(rc, 0)
            self.assertIn("rank changes", buf.getvalue())

    def test_default_chunk_size_is_unchanged(self):
        text = "# Title\n\n" + ("alpha " * 500)
        self.assertEqual(chunk_markdown(text), chunk_markdown(text, target=1600, overlap=200, hard_max=2400))
        self.assertGreater(len(chunk_markdown(text, target=200, overlap=20, hard_max=300)), 1)


if __name__ == "__main__":
    unittest.main()
