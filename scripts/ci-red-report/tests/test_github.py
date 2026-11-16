"""Unit tests for github helpers."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import github


class GitHubHelperTests(unittest.TestCase):
    def test_classify(self) -> None:
        self.assertEqual(github.classify_run_conclusion("failure"), "fail")
        self.assertEqual(github.classify_run_conclusion("success"), "success")
        self.assertEqual(github.classify_run_conclusion("skipped"), "ignore")
        self.assertEqual(github.classify_run_conclusion("cancelled"), "ignore")
        self.assertEqual(github.classify_run_conclusion("neutral"), "ignore")

    def test_pick_failing_step(self) -> None:
        payload = {
            "jobs": [
                {
                    "name": "lint",
                    "conclusion": "success",
                    "steps": [],
                },
                {
                    "name": "test",
                    "conclusion": "failure",
                    "steps": [
                        {"name": "Setup", "conclusion": "success"},
                        {"name": "Pytest", "conclusion": "failure"},
                    ],
                },
            ]
        }
        self.assertEqual(github.pick_failing_job_step(payload), ("test", "Pytest"))


if __name__ == "__main__":
    unittest.main()
