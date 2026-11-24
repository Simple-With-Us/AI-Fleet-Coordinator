"""Scanner tests using recorded fixtures (no live API)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import github
import scanner
from fake_github import FixtureGitHub


class DenyRepoGitHub(FixtureGitHub):
    def __init__(self, *, status: int, repo: str) -> None:
        super().__init__()
        self._status = status
        self._repo = repo

    def get_json(self, path: str, *, params=None):
        if path == f"/repos/fixture-org/{self._repo}":
            raise github.GitHubError("denied", status=self._status)
        return super().get_json(path, params=params)


class ScannerTests(unittest.TestCase):
    def test_streak_skips_cancelled_and_stops_on_success(self) -> None:
        runs = [
            {"conclusion": "failure", "id": 1},
            {"conclusion": "cancelled", "id": 2},
            {"conclusion": "failure", "id": 3},
            {"conclusion": "success", "id": 4},
        ]
        length, streak = scanner.stream_from_runs(runs)
        self.assertEqual(length, 2)
        self.assertEqual([r["id"] for r in stream], [1, 3])

    def test_fixture_scan_one_repo(self) -> None:
        client = FixtureGitHub()
        report = scanner.build_report(
            client,
            org="fixture-org",
            repos=["fixture-repo"],
            max_runs_per_workflow=10,
        )
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["repos_scanned"], 1)
        self.assertEqual(report["red_count"], 1)
        row = report["red_workflows"][0]
        self.assertEqual(row["repo"], "fixture-repo")
        self.assertEqual(row["workflow_name"], "CI")
        self.assertEqual(row["stream_length"], 2)
        self.assertEqual(row["first_red_commit_sha"], "ccc333")
        self.assertEqual(row["latest_commit_sha"], "aaa111")
        self.assertEqual(row["failing_job_name"], "test")
        self.assertEqual(row["failing_step_name"], "Run unittests")
        # Jobs are fetched only for the red workflow's latest run.
        job_paths = [c for c in client.calls if c.endswith("/jobs")]
        self.assertEqual(len(job_paths), 1)
        self.assertIn("/actions/runs/101/jobs", job_paths[0])

    def test_scan_continues_on_404(self) -> None:
        client = DenyRepoGitHub(status=404, repo="missing-repo")
        report = scanner.build_report(
            client,
            org="fixture-org",
            repos=["missing-repo", "fixture-repo"],
            max_runs_per_workflow=10,
        )
        self.assertEqual(report["repos_scanned"], 2)
        self.assertTrue(any("404" in err for err in report["errors"]))
        self.assertGreaterEqual(report["red_count"], 0)

    def test_scan_continues_on_403(self) -> None:
        client = DenyRepoGitHub(status=403, repo="forbidden-repo")
        report = scanner.build_report(
            client,
            org="fixture-org",
            repos=["forbidden-repo"],
            max_runs_per_workflow=10,
        )
        self.assertEqual(report["repos_scanned"], 1)
        self.assertTrue(any("403" in err for err in report["errors"]))

    def test_markdown_renders_table(self) -> None:
        report = {
            "org": "fixture-org",
            "generated_at": "2026-11-12T00:00:00Z",
            "repos_scanned": 1,
            "workflows_scanned": 1,
            "red_count": 1,
            "red_workflows": [
                {
                    "repo": "fixture-repo",
                    "workflow_name": "CI",
                    "workflow_path": ".github/workflows/ci.yml",
                    "streak_length": 2,
                    "latest_run_url": "https://example.com/run/101",
                    "failing_job_name": "test",
                    "failing_step_name": "Run unittests",
                }
            ],
        }
        md = scanner.render_markdown(report)
        self.assertIn("Red workflows: 1", md)
        self.assertIn("Run unittests", md)


class ParseOnTriggersTests(unittest.TestCase):
    def test_block_triggers(self) -> None:
        content = "name: CI\non:\n  push:\n    branches: [main]\n  schedule:\n    - cron: '0 0 * * *'\n  workflow_dispatch:\njobs:\n  build: {}\n"
        self.assertEqual(
            scanner.parse_on_triggers(content), {"push", "schedule", "workflow_dispatch"}
        )

    def test_inline_list_triggers(self) -> None:
        self.assertEqual(scanner.parse_on_triggers("on: [push, schedule]\njobs: {}\n"), {"push", "schedule"})

    def test_workflow_call_only_is_phantom_shape(self) -> None:
        triggers = scanner.parse_on_triggers("name: R\non:\n  workflow_call:\njobs:\n  caller: {}\n")
        self.assertEqual(triggers, {"workflow_call"})
        self.assertTrue(triggers.isdisjoint(scanner.REPORTABLE_TRIGGERS))

    def test_pull_request_only_is_phantom_shape(self) -> None:
        triggers = scanner.parse_on_triggers('on:\n  pull_request_target:\n    types: [labeled]\njobs: {}\n')
        self.assertEqual(triggers, {"pull_request_target"})
        self.assertTrue(triggers.isdisjoint(scanner.REPORTABLE_TRIGGERS))

    def test_nested_keys_are_not_triggers(self) -> None:
        content = "on:\n  push:\n    branches:\n      - main\n  pull_request:\njobs: {}\n"
        self.assertEqual(scanner.parse_on_triggers(content), {"push", "pull_request"})

    def test_comments_and_blank_lines_do_not_end_the_block(self) -> None:
        content = "on:\n  # scheduled nightly\n\n  schedule:\n    - cron: '0 0 * * *'\njobs: {}\n"
        self.assertEqual(scanner.parse_on_triggers(content), {"schedule"})

    def test_missing_on_block_yields_empty_set(self) -> None:
        self.assertEqual(scanner.parse_on_triggers("name: X\njobs:\n  a: {}\n"), set())


class PhantomWorkflowTests(unittest.TestCase):
    def test_phantom_red_workflow_is_excluded_and_reportable_kept(self) -> None:
        client = FixtureGitHub()
        report = scanner.build_report(
            client,
            org="fixture-org",
            repos=["phantom-repo"],
            max_runs_per_workflow=10,
        )
        self.assertEqual(report["red_count"], 1)
        row = report["red_workflows"][0]
        self.assertEqual(row["workflow_name"], "Push CI")
        self.assertEqual(row["stream_length"], 1)
        # The phantom workflow's jobs were never fetched: it was skipped
        # before the failing-job lookup, not filtered afterwards.
        job_paths = [c for c in client.calls if c.endswith("/jobs")]
        self.assertEqual(len(job_paths), 1)
        self.assertIn("/actions/runs/9102/jobs", job_paths[0])

    def test_unreadable_workflow_file_fails_open(self) -> None:
        class DenyContents(FixtureGitHub):
            def get_text_file(self, repo_path: str, file_path: str, *, ref: str) -> str:
                if "reusable.yml" in file_path:
                    raise github.GitHubError("contents denied", status=403)
                return super().get_text_file(repo_path, file_path, ref=ref)

        report = scanner.build_report(
            DenyContents(),
            org="fixture-org",
            repos=["phantom-repo"],
            max_runs_per_workflow=10,
        )
        # Fail open: the unreadable file's red row stays visible, with the
        # read failure recorded in errors.
        names = {row["workflow_name"] for row in report["red_workflows"]}
        self.assertIn("Reusable only", names)
        self.assertTrue(any("phantom-trigger check" in err for err in report["errors"]))


if __name__ == "__main__":
    unittest.main()
