"""The temp-build rule class (AFC-618):  build output and third-party clones pointed into system temp.

Covers the argv reader (temp_build.py), the hook (guard.evaluate), the build shim (build_shim.py and shims/build), the
exception rows that excuse a build, and the watcher's temp tree report.  Every row runs in detect, where it must allow and
log, and in block, where only `block` or `block temp-build` refuses.  No test writes the account's real event log:  each one
that logs points FLEET_LANE_GUARD_STATE_DIR at its own folder.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from fleet_lanes import build_shim as BS
from fleet_lanes import guard as G
from fleet_lanes import layout as L
from fleet_lanes import placement as P
from fleet_lanes import placement_watch as W
from fleet_lanes import temp_build as TB
from fleet_lanes.tests import fixtures_guard as F

HOME = "/Users/fixture-home"
SCRATCH = "/private/tmp/claude-501/-Users-x/session/scratchpad"
FLEET = "https://github.com/Simple-With-Us/BotFleet.git"
THIRD = "https://github.com/apple/swift-nio.git"
SHIMS = Path(__file__).resolve().parents[1] / "shims"


def ctx(mode: str = "detect", *, rows: tuple = (), host: str = "mac") -> G.GuardContext:
    env = {"HOME": HOME, "TMPDIR": "/private/var/folders/ab/xyz/T/", "AGENT_SEAT": "CLAUDE", P.ENV_MODE_FILE: "/nonexistent"}
    made = G.make_context(env, home=HOME, registry=L.parse_registry(F.REGISTRY_DATA), case_insensitive=True)
    made.mode = P.parse_mode(mode)
    made.exceptions = rows
    made.host = host
    return made


def payload(command: str, cwd: str = f"{HOME}/apps/lanes/BotFleet/claude-x") -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}


def row(pattern: str, cls: str = "temp-build") -> P.ExceptionRow:
    return P.ExceptionRow("r", pattern, cls, "t", "why", "CLAUDE", "2026-11-07", "2099-01-01", ("mac", "server"))


# (command, cwd, the destination the hook must name)
CAUGHT = (
    ("xcodebuild -scheme HogHunter -derivedDataPath /private/tmp/hh-dd build", None, "/private/tmp/hh-dd"),
    ("xcrun xcodebuild -derivedDataPath=/tmp/dd test", None, "/tmp/dd"),
    ("xcodebuild -archivePath $TMPDIR/hh.xcarchive archive", None, "/private/var/folders/ab/xyz/T/hh.xcarchive"),
    ("xcodebuild SYMROOT=/private/tmp/sym build", None, "/private/tmp/sym"),
    ("swift build --scratch-path /tmp/spm", None, "/tmp/spm"),
    ("swift test --package-path /private/tmp/pkg", None, "/private/tmp/pkg/.build"),
    ("cd /private/tmp/wise-hh-build && cargo build --release", None, "/private/tmp/wise-hh-build/target"),
    ("CARGO_TARGET_DIR=/tmp/ct cargo test", None, "/tmp/ct"),
    ("env CARGO_TARGET_DIR=/tmp/ct2 cargo check", None, "/tmp/ct2"),
    ("cargo build --target-dir=/var/tmp/t", None, "/var/tmp/t"),
    ("npm ci", "/private/tmp/web", "/private/tmp/web"),
    ("npm --prefix /tmp/site run build", None, "/tmp/site"),
    ("pnpm -C /tmp/p install", None, "/tmp/p"),
    ("yarn", "/tmp/y", "/tmp/y"),
    (f"git clone {THIRD} /private/tmp/nio", None, "/private/tmp/nio"),
    ("gh repo clone apple/swift-nio /tmp/nio2", None, "/tmp/nio2"),
)

# Never named, in any mode.
PASSED = (
    ("xcodebuild -scheme HogHunter build", None),
    (f"xcodebuild -derivedDataPath {SCRATCH}/dd build", None),
    ("xcodebuild -derivedDataPath /private/tmp/claude-501/dd build", None),
    (f"xcodebuild -derivedDataPath {HOME}/Library/Caches/HogHunterDerivedData build", None),
    ("cargo build", None),
    ("cargo --version", None),
    ("swift --version", None),
    ("swift package resolve", "/private/tmp/pkg"),
    ("npm run lint", "/private/tmp/web"),
    ("npm install -g typescript", "/private/tmp/web"),
    ("npm ci", SCRATCH),
    (f"git clone {THIRD} {HOME}/apps/lanes/_tools/nio", None),
    (f"git clone {THIRD} {SCRATCH}/nio", None),
    ("ls /private/tmp", None),
)


class Reader(unittest.TestCase):
    def test_tools_and_their_outputs(self) -> None:
        cases = {
            ("xcodebuild", ("-derivedDataPath", "/a", "-resultBundlePath=/b")): ["/a", "/b"],
            ("xcodebuild", ("build",)): [],
            ("swift", ("build", "--build-path", "/c")): ["/c"],
            ("swift", ("run",)): [".build"],
            ("cargo", ("b", "--manifest-path", "/d/Cargo.toml")): ["/d/target"],
            ("cargo", ("install", "ripgrep")): [],
            ("npm", ("run", "build:prod")): ["."],
            ("npm", ("test",)): [],
            ("bun", ("install", "--cwd", "/e")): ["/e"],
        }
        for (tool, args), want in cases.items():
            with self.subTest(tool=tool, args=args):
                self.assertEqual([o.path for o in TB.build_outputs(tool, args)], want)

    def test_cargo_reads_its_environment(self) -> None:
        self.assertEqual([o.path for o in TB.build_outputs("cargo", ["test"], {"CARGO_TARGET_DIR": "/f"})], ["/f"])

    def test_xcrun_unwraps(self) -> None:
        self.assertEqual(TB.unwrap_xcrun(["--sdk", "macosx", "xcodebuild", "-x"]), ("xcodebuild", ["-x"]))
        self.assertEqual(TB.unwrap_xcrun(["clang", "-v"]), (None, []))

    def test_each_alternative_names_a_place(self) -> None:
        for tool in (*TB.TOOLS, "git"):
            with self.subTest(tool=tool):
                text = TB.alternative(tool, "mac")
                self.assertTrue("~/Library/Caches" in text or "lane" in text, text)
        self.assertIn("~/.cache", TB.alternative("cargo", "server"))


class Hook(unittest.TestCase):
    def test_detect_allows_and_names_the_class(self) -> None:
        c = ctx("detect")
        for command, cwd, dest in CAUGHT:
            with self.subTest(command=command):
                d = G.evaluate(payload(command, cwd or payload("")["cwd"]), c)
                self.assertEqual((d.action, d.detected), (G.ALLOW, P.CLASS_TEMP_BUILD))
                self.assertEqual(d.detected_detail["dest"], dest)

    def test_block_temp_build_refuses_with_an_alternative(self) -> None:
        for mode in ("block temp-build", "block"):
            c = ctx(mode)
            for command, cwd, dest in CAUGHT:
                with self.subTest(command=command, mode=mode):
                    d = G.evaluate(payload(command, cwd or payload("")["cwd"]), c)
                    self.assertEqual(d.action, G.DENY)
                    self.assertIn(d.rule_id, G.TEMP_BUILD_RULES)
                    self.assertIn(dest, d.reason)
                    self.assertIn("scratchpad", d.reason)
                    self.assertTrue("~/Library/Caches" in d.reason or "lane" in d.reason, d.reason)

    def test_blocking_another_class_leaves_builds_alone(self) -> None:
        c = ctx("block temp,code,home")
        d = G.evaluate(payload("xcodebuild -derivedDataPath /private/tmp/hh-dd build"), c)
        self.assertEqual((d.action, d.detected), (G.ALLOW, P.CLASS_TEMP_BUILD))

    def test_scratchpad_and_ordinary_builds_pass(self) -> None:
        for mode in ("detect", "block"):
            c = ctx(mode)
            for command, cwd in PASSED:
                with self.subTest(command=command, mode=mode):
                    p = payload(command, cwd or payload("")["cwd"])
                    p["scratchpad_dir"] = SCRATCH
                    d = G.evaluate(p, c)
                    self.assertEqual((d.action, d.detected), (G.ALLOW, ""))

    def test_a_fleet_clone_into_temp_still_denies_in_every_mode(self) -> None:
        d = G.evaluate(payload(f"git clone {FLEET} /private/tmp/hh-push"), ctx("detect"))
        self.assertEqual((d.action, d.rule_id), (G.DENY, G.RULE_CLONE))

    def test_a_row_excuses_a_build_and_only_a_build(self) -> None:
        brew = row("**/tmp/*-20??????-*")
        c = ctx("block", rows=(brew,))
        d = G.evaluate(payload("cargo build", "/private/tmp/ripgrep-20261107-123-abc"), c)
        self.assertEqual(d.action, G.ALLOW)
        self.assertEqual(d.detected_detail["class"], G.CLASS_EXCUSED)
        cache = row("**/tmp/*-20??????-*", "tool-cache")
        d = G.evaluate(payload("cargo build", "/private/tmp/ripgrep-20261107-123-abc"), ctx("block", rows=(cache,)))
        self.assertEqual(d.action, G.DENY)
        self.assertFalse(P.row_excuses_checkout(brew, linked_worktree=False, registered_app=True))
        self.assertFalse(P.row_excuses(brew, via="shim", clone=True, in_temp=True, in_code=False, mirror_or_local=False))

    def test_shipped_rows_are_valid_and_cover_homebrew(self) -> None:
        data = json.loads((Path(__file__).resolve().parents[1] / P.EXCEPTIONS_NAME).read_text())
        self.assertEqual(P.validate_rows(data), [])
        rows = tuple(r for r in P.parse_rows(data) if r.cls == "temp-build")
        self.assertEqual({r.id for r in rows}, {"temp-build-homebrew-source", "temp-build-cargo-install"})
        c = ctx("block", rows=rows)
        self.assertEqual(G.evaluate(payload("cargo build", "/private/tmp/ripgrep-20261107-123-abc"), c).action, G.ALLOW)
        self.assertEqual(G.evaluate(payload("cargo build", "/private/tmp/wise-hh-build"), c).action, G.DENY)

    def test_the_mode_file_knows_the_class(self) -> None:
        self.assertIn(P.CLASS_TEMP_BUILD, P.RULE_CLASSES)
        m = P.parse_mode("block temp-build")
        self.assertEqual((m.problems, m.blocks(P.CLASS_TEMP_BUILD), m.blocks(P.CLASS_TEMP)), ((), True, False))


class Shim(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="tb-shim-")).resolve()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.state = self.dir / "state"
        self.mode = self.dir / "mode"
        self.env = {"HOME": HOME, "TMPDIR": "/private/var/folders/ab/xyz/T/", P.ENV_STATE_DIR: str(self.state),
                    P.ENV_MODE_FILE: str(self.mode), "FLEET_APPS_JSON": "/nonexistent", P.ENV_TEST_RUN: "1"}

    def events(self) -> list[dict]:
        return P.read_events(self.state / P.EVENT_LOG_NAME)

    def decide(self, mode: str, tool: str, args: list[str], cwd: str = HOME) -> tuple[int, str]:
        self.mode.write_text(mode + "\n")
        err = self.dir / "err"
        with open(err, "w") as fh:
            rc = BS.decide(tool, args, cwd, self.env, stderr=fh)
        return rc, err.read_text()

    def test_detect_logs_and_runs(self) -> None:
        rc, err = self.decide("detect", "cargo", ["build", "--target-dir", "/private/tmp/t"])
        self.assertEqual((rc, err), (0, ""))
        ev = self.events()
        self.assertEqual([(e["via"], e["action"], e["class"], e["dest"]) for e in ev],
                         [("build-shim", "detect", P.CLASS_TEMP_BUILD, "/private/tmp/t")])

    def test_block_refuses_with_the_reason(self) -> None:
        rc, err = self.decide("block temp-build", "xcodebuild", ["-derivedDataPath", "/tmp/dd", "build"])
        self.assertEqual(rc, BS.BLOCK_EXIT)
        self.assertIn("-derivedDataPath", err)
        self.assertEqual(self.events()[-1]["action"], "block")

    def test_scratchpad_off_and_other_tools_pass_silently(self) -> None:
        self.assertEqual(self.decide("block", "cargo", ["build", "--target-dir", f"{SCRATCH}/t"])[0], 0)
        self.assertEqual(self.decide("off", "cargo", ["build", "--target-dir", "/tmp/t"])[0], 0)
        self.assertEqual(self.decide("block", "make", ["-C", "/tmp/x"])[0], 0)
        self.assertEqual(self.events(), [])

    def test_the_script_runs_the_real_tool_or_refuses(self) -> None:
        real = self.dir / "real"
        real.mkdir()
        (real / "cargo").write_text('#!/bin/sh\necho "REAL $*"\n')
        (real / "cargo").chmod(0o755)
        shim = self.dir / "shim"
        shim.mkdir()
        (shim / "cargo").symlink_to(SHIMS / "build")
        env = {**os.environ, **self.env, "PATH": f"{shim}:{real}:/usr/bin:/bin"}
        env.pop("CARGO_TARGET_DIR", None)

        def run(mode: str, *args: str) -> subprocess.CompletedProcess:
            self.mode.write_text(mode + "\n")
            return subprocess.run(["cargo", *args], env=env, capture_output=True, text=True, timeout=30, cwd="/")

        fast = run("block", "build")  # nothing names temp:  straight to the real tool
        self.assertEqual((fast.returncode, fast.stdout.strip()), (0, "REAL build"))
        seen = run("detect", "build", "--target-dir", "/private/tmp/t")
        self.assertEqual((seen.returncode, seen.stdout.strip(), seen.stderr), (0, "REAL build --target-dir /private/tmp/t", ""))
        stopped = run("block temp-build", "build", "--target-dir", "/private/tmp/t")
        self.assertEqual((stopped.returncode, stopped.stdout), (1, ""))
        self.assertIn("Blocked", stopped.stderr)
        self.assertEqual([e["action"] for e in self.events()], ["detect", "block"])


class Watcher(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="tb-watch-")).resolve()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.now = _dt.datetime(2026, 11, 7, 20, 0, tzinfo=_dt.timezone.utc)

    def tree(self, name: str, *, marks: tuple = ("Build",), size: int = 64 * 1024, age_hours: float = 48) -> Path:
        top = self.root / name
        for mark in marks:
            (top / mark).mkdir(parents=True, exist_ok=True)
        top.mkdir(exist_ok=True)
        (top / "blob").write_bytes(os.urandom(size))
        when = self.now.timestamp() - age_hours * 3600
        for dirpath, dirnames, filenames in os.walk(top):
            for name_ in dirnames + filenames:
                os.utime(os.path.join(dirpath, name_), (when, when))
        os.utime(top, (when, when))
        return top

    def findings(self, **kw) -> list[W.Finding]:
        return W.temp_build_findings([str(self.root)], self.now, min_bytes=32 * 1024, **kw)

    def test_reports_big_idle_build_trees_only(self) -> None:
        self.tree("hh-dd")
        self.tree("fresh", age_hours=2)
        self.tree("tiny", size=1024)
        self.tree("photos", marks=())
        self.tree("claude-501")
        got = self.findings()
        self.assertEqual([(f.kind, f.public, f.group) for f in got], [("TEMP-BUILD", "{temp}/hh-dd", "temp-build")])
        self.assertIn("untouched for 2 day(s)", got[0].detail)

    def test_a_row_passes_a_tree(self) -> None:
        self.tree("ripgrep-20261101-1-abc")
        roots = L.make_roots(HOME, {"HOME": HOME}, registry=L.parse_registry(F.REGISTRY_DATA))
        got = self.findings(exceptions=(row("**/*-20??????-*"),), host="mac", layout_roots=roots)
        self.assertEqual(got, [])

    def test_the_default_threshold_is_one_gigabyte_and_a_day(self) -> None:
        self.assertEqual((W.TEMP_TREE_MIN_BYTES, W.TEMP_TREE_MIN_AGE_HOURS), (1 << 30, 24))

    def test_a_test_run_never_walks_the_real_temp(self) -> None:
        self.assertEqual(W.temp_scan_roots({}), [])
        self.assertEqual(W.temp_scan_roots({W.ENV_TEMP_SCAN_ROOTS: str(self.root)}), [str(self.root)])

    def test_events_of_the_class_get_their_own_group(self) -> None:
        roots = L.make_roots(HOME, {"HOME": HOME}, registry=L.parse_registry(F.REGISTRY_DATA))
        events = [{"at": "2026-11-07T20:00:00+00:00", "action": "detect", "class": P.CLASS_TEMP_BUILD, "via": "build-shim",
                   "dest": "/private/tmp/hh-dd/Build", "source": "xcodebuild", "seat": "CLAUDE"},
                  {"at": "2026-11-07T20:00:01+00:00", "action": "fail-open", "via": "build-shim", "detail": "time-limit"}]
        got, _ = W.findings_from_events(events, "", roots, host="mac")
        self.assertEqual([(f.kind, f.public) for f in got],
                         [("TEMP-BUILD-EVENT", "{temp}/hh-dd"), ("FAIL-OPEN", "build shim, time-limit")])
        self.assertEqual(got[0].group, "temp-build")


if __name__ == "__main__":
    unittest.main()
