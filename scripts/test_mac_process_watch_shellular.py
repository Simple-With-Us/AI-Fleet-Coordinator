#!/usr/bin/env python3
"""Synthetic-log tests for the Shellular relay check in mac-process-watch.sh.

Feeds pm2-style log pairs (shellular-out.log / shellular-error.log) through the
script's no-lock test hook and asserts kill / no-kill.  HOME is a temp dir, so
nothing real is read, written, or killed.  Runs on macOS and Linux.

  python3 scripts/test_mac_process_watch_shellular.py

Exit codes of `--shellular-check`: 0 = dead (the watch would kill the pid),
1 = leave it.  The hook prints one verdict line.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "mac-process-watch.sh"

# 2026-10-08 9:00pm local, the hour of the incident.
T0 = time.mktime((2026, 10, 8, 21, 0, 0, 0, 0, -1))

HANDSHAKE = "[error] <2026-10-09T02:10:32.465Z> Closed before handshake completed. Code: 1006 Reason: "
RELAY_FAILED = "[warn] <2026-10-09T02:10:32.611Z> Relay wss://relay.eu-central.shellular.dev/cli failed: read ECONNRESET"
LOST = "[warn] <2026-10-09T02:49:40.939Z> Connection lost (code: 1006, reason: none). Reconnecting..."
RECONNECTED = "  Reconnected to server at 10/8/2026, 9:46:35 PM"
CONNECTED = "  ✅ Connected to server at 10/8/2026, 9:44:30 PM"
BANNER = "  Shellular CLI v1.4.2"
RETRYING = "  Retrying in 5s..."
CLIENT_UP = "  ✓ Client c_demo connected on 10/8/2026, 9:50:01 PM"
CLIENT_DOWN = "  ✗ Client c_demo disconnected on 10/8/2026, 9:50:01 PM"


def stamp(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch))


class Watch(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)
        self.logs = self.home / ".pm2" / "logs"
        self.logs.mkdir(parents=True)
        self.err = self.logs / "shellular-error.log"
        self.out = self.logs / "shellular-out.log"
        self.state = self.home / "Library" / "Logs" / "mac-process-watch.state.shellular"
        self.extra_env: dict[str, str] = {}

    # -- log writers (append, like pm2) -----------------------------------
    def _append(self, path: Path, at: float, message: str) -> None:
        with path.open("a", encoding="utf-8") as fh:
            fh.write("%s:   %s\n" % (stamp(at), message.lstrip()))

    def error(self, at: float, message: str) -> None:
        self._append(self.err, at, message)

    def output(self, at: float, message: str) -> None:
        self._append(self.out, at, message)

    # -- run the hook -------------------------------------------------------
    def run_hook(self, flag: str, now: float, start: float | None = None, **env_extra: str):
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.home),
            "SHELLULAR_NOW": str(now),
        }
        if start is not None:
            env["SHELLULAR_START_EPOCH"] = str(start)
        env.update(self.extra_env)
        env.update(env_extra)
        res = subprocess.run(
            ["bash", str(SCRIPT), flag],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return res.returncode, res.stdout.strip()

    def check(self, now: float, start: float | None = None, **env_extra: str):
        return self.run_hook("--shellular-check", now, start, **env_extra)

    def assertLeftAlone(self, result, contains: str = "") -> None:
        code, verdict = result
        self.assertEqual(code, 1, "would have killed shellular: %r" % verdict)
        self.assertIn(contains, verdict)

    def assertKilled(self, result) -> None:
        code, verdict = result
        self.assertEqual(code, 0, "should have been confirmed dead: %r" % verdict)
        self.assertIn("unrecovered", verdict)


class MidReconnectIsNeverKilled(Watch):
    def test_incident_pattern_fresh_warn_over_old_handshake_failures(self) -> None:
        # The 2026-10-08 loop: old failures (already recovered) sit in the
        # error log's tail, a fresh "Connection lost" lands, and the old rule
        # saw "error log written in the last 180s + a handshake line in the
        # last 80" and killed the pid mid-reconnect.
        self.output(T0 - 4000, BANNER)
        self.output(T0 - 3990, CONNECTED)
        self.error(T0 - 1000, HANDSHAKE)
        self.error(T0 - 999, RELAY_FAILED)
        self.output(T0 - 990, RECONNECTED)
        self.error(T0 - 5, LOST)
        self.assertLeftAlone(self.check(T0))
        self.assertLeftAlone(self.check(T0 + 120))
        self.assertFalse(self.state.exists())

    def test_failure_recovered_before_the_check_is_history(self) -> None:
        self.output(T0 - 4000, CONNECTED)
        self.error(T0 - 500, HANDSHAKE)
        self.output(T0 - 400, RECONNECTED)
        self.assertLeftAlone(self.check(T0))

    def test_a_connected_client_counts_as_recovery(self) -> None:
        self.output(T0 - 4000, CONNECTED)
        self.error(T0 - 300, HANDSHAKE)
        self.output(T0 - 200, CLIENT_UP)
        self.assertLeftAlone(self.check(T0))

    def test_a_disconnected_client_is_not_a_recovery(self) -> None:
        # "disconnected on" contains "connected on"; the old substring match
        # treated it as healthy.
        self.output(T0 - 4000, CONNECTED)
        self.error(T0 - 300, HANDSHAKE)
        self.output(T0 - 200, CLIENT_DOWN)
        code, verdict = self.check(T0)
        self.assertEqual(code, 1)
        self.assertTrue(verdict.startswith("suspect"), verdict)

    def test_plain_connection_lost_lines_are_not_evidence(self) -> None:
        self.output(T0 - 4000, CONNECTED)
        for i in range(5):
            self.error(T0 - 100 + i, LOST)
        self.assertLeftAlone(self.check(T0))
        self.assertLeftAlone(self.check(T0 + 300))


class TwoPassConfirmation(Watch):
    def setUp(self) -> None:
        super().setUp()
        self.output(T0 - 4000, BANNER)
        self.output(T0 - 3990, CONNECTED)
        self.error(T0 - 30, HANDSHAKE)

    def test_first_pass_only_suspects(self) -> None:
        code, verdict = self.check(T0)
        self.assertEqual(code, 1)
        self.assertTrue(verdict.startswith("suspect"), verdict)
        self.assertTrue(self.state.exists())

    def test_second_pass_under_two_minutes_still_waits(self) -> None:
        self.check(T0)
        self.assertLeftAlone(self.check(T0 + 60), "suspect")
        self.assertLeftAlone(self.check(T0 + 119), "suspect")

    def test_second_pass_two_minutes_later_confirms(self) -> None:
        self.check(T0)
        self.assertKilled(self.check(T0 + 120))
        self.assertFalse(self.state.exists(), "state must reset after a kill")

    def test_after_a_kill_the_next_failure_needs_two_fresh_passes(self) -> None:
        self.check(T0)
        self.assertKilled(self.check(T0 + 120))
        self.assertLeftAlone(self.check(T0 + 240), "suspect")

    def test_recovery_between_passes_resets_the_streak(self) -> None:
        self.check(T0)
        self.output(T0 + 50, RECONNECTED)
        self.assertLeftAlone(self.check(T0 + 120))
        self.assertFalse(self.state.exists())
        self.error(T0 + 130, HANDSHAKE)
        self.assertLeftAlone(self.check(T0 + 140), "suspect")
        self.assertLeftAlone(self.check(T0 + 200), "suspect")
        self.assertKilled(self.check(T0 + 260))

    def test_a_new_recovery_cycle_resets_even_without_a_clean_pass(self) -> None:
        self.check(T0)
        self.output(T0 + 10, RECONNECTED)
        self.error(T0 + 100, HANDSHAKE)  # fails again after the recovery
        code, verdict = self.check(T0 + 130)
        self.assertEqual(code, 1, "baseline moved, so the old streak must not count")
        self.assertTrue(verdict.startswith("suspect age=0s"), verdict)

    def test_a_streak_left_behind_by_skipped_passes_expires(self) -> None:
        self.check(T0)
        # The watch exited early (lock held) for 20 minutes; the failure line
        # is still unrecovered but the old streak must not count.
        code, verdict = self.check(T0 + 1000)
        self.assertEqual(code, 1)
        self.assertTrue(verdict.startswith("suspect age=0s"), verdict)

    def test_a_jittery_process_start_baseline_still_confirms(self) -> None:
        # No banner and no Connected line: the baseline is the start time that
        # `ps` etime yields, which differs by about a second between passes.
        # That must not reset the streak (a reset forever = never recovers).
        self.out.write_text("")
        self.err.write_text("")
        self.error(T0 - 30, HANDSHAKE)
        first = self.check(T0, start=T0 - 1000.4)
        self.assertEqual(first[0], 1)
        self.assertTrue(first[1].startswith("suspect"), first)
        self.assertKilled(self.check(T0 + 120, start=T0 - 999.6))

    def test_confirm_window_is_a_knob(self) -> None:
        self.check(T0, MAC_PROCESS_WATCH_SHELLULAR_CONFIRM_SEC="30")
        self.assertKilled(self.check(T0 + 30, MAC_PROCESS_WATCH_SHELLULAR_CONFIRM_SEC="30"))


class GraceAfterRestart(Watch):
    def test_banner_starts_a_grace_period(self) -> None:
        self.output(T0 - 100, BANNER)
        self.error(T0 - 50, HANDSHAKE)
        self.assertLeftAlone(self.check(T0), "grace")
        self.assertFalse(self.state.exists())

    def test_process_start_time_also_starts_a_grace_period(self) -> None:
        self.error(T0 - 50, HANDSHAKE)
        self.assertLeftAlone(self.check(T0, start=T0 - 100), "grace")

    @unittest.skipUnless(os.path.exists("/bin/ps"), "needs /bin/ps")
    def test_ps_etime_of_the_pid_is_the_fallback_start_time(self) -> None:
        now = time.time()
        self.error(now - 5, HANDSHAKE)
        # This very test process started moments ago, so it is inside grace.
        result = self.check(
            now,
            SHELLULAR_PID=str(os.getpid()),
            MAC_PROCESS_WATCH_SHELLULAR_GRACE_SEC="3600",  # robust on a slow host
        )
        self.assertLeftAlone(result, "grace")

    def test_after_grace_the_two_pass_rule_applies(self) -> None:
        self.output(T0 - 100, BANNER)
        self.error(T0 - 50, HANDSHAKE)
        self.assertLeftAlone(self.check(T0 + 100), "suspect")  # 200s after the banner
        self.assertKilled(self.check(T0 + 220))

    def test_grace_clears_a_streak_from_the_previous_process(self) -> None:
        self.output(T0 - 4000, CONNECTED)
        self.error(T0 - 30, HANDSHAKE)
        self.check(T0)
        self.output(T0 + 60, BANNER)  # restarted
        self.error(T0 + 70, HANDSHAKE)
        self.assertLeftAlone(self.check(T0 + 130), "grace")
        self.assertFalse(self.state.exists())

    def test_grace_is_a_knob(self) -> None:
        self.output(T0 - 100, BANNER)
        self.error(T0 - 50, HANDSHAKE)
        self.assertLeftAlone(self.check(T0, MAC_PROCESS_WATCH_SHELLULAR_GRACE_SEC="60"), "suspect")


class EvidenceScope(Watch):
    def test_failures_older_than_the_process_are_ignored(self) -> None:
        self.error(T0 - 900, HANDSHAKE)
        self.assertLeftAlone(self.check(T0, start=T0 - 600))
        self.error(T0 - 100, HANDSHAKE)
        self.assertLeftAlone(self.check(T0, start=T0 - 600), "suspect")

    def test_ancient_unrecovered_evidence_is_not_killed_for(self) -> None:
        self.output(T0 - 9000, CONNECTED)
        self.error(T0 - 3000, HANDSHAKE)
        self.assertLeftAlone(self.check(T0), "stale-evidence")
        self.assertLeftAlone(self.check(T0 + 200), "stale-evidence")

    def test_ioreg_missing_after_start_is_evidence(self) -> None:
        self.error(T0 - 60, "ioreg: command not found")
        self.assertLeftAlone(self.check(T0, start=T0 - 1000), "suspect")
        self.assertKilled(self.check(T0 + 120, start=T0 - 1000))

    def test_ioreg_lines_from_before_a_restart_are_ignored(self) -> None:
        self.error(T0 - 600, "ioreg: command not found")
        self.output(T0 - 500, BANNER)
        self.output(T0 - 490, CONNECTED)
        self.error(T0 - 5, LOST)  # a fresh write must not resurrect the old lines
        self.assertLeftAlone(self.check(T0))

    def test_retry_loop_without_a_connect_is_evidence(self) -> None:
        self.output(T0 - 4000, BANNER)
        self.output(T0 - 3990, CONNECTED)
        self.output(T0 - 40, RETRYING)
        self.assertLeftAlone(self.check(T0), "suspect")
        self.assertKilled(self.check(T0 + 120))

    def test_retry_followed_by_connect_is_healthy(self) -> None:
        self.output(T0 - 4000, CONNECTED)
        self.output(T0 - 40, RETRYING)
        self.output(T0 - 30, RECONNECTED)
        self.assertLeftAlone(self.check(T0))


class MissingEvidenceIsNotDead(Watch):
    def test_no_logs(self) -> None:
        self.assertLeftAlone(self.check(T0))

    def test_empty_logs(self) -> None:
        self.err.write_text("")
        self.out.write_text("")
        self.assertLeftAlone(self.check(T0))

    def test_unparseable_lines(self) -> None:
        self.err.write_text("Closed before handshake completed\nno timestamp here\n")
        self.out.write_text("Retrying in 5s...\n\x00\x01garbage\n")
        self.assertLeftAlone(self.check(T0))
        self.assertFalse(self.state.exists())

    def test_corrupt_state_file_never_kills_on_its_own(self) -> None:
        self.output(T0 - 4000, CONNECTED)
        self.error(T0 - 30, HANDSHAKE)
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text("not numbers\n")
        self.assertLeftAlone(self.check(T0), "suspect")


class ErrorLogRotation(Watch):
    def test_oversized_error_log_is_copy_truncated(self) -> None:
        payload = "".join("%s:   noise line %d\n" % (stamp(T0 - 1000 + i), i) for i in range(400))
        self.err.write_text(payload)
        self.out.write_text("%s:   %s\n" % (stamp(T0), CONNECTED.strip()))
        inode = self.err.stat().st_ino
        code, _ = self.run_hook("--shellular-rotate", T0, MAC_PROCESS_WATCH_SHELLULAR_ERR_MAX_BYTES="1000")
        self.assertEqual(code, 0)
        self.assertEqual(self.err.read_text(), "")
        self.assertEqual(self.err.stat().st_ino, inode, "must truncate in place so pm2's handle keeps working")
        self.assertEqual(Path(str(self.err) + ".1").read_text(), payload)
        self.assertIn("Connected to server", self.out.read_text(), "the out log holds the Connected markers")
        log = (self.home / "Library" / "Logs" / "mac-process-watch.log").read_text()
        self.assertIn("ROTATE  shellular-error.log", log)

    def test_small_error_log_is_left_alone(self) -> None:
        self.err.write_text("%s:   small\n" % stamp(T0))
        before = self.err.read_text()
        self.run_hook("--shellular-rotate", T0, MAC_PROCESS_WATCH_SHELLULAR_ERR_MAX_BYTES="1000")
        self.assertEqual(self.err.read_text(), before)
        self.assertFalse(Path(str(self.err) + ".1").exists())

    def test_missing_error_log_is_fine(self) -> None:
        code, _ = self.run_hook("--shellular-rotate", T0)
        self.assertEqual(code, 0)

    def test_rotation_removes_stale_evidence_for_good(self) -> None:
        self.output(T0 - 4000, CONNECTED)
        self.error(T0 - 30, HANDSHAKE)
        self.run_hook("--shellular-rotate", T0, MAC_PROCESS_WATCH_SHELLULAR_ERR_MAX_BYTES="10")
        self.assertLeftAlone(self.check(T0))


class CallSite(unittest.TestCase):
    """The pass-through from the verdict to a kill is the only place a pid dies."""

    def test_the_only_shellular_kill_is_gated_on_the_confirmed_verdict(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertEqual(text.count('try_restart "pm2:shellular-relay"'), 1)
        site = text.index('verdict="$(SHELLULAR_PID="$spid" shellular_relay_dead)"')
        gate = text.index('if [ "$rc" -eq 0 ]; then', site)
        kill = text.index('try_restart "pm2:shellular-relay"')
        self.assertLess(site, gate)
        self.assertLess(gate, kill)
        self.assertLess(kill - gate, 300, "the kill must sit directly under the confirmed-dead branch")
        self.assertIn("rotate_shellular_error_log", text[site - 500 : site])

    def test_script_parses(self) -> None:
        res = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)


if __name__ == "__main__":
    unittest.main()
