"""code_main_sync with real git: the stale-lock failure the server job hit, and everything it must leave alone (AFC-618)."""
from __future__ import annotations

import contextlib
import io
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fleet_lanes import code_main_sync as S

GIT = shutil.which("git") or "/usr/bin/git"


def run(args: list[str], cwd: Path) -> str:
    env = {"HOME": os.environ.get("HOME", ""), "PATH": os.environ["PATH"], "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    return subprocess.run([GIT, *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout.strip()


class SyncCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(os.path.realpath(self.tmp.name))
        self.code = self.home / "Code"
        self.code.mkdir()
        self.upstream = self.home / "upstream.git"
        run(["init", "-q", "--bare", "-b", "main", str(self.upstream)], self.home)
        seed = self.home / "seed"
        run(["clone", "-q", str(self.upstream), str(seed)], self.home)
        run(["commit", "-q", "--allow-empty", "-m", "one"], seed)
        run(["push", "-q", "origin", "HEAD:main"], seed)
        self.seed = seed
        self.repo = self.code / "Example"
        run(["clone", "-q", str(self.upstream), str(self.repo)], self.home)
        # The origin is a local bare repo, so no network is needed;  this one check is what it cannot pass.
        patcher = mock.patch.object(S, "remote_is_fleet_github", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.seed / "new.txt").write_text("new\n", encoding="utf-8")
        run(["add", "new.txt"], seed)
        run(["commit", "-q", "-m", "two"], seed)
        run(["push", "-q", "origin", "HEAD:main"], seed)
        self.vars = {"HOME": str(self.home), "FLEET_CODE_ROOT": str(self.code), "FLEET_HOST_KIND": "mac",
                     "FLEET_LANE_GUARD_STATE_DIR": str(self.home / "state"), "GITHUB_TOKEN": "x",
                     "FLEET_PLACEMENT_EXCEPTIONS": str(self.home / "no-rows.json")}
        self.lock = self.repo / ".git" / "index.lock"

    def sync(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = S.main(list(args), env=self.vars)
        return code, out.getvalue()

    def head(self) -> str:
        return run(["rev-parse", "HEAD"], self.repo)

    def origin(self) -> str:
        return run(["rev-parse", "HEAD"], self.seed)

    def age(self, path: Path, hours: float) -> None:
        when = time.time() - hours * 3600
        os.utime(path, (when, when))


class Basics(SyncCase):
    def test_a_clean_clone_moves(self) -> None:
        code, out = self.sync()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.head(), self.origin())
        self.assertIn("updated Example", out)

    def test_dirty_work_is_never_touched(self) -> None:
        (self.repo / "mine.txt").write_text("keep\n", encoding="utf-8")
        before = self.head()
        code, out = self.sync()
        self.assertEqual(code, 0)
        self.assertEqual(self.head(), before)
        self.assertEqual((self.repo / "mine.txt").read_text(encoding="utf-8"), "keep\n")
        self.assertIn("dirty", out)

    def test_local_commits_are_never_touched(self) -> None:
        run(["commit", "-q", "--allow-empty", "-m", "local"], self.repo)
        before = self.head()
        code, out = self.sync()
        self.assertEqual((code, self.head()), (0, before))
        self.assertIn("ahead of or diverged", out)

    def test_a_clone_on_another_branch_is_left_alone(self) -> None:
        run(["switch", "-q", "-c", "codex/x"], self.repo)
        code, out = self.sync()
        self.assertEqual(code, 0)
        self.assertEqual(run(["branch", "--show-current"], self.repo), "codex/x")
        self.assertIn("expected main", out)

    def test_a_detached_clone_is_left_alone(self) -> None:
        run(["switch", "-q", "--detach"], self.repo)
        code, out = self.sync()
        self.assertEqual(code, 0)
        self.assertIn("a detached HEAD", out)

    def test_a_folder_that_is_no_repo_is_not_counted_or_failed(self) -> None:
        (self.code / "Docs").mkdir()
        code, out = self.sync()
        self.assertEqual(code, 0)
        self.assertIn("note Docs: not a Git checkout", out)
        self.assertIn("skipped=0", out)

    def test_dry_run_changes_nothing(self) -> None:
        run(["fetch", "-q", "origin"], self.repo)
        before = self.head()
        code, out = self.sync("--dry-run")
        self.assertEqual((code, self.head()), (0, before))
        self.assertIn("would_update=1", out)


class StaleLock(SyncCase):
    def test_the_server_failure_is_diagnosed_when_a_lock_cannot_be_cleared(self) -> None:
        self.lock.write_text("", encoding="utf-8")
        self.age(self.lock, 200)
        with mock.patch.object(S, "lock_is_held", return_value=None):
            code, out = self.sync()
        self.assertEqual(code, 1)
        self.assertIn("fail Example: fast-forward exit=", out)
        self.assertIn("index.lock", out)
        self.assertTrue(self.lock.exists())

    def test_an_old_unheld_lock_is_cleared_and_the_clone_moves(self) -> None:
        self.lock.write_text("", encoding="utf-8")
        self.age(self.lock, 200)
        with mock.patch.object(S, "lock_is_held", return_value=False):
            code, out = self.sync()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.head(), self.origin())
        self.assertFalse(self.lock.exists())
        self.assertIn("cleared Example", out)

    def test_a_young_lock_stays(self) -> None:
        self.lock.write_text("", encoding="utf-8")
        with mock.patch.object(S, "lock_is_held", return_value=False):
            code, out = self.sync()
        self.assertEqual(code, 1)
        self.assertTrue(self.lock.exists())
        self.assertIn("keep Example", out)

    def test_a_held_lock_stays_however_old(self) -> None:
        self.lock.write_text("", encoding="utf-8")
        self.age(self.lock, 200)
        with mock.patch.object(S, "lock_is_held", return_value=True):
            code, _ = self.sync()
        self.assertEqual(code, 1)
        self.assertTrue(self.lock.exists())

    def test_real_lsof_sees_a_held_lock(self) -> None:
        if not any(os.access(p, os.X_OK) for p in ("/usr/bin/lsof", "/usr/sbin/lsof", "/bin/lsof")):
            self.skipTest("no lsof here")
        self.lock.write_text("", encoding="utf-8")
        self.assertIs(S.lock_is_held(self.lock), False)
        with open(self.lock, encoding="utf-8"):
            self.assertIs(S.lock_is_held(self.lock), True)

    def test_a_dry_run_never_clears(self) -> None:
        run(["fetch", "-q", "origin"], self.repo)
        self.lock.write_text("", encoding="utf-8")
        self.age(self.lock, 200)
        with mock.patch.object(S, "lock_is_held", return_value=False):
            _, out = self.sync("--dry-run")
        self.assertTrue(self.lock.exists())
        self.assertIn("would clear", out)


class Exceptions(SyncCase):
    def test_a_service_pinned_clone_is_left_alone_with_its_reason(self) -> None:
        rows = self.home / "rows.json"
        rows.write_text("""{"exceptions": [{"id": "pinned", "pattern": "{code}/Example", "class": "service", "creator": "b",
            "reason": "r", "decided_by": "CLAUDE", "decided_on": "2026-11-04", "expires": "2099-01-01", "hosts": ["mac"]}]}""",
                        encoding="utf-8")
        self.vars["FLEET_PLACEMENT_EXCEPTIONS"] = str(rows)
        before = self.head()
        code, out = self.sync()
        self.assertEqual((code, self.head()), (0, before))
        self.assertIn("exception pinned", out)


class OneBadClone(SyncCase):
    def test_it_does_not_stop_the_others(self) -> None:
        second = self.code / "Zed"
        run(["clone", "-q", str(self.upstream), str(second)], self.home)
        real = S.process_repo

        def flaky(repo, *a, **k):
            if repo.name == "Example":
                raise RuntimeError("boom")
            return real(repo, *a, **k)

        with mock.patch.object(S, "process_repo", flaky):
            code, out = self.sync()
        self.assertEqual(code, 1)
        self.assertIn("fail Example: RuntimeError", out)
        self.assertIn("Zed", out)


class Errors(unittest.TestCase):
    def test_only_fleet_github_origins_count(self) -> None:
        self.assertTrue(S.remote_is_fleet_github("https://github.com/Simple-With-Us/X.git"))
        self.assertTrue(S.remote_is_fleet_github("git@github.com:jaywedgeworth22/Fleet-OPS.git"))
        self.assertFalse(S.remote_is_fleet_github("https://github.com/someone/X.git"))
        self.assertFalse(S.remote_is_fleet_github("/local/path.git"))

    def test_a_credential_in_git_stderr_is_not_logged(self) -> None:
        line = S._first_line("\nfatal: unable to access 'https://user:secret@github.com/a/b.git/': failed\n")
        self.assertNotIn("secret", line)

    def test_no_host_path_in_the_source(self) -> None:
        text = Path(S.__file__).read_text(encoding="utf-8")
        for mark in ("/mnt/", "/root/"):
            self.assertNotIn(mark, text)


if __name__ == "__main__":
    unittest.main()
