"""Real durable file/journal composition, disposable service adapter only."""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from git_relay.deploy_journal import Journal
from git_relay.deploy_supervisor import Supervisor
from git_relay.deploy_transaction import Transaction
from git_relay.tests.test_deploy_supervisor import FakeHost


class Host(FakeHost):
    def __init__(self, directory, roots, rows, old_hashes, new_hashes, fail=None):
        super().__init__(fail=fail)
        self.roots, self.rows = roots, rows
        self.old_hashes, self.new_hashes = old_hashes, new_hashes
        self.journal = Journal(directory, rows, self.hashes, lambda _: self.ident, lambda: "a")

    def hashes(self):
        return {
            r["root"] + "/" + r["name"]: hashlib.sha256(
                (self.roots[r["root"]] / r["name"]).read_bytes()
            ).hexdigest()
            if (self.roots[r["root"]] / r["name"]).exists()
            else None
            for r in self.rows
        }

    def arm_recovery(self, rows, deadline):
        super().arm_recovery(rows, deadline)
        self.journal.arm(rows, self.ident, deadline)
        self.journal.guard_installed()

    def allow_new_start(self, rows):
        super().allow_new_start(rows)
        self.journal.allow_new(rows, self.ident)
        self.assert_start("new")

    def allow_old_start(self, rows):
        super().allow_old_start(rows)
        self.journal.allow_old(rows)
        self.assert_start("old")

    def assert_start(self, expected):
        assert self.journal.decision(self.old_hashes, self.new_hashes, 1) == expected

    def mark_qualified(self):
        super().mark_qualified()
        self.journal.qualified()

    def mark_aborted(self):
        super().mark_aborted()
        self.journal.aborted_without_writes()

    def finish_recovery(self):
        assert self.journal.safe_to_finish()
        super().finish_recovery()


class IntegratedTests(unittest.TestCase):
    def test_real_prepare_apply_journal_commit_and_proof_failure_recovery(self):
        for fail in [None, "prove_new", "allow_new", "commit"]:
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as d:
                directory = Path(d)
                root = directory / "code"
                root.mkdir()
                (root / "old.py").write_bytes(b"old")
                changes = []
                for name, before, after in [
                    ("old.py", b"old", b"new"),
                    ("new.py", None, b"module"),
                ]:
                    changes.append(
                        {
                            "root": "code",
                            "name": name,
                            "before": hashlib.sha256(before).hexdigest() if before else None,
                            "after": hashlib.sha256(after).hexdigest(),
                            "data": after,
                            "meta": {"uid": os.getuid(), "gid": os.getgid(), "mode": 0o644},
                        }
                    )
                rows = [{k: v for k, v in r.items() if k != "data"} for r in changes]
                old = {"code/" + r["name"]: r["before"] for r in rows}
                new = {"code/" + r["name"]: r["after"] for r in rows}
                host = Host(directory, {"code": root}, rows, old, new, fail=fail)
                tx = Transaction(
                    {"code": root}, directory / "backup", lambda host=host: host.stopped
                )
                if fail == "commit":
                    tx.commit = lambda *_, host=host: host.step("commit")
                s = Supervisor(host, tx, rows, changes=changes)
                if fail:
                    with self.assertRaises(RuntimeError):
                        s.run()
                    self.assertEqual(host.hashes(), old)
                    self.assertEqual(host.journal.read()["phase"], "restored")
                else:
                    self.assertEqual(s.run(), "committed")
                    self.assertEqual(host.hashes(), new)
                    self.assertEqual(host.journal.read()["phase"], "committed")
                self.assertFalse(host.armed)
