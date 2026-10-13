"""The lane doctor summary:  counts only from a complete look, the 900 second reuse, --no-mac."""
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from fleet_facts import lanes as L
from fleet_facts.runner import Result

from .builders import doctor_checkout, doctor_report
from .fakes import FakeRunner

NOW = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)


def stamp(seconds_ago: float) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).isoformat(timespec="seconds")


def report(seconds_ago: float = 5, **kw) -> dict:
    return doctor_report([
        doctor_checkout("/home/example/apps/lanes/Example/claude-clean"),
        doctor_checkout("/home/example/apps/lanes/Example/claude-dirty", dirty_tracked=2, safety="NEEDS-REVIEW",
                        safety_reasons=["dirty: 2 tracked-modified @**all** `x`"], dropped_ball=True,
                        keep={"present": True, "reason": "waiting on [a](https://evil.example/)"}, janitor_keep=True),
        doctor_checkout("/home/example/apps/lanes/Example/claude-unpushed", unpushed=3, branch="claude/todo-list"),
        doctor_checkout("/home/example/apps/lanes/Example/claude-gitop", git_op_in_progress=["MERGE_HEAD"]),
        doctor_checkout("/home/example/apps/lanes/Example/claude-never", never_pushed=True, never_pushed_age_hours=30.5),
        doctor_checkout("/home/example/apps/lanes/Example/claude-unknown", safety="UNKNOWN"),
    ], generated_at=stamp(seconds_ago), **kw)


class Home(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        patcher = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.home / "apps").mkdir()
        lane = self.home / "apps" / "lane"
        lane.write_text("#!/bin/sh\n")
        lane.chmod(0o755)

    def runner_for(self, rep=None, **result_kw):
        text = json.dumps(rep if rep is not None else report())
        return FakeRunner(lambda argv: Result(result_kw.get("rc", 0), result_kw.get("out", text), "", result_kw.get("reason")))


class Summary(Home):
    def test_counts_and_the_attention_list(self):
        runner = self.runner_for()
        result = L.gather(runner, now=NOW)
        self.assertEqual(result["status"], "ok", result["reason"])
        data = result["data"]
        self.assertEqual(data["total"], 6)
        self.assertEqual(data["by_safety"], {"SAFE-TO-REMOVE": 6})
        self.assertEqual(data["attention_count"], 5)           # everything but the clean one
        names = [Path(r["path"]).name for r in data["attention"]]
        self.assertEqual(names, sorted(names))
        self.assertNotIn("claude-clean", names)
        self.assertEqual(runner.calls[0]["argv"][1:], ["ls", "--json"])
        self.assertEqual(runner.calls[0]["timeout"], 1200)     # the 20 minute cap
        self.assertFalse(data["reused_report"])

    def test_paths_stay_exact_when_plain_and_are_marked_when_not(self):
        rep = doctor_report([doctor_checkout("/home/example/apps/lanes/Example/todo-app", dirty_tracked=1),
                             doctor_checkout("/home/example/apps/lanes/Example/odd@name", dirty_tracked=1)], generated_at=stamp(5))
        data = L.gather(self.runner_for(rep), now=NOW)["data"]
        by_exact = {r["path_exact"]: r["path"] for r in data["attention"]}
        self.assertEqual(by_exact[True], "/home/example/apps/lanes/Example/todo-app")
        self.assertNotIn("@", by_exact[False])

    def test_text_is_cleaned_and_branches_stay_exact(self):
        data = L.gather(self.runner_for(), now=NOW)["data"]
        dirty = next(r for r in data["attention"] if r["path"].endswith("claude-dirty"))
        self.assertNotIn("@", dirty["reasons"][0] + dirty["keep_reason"])
        self.assertNotIn("://", dirty["keep_reason"])
        self.assertTrue(dirty["janitor_keep"] and dirty["dropped_ball"] is True)
        todo = next(r for r in data["attention"] if r["path"].endswith("claude-unpushed"))
        self.assertEqual(todo["branch"], "claude/todo-list")
        self.assertTrue(todo["branch_exact"])
        self.assertEqual(todo["unpushed"], 3)
        never = next(r for r in data["attention"] if r["path"].endswith("claude-never"))
        self.assertEqual(never["never_pushed_age_hours"], 30.5)
        gitop = next(r for r in data["attention"] if r["path"].endswith("claude-gitop"))
        self.assertEqual(gitop["git_op_in_progress"], ["MERGE_HEAD"])


class Failures(Home):
    def assert_no_counts(self, result, needle):
        self.assertEqual(result["status"], "failed")
        self.assertIn(needle, result["reason"])
        self.assertIsNone(result["data"])                       # no lane counts from a partial look

    def test_gh_failure_gives_no_counts(self):
        self.assert_no_counts(L.gather(self.runner_for(report(gh="failed")), now=NOW), "gh")
        self.assert_no_counts(L.gather(self.runner_for(report(gh="partial")), now=NOW), "gh")

    def test_lsof_failure_gives_no_counts(self):
        self.assert_no_counts(L.gather(self.runner_for(report(lsof="unavailable")), now=NOW), "lsof")

    def test_nonzero_exit_timeout_and_bad_output(self):
        self.assert_no_counts(L.gather(self.runner_for(rc=2), now=NOW), "exited 2")
        self.assert_no_counts(L.gather(self.runner_for(rc=None, reason="timed out after 1200s", out=""), now=NOW), "timed out")
        self.assert_no_counts(L.gather(self.runner_for(out="not json"), now=NOW), "JSON")
        self.assert_no_counts(L.gather(self.runner_for(out="[]"), now=NOW), "unexpected shape")
        self.assert_no_counts(L.gather(self.runner_for(out=json.dumps({"gh": "ok", "lsof": "ok"})), now=NOW), "unexpected shape")

    def test_a_failed_run_writes_no_cache(self):
        L.gather(self.runner_for(report(gh="failed")), now=NOW)
        self.assertFalse(L.cache_path().exists())

    def test_no_lane_tool_is_absent(self):
        (self.home / "apps" / "lane").unlink()
        runner = self.runner_for()
        result = L.gather(runner, now=NOW)
        self.assertEqual(result["status"], "absent")
        self.assertEqual(runner.calls, [])


class Reuse(Home):
    def test_a_fresh_cached_report_is_reused(self):
        first = self.runner_for(report(seconds_ago=10))
        self.assertEqual(L.gather(first, now=NOW)["status"], "ok")
        self.assertEqual(len(first.calls), 1)
        second = self.runner_for()
        result = L.gather(second, now=NOW + timedelta(seconds=300))
        self.assertEqual(second.calls, [])
        self.assertTrue(result["data"]["reused_report"])
        self.assertEqual(result["data"]["total"], 6)

    def test_a_report_900_seconds_old_is_not_reused(self):
        L.gather(self.runner_for(report(seconds_ago=0)), now=NOW)
        runner = self.runner_for(report(seconds_ago=0))
        L.gather(runner, now=NOW + timedelta(seconds=899))
        self.assertEqual(runner.calls, [])
        L.gather(runner, now=NOW + timedelta(seconds=900))
        self.assertEqual(len(runner.calls), 1)

    def test_age_comes_from_generated_at_not_the_file_time(self):
        L.gather(self.runner_for(report(seconds_ago=2000)), now=NOW)       # stale on arrival
        os.utime(L.cache_path())                                           # a brand new file time
        runner = self.runner_for(report(seconds_ago=1))
        L.gather(runner, now=NOW)
        self.assertEqual(len(runner.calls), 1)

    def test_a_cached_report_that_is_partial_is_not_reused(self):
        L.cache_path().parent.mkdir(parents=True)
        L.cache_path().write_text(json.dumps(report(seconds_ago=5, gh="failed")))
        runner = self.runner_for()
        result = L.gather(runner, now=NOW)
        self.assertEqual(len(runner.calls), 1)                  # the doctor ran again
        self.assertEqual(result["status"], "ok")

    def test_the_cache_is_private(self):
        L.gather(self.runner_for(), now=NOW)
        path = L.cache_path()
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_a_garbage_cache_is_ignored(self):
        L.cache_path().parent.mkdir(parents=True)
        L.cache_path().write_text("{broken")
        runner = self.runner_for()
        self.assertEqual(L.gather(runner, now=NOW)["status"], "ok")
        self.assertEqual(len(runner.calls), 1)


class NoMac(Home):
    def test_no_mac_marks_lanes_absent_and_runs_nothing(self):
        runner = self.runner_for()
        result = L.gather(runner, now=NOW, no_mac=True)
        self.assertEqual(result, {"status": "absent", "reason": "--no-mac", "data": None})
        self.assertEqual(runner.calls, [])
        self.assertFalse(L.cache_path().exists())


if __name__ == "__main__":
    unittest.main()
