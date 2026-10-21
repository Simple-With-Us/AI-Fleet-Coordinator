"""Collector tests:  lanes, PR blockers, claims, budgets and fail-closed."""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from datetime import UTC, datetime, timedelta

from fleet_view import collectors
from fleet_view.types import RequestBudget

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

SEATS = [
    {"tag": "CLAUDE", "worktreeSuffix": "claude", "branchPrefixes": ["claude/"]},
    {"tag": "MC", "worktreeSuffix": "muse-code", "branchPrefixes": ["muse-code/"]},
    {"tag": "MONET", "worktreeSuffix": "monet", "branchPrefixes": ["monet/"], "retired": True},
]


def git_runner(argv):
    """Fake git:  branch muse-code/x, two dirty files."""
    if "rev-parse" in argv:
        return 0, "muse-code/afc-394-fleet-view\n"
    if "status" in argv:
        return 0, " M a.swift\n?? b.swift\n"
    return 1, ""


class LaneTests(unittest.TestCase):
    def test_lane_record_maps_seat_branch_and_dirty(self):
        index = collectors.SeatIndex(SEATS)
        with tempfile.TemporaryDirectory() as tmp:
            lane = pathlib.Path(tmp) / "AI-Fleet-Coordinator" / "muse-code-afc-394-fleet-view"
            lane.mkdir(parents=True)
            section = collectors.collect_lanes(pathlib.Path(tmp), index, git_runner, NOW)
        self.assertEqual(section.state, "OK")
        self.assertEqual(len(section.items), 1)
        item = section.items[0]
        self.assertEqual(item.seat, "MC")
        self.assertEqual(item.branch, "muse-code/afc-394-fleet-view")
        self.assertEqual(item.dirty_files, 2)
        self.assertEqual(item.state, "OK")

    def test_lane_scan_never_takes_the_index_lock(self):
        seen = []

        def runner(argv):
            seen.append(list(argv))
            return git_runner(argv)

        index = collectors.SeatIndex(SEATS)
        with tempfile.TemporaryDirectory() as tmp:
            lane = pathlib.Path(tmp) / "AI-Fleet-Coordinator" / "claude-x"
            lane.mkdir(parents=True)
            collectors.collect_lanes(pathlib.Path(tmp), index, runner, NOW)
        status_calls = [argv for argv in seen if "status" in argv]
        self.assertTrue(status_calls)
        for argv in status_calls:
            self.assertIn("--no-optional-locks", argv)

    def test_retired_seat_lane_reads_unknown(self):
        index = collectors.SeatIndex(SEATS)
        self.assertEqual(index.seat_for_lane_name("monet-old-work"), "UNKNOWN")
        self.assertEqual(index.seat_for_branch_name("monet/old"), "UNKNOWN")

    def test_missing_lanes_root_is_unknown_never_ok(self):
        index = collectors.SeatIndex(SEATS)
        section = collectors.collect_lanes(
            pathlib.Path("/nonexistent-lanes-root"), index, git_runner, NOW
        )
        self.assertEqual(section.state, "UNKNOWN")
        self.assertEqual(section.items, ())


class BlockerTests(unittest.TestCase):
    def test_blocker_mapping(self):
        base = {
            "mergeable": "MERGEABLE",
            "mergeStateStatus": "CLEAN",
            "reviewDecision": "APPROVED",
            "autoMergeRequest": {"enabledAt": "x"},
            "statusCheckRollup": [],
        }
        self.assertEqual(collectors.blocker_for(base), "ready")
        self.assertEqual(
            collectors.blocker_for({**base, "reviewDecision": "CHANGES_REQUESTED"}),
            "changes requested",
        )
        self.assertEqual(
            collectors.blocker_for({**base, "autoMergeRequest": None}), "auto-merge not armed"
        )
        self.assertEqual(
            collectors.blocker_for({**base, "mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"}),
            "unknown",
        )
        failing = [{**base, "statusCheckRollup": [{"conclusion": "FAILURE"}]}][0]
        self.assertEqual(collectors.blocker_for(failing), "failing checks")
        context = {**base, "statusCheckRollup": [{"state": "FAILURE"}]}
        self.assertEqual(collectors.blocker_for(context), "failing checks")
        startup = {**base, "statusCheckRollup": [{"conclusion": "STARTUP_FAILURE"}]}
        self.assertEqual(collectors.blocker_for(startup), "failing checks")
        pending = {**base, "statusCheckRollup": [{"state": "PENDING"}]}
        self.assertEqual(collectors.blocker_for(pending), "checks pending")


class PullRequestTests(unittest.TestCase):
    def test_pr_collection_counts_one_request_per_repo(self):
        index = collectors.SeatIndex(SEATS)
        payload = json.dumps(
            [
                {
                    "number": 757,
                    "title": "guards",
                    "headRefName": "opencode/muster-guards",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "BLOCKED",
                    "reviewDecision": "",
                    "autoMergeRequest": None,
                    "statusCheckRollup": [],
                }
            ]
        )

        def runner(argv):
            return 0, payload

        budget = RequestBudget()
        section = collectors.collect_pull_requests(
            ["Simple-With-Us/AI-Fleet-Coordinator"], index, runner, budget
        )
        self.assertEqual(section.state, "OK")
        self.assertEqual(budget.github_used, 1)
        self.assertEqual(section.items[0].blocker, "auto-merge not armed")

    def test_budget_spent_is_unknown_and_stops(self):
        index = collectors.SeatIndex(SEATS)

        def runner(argv):
            return 0, "[]"

        budget = RequestBudget(github_max=1)
        section = collectors.collect_pull_requests(["a/b", "c/d"], index, runner, budget)
        self.assertEqual(section.state, "UNKNOWN")
        self.assertIn("budget", section.reason)

    def test_one_bad_repo_keeps_good_items_but_stays_unknown(self):
        index = collectors.SeatIndex(SEATS)
        payload = json.dumps(
            [
                {
                    "number": 1,
                    "title": "ok",
                    "headRefName": "claude/x",
                    "mergeable": "MERGEABLE",
                    "mergeStateStatus": "CLEAN",
                    "reviewDecision": "APPROVED",
                    "autoMergeRequest": {"enabledAt": "x"},
                    "statusCheckRollup": [],
                }
            ]
        )

        def runner(argv):
            if "bad/repo" in argv:
                return 1, ""
            return 0, payload

        section = collectors.collect_pull_requests(
            ["good/repo", "bad/repo"], index, runner, RequestBudget()
        )
        self.assertEqual(section.state, "UNKNOWN")
        self.assertEqual(len(section.items), 1)
        self.assertIn("bad/repo", section.reason)

    def test_gh_failure_is_unknown_never_empty_ok(self):
        index = collectors.SeatIndex(SEATS)

        def runner(argv):
            raise TimeoutError("slow")

        section = collectors.collect_pull_requests(["a/b"], index, runner, RequestBudget())
        self.assertEqual(section.state, "UNKNOWN")


class ClaimTests(unittest.TestCase):
    def test_claims_group_by_seat_and_flag_expiry(self):
        old = (NOW - timedelta(hours=100)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        fresh = (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        payload = json.dumps(
            [
                {
                    "identifier": "AFC-394",
                    "title": "Fleet View",
                    "labels": ["claimed:MC"],
                    "updated_at": old,
                },
                {
                    "identifier": "AFC-389",
                    "title": "Grid",
                    "labels": ["claimed:MM"],
                    "updated_at": fresh,
                },
                {"identifier": "AFC-390", "title": "Alerts", "labels": [], "updated_at": fresh},
            ]
        )

        def runner(argv):
            return 0, payload

        budget = RequestBudget()
        section = collectors.collect_claims(runner, budget, NOW, expiry_hours=72)
        self.assertEqual(section.state, "OK")
        self.assertEqual(budget.linear_used, 1)
        by_id = {item.identifier: item for item in section.items}
        self.assertEqual(by_id["AFC-394"].seat, "MC")
        self.assertTrue(by_id["AFC-394"].expired)
        self.assertFalse(by_id["AFC-389"].expired)
        self.assertEqual(by_id["AFC-390"].seat, "UNCLAIMED")

    def test_unreadable_stamp_flags_the_claim_never_fresh(self):
        payload = json.dumps([
            {"identifier": "AFC-1", "title": "x", "labels": ["claimed:MC"], "updated_at": "not-a-date"},
        ])

        def runner(argv):
            return 0, payload

        section = collectors.collect_claims(runner, RequestBudget(), NOW, expiry_hours=72)
        self.assertEqual(section.items[0].age_seconds, -1)
        self.assertTrue(section.items[0].expired)

    def test_board_garbage_is_unknown(self):
        def runner(argv):
            return 0, "not json"

        section = collectors.collect_claims(runner, RequestBudget(), NOW, expiry_hours=72)
        self.assertEqual(section.state, "UNKNOWN")


if __name__ == "__main__":
    unittest.main()
