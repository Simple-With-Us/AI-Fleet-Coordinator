"""lanes-v2-migrate.py tests: the plan, the moves, the refusals, the case-only folder rename, the links.

    cd scripts && python3 -m unittest fleet_lanes.tests.test_migrate_v2 -v

Real git in a throwaway home: repos under Code/, lanes under apps/lanes/ in the OLD layout, a fake
registry passed through FLEET_APPS_JSON, and a stub for lsof.  Nothing outside the temp directory is
read or written.  The volume's letter-case behavior is detected, not assumed: the case-only folder
rename is exercised on a case-insensitive volume (macOS), and on a case-sensitive one (CI) the same
old folder is an ordinary move.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / "lanes-v2-migrate.py"
_spec = importlib.util.spec_from_file_location("lanes_v2_migrate", SCRIPT)
M = importlib.util.module_from_spec(_spec)
sys.modules["lanes_v2_migrate"] = M          # dataclasses look their module up here
_spec.loader.exec_module(M)

TODAY = dt.date(2026, 10, 9)
_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "LC_ALL": "C",
    "GIT_AUTHOR_NAME": "Mig Test", "GIT_AUTHOR_EMAIL": "mig@example.invalid",
    "GIT_COMMITTER_NAME": "Mig Test", "GIT_COMMITTER_EMAIL": "mig@example.invalid",
}
REGISTRY = {
    "owner": "Simple-With-Us",
    "apps": [
        {"repo": "DealDex", "acronym": "DD", "worktreePrefix": "dealdex", "codeDir": "DealDex"},
        {"repo": "AI-Fleet-Coordinator", "acronym": "AFC", "worktreePrefix": "fleet", "codeDir": "AI-Fleet-Coordinator"},
        {"repo": "Congress.Trade", "acronym": "CT", "worktreePrefix": "congress", "codeDir": "Congress.Trade"},
        {"repo": "Socratic-Trade", "acronym": "ST", "worktreePrefix": "trading", "codeDir": "Socratic-Trade"},
    ],
    "seats": [
        {"tag": "CLAUDE", "worktreeSuffix": "claude", "branchPrefixes": ["claude/"]},
        {"tag": "MM", "worktreeSuffix": "minimax", "branchPrefixes": ["minimax/"]},
        {"tag": "CODEX", "worktreeSuffix": "codex", "branchPrefixes": ["codex/"]},
        {"tag": "CURSOR", "worktreeSuffix": "cursor", "branchPrefixes": ["cursor/"]},
    ],
}


def git(cwd, *args, check=True) -> str:
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(cwd), **_ENV}
    proc = subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout.strip()


def tree_hash(root: Path) -> str:
    """Names, link targets and file contents of everything under root (not the .git internals' mtimes)."""
    h = hashlib.sha256()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for n in sorted(dirnames + filenames):
            p = os.path.join(dirpath, n)
            h.update(os.path.relpath(p, root).encode())
            if os.path.islink(p):
                h.update(b"->" + os.readlink(p).encode())
            elif os.path.isfile(p) and ".git/index" not in p.replace(os.sep, "/"):
                with open(p, "rb") as fh:
                    h.update(fh.read())
    return h.hexdigest()


def volume_is_case_insensitive(base: Path) -> bool:
    probe = base / "CaseProbe"
    probe.mkdir()
    try:
        return (base / "caseprobe").exists()
    finally:
        probe.rmdir()


class World(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="lanes-v2-test-")
        self.addCleanup(tmp.cleanup)
        self.home = Path(os.path.realpath(tmp.name)) / "home"
        (self.home / "apps" / "lanes").mkdir(parents=True)
        (self.home / "Code").mkdir()
        self.lanes = self.home / "apps" / "lanes"
        self.ci = volume_is_case_insensitive(self.home)
        reg = self.home.parent / "fleet-apps.json"
        reg.write_text(json.dumps(REGISTRY), encoding="utf-8")
        self.env = {"HOME": str(self.home), "FLEET_APPS_JSON": str(reg)}
        self.procs: list = []                      # (pid, command, cwd) rows the lsof stub reports
        self.lsof_works = True
        self.repos = {name: self.make_repo(name) for name in ("DealDex", "AI-Fleet-Coordinator", "Congress.Trade", "Socratic-Trade")}

    # ---- fixtures
    def make_repo(self, name: str) -> Path:
        path = self.home / "Code" / name
        path.mkdir()
        git(path, "init", "-q", "-b", "main")
        (path / "README").write_text(name + "\n")
        git(path, "add", "README")
        git(path, "commit", "-q", "-m", "init")
        return path

    def lane(self, bucket: str, name: str, repo: str, branch: "str | None" = None) -> Path:
        path = self.lanes / bucket / name
        path.parent.mkdir(parents=True, exist_ok=True)
        git(self.repos[repo], "worktree", "add", "-q", "-b", branch or f"claude/{name}", str(path))
        return path

    def clone(self, bucket: str, name: str, origin: "str | None") -> Path:
        path = self.lanes / bucket / name
        path.parent.mkdir(parents=True, exist_ok=True)
        git(self.home, "init", "-q", "-b", "main", str(path))
        (path / "README").write_text("clone\n")
        git(path, "add", "README")
        git(path, "commit", "-q", "-m", "init")
        if origin:
            git(path, "remote", "add", "origin", origin)
        return path

    # ---- running the script
    def run_cli(self, *args: str, today: "dt.date | None" = None) -> "tuple[int, str]":
        out = io.StringIO()
        rc = M.main(list(args), home=str(self.home), env=self.env, lsof=self.lsof, today=today or TODAY, stdout=out)
        return rc, out.getvalue()

    def lsof(self):
        return list(self.procs) if self.lsof_works else None

    def ctx(self, **kw):
        return M.make_ctx(str(self.home), self.env, lsof=self.lsof, today=TODAY, **kw)

    def plan(self, **kw):
        return {it.old: it for it in M.build_plan(self.ctx(**kw))}

    def worktrees(self, repo: str) -> list:
        return [ln[len("worktree "):] for ln in git(self.repos[repo], "worktree", "list", "--porcelain").splitlines()
                if ln.startswith("worktree ")]

    def assert_valid_lane(self, path: Path, repo: str, branch: str) -> None:
        self.assertTrue(path.is_dir(), path)
        self.assertEqual(git(path, "rev-parse", "--abbrev-ref", "HEAD"), branch)
        self.assertEqual(git(path, "status", "--porcelain"), "")
        self.assertIn(os.path.realpath(path), [os.path.realpath(p) for p in self.worktrees(repo)])


class DryRunTests(World):
    def test_a_dry_run_changes_nothing_and_says_what_it_would_do(self) -> None:
        a = self.lane("fleet", "claude-a", "AI-Fleet-Coordinator")
        self.clone("fleetlink", "claude-legacy-hotfix", "https://github.com/Simple-With-Us/fleetlink-legacy.git")
        (self.lanes / "_managed" / "claude").mkdir(parents=True)
        (self.lanes / "_managed" / "claude" / ".DS_Store").write_text("x")
        before = tree_hash(self.home)
        rc, out = self.run_cli()
        self.assertEqual(rc, 0, out)
        self.assertEqual(tree_hash(self.home), before, "a dry run must not change a byte")
        self.assertIn("Dry run: nothing is changed", out)
        self.assertIn(f"{a}", out.replace("~", str(self.home)))
        self.assertIn("AI-Fleet-Coordinator/claude-a", out)
        self.assertIn("fleetlink-legacy/claude-legacy-hotfix", out)
        self.assertIn("remove-empty", out)
        self.assertFalse((self.lanes / ".lanes-v2-migration.json").exists(), "a dry run writes no log")

    def test_the_default_is_a_dry_run_even_when_json_is_asked_for(self) -> None:
        self.lane("fleet", "claude-a", "AI-Fleet-Coordinator")
        rc, out = self.run_cli("--json")
        body = json.loads(out)
        self.assertEqual((rc, body["apply"]), (0, False))
        self.assertTrue(any(i["action"] == "move" for i in body["items"]))
        self.assertTrue((self.lanes / "fleet" / "claude-a").is_dir())

    def test_apply_with_json_keeps_stdout_one_parseable_document(self) -> None:
        self.lane("fleet", "claude-a", "AI-Fleet-Coordinator")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc, out = self.run_cli("--apply", "--json")
        self.assertEqual(rc, 0, out)
        body = json.loads(out)
        self.assertEqual((body["apply"], body["failures"]), (True, 0))
        self.assertIn("done", err.getvalue(), "the progress lines went to stderr")
        self.assertIn("Finished:", err.getvalue())

    def test_a_folder_that_cannot_be_read_is_a_skip_not_a_traceback(self) -> None:
        a = self.lane("fleet", "claude-a", "AI-Fleet-Coordinator")
        with mock.patch.object(M, "inspect", side_effect=PermissionError("denied")):
            plan = self.plan()
        self.assertEqual(plan[str(a)].action, "skip")
        self.assertIn("cannot be read right now", plan[str(a)].reasons[0])

    def test_a_broken_symlink_where_a_repo_folder_belongs_does_not_crash_the_plan(self) -> None:
        self.lane("fleet", "claude-a", "AI-Fleet-Coordinator")
        (self.lanes / "AI-Fleet-Coordinator").symlink_to(self.home / "nowhere")
        rc, out = self.run_cli()
        self.assertEqual(rc, 0, out)
        self.assertIn("Dry run", out)

    def test_an_error_while_planning_exits_2_not_with_a_traceback(self) -> None:
        self.lane("fleet", "claude-a", "AI-Fleet-Coordinator")
        err = io.StringIO()
        with mock.patch.object(M, "build_plan", side_effect=OSError("disk gone")), contextlib.redirect_stderr(err):
            rc, _out = self.run_cli()
        self.assertEqual(rc, 2)
        self.assertIn("disk gone", err.getvalue())

    def test_usage_errors_are_64(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.run_cli("--grace-days", "-1")[0], 64)
            self.assertEqual(self.run_cli("--nonsense")[0], 64)

    def test_no_lanes_root_is_nothing_to_do(self) -> None:
        shutil.rmtree(self.lanes)
        rc, out = self.run_cli("--apply")
        self.assertEqual(rc, 0)
        self.assertIn("Nothing to do", out)


class MoveTests(World):
    def test_a_worktree_moves_and_leaves_a_symlink_with_a_removal_date(self) -> None:
        old = self.lane("fleet", "claude-a", "AI-Fleet-Coordinator")
        rc, out = self.run_cli("--apply")
        self.assertEqual(rc, 0, out)
        new = self.lanes / "AI-Fleet-Coordinator" / "claude-a"
        self.assert_valid_lane(new, "AI-Fleet-Coordinator", "claude/claude-a")
        self.assertTrue(old.is_symlink())
        self.assertEqual(os.readlink(old), str(new))
        self.assertEqual(git(old, "rev-parse", "--abbrev-ref", "HEAD"), "claude/claude-a", "the old path still works")
        log = json.loads((self.lanes / ".lanes-v2-migration.json").read_text())
        (link,) = [ln for ln in log["links"] if ln["link"] == str(old)]
        self.assertEqual((link["target"], link["remove_after"], link["removed"]), (str(new), "2026-10-16", None))
        self.assertNotIn(str(old), [os.path.realpath(p) for p in self.worktrees("AI-Fleet-Coordinator") if p != str(new)])

    def test_the_repo_comes_from_git_not_from_the_old_folder_name(self) -> None:
        # a Congress.Trade worktree that someone filed under the fleet folder
        old = self.lane("fleet", "claude-misfiled", "Congress.Trade")
        plan = self.plan()
        item = plan[str(old)]
        self.assertEqual(item.new, str(self.lanes / "Congress.Trade" / "claude-misfiled"))
        self.assertTrue(any("git wins" in n for n in item.notes), item.notes)
        self.run_cli("--apply")
        self.assert_valid_lane(self.lanes / "Congress.Trade" / "claude-misfiled", "Congress.Trade", "claude/claude-misfiled")

    def test_a_second_run_is_idempotent(self) -> None:
        self.lane("fleet", "claude-a", "AI-Fleet-Coordinator")
        self.run_cli("--apply")
        before = tree_hash(self.home)
        rc, out = self.run_cli("--apply")
        self.assertEqual(rc, 0, out)
        self.assertEqual(tree_hash(self.home), before)
        self.assertIn("0 done", out)

    def test_a_full_clone_moves_by_its_origin_and_a_clone_without_one_is_refused(self) -> None:
        old = self.clone("fleetlink", "claude-legacy-hotfix", "https://github.com/Simple-With-Us/fleetlink-legacy.git")
        orphan = self.clone("fleetlink", "claude-no-origin", None)
        plan = self.plan()
        self.assertEqual(plan[str(old)].new, str(self.lanes / "fleetlink-legacy" / "claude-legacy-hotfix"))
        self.assertEqual(plan[str(orphan)].action, "skip")
        self.assertIn("no GitHub origin", plan[str(orphan)].reasons[0])
        self.run_cli("--apply")
        new = self.lanes / "fleetlink-legacy" / "claude-legacy-hotfix"
        self.assertTrue((new / ".git").is_dir())
        self.assertEqual(git(new, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertTrue(old.is_symlink())
        self.assertTrue(orphan.is_dir() and not orphan.is_symlink(), "the clone it could not place stays put")

    def test_a_trailing_repo_name_is_dropped_from_a_name_that_is_not_kebab(self) -> None:
        old = self.lane("fleet", "minimax-zulip-stanza-DealDex", "DealDex", branch="minimax/zulip-stanza")
        item = self.plan()[str(old)]
        self.assertEqual(item.new, str(self.lanes / "DealDex" / "minimax-zulip-stanza"))
        self.assertTrue(any("trailing -DealDex" in n for n in item.notes), item.notes)
        self.run_cli("--apply")
        self.assert_valid_lane(self.lanes / "DealDex" / "minimax-zulip-stanza", "DealDex", "minimax/zulip-stanza")

    def test_a_name_that_is_not_a_lane_name_still_moves_with_a_note(self) -> None:
        old = self.lane("_managed", "codecaps-cursor-163", "DealDex", branch="cursor/odd")
        item = self.plan()[str(old)]
        self.assertEqual((item.action, item.new), ("move", str(self.lanes / "DealDex" / "codecaps-cursor-163")))
        self.assertTrue(any("does not read as <seat>-<slug>" in n for n in item.notes), item.notes)
        self.run_cli("--apply")
        self.assertTrue((self.lanes / "DealDex" / "codecaps-cursor-163" / "README").is_file())

    def test_an_old_review_name_becomes_review_pr(self) -> None:
        old = self.lanes / "_review" / "dealdex" / "pr-12-claude"
        old.parent.mkdir(parents=True)
        git(self.repos["DealDex"], "worktree", "add", "-q", "--detach", str(old))
        item = self.plan()[str(old)]
        self.assertEqual(item.new, str(self.lanes / "DealDex" / "review-pr-12-claude"))
        self.run_cli("--apply")
        self.assertTrue((self.lanes / "DealDex" / "review-pr-12-claude" / "README").is_file())
        self.assertTrue(old.is_symlink(), "a symlink stays at the old review path for the grace period")


class RefusalTests(World):
    def setUp(self) -> None:
        super().setUp()
        self.a = self.lane("fleet", "claude-a", "AI-Fleet-Coordinator")
        self.b = self.lane("fleet", "claude-b", "AI-Fleet-Coordinator")

    def reasons(self, path: Path) -> str:
        item = self.plan()[str(path)]
        self.assertEqual(item.action, "skip", item.reasons)
        return " ".join(item.reasons)

    def test_uncommitted_work_is_never_moved(self) -> None:
        (self.a / "notes.txt").write_text("unsaved")                     # untracked
        (self.b / "README").write_text("edited\n")                       # tracked
        self.assertIn("uncommitted work", self.reasons(self.a))
        self.assertIn("uncommitted work", self.reasons(self.b))
        self.run_cli("--apply")
        self.assertTrue(self.a.is_dir() and not self.a.is_symlink() and (self.a / "notes.txt").read_text() == "unsaved")
        self.assertFalse((self.lanes / "AI-Fleet-Coordinator").exists(), "no folder is made for lanes that did not move")

    def test_a_lane_with_a_process_in_it_is_skipped_and_others_still_move(self) -> None:
        self.procs = [(4242, "zsh", str(self.a / "src"))]
        self.assertIn("in use: zsh(4242)", self.reasons(self.a))
        self.run_cli("--apply")
        self.assertTrue(self.a.is_dir() and not self.a.is_symlink())
        self.assertTrue(self.b.is_symlink(), "the free lane moved")

    def test_a_process_with_a_different_letter_case_in_its_cwd_still_counts_on_a_case_insensitive_volume(self) -> None:
        if not self.ci:
            self.skipTest("a case-sensitive volume: the spellings are different folders")
        self.procs = [(7, "node", str(self.a).replace("claude-a", "CLAUDE-A"))]
        self.assertIn("in use", self.reasons(self.a))

    def test_unreadable_lsof_refuses_every_move(self) -> None:
        self.lsof_works = False
        self.assertIn("lsof could not be read", self.reasons(self.a))
        rc, out = self.run_cli("--apply")
        self.assertEqual(rc, 0)
        self.assertTrue(self.a.is_dir() and not self.a.is_symlink() and not self.b.is_symlink())

    def test_a_locked_worktree_is_skipped(self) -> None:
        git(self.repos["AI-Fleet-Coordinator"], "worktree", "lock", "--reason", "do not move", str(self.a))
        self.assertIn("locked", self.reasons(self.a))

    def test_an_existing_target_is_never_overwritten(self) -> None:
        target = self.lanes / "AI-Fleet-Coordinator" / "claude-a"
        target.mkdir(parents=True)
        (target / "keep.txt").write_text("mine")
        self.assertIn("already exists", self.reasons(self.a))
        self.run_cli("--apply")
        self.assertEqual((target / "keep.txt").read_text(), "mine")
        self.assertTrue(self.a.is_dir() and not self.a.is_symlink())

    def test_a_symlink_is_not_followed_or_moved(self) -> None:
        link = self.lanes / "fleet" / "claude-link"
        link.symlink_to(self.a)
        plan = self.plan()
        self.assertEqual(plan[str(link)].action, "note")
        self.run_cli("--apply")
        self.assertTrue(link.is_symlink())

    def test_the_runtime_checkout_is_always_skipped(self) -> None:
        runtime = self.lanes / "_managed" / "fleet" / "agent-sync-runtime"
        runtime.parent.mkdir(parents=True)
        git(self.repos["AI-Fleet-Coordinator"], "worktree", "add", "-q", "--detach", str(runtime))
        item = self.plan()[str(runtime)]
        self.assertEqual(item.action, "skip")
        self.assertIn("live infrastructure", item.reasons[0])
        self.run_cli("--apply")
        self.assertTrue(runtime.is_dir() and not runtime.is_symlink() and (runtime / "README").is_file())

    def test_this_scripts_own_lane_is_in_use(self) -> None:
        ctx = self.ctx()
        object.__setattr__(ctx, "self_paths", (str(self.a / "scripts"),))
        self.assertIn("this script", " ".join(M.processes_in(ctx, str(self.a)) or []))

    def test_a_worktree_with_a_submodule_is_reported_when_git_refuses_it(self) -> None:
        # git refuses `worktree move` for a worktree that has submodules; the step must end as a skip, not a failure
        orig = M.run_git

        def refusing(cwd, argv):
            if tuple(argv[:2]) == ("worktree", "move"):
                return 128, "", "fatal: working trees containing submodules cannot be moved or removed"
            return orig(cwd, argv)
        ctx = self.ctx()
        ctx.git = refusing
        log = {"links": [], "events": []}
        item = M.Item("move", str(self.a), str(self.lanes / "AI-Fleet-Coordinator" / "claude-a"))
        with self.assertRaises(M.Skipped) as cm:
            M.apply_move(ctx, item, log)
        self.assertIn("git refused", str(cm.exception))
        self.assertTrue(self.a.is_dir() and not self.a.is_symlink())
        self.assertEqual(log["links"], [])


class InnerCheckoutTests(World):
    """A lane that holds another checkout (claude -w, subagent isolation: worktree) must not move."""

    def ignore_claude_dir(self, repo: str) -> None:
        main = self.repos[repo]
        (main / ".gitignore").write_text(".claude/\n")
        git(main, "add", ".gitignore")
        git(main, "commit", "-q", "-m", "ignore .claude")

    def lane_with_inner(self) -> "tuple[Path, Path]":
        self.ignore_claude_dir("AI-Fleet-Coordinator")
        lane = self.lane("fleet", "claude-a", "AI-Fleet-Coordinator")
        inner = lane / ".claude" / "worktrees" / "n"
        git(self.repos["AI-Fleet-Coordinator"], "worktree", "add", "-q", "-b", "claude/inner", str(inner))
        return lane, inner

    def test_a_gitignored_inner_worktree_reads_clean_but_still_blocks_the_move(self) -> None:
        lane, inner = self.lane_with_inner()
        self.assertEqual(git(lane, "status", "--porcelain"), "", "the premise: git status cannot see the inner checkout")
        item = self.plan()[str(lane)]
        self.assertEqual(item.action, "skip", item.reasons)
        self.assertIn("inner checkout", " ".join(item.reasons))
        self.assertIn(os.path.join(".claude", "worktrees", "n"), " ".join(item.reasons))
        rc, out = self.run_cli("--apply")
        self.assertEqual(rc, 0, out)
        self.assertTrue(lane.is_dir() and not lane.is_symlink(), "the lane stays where it is")
        self.assertFalse((self.lanes / "AI-Fleet-Coordinator").exists())
        self.assertIn(os.path.realpath(inner), [os.path.realpath(p) for p in self.worktrees("AI-Fleet-Coordinator")])
        self.assertNotIn("prunable", git(self.repos["AI-Fleet-Coordinator"], "worktree", "list", "--porcelain"))

    def test_the_lane_moves_once_the_inner_checkout_is_gone(self) -> None:
        lane, inner = self.lane_with_inner()
        git(self.repos["AI-Fleet-Coordinator"], "worktree", "remove", str(inner))
        rc, out = self.run_cli("--apply")
        self.assertEqual(rc, 0, out)
        self.assert_valid_lane(self.lanes / "AI-Fleet-Coordinator" / "claude-a", "AI-Fleet-Coordinator", "claude/claude-a")

    def test_a_move_that_started_before_the_inner_checkout_appeared_is_refused_at_the_last_moment(self) -> None:
        self.ignore_claude_dir("AI-Fleet-Coordinator")
        lane = self.lane("fleet", "claude-a", "AI-Fleet-Coordinator")
        item = self.plan()[str(lane)]
        self.assertEqual(item.action, "move")
        git(self.repos["AI-Fleet-Coordinator"], "worktree", "add", "-q", "-b", "claude/late", str(lane / ".claude" / "worktrees" / "late"))
        with self.assertRaises(M.Skipped) as cm:
            M.apply_move(self.ctx(), item, {"links": [], "events": []})
        self.assertIn("inner checkout", str(cm.exception))
        self.assertTrue(lane.is_dir() and not lane.is_symlink())

    def test_the_walker_reports_what_git_is_tied_to_and_leaves_standalone_clones_alone(self) -> None:
        lane = self.lane("fleet", "claude-a", "AI-Fleet-Coordinator")
        self.assertEqual(M.inner_checkouts(str(lane)), [], "the lane's own .git is not an inner checkout")
        (lane / "node_modules" / "pkg").mkdir(parents=True)
        git(lane / "node_modules" / "pkg", "init", "-q")
        self.assertEqual(M.inner_checkouts(str(lane)), [], "node_modules is not entered")
        elsewhere = self.home / "elsewhere"
        git(self.home, "init", "-q", str(elsewhere))
        (lane / "link").symlink_to(elsewhere)
        self.assertEqual(M.inner_checkouts(str(lane)), [], "a symlink is not followed")
        # a SwiftPM-style checkout is a standalone clone: nothing outside it names its path
        spm = lane / ".build" / "checkouts" / "Sparkle"
        spm.mkdir(parents=True)
        git(spm, "init", "-q")
        self.assertEqual(M.inner_checkouts(str(lane)), [], "a plain clone is not tied to the lane")
        # a linked worktree (a .git FILE) is: its repository records this path
        deep = lane / "a" / "b" / "c"
        deep.parent.mkdir(parents=True)
        git(self.repos["DealDex"], "worktree", "add", "-q", "--detach", str(deep))
        self.assertEqual(M.inner_checkouts(str(lane)), [str(deep)])
        too_deep = lane / "v" / "w" / "x" / "y" / "z"
        too_deep.parent.mkdir(parents=True)
        git(self.repos["DealDex"], "worktree", "add", "-q", "--detach", str(too_deep))
        self.assertEqual(M.inner_checkouts(str(lane)), [str(deep)], "the walk stops four levels down")
        # a clone with a worktree hanging off it is tied to the lane the other way round
        host = lane / "host"
        host.mkdir()
        git(host, "init", "-q", "-b", "main")
        (host / "f").write_text("x")
        git(host, "add", "f")
        git(host, "commit", "-q", "-m", "x")
        git(host, "worktree", "add", "-q", "--detach", str(self.home / "hanging"))
        self.assertEqual(M.inner_checkouts(str(lane)), sorted([str(deep), str(host)]))

    def test_a_lane_that_only_holds_a_standalone_clone_still_moves(self) -> None:
        lane = self.lane("fleet", "claude-a", "AI-Fleet-Coordinator")
        spm = lane / ".build" / "checkouts" / "Sparkle"
        spm.mkdir(parents=True)
        git(spm, "init", "-q")
        (lane / ".gitignore").write_text(".build/\n")
        git(lane, "add", ".gitignore")
        git(lane, "commit", "-q", "-m", "ignore .build")
        self.assertEqual(self.plan()[str(lane)].action, "move")

    def test_one_lane_with_an_inner_checkout_defers_the_whole_case_only_folder(self) -> None:
        if not self.ci:
            self.skipTest("a case-sensitive volume has no whole-folder case rename")
        self.ignore_claude_dir("DealDex")
        a = self.lane("dealdex", "claude-a", "DealDex")
        b = self.lane("dealdex", "minimax-b", "DealDex", branch="minimax/b")
        git(self.repos["DealDex"], "worktree", "add", "-q", "-b", "claude/inner", str(b / ".claude" / "worktrees" / "n"))
        (folder,) = [i for i in self.plan().values() if i.action == "defer"]
        self.assertIn("minimax-b: ", " ".join(folder.reasons))
        self.assertIn("inner checkout", " ".join(folder.reasons))
        self.run_cli("--apply")
        self.assertIn("dealdex", os.listdir(self.lanes), "nothing in the folder was renamed")
        self.assertTrue(a.exists())


class LegacyFolderTests(World):
    def test_empty_leftovers_are_removed_with_rmdir_only(self) -> None:
        for rel in ("_managed/antigravity", "_managed/botfleet", "_review/botfleet", "_notes"):
            (self.lanes / rel).mkdir(parents=True)
        (self.lanes / "_managed" / "claude").mkdir()
        (self.lanes / "_managed" / "claude" / ".DS_Store").write_text("x")
        (self.lanes / "_managed" / "cursor").mkdir()
        (self.lanes / "_managed" / "cursor" / "real-file.txt").write_text("keep me")
        rc, out = self.run_cli("--apply")
        self.assertEqual(rc, 0, out)
        for rel in ("_managed/antigravity", "_managed/botfleet", "_managed/claude", "_review", "_notes"):
            self.assertFalse((self.lanes / rel).exists(), rel)
        self.assertEqual((self.lanes / "_managed" / "cursor" / "real-file.txt").read_text(), "keep me")

    def test_symlinks_inside_a_legacy_folder_are_never_followed_or_removed(self) -> None:
        keep = self.home / "precious"
        keep.mkdir()
        (keep / "data").write_text("x")
        (self.lanes / "_managed" / "tool").mkdir(parents=True)
        (self.lanes / "_managed" / "tool" / "link").symlink_to(keep)
        self.run_cli("--apply")
        self.assertEqual((keep / "data").read_text(), "x")
        self.assertTrue((self.lanes / "_managed" / "tool" / "link").is_symlink())


class CaseOnlyFolderTests(World):
    def test_a_folder_that_differs_only_in_case_is_renamed_once_for_all_its_lanes(self) -> None:
        a = self.lane("dealdex", "claude-a", "DealDex")
        b = self.lane("dealdex", "minimax-b", "DealDex", branch="minimax/b")
        plan = self.plan()
        if self.ci:
            (folder,) = [i for i in plan.values() if i.action == "rename-folder"]
            self.assertEqual((folder.old, folder.new), (str(self.lanes / "dealdex"), str(self.lanes / "DealDex")))
            self.assertEqual(sorted(folder.members), sorted([str(a), str(b)]))
        else:
            self.assertEqual({plan[str(a)].action, plan[str(b)].action}, {"move"}, "a case-sensitive volume has two folders")
        rc, out = self.run_cli("--apply")
        self.assertEqual(rc, 0, out)
        self.assertIn("DealDex", os.listdir(self.lanes))
        if self.ci:
            self.assertNotIn("dealdex", os.listdir(self.lanes), "the folder carries the exact spelling now")
            self.assertFalse((self.lanes / "DealDex" / "claude-a").is_symlink(), "same path on this volume: no symlink")
            # git records the new spelling: `git worktree repair` alone leaves a case-only difference alone
            # (core.ignorecase), so the migration also rewrites the one gitdir line
            self.assertIn(str(self.lanes / "DealDex" / "claude-a"), self.worktrees("DealDex"))
            self.assertFalse(any("/dealdex/" in p for p in self.worktrees("DealDex")), self.worktrees("DealDex"))
        for name, branch in (("claude-a", "claude/claude-a"), ("minimax-b", "minimax/b")):
            self.assert_valid_lane(self.lanes / "DealDex" / name, "DealDex", branch)

    def test_one_unsafe_lane_defers_the_whole_folder_rename(self) -> None:
        if not self.ci:
            self.skipTest("a case-sensitive volume has no whole-folder case rename")
        a = self.lane("dealdex", "claude-a", "DealDex")
        b = self.lane("dealdex", "minimax-b", "DealDex", branch="minimax/b")
        (b / "wip.txt").write_text("unsaved")
        plan = self.plan()
        (folder,) = [i for i in plan.values() if i.action == "defer"]
        self.assertIn("minimax-b: uncommitted work", " ".join(folder.reasons))
        self.run_cli("--apply")
        self.assertIn("dealdex", os.listdir(self.lanes))
        self.assertTrue((self.lanes / "dealdex" / "minimax-b" / "wip.txt").is_file())
        # once the work is saved, the next run renames it
        git(b, "add", "wip.txt")
        git(b, "commit", "-q", "-m", "wip")
        self.run_cli("--apply")
        self.assertIn("DealDex", os.listdir(self.lanes))
        self.assertTrue(a.exists())

    def test_a_process_in_the_folder_defers_it(self) -> None:
        if not self.ci:
            self.skipTest("a case-sensitive volume has no whole-folder case rename")
        self.lane("dealdex", "claude-a", "DealDex")
        self.procs = [(9, "zsh", str(self.lanes / "dealdex"))]
        (folder,) = [i for i in self.plan().values() if i.action in ("defer", "rename-folder")]
        self.assertEqual(folder.action, "defer")
        self.assertIn("in use", " ".join(folder.reasons))

    def test_a_lane_that_also_changes_name_is_renamed_after_its_folder(self) -> None:
        old = self.lane("dealdex", "minimax-x-DealDex", "DealDex", branch="minimax/x")
        self.run_cli("--apply")
        new = self.lanes / "DealDex" / "minimax-x"
        self.assert_valid_lane(new, "DealDex", "minimax/x")
        self.assertIn("DealDex", os.listdir(self.lanes))
        self.assertTrue(old.is_symlink(), "the old name keeps working for the grace period")
        self.assertEqual(os.readlink(old), str(new))

    def test_the_folder_holding_a_clone_for_another_repo_waits_for_it_to_move_first(self) -> None:
        # the fleetlink shape: one old folder, a worktree that belongs in the case-only folder and a clone that does not
        if not self.ci:
            self.skipTest("only a case-insensitive volume makes the clone block a whole-folder rename")
        self.repos["FleetLink"] = self.make_repo("FleetLink")
        reg = json.loads(Path(self.env["FLEET_APPS_JSON"]).read_text())
        reg["apps"].append({"repo": "FleetLink", "acronym": "FL", "worktreePrefix": "fleetlink", "codeDir": "FleetLink"})
        Path(self.env["FLEET_APPS_JSON"]).write_text(json.dumps(reg))
        wt = self.lane("fleetlink", "minimax-zulip-stanza-FleetLink", "FleetLink", branch="minimax/zulip-stanza")
        clone = self.clone("fleetlink", "claude-legacy-hotfix", "https://github.com/Simple-With-Us/fleetlink-legacy.git")
        rc, out = self.run_cli("--apply")
        self.assertEqual(rc, 0, out)
        self.assertTrue((self.lanes / "fleetlink-legacy" / "claude-legacy-hotfix" / ".git").is_dir())
        self.assert_valid_lane(self.lanes / "FleetLink" / "minimax-zulip-stanza", "FleetLink", "minimax/zulip-stanza")
        self.assertIn("FleetLink", os.listdir(self.lanes))
        self.assertTrue(clone.is_symlink() and os.readlink(clone) == str(self.lanes / "fleetlink-legacy" / "claude-legacy-hotfix"))
        self.assertTrue(wt.is_symlink() and os.readlink(wt) == str(self.lanes / "FleetLink" / "minimax-zulip-stanza"))

    def test_the_folder_is_renamed_in_place_first_so_its_lanes_never_leave_their_paths(self) -> None:
        if not self.ci:
            self.skipTest("a case-sensitive volume has no whole-folder case rename")
        self.lane("dealdex", "claude-a", "DealDex")
        real = os.rename
        calls: list = []

        def spy(src, dst, *a, **k):
            calls.append((os.path.basename(src), os.path.basename(dst)))
            return real(src, dst, *a, **k)
        with mock.patch.object(M.os, "rename", side_effect=spy):
            M.case_rename(self.ctx(), str(self.lanes / "dealdex"), str(self.lanes / "DealDex"))
        self.assertEqual(calls, [("dealdex", "DealDex")], "one rename, no temporary name")
        self.assertIn("DealDex", os.listdir(self.lanes))

    def test_a_volume_that_ignores_the_direct_rename_falls_back_to_the_temporary_name(self) -> None:
        if not self.ci:
            self.skipTest("a case-sensitive volume has no whole-folder case rename")
        self.lane("dealdex", "claude-a", "DealDex")
        real = os.rename
        calls: list = []

        def flaky(src, dst, *a, **k):
            calls.append(os.path.basename(dst))
            if len(calls) == 1:
                raise OSError("refused")
            return real(src, dst, *a, **k)
        with mock.patch.object(M.os, "rename", side_effect=flaky):
            M.case_rename(self.ctx(), str(self.lanes / "dealdex"), str(self.lanes / "DealDex"))
        self.assertEqual(len(calls), 3, calls)
        self.assertTrue(calls[1].startswith(".dealdex-case-rename-"))
        self.assertIn("DealDex", os.listdir(self.lanes))
        self.assertNotIn("dealdex", os.listdir(self.lanes))

    def test_a_full_clone_that_also_changes_name_is_renamed_in_place_because_git_cannot_move_a_main_tree(self) -> None:
        if not self.ci:
            self.skipTest("a case-sensitive volume has no whole-folder case rename")
        old = self.clone("dealdex", "claude-x-DealDex", "https://github.com/Simple-With-Us/DealDex.git")
        self.lane("dealdex", "minimax-b", "DealDex", branch="minimax/b")
        (folder,) = [i for i in self.plan().values() if i.action == "rename-folder"]
        self.assertEqual(folder.extra["clones"], ["claude-x-DealDex"])
        rc, out = self.run_cli("--apply")
        self.assertEqual(rc, 0, out)
        new = self.lanes / "DealDex" / "claude-x"
        self.assertTrue((new / ".git").is_dir() and (new / "README").is_file())
        self.assertEqual(git(new, "rev-parse", "--abbrev-ref", "HEAD"), "main")
        self.assertTrue(old.is_symlink() and os.readlink(old) == str(new), "the old name keeps working for the grace period")

    def test_another_checkout_still_in_the_folder_at_apply_time_defers_the_rename(self) -> None:
        if not self.ci:
            self.skipTest("a case-sensitive volume has no whole-folder case rename")
        self.lane("dealdex", "claude-a", "DealDex")
        (folder,) = [i for i in self.plan().values() if i.action == "rename-folder"]
        # a clone for another repo shows up after the plan was made and cannot move out
        stray = self.clone("dealdex", "claude-stray", "https://github.com/Simple-With-Us/elsewhere.git")
        with self.assertRaises(M.Skipped) as cm:
            M.apply_bucket(self.ctx(), folder, {"links": [], "events": []})
        self.assertIn("claude-stray", str(cm.exception))
        self.assertIn("dealdex", os.listdir(self.lanes), "the folder was not renamed")
        self.assertTrue(stray.is_dir())


class GitdirCaseTests(World):
    def test_only_a_pure_case_difference_inside_the_admin_dir_is_rewritten(self) -> None:
        lane = self.lane("fleet", "claude-a", "AI-Fleet-Coordinator")
        main = str(self.repos["AI-Fleet-Coordinator"])
        admin = next((self.repos["AI-Fleet-Coordinator"] / ".git" / "worktrees").iterdir())
        gitdir_file = admin / "gitdir"
        right = gitdir_file.read_text().strip()
        self.assertFalse(M.fix_gitdir_case(str(lane), main), "already right: untouched")
        gitdir_file.write_text(right.replace("claude-a", "CLAUDE-A") + "\n")
        self.assertTrue(M.fix_gitdir_case(str(lane), main))
        self.assertEqual(gitdir_file.read_text().strip(), right)
        # a different path is never "fixed"
        gitdir_file.write_text("/somewhere/else/.git\n")
        self.assertFalse(M.fix_gitdir_case(str(lane), main))
        self.assertEqual(gitdir_file.read_text().strip(), "/somewhere/else/.git")
        # an admin dir outside <main>/.git/worktrees is refused
        self.assertFalse(M.fix_gitdir_case(str(lane), str(self.repos["DealDex"])))


class CodexTests(World):
    def test_codex_worktrees_wait_for_the_flag_and_then_move(self) -> None:
        old = self.home / ".codex" / "worktrees" / "ab12" / "DealDex"
        old.parent.mkdir(parents=True)
        git(self.repos["DealDex"], "worktree", "add", "-q", "-b", "codex/ab12", str(old))
        item = self.plan()[str(old)]
        self.assertEqual(item.action, "skip")
        self.assertIn("--include-codex", item.reasons[0])
        self.run_cli("--apply")
        self.assertTrue(old.is_dir() and not old.is_symlink())
        rc, out = self.run_cli("--apply", "--include-codex")
        self.assertEqual(rc, 0, out)
        new = self.lanes / "_codex" / "ab12" / "DealDex"
        self.assert_valid_lane(new, "DealDex", "codex/ab12")
        self.assertTrue(old.is_symlink())

    def test_the_managed_codex_folder_is_codexs_live_root_so_it_waits_for_the_flag_too(self) -> None:
        old = self.lanes / "_managed" / "codex" / "cd34" / "DealDex"
        old.parent.mkdir(parents=True)
        git(self.repos["DealDex"], "worktree", "add", "-q", "-b", "codex/cd34", str(old))
        (old.parent / ".codex-worktree-name").write_text("cd34\n")
        item = self.plan()[str(old)]
        self.assertEqual(item.action, "skip")
        self.assertIn("--include-codex", " ".join(item.reasons))
        self.run_cli("--apply")
        self.assertTrue(old.is_dir() and not old.is_symlink(), "an open Codex app is not pulled out from under itself")
        rc, out = self.run_cli("--apply", "--include-codex")
        self.assertEqual(rc, 0, out)
        new = self.lanes / "_codex" / "cd34" / "DealDex"
        self.assert_valid_lane(new, "DealDex", "codex/cd34")
        self.assertEqual((new.parent / ".codex-worktree-name").read_text(), "cd34\n", "the marker is copied beside the checkout")
        self.assertTrue((old.parent / ".codex-worktree-name").is_file(), "the original marker is never deleted")

    def test_a_slug_folder_with_only_a_marker_is_said_not_ignored(self) -> None:
        marker_only = self.home / ".codex" / "worktrees" / "ef56"
        marker_only.mkdir(parents=True)
        (marker_only / ".codex-worktree-name").write_text("ef56\n")
        item = self.plan()[str(marker_only)]
        self.assertEqual(item.action, "note")
        self.assertIn("marker", " ".join(item.reasons))


class LinkTests(World):
    def moved(self) -> "tuple[Path, Path]":
        old = self.lane("fleet", "claude-a", "AI-Fleet-Coordinator")
        self.run_cli("--apply")
        return old, self.lanes / "AI-Fleet-Coordinator" / "claude-a"

    def test_links_wait_for_their_date_then_go(self) -> None:
        old, new = self.moved()
        rc, out = self.run_cli("--remove-links", "--apply", today=dt.date(2026, 10, 15))
        self.assertEqual(rc, 0, out)
        self.assertIn("waiting until 2026-10-16", out)
        self.assertTrue(old.is_symlink())
        rc, out = self.run_cli("--remove-links", today=dt.date(2026, 10, 16))
        self.assertIn("due", out)
        self.assertTrue(old.is_symlink(), "a dry run removes nothing")
        rc, out = self.run_cli("--remove-links", "--apply", today=dt.date(2026, 10, 16))
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.lexists(old))
        self.assertTrue(new.is_dir(), "the lane itself is untouched")
        self.assertFalse((self.lanes / "fleet").exists(), "the old prefix folder goes with its last link")
        log = json.loads((self.lanes / ".lanes-v2-migration.json").read_text())
        self.assertIsNotNone(log["links"][0]["removed"])

    def test_a_link_whose_lane_was_retired_goes_on_its_date_and_lets_the_old_folder_empty(self) -> None:
        old, new = self.moved()
        git(self.repos["AI-Fleet-Coordinator"], "worktree", "remove", str(new))        # what the janitor does to a merged lane
        self.assertTrue(old.is_symlink() and not old.exists(), "the link now leads nowhere")
        rc, out = self.run_cli("--remove-links", "--apply", today=dt.date(2026, 10, 15))
        self.assertIn("waiting until 2026-10-16", out)
        self.assertTrue(old.is_symlink(), "not before its date")
        rc, out = self.run_cli("--remove-links", "--apply", today=dt.date(2026, 10, 16))
        self.assertEqual(rc, 0, out)
        self.assertIn("its target is gone", out)
        self.assertNotIn("refused", out)
        self.assertFalse(os.path.lexists(old))
        self.assertFalse((self.lanes / "fleet").exists(), "the old prefix folder goes with its last link")

    def test_a_link_that_now_points_elsewhere_is_refused(self) -> None:
        old, new = self.moved()
        old.unlink()
        other = self.home / "elsewhere"
        other.mkdir()
        old.symlink_to(other)
        rc, out = self.run_cli("--remove-links", "--apply", today=dt.date(2026, 11, 1))
        self.assertIn("points somewhere else", out)
        self.assertTrue(old.is_symlink() and other.is_dir())

    def test_a_real_folder_where_a_link_was_is_never_removed(self) -> None:
        old, new = self.moved()
        old.unlink()
        old.mkdir()
        (old / "mine.txt").write_text("x")
        rc, out = self.run_cli("--remove-links", "--apply", today=dt.date(2026, 11, 1))
        self.assertIn("not a symlink", out)
        self.assertEqual((old / "mine.txt").read_text(), "x")

    def test_a_link_outside_the_lanes_root_is_refused(self) -> None:
        old, new = self.moved()
        log_path = self.lanes / ".lanes-v2-migration.json"
        log = json.loads(log_path.read_text())
        victim = self.home / "victim"
        victim.symlink_to(new)
        log["links"].append({"link": str(victim), "target": str(new), "made": "x", "remove_after": "2000-01-01", "removed": None})
        log_path.write_text(json.dumps(log))
        rc, out = self.run_cli("--remove-links", "--apply", today=dt.date(2026, 11, 1))
        self.assertTrue(victim.is_symlink())
        self.assertIn("refused: not inside the lanes root", out)


class SafetyTests(unittest.TestCase):
    def test_git_is_limited_to_reads_plus_worktree_move_and_repair(self) -> None:
        for ok in (["rev-parse", "--absolute-git-dir"], ["status", "--porcelain"], ["worktree", "list", "--porcelain"],
                   ["worktree", "move", "/a/old", "/a/new"], ["worktree", "repair", "/a/x", "/a/y"],
                   ["config", "--get", "remote.origin.url"]):
            M.check_git(ok)
        for bad in (["worktree", "move", "--force", "/a/old", "/a/new"], ["worktree", "move", "-f", "-f", "/a", "/b"],
                    ["worktree", "remove", "/a"], ["worktree", "prune"], ["reset", "--hard"], ["clean", "-fd"],
                    ["push", "origin", "main"], ["checkout", "main"], ["branch", "-D", "x"], ["gc"],
                    ["worktree", "repair", "--force"], ["worktree", "lock", "/a"], ["worktree", "move", "/a"], []):
            with self.assertRaises(M.MigrateError, msg=bad):
                M.check_git(bad)

    def test_paths_outside_the_lanes_root_are_refused_before_anything_runs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(os.path.realpath(tmp))
            (home / "apps" / "lanes").mkdir(parents=True)
            reg = home / "fleet-apps.json"
            reg.write_text(json.dumps(REGISTRY))
            ctx = M.make_ctx(str(home), {"HOME": str(home), "FLEET_APPS_JSON": str(reg)}, lsof=lambda: [], today=TODAY)
            calls: list = []
            ctx.git = lambda cwd, argv: calls.append(argv) or (0, "", "")
            for old, new in ((str(home / "Code" / "X"), str(home / "apps" / "lanes" / "A" / "x")),
                             (str(home / "apps" / "lanes" / "A" / "x"), str(home / "elsewhere" / "x")),
                             (str(home / "apps" / "lanes"), str(home / "apps" / "lanes" / "A" / "x"))):
                with self.assertRaises(M.MigrateError):
                    M.apply_move(ctx, M.Item("move", old, new), {"links": [], "events": []})
            self.assertEqual(calls, [])
            with self.assertRaises(M.MigrateError):
                M.apply_rmdir(ctx, M.Item("remove-empty", str(home)), {"links": [], "events": []})

    def test_the_script_runs_under_python_3_9_syntax(self) -> None:
        import ast
        ast.parse(SCRIPT.read_text(encoding="utf-8"), feature_version=(3, 9))


if __name__ == "__main__":
    unittest.main()
