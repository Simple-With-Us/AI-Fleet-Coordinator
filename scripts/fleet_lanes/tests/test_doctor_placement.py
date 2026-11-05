"""The doctor's placement fields and main-clone checks, with real git in a fake home (AFC-618).

The promise under test:  placement reporting changes, and what the cleaners read does not.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from fleet_lanes import doctor as D
from fleet_lanes import install_tools as IT
from fleet_lanes import layout as L
from fleet_lanes.tests import fixtures_guard as F

GIT = shutil.which("git") or "/usr/bin/git"
URL = "https://github.com/Simple-With-Us/BotFleet.git"


def git(args: list[str], cwd: Path) -> str:
    env = {"HOME": str(cwd), "PATH": os.environ["PATH"], "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e"}
    return subprocess.run([GIT, *args], cwd=cwd, check=True, capture_output=True, text=True, env=env).stdout.strip()


class Case(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(os.path.realpath(self.tmp.name)) / "home"
        self.code = self.home / "Code"
        self.lanes = self.home / "apps" / "lanes"
        self.code.mkdir(parents=True)
        self.lanes.mkdir(parents=True)
        self.upstream = self.home / "up.git"
        git(["init", "-q", "--bare", "-b", "main", str(self.upstream)], self.home)
        self.repo = self.code / "BotFleet"
        git(["clone", "-q", str(self.upstream), str(self.repo)], self.home)
        git(["commit", "-q", "--allow-empty", "-m", "one"], self.repo)
        git(["push", "-q", "origin", "HEAD:main"], self.repo)
        git(["remote", "set-url", "origin", URL], self.repo)
        reg = self.home / "reg.json"
        reg.write_text(json.dumps(F.REGISTRY_DATA), encoding="utf-8")
        self.vars = {"HOME": str(self.home), "FLEET_APPS_JSON": str(reg), "FLEET_HOST_KIND": "mac",
                     "FLEET_LANES_EXTERNAL_ROOT": "", "PATH": os.environ["PATH"],
                     "FLEET_PLACEMENT_EXCEPTIONS": str(self.home / "rows.json")}
        self.rows()

    def rows(self, *rows: dict) -> None:
        (self.home / "rows.json").write_text(json.dumps({"exceptions": list(rows)}), encoding="utf-8")

    def row(self, pattern: str, **kw) -> dict:
        base = {"id": "r", "pattern": pattern, "class": "service", "creator": "x", "reason": "because",
                "decided_by": "CLAUDE", "decided_on": "2026-11-04", "expires": "2099-01-01", "hosts": ["mac"]}
        base.update(kw)
        return base

    def report(self, **kw) -> dict:
        return D.build_report(str(self.home), self.vars, cwd=None, use_gh=False, tmp_scan_roots=[], lsof_cwds=lambda: [], **kw)

    def co(self, report: dict, path: Path) -> dict:
        real = os.path.realpath(path)
        return next(c for c in report["checkouts"] if c["realpath"] == real)

    def anomalies(self, report: dict, kind: str) -> list[str]:
        return [a["path"] for a in report["placement_anomalies"] if a["type"] == kind]


class RepoLocalWorktrees(Case):
    def setUp(self) -> None:
        super().setUp()
        self.muse = self.repo / ".muse" / "worktrees" / "subagent-v2-a"
        self.claude = self.repo / ".claude" / "worktrees" / "b"
        git(["worktree", "add", "-q", "-b", "mc/a", str(self.muse)], self.repo)
        git(["worktree", "add", "-q", "-b", "claude/b", str(self.claude)], self.repo)

    def test_they_are_not_sanctioned_and_stay_cleanup_eligible(self) -> None:
        rep = self.report()
        for path in (self.muse, self.claude):
            co = self.co(rep, path)
            self.assertEqual(co["location_class"], "MANAGED")
            self.assertEqual(co["layout_status"], "wrong")
            self.assertIs(co["sanctioned"], False)
            self.assertIs(co["cleanup_eligible"], True)
            self.assertIs(co["placement_violation"], True)

    def test_the_old_strict_count_is_unchanged_and_the_new_flag_counts_them(self) -> None:
        rep = self.report()
        self.assertEqual(rep["summary"]["strict_violations"], 0)
        self.assertEqual(sorted(self.anomalies(rep, "MISPLACED")), sorted(os.path.realpath(p) for p in (self.claude, self.muse)))
        self.assertGreaterEqual(rep["summary"]["placement_violations"], 2)

    def test_strict_placement_exits_two_and_strict_does_not(self) -> None:
        out = open(os.devnull, "w", encoding="utf-8")
        self.addCleanup(out.close)
        base = ["--home", str(self.home), "--no-gh", "--json", "--tmp-root", str(self.home / "none")]
        kw = {"env": self.vars, "cwd": str(self.home), "lsof_cwds": lambda: [], "stdout": out, "stderr": out}
        self.assertEqual(D.main([*base], **kw), 0)
        self.assertEqual(D.main([*base, "--strict"], **kw), 0)
        self.assertEqual(D.main([*base, "--strict-placement"], **kw), 2)

    def test_an_exception_excuses_and_an_expired_one_does_not(self) -> None:
        self.rows(self.row("{code}/BotFleet/.muse"))
        rep = self.report()
        self.assertIs(self.co(rep, self.muse)["placement_violation"], False)
        self.assertEqual(self.co(rep, self.muse)["placement_exception"], "r")
        self.rows(self.row("{code}/BotFleet/.muse", decided_on="2020-01-01", expires="2020-02-01"))
        rep = self.report()
        self.assertIs(self.co(rep, self.muse)["placement_violation"], True)
        self.assertIn("expired", self.co(rep, self.muse)["placement_reason"])
        self.assertEqual(len(self.anomalies(rep, "EXPIRED-EXCEPTION")), 1)


class CleanerUnchanged(Case):
    def test_layout_and_location_answers_stay_as_they_were(self) -> None:
        roots = L.make_roots(str(self.home), self.vars)
        reg = L.load_registry(env=self.vars)
        for rel, want in ((".muse/worktrees/x", "muse-repo"), (".claude/worktrees/y", "claude-repo")):
            path = self.repo / rel
            res = L.explain_layout(path, reg, roots)
            self.assertEqual(str(res.status), "wrong")
            self.assertIn(want, res.reasons[0])
            self.assertEqual(str(res.location), "MANAGED")

    def test_only_the_two_repo_local_rows_split_the_flags(self) -> None:
        roots = L.make_roots(str(self.home), self.vars)
        split = {h.name for h in roots.harness_locations if h.cleanup_eligible != h.sanctioned}
        self.assertEqual(split, {"claude-repo", "muse-repo"})
        self.assertEqual({h.name for h in roots.harness_locations if not h.sanctioned},
                         {"claude-repo", "muse-repo", "zcode-workspace", "antigravity-scratch", "botfleet-workspaces", "documents"})

    def test_the_cleaner_list_ignores_the_new_fields(self) -> None:
        co = D.Checkout(path="/x", realpath="/x")
        co.placement_violation = True
        self.assertFalse(D.is_cleaner_candidate(co))


class FullCloneInLane(Case):
    def test_a_clone_in_a_lane_folder_is_a_violation_and_under_a_tool_root_is_not(self) -> None:
        bad = self.lanes / "BotFleet" / "full"
        ok = self.lanes / "_managed" / "fleet" / "runtime"
        for dest in (bad, ok):
            dest.parent.mkdir(parents=True, exist_ok=True)
            git(["clone", "-q", str(self.upstream), str(dest)], self.home)
            git(["remote", "set-url", "origin", URL], dest)
        rep = self.report()
        self.assertIs(self.co(rep, bad)["placement_violation"], True)
        self.assertEqual(self.co(rep, bad)["placement_reason"], "full clone in a lane folder")
        self.assertIs(self.co(rep, ok)["placement_violation"], False)


class MainClones(Case):
    def state(self, rep: dict, name: str = "BotFleet") -> dict:
        return next(r for r in rep["main_clones"] if r["repo"] == name)

    def fake_origin(self, behind: int = 0) -> None:
        """Local refs only: the doctor never fetches, so a remote-tracking ref is all it needs."""
        git(["update-ref", "refs/remotes/origin/main", "HEAD"], self.repo)
        for _ in range(behind):
            head = git(["commit-tree", "refs/remotes/origin/main^{tree}", "-p", "refs/remotes/origin/main", "-m", "n"], self.repo)
            git(["update-ref", "refs/remotes/origin/main", head], self.repo)

    def test_on_main_and_current_has_no_issues(self) -> None:
        self.fake_origin()
        rep = self.report()
        row = self.state(rep)
        self.assertEqual((row["branch"], row["default_branch"], row["ahead"], row["behind"], row["issues"]), ("main", "main", 0, 0, []))
        self.assertEqual(self.anomalies(rep, "MAIN-CLONE"), [])

    def test_off_the_default_branch(self) -> None:
        self.fake_origin()
        git(["switch", "-q", "-c", "codex/x"], self.repo)
        self.assertIn("off-default-branch", self.state(self.report())["issues"])

    def test_detached(self) -> None:
        self.fake_origin()
        git(["switch", "-q", "--detach"], self.repo)
        row = self.state(self.report())
        self.assertIn("detached", row["issues"])
        self.assertTrue(row["detached_sha"])

    def test_ahead(self) -> None:
        self.fake_origin()
        git(["commit", "-q", "--allow-empty", "-m", "local"], self.repo)
        row = self.state(self.report())
        self.assertEqual(row["ahead"], 1)
        self.assertIn("ahead", row["issues"])

    def test_behind_only_past_the_threshold(self) -> None:
        self.fake_origin(behind=3)
        self.assertNotIn("behind", self.state(self.report())["issues"])
        row = self.state(self.report(main_clone_behind=2))
        self.assertEqual(row["behind"], 3)
        self.assertIn("behind", row["issues"])

    def test_a_stale_lock_is_named(self) -> None:
        self.fake_origin()
        lock = self.repo / ".git" / "index.lock"
        lock.write_text("", encoding="utf-8")
        old = lock.stat().st_mtime - 9 * 3600
        os.utime(lock, (old, old))
        self.assertIn("stale-index-lock", self.state(self.report())["issues"])

    def test_a_service_pinned_clone_with_a_row_is_reported_and_excused(self) -> None:
        self.fake_origin()
        git(["switch", "-q", "--detach"], self.repo)
        self.rows(self.row("{code}/BotFleet"))
        rep = self.report()
        row = self.state(rep)
        self.assertEqual(row["exception"], "r")
        self.assertIn("detached", row["issues"])
        self.assertEqual(self.anomalies(rep, "MAIN-CLONE"), [])

    def test_a_folder_that_is_no_repo_is_not_listed(self) -> None:
        (self.code / "Docs").mkdir()
        self.fake_origin()
        self.assertEqual([r["repo"] for r in self.report()["main_clones"]], ["BotFleet"])

    def test_a_local_repo_with_no_remote_is_not_listed(self) -> None:
        self.fake_origin()
        local = self.code / "Scratch"
        local.mkdir()
        git(["init", "-q", "-b", "main"], local)
        git(["commit", "-q", "--allow-empty", "-m", "x"], local)
        self.assertEqual([r["repo"] for r in self.report()["main_clones"]], ["BotFleet"])

    def test_a_lane_is_not_a_main_clone(self) -> None:
        self.fake_origin()
        (self.lanes / "BotFleet").mkdir()
        git(["worktree", "add", "-q", "-b", "claude/z", str(self.lanes / "BotFleet" / "claude-z")], self.repo)
        self.assertEqual([r["repo"] for r in self.report()["main_clones"]], ["BotFleet"])


class Shipping(unittest.TestCase):
    def test_the_stable_copy_carries_the_shim_and_the_rows(self) -> None:
        self.assertIn("placement.py", IT.REQUIRED_MODULES)
        self.assertEqual(IT.file_mode(IT.GIT_SHIM_FILE), 0o755)
        self.assertTrue(IT._expected_shape(IT.GIT_SHIM_FILE))
        self.assertTrue(IT._expected_shape(f"{IT.PACKAGE}/{IT.EXCEPTIONS_FILE}"))

    def test_the_claude_matcher_covers_the_bash_gate_and_the_spawn_tools(self) -> None:
        import re

        for tool in ("Bash", "Agent", "Task", "EnterWorktree", "mcp__terminal__run_in_terminal",
                     "mcp__plugin_desktop-commander_desktop-commander__start_process"):
            self.assertIsNotNone(re.fullmatch(IT.CLAUDE_MATCHER, tool), tool)
        self.assertIsNone(re.fullmatch(IT.CLAUDE_MATCHER, "Read"))
        self.assertTrue(IT._matcher_gates(IT.CLAUDE_MATCHER, "Bash"))


class RealGitPath(unittest.TestCase):
    def test_lane_skips_the_shim_on_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            shims = Path(os.path.realpath(tmp)) / "shims"
            shims.mkdir()
            (shims / "git").write_text("#!/bin/sh\n", encoding="utf-8")
            os.chmod(shims / "git", 0o755)
            link = Path(os.path.realpath(tmp)) / "bin"
            link.mkdir()
            os.symlink(shims / "git", link / "git")
            got = L.real_git({"PATH": f"{link}:{shims}:{os.path.dirname(GIT)}"})
            self.assertEqual(os.path.realpath(got), os.path.realpath(GIT))
            self.assertEqual(L.real_git({"PATH": f"{link}"}), "git")


if __name__ == "__main__":
    unittest.main()
