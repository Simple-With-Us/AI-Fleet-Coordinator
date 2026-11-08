"""code-main-keeper (scripts/code-main-keeper.sh) with real git and a fake Code root:  it fast-forwards a clean allowlisted clone on main and
does nothing else to anything (AFC-618, the mirror rule)."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "code-main-keeper.sh"
GIT = shutil.which("git") or "/usr/bin/git"


class KeeperCase(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(os.path.realpath(tmp.name))
        self.code = self.base / "Code"
        self.logs = self.base / "logs"
        self.code.mkdir()
        self.remotes = self.base / "remotes"
        self.remotes.mkdir()
        self.environ = {"HOME": str(self.base), "PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                        "CODE_ROOT": str(self.code), "CODE_MAIN_KEEPER_LOG_DIR": str(self.logs)}

    def git(self, cwd: Path, *args: str) -> str:
        res = subprocess.run([GIT, "-c", "user.email=t@e", "-c", "user.name=t", "-c", "commit.gpgsign=false", *args], cwd=cwd,
                             capture_output=True, text=True, env=self.environ)
        self.assertEqual(res.returncode, 0, f"git {args}: {res.stderr}")
        return res.stdout.strip()

    def make(self, name: str) -> Path:
        """A clone under Code whose origin is a bare repo with one commit on main."""
        bare = self.remotes / f"{name}.git"
        seed = self.remotes / f"{name}-seed"
        seed.mkdir()
        self.git(seed, "init", "-q", "-b", "main")
        (seed / "a.txt").write_text("1\n")
        self.git(seed, "add", "a.txt")
        self.git(seed, "commit", "-q", "-m", "one")
        subprocess.run([GIT, "clone", "-q", "--bare", str(seed), str(bare)], check=True, capture_output=True, env=self.environ)
        clone = self.code / name
        subprocess.run([GIT, "clone", "-q", str(bare), str(clone)], check=True, capture_output=True, env=self.environ)
        return clone

    def push_upstream(self, name: str, text: str = "2\n") -> str:
        """A new commit on the origin's main, as another seat's merged PR would be."""
        seed = self.remotes / f"{name}-seed"
        (seed / "a.txt").write_text(text)
        self.git(seed, "commit", "-q", "-am", "two")
        self.git(seed, "push", "-q", str(self.remotes / f"{name}.git"), "main")
        return self.git(seed, "rev-parse", "HEAD")

    def run_keeper(self, *flags: str, allow: str | None = None) -> str:
        env = dict(self.environ)
        if allow is not None:
            env["CODE_MAIN_KEEPER_ALLOWLIST"] = allow
        res = subprocess.run(["/bin/bash", str(SCRIPT), *flags], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(res.returncode, 0, res.stderr)
        log = self.logs / "code-main-keeper.log"
        return log.read_text() if log.exists() else ""

    def snapshot(self, clone: Path) -> dict[str, str]:
        """Everything the keeper must not change."""
        return {
            "head": self.git(clone, "rev-parse", "HEAD"),
            "branch": self.git(clone, "branch", "--show-current"),
            "refs": self.git(clone, "for-each-ref", "refs/heads", "refs/remotes", "refs/stash"),
            "stash": self.git(clone, "stash", "list"),
            "status": self.git(clone, "status", "--porcelain"),
        }


class FastForward(KeeperCase):
    def test_a_clean_allowlisted_clone_on_main_is_fast_forwarded(self) -> None:
        clone = self.make("Alpha")
        new = self.push_upstream("Alpha")
        log = self.run_keeper(allow="Alpha")
        self.assertEqual(self.git(clone, "rev-parse", "HEAD"), new)
        self.assertIn("SYNC  Alpha  ff-only +1", log)

    def test_already_current_is_ok(self) -> None:
        self.make("Alpha")
        self.assertIn("OK    Alpha  already at origin/main", self.run_keeper(allow="Alpha"))

    def test_the_default_allowlist_names_the_fleet_repos_and_no_push_clone(self) -> None:
        text = SCRIPT.read_text()
        block = text[text.index("ALLOWLIST_DEFAULT=("):].split(")", 1)[0]
        names = re.findall(r'"([^"]+)"', block)
        self.assertIn("AI-Fleet-Coordinator", names)
        self.assertIn("HogHunter", names)
        self.assertEqual(len(names), len(set(names)))
        self.assertFalse([n for n in names if n.endswith("-push") or n == "homebrew-tap"])


class LeavesEverythingElseAlone(KeeperCase):
    def test_a_clone_off_the_allowlist_is_not_fetched_or_moved(self) -> None:
        clone = self.make("Beta")
        before = self.snapshot(clone)
        new = self.push_upstream("Beta")
        log = self.run_keeper(allow="Alpha")
        self.assertNotEqual(self.git(clone, "rev-parse", "HEAD"), new)
        self.assertEqual(self.snapshot(clone), before)
        self.assertEqual(self.git(clone, "rev-parse", "origin/main"), before["head"])  # no fetch either
        self.assertIn("NOTE  Beta  mirror only (not on the allowlist)", log)

    def test_an_empty_allowlist_moves_nothing(self) -> None:
        clone = self.make("Alpha")
        before = self.snapshot(clone)
        self.push_upstream("Alpha")
        self.run_keeper(allow="")
        self.assertEqual(self.snapshot(clone), before)

    def test_a_dirty_tree_is_logged_and_kept(self) -> None:
        clone = self.make("Alpha")
        self.push_upstream("Alpha")
        (clone / "a.txt").write_text("mine\n")
        before = self.snapshot(clone)
        log = self.run_keeper(allow="Alpha")
        self.assertEqual(self.snapshot(clone), before)
        self.assertEqual((clone / "a.txt").read_text(), "mine\n")
        self.assertIn("NOTE  Alpha  dirty tree (1 path(s))", log)
        self.assertIn("HOLD  Alpha  dirty tree", log)

    def test_an_untracked_file_counts_as_dirty(self) -> None:
        clone = self.make("Alpha")
        self.push_upstream("Alpha")
        (clone / "stray.txt").write_text("x\n")
        before = self.snapshot(clone)
        self.assertIn("HOLD  Alpha  dirty tree", self.run_keeper(allow="Alpha"))
        self.assertEqual(self.snapshot(clone), before)

    def test_a_clean_feature_branch_is_not_switched_to_main(self) -> None:
        clone = self.make("Alpha")
        self.git(clone, "checkout", "-q", "-b", "claude/work")
        self.push_upstream("Alpha")
        before = self.snapshot(clone)
        log = self.run_keeper(allow="Alpha")
        self.assertEqual(self.snapshot(clone), before)
        self.assertEqual(self.git(clone, "branch", "--show-current"), "claude/work")
        self.assertIn("NOTE  Alpha  on 'claude/work', not main", log)
        self.assertIn("1 local-only branch(es): claude/work", log)
        self.assertIn("HOLD  Alpha  on 'claude/work', not main", log)

    def test_a_detached_head_is_left_detached(self) -> None:
        clone = self.make("Alpha")
        self.git(clone, "checkout", "-q", "--detach")
        before = self.snapshot(clone)
        log = self.run_keeper(allow="Alpha")
        self.assertEqual(self.snapshot(clone), before)
        self.assertEqual(self.git(clone, "branch", "--show-current"), "")
        self.assertIn("HOLD  Alpha  on 'detached', not main", log)

    def test_local_commits_on_main_are_never_reset(self) -> None:
        clone = self.make("Alpha")
        (clone / "b.txt").write_text("local\n")
        self.git(clone, "add", "b.txt")
        self.git(clone, "commit", "-q", "-m", "local work")
        self.push_upstream("Alpha")
        before = self.snapshot(clone)
        log = self.run_keeper(allow="Alpha")
        self.assertEqual(self.git(clone, "rev-parse", "HEAD"), before["head"])
        self.assertTrue((clone / "b.txt").exists())
        self.assertIn("HOLD  Alpha  local main has 1 commit(s) not on origin/main", log)

    def test_a_stash_is_reported_and_kept(self) -> None:
        clone = self.make("Alpha")
        (clone / "a.txt").write_text("stashed\n")
        self.git(clone, "stash", "push", "-q")
        before = self.snapshot(clone)
        log = self.run_keeper(allow="Alpha")
        self.assertEqual(self.snapshot(clone), before)
        self.assertEqual(self.git(clone, "stash", "list").count("stash@{0}"), 1)
        self.assertIn("NOTE  Alpha  1 stash(es)", log)

    def test_a_pushed_branch_is_not_called_local_only(self) -> None:
        clone = self.make("Alpha")
        self.git(clone, "branch", "kept")
        self.git(clone, "push", "-q", "origin", "kept")
        self.assertNotIn("local-only", self.run_keeper(allow="Alpha"))

    def test_a_long_branch_list_is_cut_to_five_names(self) -> None:
        clone = self.make("Alpha")
        for i in range(8):
            self.git(clone, "branch", f"b{i}")
        log = self.run_keeper(allow="Alpha")
        self.assertIn("8 local-only branch(es): b0 b1 b2 b3 b4 ...", log)

    def test_dry_run_changes_and_fetches_nothing(self) -> None:
        clone = self.make("Alpha")
        before = self.snapshot(clone)
        self.push_upstream("Alpha")
        log = self.run_keeper("--dry-run", allow="Alpha")
        self.assertEqual(self.snapshot(clone), before)
        self.assertEqual(self.git(clone, "rev-parse", "origin/main"), before["head"])
        self.assertIn("DRY   Alpha", log)

    def test_a_linked_worktree_under_code_is_reported_never_removed(self) -> None:
        clone = self.make("Alpha")
        stray = self.code / "alpha-wt"
        self.git(clone, "worktree", "add", "-q", "-b", "x", str(stray))
        log = self.run_keeper(allow="Alpha")
        self.assertTrue(stray.is_dir())
        self.assertIn("STRAY-WORKTREE  alpha-wt", log)

    def test_a_stale_lock_that_is_not_empty_is_left_and_nothing_runs(self) -> None:
        clone = self.make("Alpha")
        new = self.push_upstream("Alpha")
        lock = self.logs / "run.lock"
        lock.mkdir(parents=True)
        (lock / "keep.txt").write_text("x\n")
        old = 1_000_000_000
        os.utime(lock, (old, old))
        log = self.run_keeper(allow="Alpha")
        self.assertTrue((lock / "keep.txt").exists())
        self.assertNotEqual(self.git(clone, "rev-parse", "HEAD"), new)
        self.assertIn("HOLD  stale run lock is not empty", log)

    def test_the_script_runs_no_destructive_git_command(self) -> None:
        # Log lines are prose (one tells the owner how to remove a stray worktree);  every other line is code.
        code = [ln for ln in SCRIPT.read_text().splitlines() if not ln.lstrip().startswith(("#", "log "))]
        verbs = set()
        for ln in code:
            # `-c key=value` takes an argument, so the word after it is still an option, not the verb.
            for m in re.finditer(r'\bgit(?: -C "?\$\{?\w+\}?"?)? (?:-c \S+ |-\S+ )*([a-z][a-z-]*)', ln):
                verbs.add(m.group(1))
        read_or_ff = {"rev-parse", "status", "for-each-ref", "show-ref", "stash", "branch", "fetch", "merge", "merge-base", "rev-list"}
        self.assertEqual(verbs - read_or_ff, set(), verbs)
        text = "\n".join(code)
        self.assertNotRegex(text, r"\bgit\b[^\n]* stash (?!list)")
        self.assertNotRegex(text, r"\bgit\b[^\n]* branch (?!--show-current)")
        self.assertNotRegex(text, r"\bgit\b[^\n]* merge (?!--ff-only|-base)")
        for word in ("--prune", " reset", "clean -", "checkout", "switch", "rm -rf", "worktree remove", "--force", " restore "):
            self.assertNotIn(word, text, word)


if __name__ == "__main__":
    unittest.main()
