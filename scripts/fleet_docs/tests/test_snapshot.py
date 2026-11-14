"""Real collector output through the production snapshot and promotion paths."""

import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fleet_docs import snapshot


class SnapshotTests(unittest.TestCase):
    def test_unflushed_transaction_is_never_published_at_the_authoritative_path(self):
        with (
            patch.object(
                snapshot.os, "fsync", side_effect=OSError("fixture interrupted metadata flush")
            ),
            self.assertRaises(snapshot.Refused),
        ):
            snapshot.save_transaction(
                self.destination,
                self.parent / ".generated-docs-stage-fixture",
                self.parent / ".generated-docs-backup-fixture",
                None,
                "0" * 64,
            )
        self.assertFalse(snapshot.transaction_path(self.destination).exists())
        self.assertFalse(list(self.parent.glob(".generated-docs-stage-write-*")))

    def test_failed_metadata_publish_leaves_previous_complete_receipt_and_no_temporary(self):
        parent = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, parent)
        parent.chmod(0o700)
        target = parent / "receipt.json"
        target.write_bytes(b'{"old":true}\n')
        target.chmod(0o600)
        with (
            patch.object(
                snapshot.os, "replace", side_effect=OSError("fixture interrupted publish")
            ),
            self.assertRaises((OSError, snapshot.Refused)),
        ):
            snapshot.atomic_write(parent, "receipt.json", b'{"new":true}\n')
        self.assertEqual(target.read_bytes(), b'{"old":true}\n')
        self.assertEqual([path.name for path in parent.iterdir()], ["receipt.json"])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "Code/Example"
        self.repo.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        self.git("remote", "add", "origin", "https://github.com/Simple-With-Us/Example.git")
        (self.repo / "README.md").write_text("# Example\n\nPublished documentation.\n")
        self.git("add", "README.md")
        self.git(
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "fixture",
        )
        self.git("update-ref", "refs/remotes/origin/main", self.git("rev-parse", "HEAD"))
        self.source = self.root / "source"
        snapshot.collector().collect(None, self.source, repo_paths=[self.repo])
        self.parent = self.root / "MacDocs"
        self.parent.mkdir()
        self.destination = self.parent / "Generated"
        self.legacy = self.parent / "legacy.txt"
        self.legacy.write_text("preserve legacy document")

    def git(self, *args):
        return subprocess.check_output(
            ["git", "-C", str(self.repo), *args], stderr=subprocess.DEVNULL, text=True, timeout=15
        ).strip()

    def payload(self, before=None):
        stream = io.BytesIO()
        snapshot.write_snapshot(
            self.source, snapshot.validate_generated(self.source), stream, before
        )
        stream.seek(0)
        return stream

    def apply(self):
        before = (
            snapshot.validate_generated(self.destination)["sha256"]
            if self.destination.exists()
            else None
        )
        with snapshot.receiver_lock(self.destination):
            return snapshot.receive(self.destination, self.payload(before))

    def test_real_collection_replicates_identically_and_preserves_legacy(self):
        result = self.apply()
        self.assertEqual(result["state"], "synced")
        self.assertEqual(snapshot.inventory(self.source), snapshot.inventory(self.destination))
        self.assertEqual(self.legacy.read_text(), "preserve legacy document")
        self.assertFalse(snapshot.transaction_path(self.destination).exists())

    def test_edited_or_unknown_generated_content_is_never_overwritten(self):
        self.apply()
        path = self.destination / "Example/documents/README.md"
        path.write_text("unique edited content")
        with snapshot.receiver_lock(self.destination), self.assertRaises(snapshot.Refused):
            snapshot.receive(self.destination, self.payload())
        self.assertEqual(path.read_text(), "unique edited content")

    def test_receiver_only_immutable_history_blocks_older_authoritative_snapshot(self):
        self.apply()
        (self.repo / "README.md").write_text("# Example\n\nNew published document.\n")
        self.git("add", "README.md")
        self.git(
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-qm",
            "new document",
        )
        self.git("update-ref", "refs/remotes/origin/main", self.git("rev-parse", "HEAD"))
        snapshot.collector().collect(None, self.destination, repo_paths=[self.repo])
        before = snapshot.validate_generated(self.destination)
        with (
            snapshot.receiver_lock(self.destination),
            self.assertRaisesRegex(snapshot.Refused, "immutable-history"),
        ):
            snapshot.receive(self.destination, self.payload(before["sha256"]))
        self.assertEqual(snapshot.inventory(self.destination), before)
        self.assertFalse(list(self.parent.glob(".generated-docs-stage-*")))
        self.assertFalse(snapshot.transaction_path(self.destination).exists())

    def test_symlink_and_traversal_are_refused(self):
        (self.source / "escape").symlink_to(self.legacy)
        with self.assertRaises(snapshot.Refused):
            snapshot.inventory(self.source)
        for value in ("../escape", "/absolute", "dir/../escape", "dir//file", "dir\\file"):
            with self.subTest(value=value), self.assertRaises(snapshot.Refused):
                snapshot.relative(value)

    def test_promotion_failure_restores_old_tree_and_recovery_cleans_transaction(self):
        self.apply()
        before = snapshot.inventory(self.destination)
        rename = os.rename

        def fail_stage(source, destination):
            if Path(source).name.startswith(".generated-docs-stage-"):
                raise PermissionError("fixture promotion failure")
            return rename(source, destination)

        with snapshot.receiver_lock(self.destination):
            with self.assertRaises(PermissionError):
                snapshot.receive(
                    self.destination, self.payload(before["sha256"]), rename=fail_stage
                )
            self.assertEqual(snapshot.inventory(self.destination), before)
            snapshot.recover(self.destination)
        self.assertFalse(snapshot.transaction_path(self.destination).exists())
        self.assertFalse(list(self.parent.glob(".generated-docs-stage-*")))

    def test_crash_after_backup_rename_recovers_exact_previous_tree(self):
        self.apply()
        before = snapshot.inventory(self.destination)

        class Crash(BaseException):
            pass

        def crash(source, destination):
            if Path(source).name.startswith(".generated-docs-stage-"):
                raise Crash()
            return os.rename(source, destination)

        with snapshot.receiver_lock(self.destination):
            with self.assertRaises(Crash):
                snapshot.receive(self.destination, self.payload(before["sha256"]), rename=crash)
            # The normal exception guard restores the old directory even for cancellation.
            self.assertEqual(snapshot.inventory(self.destination), before)
            snapshot.recover(self.destination)

    def test_second_receiver_is_refused_for_lifetime_of_first(self):
        with snapshot.receiver_lock(self.destination), self.assertRaises(snapshot.Refused):
            with snapshot.receiver_lock(self.destination):
                self.fail("second receiver acquired the lock")

    def test_archive_symlink_member_is_refused_without_touching_legacy(self):
        manifest = snapshot.validate_generated(self.source)
        payload = io.BytesIO(snapshot.canonical({"manifest": manifest, "base_sha256": None}))
        payload.seek(0, 2)
        with tarfile.open(fileobj=payload, mode="w|") as archive:
            member = tarfile.TarInfo(next(iter(manifest["files"])))
            member.type = tarfile.SYMTYPE
            member.linkname = str(self.legacy)
            archive.addfile(member)
        payload.seek(0)
        with snapshot.receiver_lock(self.destination), self.assertRaises(snapshot.Refused):
            snapshot.receive(self.destination, payload)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.legacy.read_text(), "preserve legacy document")

    def test_unknown_file_is_preserved_and_blocks_replacement(self):
        self.apply()
        unknown = self.destination / "unique.txt"
        unknown.write_text("unique")
        unknown.chmod(0o600)
        with snapshot.receiver_lock(self.destination), self.assertRaises(snapshot.Refused):
            snapshot.receive(self.destination, self.payload())
        self.assertEqual(unknown.read_text(), "unique")

    def test_recover_actual_interrupted_rename_window(self):
        self.apply()
        before = snapshot.inventory(self.destination)
        stage = self.parent / ".generated-docs-stage-fixture"
        shutil.copytree(self.source, stage)
        backup = self.parent / ".generated-docs-backup-fixture"
        with snapshot.receiver_lock(self.destination):
            snapshot.save_transaction(
                self.destination, stage, backup, before["sha256"], before["sha256"]
            )
            os.rename(self.destination, backup)
            # This is the on-disk state left when a process is killed between renames.
            self.assertFalse(self.destination.exists())
            snapshot.recover(self.destination)
        self.assertEqual(snapshot.inventory(self.destination), before)
        self.assertFalse(backup.exists())
        self.assertFalse(stage.exists())

    def test_collector_lock_spans_snapshot_transfer(self):
        from fleet_docs import pipeline

        def transfer(root, desired, configuration):
            with self.assertRaises(snapshot.collector().CollectionError):
                snapshot.collector().collect(None, root, repo_paths=[self.repo])
            return {"state": "current", "sha256": desired["sha256"]}

        result = pipeline.refresh(
            self.root / "Code",
            self.root / "state",
            {"peer": "mac", "private_docs": [], "remote_sources": []},
            transfer=transfer,
        )
        self.assertEqual(result["state"], "current")

    def test_directory_symlink_swap_before_anchor_is_refused(self):
        documents = self.source / "Example/documents"
        foreign = self.root / "foreign"
        foreign.mkdir(mode=0o700)
        (foreign / "README.md").write_text("foreign fixture must not be read")
        (foreign / "README.md").chmod(0o600)
        original = os.open
        swapped = False

        def swap(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == "documents" and "dir_fd" in kwargs and not swapped:
                swapped = True
                documents.rename(documents.with_name("original-documents"))
                documents.symlink_to(foreign, target_is_directory=True)
            return original(path, flags, *args, **kwargs)

        with (
            patch.object(snapshot.os, "open", side_effect=swap),
            self.assertRaises(snapshot.Refused),
        ):
            snapshot.read_file(documents / "README.md")
        self.assertTrue(swapped)

    def test_swap_after_directory_anchor_reads_pinned_original_only(self):
        documents = self.source / "Example/documents"
        expected = snapshot.read_file(documents / "README.md")
        foreign = self.root / "foreign"
        foreign.mkdir(mode=0o700)
        (foreign / "README.md").write_text("foreign fixture must not be read")
        (foreign / "README.md").chmod(0o600)
        original = os.open
        swapped = False

        def swap(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == "README.md" and "dir_fd" in kwargs and not swapped:
                swapped = True
                documents.rename(documents.with_name("original-documents"))
                documents.symlink_to(foreign, target_is_directory=True)
            return original(path, flags, *args, **kwargs)

        with patch.object(snapshot.os, "open", side_effect=swap):
            self.assertEqual(snapshot.read_file(documents / "README.md"), expected)
        self.assertTrue(swapped)

    def test_deep_new_paths_get_private_components(self):
        stage = self.root / "deep-stage"
        stage.mkdir(mode=0o700)
        fd = snapshot.create_file(stage, "one/two/three/value.txt")
        os.close(fd)
        for path in (stage / "one", stage / "one/two", stage / "one/two/three"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o700)

    def test_journal_and_namespace_changes_have_directory_fsync_barriers(self):
        self.apply()
        before = snapshot.inventory(self.destination)
        events = []
        original = snapshot.fsync_directory

        def sync(path):
            events.append("parent-fsync" if path == self.parent else "stage-fsync")
            original(path)

        def rename(source, destination):
            events.append("rename")
            return os.rename(source, destination)

        with (
            snapshot.receiver_lock(self.destination),
            patch.object(snapshot, "fsync_directory", side_effect=sync),
        ):
            snapshot.receive(self.destination, self.payload(before["sha256"]), rename=rename)
        renames = [index for index, value in enumerate(events) if value == "rename"]
        self.assertEqual(len(renames), 2)
        self.assertTrue(
            all(
                events[index - 1] == "parent-fsync" and events[index + 1] == "parent-fsync"
                for index in renames
            )
        )
        self.assertEqual(events[-1], "parent-fsync")


if __name__ == "__main__":
    unittest.main()
