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


    def test_a_letter_names_its_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            OR.register_session(
                db,
                platform="minimax",
                verified_host="mac",
                profile_scope="default",
                native_session_id="mvs_abc",
                seat="MM",
                source="test",
                title="Work",
            )
            # The other direction of the join:  a letter names one session.
            found = OR.resolve_label(db, "minimax", "A")
            self.assertEqual(found["native_session_id"], "mvs_abc")
            self.assertEqual(found["seat"], "MM")
            self.assertEqual(found["title"], "Work")
            self.assertEqual(found["title_display"], "A: Work")
            self.assertEqual(found["ref"], "MM A")

    def test_a_letter_is_unique_on_its_platform(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            OR.register_session(
                db,
                platform="minimax",
                verified_host="mac",
                profile_scope="default",
                native_session_id="mvs_abc",
                seat="MM",
                source="test",
                title="Work",
            )
            # The same letter on another platform is a different session.
            self.assertIsNone(OR.resolve_label(db, "codex", "A"))

    def test_an_unknown_letter_resolves_to_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            OR.register_session(
                db,
                platform="minimax",
                verified_host="mac",
                profile_scope="default",
                native_session_id="mvs_abc",
                seat="MM",
                source="test",
            )
            # A letter that was never issued is a miss, and a miss is None.
            self.assertIsNone(OR.resolve_label(db, "minimax", "Z"))

    def test_malformed_lookup_input_is_refused_rather_than_read_as_a_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            for bad in ("", "  ", "AA", "1", "A1"):
                with self.assertRaises(ValueError):
                    OR.resolve_label(db, "minimax", bad)
            with self.assertRaises(ValueError):
                OR.resolve_label(db, "", "A")
            with self.assertRaises(ValueError):
                OR.resolve_label(db, "minimax", "A", 0)
            with self.assertRaises(ValueError):
                OR.resolve_label(db, "minimax", "A", True)

    def test_a_missing_registry_is_not_reported_as_a_missing_letter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            # The registry is not there, which is not the same as the letter being free.
            with self.assertRaises(OR.RegistryUnavailable):
                OR.resolve_label(db, "minimax", "A")

    def test_the_platform_namespace_is_folded_on_both_sides(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            OR.register_session(
                db,
                platform="MiniMax",
                verified_host="mac",
                profile_scope="default",
                native_session_id="mvs_abc",
                seat="MM",
                source="test",
            )
            # The display form and the namespace name the same registry namespace.
            self.assertEqual(
                OR.resolve_label(db, "minimax", "A")["native_session_id"], "mvs_abc"
            )
            self.assertEqual(
                OR.resolve_label(db, "MINIMAX", "A")["native_session_id"], "mvs_abc"
            )

    def test_a_reference_form_names_its_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            OR.register_session(
                db,
                platform="minimax",
                verified_host="mac",
                profile_scope="default",
                native_session_id="mvs_abc",
                seat="MM",
                source="test",
                title="Work",
            )
            found = OR.resolve_ref(db, "minimax", "MM A")
            self.assertEqual(found["native_session_id"], "mvs_abc")
            # A malformed reference is refused rather than read as a miss.
            with self.assertRaises(ValueError):
                OR.resolve_ref(db, "minimax", "A")
            with self.assertRaises(ValueError):
                OR.resolve_ref(db, "minimax", "")

    def test_a_recycled_reference_names_the_older_session(self) -> None:
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
            registry = OR.SessionRegistry(db)
            self.addCleanup(registry.close_db)
            registry.close(first["record_id"])
            OR.register_session(
                db,
                platform="minimax",
                verified_host="mac",
                profile_scope="default",
                native_session_id="mvs_def",
                seat="MM",
                source="test",
                title="Next",
            )
            # A reference form is an exact inverse of the form the registry hands
            # out, so a bare "MM A" names the generation it was handed out for.  It
            # was handed out for the first holder, so it still names that session
            # after the letter is recycled;  a reply to an old message reaches the
            # session it was written to, not the one that holds A now.
            self.assertEqual(
                OR.resolve_ref(db, "minimax", "MM A")["native_session_id"], "mvs_abc"
            )
            # The generation form names the other one, and the suffix on the older
            # one names the older one too.
            self.assertEqual(
                OR.resolve_ref(db, "minimax", "MM A (gen 2)")["native_session_id"], "mvs_def"
            )
            self.assertEqual(
                OR.resolve_ref(db, "minimax", "MM A (gen 1)")["native_session_id"], "mvs_abc"
            )
            # The letter itself still answers "who holds A now", which is the other
            # question.
            self.assertEqual(
                OR.resolve_label(db, "minimax", "A")["native_session_id"], "mvs_def"
            )

    def test_a_recycled_letter_names_the_right_generation(self) -> None:
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
            registry = OR.SessionRegistry(db)
            self.addCleanup(registry.close_db)
            registry.close(first["record_id"])
            OR.register_session(
                db,
                platform="minimax",
                verified_host="mac",
                profile_scope="default",
                native_session_id="mvs_def",
                seat="MM",
                source="test",
                title="Next",
            )
            # The live letter names the session that holds it now.
            live = OR.resolve_label(db, "minimax", "A")
            self.assertEqual(live["native_session_id"], "mvs_def")
            self.assertEqual(live["generation"], 2)
            # And the generation names the older one.  The suffix is the differentiator:
            # only a recycled letter carries it, so the first holder reads "MM A".
            older = OR.resolve_label(db, "minimax", "A", 1)
            self.assertEqual(older["native_session_id"], "mvs_abc")
            self.assertEqual(older["ref"], "MM A")
            self.assertEqual(live["ref"], "MM A (gen 2)")

    def test_the_resolve_command_names_the_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            OR.register_session(
                db,
                platform="minimax",
                verified_host="mac",
                profile_scope="default",
                native_session_id="mvs_abc",
                seat="MM",
                source="test",
                title="Work",
            )
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = OR.main(
                    ["resolve", "--db", db, "--platform", "minimax", "--letter", "A"]
                )
            self.assertEqual(code, 0)
            out = json.loads(buffer.getvalue())
            self.assertEqual(out["native_session_id"], "mvs_abc")
            self.assertEqual(out["ref"], "MM A")


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

    def test_the_lookup_never_creates_the_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            self.assertFalse(os.path.exists(db))
            # The lookup checks the file first, so a missing registry is a miss.
            self.assertEqual(
                OR.lookup_many(db, [("minimax", "mac", "default", "mvs_abc")]), {}
            )
            self.assertFalse(os.path.exists(db))
            # And the handle itself refuses a missing file, so a registry that vanishes
            # between the check and the open is not brought back into being.
            with self.assertRaises(sqlite3.OperationalError):
                OR.SessionRegistry(db, read_only=True)
            self.assertFalse(os.path.exists(db))

    def test_a_path_with_two_leading_slashes_still_opens_the_same_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            OR.register_session(
                db,
                platform="minimax",
                verified_host="mac",
                profile_scope="default",
                native_session_id="mvs_abc",
                seat="MM",
                source="test",
            )
            # A path that begins with two slashes is the same file on this platform, and a
            # URI built by quoting it would read the first segment as an authority and
            # open a different file.
            twin = "//" + os.path.abspath(db).lstrip("/")
            found = OR.lookup_entry(twin, "minimax", "mac", "default", "mvs_abc")
            self.assertEqual(found["label"], "A")

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


class DirectMessageLetterTests(DaemonHarness):
    """A direct message carries no letter (owner ruling, Oct 21 2026).

    The letter is for a tagged address, and a DM is owner-only plain replies.  So a DM
    context carries no label at all, rather than a letter the owner has to be told to
    ignore.
    """

    def setup_rows(self, rows, private=False):
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
            {"type": "private" if private else "stream", "channel": "agent-sync", "topic": "work"},
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

    def test_a_direct_message_carries_no_letter(self) -> None:
        d, r, p = self.setup_rows([self.row()], private=True)
        self.register(d)
        context = d.chat_context(r, p)
        self.assertEqual(len(context), 1)
        self.assertEqual(context[0]["label"], "")
        self.assertEqual(context[0]["label_ref"], "")

    def test_a_channel_message_still_carries_the_letter(self) -> None:
        d, r, p = self.setup_rows([self.row()], private=False)
        self.register(d)
        context = d.chat_context(r, p)
        self.assertEqual(context[0]["label"], "A")
        self.assertEqual(context[0]["label_ref"], "MM A")


class AddressedRowTests(DaemonHarness):
    """A tagged address names one session, so that session has to be in the wake.

    The rows are ranked and cut before the letters join, so without this the addressed
    session could be cut out of the list and the address would name a session the wake
    never saw.
    """

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

    def test_the_addressed_session_is_in_the_wake_even_when_it_is_cut(self) -> None:
        # Forty rows, none of them the addressed one:  the addressed session is
        # last by rank, so the cut would drop it.
        rows = [
            self.row(id="%064d" % i, session_id="mvs_%03d" % i, title="Session %d" % i)
            for i in range(40)
        ]
        d, r, p = self.setup_rows(rows)
        self.register(d, native_session_id="mvs_039")
        context = d.chat_context(r, p, None, "A")
        titles = [str(row.get("title")) for row in context]
        self.assertIn("Session 39", titles)

    def test_no_letter_leaves_the_rows_alone(self) -> None:
        rows = [self.row(id="%064d" % i, session_id="mvs_%03d" % i) for i in range(40)]
        d, r, p = self.setup_rows(rows)
        self.register(d, native_session_id="mvs_039")
        context = d.chat_context(r, p, None, "")
        titles = [str(row.get("title")) for row in context]
        self.assertNotIn("Session 39", titles)

    def test_a_letter_nobody_holds_names_no_row(self) -> None:
        rows = [self.row()]
        d, r, p = self.setup_rows(rows)
        self.register(d)
        context = d.chat_context(r, p, None, "Z")
        self.assertEqual(len(context), 1)
        self.assertEqual(context[0]["label"], "A")


if __name__ == "__main__":
    unittest.main()


class TaggedLetterTests(DaemonHarness):
    """A letter is read off a trigger only in the shape the owner ruled.

    The letter sits next to the mention, the way the owner ruled:  "@**MiniMax** Z".
    A capital letter in ordinary prose is never an address, and a bare letter with no
    mention is not an address at all.
    """

    def bot(self, d, seat="CLAUDE", name="Claude", uid=1212600, email="claude-bot@simplewithus.zulipchat.com"):
        runner = d.seats["CLAUDE"]
        runner.users[uid] = {
            "user_id": uid,
            "full_name": name,
            "email": email,
            "is_bot": True,
            "is_active": True,
            "bot_owner_id": d.config.owner_user_id,
        }
        d.fleet_tags = frozenset({seat})
        return runner

    def test_a_letter_next_to_the_mention_is_read(self) -> None:
        d = self.daemon()
        self.bot(d)
        history = [{"id": 1, "content": "@**Claude** Z what is the state of the closeout?"}]
        self.assertEqual(d.tagged_letter(d.seats["CLAUDE"], history), "Z")

    def test_the_id_form_of_the_mention_is_read_too(self) -> None:
        d = self.daemon()
        self.bot(d)
        history = [{"id": 1, "content": "@**Claude|1212600** Z what is the state of the closeout?"}]
        self.assertEqual(d.tagged_letter(d.seats["CLAUDE"], history), "Z")

    def test_a_capital_letter_in_prose_is_not_an_address(self) -> None:
        d = self.daemon()
        self.bot(d)
        history = [{"id": 1, "content": "@**Claude** the plan is A and B, so pick one"}]
        self.assertEqual(d.tagged_letter(d.seats["CLAUDE"], history), "")

    def test_a_bare_letter_with_no_mention_is_not_an_address(self) -> None:
        d = self.daemon()
        self.bot(d)
        history = [{"id": 1, "content": "is Z still running?"}]
        self.assertEqual(d.tagged_letter(d.seats["CLAUDE"], history), "")

    def test_a_mention_of_another_seat_is_not_an_address(self) -> None:
        d = self.daemon()
        self.bot(d)
        history = [{"id": 1, "content": "@**Codex** Z is that yours?"}]
        self.assertEqual(d.tagged_letter(d.seats["CLAUDE"], history), "")

    def test_the_candidate_query_finds_the_live_holder(self) -> None:
        d = self.daemon()
        db = OR.registry_db_path(d.root, d.env)
        OR.register_session(
            db,
            platform="minimax",
            verified_host="mac",
            profile_scope="default",
            native_session_id="mvs_abc",
            seat="MM",
            source="test",
            title="Work",
        )
        found = d.operator_candidates("minimax", "A")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["native_session_id"], "mvs_abc")
        self.assertEqual(found[0]["ref"], "MM A")
        # A letter nobody holds has no candidates, and the query never mints one.
        self.assertEqual(d.operator_candidates("minimax", "Z"), [])


class OperatorIssuesLettersTests(unittest.TestCase):
    """The owner ruled (Oct 21 2026) that no agent issues its own letter:  the Operator does.

    A seat that could register itself would be choosing its own handle, and the handle
    is what decides who a reply reaches.  So the register path refuses every issuer but
    the Operator, and it refuses before anything is written.
    """

    def test_a_seat_cannot_issue_its_own_letter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            with self.assertRaises(ValueError) as caught:
                OR.register_session(
                    db,
                    platform="minimax",
                    verified_host="mac",
                    profile_scope="default",
                    native_session_id="mvs_abc",
                    seat="MM",
                    source="test",
                    title="Work",
                    issuer="MM",
                )
            self.assertIn("Operator", str(caught.exception))
            # Nothing was written:  a refused issue leaves no letter behind.
            self.assertFalse(os.path.exists(db))

    def test_the_operator_issues_the_letter(self) -> None:
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
                issuer=OR.ISSUER_OPERATOR,
            )
            self.assertEqual(out["label"], "A")


class PlatformNamespaceTests(unittest.TestCase):
    """One letter space per platform, not per adapter.

    The catalog discovers Cursor through "cursor-cloud" and "cursor-local", and both are
    Cursor.  Keyed on the adapter, one Cursor session under each would be issued the
    same letter twice, and both would read as "CURSOR A".
    """

    def test_two_adapters_of_one_platform_share_the_letter_space(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            first = OR.register_session(
                db,
                platform="cursor-cloud",
                verified_host="mac",
                profile_scope="default",
                native_session_id="mvs_aaa",
                seat="CURSOR",
                source="test",
                title="Cloud",
            )
            second = OR.register_session(
                db,
                platform="cursor-local",
                verified_host="mac",
                profile_scope="default",
                native_session_id="mvs_bbb",
                seat="CURSOR",
                source="test",
                title="Local",
            )
            self.assertEqual(first["label"], "A")
            self.assertNotEqual(second["label"], first["label"])
            # The second session is found under either adapter name, because the
            # record is keyed on the platform.
            found = OR.resolve_label(db, "cursor", second["label"])
            self.assertEqual(found["native_session_id"], "mvs_bbb")

    def test_an_unknown_adapter_gets_its_own_namespace(self) -> None:
        self.assertEqual(OR.normalize_platform("cursor-cloud"), "cursor")
        self.assertEqual(OR.normalize_platform("grok-leader"), "grok")
        self.assertEqual(OR.normalize_platform("minimax"), "minimax")
        # An adapter nobody has heard of folds to itself rather than to another
        # platform's namespace.
        self.assertEqual(OR.normalize_platform("brand-new"), "brand-new")


class ReferenceFormTests(unittest.TestCase):
    """The reference form is an exact inverse of the form the registry hands out."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = OR.registry_db_path(self.tmp.name)

    def register(self, native: str, seat: str = "MM", title: str = "Work"):
        return OR.register_session(
            self.db,
            platform="minimax",
            verified_host="mac",
            profile_scope="default",
            native_session_id=native,
            seat=seat,
            source="test",
            title=title,
        )

    def recycle_a(self) -> None:
        first = self.register("mvs_abc")
        registry = OR.SessionRegistry(self.db)
        self.addCleanup(registry.close_db)
        registry.close(first["record_id"])
        self.register("mvs_def")

    def test_a_bare_ref_names_generation_one_after_a_recycle(self) -> None:
        self.recycle_a()
        # "MM A" was handed out for the first holder, so it names that session and
        # not the one that holds A now.
        found = OR.resolve_ref(self.db, "minimax", "MM A")
        self.assertEqual(found["native_session_id"], "mvs_abc")
        self.assertEqual(found["generation"], 1)
        # The generation form names the other one.
        live = OR.resolve_ref(self.db, "minimax", "MM A (gen 2)")
        self.assertEqual(live["native_session_id"], "mvs_def")

    def test_a_ref_names_the_seat_that_holds_the_letter(self) -> None:
        self.register("mvs_abc", seat="MM")
        # "CODEX A" claims Codex holds A.  It does not, so the ref names no session.
        self.assertIsNone(OR.resolve_ref(self.db, "minimax", "CODEX A"))
        self.assertIsNotNone(OR.resolve_ref(self.db, "minimax", "MM A"))
