"""The git shim, with real git in a fake home: deny, allow, argv preservation, recursion, unmounted lanes symlink,
missing Python, aliases, -C and --git-dir (AFC-618)."""
from __future__ import annotations

import json
import os
import shutil
import statistics
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent
SHIM = PKG / "shims" / "git"
REAL_GIT = shutil.which("git") or "/usr/bin/git"
FLEET_URL = "https://github.com/Simple-With-Us/BotFleet.git"


class ShimCase(unittest.TestCase):
    def setUp(self) -> None:
        # Not under a temp root name the guard treats as temp for this home: the guard exempts a home inside temp.
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(os.path.realpath(self.tmp.name)) / "home"
        self.code = self.home / "Code"
        self.lanes = self.home / "apps" / "lanes"
        self.code.mkdir(parents=True)
        self.lanes.mkdir(parents=True)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        os.symlink(SHIM, self.bin / "git")
        self.mode_file = self.home / ".config" / "fleet" / "lane-guard-mode"
        self.mode_file.parent.mkdir(parents=True)
        self.state = self.home / ".local" / "state" / "fleet"
        self.repo = self.code / "BotFleet"
        self.repo.mkdir()
        self.real(["init", "-q", "-b", "main"], self.repo)
        self.real(["-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"], self.repo)
        self.real(["remote", "add", "origin", FLEET_URL], self.repo)

    def env(self, **extra: str) -> dict[str, str]:
        base = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:{os.path.dirname(REAL_GIT)}:/usr/bin:/bin",
            "TMPDIR": str(self.home / "tmp"),
            "AGENT_SEAT": "CLAUDE",
            "GIT_CONFIG_NOSYSTEM": "1",
            "FLEET_APPS_JSON": str(self.home / "no-registry.json"),
            "FLEET_LANES_EXTERNAL_ROOT": "",
        }
        base.update(extra)
        return base

    def real(self, args: list[str], cwd: Path) -> subprocess.CompletedProcess:
        return subprocess.run([REAL_GIT, *args], cwd=cwd, check=True, capture_output=True, text=True,
                              env={"HOME": str(self.home), "PATH": os.environ["PATH"], "GIT_CONFIG_NOSYSTEM": "1"})

    def git(self, *args: str, cwd: Path | None = None, **env: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.bin / "git"), *args], cwd=cwd or self.repo, capture_output=True, text=True,
                              env=self.env(**env), timeout=30)

    def set_mode(self, text: str) -> None:
        self.mode_file.write_text(text + "\n", encoding="utf-8")

    def events(self) -> list[dict]:
        path = self.state / "lane-guard-events.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class Passthrough(ShimCase):
    def test_ordinary_commands_keep_argv_output_and_exit_code(self) -> None:
        self.assertEqual(self.git("rev-parse", "--is-inside-work-tree").stdout.strip(), "true")
        self.assertEqual(self.git("log", "-1", "--format=%s").stdout.strip(), "init")
        self.assertEqual(self.git("log", "--format=a b  c%x20", "-1").stdout, "a b  c \n")
        self.assertNotEqual(self.git("no-such-subcommand-zz").returncode, 0)
        res = self.git("rev-parse", "--verify", "refs/nope")
        self.assertEqual(res.returncode, subprocess.run([REAL_GIT, "rev-parse", "--verify", "refs/nope"], cwd=self.repo, capture_output=True).returncode)

    def test_arguments_with_spaces_and_globs_arrive_intact(self) -> None:
        out = self.git("-c", "core.abbrev=12", "rev-parse", "--sq-quote", "a b", "*", "$HOME", '"q"', "").stdout
        self.assertEqual(out.strip(), self.real(["rev-parse", "--sq-quote", "a b", "*", "$HOME", '"q"', ""], self.repo).stdout.strip())

    def test_version_and_bare_git(self) -> None:
        self.assertIn("git version", self.git("--version").stdout)
        self.assertIn("usage", (self.git().stdout + self.git().stderr).lower())

    def test_stdin_passes_through(self) -> None:
        p = subprocess.run([str(self.bin / "git"), "hash-object", "--stdin"], input="hello\n", capture_output=True, text=True,
                           cwd=self.repo, env=self.env())
        self.assertEqual(p.stdout.strip(), "ce013625030ba8dba906f756967f9e9ca394464a")

    def test_no_python_on_the_fast_path(self) -> None:
        # A broken interpreter choice shows up as nothing at all when Python is never started.
        out = self.git("status", "--short", FLEET_GIT_SHIM_PYTHON="/nonexistent")
        self.assertEqual(out.returncode, 0)
        self.assertEqual(self.events(), [])


class Decisions(ShimCase):
    def test_detect_logs_and_lets_it_through(self) -> None:
        dest = self.home / "elsewhere-tree"
        res = self.git("worktree", "add", "-q", str(dest))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(dest.is_dir())
        self.assertEqual(res.stderr.strip().count("Blocked"), 0)
        ev = self.events()
        self.assertEqual([(e["action"], e["class"]) for e in ev], [("detect", "home")])
        self.assertEqual(ev[0]["seat"], "CLAUDE")

    def test_block_stops_before_anything_exists(self) -> None:
        self.set_mode("block home")
        dest = self.home / "elsewhere-tree"
        res = self.git("worktree", "add", "-q", str(dest))
        self.assertEqual(res.returncode, 128)
        self.assertFalse(dest.exists())
        self.assertIn("Blocked", res.stderr)
        self.assertIn("lane", res.stderr)
        self.assertEqual(self.events()[0]["action"], "block")

    def test_block_of_one_class_leaves_the_others_in_detect(self) -> None:
        self.set_mode("block temp")
        dest = self.home / "apps" / "flat-lane"
        self.assertEqual(self.git("worktree", "add", "-q", str(dest)).returncode, 0)
        self.assertEqual(self.events()[0]["action"], "detect")

    def test_a_lane_is_allowed_in_block_with_no_log(self) -> None:
        self.set_mode("block")
        (self.lanes / "BotFleet").mkdir()
        dest = self.lanes / "BotFleet" / "claude-x"
        res = self.git("worktree", "add", "-q", "-b", "claude/x", str(dest))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.events(), [])

    def test_a_third_party_clone_passes_in_block(self) -> None:
        self.set_mode("block")
        source = self.home / "other-src"
        source.mkdir()
        self.real(["init", "-q", "-b", "main"], source)
        res = self.git("clone", "-q", "https://github.invalid/someone/else.git", str(self.home / "else"),
                       GIT_TERMINAL_PROMPT="0", GIT_ALLOW_PROTOCOL="file")
        self.assertNotIn("Blocked", res.stderr)

    def test_a_fleet_clone_is_blocked_with_no_network(self) -> None:
        self.set_mode("block")
        res = self.git("clone", FLEET_URL, str(self.home / "tmp-x"))
        self.assertEqual(res.returncode, 128)
        self.assertIn("Blocked", res.stderr)
        self.assertFalse((self.home / "tmp-x").exists())

    def test_minus_capital_c_is_honoured(self) -> None:
        self.set_mode("block")
        dest = self.home / "c-tree"
        res = self.git("-C", str(self.repo), "worktree", "add", "-q", str(dest), cwd=self.home)
        self.assertEqual(res.returncode, 128)
        self.assertFalse(dest.exists())

    def test_minus_capital_c_relative_destination(self) -> None:
        self.set_mode("block")
        res = self.git("-C", str(self.repo), "worktree", "add", "-q", "../../rel-tree", cwd=self.home)
        self.assertEqual(res.returncode, 128)
        self.assertFalse((self.home / "rel-tree").exists())

    def test_git_dir_is_honoured(self) -> None:
        self.set_mode("block")
        dest = self.home / "gd-tree"
        res = self.git("--git-dir", str(self.repo / ".git"), "worktree", "add", "-q", str(dest), cwd=self.home)
        self.assertEqual(res.returncode, 128)
        self.assertFalse(dest.exists())
        res = self.git(f"--git-dir={self.repo / '.git'}", "worktree", "add", "-q", str(dest), cwd=self.home)
        self.assertEqual(res.returncode, 128)

    def test_worktree_list_and_remove_are_not_judged(self) -> None:
        self.set_mode("block")
        self.assertEqual(self.git("worktree", "list").returncode, 0)
        self.assertEqual(self.git("worktree", "prune").returncode, 0)
        self.assertEqual(self.events(), [])


class Aliases(ShimCase):
    def test_inline_alias_is_expanded(self) -> None:
        self.set_mode("block")
        dest = self.home / "alias-tree"
        res = self.git("-c", "alias.w=worktree", "w", "add", "-q", str(dest))
        self.assertEqual(res.returncode, 128)
        self.assertFalse(dest.exists())

    def test_configured_alias_is_expanded(self) -> None:
        self.set_mode("block")
        self.real(["config", "alias.wt", "worktree"], self.repo)
        dest = self.home / "alias2-tree"
        self.assertEqual(self.git("wt", "add", "-q", str(dest)).returncode, 128)
        self.assertFalse(dest.exists())

    def test_alias_chain_is_followed(self) -> None:
        self.set_mode("block")
        self.real(["config", "alias.a1", "a2 add"], self.repo)
        self.real(["config", "alias.a2", "worktree"], self.repo)
        dest = self.home / "chain-tree"
        self.assertEqual(self.git("a1", "-q", str(dest)).returncode, 128)
        self.assertFalse(dest.exists())

    def test_an_alias_loop_does_not_hang(self) -> None:
        self.real(["config", "alias.l1", "l2"], self.repo)
        self.real(["config", "alias.l2", "l1"], self.repo)
        self.assertNotEqual(self.git("l1").returncode, 77)

    def test_a_shell_alias_is_read_as_a_command(self) -> None:
        """Git puts its exec path first on PATH for a `!` alias, so the `git` inside it never meets the shim."""
        self.set_mode("block")
        dest = self.home / "bang-tree"
        self.real(["config", "alias.bang", f"!git worktree add -q {dest}"], self.repo)
        res = self.git("bang")
        self.assertNotEqual(res.returncode, 0)
        self.assertFalse(dest.exists())

    def test_a_harmless_alias_runs(self) -> None:
        self.real(["config", "alias.st", "status --short"], self.repo)
        self.assertEqual(self.git("st").returncode, 0)

    def test_an_unknown_word_with_no_alias_is_gits_error(self) -> None:
        res = self.git("zzzz")
        self.assertIn("not a git command", res.stderr)


class Modes(ShimCase):
    def test_off_mode_file(self) -> None:
        self.set_mode("off")
        self.assertEqual(self.git("worktree", "add", "-q", str(self.home / "t1")).returncode, 0)
        self.assertEqual(self.events(), [])

    def test_kill_switch(self) -> None:
        self.set_mode("block")
        self.assertEqual(self.git("worktree", "add", "-q", str(self.home / "t2"), FLEET_LANE_GUARD="off").returncode, 0)

    def test_default_is_detect(self) -> None:
        self.assertFalse(self.mode_file.exists())
        self.assertEqual(self.git("worktree", "add", "-q", str(self.home / "t3")).returncode, 0)
        self.assertEqual(self.events()[0]["action"], "detect")


class FailOpen(ShimCase):
    def test_no_decision_module_runs_git_and_logs(self) -> None:
        """The branch a missing Python takes:  a copy of the shim with no module beside it."""
        self.set_mode("block")
        self.state.mkdir(parents=True)
        no_shim_path = f"{os.path.dirname(REAL_GIT)}:/usr/bin:/bin"
        fake = self.home / "fakebin"
        (fake / "shims").mkdir(parents=True)
        shutil.copy(SHIM, fake / "shims" / "git")
        res = subprocess.run([str(fake / "shims" / "git"), "worktree", "add", "-q", str(self.home / "t4")],
                             cwd=self.repo, capture_output=True, text=True, env=self.env(PATH=no_shim_path))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue((self.home / "t4").is_dir())
        self.assertIn("fail-open", (self.state / "lane-guard-events.jsonl").read_text(encoding="utf-8"))

    def test_a_crashing_decision_module_runs_git(self) -> None:
        self.set_mode("block")
        fake = self.home / "fakepkg"
        (fake / "shims").mkdir(parents=True)
        shutil.copy(SHIM, fake / "shims" / "git")
        (fake / "git_shim.py").write_text("raise SystemExit(1)\n", encoding="utf-8")
        res = subprocess.run([str(fake / "shims" / "git"), "worktree", "add", "-q", str(self.home / "t5")],
                             cwd=self.repo, capture_output=True, text=True,
                             env=self.env(PATH=f"{os.path.dirname(REAL_GIT)}:/usr/bin:/bin"))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue((self.home / "t5").is_dir())

    def test_a_garbage_registry_never_stops_git(self) -> None:
        self.set_mode("block")
        bad = self.home / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        self.assertEqual(self.git("status", FLEET_APPS_JSON=str(bad)).returncode, 0)
        res = self.git("worktree", "list", FLEET_APPS_JSON=str(bad))
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_no_real_git_on_path_uses_a_fixed_one(self) -> None:
        res = subprocess.run([str(self.bin / "git"), "--version"], capture_output=True, text=True,
                             env={**self.env(), "PATH": str(self.bin)})
        self.assertIn("git version", res.stdout)


class Recursion(ShimCase):
    def test_a_shim_linked_twice_on_path_finds_real_git(self) -> None:
        other = self.home / "bin2"
        other.mkdir()
        os.symlink(SHIM, other / "git")
        res = self.git("--version", PATH=f"{self.bin}:{other}:{os.path.dirname(REAL_GIT)}:/usr/bin:/bin")
        self.assertIn("git version", res.stdout)

    def test_two_copies_on_path_cannot_loop(self) -> None:
        one, two = self.home / "copy1", self.home / "copy2"
        for d in (one, two):
            d.mkdir()
            shutil.copy(SHIM, d / "git")
        res = subprocess.run([str(one / "git"), "--version"], capture_output=True, text=True, timeout=20,
                             env={**self.env(), "PATH": f"{one}:{two}"})
        self.assertIn("git version", res.stdout)

    def test_a_stale_inherited_real_path_is_ignored(self) -> None:
        res = self.git("--version", FLEET_GIT_SHIM_REAL="/nonexistent/git")
        self.assertIn("git version", res.stdout)

    def test_an_inherited_real_path_pointing_at_the_shim_is_ignored(self) -> None:
        res = self.git("--version", FLEET_GIT_SHIM_REAL=str(self.bin / "git"))
        self.assertIn("git version", res.stdout)


class UnmountedLanes(ShimCase):
    def test_a_dangling_repo_symlink_does_not_read_as_misplaced(self) -> None:
        self.set_mode("block")
        os.symlink("/nonexistent/External/Lanes/BotFleet", self.lanes / "BotFleet")
        res = self.git("worktree", "add", "-q", str(self.lanes / "BotFleet" / "claude-x"))
        self.assertNotIn("Blocked", res.stderr)
        self.assertEqual(self.events(), [])

    def test_a_dangling_lanes_root_does_not_read_as_misplaced(self) -> None:
        self.set_mode("block")
        shutil.rmtree(self.lanes)
        os.symlink("/nonexistent/External/Lanes", self.lanes)
        res = self.git("worktree", "add", "-q", str(self.lanes / "BotFleet" / "claude-x"))
        self.assertNotIn("Blocked", res.stderr)
        self.assertEqual(self.events(), [])


class Exceptions(ShimCase):
    def test_an_exception_row_passes_in_block(self) -> None:
        self.set_mode("block")
        rows = self.home / "rows.json"
        rows.write_text(json.dumps({"exceptions": [{
            "id": "x", "pattern": "{apps}/svc", "class": "service", "creator": "c", "reason": "r",
            "decided_by": "CLAUDE", "decided_on": "2026-11-04", "expires": "2099-01-01", "hosts": ["mac", "server"],
        }]}), encoding="utf-8")
        res = self.git("worktree", "add", "-q", str(self.home / "apps" / "svc"), FLEET_PLACEMENT_EXCEPTIONS=str(rows))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.events(), [])


class Latency(ShimCase):
    def test_the_fast_path_adds_little(self) -> None:
        def measure(argv: list[str]) -> float:
            samples = []
            for _ in range(15):
                t = time.perf_counter()
                subprocess.run([*argv, "rev-parse", "HEAD"], cwd=self.repo, capture_output=True, env=self.env(), check=True)
                samples.append(time.perf_counter() - t)
            return statistics.median(samples)

        added = measure([str(self.bin / "git")]) - measure([REAL_GIT])
        # A wide bound for a loaded CI box.  The measured numbers are in the PR.
        self.assertLess(added, 0.05, f"the fast path added {added * 1000:.1f} ms")


if __name__ == "__main__":
    unittest.main()
