import copy
import os
import tempfile
import unittest
from pathlib import Path

from git_relay.deploy_transaction import Transaction, TransactionError, sha


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.code = self.root / "code"
        self.code.mkdir()
        self.policy = self.root / "policy"
        self.policy.mkdir()
        (self.code / "old.py").write_bytes(b"old code")
        (self.policy / "seats.json").write_bytes(b"old policy")
        self.stopped = True
        self.t = Transaction(
            {"code": self.code, "policy": self.policy}, self.root / "backup", lambda: self.stopped
        )
        meta = {"uid": os.getuid(), "gid": os.getgid(), "mode": 0o644}
        self.changes = [
            {
                "root": "code",
                "name": "old.py",
                "before": sha(b"old code"),
                "after": sha(b"new code"),
                "meta": meta,
                "data": b"new code",
            },
            {
                "root": "code",
                "name": "new.py",
                "before": None,
                "after": sha(b"new module"),
                "meta": meta,
                "data": b"new module",
            },
            {
                "root": "policy",
                "name": "seats.json",
                "before": sha(b"old policy"),
                "after": sha(b"new policy"),
                "meta": meta,
                "data": b"new policy",
            },
        ]

    def prepare(self):
        return self.t.prepare(self.changes)["rows"]

    def assert_original(self):
        self.assertEqual((self.code / "old.py").read_bytes(), b"old code")
        self.assertFalse((self.code / "new.py").exists())
        self.assertEqual((self.policy / "seats.json").read_bytes(), b"old policy")

    def test_crash_after_each_file_and_repeated_recovery(self):
        for fail_at in range(3):
            with self.subTest(fail_at=fail_at):
                rows = self.prepare()

                def crash(index, fail_at=fail_at):
                    if index == fail_at:
                        raise SystemExit("executor lost")

                with self.assertRaises(SystemExit):
                    self.t.apply(rows, crash)
                # A fresh executor reads durable state, without namespace fds.
                fresh = Transaction(self.t.roots, self.t.backup, lambda: True)
                self.assertEqual(fresh.recover(rows), "restored")
                self.assertEqual(fresh.recover(rows), "restored")
                self.assert_original()
                import shutil

                shutil.rmtree(self.t.backup)

    def test_prepared_applied_and_qualified_commit(self):
        rows = self.prepare()
        self.t.recover(rows)
        self.assert_original()
        import shutil

        shutil.rmtree(self.t.backup)
        rows = self.prepare()
        self.t.apply(rows)
        with self.assertRaises(TransactionError):
            self.t.commit(rows, lambda: False)
        self.t.commit(rows, lambda: True)
        self.assertEqual(self.t.recover(rows), "committed")
        self.assertEqual((self.code / "new.py").read_bytes(), b"new module")

    def test_fail_closed_active_drift_symlink_and_unanchored_manifest(self):
        self.stopped = False
        with self.assertRaises(TransactionError):
            self.prepare()
        self.stopped = True
        rows = self.prepare()
        changed = copy.deepcopy(rows)
        changed[0]["name"] = "../outside"
        with self.assertRaises(TransactionError):
            self.t.load(changed)
        self.t.apply(rows)
        (self.code / "new.py").write_bytes(b"another writer")
        with self.assertRaises(TransactionError):
            self.t.recover(rows)
        # No partial restore or deletion on conflicting target.
        self.assertEqual((self.code / "old.py").read_bytes(), b"new code")
        self.assertEqual((self.code / "new.py").read_bytes(), b"another writer")

    def test_rejects_baseline_and_backup_drift(self):
        self.changes[0]["before"] = "wrong"
        with self.assertRaises(TransactionError):
            self.prepare()
        self.changes[0]["before"] = sha(b"old code")
        rows = self.prepare()
        self.t.apply(rows)
        (self.t.backup / "0.before").write_bytes(b"corrupt")
        with self.assertRaises(TransactionError):
            self.t.recover(rows)
        self.assertEqual((self.code / "old.py").read_bytes(), b"new code")
