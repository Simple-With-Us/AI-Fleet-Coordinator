#!/usr/bin/env python3
"""Tests for scripts/check-fleet-activity-main-artifacts.py."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "check-fleet-activity-main-artifacts.py"


class CheckFleetActivityMainArtifactsTests(unittest.TestCase):
    def test_clean_tree_passes(self) -> None:
        root = Path(__file__).resolve().parent.parent
        done = subprocess.run(
            ["python3", str(SCRIPT)],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(done.returncode, 0, msg=done.stdout + done.stderr)

    def test_tracked_index_html_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "test@example.com"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "test"],
                cwd=root,
                check=True,
            )
            (root / "site").mkdir()
            (root / "site" / "index.html").write_text("<html></html>\n")
            subprocess.run(["git", "add", "site/index.html"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-q", "-m", "fixture"], cwd=root, check=True)
            done = subprocess.run(
                ["python3", str(SCRIPT), "--root", str(root)],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(done.returncode, 1)
            self.assertIn("site/index.html", done.stdout)


if __name__ == "__main__":
    unittest.main()
