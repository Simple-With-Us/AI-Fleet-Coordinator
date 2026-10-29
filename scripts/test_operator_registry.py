"""Tests for agent_sync.operator_registry (AFC-412 PR1).  Run:  python3 scripts/test_operator_registry.py"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from agent_sync import operator_registry as R

# A synthetic credential for the positive scan cases:  32 mixed-case characters with no
# provider prefix, so the scanner still trips on it while the file carries no real token's
# shape.  A prefixed value would be caught by a secret scanner on the repository itself.
SYNTHETIC_SECRET = "A1b2C3d4" * 4


def ident(seat="MM", platform="zulip", host="mac-1", scope="simplewithus", native="native-1", **kw):
    base = dict(
        seat=seat,
        platform=platform,
        verified_host=host,
        profile_scope=scope,
        native_session_id=native,
        source="test",
        title="Thread Title",
    )
    base.update(kw)
    return base


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "registry.sqlite3")
        self.reg = R.SessionRegistry(self.db)

    def _loc(self, **kw):
        fields = ident(**kw)
        return {
            "verified_host": fields["verified_host"],
            "profile_scope": fields["profile_scope"],
        }

    def tearDown(self):
        self.reg.close_db()
        self.tmp.cleanup()

    def test_register_allocates_A_gen1(self):
        r = self.reg.resolve_or_register(ident())
        self.assertTrue(r["created"])
        self.assertEqual(r["outcome"], "registered")
        self.assertEqual(r["label"], "A")
        self.assertEqual(r["generation"], 1)

    def test_resume_same_identity_returns_same_record_and_label(self):
        first = self.reg.resolve_or_register(ident())
        again = self.reg.resolve_or_register(ident())
        self.assertFalse(again["created"])
        self.assertEqual(again["outcome"], "resumed")
        self.assertEqual(again["record_id"], first["record_id"])
        self.assertEqual(again["label"], "A")

    def test_new_native_id_is_new_registration_new_label(self):
        self.reg.resolve_or_register(ident(native="native-1"))
        other = self.reg.resolve_or_register(ident(native="native-2"))
        self.assertTrue(other["created"])
        self.assertEqual(other["label"], "B")

    def test_seat_mismatch_returns_explicit_outcome_and_changes_nothing(self):
        first = self.reg.resolve_or_register(ident(seat="MM"))
        r = self.reg.resolve_or_register(ident(seat="CODEX"))
        self.assertEqual(r["outcome"], "seat_mismatch")
        self.assertEqual(r["record_id"], first["record_id"])
        row = self.reg._get(first["record_id"])
        self.assertEqual(row["seat"], "MM")  # no rebind
        conn = sqlite3.connect(self.db)
        n = conn.execute(
            "SELECT COUNT(*) FROM events WHERE event = 'resolve' AND outcome = 'seat_mismatch'"
        ).fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)

    def test_recycle_wraps_and_bumps_generation(self):
        a = self.reg.resolve_or_register(ident(native="n1"))
        self.assertEqual(a["label"], "A")
        self.assertTrue(self.reg.close(a["record_id"], actor="MM", source="test"))
        b = self.reg.resolve_or_register(ident(native="n2"))
        self.assertEqual(b["label"], "A")  # wrapped back to A
        self.assertEqual(b["generation"], 2)  # disambiguated by generation
        self.assertEqual(R.cross_seat_ref("MM", b["label"], b["generation"]), "MM A (gen 2)")
        self.assertEqual(R.cross_seat_ref("MM", "B", 1), "MM B")

    def test_lowest_free_letter_wins(self):
        a = self.reg.resolve_or_register(ident(native="n1"))
        b = self.reg.resolve_or_register(ident(native="n2"))
        self.assertEqual((a["label"], b["label"]), ("A", "B"))
        self.reg.close(a["record_id"], actor="MM", source="test")
        c = self.reg.resolve_or_register(ident(native="n3"))
        self.assertEqual(c["label"], "A")

    def test_label_exhaustion(self):
        for i in range(26):
            self.reg.resolve_or_register(ident(native=f"n{i}"))
        with self.assertRaises(R.LabelExhausted):
            self.reg.resolve_or_register(ident(native="n26"))

    def test_close_is_idempotent(self):
        a = self.reg.resolve_or_register(ident())
        self.assertTrue(self.reg.close(a["record_id"], actor="MM", source="test"))
        self.assertFalse(self.reg.close(a["record_id"], actor="MM", source="test"))

    def test_resumed_closed_is_explicit(self):
        a = self.reg.resolve_or_register(ident())
        self.reg.close(a["record_id"], actor="MM", source="test")
        r = self.reg.resolve_or_register(ident())
        self.assertEqual(r["outcome"], "resumed_closed")

    def test_register_writes_event_and_outbox_atomically(self):
        self.reg.resolve_or_register(ident())
        pending = R.pending_outbox(self.db)
        kinds = sorted(p["kind"] for p in pending)
        self.assertEqual(kinds, ["event_append", "session_upsert"])

    def test_resume_enqueues_the_session_upsert(self):
        # The exported sheet refreshes "Last Seen At" only from a session_upsert
        # payload, so a resume that moves the column without enqueueing one leaves
        # the database and the sheet disagreeing.
        a = self.reg.resolve_or_register(ident())
        R.mark_exported(self.db, [p["outbox_id"] for p in R.pending_outbox(self.db)])
        self.assertEqual(R.pending_outbox(self.db), [])
        again = self.reg.resolve_or_register(ident())
        self.assertEqual(again["outcome"], "resumed")
        pending = R.pending_outbox(self.db)
        upserts = [p for p in pending if p["kind"] == "session_upsert"]
        self.assertEqual(len(upserts), 1)
        cells = json.loads(upserts[0]["payload"])["cells"]
        row = self.reg._conn.execute(
            "SELECT last_seen_at FROM sessions WHERE record_id = ?", (a["record_id"],)
        ).fetchone()
        self.assertEqual(cells[R.SESSIONS_HEADERS.index("Last Seen At")], row["last_seen_at"])

    def test_mark_exported_then_crash_rerun(self):
        self.reg.resolve_or_register(ident())
        pending = R.pending_outbox(self.db)
        self.assertEqual(R.mark_exported(self.db, [p["outbox_id"] for p in pending]), 2)
        self.assertEqual(R.pending_outbox(self.db), [])

    def test_update_attrs_refuses_immutable_fields(self):
        a = self.reg.resolve_or_register(ident())
        with self.assertRaises(ValueError):
            self.reg.update_attrs(a["record_id"], record_id="nope")
        with self.assertRaises(ValueError):
            self.reg.update_attrs(a["record_id"], label="Z")
        self.reg.update_attrs(a["record_id"], title="New", state="paused")
        row = self.reg._get(a["record_id"])
        self.assertEqual((row["title"], row["state"]), ("New", "paused"))

    def test_update_attrs_cannot_forge_the_lifecycle_state(self):
        a = self.reg.resolve_or_register(ident())
        with self.assertRaises(ValueError):  # 'closed' without closed_at and alias retirement
            self.reg.update_attrs(a["record_id"], state="closed")
        self.reg.close(a["record_id"], actor="MM", source="test")
        with self.assertRaises(ValueError):  # a closed row cannot be marked active again
            self.reg.update_attrs(a["record_id"], state="active")
        self.assertEqual(self.reg._get(a["record_id"])["state"], "closed")

    def test_outbox_follows_commit_order_not_caller_timestamps(self):
        """A historical close(at=...) must not export before the registration it supersedes."""
        a = self.reg.resolve_or_register(ident())
        self.reg.close(a["record_id"], at="1999-01-01T00:00:00.000Z", actor="MM", source="test")
        upserts = [p for p in R.pending_outbox(self.db) if p["kind"] == "session_upsert"]
        self.assertEqual(json.loads(upserts[0]["payload"])["cells"][10], "active")
        self.assertEqual(json.loads(upserts[1]["payload"])["cells"][10], "closed")

    def test_display_title_format(self):
        self.assertEqual(R.display_title("H", "Thread Title"), "H: Thread Title")

    # ---- two letter spaces:  a conversation takes one letter, a subthread two ----

    def test_a_subthread_takes_a_double_letter(self):
        r = self.reg.resolve_or_register(ident(platform="minimax", letter_space="double"))
        self.assertEqual(len(r["label"]), 2)
        self.assertIn(r["label"], R.LETTER_SPACES["double"])

    def test_a_conversation_still_takes_a_single_letter(self):
        r = self.reg.resolve_or_register(ident(platform="minimax"))
        self.assertEqual(len(r["label"]), 1)
        self.assertIn(r["label"], R.LETTERS)

    def test_the_two_spaces_do_not_collide(self):
        one = self.reg.resolve_or_register(ident(platform="minimax", native="native-1"))
        two = self.reg.resolve_or_register(
            ident(platform="minimax", native="native-2", letter_space="double")
        )
        self.assertNotEqual(one["label"], two["label"])
        self.assertEqual(len(one["label"]), 1)
        self.assertEqual(len(two["label"]), 2)

    def test_a_subthread_resumes_with_the_same_double_letter(self):
        first = self.reg.resolve_or_register(
            ident(platform="minimax", native="native-1", letter_space="double")
        )
        again = self.reg.resolve_or_register(
            ident(platform="minimax", native="native-1", letter_space="double")
        )
        self.assertEqual(again["label"], first["label"])
        self.assertFalse(again["created"])

    # ---- update_session: the title follows the thread, keyed on the session id ----

    def test_update_session_sets_the_title_and_keeps_the_letter(self):
        a = self.reg.resolve_or_register(ident(platform="minimax", title="Placeholder"))
        out = R.update_session(
            self.db,
            platform="minimax",
            native_session_id="native-1",
            title="Memory sync, fleet skills and Operator session letter",
            **self._loc(platform="minimax"),
        )
        self.assertEqual(out["outcome"], "updated")
        self.assertEqual(out["label"], a["label"])  # the letter is never touched
        self.assertEqual(out["record_id"], a["record_id"])
        self.assertEqual(
            out["title_display"],
            "A: Memory sync, fleet skills and Operator session letter",
        )
        self.assertEqual(self.reg._get(a["record_id"])["title"], out["title"])

    def test_update_session_strips_a_display_title_prefix(self):
        self.reg.resolve_or_register(ident(platform="minimax", title="Placeholder"))
        out = R.update_session(
            self.db,
            platform="minimax",
            native_session_id="native-1",
            title="A: Memory sync",
            **self._loc(platform="minimax"),
        )
        self.assertEqual(out["title_display"], "A: Memory sync")
        self.assertEqual(self.reg._get(out["record_id"])["title"], "Memory sync")

    def test_update_session_is_idempotent_on_the_same_session_id(self):
        self.reg.resolve_or_register(ident(platform="minimax", title="Placeholder"))
        first = R.update_session(
            self.db, platform="minimax", native_session_id="native-1", title="T", **self._loc()
        )
        again = R.update_session(
            self.db, platform="minimax", native_session_id="native-1", title="T", **self._loc()
        )
        self.assertEqual(first["outcome"], "updated")
        self.assertEqual(again["outcome"], "unchanged")
        self.assertEqual(again["changed"], [])
        self.assertEqual(again["label"], first["label"])

    def test_register_refuses_a_title_that_carries_a_credential(self):
        secret = SYNTHETIC_SECRET
        with self.assertRaises(ValueError) as caught:
            R.register_session(
                self.db,
                platform="minimax",
                native_session_id="native-1",
                seat="MM",
                source="test",
                title="Deploy with " + secret,
                issuer=R.ISSUER_OPERATOR,
                **self._loc(platform="minimax"),
            )
        self.assertEqual(str(caught.exception), "secret_detected")
        self.assertNotIn(secret, str(caught.exception))
        stored = self.reg._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        self.assertEqual(stored, 0)

    def test_update_refuses_a_title_that_carries_a_credential(self):
        a = self.reg.resolve_or_register(ident(platform="minimax", title="Placeholder"))
        secret = SYNTHETIC_SECRET
        with self.assertRaises(ValueError) as caught:
            R.update_session(
                self.db,
                platform="minimax",
                native_session_id="native-1",
                title="Deploy with " + secret,
                **self._loc(platform="minimax"),
            )
        self.assertEqual(str(caught.exception), "secret_detected")
        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(self.reg._get(a["record_id"])["title"], "Placeholder")

    def test_a_title_that_is_not_a_string_is_refused_not_coerced(self):
        # str() of a mapping or a number can scan clean, so a value of another type is
        # refused with its own code instead of being turned into text.
        for value in (123, {"a": 1}, ["x"], b"bytes"):
            with self.assertRaises(ValueError) as caught:
                R._scan_title(value)
            self.assertEqual(str(caught.exception), "title_not_string")
            with self.assertRaises(ValueError) as caught:
                R._scan_fields({"title": value})
            self.assertEqual(str(caught.exception), "title_not_string")
        R._scan_title(None)
        R._scan_title("A plain title")

    def test_register_refuses_a_title_that_is_not_a_string(self):
        with self.assertRaises(ValueError) as caught:
            R.register_session(
                self.db,
                platform="minimax",
                native_session_id="native-1",
                seat="MM",
                source="test",
                title=123,
                issuer=R.ISSUER_OPERATOR,
                **self._loc(platform="minimax"),
            )
        self.assertEqual(str(caught.exception), "title_not_string")
        stored = self.reg._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        self.assertEqual(stored, 0)

    def test_update_refuses_a_project_that_carries_a_credential(self):
        # Every field that is written is scanned, not only the title.
        a = self.reg.resolve_or_register(ident(platform="minimax", title="Placeholder"))
        secret = SYNTHETIC_SECRET
        with self.assertRaises(ValueError) as caught:
            R.update_session(
                self.db,
                platform="minimax",
                native_session_id="native-1",
                project="Deploy with " + secret,
                **self._loc(platform="minimax"),
            )
        self.assertEqual(str(caught.exception), "project_secret_detected")
        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(self.reg._get(a["record_id"])["project"], "")

    def test_update_refuses_a_thread_link_that_carries_a_credential(self):
        a = self.reg.resolve_or_register(ident(platform="minimax", title="Placeholder"))
        secret = SYNTHETIC_SECRET
        with self.assertRaises(ValueError) as caught:
            R.update_session(
                self.db,
                platform="minimax",
                native_session_id="native-1",
                thread_link="https://example.invalid/" + secret,
                **self._loc(platform="minimax"),
            )
        self.assertEqual(str(caught.exception), "thread_link_secret_detected")
        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(self.reg._get(a["record_id"])["thread_link"], "")

    def test_register_refuses_a_field_that_carries_a_credential(self):
        secret = SYNTHETIC_SECRET
        with self.assertRaises(ValueError) as caught:
            R.register_session(
                self.db,
                platform="minimax",
                native_session_id="native-1",
                seat="MM",
                source="test",
                apple_note_link="https://example.invalid/" + secret,
                issuer=R.ISSUER_OPERATOR,
                **self._loc(platform="minimax"),
            )
        self.assertEqual(str(caught.exception), "apple_note_link_secret_detected")
        self.assertNotIn(secret, str(caught.exception))
        stored = self.reg._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        self.assertEqual(stored, 0)

    def test_a_benign_field_is_still_written(self):
        # The scan refuses a credential, not a field.
        a = self.reg.resolve_or_register(ident(platform="minimax", title="Placeholder"))
        out = R.update_session(
            self.db,
            platform="minimax",
            native_session_id="native-1",
            project="AFC-564",
            **self._loc(platform="minimax"),
        )
        self.assertEqual(out["outcome"], "updated")
        self.assertEqual(self.reg._get(a["record_id"])["project"], "AFC-564")

    def test_update_session_on_an_unregistered_session_raises(self):
        with self.assertRaises(ValueError):
            R.update_session(
                self.db, platform="minimax", native_session_id="nope", title="T", **self._loc()
            )

    def test_update_session_needs_a_field(self):
        self.reg.resolve_or_register(ident(platform="minimax"))
        with self.assertRaises(ValueError):
            R.update_session(
                self.db, platform="minimax", native_session_id="native-1", **self._loc()
            )

    def test_update_session_does_not_mint_a_letter(self):
        self.reg.resolve_or_register(ident(platform="minimax"))
        R.update_session(
            self.db,
            platform="minimax",
            native_session_id="native-1",
            title="T",
            **self._loc(platform="minimax"),
        )
        row = self.reg._conn.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE platform = 'minimax'"
        ).fetchone()
        self.assertEqual(row["n"], 1)

    def test_update_session_on_a_closed_session_answers_the_closed_state(self):
        # The letter is over, so the row can never change again.  The call answers the
        # closed state instead of raising:  the seat's stop hook carries only the
        # title, and a refusal would block that hook forever on a swept session.
        a = self.reg.resolve_or_register(ident(platform="minimax"))
        self.reg.close(a["record_id"], actor="MM", source="test")
        out = R.update_session(
            self.db,
            platform="minimax",
            native_session_id="native-1",
            title="T",
            **self._loc(platform="minimax"),
        )
        self.assertEqual(out["outcome"], "closed")
        self.assertEqual(out["changed"], [])
        self.assertEqual(out["record_id"], a["record_id"])
        # The letter that ended is named, the same one close_session reports.
        self.assertEqual(out["label"], a["label"])
        self.assertGreater(out["generation"], 0)
        row = self.reg._conn.execute(
            "SELECT title FROM sessions WHERE record_id = ?", (a["record_id"],)
        ).fetchone()
        self.assertNotEqual(row["title"], "T")

    def test_update_session_refreshes_last_seen(self):
        """A long-lived session is alive:  it must not look stale to the sweep."""
        a = self.reg.resolve_or_register(ident(platform="minimax"))
        self.reg._conn.execute(
            "UPDATE sessions SET last_seen_at = ? WHERE record_id = ?",
            ("2020-01-01T00:00:00.000Z", a["record_id"]),
        )
        self.reg._conn.commit()
        R.update_session(
            self.db,
            platform="minimax",
            native_session_id="native-1",
            title="T",
            **self._loc(platform="minimax"),
        )
        seen = self.reg._get(a["record_id"])["last_seen_at"]
        self.assertNotEqual(seen, "2020-01-01T00:00:00.000Z")
        ended = R.sweep_stale(self.db, platform="minimax", seat="MM", stale_after_minutes=120)
        self.assertEqual(ended, [])

    # ---- the letter's life:  it ends, and the history keeps the window ------------

    def test_close_session_ends_the_letter_and_keeps_the_history(self):
        a = self.reg.resolve_or_register(ident(platform="minimax", title="Thread Title"))
        out = R.close_session(
            self.db,
            platform="minimax",
            native_session_id="native-1",
            reason="stale and replaced by a newer session",
            **self._loc(platform="minimax"),
        )
        self.assertTrue(out["closed"])
        self.assertEqual(out["label"], a["label"])
        self.assertTrue(out["closed_at"])
        history = R.letter_history(self.db, "minimax")
        self.assertEqual(len(history), 1)
        row = history[0]
        self.assertEqual(row["letter"], a["label"])
        self.assertTrue(row["assigned_at"])
        self.assertTrue(row["ended_at"])  # the window has an end
        self.assertIn("stale", row["end_reason"])
        self.assertFalse(row["live"])

    def test_close_session_twice_changes_nothing(self):
        self.reg.resolve_or_register(ident(platform="minimax"))
        first = R.close_session(
            self.db,
            platform="minimax",
            native_session_id="native-1",
            **self._loc(platform="minimax"),
        )
        again = R.close_session(
            self.db,
            platform="minimax",
            native_session_id="native-1",
            **self._loc(platform="minimax"),
        )
        self.assertTrue(first["closed"])
        self.assertFalse(again["closed"])
        self.assertEqual(again["outcome"], "already_closed")

    def test_close_session_twice_still_names_the_letter(self):
        # The alias is retired by the first close, so the live lookup answers
        # nothing:  the second close still has to name the letter that ended.
        self.reg.resolve_or_register(ident(platform="minimax"))
        first = R.close_session(
            self.db,
            platform="minimax",
            native_session_id="native-1",
            **self._loc(platform="minimax"),
        )
        again = R.close_session(
            self.db,
            platform="minimax",
            native_session_id="native-1",
            **self._loc(platform="minimax"),
        )
        self.assertEqual(first["label"], "A")
        self.assertEqual(again["label"], "A")
        self.assertEqual(again["generation"], 1)

    def test_close_session_on_an_unregistered_session_raises(self):
        with self.assertRaises(ValueError):
            R.close_session(
                self.db,
                platform="minimax",
                native_session_id="nope",
                **self._loc(platform="minimax"),
            )

    def test_history_lists_live_and_retired_letters_with_their_windows(self):
        self.reg.resolve_or_register(ident(platform="minimax", native="native-1"))
        self.reg.resolve_or_register(ident(platform="minimax", native="native-2"))
        R.close_session(
            self.db,
            platform="minimax",
            native_session_id="native-1",
            **self._loc(platform="minimax"),
        )
        history = R.letter_history(self.db, "minimax")
        by_letter = {row["letter"]: row for row in history}
        self.assertFalse(by_letter["A"]["live"])
        self.assertTrue(by_letter["B"]["live"])
        self.assertIsNone(by_letter["B"]["ended_at"])

    def test_sweep_ends_a_stale_session_only_when_it_was_replaced(self):
        """A stale session nobody replaced keeps its letter:  a reply to it is still
        the seat's business."""
        old = self.reg.resolve_or_register(ident(platform="minimax", native="native-1"))
        self.reg._conn.execute(
            "UPDATE sessions SET last_seen_at = ?, registered_at = ? WHERE record_id = ?",
            ("2020-01-01T00:00:00.000Z", "2020-01-01T00:00:00.000Z", old["record_id"]),
        )
        self.reg._conn.commit()
        ended = R.sweep_stale(self.db, platform="minimax", seat="MM", stale_after_minutes=120)
        self.assertEqual(ended, [])  # stale, but nobody replaced it
        self.assertIsNone(self.reg._get(old["record_id"])["closed_at"])

    def test_sweep_ends_a_stale_replaced_session(self):
        old = self.reg.resolve_or_register(ident(platform="minimax", native="native-1"))
        self.reg._conn.execute(
            "UPDATE sessions SET last_seen_at = ?, registered_at = ? WHERE record_id = ?",
            ("2020-01-01T00:00:00.000Z", "2020-01-01T00:00:00.000Z", old["record_id"]),
        )
        self.reg._conn.commit()
        self.reg.resolve_or_register(ident(platform="minimax", native="native-2"))
        ended = R.sweep_stale(self.db, platform="minimax", seat="MM", stale_after_minutes=120)
        self.assertEqual(len(ended), 1)
        self.assertEqual(ended[0]["label"], old["label"])
        self.assertIsNotNone(self.reg._get(old["record_id"])["closed_at"])
        history = R.letter_history(self.db, "minimax")
        retired = [row for row in history if row["letter"] == old["label"]]
        self.assertTrue(retired[0]["ended_at"])
        self.assertFalse(retired[0]["live"])

    def test_missing_identity_fields_raise(self):
        with self.assertRaises(ValueError):
            self.reg.resolve_or_register({"seat": "MM"})

    def test_concurrent_same_identity_resumes_instead_of_integrity_error(self):
        """Two writers registering one identity:  the second must resume the committed record,
        not race past the unlocked SELECT into an unhandled IntegrityError (review 18197523/1)."""
        # Synchronize before BEGIN IMMEDIATE, not inside the locked identity
        # read. A barrier inside that transaction waits for a peer that cannot
        # enter until commit, burning the same five seconds as SQLite's busy
        # timeout and intermittently causing SQLITE_BUSY in a correct writer.
        start = threading.Barrier(2, timeout=5)
        errs = []

        def worker():
            reg = None
            try:
                reg = R.SessionRegistry(self.db)
                start.wait()
                reg.resolve_or_register(ident())
            except Exception as exc:  # noqa: BLE001 - surfaced by assertion
                errs.append(exc)
            finally:
                if reg is not None:
                    reg.close_db()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))

        self.assertEqual(errs, [])
        with sqlite3.connect(self.db) as connection:
            rows = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        self.assertEqual(rows, 1)  # one record, not a duplicate

    def test_write_lock_is_held_before_the_first_read(self):
        """The write lock must already be held when the identity/status checks run, or a second
        writer can read the same pre-commit state (review 18197523/0 and /1)."""
        seen = {}
        real_get_by_identity = R.SessionRegistry._get_by_identity
        real_get = R.SessionRegistry._get

        def probe_identity(self, *args):
            seen["identity"] = self._conn.in_transaction
            return real_get_by_identity(self, *args)

        def probe_get(self, record_id):
            seen["get"] = self._conn.in_transaction
            return real_get(self, record_id)

        R.SessionRegistry._get_by_identity = probe_identity
        R.SessionRegistry._get = probe_get
        try:
            a = self.reg.resolve_or_register(ident())
            self.reg.close(a["record_id"])
        finally:
            R.SessionRegistry._get_by_identity = real_get_by_identity
            R.SessionRegistry._get = real_get
        self.assertTrue(seen["identity"])
        self.assertTrue(seen["get"])

    def test_outbox_ties_on_created_at_keep_commit_order(self):
        """Rows written by one transaction share a created_at;  the tiebreak must follow commit
        order (rowid) and not the ULID suffix, which would reorder an event against the session
        row it announces (review 18197523/0)."""
        stamp = "2026-10-20T00:00:00.000Z"
        conn = sqlite3.connect(self.db)
        # Same instant, inserted in commit order 1 then 2;  the ULIDs sort the other way round.
        for outbox_id, tag in (
            ("zzz-ulid-first-committed", "a"),
            ("aaa-ulid-second-committed", "b"),
        ):
            conn.execute(
                "INSERT INTO export_outbox(outbox_id, kind, payload, created_at)"
                " VALUES (?, 'session_upsert', ?, ?)",
                (outbox_id, json.dumps({"tag": tag}), stamp),
            )
        conn.commit()
        conn.close()
        self.assertEqual(
            [json.loads(p["payload"])["tag"] for p in R.pending_outbox(self.db)],
            ["a", "b"],  # commit order, not ULID order
        )

    def test_session_cells_match_sessions_headers(self):
        a = self.reg.resolve_or_register(ident())
        pending = R.pending_outbox(self.db)
        upsert = [p for p in pending if p["kind"] == "session_upsert"][0]
        cells = json.loads(upsert["payload"])["cells"]
        self.assertEqual(len(cells), len(R.SESSIONS_HEADERS))
        self.assertEqual(cells[0], a["record_id"])
        self.assertEqual(cells[1], "A")


class RegistrationsForNativeIdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "registry.sqlite3")
        self.reg = R.SessionRegistry(self.db)

    def tearDown(self):
        self.reg.close_db()
        self.tmp.cleanup()

    def test_unlettered_session_is_an_empty_answer(self):
        self.assertEqual(R.registrations_for_native_id(self.db, "zulip", "native-9"), [])

    def test_an_unreadable_registry_names_its_path(self):
        bad = os.path.join(self.tmp.name, "not-a-registry.sqlite3")
        with open(bad, "w") as handle:
            handle.write("this is not sqlite")
        with self.assertRaises(R.RegistryUnavailable) as raised:
            R.registrations_for_native_id(bad, "zulip", "native-9")
        self.assertIn(bad, str(raised.exception))

    def test_finds_the_session_at_each_of_its_locations(self):
        self.reg.resolve_or_register(ident(host="mac-1", native="native-7"))
        self.reg.resolve_or_register(ident(host="server-1", native="native-7"))
        rows = R.registrations_for_native_id(self.db, "zulip", "native-7")
        self.assertEqual([row["verified_host"] for row in rows], ["mac-1", "server-1"])
        self.assertEqual([row["label"] for row in rows], ["A", "B"])

    def test_other_platforms_and_sessions_are_not_returned(self):
        self.reg.resolve_or_register(ident(platform="zulip", native="native-7"))
        self.reg.resolve_or_register(ident(platform="cursor", native="native-7"))
        self.reg.resolve_or_register(ident(platform="zulip", native="native-8"))
        rows = R.registrations_for_native_id(self.db, "zulip", "native-7")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verified_host"], "mac-1")

    def test_adapter_names_fold_to_the_platform(self):
        self.reg.resolve_or_register(ident(platform="cursor", native="native-7"))
        rows = R.registrations_for_native_id(self.db, "cursor-cloud", "native-7")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["label"], "A")

    def test_a_missing_registry_raises_not_empty(self):
        with self.assertRaises(R.RegistryUnavailable):
            R.registrations_for_native_id(
                os.path.join(self.tmp.name, "nope.sqlite3"), "zulip", "native-7"
            )

    def test_a_bad_session_id_is_refused(self):
        with self.assertRaises(ValueError):
            R.registrations_for_native_id(self.db, "zulip", "no way")

    def test_a_retired_letter_reads_as_unlettered(self):
        made = self.reg.resolve_or_register(ident(native="native-7"))
        self.assertTrue(self.reg.close(made["record_id"], actor="MM", source="test"))
        self.assertEqual(R.registrations_for_native_id(self.db, "zulip", "native-7"), [])


if __name__ == "__main__":
    unittest.main()
