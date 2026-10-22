#!/usr/bin/env python3
"""copy_mirror contract: File Provider-safe recursive copy + delete extras.

Also the contracts the daily backup job depends on:

  - A subtree that cannot be mirrored is reported, not raised.  The mirror is
    the optional half of the job; the repository zips are the half the owner
    depends on, and they were already safely in Drive before the mirror ran.
  - Retention drops the dated backup folders beyond the newest N, and a dry
    run changes nothing.
  - A Drive that cannot be listed is a skipped retention, not a failed job:
    the exit status stays 0, and the operator is told the retention was
    skipped instead of seeing a healthy looking "removed 0" summary.

The dry run covers the whole job, not just the retention:  a dry run writes
no dated folder, no zips and no notes either.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
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


@contextlib.contextmanager
def refusing_listing():
    """Make the Drive root refuse to be listed, the way the mount does."""
    real_iterdir = Path.iterdir

    def refuse(iterated_path):
        raise PermissionError(1, "Operation not permitted", str(iterated_path))

    Path.iterdir = refuse
    try:
        yield
    finally:
        Path.iterdir = real_iterdir


@contextlib.contextmanager
def refusing_entry_stat(entry_name: str):
    """Make the stat of one Drive entry refuse, the way the mount does."""
    real_is_dir = Path.is_dir

    def refuse(path_self):
        if path_self.name == entry_name:
            raise PermissionError(1, "Operation not permitted", str(path_self))
        return real_is_dir(path_self)

    Path.is_dir = refuse
    try:
        yield
    finally:
        Path.is_dir = real_is_dir


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

    def test_a_drive_that_cannot_be_listed_does_not_take_the_job_down(self) -> None:
        # The Drive is a File Provider mount, and it refuses some requests:
        # listing the root of a large Drive is one of them, and it has refused
        # from the launchd job with "Operation not permitted" while the same
        # call succeeds from a shell.  Retention is housekeeping and it runs
        # *after* the repository zips are already in Drive, so a refusal there
        # has to be reported and carried on rather than raised.  Raised, it
        # takes the whole job down, the mirror never runs, and the daily
        # backup is reported as broken when the part the owner depends on had
        # already succeeded.
        self.make_backup("2026-10-01")

        err = io.StringIO()
        with refusing_listing():
            with contextlib.redirect_stderr(err):
                found = self.mod.dated_backup_folders(self.gdrive)
                removed = self.mod.prune_old_backups(self.gdrive, keep=0)

        self.assertIsNone(
            found,
            "an unlistable Drive is a refusal, not a Drive with nothing in it",
        )
        self.assertIsNone(
            removed,
            "a refused retention is skipped, not a prune that removed nothing",
        )
        self.assertTrue(
            (self.gdrive / "Website & App Source Backups - 2026-10-01").is_dir(),
            "the backup folder is still there",
        )
        self.assertIn(
            "could not list",
            err.getvalue(),
            "the refusal is reported, not swallowed silently",
        )
        self.assertIn(
            "skipped",
            err.getvalue(),
            "the operator is told the retention was skipped",
        )

    def test_a_drive_with_no_dated_folders_is_an_ordinary_no_op_prune(self) -> None:
        # The other half of the refusal contract.  A Drive that lists fine but
        # holds no dated backup folders is a successful no-op, and the
        # operator has to be able to tell it from a refusal.  The old code
        # returned the same empty list for both, which is how a refusal read
        # as "there was nothing to prune" and the backup set grew unnoticed.
        (self.gdrive / "Personal-Site").mkdir()

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            listed = self.mod.prune_old_backups(self.gdrive, keep=14)

        self.assertIsNotNone(listed, "a Drive that lists fine is not a refusal")
        self.assertEqual(listed, [], "a Drive with no dated folders removes nothing")
        self.assertNotIn(
            "skipped",
            err.getvalue(),
            "an ordinary no-op prune is not reported as a skipped retention",
        )

        refused_err = io.StringIO()
        with refusing_listing():
            with contextlib.redirect_stderr(refused_err):
                refused = self.mod.prune_old_backups(self.gdrive, keep=14)

        self.assertIsNone(refused, "a Drive that cannot be listed is a refusal")
        self.assertIn(
            "could not list",
            refused_err.getvalue(),
            "the refusal is reported, not swallowed silently",
        )
        self.assertNotEqual(
            listed,
            refused,
            "the two outcomes have to be different outcomes",
        )

    def test_a_per_entry_permission_error_is_skipped_not_raised(self) -> None:
        # The refusal can come for one entry instead of the whole listing, so
        # the stat that decides whether an entry is a folder has to be guarded
        # too.  One unreadable folder in the Drive must not cost the owner the
        # whole backup.
        oldest = self.make_backup("2026-10-01")
        middle = self.make_backup("2026-10-02")
        self.make_backup("2026-10-03")

        err = io.StringIO()
        with refusing_entry_stat(oldest.name):
            with contextlib.redirect_stderr(err):
                removed = self.mod.prune_old_backups(self.gdrive, keep=1)

        self.assertEqual(
            [p.name for p in removed],
            [middle.name],
            "the rest of the prune still happened",
        )
        self.assertTrue(
            oldest.is_dir(),
            "the entry that could not be stat-ed is left alone",
        )
        self.assertIn(
            oldest.name,
            err.getvalue(),
            "the warning names the path it could not read",
        )
        self.assertIn("could not stat", err.getvalue())


class DryRunTests(unittest.TestCase):
    """--dry-run changes nothing:  no folder, no zips, no notes, no removal."""

    def setUp(self) -> None:
        self.mod = load_mod(BACKUP_SCRIPT, "backup_fleet_to_gdrive")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.gdrive = self.root / "gdrive"
        self.gdrive.mkdir()
        self.code = self.root / "Code"
        self.repo = self.code / "Some-App"
        self.repo.mkdir(parents=True)
        (self.repo / "README.md").write_text("hello")
        self.mod.CODE_ROOT = self.code
        self.mod.find_gdrive = lambda: self.gdrive
        self.mod.planned_repos = lambda: ["Some-App"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_main(self, **overrides) -> int:
        args = dict(
            list=False,
            only=None,
            keep=14,
            dry_run=False,
            prune_only=False,
        )
        args.update(overrides)
        self.mod.parse_args = lambda: argparse.Namespace(**args)
        return self.mod.main()

    def test_a_dry_run_writes_nothing(self) -> None:
        self.assertEqual(self.run_main(dry_run=True), 0)
        self.assertEqual(list(self.gdrive.iterdir()), [])

    def test_a_dry_run_removes_nothing(self) -> None:
        old = self.gdrive / "Website & App Source Backups - 2026-01-01"
        old.mkdir()
        self.assertEqual(self.run_main(dry_run=True, keep=0), 0)
        self.assertTrue(old.is_dir())

    def test_a_real_run_writes_the_zip_and_the_notes(self) -> None:
        self.assertEqual(self.run_main(), 0)
        dest = self.gdrive / f"Website & App Source Backups - {self.mod.DATE_STR}"
        self.assertTrue((dest / f"Some-App-backup-{self.mod.DATE_STR}.zip").is_file())
        self.assertTrue((dest / "BACKUP-NOTES.md").is_file())


class RefusedDriveRunTests(unittest.TestCase):
    """A refused Drive costs the operator the retention, not the backup."""

    def setUp(self) -> None:
        self.mod = load_mod(BACKUP_SCRIPT, "backup_fleet_to_gdrive")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.gdrive = self.root / "gdrive"
        self.gdrive.mkdir()
        self.code = self.root / "Code"
        self.repo = self.code / "Some-App"
        self.repo.mkdir(parents=True)
        (self.repo / "README.md").write_text("hello")
        self.mod.CODE_ROOT = self.code
        self.mod.find_gdrive = lambda: self.gdrive
        self.mod.planned_repos = lambda: ["Some-App"]

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_main(self, **overrides) -> int:
        args = dict(
            list=False,
            only=None,
            keep=14,
            dry_run=False,
            prune_only=False,
        )
        args.update(overrides)
        self.mod.parse_args = lambda: argparse.Namespace(**args)
        return self.mod.main()

    def test_an_unlistable_drive_still_backs_up_and_exits_zero(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with refusing_listing():
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = self.run_main()

        self.assertEqual(code, 0, "housekeeping never takes the daily job down")
        dest = self.gdrive / f"Website & App Source Backups - {self.mod.DATE_STR}"
        self.assertTrue(
            (dest / f"Some-App-backup-{self.mod.DATE_STR}.zip").is_file(),
            "the repository zip is still in Drive",
        )
        self.assertIn(
            "Retention: skipped",
            out.getvalue(),
            "the operator sees that the retention was skipped",
        )
        self.assertIn("could not list", err.getvalue())

    def test_a_refused_drive_entry_does_not_stop_the_backup(self) -> None:
        old = self.gdrive / "Website & App Source Backups - 2026-10-01"
        old.mkdir()

        out, err = io.StringIO(), io.StringIO()
        with refusing_entry_stat(old.name):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = self.run_main()

        self.assertEqual(code, 0)
        dest = self.gdrive / f"Website & App Source Backups - {self.mod.DATE_STR}"
        self.assertTrue(
            (dest / f"Some-App-backup-{self.mod.DATE_STR}.zip").is_file(),
            "the repository zip is still in Drive",
        )
        self.assertIn("Retention:", out.getvalue(), "the run reported its retention")
        self.assertIn("could not stat", err.getvalue())
        self.assertTrue(old.is_dir(), "the unreadable folder is left alone")

    def test_prune_only_reports_the_skipped_retention_and_exits_zero(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with refusing_listing():
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = self.run_main(prune_only=True)

        self.assertEqual(code, 0, "housekeeping never takes the daily job down")
        self.assertIn("Retention: skipped", out.getvalue())
        self.assertIn("skipped", err.getvalue())

    def test_prune_only_dry_run_reports_the_skipped_retention(self) -> None:
        # The dry run is how the operator inspects what the retention would
        # do, so a refusal has to say so there too instead of reporting an
        # empty plan.
        out, err = io.StringIO(), io.StringIO()
        with refusing_listing():
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = self.run_main(prune_only=True, dry_run=True)

        self.assertEqual(code, 0)
        self.assertIn("Retention: skipped", out.getvalue())
        self.assertIn("skipped", err.getvalue())


if __name__ == "__main__":
    unittest.main()
