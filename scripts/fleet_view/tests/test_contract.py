"""Contract tests:  pinned schema, verdict math and the secret ban (AFC-394)."""

from __future__ import annotations

import json
import unittest

from fleet_view import SCHEMA_VERSION, contract
from fleet_view.types import ClaimRecord, FleetReport, LaneRecord, PullRequestRecord, Section


def ok_report() -> FleetReport:
    return FleetReport(
        generated_at="2026-10-20T12:00:00Z",
        lanes=Section(state="OK", reason="1 lane(s)", items=(
            LaneRecord("AI-Fleet-Coordinator", "muse-code-x", "MC", "muse-code/x", 0, 60, "OK"),)),
        pull_requests=Section(state="OK", reason="1 open PR(s)", items=(
            PullRequestRecord("Simple-With-Us/AI-Fleet-Coordinator", 757, "guards", "opencode/x", "OPENCODE", "ready", "OK"),)),
        claims=Section(state="OK", reason="1 claim(s)", items=(
            ClaimRecord("AFC-394", "Fleet View", "MC", 60, False),)),
    )


class ContractTests(unittest.TestCase):
    def test_schema_version_is_pinned(self):
        self.assertEqual(SCHEMA_VERSION, 1)
        envelope = contract.build_envelope(ok_report())
        self.assertEqual(envelope["schemaVersion"], 1)
        self.assertEqual(envelope["summary"]["overallVerdict"], "PASS")

    def test_unknown_section_never_verdicts_pass(self):
        report = ok_report()
        report.claims = Section(state="UNKNOWN", reason="board failed")
        envelope = contract.build_envelope(report)
        self.assertEqual(envelope["summary"]["overallVerdict"], "UNKNOWN")
        self.assertEqual(contract.overall_verdict(report), "UNKNOWN")

    def test_envelope_carries_counts_and_no_secret_shaped_field(self):
        envelope = contract.build_envelope(ok_report())
        self.assertEqual(envelope["requestCounts"]["githubMax"], 60)
        self.assertEqual(envelope["requestCounts"]["linearMax"], 30)
        text = json.dumps(envelope).lower()
        for banned in ("api_key", "apikey", "token", "secret", "password", "credential"):
            self.assertNotIn(banned, text)


class PackagingTests(unittest.TestCase):
    """A copy of just this package imports under isolated Python (``-I``).

    This is what keeps fleet_view stdlib-only, and it catches a spelling
    that only the newest interpreter has, which would break on the fleet's
    older Macs and nowhere else.
    """

    def test_a_copy_of_just_this_package_runs_under_isolated_python(self):
        import os
        import pathlib
        import shutil
        import subprocess
        import sys
        import tempfile

        scripts = pathlib.Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as temp:
            dest = pathlib.Path(temp) / "stable"
            shutil.copytree(
                scripts / "fleet_view",
                dest / "fleet_view",
                ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc"),
            )
            env = dict(os.environ)
            for var in ("PYTHONPATH", "PYTHONHOME"):
                env.pop(var, None)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-c",
                    "import sys; sys.path.insert(0, sys.argv[1]);"
                    "import fleet_view;"
                    "from fleet_view import collectors, contract, types;"
                    "assert fleet_view.__file__.startswith(sys.argv[1]), fleet_view.__file__;"
                    "print(fleet_view.SCHEMA_VERSION)",
                    str(dest),
                ],
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(
                completed.returncode,
                0,
                "the isolated import failed: %s" % completed.stderr.decode("utf-8", "replace"),
            )
            self.assertEqual(completed.stdout.decode("utf-8", "replace").strip(), "1")


if __name__ == "__main__":
    unittest.main()
