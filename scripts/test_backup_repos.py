#!/usr/bin/env python3
"""Tests for scripts/backup_repos.py (AFC-682).

The clone command is stubbed through BACKUP_REPOS_CLONE_CMD, so nothing
reaches GitHub or needs GH_TOKEN;  the stub copies fixture content for known
repos and fails for any repo whose name ends in "-missing".
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "backup_repos.py"
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import backup_repos

STUB = """\
import shutil, sys
from pathlib import Path
repo, dest = sys.argv[1], sys.argv[2]
if repo.endswith("-missing"):
    sys.exit(3)
root = Path(dest)
(root / "node_modules" / "pkg").mkdir(parents=True)
(root / ".git").mkdir(parents=True)
(root / "dist").mkdir(parents=True)
(root / "build").mkdir(parents=True)
(root / "src").mkdir(parents=True)
(root / "src" / "node_modules" / "pkg").mkdir(parents=True)
(root / "src" / ".git").mkdir(parents=True)
(root / "src" / "dist").mkdir(parents=True)
(root / "src" / "build").mkdir(parents=True)
(root / "app.py").write_text("print('hi')\\n")
(root / "src" / "keep.txt").write_text("keep\\n")
(root / ".env").write_text("SECRET=1\\n")
(root / ".env.local").write_text("SECRET=2\\n")
(root / "src" / ".env").write_text("SECRET=3\\n")
(root / ".git" / "config").write_text("[core]\\n")
(root / "src" / ".git" / "HEAD").write_text("ref\\n")
(root / "node_modules" / "pkg" / "index.js").write_text("x\\n")
(root / "src" / "node_modules" / "pkg" / "index.js").write_text("x\\n")
(root / "dist" / "out.js").write_text("x\\n")
(root / "src" / "dist" / "out.js").write_text("x\\n")
(root / "build" / "out.js").write_text("x\\n")
(root / "src" / "build" / "out.js").write_text("x\\n")
(root / "secret.key").write_text("k\\n")
(root / "cert.pem").write_text("p\\n")
(root / "id_rsa").write_text("rsa\\n")
(root / "src" / "id_backup").write_text("rsa2\\n")
(root / "credentials.json").write_text("{}\\n")
(root / "src" / "my-credentials.txt").write_text("c\\n")
(root / "service-account.json").write_text("{}\\n")
(root / "src" / "svc-service-account.json").write_text("{}\\n")
"""


class BackupReposTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="backup-repos-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.stub = self.tmp / "clone_stub.py"
        self.stub.write_text(STUB)
        self.env_file = self.tmp / "github_env"
        self.step_summary = self.tmp / "step_summary"
        self.repo_list = self.tmp / "repo-list.txt"
        self.outdir = self.tmp / "backups"

    def run_script(self, repos):
        self.repo_list.write_text("\n".join(repos) + "\n")
        env = dict(os.environ)
        env["BACKUP_REPOS_CLONE_CMD"] = f"{sys.executable} {self.stub} {{repo}} {{dest}}"
        env["GITHUB_ENV"] = str(self.env_file)
        env["GITHUB_STEP_SUMMARY"] = str(self.step_summary)
        env.pop("GH_TOKEN", None)
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--repo-list",
                str(self.repo_list),
                "--outdir",
                str(self.outdir),
                "--date",
                "2026-11-10",
            ],
            env=env,
            capture_output=True,
            text=True,
        )

    def zip_names(self, zip_path):
        with zipfile.ZipFile(zip_path) as zf:
            return set(zf.namelist())

    def test_all_succeed_exit_zero(self):
        proc = self.run_script(["Alpha", "Beta"])
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertTrue((self.outdir / "Alpha-backup-2026-11-10.zip").exists())
        self.assertTrue((self.outdir / "Beta-backup-2026-11-10.zip").exists())
        summary = (self.outdir / "BACKUP-SUMMARY.txt").read_text()
        self.assertIn("COMPLETE", summary)
        self.assertIn("failed (0): -", summary)
        self.assertIn("BACKUP_ARTIFACT_SUFFIX=\n", self.env_file.read_text())
        # Clone dirs are cleaned up after zipping.
        self.assertFalse((self.outdir / "Alpha").exists())

    def test_failed_target_keeps_partial_archives_and_exits_nonzero(self):
        proc = self.run_script(["Alpha", "Ghost-missing", "Beta"])
        self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
        # The failure did not discard the archives that succeeded.
        self.assertTrue((self.outdir / "Alpha-backup-2026-11-10.zip").exists())
        self.assertTrue((self.outdir / "Beta-backup-2026-11-10.zip").exists())
        self.assertFalse((self.outdir / "Ghost-missing-backup-2026-11-10.zip").exists())
        summary = (self.outdir / "BACKUP-SUMMARY.txt").read_text()
        self.assertIn("PARTIAL", summary)
        self.assertIn("failed (1): Ghost-missing", summary)
        self.assertIn("incomplete", summary)
        self.assertIn("BACKUP_ARTIFACT_SUFFIX=-PARTIAL\n", self.env_file.read_text())
        self.assertIn("PARTIAL", self.step_summary.read_text())
        self.assertIn("::error::could not clone Simple-With-Us/Ghost-missing", proc.stdout)

    def test_secret_and_noise_exclusions(self):
        # Actual archive content, root and nested:  a fixture shaped like a
        # private repo with secret-shaped files at both levels must archive
        # only the two plain source files, so no secret spills offsite.
        proc = self.run_script(["Alpha"])
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        names = self.zip_names(self.outdir / "Alpha-backup-2026-11-10.zip")
        self.assertEqual(names, {"app.py", "src/keep.txt"})
        # Byte-level check:  no secret value appears anywhere in the archive.
        blob = (self.outdir / "Alpha-backup-2026-11-10.zip").read_bytes()
        for needle in (b"SECRET=", b"rsa", b"service-account", b"credential"):
            self.assertNotIn(needle, blob)

    def test_exclusion_matcher(self):
        for excluded in (
            ".env",
            ".env.production",
            "sub/.env",
            ".git/config",
            "a/.git/HEAD",
            "node_modules/x/y.js",
            "a/node_modules/x/y.js",
            "dist/out.js",
            "a/b.key",
            "c.pem",
            "d.p12",
            "e.pfx",
            "id_rsa",
            "sub/id_ed25519",
            "credentials.json",
            "sub/my-credential.txt",
            "service-account.json",
            "sub/svc-service-account.json",
        ):
            self.assertTrue(backup_repos.is_excluded(excluded), excluded)
        for kept in ("app.py", "src/keep.txt", "README.md", "keys.txt", "src/identity.py"):
            self.assertFalse(backup_repos.is_excluded(kept), kept)

    def test_count_guard_catches_count_mismatch(self):
        # A stray archive makes the on-disk count disagree with the repo
        # list:  the guard must turn that into a loud failure.
        proc_env_repo = ["Alpha"]
        self.outdir.mkdir(parents=True)
        stray = self.outdir / "Stray-backup-2026-11-10.zip"
        with zipfile.ZipFile(stray, "w") as zf:
            zf.writestr("x", "y")
        proc = self.run_script(proc_env_repo)
        self.assertEqual(proc.returncode, 1, proc.stderr + proc.stdout)
        self.assertIn("::error::expected", proc.stdout)


if __name__ == "__main__":
    unittest.main()
