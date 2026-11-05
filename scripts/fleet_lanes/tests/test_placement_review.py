"""Regressions from the AFC-618 review of the placement guard, shim, doctor, watcher and sync job.

Each class holds what one review finding reproduced, with real git in a fake home where git is involved.  A package
manager's argv here is the one captured from the real tool on Wed, Nov 4, 2026:  npm, pnpm, yarn, pip, uv and SwiftPM.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import multiprocessing
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fleet_lanes import code_main_sync as S
from fleet_lanes import doctor as D
from fleet_lanes import git_shim as GS
from fleet_lanes import guard as G
from fleet_lanes import install_tools as IT
from fleet_lanes import lane_guard_hook as H
from fleet_lanes import layout as L
from fleet_lanes import placement as P
from fleet_lanes import placement_watch as W
from fleet_lanes.tests import fixtures_guard as F
from fleet_lanes.tests import test_code_main_sync as TS
from fleet_lanes.tests import test_doctor_placement as TD
from fleet_lanes.tests import test_git_shim as TG
from fleet_lanes.tests import test_guard_placement as TP
from fleet_lanes.tests import test_placement_watch as TW

PKG = Path(__file__).resolve().parent.parent
REPO = PKG.parent.parent
FLEET = TG.FLEET_URL
SHIPPED = P.default_exceptions_path()
NO_NET = {"GIT_ALLOW_PROTOCOL": "file", "GIT_TERMINAL_PROMPT": "0"}


class ShimReview(TG.ShimCase):
    """Base for these:  the shipped exceptions file, a fixed host, and the decision without running git."""

    def env(self, **extra: str) -> dict[str, str]:
        return super().env(**{"FLEET_HOST_KIND": "mac", **extra})

    def decide(self, *args: str, cwd: Path | None = None, **env: str) -> int:
        full = {**self.env(**env), "FLEET_GIT_SHIM_REAL": TG.REAL_GIT}
        with mock.patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": "1", "HOME": str(self.home)}):
            return GS.decide(list(args), str(cwd or self.repo), full, stderr=io.StringIO())

    def blocked(self, res: subprocess.CompletedProcess, dest: Path) -> None:
        self.assertEqual(res.returncode, 128, res.stderr)
        self.assertIn("Blocked", res.stderr)
        self.assertFalse(dest.exists())


class PackageManagers(ShimReview):
    def argvs(self) -> list[tuple[str, ...]]:
        h = self.home
        return [
            ("--no-replace-objects", "clone", FLEET, f"{h}/.npm/_cacache/tmp/git-cloneX", "--recurse-submodules", "--depth=1"),
            ("clone", FLEET, f"{h}/Library/pnpm/store/v10/tmp/_tmp_20255_abc"),
            ("clone", FLEET, f"{h}/.local/share/pnpm/store/v10/tmp/_tmp_20255_abc"),
            ("clone", FLEET, f"{h}/Library/Caches/Yarn/v6/.tmp/abc"),
            ("clone", FLEET, f"{h}/.cache/yarn/v6/.tmp/abc"),
            ("clone", "--quiet", "--bare", FLEET, f"{h}/.bun/install/cache/@G@x"),
            ("clone", "--bare", FLEET, f"{h}/.cache/uv/git-v0/db/abc"),
            ("clone", "--filter=blob:none", "--quiet", FLEET, "/var/tmp/pip-req-build-x"),
            ("clone", FLEET, "/var/tmp/_tmp_123_abc"),
            ("clone", FLEET, "/opt/homebrew/Library/Taps/simple-with-us/homebrew-tap"),
        ]

    def test_each_captured_argv_passes_in_block_and_is_recorded(self) -> None:
        self.set_mode("block")
        for argv in self.argvs():
            with self.subTest(argv=argv):
                self.assertEqual(self.decide(*argv), 0)
                event = self.events()[-1]
                self.assertEqual(event["action"], "excused")
                self.assertTrue(event["rule"])

    def test_a_pip_build_folder_in_the_real_temp_dir(self) -> None:
        c = TP.ctx("block", rows=P.load_exceptions(SHIPPED, env={}))
        verdict = G.evaluate_git_argv(["clone", "--filter=blob:none", FLEET, "/private/var/folders/0b/x/T/pip-req-build-x"], "/x", c)
        self.assertEqual(verdict[0], G.CLASS_EXCUSED)

    def test_a_cache_row_never_excuses_a_worktree_or_the_code_root(self) -> None:
        self.set_mode("block")
        self.assertEqual(self.decide("worktree", "add", f"{self.home}/.npm/_cacache/lane"), GS.BLOCK_EXIT)
        self.assertEqual(self.decide("clone", FLEET, f"{self.code}/x/pip-req-build-y"), GS.BLOCK_EXIT)
        self.assertEqual(self.decide("clone", FLEET, f"{self.home}/pip-req-build-y"), GS.BLOCK_EXIT)

    def test_a_cached_copy_with_a_fleet_origin_passes_as_a_local_clone(self) -> None:
        self.set_mode("block")
        db = self.home / ".cache/uv/git-v0/db/abc"
        db.parent.mkdir(parents=True)
        self.real(["clone", "-q", str(self.repo), str(db)], self.home)
        self.real(["remote", "set-url", "origin", FLEET], db)
        self.assertEqual(self.decide("clone", "--local", str(db), f"{self.home}/.cache/uv/git-v0/checkouts/abc/1"), 0)


class SwiftPackages(ShimReview):
    def test_the_mirror_and_the_local_checkout_pass(self) -> None:
        self.set_mode("block")
        cache = self.home / "Library/Caches/org.swift.swiftpm/repositories/x-1"
        build = self.code / "BotFleet/.build"
        for argv in (
            ("clone", "-c", "core.symlinks=true", "--mirror", FLEET, str(cache), "--progress"),
            ("clone", "--mirror", str(self.repo), str(build / "repositories/x-1")),
            ("clone", "--shared", "--no-checkout", str(build / "repositories/x-1"), str(build / "checkouts/x")),
            ("clone", "--mirror", FLEET, str(self.lanes / "BotFleet/claude-x/.build/repositories/x-1")),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self.decide(*argv), 0)

    def test_a_typed_clone_or_worktree_in_a_build_folder_is_still_judged(self) -> None:
        self.set_mode("block")
        for argv in (
            ("clone", FLEET, "/var/tmp/x/.build/checkouts/BotFleet"),
            ("clone", FLEET, str(self.home / "x/SourcePackages/checkouts/y")),
            ("worktree", "add", ".build/checkouts/y"),
            ("clone", FLEET, str(self.home / "Library/Caches/org.swift.swiftpm/x")),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(self.decide(*argv), GS.BLOCK_EXIT)


class WholeTreeRows(ShimReview):
    def test_a_personal_row_never_excuses_a_fleet_clone(self) -> None:
        self.set_mode("block")
        self.assertEqual(self.decide("clone", FLEET, f"{self.home}/Documents/lane-x"), GS.BLOCK_EXIT)

    def test_a_tool_row_excuses_a_clone_and_records_it_but_never_a_worktree(self) -> None:
        self.set_mode("block")
        self.assertEqual(self.decide("clone", FLEET, f"{self.home}/.claude/plugins/lane-x"), 0)
        self.assertEqual(self.events()[-1]["action"], "excused")
        self.assertEqual(self.decide("worktree", "add", f"{self.home}/.codex/memories/lane"), GS.BLOCK_EXIT)

    def test_a_service_row_passes_and_records(self) -> None:
        self.set_mode("block")
        self.assertEqual(self.decide("clone", FLEET, f"{self.home}/.botfleet/workspaces/mine"), 0)
        self.assertEqual(self.events()[-1]["action"], "excused")


class HookRows(unittest.TestCase):
    """An agent's own tool call is never a tool cache filling itself, so only a service or backup row excuses it."""

    def rows(self) -> tuple:
        return P.load_exceptions(SHIPPED, env={})

    def test_the_hook_denies_what_the_cache_rows_cover(self) -> None:
        c = TP.ctx("block", rows=self.rows())
        for command in (
            f"git clone {FLEET} ~/Documents/lane-x",
            f"git clone {FLEET} ~/.claude/plugins/lane-x",
            f"git clone {FLEET} ~/Library/Caches/org.swift.swiftpm/x",
            f"git clone {FLEET} /tmp/x/.build/checkouts/BotFleet",
            f"git clone {FLEET} ~/x/SourcePackages/checkouts/y",
            f"git clone {FLEET} ~/.npm/_cacache/tmp/x",
            "git worktree add .build/checkouts/y",
            "git worktree add ~/.codex/memories/lane",
        ):
            with self.subTest(command=command):
                self.assertEqual(G.evaluate(TP.payload(command), c).action, G.DENY)

    def test_the_hook_records_what_a_service_row_excuses(self) -> None:
        d = G.evaluate(TP.payload(f"git clone {FLEET} ~/.botfleet/workspaces/mine"), TP.ctx("detect", rows=self.rows()))
        self.assertEqual(d.action, G.ALLOW)
        self.assertEqual(d.detected, "")
        self.assertEqual((d.detected_detail["action"], d.detected_detail["rule"]), ("excused", "service-botfleet-workspaces-mac"))

    def test_the_hook_keeps_a_service_row(self) -> None:
        c = TP.ctx("block", rows=self.rows())
        self.assertEqual(G.evaluate(TP.payload(f"git clone {FLEET} ~/.botfleet/workspaces/mine"), c).action, G.ALLOW)
        self.assertEqual(G.evaluate(TP.payload(f"git clone {FLEET} ~/apps/botfleet-server"), c).action, G.ALLOW)


class ShimRoots(ShimReview):
    """The Lane Map's order:  the environment, the home folder, then the volume in FLEET_SERVER_VOLUME."""

    def setUp(self) -> None:
        super().setUp()
        self.vol = self.home / "vol"
        (self.vol / "Code").mkdir(parents=True)
        (self.vol / "apps" / "lanes").mkdir(parents=True)
        shutil.move(str(self.repo), str(self.vol / "Code" / "BotFleet"))
        self.repo = self.vol / "Code" / "BotFleet"
        shutil.rmtree(self.home / "Code")
        shutil.rmtree(self.home / "apps")

    def test_a_shell_with_only_the_volume_set_judges_a_repo_on_the_volume(self) -> None:
        self.set_mode("block")
        dest = self.home / "wrongplace"
        self.blocked(self.git("worktree", "add", "-q", str(dest), FLEET_SERVER_VOLUME=str(self.vol)), dest)

    def test_a_lane_on_the_volume_passes(self) -> None:
        self.set_mode("block")
        dest = self.vol / "apps" / "lanes" / "BotFleet" / "claude-x"
        res = self.git("worktree", "add", "-q", "-b", "claude/x", str(dest), FLEET_SERVER_VOLUME=str(self.vol))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.events(), [])

    def test_a_root_set_in_the_environment_wins(self) -> None:
        env = GS.with_fleet_roots({"HOME": str(self.home), "FLEET_SERVER_VOLUME": str(self.vol), "FLEET_CODE_ROOT": "/other"})
        self.assertEqual(env["FLEET_CODE_ROOT"], "/other")
        self.assertEqual(env["FLEET_LANES_ROOT"], str(self.vol / "apps" / "lanes"))


class InstalledCopy(unittest.TestCase):
    """The stable copy in <apps>/lane-tools, where no fleet-apps.json sits two levels above the module."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(os.path.realpath(self.tmp.name)) / "home"
        self.stable = self.home / "apps" / "lane-tools"
        self.stable.mkdir(parents=True)
        shutil.copytree(PKG, self.stable / "fleet_lanes", ignore=shutil.ignore_patterns("tests", "__pycache__"))
        shutil.copy(REPO / "fleet-apps.json", self.stable / "fleet-apps.json")
        (self.stable / "shims").mkdir()
        shutil.copy(PKG / "shims" / "git", self.stable / "shims" / "git")
        (self.home / "Code").mkdir()
        self.env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "AGENT_SEAT": "CLAUDE", "TMPDIR": str(self.home / "tmp"),
                    "FLEET_HOST_KIND": "mac", "FLEET_LANES_EXTERNAL_ROOT": ""}

    def test_the_registry_beside_the_package_is_found(self) -> None:
        out = subprocess.run(
            ["/usr/bin/env", "python3", "-I", "-c",
             "import sys; sys.path.insert(0, sys.argv[1]); from fleet_lanes import layout; print(layout.default_registry_path({}))",
             str(self.stable)], capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(os.path.realpath(out), os.path.realpath(self.stable / "fleet-apps.json"))

    def test_the_exact_integration_clone_passes_under_block_code(self) -> None:
        mode = self.home / ".config/fleet/lane-guard-mode"
        mode.parent.mkdir(parents=True)
        mode.write_text("block code\n", encoding="utf-8")
        res = subprocess.run([str(self.stable / "shims/git"), "clone", "-q", "--depth=1",
                              "https://github.com/Simple-With-Us/Congress.Trade.git", str(self.home / "Code/Congress.Trade")],
                             cwd=self.home, capture_output=True, text=True, env={**self.env, **NO_NET})
        self.assertNotIn("Blocked", res.stderr)

    def test_the_watcher_runs_with_no_registry_variable(self) -> None:
        res = subprocess.run(["/usr/bin/env", "python3", "-I", str(self.stable / "fleet_lanes/placement_watch.py"), "--seat", "CLAUDE"],
                             cwd=self.home, capture_output=True, text=True, env=self.env, timeout=120)
        self.assertNotIn("Traceback", res.stderr)
        self.assertEqual(res.returncode, W.EXIT_OK, res.stderr)


class FastPath(ShimReview):
    def words(self) -> set[str]:
        text = (PKG / "shims" / "git").read_text(encoding="utf-8")
        block = text.split("# A builtin cannot be aliased", 1)[1].split('_fleet_exec "$_fleet_real"', 1)[0]
        block = "\n".join(line for line in block.splitlines()[1:] if not line.strip().startswith("#"))
        return {w for w in re.split(r"[\s|)\\]+", block) if w}

    def test_every_word_on_the_fast_path_is_a_builtin_of_this_git(self) -> None:
        builtins = set(subprocess.run([TG.REAL_GIT, "--list-cmds=builtins"], capture_output=True, text=True, check=True).stdout.split())
        self.assertEqual(self.words() - builtins, set())

    def test_an_alias_named_like_an_external_command_is_followed(self) -> None:
        self.set_mode("block")
        for word in ("ls", "gui", "gitk", "lfs", "submodule"):
            with self.subTest(word=word):
                dest = self.home / f"a_{word}"
                self.real(["config", f"alias.{word}", f"worktree add -q {dest}"], self.repo)
                self.blocked(self.git(word), dest)

    def test_a_shell_alias_that_runs_git_reaches_python(self) -> None:
        self.set_mode("block")
        dest = self.home / "viafunc"
        self.real(["config", "alias.sh1", f"!f() {{ git worktree add -q {dest}; }}; f"], self.repo)
        self.blocked(self.git("sh1"), dest)


class AliasWalk(ShimReview):
    def setUp(self) -> None:
        super().setUp()
        self.set_mode("block")
        self.real(["config", "alias.wt", "worktree"], self.repo)
        self.dest = self.home / "walked"

    def test_a_tab_after_the_word(self) -> None:
        self.real(["config", "alias.x2", "wt\tadd"], self.repo)
        self.blocked(self.git("x2", "-q", str(self.dest)), self.dest)

    def test_a_quoted_word(self) -> None:
        self.real(["config", "alias.x3", "'wt' add"], self.repo)
        self.blocked(self.git("x3", "-q", str(self.dest)), self.dest)

    def test_a_long_chain(self) -> None:
        for one, two in (("c1", "c2"), ("c2", "c3"), ("c3", "c4"), ("c4", "c5"), ("c5", "c6"), ("c6", "c7"), ("c7", "c8"),
                         ("c8", "c9"), ("c9", "c10"), ("c10", "wt")):
            self.real(["config", f"alias.{one}", two], self.repo)
        self.blocked(self.git("c1", "add", "-q", str(self.dest)), self.dest)

    def test_an_alias_from_an_inline_include(self) -> None:
        inc = self.home / "inc.cfg"
        inc.write_text("[alias]\n\tzz = worktree\n", encoding="utf-8")
        self.blocked(self.git("-c", f"include.path={inc}", "zz", "add", "-q", str(self.dest)), self.dest)

    def test_shallow_file_takes_a_value(self) -> None:
        self.blocked(self.git("--shallow-file", "", "worktree", "add", "-q", str(self.dest)), self.dest)
        self.blocked(self.git("--shallow-file", "", "clone", "-q", str(self.repo), str(self.dest)), self.dest)

    def test_a_config_git_cannot_read_still_reaches_git_error(self) -> None:
        bad = self.home / "bad"
        bad.mkdir()
        self.real(["init", "-q"], bad)
        (bad / ".git" / "config").write_text("[broken\n", encoding="utf-8")
        res = self.git("zzword", cwd=bad)
        self.assertNotEqual(res.returncode, 0)
        self.assertNotIn("Traceback", res.stderr)


class ShellAliasArguments(ShimReview):
    def test_a_function_alias_with_its_argument(self) -> None:
        self.set_mode("block")
        dest = self.home / "arg1"
        self.real(["config", "alias.mk", '!f() { git worktree add -q "$1"; }; f'], self.repo)
        self.blocked(self.git("mk", str(dest)), dest)

    def test_a_plain_alias_with_its_argument(self) -> None:
        self.set_mode("block")
        dest = self.home / "arg2"
        self.real(["config", "alias.mk2", '!git worktree add -q "$1" #'], self.repo)
        self.blocked(self.git("mk2", str(dest)), dest)

    def test_a_harmless_shell_alias_runs(self) -> None:
        self.set_mode("block")
        self.real(["config", "alias.hi", "!echo hi"], self.repo)
        self.assertEqual(self.git("hi").stdout.strip(), "hi")


class GitDirInEnvironment(ShimReview):
    def test_a_fleet_repo_named_only_by_the_variable_is_judged(self) -> None:
        self.set_mode("block")
        away = self.home / "away"
        away.mkdir()
        dest = away / "e1"
        self.blocked(self.git("worktree", "add", "-q", str(dest), cwd=away, GIT_DIR=str(self.repo / ".git")), dest)

    def test_the_variable_beats_the_folder_the_command_runs_in(self) -> None:
        self.set_mode("block")
        other = self.home / "thirdparty"
        other.mkdir()
        self.real(["init", "-q", "-b", "main"], other)
        self.real(["-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x"], other)
        dest = self.home / "tpw"
        res = self.git("worktree", "add", "-q", str(dest), GIT_DIR=str(other / ".git"))
        self.assertNotIn("Blocked", res.stderr)


class OptionParsing(ShimReview):
    def test_clustered_and_abbreviated_clone_options(self) -> None:
        self.set_mode("block")
        for argv in (("-qb", "main"), ("-nb", "main", "-q"), ("--branc", "main", "-q"), ("-qbmain",), ("--dep", "1", "-q")):
            dest = self.home / f"opt{abs(hash(argv))}"
            with self.subTest(argv=argv):
                self.blocked(self.git("clone", *argv, str(self.repo), str(dest), cwd=self.home), dest)

    def test_an_abbreviated_mirror_counts_as_a_mirror(self) -> None:
        self.set_mode("block")
        dest = self.home / "Library/Caches/org.swift.swiftpm/repositories/y"
        self.assertEqual(self.decide("clone", "--mir", FLEET, str(dest)), 0)

    def test_abbreviated_and_clustered_worktree_options_find_the_destination(self) -> None:
        self.set_mode("block")
        lane = self.lanes / "BotFleet" / "claude-y"
        self.assertEqual(self.decide("worktree", "add", "-q", "--lock", "--reas", "x", str(lane)), 0)
        self.assertEqual(self.decide("worktree", "add", "-qb", "claude/y", str(lane)), 0)
        self.assertEqual(self.decide("worktree", "add", "-q", "--lock", "--reas", "x", str(self.home / "w")), GS.BLOCK_EXIT)


class FleetIdentity(ShimReview):
    def test_a_misplaced_fleet_checkout_is_judged_by_its_origin(self) -> None:
        self.set_mode("block")
        stray = self.home / "misplaced"
        self.real(["clone", "-q", str(self.repo), str(stray)], self.home)
        self.real(["remote", "set-url", "origin", FLEET], stray)
        dest = self.home / "from-stray"
        self.blocked(self.git("worktree", "add", "-q", str(dest), cwd=stray), dest)
        dest2 = self.home / "cl-stray"
        self.blocked(self.git("clone", "-q", str(stray), str(dest2), cwd=self.home), dest2)

    def test_a_repo_under_a_root_with_no_fleet_origin_is_not_fleet(self) -> None:
        self.set_mode("block")
        for parent, name in ((self.home / "apps", "vendor"), (self.code, "personal")):
            repo = parent / name
            repo.mkdir(parents=True)
            self.real(["init", "-q", "-b", "main"], repo)
            self.real(["-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x"], repo)
            dest = self.home / f"{name}-wt"
            with self.subTest(repo=name):
                res = self.git("worktree", "add", "-q", str(dest), cwd=repo)
                self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.events(), [])

    def test_a_symlink_in_the_lanes_root_is_judged_where_it_lands(self) -> None:
        self.set_mode("block")
        outside = self.home / "outside"
        outside.mkdir()
        (self.lanes / "BotFleet").mkdir()
        os.symlink(outside, self.lanes / "BotFleet" / "foo")
        res = self.git("worktree", "add", "-q", str(self.lanes / "BotFleet" / "foo" / "y"))
        self.assertEqual(res.returncode, 128, res.stderr)
        self.assertFalse((outside / "y").exists())


class TempRulesInTheShim(ShimReview):
    def setUp(self) -> None:
        super().setUp()
        self.set_mode("block temp")
        self.out = Path(tempfile.mkdtemp(dir="/var/tmp"))
        self.addCleanup(shutil.rmtree, self.out, True)

    def test_archive_and_checkout_index(self) -> None:
        tar = self.out / "x.tar"
        self.blocked(self.git("archive", f"--output={tar}", "HEAD"), tar)
        ci = self.out / "ci"
        self.blocked(self.git("checkout-index", f"--prefix={ci}/", "-a"), ci)
        self.assertEqual([e["action"] for e in self.events()], ["block", "block"])

    def test_fetch_into_a_temp_repo_from_a_path_and_from_a_named_remote(self) -> None:
        dest = self.out / "d"
        self.real(["init", "-q", str(dest)], self.out)
        res = self.git("-C", str(dest), "fetch", "-q", str(self.repo), "main")
        self.assertEqual(res.returncode, 128, res.stderr)
        self.real(["remote", "add", "origin", FLEET], dest)
        res = self.git("fetch", "origin", cwd=dest)
        self.assertEqual(res.returncode, 128, res.stderr)
        res = self.git("remote", "add", "-f", "other", FLEET, cwd=dest)
        self.assertEqual(res.returncode, 128, res.stderr)

    def test_an_ordinary_fetch_stays_out_of_python(self) -> None:
        res = self.git("fetch", "-q", str(self.repo), "main", FLEET_GIT_SHIM_PYTHON="/nonexistent")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.events(), [])


class HarnessIsolation(ShimReview):
    def add(self, mode: str, name: str) -> subprocess.CompletedProcess:
        self.set_mode(mode)
        dest = self.repo / ".claude" / "worktrees" / name
        return self.git("worktree", "add", "-q", "-b", f"worktree-{name}", str(dest))

    def test_isolation_off_leaves_a_harness_worktree_alone_under_block_code(self) -> None:
        res = self.add("block code\nisolation off", "a")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.events(), [])

    def test_isolation_detect_logs(self) -> None:
        self.assertEqual(self.add("block code\nisolation detect", "b").returncode, 0)
        self.assertEqual([(e["action"], e["class"]) for e in self.events()], [("detect", "isolation")])

    def test_isolation_block_stops_it(self) -> None:
        self.assertEqual(self.add("detect\nisolation block", "c").returncode, 128)

    def test_another_code_destination_still_obeys_block_code(self) -> None:
        self.set_mode("block code\nisolation off")
        dest = self.repo / "nested"
        self.blocked(self.git("worktree", "add", "-q", str(dest)), dest)


class TimeLimit(ShimReview):
    def test_a_stalled_guard_leaves_a_fail_open_event(self) -> None:
        self.set_mode("block")
        fifo = self.home / "fifo"
        os.mkfifo(fifo)
        dest = self.home / "stalled"
        res = self.git("worktree", "add", "-q", str(dest), FLEET_APPS_JSON=str(fifo))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual([(e["action"], e["detail"]) for e in self.events()], [("fail-open", "time-limit")])


class ShimMain(ShimReview):
    def test_main_restores_the_alarm(self) -> None:
        before = signal.getsignal(signal.SIGALRM)
        with mock.patch.dict(os.environ, {"HOME": str(self.home), "FLEET_LANE_GUARD": "off"}):
            GS.main(["worktree", "list"])
        self.assertEqual(signal.getsignal(signal.SIGALRM), before)
        self.assertEqual(signal.alarm(0), 0)

    def test_a_closed_stderr_still_blocks_and_logs_once(self) -> None:
        self.set_mode("block")

        class Closed(io.StringIO):
            def write(self, _text: str) -> int:
                raise BrokenPipeError

        full = {**self.env(), "FLEET_GIT_SHIM_REAL": TG.REAL_GIT}
        self.assertEqual(GS.decide(["worktree", "add", str(self.home / "t")], str(self.repo), full, stderr=Closed()), GS.BLOCK_EXIT)
        self.assertEqual(len(self.events()), 1)


class HookCwd(unittest.TestCase):
    DC = "mcp__plugin_desktop-commander_desktop-commander__start_process"

    def test_a_tool_with_no_cwd_input_does_not_use_the_session_folder(self) -> None:
        c = TP.ctx("detect")
        for command in (f"git clone {FLEET} x", "git worktree add ../y"):
            with self.subTest(command=command):
                d = G.evaluate(TP.payload(command, cwd=f"{TP.HOME}/Code/HogHunter", tool=self.DC), c)
                self.assertEqual(d.action, G.ALLOW)
        d = G.evaluate(TP.payload(f"git clone {FLEET} {TP.HOME}/Code/HogHunter/x", tool=self.DC), c)
        self.assertEqual(d.action, G.DENY)

    def test_a_terminal_with_its_own_cwd_is_judged_there(self) -> None:
        c = TP.ctx("detect")
        call = {"tool_name": "mcp__terminal__run_in_terminal", "cwd": f"{TP.HOME}/apps",
                "tool_input": {"command": f"git clone {FLEET} x", "cwd": f"{TP.HOME}/Code/HogHunter"}}
        self.assertEqual(G.evaluate(call, c).action, G.DENY)
        call = {"tool_name": "mcp__terminal__run_in_terminal", "cwd": f"{TP.HOME}/Code/HogHunter",
                "tool_input": {"command": f"git clone {FLEET} x"}}
        self.assertEqual(G.evaluate(call, c).action, G.ALLOW)


class LocationIsNotFleet(unittest.TestCase):
    def test_a_repo_with_no_known_app_or_owner_is_not_placed(self) -> None:
        c = TP.ctx("block")
        self.assertEqual(G.evaluate(TP.payload("git worktree add ../vendor-wt", cwd=f"{TP.HOME}/apps/vendor"), c).action, G.ALLOW)
        self.assertEqual(G.evaluate(TP.payload("git worktree add ~/wt", cwd=f"{TP.HOME}/Code/nobody-registered"), c).action, G.ALLOW)
        self.assertEqual(G.evaluate(TP.payload("git worktree add ~/wt", cwd=f"{TP.HOME}/Code/BotFleet"), c).action, G.DENY)


class IsolationRequests(unittest.TestCase):
    def ask(self, tool: str, ti: dict, cwd: str) -> G.Decision:
        return G.evaluate({"tool_name": tool, "tool_input": ti, "cwd": cwd}, TP.ctx("detect\nisolation block"))

    def test_entering_an_existing_worktree_creates_nothing(self) -> None:
        d = self.ask("EnterWorktree", {"path": f"{TP.LANES}/BotFleet/claude-x"}, f"{TP.HOME}/Code/BotFleet")
        self.assertEqual(d.action, G.ALLOW)
        self.assertEqual(self.ask("EnterWorktree", {}, f"{TP.HOME}/Code/BotFleet").action, G.DENY)

    def test_only_a_registered_integration_tree_counts(self) -> None:
        d = self.ask("Agent", {"isolation": "worktree"}, f"{TP.HOME}/Code/personal-notes")
        self.assertEqual(d.action, G.ALLOW)
        self.assertEqual(self.ask("Agent", {"isolation": "worktree"}, f"{TP.HOME}/Code/BotFleet").action, G.DENY)


class DenyText(unittest.TestCase):
    def test_external_disk_spelling_of_a_lanes_row(self) -> None:
        row = P.parse_rows({"exceptions": [TD_ROW("{lanes}/Kodus-Config")]})
        c = TP.ctx("block", rows=row, env={"FLEET_LANES_EXTERNAL_ROOT": "/Volumes/External/Lanes"})
        url = "https://github.com/Simple-With-Us/Kodus-Config"
        for dest in (f"{TP.LANES}/Kodus-Config/claude-x", "/Volumes/External/Lanes/Kodus-Config/claude-x"):
            with self.subTest(dest=dest):
                self.assertEqual(G.evaluate(TP.payload(f"git clone {url} {dest}"), c).action, G.ALLOW)

    def test_a_repo_with_no_integration_tree_is_not_told_to_clone_where_it_was_refused(self) -> None:
        c = TP.ctx("block")
        d = G.evaluate(TP.payload(f"git clone https://github.com/Simple-With-Us/upptime-status {TP.LANES}/upptime-status/claude-x"), c)
        self.assertEqual(d.action, G.DENY)
        self.assertNotIn("claude-claude", d.reason)
        self.assertNotIn("Create it under the lanes root instead", d.reason)
        self.assertIn("worktree add", d.reason)

    def test_an_unknown_underscore_folder_is_not_a_tool_root(self) -> None:
        c = TP.ctx("block")
        self.assertEqual(G.evaluate(TP.payload(f"git clone {FLEET} ~/apps/lanes/_scratch/x"), c).action, G.DENY)
        self.assertEqual(G.evaluate(TP.payload("git worktree add ~/apps/lanes/_scratch/y"), c).action, G.DENY)
        for root in ("_codex", "_managed", "_review", "_conductor"):
            with self.subTest(root=root):
                self.assertEqual(G.evaluate(TP.payload(f"git clone {FLEET} ~/apps/lanes/{root}/x"), c).action, G.ALLOW)

    def test_cursor_is_told_the_rule_that_fired(self) -> None:
        def user(reason: str) -> str:
            return json.loads(H.render(reason, "cursor", "~/x"))["user_message"]

        self.assertIn("outside the lanes folder", user("Blocked: fleet-repo checkouts are not allowed outside the lanes folder, and"))
        self.assertIn("full clone", user("Blocked: fleet-repo checkouts are not allowed as full clones in a lane folder, because"))
        self.assertIn("harness worktree", user("Blocked: this would create a harness worktree inside the integration tree /x"))
        self.assertIn("integration tree", user("Blocked: fleet-repo checkouts are not allowed inside the ~/Code integration tree, and"))
        self.assertIn("temp directory", user("Blocked: fleet-repo checkouts are not allowed in temp directories, and"))


def TD_ROW(pattern: str, **kw) -> dict:
    base = {"id": "r", "pattern": pattern, "class": "service", "creator": "x", "reason": "because", "decided_by": "CLAUDE",
            "decided_on": "2026-11-04", "expires": "2099-01-01", "hosts": ["mac", "server"]}
    base.update(kw)
    return base


class ExceptionFile(unittest.TestCase):
    def test_catch_all_patterns_are_invalid_and_never_load(self) -> None:
        for pattern in ("**/", "**", "{home}", "{home}/", "{code}", "{lanes}/*", "{apps}/**", "**/*"):
            with self.subTest(pattern=pattern):
                data = {"exceptions": [TD_ROW(pattern)]}
                self.assertTrue(P.validate_rows(data))
                self.assertEqual(P.parse_rows(data), ())
        self.assertEqual(P.validate_rows({"exceptions": [TD_ROW("{home}/.npm/_cacache")]}), [])
        self.assertEqual(len(P.parse_rows({"exceptions": [TD_ROW("**/.build/checkouts")]})), 1)

    def test_the_shipped_rows_do_not_all_lapse_on_one_day(self) -> None:
        rows = json.loads(Path(SHIPPED).read_text(encoding="utf-8"))["exceptions"]
        days = [r["expires"] for r in rows]
        self.assertGreaterEqual(len(set(days)), 5)
        self.assertLessEqual(max(days.count(d) for d in set(days)), len(days) // 3)

    def test_expiring_rows_come_with_a_lead_time(self) -> None:
        rows = P.parse_rows({"exceptions": [TD_ROW("{home}/a", id="soon", expires="2026-11-20"),
                                            TD_ROW("{home}/b", id="later", expires="2027-06-01"),
                                            TD_ROW("{home}/c", id="gone", expires="2026-11-01")]})
        got = P.expiring_rows(rows, host="mac", today=dt.date(2026, 11, 4))
        self.assertEqual([r.id for r in got], ["soon"])

    def test_the_strict_loader_refuses_what_the_tolerant_one_drops(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "rows.json")
            with self.assertRaises(P.ExceptionsUnusable):
                P.load_exceptions_strict(path, env={})
            Path(path).write_text('{"exceptions": [ truncated', encoding="utf-8")
            with self.assertRaises(P.ExceptionsUnusable):
                P.load_exceptions_strict(path, env={})
            row = TD_ROW("{code}/svc")
            del row["creator"]
            Path(path).write_text(json.dumps({"exceptions": [row]}), encoding="utf-8")
            with self.assertRaises(P.ExceptionsUnusable):
                P.load_exceptions_strict(path, env={})
            self.assertEqual(P.load_exceptions(path, env={}), ())
            Path(path).write_text(json.dumps({"exceptions": [TD_ROW("{code}/svc")]}), encoding="utf-8")
            self.assertEqual(len(P.load_exceptions_strict(path, env={})), 1)

    def test_the_shipped_file_is_valid(self) -> None:
        self.assertEqual(P.validate_rows(json.loads(Path(SHIPPED).read_text(encoding="utf-8"))), [])
        self.assertTrue(P.load_exceptions_strict(SHIPPED, env={}))


def _writer(barrier, home: str, env: dict) -> None:
    barrier.wait()
    P.log_event(home, {"via": "t", "action": "x"}, env)


class Rotation(unittest.TestCase):
    def test_concurrent_writers_never_destroy_the_rotated_log(self) -> None:
        ctx = multiprocessing.get_context("fork")
        with tempfile.TemporaryDirectory() as tmp:
            env = {P.ENV_STATE_DIR: tmp}
            path = P.event_log_path(tmp, env)
            for _ in range(40):
                with open(path, "wb") as fh:
                    fh.write(b"x" * (P.EVENT_LOG_MAX_BYTES + 1))
                with contextlib.suppress(OSError):
                    os.unlink(path + ".1")
                barrier = ctx.Barrier(8)
                procs = [ctx.Process(target=_writer, args=(barrier, tmp, env)) for _ in range(8)]
                for p in procs:
                    p.start()
                for p in procs:
                    p.join()
                self.assertGreater(os.path.getsize(path + ".1"), P.EVENT_LOG_MAX_BYTES)
                with open(path, encoding="utf-8") as fh:
                    self.assertEqual(sum(1 for _ in fh), 8)


class ClaudeMatcherNote(unittest.TestCase):
    def merged(self, matcher: str, extra: bool) -> tuple[dict, str, list[str]]:
        plat = IT.PLATFORMS["claude"]
        cmd = "/x/lane-guard-hook run"
        hooks = [{"type": "command", "command": cmd}]
        if extra:
            hooks.append({"type": "command", "command": "other"})
        doc = {"hooks": {"PreToolUse": [{"matcher": matcher, "hooks": hooks}]}}
        return IT.merge_document(doc, plat, cmd)

    def test_a_group_left_narrow_says_so(self) -> None:
        for matcher, extra in (("Bash", True), ("Bash|Edit", False)):
            with self.subTest(matcher=matcher):
                new, action, notes = self.merged(matcher, extra)
                self.assertEqual(new["hooks"]["PreToolUse"][0]["matcher"], matcher)
                self.assertEqual(action, "unchanged")
                self.assertTrue(any("left" in n and "narrow" in n for n in notes), notes)

    def test_our_own_full_group_has_no_note(self) -> None:
        _, action, notes = self.merged(IT.CLAUDE_MATCHER, False)
        self.assertEqual((action, notes), ("unchanged", []))


class ReleaseBundle(unittest.TestCase):
    def test_the_exceptions_file_ships_with_the_module(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("fleet_cli_release_probe", REPO / "scripts" / "fleet-cli-release.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertTrue(mod.allowed_path("scripts/fleet_lanes/placement.py"))
        self.assertTrue(mod.allowed_path("scripts/fleet_lanes/placement-exceptions.json"))


# ---------------------------------------------------------------------------- the doctor


class DoctorRows(TD.Case):
    def test_a_third_party_clone_in_the_code_root_is_not_a_main_clone(self) -> None:
        other = self.code / "oss"
        TD.git(["clone", "-q", str(self.upstream), str(other)], self.home)
        TD.git(["checkout", "-q", "-b", "dev"], other)
        TD.git(["remote", "set-url", "origin", "https://github.com/octocat/oss.git"], other)
        self.rows()
        rep = self.report()
        self.assertNotIn("oss", [m["repo"] for m in rep["main_clones"]])
        self.assertNotIn(str(other), self.anomalies(rep, "MAIN-CLONE"))

    def _misplaced_worktree(self, rel: str) -> Path:
        dest = self.home / rel
        dest.parent.mkdir(parents=True)
        TD.git(["worktree", "add", "-q", "-b", "claude/z", str(dest)], self.repo)
        return dest

    def test_a_linked_worktree_under_a_cache_or_personal_row_is_not_excused(self) -> None:
        dest = self._misplaced_worktree(".codex/memories/lane")
        self.rows(self.row("{home}/.codex/memories", **{"class": "tool-cache"}))
        rep = self.report()
        self.assertTrue(self.co(rep, dest)["placement_violation"])

    def test_a_service_row_excuses_a_worktree(self) -> None:
        dest = self._misplaced_worktree("svc/lane")
        self.rows(self.row("{home}/svc"))
        self.assertFalse(self.co(self.report(), dest)["placement_violation"])

    def test_a_personal_row_does_not_excuse_a_clone_of_a_registered_app(self) -> None:
        registry = L.parse_registry(F.REGISTRY_DATA)
        roots = L.make_roots(str(self.home), self.vars, registry=registry)
        rows = P.parse_rows({"exceptions": [self.row("{home}/Documents", **{"class": "personal"})]})

        def verdict(owner_repo: str) -> bool:
            path = str(self.home / "Documents" / "x")
            co = D.Checkout(path=path, realpath=path, kind="FULL-CLONE", scope="fleet", owner_repo=owner_repo,
                            remote_url="u", layout_status="unsanctioned", layout_reasons=["outside-the-map"])
            D.placement_verdict(co, roots, rows, "mac", registry=registry)
            return co.placement_violation

        self.assertTrue(verdict("Simple-With-Us/BotFleet"))
        self.assertFalse(verdict("jaywedgeworth22/old-thing"))

    def test_a_row_near_its_expiry_is_a_warning_and_not_a_strict_failure(self) -> None:
        soon = (dt.date.today() + dt.timedelta(days=10)).isoformat()
        self.rows(self.row("{home}/svc", expires=soon))
        rep = self.report()
        self.assertEqual([w["type"] for w in rep["placement_warnings"]], ["EXPIRING-EXCEPTION"])
        self.assertEqual(self.anomalies(rep, "EXPIRING-EXCEPTION"), [])


# ---------------------------------------------------------------------------- the watcher


class Run(TW.FakeRun):
    def __init__(self, *, raises: str | None = None, code: int = 0, verb: str | None = None, show: str = "open") -> None:
        super().__init__()
        self.raises, self.code, self.verb, self.show = raises, code, verb, show

    def __call__(self, argv, **kw):
        if self.raises and argv[1] == self.raises:
            self.calls.append(list(argv))
            raise subprocess.TimeoutExpired(argv, 90)
        if self.code and argv[1] == self.verb:
            self.calls.append(list(argv))
            return SimpleNamespace(returncode=self.code, stdout="", stderr="refused")
        if argv[:2] == ["simple-board", "show"]:
            self.calls.append(list(argv))
            return SimpleNamespace(returncode=0, stdout=json.dumps({"status": self.show}), stderr="")
        return super().__call__(argv, **kw)


class WatchReview(TW.WatchCase):
    def p(self, name: str) -> str:
        return os.path.join(self.home, name)

    def one(self, path: str, **co) -> dict:
        return self.report([TW.anomaly(path)], [{"path": path, "owner_repo": "Simple-With-Us/BotFleet", **co}])

    def event(self, dest: str, at: str = "2026-11-04T19:00:00+00:00", **kw) -> None:
        path = Path(self.state) / P.EVENT_LOG_NAME
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"at": at, "via": "shim", "action": "detect", "class": "home", "dest": dest,
               "source": "Simple-With-Us/BotFleet", "seat": "CLAUDE", **kw}
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def state_doc(self) -> dict:
        return json.loads(Path(self.state, W.STATE_NAME).read_text(encoding="utf-8"))


class WatcherInstall(WatchReview):
    def test_the_server_env_label_is_one_the_board_takes(self) -> None:
        text = (REPO / "scripts" / "linear-board" / "board").read_text(encoding="utf-8")
        envs = re.search(r"^ENVS = (\{[^}]*\})", text, re.M).group(1)
        allowed = {v for v in eval(envs).values()}  # noqa: S307 - a literal from this repo
        self.assertIn(W.board_env(P.HOST_SERVER), allowed)
        self.assertIn(W.board_env(P.HOST_MAC), allowed)

    def test_the_templates_carry_the_bearer_locator_and_registry(self) -> None:
        plist = (PKG / "templates" / "com.jay.lane-placement-watch.plist").read_text(encoding="utf-8")
        self.assertIn("SIMPLE_BOARD_BEARER_LOCATOR", plist)
        self.assertIn("FLEET_APPS_JSON", plist)
        unit = (REPO / "scripts" / "systemd" / "lane-placement-watch.service").read_text(encoding="utf-8")
        self.assertIn("placement-watch.conf", unit)

    def test_an_apply_with_no_bearer_sends_nothing_and_says_why(self) -> None:
        self.event(self.p("foo"))
        with mock.patch.object(W, "Filer", side_effect=AssertionError("no filer without a bearer")):
            rc = W.run_once(home=self.home, env=self.vars, apply=True, seat="CLAUDE", session="s", report_path=None,
                            verify_hooks=False, stable=None, now=TW.NOW, build_report=lambda *a, **k: self.report([]), out=io.StringIO())
        self.assertEqual(rc, W.EXIT_FILING)
        status = json.loads(Path(self.state, W.STATUS_NAME).read_text(encoding="utf-8"))
        self.assertFalse(status["ok"])
        self.assertIn("SIMPLE_BOARD_BEARER_LOCATOR", " ".join(status["errors"]))

    def test_an_unreadable_registry_is_a_status_and_not_a_crash(self) -> None:
        self.vars["FLEET_APPS_JSON"] = self.p("missing.json")
        rc = self.run_watch(self.report([]), Run())
        self.assertEqual(rc, W.EXIT_STALE)
        status = json.loads(Path(self.state, W.STATUS_NAME).read_text(encoding="utf-8"))
        self.assertFalse(status["ok"])


class EventCursor(WatchReview):
    def test_a_failed_filing_keeps_the_event(self) -> None:
        self.event(self.p("foo"))
        down = Run(code=1, verb="file")
        self.assertEqual(self.run_watch(self.report([]), down), W.EXIT_FILING)
        self.assertEqual(self.state_doc()["events_since"], "")
        up = Run()
        self.assertEqual(self.run_watch(self.report([]), up), W.EXIT_OK)
        self.assertEqual(len(up.named("simple-board", "file")), 1)

    def test_a_later_event_in_the_same_second_is_read(self) -> None:
        self.event(self.p("foo"))
        run = Run()
        self.run_watch(self.report([]), run)
        self.event(self.p("bar"))
        self.run_watch(self.report([]), run)
        self.assertIn("{home}/bar", "".join(run.bodies) + " ".join(" ".join(c) for c in run.calls))

    def test_a_fail_open_event_is_reported_once(self) -> None:
        self.event("", action="fail-open", detail="time-limit")
        run = Run()
        self.assertEqual(self.run_watch(self.report([]), run), W.EXIT_OK)
        self.assertEqual(len(run.named("simple-board", "file")), 1)
        self.assertIn("time-limit", "".join(run.bodies))
        self.run_watch(self.report([]), run)
        self.assertEqual(len(run.named("simple-board", "file")), 1)


class Fingerprint(WatchReview):
    def test_the_shim_and_the_doctor_report_one_checkout_once(self) -> None:
        path = self.p("foo")
        self.event(path)
        run = Run()
        self.run_watch(self.one(path, branch="main"), run)
        self.assertEqual(len(run.named("simple-board", "file")), 1)

    def test_a_seat_tag_and_its_branch_prefix_are_one_creator(self) -> None:
        tag = next(s for s in L.parse_registry(F.REGISTRY_DATA).seats if s.name.lower() != s.suffix.lower())
        path = self.p("foo")
        self.event(path, seat=tag.name)
        run = Run()
        self.run_watch(self.report([]), run)
        other = self.p("bar")
        self.run_watch(self.one(other, branch=f"{tag.branch_prefixes[0]}x"), run)
        self.assertEqual(len(run.named("simple-board", "file")), 1)

    def test_a_new_branch_on_the_same_path_files_nothing(self) -> None:
        path = self.p("foo")
        run = Run()
        for branch in ("claude/x", "codex/y", "main"):
            self.run_watch(self.one(path, branch=branch), run)
        self.assertEqual(len(run.named("simple-board", "file")), 1)
        self.assertEqual(run.named("simple-board", "comment"), [])


class Regressions(WatchReview):
    def main_clone(self, detail: str) -> dict:
        return self.report([TW.anomaly(os.path.join(self.code, "BotFleet"), "MAIN-CLONE", detail)])

    def test_a_new_problem_on_a_reported_clone_is_reported(self) -> None:
        run = Run()
        self.run_watch(self.main_clone("off-default-branch, ahead"), run)
        self.run_watch(self.main_clone("off-default-branch, ahead, stale-index-lock"), run)
        comments = run.named("simple-board", "comment")
        self.assertEqual(len(comments), 1)
        self.assertIn("stale-index-lock", " ".join(comments[0]))

    def test_a_checkout_fixed_and_broken_again_is_reported_again(self) -> None:
        path = self.p("foo")
        run = Run()
        self.run_watch(self.one(path), run)
        self.run_watch(self.report([]), run)
        self.run_watch(self.one(path), run)
        self.assertEqual(len(run.named("simple-board", "comment")), 1)

    def test_a_closed_issue_gets_a_new_one_with_a_new_uid(self) -> None:
        first = self.p("foo")
        run = Run()
        self.run_watch(self.one(first), run)
        run.show = "completed"
        self.run_watch(self.report([TW.anomaly(first), TW.anomaly(self.p("bar"))],
                                   [{"path": first, "owner_repo": "Simple-With-Us/BotFleet"},
                                    {"path": self.p("bar"), "owner_repo": "Simple-With-Us/BotFleet"}]), run)
        filed = run.named("simple-board", "file")
        self.assertEqual(len(filed), 2)
        self.assertEqual(run.named("simple-board", "comment"), [])
        uid = [c[c.index("--uid") + 1] for c in filed]
        self.assertNotEqual(uid[0], uid[1])

    def test_a_status_that_cannot_be_read_is_treated_as_open(self) -> None:
        run = Run()
        self.run_watch(self.one(self.p("foo")), run)
        run.code, run.verb = 1, "show"
        self.run_watch(self.report([TW.anomaly(self.p("foo")), TW.anomaly(self.p("bar"))],
                                   [{"path": self.p("foo"), "owner_repo": "Simple-With-Us/BotFleet"},
                                    {"path": self.p("bar"), "owner_repo": "Simple-With-Us/BotFleet"}]), run)
        self.assertEqual(len(run.named("simple-board", "file")), 1)
        self.assertEqual(len(run.named("simple-board", "comment")), 1)


class Burst(WatchReview):
    def many(self) -> dict:
        paths = [self.p(f"x{i}") for i in range(W.MAX_NEW_PER_RUN + 1)]
        return self.report([TW.anomaly(p) for p in paths], [{"path": p, "owner_repo": "Simple-With-Us/BotFleet"} for p in paths])

    def test_a_burst_files_nothing_remembers_nothing_and_says_so(self) -> None:
        run = Run()
        self.assertEqual(self.run_watch(self.many(), run), W.EXIT_FILING)
        self.assertEqual(run.calls, [])
        status = json.loads(Path(self.state, W.STATUS_NAME).read_text(encoding="utf-8"))
        self.assertIn("burst", " ".join(status["errors"]))

    def test_a_person_can_accept_it(self) -> None:
        run = Run()
        filer = W.Filer("CLAUDE", "s", "Mac", run=run)
        rc = W.run_once(home=self.home, env=self.vars, apply=False, seat="CLAUDE", session="s", report_path=None, verify_hooks=False,
                        stable=None, now=TW.NOW, build_report=lambda *a, **k: self.many(), filer=filer, out=io.StringIO(), accept_burst=True)
        self.assertEqual(rc, W.EXIT_OK)
        self.assertEqual(len(run.named("simple-board", "file")), 1)


class OwnerMessages(WatchReview):
    def test_repo_local_harness_worktrees_do_not_message_the_owner(self) -> None:
        for mark in (".claude/worktrees", ".muse/worktrees"):
            path = os.path.join(self.code, "BotFleet", mark, "wip")
            run = Run()
            self.run_watch(self.one(path, unpushed=1), run)
            self.assertEqual(run.named("simple-chat", "dm"), [], mark)

    def test_a_timed_out_dm_is_never_resent(self) -> None:
        path = os.path.join(self.code, "BotFleet")
        run = Run(raises="dm")
        for _ in range(3):
            self.run_watch(self.one(path, unpushed=1), run)
        self.assertEqual(len(run.named("simple-chat", "dm")), 1)

    def test_a_refused_dm_is_tried_again(self) -> None:
        path = os.path.join(self.code, "BotFleet")
        run = Run(code=6, verb="dm")
        self.run_watch(self.one(path, unpushed=1), run)
        run.code = 0
        self.run_watch(self.one(path, unpushed=1), run)
        self.run_watch(self.one(path, unpushed=1), run)
        self.assertEqual(len(run.named("simple-chat", "dm")), 2)

    def test_an_unknown_exit_from_a_dm_is_never_resent(self) -> None:
        path = os.path.join(self.code, "BotFleet")
        run = Run(code=1, verb="dm")
        for _ in range(3):
            self.run_watch(self.one(path, unpushed=1), run)
        self.assertEqual(len(run.named("simple-chat", "dm")), 1)

    def test_a_timed_out_comment_is_never_resent(self) -> None:
        run = Run()
        self.run_watch(self.one(self.p("foo")), run)
        run.raises = "comment"
        both = self.report([TW.anomaly(self.p("foo")), TW.anomaly(self.p("bar"))],
                           [{"path": self.p("foo"), "owner_repo": "Simple-With-Us/BotFleet"},
                            {"path": self.p("bar"), "owner_repo": "Simple-With-Us/BotFleet"}])
        for _ in range(3):
            self.run_watch(both, run)
        self.assertEqual(len(run.named("simple-board", "comment")), 1)

    def test_a_timed_out_post_after_filing_adds_no_comment(self) -> None:
        run = Run(raises="post")
        self.run_watch(self.one(self.p("foo")), run)
        self.run_watch(self.one(self.p("foo")), run)
        self.assertEqual(len(run.named("simple-board", "file")), 1)
        self.assertEqual(run.named("simple-board", "comment"), [])


class Reports(WatchReview):
    def write(self, path: str, age_hours: float, marker: str) -> None:
        at = TW.NOW - dt.timedelta(hours=age_hours)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps({"generated_at": at.isoformat(), "placement_anomalies": [TW.anomaly(self.p(marker))],
                                          "checkouts": [{"path": self.p(marker), "owner_repo": "Simple-With-Us/BotFleet"}]}), encoding="utf-8")

    def test_the_watcher_does_not_trust_its_own_last_sweep(self) -> None:
        self.write(os.path.join(self.state, W.REPORT_NAME), 1.5, "own")
        self.write(os.path.join(self.home, W.MAINTAIN_REPORT), 0.5, "maintain")
        run = Run()
        self.run_watch(None, run)
        self.assertIn("{home}/maintain", "".join(run.bodies))
        self.assertNotIn("{home}/own", "".join(run.bodies))

    def test_a_report_from_the_future_is_refused(self) -> None:
        path = self.p("r.json")
        self.write(path, -48, "future")
        self.assertIsNone(W.load_report(path, TW.NOW, W.REPORT_MAX_AGE_HOURS))

    def test_a_report_older_than_two_hours_is_refused(self) -> None:
        path = self.p("r.json")
        self.write(path, 3, "old")
        self.assertIsNone(W.load_report(path, TW.NOW, W.REPORT_MAX_AGE_HOURS))


class LostState(WatchReview):
    def test_a_corrupt_state_file_stops_the_sends_and_stays_on_disk(self) -> None:
        path = Path(self.state, W.STATE_NAME)
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        run = Run()
        self.assertEqual(self.run_watch(self.one(self.p("foo")), run), W.EXIT_FILING)
        self.assertEqual(run.calls, [])
        self.assertEqual(path.read_text(encoding="utf-8"), "{not json")

    def test_a_state_file_gone_after_a_run_stops_the_sends(self) -> None:
        run = Run()
        self.run_watch(self.one(self.p("foo")), run)
        os.unlink(Path(self.state, W.STATE_NAME))
        sent = len(run.calls)
        self.assertEqual(self.run_watch(self.one(self.p("foo")), run), W.EXIT_FILING)
        self.assertEqual(len(run.calls), sent)

    def test_a_person_can_reset_it(self) -> None:
        path = Path(self.state, W.STATE_NAME)
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        run = Run()
        filer = W.Filer("CLAUDE", "s", "Mac", run=run)
        rc = W.run_once(home=self.home, env=self.vars, apply=False, seat="CLAUDE", session="s", report_path=None, verify_hooks=False,
                        stable=None, now=TW.NOW, build_report=lambda *a, **k: self.one(self.p("foo")), filer=filer, out=io.StringIO(),
                        reset_state=True)
        self.assertEqual(rc, W.EXIT_OK)
        self.assertTrue(run.calls)


class DeadHooks(WatchReview):
    def write(self, rel: str, doc: dict) -> None:
        path = Path(self.home, rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(doc), encoding="utf-8")

    def test_only_hooks_count_and_a_quoted_path_is_read(self) -> None:
        self.write(".gemini/settings.json", {"mcpServers": {"x": {"command": "/nonexistent/mcp-server"}}})
        self.write(".claude/settings.json", {
            "statusLine": {"command": "/nonexistent/statusline.sh"},
            "hooks": {"PreToolUse": [{"hooks": [{"command": '"/nonexistent/quoted hook" arg'},
                                                {"command": "/nonexistent/plain arg"}]}]}})
        got = [cmd for _path, cmd in W.dead_hook_commands(self.home)]
        self.assertEqual(sorted(got), ["/nonexistent/plain", "/nonexistent/quoted hook"])

    def test_a_hook_file_with_the_list_at_the_top_is_read(self) -> None:
        self.write(".grok/hooks/lane-guard.json", {"PreToolUse": [{"command": "/nonexistent/grok-hook"}]})
        self.assertEqual([c for _p, c in W.dead_hook_commands(self.home)], ["/nonexistent/grok-hook"])


# ---------------------------------------------------------------------------- the sync job


class SyncRows(TS.SyncCase):
    def write_rows(self, text: str) -> None:
        (self.home / "rows.json").write_text(text, encoding="utf-8")
        self.vars["FLEET_PLACEMENT_EXCEPTIONS"] = str(self.home / "rows.json")

    def row(self, **kw) -> dict:
        return TD_ROW("{code}/Example", id="svc", **kw)

    def head(self) -> str:
        return TS.run(["rev-parse", "HEAD"], self.repo)

    def test_an_in_date_row_pins(self) -> None:
        self.write_rows(json.dumps({"exceptions": [self.row()]}))
        before = self.head()
        code, out = self.sync()
        self.assertEqual((code, self.head()), (0, before))
        self.assertIn("exception svc", out)

    def test_a_lapsed_row_still_pins_and_says_so(self) -> None:
        self.write_rows(json.dumps({"exceptions": [self.row(decided_on="2025-01-01", expires="2026-01-02")]}))
        before = self.head()
        code, out = self.sync()
        self.assertEqual(self.head(), before)
        self.assertEqual(code, 0)
        self.assertIn("expired 2026-01-02", out)

    def test_a_bad_exceptions_file_moves_nothing_and_exits_one(self) -> None:
        row = self.row()
        del row["creator"]
        for label, text in (("truncated", '{"exceptions": [ truncated'), ("missing key", json.dumps({"exceptions": [row]}))):
            with self.subTest(label=label):
                self.write_rows(text)
                before = self.head()
                code, out = self.sync()
                self.assertEqual((code, self.head()), (1, before))
                self.assertIn("fail Example", out)

    def test_a_missing_exceptions_file_moves_nothing_and_exits_one(self) -> None:
        self.vars["FLEET_PLACEMENT_EXCEPTIONS"] = str(self.home / "gone.json")
        before = self.head()
        code, _ = self.sync()
        self.assertEqual((code, self.head()), (1, before))


class SyncSafety(TS.SyncCase):
    def test_a_fast_forward_never_overwrites_an_ignored_file(self) -> None:
        (self.repo / ".git" / "info" / "exclude").write_text("local.cfg\n", encoding="utf-8")
        (self.repo / "local.cfg").write_text("LOCAL\n", encoding="utf-8")
        (self.seed / "local.cfg").write_text("from-upstream\n", encoding="utf-8")
        TS.run(["add", "-f", "local.cfg"], self.seed)
        TS.run(["commit", "-q", "-m", "tracked"], self.seed)
        TS.run(["push", "-q", "origin", "HEAD:main"], self.seed)
        code, out = self.sync()
        self.assertEqual((self.repo / "local.cfg").read_text(encoding="utf-8"), "LOCAL\n")
        self.assertEqual(code, 1)
        self.assertIn("fail Example", out)

    def test_a_broken_clone_is_a_failure_not_a_folder(self) -> None:
        os.unlink(self.repo / ".git" / "HEAD")
        code, out = self.sync()
        self.assertEqual(code, 1)
        self.assertIn("fail Example", out)
        self.assertNotIn("not a Git checkout", out)

    def test_a_plain_folder_is_passed_over(self) -> None:
        (self.code / "Docs").mkdir()
        code, out = self.sync()
        self.assertEqual(code, 0)
        self.assertIn("note Docs: not a Git checkout", out)


class SyncToken(unittest.TestCase):
    def fake_tool(self, home: Path, body: str) -> None:
        tool = home / ".local" / "bin" / "fleet-secrets"
        tool.parent.mkdir(parents=True)
        tool.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)

    def test_gh_token_is_honoured_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(S.resolve_github_token({"GH_TOKEN": "a", "GITHUB_ADMIN_PAT": "b"}, Path(tmp)), "GH_TOKEN")

    def test_a_fetched_token_reaches_the_name_the_credential_helper_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GH_TOKEN", None)
            self.fake_tool(Path(tmp), "printf dummy-value")
            self.assertEqual(S.resolve_github_token({}, Path(tmp)), "GH_TOKEN")
            self.assertEqual(os.environ["GH_TOKEN"], "dummy-value")

    def test_a_token_variable_the_helper_ignores_is_exported_under_the_name_it_reads(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GH_TOKEN", None)
            self.assertEqual(S.resolve_github_token({"GITHUB_ADMIN_PAT": "b"}, Path(tmp)), "GH_TOKEN")
            self.assertEqual(os.environ["GH_TOKEN"], "b")

    def test_a_lookup_that_hangs_is_logged_and_the_run_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(S.subprocess, "run", side_effect=subprocess.TimeoutExpired("x", 60)):
            self.fake_tool(Path(tmp), "exit 0")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(S.resolve_github_token({}, Path(tmp)), "")
            self.assertIn("timed out", out.getvalue())


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------- the second review round, on the shim


def _stub(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


class ShellOptions(ShimReview):
    def test_an_inherited_errexit_does_not_end_the_shim_before_git(self) -> None:
        for args in (("update-server-info",), ("submodule", "status")):
            with self.subTest(args=args):
                res = self.git(*args, SHELLOPTS="errexit")
                self.assertEqual(res.returncode, 0, res.stderr)

    def test_a_python_that_fails_still_runs_git_under_errexit(self) -> None:
        dest = self.lanes / "BotFleet" / "claude-e"
        dest.parent.mkdir()
        py = _stub(self.home / "py-fails", "exit 3")
        res = self.git("worktree", "add", "-q", str(dest), SHELLOPTS="errexit", FLEET_GIT_SHIM_PYTHON=str(py))
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(dest.exists())

    def test_an_inherited_xtrace_prints_nothing_of_the_shim(self) -> None:
        res = self.git("update-server-info", SHELLOPTS="xtrace")
        self.assertEqual((res.returncode, res.stderr), (0, ""))


    def test_the_options_reach_git_as_they_came(self) -> None:
        # A shell-script subcommand reads them.  Under nounset Apple's fails, and that is git's answer to give.
        for opts in ("errexit", "xtrace", "nounset", "pipefail", "allexport", "errexit:nounset:xtrace"):
            with self.subTest(opts=opts):
                env = self.env(SHELLOPTS=opts)
                env["PATH"] = "/usr/bin:/bin"
                real = subprocess.run([TG.REAL_GIT, "submodule", "status"], cwd=self.repo, capture_output=True, text=True, env=env, timeout=30)
                res = self.git("submodule", "status", SHELLOPTS=opts)
                self.assertEqual((res.returncode, res.stdout, res.stderr), (real.returncode, real.stdout, real.stderr))

    @unittest.skipUnless(os.path.exists("/bin/dash"), "needs dash")
    def test_the_options_line_is_one_dash_accepts(self) -> None:
        # /bin/sh is dash on a Debian host, and dash ends the script on a `set` letter it does not know, with the message hidden.
        dest = self.lanes / "BotFleet" / "claude-d"
        dest.parent.mkdir()
        for args in (("rev-parse", "--git-dir"), ("worktree", "add", "-q", str(dest))):
            with self.subTest(args=args):
                res = subprocess.run(["/bin/dash", str(self.bin / "git"), *args], cwd=self.repo, capture_output=True, text=True,
                                     env=self.env(SHELLOPTS="errexit"), timeout=30)
                self.assertEqual((res.returncode, res.stderr), (0, ""))
        self.assertTrue(dest.exists())

    def test_posix_does_not_leak_into_a_bash_hook(self) -> None:
        # Running as sh turns posix on, and an exported SHELLOPTS would hand that to a #!/bin/bash hook.
        folder = self.home / "external"
        folder.mkdir()
        (folder / "git-shows").symlink_to(shutil.which("printenv"))
        res = self.git("shows", "SHELLOPTS", SHELLOPTS="errexit", PATH=f"{self.bin}:{folder}:/usr/bin:/bin")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("errexit", res.stdout)
        self.assertNotIn("posix", res.stdout)


class BrokenInterpreterIsSilent(ShimReview):
    """Callers parse git's output, so a broken interpreter adds nothing to it, and the only trace is the event."""

    def worktree_with(self, py: str) -> subprocess.CompletedProcess:
        dest = self.lanes / "BotFleet" / "claude-q"
        dest.parent.mkdir()
        res = self.git("worktree", "add", "-q", str(dest), FLEET_GIT_SHIM_PYTHON=py)
        self.assertTrue(dest.exists())
        return res

    def test_a_loader_error_stays_out_of_gits_output(self) -> None:
        res = self.worktree_with(str(_stub(self.home / "py-talks", 'echo "dyld: Library not loaded" >&2\necho junk\nexit 1')))
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))
        self.assertEqual([e["detail"] for e in self.events() if e["action"] == "fail-open"], ["python exited 1"])

    def test_a_corrupt_binary_stays_out_of_gits_output(self) -> None:
        bad = self.home / "py-corrupt"
        bad.write_bytes(b"\x7fELFgarbage")
        bad.chmod(0o755)
        res = self.worktree_with(str(bad))
        self.assertEqual((res.returncode, res.stdout, res.stderr), (0, "", ""))

    def test_a_child_that_outlives_the_interpreter_holds_no_pipe(self) -> None:
        # subprocess reads to the end of the pipes, so this measures who still holds them.
        start = time.monotonic()
        res = self.worktree_with(str(_stub(self.home / "py-child", "sleep 9")))
        self.assertLess(time.monotonic() - start, 7.5)
        self.assertEqual((res.returncode, res.stderr), (0, ""))

    def test_a_block_still_prints_why_and_leaves_no_note(self) -> None:
        self.set_mode("block")
        notes = self.home / "notes"
        notes.mkdir()
        dest = self.home / "stray"
        res = self.git("clone", "-q", FLEET, str(dest), TMPDIR=str(notes))
        self.blocked(res, dest)
        self.assertTrue(res.stderr.startswith("fatal: Blocked"), res.stderr)
        self.assertEqual(list(notes.iterdir()), [])

    def test_a_block_with_no_note_still_blocks_and_says_so(self) -> None:
        self.set_mode("block")
        dest = self.home / "stray"
        res = self.git("clone", "-q", FLEET, str(dest), TMPDIR=str(self.home / "no-such-folder"))
        self.blocked(res, dest)


class FetchIsJudgedWhereItWrites(ShimReview):
    def setUp(self) -> None:
        super().setUp()
        self.set_mode("block")
        self.temp = Path(tempfile.mkdtemp(dir="/var/tmp"))
        self.addCleanup(shutil.rmtree, self.temp, True)

    def test_a_repo_outside_temp_named_from_a_temp_folder_passes(self) -> None:
        git_dir = str(self.repo / ".git")
        self.assertEqual(self.decide("fetch", "origin", cwd=self.temp, GIT_DIR=git_dir), 0)
        self.assertEqual(self.decide("--git-dir", git_dir, "fetch", FLEET, cwd=self.temp), 0)
        self.assertEqual(self.decide(f"--git-dir={git_dir}", "fetch", "origin", cwd=self.temp), 0)
        self.assertEqual(self.decide("--git-dir", git_dir, "--work-tree", str(self.repo), "pull", FLEET, cwd=self.temp), 0)

    def test_a_repo_in_temp_named_from_outside_is_blocked(self) -> None:
        dest = self.temp / "d"
        self.real(["init", "-q", str(dest)], self.temp)
        self.assertEqual(self.decide("--git-dir", str(dest / ".git"), "fetch", FLEET), GS.BLOCK_EXIT)
        self.assertEqual(self.decide("fetch", FLEET, GIT_DIR=str(dest / ".git")), GS.BLOCK_EXIT)

    def test_a_pull_writes_its_work_tree_too(self) -> None:
        tree = self.temp / "tree"
        self.assertEqual(self.decide("--git-dir", str(self.repo / ".git"), "--work-tree", str(tree), "pull", FLEET), GS.BLOCK_EXIT)
        self.assertEqual(self.decide("pull", FLEET, GIT_DIR=str(self.repo / ".git"), GIT_WORK_TREE=str(tree)), GS.BLOCK_EXIT)


class ClosedStderr(ShimReview):
    def test_a_block_holds_with_stderr_closed(self) -> None:
        self.set_mode("block")
        dest = self.home / "x1"
        res = subprocess.run(["/bin/sh", "-c", 'exec "$@" 2>&-', "sh", str(self.bin / "git"), "clone", "-q", FLEET, str(dest)],
                             cwd=self.repo, capture_output=True, text=True, env=self.env(**NO_NET), timeout=30)
        self.assertEqual(res.returncode, 128)
        self.assertEqual([e["action"] for e in self.events()], ["block"])
        self.assertFalse(dest.exists())


class RealGitFromPath(ShimReview):
    def test_a_wrapper_after_the_shim_runs_once(self) -> None:
        count = self.home / "count"
        wrap = self.home / "wrap"
        _stub(wrap / "git", f'echo x >> "{count}"\nPATH=$(printf %s "$PATH" | sed "s#{wrap}:##")\nexport PATH\nexec git "$@"')
        res = self.git("rev-parse", "--git-dir", PATH=f"{self.bin}:{wrap}:/usr/bin:/bin")
        self.assertEqual((res.returncode, res.stdout.strip()), (0, ".git"), res.stderr)
        self.assertEqual(len(count.read_text(encoding="utf-8").splitlines()), 1)

    def test_an_inherited_real_path_does_not_beat_path(self) -> None:
        alt = self.home / "alt"
        _stub(alt / "git", "echo ALT-GIT")
        res = self.git("version", PATH=f"{self.bin}:{alt}:/usr/bin:/bin", FLEET_GIT_SHIM_REAL=TG.REAL_GIT)
        self.assertEqual(res.stdout.strip(), "ALT-GIT")


class InterpreterStall(ShimReview):
    def test_a_stalled_interpreter_lets_git_run_and_holds_no_pipe(self) -> None:
        py = _stub(self.home / "py-hangs", "exec sleep 9")
        dest = self.lanes / "BotFleet" / "claude-s"
        dest.parent.mkdir()
        start = time.monotonic()
        res = self.git("worktree", "add", "-q", str(dest), FLEET_GIT_SHIM_PYTHON=str(py))
        took = time.monotonic() - start
        self.assertLess(took, 7.5)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stderr, "")
        self.assertTrue(dest.exists())
        stalled = [e for e in self.events() if e["action"] == "fail-open"]
        self.assertEqual(stalled[0]["detail"], "no answer from python")
        self.assertTrue(stalled[0]["at"])

    def test_a_missing_python_leaves_an_event_the_watcher_can_read(self) -> None:
        shim = self.home / "bare" / "git"
        shim.parent.mkdir()
        shutil.copy(TG.SHIM, shim)  # no module beside this copy
        res = subprocess.run([str(shim), "worktree", "add", "-q", str(self.home / "wt")], cwd=self.repo, capture_output=True, text=True,
                             env=self.env(PATH="/usr/bin:/bin"), timeout=30)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertTrue(self.events()[0]["at"])


class ShimCost(ShimReview):
    def started_python(self, *args: str, cwd: Path | None = None, **env: str) -> bool:
        marker = self.home / "started"
        py = _stub(self.home / "py-marks", f'touch "{marker}"\nexit 0')
        self.git(*args, cwd=cwd, FLEET_GIT_SHIM_PYTHON=str(py), **env)
        started = marker.exists()
        if started:
            marker.unlink()
        return started

    def test_what_cannot_make_a_checkout_stays_out_of_python(self) -> None:
        for args in (("-C", ".", "remote"), ("-C", ".", "remote", "-v"), ("-c", "a.b=c", "remote"), ("-C", ".", "stage", "-h"),
                     ("-C", ".", "submodule", "status")):
            with self.subTest(args=args):
                self.assertFalse(self.started_python(*args))
        # This fake home sits in a temp folder, so a fetch has to run from somewhere that is not.
        for args in (("-c", "a.b=c", "fetch", str(self.repo)), ("-c", "a.b=c", "pull", str(self.repo)), ("-C", "/", "fetch", str(self.repo))):
            with self.subTest(args=args):
                self.assertFalse(self.started_python(*args, cwd=Path("/")))

    def test_what_can_still_goes_to_python(self) -> None:
        temp = Path(tempfile.mkdtemp(dir="/var/tmp"))
        self.addCleanup(shutil.rmtree, temp, True)
        self.real(["config", "alias.grab", "clone"], self.repo)
        for args in (("-C", str(temp), "fetch", FLEET), ("-C", str(temp), "remote", "add", "-f", "o", FLEET), ("-C", ".", "grab", FLEET),
                     ("-c", "a.b=c", "stage", "-h"), ("-C", ".", "-C", ".", "stage", "-h"), ("-C", "../..", "fetch", FLEET),
                     ("--git-dir", str(self.repo / ".git"), "fetch", FLEET)):
            with self.subTest(args=args):
                self.assertTrue(self.started_python(*args, **NO_NET))

    def test_an_alias_in_the_repo_named_by_minus_c_is_still_blocked(self) -> None:
        self.set_mode("block")
        self.real(["config", "alias.grab", "clone"], self.repo)
        dest = self.home / "viaalias"
        self.blocked(self.git("-C", str(self.repo), "grab", FLEET, str(dest), cwd=self.home, **NO_NET), dest)


class MorePackageManagers(ShimReview):
    def setUp(self) -> None:
        super().setUp()
        self.set_mode("block")

    def test_an_editable_pip_install_clones_into_any_real_venv(self) -> None:
        venv = self.home / "w" / "v"
        (venv / "src").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        self.assertEqual(self.decide("clone", "--filter=blob:none", "--quiet", FLEET, str(venv / "src" / "dep")), 0)
        self.assertEqual(self.events()[-1]["rule"], "pkg-pip-editable")

    def test_a_folder_named_src_is_not_a_venv(self) -> None:
        self.assertEqual(self.decide("clone", FLEET, str(self.home / "w" / "plain" / "src" / "dep")), GS.BLOCK_EXIT)

    def test_a_worktree_never_passes_through_the_venv_row(self) -> None:
        venv = self.home / "w" / "v"
        (venv / "src").mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("x\n", encoding="utf-8")
        self.assertEqual(self.decide("worktree", "add", str(venv / "src" / "dep")), GS.BLOCK_EXIT)

    def test_homebrew_and_yarn_temp_clones(self) -> None:
        self.assertEqual(self.decide("clone", FLEET, str(self.home / "Library" / "Caches" / "Homebrew" / "x--git")), 0)
        temp = Path(tempfile.mkdtemp(dir="/var/tmp"))
        self.addCleanup(shutil.rmtree, temp, True)
        self.assertEqual(self.decide("clone", FLEET, str(temp / "xfs-abc" / "repo")), 0)
        self.assertEqual(self.decide("clone", FLEET, str(self.home / "xfs-abc" / "repo")), GS.BLOCK_EXIT)


class UnusableExceptions(ShimReview):
    def test_the_deny_names_the_file_and_the_event_marks_it(self) -> None:
        self.set_mode("block")
        rows = self.home / "rows.json"
        rows.write_text("{ nope", encoding="utf-8")
        err = io.StringIO()
        full = {**self.env(FLEET_PLACEMENT_EXCEPTIONS=str(rows)), "FLEET_GIT_SHIM_REAL": TG.REAL_GIT}
        with mock.patch.dict(os.environ, {"GIT_CONFIG_NOSYSTEM": "1", "HOME": str(self.home)}):
            rc = GS.decide(["clone", FLEET, str(self.home / ".npm" / "_cacache" / "tmp" / "git-cloneA")], str(self.repo), full, stderr=err)
        self.assertEqual(rc, GS.BLOCK_EXIT)
        self.assertIn("exceptions file cannot be used", err.getvalue())
        self.assertEqual(self.events()[-1]["exceptions"], "unusable")

    def test_the_reporting_load_says_why(self) -> None:
        rows = self.home / "rows.json"
        rows.write_text("{ nope", encoding="utf-8")
        self.assertIn("JSONDecodeError", P.load_exceptions_reporting(rows, env={})[1])
        self.assertIn("FileNotFoundError", P.load_exceptions_reporting(self.home / "gone.json", env={})[1])
        rows.write_text(json.dumps({"exceptions": [TD_ROW("{home}/x", **{"class": "servcie"})]}), encoding="utf-8")
        got, problem = P.load_exceptions_reporting(rows, env={})
        self.assertEqual(got, ())
        self.assertIn("servcie", problem)
        self.assertIsNone(P.load_exceptions_reporting(SHIPPED, env={})[1])


class UnknownClass(ShimReview):
    def test_a_misspelled_class_excuses_nothing(self) -> None:
        self.set_mode("block")
        rows = self.home / "rows.json"
        for cls in ("servcie", "Trash"):
            rows.write_text(json.dumps({"exceptions": [TD_ROW("{home}/typoed", hosts=["mac", "server"], **{"class": cls})]}), encoding="utf-8")
            with self.subTest(cls=cls):
                self.assertEqual(self.decide("clone", FLEET, str(self.home / "typoed" / "x"), FLEET_PLACEMENT_EXCEPTIONS=str(rows)), GS.BLOCK_EXIT)

    def test_row_excuses_and_the_doctor_refuse_it_too(self) -> None:
        row = P.ExceptionRow("r", "{home}/x", "servcie", "c", "w", "d", "2026-01-01", "2099-01-01", ("mac",))
        self.assertFalse(P.row_excuses(row, via="shim", clone=True, in_temp=False, in_code=False, mirror_or_local=False))
        self.assertFalse(P.row_excuses_checkout(row, linked_worktree=False, registered_app=False))


class ModeFile(unittest.TestCase):
    def test_a_bad_line_leaves_a_good_one_standing(self) -> None:
        m = P.parse_mode("block home\nblock bogus")
        self.assertEqual((m.mode, m.block_classes), ("block", ("home",)))
        self.assertTrue(m.problems)

    def test_a_typo_alone_stays_detect_and_says_so(self) -> None:
        m = P.parse_mode("block tmep")
        self.assertEqual(m.mode, "detect")
        self.assertIn("tmep", m.problems[0])

    def test_a_later_good_line_still_wins(self) -> None:
        self.assertEqual(P.parse_mode("block home\ndetect").mode, "detect")
        self.assertEqual(P.parse_mode("block home\ndetect").block_classes, ())

    def test_an_unreadable_mode_file_says_so(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, P.MODE_FILE_REL))
            self.assertTrue(P.read_mode(home, {}).problems)
            self.assertEqual(P.read_mode(os.path.join(home, "none"), {}).problems, ())


class PartialBlockAlias(ShimReview):
    URL = "https://github.com/Simple-With-Us/x"

    def alias(self, *dests) -> str:
        return "alias.four=!" + "; ".join(f"git clone {self.URL} {d}" for d in dests) + "; echo STILLRAN"

    def test_a_blocked_class_later_in_the_alias_blocks(self) -> None:
        temp = tempfile.mkdtemp(dir="/var/tmp")
        self.addCleanup(shutil.rmtree, temp, True)
        self.set_mode("block temp")
        self.assertEqual(self.decide("-c", self.alias(self.code / "oops", f"{temp}/qqz"), "four"), GS.BLOCK_EXIT)
        got = sorted((e["action"], e["class"]) for e in self.events())
        self.assertEqual(got, [("block", "temp"), ("detect", "code")])

    def test_a_temp_hit_does_not_hide_a_blocked_home_clone(self) -> None:
        temp = tempfile.mkdtemp(dir="/var/tmp")
        self.addCleanup(shutil.rmtree, temp, True)
        self.set_mode("block home")
        self.assertEqual(self.decide("-c", self.alias(self.home / "foo", f"{temp}/qqz"), "four"), GS.BLOCK_EXIT)
        got = sorted((e["action"], e["class"]) for e in self.events())
        self.assertEqual(got, [("block", "home"), ("detect", "temp")])

    def test_the_hook_still_stops_at_the_first_hit(self) -> None:
        c = TP.ctx("detect")
        d = G.evaluate(TP.payload(f"git clone {FLEET} /tmp/a; git clone {FLEET} ~/Code/b"), c)
        self.assertEqual(d.action, G.DENY)
        self.assertIn("/tmp/a", d.reason)


class OriginIdentity(ShimReview):
    def setUp(self) -> None:
        super().setUp()
        self.set_mode("block")
        self.temp = Path(tempfile.mkdtemp(dir="/var/tmp"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        self.work = self.home / "work" / "hh"
        self.work.parent.mkdir()
        self.real(["clone", "-q", str(self.repo), str(self.work)], self.home)

    def test_a_clone_of_a_clone_is_still_fleet(self) -> None:
        self.assertEqual(self.decide("clone", str(self.work), str(self.temp / "qq")), GS.BLOCK_EXIT)
        self.assertEqual(self.decide("-C", str(self.work), "worktree", "add", str(self.home / "y")), GS.BLOCK_EXIT)

    def test_a_renamed_remote_is_still_fleet(self) -> None:
        self.real(["remote", "rename", "origin", "upstream"], self.repo)
        self.assertEqual(self.decide("-C", str(self.repo), "worktree", "add", str(self.temp / "qq")), GS.BLOCK_EXIT)
        self.assertEqual(self.decide("-C", str(self.repo), "worktree", "add", str(self.code / "oops")), GS.BLOCK_EXIT)

    def test_a_local_chain_that_ends_outside_the_fleet_is_not(self) -> None:
        other = self.home / "mine"
        other.mkdir()
        self.real(["init", "-q", "-b", "main"], other)
        self.real(["-c", "user.email=t@e", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "x"], other)
        again = self.home / "mine2"
        self.real(["clone", "-q", str(other), str(again)], self.home)
        self.assertEqual(self.decide("clone", str(again), str(self.temp / "qq")), 0)

    def test_an_origin_of_someone_else_wins_over_another_remote(self) -> None:
        self.real(["remote", "set-url", "origin", "https://github.com/someone-else/x.git"], self.repo)
        self.real(["remote", "add", "upstream", FLEET], self.repo)
        self.assertEqual(self.decide("-C", str(self.repo), "worktree", "add", str(self.temp / "qq")), 0)

    def test_an_origin_on_another_host_is_not_read_as_a_path(self) -> None:
        self.real(["remote", "set-url", "origin", "git@gitlab.example.com:someone/x.git"], self.repo)
        self.assertEqual(self.decide("-C", str(self.repo), "worktree", "add", str(self.temp / "qq")), 0)

    def test_a_loop_of_local_origins_ends(self) -> None:
        self.real(["remote", "set-url", "origin", str(self.work)], self.repo)
        self.real(["remote", "set-url", "origin", str(self.repo)], self.work)
        self.assertEqual(self.decide("clone", str(self.work), str(self.temp / "qq")), 0)


class HookFolderNames(unittest.TestCase):
    def test_a_folder_that_only_starts_with_an_app_name_is_nobodys(self) -> None:
        for mode in ("block", "detect"):
            d = G.evaluate(TP.payload(f"git -C {TP.HOME}/apps/botfleet-tools worktree add {TP.HOME}/s", cwd=TP.HOME), TP.ctx(mode))
            self.assertEqual((d.action, d.detected), (G.ALLOW, ""), mode)

    def test_a_flat_lane_is_still_the_apps(self) -> None:
        d = G.evaluate(TP.payload(f"git -C {TP.HOME}/apps/botfleet-claude worktree add {TP.HOME}/s", cwd=TP.HOME), TP.ctx("block"))
        self.assertEqual(d.action, G.DENY)

    def test_the_older_temp_rule_still_goes_by_place(self) -> None:
        d = G.evaluate(TP.payload(f"git -C {TP.HOME}/apps/botfleet-tools worktree add /tmp/s", cwd=TP.HOME), TP.ctx("detect"))
        self.assertEqual(d.action, G.DENY)


class NarrowMatcherIsVisible(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.realpath(self.tmp.name)
        os.makedirs(os.path.join(self.home, ".claude"))

    def cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "fleet_lanes.install_tools", *args], cwd=REPO / "scripts", capture_output=True, text=True,
                              env={"HOME": self.home, "PATH": "/usr/bin:/bin", "FLEET_LANES_EXTERNAL_ROOT": ""}, timeout=120)

    def test_apply_and_verify_both_say_so(self) -> None:
        self.assertEqual(self.cli("apply", "--home", self.home, "tools").returncode, 0)
        hook = os.path.join(self.home, "apps", "lane-tools", "lane-guard-hook")
        settings = Path(self.home, ".claude", "settings.json")
        settings.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": f"{hook} --format claude"}, {"type": "command", "command": "python3 ~/x.py"}]}]}}), encoding="utf-8")
        applied = self.cli("apply", "--home", self.home, "claude", "--no-verify")
        self.assertIn("left narrow", applied.stdout + applied.stderr)
        verified = self.cli("verify", "--home", self.home, "claude")
        self.assertRegex(verified.stdout, r"WARN\s+.*matcher")
        self.assertIn("Agent, EnterWorktree", verified.stdout)


# ---------------------------------------------------------------------------- the second review round, on the watcher


class WatchRoundTwo(WatchReview):
    def many(self, n: int) -> dict:
        paths = [self.p(f"m{i:02d}") for i in range(n)]
        return self.report([TW.anomaly(p) for p in paths], [{"path": p, "owner_repo": "Simple-With-Us/BotFleet"} for p in paths])

    def remembered(self) -> set:
        return {k for g in self.state_doc()["groups"].values() for k in g["paths"]}

    def run_burst(self, report: dict, run) -> int:
        filer = W.Filer("CLAUDE", "s", "Mac", run=run)
        return W.run_once(home=self.home, env=self.vars, apply=False, seat="CLAUDE", session="s", report_path=None, verify_hooks=False,
                          stable=None, now=TW.NOW, build_report=lambda *a, **k: report, filer=filer, out=io.StringIO(), accept_burst=True)


class UnmountedDisk(WatchRoundTwo):
    def setUp(self) -> None:
        super().setUp()
        self.ext = Path(self.home, "ExtDisk", "Lanes")
        (self.ext / "Ex").mkdir(parents=True)
        Path(self.home, "apps", "lanes").mkdir(parents=True)
        os.symlink(self.ext / "Ex", Path(self.home, "apps", "lanes", "Ex"))
        self.vars["FLEET_LANES_EXTERNAL_ROOT"] = str(self.ext)
        self.path = str(self.ext / "Ex" / "clone")

    def test_a_clean_sweep_with_the_disk_away_forgets_nothing(self) -> None:
        run = Run()
        self.run_watch(self.one(self.path), run)
        sent = len([c for c in run.calls if c[1] != "show"])
        shutil.rmtree(self.ext.parent)
        self.assertEqual(self.run_watch(self.report([]), run), W.EXIT_OK)
        self.assertEqual(self.remembered(), {f"{self.path}|misplaced"})
        status = json.loads(Path(self.state, W.STATUS_NAME).read_text(encoding="utf-8"))
        self.assertEqual(status["out_of_sight"], 1)
        (self.ext / "Ex").mkdir(parents=True)
        self.run_watch(self.one(self.path), run)
        self.assertEqual(len([c for c in run.calls if c[1] != "show"]), sent)

    def test_a_clean_sweep_with_the_disk_here_forgets_it(self) -> None:
        run = Run()
        self.run_watch(self.one(self.path), run)
        self.run_watch(self.report([]), run)
        self.assertEqual(self.remembered(), set())

    def test_a_dangling_lanes_link_counts_as_away(self) -> None:
        link = Path(self.home, "apps", "lanes", "Ex", "clone")
        roots = L.make_roots(self.home, self.vars, registry=L.load_registry(env=self.vars))
        shutil.rmtree(self.ext / "Ex")
        self.assertTrue(W._out_of_sight(str(link), roots))  # noqa: SLF001
        self.assertFalse(W._out_of_sight(self.p("gone/here"), roots))  # noqa: SLF001


class FirstInstall(WatchRoundTwo):
    def applied(self, env: dict, **kw) -> int:
        return W.run_once(home=self.home, env=env, apply=True, seat="CLAUDE", session="s", report_path=None, verify_hooks=False, stable=None,
                          now=TW.NOW, build_report=lambda *a, **k: self.report([]), out=io.StringIO(), **kw)

    def test_a_first_run_with_no_locator_does_not_hold_the_next(self) -> None:
        self.assertEqual(self.applied(self.vars), W.EXIT_FILING)
        self.assertFalse(Path(self.state, W.STATE_NAME).exists())
        run = Run()
        self.assertEqual(self.run_watch(self.one(self.p("foo")), run), W.EXIT_OK)
        self.assertTrue(run.named("simple-board", "file"))

    def test_a_first_run_with_no_registry_does_not_hold_the_next(self) -> None:
        with mock.patch.object(W.L, "load_registry", side_effect=OSError("no")):
            self.assertEqual(self.applied(self.vars), W.EXIT_STALE)
        run = Run()
        self.assertEqual(self.run_watch(self.one(self.p("foo")), run), W.EXIT_OK)

    def test_a_reset_with_no_locator_does_not_leave_a_hold(self) -> None:
        path = Path(self.state, W.STATE_NAME)
        path.parent.mkdir(parents=True)
        path.write_text("{not json", encoding="utf-8")
        self.applied(self.vars, reset_state=True)
        run = Run()
        self.assertEqual(self.run_watch(self.one(self.p("foo")), run), W.EXIT_OK)

    def test_a_reset_with_no_locator_after_earlier_runs_does_not_leave_a_hold(self) -> None:
        self.run_watch(self.one(self.p("foo")), Run())
        Path(self.state, W.STATE_NAME).write_text("{not json", encoding="utf-8")
        self.applied(self.vars, reset_state=True)
        self.assertEqual(self.run_watch(self.one(self.p("foo")), Run()), W.EXIT_OK)

    def test_a_real_loss_keeps_holding_run_after_run(self) -> None:
        run = Run()
        self.run_watch(self.one(self.p("foo")), run)
        os.unlink(Path(self.state, W.STATE_NAME))
        sent = len(run.calls)
        for _ in range(3):
            self.assertEqual(self.run_watch(self.one(self.p("foo")), run), W.EXIT_FILING)
        self.assertEqual(len(run.calls), sent)

    def test_the_readme_names_the_locator_before_the_first_apply(self) -> None:
        text = (PKG / "README.md").read_text(encoding="utf-8")
        self.assertLess(text.index("export " + W.BEARER_LOCATOR + "="), text.index("--apply --accept-burst"))


class CliExits(WatchRoundTwo):
    def test_a_proven_board_refusal_keeps_the_findings(self) -> None:
        for code in (2, 3, 4, 5, 6):
            with self.subTest(code=code):
                self.setUp()
                run = Run()
                self.run_watch(self.one(self.p("foo")), run)
                both = self.report([TW.anomaly(self.p("foo")), TW.anomaly(self.p("bar"))],
                                   [{"path": self.p("foo"), "owner_repo": "Simple-With-Us/BotFleet"},
                                    {"path": self.p("bar"), "owner_repo": "Simple-With-Us/BotFleet"}])
                run.code, run.verb = code, "comment"
                self.assertEqual(self.run_watch(both, run), W.EXIT_FILING)
                self.assertNotIn(f"{self.p('bar')}|misplaced", self.remembered())
                run.code = 0
                self.assertEqual(self.run_watch(both, run), W.EXIT_OK)
                self.assertEqual(len(run.named("simple-board", "comment")), 2)

    def test_a_dm_the_credential_refused_goes_out_once_it_is_fixed(self) -> None:
        path = os.path.join(self.code, "BotFleet")
        run = Run(code=3, verb="dm")
        self.run_watch(self.one(path, unpushed=1), run)
        self.assertEqual(self.state_doc()["dm_paths"], [])
        run.code = 0
        self.run_watch(self.one(path, unpushed=1), run)
        self.assertEqual(self.state_doc()["dm_paths"], [path])
        self.run_watch(self.one(path, unpushed=1), run)
        self.assertEqual(len(run.named("simple-chat", "dm")), 2)

    def test_a_chat_network_error_that_may_have_delivered_is_never_resent(self) -> None:
        class MaybeSent(Run):
            def __call__(self, argv, **kw):
                if argv[1] == "dm":
                    self.calls.append(list(argv))
                    return SimpleNamespace(returncode=6, stdout="", stderr="timed out.  The message was not retried.  Check your messages")
                return super().__call__(argv, **kw)

        path = os.path.join(self.code, "BotFleet")
        run = MaybeSent()
        for _ in range(3):
            self.run_watch(self.one(path, unpushed=1), run)
        self.assertEqual(len(run.named("simple-chat", "dm")), 1)

    def test_a_chat_network_error_before_sending_is_tried_again(self) -> None:
        path = os.path.join(self.code, "BotFleet")
        run = Run(code=6, verb="dm")
        self.run_watch(self.one(path, unpushed=1), run)
        run.code = 0
        self.run_watch(self.one(path, unpushed=1), run)
        self.assertEqual(len(run.named("simple-chat", "dm")), 2)

    def test_the_words_this_relies_on_are_in_the_clis(self) -> None:
        text = (REPO / "scripts" / "agent_sync" / "cli.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(text.count(W.CHAT_MAYBE_SENT), 2)
        self.assertIn("(already filed; unchanged)", (REPO / "scripts" / "linear-board" / "board").read_text(encoding="utf-8"))


class BoardDedupe(WatchRoundTwo):
    class Board(Run):
        """The board's own answer to a uid it has seen:  the old issue, its text unchanged."""

        def __init__(self, known: dict | None = None, **kw) -> None:
            super().__init__(**kw)
            self.known = known or {}
            self.closed: set = set()

        def __call__(self, argv, **kw):
            if argv[:2] == ["simple-board", "show"]:
                self.calls.append(list(argv))
                return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "completed" if argv[2] in self.closed else "open"}), stderr="")
            if argv[:2] == ["simple-board", "file"]:
                uid = argv[argv.index("--uid") + 1]
                if uid in self.known:
                    self.calls.append(list(argv))
                    return SimpleNamespace(returncode=0, stdout=f"filed x  {self.known[uid]}  (already filed; unchanged)\n", stderr="")
                res = super().__call__(argv, **kw)
                self.known[uid] = f"AFC-{self.issue}"
                return res
            return super().__call__(argv, **kw)

    UID = "placement-watch:mac:claude|BotFleet"

    def test_a_retried_filing_puts_the_new_rows_in_a_comment(self) -> None:
        run = self.Board({self.UID: "AFC-700"})
        run.issue = 700
        self.assertEqual(self.run_watch(self.one(self.p("foo"), branch="claude/x"), run), W.EXIT_OK)
        self.assertEqual(self.state_doc()["groups"]["claude|BotFleet"]["issue"], "AFC-700")
        self.assertEqual(run.named("simple-chat", "post"), [])
        comment = run.named("simple-board", "comment")[0]
        self.assertIn("foo", comment[comment.index("--text") + 1])

    def test_a_closed_issue_found_by_uid_gets_a_new_one(self) -> None:
        run = self.Board({self.UID: "AFC-700"})
        run.issue = 800
        run.closed.add("AFC-700")
        self.assertEqual(self.run_watch(self.one(self.p("foo"), branch="claude/x"), run), W.EXIT_OK)
        group = self.state_doc()["groups"]["claude|BotFleet"]
        self.assertEqual((group["issue"], group["generation"]), ("AFC-801", 1))
        self.assertEqual(run.named("simple-board", "comment"), [])
        self.assertTrue(any("foo" in body for body in run.bodies))

    def test_a_reset_does_not_message_the_owner_or_post_again(self) -> None:
        path = os.path.join(self.code, "BotFleet")
        report = self.one(path, unpushed=1, branch="claude/x")
        run = self.Board()
        self.run_watch(report, run)
        Path(self.state, W.STATE_NAME).write_text("{not json", encoding="utf-8")
        filer = W.Filer("CLAUDE", "s", "Mac", run=run)
        rc = W.run_once(home=self.home, env=self.vars, apply=False, seat="CLAUDE", session="s", report_path=None, verify_hooks=False, stable=None,
                        now=TW.NOW, build_report=lambda *a, **k: report, filer=filer, out=io.StringIO(), reset_state=True)
        self.assertEqual(rc, W.EXIT_OK)
        self.assertEqual(len(run.named("simple-chat", "dm")), 1)
        self.assertEqual(len(run.named("simple-chat", "post")), 1)


class EveryRowIsSent(WatchRoundTwo):
    def test_past_the_display_limit_the_rows_go_in_comments(self) -> None:
        run = Run()
        self.assertEqual(self.run_burst(self.many(90), run), W.EXIT_OK)
        sent = run.bodies[0] + "".join(c[c.index("--text") + 1] for c in run.named("simple-board", "comment"))
        for i in range(90):
            self.assertIn(f"m{i:02d}", sent)
        self.assertEqual(len(run.named("simple-board", "comment")), 2)
        self.assertEqual(len(self.remembered()), 90)

    def test_a_refused_comment_remembers_only_what_went_out(self) -> None:
        run = Run(code=4, verb="comment")
        self.run_burst(self.many(60), run)
        self.assertEqual(len(self.remembered()), 40)
        run.code = 0
        self.run_burst(self.many(60), run)
        self.assertEqual(len(self.remembered()), 60)

    def test_a_dm_past_its_limit_tells_the_rest_next_time(self) -> None:
        paths = [os.path.join(self.code, f"R{i:02d}") for i in range(14)]
        report = self.report([TW.anomaly(p) for p in paths], [{"path": p, "owner_repo": "Simple-With-Us/BotFleet", "unpushed": 1} for p in paths])
        run = Run()
        self.run_watch(report, run)
        self.assertEqual(len(self.state_doc()["dm_paths"]), 10)
        self.run_watch(report, run)
        self.assertEqual(len(self.state_doc()["dm_paths"]), 14)


class BadStateShapes(WatchRoundTwo):
    SHAPES = ({"groups": {"x": None}}, {"groups": {"x": {"paths": 5}}}, {"groups": {"x": {"issue": 5, "paths": {}}}},
              {"groups": {"x": {"generation": "z", "paths": {}}}}, {"groups": {}, "events_since": 5}, {"groups": {}, "dm_paths": 5},
              {"groups": {}, "dm_paths": [5]}, {"groups": {}, "repeat_pending": 5}, {"groups": {}, "last_repeat_dm": 5})

    def test_each_holds_the_sends_without_a_crash_and_a_reset_clears_it(self) -> None:
        path = Path(self.state, W.STATE_NAME)
        path.parent.mkdir(parents=True)
        for shape in self.SHAPES:
            with self.subTest(shape=shape):
                path.write_text(json.dumps(shape), encoding="utf-8")
                run = Run()
                self.assertEqual(self.run_watch(self.one(self.p("foo")), run), W.EXIT_FILING)
                self.assertEqual(run.calls, [])
                filer = W.Filer("CLAUDE", "s", "Mac", run=run)
                rc = W.run_once(home=self.home, env=self.vars, apply=False, seat="CLAUDE", session="s", report_path=None, verify_hooks=False,
                                stable=None, now=TW.NOW, build_report=lambda *a, **k: self.one(self.p("foo")), filer=filer, out=io.StringIO(),
                                reset_state=True)
                self.assertEqual(rc, W.EXIT_OK)


class VerifyCannotRun(WatchRoundTwo):
    def test_it_is_not_a_clean_result(self) -> None:
        filer = W.Filer("CLAUDE", "s", "Mac", run=Run())
        rc = W.run_once(home=self.home, env=self.vars, apply=False, seat="CLAUDE", session="s", report_path=None, verify_hooks=True,
                        stable=self.p("nonexistent"), now=TW.NOW, build_report=lambda *a, **k: self.report([]), filer=filer, out=io.StringIO())
        self.assertEqual(rc, W.EXIT_STALE)
        status = json.loads(Path(self.state, W.STATUS_NAME).read_text(encoding="utf-8"))
        self.assertFalse(status["ok"])
        self.assertIn("verify", status["errors"][0])

    def test_a_crash_is_none_and_clean_is_empty(self) -> None:
        crash = lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="")  # noqa: E731
        clean = lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="PASS x y z\n")  # noqa: E731
        self.assertIsNone(W.verify_failures("/s", ("claude",), run=crash))
        self.assertEqual(W.verify_failures("/s", ("claude",), run=clean), [])


class DeadHookForms(DeadHooks):
    def test_an_interpreter_or_a_home_prefix_does_not_hide_a_missing_script(self) -> None:
        alive = Path(self.home, "alive.py")
        alive.write_text("", encoding="utf-8")
        self.write(".claude/settings.json", {"hooks": {"PreToolUse": [{"hooks": [
            {"command": "/usr/bin/python3 /gone/a.py"}, {"command": "python3 -I ~/gone/b.py"}, {"command": "$HOME/gone/c"},
            {"command": "~/gone/d arg"}, {"command": "FOO=1 /usr/bin/env python3 /gone/e.py"}, {"command": "/gone/f"},
            {"command": f"python3 {alive}"}, {"command": "python3 -c 'print(1)'"}, {"command": "python3 -m gone.module"},
            {"command": "bash -c '/gone/g'"}, {"command": "bash -lc '/gone/j'"}, {"command": "python3 $UNKNOWN/h.py"}, {"command": "$OTHER/i"}, {"command": "python3 relative.py"},
        ]}]}})
        got = sorted(cmd for _path, cmd in W.dead_hook_commands(self.home))
        self.assertEqual(got, sorted(["/gone/a.py", f"{self.home}/gone/b.py", f"{self.home}/gone/c", f"{self.home}/gone/d", "/gone/e.py", "/gone/f"]))


class EventLogBoundary(unittest.TestCase):
    def test_the_line_that_crossed_the_limit_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            env = {P.ENV_STATE_DIR: home}
            path = P.event_log_path(home, env)
            written = 0
            with mock.patch.object(P, "EVENT_LOG_MAX_BYTES", 2048):
                while not os.path.exists(path + ".1"):
                    P.log_event(home, {"n": written, "pad": "x" * 100}, env)
                    written += 1
            limit = 2048
            with open(path + ".1", "rb") as fh:
                self.assertGreater(len(fh.read()), limit)
            seen = {e["n"] for e in P.read_events(path)}
            self.assertEqual(seen, set(range(written)))

    def test_the_default_limit_is_past_the_rotation_size(self) -> None:
        import inspect

        default = inspect.signature(P.read_events).parameters["limit_bytes"].default
        self.assertGreater(default, P.EVENT_LOG_MAX_BYTES)

    def test_a_file_past_the_limit_keeps_its_newest_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "log")
            with open(path, "w", encoding="utf-8") as fh:
                for i in range(100):
                    fh.write(json.dumps({"n": i}) + "\n")
            got = [e["n"] for e in P.read_events(path, limit_bytes=60)]
            self.assertEqual(got[-1], 99)
            self.assertNotIn(0, got)


class CodeCloneWork(TD.Case):
    def on_default(self) -> None:
        TD.git(["remote", "set-url", "origin", str(self.upstream)], self.repo)
        TD.git(["fetch", "-q", "origin"], self.repo)
        TD.git(["remote", "set-head", "origin", "main"], self.repo)

    def work(self, rep: dict) -> list[dict]:
        return [w for w in rep["placement_warnings"] if w["type"] == "CODE-WORK"]

    def test_commits_off_the_checked_out_branch_reach_the_owner(self) -> None:
        self.on_default()
        self.assertEqual(self.work(self.report()), [])
        TD.git(["checkout", "-q", "-b", "wip"], self.repo)
        TD.git(["commit", "-q", "--allow-empty", "-m", "side"], self.repo)
        TD.git(["checkout", "-q", "main"], self.repo)
        rep = self.report()
        self.assertEqual([w["path"] for w in self.work(rep)], [str(self.repo)])
        self.assertIn("1 commit(s) on other branches", self.work(rep)[0]["detail"])
        self.assertEqual(self.co(rep, self.repo)["unpushed"], 0)
        self.assertEqual(rep["main_clones"][0]["issues"], [])  # old squash-merged branches look the same, so no check fails on it
        self.assertEqual(rep["placement_anomalies"], [])
        registry = L.load_registry(env=self.vars)
        found = W.findings_from_report(rep, L.make_roots(str(self.home), self.vars, registry=registry), registry)
        self.assertEqual([(f.creator, f.unpushed_in_code) for f in found], [("code-work", True)])

    def test_a_stash_reaches_the_owner(self) -> None:
        self.on_default()
        (self.repo / "f").write_text("x", encoding="utf-8")
        TD.git(["add", "f"], self.repo)
        TD.git(["stash", "-q"], self.repo)
        self.assertIn("a stash", self.work(self.report())[0]["detail"])

    def test_a_clone_with_a_row_is_left_alone(self) -> None:
        self.on_default()
        TD.git(["checkout", "-q", "-b", "wip"], self.repo)
        TD.git(["commit", "-q", "--allow-empty", "-m", "side"], self.repo)
        TD.git(["checkout", "-q", "main"], self.repo)
        self.rows(self.row("{code}/BotFleet"))
        self.assertEqual(self.work(self.report()), [])


class OneMessagePerClone(WatchRoundTwo):
    def test_many_clones_are_one_issue_and_one_message_each(self) -> None:
        paths = [os.path.join(self.code, f"R{i}") for i in range(3)]
        report = self.report([])
        report["placement_warnings"] = [{"type": "CODE-WORK", "path": p, "detail": "a stash"} for p in paths]
        run = Run()
        self.run_watch(report, run)
        self.run_watch(report, run)
        self.assertEqual(len(run.named("simple-board", "file")), 1)
        self.assertEqual(len(run.named("simple-chat", "dm")), 1)
        self.assertEqual(self.state_doc()["dm_paths"], sorted(paths))


class ThrowawayRepos(TD.Case):
    def setUp(self) -> None:
        super().setUp()
        self.temp = Path(os.path.realpath(tempfile.mkdtemp(dir="/var/tmp")))
        self.addCleanup(shutil.rmtree, self.temp, True)

    def report_with_temp(self) -> dict:
        return D.build_report(str(self.home), self.vars, cwd=None, use_gh=False, tmp_scan_roots=[self.temp], lsof_cwds=lambda: [])

    def test_a_clone_of_a_local_test_repo_in_temp_is_not_misplaced(self) -> None:
        bare = self.temp / "remote.git"
        TD.git(["init", "-q", "--bare", "-b", "main", str(bare)], self.temp)
        clone = self.temp / "t"
        TD.git(["clone", "-q", str(bare), str(clone)], self.temp)
        self.assertFalse(self.co(self.report_with_temp(), clone)["placement_violation"])

    def test_a_clone_of_a_fleet_repo_in_temp_still_is(self) -> None:
        clone = self.temp / "leak"
        TD.git(["clone", "-q", str(self.repo), str(clone)], self.temp)
        self.assertTrue(self.co(self.report_with_temp(), clone)["placement_violation"])

    def test_a_network_origin_of_unknown_owner_in_temp_still_is(self) -> None:
        clone = self.temp / "other"
        TD.git(["init", "-q", str(clone)], self.temp)
        TD.git(["remote", "add", "origin", "https://example.invalid/x/y.git"], clone)
        self.assertTrue(self.co(self.report_with_temp(), clone)["placement_violation"])


class DoctorSaysTheFileIsBroken(TD.Case):
    def test_an_unusable_file_and_a_mode_typo_are_reported(self) -> None:
        (self.home / "rows.json").write_text("{ nope", encoding="utf-8")
        mode = self.home / ".config" / "fleet" / "lane-guard-mode"
        mode.parent.mkdir(parents=True)
        mode.write_text("block tmep\n", encoding="utf-8")
        rep = self.report()
        self.assertEqual(rep["placement_anomalies"][0]["type"], "EXCEPTIONS-UNUSABLE")
        self.assertEqual(rep["placement_warnings"][0]["type"], "MODE-FILE")


class WatcherAndBrokenFiles(WatchRoundTwo):
    def test_a_broken_exceptions_file_files_only_itself(self) -> None:
        Path(self.home, "none.json").write_text("{ nope", encoding="utf-8")
        self.event(self.p("excused-really"))
        report = self.one(self.p("excused"))
        report["placement_anomalies"].insert(0, {"type": "EXCEPTIONS-UNUSABLE", "path": P.EXCEPTIONS_NAME, "detail": "broken"})
        run = Run()
        self.assertEqual(self.run_watch(report, run), W.EXIT_STALE)
        self.assertEqual(list(self.state_doc()["groups"]), ["exceptions-file|AI-Fleet-Coordinator"])
        self.assertIn("Unusable", run.named("simple-board", "file")[0][3])
        self.assertEqual(self.state_doc()["events_since"], "")

    def test_repairing_it_brings_the_real_findings_back(self) -> None:
        Path(self.home, "none.json").write_text("{ nope", encoding="utf-8")
        run = Run()
        self.run_watch(self.one(self.p("foo")), run)
        Path(self.home, "none.json").write_text('{"exceptions": []}\n', encoding="utf-8")
        self.assertEqual(self.run_watch(self.one(self.p("foo")), run), W.EXIT_OK)
        self.assertIn(f"{self.p('foo')}|misplaced", self.remembered())

    def test_a_detect_event_judged_without_rows_is_not_a_finding(self) -> None:
        self.event(self.p("foo"), exceptions="unusable")
        run = Run()
        self.assertEqual(self.run_watch(self.report([]), run), W.EXIT_OK)
        self.assertEqual(run.calls, [])

    def test_a_mode_typo_is_filed_once(self) -> None:
        mode = Path(self.home, ".config", "fleet", "lane-guard-mode")
        mode.parent.mkdir(parents=True)
        mode.write_text("block tmep\n", encoding="utf-8")
        run = Run()
        report = self.report([])
        report["placement_warnings"] = [{"type": "MODE-FILE", "path": "mode-file:unknown rule classes: tmep", "detail": "d"}]
        self.run_watch(report, run)
        self.run_watch(report, run)
        self.assertEqual(len(run.named("simple-board", "file")), 1)
        self.assertIn("Mode File", run.named("simple-board", "file")[0][3])


class RepeatLeakRate(WatchRoundTwo):
    def old_group(self) -> None:
        path = Path(self.state, W.STATE_NAME)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"groups": {"claude|BotFleet": {"first_seen": "2026-10-01T00:00:00+00:00", "issue": "AFC-1", "paths": {}}}}),
                        encoding="utf-8")

    def test_new_paths_in_an_old_group_message_the_owner_once_a_day(self) -> None:
        self.old_group()
        run = Run()
        for i in range(3):
            self.event(self.p(f"new{i}"))
            self.run_watch(self.report([]), run, now=TW.NOW + dt.timedelta(hours=i))
        self.assertEqual(len(run.named("simple-chat", "dm")), 1)
        self.assertEqual(len(self.state_doc()["repeat_pending"]), 2)
        self.event(self.p("new3"))
        self.run_watch(self.report([]), run, now=TW.NOW + dt.timedelta(hours=25))
        self.assertEqual(len(run.named("simple-chat", "dm")), 2)
        self.assertEqual(self.state_doc()["repeat_pending"], {})
        self.assertIn("new2", run.bodies[-1])

    def test_a_refused_message_stays_pending_and_is_not_rate_limited(self) -> None:
        self.old_group()
        run = Run(code=3, verb="dm")
        self.event(self.p("a"))
        self.run_watch(self.report([]), run)
        self.assertEqual(self.state_doc()["last_repeat_dm"], "")
        run.code = 0
        self.run_watch(self.report([]), run)
        self.assertEqual(self.state_doc()["repeat_pending"], {})
        self.assertEqual(len(run.named("simple-chat", "dm")), 2)
