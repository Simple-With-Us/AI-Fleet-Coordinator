"""Shared cadence and private credential binding without network or real sleeps."""
import os
import contextlib
import io
import fcntl
from pathlib import Path
import tempfile
import sys
import unittest
from unittest import mock

from fleet_lanes import code_main_sync as sync
from fleet_lanes import code_mirror_credentials as credentials


class LoopTests(unittest.TestCase):
    def test_sweep_command_forwards_remainder_without_parsing_or_logging(self):
        command = [sys.executable, "private-sentinel", "--dry-run", "--loop"]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(sync, "run_loop", side_effect=lambda run: run()) as loop, mock.patch.object(sync.subprocess, "run", return_value=mock.Mock(returncode=0, stdout=b"")) as run:
            self.assertEqual(sync.main(["--loop", "--sweep-command", *command], env={"FLEET_LANE_GUARD_STATE_DIR":tmp}), 0)
        self.assertEqual(run.call_args.args[0], command)
        self.assertIs(run.call_args.kwargs["shell"], False)
        self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_only_strict_counter_summary_survives_malicious_adjacent_output(self):
        summary = "summary: updated=0 current=17 would_update=0 skipped=0 failed=0"
        raw = "\n".join(["private-sentinel", "2026-11-13T08:05:21+00:00 "+summary,
                          summary+" private-sentinel", "private-sentinel "+summary,
                          summary.replace("current=17", "current=1000000"),
                          summary.replace("failed=0", "failed=-1")]).encode()
        result = mock.Mock(returncode=0, stdout=raw, stderr=summary.encode()+b" private-sentinel")
        with mock.patch.object(sync.subprocess, "run", return_value=result), mock.patch.object(sync, "log") as log:
            self.assertEqual(sync.run_sweep_command([sys.executable]),0)
        log.assert_called_once_with(summary)

    def test_invalid_sweep_command_combinations_are_rejected(self):
        cases = [["--sweep-command", sys.executable], ["--loop", "--dry-run", "--sweep-command", sys.executable], ["--print-interval", "--sweep-command", sys.executable], ["--loop", "--sweep-command"], ["--loop", "--sweep-command", "relative-private-sentinel"]]
        for argv in cases:
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()) as error, self.assertRaises(SystemExit) as raised:
                sync.main(argv)
            self.assertEqual(raised.exception.code, 2)
            self.assertNotIn("private-sentinel", error.getvalue())

    def test_sweep_failure_keeps_cadence_without_reflecting_child_output(self):
        output, error = io.StringIO(), io.StringIO()
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise KeyboardInterrupt
        with mock.patch.object(sync.subprocess, "run", side_effect=[mock.Mock(returncode=-15, stdout=b"private-sentinel", stderr=b"private-sentinel"), OSError("private-sentinel")]) as run, contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            self.assertEqual(sync.run_loop(lambda: sync.run_sweep_command([sys.executable, "private-sentinel"]), read=lambda: 300, sleep=sleep), 0)
        self.assertEqual(sleeps, [300, 300])
        self.assertEqual(run.call_count, 2)
        self.assertIn("exit=-15", error.getvalue())
        self.assertIn("exit=unavailable", error.getvalue())
        self.assertNotIn("private-sentinel", output.getvalue()+error.getvalue())

    def test_duplicate_loop_never_reads_or_runs_a_sweep(self):
        with tempfile.TemporaryDirectory() as tmp:
            fd = os.open(Path(tmp)/"code-main-sync-scheduler.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with mock.patch.object(sync, "run_loop") as loop, mock.patch.object(sync, "process_repo") as sweep:
                    self.assertEqual(sync.main(["--loop"], env={"FLEET_LANE_GUARD_STATE_DIR":tmp}), 0)
                loop.assert_not_called()
                sweep.assert_not_called()
            finally:
                os.close(fd)

    def test_print_failure_has_no_stdout(self):
        output, error = io.StringIO(), io.StringIO()
        with mock.patch.object(sync, "read_interval", side_effect=ValueError("private-sentinel")), contextlib.redirect_stdout(output), contextlib.redirect_stderr(error):
            self.assertEqual(sync.main(["--print-interval"]), 1)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("read-failed", error.getvalue())
        self.assertNotIn("private-sentinel", error.getvalue())

    def test_print_interval_does_not_start_a_sweep(self):
        output = io.StringIO()
        with mock.patch.object(sync, "read_interval", return_value=300), mock.patch.object(sync, "process_repo") as run, contextlib.redirect_stdout(output):
            self.assertEqual(sync.main(["--print-interval"]), 0)
        self.assertEqual(output.getvalue(), "300\n")
        run.assert_not_called()

    def test_changes_and_outage_keep_last_accepted_interval(self):
        reads = iter([300, 600, ValueError("private-error"), 900])
        sleeps, runs = [], []
        def read():
            value = next(reads)
            if isinstance(value, Exception):
                raise value
            return value
        def sleep(value):
            sleeps.append(value)
            if len(sleeps) == 4:
                raise KeyboardInterrupt
        with mock.patch.object(sync, "log") as log:
            self.assertEqual(sync.run_loop(lambda: runs.append(True), read, sleep), 0)
        self.assertEqual(sleeps, [300, 600, 600, 900])
        self.assertEqual(len(runs), 4)
        self.assertNotIn("private-error", str(log.call_args_list))

    def test_no_first_trusted_value_means_no_sweep_or_sleep(self):
        for value in (None, 29, 86401, True):
            run, sleep = mock.Mock(), mock.Mock()
            with mock.patch.object(sync, "log"):
                self.assertEqual(sync.run_loop(run, lambda: value, sleep), 1)
            run.assert_not_called()
            sleep.assert_not_called()

    def test_reader_refuses_default_and_accepts_lkg(self):
        from fleet_knobs import knobs
        from contextlib import nullcontext
        for source, accepted in (("default", False), ("lkg", True), ("infisical", True)):
            with mock.patch.object(credentials, "settings_read_binding", return_value=nullcontext()), mock.patch.object(knobs, "refresh") as refresh, mock.patch.object(knobs, "resolve", return_value=knobs.Knob("CODE_MIRROR_INTERVAL_SECONDS", 300, source)):
                if accepted:
                    self.assertEqual(sync.read_interval(), 300)
                else:
                    with self.assertRaises(ValueError):
                        sync.read_interval()
                refresh.assert_called_once()


class BindingTests(unittest.TestCase):
    def test_loaded_credential_is_redacted_in_repr_and_str(self):
        material = "st."+"fixture-sensitive-material"
        credential = credentials._Credential(material)
        self.assertNotIn(material, repr(credential))
        self.assertNotIn(material, str(credential))
        self.assertIn("redacted", repr(credential))

    def test_mac_reads_only_named_account_and_captures_output(self):
        result = mock.Mock(returncode=0, stdout="st."+"x"*30+"\n")
        with mock.patch.object(credentials.sys, "platform", "darwin"), mock.patch.object(credentials.subprocess, "run", return_value=result) as run:
            self.assertIsInstance(credentials._load_token(), credentials._Credential)
        self.assertEqual(run.call_args.args[0], ["/usr/bin/security", "find-generic-password", "-s", "infisical-service-token", "-a", credentials.NAME, "-w"])
        self.assertTrue(run.call_args.kwargs["capture_output"])

    def test_existing_binding_is_preserved_without_store_access(self):
        with mock.patch.dict(os.environ, {credentials.ENV: "existing"}), mock.patch.object(credentials, "_load_token") as load:
            with credentials.settings_read_binding():
                self.assertEqual(os.environ[credentials.ENV], "existing")
            load.assert_not_called()

    def test_binding_restored_even_after_fetch_exception(self):
        material = "st."+"fixture-sensitive-material"
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(credentials, "_load_token", return_value=credentials._Credential(material)):
            with self.assertRaises(RuntimeError):
                with credentials.settings_read_binding():
                    self.assertEqual(os.environ[credentials.ENV], material)
                    raise RuntimeError
            self.assertNotIn(credentials.ENV, os.environ)

    def test_linux_refuses_symlinks_and_public_or_large_credentials(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": tmp}), mock.patch.object(credentials.sys, "platform", "linux"):
            path = Path(tmp)/credentials.NAME
            path.write_text("st."+"x"*30)
            path.chmod(0o600)
            self.assertIsInstance(credentials._load_token(), credentials._Credential)
            path.chmod(0o644)
            with self.assertRaises(ValueError): credentials._load_token()
            path.chmod(0o600)
            path.write_text("x"*8193)
            with self.assertRaises(ValueError): credentials._load_token()
            path.unlink()
            path.symlink_to(Path(tmp)/"missing")
            with self.assertRaises(OSError): credentials._load_token()
