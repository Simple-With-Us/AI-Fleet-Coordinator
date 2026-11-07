import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from git_relay.deploy_runner import Runner, executor_identity, load_manifest
from git_relay.deploy_transaction import TransactionError


class RunnerTests(unittest.TestCase):
    def test_current_executor_exact_identity(self):
        x = executor_identity()
        self.assertEqual(x["pid"], os.getpid())
        self.assertTrue(x["boot"])
        self.assertTrue(x["start_ticks"].isdigit())

    def test_manifest_hash_and_absolute_name_refusals(self):
        with tempfile.TemporaryDirectory() as d, patch("git_relay.deploy_runner.private_anchor"):
            (Path(d) / "manifest.json").write_bytes(b"{}")
            with self.assertRaises(TransactionError):
                load_manifest(d, "wrong")

    def test_real_linux_pending_default_term_preempts_userspace(self):
        # Disposable child only. No production service, credentials or network.
        with tempfile.TemporaryDirectory() as d:
            marker = Path(d) / "resumed"
            child = subprocess.Popen(
                [
                    "/usr/bin/python3",
                    "-c",
                    "import os,signal,pathlib; os.kill(os.getpid(),signal.SIGSTOP); pathlib.Path("
                    + repr(str(marker))
                    + ").write_text('unsafe')",
                ]
            )
            try:
                for _ in range(100):
                    state = Path(f"/proc/{child.pid}/status").read_text()
                    if "State:\tT" in state:
                        break
                    time.sleep(0.01)
                else:
                    self.fail("child did not stop")
                os.kill(child.pid, signal.SIGTERM)
                status = dict(
                    line.split(":", 1)
                    for line in Path(f"/proc/{child.pid}/status").read_text().splitlines()
                    if ":" in line
                )
                self.assertTrue((int(status["SigPnd"], 16) | int(status["ShdPnd"], 16)) & 0x4000)
                os.kill(child.pid, signal.SIGCONT)
                self.assertEqual(child.wait(timeout=2), -signal.SIGTERM)
                self.assertFalse(marker.exists())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait()

    def test_guard_never_recursively_stops_start_job(self):
        r = Runner.__new__(Runner)

        class Host:
            old_hashes, new_hashes = {}, {}

            def lock(self):
                import contextlib

                return contextlib.nullcontext()

            def properties(self):
                return {"MainPID": "0", "ActiveState": "activating"}

        class Journal:
            def decision(self, *_):
                return "recover"

        r.host, r.journal = Host(), Journal()
        r.reconcile_commit = lambda: False
        events = []
        r.restore_stopped = lambda guard: events.append(guard)
        self.assertEqual(r.guard(), "old")
        self.assertEqual(events, [True])

    def test_failed_exit_unchanged_old_active_never_stops_handlers(self):
        import contextlib

        events = []
        r = Runner.__new__(Runner)

        class Host:
            def __init__(self):
                self.old_hashes = {"code/old.py": "old"}

            def lock(self):
                return contextlib.nullcontext()

            def hashes(self, _):
                return self.old_hashes

            def properties(self):
                return {"ActiveState": "active"}

            def process_identity(self):
                return {"pid": 42}

            def resume_if_same(self, _):
                events.append("resume")

            def prove_old_runtime(self, _):
                events.append("proof")
                return True

        class Journal:
            path = type("P", (), {"exists": lambda _: True})()

            def read(self):
                return {"phase": "prepared"}

            def aborted_without_writes(self):
                events.append("aborted")

        class Recovery:
            def finish(self):
                events.append("finish")

        r.host, r.journal, r.recovery, r.rows = Host(), Journal(), Recovery(), []
        r.reconcile_commit = lambda: False
        self.assertEqual(r.recover(), "aborted")
        self.assertEqual(events, ["aborted", "resume", "proof", "finish"])
