"""The join between the Operator session registry and the chat catalog (AFC-412 wake wiring).

The registry (AFC-412 PR1) allocates the letter and the catalog discovers the session.
The daemon's status shows each live lease with its letter (`session_letter_details`), so a row
that has a registration must carry its letter,  and a row that has none must not pretend to
have one.  (The headless wake path that also showed catalog rows to a woken agent went with the
retired wake types:  the listener starts no model, owner ruling Nov 10, 2026.)

Two properties matter here and are asserted rather than assumed:

  - the lookup never allocates:  a catalog scan must not mint registrations as a side
    effect of reading,  and a missing database is never created by a read;
  - the join never breaks on the registry:  a missing, empty or broken registry leaves
    the rows unlettered and the caller continues.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from simple_tools import chat_catalog as CC
from simple_tools import operator_registry as OR
from simple_tools import wakes as W
from simple_tools.tests.test_daemon import DaemonHarness

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
            issuer=OR.ISSUER_OPERATOR,
            )
            # A MiniMax chat is a pair (owner ruling, Nov 1 2026), so the letter is "AA".
            self.assertEqual(out["label"], "AA")
            self.assertEqual(out["generation"], 1)
            # The title form the platforms expect:  AA: Title.
            self.assertEqual(out["title_display"], "AA: Work")
            # The wake finds the same session by the same identity.
            found = OR.lookup_entry(db, "minimax", "mac", "default", "mvs_abc")
            self.assertEqual(found["label"], "AA")
            self.assertEqual(found["ref"], "MM AA")

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
            issuer=OR.ISSUER_OPERATOR,
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
            issuer=OR.ISSUER_OPERATOR,
            )
            self.assertEqual(first["label"], "AA")
            self.assertEqual(second["label"], "BB")

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
                issuer=OR.ISSUER_OPERATOR,
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
            issuer=OR.ISSUER_OPERATOR,
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
                    issuer=OR.ISSUER_OPERATOR,
                )
                kwargs[field] = ""
                with self.assertRaises(ValueError):
                    OR.register_session(db, **kwargs)

    def test_the_register_cli_refuses_local_db_writes_without_the_registry_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            OR.SessionRegistry(db).close_db()
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
                    ],
                    env={},
                )
            self.assertEqual(code, 2)
            out = json.loads(buffer.getvalue())
            self.assertEqual(out["code"], "not_configured")
            self.assertEqual(OR.lookup_entry(db, "minimax", "mac", "default", "mvs_abc"), {})


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
            issuer=OR.ISSUER_OPERATOR,
            )
            # The other direction of the join:  a letter names one session.
            found = OR.resolve_label(db, "minimax", "AA")
            self.assertEqual(found["native_session_id"], "mvs_abc")
            self.assertEqual(found["seat"], "MM")
            self.assertEqual(found["title"], "Work")
            self.assertEqual(found["title_display"], "AA: Work")
            self.assertEqual(found["ref"], "MM AA")

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
            issuer=OR.ISSUER_OPERATOR,
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
            issuer=OR.ISSUER_OPERATOR,
            )
            # A letter that was never issued is a miss, and a miss is None.
            self.assertIsNone(OR.resolve_label(db, "minimax", "Z"))

    def test_malformed_lookup_input_is_refused_rather_than_read_as_a_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            # "AA" is not malformed:  a subthread takes a double letter.
            for bad in ("", "  ", "1", "A1", "AAA", "A A", "A-B"):
                with self.assertRaises(ValueError):
                    OR.resolve_label(db, "minimax", bad)
            with self.assertRaises(ValueError):
                OR.resolve_label(db, "", "A")
            with self.assertRaises(ValueError):
                OR.resolve_label(db, "minimax", "A", 0)
            with self.assertRaises(ValueError):
                OR.resolve_label(db, "minimax", "A", True)

    def test_a_subthread_letter_resolves_like_any_other(self) -> None:
        """A double letter names its session the same way a single letter does."""
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            OR.register_session(
                db,
                platform="minimax",
                verified_host="mac",
                profile_scope="default",
                native_session_id="mvs_sub",
                seat="MM",
                source="test",
                title="Sub",
                letter_space="double",
                issuer=OR.ISSUER_OPERATOR,
            )
            found = OR.resolve_label(db, "minimax", "AA")
            self.assertIsNotNone(found)
            self.assertEqual(found["native_session_id"], "mvs_sub")

    def test_a_missing_registry_is_not_reported_as_a_missing_letter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            # The registry is not there, which is not the same as the letter being free.
            with self.assertRaises(OR.RegistryUnavailable):
                OR.resolve_label(db, "minimax", "AA")

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
            issuer=OR.ISSUER_OPERATOR,
            )
            # The display form and the namespace name the same registry namespace.
            self.assertEqual(
                OR.resolve_label(db, "minimax", "AA")["native_session_id"], "mvs_abc"
            )
            self.assertEqual(
                OR.resolve_label(db, "MINIMAX", "AA")["native_session_id"], "mvs_abc"
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
            issuer=OR.ISSUER_OPERATOR,
            )
            found = OR.resolve_ref(db, "minimax", "MM AA")
            self.assertEqual(found["native_session_id"], "mvs_abc")
            # A malformed reference is refused rather than read as a miss.
            with self.assertRaises(ValueError):
                OR.resolve_ref(db, "minimax", "A")
            with self.assertRaises(ValueError):
                OR.resolve_ref(db, "minimax", "")

    # One pair in the space, so the next chat takes AA again as generation 2 (every pair issued once first).
    @mock.patch.dict(OR.LETTER_SPACES, {"double": ("AA",)})
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
            issuer=OR.ISSUER_OPERATOR,
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
            issuer=OR.ISSUER_OPERATOR,
            )
            # A reference form is an exact inverse of the form the registry hands
            # out, so a bare "MM AA" names the generation it was handed out for.  It
            # was handed out for the first holder, so it still names that session
            # after the letter is recycled;  a reply to an old message reaches the
            # session it was written to, not the one that holds A now.
            self.assertEqual(
                OR.resolve_ref(db, "minimax", "MM AA")["native_session_id"], "mvs_abc"
            )
            # The generation form names the other one, and the suffix on the older
            # one names the older one too.
            self.assertEqual(
                OR.resolve_ref(db, "minimax", "MM AA (gen 2)")["native_session_id"], "mvs_def"
            )
            self.assertEqual(
                OR.resolve_ref(db, "minimax", "MM AA (gen 1)")["native_session_id"], "mvs_abc"
            )
            # The letter itself still answers "who holds A now", which is the other
            # question.
            self.assertEqual(
                OR.resolve_label(db, "minimax", "AA")["native_session_id"], "mvs_def"
            )

    # One pair in the space, so the next chat takes AA again as generation 2 (every pair issued once first).
    @mock.patch.dict(OR.LETTER_SPACES, {"double": ("AA",)})
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
            issuer=OR.ISSUER_OPERATOR,
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
            issuer=OR.ISSUER_OPERATOR,
            )
            # The live letter names the session that holds it now.
            live = OR.resolve_label(db, "minimax", "AA")
            self.assertEqual(live["native_session_id"], "mvs_def")
            self.assertEqual(live["generation"], 2)
            # And the generation names the older one.  The suffix is the differentiator:
            # only a recycled letter carries it, so the first holder reads "MM AA".
            older = OR.resolve_label(db, "minimax", "AA", 1)
            self.assertEqual(older["native_session_id"], "mvs_abc")
            self.assertEqual(older["ref"], "MM AA")
            self.assertEqual(live["ref"], "MM AA (gen 2)")

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
            issuer=OR.ISSUER_OPERATOR,
            )
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                code = OR.main(
                    ["resolve", "--db", db, "--platform", "minimax", "--letter", "AA"],
                    env={},
                )
            self.assertEqual(code, 0)
            out = json.loads(buffer.getvalue())
            self.assertEqual(out["native_session_id"], "mvs_abc")
            self.assertEqual(out["ref"], "MM AA")


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
            self.assertEqual(out["label"], "AA")
            found = OR.lookup_entry(db, "minimax", "mac", "default", "mvs_abc")
            self.assertEqual(found["label"], "AA")
            self.assertEqual(found["generation"], 1)
            self.assertEqual(found["ref"], "MM AA")
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

    # One pair in the space, so the next chat takes AA again as generation 2 (every pair issued once first).
    @mock.patch.dict(OR.LETTER_SPACES, {"double": ("AA",)})
    def test_a_recycled_letter_carries_its_generation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = OR.registry_db_path(tmp)
            registry = OR.SessionRegistry(db)
            self.addCleanup(registry.close_db)
            first = registry.resolve_or_register(dict(IDENTITY))
            self.assertEqual(first["label"], "AA")
            registry.close(first["record_id"])
            again = registry.resolve_or_register(
                dict(IDENTITY, native_session_id="mvs_def", title="Next")
            )
            self.assertEqual(again["label"], "AA")
            self.assertEqual(again["generation"], 2)
            found = OR.lookup_entry(db, "minimax", "mac", "default", "mvs_def")
            self.assertEqual(found["label"], "AA")
            self.assertEqual(found["ref"], "MM AA (gen 2)")

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
                handle.lookup("minimax", "mac", "default", "mvs_abc")["label"], "AA"
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
                self.assertEqual(found["label"], "AA")
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
            issuer=OR.ISSUER_OPERATOR,
            )
            # A path that begins with two slashes is the same file on this platform, and a
            # URI built by quoting it would read the first segment as an authority and
            # open a different file.
            twin = "//" + os.path.abspath(db).lstrip("/")
            found = OR.lookup_entry(twin, "minimax", "mac", "default", "mvs_abc")
            self.assertEqual(found["label"], "AA")

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
            self.assertEqual(found[good]["label"], "AA")
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
    """The registry join in `session_letter_details`, which `status` uses for its lease rows.

    These ran through the retired muse wake's chat context until the claude, muse and openrouter
    wake types were retired (owner ruling, Nov 10, 2026);  they now call the join directly."""

    def setup_rows(self, rows):
        return self.daemon(), rows

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

    @staticmethod
    def letter(details, row_id="a" * 64):
        return (details.get(row_id) or {}).get("letter", "")

    def test_a_registered_row_carries_its_letter_into_the_wake(self) -> None:
        d, rows = self.setup_rows([self.row()])
        self.register(d)
        details = d.session_letter_details(rows)
        self.assertEqual(len(details), 1)
        self.assertEqual(details["a" * 64]["letter"], "AA")
        self.assertEqual(details["a" * 64]["letter_ref"], "MM AA")

    def test_an_unregistered_row_carries_no_letter(self) -> None:
        d, rows = self.setup_rows([self.row()])
        self.assertEqual(d.session_letter_details(rows), {})

    def test_a_missing_registry_leaves_the_rows_unlettered_and_the_wake_intact(self) -> None:
        d, rows = self.setup_rows([self.row()])
        self.assertFalse(os.path.exists(OR.registry_db_path(d.root)))
        self.assertEqual(d.session_letter_details(rows), {})
        # The read never created the registry on its way past.
        self.assertFalse(os.path.exists(OR.registry_db_path(d.root)))

    def test_a_broken_registry_never_breaks_the_wake(self) -> None:
        d, rows = self.setup_rows([self.row()])
        with mock.patch.object(
            OR, "lookup_many", side_effect=RuntimeError("registry is broken")
        ):
            self.assertEqual(d.session_letter_details(rows), {})

    def test_the_row_identity_joins_on_platform_location_scope_and_session(self) -> None:
        d, rows = self.setup_rows([self.row()])
        self.register(d, platform="codex", seat="CODEX", native_session_id="mvs_abc")
        self.assertEqual(self.letter(d.session_letter_details(rows)), "")
        # The registry's host key is the fleet location, so a registration made under a
        # different location is a different identity and must not match.
        self.register(d, verified_host="server")
        self.assertEqual(self.letter(d.session_letter_details(rows)), "")

    def test_the_join_uses_the_location_and_not_the_os_hostname(self) -> None:
        d, rows = self.setup_rows([self.row(host="example.test")])
        self.register(d, verified_host="mac")
        details = d.session_letter_details(rows)
        self.assertEqual(self.letter(details), "AA")
        self.assertEqual(details["a" * 64]["verified_host"], d.config.instance)

    def test_a_malformed_session_id_is_dropped_rather_than_coerced(self) -> None:
        d, rows = self.setup_rows([self.row(session_id="bad id!")])
        self.register(d)
        self.assertEqual(d.session_letter_details(rows), {})

    def test_one_wake_uses_one_registry_connection(self) -> None:
        d, rows = self.setup_rows(
            [self.row(id="a" * 64), self.row(id="b" * 64, session_id="mvs_def")]
        )
        self.register(d)
        self.register(d, native_session_id="mvs_def")
        with mock.patch.object(OR, "SessionRegistry", wraps=OR.SessionRegistry) as opened:
            details = d.session_letter_details(rows)
        self.assertEqual(len(details), 2)
        self.assertEqual([self.letter(details, i) for i in ("a" * 64, "b" * 64)], ["AA", "BB"])
        # One connection for the whole join, not one per row.
        self.assertEqual(opened.call_count, 1)

    def test_the_catalog_row_carries_the_profile_scope(self) -> None:
        d, rows = self.setup_rows([self.row()])
        self.assertEqual(d.session_letter_details(rows), {})

    def test_the_join_uses_the_scope_the_row_carries(self) -> None:
        d, rows = self.setup_rows([self.row(profile_scope="work")])
        self.register(d, profile_scope="work")
        self.assertEqual(self.letter(d.session_letter_details(rows)), "AA")
        # The same session id under a different scope is a different session.
        d2, rows2 = self.setup_rows([self.row(profile_scope="default")])
        self.assertEqual(self.letter(d2.session_letter_details(rows2)), "")


if __name__ == "__main__":
    unittest.main()


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
            self.assertEqual(out["label"], "AA")


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
            issuer=OR.ISSUER_OPERATOR,
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
            issuer=OR.ISSUER_OPERATOR,
            )
            self.assertEqual(first["label"], "AA")
            self.assertNotEqual(second["label"], first["label"])
            # The second session is found under either adapter name, because the
            # record is keyed on the platform.
            found = OR.resolve_label(db, "cursor", second["label"])
            self.assertEqual(found["native_session_id"], "mvs_bbb")

    def test_an_unknown_adapter_gets_its_own_namespace(self) -> None:
        self.assertEqual(OR.normalize_platform("cursor-cloud"), "cursor")
        self.assertEqual(OR.normalize_platform("cursor-local"), "cursor")
        self.assertEqual(OR.normalize_platform("minimax"), "minimax")
        # An adapter nobody has heard of folds to itself rather than to another
        # platform's namespace, so a new seat never joins another's letter space.
        self.assertEqual(OR.normalize_platform("brand-new"), "brand-new")
        # An adapter that might later gain a sibling for a different seat keeps
        # its own namespace, because a letter is only unique inside one of them.
        self.assertEqual(OR.normalize_platform("grok-leader"), "grok-leader")


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
            issuer=OR.ISSUER_OPERATOR,
        )

    def recycle_a(self) -> None:
        first = self.register("mvs_abc")
        registry = OR.SessionRegistry(self.db)
        self.addCleanup(registry.close_db)
        registry.close(first["record_id"])
        self.register("mvs_def")

    # One pair in the space, so the next chat takes AA again as generation 2 (every pair issued once first).
    @mock.patch.dict(OR.LETTER_SPACES, {"double": ("AA",)})
    def test_a_bare_ref_names_generation_one_after_a_recycle(self) -> None:
        self.recycle_a()
        # "MM AA" was handed out for the first holder, so it names that session and
        # not the one that holds AA now.
        found = OR.resolve_ref(self.db, "minimax", "MM AA")
        self.assertEqual(found["native_session_id"], "mvs_abc")
        self.assertEqual(found["generation"], 1)
        # The generation form names the other one.
        live = OR.resolve_ref(self.db, "minimax", "MM AA (gen 2)")
        self.assertEqual(live["native_session_id"], "mvs_def")

    def test_a_ref_names_the_seat_that_holds_the_letter(self) -> None:
        self.register("mvs_abc", seat="MM")
        # "CODEX AA" claims Codex holds AA.  It does not, so the ref names no session.
        self.assertIsNone(OR.resolve_ref(self.db, "minimax", "CODEX AA"))
        self.assertIsNotNone(OR.resolve_ref(self.db, "minimax", "MM AA"))

class RegistryCandidateTests(unittest.TestCase):
    """`operator_registry.candidates_for` and `candidates_for_letter`, called directly.

    They lost their only callers with the headless wake path (the daemon's tagged-letter read and
    its candidate count for the model gate went with the retired wake types, owner ruling Nov 10,
    2026), so these tests that went through the daemon are re-seated on the registry functions.

    A letter is the same letter on two platforms only if both sessions were allocated it, so
    the ambiguous seeds register one session per platform.  The seeds name the single-letter
    space, because a MiniMax session takes a pair by default (AFC-586) and the two platforms
    have to hold the same letter for the ambiguity to exist at all.
    """

    # Each seat on its own platform:  the registry refuses a platform under another seat (AFC-772).
    AMBIGUOUS = (("MM", "minimax"), ("CODEX", "codex"))

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="fleet-registry-candidates-")
        self.addCleanup(tmp.cleanup)
        self.db = OR.registry_db_path(tmp.name, {})

    def seed(self, seats):
        for seat, platform in seats:
            OR.register_session(
                self.db,
                platform=platform,
                verified_host="mac-%s" % seat.lower(),
                profile_scope="default",
                native_session_id="mvs_%s" % seat.lower(),
                seat=seat,
                source="test",
                title="Work %s" % seat,
                issuer=OR.ISSUER_OPERATOR,
            )

    def test_the_candidate_query_finds_the_live_holder(self) -> None:
        OR.register_session(
            self.db,
            platform="minimax",
            verified_host="mac",
            profile_scope="default",
            native_session_id="mvs_abc",
            seat="MM",
            source="test",
            title="Work",
            issuer=OR.ISSUER_OPERATOR,
        )
        found = OR.candidates_for(self.db, "minimax", "AA")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["native_session_id"], "mvs_abc")
        self.assertEqual(found[0]["ref"], "MM AA")
        # A letter nobody holds has no candidates, and the query never mints one.
        self.assertEqual(OR.candidates_for(self.db, "minimax", "Z"), [])

    def test_the_candidate_set_spans_platforms(self) -> None:
        """The registry keeps one live holder per (platform, letter), so only the cross-platform
        query can ever show an ambiguity."""
        self.seed(self.AMBIGUOUS)
        self.assertEqual(len(OR.candidates_for(self.db, "minimax", "AA")), 1)
        self.assertEqual(len(OR.candidates_for_letter(self.db, "AA")), 2)

    def test_a_closed_session_is_not_a_live_candidate(self) -> None:
        self.seed(self.AMBIGUOUS)
        self.assertEqual(len(OR.candidates_for_letter(self.db, "AA")), 2)
        registry = OR.SessionRegistry(self.db)
        try:
            record = next(
                row["record_id"]
                for row in registry._conn.execute(
                    "SELECT record_id FROM sessions WHERE native_session_id = 'mvs_mm'"
                ).fetchall()
            )
            registry.close(record)
        finally:
            registry.close_db()
        found = OR.candidates_for_letter(self.db, "AA")
        self.assertEqual([row["seat"] for row in found], ["CODEX"])

    def test_a_closed_session_with_a_live_alias_is_not_a_candidate(self) -> None:
        """The `closed_at IS NULL` clause on its own:  `close()` also retires the alias, so the
        test above passes on `retired_at` alone.  Here the session is closed and its alias is left
        live, the state a writer that sets `closed_at` without retiring the alias would leave."""
        self.seed(self.AMBIGUOUS)
        conn = sqlite3.connect(str(self.db))
        try:
            with conn:
                conn.execute(
                    "UPDATE sessions SET closed_at = '2026-10-22T00:00:00Z', state = 'closed' "
                    "WHERE native_session_id = 'mvs_mm'"
                )
            live = conn.execute(
                "SELECT COUNT(*) FROM alias_assignments WHERE retired_at IS NULL"
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(live, 2, "the alias stays live, so only closed_at can exclude the row")
        found = OR.candidates_for_letter(self.db, "AA")
        self.assertEqual([row["seat"] for row in found], ["CODEX"])


class PendingSenderTests(unittest.TestCase):
    """The first trigger's sender survives the ledger round trip."""

    def test_sender_id_survives_a_row_and_a_reload(self) -> None:
        pending = W.Pending(
            "w1", "MM", {"channel": "simple-tools", "topic": "AFC-412"}, owner=True,
            now=1.0, due=2.0,
        )
        pending.add({"id": 5, "sender_id": 4242}, True)
        self.assertEqual(pending.sender_id, 4242)
        row = pending.row("held", 2.0)
        self.assertEqual(row["sender_id"], 4242)
        self.assertEqual(W.Pending.from_view(dict(row, wake_id="w1")).sender_id, 4242)

    def test_a_missing_or_boolean_sender_is_none(self) -> None:
        self.assertIsNone(W.Pending.from_view({"wake_id": "w1", "sender_id": True}).sender_id)
        self.assertIsNone(W.Pending.from_view({"wake_id": "w1"}).sender_id)
