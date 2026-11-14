"""Capacity archival and correlation ids for the outcome store.

The safety case these tests pin:

- a store full of held unknown rows admits new writes by archiving the oldest
  unknowns - evidence preserved verbatim in the archive, duplicate denial
  preserved through the index;
- an archived unknown refuses exactly as a live one, across restarts;
- a reconciliation finish() releases an archived key and keeps the archive;
- a torn archive tail is quarantined (never silently dropped) and mid-file
  corruption fails closed;
- every accepted attempt gets a random correlation id that finish() keeps;
- nothing about archival touches rows whose key is not archived.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from git_relay import outcomes
from git_relay.outcomes import OutcomeStore, OutcomeStoreError

# Small caps keep the proof fast; the logic is size-independent.
MAX_ENTRIES = 20
ARCHIVE_BATCH = 5


def hexkey(i: int) -> str:
    return f"{i:064x}"


class Clock:
    def __init__(self):
        self.now = 1_700_000_000.0

    def __call__(self):
        self.now += 1.0
        return self.now


class ArchiveTestCase(unittest.TestCase):
    def setUp(self):
        self._caps = (
            mock.patch.object(outcomes, "MAX_ENTRIES", MAX_ENTRIES),
            mock.patch.object(outcomes, "ARCHIVE_BATCH", ARCHIVE_BATCH),
        )
        for patcher in self._caps:
            patcher.start()
        self.addCleanup(lambda: [p.stop() for p in self._caps])
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.path = str(self.dir / "outcomes.json")
        self.archive = self.path + ".archive.jsonl"
        self.index = self.path + ".archive-index.json"
        self.clock = Clock()

    def tearDown(self):
        self._tmp.cleanup()

    def fill_unknowns(self, store, count):
        for i in range(count):
            key = hexkey(i)
            self.assertIsNone(store.begin(key))
            store.finish(key, "unknown")

    def read_archive(self):
        with open(self.archive, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def test_capacity_archives_oldest_unknowns_and_admits(self):
        store = OutcomeStore(self.path, clock=self.clock)
        self.fill_unknowns(store, MAX_ENTRIES)
        new_key = "f" * 64
        # The write that hits the wall is admitted through archival, not refused.
        self.assertIsNone(store.begin(new_key))
        entries = self.read_archive()
        self.assertEqual(len(entries), ARCHIVE_BATCH)
        # Oldest first, evidence verbatim (state and ts preserved).
        self.assertEqual([e["key"] for e in entries], [hexkey(i) for i in range(ARCHIVE_BATCH)])
        self.assertEqual(entries[0]["row"]["state"], "unknown")
        self.assertIn("archived_at", entries[0])
        with open(self.index, encoding="utf-8") as fh:
            index = json.load(fh)
        self.assertEqual(len(index), ARCHIVE_BATCH)

    def test_archived_unknown_refuses_across_restart(self):
        store = OutcomeStore(self.path, clock=self.clock)
        self.fill_unknowns(store, MAX_ENTRIES)
        self.assertIsNone(store.begin("f" * 64))
        oldest = hexkey(0)
        held = store.begin(oldest)
        self.assertIsNotNone(held, "archived unknown must still refuse a replay")
        self.assertEqual(held["state"], "unknown")
        self.assertTrue(held.get("archived"))
        reloaded = OutcomeStore(self.path, clock=self.clock)
        held = reloaded.begin(oldest)
        self.assertIsNotNone(held)
        self.assertEqual(held["state"], "unknown")
        # A never-archived unknown refuses too, and a fresh key is admitted.
        self.assertIsNotNone(reloaded.begin(hexkey(MAX_ENTRIES - 1)))
        self.assertIsNone(reloaded.begin("e" * 64))

    def test_reconciliation_finish_releases_archived_key(self):
        store = OutcomeStore(self.path, clock=self.clock)
        self.fill_unknowns(store, MAX_ENTRIES)
        self.assertIsNone(store.begin("f" * 64))
        oldest = hexkey(0)
        self.assertIsNotNone(store.begin(oldest))
        store.finish(oldest, "ok", {"reconciled": True})
        self.assertEqual(store.begin(oldest)["state"], "ok")
        with open(self.index, encoding="utf-8") as fh:
            self.assertNotIn(oldest, json.load(fh))
        # The archive evidence stays append-only even after release.
        self.assertIn(oldest, [e["key"] for e in self.read_archive()])

    def test_index_rebuilt_from_archive_when_index_missing(self):
        store = OutcomeStore(self.path, clock=self.clock)
        self.fill_unknowns(store, MAX_ENTRIES)
        self.assertIsNone(store.begin("f" * 64))
        os.remove(self.index)
        reloaded = OutcomeStore(self.path, clock=self.clock)
        self.assertIsNotNone(reloaded.begin(hexkey(0)))
        self.assertTrue(os.path.exists(self.index), "rebuild rewrites the index")

    def test_torn_archive_tail_quarantined_not_dropped(self):
        store = OutcomeStore(self.path, clock=self.clock)
        self.fill_unknowns(store, MAX_ENTRIES)
        self.assertIsNone(store.begin("f" * 64))
        with open(self.archive, "ab") as fh:
            fh.write(b'{"key": "abc123')
        reloaded = OutcomeStore(self.path, clock=self.clock)
        self.assertIsNotNone(reloaded.begin(hexkey(0)))
        quarantines = [p for p in self.dir.iterdir() if ".corrupt-" in p.name]
        self.assertEqual(len(quarantines), 1)
        self.assertEqual(quarantines[0].read_bytes(), b'{"key": "abc123')
        # The archive itself holds only complete lines after repair.
        self.read_archive()

    def test_midfile_archive_corruption_fails_closed(self):
        store = OutcomeStore(self.path, clock=self.clock)
        self.fill_unknowns(store, MAX_ENTRIES)
        self.assertIsNone(store.begin("f" * 64))
        os.remove(self.index)
        data = bytearray(Path(self.archive).read_bytes())
        # Corrupt the first line without changing line structure.
        data[10] ^= 0xFF
        Path(self.archive).write_bytes(bytes(data))
        with self.assertRaises(OutcomeStoreError):
            OutcomeStore(self.path, clock=self.clock)
        self.assertTrue(Path(self.archive).exists(), "evidence file untouched")

    def test_pending_rows_are_never_archived(self):
        store = OutcomeStore(self.path, clock=self.clock)
        # One in-flight pending row plus a wall of unknowns.
        self.assertIsNone(store.begin("d" * 64))
        self.fill_unknowns(store, MAX_ENTRIES - 1)
        self.assertIsNone(store.begin("f" * 64))
        entries = self.read_archive()
        self.assertNotIn("d" * 64, [e["key"] for e in entries])
        self.assertEqual(store.begin("d" * 64)["state"], "pending")

    def test_unarchived_rows_untouched_by_archival(self):
        store = OutcomeStore(self.path, clock=self.clock)
        self.fill_unknowns(store, MAX_ENTRIES)
        youngest = hexkey(MAX_ENTRIES - 1)
        self.assertIsNone(store.begin("f" * 64))
        row = store.begin(youngest)
        self.assertIsNotNone(row)
        self.assertNotIn(youngest, [e["key"] for e in self.read_archive()])

    def test_corr_assigned_preserved_and_exposed(self):
        store = OutcomeStore(self.path, clock=self.clock)
        key = "a" * 64
        self.assertIsNone(store.begin(key))
        corr = store.corr_of(key)
        self.assertIsInstance(corr, str)
        self.assertEqual(len(corr), 32)
        store.finish(key, "ok", {"n": 1})
        self.assertEqual(store.corr_of(key), corr, "finish preserves the attempt's corr")
        store.finish(key, "unknown")
        self.assertEqual(store.corr_of(key), corr)

    def test_corr_distinct_per_attempt(self):
        store = OutcomeStore(self.path, clock=self.clock)
        key = "a" * 64
        self.assertIsNone(store.begin(key))
        first = store.corr_of(key)
        store.finish(key, "ok")
        self.assertIsNone(store.begin(key, reapply_ok=True))
        second = store.corr_of(key)
        self.assertNotEqual(first, second, "a new attempt gets a fresh corr")

    def test_corr_survives_archival_and_reconciliation(self):
        store = OutcomeStore(self.path, clock=self.clock)
        self.fill_unknowns(store, MAX_ENTRIES)
        self.assertIsNone(store.begin("f" * 64))
        oldest = hexkey(0)
        corr = store.corr_of(oldest)
        self.assertIsNotNone(corr)
        self.assertEqual(store.corr_of(oldest), corr, "archived key keeps its corr")
        store.finish(oldest, "ok")
        self.assertEqual(store.corr_of(oldest), corr)

    def test_rows_without_corr_validate(self):
        # Rows written before correlation existed (the live 272's shape) load fine.
        key = "b" * 64
        Path(self.path).write_text(json.dumps({key: {"state": "unknown", "ts": 1.0}}))
        store = OutcomeStore(self.path, clock=self.clock)
        self.assertIsNone(store.corr_of(key))
        self.assertEqual(store.begin(key)["state"], "unknown")

    def test_in_memory_store_unchanged_cap_behavior(self):
        store = OutcomeStore(None, clock=self.clock)
        self.fill_unknowns(store, MAX_ENTRIES)
        with self.assertRaises(OutcomeStoreError):
            store.begin("f" * 64)

    def test_released_key_not_resurrected_by_index_rebuild(self):
        store = OutcomeStore(self.path, clock=self.clock)
        self.fill_unknowns(store, MAX_ENTRIES)
        self.assertIsNone(store.begin("f" * 64))
        oldest = hexkey(0)
        store.finish(oldest, "ok", {"reconciled": True})
        old_corr = store.corr_of(oldest)
        os.remove(self.index)
        reloaded = OutcomeStore(self.path, clock=self.clock)
        row = reloaded.begin(oldest)
        self.assertIsNotNone(row)
        self.assertEqual(
            row["state"], "ok", "the reconciled row rules, never the stale archived unknown"
        )
        self.assertFalse(row.get("archived", False))
        # A later attempt carries its own fresh correlation, never the held one.
        self.assertIsNone(reloaded.begin(oldest, reapply_ok=True, corr="e" * 32))
        self.assertEqual(reloaded.corr_of(oldest), "e" * 32)
        self.assertNotEqual(old_corr, "e" * 32)
        # The archive log keeps BOTH the hold and the release as evidence.
        kinds = [("released_at" in e) for e in self.read_archive() if e["key"] == oldest]
        self.assertEqual(kinds, [False, True])

    def test_incomplete_index_still_denies_archived_duplicate(self):
        store = OutcomeStore(self.path, clock=self.clock)
        self.fill_unknowns(store, MAX_ENTRIES)
        self.assertIsNone(store.begin("f" * 64))
        oldest = hexkey(0)
        # Simulate an index that lost a key (partial write recovery gap):
        with open(self.index, encoding="utf-8") as fh:
            index = json.load(fh)
        del index[oldest]
        with open(self.index, "w", encoding="utf-8") as fh:
            json.dump(index, fh)
        reloaded = OutcomeStore(self.path, clock=self.clock)
        held = reloaded.begin(oldest)
        self.assertIsNotNone(held, "a valid-but-incomplete index must not admit the duplicate")
        self.assertEqual(held["state"], "unknown")
        with open(self.index, encoding="utf-8") as fh:
            self.assertIn(oldest, json.load(fh), "the fold restores the missing denial")

    def test_stale_index_entry_dropped_after_release_marker(self):
        store = OutcomeStore(self.path, clock=self.clock)
        self.fill_unknowns(store, MAX_ENTRIES)
        self.assertIsNone(store.begin("f" * 64))
        oldest = hexkey(0)
        with open(self.index, encoding="utf-8") as fh:
            stale_index = json.load(fh)
        store.finish(oldest, "ok")
        # Simulate the crash window:  release marker landed, index write did not.
        with open(self.index, "w", encoding="utf-8") as fh:
            json.dump(stale_index, fh)
        reloaded = OutcomeStore(self.path, clock=self.clock)
        row = reloaded.begin(oldest)
        self.assertIsNotNone(row)
        self.assertEqual(row["state"], "ok", "the release marker wins over the stale index entry")

    def test_thread_race_at_capacity_stays_consistent(self):
        import threading as _threading

        store = OutcomeStore(self.path, clock=self.clock)
        self.fill_unknowns(store, MAX_ENTRIES - 1)
        errors = []

        def worker(n):
            try:
                store.begin(f"{n:064x}")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [_threading.Thread(target=worker, args=(1000 + n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        with open(self.index, encoding="utf-8") as fh:
            index = json.load(fh)
        archived_keys = {e["key"] for e in self.read_archive()}
        self.assertEqual(set(index), archived_keys, "index and archive never disagree")
        total = len(store._rows) + len(archived_keys)
        self.assertEqual(total, MAX_ENTRIES - 1 + 6, "no row lost or duplicated under the lock")

    def test_begin_binds_caller_corr(self):
        store = OutcomeStore(self.path, clock=self.clock)
        key = "a" * 64
        self.assertIsNone(store.begin(key, corr="c" * 32))
        self.assertEqual(store.corr_of(key), "c" * 32)
        store.finish(key, "ok")
        self.assertEqual(store.corr_of(key), "c" * 32)
        self.assertIsNone(store.begin(key, reapply_ok=True, corr="d" * 32))
        self.assertEqual(store.corr_of(key), "d" * 32, "each attempt binds its own corr")

    def test_valid_index_missing_archive_fails_closed(self):
        store = OutcomeStore(self.path, clock=self.clock)
        self.fill_unknowns(store, MAX_ENTRIES)
        self.assertIsNone(store.begin("f" * 64))
        self.assertTrue(os.path.exists(self.archive))
        self.assertTrue(os.path.exists(self.index))
        os.unlink(self.archive)
        with self.assertRaises(OutcomeStoreError):
            OutcomeStore(self.path, clock=self.clock)

    def test_append_repairs_torn_tail_before_writing(self):
        store = OutcomeStore(self.path, clock=self.clock)
        self.fill_unknowns(store, MAX_ENTRIES)
        self.assertIsNone(store.begin("f" * 64))
        archived_key = hexkey(0)
        before = open(self.archive, "rb").read()
        # Simulate an interrupted earlier write: a fragment with no newline.
        with open(self.archive, "ab") as fh:
            fh.write(b'{"key": "deadbeef')
        # finish() on the archived key appends a release marker:  it must
        # quarantine the fragment and truncate back to the intact prefix
        # first, never glue its row onto the fragment.
        store.finish(archived_key, "ok")
        data = open(self.archive, "rb").read()
        self.assertTrue(data.endswith(b"\n"))
        self.assertTrue(data.startswith(before))
        lines = [ln for ln in data.split(b"\n") if ln]
        # No glued line survives.
        entries = [json.loads(ln) for ln in lines]
        self.assertEqual(entries[-1]["key"], archived_key)
        self.assertIn("released_at", entries[-1])
        quarantines = list(self.dir.glob("outcomes.json.archive.jsonl.corrupt-*"))
        self.assertEqual(len(quarantines), 1)
        self.assertEqual(quarantines[0].read_bytes(), b'{"key": "deadbeef')
        # The fold still works after the repair:  key released, others held.
        reloaded = OutcomeStore(self.path, clock=self.clock)
        self.assertIsNone(reloaded.begin(archived_key, reapply_ok=True))
        self.assertIsNotNone(reloaded.begin(hexkey(1)))

    def test_failed_append_rolls_back(self):
        store = OutcomeStore(self.path, clock=self.clock)
        self.fill_unknowns(store, MAX_ENTRIES)
        self.assertIsNone(store.begin("f" * 64))
        # Refill to the wall so the next begin() archives again.
        for i in range(ARCHIVE_BATCH - 1):
            key = f"{1000 + i:064x}"
            self.assertIsNone(store.begin(key))
            store.finish(key, "unknown")
        before_archive = open(self.archive, "rb").read()
        before_index = open(self.index, "rb").read()
        before_rows = {k: dict(r) for k, r in store._rows.items()}
        real_write = os.write
        calls = {"n": 0}

        def flaky(fd, data):
            # Fail only the second write carrying archive-row payloads, so
            # unrelated writes (registry saves) pass through.
            if b'"archived_at"' in bytes(data):
                calls["n"] += 1
                if calls["n"] == 2:
                    raise OSError("ENOSPC")
            return real_write(fd, data)

        # The next begin() hits the wall and archives;  die mid-batch.
        with mock.patch.object(outcomes.os, "write", flaky), self.assertRaises(OSError):
            store.begin("e" * 64)
        self.assertEqual(
            open(self.archive, "rb").read(),
            before_archive,
            "interrupted batch must roll back to the pre-append offset",
        )
        self.assertEqual(open(self.index, "rb").read(), before_index)
        self.assertEqual(store._rows, before_rows, "live registry untouched")
        # Recovery:  with the fault gone the same begin archives cleanly.
        self.assertIsNone(store.begin("e" * 64))
        self.assertEqual(len(self.read_archive()), 2 * ARCHIVE_BATCH)


class ArchiveHardeningTests(ArchiveTestCase):
    """Failure-path pins for the archive consumption and quarantine fixes."""

    def write_raw_archive(self, lines):
        with open(self.archive, "wb") as fh:
            fh.write(b"".join(line + b"\n" for line in lines))

    def row_line(self, key, state="unknown", corr=None):
        row = {"state": state, "ts": 1.0}
        if corr is not None:
            row["corr"] = corr
        return json.dumps({"key": key, "archived_at": 2.0, "row": row}).encode()

    def marker_line(self, key, released_at=3.0, state="refused"):
        return json.dumps(
            {"key": key, "released_at": released_at, "state": state}
        ).encode()

    def test_valid_marker_releases_and_bad_released_at_fails_closed(self):
        key = hexkey(0)
        self.write_raw_archive([self.row_line(key), self.marker_line(key)])
        store = OutcomeStore(self.path, clock=self.clock)
        self.assertIsNone(store.begin(key), "valid release marker releases the key")
        # A non-numeric released_at must not release:  the load fails closed.
        self.write_raw_archive(
            [self.row_line(key), self.marker_line(key, released_at="junk")]
        )
        with self.assertRaises(OutcomeStoreError):
            OutcomeStore(self.path, clock=self.clock)

    def test_marker_with_bad_state_fails_closed(self):
        key = hexkey(0)
        self.write_raw_archive(
            [self.row_line(key), self.marker_line(key, state="junk")]
        )
        with self.assertRaises(OutcomeStoreError):
            OutcomeStore(self.path, clock=self.clock)
        # Evidence file untouched by the refused fold.
        self.assertEqual(len(self.read_archive()), 2)

    def test_archived_row_with_bad_corr_fails_closed(self):
        for bad in (123, ""):
            self.write_raw_archive([self.row_line(hexkey(0), corr=bad)])
            with self.assertRaises(OutcomeStoreError, msg=f"corr={bad!r}"):
                OutcomeStore(self.path, clock=self.clock)

    def test_same_second_quarantines_never_overwrite(self):
        class FixedClock:
            def __call__(self):
                return 1_700_000_000.0

        self.write_raw_archive([self.row_line(hexkey(0))])
        # Tear the tail:  first load quarantines fragment one.
        with open(self.archive, "ab") as fh:
            fh.write(b"{torn-one")
        OutcomeStore(self.path, clock=FixedClock())
        # Tear it again in the same second:  the second quarantine must not
        # truncate the first.
        with open(self.archive, "ab") as fh:
            fh.write(b"{torn-two")
        OutcomeStore(self.path, clock=FixedClock())
        quarantined = sorted(
            p for p in self.dir.iterdir() if ".corrupt-" in p.name
        )
        self.assertEqual(len(quarantined), 2)
        bodies = {p.read_bytes() for p in quarantined}
        self.assertEqual(bodies, {b"{torn-one", b"{torn-two"})

    def test_quarantine_is_fsynced_before_the_log_is_truncated(self):
        self.write_raw_archive([self.row_line(hexkey(0))])
        with open(self.archive, "ab") as fh:
            fh.write(b"{torn")
        fsyncs = []
        real_fsync = os.fsync
        with mock.patch.object(
            outcomes.os, "fsync", lambda fd: (fsyncs.append(fd), real_fsync(fd))[1]
        ):
            OutcomeStore(self.path, clock=self.clock)
        # quarantine fd + archive fd + quarantine dir fd, at minimum.
        self.assertGreaterEqual(len(fsyncs), 3)


if __name__ == "__main__":
    unittest.main()
