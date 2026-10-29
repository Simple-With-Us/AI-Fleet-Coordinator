"""Tests for agent_sync.operator_registry (AFC-412 PR1).  Run:  python3 scripts/test_operator_registry.py"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

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

    # ---- the pick:  the lowest free letter, or the one unused the longest ----

    def _pair(self, native, pick="lowest", parent=""):
        return self.reg.resolve_or_register(
            ident(
                platform="claude",
                native=native,
                letter_space="double",
                letter_pick=pick,
                parent_session_id=parent,
            )
        )

    def test_the_default_pick_is_the_lowest_free_letter(self):
        a = self._pair("n-1")
        self.reg.close(a["record_id"])
        self.assertEqual((a["label"], self._pair("n-2")["label"]), ("AA", "AA"))

    def test_the_oldest_pick_leaves_a_retired_letter_alone_while_unused_ones_remain(self):
        a = self._pair("n-1", "oldest")
        self.reg.close(a["record_id"])
        self.assertEqual(
            [self._pair(n, "oldest")["label"] for n in ("n-2", "n-3")], ["AB", "AC"]
        )

    def test_the_oldest_pick_takes_the_longest_retired_letter_once_every_letter_was_used(self):
        with mock.patch.dict(R.LETTER_SPACES, {"double": ("AA", "AB", "AC")}):
            made = [self._pair("n-%d" % i, "oldest") for i in (1, 2, 3)]
            self.assertEqual([m["label"] for m in made], ["AA", "AB", "AC"])
            for step, index in enumerate((2, 0, 1)):  # retired in this order:  AC first, AB last
                self.reg.close(made[index]["record_id"], at="2026-10-01T00:00:0%d.000Z" % step)
            again = [self._pair("n-%d" % i, "oldest") for i in (4, 5, 6)]
        self.assertEqual([(a["label"], a["generation"]) for a in again], [("AC", 2), ("AA", 2), ("AB", 2)])

    def test_exhaustion_says_which_space_was_full(self):
        with mock.patch.dict(R.LETTER_SPACES, {"single": ("A",), "double": ("AA",)}):
            self.reg.resolve_or_register(ident(platform="claude", native="n-1"))
            self.reg.resolve_or_register(ident(platform="claude", native="n-2", letter_space="double"))
            with self.assertRaises(R.LabelExhausted) as one:
                self.reg.resolve_or_register(ident(platform="claude", native="n-3"))
            with self.assertRaises(R.LabelExhausted) as two:
                self.reg.resolve_or_register(ident(platform="claude", native="n-4", letter_space="double"))
        self.assertEqual((one.exception.space, two.exception.space), ("single", "double"))

    def test_a_pick_that_is_not_known_is_refused(self):
        with self.assertRaises(ValueError) as caught:
            R.register_session(
                self.db,
                platform="claude",
                verified_host="mac",
                profile_scope="default",
                native_session_id="native-1",
                seat="CLAUDE",
                source="test",
                issuer=R.ISSUER_OPERATOR,
                letter_space="double",
                letter_pick="random",
            )
        self.assertIn("letter_pick", str(caught.exception))

    def test_the_register_command_answers_json_when_every_letter_is_live(self):
        # What a platform that asks for its own letter (BotFleet, say) reads:  a code it can act on, not a traceback.
        base = [
            "register", "--db", self.db, "--platform", "botfleet", "--location", "mac", "--scope", "default",
            "--seat", "BF-BUILDER", "--source", "test", "--letter-space", "double", "--letter-pick", "oldest",
        ]
        out = io.StringIO()
        with mock.patch.dict(R.LETTER_SPACES, {"double": ("AA",)}), contextlib.redirect_stdout(out):
            first = R.main(base + ["--session-id", "thread-0001"])
            second = R.main(base + ["--session-id", "thread-0002"])
        lines = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual((first, second), (0, 2))
        self.assertEqual(lines[0]["label"], "AA")
        self.assertEqual(lines[1]["code"], "labels_exhausted")
        self.assertIn("1 letters are active", lines[1]["error"])

    def test_reopen_gives_a_chat_its_own_letter_back_when_it_is_free(self):
        pair = self._pair("n-1", "oldest")
        single = self.reg.resolve_or_register(ident(platform="claude", native="n-2"))
        for made in (pair, single):
            self.reg.close(made["record_id"])
        # Both letters are free, so each chat has its own back (no allocation runs;  the space rule is tested below).
        self.assertEqual(self.reg.reopen(pair["record_id"], pick="oldest")[0], "AA")
        self.assertEqual(self.reg.reopen(single["record_id"], pick="oldest")[0], "A")

    def test_reopen_allocates_in_the_space_of_the_letter_it_held(self):
        with mock.patch.dict(R.LETTER_SPACES, {"double": ("AA", "AB")}):
            held = self._pair("n-1", "oldest")  # AA
            self.reg.close(held["record_id"])
            self._pair("n-2", "oldest")  # AB
            self._pair("n-3", "oldest")  # AA again, generation 2
            other = self.reg.lookup("claude", "mac-1", "simplewithus", "n-2")
            self.reg.close(other["record_id"])  # AB is free now
            letter, generation, previous = self.reg.reopen(held["record_id"], pick="oldest")
        self.assertEqual((letter, previous), ("AB", "AA"))  # a pair, not a single letter
        self.assertEqual(generation, 2)

    # ---- main chats take AA, BB ... ZZ in order;  a subthread takes the pair after its parent's (owner, Oct 29) ----

    def _labels(self, natives, pick="primary", parent=""):
        return [self._pair(n, pick, parent)["label"] for n in natives]

    def test_the_primary_order_is_the_doubled_pairs_then_each_rows_last_slots_first(self):
        order = R.PRIMARY_ORDER
        self.assertEqual((len(order), len(set(order))), (676, 676))
        self.assertEqual(order[:3], ("AA", "BB", "CC"))
        self.assertEqual(order[:26], tuple(a + a for a in R.LETTERS))
        # Past the doubled pairs, the pair just before each row's letter (the last a subthread reaches) goes first.
        self.assertEqual(order[26:29], ("AZ", "BA", "CB"))
        # and the first slot of every row (AB, BC ... ZA) goes last.
        self.assertEqual(order[-26:], tuple(R.LETTERS[i] + R.LETTERS[(i + 1) % 26] for i in range(26)))
        self.assertEqual(sorted(order), sorted(R.LETTER_SPACES["double"]))

    def test_main_chats_take_the_doubled_pairs_in_order(self):
        self.assertEqual(self._labels(["n-1", "n-2", "n-3"]), ["AA", "BB", "CC"])

    def test_a_freed_doubled_pair_is_the_next_main_chat_s(self):
        first = self._pair("n-1", "primary")
        self._labels(["n-2", "n-3"])  # BB, CC
        self.reg.close(first["record_id"])
        # Sequentially means the lowest free doubled pair, so a freed AA is taken before DD.
        self.assertEqual(self._labels(["n-4", "n-5"]), ["AA", "DD"])

    def test_main_chats_past_the_26_doubled_pairs_take_the_last_slot_of_each_row(self):
        self.assertEqual(self._labels(["n-%d" % i for i in range(26)])[-1], "ZZ")
        self.assertEqual(self._labels(["x-1", "x-2", "x-3"]), ["AZ", "BA", "CB"])

    def test_a_pick_of_primary_in_the_single_space_is_the_lowest_letter(self):
        labels = [
            self.reg.resolve_or_register(ident(platform="claude", native=n, letter_pick="primary"))["label"]
            for n in ("n-1", "n-2")
        ]
        self.assertEqual(labels, ["A", "B"])

    def test_the_pairs_after_a_pair_follow_it_round_the_alphabet_and_skip_the_doubled_one(self):
        # The owner's examples:  AA has AB, AC, AD ...;  XX has XY, XZ, XA, XB ...
        self.assertEqual(R.pairs_after("AA")[:3], ("AB", "AC", "AD"))
        self.assertEqual(R.pairs_after("AA")[-1], "AZ")
        self.assertEqual(R.pairs_after("XX")[:4], ("XY", "XZ", "XA", "XB"))
        self.assertEqual(R.pairs_after("XX")[-1], "XW")
        for parent in ("AA", "XX", "ZZ"):
            self.assertEqual(len(R.pairs_after(parent)), 25, parent)
            self.assertNotIn(parent, R.pairs_after(parent))
        self.assertEqual(R.pairs_after("CC")[:3], ("CD", "CE", "CF"))
        self.assertEqual(R.pairs_after("ZZ")[:3], ("ZA", "ZB", "ZC"))
        self.assertEqual(R.pairs_after("CD")[0], "CE")  # a subthread's own subthreads go after its pair
        self.assertEqual(R.pairs_after("XA")[:2], ("XB", "XC"))
        self.assertNotIn("XX", R.pairs_after("XA"))  # never the doubled pair, which belongs to a main chat
        self.assertNotIn("XA", R.pairs_after("XA"))
        self.assertEqual((R.pairs_after("Z"), R.pairs_after("ZZZ"), R.pairs_after("")), ((), (), ()))

    def test_a_subthread_takes_the_pair_right_after_its_parent(self):
        self._labels(["a", "b", "c"])  # AA, BB, CC
        self.assertEqual(self._labels(["c-1", "c-2", "c-3"], parent="c"), ["CD", "CE", "CF"])
        # A subthread of a subthread goes after its own parent, in the same row, past the pairs already taken.
        self.assertEqual(self._labels(["g-1"], parent="c-1"), ["CG"])
        self.assertEqual(self._labels(["b-1"], parent="b"), ["BC"])
        self.assertEqual(self._labels(["a-1"], parent="a"), ["AB"])

    def test_a_subthread_skips_taken_pairs_and_takes_a_freed_one_again(self):
        self._labels(["a", "b", "c"])
        first, second = [self._pair(n, "primary", "c") for n in ("c-1", "c-2")]  # CD, CE
        self.reg.close(first["record_id"])
        self.assertEqual(self._pair("c-3", "primary", "c")["label"], "CD")  # the first free pair after CC
        self.assertEqual(self._pair("c-4", "primary", "c")["label"], "CF")  # CD and CE are taken
        self.assertEqual(second["label"], "CE")

    def test_a_subthread_whose_parent_has_no_free_pair_after_it_is_picked_as_a_main_chat(self):
        self._labels(["a"])  # AA
        row = self._labels(["a-%d" % i for i in range(25)], parent="a")  # AB ... AZ
        self.assertEqual((row[0], row[-1]), ("AB", "AZ"))
        # Row A is full, so the next subthread takes the next main-chat pair, then the one after it.
        self.assertEqual(self._labels(["a-x", "a-y"], parent="a"), ["BB", "CC"])

    def test_a_subthreads_pairs_wrap_past_z_back_to_a(self):
        self._labels(["n-%d" % i for i in range(24)])  # AA ... XX
        # The owner's example:  a subthread of XX gets XY, XZ, XA, XB.
        self.assertEqual(self._labels(["x-1", "x-2", "x-3", "x-4"], parent="n-23"), ["XY", "XZ", "XA", "XB"])

    def test_a_subthread_of_the_last_doubled_pair_wraps_to_the_start_of_its_row(self):
        self._labels(["n-%d" % i for i in range(26)])  # AA ... ZZ
        self.assertEqual(self._labels(["z-1", "z-2"], parent="n-25"), ["ZA", "ZB"])

    def test_a_subthread_never_takes_a_doubled_pair_even_when_one_is_free(self):
        self._labels(["n-%d" % i for i in range(26)])
        parent = self._pair("p", "primary")  # AZ, the first main chat past the doubled pairs
        self.assertEqual(parent["label"], "AZ")
        self.reg.close(self.reg.lookup("claude", "mac-1", "simplewithus", "n-0")["record_id"])  # AA is free
        # Past Z the row wraps to A, which would be AA, the row's doubled pair:  it is skipped for AB.
        self.assertEqual(self._pair("p-1", "primary", "p")["label"], "AB")
        self.assertEqual(self._pair("n-new", "primary")["label"], "AA")  # and a main chat still gets it

    def test_an_unknown_closed_single_or_own_parent_is_no_parent(self):
        parent = self._pair("p", "primary")  # AA
        self.reg.close(parent["record_id"])
        self.assertEqual(self._pair("k-1", "primary", "p")["label"], "AA")  # a closed parent
        self.assertEqual(self._pair("k-2", "primary", "nobody")["label"], "BB")  # an unknown one
        single = self.reg.resolve_or_register(ident(platform="claude", native="s"))  # A, a single letter
        self.assertEqual(single["label"], "A")
        self.assertEqual(self._pair("k-3", "primary", "s")["label"], "CC")  # a parent with a single letter
        self.assertEqual(self._pair("k-4", "primary", "k-4")["label"], "DD")  # a chat is not its own parent

    def test_the_parent_is_found_on_any_host(self):
        self.reg.resolve_or_register(
            ident(platform="claude", native="m", host="mac", letter_space="double", letter_pick="primary")
        )  # AA on the Mac
        child = self.reg.resolve_or_register(
            ident(
                platform="claude", native="s-1", host="server", letter_space="double", letter_pick="primary",
                parent_session_id="m",
            )
        )
        self.assertEqual(child["label"], "AB")

    def test_the_register_event_names_the_parent_pair(self):
        self._labels(["a", "b"])
        child = self._pair("b-1", "primary", "b")
        plain = self._pair("p-1", "primary")
        conn = sqlite3.connect(self.db)
        try:
            rows = dict(
                conn.execute(
                    "SELECT session_record_id, details FROM events WHERE event = 'register'"
                ).fetchall()
            )
        finally:
            conn.close()
        self.assertEqual((child["label"], rows[child["record_id"]]), ("BC", "after BB"))
        self.assertEqual((plain["label"], rows[plain["record_id"]]), ("CC", ""))

    def test_a_reopened_subthread_takes_a_pair_after_its_parent_when_its_own_is_gone(self):
        self._labels(["a", "b", "c"])
        child = self._pair("c-1", "primary", "c")  # CD
        self.reg.close(child["record_id"])
        self._pair("c-2", "primary", "c")  # CD again, taken by another chat
        letter, _, previous = self.reg.reopen(child["record_id"], pick="primary", parent_session_id="c")
        self.assertEqual((letter, previous), ("CE", "CD"))

    def test_a_reopened_main_chat_takes_the_next_doubled_pair_when_its_own_is_gone(self):
        first = self._pair("n-1", "primary")  # AA
        self.reg.close(first["record_id"])
        self._labels(["n-2"])  # AA again
        self.assertEqual(self.reg.reopen(first["record_id"], pick="primary")[0], "BB")

    def _by_letter(self, native, letter, **kw):
        return self.reg.resolve_or_register(
            ident(
                platform="claude", native=native, letter_space="double", letter_pick="primary",
                parent_letter=letter, **kw,
            )
        )

    def test_a_parent_named_by_its_letter_gets_the_pair_after_it(self):
        self._labels(["a", "b", "c"])  # AA, BB, CC
        first = self._by_letter("k-1", "CC")
        second = self._by_letter("k-2", "CC")
        self.assertEqual((first["label"], first["placed_after"], second["label"]), ("CD", "CC", "CE"))

    def test_a_parent_letter_that_no_live_session_holds_is_no_parent(self):
        self._labels(["a"])  # AA
        self.assertEqual(self._by_letter("k-1", "CC")["label"], "BB")  # nobody holds CC
        for number, bad in enumerate(("C", "cc", "CCC", "C1", "A A", "")):
            out = self._by_letter("j-%d" % number, bad)
            self.assertEqual(out["placed_after"], "", bad)
        closed = self._pair("g-1", "primary")
        self.reg.close(closed["record_id"])
        self.assertEqual(self._by_letter("k-2", closed["label"])["placed_after"], "")  # a closed holder

    def test_a_parent_letter_on_another_platform_is_not_a_parent(self):
        self.reg.resolve_or_register(ident(platform="minimax", native="mm-1", letter_space="double"))  # AA there
        self._labels(["a"])  # AA on claude too
        self.assertEqual(self._by_letter("k-1", "AA")["label"], "AB")  # followed claude's AA
        self.reg.close(self.reg.lookup("claude", "mac-1", "simplewithus", "a")["record_id"])  # claude's AA is gone
        out = self._by_letter("k-2", "AA")  # minimax still holds an AA, which is not this platform's
        self.assertEqual(out["placed_after"], "")

    def test_a_parent_session_id_wins_over_a_parent_letter(self):
        self._labels(["a", "b"])  # AA, BB
        out = self.reg.resolve_or_register(
            ident(
                platform="claude", native="k-1", letter_space="double", letter_pick="primary",
                parent_session_id="b", parent_letter="AA",
            )
        )
        self.assertEqual((out["label"], out["placed_after"]), ("BC", "BB"))

    def test_a_parent_on_another_platform_is_not_followed_by_session_id(self):
        self.reg.resolve_or_register(ident(platform="minimax", native="shared-id", letter_space="double"))  # AA
        out = self._pair("k-1", "primary", "shared-id")
        self.assertEqual((out["label"], out["placed_after"]), ("AA", ""))  # a main chat, not AB

    def test_the_answer_says_nothing_about_a_parent_the_child_did_not_follow(self):
        self._labels(["a"])
        row = self._labels(["a-%d" % i for i in range(25)], parent="a")  # AB ... AZ
        self.assertEqual(len(row), 25)
        full = self._pair("a-x", "primary", "a")  # the row is full:  a main chat's pair, not "after AA"
        self.assertEqual((full["label"], full["placed_after"]), ("BB", ""))
        conn = sqlite3.connect(self.db)
        try:
            details = conn.execute(
                "SELECT details FROM events WHERE session_record_id = ? AND event = 'register'", (full["record_id"],)
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(details, "")

    def test_a_row_cut_down_by_a_small_space_hands_out_no_pair_outside_it(self):
        with mock.patch.dict(R.LETTER_SPACES, {"double": ("AA", "AB", "BB")}):
            self._labels(["a"])  # AA
            # pairs_after("AA") runs AB ... AZ, but only AB is in this space
            self.assertEqual(self._labels(["a-1", "a-2"], parent="a"), ["AB", "BB"])
            with self.assertRaises(R.LabelExhausted):
                self._pair("a-3", "primary", "a")

    def test_a_padded_parent_id_finds_the_same_parent_on_every_path(self):
        self._labels(["parent-aaaa"])  # AA
        kid = self._pair("kid-0001", "primary", "parent-aaaa")
        self.assertEqual(kid["label"], "AB")
        self.reg.close(kid["record_id"])
        self._pair("kid-0002", "primary", "parent-aaaa")  # takes AB
        letter, _, previous = self.reg.reopen(kid["record_id"], pick="primary", parent_session_id="  parent-aaaa ")
        self.assertEqual((letter, previous), ("AC", "AB"))  # after the parent, not a doubled pair
        plain = self.reg.resolve_or_register(
            ident(platform="claude", native="kid-0003", letter_space="double", letter_pick="primary",
                  parent_session_id=" parent-aaaa")
        )
        self.assertEqual(plain["label"], "AD")

    def test_the_oldest_pick_still_follows_a_parent_first(self):
        self._pair("a", "oldest")  # AA
        self.assertEqual(self._pair("k-1", "oldest", "a")["label"], "AB")
        self.assertEqual(self._pair("k-2", "oldest")["label"], "AC")  # a chat with no parent keeps the old order

    def test_the_register_command_takes_a_parent_letter(self):
        base = [
            "register", "--db", self.db, "--platform", "codex", "--location", "mac", "--scope", "default",
            "--seat", "CODEX", "--source", "test", "--letter-space", "double", "--letter-pick", "primary",
        ]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            R.main(base + ["--session-id", "thread-0001"])
            R.main(base + ["--session-id", "thread-0002", "--parent-letter", "AA"])
            R.main(base + ["--session-id", "thread-0003", "--parent-letter", "ZZ"])
        rows = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([r["label"] for r in rows], ["AA", "AB", "BB"])
        self.assertEqual([r["placed_after"] for r in rows], ["", "AA", ""])

    def test_the_register_command_takes_the_parent_and_the_primary_pick(self):
        base = [
            "register", "--db", self.db, "--platform", "claude", "--location", "mac", "--scope", "default",
            "--seat", "CLAUDE", "--source", "test", "--letter-space", "double", "--letter-pick", "primary",
        ]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            codes = [
                R.main(base + ["--session-id", "thread-0001"]),
                R.main(base + ["--session-id", "thread-0002", "--parent-session-id", "thread-0001"]),
                R.main(base + ["--session-id", "thread-0003", "--parent-session-id", "not a session id!"]),
            ]
        labels = [json.loads(line)["label"] for line in out.getvalue().splitlines()]
        self.assertEqual((codes, labels), ([0, 0, 0], ["AA", "AB", "BB"]))  # a bad parent is no parent

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
        peer = R.SessionRegistry(self.db)  # second connection, same database file

        real_get = R.SessionRegistry._get_by_identity
        barrier = threading.Barrier(2, timeout=5)

        def hooked(self, *args):
            row = real_get(self, *args)
            try:  # both peers have read; only BEGIN IMMEDIATE can keep this from interleaving
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            return row

        errs = []

        def worker():
            # Each connection is opened in the thread that uses it:  a registry is the one writer
            # of the database, and the contended case is two of them, as separate processes.
            reg = R.SessionRegistry(self.db)
            try:
                reg.resolve_or_register(ident())
            except Exception as exc:  # noqa: BLE001 - surfaced through the assertion below
                errs.append(exc)
            finally:
                reg.close_db()

        R.SessionRegistry._get_by_identity = hooked
        try:
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        finally:
            R.SessionRegistry._get_by_identity = real_get

        self.assertEqual(errs, [])
        rows = sqlite3.connect(self.db).execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
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
