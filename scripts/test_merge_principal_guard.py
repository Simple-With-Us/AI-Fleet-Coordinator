#!/usr/bin/env python3
"""Exercise the actual principal guard without external writes."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_auto_merge_reusable import step_script


class PrincipalGuard(unittest.TestCase):
    def run_guard(self, expected, actual, fail=False):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gh = root / "gh"
            gh.write_text(
                '#!/bin/sh\n[ "$FAKE_FAIL" = 1 ] && exit 1\nprintf "%s\\n" "$FAKE_ACTOR"\n'
            )
            gh.chmod(0o755)
            env = dict(
                os.environ,
                PATH=f"{root}:{os.environ['PATH']}",
                EXPECTED_MERGE_ACTOR=expected,
                FAKE_ACTOR=actual,
                FAKE_FAIL="1" if fail else "0",
            )
            return subprocess.run(
                ["bash", "-c", step_script("Require the configured bot principal before arming")],
                env=env,
                capture_output=True,
                text=True,
            )

    def test_exact_bot(self):
        self.assertEqual(self.run_guard("swu-wise[bot]", "swu-wise[bot]").returncode, 0)

    def test_personal_expected(self):
        self.assertNotEqual(self.run_guard("jaywedgeworth22", "jaywedgeworth22").returncode, 0)

    def test_personal_actual(self):
        self.assertNotEqual(self.run_guard("swu-wise[bot]", "jaywedgeworth22").returncode, 0)

    def test_wrong_bot(self):
        self.assertNotEqual(self.run_guard("swu-wise[bot]", "swu-echo[bot]").returncode, 0)

    def test_missing_expected(self):
        self.assertNotEqual(self.run_guard("", "swu-wise[bot]").returncode, 0)

    def test_missing_actual(self):
        self.assertNotEqual(self.run_guard("swu-wise[bot]", "").returncode, 0)

    def test_unverifiable(self):
        self.assertNotEqual(self.run_guard("swu-wise[bot]", "swu-wise[bot]", True).returncode, 0)


class DisarmPrincipal(unittest.TestCase):
    def run_disarm(self, actor="github-actions[bot]", mode="ok"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            gh = root / "gh"
            gh.write_text("""#!/bin/bash
printf '%s\\n' "$*" >> "$CALLS"
case "$*" in
  api*) [ "$MODE" = api_failure ] && exit 1; printf '%s\\n' "$ACTOR" ;;
  'pr view'*)
    if [ "$MODE" = off ] || [ -f "$DONE" ]; then echo off
    elif [ "$MODE" = unreadable ]; then exit 1
    else echo armed; fi ;;
  'pr merge'*)
    [ "$MODE" = denied ] && exit 1
    [ "$MODE" = unreadable ] && exit 1
    touch "$DONE"
    [ "$MODE" = response_lost ] && exit 1
    exit 0 ;;
  *) exit 1 ;;
esac
""")
            gh.chmod(0o755)
            env = dict(
                os.environ,
                PATH=f"{root}:{os.environ['PATH']}",
                ACTOR=actor,
                MODE=mode,
                CALLS=str(root / "calls"),
                DONE=str(root / "done"),
                GH_TOKEN="repository-token",
                PR_URL="https://github.com/example/repo/pull/1",
                REASON="hold",
            )
            result = subprocess.run(
                [
                    "bash",
                    "-c",
                    step_script(
                        "Disable any armed auto-merge (inline port of disable-pr-automerge.sh)"
                    ),
                ],
                env=env,
                capture_output=True,
                text=True,
            )
            return result.returncode, (root / "calls").read_text()

    def test_repository_bot_disarms_and_verifies(self):
        code, calls = self.run_disarm()
        self.assertEqual(code, 0)
        self.assertIn("pr merge --disable-auto", calls)
        self.assertEqual(calls.count("pr view"), 2)

    def test_personal_and_other_bot_never_write(self):
        for actor in ["jaywedgeworth22", "swu-wise[bot]", ""]:
            code, calls = self.run_disarm(actor)
            self.assertNotEqual(code, 0)
            self.assertNotIn("pr merge", calls)
            self.assertNotIn("pr view", calls)

    def test_unverified_never_writes(self):
        code, calls = self.run_disarm(mode="api_failure")
        self.assertNotEqual(code, 0)
        self.assertNotIn("pr merge", calls)

    def test_already_off_never_writes(self):
        code, calls = self.run_disarm(mode="off")
        self.assertEqual(code, 0)
        self.assertNotIn("pr merge", calls)

    def test_denied_and_unreadable_fail(self):
        for mode in ["denied", "unreadable"]:
            code, _ = self.run_disarm(mode=mode)
            self.assertNotEqual(code, 0)

    def test_lost_response_readback(self):
        self.assertEqual(self.run_disarm(mode="response_lost")[0], 0)


class RelayWorkflowContainment(unittest.TestCase):
    def test_relay_does_not_publish_secret(self):
        source = (Path(__file__).resolve().parents[1] / ".github/workflows/auto-merge-reusable.yml").read_text()
        self.assertNotIn("WISE_MERGE_TOKEN", source)
        self.assertNotIn("--github-env", source)
        self.assertIn("python3 - -- bash", source)
        self.assertIn("GH_TOKEN: ${{ github.token }}", source)


if __name__ == "__main__":
    unittest.main()
