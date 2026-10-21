"""The join between the Operator session registry and the chat catalog (AFC-412 wake wiring).

The registry (AFC-412 PR1) allocates the letter and the catalog discovers the session.
The wake path shows catalog rows to the woken agent, so a row that has a registration
must carry its letter,  and a row that has none must not pretend to have one.

Two properties matter here and are asserted rather than assumed:

  - the lookup never allocates:  a catalog scan must not mint registrations as a side
    effect of reading,  and a missing database is never created by a read;
  - the wake never breaks on the registry:  a missing, empty or broken registry leaves
    the rows unlettered and the wake continues.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from agent_sync import chat_catalog as CC
from agent_sync import operator_registry as OR
from agent_sync import wakes as W
from agent_sync.tests.test_daemon import DaemonHarness

IDENTITY = {
    "platform": "minimax",
    "verified_host": "mac",
    "profile_scope": "default",
    "native_session_id": "mvs_abc",
    "seat": "MM",
    "source": "test",
    "title": "Work",
}


class RegistryLookupTests(unittest.TestCase):
    def test_the_registry_path_is_one_shared_place_under_the_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                OR.registry_db_path(tmp), os.path.join(tmp, "operator", "registry.sqlite3")
            )

    def test_a_missing_database_is_not_created_and_reads_as_no_letter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            self.assertFalse(os.path.exists(db))
            self.assertEqual(OR.lookup_entry(db, "minimax", "mac", "default", "mvs_abc"), {})
            self.assertEqual(OR.lookup_label(db, "minimax", "mac", "default", "mvs_abc"), "")
            self.assertFalse(os.path.exists(db))

    def test_a_registered_session_reads_back_its_letter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            registry = OR.SessionRegistry(db)
            self.addCleanup(registry.close_db)
            out = registry.resolve_or_register(dict(IDENTITY))
            self.assertEqual(out["label"], "A")
            found = OR.lookup_entry(db, "minimax", "mac", "default", "mvs_abc")
            self.assertEqual(found["label"], "A")
            self.assertEqual(found["generation"], 1)
            self.assertEqual(found["ref"], "MM A")
            self.assertEqual(found["seat"], "MM")

    def test_an_unregistered_identity_reads_as_no_letter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            registry = OR.SessionRegistry(db)
            self.addCleanup(registry.close_db)
            registry.resolve_or_register(dict(IDENTITY))
            self.assertEqual(OR.lookup_entry(db, "minimax", "mac", "default", "mvs_other"), {})
            # The platform is part of the address:  the same session id on another
            # platform is a different session and never shares the letter.
            self.assertEqual(OR.lookup_entry(db, "codex", "mac", "default", "mvs_abc"), {})

    def test_a_recycled_letter_carries_its_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            registry = OR.SessionRegistry(db)
            self.addCleanup(registry.close_db)
            first = registry.resolve_or_register(dict(IDENTITY))
            self.assertEqual(first["label"], "A")
            registry.close(first["record_id"])
            again = registry.resolve_or_register(
                dict(IDENTITY, native_session_id="mvs_def", title="Next")
            )
            self.assertEqual(again["label"], "A")
            self.assertEqual(again["generation"], 2)
            found = OR.lookup_entry(db, "minimax", "mac", "default", "mvs_def")
            self.assertEqual(found["label"], "A")
            self.assertEqual(found["ref"], "MM A (gen 2)")

    def test_the_lookup_never_writes_to_the_database(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            registry = OR.SessionRegistry(db)
            self.addCleanup(registry.close_db)
            registry.resolve_or_register(dict(IDENTITY))
            before = os.stat(db)
            OR.lookup_entry(db, "minimax", "mac", "default", "mvs_abc")
            after = os.stat(db)
            self.assertEqual(before.st_size, after.st_size)

    def test_the_lookup_answers_while_a_writer_holds_the_write_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            registry = OR.SessionRegistry(db)
            self.addCleanup(registry.close_db)
            registry.resolve_or_register(dict(IDENTITY))
            # A writer holds the write lock.  A read-only lookup still answers, because
            # it never asks for a write lock of its own:  the wake path must not stall
            # behind the Operator registering a session.
            writer = sqlite3.connect(db, timeout=0.5)
            self.addCleanup(writer.close)
            writer.execute("BEGIN IMMEDIATE")
            try:
                found = OR.lookup_entry(db, "minimax", "mac", "default", "mvs_abc")
                self.assertEqual(found["label"], "A")
            finally:
                writer.rollback()

    def test_the_catalog_public_projection_keeps_the_profile_scope(self) -> None:
        row = CC.ChatCatalog._public(
            {
                "id": "x" * 64,
                "platform": "minimax",
                "host": "mac",
                "seat": "MM",
                "session_id": "mvs_abc",
                "profile_scope": "work",
                "cwd": "/repo",
                "repo": None,
                "title": "Work",
                "updated_at": 1.0,
                "status": "available",
                "stale": False,
                "capabilities": {"read": True, "wake": True},
            }
        )
        self.assertEqual(row["profile_scope"], "work")


class WakeLetterTests(DaemonHarness):
    def setup_rows(self, rows):
        d = self.daemon()
        r = d.seats["CLAUDE"]
        r.cfg.wake = "muse"
        d._chat_catalog = SimpleNamespace(
            discover=mock.Mock(return_value=rows),
            resolve=mock.Mock(return_value=rows[0] if rows else None),
            excerpt=mock.Mock(return_value=[]),
        )
        p = W.Pending(
            "wake",
            "CLAUDE",
            {"channel": "agent-sync", "topic": "work"},
            owner=True,
            now=self.clock.time(),
            due=self.clock.time(),
        )
        p.add({"id": 42}, True)
        return d, r, p

    def row(self, **over):
        base = {
            "id": "a" * 64,
            "seat": "MM",
            "platform": "minimax",
            "host": "mac",
            "profile_scope": "default",
            "session_id": "mvs_abc",
            "cwd": "/repo",
            "title": "Work",
            "updated_at": 100.0,
            "status": "available",
            "stale": False,
            "capabilities": {"read": True, "wake": True},
        }
        base.update(over)
        return base

    def register(self, d, **over):
        db = OR.registry_db_path(d.root)
        registry = OR.SessionRegistry(db)
        self.addCleanup(registry.close_db)
        identity = dict(IDENTITY, **over)
        return registry.resolve_or_register(identity)

    def test_a_registered_row_carries_its_letter_into_the_wake(self) -> None:
        d, r, p = self.setup_rows([self.row()])
        self.register(d)
        context = d.chat_context(r, p)
        self.assertEqual(len(context), 1)
        self.assertEqual(context[0]["label"], "A")
        self.assertEqual(context[0]["label_ref"], "MM A")

    def test_an_unregistered_row_carries_no_letter(self) -> None:
        d, r, p = self.setup_rows([self.row()])
        context = d.chat_context(r, p)
        self.assertEqual(context[0]["label"], "")
        self.assertEqual(context[0]["label_ref"], "")

    def test_a_missing_registry_leaves_the_rows_unlettered_and_the_wake_intact(self) -> None:
        d, r, p = self.setup_rows([self.row()])
        self.assertFalse(os.path.exists(OR.registry_db_path(d.root)))
        context = d.chat_context(r, p)
        self.assertEqual(len(context), 1)
        self.assertEqual(context[0]["label"], "")
        # The read never created the registry on its way past.
        self.assertFalse(os.path.exists(OR.registry_db_path(d.root)))

    def test_a_broken_registry_never_breaks_the_wake(self) -> None:
        d, r, p = self.setup_rows([self.row()])
        with mock.patch.object(
            OR, "lookup_entry", side_effect=RuntimeError("registry is broken")
        ):
            context = d.chat_context(r, p)
        self.assertEqual(len(context), 1)
        self.assertEqual(context[0]["label"], "")

    def test_the_row_identity_joins_on_platform_host_scope_and_session(self) -> None:
        d, r, p = self.setup_rows([self.row()])
        self.register(d, platform="codex", native_session_id="mvs_abc")
        context = d.chat_context(r, p)
        self.assertEqual(context[0]["label"], "")
        self.register(d, verified_host="server")
        context = d.chat_context(r, p)
        self.assertEqual(context[0]["label"], "")

    def test_the_catalog_row_carries_the_profile_scope(self) -> None:
        d, r, p = self.setup_rows([self.row()])
        labels = d.session_labels([self.row()])
        self.assertEqual(labels, {})

    def test_the_join_uses_the_scope_the_row_carries(self) -> None:
        d, r, p = self.setup_rows([self.row(profile_scope="work")])
        self.register(d, profile_scope="work")
        context = d.chat_context(r, p)
        self.assertEqual(context[0]["label"], "A")
        # The same session id under a different scope is a different session.
        d2, r2, p2 = self.setup_rows([self.row(profile_scope="default")])
        self.assertEqual(d2.chat_context(r2, p2)[0]["label"], "")


if __name__ == "__main__":
    unittest.main()
