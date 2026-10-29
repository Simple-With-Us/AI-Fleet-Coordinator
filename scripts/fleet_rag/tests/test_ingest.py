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

from fleet_rag import health, ingest, sources
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

    def test_prune_removes_vanished_docs(self) -> None:
        self.docs["doc"] = [make_doc("doc/a", LONG_A), make_doc("doc/b", LONG_B),
                            make_doc("doc/c", "Shared tail text here.", title="S"),
                            make_doc("doc/d", "Shared tail text here.", title="S")]
        self._run()
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
        self.assertIn("doc/b", json.loads(self.state.read_text()))

        rep = self._run(prune=True)
        self.assertTrue(rep["ok"])
        s = rep["per_source"]["doc"]
        self.assertEqual(s["docs_pruned"], 2)
        self.assertEqual(s["chunks_deleted"], len(b_ids))
        self.assertEqual(sorted(self.qd.deleted), sorted(b_ids))
        self.assertTrue(all(i in self.qd.points for i in shared))   # shared with live doc/d: kept
        self.assertIn("board-pt", self.qd.points)
        st = json.loads(self.state.read_text())
        self.assertEqual(set(st), {"doc/a", "doc/d", "board/zzz"})
        self.assertTrue(all(len(c) <= ingest.DELETE_BATCH for c in self.qd.delete_calls))

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


if __name__ == "__main__":
    unittest.main()
