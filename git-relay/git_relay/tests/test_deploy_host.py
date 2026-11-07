import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from git_relay.deploy_host import LinuxHost
from git_relay.deploy_transaction import TransactionError


class LinuxHostTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.proc = Path(self.temp.name)
        (self.proc / "sys/kernel/random").mkdir(parents=True)
        (self.proc / "sys/kernel/random/boot_id").write_text("boot-a")
        p = self.proc / "42"
        p.mkdir()
        (p / "stat").write_text(
            "42 (python unusual (name)) " + " ".join(["S"] + ["0"] * 18 + ["12345"])
        )
        (p / "cgroup").write_text("0::/system.slice/git-relay.service\n")
        (p / "exe").symlink_to("/usr/bin/python3")
        self.calls = []

        def run(args, **kw):
            self.calls.append((args, kw))
            return SimpleNamespace(
                returncode=0, stdout="MainPID=42\nActiveState=active\nWatchdogUSec=0\n"
            )

        self.h = LinuxHost("x", {}, {}, None, lambda *args: True, run=run, proc=self.proc)

    def test_process_identity_parses_parentheses_and_commands_are_bounded(self):
        x = self.h.process_identity()
        self.assertEqual(x["pid"], 42)
        self.assertEqual(x["start_ticks"], "12345")
        self.assertEqual(x["boot"], "boot-a")
        self.assertEqual(self.calls[0][1]["timeout"], 20)
        self.assertNotIn("shell", self.calls[0][1])

    def test_wrong_cgroup_and_executable_refused(self):
        (self.proc / "42/cgroup").write_text("0::/other.service")
        with self.assertRaises(TransactionError):
            self.h.process_identity()

    def test_no_stop_if_busy_recovery(self):
        expected = {"pid": 42}
        with (
            patch.object(self.h, "confirmed_stopped", return_value=False),
            patch.object(self.h, "process_identity", return_value=expected),
            patch.object(self.h, "freeze"),
            patch.object(self.h, "tasks_and_children", return_value=({42: "T", 43: "T"}, [])),
            patch.object(self.h, "pending_count", return_value=0),
            patch.object(self.h, "stop_job_exists", return_value=False),
            patch.object(self.h, "reconcile_frozen_stop", return_value="running") as resume,
            patch.object(self.h, "stop_without_kill") as stop,
        ):
            with self.assertRaises(TransactionError):
                self.h.stop_for_recovery()
            resume.assert_called_once_with(expected)
            stop.assert_not_called()

    def test_hash_target_anchor(self):
        with self.assertRaises(TransactionError):
            self.h.hashes({"code/../other": None})

    def test_default_term_queued_before_resume(self):
        expected = {"pid": 42}
        status = self.proc / "42/status"
        status.write_text("SigCgt: 0000\nSigIgn: 0000\nSigPnd: 0000\nShdPnd: 0000\n")
        events = []

        def kill(pid, sig):
            events.append("term")
            status.write_text("SigCgt: 0000\nSigIgn: 0000\nSigPnd: 0000\nShdPnd: 4000\n")

        with (
            patch.object(self.h, "command"),
            patch.object(self.h, "process_identity", return_value=expected),
            patch("os.kill", side_effect=kill),
            patch.object(self.h, "resume_if_same", side_effect=lambda _: events.append("resume")),
            patch.object(self.h, "confirmed_stopped", return_value=True),
        ):
            self.h.stop_without_kill(expected)
        self.assertEqual(events, ["term", "resume"])

    def test_caught_term_never_resumes_accept_loop(self):
        expected = {"pid": 42}
        (self.proc / "42/status").write_text("SigCgt: 4000\nSigIgn: 0000\n")
        with (
            patch.object(self.h, "command"),
            patch.object(self.h, "process_identity", return_value=expected),
            patch("os.kill") as kill,
            patch.object(self.h, "resume_if_same") as resume,
        ):
            with self.assertRaises(TransactionError):
                self.h.stop_without_kill(expected)
            kill.assert_not_called()
            resume.assert_not_called()

    def test_malformed_property_and_resume_preserves_original_failure(self):
        with (
            patch.object(self.h, "command", return_value="MainPID=42\nbad-row"),
            self.assertRaises(TransactionError),
        ):
            self.h.properties()
        for error in [ValueError(), KeyError(), OSError(), TransactionError()]:
            with (
                patch.object(self.h, "process_identity", side_effect=error),
                patch("os.kill") as kill,
            ):
                self.h.resume_if_same({"pid": 42})
                kill.assert_not_called()

    def test_startup_health_wait_is_bounded_and_provider_proof_not_retried(self):
        import io
        from unittest.mock import MagicMock
        from urllib.error import URLError

        identity = {"pid": 43}
        ticks = [0.0]
        self.h.clock = lambda ticks=ticks: ticks[0]
        self.h.sleep = lambda n, ticks=ticks: ticks.__setitem__(0, ticks[0] + n)
        response = MagicMock()
        response.__enter__.return_value.status = 200
        response.__enter__.return_value.read = io.BytesIO(b'{"ok":true}').read
        proof = MagicMock(return_value=True)
        self.h.proof = proof
        with (
            patch.object(self.h, "process_identity", return_value=identity),
            patch.object(self.h, "hashes", return_value={"code/x": "new"}),
            patch(
                "git_relay.deploy_host.urlopen", side_effect=[URLError("not bound"), response]
            ) as get,
        ):
            self.assertTrue(self.h.runtime({"code/x": "new"}, {"pid": 42}))
            self.assertEqual(get.call_count, 2)
            proof.assert_called_once()
        proof.reset_mock()
        ticks[0] = 0
        with (
            patch.object(self.h, "process_identity", return_value=identity),
            patch.object(self.h, "hashes", return_value={"code/x": "new"}),
            patch("git_relay.deploy_host.urlopen", side_effect=URLError("not bound")),
        ):
            self.assertFalse(self.h.runtime({"code/x": "new"}, {"pid": 42}))
            self.assertLess(ticks[0], 8.2)
            proof.assert_not_called()

    def test_presignal_failure_thaws_exact_process(self):
        expected = {"pid": 42}
        for unsafe in [False, True]:
            with (
                patch.object(self.h, "confirmed_stopped", return_value=False),
                patch.object(self.h, "signal_state", return_value=(False, unsafe)),
                patch.object(self.h, "accepted_stop", return_value=False),
                patch.object(self.h, "process_identity", return_value=expected),
                patch.object(self.h, "tasks_and_children", return_value=({42: "S"}, [])),
                patch("os.kill") as kill,
            ):
                self.assertEqual(self.h.reconcile_frozen_stop(expected), "running")
                import signal

                kill.assert_called_once_with(42, signal.SIGCONT)

    def test_queued_term_requires_accepted_stop_before_continue(self):
        expected = {"pid": 42}
        events = []
        with (
            patch.object(self.h, "confirmed_stopped", side_effect=[False, False, True]),
            patch.object(self.h, "signal_state", return_value=(True, False)),
            patch.object(self.h, "accepted_stop", side_effect=[False, True]),
            patch.object(self.h, "command", side_effect=lambda *a: events.append("stop-accepted")),
            patch("os.kill", side_effect=lambda *a: events.append("continue")),
        ):
            self.assertEqual(self.h.reconcile_frozen_stop(expected), "stopped")
            self.assertEqual(events, ["stop-accepted", "continue"])

    def test_unaccepted_stop_or_unsafe_term_never_continues(self):
        ticks = [0.0]
        self.h.clock = lambda ticks=ticks: ticks[0]
        self.h.sleep = lambda n, ticks=ticks: ticks.__setitem__(0, ticks[0] + n)
        for unsafe in [False, True]:
            ticks[0] = 0
            with (
                patch.object(self.h, "confirmed_stopped", return_value=False),
                patch.object(self.h, "signal_state", return_value=(True, unsafe)),
                patch.object(self.h, "accepted_stop", return_value=False),
                patch.object(self.h, "command", side_effect=TransactionError("stop failed")),
                patch("os.kill") as kill,
            ):
                with self.assertRaisesRegex(TransactionError, "guard retained"):
                    self.h.reconcile_frozen_stop({"pid": 42})
                kill.assert_not_called()
                self.assertLess(ticks[0], 10.2)

    def test_signal_send_uncertainty_uses_actual_pending_state(self):
        expected = {"pid": 42}
        for result in ["running", "stopped"]:
            with (
                patch.object(self.h, "confirmed_stopped", return_value=False),
                patch.object(self.h, "process_identity", return_value=expected),
                patch.object(self.h, "freeze"),
                patch.object(self.h, "tasks_and_children", return_value=({42: "T"}, [])),
                patch.object(self.h, "pending_count", return_value=0),
                patch.object(self.h, "stop_job_exists", return_value=False),
                patch.object(self.h, "stop_without_kill", side_effect=OSError("ambiguous signal")),
                patch.object(self.h, "reconcile_frozen_stop", return_value=result) as reconcile,
            ):
                if result == "stopped":
                    self.h.stop_for_recovery()
                else:
                    with self.assertRaises(OSError):
                        self.h.stop_for_recovery()
                reconcile.assert_called_once_with(expected)

    def test_reconciliation_never_signals_replacement_identity(self):
        with (
            patch.object(self.h, "confirmed_stopped", return_value=False),
            patch.object(self.h, "process_identity", return_value={"pid": 43}),
            patch("os.kill") as kill,
        ):
            with self.assertRaises(TransactionError):
                self.h.reconcile_frozen_stop({"pid": 42})
            kill.assert_not_called()

    def test_blocked_term_is_not_default_immediate_termination(self):
        (self.proc / "42/status").write_text("SigCgt: 0000\nSigIgn: 0000\nSigBlk: 4000\n")
        with (
            patch.object(self.h, "process_identity", return_value={"pid": 42}),
            patch("os.kill") as kill,
        ):
            with self.assertRaises(TransactionError):
                self.h.stop_without_kill({"pid": 42})
            kill.assert_not_called()

    def test_busy_multitask_thaw_does_not_require_quiescent_stop(self):
        expected = {"pid": 42}
        ticks = [0.0]
        self.h.clock = lambda ticks=ticks: ticks[0]
        self.h.sleep = lambda n, ticks=ticks: ticks.__setitem__(0, ticks[0] + n)
        with (
            patch.object(self.h, "confirmed_stopped", return_value=False),
            patch.object(self.h, "signal_state", return_value=(False, False)),
            patch.object(self.h, "accepted_stop", return_value=False),
            patch.object(self.h, "process_identity", return_value=expected),
            patch.object(
                self.h,
                "tasks_and_children",
                side_effect=[({42: "T", 43: "T"}, [99]), ({42: "S", 43: "R"}, [99])],
            ),
            patch("os.kill") as kill,
        ):
            self.assertEqual(self.h.reconcile_frozen_stop(expected), "running")
            kill.assert_called_once()

    def test_vanishing_or_esrch_is_never_false_running(self):
        for error in [ProcessLookupError(), FileNotFoundError()]:
            with (
                patch.object(self.h, "_reconcile_frozen_stop", side_effect=error),
                patch.object(self.h, "confirmed_stopped", return_value=True),
            ):
                self.assertEqual(self.h.reconcile_frozen_stop({"pid": 42}), "stopped")
        ticks = [0.0]
        self.h.clock = lambda ticks=ticks: ticks[0]
        self.h.sleep = lambda n, ticks=ticks: ticks.__setitem__(0, ticks[0] + n)
        with (
            patch.object(self.h, "_reconcile_frozen_stop", side_effect=ProcessLookupError()),
            patch.object(self.h, "confirmed_stopped", return_value=False),
            self.assertRaisesRegex(TransactionError, "not confirmed stopped"),
        ):
            self.h.reconcile_frozen_stop({"pid": 42})

    def test_busy_thaw_identity_drift_or_stuck_task_refused(self):
        expected = {"pid": 42}
        for drift in [True, False]:
            ticks = [0.0]
            self.h.clock = lambda ticks=ticks: ticks[0]
            self.h.sleep = lambda n, ticks=ticks: ticks.__setitem__(0, ticks[0] + n)
            with (
                patch.object(self.h, "confirmed_stopped", return_value=False),
                patch.object(self.h, "signal_state", return_value=(False, False)),
                patch.object(self.h, "accepted_stop", return_value=False),
                patch.object(
                    self.h,
                    "process_identity",
                    side_effect=[expected, {"pid": 43}] if drift else lambda: expected,
                ),
                patch.object(self.h, "tasks_and_children", return_value=({42: "S", 43: "T"}, [])),
                patch("os.kill"),
                self.assertRaisesRegex(TransactionError, "guard retained"),
            ):
                self.h.reconcile_frozen_stop(expected)

    def test_signal_state_accepts_exact_deactivating_process_only_for_cleanup(self):
        expected = self.h.process_identity()
        (self.proc / "42/status").write_text(
            "SigCgt: 0000\nSigIgn: 0000\nSigBlk: 0000\nSigPnd: 4000\nShdPnd: 0000\n"
        )
        with patch.object(
            self.h, "properties", return_value={"MainPID": "42", "ActiveState": "deactivating"}
        ):
            self.assertEqual(self.h.signal_state(expected), (True, False))
            with self.assertRaises(TransactionError):
                self.h.process_identity()
        with (
            patch.object(
                self.h, "properties", return_value={"MainPID": "43", "ActiveState": "deactivating"}
            ),
            self.assertRaises((TransactionError, FileNotFoundError)),
        ):
            self.h.signal_state(expected)

    def test_thawed_process_exit_is_success_only_with_full_stopped_proof(self):
        expected = {"pid": 42}
        with (
            patch.object(self.h, "confirmed_stopped", side_effect=[False, True]),
            patch.object(self.h, "signal_state", return_value=(False, True)),
            patch.object(self.h, "accepted_stop", return_value=False),
            patch.object(self.h, "process_identity", return_value=expected),
            patch("os.kill"),
        ):
            self.assertEqual(self.h.reconcile_frozen_stop(expected), "stopped")
        with (
            patch.object(self.h, "confirmed_stopped", side_effect=[False, False, True]),
            patch.object(self.h, "signal_state", return_value=(False, False)),
            patch.object(self.h, "accepted_stop", return_value=False),
            patch.object(
                self.h, "process_identity", side_effect=[expected, TransactionError("inactive")]
            ),
            patch("os.kill"),
        ):
            self.assertEqual(self.h.reconcile_frozen_stop(expected), "stopped")

    def test_interrupt_cleanup_preserves_original_after_safe_thaw(self):
        expected = {"pid": 42}
        for error in [KeyboardInterrupt(), SystemExit(7)]:
            with (
                patch.object(self.h, "confirmed_stopped", return_value=False),
                patch.object(self.h, "process_identity", return_value=expected),
                patch.object(self.h, "freeze", side_effect=error),
                patch.object(self.h, "reconcile_frozen_stop", return_value="running") as cleanup,
                self.assertRaises(type(error)),
            ):
                self.h.stop_for_recovery()
            cleanup.assert_called_once_with(expected)
