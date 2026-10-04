"""Unit tests for fleet_rag.search_log and retrieval telemetry."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from fleet_rag import doctor, search_log


class SearchLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="fleet-rag-search-test-")
        self.state_dir = os.path.join(self.tmp, "state")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_log_search_appends_and_scrubs(self) -> None:
        sample_token = "ghp_" + "a" * 36
        search_log.log_search(
            query=f"how do I fix token {sample_token}?",
            seat="CLAUDE",
            app="socratic-trade",
            hits_count=5,
            latency_ms=120,
            mode="hybrid+rerank",
            state_dir=self.state_dir,
            now_ms=1000000,
        )

        log_file = search_log.log_path(self.state_dir)
        self.assertTrue(os.path.exists(log_file))

        with open(log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 1)
        rec = json.loads(lines[0])

        self.assertEqual(rec["seat"], "CLAUDE")
        self.assertEqual(rec["app"], "socratic-trade")
        self.assertEqual(rec["hits"], 5)
        self.assertEqual(rec["latency_ms"], 120)
        self.assertEqual(rec["mode"], "hybrid+rerank")
        self.assertEqual(rec["timestamp"], 1000000)
        # Gitleaks / regex scrub check: token should be scrubbed
        self.assertNotIn(sample_token, rec["query"])

    def test_search_summary_and_lookback(self) -> None:
        now = 10 * search_log.DAY_MS
        # 1 day ago (inside 7d window)
        search_log.log_search("query 1", seat="CLAUDE", app="fleet", hits_count=3, latency_ms=100,
                              state_dir=self.state_dir, now_ms=now - search_log.DAY_MS)
        search_log.log_search("query 2", seat="CLAUDE", app="fleet", hits_count=4, latency_ms=200,
                              state_dir=self.state_dir, now_ms=now - search_log.DAY_MS)
        search_log.log_search("query 3", seat="CODEX", app="botfleet", hits_count=2, latency_ms=300,
                              state_dir=self.state_dir, now_ms=now - 2 * search_log.DAY_MS)
        # 9 days ago (outside 7d window)
        search_log.log_search("query old", seat="CLAUDE", app="fleet", hits_count=5, latency_ms=150,
                              state_dir=self.state_dir, now_ms=now - 9 * search_log.DAY_MS)

        summary = search_log.search_summary(state_dir=self.state_dir, days=7, now_ms=now)
        self.assertEqual(summary["total_searches"], 3)
        self.assertEqual(summary["by_seat"], {"CLAUDE": 2, "CODEX": 1})
        self.assertEqual(summary["by_app"], {"fleet": 2, "botfleet": 1})
        self.assertEqual(summary["avg_latency_ms"], 200.0)

    def test_compute_ratios_and_warning(self) -> None:
        contribs = {
            "CLAUDE": 5,
            "MINIMAX": 4,
            "CODEX": 1,
            "AG": 0,
        }
        searches = {
            "CLAUDE": 25,  # 5.0x ratio -> healthy
            "MINIMAX": 2,   # 0.5x ratio (< 2.0x with >=2 contribs) -> WARN
            "CODEX": 0,    # 0.0x ratio but only 1 contrib -> no warning threshold
            "AG": 10,      # searches with 0 contribs -> healthy ratio=None
        }

        ratios = search_log.compute_ratios(contribs, searches)
        by_seat = {r["seat"]: r for r in ratios}

        self.assertFalse(by_seat["CLAUDE"]["warning"])
        self.assertEqual(by_seat["CLAUDE"]["ratio"], 5.0)

        self.assertTrue(by_seat["MINIMAX"]["warning"])
        self.assertEqual(by_seat["MINIMAX"]["ratio"], 0.5)

        self.assertFalse(by_seat["CODEX"]["warning"])
        self.assertEqual(by_seat["CODEX"]["ratio"], 0.0)

        self.assertFalse(by_seat["AG"]["warning"])
        self.assertIsNone(by_seat["AG"]["ratio"])

    def test_format_digest_with_searches(self) -> None:
        digest_data = {
            "days": 7,
            "app": None,
            "total": 1,
            "apps": {
                "fleet": {
                    "lesson": [
                        {
                            "date": "2026-10-03",
                            "seat": "CLAUDE",
                            "title": "A sample lesson",
                            "doc_id": "contrib/CLAUDE/1",
                            "url": "",
                        }
                    ]
                }
            },
            "searches": {
                "total_searches": 15,
                "by_seat": {"CLAUDE": 12, "MINIMAX": 3},
                "avg_latency_ms": 150.5,
            },
            "ratios": [
                {
                    "seat": "MINIMAX",
                    "searches": 3,
                    "contributions": 4,
                    "ratio": 0.75,
                    "warning": True,
                },
                {
                    "seat": "CLAUDE",
                    "searches": 12,
                    "contributions": 1,
                    "ratio": 12.0,
                    "warning": False,
                },
            ],
        }

        formatted = doctor.format_digest(digest_data)
        self.assertIn("=== Fleet Retrieval Telemetry (7d) ===", formatted)
        self.assertIn("Total searches: 15 across 2 seat(s)", formatted)
        self.assertIn("[WARN: low retrieval ratio - search before diagnosing]", formatted)
        self.assertIn("12.00x", formatted)


if __name__ == "__main__":
    unittest.main()
