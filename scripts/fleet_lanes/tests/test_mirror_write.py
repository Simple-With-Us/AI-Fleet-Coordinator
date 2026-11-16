"""The mirror rule through the real shim and real git, in a fake home:  a commit or a stash in a ~/Code clone warns, `mirror block`
refuses, and nothing else is touched (AFC-618)."""
from __future__ import annotations

import subprocess
import unittest
from pathlib import Path

from fleet_lanes import mirror_write as M
from fleet_lanes import placement as P
from fleet_lanes.tests.test_git_shim import REAL_GIT, ShimCase

MESSAGE = "~/Code is a mirror; do this work in a lane: ~/apps/lane new <app> <slug>"


class ModeLine(unittest.TestCase):
    def test_default_is_detect_and_never_a_class(self) -> None:
        m = P.parse_mode("")
        self.assertEqual(m.mirror, "detect")
        self.assertFalse(m.blocks_mirror())
        self.assertNotIn("mirror", P.RULE_CLASSES)

    def test_bare_block_does_not_turn_the_mirror_block_on(self) -> None:
        self.assertFalse(P.parse_mode("block").blocks_mirror())

    def test_mirror_block_and_off(self) -> None:
        self.assertTrue(P.parse_mode("mirror block").blocks_mirror())
        self.assertEqual(P.parse_mode("mirror off").mirror, "off")
        self.assertFalse(P.parse_mode("off\nmirror block").blocks_mirror())

    def test_a_typo_never_turns_a_block_on(self) -> None:
        m = P.parse_mode("mirror blok\nmirror")
        self.assertEqual(m.mirror, "detect")
        self.assertEqual(len(m.problems), 2)

    def test_the_environment_switch_is_off(self) -> None:
        self.assertEqual(P.read_mode("/nonexistent", {"FLEET_LANE_GUARD": "off"}).mirror, "off")


class Verbs(unittest.TestCase):
    def test_writes_and_reads(self) -> None:
        w = lambda *a: M.write_verb(list(a))  # noqa: E731
        self.assertEqual(w("commit", "-m", "x"), "commit")
        self.assertEqual(w("stash"), "stash")
        self.assertEqual(w("stash", "push", "-u"), "stash")
        self.assertEqual(w("stash", "pop"), "stash")
        self.assertIsNone(w("stash", "list"))
        self.assertIsNone(w("stash", "show", "-p"))
        self.assertIsNone(w("commit", "--dry-run"))
        self.assertIsNone(w("status"))
        self.assertIsNone(w())


class Mirror(ShimCase):
    def dirty(self, text: str = "x\n") -> None:
        (self.repo / "f.txt").write_text(text, encoding="utf-8")
        self.real(["add", "f.txt"], self.repo)

    def commit(self, **env: str) -> subprocess.CompletedProcess:
        return self.git("-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "-m", "work", **env)

    def test_commit_warns_and_still_commits_by_default(self) -> None:
        self.dirty()
        res = self.commit()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(MESSAGE, res.stderr)
        self.assertIn("Warning", res.stderr)
        self.assertEqual(self.real(["log", "-1", "--format=%s"], self.repo).stdout.strip(), "work")
        ev = self.events()
        self.assertEqual([(e["action"], e["class"], e["verb"]) for e in ev], [("mirror-warn", "mirror", "commit")])

    def test_stash_warns_and_still_stashes(self) -> None:
        (self.repo / "tracked.txt").write_text("a\n", encoding="utf-8")
        self.real(["add", "tracked.txt"], self.repo)
        res = self.git("-c", "user.email=t@e", "-c", "user.name=t", "stash", "push", "-q")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(MESSAGE, res.stderr)
        self.assertEqual(self.real(["stash", "list"], self.repo).stdout.count("stash@{0}"), 1)

    def test_stash_list_and_commit_dry_run_say_nothing(self) -> None:
        self.assertEqual(self.git("stash", "list").stderr, "")
        self.dirty()
        res = self.git("commit", "--dry-run")
        self.assertNotIn("mirror", res.stderr)
        self.assertEqual(self.events(), [])

    def test_block_refuses_before_anything_changes(self) -> None:
        self.set_mode("mirror block")
        self.dirty()
        res = self.commit()
        self.assertEqual(res.returncode, 128)
        self.assertIn("Blocked", res.stderr)
        self.assertIn(MESSAGE, res.stderr)
        self.assertEqual(self.real(["log", "-1", "--format=%s"], self.repo).stdout.strip(), "init")
        self.assertEqual(self.events()[0]["action"], "mirror-block")

    def test_bare_block_still_only_warns(self) -> None:
        self.set_mode("block")
        self.dirty()
        self.assertEqual(self.commit().returncode, 0)

    def test_mirror_off_and_the_kill_switch_are_silent(self) -> None:
        self.dirty()
        self.set_mode("mirror off")
        res = self.commit()
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        self.set_mode("detect")
        self.dirty("y\n")
        res = self.commit(FLEET_LANE_GUARD="off")
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        self.assertEqual(self.events(), [])

    def test_minus_capital_c_from_outside_is_judged(self) -> None:
        self.dirty()
        res = self.git("-C", str(self.repo), "-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "-m", "w", cwd=self.home)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(MESSAGE, res.stderr)

    def test_a_commit_in_a_lane_is_not_judged(self) -> None:
        lane = self.lanes / "BotFleet" / "claude-x"
        self.real(["worktree", "add", "-q", "-b", "claude/x", str(lane)], self.repo)
        (lane / "g.txt").write_text("g\n", encoding="utf-8")
        self.real(["add", "g.txt"], lane)
        res = self.git("-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "-m", "w", cwd=lane,
                       FLEET_LANE_GUARD_STATE_DIR=str(self.state))
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        self.assertEqual(self.events(), [])

    def test_a_repo_outside_code_is_not_judged_and_never_starts_python(self) -> None:
        other = self.home / "scratch"
        other.mkdir()
        self.real(["init", "-q", "-b", "main"], other)
        res = self.git("-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "w", cwd=other,
                       FLEET_GIT_SHIM_PYTHON="/nonexistent")
        self.assertEqual((res.returncode, res.stderr), (0, ""))
        self.assertEqual(self.events(), [])

    def test_a_subfolder_of_a_clone_is_judged(self) -> None:
        sub = self.repo / "deep"
        sub.mkdir()
        res = self.git("-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "w", cwd=sub)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(MESSAGE, res.stderr)

    def test_an_alias_of_commit_is_judged(self) -> None:
        self.real(["config", "alias.ci", "commit"], self.repo)
        res = self.git("-c", "user.email=t@e", "-c", "user.name=t", "ci", "-q", "--allow-empty", "-m", "w")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(MESSAGE, res.stderr)


if __name__ == "__main__":
    unittest.main()
