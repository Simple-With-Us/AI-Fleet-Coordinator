#!/usr/bin/env python3
"""copy_mirror contract: File Provider-safe recursive copy + delete extras.

Also the two contracts the daily backup job depends on:

  - A subtree that cannot be mirrored is reported, not raised.  The mirror is
    the optional half of the job; the repository zips are the half the owner
    depends on, and they were already safely in Drive before the mirror ran.
  - Retention drops the dated backup folders beyond the newest N, and a dry
    run changes nothing.
"""

from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "sync-fleet-agent-config-to-gdrive.py"
BACKUP_SCRIPT = Path(__file__).resolve().parent / "backup-fleet-to-gdrive.py"


def load_mod(path: Path = SCRIPT, name: str = "sync_fleet_agent_config"):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


class CopyMirrorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.mod = load_mod()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.src = self.root / "src"
        self.dest = self.root / "dest"
        self.src.mkdir()
        (self.src / "keep.txt").write_text("hello")
        (self.src / "sub").mkdir()
        (self.src / "sub" / "nested.txt").write_text("nested")
        (self.src / ".DS_Store").write_text("junk")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_copies_tree_and_skips_ds_store(self) -> None:
        self.mod.copy_mirror(self.src, self.dest)
        self.assertEqual((self.dest / "keep.txt").read_text(), "hello")
        self.assertEqual((self.dest / "sub" / "nested.txt").read_text(), "nested")
        self.assertFalse((self.dest / ".DS_Store").exists())

    def test_deletes_dest_extras(self) -> None:
        self.dest.mkdir()
        (self.dest / "stale.txt").write_text("gone")
        self.mod.copy_mirror(self.src, self.dest)
        self.assertFalse((self.dest / "stale.txt").exists())
        self.assertTrue((self.dest / "keep.txt").is_file())

    def test_skips_same_size_even_if_mtime_differs(self) -> None:
        self.mod.copy_mirror(self.src, self.dest)
        dest_file = self.dest / "keep.txt"
        dest_file.write_text("hello")
        first_mtime = dest_file.stat().st_mtime
        self.mod.copy_mirror(self.src, self.dest)
        self.assertEqual(dest_file.stat().st_mtime, first_mtime)


class MirrorFailureIsNotFatalTests(unittest.TestCase):
    """A refused subtree is reported, not raised."""

    def setUp(self) -> None:
        self.mod = load_mod()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.gdrive = self.root / "gdrive"
        self.gdrive.mkdir()
        self.src = self.root / "src"
        self.src.mkdir()
        (self.src / "SKILL.md").write_text("x")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_failed_subtree_is_reported_and_the_rest_still_mirrors(self) -> None:
        self.mod.MIRROR_SPECS = [
            ("broken/skills", [".claude/skills"]),
            ("good/skills", [".grok/skills"]),
        ]
        self.mod.resolve_home_source = lambda _rels: self.src

        def mirror(src, dest):
            if "broken" in str(dest):
                raise RuntimeError("1 file(s) failed to mirror")
            self.mod.copy_mirror(src, dest)

        self.mod.rsync_mirror = mirror

        mirrored, failures = self.mod.mirror_agent_config(self.gdrive)
        self.assertEqual(mirrored, ["good/skills"])
        self.assertEqual(failures, ["broken/skills"])
        # The subtree that worked is really there.
        self.assertTrue(
            (self.gdrive / "fleet-agent-config" / "good" / "skills" / "SKILL.md").is_file()
        )

    def test_the_skills_catalog_failure_is_reported_not_raised(self) -> None:
        def boom(_src, _dest):
            raise RuntimeError("catalog refused")

        self.mod.rsync_mirror = boom
        self.mod.docs_fleet_skills_path = lambda: self.src
        self.assertFalse(self.mod.mirror_fleet_skills_catalog(self.gdrive))


class RetentionTests(unittest.TestCase):
    """The dated backup folders beyond the newest N are dropped."""

    def setUp(self) -> None:
        self.mod = load_mod(BACKUP_SCRIPT, "backup_fleet_to_gdrive")
        self.tmp = tempfile.TemporaryDirectory()
        self.gdrive = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_backup(self, day: str) -> Path:
        path = self.gdrive / f"Website & App Source Backups - {day}"
        path.mkdir(parents=True)
        (path / "repo-backup.zip").write_text("zip")
        return path

    def test_only_dated_backup_folders_count(self) -> None:
        self.make_backup("2026-10-01")
        (self.gdrive / "Personal-Site").mkdir()
        (self.gdrive / "Website & App Source Backups - not-a-date").mkdir()
        (self.gdrive / "fleet-agent-config").mkdir()

        found = self.mod.dated_backup_folders(self.gdrive)
        self.assertEqual([p.name for _d, p in found], ["Website & App Source Backups - 2026-10-01"])

    def test_the_newest_folders_are_kept_and_the_oldest_go(self) -> None:
        for day in ("2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05"):
            self.make_backup(day)

        removed = self.mod.prune_old_backups(self.gdrive, keep=2)
        self.assertEqual(
            [p.name for p in removed],
            [
                "Website & App Source Backups - 2026-10-01",
                "Website & App Source Backups - 2026-10-02",
                "Website & App Source Backups - 2026-10-03",
            ],
        )
        self.assertTrue((self.gdrive / "Website & App Source Backups - 2026-10-05").is_dir())
        self.assertFalse((self.gdrive / "Website & App Source Backups - 2026-10-01").exists())

    def test_a_dry_run_changes_nothing(self) -> None:
        for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
            self.make_backup(day)

        removed = self.mod.prune_old_backups(self.gdrive, keep=1, dry_run=True)
        self.assertEqual(len(removed), 2)
        for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
            self.assertTrue((self.gdrive / f"Website & App Source Backups - {day}").is_dir())

    def test_nothing_outside_the_backup_folders_is_touched(self) -> None:
        self.make_backup("2026-10-01")
        other = self.gdrive / "Personal-Site"
        other.mkdir()
        (other / "keep.txt").write_text("keep")

        self.mod.prune_old_backups(self.gdrive, keep=0)
        self.assertTrue((other / "keep.txt").is_file())

    def test_keeping_more_than_there_is_removes_nothing(self) -> None:
        self.make_backup("2026-10-01")
        self.assertEqual(self.mod.prune_old_backups(self.gdrive, keep=30), [])


if __name__ == "__main__":
    unittest.main()
