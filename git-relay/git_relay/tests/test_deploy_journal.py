import tempfile
import unittest
from pathlib import Path

from git_relay.deploy_journal import Journal
from git_relay.deploy_transaction import TransactionError


class JournalTests(unittest.TestCase):
    def test_reboot_timeout_and_unknown_rows_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            identity = {
                "pid": 2,
                "start_ticks": "3",
                "exe": "/usr/bin/python3",
                "cgroup": "patch.service",
                "boot": "boot-a",
            }
            old, new = {"code": "old"}, {"code": "new"}
            j = Journal(
                d, [{"name": "fixed"}], lambda: new, lambda expected: identity, lambda: "boot-a"
            )
            j.arm(j.inventory, identity, 100)
            self.assertEqual(j.decision(old, new, 1), "recover")
            j.guard_installed()
            j.allow_new(j.inventory, identity)
            self.assertEqual(j.decision(old, new, 99), "new")
            self.assertEqual(j.decision(old, new, 100), "recover")
            fresh = Journal(d, j.inventory, lambda: new, lambda expected: None, lambda: "boot-b")
            self.assertEqual(fresh.decision(old, new, 1), "recover")
            corrupt = Journal(
                d, [{"name": "other"}], lambda: new, lambda expected: identity, lambda: "boot-a"
            )
            with self.assertRaises(TransactionError):
                corrupt.read()
            j.qualified()
            self.assertEqual(fresh.decision(old, new, 1000), "new")
            self.assertTrue(j.safe_to_finish())
            self.assertTrue((Path(d) / "supervisor.json").exists())

    def test_abort_only_before_apply_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            j = Journal(d, [], dict, lambda expected: None, lambda: "a")
            j.arm([], {}, 100)
            with self.assertRaises(TransactionError):
                j.arm([], {}, 100)
            j.aborted_without_writes()
            self.assertTrue(j.safe_to_finish())
            with self.assertRaises(TransactionError):
                j.aborted_without_writes()
