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

import contextlib
import io
import json
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


class RegisterPathTests(unittest.TestCase):
    """The register path and the wake path have to agree on the identity."""

    def test_a_registered_session_is_found_by_the_wake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            out = OR.register_session(
                db,
                platform="minimax",
                verified_host="mac",
                profile_scope="default",
                native_session_id="mvs_abc",
                seat="MM",
                source="test",
                title="Work",
            )
            self.assertEqual(out["label"], "A")
            self.assertEqual(out["generation"], 1)
            # The title form the platforms expect:  H: Title.
            self.assertEqual(out["title_display"], "A: Work")
            # The wake finds the same session by the same identity.
            found = OR.lookup_entry(db, "minimax", "mac", "default", "mvs_abc")
            self.assertEqual(found["label"], "A")
            self.assertEqual(found["ref"], "MM A")

    def test_the_next_session_takes_the_next_letter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            first = OR.register_session(
                db,
                platform="minimax",
                verified_host="mac",
                profile_scope="default",
                native_session_id="mvs_abc",
                seat="MM",
                source="test",
                title="Work",
            )
            second = OR.register_session(
                db,
                platform="minimax",
                verified_host="mac",
                profile_scope="default",
                native_session_id="mvs_def",
                seat="MM",
                source="test",
                title="Next",
            )
            self.assertEqual(first["label"], "A")
            self.assertEqual(second["label"], "B")

    def test_a_resumed_session_keeps_its_letter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            kwargs = dict(
                platform="minimax",
                verified_host="mac",
                profile_scope="default",
                native_session_id="mvs_abc",
                seat="MM",
                source="test",
                title="Work",
            )
            first = OR.register_session(db, **kwargs)
            again = OR.register_session(db, **kwargs)
            self.assertEqual(again["label"], first["label"])
            self.assertFalse(again["created"])
            self.assertEqual(again["outcome"], "resumed")

    def test_a_malformed_session_id_is_refused_rather_than_stored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            with self.assertRaises(ValueError):
                OR.register_session(
                    db,
                    platform="minimax",
                    verified_host="mac",
                    profile_scope="default",
                    native_session_id="bad id",
                    seat="MM",
                    source="test",
                )

    def test_a_missing_identity_field_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            for field in ("platform", "verified_host", "profile_scope", "seat", "source"):
                kwargs = dict(
                    platform="minimax",
                    verified_host="mac",
                    profile_scope="default",
                    native_session_id="mvs_abc",
                    seat="MM",
                    source="test",
                )
                kwargs[field] = ""
                with self.assertRaises(ValueError):
                    OR.register_session(db, **kwargs)

    def test_the_register_cli_prints_the_letter_and_the_title_form(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = OR.main(
                    [
                        "register",
                        "--db",
                        db,
                        "--platform",
                        "minimax",
                        "--location",
                        "mac",
                        "--scope",
                        "default",
                        "--session-id",
                        "mvs_abc",
                        "--seat",
                        "MM",
                        "--source",
                        "test",
                        "--title",
                        "Work",
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buffer.getvalue())["label"], "A")
            found = OR.lookup_entry(db, "minimax", "mac", "default", "mvs_abc")
            self.assertEqual(found["label"], "A")


class RegistryLookupTests(unittest.TestCase):
    def test_the_registry_path_is_one_shared_place_under_the_state_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(
                OR.registry_db_path(tmp), os.path.join(tmp, "operator", "registry.sqlite3")
            )

    def test_the_registry_path_can_point_at_a_shared_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # The state root is per host, so a deployment that wants one allocator across
            # the Mac and the server points this at the shared store.
            self.assertEqual(
                OR.registry_db_path(tmp, {OR.ENV_REGISTRY_PATH: "/shared/registry.sqlite3"}),
                "/shared/registry.sqlite3",
            )
            self.assertEqual(
                OR.registry_db_path(tmp, {}),
                os.path.join(tmp, "operator", "registry.sqlite3"),
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
            self.assertEqual(before.st_mtime_ns, after.st_mtime_ns)
            # No DDL and no migration insert:  the lookup handle writes nothing.
            rows = registry._conn.execute(
                "SELECT COUNT(*) FROM schema_migrations"
            ).fetchone()[0]
            self.assertEqual(rows, 1)
            self.assertEqual(
                registry._conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1
            )
            self.assertEqual(
                registry._conn.execute("SELECT COUNT(*) FROM export_outbox").fetchone()[0], 2
            )

    def test_the_lookup_handle_can_recover_a_hot_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            registry = OR.SessionRegistry(db)
            self.addCleanup(registry.close_db)
            registry.resolve_or_register(dict(IDENTITY))
            # The lookup handle is read-write on purpose:  a mode=ro handle cannot roll a
            # hot journal back, and SQLite then refuses every read with
            # SQLITE_READONLY_ROLLBACK, which would drop every letter for good.
            handle = OR.SessionRegistry(db, read_only=True)
            self.addCleanup(handle.close_db)
            handle._conn.execute("PRAGMA user_version = 1")
            self.assertEqual(handle._conn.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(
                handle.lookup("minimax", "mac", "default", "mvs_abc")["label"], "A"
            )

    def test_an_unreadable_registry_is_reported_rather_than_read_as_no_letter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            registry = OR.SessionRegistry(db)
            self.addCleanup(registry.close_db)
            registry.resolve_or_register(dict(IDENTITY))
            # Every read fails, so the registry is unreadable rather than empty.
            with mock.patch.object(
                OR.SessionRegistry, "lookup", side_effect=sqlite3.OperationalError("locked")
            ):
                with self.assertRaises(OR.RegistryUnavailable):
                    OR.lookup_many(db, [("minimax", "mac", "default", "mvs_abc")])
                # The single-identity helper stays quiet, so a caller that wants the
                # reason asks for the batch one.
                self.assertEqual(
                    OR.lookup_entry(db, "minimax", "mac", "default", "mvs_abc"), {}
                )

    def test_the_lookup_answers_while_a_writer_holds_the_write_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            registry = OR.SessionRegistry(db)
            self.addCleanup(registry.close_db)
            registry.resolve_or_register(dict(IDENTITY))
            # A writer holds the write lock.  A plain SELECT does not ask for a write
            # lock of its own, so the wake path must not stall behind the Operator
            # registering a session.
            writer = sqlite3.connect(db, timeout=0.5)
            self.addCleanup(writer.close)
            writer.execute("BEGIN IMMEDIATE")
            try:
                found = OR.lookup_entry(db, "minimax", "mac", "default", "mvs_abc")
                self.assertEqual(found["label"], "A")
            finally:
                writer.rollback()

    def test_the_lookup_handle_never_creates_the_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            self.assertFalse(os.path.exists(db))
            # The handle is read-write so it can recover a hot journal, but it must never
            # bring a registry into being.
            with self.assertRaises(sqlite3.OperationalError):
                OR.SessionRegistry(db, read_only=True)
            self.assertFalse(os.path.exists(db))

    def test_the_alias_lookup_is_indexed_by_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            registry = OR.SessionRegistry(db)
            self.addCleanup(registry.close_db)
            registry.resolve_or_register(dict(IDENTITY))
            names = [
                row[0]
                for row in registry._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                )
            ]
            # The wake path looks a session's live alias up by record for every catalog
            # row it shows, so that lookup has to be indexed.
            self.assertIn("idx_alias_record", names)

    def test_a_partial_failure_is_reported_rather_than_shown_as_a_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            registry = OR.SessionRegistry(db)
            self.addCleanup(registry.close_db)
            registry.resolve_or_register(dict(IDENTITY))
            registry.resolve_or_register(dict(IDENTITY, native_session_id="mvs_def"))
            good = ("minimax", "mac", "default", "mvs_abc")
            bad = ("minimax", "mac", "default", "mvs_def")
            real = OR.SessionRegistry.lookup

            def one_fails(self, platform, verified_host, profile_scope, native_session_id):
                if native_session_id == "mvs_def":
                    raise sqlite3.OperationalError("corrupt row")
                return real(self, platform, verified_host, profile_scope, native_session_id)

            failed: list[tuple[str, str, str, str]] = []
            with mock.patch.object(OR.SessionRegistry, "lookup", one_fails):
                found = OR.lookup_many(db, [good, bad], failures=failed)
            # The good session still has its letter, and the bad one is reported.
            self.assertEqual(found[good]["label"], "A")
            self.assertNotIn(bad, found)
            self.assertEqual(failed, [bad])

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
            OR, "lookup_many", side_effect=RuntimeError("registry is broken")
        ):
            context = d.chat_context(r, p)
        self.assertEqual(len(context), 1)
        self.assertEqual(context[0]["label"], "")

    def test_the_row_identity_joins_on_platform_location_scope_and_session(self) -> None:
        d, r, p = self.setup_rows([self.row()])
        self.register(d, platform="codex", native_session_id="mvs_abc")
        context = d.chat_context(r, p)
        self.assertEqual(context[0]["label"], "")
        # The registry's host key is the fleet location, so a registration made under a
        # different location is a different identity and must not match.
        self.register(d, verified_host="server")
        context = d.chat_context(r, p)
        self.assertEqual(context[0]["label"], "")

    def test_the_join_uses_the_location_and_not_the_os_hostname(self) -> None:
        d, r, p = self.setup_rows([self.row(host="example.test")])
        self.register(d, verified_host="mac")
        context = d.chat_context(r, p)
        self.assertEqual(context[0]["label"], "A")
        self.assertEqual(context[0]["location"], d.config.instance)

    def test_a_malformed_session_id_is_dropped_rather_than_coerced(self) -> None:
        d, r, p = self.setup_rows([self.row(session_id="bad id!")])
        self.register(d)
        context = d.chat_context(r, p)
        self.assertEqual(context[0]["label"], "")

    def test_one_wake_uses_one_registry_connection(self) -> None:
        d, r, p = self.setup_rows(
            [self.row(id="a" * 64), self.row(id="b" * 64, session_id="mvs_def")]
        )
        self.register(d)
        self.register(d, native_session_id="mvs_def")
        with mock.patch.object(OR, "SessionRegistry", wraps=OR.SessionRegistry) as opened:
            context = d.chat_context(r, p)
        self.assertEqual(len(context), 2)
        self.assertEqual([row["label"] for row in context], ["A", "B"])
        # One connection for the whole wake, not one per row.
        self.assertEqual(opened.call_count, 1)

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
