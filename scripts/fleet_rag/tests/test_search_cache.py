"""In-process LRU for recall_search.

    cd scripts && python3 -m unittest fleet_rag.tests.test_search_cache -v
"""
from __future__ import annotations

import json
import os
import pathlib
import tempfile
import unittest
from unittest import mock

from fleet_rag import recall_api, search_cache
from fleet_rag.core import FleetRagError
from fleet_rag.recall_api import FakeQdrant


class SearchCacheRecallTests(unittest.TestCase):
    def setUp(self) -> None:
        recall_api.install_fake_backend()
        self.embeds = 0
        self.embedded: list[list[str]] = []
        real = recall_api.embed

        def counting(cfg, texts):  # noqa: ANN001
            self.embeds += 1
            self.embedded.append(list(texts))
            return real(cfg, texts)

        recall_api.embed = counting

    def tearDown(self) -> None:
        search_cache.reset()

    def test_repeated_search_skips_embed_and_qdrant(self) -> None:
        first = recall_api.recall_search("Coolify deploy", limit=2)
        calls = len(FakeQdrant.calls)
        groups = len(FakeQdrant.group_calls)
        embeds = self.embeds
        second = recall_api.recall_search("  COOLIFY   deploy ", limit=2)
        self.assertEqual(second["hits"], first["hits"])
        self.assertEqual(second["mode"], first["mode"])
        self.assertEqual(len(FakeQdrant.calls), calls)
        self.assertEqual(len(FakeQdrant.group_calls), groups)
        self.assertEqual(self.embeds, embeds)
        counted = search_cache.stats()
        self.assertEqual(counted["hits"], 1)
        self.assertGreaterEqual(counted["misses"], 1)
        self.assertEqual(counted["max_entries"], 256)
        self.assertEqual(counted["ttl_seconds"], 600.0)
        self.assertEqual(recall_api.recall_stats()["search_cache"]["hits"], 1)

    def test_limit_app_and_view_flags_are_part_of_the_key(self) -> None:
        recall_api.recall_search("Coolify deploy", limit=2, app="fleet")
        embeds = self.embeds
        recall_api.recall_search("Coolify deploy", limit=3, app="fleet")
        recall_api.recall_search("Coolify deploy", limit=2, app="ops")
        other = recall_api.recall_search("Coolify deploy", limit=2, compact=False, full=True)
        same = recall_api.recall_search("Coolify deploy", limit=2, app="fleet")
        self.assertEqual(self.embeds, embeds + 3)
        self.assertEqual(other["hits"], same["hits"])

    def test_since_days_stays_a_hit_when_the_cutoff_moves(self) -> None:
        recall_api.recall_search("Coolify deploy", limit=2, since_days=7)
        embeds = self.embeds
        with mock.patch("fleet_rag.recall_api.now_ms", return_value=10**15):
            recall_api.recall_search("Coolify deploy", limit=2, since_days=7)
        self.assertEqual(self.embeds, embeds)
        self.assertEqual(search_cache.stats()["hits"], 1)

    def test_the_embedder_and_reranker_keep_the_caller_casing(self) -> None:
        recall_api.recall_search("  Coolify Deploy ", limit=2)
        self.assertEqual(self.embedded[-1], ["Coolify Deploy"])
        seen: list[str] = []

        def fake_rerank(cfg, query, docs):  # noqa: ANN001
            seen.append(query)
            return [0.2] * len(docs)

        with (
            mock.patch.object(recall_api, "rerank_configured", return_value=True),
            mock.patch.object(recall_api, "rerank", fake_rerank),
        ):
            recall_api.recall_search("Coolify Deploy", limit=2, rerank=True)
        self.assertEqual(seen, ["Coolify Deploy"])
        embeds = self.embeds
        recall_api.recall_search("coolify deploy", limit=2)
        self.assertEqual(self.embeds, embeds)

    def test_ttl_expires_on_the_injected_clock(self) -> None:
        clock = [0.0]
        search_cache.configure(max_entries=8, ttl_s=10, now=lambda: clock[0])
        recall_api.recall_search("Coolify deploy", limit=2)
        embeds = self.embeds
        clock[0] = 9
        recall_api.recall_search("Coolify deploy", limit=2)
        self.assertEqual(self.embeds, embeds)
        clock[0] = 10
        recall_api.recall_search("Coolify deploy", limit=2)
        self.assertEqual(self.embeds, embeds + 1)

    def test_lru_drops_the_oldest_entry(self) -> None:
        search_cache.configure(max_entries=2, ttl_s=100)
        recall_api.recall_search("alpha query unique", limit=1)
        recall_api.recall_search("beta query unique", limit=1)
        recall_api.recall_search("gamma query unique", limit=1)
        embeds = self.embeds
        recall_api.recall_search("alpha query unique", limit=1)
        self.assertEqual(self.embeds, embeds + 1)
        embeds = self.embeds
        recall_api.recall_search("gamma query unique", limit=1)
        self.assertEqual(self.embeds, embeds)

    def test_callers_cannot_mutate_the_stored_hit(self) -> None:
        first = recall_api.recall_search("Coolify deploy", limit=2)
        first["hits"][0]["text"] = "mutated by the caller"
        second = recall_api.recall_search("Coolify deploy", limit=2)
        self.assertNotEqual(second["hits"][0]["text"], "mutated by the caller")
        self.assertEqual(self.embeds, 1)

    def test_size_zero_does_not_store(self) -> None:
        search_cache.configure(max_entries=0, ttl_s=100)
        recall_api.recall_search("Coolify deploy", limit=1)
        embeds = self.embeds
        recall_api.recall_search("Coolify deploy", limit=1)
        self.assertEqual(self.embeds, embeds + 1)
        self.assertEqual(search_cache.stats()["entries"], 0)

    def test_a_bad_flag_does_not_count_as_a_miss(self) -> None:
        before = search_cache.stats()["misses"]
        with self.assertRaises(FleetRagError):
            recall_api.recall_search("Coolify deploy", compact="yes")  # type: ignore[arg-type]
        self.assertEqual(search_cache.stats()["misses"], before)

    def test_ingest_completion_forces_the_next_search_to_embed(self) -> None:
        recall_api.recall_search("Coolify deploy", limit=2)
        embeds = self.embeds
        with tempfile.TemporaryDirectory() as tmp:
            search_cache.note_ingest_complete(
                {"run_id": "run-1", "ok": True, "finished_at": 1},
                epoch_path=pathlib.Path(tmp) / "search-cache-epoch.json",
            )
            body = json.loads((pathlib.Path(tmp) / "search-cache-epoch.json").read_text())
        self.assertEqual(body["run_id"], "run-1")
        self.assertTrue(body["ok"])
        recall_api.recall_search("Coolify deploy", limit=2)
        self.assertEqual(self.embeds, embeds + 1)

    def test_a_contribution_publishes_the_epoch_for_other_processes(self) -> None:
        good = (
            "pm2 start does not re-read env from the ecosystem file; restart with --update-env "
            "or delete and start the app again so the cached PATH is replaced."
        )
        replacement = (
            "pm2 start only applies a new ecosystem env after restart --update-env; "
            "check pm2 describe for the process env before debugging PATH."
        )
        with tempfile.TemporaryDirectory() as tmp:
            epoch = pathlib.Path(tmp) / "search-cache-epoch.json"
            other = search_cache.SearchCache()
            other.configure(marker_path=epoch, max_entries=4, ttl_s=60)
            _, token = other.lookup("k")
            other.store("k", {"hits": [], "mode": "hybrid"}, token)
            search_cache.configure(isolated=False)
            with mock.patch.dict(os.environ, {search_cache.EPOCH_ENV: str(epoch)}):
                created = recall_api.recall_contribute(good, "lesson", seat="CLAUDE")
                body = json.loads(epoch.read_text())
                self.assertTrue(body["run_id"].startswith("contribute:"))
                missed, _ = other.lookup("k")
                self.assertIsNone(missed)
                _, token = other.lookup("k")
                other.store("k", {"hits": [], "mode": "hybrid"}, token)
                written = epoch.read_bytes()
                duplicate = recall_api.recall_contribute(good, "lesson", seat="CLAUDE")
                self.assertEqual(duplicate["status"], "duplicate")
                self.assertEqual(epoch.read_bytes(), written)
                hit, _ = other.lookup("k")
                self.assertEqual(hit["mode"], "hybrid")
                recall_api.recall_correct(
                    created["doc_id"], replacement, "make the restart command exact", seat="CLAUDE"
                )
                missed, _ = other.lookup("k")
                self.assertIsNone(missed)
                self.assertTrue(json.loads(epoch.read_text())["run_id"].startswith("correct:"))
                _, token = other.lookup("k")
                other.store("k", {"hits": [], "mode": "hybrid"}, token)
                same = recall_api.recall_correct(
                    created["doc_id"], replacement, "make the restart command exact", seat="CLAUDE"
                )
                self.assertEqual(same["status"], "unchanged")
                self.assertTrue(json.loads(epoch.read_text())["run_id"].startswith("correct:"))
                hit, _ = other.lookup("k")
                self.assertEqual(hit["mode"], "hybrid")
                recall_api.recall_retract(
                    created["doc_id"], "the PATH lesson was retracted", seat="CLAUDE"
                )
                missed, _ = other.lookup("k")
                self.assertIsNone(missed)
                self.assertTrue(json.loads(epoch.read_text())["run_id"].startswith("retract:"))
                _, token = other.lookup("k")
                other.store("k", {"hits": [], "mode": "hybrid"}, token)
                written = epoch.read_bytes()
                again = recall_api.recall_retract(
                    created["doc_id"], "the PATH lesson was retracted", seat="CLAUDE"
                )
                self.assertEqual(again["status"], "already_retracted")
                self.assertEqual(epoch.read_bytes(), written)
                hit, _ = other.lookup("k")
                self.assertEqual(hit["mode"], "hybrid")


class SearchCacheSettingsTests(unittest.TestCase):
    def tearDown(self) -> None:
        search_cache.reset()

    def test_unparsable_values_fall_back_to_the_defaults(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"RECALL_SEARCH_CACHE_SIZE": "nope", "RECALL_SEARCH_CACHE_TTL_SECONDS": "-1"},
        ):
            self.assertEqual(search_cache.read_settings(), (256, 600.0))

    def test_infisical_wins_over_the_environment(self) -> None:
        values = {"RECALL_SEARCH_CACHE_SIZE": "8", "RECALL_SEARCH_CACHE_TTL_SECONDS": "30"}

        def fake_get(key, default=None):  # noqa: ANN001
            return values.get(key, default)

        with (
            mock.patch.object(search_cache.infisical_settings, "get", fake_get),
            mock.patch.dict(os.environ, {"RECALL_SEARCH_CACHE_SIZE": "4", "RECALL_SEARCH_CACHE_TTL_SECONDS": "9"}),
        ):
            self.assertEqual(search_cache.read_settings(), (8, 30.0))

    def test_a_rewritten_marker_changes_the_token(self) -> None:
        first = search_cache.SearchCache()
        second = search_cache.SearchCache()
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "search-cache-epoch.json"
            path.write_text("one\n", encoding="utf-8")
            first.configure(marker_path=path, max_entries=4, ttl_s=60)
            second.configure(marker_path=path, max_entries=4, ttl_s=60)
            stored = []
            for cache in (first, second):
                _, token = cache.lookup("k")
                cache.store("k", {"hits": [], "mode": "hybrid"}, token)
                hit, seen = cache.lookup("k")
                self.assertEqual(hit["mode"], "hybrid")
                stored.append(seen)
            path.write_text("two-more\n", encoding="utf-8")
            for cache, before in zip((first, second), stored):
                missed, after = cache.lookup("k")
                self.assertIsNone(missed)
                self.assertNotEqual(after, before)
                self.assertTrue(after.startswith("g0|"))

    def test_a_stale_token_is_not_stored(self) -> None:
        cache = search_cache.SearchCache()
        cache.reset()
        _, token = cache.lookup("k")
        cache.invalidate()
        cache.store("k", {"hits": []}, token)
        self.assertEqual(cache.stats()["entries"], 0)
        self.assertEqual(cache.stats()["misses"], 1)
