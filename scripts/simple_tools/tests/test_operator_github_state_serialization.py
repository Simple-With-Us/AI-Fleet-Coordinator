"""The existing retained primitive serializes callers without another store implementation."""

import os
import stat
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from simple_tools.operator_github_state import LedgerError, NonceLedger


class SerializationTests(unittest.TestCase):
    def test_kernel_lock_excludes_an_actual_second_process_until_release(self):
        program = """
import sys
sys.path.insert(0, sys.argv[1])
from simple_tools.operator_github_state import NonceLedger, LedgerError
try:
    with NonceLedger(sys.argv[2], retain_unsettled=True).serialized(timeout=0.03):
        print('entered')
except LedgerError:
    print('held')
"""
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "outcomes"
            ledger = NonceLedger(directory, retain_unsettled=True)
            command = [
                sys.executable,
                "-I",
                "-B",
                "-c",
                program,
                str(Path(__file__).resolve().parents[2]),
                str(directory),
            ]

            def probe():
                result = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    env={"PATH": os.defpath},
                    timeout=3,
                    check=True,
                )
                return result.stdout.strip()

            with ledger.serialized():
                self.assertEqual(probe(), "held")
            self.assertEqual(probe(), "entered")

    def test_a_competing_caller_times_out_then_can_enter_after_release(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "outcomes"
            first = NonceLedger(directory, retain_unsettled=True)
            second = NonceLedger(directory, retain_unsettled=True)
            entered = []

            def compete():
                with second.serialized(timeout=0.03):
                    entered.append(True)

            with ThreadPoolExecutor(max_workers=1) as executor:
                with first.serialized():
                    future = executor.submit(compete)
                    with self.assertRaises(LedgerError):
                        future.result(timeout=2)
                    self.assertEqual(entered, [])
                executor.submit(compete).result(timeout=2)
            self.assertEqual(entered, [True])
            lock = directory / "journal.lock"
            self.assertEqual((stat.S_IMODE(lock.stat().st_mode), lock.stat().st_nlink), (0o600, 1))

    def test_symlink_hardlink_or_writable_lock_is_refused_before_entry(self):
        for kind in ("symlink", "hardlink", "writable"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                directory = Path(root) / "outcomes"
                ledger = NonceLedger(directory, retain_unsettled=True)
                target, lock = Path(root) / "untouched", directory / "journal.lock"
                target.write_text("unchanged")
                target.chmod(0o600)
                if kind == "symlink":
                    lock.symlink_to(target)
                elif kind == "hardlink":
                    os.link(target, lock)
                else:
                    lock.write_text("")
                    lock.chmod(0o666)
                with self.assertRaises(LedgerError):
                    with ledger.serialized():
                        self.fail("untrusted lock admitted a mutation")
                self.assertEqual(target.read_text(), "unchanged")

    def test_the_new_serialization_is_opt_in_and_legacy_calls_stay_unchanged(self):
        with tempfile.TemporaryDirectory() as root:
            legacy = NonceLedger(Path(root) / "legacy")
            with self.assertRaises(LedgerError):
                with legacy.serialized():
                    self.fail("legacy mode acquired a retained mutation lock")
            self.assertEqual(legacy.begin("namespace", "nonce", 10, 0, 10), "fresh")
            legacy.release("namespace", "nonce")
            self.assertIsNone(legacy.lookup("namespace", "nonce"))

    def test_caller_failure_is_not_reclassified_as_a_lock_failure(self):
        with tempfile.TemporaryDirectory() as root:
            ledger = NonceLedger(Path(root) / "outcomes", retain_unsettled=True)
            failure = OSError("external mutator failed")
            with self.assertRaises(OSError) as raised:
                with ledger.serialized():
                    raise failure
            self.assertIs(raised.exception, failure)
            with ledger.serialized():
                pass

    def test_lost_database_with_initialization_evidence_is_never_recreated(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "outcomes"
            ledger = NonceLedger(directory, retain_unsettled=True)
            ledger.begin("namespace", "nonce", 10, 1, 10)
            ledger.settle("namespace", "nonce", "outcome_unknown", "synthetic metadata")
            marker = directory / ledger.INITIALIZED_NAME
            self.assertEqual(marker.read_bytes(), ledger.INITIALIZED_BYTES)
            ledger.path.unlink()
            with self.assertRaises(LedgerError):
                NonceLedger(directory, retain_unsettled=True)
            self.assertFalse(ledger.path.exists())

    def test_invalid_initialization_evidence_refuses_without_creating_a_database(self):
        for kind in ("symlink", "hardlink", "writable", "truncated"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                directory = Path(root) / "outcomes"
                directory.mkdir(mode=0o700)
                target = Path(root) / "untouched"
                target.write_bytes(NonceLedger.INITIALIZED_BYTES)
                target.chmod(0o600)
                marker = directory / NonceLedger.INITIALIZED_NAME
                if kind == "symlink":
                    marker.symlink_to(target)
                elif kind == "hardlink":
                    os.link(target, marker)
                else:
                    marker.write_bytes(b"" if kind == "truncated" else NonceLedger.INITIALIZED_BYTES)
                    marker.chmod(0o666 if kind == "writable" else 0o600)
                with self.assertRaises(LedgerError):
                    NonceLedger(directory, retain_unsettled=True)
                self.assertFalse((directory / "nonces.sqlite3").exists())
                self.assertEqual(target.read_bytes(), NonceLedger.INITIALIZED_BYTES)

    def test_short_marker_write_never_publishes_partial_initialization_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "outcomes"
            real_write = os.write

            def short_write(fd, value):
                return real_write(fd, value[:2])

            with mock.patch.object(os, "write", short_write), self.assertRaises(LedgerError):
                NonceLedger(directory, retain_unsettled=True)
            self.assertEqual(list(directory.iterdir()), [])
            ledger = NonceLedger(directory, retain_unsettled=True)
            self.assertEqual((directory / ledger.INITIALIZED_NAME).read_bytes(), ledger.INITIALIZED_BYTES)

    def test_existing_torn_marker_is_never_repaired_over_lost_history(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / "outcomes"
            ledger = NonceLedger(directory, retain_unsettled=True)
            ledger.begin("namespace", "old-unknown", 10, 1, 10)
            ledger.settle("namespace", "old-unknown", "outcome_unknown")
            marker = directory / ledger.INITIALIZED_NAME
            marker.write_bytes(b"torn")
            ledger.path.unlink()
            with self.assertRaises(LedgerError):
                NonceLedger(directory, retain_unsettled=True)
            self.assertEqual(marker.read_bytes(), b"torn")
            self.assertFalse(ledger.path.exists())

    def test_default_constructor_validates_ancestors_before_creating_any_child(self):
        for kind in ("symlink", "writable"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as root:
                target = Path(root) / "target"
                target.mkdir(mode=0o700)
                if kind == "symlink":
                    parent = Path(root) / "linked"
                    parent.symlink_to(target, target_is_directory=True)
                else:
                    parent = target
                    parent.chmod(0o777)
                with self.assertRaises(LedgerError):
                    NonceLedger(parent / "never-created")
                self.assertEqual(list(target.iterdir()), [])
