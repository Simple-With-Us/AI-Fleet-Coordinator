#!/usr/bin/env python3
"""Unit tests for the read-only Actions spend report."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import github_api
import report
import spend


def _minutes(**kwargs):
    base = spend.empty_minutes()
    base.update(kwargs)
    return base


def _run(**kwargs):
    base = {
        "id": 1,
        "name": "CI",
        "path": ".github/workflows/ci.yml",
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        "created_at": "2026-11-12T00:00:00Z",
        "updated_at": "2026-11-12T00:30:00Z",
        "run_started_at": "2026-11-12T00:00:00Z",
        "head_sha": "abc",
    }
    base.update(kwargs)
    return base


def _measured(minutes, jobs=None, source="jobs"):
    return {"minutes": _minutes(**minutes), "jobs": jobs or [], "source": source}


class CronTests(unittest.TestCase):
    def test_hourly_daily_and_weekly(self):
        self.assertEqual(spend.cron_hits_per_week("53 * * * *"), 168)
        self.assertEqual(spend.cron_hits_per_week("0 7 * * *"), 7)
        self.assertEqual(spend.cron_hits_per_week("17 13 * * 1"), 1)
        self.assertEqual(spend.cron_hits_per_week("11,41 * * * *"), 336)
        self.assertEqual(spend.cron_hits_per_week("25 1,7,13,19 * * *"), 28)

    def test_unparsed_cron_is_none(self):
        self.assertIsNone(spend.cron_hits_per_week("not a cron"))
        self.assertIsNone(spend.cron_hits_per_week("* * *"))


class LookupTests(unittest.TestCase):
    def test_skipped_and_short_cancels_do_not_need_a_lookup(self):
        self.assertFalse(spend.needs_job_lookup(_run(conclusion="skipped")))
        self.assertFalse(spend.needs_job_lookup(_run(status="in_progress", conclusion=None)))
        self.assertTrue(spend.needs_job_lookup(_run(conclusion="success")))
        self.assertFalse(
            spend.needs_job_lookup(
                _run(
                    conclusion="cancelled",
                    run_started_at="2026-11-12T00:00:00Z",
                    updated_at="2026-11-12T00:00:30Z",
                )
            )
        )
        self.assertTrue(
            spend.needs_job_lookup(
                _run(
                    conclusion="cancelled",
                    run_started_at="2026-11-12T00:00:00Z",
                    updated_at="2026-11-12T00:02:00Z",
                )
            )
        )
        self.assertTrue(
            spend.needs_job_lookup(
                _run(
                    conclusion="cancelled",
                    run_started_at="",
                    updated_at="",
                )
            )
        )


class TimingFallbackTests(unittest.TestCase):
    def test_zero_timing_uses_job_timestamps_and_labels(self):
        timing = {
            "billable": {
                "UBUNTU": {"total_ms": 0, "jobs": 1, "job_runs": [{"job_id": 1, "duration_ms": 0}]},
                "MACOS": {
                    "total_ms": 0,
                    "jobs": 2,
                    "job_runs": [
                        {"job_id": 2, "duration_ms": 0},
                        {"job_id": 3, "duration_ms": 0},
                    ],
                },
            }
        }
        jobs = [
            {
                "name": "test",
                "conclusion": "success",
                "labels": ["ubuntu-latest"],
                "started_at": "2026-11-12T00:40:22Z",
                "completed_at": "2026-11-12T01:08:06Z",
            },
            {
                "name": "Muster macOS build",
                "conclusion": "success",
                "labels": ["macos-latest"],
                "started_at": "2026-11-12T01:25:35Z",
                "completed_at": "2026-11-12T01:32:17Z",
            },
            {
                "name": "Mac scripts",
                "conclusion": "success",
                "labels": ["macos-latest"],
                "started_at": "2026-11-12T01:26:43Z",
                "completed_at": "2026-11-12T01:27:59Z",
            },
        ]
        chosen, source = spend.choose_minutes(timing, jobs)
        self.assertEqual(source, "jobs")
        self.assertEqual(chosen["minutes"]["linux"], 28)
        self.assertEqual(chosen["minutes"]["macos"], 9)
        self.assertEqual(spend.weighted(chosen["minutes"]), 118)

    def test_positive_timing_wins_over_jobs(self):
        timing = {
            "billable": {
                "UBUNTU": {"total_ms": 180000, "job_runs": [{"job_id": 1, "duration_ms": 180000}]},
                "WINDOWS": {"total_ms": 60000, "job_runs": [{"job_id": 2, "duration_ms": 61000}]},
            }
        }
        chosen, source = spend.choose_minutes(
            timing,
            [
                {
                    "name": "ignored",
                    "labels": ["macos-latest"],
                    "started_at": "2026-11-12T00:00:00Z",
                    "completed_at": "2026-11-12T01:00:00Z",
                }
            ],
        )
        self.assertEqual(source, "timing")
        self.assertEqual(chosen["minutes"]["linux"], 3)
        self.assertEqual(chosen["minutes"]["windows"], 2)
        self.assertEqual(chosen["minutes"]["macos"], 0)
        self.assertEqual(spend.weighted(chosen["minutes"]), 7)

    def test_self_hosted_is_not_priced_as_linux(self):
        chosen = spend.minutes_from_jobs(
            [
                {
                    "name": "local",
                    "labels": ["self-hosted", "linux"],
                    "started_at": "2026-11-12T00:00:00Z",
                    "completed_at": "2026-11-12T00:03:30Z",
                }
            ]
        )
        self.assertEqual(chosen["minutes"]["self_hosted"], 4)
        self.assertEqual(chosen["minutes"]["linux"], 0)
        self.assertEqual(spend.weighted(chosen["minutes"]), 0)


class ProfileTests(unittest.TestCase):
    def _profiles(self, files):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, text in files.items():
                (root / name).write_text(text, encoding="utf-8")
            return spend.load_profiles(root)

    def test_yaml_on_key_and_push_limits(self):
        profiles = self._profiles(
            {
                "wide.yml": "name: Wide\non:\n  push:\n  pull_request:\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - run: npm test\n",
                "main.yml": "name: Main\non:\n  push:\n    branches: [main]\n    paths:\n      - scripts/**\n  pull_request:\n    paths:\n      - scripts/**\njobs: {}\n",
                "hold.yml": "name: Hold\non:\n  pull_request_target:\nconcurrency:\n  group: hold\n  cancel-in-progress: false   # never drop a disarm behind a newer run\njobs: {}\n",
                "missing.yml": "name: Missing\non: [push]\njobs:\n  mac:\n    runs-on: macos-latest\n    steps:\n      - run: npm test\n",
                "needed.yml": "name: Needed\non:\n  pull_request:\njobs:\n  build:\n    name: Muster macOS build\n    runs-on: macos-latest\n    steps:\n      - run: xcodebuild -scheme Muster build\n",
            }
        )
        wide = profiles[".github/workflows/wide.yml"]
        self.assertTrue(wide["has_push"])
        self.assertTrue(wide["has_pull_request"])
        self.assertFalse(wide["push_main_only"])
        self.assertFalse(wide["paths_limited"])
        self.assertEqual(wide["cancel"], "missing")
        main = profiles[".github/workflows/main.yml"]
        self.assertTrue(main["push_main_only"])
        self.assertTrue(main["paths_limited"])
        self.assertEqual(profiles[".github/workflows/hold.yml"]["cancel"], "intentional-false")
        portable = profiles[".github/workflows/missing.yml"]["jobs"][0]
        self.assertIn("macos", portable["operating_systems"])
        self.assertFalse(portable["mac_signal"])
        required = profiles[".github/workflows/needed.yml"]["jobs"][0]
        self.assertTrue(required["mac_signal"])

    def test_live_workflows_parse_and_ci_macos_stays_macos(self):
        profiles = spend.load_profiles(REPO / ".github" / "workflows")
        ci = profiles[".github/workflows/ci.yml"]
        self.assertTrue(ci["push_main_only"])
        self.assertFalse(ci["paths_limited"])
        macos_jobs = [job for job in ci["jobs"] if "macos" in job["operating_systems"]]
        self.assertGreaterEqual(len(macos_jobs), 2)
        for job in macos_jobs:
            self.assertTrue(job["mac_signal"], job["names"])
        self.assertEqual(
            profiles[".github/workflows/hold-guard.yml"]["cancel"], "intentional-false"
        )
        self.assertGreater(profiles[".github/workflows/kody-first-review.yml"]["cron_per_week"], 24)
        spend_wf = profiles[".github/workflows/actions-spend.yml"]
        self.assertEqual(spend_wf["cron_per_week"], 1)
        self.assertEqual(spend_wf["cancel"], "true")
        self.assertFalse(spend_wf["has_push"])
        self.assertTrue(spend_wf["jobs"])
        self.assertTrue(all("linux" in job["operating_systems"] for job in spend_wf["jobs"]))


class CutTests(unittest.TestCase):
    def test_schedule_duplicate_macos_and_cancel(self):
        runs = [
            _run(
                id=1,
                name="Hourly",
                path=".github/workflows/hourly.yml",
                event="schedule",
                head_sha="h1",
            ),
            _run(
                id=2,
                name="Wide",
                path=".github/workflows/wide.yml",
                event="push",
                head_sha="same",
                run_started_at="2026-11-12T00:00:00Z",
                updated_at="2026-11-12T00:10:00Z",
            ),
            _run(
                id=3,
                name="Wide",
                path=".github/workflows/wide.yml",
                event="pull_request",
                head_sha="same",
                run_started_at="2026-11-12T01:00:00Z",
                updated_at="2026-11-12T01:10:00Z",
            ),
            _run(
                id=4,
                name="Portable",
                path=".github/workflows/portable.yml",
                event="pull_request",
                head_sha="p1",
            ),
            _run(
                id=5,
                name="Needed",
                path=".github/workflows/needed.yml",
                event="pull_request",
                head_sha="n1",
            ),
            _run(
                id=6,
                name="Race",
                path=".github/workflows/race.yml",
                event="push",
                head_sha="race",
                run_started_at="2026-11-12T00:00:00Z",
                updated_at="2026-11-12T00:30:00Z",
            ),
            _run(
                id=7,
                name="Race",
                path=".github/workflows/race.yml",
                event="push",
                head_sha="race",
                run_started_at="2026-11-12T00:10:00Z",
                updated_at="2026-11-12T00:40:00Z",
            ),
        ]
        measured = {
            1: _measured({"linux": 600}, [{"name": "tick", "os": "linux", "minutes": 600}]),
            2: _measured({"linux": 100}, [{"name": "test", "os": "linux", "minutes": 100}]),
            3: _measured({"linux": 80}, [{"name": "test", "os": "linux", "minutes": 80}]),
            4: _measured({"macos": 50}, [{"name": "unit", "os": "macos", "minutes": 50}]),
            5: _measured(
                {"macos": 10}, [{"name": "Muster macOS build", "os": "macos", "minutes": 10}]
            ),
            6: _measured({"linux": 30}, [{"name": "test", "os": "linux", "minutes": 30}]),
            7: _measured({"linux": 30}, [{"name": "test", "os": "linux", "minutes": 30}]),
        }
        profiles = {
            ".github/workflows/hourly.yml": {
                "cron_per_week": 168,
                "crons": ["53 * * * *"],
                "cancel": "true",
                "has_push": False,
                "has_pull_request": False,
                "push_main_only": False,
                "paths_limited": True,
                "jobs": [],
            },
            ".github/workflows/wide.yml": {
                "cron_per_week": 0,
                "crons": [],
                "cancel": "true",
                "has_push": True,
                "has_pull_request": True,
                "push_main_only": False,
                "paths_limited": False,
                "jobs": [],
            },
            ".github/workflows/portable.yml": {
                "cron_per_week": 0,
                "crons": [],
                "cancel": "true",
                "has_push": False,
                "has_pull_request": True,
                "push_main_only": False,
                "paths_limited": False,
                "jobs": [
                    {"names": ("unit",), "operating_systems": ("macos",), "mac_signal": False}
                ],
            },
            ".github/workflows/needed.yml": {
                "cron_per_week": 0,
                "crons": [],
                "cancel": "true",
                "has_push": False,
                "has_pull_request": True,
                "push_main_only": False,
                "paths_limited": False,
                "jobs": [
                    {
                        "names": ("Muster macOS build",),
                        "operating_systems": ("macos",),
                        "mac_signal": True,
                    }
                ],
            },
            ".github/workflows/race.yml": {
                "cron_per_week": 0,
                "crons": [],
                "cancel": "missing",
                "has_push": True,
                "has_pull_request": False,
                "push_main_only": True,
                "paths_limited": True,
                "jobs": [],
            },
        }
        built = spend.assemble(
            runs,
            measured,
            profiles,
            {
                "repo": "fixture-owner/Fixture-Repo",
                "start": "2026-11-05T00:00:00Z",
                "end": "2026-11-12T00:00:00Z",
                "method": "jobs",
                "truncated": False,
            },
        )
        by_kind = {cut["kind"]: cut for cut in built["cuts"]}
        self.assertEqual(by_kind["schedule"]["savings_weighted"], 575)
        self.assertEqual(by_kind["duplicate"]["savings_weighted"], 80)
        self.assertIn("Restrict push to main", by_kind["duplicate"]["detail"])
        self.assertEqual(by_kind["macos-linux"]["savings_weighted"], 450)
        self.assertEqual(by_kind["macos-linux"]["confidence"], "measured")
        self.assertEqual(by_kind["macos-paths"]["savings_weighted"], 100)
        self.assertEqual(by_kind["macos-paths"]["confidence"], "upper-bound")
        self.assertEqual(by_kind["cancel"]["savings_weighted"], 20)
        self.assertEqual(built["workflows"][0]["weighted"], 600)
        self.assertEqual(built["totals"]["macos_min"], 60)
        self.assertEqual(built["totals"]["weighted"], 600 + 180 + 500 + 100 + 60)
        self.assertIn("\u00a0 ", spend.render_markdown(built))

    def test_unmeasured_runs_keep_totals_a_lower_bound(self):
        built = spend.assemble(
            [_run(id=1), _run(id=2, head_sha="other")],
            {1: _measured({"linux": 5}, [{"name": "test", "os": "linux", "minutes": 5}])},
            {},
            {"repo": "o/r", "start": "s", "end": "e", "method": "jobs", "truncated": False},
        )
        self.assertTrue(built["partial"])
        self.assertEqual(built["totals"]["linux_min"], 5)
        self.assertEqual(built["totals"]["unmeasured_runs"], 1)
        self.assertIn("lower bound", " ".join(built["notes"]))

    def test_main_only_push_without_overlap_is_not_a_duplicate_cut(self):
        runs = [_run(id=1, event="push", head_sha="only-push")]
        profiles = {
            ".github/workflows/ci.yml": {
                "cron_per_week": 0,
                "crons": [],
                "cancel": "conditional",
                "has_push": True,
                "has_pull_request": True,
                "push_main_only": True,
                "paths_limited": True,
                "jobs": [],
            }
        }
        built = spend.assemble(
            runs,
            {1: _measured({"linux": 4})},
            profiles,
            {"repo": "o/r", "start": "s", "end": "e", "method": "timing", "truncated": False},
        )
        self.assertEqual(built["cuts"], [])


class RenderAndCollectTests(unittest.TestCase):
    def test_top_10_and_summary_do_not_keep_a_token(self):
        workflows = []
        for index in range(12):
            workflows.append(
                {
                    "path": f".github/workflows/wf-{index}.yml",
                    "name": f"Workflow {index}",
                    "runs": 1,
                    "open_runs": 0,
                    "linux_min": 12 - index,
                    "macos_min": 0,
                    "windows_min": 0,
                    "self_hosted_min": 0,
                    "other_min": 0,
                    "weighted": 12 - index,
                    "measured": 1,
                    "unmeasured": 0,
                }
            )
        document = {
            "repo": "o/r",
            "start": "s",
            "end": "e",
            "notes": ["The timing API was checked.", "Totals are measured."],
            "totals": {
                "runs": 12,
                "open_runs": 0,
                "linux_min": 1,
                "macos_min": 0,
                "windows_min": 0,
                "self_hosted_min": 0,
                "other_min": 0,
                "weighted": 1,
                "unmeasured_runs": 0,
                "timing_runs": 1,
                "job_runs": 0,
            },
            "workflows": workflows,
            "cuts": [],
        }
        document["workflows"][0]["name"] = "Workflow sample-token"
        text = spend.render_markdown(document)
        top, rest = text.split("## Every Workflow", 1)
        self.assertIn("wf-0.yml", top)
        self.assertNotIn("wf-11.yml", top)
        self.assertIn("wf-11.yml", rest)
        self.assertIn("checked.\u00a0 Totals", text)
        with tempfile.TemporaryDirectory() as tmp:
            summary = Path(tmp) / "summary.md"
            previous = os.environ.get("GITHUB_STEP_SUMMARY")
            os.environ["GITHUB_STEP_SUMMARY"] = str(summary)
            try:
                report.write_outputs(document, Path(tmp) / "out", "sample-token")
            finally:
                if previous is None:
                    os.environ.pop("GITHUB_STEP_SUMMARY", None)
                else:
                    os.environ["GITHUB_STEP_SUMMARY"] = previous
            written = (Path(tmp) / "out" / "report.json").read_text(encoding="utf-8")
            summary_text = summary.read_text(encoding="utf-8")
            self.assertIn("Actions Spend Report", summary_text)
            self.assertNotIn("sample-token", written)
            self.assertNotIn("sample-token", summary_text)
            self.assertIn("[redacted]", written)

    def test_collect_dedupes_and_drops_the_boundary(self):
        start = datetime(2026, 11, 12, tzinfo=UTC)
        end = start + timedelta(hours=2)

        def fetch(slice_start, slice_end):
            return [
                _run(id=5, created_at="2026-11-12T00:10:00Z"),
                _run(id=5, created_at="2026-11-12T00:10:00Z"),
                _run(id=6, created_at="2026-11-12T02:00:00Z"),
                _run(id=7, created_at="2026-11-11T23:00:00Z"),
            ], False

        runs, truncated = spend.collect_runs(fetch, start, end, slice_hours=2)
        self.assertFalse(truncated)
        self.assertEqual([item["id"] for item in runs], [5])

    def test_capped_window_splits(self):
        def fetch(slice_start, slice_end):
            if slice_end - slice_start > timedelta(hours=1):
                return [_run(id=1)], True
            return [
                _run(id=int(slice_start.hour) + 10, created_at=spend.format_time(slice_start))
            ], False

        start = datetime(2026, 11, 12, tzinfo=UTC)
        batch, capped = spend.list_range(fetch, start, start + timedelta(hours=2))
        self.assertFalse(capped)
        self.assertEqual(sorted(item["id"] for item in batch), [10, 11])

    def test_client_repr_hides_the_token(self):
        client = github_api.Client("sample-token")
        self.assertNotIn("sample-token", repr(client))
        self.assertEqual(client.redact("sample-token leaked"), "[redacted] leaked")

    def test_lookup_order_finishes_macos_workflows_first(self):
        runs = [
            _run(id=1, path=".github/workflows/linux.yml", created_at="2026-11-12T00:00:00Z"),
            _run(id=2, path=".github/workflows/linux.yml", created_at="2026-11-12T01:00:00Z"),
            _run(id=3, path=".github/workflows/ci.yml", created_at="2026-11-12T02:00:00Z"),
            _run(id=4, path=".github/workflows/small.yml", created_at="2026-11-12T03:00:00Z"),
        ]
        profiles = {
            ".github/workflows/ci.yml": {"jobs": [{"operating_systems": ("macos", "linux")}]},
            ".github/workflows/linux.yml": {"jobs": [{"operating_systems": ("linux",)}]},
            ".github/workflows/small.yml": {"jobs": [{"operating_systems": ("linux",)}]},
        }
        order = report._order_lookups(runs, profiles)
        self.assertEqual([item["id"] for item in order], [3, 1, 2, 4])

    def test_refresh_swaps_an_expired_token_once_per_window(self):
        issued = []

        def refresh():
            issued.append("second-token")
            return "second-token"

        client = github_api.Client("first-token", clock=lambda: 1000, refresh=refresh)
        self.assertTrue(client._refresh_token())
        self.assertFalse(client._refresh_token())
        self.assertEqual(issued, ["second-token"])
        self.assertNotIn("second-token", repr(client))

    def test_default_budget_allows_one_long_wait(self):
        sleeps = []
        client = github_api.Client("sample-token", sleep=sleeps.append, clock=lambda: 1000)
        client.remaining = 1
        client.reset_at = 1030
        client._respect_budget()
        self.assertEqual(sleeps, [32.0])
        client.remaining = 1
        client.reset_at = 1100
        with self.assertRaises(github_api.RateLimited):
            client._respect_budget()
        self.assertTrue(client.stop)


class WorkflowFileTests(unittest.TestCase):
    def test_weekly_workflow_is_read_only(self):
        text = (REPO / ".github" / "workflows" / "actions-spend.yml").read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch", text)
        self.assertIn('cron: "17 13 * * 1"', text)
        self.assertIn("actions: read", text)
        self.assertIn("contents: read", text)
        self.assertNotIn("contents: write", text)
        self.assertNotIn("pull-requests: write", text)
        self.assertIn("runs-on: ubuntu-latest", text)
        self.assertNotIn("macos-latest", text)
        self.assertIn("actions/upload-artifact@", text)
        self.assertIn("scripts/actions-spend/report.py", text)
        loaded = json.dumps(text)
        self.assertNotIn("ghp_", loaded)



class _Response:
    def __init__(self, body):
        self._body = body
        self.headers = {}

    def read(self, limit=-1):
        return self._body if limit < 0 else self._body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ReviewFollowUpTests(unittest.TestCase):
    """Kody findings on #2093."""

    def test_redirect_off_api_host_is_refused(self):
        handler = github_api._SameHostRedirects()
        request = github_api.urllib.request.Request(
            "https://api.github.com/repos/a/b", headers={"Authorization": "Bearer x"}
        )
        with self.assertRaises(github_api.urllib.error.HTTPError):
            handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/x")
        same = handler.redirect_request(
            request, None, 301, "Moved", {}, "https://api.github.com/repositories/1"
        )
        self.assertEqual(same.full_url, "https://api.github.com/repositories/1")

    def test_oversized_response_is_rejected(self):
        body = b"[" + b" " * (github_api.MAX_RESPONSE_BYTES + 1) + b"]"
        with self.assertRaises(ValueError):
            github_api._read_json(_Response(body))

    def _client_with(self, opener):
        original = github_api._OPENER
        github_api._OPENER = opener
        self.addCleanup(setattr, github_api, "_OPENER", original)
        return github_api.Client("t", sleep=lambda _s: None)

    def test_transport_errors_become_api_errors(self):
        class Opener:
            def open(self, request, timeout=None):
                raise ConnectionResetError("reset")

        with self.assertRaises(github_api.ApiError) as ctx:
            self._client_with(Opener()).get("rate_limit")
        self.assertEqual(ctx.exception.status, 0)

    def test_unparseable_body_becomes_api_error(self):
        class Opener:
            def open(self, request, timeout=None):
                return _Response(b"<html>not json</html>")

        with self.assertRaises(github_api.ApiError):
            self._client_with(Opener()).get("rate_limit")

    def test_malformed_cache_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.jsonl"
            path.write_text('{"id": 1, "result": {"ok": 1}}\n{torn\n{"result": 2}\n[]\n', encoding="utf-8")
            self.assertEqual(report._load_cache(path), {1: {"ok": 1}})

    def test_cache_save_prunes_ids_outside_the_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.jsonl"
            report._save_cache(path, {1: {"a": 1}, 2: {"b": 2}}, keep={2})
            self.assertEqual(report._load_cache(path), {2: {"b": 2}})

    def test_symlinked_out_dir_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "elsewhere"
            target.mkdir()
            link = Path(tmp) / "out"
            link.symlink_to(target)
            with self.assertRaises(SystemExit):
                report.write_outputs({}, link, "t")

    def test_non_positive_slice_hours_are_rejected(self):
        start = datetime(2026, 11, 1, tzinfo=UTC)
        with self.assertRaises(ValueError):
            list(spend.iter_slices(start, start + timedelta(hours=1), 0))
        with self.assertRaises(SystemExit):
            report.parse_args(["--repo", "a/b", "--out-dir", "x", "--slice-hours", "0"])

    def test_malformed_timing_falls_back_to_jobs(self):
        good = {"UBUNTU": {"total_ms": 120000, "job_runs": [{"job_id": 1, "duration_ms": 120000}]}}
        self.assertIsNotNone(spend.minutes_from_timing({"billable": good}))
        for bad in (
            {**good, "MACOS": "oops"},
            {"UBUNTU": {"job_runs": [{"job_id": 1, "duration_ms": "12"}]}},
            {"UBUNTU": {"job_runs": [{"job_id": 1, "duration_ms": -5}]}},
            {"UBUNTU": {"job_runs": "nope"}},
            {"UBUNTU": {"total_ms": "120000"}},
        ):
            self.assertIsNone(spend.minutes_from_timing({"billable": bad}), bad)

    def test_numeric_cancel_in_progress_counts(self):
        self.assertEqual(spend._cancel_mode({"concurrency": {"cancel-in-progress": 1}}, ""), "true")
        self.assertEqual(spend._cancel_mode({"concurrency": {"cancel-in-progress": True}}, ""), "true")

    def test_remote_or_pinned_callee_is_not_resolved_locally(self):
        docs = {"reusable.yml": {"jobs": {}}}
        self.assertIs(spend._local_callee("./.github/workflows/reusable.yml", docs), docs["reusable.yml"])
        self.assertIsNone(spend._local_callee("other/repo/.github/workflows/reusable.yml@main", docs))
        self.assertIsNone(spend._local_callee("owner/repo/.github/workflows/reusable.yml@0123abc", docs))

    def test_empty_sha_rows_do_not_count_as_duplicates(self):
        rows = [
            {"event": "push", "sha": "", "weighted": 10},
            {"event": "pull_request", "sha": "", "weighted": 10},
            {"event": "push", "sha": "abc", "weighted": 4},
            {"event": "pull_request", "sha": "abc", "weighted": 6},
        ]
        self.assertEqual(spend._duplicate_savings(rows), 4)

    def test_timing_mode_keeps_both_macos_proposals(self):
        profile = {
            "jobs": [
                {"names": ("lint",), "operating_systems": ("macos",), "mac_signal": False},
                {"names": ("xcode",), "operating_systems": ("macos",), "mac_signal": True},
            ]
        }
        portable, _pn, required, _rn = spend._portable_macos_minutes(profile, {}, 100)
        self.assertEqual((portable, required), (50, 50))

    def test_one_failing_run_does_not_abort_the_batch(self):
        runs = [
            {"id": index, "status": "completed", "conclusion": "success", "path": "w.yml"}
            for index in range(1, 13)
        ]
        original = report._measure_one

        def fake(client, repo, run, use_timing):
            if run["id"] == 10:
                raise KeyError("boom")
            return {"minutes": spend.empty_minutes(), "jobs": [], "source": "jobs"}

        report._measure_one = fake
        self.addCleanup(setattr, report, "_measure_one", original)

        class Client:
            stop = False

        with tempfile.TemporaryDirectory() as tmp:
            measured, _method, partial = report.measure(
                Client(), "a/b", runs, Path(tmp) / "c.jsonl", 2
            )
        self.assertEqual(len(measured), 11)
        self.assertTrue(partial)


if __name__ == "__main__":
    unittest.main()
