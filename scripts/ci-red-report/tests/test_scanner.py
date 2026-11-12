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
        if path == f"/repos/Simple-With-Us/{self._repo}":
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
        length, streak = scanner.streak_from_runs(runs)
        self.assertEqual(length, 2)
        self.assertEqual([r["id"] for r in streak], [1, 3])

    def test_fixture_scan_one_repo(self) -> None:
        client = FixtureGitHub()
        report = scanner.build_report(
            client,
            org="Simple-With-Us",
            repos=["AI-Fleet-Coordinator"],
            max_runs_per_workflow=10,
        )
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["repos_scanned"], 1)
        self.assertEqual(report["red_count"], 1)
        row = report["red_workflows"][0]
        self.assertEqual(row["repo"], "AI-Fleet-Coordinator")
        self.assertEqual(row["workflow_name"], "CI")
        self.assertEqual(row["streak_length"], 2)
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
            org="Simple-With-Us",
            repos=["missing-repo", "AI-Fleet-Coordinator"],
            max_runs_per_workflow=10,
        )
        self.assertEqual(report["repos_scanned"], 2)
        self.assertTrue(any("404" in err for err in report["errors"]))
        self.assertGreaterEqual(report["red_count"], 0)

    def test_scan_continues_on_403(self) -> None:
        client = DenyRepoGitHub(status=403, repo="forbidden-repo")
        report = scanner.build_report(
            client,
            org="Simple-With-Us",
            repos=["forbidden-repo"],
            max_runs_per_workflow=10,
        )
        self.assertEqual(report["repos_scanned"], 1)
        self.assertTrue(any("403" in err for err in report["errors"]))

    def test_markdown_renders_table(self) -> None:
        report = {
            "org": "Simple-With-Us",
            "generated_at": "2026-11-12T00:00:00Z",
            "repos_scanned": 1,
            "workflows_scanned": 1,
            "red_count": 1,
            "red_workflows": [
                {
                    "repo": "AI-Fleet-Coordinator",
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


if __name__ == "__main__":
    unittest.main()
