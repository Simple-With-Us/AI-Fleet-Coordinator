import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from git_relay.deploy_recovery import Recovery, supervisor_unit
from git_relay.deploy_transaction import TransactionError


class Journal:
    def __init__(self):
        self.events = []
        self.safe = False

    def arm(self, *args):
        self.events.append("arm")

    def guard_installed(self):
        self.events.append("guard")

    def allow_new(self, *args):
        self.events.append("new")

    def allow_old(self, *args):
        self.events.append("old")

    def safe_to_finish(self):
        return self.safe

    def finished(self):
        self.events.append("finished")


class RecoveryTests(unittest.TestCase):
    def test_unit_is_finite_root_and_failed_exit_recovers(self):
        properties = supervisor_unit("/opt/recovery/run.py")
        self.assertEqual(properties["User"], "root")
        self.assertEqual(properties["TimeoutStartSec"], "120s")
        self.assertEqual(properties["RuntimeMaxSec"], "120s")
        self.assertTrue(properties["ExecStopPost"].endswith("run.py recover"))
        for path in ["relative", "/tmp/a b", "/tmp/a%I", "/tmp/$(x)"]:
            with self.assertRaises(TransactionError):
                supervisor_unit(path)

    def test_arm_order_preserves_existing_config_and_finish_is_exact(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            runner = root / "runner.py"
            runner.write_text("# reviewed runner\n")
            directory = root / "state"
            directory.mkdir()
            dropin = root / "etc/systemd/system/git-relay.service.d"
            dropin.mkdir(parents=True)
            old = dropin / "40-original.conf"
            old.write_bytes(b"original\n")
            journal = Journal()
            commands = []
            recovery = Recovery(
                directory,
                runner,
                hashlib.sha256(runner.read_bytes()).hexdigest(),
                lambda *args: commands.append(args),
                journal,
                lambda: {"pid": 123},
                root=root,
            )
            # Simulate root ownership only in the disposable test filesystem.
            with patch("git_relay.deploy_recovery.atomic") as write:

                def fake_atomic(path, data, meta):
                    path.write_bytes(data)

                write.side_effect = fake_atomic
                recovery.arm([{"name": "a"}], 100)
            self.assertEqual(journal.events, ["arm", "guard"])
            self.assertEqual(old.read_bytes(), b"original\n")
            self.assertIn("ExecStartPre=+", recovery.dropin.read_text())
            with self.assertRaises(TransactionError):
                recovery.finish()
            journal.safe = True
            # read_regular metadata UID is this test user, so patch only UID.
            from git_relay.deploy_transaction import read_regular

            data, meta = read_regular(recovery.dropin)
            meta["uid"] = 0
            with patch("git_relay.deploy_recovery.read_regular", return_value=(data, meta)):
                recovery.finish()
            self.assertFalse(recovery.dropin.exists())
            self.assertEqual(old.read_bytes(), b"original\n")
            self.assertEqual(journal.events[-1], "finished")
            # Success ExecStopPost and crash-after-unlink repeat cleanup.
            recovery.finish()
            self.assertEqual(journal.events[-1], "finished")
            self.assertEqual(old.read_bytes(), b"original\n")
