"""Tests for the near-duplicate guard (fleet_rag.contribute_guard) and its CLI wiring.

    cd scripts && python3 -m unittest fleet_rag.tests.test_contribute_guard -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from fleet_rag import contribute_guard, recall_api
from fleet_rag.core import FleetRagError, build_point
from fleet_rag.recall_api import FakeQdrant

CLI = pathlib.Path(__file__).resolve().parents[2] / "recall"
SEAMS = ("load_config", "embed", "embedder_healthy", "rerank_healthy", "Qdrant", "gitleaks_flagged",
         "gitleaks_available")
EXISTING = ("pm2 start does not re-read env from the ecosystem file; restart with --update-env so "
            "the cached PATH is replaced.")


def load_cli():
    loader = importlib.machinery.SourceFileLoader("recall_cli_guard_test", str(CLI))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def add_contrib(text: str, **payload) -> dict:
    base = {"source": "agent-contribution", "app": "fleet", "category": "lesson", "seat": "GROK",
            "doc_id": "contrib/GROK/2026-09-01/abcd1234", "chunk_index": 0, "chunk_count": 1, "heading": "",
            "title": "pm2 env cache", "url": "", "path": "", "created_at": 1756684800000,
            "updated_at": 1756684800000, "ingest_run": "contrib-2026-09-01"}
    pt = build_point(text, {**base, **payload})
    FakeQdrant.points.append({"id": pt["id"], "payload": pt["payload"]})
    return pt


class ScoredFake(FakeQdrant):
    """FakeQdrant whose dense score comes from a per-text table (default 0.5)."""

    scores: dict[str, float] = {}

    def search_dense(self, vector, limit=5, flt=None):
        FakeQdrant.calls.append((vector, [], limit, flt))
        out = []
        for p in self.points:
            if not self._matches(p, flt):
                continue
            text = p["payload"].get("text", "")
            out.append({"id": p["id"], "score": self.scores.get(text, 0.5), "payload": p["payload"]})
        out.sort(key=lambda h: -h["score"])
        return out[:limit]


class GuardBase(unittest.TestCase):
    def setUp(self):
        self._saved = {k: getattr(recall_api, k) for k in SEAMS}
        recall_api.install_fake_backend(seed=True)
        ScoredFake.scores = {}
        recall_api.Qdrant = ScoredFake
        self.env = mock.patch.dict(os.environ, {"AGENT_SEAT": "TESTSEAT"})
        self.env.start()
        self.cfg = recall_api.get_config()
        self.q = ScoredFake(self.cfg)

    def tearDown(self):
        self.env.stop()
        for k, v in self._saved.items():
            setattr(recall_api, k, v)
        recall_api.reset_config_cache()


class NearDuplicateTests(GuardBase):
    def test_duplicate_at_or_above_threshold(self):
        add_contrib(EXISTING)
        ScoredFake.scores[EXISTING] = 0.95
        res = contribute_guard.near_duplicate(self.cfg, self.q, "pm2 start ignores env changes; use --update-env.")
        self.assertTrue(res["duplicate"])
        self.assertEqual(res["threshold"], 0.92)
        ex = res["existing"]
        self.assertEqual(ex["doc_id"], "contrib/GROK/2026-09-01/abcd1234")
        self.assertEqual(ex["seat"], "GROK")
        self.assertEqual(ex["created_at"], 1756684800000)
        self.assertEqual(ex["score"], 0.95)
        self.assertEqual(ex["title"], "pm2 env cache")
        self.assertEqual(ex["excerpt"], EXISTING)
        self.assertEqual(len(res["candidates"]), 1)
        ScoredFake.scores[EXISTING] = 0.92
        self.assertTrue(contribute_guard.near_duplicate(self.cfg, self.q, "x" * 50)["duplicate"])

    def test_below_threshold_is_not_duplicate_but_candidates_returned(self):
        add_contrib(EXISTING)
        ScoredFake.scores[EXISTING] = 0.9199
        res = contribute_guard.near_duplicate(self.cfg, self.q, "pm2 start ignores env changes.")
        self.assertFalse(res["duplicate"])
        self.assertIsNone(res["existing"])
        self.assertEqual(res["candidates"][0]["doc_id"], "contrib/GROK/2026-09-01/abcd1234")
        self.assertEqual(res["candidates"][0]["score"], 0.9199)

    def test_only_agent_contributions_are_compared_and_meta_excluded(self):
        # Seed docs (source=doc) score 0.99 but must be filtered out; so must the sentinel.
        for p in FakeQdrant.points:
            ScoredFake.scores[p["payload"]["text"]] = 0.99
        sentinel = build_point("sentinel", {"source": "meta", "app": "fleet", "category": "meta", "seat": "FLEET",
                               "doc_id": "meta/ingest-status", "created_at": 1, "updated_at": 1})
        FakeQdrant.points.append({"id": sentinel["id"], "payload": sentinel["payload"]})
        ScoredFake.scores["sentinel"] = 0.99
        res = contribute_guard.near_duplicate(self.cfg, self.q, "anything at all that is long enough to embed")
        self.assertFalse(res["duplicate"])
        self.assertEqual(res["candidates"], [])
        vector, terms, limit, flt = FakeQdrant.calls[-1]
        self.assertEqual(limit, contribute_guard.LIMIT)
        self.assertIn({"key": "source", "match": {"value": "agent-contribution"}}, flt["must"])
        self.assertEqual(flt["must_not"], [recall_api.meta_exclude(), recall_api.retracted_exclude()])

    def test_excerpt_is_capped_at_200_chars_and_whitespace_collapsed(self):
        long = "word " * 100 + "\n\nend"
        add_contrib(long)
        ScoredFake.scores[long] = 0.99
        res = contribute_guard.near_duplicate(self.cfg, self.q, "words words words words words words words")
        self.assertTrue(res["duplicate"])
        self.assertLessEqual(len(res["existing"]["excerpt"]), 200)
        self.assertTrue(res["existing"]["excerpt"].endswith("..."))
        self.assertNotIn("\n", res["existing"]["excerpt"])

    def test_candidate_is_scrubbed_before_embedding(self):
        seen = []

        def spy_embed(cfg, texts):
            seen.extend(texts)
            return [[0.0] * 4 for _ in texts]

        contribute_guard.near_duplicate(self.cfg, self.q, "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd leaked",
                                        embed=spy_embed)
        self.assertEqual(len(seen), 1)
        self.assertNotIn("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789abcd", seen[0])

    def test_empty_text_rejected_and_threshold_override(self):
        for bad in ("", "   ", None, 5):
            with self.assertRaises(FleetRagError):
                contribute_guard.near_duplicate(self.cfg, self.q, bad)
        add_contrib(EXISTING)
        ScoredFake.scores[EXISTING] = 0.8
        self.assertTrue(contribute_guard.near_duplicate(self.cfg, self.q, "x" * 50, threshold=0.75)["duplicate"])

    def test_duplicate_message(self):
        msg = contribute_guard.duplicate_message({"doc_id": "contrib/GROK/d/1", "score": 0.9512})
        self.assertEqual(msg, "similar lesson already exists: contrib/GROK/d/1 (score 0.95) — use --force to add anyway")
        self.assertIn("abc", contribute_guard.duplicate_message({"id": "abc", "score": 1}))


class CliContributeGuardTests(GuardBase):
    @classmethod
    def setUpClass(cls):
        cls.cli = load_cli()

    def run_cli(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = self.cli.main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_duplicate_refused_exit_1_and_nothing_stored(self):
        add_contrib(EXISTING)
        ScoredFake.scores[EXISTING] = 0.95
        before = len(FakeQdrant.points)
        rc, out, err = self.run_cli(["contribute", "pm2 start ignores env changes; use --update-env to fix.",
                                     "--category", "lesson"])
        self.assertEqual(rc, 1)
        self.assertIn("similar lesson already exists: contrib/GROK/2026-09-01/abcd1234 (score 0.95)", err)
        self.assertIn("--force", err)
        self.assertEqual(out, "")
        self.assertEqual(len(FakeQdrant.points), before)
        self.assertEqual(FakeQdrant.upserts, [])

    def test_duplicate_json_mode(self):
        add_contrib(EXISTING)
        ScoredFake.scores[EXISTING] = 0.93
        rc, out, err = self.run_cli(["contribute", "pm2 start ignores env changes; use --update-env to fix.",
                                     "--category", "lesson", "--json"])
        self.assertEqual(rc, 1)
        d = json.loads(out)
        self.assertEqual(d["status"], "duplicate")
        self.assertEqual(d["existing"]["seat"], "GROK")
        self.assertEqual(d["threshold"], 0.92)
        self.assertIn("--force", d["message"])

    def test_force_stores_anyway(self):
        add_contrib(EXISTING)
        ScoredFake.scores[EXISTING] = 0.99
        rc, out, err = self.run_cli(["contribute", "pm2 start ignores env changes; use --update-env to fix.",
                                     "--category", "lesson", "--force", "--json"])
        self.assertEqual(rc, 0, err)
        d = json.loads(out)
        self.assertTrue(d["doc_id"].startswith("contrib/TESTSEAT/"))
        self.assertNotIn("nearest", d)                 # guard skipped, no candidates reported
        self.assertEqual(len(FakeQdrant.upserts), 1)

    def test_not_duplicate_stores_and_reports_nearest(self):
        add_contrib(EXISTING)
        ScoredFake.scores[EXISTING] = 0.7
        rc, out, err = self.run_cli(["contribute", "Something different enough to be its own lesson.",
                                     "--category", "lesson"])
        self.assertEqual(rc, 0, err)
        self.assertIn("stored contrib/TESTSEAT/", out)
        self.assertIn("nearest : contrib/GROK/2026-09-01/abcd1234 (score 0.70)", out)
        self.assertEqual(len(FakeQdrant.upserts), 1)

    def test_out_of_range_text_skips_guard_and_reports_length(self):
        add_contrib(EXISTING)
        ScoredFake.scores[EXISTING] = 0.99
        rc, out, err = self.run_cli(["contribute", "too short", "--category", "lesson"])
        self.assertEqual(rc, 2)
        self.assertIn("too short", err)
        self.assertEqual(FakeQdrant.calls, [])         # no search was made

    def test_guard_runs_with_read_config_then_write_for_store(self):
        modes = []
        real = recall_api.load_config

        def spy(need_write=False, extra=()):
            modes.append(need_write)
            return real(need_write=need_write, extra=extra)

        recall_api.load_config = spy
        recall_api.reset_config_cache()
        rc, out, err = self.run_cli(["contribute", "A brand new lesson with nothing similar in the corpus.",
                                     "--category", "lesson"])
        self.assertEqual(rc, 0, err)
        self.assertEqual(modes, [False, True])


if __name__ == "__main__":
    unittest.main()
