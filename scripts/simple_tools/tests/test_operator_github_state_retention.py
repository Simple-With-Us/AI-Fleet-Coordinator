"""Durable replay retention uses synthetic metadata and private temporary files only."""

import contextlib
import json
import os
import sqlite3
import stat
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

from simple_tools import operator_github_state as state


class RetainedNonceLedgerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.directory = self.root / "ledger"
        self.ledger = state.NonceLedger(self.directory, retain_unsettled=True)
        self.metadata = json.dumps({"phase": "prepared", "digest": "a" * 64})

    def begin(self, nonce="request", *, expires=100, now=1, capacity=10, response=None):
        return self.ledger.begin(
            "settings", nonce, expires, now, capacity,
            response=self.metadata if response is None else response,
        )

    def mutate(self, sql, args=()):
        with contextlib.closing(sqlite3.connect(self.ledger.path)) as db, db:
            db.execute(sql, args)

    def test_legacy_mode_still_expires_every_outcome(self):
        for outcome in ("pending", "outcome_unknown", "replay", "ok", "refused"):
            with self.subTest(outcome=outcome):
                ledger = state.NonceLedger(self.root / outcome)
                self.assertEqual(ledger.begin("legacy", "same", 10, 1, 1), "fresh")
                ledger.settle("legacy", "same", outcome)
                self.assertEqual(ledger.begin("legacy", "same", 30, 11, 1), "fresh")
                ledger.release("legacy", "same")
                self.assertIsNone(ledger.lookup("legacy", "same"))

    def test_all_unsettled_states_survive_expiry_and_restart(self):
        for outcome in ("pending", "outcome_unknown", "unknown", "replay"):
            with self.subTest(outcome=outcome):
                nonce = "request-" + outcome
                self.assertEqual(self.begin(nonce, expires=10), "fresh")
                if outcome == "replay":
                    self.ledger.settle("settings", nonce, "ok", self.metadata)
                    self.assertEqual(
                        self.ledger.begin("settings", nonce, 10, 2, 10),
                        ("repeat-claimed", "ok", self.metadata),
                    )
                else:
                    self.ledger.settle("settings", nonce, outcome, self.metadata)
                reopened = state.NonceLedger(self.directory, retain_unsettled=True)
                self.assertEqual(
                    reopened.begin("settings", nonce, 999999, 999998, 10),
                    ("repeat", outcome, self.metadata),
                )

    def test_held_unsettled_rows_refuse_new_work_at_capacity(self):
        self.assertEqual(self.begin("held", expires=2, capacity=1), "fresh")
        self.ledger.settle("settings", "held", "outcome_unknown", self.metadata)
        self.assertEqual(self.begin("new", now=100, capacity=1), "capacity")
        self.assertEqual(self.ledger.lookup("settings", "held"), ("outcome_unknown", self.metadata))
        self.assertIsNone(self.ledger.lookup("settings", "new"))

    def test_namespace_barrier_excludes_only_the_current_request(self):
        self.assertFalse(self.ledger.has_unsettled("settings", "new"))
        self.assertEqual(self.begin("same", expires=2), "fresh")
        self.assertFalse(self.ledger.has_unsettled("settings", "same"))
        self.assertFalse(self.ledger.has_unsettled("other-namespace", "new"))
        for outcome in ("pending", "outcome_unknown", "unknown", "replay"):
            with self.subTest(outcome=outcome):
                self.mutate("UPDATE nonces SET outcome=?", (outcome,))
                self.assertTrue(self.ledger.has_unsettled("settings", "new"))
        self.ledger.settle("settings", "same", "refused", self.metadata)
        self.assertFalse(self.ledger.has_unsettled("settings", "new"))

    def test_namespace_barrier_fails_closed_on_invalid_storage(self):
        self.assertEqual(self.begin(), "fresh")
        self.mutate("UPDATE nonces SET response=?", (sqlite3.Binary(b"PRIVATE_MARKER"),))
        with self.assertRaises(state.LedgerError) as caught:
            self.ledger.has_unsettled("settings", "new")
        self.assertEqual(str(caught.exception), "replay storage unavailable")

    def test_refused_cancellation_tombstone_lives_until_original_expiry(self):
        self.assertEqual(self.begin(expires=10), "fresh")
        cancelled = json.dumps({"phase": "cancelled", "code": "NOT_FOUND"})
        self.ledger.settle("settings", "request", "refused", cancelled)
        self.assertEqual(self.begin(now=10), ("repeat", "refused", cancelled))
        with self.assertRaises(state.LedgerError):
            self.ledger.release("settings", "request")
        self.assertEqual(self.begin(now=11, expires=50), "fresh")

    def test_ok_terminal_row_expires_but_replay_claim_does_not(self):
        self.assertEqual(self.begin("complete", expires=10), "fresh")
        self.ledger.settle("settings", "complete", "ok", self.metadata)
        self.assertEqual(self.begin("complete", now=11), "fresh")
        self.assertEqual(self.begin("claimed", expires=10), "fresh")
        self.ledger.settle("settings", "claimed", "ok", self.metadata)
        self.assertEqual(self.begin("claimed", now=2), ("repeat-claimed", "ok", self.metadata))
        self.assertEqual(self.begin("claimed", now=100), ("repeat", "replay", self.metadata))

    def test_metadata_is_in_the_same_durable_pending_claim(self):
        self.assertEqual(self.begin(), "fresh")
        reopened = state.NonceLedger(self.directory, retain_unsettled=True)
        self.assertEqual(reopened.lookup("settings", "request"), ("pending", self.metadata))
        replacement = json.dumps({"phase": "sending", "digest": "b" * 64})
        self.assertEqual(
            reopened.begin("settings", "request", 100, 2, 10, response=replacement),
            ("repeat", "pending", self.metadata),
        )
        reopened.settle("settings", "request", "pending", replacement)
        self.assertEqual(self.ledger.lookup("settings", "request"), ("pending", replacement))

    def test_parallel_claims_store_exactly_the_winners_metadata(self):
        other = state.NonceLedger(self.directory, retain_unsettled=True)
        barrier = threading.Barrier(2)

        def claim(ledger, text):
            barrier.wait(timeout=5)
            return text, ledger.begin("settings", "same", 100, 1, 10, response=text)

        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(claim, self.ledger, "first-metadata")
            two = pool.submit(claim, other, "second-metadata")
            answers = [one.result(timeout=10), two.result(timeout=10)]
        winner = [text for text, result in answers if result == "fresh"]
        self.assertEqual(len(winner), 1)
        self.assertEqual(self.ledger.lookup("settings", "same"), ("pending", winner[0]))
        self.assertEqual(
            [result for _text, result in answers if result != "fresh"],
            [("repeat", "pending", winner[0])],
        )

    def test_only_one_ok_replay_can_be_claimed(self):
        self.assertEqual(self.begin(), "fresh")
        self.ledger.settle("settings", "request", "ok", self.metadata)
        other = state.NonceLedger(self.directory, retain_unsettled=True)
        self.assertEqual(self.begin(now=2), ("repeat-claimed", "ok", self.metadata))
        self.assertEqual(
            other.begin("settings", "request", 100, 2, 10),
            ("repeat", "replay", self.metadata),
        )

    def test_lookup_is_read_only_and_settle_requires_an_existing_claim(self):
        self.assertIsNone(self.ledger.lookup("settings", "absent"))
        with self.assertRaises(state.LedgerError):
            self.ledger.settle("settings", "absent", "ok", self.metadata)
        self.assertIsNone(self.ledger.lookup("settings", "absent"))

    def test_corrupt_or_truncated_database_fails_closed_with_fixed_error(self):
        for payload in (b"", b"not a database containing PRIVATE_MARKER"):
            with self.subTest(length=len(payload)):
                self.ledger.path.write_bytes(payload)
                with self.assertRaises(state.LedgerError) as caught:
                    state.NonceLedger(self.directory, retain_unsettled=True)
                self.assertEqual(str(caught.exception), "replay storage unavailable")
                self.assertNotIn("PRIVATE_MARKER", str(caught.exception))

    def test_invalid_stored_rows_are_never_returned(self):
        self.assertEqual(self.begin(), "fresh")
        for column, value in (
            ("outcome", "PRIVATE_MARKER"),
            ("expires", float("inf")),
            ("response", sqlite3.Binary(b"PRIVATE_MARKER")),
            ("response", "PRIVATE_MARKER" * 6000),
        ):
            with self.subTest(column=column, kind=type(value).__name__):
                self.mutate("UPDATE nonces SET " + column + "=?", (value,))
                with self.assertRaises(state.LedgerError) as caught:
                    self.ledger.lookup("settings", "request")
                self.assertEqual(str(caught.exception), "replay storage unavailable")
                self.assertNotIn("PRIVATE_MARKER", str(caught.exception))
                self.mutate(
                    "UPDATE nonces SET outcome='pending',expires=100,response=?", (self.metadata,)
                )

    def test_unexpected_schema_and_trigger_fail_closed(self):
        self.mutate("CREATE TRIGGER erase_pending AFTER INSERT ON nonces BEGIN DELETE FROM nonces; END")
        with self.assertRaises(state.LedgerError):
            self.ledger.lookup("settings", "request")
        self.mutate("DROP TRIGGER erase_pending")
        self.mutate("ALTER TABLE nonces ADD COLUMN extra TEXT")
        with self.assertRaises(state.LedgerError):
            self.ledger.lookup("settings", "request")

    def test_new_oversized_metadata_is_refused_before_a_row_exists(self):
        with self.assertRaises(state.LedgerError):
            self.begin(response="x" * (self.ledger.MAX_RESPONSE_BYTES + 1))
        self.assertIsNone(self.ledger.lookup("settings", "request"))

    def test_oversized_row_count_is_refused_without_loading_metadata(self):
        with contextlib.closing(sqlite3.connect(self.ledger.path)) as db, db:
            db.executemany(
                "INSERT INTO nonces VALUES ('settings',?,100,'pending',NULL)",
                [(str(index),) for index in range(self.ledger.MAX_RETAINED_ROWS + 1)],
            )
        with self.assertRaises(state.LedgerError):
            self.ledger.lookup("settings", "missing")

    def test_unsafe_directory_database_and_sidecar_are_refused(self):
        for mode in (0o755, 0o770):
            with self.subTest(directory_mode=mode):
                self.directory.chmod(mode)
                with self.assertRaises(state.LedgerError):
                    self.ledger.lookup("settings", "request")
                self.directory.chmod(0o700)
        self.ledger.path.chmod(0o644)
        with self.assertRaises(state.LedgerError):
            self.ledger.lookup("settings", "request")
        self.ledger.path.chmod(0o600)
        alias = self.root / "alias.sqlite3"
        os.link(self.ledger.path, alias)
        with self.assertRaises(state.LedgerError):
            self.ledger.lookup("settings", "request")
        alias.unlink()
        sidecar = self.directory / "nonces.sqlite3-journal"
        sidecar.symlink_to(self.ledger.path)
        with self.assertRaises(state.LedgerError):
            self.ledger.lookup("settings", "request")
        sidecar.unlink()

    def test_symlink_ancestor_and_writable_parent_are_refused(self):
        linked = self.root / "linked"
        linked.symlink_to(self.directory, target_is_directory=True)
        with self.assertRaises(state.LedgerError):
            state.NonceLedger(linked, retain_unsettled=True)
        writable = self.root / "writable"
        writable.mkdir(mode=0o777)
        writable.chmod(0o777)
        with self.assertRaises(state.LedgerError):
            state.NonceLedger(writable / "private", retain_unsettled=True)

    def test_replaced_database_is_not_silently_accepted(self):
        original = self.directory / "original.sqlite3"
        self.ledger.path.rename(original)
        with contextlib.closing(sqlite3.connect(self.ledger.path)) as db, db:
            db.execute("CREATE TABLE unrelated(value TEXT)")
        self.ledger.path.chmod(0o600)
        with self.assertRaises(state.LedgerError):
            self.ledger.lookup("settings", "request")

    def test_constructor_closes_its_connection(self):
        closed = []

        class ObservedConnection(sqlite3.Connection):
            def close(self):
                closed.append(True)
                return super().close()

        original = sqlite3.connect
        with mock.patch.object(state.sqlite3, "connect", side_effect=lambda *a, **k: original(*a, **k, factory=ObservedConnection)):
            state.NonceLedger(self.root / "close-check", retain_unsettled=True)
        self.assertEqual(closed, [True])

    def test_new_directory_and_database_entries_are_durable_before_connect(self):
        events = []
        original_mkdir, original_open, original_fsync = os.mkdir, os.open, os.fsync
        original_connect = sqlite3.connect

        def identity(fd):
            info = os.fstat(fd)
            return info.st_dev, info.st_ino

        def mkdir(path, *args, **kwargs):
            parent = kwargs.get("dir_fd")
            answer = original_mkdir(path, *args, **kwargs)
            events.append(("mkdir", identity(parent)))
            return answer

        def opening(path, flags, *args, **kwargs):
            fd = original_open(path, flags, *args, **kwargs)
            if path == "nonces.sqlite3" and flags & os.O_CREAT and flags & os.O_EXCL:
                events.append(("create", identity(fd), identity(kwargs["dir_fd"])))
            return fd

        def sync(fd):
            events.append(("fsync", identity(fd), stat.S_ISDIR(os.fstat(fd).st_mode)))
            return original_fsync(fd)

        def connect(*args, **kwargs):
            events.append(("connect",))
            return original_connect(*args, **kwargs)

        with (
            mock.patch.object(state.os, "mkdir", side_effect=mkdir),
            mock.patch.object(state.os, "open", side_effect=opening),
            mock.patch.object(state.os, "fsync", side_effect=sync),
            mock.patch.object(state.sqlite3, "connect", side_effect=connect),
        ):
            state.NonceLedger(self.root / "durable" / "nested", retain_unsettled=True)
        self.assertEqual(sum(event[0] == "mkdir" for event in events), 2)
        for index, event in enumerate(events):
            if event[0] == "mkdir":
                self.assertEqual(events[index + 1], ("fsync", event[1], True))
            if event[0] == "create":
                self.assertEqual(events[index + 1], ("fsync", event[1], False))
                self.assertEqual(events[index + 2], ("fsync", event[2], True))
                self.assertLess(index + 2, events.index(("connect",)))

    def test_directory_fsync_failure_refuses_before_database_connection(self):
        with (
            mock.patch.object(state.os, "fsync", side_effect=OSError("PRIVATE_MARKER")),
            mock.patch.object(state.sqlite3, "connect") as connect,
        ):
            with self.assertRaises(state.LedgerError) as caught:
                state.NonceLedger(self.root / "cannot-sync", retain_unsettled=True)
        self.assertEqual(str(caught.exception), "replay storage unavailable")
        connect.assert_not_called()

    def test_new_database_directory_entry_fsync_failure_refuses_before_connect(self):
        directory = self.root / "file-entry"
        directory.mkdir(mode=0o700)
        original_fsync = os.fsync

        def sync(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError("PRIVATE_MARKER")
            return original_fsync(fd)

        with (
            mock.patch.object(state.os, "fsync", side_effect=sync),
            mock.patch.object(state.sqlite3, "connect") as connect,
        ):
            with self.assertRaises(state.LedgerError) as caught:
                state.NonceLedger(directory, retain_unsettled=True)
        self.assertEqual(str(caught.exception), "replay storage unavailable")
        connect.assert_not_called()
        # A failed initializer cannot turn the leftover empty file into a fresh ledger.
        with self.assertRaises(state.LedgerError):
            state.NonceLedger(directory, retain_unsettled=True)


if __name__ == "__main__":
    unittest.main()
