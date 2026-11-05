"""The placement watcher: dedupe, grouping, owner messages, stale inputs, and that it only ever reports (AFC-618)."""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fleet_lanes import layout as L
from fleet_lanes import placement as P
from fleet_lanes import placement_watch as W
from fleet_lanes.tests import fixtures_guard as F

NOW = dt.datetime(2026, 11, 4, 20, 0, tzinfo=dt.timezone.utc)


class FakeRun:
    """Stands in for simple-board and simple-chat, keeping every call and the body it was given."""

    def __init__(self, *, fail: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.bodies: list[str] = []
        self.fail = fail
        self.issue = 100
        self.modes = None

    def __call__(self, argv, **_kw):
        self.calls.append(list(argv))
        for flag in ("--desc-file", "--body-file"):
            if flag in argv:
                path = argv[argv.index(flag) + 1]
                self.bodies.append(Path(path).read_text(encoding="utf-8"))
                self.modes = os.stat(path).st_mode & 0o777
        if self.fail and argv[0] == self.fail:
            return SimpleNamespace(returncode=1, stdout="", stderr="no bearer")
        if argv[:2] == ["simple-board", "file"]:
            self.issue += 1
            return SimpleNamespace(returncode=0, stdout=f"Filed AFC-{self.issue}\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="ok\n", stderr="")

    def named(self, *head: str) -> list[list[str]]:
        return [c for c in self.calls if c[: len(head)] == list(head)]


def anomaly(path: str, kind: str = "MISPLACED", detail: str = "outside-the-map") -> dict:
    return {"type": kind, "path": path, "detail": detail}


class WatchCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = os.path.realpath(self.tmp.name)
        self.state = os.path.join(self.home, "state")
        reg = os.path.join(self.home, "reg.json")
        Path(reg).write_text(json.dumps(F.REGISTRY_DATA), encoding="utf-8")
        self.vars = {"HOME": self.home, P.ENV_STATE_DIR: self.state, "FLEET_APPS_JSON": reg, P.ENV_HOST_KIND: "mac",
                     "FLEET_LANES_EXTERNAL_ROOT": "", P.ENV_EXCEPTIONS: os.path.join(self.home, "none.json")}
        Path(self.home, "none.json").write_text('{"exceptions": []}\n', encoding="utf-8")
        self.code = os.path.join(self.home, "Code")

    def report(self, anomalies: list[dict], checkouts: list[dict] | None = None, age_hours: float = 0.0) -> dict:
        at = NOW - dt.timedelta(hours=age_hours)
        return {"generated_at": at.isoformat(), "placement_anomalies": anomalies, "checkouts": checkouts or []}

    def run_watch(self, report: dict | None, run: FakeRun, *, now=NOW) -> int:
        def build(*_a, **_k):
            if report is None:
                raise OSError("no")
            return report

        filer = W.Filer("CLAUDE", "session-x", "Mac", run=run)
        return W.run_once(home=self.home, env=self.vars, apply=False, seat="CLAUDE", session="session-x", report_path=None,
                          verify_hooks=False, stable=None, now=now, build_report=build, filer=filer, out=io.StringIO())


class Grouping(WatchCase):
    def test_one_issue_per_creator_and_repo_never_per_path(self) -> None:
        wt = os.path.join(self.code, "BotFleet/.muse/worktrees")
        paths = [f"{wt}/a", f"{wt}/b", f"{wt}/c"]
        cos = [{"path": p, "owner_repo": "Simple-With-Us/BotFleet"} for p in paths]
        cos.append({"path": os.path.join(self.code, "DealDex/.muse/worktrees/d"), "owner_repo": "Simple-With-Us/DealDex"})
        run = FakeRun()
        rc = self.run_watch(self.report([anomaly(c["path"]) for c in cos], cos), run)
        self.assertEqual(rc, W.EXIT_OK)
        self.assertEqual(len(run.named("simple-board", "file")), 2)
        self.assertEqual(len(run.named("simple-chat", "post")), 2)

    def test_a_topic_names_the_issue_and_fits(self) -> None:
        p = os.path.join(self.home, "tmp-x")
        run = FakeRun()
        self.run_watch(self.report([anomaly(p)], [{"path": p, "owner_repo": "Simple-With-Us/BotFleet", "branch": "claude/x"}]), run)
        post = run.named("simple-chat", "post")[0]
        topic = post[post.index("--topic") + 1]
        self.assertTrue(topic.startswith("AFC-101 "))
        self.assertLessEqual(len(topic), 60)
        self.assertEqual(post[post.index("--as") + 1], "CLAUDE")
        board = run.named("simple-board", "file")[0]
        self.assertEqual(board[board.index("--by") + 1], "CLAUDE")
        self.assertIn("--session", board)

    def test_bodies_use_root_tokens_never_real_paths(self) -> None:
        p = os.path.join(self.home, "apps", "flat")
        run = FakeRun()
        self.run_watch(self.report([anomaly(p)], [{"path": p, "owner_repo": "Simple-With-Us/BotFleet"}]), run)
        self.assertTrue(run.bodies)
        for body in run.bodies:
            self.assertNotIn(self.home, body)
            self.assertIn("{apps}/flat", body)
        self.assertIn("  ", run.bodies[0])
        self.assertEqual(run.modes, 0o600)


class PublicText(WatchCase):
    def test_a_path_outside_every_root_is_reduced_to_its_name(self) -> None:
        roots = L.make_roots(self.home, self.vars)
        self.assertEqual(W.public_path("/var/lib/private/volume/thing", roots), "{outside}/thing")
        self.assertEqual(W.public_path(os.path.join(self.home, "x"), roots), "{home}/x")

    def test_token_shaped_text_is_scrubbed_from_a_body(self) -> None:
        finding = W.Finding("/p", "{home}/p", "c", "r", "MISPLACED", "https://user:hunter2secret@github.com/a/b.git")
        self.assertNotIn("hunter2secret", W._lines([finding]))


class Dedupe(WatchCase):
    @staticmethod
    def co(path: str) -> dict:
        return {"path": path, "owner_repo": "Simple-With-Us/BotFleet", "branch": "claude/x"}

    def test_a_second_run_is_silent_and_a_new_path_is_a_comment(self) -> None:
        a, b = os.path.join(self.home, "x1"), os.path.join(self.home, "x2")
        run = FakeRun()
        self.run_watch(self.report([anomaly(a)], [self.co(a)]), run)
        self.assertEqual(len(run.calls), 2)
        self.run_watch(self.report([anomaly(a)], [self.co(a)]), run)
        self.assertEqual(len(run.calls), 2)
        self.run_watch(self.report([anomaly(a), anomaly(b)], [self.co(a), self.co(b)]), run)
        comments = run.named("simple-board", "comment")
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0][2], "AFC-101")
        self.assertEqual(len(run.named("simple-board", "file")), 1)

    def test_a_failed_filing_retries_and_exits_nonzero(self) -> None:
        p = os.path.join(self.home, "x1")
        bad = FakeRun(fail="simple-board")
        self.assertEqual(self.run_watch(self.report([anomaly(p)], [{"path": p}]), bad), W.EXIT_FILING)
        good = FakeRun()
        self.assertEqual(self.run_watch(self.report([anomaly(p)], [{"path": p}]), good), W.EXIT_OK)
        self.assertEqual(len(good.named("simple-board", "file")), 1)

    def test_filing_carries_a_stable_uid(self) -> None:
        p = os.path.join(self.home, "x1")
        run = FakeRun()
        self.run_watch(self.report([anomaly(p)], [self.co(p)]), run)
        call = run.named("simple-board", "file")[0]
        self.assertEqual(call[call.index("--uid") + 1], "placement-watch:mac:claude|BotFleet")


class Owner(WatchCase):
    @staticmethod
    def co(path: str) -> dict:
        return {"path": path, "owner_repo": "Simple-With-Us/BotFleet", "branch": "claude/x"}

    def test_no_message_for_an_ordinary_new_leak(self) -> None:
        p = os.path.join(self.home, "x1")
        run = FakeRun()
        self.run_watch(self.report([anomaly(p)], [{"path": p}]), run)
        self.assertEqual(run.named("simple-chat", "dm"), [])

    def test_unpushed_work_in_the_code_root_tells_the_owner_once(self) -> None:
        p = os.path.join(self.code, "BotFleet")
        co = [{"path": p, "owner_repo": "Simple-With-Us/BotFleet", "unpushed": 2}]
        run = FakeRun()
        self.run_watch(self.report([anomaly(p)], co), run)
        self.assertEqual(len(run.named("simple-chat", "dm")), 1)
        self.run_watch(self.report([anomaly(p)], co), run)
        self.assertEqual(len(run.named("simple-chat", "dm")), 1)

    def test_a_repeat_leak_past_seven_days_tells_the_owner(self) -> None:
        a, b, c = (os.path.join(self.home, n) for n in ("x1", "x2", "x3"))
        run = FakeRun()
        self.run_watch(self.report([anomaly(a)], [self.co(a)]), run)
        later = NOW + dt.timedelta(days=3)
        self.run_watch(self.report([anomaly(a), anomaly(b)], [self.co(a), self.co(b)], age_hours=-72), run, now=later)
        self.assertEqual(run.named("simple-chat", "dm"), [])
        much_later = NOW + dt.timedelta(days=8)
        rows = [self.co(a), self.co(b), self.co(c)]
        self.run_watch(self.report([anomaly(a), anomaly(b), anomaly(c)], rows, age_hours=-192), run, now=much_later)
        self.assertEqual(len(run.named("simple-chat", "dm")), 1)


class Inputs(WatchCase):
    def write_report(self, report: dict) -> str:
        path = os.path.join(self.home, W.MAINTAIN_REPORT)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text(json.dumps(report), encoding="utf-8")
        return path

    def test_a_young_report_is_reused_without_a_sweep(self) -> None:
        self.write_report(self.report([]))

        def boom(*_a, **_k):
            raise AssertionError("swept anyway")

        filer = W.Filer("CLAUDE", "s", "Mac", run=FakeRun())
        rc = W.run_once(home=self.home, env=self.vars, apply=False, seat="CLAUDE", session="s", report_path=None, verify_hooks=False,
                        stable=None, now=NOW, build_report=boom, filer=filer, out=io.StringIO())
        self.assertEqual(rc, W.EXIT_OK)

    def test_an_old_report_means_a_sweep(self) -> None:
        self.write_report(self.report([], age_hours=7))
        p = os.path.join(self.home, "x1")
        run = FakeRun()
        self.run_watch(self.report([anomaly(p)], [{"path": p}]), run)
        self.assertEqual(len(run.named("simple-board", "file")), 1)

    def test_a_report_from_an_older_doctor_is_not_trusted(self) -> None:
        path = self.write_report({"generated_at": NOW.isoformat(), "checkouts": []})
        self.assertIsNone(W.load_report(path, NOW, 6))

    def test_stale_inputs_exit_nonzero_and_show_in_the_status_file(self) -> None:
        rc = self.run_watch(None, FakeRun())
        self.assertEqual(rc, W.EXIT_STALE)
        status = json.loads(Path(os.path.join(self.state, W.STATUS_NAME)).read_text(encoding="utf-8"))
        self.assertFalse(status["ok"])
        self.assertIn("sweep failed", status["errors"][0])

    def test_events_are_read_once(self) -> None:
        run = FakeRun()
        event = {"via": "shim", "action": "detect", "class": "home", "dest": f"{self.home}/z",
                 "source": "Simple-With-Us/BotFleet", "seat": "MM"}
        P.log_event(self.home, event, self.vars)
        self.run_watch(self.report([]), run)
        self.assertEqual(len(run.named("simple-board", "file")), 1)
        call = run.named("simple-board", "file")[0]
        self.assertEqual(call[call.index("--uid") + 1], "placement-watch:mac:minimax|BotFleet")
        self.run_watch(self.report([]), run)
        self.assertEqual(len(run.named("simple-board", "file")), 1)


class ReportOnly(WatchCase):
    def test_without_apply_nothing_is_called_or_written(self) -> None:
        p = os.path.join(self.home, "x1")
        out = io.StringIO()
        rc = W.run_once(home=self.home, env=self.vars, apply=False, seat="CLAUDE", session="s", report_path=None, verify_hooks=False,
                        stable=None, now=NOW, build_report=lambda *a, **k: self.report([anomaly(p)], [{"path": p}]), out=out)
        self.assertEqual(rc, W.EXIT_OK)
        self.assertIn("1 new", out.getvalue())
        self.assertFalse(os.path.exists(self.state))

    def test_the_source_never_removes_or_moves_a_checkout_or_lists_processes(self) -> None:
        text = Path(W.__file__).read_text(encoding="utf-8")
        for needle in ("rmtree", 'worktree", "remove', "shutil.move", "os.rename", '"ps"', "'ps'"):
            self.assertNotIn(needle, text)


class Hooks(WatchCase):
    def test_a_dead_hook_command_is_a_finding_and_a_live_one_is_not(self) -> None:
        live = os.path.join(self.home, "live.sh")
        Path(live).write_text("#!/bin/sh\n", encoding="utf-8")
        os.chmod(live, 0o755)
        plugin = Path(self.home, ".minimax/plugins/fleet-guard/hooks")
        plugin.mkdir(parents=True)
        dead = "/nowhere/apps/lane-tools/minimax-guard-hook"
        (plugin / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [
            {"command": dead}, {"command": f"{live} --format claude"}, {"command": "relative-on-path"}]}]}}), encoding="utf-8")
        got = W.dead_hook_commands(self.home)
        self.assertEqual([cmd for _, cmd in got], [dead])

    def test_verify_failures_are_parsed(self) -> None:
        def run(_argv, **_kw):
            return SimpleNamespace(returncode=1, stdout="PASS  claude  entry  ok\nFAIL  minimax  hooks  stale\nverify: 1 FAIL\n")

        self.assertEqual(W.verify_failures("/stable", ("minimax",), run=run), ["minimax hooks"])

    def test_verify_that_cannot_run_is_not_a_finding(self) -> None:
        def run(_argv, **_kw):
            raise subprocess.SubprocessError()

        self.assertIsNone(W.verify_failures("/stable", ("minimax",), run=run))

    def test_muse_and_clutch_are_not_probed(self) -> None:
        for name in ("muse", "clutch", "kimi"):
            self.assertNotIn(name, W.VERIFY_TARGETS)


class Creator(unittest.TestCase):
    REG = L.parse_registry(F.REGISTRY_DATA)

    def test_place_first_then_branch_then_tool(self) -> None:
        self.assertEqual(W.creator_of("/h/Code/R/.muse/worktrees/a", {"branch": "claude/x"}, self.REG), "muse")
        self.assertEqual(W.creator_of("/h/x", {"branch": "codex/y"}, self.REG), "codex")
        self.assertEqual(W.creator_of("/h/x", {"branch": "bffixer/y"}, self.REG), "botfleet")
        self.assertEqual(W.creator_of("/h/x", {"creating_tool": "cursor"}, self.REG), "cursor")
        self.assertEqual(W.creator_of("/h/x", {}, self.REG), "unknown")


class Templates(unittest.TestCase):
    DIR = Path(W.__file__).parent / "templates"
    UNITS = Path(W.__file__).parent.parent / "systemd"

    def test_plist_parses_and_is_hourly(self) -> None:
        import plistlib

        data = plistlib.loads((self.DIR / "com.jay.lane-placement-watch.plist").read_bytes())
        self.assertEqual(data["Label"], "com.jay.lane-placement-watch")
        self.assertEqual(data["StartInterval"], 3600)
        self.assertIn("CLAUDE", data["ProgramArguments"])

    def test_no_private_path_in_a_template(self) -> None:
        for path in (*self.DIR.iterdir(), self.UNITS / "lane-placement-watch.service", self.UNITS / "lane-placement-watch.timer"):
            text = path.read_text(encoding="utf-8")
            for mark in ("/mnt/", "/Users/jay"):
                self.assertNotIn(mark, text, path.name)

    def test_timer_runs_hourly(self) -> None:
        self.assertIn("OnUnitActiveSec=1h", (self.UNITS / "lane-placement-watch.timer").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
