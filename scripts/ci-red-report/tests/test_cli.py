"""CLI exit-code tests."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_MAIN = Path(__file__).resolve().parents[1] / "__main__.py"
_spec = importlib.util.spec_from_file_location("ci_red_report_main", _MAIN)
assert _spec and _spec.loader
cli = importlib.util.module_from_spec(_spec)
sys.modules["ci_red_report_main"] = cli
_spec.loader.exec_module(cli)


class CliTests(unittest.TestCase):
    def test_fail_if_streak_ge(self) -> None:
        fake_report = {
            "schema_version": 1,
            "red_workflows": [{"streak_length": 5}],
        }
        with mock.patch.object(cli, "build_report", return_value=fake_report):
            with mock.patch.object(cli, "GitHubClient"):
                with tempfile.TemporaryDirectory() as tmp:
                    jpath = Path(tmp) / "out.json"
                    mpath = Path(tmp) / "out.md"
                    with mock.patch.dict("os.environ", {"GITHUB_TOKEN": "ghs_fixture"}):
                        code = cli.main(
                            [
                                "--org",
                                "Simple-With-Us",
                                "--repo",
                                "AI-Fleet-Coordinator",
                                "--json-out",
                                str(jpath),
                                "--markdown-out",
                                str(mpath),
                                "--fail-if-streak-ge",
                                "3",
                            ]
                        )
                        self.assertEqual(code, 1)
                        self.assertTrue(jpath.is_file())
                        loaded = json.loads(jpath.read_text(encoding="utf-8"))
                        self.assertEqual(loaded["red_workflows"][0]["streak_length"], 5)


if __name__ == "__main__":
    unittest.main()
