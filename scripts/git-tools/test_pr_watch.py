#!/usr/bin/env python3
"""Tests for `watch` and `run-log` in the fleet git tools CLI (scripts/git-tools/git).  Pure decision logic
and a faked clock:  no network, no credentials."""

import importlib.machinery
import importlib.util
import io
import unittest
from pathlib import Path

IMPL = Path(__file__).resolve().parent / "git"
loader = importlib.machinery.SourceFileLoader("git_cli_watch", str(IMPL))
spec = importlib.util.spec_from_loader("git_cli_watch", loader)
git = importlib.util.module_from_spec(spec)
loader.exec_module(git)


def report(checks, **extra):
    base = {
        "complete": True,
        "state": "open",
        "merged": False,
        "head": "a" * 40,
        "checks": checks,
        "review_threads": {"unresolved": []},
        "reviews": [],
        "verdict": "blocked",
    }
    base.update(extra)
    return base


def chk(name, status="completed", conclusion="success"):
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion if status == "completed" else None,
    }


class WatchDecision(unittest.TestCase):
    def test_pending_keeps_polling(self):
        self.assertIsNone(git.watch_decision(report([chk("a"), chk("b", "in_progress")])))

    def test_no_checks_yet_keeps_polling(self):
        self.assertIsNone(git.watch_decision(report([])))

    def test_all_passed_exits_zero(self):
        self.assertEqual(
            git.watch_decision(report([chk("a"), chk("b", conclusion="skipped")]))[0], 0
        )

    def test_failure_waits_for_the_rest_unless_fail_fast(self):
        r = report([chk("a", conclusion="failure"), chk("b", "queued")])
        self.assertIsNone(git.watch_decision(r))
        self.assertEqual(git.watch_decision(r, fail_fast=True)[0], git.WATCH_EXIT_FAILED)

    def test_failure_when_everything_finished(self):
        r = report([chk("a", conclusion="failure"), chk("b")])
        self.assertEqual(git.watch_decision(r)[0], git.WATCH_EXIT_FAILED)

    def test_cancelled_counts_as_failed(self):
        self.assertEqual(
            git.watch_decision(report([chk("a", conclusion="cancelled")]))[0], git.WATCH_EXIT_FAILED
        )

    def test_merged_and_closed(self):
        self.assertEqual(git.watch_decision(report([chk("a", "queued")], merged=True))[0], 0)
        self.assertEqual(
            git.watch_decision(report([chk("a", "queued")], state="closed"))[0],
            git.WATCH_EXIT_FAILED,
        )

    def test_refusal_is_unknown_and_verbatim(self):
        code, why = git.watch_decision(report([], relay_reason="relay_refused"))
        self.assertEqual(code, git.WATCH_EXIT_UNKNOWN)
        self.assertIn("relay_refused", why)

    def test_ready_waits_for_the_clean_verdict(self):
        r = report([chk("a")])
        self.assertEqual(git.watch_decision(r)[0], 0)
        self.assertIsNone(git.watch_decision(r, require_ready=True))
        self.assertEqual(
            git.watch_decision(dict(r, verdict="observed_checks_passed"), require_ready=True)[0], 0
        )


class WatchLoop(unittest.TestCase):
    def run_loop(self, reports, timeout_s=100, interval=10, **kw):
        seq = list(reports)
        now = [0.0]
        calls = []
        orig = git.collect_readiness
        git.collect_readiness = lambda read, repo, number, head=None, **k: (
            calls.append(1),
            seq.pop(0) if len(seq) > 1 else seq[0],
        )[1]
        try:
            out = io.StringIO()
            code = git.run_watch(
                lambda: None,
                "O/R",
                1,
                interval,
                timeout_s,
                kw.get("fail_fast", False),
                kw.get("require_ready", False),
                None,
                sleep=lambda s: now.__setitem__(0, now[0] + s),
                clock=lambda: now[0],
                out=out,
            )
        finally:
            git.collect_readiness = orig
        return code, out.getvalue(), len(calls)

    def test_polls_until_passed(self):
        code, out, n = self.run_loop(
            [report([chk("a", "queued")]), report([chk("a", "in_progress")]), report([chk("a")])]
        )
        self.assertEqual((code, n), (0, 3))
        self.assertIn("all checks passed", out)

    def test_timeout_is_eight(self):
        code, out, _ = self.run_loop([report([chk("a", "queued")])], timeout_s=35)
        self.assertEqual(code, git.WATCH_EXIT_PENDING)

    def test_unchanged_lines_are_not_repeated(self):
        _, out, _ = self.run_loop([report([chk("a", "queued")])] * 3 + [report([chk("a")])])
        self.assertEqual(out.count("pending=1"), 1)

    def test_three_incomplete_reports_give_up(self):
        bad = {"complete": False, "unknowns": ["required read failed: x"], "checks": []}
        code, out, n = self.run_loop([bad])
        self.assertEqual((code, n), (git.WATCH_EXIT_UNKNOWN, 3))

    def test_refusal_stops_at_once(self):
        code, out, n = self.run_loop(
            [{"complete": False, "unknowns": [], "relay_reason": "relay_refused", "checks": []}]
        )
        self.assertEqual((code, n), (git.WATCH_EXIT_UNKNOWN, 1))
        self.assertIn("relay_refused", out)


class RunLog(unittest.TestCase):
    def test_clean_lines_strip_ansi_and_timestamps(self):
        raw = "2026-11-27T07:13:13.2960674Z \x1b[31mFAIL\x1b[39m tests/a.test.ts\nplain"
        self.assertEqual(git.clean_log_lines(raw), ["FAIL tests/a.test.ts", "plain"])

    def test_excerpt_finds_errors_with_context(self):
        lines = (
            ["ok %d" % i for i in range(30)]
            + ["AssertionError: expected 1 to equal 2"]
            + ["after"] * 10
        )
        got = git.failure_excerpt(lines, context=1)
        self.assertEqual(got, ["ok 29", "AssertionError: expected 1 to equal 2", "after"])

    def test_excerpt_falls_back_to_the_tail(self):
        lines = ["line %d" % i for i in range(100)]
        got = git.failure_excerpt(lines, max_lines=5)
        self.assertEqual(got, lines[-5:])

    def test_excerpt_is_bounded(self):
        lines = ["error: bad %d" % i for i in range(100)]
        self.assertEqual(len(git.failure_excerpt(lines, max_lines=10)), 10)


if __name__ == "__main__":
    unittest.main()
