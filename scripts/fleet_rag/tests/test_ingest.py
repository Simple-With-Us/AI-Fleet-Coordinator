"""Ingest orchestrator tests with fake embed / Qdrant / gitleaks / sentinel (no network)."""
from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import pathlib
import tempfile
import threading
import time
import unittest
from unittest import mock

from fleet_rag import health, ingest, mirror, qdrant_snapshot, sources
from fleet_rag.core import content_hash, point_id
from fleet_rag.scrub import GitleaksError


class FakeQdrant:
    """In-memory stand-in for core.Qdrant covering the calls ingest makes."""

    def __init__(self, cfg=None, collection=None):  # noqa: ANN001
        self.points: dict[str, dict] = {}
        self.upserts: list[list[str]] = []
        self.deleted: list[str] = []
        self.delete_calls: list[list[str]] = []
        self.payload_sets: list[tuple[list[str], dict]] = []
        self.scrolls: list[dict | None] = []
        self.collection = "fleet-agents"
        self.snapshot_names: list[str] = []
        self.snapshot_calls = 0

    def _cpath(self, suffix=""):  # noqa: ANN001
        return f"/collections/{self.collection}{suffix}"

    def _call(self, path, body=None, method=None, write=False, timeout=0):  # noqa: ANN001
        assert path.endswith("/points") and body and "ids" in body
        wp = body.get("with_payload", False)
        out = []
        for i in body["ids"]:
            if i not in self.points:
                continue
            pl = self.points[i]["payload"]
            if wp is True:
                out.append({"id": i, "payload": pl})
            elif isinstance(wp, list):
                out.append({"id": i, "payload": {k: pl[k] for k in wp if k in pl}})
            else:
                out.append({"id": i})
        return {"result": out}

    def upsert(self, points, wait=True):  # noqa: ANN001
        self.upserts.append([p["id"] for p in points])
        for p in points:
            self.points[p["id"]] = p
        return "completed"

    def delete_ids(self, ids, wait=True):  # noqa: ANN001
        assert ids, "delete_ids with an empty list"
        self.delete_calls.append(list(ids))
        self.deleted.extend(ids)
        for i in ids:
            self.points.pop(i, None)
        return "completed"

    def delete_by_filter(self, flt, wait=True):  # noqa: ANN001
        raise AssertionError("ingest must never delete by filter")

    def scroll(self, flt=None, limit=256, with_payload=True, with_vector=False):  # noqa: ANN001
        self.scrolls.append(flt)
        for pid, p in list(self.points.items()):
            if flt and any(c["match"]["value"] != p["payload"].get(c["key"]) for c in flt["must"]):
                continue
            yield {"id": pid, "payload": p["payload"] if with_payload else None}

    def set_payload(self, ids, payload, wait=True):  # noqa: ANN001
        self.payload_sets.append((list(ids), dict(payload)))
        for i in ids:
            self.points[i]["payload"].update(payload)
        return "completed"

    def create_snapshot(self, *, wait=True):  # noqa: ANN001
        self.snapshot_calls += 1
        name = f"ingest-snap-{self.snapshot_calls}"
        self.snapshot_names.append(name)
        return {"name": name}

    def list_snapshots(self):  # noqa: ANN001
        return [{"name": n} for n in self.snapshot_names]

    def delete_snapshot(self, name):  # noqa: ANN001
        self.snapshot_names = [n for n in self.snapshot_names if n != name]


def make_doc(doc_id: str, text: str, **kw) -> sources.Doc:
    base = dict(doc_id=doc_id, title=kw.pop("title", "T"), text_markdown=text, source="doc", app="fleet",
                category="doc", seat="FLEET", url="", path="", created_at_ms=1700000000000,
                updated_at_ms=1700000000000)
    base.update(kw)
    return sources.Doc(**base)


LONG_A = "# Alpha\n\n" + "\n\n".join(f"Paragraph {i} about alpha. " * 12 for i in range(6))
LONG_B = "# Beta\n\n" + "\n\n".join(f"Paragraph {i} about beta. " * 12 for i in range(4))


class IngestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.tmp.name)
        self.state = root / "state.json"
        self.last = root / "last-run.json"
        self.logf = root / "ingest.log"
        self.qd = FakeQdrant()
        self.embed_calls: list[list[str]] = []
        self.sentinels: list[dict] = []
        self.docs: dict[str, list[sources.Doc]] = {"doc": [make_doc("doc/a", LONG_A), make_doc("doc/b", LONG_B)]}

        def fake_embed(cfg, texts):  # noqa: ANN001
            self.embed_calls.append(list(texts))
            return [[0.1] * 4 for _ in texts]

        def gen(limit=None):  # noqa: ANN001
            yield from self.docs["doc"][:limit] if limit else self.docs["doc"]

        def fake_sentinel(cfg, qd, report):  # noqa: ANN001
            self.sentinels.append(json.loads(json.dumps(report)))
            return "sentinel-id"

        self.patches = [
            mock.patch.object(sources, "NIGHTLY_SOURCES", ("doc",)),
            mock.patch.object(ingest, "embed", fake_embed),
            mock.patch.object(ingest, "gitleaks_gate", lambda path: set()),
            mock.patch.object(ingest, "GENERATORS", {"doc": gen}),
            mock.patch.object(ingest, "EMBED_RATE", 1e9),
            mock.patch.object(ingest, "_heartbeat", lambda: None),
            mock.patch.object(ingest, "write_sentinel", fake_sentinel),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _run(self, **kw):  # noqa: ANN001
        kw.setdefault("prune", not (kw.get("limit") or kw.get("since")))
        return ingest.run("doc", cfg={"x": "y"}, qd=self.qd, state_path=self.state, last_run_path=self.last,
                          log_path=self.logf, log=lambda *a: None, **kw)

    def test_full_nonpruning_ingest_cannot_attest_nightly_health(self) -> None:
        with mock.patch.object(ingest, "_heartbeat") as heartbeat:
            self._run(prune=False)
        self.assertFalse(self.last.exists())
        self.assertEqual(self.sentinels, [])
        heartbeat.assert_not_called()

    def test_partial_ingest_preserves_global_nightly_health(self) -> None:
        self.last.write_text('{"ok": false, "started_at": 1}')
        before = self.last.read_bytes()
        with (mock.patch.object(sources, "NIGHTLY_SOURCES", ("doc", "zulip")),
              mock.patch.object(ingest, "_heartbeat") as heartbeat):
            report = self._run()
        self.assertTrue(report["ok"])
        self.assertEqual(self.last.read_bytes(), before)
        self.assertEqual(self.sentinels, [])
        heartbeat.assert_not_called()
        self.assertTrue(self.logf.exists())
        self.assertTrue(self.state.exists())

    def test_linear_delta_never_prunes_live_rows_even_with_full_flag(self) -> None:
        # Persist a complete earlier document, then yield no delta documents.
        with mock.patch.dict(ingest.GENERATORS, {"linear": lambda limit=None: iter([
            make_doc("linear/unchanged", LONG_A, source="linear")
        ])}):
            ingest.run("linear", cfg={"x": "y"}, qd=self.qd, state_path=self.state,
                       last_run_path=self.last, log_path=self.logf, log=lambda *a: None)
        before = json.loads(self.state.read_text())
        points = set(self.qd.points)
        for full in ("", "1"):
            with (mock.patch.object(sources, "NIGHTLY_SOURCES", ("linear",)),
                  mock.patch.dict(os.environ, {"FLEET_RAG_LINEAR_FULL": full}),
                  mock.patch.dict(ingest.GENERATORS, {"linear": lambda limit=None: iter([])})):
                report = ingest.run("linear", prune=True, cfg={"x": "y"}, qd=self.qd,
                                    state_path=self.state, last_run_path=self.last,
                                    log_path=self.logf, log=lambda *a: None)
            self.assertTrue(report["ok"])
            self.assertEqual(json.loads(self.state.read_text()), before)
            self.assertEqual(set(self.qd.points), points)
            self.assertTrue(any("pruning skipped" in w for w in report["warnings"]))

    def test_linear_receipt_separates_full_read_from_commit_and_dry_run(self) -> None:
        from fleet_rag.tests.test_sources import _linear_node
        for dry in (True, False):
            with (mock.patch.dict(ingest.GENERATORS, {"linear": sources.iter_linear}),
                  mock.patch.object(sources, "_linear_light_scan", return_value={
                      "uuid-1": "2026-10-21T01:00:00Z"}),
                  mock.patch.object(sources, "_linear_fetch_full", return_value=[_linear_node()]),
                  mock.patch.dict(os.environ, {"FLEET_RAG_LINEAR_FULL": "1"})):
                report = ingest.run("linear", dry_run=dry, cfg={"x": "y"}, qd=self.qd,
                    state_path=self.state, last_run_path=self.last, log_path=self.logf,
                    log=lambda *a: None)
            receipt = report["linear_read_receipt"]
            self.assertTrue(report["ok"])
            self.assertTrue(receipt["source_complete"])
            self.assertFalse(receipt["deletion_authorized"])
            self.assertEqual(not dry, receipt["issues"]["uuid-1"]["state_hash_verified"])

    def test_full_linear_skips_fail_after_complete_pending_docs_commit(self) -> None:
        from fleet_rag.tests.test_sources import _linear_node
        skipped = _linear_node(uid="uuid-skipped")
        skipped["_linear_truncated"] = True
        with (mock.patch.dict(ingest.GENERATORS, {"linear": sources.iter_linear}),
              mock.patch.object(sources, "_linear_light_scan", return_value={
                  "uuid-1": "2026-10-21", "uuid-skipped": "2026-10-21"}),
              mock.patch.object(sources, "_linear_fetch_full", return_value=[_linear_node(), skipped]),
              mock.patch.dict(os.environ, {"FLEET_RAG_LINEAR_FULL": "1"})):
            report = ingest.run("linear", cfg={"x": "y"}, qd=self.qd,
                state_path=self.state, last_run_path=self.last, log_path=self.logf,
                log=lambda *a: None)
        self.assertFalse(report["ok"])
        state = json.loads(self.state.read_text())
        self.assertIn("linear/uuid-1", state)
        self.assertNotIn("linear/uuid-skipped", state)
        self.assertNotIn(sources.LINEAR_CURSOR_KEY, state)
        receipt = report["linear_read_receipt"]
        self.assertFalse(receipt["source_complete"])
        self.assertTrue(receipt["issues"]["uuid-1"]["state_hash_verified"])
        self.assertEqual("skipped", receipt["issues"]["uuid-skipped"]["status"])

    def _linear_query(self, node):
        def fake_query(query, timeout_s=0):
            if "orderBy: updatedAt" in query:
                if "updatedAt: { gte:" in query and node["updatedAt"] < query.split('gte: "', 1)[1][:24]:
                    return {"issues": {"nodes": [], "pageInfo": {"hasNextPage": False}}}
                return {
                    "issues": {
                        "nodes": [{
                            "id": node["id"],
                            "identifier": node["identifier"],
                            "updatedAt": node["updatedAt"],
                        }],
                        "pageInfo": {"hasNextPage": False},
                    }
                }
            return {"issues": {"nodes": [node], "pageInfo": {"hasNextPage": False}}}

        return fake_query

    def test_cursor_advances_after_success_and_stays_on_failure(self) -> None:
        from fleet_rag.tests.test_sources import _linear_node

        node = _linear_node()
        now = sources.parse_ts_ms("2026-11-12T06:00:00.000Z")
        env = {"FLEET_RAG_LINEAR_STATE": str(self.state)}
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(sources, "now_ms", return_value=now),
            mock.patch.dict(ingest.GENERATORS, {"linear": sources.iter_linear}),
            mock.patch.object(sources, "_linear_query", side_effect=self._linear_query(node)),
        ):
            report = ingest.run(
                "linear", cfg={"x": "y"}, qd=self.qd, state_path=self.state,
                last_run_path=self.last, log_path=self.logf, log=lambda *a: None,
            )
        self.assertTrue(report["ok"])
        state = json.loads(self.state.read_text())
        sync = state[sources.LINEAR_CURSOR_KEY]
        self.assertEqual(sync["updated_at_cursor"], sources._linear_iso(now))
        self.assertEqual(sync["last_full_sweep_at_ms"], now)
        self.assertEqual(sync["kind"], "linear-sync")
        self.assertIn("linear/uuid-1", state)
        self.assertFalse(report["linear_read_receipt"]["deletion_authorized"])
        before = self.state.read_text()
        points = set(self.qd.points)

        def boom(query, timeout_s=0):
            raise sources.LinearSourceError("linear: transport down")

        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(sources, "now_ms", return_value=now + 60_000),
            mock.patch.dict(ingest.GENERATORS, {"linear": sources.iter_linear}),
            mock.patch.object(sources, "_linear_query", side_effect=boom),
        ):
            failed = ingest.run(
                "linear", cfg={"x": "y"}, qd=self.qd, state_path=self.state,
                last_run_path=self.last, log_path=self.logf, log=lambda *a: None,
            )
        self.assertFalse(failed["ok"])
        self.assertEqual(before, self.state.read_text())
        self.assertEqual(set(self.qd.points), points)

    def test_since_does_not_advance_the_cursor_past_a_skipped_issue(self) -> None:
        from fleet_rag.tests.test_sources import _linear_node

        node = _linear_node()
        now = sources.parse_ts_ms("2026-10-22T12:00:00.000Z")
        cursor = "2026-10-20T00:00:00.000Z"
        env = {"FLEET_RAG_LINEAR_STATE": str(self.state)}
        self.state.write_text(json.dumps({
            sources.LINEAR_CURSOR_KEY: {
                "kind": "linear-sync",
                "updated_at_cursor": cursor,
                "last_full_sweep_at_ms": sources.parse_ts_ms("2026-10-21T00:00:00.000Z"),
            }
        }))
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(sources, "now_ms", return_value=now),
            mock.patch.dict(ingest.GENERATORS, {"linear": sources.iter_linear}),
            mock.patch.object(sources, "_linear_query", side_effect=self._linear_query(node)),
        ):
            skipped = ingest.run(
                "linear", since="2026-10-22T00:00:00.000Z", cfg={"x": "y"}, qd=self.qd,
                state_path=self.state, last_run_path=self.last, log_path=self.logf,
                log=lambda *a: None,
            )
        self.assertTrue(skipped["ok"])
        self.assertEqual(skipped["per_source"]["linear"]["docs_seen"], 1)
        self.assertEqual(skipped["per_source"]["linear"]["docs_changed"], 0)
        self.assertFalse(skipped["linear_read_receipt"]["cursor_advance"])
        held = json.loads(self.state.read_text())
        self.assertEqual(held[sources.LINEAR_CURSOR_KEY]["updated_at_cursor"], cursor)
        self.assertNotIn("linear/uuid-1", held)
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(sources, "now_ms", return_value=now + 60_000),
            mock.patch.dict(ingest.GENERATORS, {"linear": sources.iter_linear}),
            mock.patch.object(sources, "_linear_query", side_effect=self._linear_query(node)),
        ):
            caught = ingest.run(
                "linear", cfg={"x": "y"}, qd=self.qd, state_path=self.state,
                last_run_path=self.last, log_path=self.logf, log=lambda *a: None,
            )
        self.assertTrue(caught["ok"])
        self.assertEqual(caught["per_source"]["linear"]["docs_changed"], 1)
        advanced = json.loads(self.state.read_text())
        self.assertIn("linear/uuid-1", advanced)
        self.assertEqual(
            advanced[sources.LINEAR_CURSOR_KEY]["updated_at_cursor"],
            sources._linear_iso(now + 60_000),
        )

    def test_unchanged_linear_issue_is_not_reembedded(self) -> None:
        from fleet_rag.tests.test_sources import _linear_node

        node = _linear_node()
        env = {"FLEET_RAG_LINEAR_STATE": str(self.state)}
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.dict(ingest.GENERATORS, {"linear": sources.iter_linear}),
            mock.patch.object(sources, "_linear_query", side_effect=self._linear_query(node)),
        ):
            first = ingest.run(
                "linear", cfg={"x": "y"}, qd=self.qd, state_path=self.state,
                last_run_path=self.last, log_path=self.logf, log=lambda *a: None,
            )
        self.assertGreater(first["per_source"]["linear"]["docs_changed"], 0)
        self.assertTrue(self.embed_calls)
        state = json.loads(self.state.read_text())
        state["linear/uuid-1"]["updated_at"] = 1
        state["linear/uuid-1"]["refreshed_at"] = 1
        del state[sources.LINEAR_CURSOR_KEY]
        self.state.write_text(json.dumps(state))
        self.embed_calls.clear()
        upserts = len(self.qd.upserts)
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.dict(ingest.GENERATORS, {"linear": sources.iter_linear}),
            mock.patch.object(sources, "_linear_query", side_effect=self._linear_query(node)),
        ):
            second = ingest.run(
                "linear", cfg={"x": "y"}, qd=self.qd, state_path=self.state,
                last_run_path=self.last, log_path=self.logf, log=lambda *a: None,
            )
        self.assertEqual(second["per_source"]["linear"]["docs_seen"], 1)
        self.assertEqual(second["per_source"]["linear"]["docs_changed"], 0)
        self.assertEqual(self.embed_calls, [])
        self.assertEqual(len(self.qd.upserts), upserts)
        again = json.loads(self.state.read_text())
        self.assertIn(sources.LINEAR_CURSOR_KEY, again)
        self.assertEqual(again["linear/uuid-1"]["doc_hash"], state["linear/uuid-1"]["doc_hash"])

    def test_incremental_empty_window_advances_cursor_without_pruning(self) -> None:
        from fleet_rag.tests.test_sources import _linear_node

        node = _linear_node()
        now = sources.parse_ts_ms("2026-11-12T06:00:00.000Z")
        env = {"FLEET_RAG_LINEAR_STATE": str(self.state)}
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(sources, "now_ms", return_value=now),
            mock.patch.dict(ingest.GENERATORS, {"linear": sources.iter_linear}),
            mock.patch.object(sources, "_linear_query", side_effect=self._linear_query(node)),
        ):
            ingest.run(
                "linear", cfg={"x": "y"}, qd=self.qd, state_path=self.state,
                last_run_path=self.last, log_path=self.logf, log=lambda *a: None,
            )
        points = set(self.qd.points)
        later = now + 60 * 60 * 1000
        with (
            mock.patch.dict(os.environ, env),
            mock.patch.object(sources, "now_ms", return_value=later),
            mock.patch.object(sources, "NIGHTLY_SOURCES", ("linear",)),
            mock.patch.dict(ingest.GENERATORS, {"linear": sources.iter_linear}),
            mock.patch.object(sources, "_linear_query", side_effect=self._linear_query(node)),
        ):
            report = ingest.run(
                "linear", prune=True, cfg={"x": "y"}, qd=self.qd, state_path=self.state,
                last_run_path=self.last, log_path=self.logf, log=lambda *a: None,
            )
        self.assertTrue(report["ok"])
        self.assertEqual(report["per_source"]["linear"]["docs_seen"], 0)
        self.assertTrue(report["linear_read_receipt"]["cursor_advance"])
        self.assertEqual(report["linear_read_receipt"]["read_mode"], "incremental")
        state = json.loads(self.state.read_text())
        self.assertEqual(state[sources.LINEAR_CURSOR_KEY]["updated_at_cursor"], sources._linear_iso(later))
        self.assertEqual(state[sources.LINEAR_CURSOR_KEY]["last_full_sweep_at_ms"], now)
        self.assertIn("linear/uuid-1", state)
        self.assertEqual(set(self.qd.points), points)
        self.assertTrue(any("pruning skipped" in w for w in report["warnings"]))

    def test_full_flag_is_scoped_to_the_run(self) -> None:
        seen = {}

        def fake_run(*args, **kwargs):
            seen["env"] = os.environ.get("FLEET_RAG_LINEAR_FULL")
            return {"ok": True}

        with mock.patch.object(ingest, "run", side_effect=fake_run):
            self.assertEqual(ingest.main(["--source", "linear", "--full", "--no-heartbeat"]), 0)
        self.assertEqual(seen["env"], "1")
        self.assertIsNone(os.environ.get("FLEET_RAG_LINEAR_FULL"))

    def test_cursor_record_is_not_a_prune_candidate(self) -> None:
        state = {
            "linear/uuid-1": {"source": "linear", "chunk_ids": ["p"]},
            sources.LINEAR_CURSOR_KEY: {
                "kind": "linear-sync",
                "updated_at_cursor": "2026-11-12T00:00:00.000Z",
            },
        }
        self.assertEqual(ingest._prune_candidates("linear", set(), state), ["linear/uuid-1"])

    def test_explicit_linear_prune_refused_before_any_write(self) -> None:
        for full in ("", "1"):
            with (mock.patch.dict(os.environ, {"FLEET_RAG_LINEAR_FULL": full}),
                  mock.patch.dict(ingest.GENERATORS, {"linear": lambda limit=None: iter([])})):
                with self.assertRaisesRegex(ingest.FleetRagError, "complete deletion inventory"):
                    ingest.run("linear", prune=True, cfg={"x": "y"}, qd=self.qd,
                               state_path=self.state, last_run_path=self.last,
                               log_path=self.logf, log=lambda *a: None)
            self.assertFalse(self.state.exists())
            self.assertFalse(self.logf.exists())
            self.assertFalse(self.qd.points)

    def test_first_run_then_idempotent(self) -> None:
        rep = self._run()
        self.assertTrue(rep["ok"])
        s = rep["per_source"]["doc"]
        self.assertEqual(s["docs_seen"], 2)
        self.assertEqual(s["docs_changed"], 2)
        self.assertGreater(s["chunks_new"], 2)
        self.assertEqual(s["chunks_new"], len(self.qd.points))
        self.assertEqual(sum(len(c) for c in self.embed_calls), s["chunks_new"])
        # payload v2 shape
        p = next(iter(self.qd.points.values()))["payload"]
        for k in ("text", "source", "app", "category", "seat", "doc_id", "chunk_index", "chunk_count", "heading",
                  "title", "url", "path", "created_at", "updated_at", "content_hash", "embed_model", "ingest_run"):
            self.assertIn(k, p)
        self.assertEqual(p["created_at"], 1700000000000)
        self.assertEqual(next(iter(self.qd.points)), point_id(content_hash(p["text"])))
        # state file shape
        st = json.loads(self.state.read_text())
        self.assertEqual(set(st), {"doc/a", "doc/b"})
        self.assertEqual(st["doc/a"]["doc_hash"], content_hash(LONG_A))
        self.assertEqual(sorted(st["doc/a"]["chunk_ids"] + st["doc/b"]["chunk_ids"]), sorted(self.qd.points))
        self.assertEqual(st["doc/a"]["updated_at"], 1700000000000)
        self.assertEqual(st["doc/a"]["source"], "doc")
        # last-run + log + sentinel
        last = json.loads(self.last.read_text())
        self.assertEqual(last["run_id"], rep["run_id"])
        self.assertEqual(set(last["per_source"]["doc"]), set(ingest._empty_stats()))
        self.assertEqual(last["sentinel"], "sentinel-id")
        self.assertIn(rep["run_id"], self.logf.read_text())
        self.assertEqual(len(self.sentinels), 1)
        self.assertTrue(self.sentinels[0]["ok"])
        self.assertEqual(self.sentinels[0]["run_id"], rep["run_id"])
        self.assertIsNotNone(self.sentinels[0]["finished_at"])

        # second run: nothing changed -> nothing embedded, nothing upserted
        self.embed_calls.clear()
        n_upserts = len(self.qd.upserts)
        rep2 = self._run()
        self.assertEqual(rep2["per_source"]["doc"]["docs_changed"], 0)
        self.assertEqual(rep2["per_source"]["doc"]["chunks_new"], 0)
        self.assertEqual(self.embed_calls, [])
        self.assertEqual(len(self.qd.upserts), n_upserts)
        self.assertEqual(len(self.sentinels), 2)

    def test_hash_skip_advances_provider_timestamp(self) -> None:
        self._run()
        st = json.loads(self.state.read_text())
        self.assertEqual(st["doc/a"]["updated_at"], 1700000000000)
        first_refresh = st["doc/a"]["refreshed_at"]
        self.assertIsInstance(first_refresh, int)
        # Provider bumps updatedAt while the text stays identical: nothing re-ingests,
        # but the state row tracks the newer provider timestamp and the hash-verified
        # refresh time, so state-selecting sources stop refetching the doc.
        self.docs["doc"] = [
            make_doc("doc/a", LONG_A, updated_at_ms=1700000000000 + 60000),
            make_doc("doc/b", LONG_B, updated_at_ms=1700000000000 + 60000),
        ]
        self.embed_calls.clear()
        n_upserts = len(self.qd.upserts)
        rep = self._run()
        self.assertEqual(rep["per_source"]["doc"]["docs_changed"], 0)
        self.assertEqual(self.embed_calls, [])
        self.assertEqual(len(self.qd.upserts), n_upserts)
        st = json.loads(self.state.read_text())
        self.assertEqual(st["doc/a"]["updated_at"], 1700000000000 + 60000)
        self.assertEqual(st["doc/b"]["updated_at"], 1700000000000 + 60000)
        self.assertGreaterEqual(st["doc/a"]["refreshed_at"], first_refresh)

    def test_dry_run_advances_no_state(self) -> None:
        """A dry run may hash-verify in memory, but nothing persists:  an uncommitted
        run can never suppress a later refetch through state."""
        self._run()
        before = self.state.read_text()
        self.docs["doc"] = [
            make_doc("doc/a", LONG_A, updated_at_ms=1700000000000 + 60000),
            make_doc("doc/b", LONG_B, updated_at_ms=1700000000000 + 60000),
        ]
        self._run(dry_run=True)
        self.assertEqual(before, self.state.read_text())

    def test_existing_points_not_reembedded_without_state(self) -> None:
        self._run()
        self.embed_calls.clear()
        self.state.unlink()                       # lose the state file, keep the collection
        rep = self._run()
        self.assertEqual(rep["per_source"]["doc"]["docs_changed"], 2)
        self.assertEqual(rep["per_source"]["doc"]["chunks_new"], 0)
        self.assertEqual(self.embed_calls, [])
        self.assertEqual(self.qd.payload_sets, [])   # same owners: nothing re-pointed

    def test_edit_deletes_stale_chunks(self) -> None:
        self._run()
        old_ids = set(json.loads(self.state.read_text())["doc/a"]["chunk_ids"])
        self.docs["doc"][0] = make_doc("doc/a", LONG_A.replace("Paragraph 5", "Changed 5"))
        self.embed_calls.clear()
        rep = self._run()
        s = rep["per_source"]["doc"]
        self.assertEqual(s["docs_changed"], 1)
        new_ids = set(json.loads(self.state.read_text())["doc/a"]["chunk_ids"])
        stale = old_ids - new_ids
        self.assertTrue(stale)
        self.assertEqual(s["chunks_deleted"], len(stale))
        self.assertEqual(set(self.qd.deleted), stale)
        self.assertEqual(s["chunks_new"], len(new_ids - old_ids))
        self.assertEqual(sum(len(c) for c in self.embed_calls), s["chunks_new"])
        self.assertTrue(stale.isdisjoint(self.qd.points))
        self.assertTrue(new_ids <= set(self.qd.points))

    def test_stale_deletion_unions_collection_ids(self) -> None:
        """Ids the collection holds under the doc_id but state never recorded are retired too."""
        self._run()
        orphan = "orphan-1"
        self.qd.points[orphan] = {"id": orphan, "payload": {"doc_id": "doc/a", "text": "old", "source": "doc"}}
        shared_orphan = "orphan-shared"
        self.qd.points[shared_orphan] = {"id": shared_orphan, "payload": {"doc_id": "doc/a", "text": "x"}}
        st = json.loads(self.state.read_text())
        st["doc/b"]["chunk_ids"].append(shared_orphan)        # credited to doc/b as well
        self.state.write_text(json.dumps(st))
        self.docs["doc"][0] = make_doc("doc/a", LONG_A + "\n\nOne more paragraph.\n")
        self.qd.scrolls.clear()
        rep = self._run()
        self.assertTrue(rep["ok"])
        self.assertIn(orphan, self.qd.deleted)
        self.assertNotIn(shared_orphan, self.qd.deleted)
        self.assertNotIn(orphan, self.qd.points)
        self.assertIn({"must": [{"key": "doc_id", "match": {"value": "doc/a"}}]}, self.qd.scrolls)
        # unchanged doc/b was not scrolled
        self.assertNotIn({"must": [{"key": "doc_id", "match": {"value": "doc/b"}}]}, self.qd.scrolls)

    def test_shared_chunk_survives_other_docs_edit(self) -> None:
        self.docs["doc"] = [make_doc("doc/a", "Same body text here.", title="Same"),
                            make_doc("doc/b", "Same body text here.", title="Same")]
        self._run()
        self.assertEqual(len(self.qd.points), 1)
        self.docs["doc"][1] = make_doc("doc/b", "Different body text now.", title="Same")
        self._run()
        st = json.loads(self.state.read_text())
        self.assertEqual(self.qd.deleted, [])
        self.assertIn(st["doc/a"]["chunk_ids"][0], self.qd.points)
        self.assertEqual(len(self.qd.points), 2)
        # doc/a is live (yielded first), so the shared point was never re-pointed to doc/b
        self.assertEqual(self.qd.payload_sets, [])

    def test_renamed_doc_repoints_shared_chunk(self) -> None:
        self.docs["doc"] = [make_doc("doc/old-name", "Stable body text here.", title="Same")]
        self._run()
        (pid,) = self.qd.points
        self.assertEqual(self.qd.points[pid]["payload"]["doc_id"], "doc/old-name")
        self.docs["doc"] = [make_doc("doc/new-name", "Stable body text here.", title="Same", url="u2")]
        self.embed_calls.clear()
        rep = self._run()
        s = rep["per_source"]["doc"]
        self.assertEqual(s["chunks_new"], 0)
        self.assertEqual(s["chunks_repointed"], 1)
        self.assertEqual(self.embed_calls, [])
        self.assertEqual(len(self.qd.payload_sets), 1)
        ids, payload = self.qd.payload_sets[0]
        self.assertEqual(ids, [pid])
        self.assertEqual(payload["doc_id"], "doc/new-name")
        self.assertEqual(payload["url"], "u2")
        for k in ("text", "content_hash", "embed_model"):
            self.assertNotIn(k, payload)
        self.assertEqual(self.qd.points[pid]["payload"]["doc_id"], "doc/new-name")
        self.assertEqual(self.qd.points[pid]["payload"]["text"].split("\n")[-1], "Stable body text here.")
        # dry-run counts but does not write
        self.docs["doc"] = [make_doc("doc/third", "Stable body text here.", title="Same")]
        self.state.unlink()
        rep = self._run(dry_run=True)
        self.assertEqual(rep["per_source"]["doc"]["chunks_repointed"], 1)
        self.assertEqual(len(self.qd.payload_sets), 1)

    def test_nightly_prune_keeps_apple_note_state_rows(self) -> None:
        self.docs["doc"] = [make_doc("doc/a", LONG_A)]
        self._run()
        st = json.loads(self.state.read_text())
        st["note/ABC/ICNote/p1"] = {
            "doc_hash": "h",
            "chunk_ids": ["pt-note"],
            "updated_at": 0,
            "source": "apple-note",
        }
        self.qd.points["pt-note"] = {
            "id": "pt-note",
            "payload": {"doc_id": "note/ABC/ICNote/p1", "text": "Pinned fleet note body text."},
        }
        self.state.write_text(json.dumps(st))
        self.docs["doc"] = [make_doc("doc/a", LONG_A)]
        rep = self._run(prune=True)
        self.assertTrue(rep["ok"])
        st_after = json.loads(self.state.read_text())
        self.assertIn("note/ABC/ICNote/p1", st_after)
        self.assertIn("pt-note", self.qd.points)

    def test_prune_removes_vanished_docs(self) -> None:
        self.docs["doc"] = [make_doc("doc/a", LONG_A), make_doc("doc/b", LONG_B),
                            make_doc("doc/c", "Shared tail text here.", title="S"),
                            make_doc("doc/d", "Shared tail text here.", title="S")]
        self._run(prune=False)
        st = json.loads(self.state.read_text())
        # doc/c and doc/d share one point; state credits it to both
        shared = st["doc/c"]["chunk_ids"]
        self.assertEqual(shared, st["doc/d"]["chunk_ids"])
        b_ids = st["doc/b"]["chunk_ids"]
        # a row from another source must never be pruned by this source
        st["board/zzz"] = {"doc_hash": "h", "chunk_ids": ["board-pt"], "updated_at": 0, "source": "board"}
        self.qd.points["board-pt"] = {"id": "board-pt", "payload": {"doc_id": "board/zzz", "text": "b"}}
        self.state.write_text(json.dumps(st))

        # doc/b and doc/c vanish from the source
        self.docs["doc"] = [make_doc("doc/a", LONG_A), make_doc("doc/d", "Shared tail text here.", title="S")]
        rep = self._run(prune=True, dry_run=True)
        self.assertEqual(rep["per_source"]["doc"]["docs_pruned"], 2)
        self.assertEqual(self.qd.deleted, [])                     # dry-run: nothing deleted
        self.assertEqual(self.qd.snapshot_calls, 0)
        self.assertIn("doc/b", json.loads(self.state.read_text()))

        rep = self._run(prune=True)
        self.assertTrue(rep["ok"])
        self.assertEqual(self.qd.snapshot_calls, 1)
        self.assertIn("qdrant_prune_snapshot", rep)
        s = rep["per_source"]["doc"]
        self.assertEqual(s["docs_pruned"], 2)
        self.assertEqual(s["chunks_deleted"], len(b_ids))
        self.assertEqual(sorted(self.qd.deleted), sorted(b_ids))
        self.assertTrue(all(i in self.qd.points for i in shared))   # shared with live doc/d: kept
        self.assertIn("board-pt", self.qd.points)
        st = json.loads(self.state.read_text())
        self.assertEqual(set(st), {"doc/a", "doc/d", "board/zzz"})
        self.assertTrue(all(len(c) <= ingest.DELETE_BATCH for c in self.qd.delete_calls))

    def test_prune_aborts_when_pre_prune_snapshot_fails(self) -> None:
        self.docs["doc"] = [make_doc("doc/a", LONG_A), make_doc("doc/b", LONG_B)]
        self._run(prune=False)
        self.docs["doc"] = [make_doc("doc/a", LONG_A)]

        def boom(qd, **_kwargs):
            raise qdrant_snapshot.PruneSnapshotError("api down")

        with mock.patch.object(qdrant_snapshot, "take_prune_snapshot", side_effect=boom):
            rep = self._run(prune=True)
        self.assertFalse(rep["ok"])
        self.assertEqual(self.qd.deleted, [])
        self.assertTrue(any("snapshot failed" in item for item in rep["errors"]))
        self.assertIn("doc/b", json.loads(self.state.read_text()))

    def test_prune_skipped_on_source_error_and_refused_with_limit(self) -> None:
        self._run()

        def boom(limit=None):  # noqa: ANN001
            yield self.docs["doc"][0]
            raise RuntimeError("db locked")

        with mock.patch.object(ingest, "GENERATORS", {"doc": boom}):
            rep = self._run(prune=True)
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["per_source"]["doc"]["docs_pruned"], 0)
        self.assertEqual(self.qd.deleted, [])
        self.assertIn("doc/b", json.loads(self.state.read_text()))
        with self.assertRaises(ingest.FleetRagError):
            self._run(prune=True, limit=1)
        # --since does not cause pruning: skipped-by-since docs are still yielded
        self.docs["doc"][1] = make_doc("doc/b", LONG_B, updated_at_ms=1500000000000)
        rep = self._run(prune=True, since=1600000000000)
        self.assertEqual(rep["per_source"]["doc"]["docs_pruned"], 0)
        self.assertIn("doc/b", json.loads(self.state.read_text()))

    def test_prune_deferred_and_skipped_when_any_later_source_fails(self) -> None:
        # AFC-677:  a failure in ANY source of the run blocks every prune, including a
        # source that ran (and finished cleanly) before the failure.
        self._run()
        self.docs["doc"] = [make_doc("doc/a", LONG_A)]

        def gen(limit=None):  # noqa: ANN001
            yield from self.docs["doc"]

        def broken(limit=None):  # noqa: ANN001
            raise RuntimeError("simple-board api query exited 4")
            yield  # pragma: no cover

        with mock.patch.object(sources, "NIGHTLY_SOURCES", ("doc", "linear")), \
                mock.patch.object(ingest, "GENERATORS", {"doc": gen, "linear": broken}):
            rep = ingest.run("doc,linear", cfg={"x": "y"}, qd=self.qd, state_path=self.state,
                             last_run_path=self.last, log_path=self.logf, log=lambda *a: None,
                             prune=True)
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["per_source"]["doc"]["docs_pruned"], 0)
        self.assertEqual(self.qd.deleted, [])
        self.assertIn("doc/b", json.loads(self.state.read_text()))
        self.assertTrue(any("prune skipped for every source" in w for w in rep["warnings"]))

    def test_prune_refused_when_more_docs_would_go_than_were_yielded(self) -> None:
        self.docs["doc"] = [make_doc(f"doc/x{i}", f"Body number {i} text here.", title=f"X{i}")
                            for i in range(3)]
        self._run()
        self.docs["doc"] = [make_doc("doc/x0", "Body number 0 text here.", title="X0")]
        rep = self._run(prune=True)
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["per_source"]["doc"]["docs_pruned"], 0)
        self.assertEqual(self.qd.deleted, [])
        self.assertTrue(any("yielded only 1" in e for e in rep["errors"]))
        self.assertTrue(any(t.get("reason") == "prune-refused" for t in rep["guard_trips"]))
        self.assertEqual(set(json.loads(self.state.read_text())), {"doc/x0", "doc/x1", "doc/x2"})

    def test_prune_refused_when_a_whole_repo_vanishes_unless_forced(self) -> None:
        keep = [make_doc(f"doc/Keep/k{i}.md", f"Keep body {i} text.", title=f"K{i}") for i in range(8)]
        lost = [make_doc(f"doc/Socratic-Trade/s{i}.md", f"Lost body {i} text.", title=f"S{i}")
                for i in range(5)]
        self.docs["doc"] = keep + lost
        self._run()
        self.docs["doc"] = keep
        rep = self._run(prune=True)
        self.assertFalse(rep["ok"])
        self.assertEqual(self.qd.deleted, [])
        self.assertTrue(any("doc/Socratic-Trade" in e and "missing" in e for e in rep["errors"]))
        with mock.patch.dict(os.environ, {ingest.PRUNE_FORCE_ENV: "1"}):
            rep = self._run(prune=True)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["per_source"]["doc"]["docs_pruned"], 5)
        self.assertTrue(any("overridden" in w for w in rep["warnings"]))

    def _hold(self, held: set[str]):
        docs = list(self.docs["doc"])

        def gen(limit=None):  # noqa: ANN001
            sources.DOC_HELD_IDS.update(held)
            yield from docs[:limit] if limit else docs

        return mock.patch.object(ingest, "GENERATORS", {"doc": gen})

    def _stamp_diet(self) -> None:
        ingest._write_diet_stamp(
            self.state,
            {"docs_before": 1, "docs_after": 1, "chunks_before": 1, "chunks_after": 1},
        )

    def test_duplicate_chunk_bodies_embed_once_and_keep_paths(self) -> None:
        body = "Shared paragraph about the fleet diet.\n"
        self.docs["doc"] = [
            make_doc("doc/one", body, title="One", path="docs/one.md"),
            make_doc("doc/two", body, title="Two", path="docs/two.md"),
        ]
        rep = self._run(prune=False)
        self.assertTrue(rep["ok"])
        s = rep["per_source"]["doc"]
        self.assertEqual(len(self.qd.points), 1)
        self.assertEqual(s["chunks_new"], 1)
        self.assertEqual(s["chunks_deduped"], 1)
        self.assertEqual(sum(len(c) for c in self.embed_calls), 1)
        payload = next(iter(self.qd.points.values()))["payload"]
        self.assertEqual(payload["path"], "docs/one.md")
        self.assertEqual(payload["also_paths"], ["docs/two.md"])
        self.assertEqual(payload["also_path_count"], 1)
        st = json.loads(self.state.read_text())
        canonical = st["doc/one"]["chunk_ids"]
        self.assertEqual(canonical, st["doc/two"]["chunk_ids"])
        self.assertEqual(canonical, [next(iter(self.qd.points))])

        rows, _scrubbed = ingest.doc_rows(self.docs["doc"][1], "run")
        own_id = rows[0]["id"]
        self.assertNotEqual(own_id, canonical[0])
        st["doc/two"]["chunk_ids"] = [own_id]
        self.state.write_text(json.dumps(st))
        self.qd.points[own_id] = {"id": own_id, "payload": dict(rows[0]["payload"])}
        self.qd.deleted.clear()
        self.embed_calls.clear()
        recorded: dict = {}
        real_delete = self.qd.delete_ids

        def spy_delete(ids, wait=True):  # noqa: ANN001
            recorded["state"] = json.loads(self.state.read_text())
            recorded["ids"] = list(ids)
            return real_delete(ids, wait=wait)

        self.qd.delete_ids = spy_delete
        rep2 = self._run(prune=False)
        self.assertIn(own_id, recorded["ids"])
        self.assertEqual(recorded["state"]["doc/two"]["chunk_ids"], canonical)
        self.assertNotIn(own_id, recorded["state"]["doc/two"]["chunk_ids"])
        self.assertEqual(rep2["per_source"]["doc"]["docs_changed"], 0)
        self.assertEqual(self.embed_calls, [])
        self.assertIn(own_id, self.qd.deleted)
        st = json.loads(self.state.read_text())
        self.assertEqual(st["doc/one"]["chunk_ids"], st["doc/two"]["chunk_ids"])
        self.assertNotIn(own_id, st["doc/two"]["chunk_ids"])
        self.assertGreaterEqual(rep2["per_source"]["doc"]["chunks_deduped"], 1)

    def test_unchanged_chunker_drift_does_not_delete(self) -> None:
        self._run(prune=False)
        st = json.loads(self.state.read_text())
        st["doc/a"]["chunk_ids"] = ["not-the-real-id"]
        self.state.write_text(json.dumps(st))
        self.qd.deleted.clear()
        rep = self._run(prune=False)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["per_source"]["doc"]["chunks_deleted"], 0)
        self.assertEqual(self.qd.deleted, [])
        self.assertEqual(
            json.loads(self.state.read_text())["doc/a"]["chunk_ids"], ["not-the-real-id"]
        )

    def test_diet_hold_blocks_prune_without_an_explicit_release(self) -> None:
        extra = make_doc("doc/generated/old.md", "Generated body text here.", title="Old")
        self.docs["doc"] = [
            make_doc("doc/keep/a.md", "Keep body alpha text.", title="A"),
            make_doc("doc/keep/b.md", "Keep body beta text.", title="B"),
            extra,
        ]
        self._run(prune=False)
        self.docs["doc"] = self.docs["doc"][:2]
        with self._hold({extra.doc_id}):
            rep = self._run(prune=True)
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["per_source"]["doc"]["docs_pruned"], 0)
        self.assertEqual(self.qd.deleted, [])
        self.assertIn(extra.doc_id, json.loads(self.state.read_text()))
        self.assertTrue(any("held from prune" in w for w in rep["warnings"]))

    def test_diet_prune_needs_a_matching_dry_run_stamp(self) -> None:
        extra = make_doc("doc/generated/old.md", "Generated body text here.", title="Old")
        keep = [make_doc(f"doc/keep/{i}.md", f"Keep body {i} text.", title=f"K{i}") for i in range(4)]
        self.docs["doc"] = keep + [extra]
        self._run(prune=False)
        self.docs["doc"] = keep
        with self._hold({extra.doc_id}):
            rep = self._run(prune=True, prune_excluded=True)
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["per_source"]["doc"]["docs_pruned"], 0)
        self.assertEqual(self.qd.deleted, [])
        self.assertIn(extra.doc_id, json.loads(self.state.read_text()))
        self.assertTrue(any(t.get("reason") == "diet-prune-needs-dry-run" for t in rep["guard_trips"]))
        with self._hold({extra.doc_id}), mock.patch.dict(os.environ, {ingest.PRUNE_FORCE_ENV: "1"}):
            forced = self._run(prune=True, prune_excluded=True)
        self.assertFalse(forced["ok"])
        self.assertEqual(self.qd.deleted, [])
        self.assertTrue(
            any(t.get("reason") == "diet-prune-needs-dry-run" for t in forced["guard_trips"])
        )

        self._stamp_diet()
        stamp = self.state.parent / ingest.DIET_REPORT_NAME
        data = json.loads(stamp.read_text())
        data["rules_hash"] = "not-the-rules"
        stamp.write_text(json.dumps(data))
        os.chmod(stamp, 0o600)
        with self._hold({extra.doc_id}):
            mismatch = self._run(prune=True, prune_excluded=True)
        self.assertFalse(mismatch["ok"])
        self.assertEqual(self.qd.deleted, [])

        self._stamp_diet()
        with self._hold({extra.doc_id}):
            rep = self._run(prune=True, prune_excluded=True)
        self.assertTrue(rep["ok"], rep["errors"])
        self.assertEqual(rep["per_source"]["doc"]["docs_pruned"], 1)
        self.assertNotIn(extra.doc_id, json.loads(self.state.read_text()))

    def test_diet_percentage_cap_blocks_and_force_lifts_only_with_a_stamp(self) -> None:
        extras = [
            make_doc("doc/generated/a.md", "Generated alpha text here.", title="Ga"),
            make_doc("doc/generated/b.md", "Generated beta text here.", title="Gb"),
        ]
        keep = [make_doc(f"doc/keep/{i}.md", f"Keep body {i} text.", title=f"K{i}") for i in range(4)]
        self.docs["doc"] = keep + extras
        self._run(prune=False)
        self.docs["doc"] = keep
        held = {d.doc_id for d in extras}
        self._stamp_diet()
        with self._hold(held):
            rep = self._run(prune=True, prune_excluded=True)
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["per_source"]["doc"]["docs_pruned"], 0)
        self.assertEqual(self.qd.deleted, [])
        self.assertTrue(any("20%" in e for e in rep["errors"]))
        self.assertEqual(set(json.loads(self.state.read_text())) >= held, True)
        with self._hold(held), mock.patch.dict(os.environ, {ingest.PRUNE_FORCE_ENV: "1"}):
            forced = self._run(prune=True, prune_excluded=True)
        self.assertTrue(forced["ok"], forced["errors"])
        self.assertEqual(forced["per_source"]["doc"]["docs_pruned"], 2)
        self.assertTrue(held.isdisjoint(json.loads(self.state.read_text())))
        self.assertTrue(any("overridden" in w for w in forced["warnings"]))

    def test_seen_guard_still_blocks_a_diet_release(self) -> None:
        extras = [
            make_doc("doc/generated/a.md", "Generated alpha text here.", title="Ga"),
            make_doc("doc/generated/b.md", "Generated beta text here.", title="Gb"),
        ]
        keep = make_doc("doc/keep/a.md", "Keep body alpha text.", title="A")
        self.docs["doc"] = [keep, *extras]
        self._run(prune=False)
        self.docs["doc"] = [keep]
        self._stamp_diet()
        with self._hold({d.doc_id for d in extras}):
            rep = self._run(prune=True, prune_excluded=True)
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["per_source"]["doc"]["docs_pruned"], 0)
        self.assertEqual(self.qd.deleted, [])
        self.assertTrue(any("yielded only 1" in e for e in rep["errors"]))
        self.assertTrue(any(t.get("reason") == "prune-refused" for t in rep["guard_trips"]))

    def test_diet_allowlist_releases_only_the_named_ids(self) -> None:
        generated = make_doc("doc/generated/old.md", "Generated body text here.", title="Old")
        vendored = make_doc("doc/vendored/lib.md", "Vendored body text here.", title="Ven")
        keep = [make_doc(f"doc/keep/{i}.md", f"Keep body {i} text.", title=f"K{i}") for i in range(5)]
        self.docs["doc"] = keep + [generated, vendored]
        self._run(prune=False)
        self.docs["doc"] = keep
        self._stamp_diet()
        env = {ingest.PRUNE_EXCLUDED_ALLOW_ENV: "doc/generated/*"}
        with self._hold({generated.doc_id, vendored.doc_id}), mock.patch.dict(os.environ, env):
            rep = self._run(prune=True)
        self.assertTrue(rep["ok"], rep["errors"])
        st = json.loads(self.state.read_text())
        self.assertNotIn(generated.doc_id, st)
        self.assertIn(vendored.doc_id, st)
        self.assertEqual(rep["per_source"]["doc"]["docs_pruned"], 1)

    def test_prune_cap_applies_only_when_diet_ids_are_among_the_gone_set(self) -> None:
        state = {
            f"doc/k{i}": {"source": "doc", "chunk_ids": [f"p{i}"], "doc_hash": "h"}
            for i in range(10)
        }
        yielded = {f"doc/k{i}" for i in range(8)}
        self.assertIsNone(
            ingest.prune_refusal("doc", yielded, state, diet_ids={"doc/k8", "doc/k9"})
        )
        over = {f"doc/k{i}" for i in range(7)}
        msg = ingest.prune_refusal("doc", over, state, diet_ids={"doc/k7"})
        self.assertIsNotNone(msg)
        self.assertIn("20%", msg)
        small = {
            f"doc/s{i}": {"source": "doc", "chunk_ids": [], "doc_hash": "h"} for i in range(4)
        }
        self.assertIsNone(
            ingest.prune_refusal("doc", {"doc/s0", "doc/s1"}, small, diet_ids=set())
        )
        self.assertIsNone(ingest.prune_refusal("doc", {"doc/s0", "doc/s1"}, small))

    def test_dry_run_reports_doc_and_chunk_counts(self) -> None:
        root = pathlib.Path(self.tmp.name) / "walk"
        repo = root / "repo"
        docs = repo / "docs"
        docs.mkdir(parents=True)
        body = "Same paragraph for the diet census.\n"
        (repo / "README.md").write_text("# Hello\n\n" + body)
        (docs / "a.md").write_text("# A\n\n" + body)
        (docs / "generated").mkdir()
        (docs / "generated" / "api.md").write_text("# API\n\n" + ("generated " * 30))
        (docs / "app.min.md").write_text("# Min\n\nnope\n")

        def gen(limit=None):  # noqa: ANN001
            yield from sources.iter_docs(
                repo,
                root / "no-ops",
                extra=[],
                limit=limit,
                code_dir=None,
                apps_dir=None,
                grok_home=None,
            )

        with mock.patch.object(ingest, "GENERATORS", {"doc": gen}):
            rep = self._run(dry_run=True, prune=True)
        self.assertTrue(rep["ok"], rep["errors"])
        diet = rep["doc_diet"]
        self.assertEqual(diet["docs_before"], 4)
        self.assertEqual(diet["docs_after"], 2)
        self.assertEqual(diet["docs_excluded"], 2)
        self.assertGreater(diet["chunks_before"], diet["chunks_after"])
        self.assertGreaterEqual(diet["chunks_deduped"], 1)
        self.assertEqual(diet["excluded_held"], 2)
        self.assertEqual(diet["excluded_released"], 0)
        stamp = self.state.parent / ingest.DIET_REPORT_NAME
        self.assertTrue(stamp.is_file())
        self.assertEqual(stamp.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(stamp.read_text())["rules_hash"], ingest.diet_rules_hash())
        self.assertEqual(self.qd.points, {})
        self.assertFalse(self.state.exists())

    def test_prune_excluded_flag_reaches_run(self) -> None:
        with mock.patch.object(ingest, "run", return_value={"ok": True}) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                ingest.main(["--source", "doc", "--prune", "--prune-excluded", "--no-heartbeat"]),
                0,
            )
        self.assertTrue(run.call_args.kwargs["prune_excluded"])

    def test_duplicate_doc_id_is_an_error(self) -> None:
        self.docs["doc"] = [make_doc("doc/a", LONG_A), make_doc("doc/a", LONG_B)]
        rep = self._run()
        self.assertFalse(rep["ok"])
        self.assertIn("duplicate doc_id 'doc/a'", rep["errors"][0])
        self.assertEqual(self.qd.points, {})          # the group was never written
        self.assertFalse(self.sentinels[0]["ok"])

    def test_gitleaks_error_fails_closed(self) -> None:
        def gate(path):  # noqa: ANN001
            raise GitleaksError("gitleaks dir exited 1: FTL boom")

        with mock.patch.object(ingest, "gitleaks_gate", gate):
            rep = self._run()
        self.assertFalse(rep["ok"])
        self.assertIn("GitleaksError", rep["errors"][0])
        self.assertEqual(self.qd.points, {})
        self.assertEqual(self.embed_calls, [])
        self.assertFalse(self.state.exists())
        self.assertFalse(self.sentinels[0]["ok"])
        # the CLI exits non-zero, and the raw-row path raises instead of writing
        with mock.patch.object(ingest, "gitleaks_gate", gate), \
                mock.patch.object(ingest, "run", lambda *a, **k: rep), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ingest.main(["--source", "doc", "--no-heartbeat"]), 1)
        with mock.patch.object(ingest, "gitleaks_gate", gate):
            with self.assertRaises(GitleaksError):
                ingest.ingest_rows([{"text": "row"}], {}, cfg={}, qd=self.qd, log=lambda *a: None)
        self.assertEqual(self.qd.points, {})

    def test_sentinel_written_ok_false_on_failure_and_real_writer(self) -> None:
        def boom(limit=None):  # noqa: ANN001
            raise RuntimeError("db locked")
            yield  # pragma: no cover

        with mock.patch.object(ingest, "GENERATORS", {"doc": boom}):
            rep = self._run()
        self.assertFalse(rep["ok"])
        self.assertEqual(len(self.sentinels), 1)
        self.assertFalse(self.sentinels[0]["ok"])
        self.assertEqual(rep["sentinel"], "sentinel-id")
        # dry-run never writes the sentinel
        self.sentinels.clear()
        self._run(dry_run=True)
        self.assertEqual(self.sentinels, [])
        # the real health writer lands a meta point in the collection with the outcome
        for p in self.patches:
            p.stop()
        try:
            with mock.patch.object(sources, "NIGHTLY_SOURCES", ("doc",)), \
                    mock.patch.object(ingest, "embed", lambda cfg, t: [[0.1] * 4 for _ in t]), \
                    mock.patch.object(health, "embed", lambda cfg, t: [[0.2] * 4 for _ in t]), \
                    mock.patch.object(ingest, "gitleaks_gate", lambda path: set()), \
                    mock.patch.object(ingest, "GENERATORS", {"doc": boom}), \
                    mock.patch.object(ingest, "_heartbeat", lambda: None):
                rep = self._run()
        finally:
            for p in self.patches:
                p.start()
        self.assertFalse(rep["ok"])
        (sid,) = self.qd.points
        self.assertEqual(rep["sentinel"], sid)
        pl = self.qd.points[sid]["payload"]
        self.assertEqual(pl["doc_id"], health.SENTINEL_DOC_ID)
        self.assertEqual(pl["source"], "meta")
        self.assertFalse(pl["ok"])
        self.assertEqual(pl["ingest_run"], rep["run_id"])
        self.assertIn("db locked", pl["summary"])
        # a failing sentinel write is reported and flips ok
        def bad_sentinel(cfg, qd, report):  # noqa: ANN001
            raise RuntimeError("qdrant down")

        with mock.patch.object(ingest, "write_sentinel", bad_sentinel):
            rep = self._run()
        self.assertFalse(rep["ok"])
        self.assertTrue(any("sentinel" in e for e in rep["errors"]))
        self.assertIsNone(rep["sentinel"])
        self.assertIn("ok=False", self.logf.read_text().splitlines()[-1])

    def test_lock_is_exclusive(self) -> None:
        lock = self.state.parent / "ingest.lock"
        rep = self._run()
        self.assertTrue(lock.exists())
        self.assertTrue(rep["ok"])
        fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(ingest.IngestLocked):
                self._run()
            with mock.patch.object(ingest, "run", side_effect=ingest.IngestLocked("held")):
                self.assertEqual(ingest.main(["--source", "doc"]), ingest.EXIT_LOCKED)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self.assertTrue(self._run()["ok"])       # released: runs again
        self.assertIsInstance(ingest.IngestLocked("x"), ingest.FleetRagError)

    def test_a_dead_pid_in_the_lock_file_is_removed_on_start(self) -> None:
        lock = self.state.parent / "ingest.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(
            f"99999999 2020-01-01T00:00:00+00:00 {ingest._ingest_lock_hostname()}\n",
            encoding="utf-8",
        )
        logs: list[str] = []

        def capture(*parts: object) -> None:
            logs.append(" ".join(str(p) for p in parts))

        rep = ingest.run(
            "doc",
            cfg={"x": "y"},
            qd=self.qd,
            state_path=self.state,
            last_run_path=self.last,
            log_path=self.logf,
            log=capture,
        )
        self.assertTrue(rep["ok"])
        self.assertTrue(any("removing stale lock" in line for line in logs))
        self.assertTrue(lock.exists())
        meta = ingest._read_ingest_lock_meta(lock)
        self.assertEqual(meta.get("pid"), os.getpid())

    def test_a_live_holder_writes_lock_held_to_the_nightly_summary(self) -> None:
        lock = self.state.parent / "ingest.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        summary = self.state.parent / "nightly-last.json"
        fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock.write_text(
            f"{os.getpid()} 2026-11-12T00:00:00+00:00 {ingest._ingest_lock_hostname()}\n",
            encoding="utf-8",
        )
        try:
            with mock.patch.dict(
                os.environ,
                {ingest.NIGHTLY_SUMMARY_ENV: str(summary)},
                clear=False,
            ):
                with self.assertRaises(ingest.IngestLocked) as ctx:
                    ingest.run(
                        ",".join(sources.NIGHTLY_SOURCES),
                        cfg={"x": "y"},
                        qd=self.qd,
                        state_path=self.state,
                        last_run_path=self.last,
                        log_path=self.logf,
                        log=lambda *a: None,
                        prune=True,
                        lock_timeout_s=0.0,
                    )
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        exc = ctx.exception
        self.assertEqual(exc.reason, "lock_held")
        self.assertEqual(exc.pid, os.getpid())
        body = json.loads(summary.read_text(encoding="utf-8"))
        self.assertEqual(body["reason"], "lock_held")
        self.assertEqual(body["ingest_lock_pid"], os.getpid())
        self.assertIn("lock_held", body["errors"])

    def test_a_partial_refresh_does_not_write_the_nightly_summary_on_lock_held(self) -> None:
        lock = self.state.parent / "ingest.lock"
        summary = self.state.parent / "nightly-last.json"
        fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock.write_text(
            f"{os.getpid()} 2026-11-12T00:00:00+00:00 {ingest._ingest_lock_hostname()}\n",
            encoding="utf-8",
        )
        try:
            with mock.patch.dict(
                os.environ,
                {ingest.NIGHTLY_SUMMARY_ENV: str(summary)},
                clear=False,
            ), mock.patch.object(sources, "NIGHTLY_SOURCES", ("doc", "zulip")):
                with self.assertRaises(ingest.IngestLocked):
                    self._run(lock_timeout_s=0.0)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self.assertFalse(summary.exists())

    def test_a_lock_without_pid_or_host_is_stale_only_past_the_age(self) -> None:
        lock = self.state.parent / "ingest.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("not-json\n", encoding="utf-8")
        old = time.time() - ingest.DEFAULT_INGEST_LOCK_STALE_S - 60
        os.utime(lock, (old, old))
        logs: list[str] = []

        def capture(*parts: object) -> None:
            logs.append(" ".join(str(p) for p in parts))

        rep = ingest.run(
            "doc",
            cfg={"x": "y"},
            qd=self.qd,
            state_path=self.state,
            last_run_path=self.last,
            log_path=self.logf,
            log=capture,
        )
        self.assertTrue(rep["ok"])
        self.assertTrue(any("missing pid or host" in line for line in logs))

        lock.write_text("not-json\n", encoding="utf-8")
        fresh = time.time() - 60
        os.utime(lock, (fresh, fresh))
        rep = ingest.run(
            "doc",
            cfg={"x": "y"},
            qd=self.qd,
            state_path=self.state,
            last_run_path=self.last,
            log_path=self.logf,
            log=capture,
        )
        self.assertTrue(rep["ok"])

    def test_the_nightly_pass_waits_for_the_lock_instead_of_failing(self) -> None:
        # The sheet sync's partial refresh holds this same lock for as long as it runs, and it
        # may hold it for its whole budget.  The nightly is the only run that attests the corpus
        # health, so it must not be the one that gives up:  it waits out the lock and runs.
        # Without the wait it exited 3 the moment the partial took the lock, left the health
        # sentinel stale, and paged the fleet for a night that never ran.
        lock = self.state.parent / "ingest.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        released = []

        def release_later() -> None:
            # Hold the lock the way a partial run would, then let the nightly in.
            time.sleep(0.2)
            released.append(True)
            fcntl.flock(fd, fcntl.LOCK_UN)

        worker = threading.Thread(target=release_later, daemon=True)
        worker.start()
        try:
            started = time.monotonic()
            #  The budget is 0.6 s, not 30 s:  IngestLock polls at
            #  min(5.0, max(0.1, deadline - now)),  so a large budget makes the first sleep a
            #  full 5 s and the test then sits out a whole poll interval to prove a 0.2 s
            #  release.  0.6 s still sleeps past the release at 0.2 s and keeps
            #  elapsed > 0.15 true,  while keeping the whole test under a second.
            rep = self._run(lock_timeout_s=0.6)
            elapsed = time.monotonic() - started
        finally:
            worker.join(5)
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        self.assertEqual(released, [True], "the wait gave up before the holder released")
        self.assertGreater(elapsed, 0.15, "it waited for the lock rather than failing at once")
        self.assertTrue(rep["ok"])

    def test_the_wait_is_bounded_and_a_partial_refresh_still_fails_fast(self) -> None:
        # Two properties the nightly wait must keep.  The wait is bounded, so a lock held by a
        # hung run cannot hang the nightly forever.  A partial refresh keeps the fast exit,
        # because the sheet sync's ingest is best-effort and a long wait there would hold its
        # own budget open.
        lock = self.state.parent / "ingest.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            started = time.monotonic()
            with self.assertRaises(ingest.IngestLocked):
                self._run(lock_timeout_s=0.3)
            bounded = time.monotonic() - started
            self.assertGreaterEqual(bounded, 0.25, "it waited for the lock before giving up")
            self.assertLess(bounded, 20.0, "the wait is bounded")
            # A partial refresh, which is what the sheet sync runs, asks for no wait at all.
            started = time.monotonic()
            with self.assertRaises(ingest.IngestLocked):
                self._run()
            self.assertLess(time.monotonic() - started, 1.0, "a partial refresh fails at once")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_the_cli_gives_the_nightly_the_wait_and_a_partial_refresh_none(self) -> None:
        # The wait is chosen by the command line, because that is where the run is identified:
        # only the full nightly pass attests the corpus health, so only it waits.  An operator
        # can override the budget, and 0 makes even the nightly fail at once.
        with mock.patch.object(ingest, "run", return_value={"ok": True}) as run:
            self.assertEqual(ingest.main(["--all", "--prune", "--no-heartbeat"]), 0)
        self.assertEqual(run.call_args.kwargs["lock_timeout_s"], 6000.0)
        with mock.patch.object(ingest, "run", return_value={"ok": True}) as run:
            self.assertEqual(ingest.main(["--source", "doc", "--no-heartbeat"]), 0)
        self.assertEqual(run.call_args.kwargs["lock_timeout_s"], 0.0)
        with mock.patch.object(ingest, "run", return_value={"ok": True}) as run:
            self.assertEqual(
                ingest.main(["--all", "--prune", "--lock-timeout", "0", "--no-heartbeat"]), 0
            )
        self.assertEqual(run.call_args.kwargs["lock_timeout_s"], 0.0)

    def test_an_enumerated_nightly_run_gets_the_wait_because_it_writes_the_sentinel(self) -> None:
        # The wait used to be keyed on the --all flag while the sentinel was keyed on the
        # resolved source set equalling NIGHTLY_SOURCES, so the two predicates disagreed and
        # this run attested health (it writes the sentinel and last-run.json) and yet was given
        # no wait:  a health-attesting run could still be locked out, which is the exact hole
        # the wait was added to close.  Both now key on the same predicate (Kody review, PR #800).
        enumerated = ",".join(sources.NIGHTLY_SOURCES)
        with mock.patch.object(ingest, "run", return_value={"ok": True}) as run:
            self.assertEqual(ingest.main(["--source", enumerated, "--prune", "--no-heartbeat"]), 0)
        self.assertEqual(
            run.call_args.kwargs["lock_timeout_s"],
            6000.0,
            "this run is a sentinel-writing nightly, so it must be given the wait",
        )
        # A truncated run over the same sources attests nothing, so it still fails at once.
        with mock.patch.object(ingest, "run", return_value={"ok": True}) as run:
            self.assertEqual(
                ingest.main(["--source", enumerated, "--prune", "--limit", "5", "--no-heartbeat"]), 0
            )
        self.assertEqual(run.call_args.kwargs["lock_timeout_s"], 0.0)

    def _claims(self) -> list[str]:
        """The names of the pending-nightly claims that sit beside the lock."""
        directory = self.state.parent / ingest.PENDING_NIGHTLY_NAME
        if not directory.is_dir():
            return []
        return sorted(p.name for p in directory.iterdir() if not p.name.startswith("."))

    def test_a_partial_refresh_refuses_the_lock_while_a_nightly_is_pending(self) -> None:
        # The late-start race:  a partial that arrives while the nightly is still waiting
        # used to take the lock in the gap between the waiter's polls and then hold it for its
        # whole budget, which is how a nightly got locked out even though it was willing to
        # wait.  A pending nightly now claims the queue, and a partial refuses to take the
        # lock from it (Kody review, PR #800).
        directory = self.state.parent / ingest.PENDING_NIGHTLY_NAME
        owner = "owner-a"
        ingest._write_pending_nightly(directory, ingest.now_ms() + 60_000, owner)
        try:
            #  prune=False makes this a partial refresh:  it attests nothing, so it must not
            #  take the lock from a nightly that is waiting for it.
            with self.assertRaises(ingest.NightlyPending):
                self._run(prune=False)
        finally:
            ingest._clear_pending_nightly(directory, owner)
        self.assertEqual(self._claims(), [], "the refusal left a claim behind")

    def test_a_pending_marker_that_has_expired_does_not_refuse_a_partial_refresh(self) -> None:
        # A nightly that was killed must not wedge the partial refreshes forever, so an
        # expired claim is treated as absent.
        directory = self.state.parent / ingest.PENDING_NIGHTLY_NAME
        owner = "owner-a"
        ingest._write_pending_nightly(directory, ingest.now_ms() - 1_000, owner)
        try:
            report = self._run(prune=False)
        finally:
            ingest._clear_pending_nightly(directory, owner)
        self.assertTrue(report["ok"])

    def test_a_nightly_clears_its_marker_when_it_is_done(self) -> None:
        # The marker is a queue claim, not a lock:  it must not outlive the run.
        #  This harness patches NIGHTLY_SOURCES to ("doc",),  so a pruning run over "doc" is
        #  a nightly here:  it claims the queue,  and the claim must not outlive the run.
        self._run(lock_timeout_s=60)
        self.assertEqual(self._claims(), [], "the pending claim outlived the run")

    def test_a_nightly_only_clears_its_own_claim(self) -> None:
        # Two nightlies can run at once.  The one that finishes first must not clear the claim
        # of the one that is still waiting, or a partial could take the lock from it and the
        # nightly would be locked out (Sentry review, PR #813).
        directory = self.state.parent / ingest.PENDING_NIGHTLY_NAME
        other = "owner-other"
        ingest._write_pending_nightly(directory, ingest.now_ms() + 60_000, other)
        try:
            self._run(lock_timeout_s=60)
            self.assertEqual(self._claims(), [other], "a run cleared another run's claim")
        finally:
            ingest._clear_pending_nightly(directory, other)

    def test_a_dry_run_does_not_claim_the_queue(self) -> None:
        # A dry run writes no sentinel, so it attests nothing:  it must not claim the queue,
        # or a diagnostic would refuse the partial refreshes for the length of the claim
        # (Sentry review, PR #813).
        report = self._run(dry_run=True, lock_timeout_s=60)
        self.assertTrue(report["ok"])
        self.assertEqual(self._claims(), [], "a dry run claimed the queue")

    def test_a_dry_run_refuses_the_lock_while_a_nightly_is_pending(self) -> None:
        # A dry run writes nothing, but it would still take the lock from a nightly that is
        # waiting for it, so it is refused the same way a partial refresh is.
        directory = self.state.parent / ingest.PENDING_NIGHTLY_NAME
        owner = "owner-a"
        ingest._write_pending_nightly(directory, ingest.now_ms() + 60_000, owner)
        try:
            with self.assertRaises(ingest.NightlyPending):
                self._run(dry_run=True)
        finally:
            ingest._clear_pending_nightly(directory, owner)

    def test_a_run_with_no_wait_budget_does_not_claim_the_queue(self) -> None:
        # A nightly with `--lock-timeout 0` never waits, so there is nothing for a claim to
        # protect, and holding one would refuse the partial refreshes for nothing.
        self._run(lock_timeout_s=0)
        self.assertEqual(self._claims(), [], "a run with no wait budget claimed the queue")

    def test_a_dry_run_gets_no_wait_because_it_attests_nothing(self) -> None:
        # The wait and the claim must both key on the same predicate, and a dry run attests
        # nothing:  the sentinel write sits behind `if not dry_run:`.
        with mock.patch.object(ingest, "run", return_value={"ok": True}) as run:
            self.assertEqual(
                ingest.main(["--all", "--prune", "--dry-run", "--no-heartbeat"]), 0
            )
        self.assertEqual(
            run.call_args.kwargs["lock_timeout_s"],
            0.0,
            "a dry run attests nothing, so it must not be given the nightly wait",
        )

    def test_gitleaks_drop_plumbing(self) -> None:
        staged: list[list[dict]] = []

        def fake_gate(path):  # noqa: ANN001
            rows = [json.loads(l) for l in pathlib.Path(path).read_text().splitlines()]
            staged.append(rows)
            return {n for n, r in enumerate(rows, 1) if "leak-me" in r["text"]}

        with mock.patch.object(ingest, "gitleaks_gate", fake_gate):
            self.docs["doc"] = [make_doc("doc/a", "Clean paragraph one.\n\n"),
                                make_doc("doc/b", "Paragraph with leak-me inside.\n")]
            rep = self._run()
        s = rep["per_source"]["doc"]
        self.assertEqual(s["chunks_dropped_by_gitleaks"], 1)
        self.assertEqual(s["chunks_new"], 1)
        self.assertEqual(len(self.qd.points), 1)
        self.assertTrue(all("leak-me" not in p["payload"]["text"] for p in self.qd.points.values()))
        self.assertEqual(staged[0][0]["doc_id"], "doc/a")
        st = json.loads(self.state.read_text())
        self.assertEqual(st["doc/b"]["chunk_ids"], [])       # dropped chunk is not tracked

    def test_scrub_counts_and_payload(self) -> None:
        self.docs["doc"] = [make_doc("doc/a", "Deploy note.\n\nSLACK_TOKEN=xoxb-123456789012-abcdefghijkl\n")]
        rep = self._run()
        self.assertEqual(rep["per_source"]["doc"]["chunks_scrubbed"], 1)
        p = next(iter(self.qd.points.values()))["payload"]
        self.assertIn("[REDACTED", p["text"])
        self.assertNotIn("xoxb-", p["text"])
        self.assertIn("slack-token", p["scrubbed"])

    def test_dry_run_writes_nothing(self) -> None:
        rep = self._run(dry_run=True)
        self.assertGreater(rep["per_source"]["doc"]["chunks_new"], 0)
        self.assertEqual(self.qd.points, {})
        self.assertEqual(self.embed_calls, [])
        self.assertFalse(self.state.exists())
        self.assertFalse(self.last.exists())
        self.assertEqual(self.sentinels, [])

    def test_source_error_is_reported_not_fatal(self) -> None:
        def boom(limit=None):  # noqa: ANN001
            raise RuntimeError("db locked")
            yield  # pragma: no cover

        with mock.patch.object(ingest, "GENERATORS", {"doc": boom}):
            with mock.patch.object(ingest.telemetry, "capture_exception") as cap:
                rep = self._run()
        self.assertFalse(rep["ok"])
        self.assertIn("db locked", rep["errors"][0])
        cap.assert_called_once()
        (exc,), kwargs = cap.call_args
        self.assertIsInstance(exc, RuntimeError)
        self.assertEqual(kwargs, {"operation": "ingest", "source": "doc"})
        with self.assertRaises(ingest.FleetRagError):
            ingest.run("nope", cfg={}, qd=self.qd, state_path=self.state, log=lambda *a: None)

    def test_source_warnings_reach_the_report(self) -> None:
        def warny(limit=None):  # noqa: ANN001
            sources.warn("Notes unavailable; served 3 cached notes")
            yield from self.docs["doc"]

        with mock.patch.object(ingest, "GENERATORS", {"doc": warny}):
            rep = self._run()
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["warnings"], ["doc: Notes unavailable; served 3 cached notes"])
        self.assertEqual(sources.WARNINGS, [])
        self.assertIn("warnings=1", self.logf.read_text())

    def test_since_filter(self) -> None:
        self.docs["doc"][1] = make_doc("doc/b", LONG_B, updated_at_ms=1500000000000)
        rep = self._run(since=1600000000000)
        self.assertEqual(rep["per_source"]["doc"]["docs_seen"], 2)
        self.assertEqual(rep["per_source"]["doc"]["docs_changed"], 1)
        self.assertEqual(ingest._parse_since("2026-08-31T22:00:00Z"), 1788213600000)
        self.assertIsNone(ingest._parse_since(None))
        self.assertGreater(ingest._parse_since("7d"), 0)

    def test_fix_seeds(self) -> None:
        self.assertEqual(ingest.SEED_MS, 1788213600000)
        pid = "seed-1"
        self.qd.points[pid] = {"id": pid, "payload": {"doc_id": ingest.SEED_DOC_ID, "created_at": 0}}
        self.qd.points["other"] = {"id": "other", "payload": {"doc_id": "doc/x", "created_at": 5}}
        n = ingest.fix_seeds(dry_run=True, cfg={}, qd=self.qd, log=lambda *a: None)
        self.assertEqual(n, 1)
        self.assertEqual(self.qd.payload_sets, [])
        n = ingest.fix_seeds(cfg={}, qd=self.qd, log=lambda *a: None)
        self.assertEqual(n, 1)
        self.assertEqual(self.qd.points[pid]["payload"]["created_at"], 1788213600000)
        self.assertEqual(self.qd.points[pid]["payload"]["updated_at"], ingest.SEED_MS)
        self.assertEqual(self.qd.points["other"]["payload"]["created_at"], 5)

    def test_ingest_rows(self) -> None:
        rows = [{"text": "Row one about Qdrant."}, {"text": "Row two with token=abcdefghijklmnop1234", "app": "ST"}]
        res = ingest.ingest_rows(rows, {"source": "doc", "app": "fleet", "category": "lesson", "seat": "claude",
                                        "doc_id": "rows/test"}, cfg={}, qd=self.qd, log=lambda *a: None)
        self.assertEqual(res["written"], 2)
        self.assertEqual(res["scrubbed"], 1)
        payloads = [p["payload"] for p in self.qd.points.values()]
        self.assertEqual({p["doc_id"] for p in payloads}, {"rows/test"})
        self.assertEqual({p["seat"] for p in payloads}, {"CLAUDE"})
        self.assertIn("socratic-trade", {p["app"] for p in payloads})
        self.assertTrue(all(p["chunk_count"] == 2 for p in payloads))
        res2 = ingest.ingest_rows(rows, {"doc_id": "rows/other"}, cfg={}, qd=self.qd, log=lambda *a: None)
        self.assertEqual(res2["written"], 0)
        self.assertEqual(res2["already_present"], 2)
        self.assertEqual(self.qd.payload_sets, [])     # raw rows never re-point existing chunks

    def _gens(self, **by_source):
        def make(name):
            rows = by_source.get(name, [])

            def gen(limit=None):  # noqa: ANN001
                yield from rows[:limit] if limit else rows

            return gen

        return {
            name: make(name)
            for name in ("linear", "doc", "skill", "memory", "apple-note", "chat-log")
        }

    def _multi(self, selection, **kw):
        return ingest.run(
            selection,
            cfg={"x": "y"},
            qd=self.qd,
            state_path=self.state,
            last_run_path=self.last,
            log_path=self.logf,
            log=lambda *a: None,
            **kw,
        )

    def test_a_partial_linear_prune_stays_refused_and_the_server_set_is_allowed(self) -> None:
        with self.assertRaises(ingest.FleetRagError):
            self._multi("linear,doc", prune=True)
        with mock.patch.object(ingest, "GENERATORS", self._gens()):
            rep = self._multi("linear,doc,skill,memory", prune=True)
        self.assertTrue(rep["ok"], rep["errors"])
        self.assertTrue(any("linear: pruning skipped" in w for w in rep["warnings"]))
        self.assertTrue(
            ingest._attests_health(["linear", "doc", "skill", "memory"], True, None, None)
        )

    def test_apple_note_is_never_pruned_even_when_force_is_set(self) -> None:
        one = make_doc("apple-note/one", LONG_A, source="apple-note")
        two = make_doc("apple-note/two", LONG_B, source="apple-note")
        with mock.patch.object(ingest, "GENERATORS", self._gens(**{"apple-note": [one, two]})):
            first = self._multi("apple-note", prune=True)
        self.assertTrue(first["ok"], first["errors"])
        with (
            mock.patch.object(ingest, "GENERATORS", self._gens(**{"apple-note": [one]})),
            mock.patch.dict(os.environ, {ingest.PRUNE_FORCE_ENV: "1"}),
        ):
            second = self._multi("apple-note", prune=True)
        self.assertTrue(second["ok"], second["errors"])
        self.assertIn("apple-note/two", json.loads(self.state.read_text()))
        self.assertTrue(any("write-time upserts" in w for w in second["warnings"]))
        self.assertFalse(any(t["source"] == "apple-note" for t in second["guard_trips"]))

    def test_stale_or_missing_mirror_skips_skill_and_memory_prune(self) -> None:
        doc_a = make_doc("doc/a", LONG_A)
        doc_b = make_doc("doc/b", LONG_B)
        skill_a = make_doc("skill/claude/alpha", LONG_A, source="skill")
        skill_b = make_doc("skill/claude/beta", LONG_B, source="skill")
        memory = make_doc("memory/codex/a.md", LONG_A, source="memory")
        with mock.patch.object(
            ingest,
            "GENERATORS",
            self._gens(doc=[doc_a, doc_b], skill=[skill_a, skill_b], memory=[memory]),
        ):
            first = self._multi("linear,doc,skill,memory", prune=True)
        self.assertTrue(first["ok"], first["errors"])
        mirror_dir = pathlib.Path(self.tmp.name) / "mirror"
        mirror_dir.mkdir()
        (mirror_dir / ".complete").write_text(
            json.dumps({"complete": True, "at": int(time.time()) - 40 * 3600, "files": {}}) + "\n"
        )
        env = {mirror.MIRROR_ENV: str(mirror_dir), ingest.PRUNE_FORCE_ENV: "1"}
        with (
            mock.patch.object(
                ingest,
                "GENERATORS",
                self._gens(doc=[doc_a], skill=[skill_a], memory=[]),
            ),
            mock.patch.dict(os.environ, env),
        ):
            second = self._multi("linear,doc,skill,memory", prune=True)
        self.assertTrue(second["ok"], second["errors"])
        state = json.loads(self.state.read_text())
        self.assertNotIn("doc/b", state)
        self.assertIn("skill/claude/beta", state)
        self.assertIn("memory/codex/a.md", state)
        self.assertEqual(second["per_source"]["skill"]["docs_seen"], 0)
        self.assertEqual(second["per_source"]["memory"]["docs_seen"], 0)
        self.assertEqual(second["per_source"]["doc"]["docs_seen"], 1)
        self.assertGreaterEqual(second["mirror_stale"]["age_s"], 40 * 3600 - 5)
        self.assertEqual(second["mirror_stale"]["reason"], "stale")
        self.assertTrue(str(second["mirror_stale"]["marker"]).endswith("/.complete"))
        reasons = {(t["source"], t["reason"]) for t in second["guard_trips"]}
        self.assertEqual(reasons, {("skill", "stale"), ("memory", "stale")})

    def test_a_fresh_mirror_and_an_unset_mirror_still_prune_skill(self) -> None:
        skill_a = make_doc("skill/claude/alpha", LONG_A, source="skill")
        skill_b = make_doc("skill/claude/beta", LONG_B, source="skill")
        with mock.patch.object(ingest, "GENERATORS", self._gens(skill=[skill_a, skill_b])):
            first = self._multi("skill", prune=True)
        self.assertTrue(first["ok"], first["errors"])
        with mock.patch.object(ingest, "GENERATORS", self._gens(skill=[skill_a])):
            unset = self._multi("skill", prune=True)
        self.assertTrue(unset["ok"], unset["errors"])
        self.assertNotIn("skill/claude/beta", json.loads(self.state.read_text()))
        self.assertEqual(unset["guard_trips"], [])

        with mock.patch.object(ingest, "GENERATORS", self._gens(skill=[skill_a, skill_b])):
            self._multi("skill", prune=True)
        mirror_dir = pathlib.Path(self.tmp.name) / "fresh"
        mirror_dir.mkdir()
        (mirror_dir / ".complete").write_text(
            json.dumps({"complete": True, "at": int(time.time()), "files": {}}) + "\n"
        )
        with (
            mock.patch.object(ingest, "GENERATORS", self._gens(skill=[skill_a])),
            mock.patch.dict(os.environ, {mirror.MIRROR_ENV: str(mirror_dir)}),
        ):
            fresh = self._multi("skill", prune=True)
        self.assertTrue(fresh["ok"], fresh["errors"])
        self.assertNotIn("skill/claude/beta", json.loads(self.state.read_text()))
        self.assertEqual(fresh["guard_trips"], [])
        self.assertIsNone(fresh["mirror_stale"])
        self.assertGreater(fresh["per_source"]["skill"]["docs_seen"], 0)

    def test_stale_mirror_skips_apple_note_ingest_and_never_prunes_it(self) -> None:
        note_a = make_doc("note/one", LONG_A, source="apple-note")
        note_b = make_doc("note/two", LONG_B, source="apple-note")
        doc_a = make_doc("doc/a", LONG_A)
        doc_b = make_doc("doc/b", LONG_B)
        called: list[str] = []

        def note_gen(limit=None):  # noqa: ANN001
            called.append("apple-note")
            raise AssertionError("apple-note ingest ran")

        with mock.patch.object(
            ingest,
            "GENERATORS",
            self._gens(**{"apple-note": [note_a, note_b], "doc": [doc_a, doc_b]}),
        ):
            first = self._multi("doc,apple-note", prune=True)
        self.assertTrue(first["ok"], first["errors"])
        mirror_dir = pathlib.Path(self.tmp.name) / "notes-mirror"
        mirror_dir.mkdir()
        env = {mirror.MIRROR_ENV: str(mirror_dir), ingest.PRUNE_FORCE_ENV: "1"}
        gens = self._gens(doc=[doc_a])
        gens["apple-note"] = note_gen
        with mock.patch.object(ingest, "GENERATORS", gens), mock.patch.dict(os.environ, env):
            second = self._multi("doc,apple-note", prune=True)
        self.assertTrue(second["ok"], second["errors"])
        self.assertEqual(called, [])
        state = json.loads(self.state.read_text())
        self.assertIn("note/two", state)
        self.assertNotIn("doc/b", state)
        self.assertEqual(second["per_source"]["apple-note"]["docs_seen"], 0)
        self.assertEqual(second["per_source"]["doc"]["docs_seen"], 1)
        self.assertEqual(second["mirror_stale"]["reason"], "missing-stamp")
        self.assertIsNone(second["mirror_stale"]["age_s"])
        reasons = {(t["source"], t["reason"]) for t in second["guard_trips"]}
        self.assertIn(("apple-note", "missing-stamp"), reasons)
        self.assertEqual(second["per_source"]["apple-note"]["docs_pruned"], 0)

    def test_mirror_max_age_env_is_the_ingest_skip_limit(self) -> None:
        skill_a = make_doc("skill/claude/alpha", LONG_A, source="skill")
        skill_b = make_doc("skill/claude/beta", LONG_B, source="skill")
        with mock.patch.object(ingest, "GENERATORS", self._gens(skill=[skill_a, skill_b])):
            self._multi("skill", prune=True)
        mirror_dir = pathlib.Path(self.tmp.name) / "age-mirror"
        mirror_dir.mkdir()
        (mirror_dir / ".complete").write_text(
            json.dumps({"complete": True, "at": int(time.time()) - 2 * 3600, "files": {}}) + "\n"
        )
        with (
            mock.patch.object(ingest, "GENERATORS", self._gens(skill=[skill_a])),
            mock.patch.dict(os.environ, {mirror.MIRROR_ENV: str(mirror_dir)}),
        ):
            fresh = self._multi("skill", prune=True)
        self.assertIsNone(fresh["mirror_stale"])
        self.assertNotIn("skill/claude/beta", json.loads(self.state.read_text()))
        with mock.patch.object(ingest, "GENERATORS", self._gens(skill=[skill_a, skill_b])):
            self._multi("skill", prune=True)
        short = {mirror.MIRROR_ENV: str(mirror_dir), "FLEET_RAG_MIRROR_MAX_AGE_S": "3600"}
        with (
            mock.patch.object(ingest, "GENERATORS", self._gens(skill=[skill_a])),
            mock.patch.dict(os.environ, short),
        ):
            skipped = self._multi("skill", prune=True)
        self.assertTrue(skipped["ok"], skipped["errors"])
        self.assertEqual(skipped["per_source"]["skill"]["docs_seen"], 0)
        self.assertIn("skill/claude/beta", json.loads(self.state.read_text()))
        self.assertEqual(skipped["mirror_stale"]["max_age_s"], 3600)
        self.assertGreater(skipped["mirror_stale"]["age_s"], 3600)

    def test_server_job_skips_doc_prune_when_the_code_root_is_unset(self) -> None:
        self._run()
        self.docs["doc"] = [make_doc("doc/a", LONG_A)]
        with mock.patch.dict(os.environ, {"FLEET_RAG_REQUIRE_CODE_ROOT": "1", "FLEET_CODE_ROOT": ""}):
            rep = self._run(prune=True)
        self.assertTrue(rep["ok"], rep["errors"])
        self.assertIn("doc/b", json.loads(self.state.read_text()))
        self.assertTrue(any(t["reason"] == "code-root-unset" for t in rep["guard_trips"]))


if __name__ == "__main__":
    unittest.main()
