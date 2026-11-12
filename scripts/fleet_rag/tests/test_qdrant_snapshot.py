"""Pre-prune Qdrant snapshot helpers (AFC-754)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fleet_rag import qdrant_snapshot
from fleet_rag.core import FleetRagError


class FakeQd:
    def __init__(self) -> None:
        self.collection = "test-collection"
        self.created: list[str] = []
        self.deleted: list[str] = []

    def create_snapshot(self, *, wait: bool = True) -> dict:
        del wait
        name = f"snap-{len(self.created)}"
        self.created.append(name)
        return {"name": name}

    def list_snapshots(self) -> list[dict]:
        return [{"name": n} for n in self.created]

    def delete_snapshot(self, name: str) -> None:
        self.deleted.append(name)
        self.created = [n for n in self.created if n != name]


class QdrantSnapshotTests(unittest.TestCase):
    def test_keep_parses_env(self) -> None:
        with mock.patch.dict(os.environ, {qdrant_snapshot.KEEP_ENV: "5"}):
            self.assertEqual(qdrant_snapshot.snapshot_keep(), 5)
        with mock.patch.dict(os.environ, {qdrant_snapshot.KEEP_ENV: "0"}):
            self.assertEqual(qdrant_snapshot.snapshot_keep(), 1)

    def test_snapshot_dir_follows_nightly_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / "last.json"
            with mock.patch.dict(os.environ, {qdrant_snapshot.NIGHTLY_SUMMARY_ENV: str(summary)}):
                self.assertEqual(
                    qdrant_snapshot.snapshot_dir(),
                    summary.parent / qdrant_snapshot.DEFAULT_STATE_SUBDIR,
                )

    def test_remote_retention_drops_oldest(self) -> None:
        qd = FakeQd()
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                qdrant_snapshot.KEEP_ENV: "2",
                qdrant_snapshot.DIR_ENV: str(Path(tmp) / "snap-root" / "prune"),
            }
            with mock.patch.dict(os.environ, env, clear=False):
                qdrant_snapshot.take_prune_snapshot(qd)
                qdrant_snapshot.take_prune_snapshot(qd)
                qdrant_snapshot.take_prune_snapshot(qd)
        self.assertEqual(qd.created, ["snap-1", "snap-2"])
        self.assertIn("snap-0", qd.deleted)

    def test_volume_move_archives_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snap_name = "test-snap"

            class NamedFake(FakeQd):
                def create_snapshot(self, *, wait: bool = True) -> dict:
                    del wait
                    self.created.append(snap_name)
                    return {"name": snap_name}

            qd = NamedFake()
            volume = root / "vol"
            coll_dir = volume / "snapshots" / qd.collection
            coll_dir.mkdir(parents=True)
            src = coll_dir / snap_name
            src.write_bytes(b"payload")
            archive_parent = root / "state"
            env = {
                qdrant_snapshot.VOLUME_ENV: str(volume),
                qdrant_snapshot.DIR_ENV: str(archive_parent / "qdrant-prune-snapshots"),
                qdrant_snapshot.KEEP_ENV: "3",
            }
            with mock.patch.dict(os.environ, env, clear=False):
                meta = qdrant_snapshot.take_prune_snapshot(qd, state_parent=archive_parent)
            self.assertEqual(meta["name"], snap_name)
            self.assertFalse(src.exists())
            archived = list((archive_parent / "qdrant-prune-snapshots").glob("*.snapshot"))
            self.assertEqual(len(archived), 1)
            self.assertIn(snap_name, qd.deleted)

    def test_maybe_snapshot_blocks_prune_on_failure(self) -> None:
        report: dict = {"errors": [], "warnings": [], "ok": True}
        logs: list[str] = []

        class BoomQd(FakeQd):
            def create_snapshot(self, *, wait: bool = True) -> dict:
                raise FleetRagError("down")

        ok = qdrant_snapshot.maybe_snapshot_before_prune(
            prune=True,
            dry_run=False,
            pending_prunes=[("doc", set(), {})],
            report=report,
            qd=BoomQd(),
            state_path=Path("/tmp/state.json"),
            log=logs.append,
        )
        self.assertFalse(ok)
        self.assertFalse(report["ok"])
        self.assertTrue(any("snapshot failed" in e for e in report["errors"]))

    def test_maybe_snapshot_skips_dry_run(self) -> None:
        report: dict = {"errors": [], "ok": True}
        qd = FakeQd()
        ok = qdrant_snapshot.maybe_snapshot_before_prune(
            prune=True,
            dry_run=True,
            pending_prunes=[("doc", set(), {})],
            report=report,
            qd=qd,
            state_path=Path("/tmp/state.json"),
            log=lambda *a: None,
        )
        self.assertTrue(ok)
        self.assertEqual(qd.created, [])


if __name__ == "__main__":
    unittest.main()
