from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "collect-repo-documents.py"
SPEC = importlib.util.spec_from_file_location("collect_repo_documents", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
collector = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = collector
SPEC.loader.exec_module(collector)


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


class RepositoryFixture:
    def __init__(
        self, code_root: Path, folder: str, repo_name: str, owner: str = "simple-with-us"
    ) -> None:
        self.path = code_root / folder
        self.path.mkdir(parents=True)
        git(self.path, "init", "--quiet", "--initial-branch=main")
        git(self.path, "config", "user.name", "Fixture")
        git(self.path, "config", "user.email", "fixture@example.invalid")
        git(self.path, "remote", "add", "origin", f"https://github.com/{owner}/{repo_name}.git")

    def write(self, relative: str, content: str | bytes) -> None:
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))

    def commit_published(self) -> str:
        git(self.path, "add", "--all")
        git(self.path, "commit", "--quiet", "-m", "fixture published")
        commit = git(self.path, "rev-parse", "HEAD")
        git(self.path, "update-ref", "refs/remotes/origin/main", commit)
        git(self.path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        return commit


class CollectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=ROOT, prefix=".collect-docs-test-")
        self.base = Path(self.temp.name)
        self.code = self.base / "Code"
        self.code.mkdir()
        self.output = self.code / "Docs"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def make_repo(
        self, folder: str = "ExampleRepo", name: str = "ExampleRepo", owner: str = "simple-with-us"
    ) -> RepositoryFixture:
        return RepositoryFixture(self.code, folder, name, owner)

    def test_collection_uses_published_default_ref_and_only_tracked_human_docs(self) -> None:
        repo = self.make_repo()
        repo.write("README.md", "# Published README\n")
        repo.write(
            "docs/guide.md",
            "Published docs.\n\n[runbook](runbooks/on-call.md) [chart](assets/chart.png) [escape](../../outside.md) [private](private/evidence.jsonl) [malformed](http://[broken)\n",
        )
        repo.write("docs/runbooks/on-call.md", "Operational runbook.\n")
        repo.write("docs/assets/chart.png", b"png fixture")
        repo.write("docs/rollouts/rollout.txt", "Rollout report.\n")
        repo.write("docs/reports/evidence.jsonl", '{"result":"verified"}\n')
        repo.write(
            "docs/reports/receipt.md", "[evidence](evidence.jsonl) [missing](missing.jsonl)\n"
        )
        repo.write("docs/.secrets/internal.md", "must not copy")
        repo.write(".env.production", "must not copy")
        repo.write("src/README.md", "not a root README or docs path")
        repo.write("docs/private/owner.md", "must remain private")
        repo.write("docs/private-audits/internal.md", "must remain private")
        repo.write("REVIEW.md", "Root-level review note")
        repo.write("docs/linked.md", "symlink target")
        (repo.path / "docs" / "link.md").symlink_to("linked.md")
        published = repo.commit_published()

        # Local feature commit and dirty/untracked content must not enter the collection.
        repo.write("docs/guide.md", "feature branch content")
        git(repo.path, "add", "docs/guide.md")
        git(repo.path, "commit", "--quiet", "-m", "unpublished feature")
        repo.write("docs/untracked.md", "untracked content")

        ops = collector.collect(self.code, self.output, check_only=False)
        mirror = self.output / "ExampleRepo"
        self.assertTrue(any("reports/" in op for op in ops))
        self.assertEqual(
            (mirror / "documents/docs/guide.md").read_text(),
            "Published docs.\n\n[runbook](runbooks/on-call.md) [chart](assets/chart.png) [escape](../../outside.md) [private](private/evidence.jsonl) [malformed](http://[broken)\n",
        )
        self.assertEqual(
            (mirror / "documents/docs/runbooks/on-call.md").read_text(), "Operational runbook.\n"
        )
        self.assertEqual((mirror / "documents/docs/assets/chart.png").read_bytes(), b"png fixture")
        self.assertEqual(
            (mirror / "documents/docs/reports/evidence.jsonl").read_text(),
            '{"result":"verified"}\n',
        )
        self.assertFalse((mirror / "documents/docs/link.md").exists())
        self.assertFalse((mirror / "documents/docs/.secrets/internal.md").exists())
        self.assertFalse((mirror / "documents/.env.production").exists())
        self.assertFalse((mirror / "documents/src/README.md").exists())
        self.assertFalse((mirror / "documents/docs/private/owner.md").exists())
        self.assertFalse((mirror / "documents/docs/private-audits/internal.md").exists())
        self.assertEqual((mirror / "documents/REVIEW.md").read_text(), "Root-level review note")
        self.assertFalse((mirror / "documents/docs/untracked.md").exists())

        manifest = json.loads((mirror / "manifest.json").read_text())
        self.assertEqual(manifest["source_ref"], "origin/main")
        self.assertEqual(manifest["source_commit"], published)
        self.assertEqual(manifest["files"]["documents/docs/guide.md"]["source_commit"], published)
        self.assertTrue((mirror / "reports" / published / "README.md").is_file())
        self.assertIn(
            {"source_path": "docs/reports/receipt.md", "target_path": "docs/reports/missing.jsonl"},
            manifest["missing_local_links"],
        )
        self.assertFalse(
            any("private" in item["target_path"] for item in manifest["missing_local_links"])
        )
        self.assertIn("Missing linked files", (mirror / "README.md").read_text())
        index = (self.output / "README.md").read_text()
        self.assertIn("ExampleRepo/README.md", index)
        self.assertNotIn("private", index)

    def test_duplicate_checkout_prefers_exact_repo_folder_and_ambiguous_names_are_skipped(
        self,
    ) -> None:
        alias = self.make_repo(folder="work-copy", name="ExactRepo")
        alias.write("README.md", "alias")
        alias.commit_published()
        exact = self.make_repo(folder="ExactRepo", name="ExactRepo")
        exact.write("README.md", "exact")
        exact.commit_published()
        collision = self.make_repo(
            folder="DifferentOwnerCopy", name="Collision", owner="other-owner"
        )
        collision.write("README.md", "one")
        collision.commit_published()
        other_collision = self.make_repo(
            folder="AnotherCollision", name="Collision", owner="another-owner"
        )
        other_collision.write("README.md", "two")
        other_collision.commit_published()

        selected, skipped = collector.discover_repositories(self.code)
        self.assertEqual([repo.name for repo in selected], ["ExactRepo"])
        self.assertEqual(selected[0].path.name, "ExactRepo")
        self.assertIn("work-copy", skipped)
        self.assertIn("DifferentOwnerCopy", skipped)
        self.assertIn("AnotherCollision", skipped)

    def test_check_is_read_only_and_second_collection_is_idempotent(self) -> None:
        repo = self.make_repo()
        repo.write("README.md", "# Example\n")
        repo.write("docs/reports/report.md", "Report\n")
        repo.commit_published()
        preview = self.base / "preview"

        planned = collector.collect(self.code, self.output, check_only=True)
        preview_plan = collector.preview_report_indexes(
            [collector._repo_info(repo.path)], preview, check_only=True
        )
        self.assertTrue(planned)
        self.assertTrue(preview_plan)
        self.assertFalse(self.output.exists())
        self.assertFalse(preview.exists())

        self.output.mkdir(mode=0o700)
        self.output.chmod(0o700)
        collector.collect(self.code, self.output, check_only=True)
        self.assertFalse((self.output / collector.COLLECTION_LOCK_FILE).exists())
        self.output.rmdir()

        collector.collect(self.code, self.output)
        lock_file = self.output / collector.COLLECTION_LOCK_FILE
        self.assertEqual(stat.S_IMODE(lock_file.stat().st_mode), 0o600)
        lock_inode = lock_file.stat().st_ino
        (self.output / "private").mkdir()
        (self.output / "private").chmod(0o700)
        private = self.output / "private" / "notes.txt"
        private.write_text("owner content")
        private.chmod(0o600)
        self.assertEqual(collector.collect(self.code, self.output), [])
        self.assertEqual(lock_file.stat().st_ino, lock_inode)
        self.assertEqual(private.read_text(), "owner content")
        self.assertNotIn("owner content", (self.output / "README.md").read_text())

        collector.preview_report_indexes([collector._repo_info(repo.path)], preview)
        index = preview / "ExampleRepo/docs/reports/README.md"
        self.assertTrue(index.is_file())
        self.assertIn("[report](report.md)", index.read_text())
        self.assertEqual(
            collector.preview_report_indexes([collector._repo_info(repo.path)], preview), []
        )

    def test_second_process_cannot_recover_a_live_writer_transaction(self) -> None:
        repo = self.make_repo()
        repo.write("docs/guide.md", "guide")
        repo.commit_published()

        with collector._collection_lock(self.output, exclusive=True):
            transaction = collector._CollectionTransaction(self.output)
            active_output = self.output / "ExampleRepo/documents/docs/active.md"
            transaction.write(active_output, b"first writer output", None)
            journal = self.output / collector.COLLECTION_TRANSACTION_DIR / "journal.jsonl"
            active_journal = journal.read_bytes()

            second = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--code-root",
                    str(self.code),
                    "--output-root",
                    str(self.output),
                ],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            self.assertNotEqual(second.returncode, 0)
            self.assertIn("Another collection writer holds the library lock", second.stderr)
            self.assertTrue(active_output.is_file())
            self.assertEqual(active_output.read_bytes(), b"first writer output")
            self.assertEqual(journal.read_bytes(), active_journal)

            transaction.rollback()
            self.assertFalse(active_output.exists())

    def test_empty_versioned_report_uses_explicit_document_presence_check(self) -> None:
        repo = self.make_repo()
        repo.write("src/main.py", "print('not a human document')")
        commit = repo.commit_published()

        collector.collect(self.code, self.output)

        report = self.output / "ExampleRepo/reports" / commit / "README.md"
        self.assertIn("- No eligible tracked documents were found.", report.read_text())

    def test_later_repository_preflight_failure_writes_nothing_and_can_rerun(self) -> None:
        first = self.make_repo(folder="A-Repo", name="A-Repo")
        first.write("docs/first.md", "first")
        first.commit_published()
        second = self.make_repo(folder="B-Repo", name="B-Repo")
        second.write("docs/second.md", "second")
        second.commit_published()

        collision = self.output / "B-Repo/documents/docs/second.md"
        collision.parent.mkdir(parents=True)
        self.output.chmod(0o700)
        (self.output / "B-Repo").chmod(0o700)
        (self.output / "B-Repo/documents").chmod(0o700)
        (self.output / "B-Repo/documents/docs").chmod(0o700)
        collision.write_text("owner content")
        collision.chmod(0o600)

        with self.assertRaisesRegex(collector.CollectionError, "not collector-owned"):
            collector.collect(self.code, self.output)
        self.assertFalse((self.output / "A-Repo/documents/docs/first.md").exists())
        self.assertFalse((self.output / collector.COLLECTION_TRANSACTION_DIR).exists())
        self.assertEqual(collision.read_text(), "owner content")

        collision.unlink()
        collector.collect(self.code, self.output)
        self.assertEqual((self.output / "A-Repo/documents/docs/first.md").read_text(), "first")
        self.assertEqual((self.output / "B-Repo/documents/docs/second.md").read_text(), "second")

    def test_interrupted_multi_file_collection_recovers_without_manual_cleanup(self) -> None:
        repo = self.make_repo()
        repo.write("docs/guide.md", "guide v1")
        repo.write("docs/reports/report.md", "report v1")
        repo.commit_published()
        collector.collect(self.code, self.output)
        private_dir = self.output / "private"
        private_dir.mkdir(mode=0o700)
        private_dir.chmod(0o700)
        private_file = private_dir / "owner-note.md"
        private_file.write_text("preserve")
        private_file.chmod(0o600)

        repo.write("docs/guide.md", "guide v2")
        repo.write("docs/new.md", "new document")
        repo.write("docs/reports/report.md", "report v2")
        second_commit = repo.commit_published()

        original_write = collector._atomic_write
        interrupted = False

        def interrupt_after_document(path: Path, data: bytes, boundary: Path) -> None:
            nonlocal interrupted
            original_write(path, data, boundary)
            if not interrupted and path == self.output / "ExampleRepo/documents/docs/new.md":
                interrupted = True
                raise KeyboardInterrupt("injected process interruption")

        with patch.object(collector, "_atomic_write", side_effect=interrupt_after_document):
            with self.assertRaises(KeyboardInterrupt):
                collector.collect(self.code, self.output)

        transaction_dir = self.output / collector.COLLECTION_TRANSACTION_DIR
        journal = transaction_dir / "journal.jsonl"
        self.assertTrue(journal.is_file())
        self.assertEqual(stat.S_IMODE(transaction_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o600)
        records = [json.loads(line) for line in journal.read_text().splitlines()]
        before_images = {
            record["target"]: record for record in records if record["kind"] == "write"
        }
        self.assertEqual(
            before_images["ExampleRepo/documents/docs/guide.md"]["before_sha256"],
            hashlib.sha256(b"guide v1").hexdigest(),
        )
        self.assertFalse(before_images["ExampleRepo/documents/docs/new.md"]["before_exists"])
        journal_before_check = journal.read_bytes()
        with self.assertRaisesRegex(collector.CollectionError, "--check will not recover"):
            collector.collect(self.code, self.output, check_only=True)
        self.assertEqual(journal.read_bytes(), journal_before_check)

        restored_guide = False
        original_write = collector._atomic_write

        def observe_rollback(path: Path, data: bytes, boundary: Path) -> None:
            nonlocal restored_guide
            if path == self.output / "ExampleRepo/documents/docs/guide.md" and data == b"guide v1":
                restored_guide = True
            original_write(path, data, boundary)

        with patch.object(collector, "_atomic_write", side_effect=observe_rollback):
            collector.collect(self.code, self.output)
        self.assertTrue(restored_guide)
        self.assertFalse(transaction_dir.exists())
        self.assertEqual(private_file.read_text(), "preserve")
        self.assertEqual(
            (self.output / "ExampleRepo/documents/docs/guide.md").read_text(), "guide v2"
        )
        self.assertEqual(
            (self.output / "ExampleRepo/documents/docs/new.md").read_text(), "new document"
        )
        self.assertEqual(
            json.loads((self.output / "ExampleRepo/manifest.json").read_text())["source_commit"],
            second_commit,
        )
        self.assertTrue((self.output / "ExampleRepo/manifest.json").is_file())
        self.assertTrue((self.output / "README.md").is_file())

    def test_recovery_refuses_to_overwrite_a_post_interruption_edit(self) -> None:
        repo = self.make_repo()
        repo.write("docs/guide.md", "guide")
        repo.write("docs/reports/report.md", "report")
        repo.commit_published()
        target = self.output / "ExampleRepo/documents/docs/guide.md"
        original_write = collector._atomic_write
        interrupted = False

        def interrupt_after_document(path: Path, data: bytes, boundary: Path) -> None:
            nonlocal interrupted
            original_write(path, data, boundary)
            if not interrupted and path == target:
                interrupted = True
                raise KeyboardInterrupt("injected process interruption")

        with patch.object(collector, "_atomic_write", side_effect=interrupt_after_document):
            with self.assertRaises(KeyboardInterrupt):
                collector.collect(self.code, self.output)

        target.write_text("human edit after interruption")
        target.chmod(0o600)
        with self.assertRaisesRegex(collector.CollectionError, "changed after interruption"):
            collector.collect(self.code, self.output)
        self.assertEqual(target.read_text(), "human edit after interruption")
        self.assertTrue((self.output / collector.COLLECTION_TRANSACTION_DIR).is_dir())

    def test_atomic_write_failure_rolls_back_before_a_clean_rerun(self) -> None:
        repo = self.make_repo()
        repo.write("docs/guide.md", "guide")
        repo.write("docs/reports/report.md", "report")
        repo.commit_published()
        target = self.output / "ExampleRepo/documents/docs/guide.md"
        original_write = collector._atomic_write
        failed = False

        def fail_after_atomic_replace(path: Path, data: bytes, boundary: Path) -> None:
            nonlocal failed
            original_write(path, data, boundary)
            if not failed and path == target:
                failed = True
                raise OSError("injected writer failure")

        with patch.object(collector, "_atomic_write", side_effect=fail_after_atomic_replace):
            with self.assertRaisesRegex(OSError, "injected writer failure"):
                collector.collect(self.code, self.output)

        self.assertFalse(target.exists())
        self.assertFalse((self.output / collector.COLLECTION_TRANSACTION_DIR).exists())
        collector.collect(self.code, self.output)
        self.assertEqual(target.read_text(), "guide")

    def test_public_index_excludes_named_credential_reports_but_central_selection_keeps_them(
        self,
    ) -> None:
        repo = self.make_repo()
        excluded = (
            "docs/audits/2026-10-01-cf-token-map.md",
            "docs/reports/2026-10-02-private-billing-receipt-import.md",
            "docs/reviews/extension-credential-containment.md",
        )
        for path in excluded:
            repo.write(path, "operational detail")
        repo.write("docs/reports/2026-10-03-telemetry-token-catalog.md", "public telemetry")
        repo.write("docs/research/token-usage-pricing.md", "pricing research")
        repo.commit_published()
        info = collector._repo_info(repo.path)
        tree = collector._parse_tree(info)

        index = collector.reports_index(info, tree).decode()
        for path in excluded:
            self.assertNotIn(Path(path).stem, index)
        self.assertIn("2026-10-03-telemetry-token-catalog", index)
        self.assertIn("token-usage-pricing", index)

        selected, _, _ = collector._select_blobs(info, tree)
        for path in excluded:
            self.assertIn(path, selected)

    def test_edited_or_deleted_generated_files_fail_closed(self) -> None:
        repo = self.make_repo()
        repo.write("docs/guide.md", "source version one")
        repo.commit_published()
        collector.collect(self.code, self.output)
        mirrored = self.output / "ExampleRepo/documents/docs/guide.md"
        mirrored.write_text("private replacement")

        with self.assertRaisesRegex(collector.CollectionError, "changed or deleted"):
            collector.collect(self.code, self.output)
        self.assertEqual(mirrored.read_text(), "private replacement")

    def test_deleted_generated_index_fails_closed(self) -> None:
        repo = self.make_repo()
        repo.write("README.md", "# Example\n")
        repo.commit_published()
        collector.collect(self.code, self.output)
        generated_readme = self.output / "ExampleRepo/README.md"
        generated_readme.unlink()
        with self.assertRaisesRegex(collector.CollectionError, "README was changed or deleted"):
            collector.collect(self.code, self.output)

    def test_deleted_source_documents_are_retained_and_versioned(self) -> None:
        repo = self.make_repo()
        repo.write("docs/old.md", "old source")
        first_commit = repo.commit_published()
        collector.collect(self.code, self.output)
        (repo.path / "docs/old.md").unlink()
        git(repo.path, "add", "--all")
        git(repo.path, "commit", "--quiet", "-m", "remove old document")
        second_commit = git(repo.path, "rev-parse", "HEAD")
        git(repo.path, "update-ref", "refs/remotes/origin/main", second_commit)

        collector.collect(self.code, self.output)
        mirror = self.output / "ExampleRepo"
        self.assertEqual((mirror / "documents/docs/old.md").read_text(), "old source")
        manifest = json.loads((mirror / "manifest.json").read_text())
        self.assertTrue(manifest["files"]["documents/docs/old.md"]["retained"])
        self.assertTrue((mirror / "reports" / first_commit / "README.md").is_file())
        self.assertTrue((mirror / "reports" / second_commit / "README.md").is_file())

    def test_unowned_collision_and_output_symlink_are_rejected_without_overwrite(self) -> None:
        repo = self.make_repo()
        repo.write("docs/guide.md", "source")
        repo.commit_published()
        collision = self.output / "ExampleRepo/documents/docs/guide.md"
        collision.parent.mkdir(parents=True)
        self.output.chmod(0o700)
        (self.output / "ExampleRepo").chmod(0o700)
        (self.output / "ExampleRepo/documents").chmod(0o700)
        (self.output / "ExampleRepo/documents/docs").chmod(0o700)
        collision.write_text("private content")
        collision.chmod(0o600)
        with self.assertRaisesRegex(collector.CollectionError, "not collector-owned"):
            collector.collect(self.code, self.output)
        self.assertEqual(collision.read_text(), "private content")

        symlink_output = self.base / "symlink-output"
        symlink_output.symlink_to(self.base, target_is_directory=True)
        with self.assertRaisesRegex(collector.CollectionError, "symlink"):
            collector.collect(self.code, symlink_output)

        nested_output = self.base / "nested-output"
        nested_repo = nested_output / "ExampleRepo"
        nested_repo.mkdir(parents=True)
        nested_output.chmod(0o700)
        nested_repo.chmod(0o700)
        external = self.base / "outside"
        external.mkdir()
        (nested_repo / "documents").symlink_to(external, target_is_directory=True)
        with self.assertRaisesRegex(collector.CollectionError, "symlink"):
            collector.collect(self.code, nested_output)
        self.assertEqual(list(external.iterdir()), [])

    def test_output_and_preview_roots_must_not_overlap_source_repo(self) -> None:
        repo = self.make_repo()
        repo.write("README.md", "# Example\n")
        repo.commit_published()
        info = collector._repo_info(repo.path)
        self.assertIsNotNone(info)
        for output in (self.code, self.base, repo.path):
            with (
                self.subTest(output=output),
                self.assertRaisesRegex(collector.CollectionError, "overlap"),
            ):
                collector.collect(self.code, output)
        with self.assertRaisesRegex(collector.CollectionError, "overlap"):
            collector._validate_preview_location([info], self.code)
        with self.assertRaisesRegex(collector.CollectionError, "overlap"):
            collector._validate_preview_location([info], repo.path / ".docs-library-preview")

        owned_lane = collector.RepoInfo(
            ROOT, "Simple-With-Us", "AI-Fleet-Coordinator", "origin/main", "a" * 40
        )
        safe_preview = ROOT / ".docs-library-preview-safe-public"
        collector._validate_preview_location([owned_lane], safe_preview)
        with self.assertRaisesRegex(collector.CollectionError, "overlap"):
            collector._validate_preview_location([owned_lane], ROOT / "other-preview")

        owned_lane = collector.RepoInfo(
            ROOT, "Simple-With-Us", "AI-Fleet-Coordinator", "origin/main", "a" * 40
        )
        safe_preview = ROOT / ".docs-library-preview-safe-public"
        collector._validate_preview_location([owned_lane], safe_preview)
        with self.assertRaisesRegex(collector.CollectionError, "overlap"):
            collector._validate_preview_location([owned_lane], ROOT / "other-preview")

    def test_new_collection_directories_and_files_are_owner_only_even_with_permissive_umask(
        self,
    ) -> None:
        repo = self.make_repo()
        repo.write("docs/reports/report.md", "Report")
        published = repo.commit_published()
        prior_umask = os.umask(0)
        try:
            collector.collect(self.code, self.output)
        finally:
            os.umask(prior_umask)

        for directory in (
            self.output,
            self.output / "ExampleRepo",
            self.output / "ExampleRepo/documents",
            self.output / "ExampleRepo/documents/docs",
            self.output / "ExampleRepo/reports",
            self.output / "ExampleRepo/reports" / published,
        ):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700, directory)
        for output in self.output.rglob("*"):
            if output.is_file():
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600, output)

    def test_existing_permissive_library_root_is_rejected_without_chmod(self) -> None:
        repo = self.make_repo()
        repo.write("README.md", "# Example\n")
        repo.commit_published()
        self.output.mkdir(mode=0o755)
        self.output.chmod(0o755)
        before = stat.S_IMODE(self.output.stat().st_mode)
        with self.assertRaisesRegex(collector.CollectionError, "mode 0700"):
            collector.collect(self.code, self.output)
        self.assertEqual(stat.S_IMODE(self.output.stat().st_mode), before)

    def test_preview_index_uses_canonical_report_paths_and_excludes_private(self) -> None:
        repo = self.make_repo()
        repo.write("docs/rollouts/known report.md", "Canonical report")
        repo.write("docs/research/study.rst", "Research")
        repo.write("docs/audit/security.md", "Audit")
        repo.write("docs/AUDIT-2026-09-01.md", "Loose audit")
        repo.write("docs/design/design-note.md", "Design")
        repo.write("docs/private/do-not-list.md", "private")
        repo.write("docs/private-audits/do-not-list.md", "private")
        repo.write("docs/rollouts/linked.md", "[private evidence](../private/evidence.jsonl)")
        repo.write("docs/private/evidence.jsonl", "private evidence")
        repo.write("docs/reports/README.md", "old content ignored by source index")
        repo.commit_published()
        info = collector._repo_info(repo.path)
        index = collector.reports_index(info, collector._parse_tree(info)).decode()
        self.assertIn("../rollouts/known%20report.md", index)
        self.assertIn("../research/study.rst", index)
        self.assertIn("../audit/security.md", index)
        self.assertIn("../AUDIT-2026-09-01.md", index)
        self.assertIn("../design/design-note.md", index)
        self.assertNotIn("do-not-list", index)
        self.assertIn("private Fleet-OPS", index)
        self.assertNotIn("docs/private-audits", index)
        self.assertNotIn("old content", index)

    def test_private_audit_documents_require_verified_opt_in_and_stay_out_of_public_index(
        self,
    ) -> None:
        repo = self.make_repo(name="Fleet-OPS")
        repo.write("docs/private-audits/2026-10-30-audit.md", "[evidence](evidence.jsonl)")
        repo.write("docs/private-audits/evidence.jsonl", '{"private":true}')
        repo.write("docs/private-audits/keys/secret.md", "must not copy")
        repo.write("docs/private-audits/.env", "must not copy")
        repo.write("docs/reports/public.md", "Public report")
        repo.commit_published()
        info = collector._repo_info(repo.path)
        self.assertIsNotNone(info)
        self.assertFalse(collector._is_human_document("docs/private-audits/2026-10-30-audit.md"))
        self.assertNotIn(
            "private-audits", collector.reports_index(info, collector._parse_tree(info)).decode()
        )

        output = self.base / "private-central"
        with self.assertRaisesRegex(collector.CollectionError, "verified private repository"):
            collector.collect(
                self.code, output, repo_paths=[repo.path], private_docs_repo="other/Private-Repo"
            )

        collector.collect(
            self.code, output, repo_paths=[repo.path], private_docs_repo="Simple-With-Us/Fleet-OPS"
        )
        mirror = output / "Fleet-OPS"
        self.assertEqual(
            (mirror / "documents/docs/private-audits/2026-10-30-audit.md").read_text(),
            "[evidence](evidence.jsonl)",
        )
        self.assertEqual(
            (mirror / "documents/docs/private-audits/evidence.jsonl").read_text(),
            '{"private":true}',
        )
        self.assertFalse((mirror / "documents/docs/private-audits/keys/secret.md").exists())
        self.assertFalse((mirror / "documents/docs/private-audits/.env").exists())
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o700)
        manifest = json.loads((mirror / "manifest.json").read_text())
        self.assertEqual(manifest["private_docs_opt_in"], "simple-with-us/Fleet-OPS")
        with self.assertRaisesRegex(collector.CollectionError, "same explicit opt-in"):
            collector.collect(self.code, output, repo_paths=[repo.path])

    def test_indexes_only_cli_writes_only_to_preview_root(self) -> None:
        repo = self.make_repo()
        repo.write("docs/rollouts/rollout.md", "canonical")
        published = repo.commit_published()
        preview = self.base / "preview"
        output = self.base / "central-library"
        stdout = StringIO()
        with redirect_stdout(stdout):
            status = collector.main(
                [
                    "--code-root",
                    str(self.code),
                    "--output-root",
                    str(output),
                    "--indexes-only",
                    "--index-preview-root",
                    str(preview),
                ]
            )
        self.assertEqual(status, 0)
        self.assertFalse(output.exists())
        source = json.loads((preview / "ExampleRepo/source.json").read_text())
        self.assertEqual(source["source_ref"], "origin/main")
        self.assertEqual(source["source_commit"], published)
        self.assertNotIn(
            source["source_commit"], (preview / "ExampleRepo/docs/reports/README.md").read_text()
        )

    def test_explicit_repo_path_mode_uses_owned_worktree_and_published_ref(self) -> None:
        repo = self.make_repo()
        repo.write("docs/rollouts/report.md", "[evidence](evidence.jsonl)")
        repo.write("docs/rollouts/evidence.jsonl", "evidence")
        published = repo.commit_published()
        repo.write("docs/rollouts/report.md", "dirty worktree")
        preview = self.base / "repo-path-preview"
        stdout = StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(stdout):
            status = collector.main(
                [
                    "--repo-path",
                    str(repo.path),
                    "--indexes-only",
                    "--index-preview-root",
                    str(preview),
                ]
            )
        self.assertEqual(status, 0)
        self.assertEqual(
            json.loads((preview / "ExampleRepo/source.json").read_text())["source_commit"],
            published,
        )
        index = (preview / "ExampleRepo/docs/reports/README.md").read_text()
        self.assertNotIn("unavailable", index)
        self.assertNotIn(published, index)

    def test_source_ref_must_be_verified_remote_main_or_origin_head(self) -> None:
        repo = self.make_repo()
        repo.write("README.md", "published")
        published = repo.commit_published()
        git(repo.path, "update-ref", "-d", "refs/remotes/origin/main")
        # A verified origin/HEAD may select a non-main default branch.
        git(repo.path, "branch", "--force", "trunk", published)
        git(repo.path, "update-ref", "refs/remotes/origin/trunk", published)
        git(repo.path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
        info = collector._repo_info(repo.path)
        self.assertIsNotNone(info)
        self.assertEqual(info.ref, "origin/trunk")
        self.assertEqual(info.commit, published)

    def test_collection_is_pinned_to_the_captured_commit_if_remote_ref_advances(self) -> None:
        repo = self.make_repo()
        repo.write("docs/guide.md", "captured commit content")
        captured_commit = repo.commit_published()
        info = collector._repo_info(repo.path)
        self.assertIsNotNone(info)

        repo.write("docs/guide.md", "newer origin content")
        git(repo.path, "add", "docs/guide.md")
        git(repo.path, "commit", "--quiet", "-m", "advance published ref")
        newer_commit = git(repo.path, "rev-parse", "HEAD")
        git(repo.path, "update-ref", "refs/remotes/origin/main", newer_commit)

        manifest, _ = collector.collect_repository(info, self.output)
        mirrored = self.output / "ExampleRepo/documents/docs/guide.md"
        self.assertEqual(mirrored.read_text(), "captured commit content")
        self.assertEqual(manifest["source_commit"], captured_commit)
        self.assertNotEqual(manifest["source_commit"], newer_commit)


if __name__ == "__main__":
    unittest.main()
