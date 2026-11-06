"""The Mac's Python contract (AFC-616):  this package imports and runs on 3.9.

Muster runs ``/usr/bin/python3 -m fleet_view status --json``, and the repo says that
interpreter is 3.9 on the Mac (ruff.toml).  ``datetime.UTC`` is 3.11+, so one import
of it took the whole Apps & Work page down there while every test passed on a newer
Python.  CI runs this suite twice:  on the job's main interpreter, where the source
checks below catch a newer spelling early, and on a real Python 3.9, where the
end-to-end runs prove it.
"""

from __future__ import annotations

import ast
import json
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest

from fleet_view import collectors

SCRIPTS = pathlib.Path(__file__).resolve().parents[2]
REPO = SCRIPTS.parent
PACKAGE = SCRIPTS / "fleet_view"

# Standard-library names newer than 3.9 that a module-level import would die on.
NEWER_THAN_39 = {
    "datetime": {"UTC"},
    "enum": {"StrEnum", "ReprEnum"},
    "typing": {"Self", "TypeAlias", "ParamSpec", "TypeGuard", "Never", "LiteralString"},
}
NEWER_MODULES = {"tomllib"}

GH_PR_ROW = (
    '[{"number": 7, "title": "A PR", "headRefName": "claude/x", "mergeable": "MERGEABLE",'
    ' "mergeStateStatus": "CLEAN", "reviewDecision": "", "autoMergeRequest": {"m": 1},'
    ' "statusCheckRollup": []}]'
)
# The shape Linear's updatedAt has in `simple-board list --json`.
BOARD_ROW = (
    '[{"identifier": "FIX-1", "title": "Fixture claim", "labels": ["claimed:CLAUDE"],'
    ' "updated_at": "2026-10-20T12:00:00.123Z"}]'
)
REGISTRY = {
    "owner": "example",
    "apps": [{"repo": "Fixture-Repo"}],
    "seats": [{"tag": "CLAUDE", "worktreeSuffix": "claude", "branchPrefixes": ["claude/"]}],
}


def _sources():
    return sorted(PACKAGE.rglob("*.py"))


class SourceContractTests(unittest.TestCase):
    def test_every_file_parses_as_python_39(self):
        for path in _sources():
            with self.subTest(path=path.name):
                ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 9))

    def test_no_file_imports_a_name_newer_than_39(self):
        for path in _sources():
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module in NEWER_THAN_39:
                    bad = NEWER_THAN_39[node.module] & {alias.name for alias in node.names}
                    self.assertFalse(bad, "%s imports %s from %s" % (path.name, bad, node.module))
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn(node.module, NEWER_MODULES, path.name)
                if isinstance(node, ast.Import):
                    names = {alias.name for alias in node.names}
                    self.assertFalse(names & NEWER_MODULES, path.name)
                if isinstance(node, ast.Attribute) and node.attr == "UTC":
                    self.assertNotEqual(
                        getattr(node.value, "id", ""),
                        "datetime",
                        "%s uses datetime.UTC, which is 3.11+" % path.name,
                    )

    def test_ruff_keeps_the_package_on_the_39_target(self):
        text = (REPO / "ruff.toml").read_text(encoding="utf-8")
        self.assertIn('"scripts/fleet_view/**" = "py39"', text)
        self.assertIn('"scripts/fleet_view/**" = ["UP017"]', text)

    def test_ci_runs_this_suite_on_a_real_python_39(self):
        text = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("python-version: '3.9'", text)
        self.assertIn('"$PY39" -m unittest discover -s fleet_view/tests -t . -v', text)


class TimestampTests(unittest.TestCase):
    def test_the_stamps_the_collector_reads_parse_on_this_interpreter(self):
        for stamp in (
            "2026-10-20T12:00:00Z",
            "2026-10-20T12:00:00.123Z",
            "2026-10-20T12:00:00+00:00",
            "2026-10-20T12:00:00",
        ):
            with self.subTest(stamp=stamp):
                parsed = collectors.parse_iso(stamp)
                self.assertIsNotNone(parsed.tzinfo)
                self.assertEqual(parsed.utcoffset().total_seconds(), 0)


class EndToEndTests(unittest.TestCase):
    """The command Muster runs, on this interpreter, with stubs for gh and simple-board."""

    def _stub(self, directory, name, body):
        path = pathlib.Path(directory) / name
        path.write_text("#!/bin/sh\n%s\n" % body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)

    def _run(self, home, search_path):
        home = pathlib.Path(home)
        lanes = home / "lanes"
        lane = lanes / "Fixture-Repo" / "claude-x"
        lane.mkdir(parents=True)
        # A real checkout, so the real git's output is what the collector parses.
        subprocess.run(
            ["git", "init", "-q", str(lane)], check=True, capture_output=True, timeout=60
        )
        registry = home / "registry.json"
        registry.write_text(json.dumps(REGISTRY), encoding="utf-8")
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "fleet_view",
                "--lanes-root",
                str(lanes),
                "--registry",
                str(registry),
                "status",
                "--json",
            ],
            cwd=str(SCRIPTS),
            env={"HOME": str(home), "PATH": search_path},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )

    def test_status_exits_zero_and_prints_the_envelope(self):
        with tempfile.TemporaryDirectory() as temp:
            stubs = pathlib.Path(temp) / "bin"
            stubs.mkdir()
            self._stub(stubs, "gh", "cat <<'JSON'\n%s\nJSON" % GH_PR_ROW)
            self._stub(stubs, "simple-board", "cat <<'JSON'\n%s\nJSON" % BOARD_ROW)
            done = self._run(temp, "%s:/usr/bin:/bin" % stubs)
        self.assertEqual(done.returncode, 0, done.stderr)
        envelope = json.loads(done.stdout)
        self.assertEqual(envelope["schemaVersion"], 1)
        self.assertEqual(envelope["summary"]["overallVerdict"], "PASS")
        self.assertRegex(envelope["generatedAt"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        lane = envelope["sections"]["lanes"]["items"][0]
        self.assertEqual(lane["seat"], "CLAUDE")
        self.assertEqual(lane["state"], "OK", lane)
        self.assertTrue(lane["branch"], lane)
        self.assertEqual(lane["lastCommitSeconds"], -1, "a checkout with no commit has no age")
        self.assertIn(envelope["host"], ("mac", "server"))
        self.assertEqual(lane["host"], envelope["host"])
        self.assertEqual(envelope["sections"]["pullRequests"]["items"][0]["number"], 7)
        claim = envelope["sections"]["claims"]["items"][0]
        self.assertEqual(claim["identifier"], "FIX-1")
        self.assertGreater(claim["ageSeconds"], 0, "a Linear stamp must parse, not read as -1")

    def _assert_unknown(self, done, pr_reason, claims_reason):
        self.assertEqual(done.returncode, 3, done.stderr)
        envelope = json.loads(done.stdout)
        self.assertEqual(envelope["summary"]["overallVerdict"], "UNKNOWN")
        pull_requests = envelope["sections"]["pullRequests"]
        claims = envelope["sections"]["claims"]
        self.assertEqual(pull_requests["state"], "UNKNOWN")
        self.assertEqual(claims["state"], "UNKNOWN")
        self.assertIn(pr_reason, pull_requests["reason"])
        self.assertIn(claims_reason, claims["reason"])

    def test_failing_tools_read_unknown_and_exit_three(self):
        with tempfile.TemporaryDirectory() as temp:
            stubs = pathlib.Path(temp) / "bin"
            stubs.mkdir()
            self._stub(stubs, "gh", "exit 1")
            self._stub(stubs, "simple-board", "exit 1")
            done = self._run(temp, "%s:/usr/bin:/bin" % stubs)
        self._assert_unknown(done, "gh exited 1", "simple-board exited 1")

    def test_missing_tools_read_unknown_and_exit_three(self):
        # A search path holding only an empty folder, because a runner's own /usr/bin has gh.
        with tempfile.TemporaryDirectory() as temp:
            empty = pathlib.Path(temp) / "empty"
            empty.mkdir()
            done = self._run(temp, str(empty))
        self._assert_unknown(done, "gh failed", "simple-board failed")


if __name__ == "__main__":
    unittest.main()
