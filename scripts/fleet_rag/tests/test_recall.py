"""Unit tests for fleet_rag.recall_api, fleet_rag.eval, and the recall CLI against an in-process
fake backend.

    cd scripts && python3 -m unittest fleet_rag.tests.test_recall -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import tempfile
import threading
import time
import unittest
from unittest import mock

from fleet_rag import core, eval as ev
from fleet_rag import public_fallback, recall_api
from fleet_rag.core import FleetRagError, build_point, content_hash, point_id
from fleet_rag.recall_api import FakeQdrant

SEAMS = ("load_config", "embed", "embedder_healthy", "rerank_healthy", "Qdrant", "gitleaks_flagged",
         "gitleaks_available")
META_EXCLUDE = {"key": "source", "match": {"value": "meta"}}
RETRACTED_EXCLUDE = {"key": "retracted", "match": {"value": True}}
DEFAULT_MUST_NOT = [META_EXCLUDE, RETRACTED_EXCLUDE]


def add_fake_point(text: str, **payload) -> dict:
    base = {"source": "doc", "app": "fleet", "category": "doc", "seat": "CLAUDE", "doc_id": "x/y",
            "chunk_index": 0, "chunk_count": 1, "heading": "", "title": "", "url": "", "path": "",
            "created_at": 1756684800000, "updated_at": 1756684800000, "ingest_run": "fake"}
    pt = build_point(text, {**base, **payload})
    FakeQdrant.points.append({"id": pt["id"], "payload": pt["payload"]})
    return pt


class RecallBase(unittest.TestCase):
    def setUp(self):
        # Fresh fakes for every test; the module-level seams are restored afterwards.
        self._saved = {k: getattr(recall_api, k) for k in SEAMS}
        recall_api.install_fake_backend()
        self.env = mock.patch.dict(os.environ, {}, clear=False)
        self.env.start()
        os.environ.pop("AGENT_SEAT", None)

    def tearDown(self):
        self.env.stop()
        for k, v in self._saved.items():
            setattr(recall_api, k, v)
        recall_api.reset_config_cache()


class BuildFilterTests(RecallBase):
    def test_no_filters_still_excludes_meta(self):
        self.assertEqual(recall_api.build_filter(), {"must_not": DEFAULT_MUST_NOT})

    def test_match_fields(self):
        flt = recall_api.build_filter(category="lesson", app="fleet", source="doc", seat="CLAUDE")
        self.assertEqual(flt, {"must_not": DEFAULT_MUST_NOT, "must": [
            {"key": "category", "match": {"value": "lesson"}},
            {"key": "app", "match": {"value": "fleet"}},
            {"key": "source", "match": {"value": "doc"}},
            {"key": "seat", "match": {"value": "CLAUDE"}},
        ]})

    def test_since_days_range_alone(self):
        flt = recall_api.build_filter(since_days=7, now=10_000_000_000)
        self.assertEqual(flt, {"must_not": DEFAULT_MUST_NOT,
                               "must": [{"key": "created_at",
                                         "range": {"gte": 10_000_000_000 - 7 * 86_400_000}}]})

    def test_since_days_combined_with_match(self):
        flt = recall_api.build_filter(app="fleet", since_days=1, now=5_000_000_000)
        self.assertEqual(flt["must"][0], {"key": "app", "match": {"value": "fleet"}})
        self.assertEqual(flt["must"][1], {"key": "created_at", "range": {"gte": 5_000_000_000 - 86_400_000}})
        self.assertEqual(flt["must_not"], DEFAULT_MUST_NOT)

    def test_since_days_must_be_positive_int(self):
        for bad in (0, -3, "7", 1.5, True):
            with self.assertRaises(FleetRagError):
                recall_api.build_filter(since_days=bad)

    def test_meta_exclude_is_a_fresh_dict_each_call(self):
        a = recall_api.build_filter()
        a["must_not"][0]["match"]["value"] = "tampered"
        self.assertEqual(recall_api.build_filter()["must_not"], DEFAULT_MUST_NOT)


class SearchTests(RecallBase):
    def test_hits_have_contract_shape_and_hybrid_mode(self):
        res = recall_api.recall_search("leaking credentials handoff file", limit=3)
        self.assertEqual(res["mode"], "hybrid")
        self.assertTrue(res["hits"])
        hit = res["hits"][0]
        for f in ("score", "text") + recall_api.HIT_FIELDS:
            self.assertIn(f, hit)
        self.assertIn("List names only", hit["text"])
        vector, terms, limit, flt = FakeQdrant.calls[-1]
        self.assertEqual(flt, {"must_not": DEFAULT_MUST_NOT})
        self.assertIn("credentials", terms)
        # Without a rerank endpoint the window is `limit` docs * GROUP_DEPTH points, one fused
        # query, grouped in-process (the server-side groups query is opt-in).
        self.assertEqual(limit, 3 * recall_api.GROUP_DEPTH)
        self.assertEqual(FakeQdrant.group_calls, [])
        self.assertLessEqual(len(res["hits"]), 3)

    def test_filters_and_since_days_reach_qdrant(self):
        before = recall_api.now_ms()
        recall_api.recall_search("vector database", category="infrastructure", app="Fleet",
                                 seat="claude", since_days=30)
        _, _, _, flt = FakeQdrant.calls[-1]
        keys = [c["key"] for c in flt["must"]]
        self.assertEqual(keys, ["category", "app", "seat", "created_at"])
        self.assertEqual(flt["must"][1]["match"]["value"], "fleet")    # app lowercased
        self.assertEqual(flt["must"][2]["match"]["value"], "CLAUDE")   # seat uppercased
        gte = flt["must"][3]["range"]["gte"]
        self.assertTrue(before - 30 * 86_400_000 - 5000 <= gte <= before - 30 * 86_400_000 + 5000)
        self.assertEqual(flt["must_not"], DEFAULT_MUST_NOT)

    def test_stopword_only_query_falls_back_to_dense(self):
        res = recall_api.recall_search("is it", limit=2)
        self.assertEqual(res["mode"], "dense")
        self.assertEqual(FakeQdrant.calls[-1][1], [])
        self.assertEqual(FakeQdrant.calls[-1][3], {"must_not": DEFAULT_MUST_NOT})

    def test_ingest_sentinel_never_returned(self):
        # A source="meta" point whose text matches the query better than anything else.
        add_fake_point("handoff credentials leaking leaking leaking names only", source="meta",
                       app="fleet", doc_id="meta/ingest-run")
        for kwargs in ({}, {"app": "fleet"}, {"since_days": 3650}, {"category": "preference"}):
            res = recall_api.recall_search("handoff credentials leaking names", limit=10, **kwargs)
            self.assertTrue(res["hits"], kwargs)
            self.assertNotIn("meta", [h["source"] for h in res["hits"]], kwargs)
            self.assertNotIn("meta/ingest-run", [h["doc_id"] for h in res["hits"]], kwargs)
            self.assertEqual(FakeQdrant.calls[-1][3]["must_not"], DEFAULT_MUST_NOT, kwargs)
        # Even asking for it by source is refused (meta is not a searchable source).
        with self.assertRaisesRegex(FleetRagError, "source must be one of"):
            recall_api.recall_search("anything", source="meta")

    def test_bad_arguments(self):
        with self.assertRaises(FleetRagError):
            recall_api.recall_search("   ")
        with self.assertRaises(FleetRagError):
            recall_api.recall_search("x", limit=0)
        with self.assertRaises(FleetRagError):
            recall_api.recall_search("x", limit=recall_api.MAX_LIMIT + 1)

    def test_non_string_filters_rejected(self):
        for field in ("category", "app", "source", "seat"):
            for bad in (1, 1.5, True, ["doc"], {"a": 1}):
                with self.assertRaisesRegex(FleetRagError, f"{field} must be a string", msg=(field, bad)):
                    recall_api.recall_search("x", **{field: bad})
        self.assertEqual(FakeQdrant.calls, [])

    def test_category_and_source_enumerated(self):
        with self.assertRaisesRegex(FleetRagError, r"category must be one of .*lesson\|preference.*finding") as cm:
            recall_api.recall_search("x", category="bogus")
        self.assertIn("'bogus'", str(cm.exception))
        with self.assertRaisesRegex(FleetRagError, r"source must be one of .*board\|effort-log.*agent-contribution"):
            recall_api.recall_search("x", source="slack")
        self.assertEqual(FakeQdrant.calls, [])
        # Every listed value is accepted; blank means "no filter".
        for c in recall_api.CATEGORIES:
            recall_api.recall_search("x", category=c)
        for s in recall_api.SOURCES:
            recall_api.recall_search("x", source=s)
        recall_api.recall_search("x", category="", source="  ", app="", seat=None)
        self.assertEqual(FakeQdrant.calls[-1][3], {"must_not": DEFAULT_MUST_NOT})


class StatsTests(RecallBase):
    def test_stats_shape(self):
        res = recall_api.recall_stats()
        self.assertEqual(res["collection"], "fleet-agents-fake")
        self.assertEqual(res["status"], "green")
        self.assertEqual(res["points"], 3)
        self.assertTrue(res["embedder_healthy"])
        self.assertIsNone(res["rerank_healthy"])       # fake config has no TEI_RERANK_* keys
        self.assertEqual(
            list(res["by_source"]),
            [s for s in recall_api.SOURCES if s not in recall_api.RETIRED_SOURCES],
        )
        self.assertEqual(res["by_source"]["doc"], 3)
        self.assertNotIn("board", res["by_source"])
        self.assertNotIn("effort-log", res["by_source"])
        self.assertEqual(res["by_app"], {"fleet": 3, "other": 0})

    def test_by_app_other_bucket_reconciles(self):
        add_fake_point("a point for an app nobody registered", app="new-app")
        add_fake_point("the ingest sentinel", source="meta", app="", doc_id="meta/run")
        add_fake_point("a socratic point", app="socratic-trade")
        res = recall_api.recall_stats()
        self.assertEqual(res["points"], 6)
        self.assertEqual(res["by_app"], {"fleet": 3, "socratic-trade": 1, "other": 2})
        self.assertEqual(sum(res["by_app"].values()), res["points"])
        self.assertEqual(list(res["by_app"])[-1], "other")


GOOD = ("pm2 start does not re-read env from the ecosystem file; restart with --update-env "
        "or delete and start the app again so the cached PATH is replaced.")


class ContributeTests(RecallBase):
    def test_too_short(self):
        with self.assertRaisesRegex(FleetRagError, "too short"):
            recall_api.recall_contribute("short note", "lesson", seat="CLAUDE")

    def test_too_long(self):
        with self.assertRaisesRegex(FleetRagError, "too long"):
            recall_api.recall_contribute("x" * 4001, "lesson", seat="CLAUDE")

    def test_bad_category(self):
        with self.assertRaisesRegex(FleetRagError, "category"):
            recall_api.recall_contribute(GOOD, "finding", seat="CLAUDE")

    def test_missing_seat(self):
        with self.assertRaisesRegex(FleetRagError, "seat"):
            recall_api.recall_contribute(GOOD, "lesson")

    def test_seat_from_env(self):
        os.environ["AGENT_SEAT"] = "grok"
        res = recall_api.recall_contribute(GOOD, "lesson")
        self.assertTrue(res["doc_id"].startswith("contrib/GROK/"))

    def test_bad_app_and_url(self):
        with self.assertRaisesRegex(FleetRagError, "app"):
            recall_api.recall_contribute(GOOD, "lesson", app="Socratic Trade", seat="CLAUDE")
        with self.assertRaisesRegex(FleetRagError, "url"):
            recall_api.recall_contribute(GOOD, "lesson", seat="CLAUDE", url="ftp://x")

    def test_non_string_fields_rejected(self):
        cases = {"category": [1, ["lesson"], None], "app": [1, ["fleet"]], "seat": [7, ["CLAUDE"]],
                 "title": [3, {"t": 1}], "url": [5, ["https://x"]]}
        for field, bads in cases.items():
            for bad in bads:
                kwargs = {"seat": "CLAUDE", field: bad}
                cat = kwargs.pop("category", "lesson")
                with self.assertRaisesRegex(FleetRagError, f"{field} must be", msg=(field, bad)):
                    recall_api.recall_contribute(GOOD, cat, **kwargs)
        self.assertEqual(FakeQdrant.upserts, [])

    def test_success_payload_schema_v2(self):
        res = recall_api.recall_contribute(GOOD, "runbook", app="fleet", seat="CLAUDE",
                                           title="pm2 env cache", url="https://example.com/pr/1")
        self.assertEqual(res["status"], "completed")
        self.assertEqual(res["scrubbed"], [])
        self.assertEqual(res["id"], point_id(content_hash(GOOD)))
        self.assertRegex(res["doc_id"], r"^contrib/CLAUDE/\d{4}-\d{2}-\d{2}/[0-9a-f]{8}$")
        self.assertTrue(res["doc_id"].endswith(content_hash(GOOD)[:8]))
        (point,), = FakeQdrant.upserts
        p = point["payload"]
        self.assertEqual(p["source"], "agent-contribution")
        self.assertEqual(p["category"], "runbook")
        self.assertEqual(p["seat"], "CLAUDE")
        self.assertEqual(p["title"], "pm2 env cache")
        self.assertEqual(p["url"], "https://example.com/pr/1")
        self.assertEqual((p["chunk_index"], p["chunk_count"]), (0, 1))
        self.assertEqual(p["created_at"], p["updated_at"])
        self.assertEqual(p["text"], GOOD)
        self.assertNotIn("scrubbed", p)
        self.assertEqual(len(point["vector"]), 4)
        for f in ("heading", "path", "content_hash", "embed_model", "ingest_run"):
            self.assertIn(f, p)

    def test_scrub_applied_and_reported(self):
        leaky = ("Use GITHUB_TOKEN=ghp_" + "A" * 36 + " for the API call and keep "
                 "SLACK_TOKEN=xoxb-1234567890-abcdefghijk out of the transcript.")
        res = recall_api.recall_contribute(leaky, "lesson", seat="CLAUDE")
        self.assertIn("github-token", res["scrubbed"])
        self.assertIn("slack-token", res["scrubbed"])
        (point,), = FakeQdrant.upserts
        text = point["payload"]["text"]
        self.assertNotIn("ghp_", text)
        self.assertNotIn("xoxb-", text)
        self.assertIn("[REDACTED:", text)
        self.assertEqual(point["payload"]["scrubbed"], res["scrubbed"])
        self.assertEqual(point["id"], point_id(content_hash(text)))  # id is over the SCRUBBED text

    def test_gitleaks_gate_refuses(self):
        recall_api.gitleaks_flagged = lambda path, timeout=300: {1}
        with self.assertRaisesRegex(FleetRagError, "refusing"):
            recall_api.recall_contribute(GOOD, "lesson", seat="CLAUDE")
        self.assertEqual(FakeQdrant.upserts, [])

    def test_gitleaks_gate_fails_closed_when_it_raises(self):
        class GitleaksError(RuntimeError):
            pass

        seen: list[str] = []

        def boom(path, timeout=300):
            seen.append(path)
            raise GitleaksError("gitleaks exited 2: /tmp/x.json: not found")

        recall_api.gitleaks_flagged = boom
        with self.assertRaisesRegex(FleetRagError, r"refusing.*gitleaks.*GitleaksError") as cm:
            recall_api.recall_contribute(GOOD, "lesson", seat="CLAUDE")
        self.assertNotIn("/tmp/x.json", str(cm.exception))   # class only, never the message
        self.assertEqual(FakeQdrant.upserts, [])
        self.assertEqual(len(seen), 1)
        self.assertFalse(os.path.exists(seen[0]))              # our temp jsonl is gone

        # Any other exception class from the gate refuses too.
        def oserr(path, timeout=300):
            raise OSError("disk")

        recall_api.gitleaks_flagged = oserr
        with self.assertRaisesRegex(FleetRagError, r"refusing.*OSError"):
            recall_api.recall_contribute(GOOD, "lesson", seat="CLAUDE")
        self.assertEqual(FakeQdrant.upserts, [])

    def test_temp_jsonl_unlinked_on_success(self):
        seen: list[str] = []

        def record(path, timeout=300):
            seen.append(path)
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as fh:
                self.assertIn(GOOD[:30], fh.read())
            return set()

        recall_api.gitleaks_flagged = record
        res = recall_api.recall_contribute(GOOD, "lesson", seat="CLAUDE")
        self.assertEqual(res["scrubbed"], [])
        self.assertEqual(len(seen), 1)
        self.assertFalse(os.path.exists(seen[0]))

    def test_gitleaks_unavailable_is_reported_not_fatal(self):
        calls: list[str] = []
        recall_api.gitleaks_available = lambda: False
        recall_api.gitleaks_flagged = lambda path, timeout=300: calls.append(path) or set()
        res = recall_api.recall_contribute(GOOD, "lesson", seat="CLAUDE")
        self.assertEqual(res["scrubbed"], ["gitleaks-unavailable"])
        self.assertEqual(calls, [])                                # gitleaks never invoked
        (point,), = FakeQdrant.upserts
        self.assertNotIn("scrubbed", point["payload"])            # marker is not a redaction kind

        FakeQdrant.upserts.clear()
        leaky = "Keep SLACK_TOKEN=xoxb-1234567890-abcdefghijk out of the transcript when you paste logs."
        res = recall_api.recall_contribute(leaky, "lesson", seat="CLAUDE")
        self.assertEqual(res["scrubbed"], ["slack-token", "gitleaks-unavailable"])
        (point,), = FakeQdrant.upserts
        self.assertEqual(point["payload"]["scrubbed"], ["slack-token"])

    def test_contribute_needs_write_config(self):
        calls = []

        def fake_load(need_write=False, extra=()):
            calls.append(need_write)
            if need_write:
                raise FleetRagError("missing credentials: QDRANT_API_KEY")
            return {"QDRANT_URL": "http://fake", "QDRANT_FLEET_COLLECTION": "c", "TEI_URL": "http://f",
                    "TEI_API_KEY": "k", "QDRANT_READONLY_API_KEY": "r"}
        recall_api.load_config = fake_load
        with self.assertRaisesRegex(FleetRagError, "QDRANT_API_KEY"):
            recall_api.recall_contribute(GOOD, "lesson", seat="CLAUDE")
        self.assertEqual(calls, [True])


class CorrectRetractTests(RecallBase):
    def contribute(self, text: str = GOOD, seat: str = "CLAUDE") -> dict:
        recall_api.gitleaks_available = lambda: False
        return recall_api.recall_contribute(text, "lesson", seat=seat)

    def test_correction_preserves_id_doc_and_revision_audit(self):
        original = self.contribute()
        replacement = (
            "pm2 start only applies a new ecosystem env after restart --update-env; "
            "check pm2 describe for the process env before debugging PATH."
        )
        res = recall_api.recall_correct(
            original["doc_id"], replacement, "make the restart command exact", seat="CLAUDE",
            title="pm2 env refresh", url="https://example.com/pr/2", expected_revision=1,
        )
        self.assertEqual(res["status"], "completed")
        self.assertEqual(res["id"], original["id"])
        self.assertEqual(res["doc_id"], original["doc_id"])
        self.assertEqual(res["revision"], 2)
        self.assertEqual(len(FakeQdrant.upserts), 2)
        payload = FakeQdrant.upserts[-1][0]["payload"]
        self.assertEqual(payload["text"], replacement)
        self.assertEqual(payload["title"], "pm2 env refresh")
        self.assertEqual(payload["revision"], 2)
        self.assertEqual(payload["corrected_by"], "CLAUDE")
        self.assertEqual(payload["correction_reason"], "make the restart command exact")
        self.assertEqual(len(payload["revisions"]), 1)
        self.assertEqual(payload["revisions"][0]["text"], GOOD)
        self.assertEqual(payload["revisions"][0]["replaced_by"], "CLAUDE")

    def test_correction_archives_the_original_scrub_audit(self):
        original = self.contribute()
        point = next(p for p in FakeQdrant.points if p["payload"].get("doc_id") == original["doc_id"])
        point["payload"]["scrubbed"] = ["token"]
        replacement = (
            "pm2 start only applies a new ecosystem env after restart --update-env; "
            "check pm2 describe for the process env before debugging PATH."
        )
        recall_api.recall_correct(original["doc_id"], replacement, "replace the scrubbed text", seat="CLAUDE")
        payload = FakeQdrant.upserts[-1][0]["payload"]
        self.assertEqual(payload["revisions"][0]["scrubbed"], ["token"])
        self.assertEqual(payload["scrubbed"], ["gitleaks-unavailable"])

    def test_same_text_correction_is_unchanged_and_authority_is_enforced(self):
        original = self.contribute()
        res = recall_api.recall_correct(
            original["doc_id"], GOOD, "retry after a timeout", seat="CLAUDE",
        )
        self.assertEqual(res["status"], "unchanged")
        self.assertEqual(len(FakeQdrant.upserts), 1)
        with self.assertRaisesRegex(FleetRagError, "owning seat"):
            recall_api.recall_correct(original["doc_id"], "x" * 50, "not mine", seat="GROK")
        with self.assertRaisesRegex(FleetRagError, "expected_revision"):
            recall_api.recall_correct(original["doc_id"], "y" * 50, "stale read", seat="CLAUDE", expected_revision=2)
        self.assertEqual(len(FakeQdrant.upserts), 1)

    def test_recontribute_of_corrected_replacement_is_a_duplicate(self):
        original = self.contribute()
        replacement = (
            "pm2 restart needs --update-env before process env changes take effect; "
            "check pm2 describe after the restart."
        )
        recall_api.recall_correct(original["doc_id"], replacement, "make current", seat="CLAUDE")
        again = self.contribute(replacement)
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["id"], original["id"])
        self.assertEqual(again["existing"]["doc_id"], original["doc_id"])
        self.assertEqual(len(FakeQdrant.upserts), 2)

    def test_revision_history_cap_preserves_history_and_refuses_further_correction(self):
        original = self.contribute()
        point = next(p for p in FakeQdrant.points if p["payload"].get("doc_id") == original["doc_id"])
        history = [{"revision": i + 1, "text": f"old {i}", "content_hash": f"hash-{i}"}
                   for i in range(recall_api.REVISION_HISTORY_LIMIT)]
        point["payload"]["revisions"] = history
        before = [dict(item) for item in history]
        with self.assertRaisesRegex(FleetRagError, "preserved revisions"):
            recall_api.recall_correct(original["doc_id"], "z" * 50, "one correction too many", seat="CLAUDE")
        self.assertEqual(point["payload"]["revisions"], before)
        self.assertEqual(len(FakeQdrant.upserts), 1)

    def test_retraction_during_correction_embedding_wins_and_correction_refuses(self):
        original = self.contribute()
        real_embed = recall_api.embed

        def racing_embed(cfg, texts):
            recall_api.recall_retract(original["doc_id"], "obsolete while replacement embedded", seat="CLAUDE")
            return real_embed(cfg, texts)

        with mock.patch.object(recall_api, "embed", racing_embed):
            with self.assertRaisesRegex(FleetRagError, "retracted"):
                recall_api.recall_correct(original["doc_id"], "z" * 50, "late replacement", seat="CLAUDE")
        point = next(p for p in FakeQdrant.points if p["payload"].get("doc_id") == original["doc_id"])
        self.assertTrue(point["payload"]["retracted"])
        self.assertEqual(point["payload"]["retraction_reason"], "obsolete while replacement embedded")
        self.assertEqual(point["payload"]["text"], GOOD)

    def test_metadata_only_correction_updates_the_same_point_with_audit(self):
        original = self.contribute()
        res = recall_api.recall_correct(
            original["doc_id"], GOOD, "link the runbook", seat="CLAUDE",
            title="pm2 env", url="https://example.com/runbooks/pm2-env", expected_revision=1,
        )
        self.assertEqual(res["status"], "completed")
        self.assertEqual(res["revision"], 2)
        self.assertEqual(res["content_hash"], res["previous_content_hash"])
        self.assertEqual(len(FakeQdrant.upserts), 1)  # no re-embed / replacement point write
        point = next(p for p in FakeQdrant.points if p["payload"].get("doc_id") == original["doc_id"])
        payload = point["payload"]
        self.assertEqual(payload["text"], GOOD)
        self.assertEqual(payload["title"], "pm2 env")
        self.assertEqual(payload["url"], "https://example.com/runbooks/pm2-env")
        self.assertEqual(payload["revision"], 2)
        self.assertEqual(payload["correction_reason"], "link the runbook")
        self.assertEqual(len(payload["revisions"]), 1)
        self.assertEqual(payload["revisions"][0]["text"], GOOD)
        self.assertEqual(payload["revisions"][0]["title"], "")

    def test_recontribute_never_overwrites_retracted_or_corrected_away_point(self):
        original = self.contribute()
        recall_api.recall_retract(original["doc_id"], "obsolete instruction", seat="OWNER")
        res = self.contribute()
        self.assertEqual(res["status"], "duplicate")
        self.assertTrue(res["existing"]["retracted"])
        self.assertEqual(len(FakeQdrant.upserts), 1)
        point = next(p for p in FakeQdrant.points if p["payload"].get("doc_id") == original["doc_id"])
        self.assertTrue(point["payload"]["retracted"])
        self.assertEqual(point["payload"]["retraction_reason"], "obsolete instruction")

        FakeQdrant.reset()
        corrected = self.contribute()
        replacement = (
            "pm2 restart needs --update-env before process env changes take effect; "
            "check pm2 describe after the restart."
        )
        recall_api.recall_correct(corrected["doc_id"], replacement, "make current", seat="CLAUDE")
        again = self.contribute()
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["existing"]["doc_id"], corrected["doc_id"])
        self.assertEqual(len(FakeQdrant.upserts), 2)
        point = next(p for p in FakeQdrant.points if p["payload"].get("doc_id") == corrected["doc_id"])
        self.assertEqual(point["payload"]["text"], replacement)
        self.assertEqual(len(point["payload"]["revisions"]), 1)

    def test_retraction_keeps_audit_excludes_default_search_and_is_idempotent(self):
        original = self.contribute()
        res = recall_api.recall_retract(original["doc_id"], "obsolete instruction", seat="OWNER")
        self.assertEqual(res["status"], "completed")
        self.assertTrue(res["retracted"])
        self.assertEqual(res["retracted_by"], "OWNER")
        point = next(p for p in FakeQdrant.points if p["payload"].get("doc_id") == original["doc_id"])
        self.assertTrue(point["payload"]["retracted"])
        self.assertEqual(point["payload"]["retraction_reason"], "obsolete instruction")
        hits = recall_api.recall_search("pm2 ecosystem env", source="agent-contribution", include_retracted=False)["hits"]
        self.assertNotIn(original["doc_id"], [h["doc_id"] for h in hits])
        audit = recall_api.recall_search("pm2 ecosystem env", source="agent-contribution", include_retracted=True)["hits"]
        hit = next(h for h in audit if h["doc_id"] == original["doc_id"])
        self.assertTrue(hit["retracted"])
        self.assertEqual(hit["retracted_by"], "OWNER")
        again = recall_api.recall_retract(original["doc_id"], "duplicate retry", seat="OWNER")
        self.assertEqual(again["status"], "already_retracted")
        self.assertEqual(again["retraction_reason"], "obsolete instruction")

    def test_retracted_contribution_cannot_be_corrected(self):
        original = self.contribute()
        recall_api.recall_retract(original["doc_id"], "wrong for the current service", seat="CLAUDE")
        with self.assertRaisesRegex(FleetRagError, "retracted"):
            recall_api.recall_correct(original["doc_id"], "z" * 50, "do not resurrect", seat="CLAUDE")

    def test_mutation_input_and_gate_fail_before_lookup(self):
        with self.assertRaisesRegex(FleetRagError, "doc_id must"):
            recall_api.recall_correct("docs/x", GOOD, "reason", seat="CLAUDE")
        with self.assertRaisesRegex(FleetRagError, "reason"):
            recall_api.recall_retract("contrib/CLAUDE/2026-11-05/472fc36e", "no", seat="CLAUDE")
        recall_api.gitleaks_available = lambda: True
        recall_api.gitleaks_flagged = lambda path, timeout=300: {1}
        with self.assertRaisesRegex(FleetRagError, "looks like it contains a secret"):
            recall_api.recall_retract("contrib/CLAUDE/2026-11-05/472fc36e", "safe reason", seat="CLAUDE")
        with self.assertRaisesRegex(FleetRagError, "looks like it contains a secret"):
            recall_api.recall_correct(
                "contrib/CLAUDE/2026-11-05/472fc36e", "x" * 50, "token ghp_" + "a" * 36, seat="CLAUDE"
            )
        self.assertEqual(FakeQdrant.scrolls, [])

    def test_retracted_uses_one_bool_representation_and_initial_revision_is_one(self):
        self.assertEqual(recall_api.retracted_exclude(), {"key": "retracted", "match": {"value": True}})
        self.assertTrue(recall_api._is_retracted({"retracted": True}))
        self.assertFalse(recall_api._is_retracted({"retracted": 1}))

        original = self.contribute()
        audit = recall_api.recall_search("pm2 ecosystem env", source="agent-contribution")["hits"]
        hit = next(h for h in audit if h["doc_id"] == original["doc_id"])
        self.assertEqual(hit["revision"], 1)



class MutationConcurrencyTests(RecallBase):
    def test_one_process_lock_serializes_concurrent_correct_and_retract(self):
        original = recall_api.recall_contribute(GOOD, "lesson", seat="CLAUDE")
        recall_api.gitleaks_available = lambda: False
        state = {"inside": 0, "overlap": False}
        embedding_started = threading.Event()
        allow_embedding = threading.Event()

        class SlowQdrant(FakeQdrant):
            def scroll(self, *args, **kwargs):
                state["inside"] += 1
                state["overlap"] = state["overlap"] or state["inside"] > 1
                try:
                    time.sleep(0.03)
                    yield from super().scroll(*args, **kwargs)
                finally:
                    state["inside"] -= 1

        def slow_embed(cfg, texts):
            embedding_started.set()
            if not allow_embedding.wait(timeout=5):
                raise AssertionError("test did not release the embedding seam")
            return [[0.0] * 4 for _ in texts]

        recall_api.Qdrant = SlowQdrant
        recall_api.embed = slow_embed
        replacement = GOOD + " Check pm2 describe after the restart."
        errors = []

        def correct():
            try:
                recall_api.recall_correct(
                    original["doc_id"], replacement, "add verification", seat="CLAUDE"
                )
            except FleetRagError as exc:
                errors.append(str(exc))

        def retract():
            try:
                recall_api.recall_retract(original["doc_id"], "obsolete in test", seat="CLAUDE")
            except FleetRagError as exc:
                errors.append(str(exc))

        correction = threading.Thread(target=correct)
        correction.start()
        self.assertTrue(embedding_started.wait(timeout=5))
        retraction = threading.Thread(target=retract)
        retraction.start()
        retraction.join(timeout=5)
        self.assertFalse(retraction.is_alive())
        allow_embedding.set()
        correction.join(timeout=5)
        self.assertFalse(correction.is_alive())
        self.assertFalse(state["overlap"])
        self.assertEqual(len(errors), 1)
        self.assertIn("retracted", errors[0])
        point = next(p for p in FakeQdrant.points if p["payload"].get("doc_id") == original["doc_id"])
        self.assertEqual(point["payload"].get("revision", 1), 1)
        self.assertTrue(point["payload"]["retracted"])
        self.assertEqual(point["payload"]["text"], GOOD)
        self.assertNotIn("revisions", point["payload"])

    def test_concurrent_stale_revision_loses_after_the_first_commit(self):
        original = recall_api.recall_contribute(GOOD, "lesson", seat="CLAUDE")
        recall_api.gitleaks_available = lambda: False
        results = []

        def change(marker):
            try:
                results.append(("ok", recall_api.recall_correct(
                    original["doc_id"], GOOD + " " + marker, "concurrent " + marker,
                    seat="CLAUDE", expected_revision=1)))
            except FleetRagError as exc:
                results.append(("error", str(exc)))

        threads = [threading.Thread(target=change, args=("first correction",)),
                   threading.Thread(target=change, args=("second correction",))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(kind for kind, _ in results), ["error", "ok"])
        error = next(text for kind, text in results if kind == "error")
        self.assertIn("now at revision 2", error)
        point = next(p for p in FakeQdrant.points if p["payload"].get("doc_id") == original["doc_id"])
        self.assertEqual(point["payload"]["revision"], 2)
        self.assertEqual(len(point["payload"]["revisions"]), 1)


class DuplicateConcurrencyTests(RecallBase):
    def test_deterministic_duplicate_skips_embedding(self):
        text = "pm2 restart needs --update-env before process env changes take effect."
        first = recall_api.recall_contribute(text, "lesson", seat="CLAUDE")
        self.assertEqual(first["status"], "completed")
        recall_api.embed = mock.Mock(wraps=recall_api.embed)
        second = recall_api.recall_contribute(text, "lesson", seat="CLAUDE")
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["existing"]["doc_id"], first["doc_id"])
        self.assertEqual(second["existing"]["content_hash"], content_hash(text))
        self.assertIs(second["existing"]["retracted"], False)
        recall_api.embed.assert_not_called()
        self.assertEqual(len(FakeQdrant.upserts), 1)

    def test_duplicate_recheck_and_upsert_share_the_mutation_lock(self):
        text = "pm2 restart needs --update-env before process env changes take effect."
        state = {"inside": 0, "overlap": False, "embedded": 0}
        both_prechecked = threading.Barrier(2)
        original_embed = recall_api.embed

        def embed(cfg, texts):
            both_prechecked.wait(timeout=2)
            state["embedded"] += 1
            return original_embed(cfg, texts)

        class SlowQdrant(FakeQdrant):
            def retrieve(self, *args, **kwargs):
                if state["embedded"]:
                    state["inside"] += 1
                    state["overlap"] = state["overlap"] or state["inside"] > 1
                try:
                    time.sleep(0.05)
                    return super().retrieve(*args, **kwargs)
                finally:
                    if state["embedded"]:
                        state["inside"] -= 1

            def upsert(self, *args, **kwargs):
                state["inside"] += 1
                state["overlap"] = state["overlap"] or state["inside"] > 1
                try:
                    time.sleep(0.05)
                    return super().upsert(*args, **kwargs)
                finally:
                    state["inside"] -= 1

        recall_api.embed = embed
        recall_api.Qdrant = SlowQdrant
        recall_api.reset_config_cache()
        results = []
        threads = [
            threading.Thread(
                target=lambda: results.append(recall_api.recall_contribute(text, "lesson", seat="CLAUDE"))
            )
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(state["overlap"])
        self.assertEqual(sorted(r["status"] for r in results), ["completed", "duplicate"])
        self.assertEqual(len(FakeQdrant.upserts), 1)


class PublicMutationAuthTests(RecallBase):
    def test_owner_mutation_uses_the_distinct_owner_bearer(self):
        calls = []

        def http(url, body, headers, method, timeout, retries):
            calls.append((url, body, headers, method))
            return {"ok": True, "doc_id": (body or {}).get("doc_id"), "status": "completed"}

        env = {
            "RECALL_API_TOKEN": "shared",
            "RECALL_OWNER_TOKEN": "owner",
            "CF_ACCESS_CLIENT_ID": "cf-id",
            "CF_ACCESS_CLIENT_SECRET": "cf-secret",
        }
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(public_fallback.core, "http_json", http):
            public_fallback.call_public("recall_retract", {
                "doc_id": "contrib/CLAUDE/2026-11-05/472fc36e", "reason": "obsolete", "seat": "OWNER"})
            public_fallback.call_public("recall_stats", {})
        self.assertEqual(calls[0][2]["Authorization"], "Bearer owner")
        self.assertEqual(calls[1][2]["Authorization"], "Bearer shared")

    def test_owner_mutation_without_owner_bearer_fails_before_http(self):
        env = {"RECALL_API_TOKEN": "shared", "CF_ACCESS_CLIENT_ID": "cf-id", "CF_ACCESS_CLIENT_SECRET": "cf-secret"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(FleetRagError, "RECALL_OWNER_TOKEN"):
                public_fallback.call_public("recall_correct", {
                    "doc_id": "contrib/CLAUDE/2026-11-05/472fc36e", "text": GOOD,
                    "reason": "make current", "seat": "OWNER"})



class ConfigCacheTests(RecallBase):
    def test_read_then_write_then_read_reuses_write(self):
        calls = []

        def fake_load(need_write=False, extra=()):
            calls.append(need_write)
            return {"QDRANT_URL": "u", "QDRANT_FLEET_COLLECTION": "c", "TEI_URL": "t", "TEI_API_KEY": "k",
                    **({"QDRANT_API_KEY": "w"} if need_write else {})}
        recall_api.load_config = fake_load
        recall_api.reset_config_cache()
        recall_api.get_config(False)
        recall_api.get_config(False)
        recall_api.get_config(True)
        recall_api.get_config(False)
        self.assertEqual(calls, [False, True])

    def test_key_mode(self):
        self.assertEqual(recall_api.key_mode({"QDRANT_READONLY_API_KEY": "r", "QDRANT_API_KEY": "w"}), "read-only")
        self.assertEqual(recall_api.key_mode({"QDRANT_API_KEY": "w"}), "write")
        self.assertEqual(recall_api.key_mode({}), "none")


class EvalMatchTests(unittest.TestCase):
    HIT = {"doc_id": "doc/AI-Fleet-Coordinator/docs/RAG-FLEET-INFRA.md", "text": "Set --max-batch-tokens lower."}

    def test_prefix_only(self):
        self.assertTrue(ev.matches({"expect_doc_id_prefix": "doc/AI-Fleet-Coordinator/"}, self.HIT))
        self.assertFalse(ev.matches({"expect_doc_id_prefix": "doc/local/"}, self.HIT))

    def test_text_only_str_or_list_case_insensitive(self):
        self.assertTrue(ev.matches({"expect_text_contains": "MAX-BATCH-TOKENS"}, self.HIT))
        self.assertTrue(ev.matches({"expect_text_contains": ["nope", "lower."]}, self.HIT))
        self.assertFalse(ev.matches({"expect_text_contains": ["nope", ""]}, self.HIT))

    def test_both_present_both_must_hold(self):
        both = {"expect_doc_id_prefix": "doc/AI-Fleet-Coordinator/", "expect_text_contains": ["max-batch-tokens"]}
        self.assertTrue(ev.matches(both, self.HIT))
        self.assertFalse(ev.matches({**both, "expect_doc_id_prefix": "doc/local/"}, self.HIT))
        self.assertFalse(ev.matches({**both, "expect_text_contains": ["warmup"]}, self.HIT))

    def test_neither_never_matches(self):
        self.assertFalse(ev.matches({}, self.HIT))
        self.assertFalse(ev.matches({"expect_text_contains": []}, self.HIT))

    def test_shipped_golden_loads_and_prefixes_follow_sources_convention(self):
        rows = ev.load_golden()
        self.assertGreaterEqual(len(rows), 60)
        prefixes = [r["expect_doc_id_prefix"] for r in rows if r.get("expect_doc_id_prefix")]
        self.assertTrue(prefixes)                                   # the prefix path stays exercised
        for prefix in prefixes:
            # board rows are board/<32 hex>, notes note/<account>/ICNote/p<n>, contributions
            # contrib/<SEAT>/<day>/<hash8>, docs doc/<repo>/<path>.
            self.assertRegex(prefix, r"^(doc|skill|board|effort-log|memory|note|contrib)/[^/]+", prefix)
        for r in rows:
            if r.get("expect_source"):
                self.assertIn(r["expect_source"], recall_api.SOURCES, r["query"])
        # every source family the corpus carries is targeted by at least one row
        buckets = {ev.expected_source(r) for r in rows}
        for want in ("doc", "board", "apple-note", "agent-contribution", "any"):
            self.assertIn(want, buckets)
        self.assertGreaterEqual(sum(1 for r in rows if ev.expected_source(r) == "board"), 10)
        self.assertGreaterEqual(sum(1 for r in rows if ev.expected_source(r) == "apple-note"), 8)
        self.assertGreaterEqual(sum(1 for r in rows if ev.expected_source(r) == "agent-contribution"), 12)

    def test_run_eval_scores_with_and_semantics(self):
        rows = [
            {"query": "q1", "expect_doc_id_prefix": "doc/a/", "expect_text_contains": ["alpha"]},
            {"query": "q2", "expect_text_contains": ["beta"]},
            {"query": "q3", "expect_doc_id_prefix": "doc/c/"},
        ]
        answers = {
            "q1": [{"doc_id": "doc/a/x", "text": "not it"}, {"doc_id": "doc/b/x", "text": "alpha"},
                   {"doc_id": "doc/a/y", "text": "ALPHA here"}],
            "q2": [{"doc_id": "z", "text": "beta"}],
            "q3": [{"doc_id": "doc/x/1", "text": ""}],
        }
        with tempfile.TemporaryDirectory() as d:
            golden = pathlib.Path(d) / "g.jsonl"
            golden.write_text("".join(json.dumps(r) + "\n" for r in rows))
            res = ev.run_eval(k=3, golden=golden, search=lambda q, limit: {"hits": answers[q][:limit]})
        self.assertEqual(res["n"], 3)
        self.assertAlmostEqual(res["recall_at_1"], 1 / 3)        # only q2 at rank 1
        self.assertAlmostEqual(res["recall_at_k"], 2 / 3)        # q1 at rank 3 (first hit with BOTH), q3 miss
        self.assertAlmostEqual(res["mrr"], (1 / 3 + 1) / 3)
        self.assertEqual([m["query"] for m in res["misses"]], ["q3"])


def add_doc_chunks(doc_id: str, texts: list[str], **payload) -> None:
    for i, t in enumerate(texts):
        add_fake_point(t, doc_id=doc_id, chunk_index=i, chunk_count=len(texts), **payload)


class GroupingTests(RecallBase):
    """One hit per document by default; per_doc widens; group_hits shows depth."""

    def setUp(self):
        super().setUp()
        recall_api.install_fake_backend(seed=False)
        add_doc_chunks("board/aaaa", ["poisoned pm2 dump chunk one", "poisoned pm2 dump chunk two",
                                      "poisoned pm2 dump chunk three", "pm2 dump again"], source="board")
        add_doc_chunks("doc/local/apps/MAC-LOCAL-PROCESSES.md", ["pm2 dump poisoned by pm2 save"])
        add_doc_chunks("doc/other", ["nothing relevant here"])

    def test_one_hit_per_doc_with_group_depth(self):
        res = recall_api.recall_search("poisoned pm2 dump", limit=5)
        ids = [h["doc_id"] for h in res["hits"]]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(ids[:2], ["board/aaaa", "doc/local/apps/MAC-LOCAL-PROCESSES.md"])
        by_id = {h["doc_id"]: h for h in res["hits"]}
        self.assertEqual(by_id["board/aaaa"]["group_hits"], 4)
        self.assertEqual(by_id["doc/local/apps/MAC-LOCAL-PROCESSES.md"]["group_hits"], 1)
        self.assertEqual(by_id["board/aaaa"]["chunk_index"], 0)     # best chunk of the group
        self.assertEqual(FakeQdrant.calls[-1][2], 5 * recall_api.GROUP_DEPTH)
        self.assertEqual(FakeQdrant.group_calls, [])
        self.assertNotIn("rerank_score", by_id["board/aaaa"])

    def test_qdrant_groups_backend_is_opt_in(self):
        with mock.patch.dict(os.environ, {recall_api.QDRANT_GROUPS_ENV: "1"}):
            res = recall_api.recall_search("poisoned pm2 dump", limit=5)
        call = FakeQdrant.group_calls[-1]
        self.assertEqual((call["group_by"], call["group_size"], call["limit"], call["prefer_lessons"]),
                         ("doc_id", recall_api.GROUP_DEPTH, 5, True))
        ids = [h["doc_id"] for h in res["hits"]]
        self.assertEqual(ids[:2], ["board/aaaa", "doc/local/apps/MAC-LOCAL-PROCESSES.md"])
        self.assertEqual(res["hits"][0]["group_hits"], 4)
        with mock.patch.dict(os.environ, {recall_api.QDRANT_GROUPS_ENV: "0"}):
            recall_api.recall_search("poisoned pm2 dump", limit=5)
        self.assertEqual(len(FakeQdrant.group_calls), 1)

    def test_group_hits_is_capped_at_group_depth(self):
        add_doc_chunks("board/aaaa", ["poisoned pm2 dump chunk five", "poisoned pm2 dump chunk six"],
                       source="board")
        res = recall_api.recall_search("poisoned pm2 dump", limit=5)
        self.assertEqual(res["hits"][0]["group_hits"], recall_api.GROUP_DEPTH)

    def test_per_doc_widens(self):
        res = recall_api.recall_search("poisoned pm2 dump", limit=6, per_doc=2)
        ids = [h["doc_id"] for h in res["hits"]]
        self.assertEqual(ids.count("board/aaaa"), 2)
        self.assertEqual(ids.count("doc/local/apps/MAC-LOCAL-PROCESSES.md"), 1)
        self.assertEqual(res["hits"][0]["chunk_index"], 0)
        self.assertEqual(res["hits"][1]["chunk_index"], 1)
        self.assertTrue(all(h["group_hits"] == 4 for h in res["hits"] if h["doc_id"] == "board/aaaa"))
        res = recall_api.recall_search("poisoned pm2 dump", limit=6, per_doc=3)
        self.assertEqual([h["doc_id"] for h in res["hits"]].count("board/aaaa"), 3)

    def test_per_doc_validation(self):
        for bad in (0, 4, "2", 1.5, True):
            with self.assertRaisesRegex(FleetRagError, "per_doc"):
                recall_api.recall_search("x", per_doc=bad)
        self.assertEqual(FakeQdrant.calls, [])

    def test_limit_applies_to_documents(self):
        res = recall_api.recall_search("poisoned pm2 dump", limit=1)
        self.assertEqual([h["doc_id"] for h in res["hits"]], ["board/aaaa"])

    def test_groups_api_unsupported_falls_back_to_flat_fusion(self):
        class Old(FakeQdrant):
            def query_groups(self, *a, **k):
                raise FleetRagError("HTTP 404 from qdrant/collections/x/points/query/groups: unknown")

        with mock.patch.object(recall_api, "Qdrant", Old), \
                mock.patch.dict(os.environ, {recall_api.QDRANT_GROUPS_ENV: "1"}):
            res = recall_api.recall_search("poisoned pm2 dump", limit=5, per_doc=2)
        ids = [h["doc_id"] for h in res["hits"]]
        self.assertEqual(ids.count("board/aaaa"), 2)
        self.assertEqual(res["hits"][0]["group_hits"], 4)
        self.assertEqual(res["mode"], "hybrid")

    def test_other_backend_errors_propagate(self):
        class Down(FakeQdrant):
            def query_groups(self, *a, **k):
                raise FleetRagError("TimeoutError reaching qdrant")

        with mock.patch.object(recall_api, "Qdrant", Down), \
                mock.patch.dict(os.environ, {recall_api.QDRANT_GROUPS_ENV: "1"}):
            with self.assertRaisesRegex(FleetRagError, "TimeoutError"):
                recall_api.recall_search("poisoned pm2 dump")

    def test_group_hits_helper(self):
        pts = [{"payload": {"doc_id": "a"}, "score": 3}, {"payload": {"doc_id": "b"}, "score": 2},
               {"payload": {"doc_id": "a"}, "score": 1}, {"payload": {"doc_id": "a"}, "score": 0}]
        groups = recall_api.group_hits(pts, "doc_id", 2)
        self.assertEqual([(g["id"], len(g["hits"])) for g in groups], [("a", 2), ("b", 1)])


class GroupWidenTests(RecallBase):
    """One document crowding the fused window must not starve the other matching docs."""

    def _corpus(self, big: int, others: int = 8) -> None:
        recall_api.install_fake_backend(seed=False)
        add_doc_chunks("doc/big", [f"poisoned pm2 dump chunk {i}" for i in range(big)])
        for k in range(others):
            add_doc_chunks(f"doc/other-{k}", [f"poisoned pm2 dump other {k}"])
        # the ingest sentinel matches every term; it must never enter any window
        add_fake_point("poisoned pm2 dump sentinel", source="meta", doc_id="meta/run")

    def _windows(self) -> list[int]:
        return [c[2] for c in FakeQdrant.calls]

    def _assert_sentinel_excluded(self, res: dict) -> None:
        self.assertNotIn("meta/run", [h["doc_id"] for h in res["hits"]])
        for _, _, _, flt in FakeQdrant.calls:
            self.assertIn(META_EXCLUDE, flt["must_not"])

    def test_dominant_doc_widens_window_until_limit_docs(self):
        self._corpus(big=30)
        res = recall_api.recall_search("poisoned pm2 dump", limit=5)
        ids = [h["doc_id"] for h in res["hits"]]
        self.assertEqual(len(ids), 5)                         # was 1 before the widen loop
        self.assertEqual(len(set(ids)), 5)
        self.assertEqual(ids[0], "doc/big")
        self.assertEqual(res["hits"][0]["group_hits"], recall_api.GROUP_DEPTH)
        self.assertEqual(self._windows(), [25, 100])          # limit*depth, then x4
        self._assert_sentinel_excluded(res)

    def test_per_doc_semantics_survive_widening(self):
        self._corpus(big=30)
        res = recall_api.recall_search("poisoned pm2 dump", limit=5, per_doc=2)
        ids = [h["doc_id"] for h in res["hits"]]
        self.assertEqual(ids.count("doc/big"), 2)
        self.assertEqual([h["chunk_index"] for h in res["hits"][:2]], [0, 1])
        self.assertEqual(len(ids), 5)
        self.assertEqual(self._windows(), [25, 100])

    def test_widens_at_most_twice(self):
        self._corpus(big=130)
        res = recall_api.recall_search("poisoned pm2 dump", limit=5)
        self.assertEqual(len(res["hits"]), 5)
        self.assertEqual(self._windows(), [25, 100, 400])
        self._assert_sentinel_excluded(res)
        self._corpus(big=500)                                  # still full after the last round
        res = recall_api.recall_search("poisoned pm2 dump", limit=5)
        self.assertEqual([h["doc_id"] for h in res["hits"]], ["doc/big"])
        self.assertEqual(self._windows(), [25, 100, 400])

    def test_window_cap(self):
        self._corpus(big=30)
        with mock.patch.object(recall_api, "GROUP_WINDOW_MAX", 30):
            res = recall_api.recall_search("poisoned pm2 dump", limit=5)
        self.assertEqual(self._windows(), [25, 30])
        self.assertEqual([h["doc_id"] for h in res["hits"]], ["doc/big"])

    def test_no_refetch_when_first_window_is_not_full(self):
        self._corpus(big=3, others=2)
        res = recall_api.recall_search("poisoned pm2 dump", limit=5)
        self.assertEqual(len(res["hits"]), 3)
        self.assertEqual(self._windows(), [25])

    def test_rerank_candidates_widen_too(self):
        self._corpus(big=30)
        srv = _RerankServer({"poisoned pm2 dump other 7": 0.99})
        self.addCleanup(srv.close)

        def with_rerank() -> None:      # install_fake_backend (via _corpus) resets load_config
            base = recall_api.load_config()
            recall_api.load_config = lambda need_write=False, extra=(): {  # noqa: E731
                **base, "TEI_RERANK_URL": srv.url, "TEI_RERANK_API_KEY": "k"}
            recall_api.reset_config_cache()

        with_rerank()
        res = recall_api.recall_search("poisoned pm2 dump", limit=5)
        self.assertEqual(res["mode"], "hybrid+rerank")
        self.assertEqual(res["hits"][0]["doc_id"], "doc/other-7")
        # candidate_count(5) = 20 groups -> a 100-point first window already holds the corpus
        self.assertEqual(self._windows(), [100])
        self.assertEqual(len(srv.requests[0]["texts"]), 9)
        self._corpus(big=130)                                  # the rerank window widens too
        with_rerank()
        res = recall_api.recall_search("poisoned pm2 dump", limit=5)
        self.assertEqual(res["hits"][0]["doc_id"], "doc/other-7")
        self.assertEqual(self._windows(), [100, 400])
        self.assertEqual(len(srv.requests[-1]["texts"]), 9)


class LessonBoostTests(RecallBase):
    def setUp(self):
        super().setUp()
        recall_api.install_fake_backend(seed=False)
        # The fake scores keyword overlap and breaks ties by insertion order, so the doc (first)
        # wins the fused order and only the lesson boost can lift the contribution above it.
        add_fake_point("Coolify deploy deploy deploy: the operator guide for every deploy button.",
                       doc_id="doc/local/apps/COOLIFY.md")
        add_fake_point("Coolify merge-to-main already auto-deploys ST via the GitHub webhook; an extra "
                       "API deploy just double-builds.", source="agent-contribution", category="lesson",
                       doc_id="contrib/BF-DEPLOYER/2026-09-02/7a260e65", seat="BF-DEPLOYER")
        add_fake_point("A finding about Coolify deploy that is not a lesson.", source="agent-contribution",
                       category="finding", doc_id="contrib/GROK/2026-09-02/deadbeef")

    def test_lesson_first_by_default_and_off_for_raw_research(self):
        res = recall_api.recall_search("Coolify deploy", limit=3)
        self.assertEqual(res["hits"][0]["doc_id"], "contrib/BF-DEPLOYER/2026-09-02/7a260e65")
        res = recall_api.recall_search("Coolify deploy", limit=3, prefer_lessons=False)
        self.assertEqual(res["hits"][0]["doc_id"], "doc/local/apps/COOLIFY.md")
        with mock.patch.dict(os.environ, {recall_api.QDRANT_GROUPS_ENV: "1"}):
            recall_api.recall_search("Coolify deploy", limit=3)
            self.assertTrue(FakeQdrant.group_calls[-1]["prefer_lessons"])
            recall_api.recall_search("Coolify deploy", limit=3, prefer_lessons=False)
            self.assertFalse(FakeQdrant.group_calls[-1]["prefer_lessons"])

    def test_only_lesson_categories_are_boosted(self):
        res = recall_api.recall_search("Coolify deploy", limit=3)
        ids = [h["doc_id"] for h in res["hits"]]
        self.assertEqual(ids[-1], "contrib/GROK/2026-09-02/deadbeef")   # a "finding" gets no boost

    def test_lesson_filter_shape_and_prefetch(self):
        from fleet_rag import core
        flt = recall_api.build_filter(app="fleet")
        lf = core.lesson_filter(flt)
        self.assertEqual(lf["must"][0], {"key": "source", "match": {"value": "agent-contribution"}})
        self.assertEqual(lf["must"][1], {"key": "category",
                                         "match": {"any": ["lesson", "preference", "decision", "runbook"]}})
        self.assertEqual(lf["must"][2], flt)                    # caller's filter still applies
        self.assertEqual(core.lesson_filter(None)["must"][2:], [])
        q = core.Qdrant({"QDRANT_URL": "http://q", "QDRANT_FLEET_COLLECTION": "c", "QDRANT_API_KEY": "k"})
        pf = q._prefetch([0.1], ["example-host"], flt, 20, True)
        self.assertEqual(len(pf), 3)
        self.assertEqual(pf[0]["filter"], flt)
        self.assertEqual(pf[1]["filter"]["must"][0], flt)
        self.assertEqual(pf[2]["filter"], lf)
        self.assertEqual(pf[2]["score_threshold"], core.LESSON_SCORE_THRESHOLD)
        self.assertNotIn("score_threshold", pf[0])
        self.assertNotIn("score_threshold", pf[1])
        self.assertEqual(len(q._prefetch([0.1], [], None, 20, True)), 2)       # dense + lesson
        self.assertEqual(len(q._prefetch([0.1], ["a"], None, 20, False)), 2)   # dense + keyword
        self.assertNotIn("filter", q._prefetch([0.1], ["a"], None, 20, False)[0])


class RealQdrantBodyTests(unittest.TestCase):
    """The request bodies the real client sends, captured at the HTTP seam."""

    def setUp(self):
        from fleet_rag import core
        self.core = core
        self.q = core.Qdrant({"QDRANT_URL": "http://q", "QDRANT_FLEET_COLLECTION": "fleet-agents",
                              "QDRANT_API_KEY": "k"})
        self.sent: list[tuple[str, dict]] = []

    def _capture(self, result):
        def fake_call(path, body=None, method=None, write=False, timeout=0):
            self.sent.append((path, body))
            return result
        return fake_call

    def test_query_groups_body(self):
        groups = {"result": {"groups": [{"id": "doc/a", "hits": [{"id": 1, "score": 0.9, "payload": {}}]},
                                        {"id": "doc/b", "hits": []}]}}
        with mock.patch.object(self.q, "_call", self._capture(groups)):
            out = self.q.query_groups([0.5], ["pm2", "dump"], 7, {"must_not": [META_EXCLUDE]},
                                      group_by="doc_id", group_size=3)
        path, body = self.sent[-1]
        self.assertEqual(path, "/collections/fleet-agents/points/query/groups")
        self.assertEqual((body["group_by"], body["group_size"], body["limit"]), ("doc_id", 3, 7))
        self.assertEqual(body["query"], {"fusion": "rrf"})
        self.assertEqual(len(body["prefetch"]), 3)
        self.assertEqual(body["prefetch"][0]["limit"], 28)                    # max(4*limit, 20)
        self.assertEqual(body["prefetch"][1]["filter"]["must"][1]["should"][0],
                         {"key": "text", "match": {"text": "pm2"}})
        self.assertEqual(body["prefetch"][2]["filter"]["must"][0]["match"]["value"], "agent-contribution")
        self.assertEqual(body["prefetch"][2]["score_threshold"], self.core.LESSON_SCORE_THRESHOLD)
        self.assertTrue(body["with_payload"])
        self.assertEqual(out, [{"id": "doc/a", "hits": [{"id": 1, "score": 0.9, "payload": {}}]},
                               {"id": "doc/b", "hits": []}])

    def test_query_hybrid_bodies_and_dense_fallback(self):
        with mock.patch.object(self.q, "_call", self._capture({"result": {"points": []}})):
            self.q.query_hybrid([0.5], ["pm2"], 5, None)
            self.assertEqual(self.sent[-1][0], "/collections/fleet-agents/points/query")
            self.assertEqual(len(self.sent[-1][1]["prefetch"]), 3)
            self.q.query_hybrid([0.5], ["pm2"], 5, None, prefer_lessons=False)
            self.assertEqual(len(self.sent[-1][1]["prefetch"]), 2)
            self.q.query_hybrid([0.5], [], 5, None, prefer_lessons=True)
            self.assertEqual(len(self.sent[-1][1]["prefetch"]), 2)          # dense + lesson
        with mock.patch.object(self.q, "_call", self._capture({"result": []})):
            self.q.query_hybrid([0.5], [], 5, None, prefer_lessons=False)  # plain dense fallback
            self.assertEqual(self.sent[-1][0], "/collections/fleet-agents/points/search")
            self.assertEqual(self.sent[-1][1]["vector"], [0.5])


class _RerankServer:
    """Tiny TEI-shaped /rerank on 127.0.0.1.  Scores come from `scores` keyed by text (default 0)."""

    def __init__(self, scores: dict[str, float] | None = None, status: int = 200, delay: float = 0.0,
                 broken: bool = False):
        import http.server
        import threading
        srv = self
        srv.scores, srv.status, srv.delay, srv.broken = scores or {}, status, delay, broken
        srv.requests: list[dict] = []
        srv.auth: list[str] = []

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401 - silence
                pass

            def do_POST(self):
                import time
                n = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(n) or b"{}")
                srv.requests.append(body)
                srv.auth.append(self.headers.get("Authorization", ""))
                if srv.delay:
                    time.sleep(srv.delay)
                if srv.status != 200:
                    self.send_response(srv.status)
                    self.end_headers()
                    return
                if srv.broken:
                    payload = b'[{"score": 1.0}]'
                else:
                    rows = [{"index": i, "score": srv.scores.get(t, 0.0)} for i, t in enumerate(body["texts"])]
                    rows.sort(key=lambda r: -r["score"])
                    payload = json.dumps(rows).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class RerankTests(RecallBase):
    def setUp(self):
        super().setUp()
        recall_api.install_fake_backend(seed=False)
        add_fake_point("pm2 dump poisoned: the fused winner (keyword-heavy).", doc_id="doc/fused-first")
        add_fake_point("pm2 dump: fused second.", doc_id="doc/fused-second")
        add_fake_point("pm2: the one the cross-encoder actually likes.", doc_id="doc/reranked-first")
        self.servers: list[_RerankServer] = []

    def tearDown(self):
        for s in self.servers:
            s.close()
        super().tearDown()

    def _configure(self, server: _RerankServer) -> None:
        self.servers.append(server)
        base = recall_api.load_config()
        recall_api.load_config = lambda need_write=False, extra=(): {  # noqa: E731
            **base, "TEI_RERANK_URL": server.url, "TEI_RERANK_API_KEY": "test-rerank-key"}
        recall_api.reset_config_cache()

    def test_rerank_reorders_and_reports_scores(self):
        srv = _RerankServer({"pm2: the one the cross-encoder actually likes.": 0.97,
                             "pm2 dump poisoned: the fused winner (keyword-heavy).": 0.2})
        self._configure(srv)
        res = recall_api.recall_search("pm2 dump poisoned", limit=2)
        self.assertEqual(res["mode"], "hybrid+rerank")
        self.assertEqual([h["doc_id"] for h in res["hits"]], ["doc/reranked-first", "doc/fused-first"])
        top = res["hits"][0]
        self.assertEqual(top["score"], 0.97)
        self.assertEqual(top["rerank_score"], 0.97)
        self.assertIn("fused_score", top)
        self.assertEqual(top["group_hits"], 1)
        # the whole candidate window (not just `limit`) went to the reranker, with bearer auth
        self.assertEqual(FakeQdrant.calls[-1][2], recall_api.candidate_count(2) * recall_api.GROUP_DEPTH)
        self.assertEqual(len(srv.requests), 1)
        self.assertEqual(len(srv.requests[0]["texts"]), 3)
        self.assertEqual(srv.requests[0]["query"], "pm2 dump poisoned")
        self.assertTrue(srv.requests[0]["truncate"])
        self.assertEqual(srv.auth[0], "Bearer test-rerank-key")

    def test_rerank_false_keeps_fused_order_and_narrow_window(self):
        srv = _RerankServer({"pm2: the one the cross-encoder actually likes.": 0.97})
        self._configure(srv)
        res = recall_api.recall_search("pm2 dump poisoned", limit=2, rerank=False)
        self.assertEqual(res["mode"], "hybrid")
        self.assertEqual(res["hits"][0]["doc_id"], "doc/fused-first")
        self.assertEqual(FakeQdrant.calls[-1][2], 2 * recall_api.GROUP_DEPTH)
        self.assertEqual(srv.requests, [])
        self.assertNotIn("rerank_score", res["hits"][0])

    def test_no_keys_means_no_rerank(self):
        res = recall_api.recall_search("pm2 dump poisoned", limit=2)
        self.assertEqual(res["mode"], "hybrid")
        self.assertEqual(FakeQdrant.calls[-1][2], 2 * recall_api.GROUP_DEPTH)

    def test_server_error_falls_back_silently(self):
        self._configure(_RerankServer(status=500))
        res = recall_api.recall_search("pm2 dump poisoned", limit=2)
        self.assertEqual(res["mode"], "hybrid")
        self.assertEqual(res["hits"][0]["doc_id"], "doc/fused-first")
        self.assertNotIn("rerank_score", res["hits"][0])

    def test_server_error_reports_to_telemetry(self):
        """The fallback stays silent to the caller, but one telemetry breadcrumb records it (no
        query text, no doc content) so a real outage shows up before the next weekly eval."""
        from fleet_rag import telemetry
        self._configure(_RerankServer(status=500))
        with mock.patch.object(telemetry, "capture_message") as cap:
            recall_api.recall_search("pm2 dump poisoned", limit=2)
        cap.assert_called_once()
        (message,), kwargs = cap.call_args
        self.assertIn("rerank fallback", message)
        self.assertEqual(kwargs.get("operation"), "rerank-fallback")
        self.assertNotIn("pm2 dump poisoned", message)

    def test_malformed_response_falls_back_silently(self):
        self._configure(_RerankServer(broken=True))
        res = recall_api.recall_search("pm2 dump poisoned", limit=2)
        self.assertEqual(res["mode"], "hybrid")
        self.assertEqual(res["hits"][0]["doc_id"], "doc/fused-first")

    def test_slow_endpoint_falls_back_within_timeout(self):
        from fleet_rag import core
        self._configure(_RerankServer({"pm2: the one the cross-encoder actually likes.": 0.9}, delay=1.5))
        with mock.patch.object(core, "RERANK_TIMEOUT", 1):
            res = recall_api.recall_search("pm2 dump poisoned", limit=2)
        self.assertEqual(res["mode"], "hybrid")
        self.assertEqual(res["hits"][0]["doc_id"], "doc/fused-first")

    def test_slow_endpoint_budget_spans_batches(self):
        """RERANK_TIMEOUT is a total budget: two 0.6 s batches under a 1 s budget give up at
        ~1 s and keep the fused order, instead of taking 2 x timeout."""
        import time
        from fleet_rag import core
        for i in range(40):
            add_fake_point(f"pm2 dump poisoned candidate {i}.", doc_id=f"doc/cand-{i}")
        srv = _RerankServer({"pm2: the one the cross-encoder actually likes.": 0.9}, delay=0.6)
        self._configure(srv)
        t0 = time.monotonic()
        with mock.patch.object(core, "RERANK_TIMEOUT", 1):
            res = recall_api.recall_search("pm2 dump poisoned", limit=10)
        elapsed = time.monotonic() - t0
        self.assertEqual(res["mode"], "hybrid")
        self.assertEqual(res["hits"][0]["doc_id"], "doc/fused-first")
        self.assertNotIn("rerank_score", res["hits"][0])
        self.assertLessEqual(len(srv.requests), 2)
        self.assertLess(elapsed, 1.6, elapsed)

    def test_unreachable_endpoint_falls_back(self):
        srv = _RerankServer()
        self._configure(srv)
        srv.close()
        res = recall_api.recall_search("pm2 dump poisoned", limit=2)
        self.assertEqual(res["mode"], "hybrid")

    def test_dense_mode_label(self):
        srv = _RerankServer({"pm2: the one the cross-encoder actually likes.": 0.9})
        self._configure(srv)
        res = recall_api.recall_search("is it", limit=2)
        self.assertEqual(res["mode"], "dense+rerank")


class CoreRerankTests(unittest.TestCase):
    def setUp(self):
        from fleet_rag import core
        self.core = core
        self.servers: list[_RerankServer] = []

    def tearDown(self):
        for s in self.servers:
            s.close()

    def _cfg(self, srv: _RerankServer) -> dict:
        self.servers.append(srv)
        return {"TEI_RERANK_URL": srv.url + "/", "TEI_RERANK_API_KEY": "k"}

    def test_batches_of_32_and_aligned_scores(self):
        texts = [f"text {i}" for i in range(40)]
        srv = _RerankServer({t: i / 100 for i, t in enumerate(texts)})
        scores = self.core.rerank(self._cfg(srv), "q", texts)
        self.assertEqual(len(srv.requests), 2)
        self.assertEqual([len(r["texts"]) for r in srv.requests], [32, 8])
        self.assertEqual(scores, [i / 100 for i in range(40)])
        self.assertEqual(self.core.rerank(self._cfg(srv), "q", []), [])

    def test_not_configured_or_incomplete_raises(self):
        with self.assertRaisesRegex(FleetRagError, "not configured"):
            self.core.rerank({}, "q", ["a"])
        self.assertFalse(self.core.rerank_configured({"TEI_RERANK_URL": "http://x"}))
        srv = _RerankServer(broken=True)
        with self.assertRaisesRegex(FleetRagError, "rerank returned"):
            self.core.rerank(self._cfg(srv), "q", ["a", "b"])

    def test_timeout_is_a_total_budget_across_batches(self):
        """Each batch gets only what is left of the budget; an exhausted budget stops the loop."""
        core = self.core
        clock = [100.0]
        seen: list[float] = []

        class Clock:
            @staticmethod
            def monotonic() -> float:
                return clock[0]

        def fake_http(url, body, headers, timeout, retries):  # noqa: ANN001
            seen.append(round(timeout, 3))
            clock[0] += 5.0                                    # every batch takes 5 s
            return [{"index": i, "score": 0.5} for i in range(len(body["texts"]))]

        cfg = {"TEI_RERANK_URL": "http://rerank.test", "TEI_RERANK_API_KEY": "k"}
        with mock.patch.object(core, "time", Clock), mock.patch.object(core, "http_json", fake_http):
            with self.assertRaisesRegex(FleetRagError, "budget of 8s exhausted after 64 of 100"):
                core.rerank(cfg, "q", [f"t{i}" for i in range(100)])
            self.assertEqual(seen, [8.0, 3.0])                 # 8 s budget: 5 s spent, 3 s left
            seen.clear()
            self.assertEqual(len(core.rerank(cfg, "q", [f"t{i}" for i in range(40)], timeout=20)), 40)
            self.assertEqual(seen, [20.0, 15.0])

    def test_slow_server_budget_stops_second_batch(self):
        import time
        texts = [f"text {i}" for i in range(40)]
        srv = _RerankServer({t: 0.1 for t in texts}, delay=0.6)
        t0 = time.monotonic()
        with self.assertRaises(FleetRagError):
            self.core.rerank(self._cfg(srv), "q", texts, timeout=1)
        self.assertLess(time.monotonic() - t0, 1.6)
        self.assertLessEqual(len(srv.requests), 2)

    def test_http_error_raises_fleet_rag_error_without_retry(self):
        srv = _RerankServer(status=503)
        with self.assertRaises(FleetRagError):
            self.core.rerank(self._cfg(srv), "q", ["a"])
        self.assertEqual(len(srv.requests), 1)      # retries=0: a slow reranker is never hammered


def load_cli():
    path = pathlib.Path(__file__).resolve().parents[2] / "recall"
    loader = importlib.machinery.SourceFileLoader("recall_cli_under_test", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class CliTests(RecallBase):
    @classmethod
    def setUpClass(cls):
        cls.cli = load_cli()

    def test_normalize_argv_only_prepends_search_without_a_subcommand(self):
        n = self.cli.normalize_argv
        self.assertEqual(n([]), [])
        self.assertEqual(n(["how do I avoid leaking credentials"]), ["search", "how do I avoid leaking credentials"])
        self.assertEqual(n(["pm2 dump poisoned", "--since-days", "90"]),
                         ["search", "pm2 dump poisoned", "--since-days", "90"])
        self.assertEqual(n(["stats"]), ["stats"])
        self.assertEqual(n(["stats", "--json"]), ["stats", "--json"])
        self.assertEqual(n(["--json", "stats"]), ["--json", "stats"])            # subcommand later in argv
        self.assertEqual(n(["contribute", "-", "--category", "lesson"]), ["contribute", "-", "--category", "lesson"])
        self.assertEqual(n(["ingest", "--all"]), ["ingest", "--all"])
        self.assertEqual(n(["ingest", "--source", "doc", "--dry-run"]),
                         ["ingest", "--source", "doc", "--dry-run"])
        self.assertEqual(n(["--help"]), ["--help"])
        self.assertEqual(n(["-h"]), ["-h"])
        # A subcommand name AFTER "--" is query text, not a subcommand.
        self.assertEqual(n(["--", "stats"]), ["search", "--", "stats"])
        self.assertEqual(n(["--limit", "3", "--", "doctor"]), ["search", "--limit", "3", "--", "doctor"])
        self.assertEqual(n(["search", "--", "doctor"]), ["search", "--", "doctor"])

    def test_ingest_forwards_to_pipeline(self):
        seen: list[list[str]] = []

        def fake_main(argv=None):
            seen.append(list(argv or []))
            return 0

        import fleet_rag.ingest as ing
        with mock.patch.object(ing, "main", fake_main):
            rc = self.cli.main(["ingest", "--source", "doc", "--dry-run", "--limit", "2"])
            rc2 = self.cli.main(["ingest"])
        self.assertEqual(rc, 0)
        self.assertEqual(rc2, 0)
        self.assertEqual(seen, [["--source", "doc", "--dry-run", "--limit", "2"], ["--all"]])

    def test_ingest_forwards_the_lock_timeout_override(self):
        """The operator escape hatch the docs promise has to exist on the CLI operators type.

        `recall ingest --all --prune --lock-timeout 0` used to die in argparse with exit 2,
        because the `ingest` subparser never defined the flag and `cmd_ingest` never forwarded
        it, while the underlying module had it all along (Kody review, PR #800).
        """
        seen: list[list[str]] = []

        def fake_main(argv=None):
            seen.append(list(argv or []))
            return 0

        import fleet_rag.ingest as ing
        with mock.patch.object(ing, "main", fake_main):
            rc = self.cli.main(["ingest", "--all", "--prune", "--lock-timeout", "0"])
            rc2 = self.cli.main(["ingest", "--all", "--prune", "--lock-timeout", "30.5"])
            rc3 = self.cli.main(["ingest", "--all", "--prune"])
        self.assertEqual((rc, rc2, rc3), (0, 0, 0))
        self.assertEqual(seen[0], ["--all", "--prune", "--lock-timeout", "0.0"])
        self.assertEqual(seen[1], ["--all", "--prune", "--lock-timeout", "30.5"])
        # Absent the flag, nothing is forwarded, so the module keeps its own default.
        self.assertEqual(seen[2], ["--all", "--prune"])

    def test_fake_backend_short_circuits_the_direct_path_preflight(self):
        # The fake backend installed by RecallBase.setUp() makes using_fake_backend() True, so
        # eval/ingest must never even ask whether Tailscale is down -- exactly like
        # call_with_fallback / guard_may_run.
        import fleet_rag.ingest as ing
        with mock.patch.object(public_fallback, "tailscale_status_text",
                               side_effect=AssertionError("must not probe Tailscale for a fake backend")):
            with mock.patch.object(ing, "main", lambda argv=None: 0):
                rc = self.cli.main(["ingest"])
            self.assertEqual(rc, 0)
            with tempfile.TemporaryDirectory() as d:
                golden = pathlib.Path(d) / "golden.jsonl"
                golden.write_text(json.dumps({"query": "x", "expect_text_contains": "y"}) + "\n",
                                  encoding="utf-8")
                rc = self.cli.main(["eval", "--golden", str(golden)])
        self.assertIn(rc, (0, 1))                                  # ran for real, not blocked

    def _with_real_backend_and_tailscale_status(self, status_text):
        """Swap in the real (non-fake) Qdrant class and a fixed tailscale_status_text() so
        require_direct_path's checks actually engage, without ever touching the network -- the
        preflight must raise before recall_search/recall_api.load_config runs.  No cleanup is
        needed: RecallBase.tearDown() already restores recall_api.Qdrant to this same real class
        (it is what setUp() captured into `_saved` before installing the fake)."""
        recall_api.Qdrant = core.Qdrant
        os.environ.pop("QDRANT_URL", None)
        os.environ.pop("TEI_URL", None)
        return mock.patch.object(public_fallback, "tailscale_status_text", return_value=status_text)

    def test_eval_blocked_when_tailscale_down_and_no_override(self):
        with self._with_real_backend_and_tailscale_status("Logged out.\n"):
            err = io.StringIO()
            with mock.patch("sys.stderr", err), mock.patch("sys.stdout", io.StringIO()):
                rc = self.cli.main(["eval"])
        self.assertEqual(rc, 2)
        self.assertIn("recall eval", err.getvalue())
        self.assertIn("tailscale login", err.getvalue())
        self.assertIn("recall-tunnel", err.getvalue())

    def test_ingest_blocked_when_tailscale_down_and_no_override(self):
        import fleet_rag.ingest as ing
        with self._with_real_backend_and_tailscale_status("Logged out.\n"):
            with mock.patch.object(ing, "main", side_effect=AssertionError("must not reach the pipeline")):
                err = io.StringIO()
                with mock.patch("sys.stderr", err), mock.patch("sys.stdout", io.StringIO()):
                    rc = self.cli.main(["ingest"])
        self.assertEqual(rc, 2)
        self.assertIn("recall ingest", err.getvalue())
        self.assertIn("tailscale login", err.getvalue())

    def test_eval_and_ingest_run_when_tailscale_status_is_unknown(self):
        import fleet_rag.ingest as ing
        with self._with_real_backend_and_tailscale_status(None):
            with mock.patch.object(ing, "main", lambda argv=None: 0):
                rc = self.cli.main(["ingest"])
            self.assertEqual(rc, 0)

    def test_eval_and_ingest_run_with_a_local_override_even_if_tailscale_looks_down(self):
        import fleet_rag.ingest as ing
        with self._with_real_backend_and_tailscale_status("Logged out.\n"):
            os.environ["QDRANT_URL"] = "http://127.0.0.1:16333"
            os.environ["TEI_URL"] = "http://127.0.0.1:18081"
            self.addCleanup(os.environ.pop, "QDRANT_URL", None)
            self.addCleanup(os.environ.pop, "TEI_URL", None)
            with mock.patch.object(ing, "main", lambda argv=None: 0):
                rc = self.cli.main(["ingest"])
            self.assertEqual(rc, 0)

    def test_query_that_looks_like_a_subcommand_after_dashdash(self):
        # `recall -- stats` searches for the literal word "stats" (argparse strips the "--").
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            rc = self.cli.main(["--limit", "2", "--json", "--", "stats"])
        self.assertIn(rc, (0, 1))
        self.assertEqual(FakeQdrant.calls[-1][1], ["stats"])

    def test_contribute_text_sources(self):
        ct = self.cli.contribute_text
        self.assertEqual(ct("plain text", None), "plain text")
        self.assertEqual(ct("-", None, stdin=io.StringIO("from stdin")), "from stdin")
        self.assertEqual(ct(None, "-", stdin=io.StringIO("from stdin 2")), "from stdin 2")
        self.assertEqual(ct("-", "-", stdin=io.StringIO("both dash")), "both dash")
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d) / "lesson.md"
            f.write_text("from file\n", encoding="utf-8")
            self.assertEqual(ct(None, str(f)), "from file\n")
            with self.assertRaisesRegex(FleetRagError, "not two of them"):
                ct("plain", str(f))
            with self.assertRaisesRegex(FleetRagError, "cannot read --file"):
                ct(None, str(pathlib.Path(d) / "missing.md"))
        with self.assertRaisesRegex(FleetRagError, "needs text"):
            ct(None, None)

    def test_contribute_via_file_and_stdin_end_to_end(self):
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d) / "lesson.txt"
            f.write_text(GOOD, encoding="utf-8")
            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = self.cli.main(["contribute", "--file", str(f), "--category", "lesson",
                                    "--seat", "CLAUDE", "--json"])
        self.assertEqual(rc, 0)
        self.assertIn('"doc_id": "contrib/CLAUDE/', buf.getvalue())
        self.assertEqual(FakeQdrant.upserts[-1][0]["payload"]["text"], GOOD)

        buf = io.StringIO()
        stdin_text = (
            "Qdrant snapshots belong under /var/lib/qdrant/snapshots; verify a fresh "
            "snapshot before any collection migration.\n"
        )
        with mock.patch("sys.stdout", buf), mock.patch("sys.stdin", io.StringIO(stdin_text)):
            rc = self.cli.main(["contribute", "-", "--category", "runbook", "--seat", "GROK"])
        self.assertEqual(rc, 0)
        self.assertIn("stored contrib/GROK/", buf.getvalue())
        self.assertEqual(len(FakeQdrant.upserts), 2)

    def test_deterministic_duplicate_is_a_refusal_not_a_stored_success(self):
        text = "pm2 restart needs --update-env before process env changes take effect."
        with mock.patch("sys.stdin", io.StringIO(text)):
            rc = self.cli.main(["contribute", "-", "--category", "lesson", "--seat", "CLAUDE"])
        self.assertEqual(rc, 0)
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(text)), mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = self.cli.main(["contribute", "-", "--category", "lesson", "--seat", "CLAUDE", "--force", "--json"])
        self.assertEqual(rc, 1)
        self.assertEqual(json.loads(out.getvalue())["status"], "duplicate")
        body = json.loads(out.getvalue())
        self.assertIn("identical text is already stored as contrib/CLAUDE/", body["message"])
        self.assertIn("nothing was written", body["message"])
        self.assertNotIn("similarity", body["message"])
        self.assertNotIn("--force", body["message"])
        self.assertNotIn("stored contrib/", out.getvalue())
        self.assertEqual(err.getvalue(), "")

    def test_deterministic_duplicate_after_correction_does_not_claim_the_old_text_is_stored(self):
        old_text = "pm2 restart needs --update-env before process env changes take effect."
        new_text = "pm2 restart applies process environment changes after an explicit reload."
        with mock.patch("sys.stdin", io.StringIO(old_text)):
            rc = self.cli.main(["contribute", "-", "--category", "lesson", "--seat", "CLAUDE", "--json"])
        self.assertEqual(rc, 0)
        point = FakeQdrant.points[-1]
        doc_id = point["payload"]["doc_id"]
        with mock.patch("sys.stdin", io.StringIO(new_text)), mock.patch("sys.stdout", io.StringIO()):
            rc = self.cli.main(["correct", doc_id, "-", "--reason", "correct the steps", "--seat", "CLAUDE", "--json"])
        self.assertEqual(rc, 0)
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(old_text)), mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = self.cli.main(["contribute", "-", "--category", "lesson", "--seat", "CLAUDE", "--json"])
        self.assertEqual(rc, 1)
        body = json.loads(out.getvalue())
        self.assertIn("was corrected to different text", body["message"])
        self.assertIn("your text is not stored", body["message"])
        self.assertNotIn("identical text", body["message"])

    def test_deterministic_duplicate_after_retraction_names_the_retracted_state(self):
        text = "pm2 restart needs --update-env before process env changes take effect."
        with mock.patch("sys.stdin", io.StringIO(text)):
            rc = self.cli.main(["contribute", "-", "--category", "lesson", "--seat", "CLAUDE", "--json"])
        self.assertEqual(rc, 0)
        doc_id = FakeQdrant.points[-1]["payload"]["doc_id"]
        with mock.patch("sys.stdout", io.StringIO()):
            rc = self.cli.main(["retract", doc_id, "--reason", "obsolete", "--seat", "CLAUDE", "--json"])
        self.assertEqual(rc, 0)
        out = io.StringIO()
        err = io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(text)), mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            rc = self.cli.main(["contribute", "-", "--category", "lesson", "--seat", "CLAUDE", "--json"])
        self.assertEqual(rc, 1)
        body = json.loads(out.getvalue())
        self.assertIn("already retracted", body["message"])
        self.assertIn("excluded from search", body["message"])
        self.assertNotIn("identical text", body["message"])

    def test_deterministic_duplicate_response_validates_external_fields(self):
        text = "pm2 restart needs --update-env before process env changes take effect."
        stored_hash = content_hash(text)
        for existing in (
            {"doc_id": 123, "content_hash": stored_hash, "retracted": False},
            {"doc_id": "", "content_hash": stored_hash, "retracted": False},
            {"doc_id": "contrib/CLAUDE/2026-11-05/472fc36e", "content_hash": "not-a-hash", "retracted": False},
            {"doc_id": "contrib/CLAUDE/2026-11-05/472fc36e", "content_hash": stored_hash},
        ):
            with self.subTest(existing=existing):
                with self.assertRaisesRegex(FleetRagError, "doc_id|content_hash|retracted"):
                    self.cli.deterministic_duplicate_message(existing, text)

    def test_contribute_without_text_is_a_usage_error(self):
        err = io.StringIO()
        with mock.patch("sys.stderr", err):
            rc = self.cli.main(["contribute", "--category", "lesson", "--seat", "CLAUDE"])
        self.assertEqual(rc, 2)
        self.assertIn("needs text", err.getvalue())
        self.assertEqual(FakeQdrant.upserts, [])

    def test_help_recommends_file_for_credentials(self):
        self.assertIn("--file", self.cli.__doc__)
        self.assertIn("credential", self.cli.__doc__)
        self.assertIn("stdin", self.cli.__doc__)


if __name__ == "__main__":
    unittest.main()
