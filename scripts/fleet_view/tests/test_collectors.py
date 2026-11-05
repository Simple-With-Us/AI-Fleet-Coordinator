"""Collector tests:  lanes, PR blockers, claims, budgets and fail-closed."""

from __future__ import annotations

import json
import pathlib
import tempfile
import threading
import unittest
import unittest.mock
from datetime import datetime, timedelta, timezone

from fleet_view import collectors
from fleet_view.types import RequestBudget

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=timezone.utc)
THREE_DAYS_AGO = int(NOW.timestamp()) - 3 * 86400

SEATS = [
    {"tag": "CLAUDE", "worktreeSuffix": "claude", "branchPrefixes": ["claude/"]},
    {"tag": "MC", "worktreeSuffix": "muse-code", "branchPrefixes": ["muse-code/"]},
    {"tag": "AG", "worktreeSuffix": "antigravity", "branchPrefixes": ["ag/"]},
    {"tag": "CODEX", "worktreeSuffix": "codex", "branchPrefixes": ["codex/"]},
    {"tag": "ECHO", "worktreeSuffix": "echo", "branchPrefixes": ["echo/", "instinct/"]},
    {"tag": "WISE", "worktreeSuffix": "wise", "branchPrefixes": ["wise/", "instinct/"]},
    {"tag": "MONET", "worktreeSuffix": "monet", "branchPrefixes": ["monet/"], "retired": True},
    {"tag": "GROK-BUILD", "retired": True, "aliasOf": "GROK"},
]


def status_v2(branch="muse-code/afc-394-fleet-view", changed=2, commit="0123456789abcdef"):
    """What ``git status --porcelain=v2 --branch`` prints."""
    lines = ["# branch.oid %s" % commit, "# branch.head %s" % branch]
    lines += ["1 .M N... 100644 100644 100644 aa bb file%d.swift" % n for n in range(changed)]
    return "\n".join(lines) + "\n"


def git_runner(argv):
    """Fake git:  branch muse-code/x, two changed files, last commit three days ago."""
    if "status" in argv:
        return 0, status_v2()
    if "log" in argv:
        return 0, "%d\n" % THREE_DAYS_AGO
    return 1, ""


def make_lane(root, *parts, checkout=True):
    lane = pathlib.Path(root).joinpath(*parts)
    lane.mkdir(parents=True)
    if checkout:
        (lane / ".git").mkdir()
    return lane


class LaneTests(unittest.TestCase):
    def test_lane_record_maps_seat_branch_dirty_host_and_last_commit(self):
        index = collectors.SeatIndex(SEATS)
        with tempfile.TemporaryDirectory() as tmp:
            make_lane(tmp, "AI-Fleet-Coordinator", "muse-code-afc-394-fleet-view")
            section = collectors.collect_lanes(pathlib.Path(tmp), index, git_runner, NOW, "mac")
        self.assertEqual(section.state, "OK")
        self.assertEqual(section.reason, "1 lane")
        self.assertEqual(len(section.items), 1)
        item = section.items[0]
        self.assertEqual(item.seat, "MC")
        self.assertEqual(item.branch, "muse-code/afc-394-fleet-view")
        self.assertEqual(item.dirty_files, 2)
        self.assertEqual(item.state, "OK")
        self.assertEqual(item.host, "mac")
        self.assertFalse(item.detached)
        self.assertEqual(item.head, "0123456")
        self.assertEqual(item.last_commit_seconds, 3 * 86400)
        self.assertEqual(item.layout, "seat")
        self.assertTrue(item.path.endswith("muse-code-afc-394-fleet-view"))

    def test_lane_scan_never_takes_the_index_lock(self):
        seen = []
        lock = threading.Lock()

        def runner(argv):
            with lock:
                seen.append(list(argv))
            return git_runner(argv)

        index = collectors.SeatIndex(SEATS)
        with tempfile.TemporaryDirectory() as tmp:
            make_lane(tmp, "AI-Fleet-Coordinator", "claude-x")
            collectors.collect_lanes(pathlib.Path(tmp), index, runner, NOW)
        status_calls = [argv for argv in seen if "status" in argv]
        self.assertTrue(status_calls)
        for argv in status_calls:
            self.assertIn("--no-optional-locks", argv)

    def test_a_detached_lane_says_so_and_names_its_commit(self):
        def runner(argv):
            if "status" in argv:
                return 0, status_v2(branch="(detached)", changed=0)
            return git_runner(argv)

        index = collectors.SeatIndex(SEATS)
        with tempfile.TemporaryDirectory() as tmp:
            make_lane(tmp, "AI-Fleet-Coordinator", "review-pr-1234")
            section = collectors.collect_lanes(pathlib.Path(tmp), index, runner, NOW)
        item = section.items[0]
        self.assertTrue(item.detached)
        self.assertEqual(item.branch, "HEAD")
        self.assertEqual(item.head, "0123456")
        self.assertEqual(item.state, "OK")
        self.assertEqual(item.seat, "REVIEW")

    def test_a_short_seat_folder_finds_its_seat_from_the_branch(self):
        def runner(argv):
            if "status" in argv:
                return 0, status_v2(branch="ag/runtime-composition", changed=0)
            return git_runner(argv)

        index = collectors.SeatIndex(SEATS)
        with tempfile.TemporaryDirectory() as tmp:
            make_lane(tmp, "BotFleet", "ag-runtime-composition")
            section = collectors.collect_lanes(pathlib.Path(tmp), index, runner, NOW)
        self.assertEqual(section.items[0].seat, "AG")

    def test_a_shared_prefix_names_both_seats(self):
        index = collectors.SeatIndex(SEATS)
        self.assertEqual(index.seat_for_branch_name("instinct/x"), "ECHO or WISE")
        self.assertEqual(index.seat_for_branch_name("echo/x"), "ECHO")

    def test_an_unreadable_last_commit_is_unknown_and_not_brand_new(self):
        def runner(argv):
            if "log" in argv:
                return 128, ""
            return git_runner(argv)

        index = collectors.SeatIndex(SEATS)
        with tempfile.TemporaryDirectory() as tmp:
            make_lane(tmp, "AI-Fleet-Coordinator", "claude-x")
            section = collectors.collect_lanes(pathlib.Path(tmp), index, runner, NOW)
        self.assertEqual(section.items[0].last_commit_seconds, -1)

    def test_retired_seat_lane_reads_unknown(self):
        index = collectors.SeatIndex(SEATS)
        self.assertEqual(index.seat_for_lane_name("monet-old-work"), "UNKNOWN")
        self.assertEqual(index.seat_for_branch_name("monet/old"), "UNKNOWN")
        self.assertEqual(index.canonical("GROK-BUILD"), "GROK")
        self.assertEqual(index.canonical("CLAUDE"), "CLAUDE")

    def test_missing_lanes_root_is_unknown_never_ok(self):
        index = collectors.SeatIndex(SEATS)
        section = collectors.collect_lanes(
            pathlib.Path("/nonexistent-lanes-root"), index, git_runner, NOW
        )
        self.assertEqual(section.state, "UNKNOWN")
        self.assertEqual(section.items, ())

    def test_unreadable_lane_tree_is_unknown_never_ok(self):
        index = collectors.SeatIndex(SEATS)
        real_iterdir = pathlib.Path.iterdir

        def fake_iterdir(self):
            if self.name == "Locked-Repo":
                raise PermissionError(13, "Permission denied")
            return real_iterdir(self)

        with tempfile.TemporaryDirectory() as tmp:
            (pathlib.Path(tmp) / "Locked-Repo").mkdir()
            make_lane(tmp, "AI-Fleet-Coordinator", "claude-x")
            with unittest.mock.patch.object(pathlib.Path, "iterdir", fake_iterdir):
                section = collectors.collect_lanes(pathlib.Path(tmp), index, git_runner, NOW)
        # The tree that could not be listed is named, and the section fails
        # closed:  an unreadable tree is not an empty one.
        self.assertEqual(section.state, "UNKNOWN")
        self.assertIn("Locked-Repo", section.reason)
        self.assertEqual(len(section.items), 1)

    def test_what_was_not_read_is_counted_in_the_reason(self):
        def runner(argv):
            if "BotFleet" in argv[argv.index("-C") + 1]:
                return 128, ""
            return git_runner(argv)

        index = collectors.SeatIndex(SEATS)
        with tempfile.TemporaryDirectory() as tmp:
            make_lane(tmp, "AI-Fleet-Coordinator", "claude-x")
            make_lane(tmp, "BotFleet", "claude-broken")
            make_lane(tmp, "BotFleet", "bot-lanes", checkout=False)
            make_lane(tmp, ".codex", "cache")
            make_lane(tmp, "_managed", "fleet", "claude-old")
            section = collectors.collect_lanes(pathlib.Path(tmp), index, runner, NOW)
        self.assertEqual(section.state, "OK")
        self.assertEqual([item.lane for item in section.items], ["claude-x", "claude-broken"])
        self.assertEqual(
            section.reason,
            "2 lanes; 1 could not be read; 1 folder with no git checkout skipped;"
            " not scanned: _managed",
        )
        self.assertEqual(section.items[1].state, "UNKNOWN")

    def test_codex_desktop_lanes_are_read_with_their_own_shape(self):
        def runner(argv):
            if "status" in argv:
                return 0, status_v2(branch="codex/fleetlink-auth", changed=0)
            return git_runner(argv)

        index = collectors.SeatIndex(SEATS)
        with tempfile.TemporaryDirectory() as tmp:
            make_lane(tmp, "_codex", "fleetlink-auth-ownership", "FleetLink")
            section = collectors.collect_lanes(pathlib.Path(tmp), index, runner, NOW)
        item = section.items[0]
        self.assertEqual(item.repo, "FleetLink")
        self.assertEqual(item.lane, "fleetlink-auth-ownership")
        self.assertEqual(item.layout, "codex-desktop")
        self.assertEqual(item.seat, "CODEX")

    def test_many_lanes_keep_their_order_when_read_in_parallel(self):
        index = collectors.SeatIndex(SEATS)
        with tempfile.TemporaryDirectory() as tmp:
            names = ["claude-%02d" % n for n in range(20)]
            for name in names:
                make_lane(tmp, "AI-Fleet-Coordinator", name)
            section = collectors.collect_lanes(pathlib.Path(tmp), index, git_runner, NOW)
        self.assertEqual([item.lane for item in section.items], names)


class HostTests(unittest.TestCase):
    def test_the_host_is_computed_from_the_platform(self):
        with unittest.mock.patch.object(collectors.platform, "system", return_value="Darwin"):
            self.assertEqual(collectors.this_host(), "mac")
        with unittest.mock.patch.object(collectors.platform, "system", return_value="Linux"):
            self.assertEqual(collectors.this_host(), "server")
        self.assertEqual(collectors.host_name("mac"), "the Mac")
        self.assertEqual(collectors.host_name("server"), "the server")

    def test_plural(self):
        self.assertEqual(collectors.plural(1, "lane"), "1 lane")
        self.assertEqual(collectors.plural(0, "lane"), "0 lanes")
        self.assertEqual(collectors.plural(2, "repo was", "repos were"), "2 repos were")


class BlockerTests(unittest.TestCase):
    BASE = {
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "APPROVED",
        "autoMergeRequest": {"enabledAt": "x"},
        "statusCheckRollup": [],
    }

    def blocker(self, **changes):
        return collectors.blocker_for({**self.BASE, **changes})

    def test_blocker_mapping(self):
        self.assertEqual(self.blocker(), "ready")
        self.assertEqual(self.blocker(reviewDecision="CHANGES_REQUESTED"), "changes requested")
        self.assertEqual(self.blocker(autoMergeRequest=None), "auto-merge not armed")
        self.assertEqual(self.blocker(mergeable="UNKNOWN", mergeStateStatus="UNKNOWN"), "unknown")
        self.assertEqual(
            self.blocker(statusCheckRollup=[{"conclusion": "FAILURE"}]), "failing checks"
        )
        self.assertEqual(self.blocker(statusCheckRollup=[{"state": "FAILURE"}]), "failing checks")
        self.assertEqual(
            self.blocker(statusCheckRollup=[{"conclusion": "STARTUP_FAILURE"}]), "failing checks"
        )
        self.assertEqual(self.blocker(statusCheckRollup=[{"state": "PENDING"}]), "checks pending")

    def test_a_draft_is_a_draft_whatever_else_it_carries(self):
        self.assertEqual(self.blocker(isDraft=True, autoMergeRequest=None), "draft")
        self.assertEqual(
            self.blocker(isDraft=True, statusCheckRollup=[{"conclusion": "FAILURE"}]), "draft"
        )
        self.assertEqual(self.blocker(mergeStateStatus="DRAFT"), "draft")

    def test_a_rule_that_blocks_is_named_before_auto_merge(self):
        self.assertEqual(
            self.blocker(mergeStateStatus="BLOCKED", autoMergeRequest=None), "blocked (see PR)"
        )
        self.assertEqual(
            self.blocker(reviewDecision="REVIEW_REQUIRED", autoMergeRequest=None),
            "waiting for review",
        )

    def test_check_states_github_adds_are_not_ready(self):
        self.assertEqual(
            self.blocker(statusCheckRollup=[{"conclusion": "ACTION_REQUIRED"}]), "failing checks"
        )
        self.assertEqual(
            self.blocker(statusCheckRollup=[{"conclusion": "STALE"}]), "checks need a rerun"
        )
        self.assertEqual(self.blocker(statusCheckRollup=[{"status": "WAITING"}]), "checks pending")

    def test_only_what_needs_someone_is_an_action(self):
        self.assertEqual(collectors.severity_for("ready"), "ready")
        for blocker in ("failing checks", "conflicting with main", "changes requested"):
            self.assertEqual(collectors.severity_for(blocker), "action", blocker)
        for blocker in ("draft", "checks pending", "waiting for review", "auto-merge not armed"):
            self.assertEqual(collectors.severity_for(blocker), "waiting", blocker)
        self.assertEqual(collectors.severity_for("unknown"), "unknown")
        self.assertEqual(collectors.severity_for("something new"), "unknown")
        for blocker in collectors.SEVERITY:
            self.assertIn(collectors.SEVERITY[blocker], ("action", "waiting", "ready", "unknown"))


def pr_row(**changes):
    row = {
        "number": 1,
        "title": "ok",
        "url": "https://github.com/good/repo/pull/1",
        "isDraft": False,
        "headRefName": "claude/x",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "APPROVED",
        "autoMergeRequest": {"enabledAt": "x"},
        "statusCheckRollup": [],
    }
    row.update(changes)
    return row


class PullRequestTests(unittest.TestCase):
    def test_pr_collection_counts_one_request_per_repo(self):
        index = collectors.SeatIndex(SEATS)
        payload = json.dumps(
            [
                pr_row(
                    number=757,
                    headRefName="opencode/muster-guards",
                    mergeStateStatus="BLOCKED",
                    reviewDecision="",
                    autoMergeRequest=None,
                )
            ]
        )
        seen = []

        def runner(argv):
            seen.append(argv)
            return 0, payload

        budget = RequestBudget()
        section = collectors.collect_pull_requests(
            ["Simple-With-Us/AI-Fleet-Coordinator"], index, runner, budget
        )
        self.assertEqual(section.state, "OK")
        self.assertEqual(section.reason, "1 open PR")
        self.assertEqual(section.unread, ())
        self.assertEqual(budget.github_used, 1)
        item = section.items[0]
        self.assertEqual(item.blocker, "blocked (see PR)")
        self.assertEqual(item.severity, "waiting")
        self.assertEqual(item.url, "https://github.com/good/repo/pull/1")
        fields = seen[0][seen[0].index("--json") + 1].split(",")
        self.assertIn("isDraft", fields)
        self.assertIn("url", fields)

    def test_a_draft_reads_as_a_draft_and_waiting(self):
        index = collectors.SeatIndex(SEATS)
        payload = json.dumps([pr_row(isDraft=True, autoMergeRequest=None)])
        section = collectors.collect_pull_requests(
            ["good/repo"], index, lambda argv: (0, payload), RequestBudget()
        )
        item = section.items[0]
        self.assertTrue(item.draft)
        self.assertEqual(item.blocker, "draft")
        self.assertEqual(item.severity, "waiting")

    def test_budget_spent_is_unknown_and_names_the_repos_not_read(self):
        index = collectors.SeatIndex(SEATS)

        def runner(argv):
            return 0, "[]"

        budget = RequestBudget(github_max=1)
        section = collectors.collect_pull_requests(["a/b", "c/d", "e/f"], index, runner, budget)
        self.assertEqual(section.state, "UNKNOWN")
        self.assertIn("budget", section.reason)
        self.assertIn("2 repos were not read", section.reason)
        self.assertEqual(section.unread, ("c/d", "e/f"))

    def test_one_bad_repo_keeps_good_items_but_stays_unknown(self):
        index = collectors.SeatIndex(SEATS)
        payload = json.dumps([pr_row()])

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
        self.assertEqual(section.unread, ("bad/repo",))

    def test_one_cause_across_many_repos_reads_once(self):
        index = collectors.SeatIndex(SEATS)
        repos = ["Simple-With-Us/R%d" % n for n in range(17)]

        def runner(argv):
            return 4, ""

        section = collectors.collect_pull_requests(
            repos, index, runner, RequestBudget(), host="mac"
        )
        self.assertEqual(
            section.reason,
            "0 open PRs; gh is not signed in on the Mac for 17 repos"
            " (Simple-With-Us/R0, Simple-With-Us/R1, Simple-With-Us/R2 and 14 more)",
        )
        self.assertEqual(len(section.unread), 17)

    def test_gh_failure_is_unknown_never_empty_ok(self):
        index = collectors.SeatIndex(SEATS)

        def runner(argv):
            raise TimeoutError("slow")

        section = collectors.collect_pull_requests(["a/b"], index, runner, RequestBudget())
        self.assertEqual(section.state, "UNKNOWN")
        self.assertIn("gh timed out", section.reason)

    def test_a_full_page_is_named_as_unread(self):
        index = collectors.SeatIndex(SEATS)
        payload = json.dumps([pr_row(number=n) for n in range(collectors.PR_PAGE)])
        section = collectors.collect_pull_requests(
            ["big/repo"], index, lambda argv: (0, payload), RequestBudget()
        )
        self.assertEqual(section.state, "UNKNOWN")
        self.assertEqual(section.unread, ("big/repo",))
        self.assertEqual(len(section.items), collectors.PR_PAGE)

    def test_a_field_of_the_wrong_kind_reads_empty_not_as_an_error(self):
        index = collectors.SeatIndex(SEATS)
        payload = json.dumps([pr_row(title=["not", "text"], number="x", url=7)])
        section = collectors.collect_pull_requests(
            ["good/repo"], index, lambda argv: (0, payload), RequestBudget()
        )
        item = section.items[0]
        self.assertEqual((item.title, item.number, item.url), ("", 0, ""))


class ClaimTests(unittest.TestCase):
    def test_claims_group_by_seat_and_flag_expiry(self):
        old = (NOW - timedelta(hours=100)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        fresh = (NOW - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        payload = json.dumps(
            [
                {
                    "identifier": "AFC-394",
                    "title": "Fleet View",
                    "url": "https://linear.app/x/issue/AFC-394",
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
                {
                    "identifier": "AFC-391",
                    "title": "Old name",
                    "labels": ["claimed:GROK-BUILD"],
                    "updated_at": fresh,
                },
            ]
        )

        def runner(argv):
            return 0, payload

        budget = RequestBudget()
        index = collectors.SeatIndex(SEATS)
        section = collectors.collect_claims(runner, budget, NOW, expiry_hours=72, index=index)
        self.assertEqual(section.state, "OK")
        self.assertEqual(section.reason, "4 claims")
        self.assertEqual(budget.linear_used, 1)
        by_id = {item.identifier: item for item in section.items}
        self.assertEqual(by_id["AFC-394"].seat, "MC")
        self.assertTrue(by_id["AFC-394"].expired)
        self.assertEqual(by_id["AFC-394"].url, "https://linear.app/x/issue/AFC-394")
        self.assertFalse(by_id["AFC-389"].expired)
        self.assertEqual(by_id["AFC-389"].url, "")
        self.assertEqual(by_id["AFC-390"].seat, "UNCLAIMED")
        self.assertEqual(by_id["AFC-391"].seat, "GROK")

    def test_unreadable_stamp_flags_the_claim_never_fresh(self):
        payload = json.dumps(
            [
                {
                    "identifier": "AFC-1",
                    "title": "x",
                    "labels": ["claimed:MC"],
                    "updated_at": "not-a-date",
                },
            ]
        )

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

    def test_the_hosted_route_shape_is_named_not_misread(self):
        def runner(argv):
            return 0, json.dumps({"structured": {}, "text": "rows"})

        section = collectors.collect_claims(runner, RequestBudget(), NOW, expiry_hours=72)
        self.assertEqual(section.state, "UNKNOWN")
        self.assertIn("hosted route", section.reason)

    def test_a_board_that_cannot_start_says_so(self):
        def runner(argv):
            raise RuntimeError("No such file or directory")

        section = collectors.collect_claims(runner, RequestBudget(), NOW, expiry_hours=72)
        self.assertEqual(section.reason, "simple-board failed to start")


if __name__ == "__main__":
    unittest.main()
