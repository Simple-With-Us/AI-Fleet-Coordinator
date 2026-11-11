"""Focused tests for the file-only Codex router launchd shutdown hook."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SYNC = ROOT / "scripts" / "simple-tools-runtime-sync.sh"


class RouterRuntimeSyncTests(unittest.TestCase):
    def run_signal(self, *, loaded: bool, signal_fails: bool = False) -> tuple[list[str], str, int]:
        with tempfile.TemporaryDirectory() as td:
            calls = Path(td) / "calls"
            logs = Path(td) / "logs"
            script = r"""
source "$1"
id() { printf '501\n'; }
launchctl() {
  printf '%s\n' "$*" >> "$CALLS"
  case "$1" in
    print) [ "$LOADED" = yes ] ;;
    kill) [ "$SIGNAL_FAILS" != yes ] ;;
    *) return 99 ;;
  esac
}
log() { printf '%s\n' "$*" >> "$LOGS"; }
signal_codex_wake_router
rc=$?
printf 'rc=%s\n' "$rc"
"""
            env = dict(os.environ)
            env.update(
                CALLS=str(calls),
                LOGS=str(logs),
                LOADED="yes" if loaded else "no",
                SIGNAL_FAILS="yes" if signal_fails else "no",
            )
            result = subprocess.run(
                ["/bin/bash", "-c", script, "bash", str(SYNC)],
                text=True,
                capture_output=True,
                env=env,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            call_lines = calls.read_text().splitlines() if calls.exists() else []
            log_text = logs.read_text() if logs.exists() else ""
            status = int(result.stdout.strip().removeprefix("rc="))
            return call_lines, log_text, status

    def test_loaded_router_gets_one_sigterm(self) -> None:
        calls, logs, status = self.run_signal(loaded=True)
        self.assertEqual(status, 0)
        self.assertEqual(
            calls,
            [
                "print gui/501/com.jay.codex-wake-router",
                "kill SIGTERM gui/501/com.jay.codex-wake-router",
            ],
        )
        self.assertIn("ROUTER-SHUTDOWN-SIGTERM", logs)

    def test_unloaded_router_is_not_started_or_signalled(self) -> None:
        calls, logs, status = self.run_signal(loaded=False)
        self.assertEqual(status, 0)
        self.assertEqual(calls, ["print gui/501/com.jay.codex-wake-router"])
        self.assertIn("ROUTER-NOT-LOADED", logs)

    def test_signal_failure_is_logged(self) -> None:
        calls, logs, status = self.run_signal(loaded=True, signal_fails=True)
        self.assertEqual(status, 1)
        self.assertEqual(
            calls,
            [
                "print gui/501/com.jay.codex-wake-router",
                "kill SIGTERM gui/501/com.jay.codex-wake-router",
            ],
        )
        self.assertIn("ROUTER-SHUTDOWN-SIGNAL-FAILED", logs)


if __name__ == "__main__":
    unittest.main()
