"""Any spelling of GROK followed by BUILD is the seat GROK (owner 2026-10-10), in what recall stores.

    cd scripts && python3 -m unittest fleet_rag.tests.test_seat_alias -v

The contribute path and the search log sign GROK;  the recall_search seat FILTER does not map, so a
legacy GROK-BUILD contribution stays findable.  search_log.py carries its own copy of the pattern
(the recall service ships in a container without the agent_sync package), and the parity test keeps
it equal to the GROK row's aliasPattern in fleet-apps.json.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import tempfile
import unittest
from unittest import mock

from fleet_rag import recall_api, search_log
from fleet_rag.core import FleetRagError
from fleet_rag.recall_api import FakeQdrant
from fleet_rag.tests.test_recall import GOOD, RecallBase

REPO = pathlib.Path(__file__).resolve().parents[3]

ALIAS_SPELLINGS = ("GROK-BUILD", "Grok-Build", "grok-build", "grok_build", "Grok Build", "GROK.BUILD",
                   "Grok (Build)", "grok/build/", "GrokBuild", "GROK--BUILD!!", "  grok-build  ")
NOT_ALIASES = ("GROKBUILDER", "grok-build-bot", "grok2build", "GROK-WEB", "GROK-BOT", "GB-COMPILER",
               "BF-BUILDER", "Grok (Web/iOS)", "GROK", "", "MONET", "RENOIR", "HARNESS", "MINIMAX",
               "DEEPSEEK", "KIMI")


class CanonicalSeatTests(unittest.TestCase):
    def test_every_grok_build_spelling_is_grok(self):
        for raw in ALIAS_SPELLINGS:
            self.assertEqual(search_log.canonical_seat(raw), "GROK", repr(raw))

    def test_near_misses_and_other_retired_tags_only_upper_case(self):
        for raw in NOT_ALIASES:
            self.assertEqual(search_log.canonical_seat(raw), raw.strip().upper(), repr(raw))
        self.assertEqual(search_log.canonical_seat(None), "")

    def test_pattern_is_the_one_in_fleet_apps_json(self):
        rows = json.loads((REPO / "fleet-apps.json").read_text())["seats"]
        grok = next(r for r in rows if r["tag"] == "GROK")
        self.assertEqual(search_log.GROK_ALIAS_PATTERN, grok["aliasPattern"])


class ContributeSignsGrokTests(RecallBase):
    def test_grok_build_spellings_contribute_as_grok(self):
        for raw in ("GROK-BUILD", "Grok Build", "grok_build", "Grok (Build)"):
            FakeQdrant.upserts.clear()
            res = recall_api.recall_contribute(GOOD, "lesson", seat=raw)
            self.assertTrue(res["doc_id"].startswith("contrib/GROK/"), (raw, res["doc_id"]))
            (point,), = FakeQdrant.upserts
            self.assertEqual(point["payload"]["seat"], "GROK", raw)

    def test_agent_seat_env_is_mapped_too(self):
        os.environ["AGENT_SEAT"] = "Grok-Build"
        res = recall_api.recall_contribute(GOOD, "lesson")
        self.assertTrue(res["doc_id"].startswith("contrib/GROK/"))

    def test_near_misses_are_kept_and_other_checks_still_apply(self):
        res = recall_api.recall_contribute(GOOD, "lesson", seat="grok-build-bot")
        self.assertTrue(res["doc_id"].startswith("contrib/GROK-BUILD-BOT/"))
        res = recall_api.recall_contribute(GOOD, "lesson", seat="GROK-WEB")
        self.assertTrue(res["doc_id"].startswith("contrib/GROK-WEB/"))
        with self.assertRaisesRegex(FleetRagError, "seat must be"):
            recall_api.recall_contribute(GOOD, "lesson", seat="GROK BUILDER!")


class SearchLogSignsGrokTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-rag-seat-alias-")
        self.state_dir = os.path.join(self.tmp, "state")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def last_seat(self):
        with open(search_log.log_path(self.state_dir), encoding="utf-8") as f:
            return json.loads(f.readlines()[-1])["seat"]

    def test_log_search_stores_grok_for_every_spelling(self):
        for raw in ("GROK-BUILD", "Grok (Build)", "grok_build"):
            search_log.log_search("q", seat=raw, state_dir=self.state_dir, now_ms=1)
            self.assertEqual(self.last_seat(), "GROK", raw)

    def test_log_search_env_fallback_and_near_miss(self):
        with mock.patch.dict(os.environ, {"AGENT_SEAT": "Grok-Build"}):
            search_log.log_search("q", state_dir=self.state_dir, now_ms=1)
        self.assertEqual(self.last_seat(), "GROK")
        search_log.log_search("q", seat="GROKBUILDER", state_dir=self.state_dir, now_ms=1)
        self.assertEqual(self.last_seat(), "GROKBUILDER")
        with mock.patch.dict(os.environ, {}, clear=True):
            search_log.log_search("q", state_dir=self.state_dir, now_ms=1)
        self.assertEqual(self.last_seat(), "UNKNOWN")


class SearchFilterIsNotMappedTests(RecallBase):
    def test_seat_filter_keeps_the_legacy_tag(self):
        """Mapping the filter would hide contributions already stored as GROK-BUILD."""
        with mock.patch.object(search_log, "log_search") as logged:
            recall_api.recall_search("anything", seat="grok-build")
        _vector, _terms, _limit, flt = FakeQdrant.calls[-1]
        self.assertIn({"key": "seat", "match": {"value": "GROK-BUILD"}}, flt["must"])
        self.assertEqual(logged.call_args.kwargs["seat"], "GROK-BUILD")   # log_search maps it when it stores


if __name__ == "__main__":
    unittest.main()
