"""Tests for simple_tools.operator_registry (AFC-412 PR1).  Run:  python3 scripts/test_operator_registry.py"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import random
import re
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from simple_tools import operator_registry as R

# A synthetic credential for the positive scan cases:  32 mixed-case characters with no
# provider prefix, so the scanner still trips on it while the file carries no real token's
# shape.  A prefixed value would be caught by a secret scanner on the repository itself.
SYNTHETIC_SECRET = "A1b2C3d4" * 4


# The seat each platform namespace belongs to, for fixtures that name only a platform.  The registry
# refuses a platform under another seat (AFC-772, SeatGuardTest below), so a fixture has to name the
# platform's own;  a BotFleet bot's seat is any BF-<ROLE>.
SEATS = {
    "botfleet": "BF-BUILDER",
    "claude": "CLAUDE",
    "codex": "CODEX",
    "cursor": "CURSOR",
    "grok-leader": "GROK",
    "minimax": "MM",
    "muse": "MC",
    "opencode": "OPENCODE",
}


def ident(seat=None, platform="zulip", host="mac-1", scope="simplewithus", native="native-1", **kw):
    # A chat's seat is the one whose namespace the platform is (letters are assigned per seat, owner Nov 11 2026), so a
    # test that names only a platform registers that platform's own seat;  MM on a platform no seat owns.
    if seat is None:
        seat = SEATS.get(R.normalize_platform(platform), "MM")
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

    def test_register_allocates_AA_gen1(self):
        r = self.reg.resolve_or_register(ident())
        self.assertTrue(r["created"])
        self.assertEqual(r["outcome"], "registered")
        self.assertEqual(r["label"], "AA")
        self.assertEqual(r["generation"], 1)

    def test_resume_same_identity_returns_same_record_and_label(self):
        first = self.reg.resolve_or_register(ident())
        again = self.reg.resolve_or_register(ident())
        self.assertFalse(again["created"])
        self.assertEqual(again["outcome"], "resumed")
        self.assertEqual(again["record_id"], first["record_id"])
        self.assertEqual(again["label"], "AA")

    def test_new_native_id_is_new_registration_new_label(self):
        self.reg.resolve_or_register(ident(native="native-1"))
        other = self.reg.resolve_or_register(ident(native="native-2"))
        self.assertTrue(other["created"])
        self.assertEqual(other["label"], "BB")

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
        with mock.patch.dict(R.LETTER_SPACES, {"double": ("AA",)}):
            a = self.reg.resolve_or_register(ident(native="n1"))
            self.assertEqual(a["label"], "AA")
            self.assertTrue(self.reg.close(a["record_id"], actor="MM", source="test"))
            b = self.reg.resolve_or_register(ident(native="n2"))
        self.assertEqual(b["label"], "AA")  # wrapped back to AA once the lap is used
        self.assertEqual(b["generation"], 2)  # disambiguated by generation
        self.assertEqual(R.cross_seat_ref("MM", b["label"], b["generation"]), "MM AA (gen 2)")
        self.assertEqual(R.cross_seat_ref("MM", "B", 1), "MM B")

    def test_a_released_pair_waits_for_the_next_lap(self):
        a = self.reg.resolve_or_register(ident(native="n1"))
        b = self.reg.resolve_or_register(ident(native="n2"))
        self.assertEqual((a["label"], b["label"]), ("AA", "BB"))
        self.reg.close(a["record_id"], actor="MM", source="test")
        c = self.reg.resolve_or_register(ident(native="n3"))
        self.assertEqual(c["label"], "CC")  # keep advancing:  AA comes back after ZZ

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

    def test_a_conversation_takes_a_doubled_pair(self):
        r = self.reg.resolve_or_register(ident(platform="minimax"))
        self.assertEqual(r["label"], "AA")
        self.assertIn(r["label"], R.PRIMARY_ORDER)

    def test_a_caller_that_names_the_single_space_still_gets_a_pair(self):
        # "The order lives in the allocator":  no caller moves a main chat off the doubled pairs.
        one = self.reg.resolve_or_register(
            ident(
                platform="minimax", native="native-1", letter_space="single", letter_pick="lowest"
            )
        )
        two = self.reg.resolve_or_register(ident(platform="minimax", native="native-2"))
        self.assertEqual((one["label"], two["label"]), ("AA", "BB"))

    def test_a_subthread_resumes_with_the_same_double_letter(self):
        first = self.reg.resolve_or_register(
            ident(platform="minimax", native="native-1", letter_space="double")
        )
        again = self.reg.resolve_or_register(
            ident(platform="minimax", native="native-1", letter_space="double")
        )
        self.assertEqual(again["label"], first["label"])
        self.assertFalse(again["created"])

    # ---- a pick names nothing in the pair space:  one order for every seat (owner, Nov 11 2026) ----

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

    def test_a_released_pair_is_not_handed_out_again_whatever_the_pick(self):
        # "Keep advancing":  a freed AA is not the next chat's because it is free and first in the alphabet.
        for pick in ("lowest", "oldest", "primary"):
            with self.subTest(pick=pick):
                self.tearDown()
                self.setUp()
                a = self._pair("n-1", pick)
                self.reg.close(a["record_id"])
                self.assertEqual(
                    [a["label"]] + [self._pair(n, pick)["label"] for n in ("n-2", "n-3")],
                    ["AA", "BB", "CC"],
                )

    def test_the_next_generation_runs_in_the_same_order_whenever_the_pairs_were_retired(self):
        with mock.patch.dict(R.LETTER_SPACES, {"double": ("AA", "BB", "CC")}):
            made = [self._pair("n-%d" % i, "oldest") for i in (1, 2, 3)]
            self.assertEqual([m["label"] for m in made], ["AA", "BB", "CC"])
            for step, index in enumerate((2, 0, 1)):  # retired in this order:  CC first, BB last
                self.reg.close(made[index]["record_id"], at="2026-10-01T00:00:0%d.000Z" % step)
            again = [self._pair("n-%d" % i, "oldest") for i in (4, 5, 6)]
        self.assertEqual(
            [(a["label"], a["generation"]) for a in again], [("AA", 2), ("BB", 2), ("CC", 2)]
        )

    def test_exhaustion_is_always_the_pair_space_and_names_the_limit(self):
        with mock.patch.dict(R.LETTER_SPACES, {"single": ("A",), "double": ("AA",)}):
            self.reg.resolve_or_register(ident(platform="claude", native="n-1"))
            with self.assertRaises(R.LabelExhausted) as one:
                self.reg.resolve_or_register(ident(platform="claude", native="n-3"))
            with self.assertRaises(R.LabelExhausted) as two:
                self.reg.resolve_or_register(
                    ident(platform="claude", native="n-4", letter_space="single")
                )
        self.assertEqual((one.exception.space, two.exception.space), ("double", "double"))
        self.assertEqual(one.exception.limit, 1)

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
            "register",
            "--db",
            self.db,
            "--platform",
            "botfleet",
            "--location",
            "mac",
            "--scope",
            "default",
            "--seat",
            "BF-BUILDER",
            "--source",
            "test",
            "--letter-space",
            "double",
            "--letter-pick",
            "oldest",
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

    def test_reopen_gives_a_chat_its_own_pair_back_as_the_same_generation(self):
        pair = self._pair("n-1", "oldest")
        self.reg.close(pair["record_id"])
        # No other chat held AA since, so the chat has AA.1 back:  a return is not a second issue of AA.
        self.assertEqual(self.reg.reopen(pair["record_id"], pick="oldest"), ("AA", 1, "AA"))
        rows = self.reg._conn.execute(
            "SELECT generation, retired_at FROM alias_assignments WHERE platform = 'claude' AND letter = 'AA'"
        ).fetchall()
        self.assertEqual([(r["generation"], r["retired_at"]) for r in rows], [(1, None)])

    def test_reopen_moves_a_legacy_single_letter_onto_the_next_pair(self):
        single = self.reg.resolve_or_register(ident(platform="claude", native="n-2"))
        with self.reg._conn:
            self.reg._conn.execute(
                "UPDATE alias_assignments SET letter = 'A' WHERE record_id = ?",
                (single["record_id"],),
            )
        self.reg.close(single["record_id"])
        self.assertEqual(self.reg.reopen(single["record_id"]), ("AA", 1, "A"))

    def test_reopen_allocates_in_the_space_of_the_letter_it_held(self):
        with mock.patch.dict(R.LETTER_SPACES, {"double": ("AA", "BB")}):
            held = self._pair("n-1", "oldest")  # AA
            self.reg.close(held["record_id"])
            self._pair("n-2", "oldest")  # BB
            self._pair("n-3", "oldest")  # AA again, generation 2
            other = self.reg.lookup("claude", "mac-1", "simplewithus", "n-2")
            self.reg.close(other["record_id"])  # BB is free now
            letter, generation, previous = self.reg.reopen(held["record_id"], pick="oldest")
        self.assertEqual((letter, previous), ("BB", "AA"))  # a pair, not a single letter
        self.assertEqual(generation, 2)

    # ---- main chats take AA, BB ... ZZ in order;  a subthread takes the pair after its parent's (owner, Oct 29) ----

    def _labels(self, natives, pick="primary", parent=""):
        return [self._pair(n, pick, parent)["label"] for n in natives]

    def test_the_primary_order_is_only_the_26_doubled_pairs_in_the_alphabet(self):
        order = R.PRIMARY_ORDER
        self.assertEqual((len(order), len(set(order))), (26, 26))
        self.assertEqual(order, tuple(a + a for a in R.LETTERS))

    def test_main_chats_take_the_doubled_pairs_in_order(self):
        self.assertEqual(self._labels(["n-1", "n-2", "n-3"]), ["AA", "BB", "CC"])

    def test_a_freed_doubled_pair_is_not_the_next_main_chat_s(self):
        first = self._pair("n-1", "primary")
        self._labels(["n-2", "n-3"])  # BB, CC
        self.reg.close(first["record_id"])
        # Keep advancing (owner, Nov 11 2026):  AA was issued in this generation, so DD comes next, then EE.
        self.assertEqual(self._labels(["n-4", "n-5"]), ["DD", "EE"])

    def test_a_main_chat_never_takes_a_non_doubled_pair_even_when_every_doubled_one_is_live(self):
        self.assertEqual(self._labels(["n-%d" % i for i in range(26)])[-1], "ZZ")
        # All 26 live:  LabelExhausted, no spill into AB, AC ... ("ok just refuse actually is fine")
        with self.assertRaises(R.LabelExhausted) as caught:
            self._pair("x-1", "primary")
        self.assertEqual(caught.exception.space, "double")

    def test_neither_a_pick_nor_a_space_gives_a_single_letter(self):
        labels = [
            self.reg.resolve_or_register(
                ident(platform="claude", native=n, letter_space=space, letter_pick=pick)
            )["label"]
            for n, space, pick in (
                ("n-1", "single", "primary"),
                ("n-2", "single", "lowest"),
                ("n-3", None, None),
            )
        ]
        self.assertEqual(labels, ["AA", "BB", "CC"])

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
        self.assertEqual(
            R.pairs_after("CD")[0], "CE"
        )  # a subthread's own subthreads go after its pair
        self.assertEqual(R.pairs_after("XA")[:2], ("XB", "XC"))
        self.assertNotIn(
            "XX", R.pairs_after("XA")
        )  # never the doubled pair, which belongs to a main chat
        self.assertNotIn("XA", R.pairs_after("XA"))
        self.assertEqual(
            (R.pairs_after("Z"), R.pairs_after("ZZZ"), R.pairs_after("")), ((), (), ())
        )

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
        self.assertEqual(
            self._pair("c-3", "primary", "c")["label"], "CD"
        )  # the first free pair after CC
        self.assertEqual(self._pair("c-4", "primary", "c")["label"], "CF")  # CD and CE are taken
        self.assertEqual(second["label"], "CE")

    def test_a_full_row_gives_a_child_a_random_pair_of_two_different_letters(self):
        # Owner, Nov 11 2026:  "issue random non-pair of letters like random 2 letters not identical ones to preserve
        # those for main chats".
        offered = []

        def last(seq):
            offered.append(list(seq))
            return seq[-1]

        self.reg._choose = last
        self._labels(["a"])  # AA
        row = self._labels(["a-%d" % i for i in range(25)], parent="a")  # AB ... AZ
        self.assertEqual((row[0], row[-1]), ("AB", "AZ"))
        kids = [self._pair(n, "primary", "a") for n in ("a-x", "a-y")]
        self.assertEqual(
            [(k["label"], k["generation"], k["placed_after"]) for k in kids],
            [("ZY", 1, ""), ("ZX", 1, "")],
        )
        # The chooser was offered every free pair of two different letters, any row, and nothing else:  no doubled
        # pair, no live pair, so 650 less row A's 25.
        self.assertEqual(len(offered[0]), 625)
        self.assertTrue(all(p[0] != p[1] for p in offered[0]))
        self.assertFalse(set(offered[0]) & set(row))
        self.assertNotIn("ZY", offered[1])
        # The doubled pairs stayed the main chats':  the next main chat takes BB.
        self.assertEqual(self._pair("m-1", "primary")["label"], "BB")

    def test_the_overflow_pick_is_random_in_production_and_seeded_in_tests(self):
        self.assertIsInstance(R._OVERFLOW_RANDOM, random.SystemRandom)
        picks = []
        for _ in range(2):
            self.tearDown()
            self.setUp()
            self.reg._choose = random.Random(1111).choice
            self._labels(["a"])
            self._labels(["a-%d" % i for i in range(25)], parent="a")
            picks.append(self._pair("a-x", "primary", "a")["label"])
        self.assertEqual(picks[0], picks[1])  # a seeded chooser answers the same every run
        self.assertNotEqual(picks[0][0], picks[0][1])
        self.assertNotEqual(picks[0][0], "A")

    def test_a_random_pair_is_numbered_per_pair_per_seat(self):
        with mock.patch.dict(R.LETTER_SPACES, {"double": ("AA", "AB", "BA", "BB")}):
            self.reg._choose = lambda seq: seq[0]
            self._labels(["a"])  # AA
            self._labels(["a-1"], parent="a")  # AB:  row A is now full
            first = self._pair("a-2", "primary", "a")
            self.assertEqual((first["label"], first["generation"]), ("BA", 1))
            self.reg.close(first["record_id"])
            again = self._pair("a-3", "primary", "a")
            self.assertEqual((again["label"], again["generation"]), ("BA", 2))
            # Every pair of two different letters live:  the child is refused, never handed BB.
            with self.assertRaises(R.LabelExhausted) as caught:
                self._pair("a-4", "primary", "a")
        self.assertIn("all 2 non-doubled pairs are active", str(caught.exception))
        self.assertEqual((caught.exception.space, caught.exception.limit), ("double", 2))

    def test_a_subthreads_pairs_wrap_past_z_back_to_a(self):
        self._labels(["n-%d" % i for i in range(24)])  # AA ... XX
        # The owner's example:  a subthread of XX gets XY, XZ, XA, XB.
        self.assertEqual(
            self._labels(["x-1", "x-2", "x-3", "x-4"], parent="n-23"), ["XY", "XZ", "XA", "XB"]
        )

    def test_a_subthread_of_the_last_doubled_pair_wraps_to_the_start_of_its_row(self):
        self._labels(["n-%d" % i for i in range(26)])  # AA ... ZZ
        self.assertEqual(self._labels(["z-1", "z-2"], parent="n-25"), ["ZA", "ZB"])

    def test_a_subthread_never_takes_a_doubled_pair_even_when_one_is_free(self):
        self._labels(["a"])  # AA
        row = self._labels(["a-%d" % i for i in range(25)], parent="a")  # AB ... AZ
        self.assertEqual(row[-1], "AZ")
        self.reg.close(
            self.reg.lookup("claude", "mac-1", "simplewithus", "a")["record_id"]
        )  # AA is free
        # A child of AZ wraps past Z to A, which would be AA, the row's doubled pair:  it is skipped, the rest of row A
        # is live, so the subthread takes a random pair of two different letters (here the first:  BA), not AA or BB.
        self.reg._choose = lambda seq: seq[0]
        child = self._pair("p-1", "primary", "a-24")
        self.assertEqual((child["label"], child["placed_after"]), ("BA", ""))

    def test_an_unknown_closed_single_or_own_parent_is_no_parent(self):
        parent = self._pair("p", "primary")  # AA
        self.reg.close(parent["record_id"])
        self.assertEqual(self._pair("k-1", "primary", "p")["label"], "BB")  # a closed parent
        self.assertEqual(self._pair("k-2", "primary", "nobody")["label"], "CC")  # an unknown one
        single = self.reg.resolve_or_register(ident(platform="claude", native="s"))
        # A single letter from before the pair rule:  only legacy rows hold one now.
        with self.reg._conn:
            self.reg._conn.execute(
                "UPDATE alias_assignments SET letter = 'A' WHERE record_id = ?",
                (single["record_id"],),
            )
        self.assertEqual(
            self._pair("k-3", "primary", "s")["label"], "DD"
        )  # a parent with a single letter
        self.assertEqual(
            self._pair("k-4", "primary", "k-4")["label"], "EE"
        )  # a chat is not its own parent

    def test_the_parent_is_found_on_any_host(self):
        self.reg.resolve_or_register(
            ident(
                platform="claude",
                native="m",
                host="mac",
                letter_space="double",
                letter_pick="primary",
            )
        )  # AA on the Mac
        child = self.reg.resolve_or_register(
            ident(
                platform="claude",
                native="s-1",
                host="server",
                letter_space="double",
                letter_pick="primary",
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
        letter, _, previous = self.reg.reopen(
            child["record_id"], pick="primary", parent_session_id="c"
        )
        self.assertEqual((letter, previous), ("CE", "CD"))

    def test_a_reopened_main_chat_takes_the_next_pair_in_order_when_its_own_is_gone(self):
        with mock.patch.dict(R.LETTER_SPACES, {"double": ("AA", "BB", "CC")}):
            first = self._pair("n-1", "primary")  # AA
            self.reg.close(first["record_id"])
            self._labels(["n-2", "n-3"])  # BB, CC:  AA is not handed out again in generation 1
            self.assertEqual(
                self._pair("n-4", "primary")["label"], "AA"
            )  # generation 2 starts at AA
            self.reg.close(self.reg.lookup("claude", "mac-1", "simplewithus", "n-2")["record_id"])
            letter, generation, previous = self.reg.reopen(first["record_id"], pick="primary")
        self.assertEqual((letter, generation, previous), ("BB", 2, "AA"))

    def _by_letter(self, native, letter, **kw):
        return self.reg.resolve_or_register(
            ident(
                platform="claude",
                native=native,
                letter_space="double",
                letter_pick="primary",
                parent_letter=letter,
                **kw,
            )
        )

    def test_a_parent_named_by_its_letter_gets_the_pair_after_it(self):
        self._labels(["a", "b", "c"])  # AA, BB, CC
        first = self._by_letter("k-1", "CC")
        second = self._by_letter("k-2", "CC")
        self.assertEqual(
            (first["label"], first["placed_after"], second["label"]), ("CD", "CC", "CE")
        )

    def test_a_parent_letter_that_no_live_session_holds_is_no_parent(self):
        self._labels(["a"])  # AA
        self.assertEqual(self._by_letter("k-1", "CC")["label"], "BB")  # nobody holds CC
        for number, bad in enumerate(("C", "cc", "CCC", "C1", "A A", "")):
            out = self._by_letter("j-%d" % number, bad)
            self.assertEqual(out["placed_after"], "", bad)
        closed = self._pair("g-1", "primary")
        self.reg.close(closed["record_id"])
        self.assertEqual(
            self._by_letter("k-2", closed["label"])["placed_after"], ""
        )  # a closed holder

    def test_a_parent_letter_on_another_platform_is_not_a_parent(self):
        self.reg.resolve_or_register(
            ident(platform="minimax", native="mm-1", letter_space="double")
        )  # AA there
        self._labels(["a"])  # AA on claude too
        self.assertEqual(self._by_letter("k-1", "AA")["label"], "AB")  # followed claude's AA
        self.reg.close(
            self.reg.lookup("claude", "mac-1", "simplewithus", "a")["record_id"]
        )  # claude's AA is gone
        out = self._by_letter(
            "k-2", "AA"
        )  # minimax still holds an AA, which is not this platform's
        self.assertEqual(out["placed_after"], "")

    def test_a_parent_session_id_wins_over_a_parent_letter(self):
        self._labels(["a", "b"])  # AA, BB
        out = self.reg.resolve_or_register(
            ident(
                platform="claude",
                native="k-1",
                letter_space="double",
                letter_pick="primary",
                parent_session_id="b",
                parent_letter="AA",
            )
        )
        self.assertEqual((out["label"], out["placed_after"]), ("BC", "BB"))

    def test_a_parent_on_another_platform_is_not_followed_by_session_id(self):
        self.reg.resolve_or_register(
            ident(platform="minimax", native="shared-id", letter_space="double")
        )  # AA
        out = self._pair("k-1", "primary", "shared-id")
        self.assertEqual((out["label"], out["placed_after"]), ("AA", ""))  # a main chat, not AB

    def test_the_answer_says_nothing_about_a_parent_the_child_did_not_follow(self):
        self._labels(["a"])
        row = self._labels(["a-%d" % i for i in range(25)], parent="a")  # AB ... AZ
        self.assertEqual(len(row), 25)
        self.reg._choose = lambda seq: seq[0]
        full = self._pair("a-x", "primary", "a")  # the row is full:  a random pair, not "after AA"
        self.assertEqual((full["label"], full["placed_after"]), ("BA", ""))
        conn = sqlite3.connect(self.db)
        try:
            details = conn.execute(
                "SELECT details FROM events WHERE session_record_id = ? AND event = 'register'",
                (full["record_id"],),
            ).fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(details, "")

    def test_a_row_cut_down_by_a_small_space_hands_out_no_pair_outside_it(self):
        with mock.patch.dict(R.LETTER_SPACES, {"double": ("AA", "AB", "BB")}):
            self._labels(["a"])  # AA
            # pairs_after("AA") runs AB ... AZ, but only AB is in this space;  the row is then full and no other
            # pair of two different letters exists, so the next child is refused rather than handed BB.
            self.assertEqual(self._labels(["a-1"], parent="a"), ["AB"])
            with self.assertRaises(R.LabelExhausted):
                self._pair("a-2", "primary", "a")
            self.assertEqual(self._labels(["m-1"]), ["BB"])  # BB stays a main chat's

    def test_a_padded_parent_id_finds_the_same_parent_on_every_path(self):
        self._labels(["parent-aaaa"])  # AA
        kid = self._pair("kid-0001", "primary", "parent-aaaa")
        self.assertEqual(kid["label"], "AB")
        self.reg.close(kid["record_id"])
        self._pair("kid-0002", "primary", "parent-aaaa")  # takes AB
        letter, _, previous = self.reg.reopen(
            kid["record_id"], pick="primary", parent_session_id="  parent-aaaa "
        )
        self.assertEqual((letter, previous), ("AC", "AB"))  # after the parent, not a doubled pair
        plain = self.reg.resolve_or_register(
            ident(
                platform="claude",
                native="kid-0003",
                letter_space="double",
                letter_pick="primary",
                parent_session_id=" parent-aaaa",
            )
        )
        self.assertEqual(plain["label"], "AD")

    def test_any_pick_still_follows_a_parent_first(self):
        self._pair("a", "oldest")  # AA
        self.assertEqual(self._pair("k-1", "oldest", "a")["label"], "AB")
        self.assertEqual(
            self._pair("k-2", "oldest")["label"], "BB"
        )  # a chat with no parent takes the next main-chat pair

    # ---- one rule for every seat's main chats (owner, Nov 11 2026):  "keep advancing even if eventually that means
    # not necessarily alphabetical or even if sometimes we may skip a DD if there is a prior generation DD still active
    # and then later on will make a DD once it has been freed up and if never freed up sometimes a generation will just
    # miss out on having a particular pair";  "after ZZ it should go to the next generation if all double letters in
    # alphabet were used";  "it is okay to make the generation be generation of CC's for that platform";  "ok just
    # refuse actually is fine" ----

    def _seat_pair(self, native, platform="muse", parent="", pick="primary"):
        return self.reg.resolve_or_register(
            ident(
                seat=SEATS.get(platform, platform.upper()),
                platform=platform,
                native=native,
                letter_space="double",
                letter_pick=pick,
                parent_session_id=parent,
            )
        )

    def _close_native(self, native, platform="muse"):
        self.reg.close(self.reg.lookup(platform, "mac-1", "simplewithus", native)["record_id"])

    def _issue(self, prefix, count, platform="muse"):
        return [self._seat_pair("%s-%d" % (prefix, i), platform=platform) for i in range(count)]

    def _labelled(self, made):
        return [(m["label"], m["generation"]) for m in made]

    def _all_but_dd_closed(self):
        made = self._issue("g1", 26)
        for m in made:
            if m["label"] != "DD":
                self.reg.close(m["record_id"])
        return made

    def test_keep_advancing_mc_last_got_cc_so_the_next_is_dd_even_when_aa_and_bb_were_released(
        self,
    ):
        self.assertEqual([m["label"] for m in self._issue("mc", 3)], ["AA", "BB", "CC"])
        self._close_native("mc-0")  # AA released
        self._close_native("mc-1")  # BB released
        out = self._seat_pair("mc-3", pick="lowest")  # whatever the caller asks for
        self.assertEqual((out["label"], out["generation"]), ("DD", 1))

    def test_after_zz_it_should_go_to_the_next_generation_if_all_double_letters_were_used(self):
        self._issue("mc", 26)  # AA.1 ... ZZ.1
        for i in range(10):
            self._close_native("mc-%d" % i)  # AA ... JJ archived
        # Never AB:  main chats take only doubled pairs, the fewest-issued free one, so AA.2 then BB.2.
        self.assertEqual(self._labelled(self._issue("next", 2)), [("AA", 2), ("BB", 2)])

    def test_we_may_skip_a_dd_if_there_is_a_prior_generation_dd_still_active(self):
        self._all_but_dd_closed()
        self.assertEqual(
            self._labelled(self._issue("g2", 4)), [("AA", 2), ("BB", 2), ("CC", 2), ("EE", 2)]
        )

    def test_and_then_later_on_will_make_a_dd_once_it_has_been_freed_up_late_in_lap_2(self):
        self._all_but_dd_closed()
        self._issue("g2", 20)  # AA.2 ... UU.2, DD skipped
        self._close_native("g1-3")  # DD's generation 1 chat ends late in lap 2
        # The lap keeps advancing to ZZ and DD, freed, is made in the same lap:  "not necessarily alphabetical".
        self.assertEqual(
            self._labelled(self._issue("g2b", 6)),
            [("VV", 2), ("WW", 2), ("XX", 2), ("YY", 2), ("ZZ", 2), ("DD", 2)],
        )

    def test_if_never_freed_up_in_lap_2_dd_freed_during_lap_3_still_gets_dd_2(self):
        made = self._all_but_dd_closed()
        g2 = self._issue("g2", 25)  # every doubled pair but DD, generation 2
        self.assertNotIn("DD", [m["label"] for m in g2])
        self.assertEqual({m["generation"] for m in g2}, {2})
        for m in g2:
            self.reg.close(m["record_id"])
        first_of_3 = self._seat_pair("g3-0")  # lap 2 is over:  DD was live through all of it
        self.assertEqual((first_of_3["label"], first_of_3["generation"]), ("AA", 3))
        self.reg.close(made[3]["record_id"])  # DD freed during lap 3
        # Its generation counts its own issues ("generation of CC's for that platform"):  DD.2, and it goes first.
        self.assertEqual(self._labelled(self._issue("g3b", 2)), [("DD", 2), ("BB", 3)])

    def test_a_pair_freed_late_is_made_once_and_the_lap_goes_on(self):
        # The reviewer's sequence:  lap 1, DD live through lap 2, lap 3 issued to QQ, then DD freed.  "If never freed
        # up sometimes a generation will just miss out on having a particular pair":  one DD, then RR, never DD twice.
        made = self._all_but_dd_closed()
        for m in self._issue("g2", 25):
            self.reg.close(m["record_id"])
        for m in self._issue("g3", 16):  # AA.3 ... QQ.3 (DD.3 is not one of them:  DD is live)
            self.reg.close(m["record_id"])
        self.reg.close(made[3]["record_id"])
        got = []
        for i in range(4):
            out = self._seat_pair("g3b-%d" % i)
            got.append((out["label"], out["generation"]))
            self.reg.close(out["record_id"])
        self.assertEqual(got, [("DD", 2), ("RR", 3), ("SS", 3), ("TT", 3)])

    def test_a_returning_chat_is_not_a_second_issue_of_its_pair(self):
        # The reviewer's sequence:  AA..EE issued, AA closed and reopened.  It keeps AA.1;  FF..ZZ still come next.
        made = self._issue("r", 5)
        self.reg.close(made[0]["record_id"])
        self.assertEqual(self.reg.reopen(made[0]["record_id"]), ("AA", 1, "AA"))
        nxt = self._seat_pair("r-next")
        self.assertEqual((nxt["label"], nxt["generation"]), ("FF", 1))
        # a chat swept and reopened many times stays AA.1 and never holds AA away from the next lap
        for _ in range(10):
            self.reg.close(made[0]["record_id"])
            self.assertEqual(self.reg.reopen(made[0]["record_id"])[:2], ("AA", 1))

    def test_a_returning_chat_whose_pair_was_issued_since_takes_the_next_pair(self):
        with mock.patch.dict(R.LETTER_SPACES, {"double": ("AA", "BB", "CC")}):
            first = self._seat_pair("r-0")  # AA.1
            self.reg.close(first["record_id"])
            for i in (1, 2, 3):  # BB.1, CC.1, AA.2
                self.reg.close(self._seat_pair("r-%d" % i)["record_id"])
            # AA.2 was another chat's, so the returning chat is lettered by the rule:  BB.2.
            self.assertEqual(self.reg.reopen(first["record_id"]), ("BB", 2, "AA"))

    def test_ok_just_refuse_when_all_26_doubled_pairs_are_live(self):
        self._issue("g1", 26)
        with self.assertRaises(R.LabelExhausted) as caught:
            self._seat_pair("one-too-many")
        self.assertEqual(caught.exception.space, "double")
        self.assertIn("'muse'", str(caught.exception))
        self.assertIn("at most 26 live main chats", str(caught.exception))
        self._close_native("g1-12")  # MM.1 archived:  the next chat takes it
        self.assertEqual(self._labelled(self._issue("after", 1)), [("MM", 2)])

    def test_two_seats_advance_independently(self):
        self.assertEqual([m["label"] for m in self._issue("mc", 3)], ["AA", "BB", "CC"])
        self.assertEqual(self._seat_pair("cx-0", platform="codex")["label"], "AA")
        self._close_native("mc-0")
        self.assertEqual(self._seat_pair("mc-3")["label"], "DD")
        self.assertEqual(self._seat_pair("cx-1", platform="codex")["label"], "BB")

    def test_child_of_dd_is_de_df_dg_and_of_xx_is_xy_xz_xa_xb(self):
        parents = self._issue("p", 24)  # AA ... XX
        self.assertEqual((parents[3]["label"], parents[23]["label"]), ("DD", "XX"))
        kids = [self._seat_pair("d-%d" % i, parent="p-3") for i in range(3)]
        self.assertEqual(self._labelled(kids), [("DE", 1), ("DF", 1), ("DG", 1)])
        kids = [self._seat_pair("x-%d" % i, parent="p-23") for i in range(4)]
        self.assertEqual([k["label"] for k in kids], ["XY", "XZ", "XA", "XB"])

    def test_a_child_never_takes_a_live_pair_and_numbers_its_pair_per_pair(self):
        self._seat_pair("mc-0")  # AA
        first = self._seat_pair("k-1", parent="mc-0")  # AB
        self._seat_pair("k-2", parent="mc-0")  # AC
        self.reg.close(first["record_id"])
        third = self._seat_pair("k-3", parent="mc-0")
        self.assertEqual(
            (third["label"], third["generation"]), ("AB", 2)
        )  # the first free pair after AA, as before
        fourth = self._seat_pair("k-4", parent="mc-0")
        self.assertEqual(fourth["label"], "AD")  # AB and AC are live
        dupes = self.reg._conn.execute(
            "SELECT letter, COUNT(*) AS n FROM alias_assignments"
            " WHERE platform = 'muse' AND retired_at IS NULL GROUP BY letter HAVING n > 1"
        ).fetchall()
        self.assertEqual(dupes, [])

    def test_the_unique_index_refuses_a_second_live_holder(self):
        self._seat_pair("mc-0")  # AA
        with self.assertRaises(sqlite3.IntegrityError):
            with self.reg._conn:
                self.reg._conn.execute(
                    "INSERT INTO alias_assignments(platform, letter, generation, record_id, assigned_at)"
                    " VALUES ('muse', 'AA', 2, 'other-record', '2026-11-11T00:00:00.000Z')"
                )

    def test_an_integrity_error_is_not_reported_as_exhaustion(self):
        # Only a live pair is refused;  any other integrity error (here a record that does not exist) surfaces as
        # itself rather than as "all 26 letters are active".
        with self.assertRaises(sqlite3.IntegrityError), self.reg._write_txn() as conn:
            conn.execute("PRAGMA foreign_keys")
            self.reg._allocate(conn, "muse", "no-such-record", "2026-11-11T00:00:00.000Z")

    def test_concurrent_registrations_never_share_a_pair(self):
        out: list[str] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def work(index: int) -> None:
            reg = R.SessionRegistry(
                self.db
            )  # one connection per thread, as two processes would hold
            try:
                for n in range(5):
                    got = reg.resolve_or_register(
                        ident(
                            seat="MC",
                            platform="muse",
                            native="c-%d-%d" % (index, n),
                            letter_space="double",
                        )
                    )
                    with lock:
                        out.append(got["label"])
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)
            finally:
                reg.close_db()

        threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(out), 20)
        self.assertEqual(len(set(out)), 20)  # no pair handed out twice
        self.assertEqual(
            sorted(out), sorted(R.PRIMARY_ORDER[:20])
        )  # and the losers took the next ones in order

    def test_legacy_rows_count_each_pairs_issues(self):
        # History the old lowest-first pick left behind:  AA issued 4 times, BB twice, DD once, CC never.  The fewest
        # issued free pair goes first, the earlier on a tie, and each takes its own count plus one.
        rows = [("AA", 1), ("AA", 2), ("AA", 3), ("AA", 4), ("BB", 1), ("BB", 2), ("DD", 1)]
        old = self.reg.resolve_or_register(ident(seat="MC", platform="muse", native="old"))
        self.reg.close(old["record_id"])
        with self.reg._conn:
            self.reg._conn.execute(
                "DELETE FROM alias_assignments WHERE record_id = ?", (old["record_id"],)
            )
            for letter, generation in rows:
                self.reg._conn.execute(
                    "INSERT INTO alias_assignments(platform, letter, generation, record_id, assigned_at, retired_at)"
                    " VALUES ('muse', ?, ?, ?, '2026-10-01T00:00:00.000Z', '2026-10-02T00:00:00.000Z')",
                    (letter, generation, old["record_id"]),
                )
        with mock.patch.dict(R.LETTER_SPACES, {"double": ("AA", "BB", "CC", "DD")}):
            made = [self._seat_pair("n-%d" % i, pick="lowest") for i in range(4)]
        self.assertEqual(self._labelled(made), [("CC", 1), ("DD", 2), ("BB", 3), ("AA", 5)])
        # In the full space every never-issued doubled pair comes before the ones the old pick recycled.
        self.assertEqual(self._seat_pair("n-full")["label"], "EE")

    def test_count_the_history_uneven_legacy_issues_in_the_full_space(self):
        # Owner, Nov 11 2026:  "count the history".  Legacy rows:  AA issued 5 times, BB 3, every other doubled pair
        # once, all retired.  The 24 issued once come first, in lap order from after the newest generation's last pair
        # (AA.5), so CC.2 ... ZZ.2;  then BB.4;  then AA.6.
        old = self.reg.resolve_or_register(ident(seat="MC", platform="muse", native="old"))
        self.reg.close(old["record_id"])
        rows = [("AA", g) for g in range(1, 6)] + [("BB", g) for g in range(1, 4)]
        rows += [(p, 1) for p in R.PRIMARY_ORDER[2:]]
        with self.reg._conn:
            self.reg._conn.execute(
                "DELETE FROM alias_assignments WHERE record_id = ?", (old["record_id"],)
            )
            for letter, generation in rows:
                self.reg._conn.execute(
                    "INSERT INTO alias_assignments(platform, letter, generation, record_id, assigned_at, retired_at)"
                    " VALUES ('muse', ?, ?, ?, '2026-10-01T00:00:00.000Z', '2026-10-02T00:00:00.000Z')",
                    (letter, generation, old["record_id"]),
                )
        made = self._labelled(self._issue("n", 26))
        self.assertEqual(made[:24], [(p, 2) for p in R.PRIMARY_ORDER[2:]])
        self.assertEqual(made[24:], [("BB", 4), ("AA", 6)])

    def test_a_legacy_single_letter_moves_onto_a_pair_when_its_chat_next_registers(self):
        # Owner, Nov 11 2026:  "no single letters".  A live chat that still holds a single letter is moved onto a pair
        # by the main-chat rule on its next registration;  the single letter is retired and stays in its history.
        self._issue("mc", 2)  # AA, BB
        held = self._seat_pair(
            "legacy"
        )  # CC, rewritten below to the legacy single letter C, so CC has no issue
        with self.reg._conn:
            self.reg._conn.execute(
                "UPDATE alias_assignments SET letter = 'C' WHERE record_id = ?",
                (held["record_id"],),
            )
        again = self._seat_pair("legacy")
        self.assertEqual(
            (
                again["label"],
                again["generation"],
                again["outcome"],
                again["previous"],
                again["created"],
            ),
            ("CC", 1, "resumed", "C", False),
        )
        rows = self.reg._conn.execute(
            "SELECT letter, retired_at IS NULL AS live FROM alias_assignments WHERE record_id = ? ORDER BY rowid",
            (held["record_id"],),
        ).fetchall()
        self.assertEqual([(r["letter"], r["live"]) for r in rows], [("C", 0), ("CC", 1)])
        events = [
            (r["event"], r["details"])
            for r in self.reg._conn.execute(
                "SELECT event, details FROM events WHERE session_record_id = ? ORDER BY rowid",
                (held["record_id"],),
            )
        ]
        self.assertIn(("relabel", "previous letter C"), events)
        # A rerun is a plain resume:  the chat keeps CC.
        self.assertEqual(self._seat_pair("legacy")["label"], "CC")

    def test_a_legacy_single_letter_child_moves_into_its_parents_row(self):
        self._issue("mc", 2)  # AA, BB
        held = self._seat_pair("kid")  # CC, rewritten to the single letter K
        with self.reg._conn:
            self.reg._conn.execute(
                "UPDATE alias_assignments SET letter = 'K' WHERE record_id = ?",
                (held["record_id"],),
            )
        again = self._seat_pair("kid", parent="mc-1")
        self.assertEqual((again["label"], again["previous"]), ("BC", "K"))

    def test_no_path_issues_a_single_letter(self):
        self.reg._choose = random.Random(5).choice
        labels = [self._seat_pair("m-%d" % i)["label"] for i in range(3)]
        labels += [
            self.reg.resolve_or_register(
                ident(
                    seat="MC",
                    platform="muse",
                    native="s-%d" % i,
                    letter_space="single",
                    letter_pick="lowest",
                    parent_session_id="m-0" if i else "",
                )
            )["label"]
            for i in range(30)
        ]
        made = self._seat_pair("r-1")
        self.reg.close(made["record_id"])
        labels.append(self.reg.reopen(made["record_id"])[0])
        labels.append(self.reg.relabel(made["record_id"], space="single")["label"])
        self.assertTrue(all(len(x) == 2 for x in labels), labels)

    def _register(self, native, platform="claude", seat=None, **kw):
        return R.register_session(
            self.db,
            platform=platform,
            verified_host="mac",
            profile_scope="default",
            native_session_id=native,
            seat=seat or SEATS.get(R.normalize_platform(platform), "CLAUDE"),
            source="test",
            issuer=R.ISSUER_OPERATOR,
            **kw,
        )

    def test_every_platforms_main_chat_takes_a_doubled_pair_whoever_registers_it(self):
        # The Operator's own ask-and-answer path registers a chat with no space or pick (operator_registration,
        # daemon).  Owner, Nov 11 2026:  "if a Muse Code seat asks for a letter then it will get a DD since the last
        # assigned to MC was CC".  Every seat, not only Claude and MiniMax.
        for platform in (
            "claude",
            "minimax",
            "muse",
            "codex",
            "cursor",
            "grok-leader",
            "opencode",
            "botfleet",
        ):
            with self.subTest(platform=platform):
                self.assertEqual(
                    [
                        self._register("%s-000%d" % (platform, i), platform=platform)["label"]
                        for i in (1, 2, 3)
                    ],
                    ["AA", "BB", "CC"],
                )
        # a caller that names a space or a pick changes nothing:  the order is the allocator's
        self.assertEqual(
            self._register("muse-0004", platform="muse", letter_space="single")["label"], "DD"
        )
        self.assertEqual(
            self._register(
                "muse-0005", platform="muse", letter_space="double", letter_pick="lowest"
            )["label"],
            "EE",
        )
        with self.assertRaises(ValueError):
            self._register("muse-0006", platform="muse", letter_space="triple")

    def test_a_muse_code_seat_that_last_got_cc_gets_dd_through_the_operator_path(self):
        made = [self._register("mc-%d" % i, platform="muse", seat="MC") for i in range(3)]
        self.assertEqual([m["label"] for m in made], ["AA", "BB", "CC"])
        for m in made[:2]:
            self.reg.close(m["record_id"])
        out = self._register("mc-3", platform="muse", seat="MC")
        self.assertEqual((out["label"], out["generation"]), ("DD", 1))

    # ---- the seat label is the key (owner, Nov 11 2026) ----

    def test_one_seat_under_two_platform_labels_shares_one_lap(self):
        # Grok leader, terminal Grok and Grok Build are one seat, GROK.
        labels = [
            self._register("grok-%d" % i, platform=platform, seat="GROK")["label"]
            for i, platform in enumerate(("grok-leader", "grok", "grok-build", "GROK-BUILD"))
        ]
        self.assertEqual(labels, ["AA", "BB", "CC", "DD"])
        # and any Cursor adapter is CURSOR
        labels = [
            self._register("cursor-%d" % i, platform=platform, seat="CURSOR")["label"]
            for i, platform in enumerate(("cursor-cloud", "cursor-local", "cursor"))
        ]
        self.assertEqual(labels, ["AA", "BB", "CC"])

    def test_two_seats_under_one_label_share_its_namespace(self):
        # The namespace is the platform label, folded (one seat, many labels:  above).  Two seats under one label
        # share it, as before:  whether the key should be the seat tag itself is the owner's call (AFC-724).
        self.assertEqual(
            self._register("bf-1", platform="botfleet", seat="BF-BUILDER")["label"], "AA"
        )
        self.assertEqual(
            self._register("bf-2", platform="botfleet", seat="BF-DEPLOYER")["label"], "BB"
        )

    # ---- callers cannot move a main chat (owner, Nov 11 2026:  the order lives in the allocator) ----

    def test_a_forced_relabel_of_a_doubled_pair_with_no_parent_changes_nothing(self):
        self._register("cl-1")  # AA
        for _ in range(3):
            out = self._relabel("cl-1", platform="claude", force=True)
            self.assertEqual((out["outcome"], out["label"]), ("unchanged", "AA"))
        self.assertEqual(self._register("cl-2")["label"], "BB")  # no pair of the lap was burned

    def test_a_relabel_into_the_single_space_keeps_the_pair(self):
        self._register("cl-1")
        out = self._relabel("cl-1", platform="claude", letter_space="single", letter_pick="lowest")
        self.assertEqual((out["outcome"], out["label"]), ("unchanged", "AA"))

    # ---- relabel:  a live session moves into another letter space (AFC-586) ----

    def _relabel(self, native, seat=None, platform="minimax", **kw):
        seat = seat or SEATS.get(R.normalize_platform(platform), "CLAUDE")
        return R.relabel_session(
            self.db,
            platform=platform,
            verified_host="mac",
            profile_scope="default",
            native_session_id=native,
            seat=seat,
            source="test",
            issuer=R.ISSUER_OPERATOR,
            **kw,
        )

    def _legacy_single(self, made, letter="A"):
        """Turn a registration into one that holds a single letter, as a chat lettered before the pair rule does."""
        with self.reg._conn:
            self.reg._conn.execute(
                "UPDATE alias_assignments SET letter = ? WHERE record_id = ?",
                (letter, made["record_id"]),
            )
        return made

    def test_relabel_moves_a_live_session_into_the_pair_space(self):
        first = self._legacy_single(self._register("mm-1", platform="minimax"))
        self.assertEqual(self.reg.lookup("minimax", "mac", "default", "mm-1")["label"], "A")
        out = self._relabel("mm-1", letter_space="double", letter_pick="primary")
        self.assertEqual(out["label"], "AA")
        self.assertEqual(out["previous"], "A")
        self.assertEqual(out["outcome"], "relabelled")
        # the old letter is retired, not deleted:  its history and its generation count stay.
        retired = self.reg._conn.execute(
            "SELECT retired_at FROM alias_assignments"
            " WHERE platform = 'minimax' AND letter = 'A' AND generation = 1"
        ).fetchone()
        self.assertIsNotNone(retired["retired_at"])
        live = self.reg._conn.execute(
            "SELECT letter FROM alias_assignments WHERE platform = 'minimax' AND retired_at IS NULL"
        ).fetchall()
        self.assertEqual([row["letter"] for row in live], ["AA"])

    def test_relabel_is_idempotent_when_the_session_already_holds_that_space(self):
        first = self._register("mm-1", platform="minimax")
        self.assertEqual(first["label"], "AA")
        out = self._relabel("mm-1", letter_space="double", letter_pick="primary")
        self.assertEqual(out["outcome"], "unchanged")
        self.assertEqual(out["label"], "AA")

    def test_relabel_moves_a_child_into_its_parents_row(self):
        parent = self._register("mm-parent", platform="minimax")  # AA
        child = self._register("mm-child", platform="minimax")  # BB:  no parent was named
        self.assertEqual((parent["label"], child["label"]), ("AA", "BB"))
        out = self._relabel(
            "mm-child", parent_session_id="mm-parent", letter_space="double", force=True
        )
        self.assertEqual(out["label"], "AB")  # the first free pair after its parent's AA
        self.assertEqual(out["placed_after"], "AA")

    def test_relabel_refuses_another_seats_session(self):
        self._register("mm-1", platform="minimax")
        with self.assertRaises(ValueError):
            self._relabel("mm-1", seat="GROK", letter_space="double")

    def test_relabel_refuses_a_session_the_registry_never_saw(self):
        with self.assertRaises(ValueError):
            self._relabel("mm-unknown", letter_space="double")

    def test_relabel_only_the_operator_may_issue_a_letter(self):
        self._register("mm-1", platform="minimax")
        with self.assertRaises(ValueError):
            R.relabel_session(
                self.db,
                platform="minimax",
                verified_host="mac",
                profile_scope="default",
                native_session_id="mm-1",
                seat="CLAUDE",
                source="test",
                issuer="MM",
            )

    def test_the_relabel_command_answers_json(self):
        self._legacy_single(self._register("mm-1", platform="minimax"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = R.main(
                [
                    "relabel",
                    "--db",
                    self.db,
                    "--platform",
                    "minimax",
                    "--location",
                    "mac",
                    "--scope",
                    "default",
                    "--session-id",
                    "mm-1",
                    "--seat",
                    "MM",
                    "--source",
                    "test",
                ]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out.getvalue())["label"], "AA")

    def test_the_register_command_defaults_by_platform(self):
        base = [
            "register",
            "--db",
            self.db,
            "--location",
            "mac",
            "--scope",
            "default",
            "--seat",
            "CLAUDE",
            "--source",
            "test",
        ]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            R.main(base + ["--platform", "claude", "--session-id", "thread-0001"])
            R.main(base + ["--platform", "codex", "--seat", "CODEX", "--session-id", "thread-0002"])
            R.main(
                base
                + [
                    "--platform",
                    "claude",
                    "--session-id",
                    "thread-0003",
                    "--letter-space",
                    "single",
                ]
            )
        labels = [json.loads(line)["label"] for line in out.getvalue().splitlines()]
        # every platform, and a named single space, takes a doubled pair:  the order is the allocator's
        self.assertEqual(labels, ["AA", "AA", "BB"])

    def test_the_register_command_takes_a_parent_letter(self):
        base = [
            "register",
            "--db",
            self.db,
            "--platform",
            "codex",
            "--location",
            "mac",
            "--scope",
            "default",
            "--seat",
            "CODEX",
            "--source",
            "test",
            "--letter-space",
            "double",
            "--letter-pick",
            "primary",
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
            "register",
            "--db",
            self.db,
            "--platform",
            "claude",
            "--location",
            "mac",
            "--scope",
            "default",
            "--seat",
            "CLAUDE",
            "--source",
            "test",
            "--letter-space",
            "double",
            "--letter-pick",
            "primary",
        ]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            codes = [
                R.main(base + ["--session-id", "thread-0001"]),
                R.main(
                    base + ["--session-id", "thread-0002", "--parent-session-id", "thread-0001"]
                ),
                R.main(
                    base
                    + ["--session-id", "thread-0003", "--parent-session-id", "not a session id!"]
                ),
            ]
        labels = [json.loads(line)["label"] for line in out.getvalue().splitlines()]
        self.assertEqual(
            (codes, labels), ([0, 0, 0], ["AA", "AB", "BB"])
        )  # a bad parent is no parent

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
            "AA: Memory sync, fleet skills and Operator session letter",
        )
        self.assertEqual(self.reg._get(a["record_id"])["title"], out["title"])

    def test_update_session_strips_a_display_title_prefix(self):
        self.reg.resolve_or_register(ident(platform="minimax", title="Placeholder"))
        out = R.update_session(
            self.db,
            platform="minimax",
            native_session_id="native-1",
            title="AA: Memory sync",
            **self._loc(platform="minimax"),
        )
        self.assertEqual(out["title_display"], "AA: Memory sync")
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

    def _force_live_letter(self, record_id: str, letter: str) -> None:
        now = "2026-11-05T12:00:00.000Z"
        self.reg._conn.execute(
            "UPDATE alias_assignments SET letter = ?, retired_at = NULL WHERE record_id = ?",
            (letter, record_id),
        )
        live = self.reg._conn.execute(
            "SELECT 1 FROM alias_assignments WHERE record_id = ? AND retired_at IS NULL",
            (record_id,),
        ).fetchone()
        if not live:
            self.reg._conn.execute(
                "INSERT INTO alias_assignments(platform, letter, generation, record_id, assigned_at)"
                " VALUES (?, ?, 1, ?, ?)",
                ("minimax", letter, record_id, now),
            )
        self.reg._conn.commit()

    def _insert_orphan_session_row(self, *, host: str, letter: str, native: str = "") -> str:
        """A live row with no real native session id (AFC-640 reproduction)."""
        from simple_tools.operator_registry import _ulid

        now = "2026-11-05T12:00:00.000Z"
        record_id = _ulid()
        self.reg._conn.execute(
            "INSERT INTO sessions(record_id, seat, platform, verified_host, profile_scope,"
            " native_session_id, title, project, thread_link, apple_note_link,"
            " wake_capability, registered_at, last_seen_at, source)"
            " VALUES (?, ?, ?, ?, ?, ?, '', '', '', '', 'unknown', ?, ?, ?)",
            (record_id, "MM", "minimax", host, "default", native, now, now, "test orphan"),
        )
        self.reg._conn.execute(
            "INSERT INTO alias_assignments(platform, letter, generation, record_id, assigned_at)"
            " VALUES (?, ?, 1, ?, ?)",
            ("minimax", letter, record_id, now),
        )
        self.reg._conn.commit()
        return record_id

    def test_register_and_update_follow_the_live_session_id_not_a_placeholder_row(self):
        """AFC-640: EH holds the session id; an empty-session row holds NN and must not win."""
        native = "mvs_b3fef2c69d4e44c9b07f403581d6938a"
        mac = self._loc(platform="minimax", host="mac-1")
        cloud = self._loc(platform="minimax", host="cloud-1")
        eh = self.reg.resolve_or_register(
            ident(platform="minimax", host="mac-1", native=native, letter_space="double")
        )
        self._force_live_letter(eh["record_id"], "EH")
        remote_orphan = self._insert_orphan_session_row(host="cloud-1", letter="NN", native="")
        resumed = R.register_session(
            self.db,
            platform="minimax",
            native_session_id=native,
            seat="MM",
            source="test rename",
            issuer=R.ISSUER_OPERATOR,
            **mac,
        )
        self.assertEqual(resumed["outcome"], "resumed")
        self.assertEqual(resumed["record_id"], eh["record_id"])
        self.assertEqual(resumed["label"], "EH")
        out = R.update_session(
            self.db,
            platform="minimax",
            native_session_id=native,
            title="AFC-621 Housekeeper Webhook Credential Path",
            **mac,
        )
        self.assertEqual(out["record_id"], eh["record_id"])
        self.assertEqual(out["label"], "EH")
        self.assertIn("Housekeeper", str(self.reg._get(eh["record_id"])["title"]))
        self.assertEqual(str(self.reg._get(remote_orphan)["native_session_id"]), "")
        resolved = R.resolve_label(self.db, "minimax", "EH")
        self.assertEqual(resolved["native_session_id"], native)

    def test_update_session_at_a_mismatched_host_does_not_touch_a_remote_live_row(self):
        """Fail closed:  a cloud caller must not mutate a mac row with the same session id."""
        native = "mvs_cross_host_guard"
        mac = self._loc(platform="minimax", host="mac-1")
        cloud = self._loc(platform="minimax", host="cloud-1")
        eh = self.reg.resolve_or_register(
            ident(platform="minimax", host="mac-1", native=native, letter_space="double")
        )
        self._force_live_letter(eh["record_id"], "EH")
        with self.assertRaises(ValueError) as caught:
            R.update_session(
                self.db,
                platform="minimax",
                native_session_id=native,
                title="Should not land",
                **cloud,
            )
        self.assertIn("not registered", str(caught.exception))
        self.assertNotIn("Should not land", str(self.reg._get(eh["record_id"])["title"]))

    def test_touch_names_the_live_letter_and_follows_a_change_in_place(self):
        """A touch answers with the letter and generation the record holds now, so a hook sees a relabel."""
        native = "mvs_touch_names_letter"
        mac = self._loc(platform="minimax", host="mac-1")
        live = self.reg.resolve_or_register(ident(platform="minimax", host="mac-1", native=native))
        touched = R.touch_session(self.db, platform="minimax", native_session_id=native, **mac)
        self.assertEqual(
            touched,
            {
                "record_id": live["record_id"],
                "outcome": "seen",
                "label": live["label"],
                "generation": live["generation"],
            },
        )
        self._force_live_letter(live["record_id"], "QX")  # the registry moves the letter in place
        moved = R.touch_session(self.db, platform="minimax", native_session_id=native, **mac)
        self.assertEqual((moved["outcome"], moved["label"]), ("seen", "QX"))

    def test_touch_on_a_closed_row_keeps_its_old_answer(self):
        native = "mvs_touch_closed"
        mac = self._loc(platform="minimax", host="mac-1")
        live = self.reg.resolve_or_register(ident(platform="minimax", host="mac-1", native=native))
        self.reg.close(live["record_id"], actor="MM", source="test")
        closed = R.touch_session(self.db, platform="minimax", native_session_id=native, **mac)
        self.assertEqual(closed, {"record_id": live["record_id"], "outcome": "closed"})

    def test_update_session_on_a_closed_row_is_consistent_across_hosts(self):
        """A closed row at the caller's host answers closed;  another host sees a miss."""
        native = "mvs_closed_consistency"
        mac = self._loc(platform="minimax", host="mac-1")
        cloud = self._loc(platform="minimax", host="cloud-1")
        live = self.reg.resolve_or_register(ident(platform="minimax", host="mac-1", native=native))
        self.reg.close(live["record_id"], actor="MM", source="test")
        at_mac = R.update_session(
            self.db,
            platform="minimax",
            native_session_id=native,
            title="T",
            **mac,
        )
        self.assertEqual(at_mac["outcome"], "closed")
        with self.assertRaises(ValueError) as caught:
            R.update_session(
                self.db,
                platform="minimax",
                native_session_id=native,
                title="T",
                **cloud,
            )
        self.assertIn("not registered", str(caught.exception))

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
        self.assertEqual(first["label"], "AA")
        self.assertEqual(again["label"], "AA")
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
        self.assertFalse(by_letter["AA"]["live"])
        self.assertTrue(by_letter["BB"]["live"])
        self.assertIsNone(by_letter["BB"]["ended_at"])

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
        # Hold the first writer only until the second connection reaches its
        # BEGIN IMMEDIATE trace callback (called before lock acquisition). The
        # second writer need not enter a locked read, avoiding the old deadlock.
        first_locked = threading.Event()
        second_begin = threading.Event()
        second_ready = threading.Event()
        errs = []
        real_get = R.SessionRegistry._get_by_identity

        def hooked(reg, *args):
            if threading.current_thread().name == "registry-first":
                self.assertTrue(reg._conn.in_transaction)
                first_locked.set()
                self.assertTrue(second_begin.wait(timeout=2), "second writer never attempted BEGIN")
            return real_get(reg, *args)

        def worker(second=False):
            reg = None
            try:
                reg = R.SessionRegistry(self.db)
                if not second:
                    self.assertTrue(second_ready.wait(timeout=2), "second connection not ready")
                if second:
                    second_ready.set()
                    self.assertTrue(
                        first_locked.wait(timeout=2), "first writer never acquired lock"
                    )
                    reg._conn.set_trace_callback(
                        lambda sql: second_begin.set() if sql == "BEGIN IMMEDIATE" else None
                    )
                reg.resolve_or_register(ident())
            except Exception as exc:  # noqa: BLE001 - surfaced by assertion
                errs.append(exc)
            finally:
                if reg is not None:
                    reg.close_db()

        threads = [
            threading.Thread(target=worker, name="registry-first", daemon=True),
            threading.Thread(target=worker, args=(True,), name="registry-second", daemon=True),
        ]
        R.SessionRegistry._get_by_identity = hooked
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=4)
        finally:
            first_locked.set()
            second_begin.set()
            for thread in threads:
                thread.join(timeout=1)
            R.SessionRegistry._get_by_identity = real_get
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertTrue(first_locked.is_set())
        self.assertTrue(second_begin.is_set())

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
        self.assertEqual(cells[1], "AA")


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
        self.assertEqual([row["label"] for row in rows], ["AA", "BB"])

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
        self.assertEqual(rows[0]["label"], "AA")

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


class RegistryGuardTest(unittest.TestCase):
    """The CLI never brings a registry into being (AFC-593).

    The transport runs this CLI directly, so the guard belongs here and not only in
    the passthrough that runs it through simple-chat:  a host that keeps no registry
    refuses every op the same way, and a second registry never grows to hand out
    letters that collide with the ones the Operator keeps.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.missing = os.path.join(self.tmp.name, "no-registry-here", "registry.sqlite3")

    def _run(self, argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = R.main(argv)
        return code, json.loads(out.getvalue().splitlines()[-1])

    def test_every_op_that_takes_a_database_refuses_on_a_host_without_one(self):
        ops = [
            ["pending-outbox"],
            ["mark-exported", "1"],
            [
                "register",
                "--platform=minimax",
                "--location=server",
                "--scope=default",
                "--session-id=s-1",
                "--seat=MM",
                "--source=a test",
            ],
            [
                "relabel",
                "--platform=minimax",
                "--location=server",
                "--scope=default",
                "--session-id=s-1",
                "--seat=MM",
                "--source=a test",
            ],
            ["resolve", "--platform=minimax", "--letter=AA"],
            [
                "update",
                "--platform=minimax",
                "--location=server",
                "--scope=default",
                "--session-id=s-1",
            ],
            [
                "close",
                "--platform=minimax",
                "--location=server",
                "--scope=default",
                "--session-id=s-1",
            ],
            ["sweep", "--platform=minimax", "--seat=MM"],
            ["history", "--platform=minimax"],
            [
                "touch",
                "--platform=minimax",
                "--location=server",
                "--scope=default",
                "--session-id=s-1",
            ],
            ["reopen", "--record-id=r1"],
        ]
        for argv in ops:
            with self.subTest(op=argv[0]):
                code, answer = self._run(argv + ["--db", self.missing])
                self.assertEqual(code, 3)
                self.assertEqual(answer, {"error": "no Operator registry on this host"})
        self.assertFalse(os.path.exists(self.missing))

    def test_a_host_that_keeps_a_registry_is_not_refused(self):
        db = os.path.join(self.tmp.name, "registry.sqlite3")
        reg = R.SessionRegistry(db)
        reg.close_db()
        code, answer = self._run(["history", "--platform=minimax", "--db", db])
        self.assertEqual(code, 0)
        self.assertEqual(answer, [])


class SessionIssuesTest(unittest.TestCase):
    """AFC-694:  the Linear issues a session works on, kept in the session_issues side table."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "registry.sqlite3")
        reg = R.SessionRegistry(self.db)
        try:
            self.made = reg.resolve_or_register(
                ident(platform="minimax", host="mac-1", native="mvs_issues_1")
            )
        finally:
            reg.close_db()
        self.where = {
            "platform": "minimax",
            "verified_host": "mac-1",
            "profile_scope": "simplewithus",
            "native_session_id": "mvs_issues_1",
        }

    def update(self, **fields):
        return R.update_session(self.db, **self.where, **fields)

    def test_the_team_keys_are_the_boards(self):
        board = os.path.join(os.path.dirname(os.path.abspath(__file__)), "linear-board", "board")
        with open(board, encoding="utf-8") as fh:
            match = re.search(r"(?m)^TEAM_KEYS = (\(.*?\))\s*$", fh.read(), re.DOTALL)
        self.assertIsNotNone(match)
        self.assertEqual(R.LINEAR_TEAM_KEYS, tuple(ast.literal_eval(match.group(1))))

    def test_an_old_registry_keeps_every_row_and_gains_the_table(self):
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("DROP TABLE session_issues")
            conn.execute("DELETE FROM schema_migrations WHERE version = 3")
        conn.close()
        # A read on the old file (no DDL) answers as before, with no issues.
        found = R.resolve_label(self.db, "minimax", self.made["label"])
        self.assertNotIn("issues", found)
        self.assertNotIn("issues", R.letter_history(self.db, "minimax")[0])
        # The next writer adds the table, and the row is still there.
        out = self.update(issues=[{"id": "AFC-694", "seen_at": "2026-11-10T15:00:00Z"}])
        self.assertEqual(out["record_id"], self.made["record_id"])
        self.assertEqual(out["issues"], [{"id": "AFC-694", "seen_at": "2026-11-10T15:00:00Z"}])
        conn = sqlite3.connect(self.db)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 1)
            self.assertEqual(
                conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 3
            )
        finally:
            conn.close()

    def test_the_field_round_trips_through_every_answer_that_describes_the_record(self):
        issues = [
            {"id": "AFC-694", "seen_at": "2026-11-10T15:00:00Z"},
            {"id": "OPS-12", "seen_at": "2026-11-10T14:00:00Z"},
        ]
        out = self.update(issues=issues)
        self.assertEqual((out["outcome"], out["changed"]), ("updated", ["issues"]))
        self.assertEqual(out["issues"], issues)
        touched = R.touch_session(self.db, **self.where)
        self.assertEqual(touched["issues"], issues)
        self.assertEqual(touched["outcome"], "seen")
        self.assertEqual(R.resolve_label(self.db, "minimax", self.made["label"])["issues"], issues)
        self.assertEqual(R.letter_history(self.db, "minimax")[0]["issues"], issues)
        # The same list again changes nothing;  an update without the field leaves it as it is.
        self.assertEqual(self.update(issues=list(reversed(issues)))["outcome"], "unchanged")
        out = self.update(title="Other Words")
        self.assertEqual(out["changed"], ["title"])
        self.assertEqual(out["issues"], issues)
        # An empty list clears it, and an answer then carries no field.
        out = self.update(issues=[])
        self.assertNotIn("issues", out)
        self.assertNotIn("issues", R.touch_session(self.db, **self.where))

    def test_invalid_ids_are_dropped_and_the_list_is_deduped_newest_first_and_capped(self):
        stamp = "2026-11-10T%02d:00:00Z"
        given = [
            {"id": "XYZ-1", "seen_at": stamp % 1},  # not a team key
            {"id": "AFC-", "seen_at": stamp % 1},
            {"id": "AFC-12a", "seen_at": stamp % 1},
            {"id": "AFC-5"},  # no seen_at
            {"id": "AFC-6", "seen_at": "2026-11-10T01:00:00"},  # no zone
            "not an entry",
            {"id": "afc-7", "seen_at": stamp % 2},  # read as AFC-7
            {"id": "AFC-7", "seen_at": stamp % 9},  # the newer copy wins
            {"id": "AFC-7", "seen_at": stamp % 3},
        ] + [{"id": "OPS-%d" % n, "seen_at": stamp % (10 + n)} for n in range(1, 10)]
        out = self.update(issues=given)
        ids = [i["id"] for i in out["issues"]]
        self.assertEqual(len(ids), R.MAX_ISSUES)
        self.assertEqual(
            ids, ["OPS-9", "OPS-8", "OPS-7", "OPS-6", "OPS-5", "OPS-4", "OPS-3", "OPS-2"]
        )
        out = self.update(issues=given[:9])
        self.assertEqual(out["issues"], [{"id": "AFC-7", "seen_at": stamp % 9}])

    def test_history_carries_the_last_seen_time_and_a_touch_moves_it_forward(self):
        conn = sqlite3.connect(self.db)
        with conn:
            conn.execute("UPDATE sessions SET last_seen_at = '2026-01-01T00:00:00.000Z'")
        conn.close()
        self.assertEqual(
            R.letter_history(self.db, "minimax")[0]["last_seen_at"], "2026-01-01T00:00:00.000Z"
        )
        R.touch_session(self.db, **self.where)
        after = R.letter_history(self.db, "minimax")[0]["last_seen_at"]
        self.assertGreater(after, "2026-01-01T00:00:00.000Z")

    def test_a_value_that_is_not_a_list_is_refused(self):
        with self.assertRaises(ValueError):
            self.update(issues="AFC-694")

    def test_a_closed_session_keeps_its_issues_and_takes_no_new_ones(self):
        self.update(issues=[{"id": "AFC-694", "seen_at": "2026-11-10T15:00:00Z"}])
        reg = R.SessionRegistry(self.db)
        try:
            reg.close(self.made["record_id"], actor="MM", source="test")
        finally:
            reg.close_db()
        out = self.update(issues=[{"id": "OPS-1", "seen_at": "2026-11-10T16:00:00Z"}])
        self.assertEqual(out["outcome"], "closed")
        self.assertEqual([i["id"] for i in out["issues"]], ["AFC-694"])
        # A touch on the closed session reports the list it keeps, as update does (Kody on #1762).
        self.assertEqual(
            R.touch_session(self.db, **self.where),
            {
                "record_id": self.made["record_id"],
                "outcome": "closed",
                "issues": [{"id": "AFC-694", "seen_at": "2026-11-10T15:00:00Z"}],
            },
        )

    def test_the_cli_update_takes_issues_as_json(self):
        argv = [
            "update",
            "--db",
            self.db,
            "--platform",
            "minimax",
            "--location",
            "mac-1",
            "--scope",
            "simplewithus",
            "--session-id",
            "mvs_issues_1",
        ]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = R.main(
                argv + ["--issues", '[{"id": "CT-7", "seen_at": "2026-11-10T15:00:00Z"}]'], env={}
            )
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(out.getvalue())["issues"],
            [{"id": "CT-7", "seen_at": "2026-11-10T15:00:00Z"}],
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = R.main(argv + ["--issues", "CT-7"], env={})
        self.assertEqual(code, 2)
        self.assertIn("must be a list", json.loads(out.getvalue())["error"])


if __name__ == "__main__":
    unittest.main()


class ReopenCommandTest(unittest.TestCase):
    """The reopen verb of the registry's own CLI.

    A host that keeps no registry asks the host that holds one, so the reopen has to
    be a verb the CLI answers, not a call into the registry from inside a hook.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "registry.sqlite3")
        self.reg = R.SessionRegistry(self.db)

    def tearDown(self):
        self.reg.close_db()
        self.tmp.cleanup()

    def test_the_reopen_command_gives_a_closed_chat_its_letter_back(self):
        made = self.reg.resolve_or_register(ident(platform="minimax", native="n-9"))
        self.reg.close(made["record_id"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = R.main(
                [
                    "reopen",
                    "--db",
                    self.db,
                    "--record-id",
                    made["record_id"],
                    "--actor",
                    "MM",
                    "--source",
                    "test",
                    "--letter-pick",
                    "oldest",
                ]
            )
        answer = json.loads(out.getvalue().splitlines()[-1])
        self.assertEqual(code, 0)
        self.assertEqual(answer["outcome"], "reopened")
        self.assertEqual(answer["label"], made["label"])
        self.assertIsNone(self.reg._get(made["record_id"])["closed_at"])

    def test_the_reopen_command_answers_a_refusal_as_json(self):
        # A record that is not there is a readable answer, never a traceback.
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = R.main(["reopen", "--db", self.db, "--record-id", "no-such-record"])
        answer = json.loads(out.getvalue().splitlines()[-1])
        self.assertEqual(code, 2)
        self.assertEqual(answer["code"], "reopen_failed")
        self.assertEqual(answer["outcome"], "reopen_refused")

    def test_the_reopen_command_never_brings_a_registry_into_being(self):
        # A host that keeps no registry must not grow one:  a second registry hands
        # out letters that collide with the ones the Operator keeps, so the CLI
        # refuses and the file is never created.
        missing = os.path.join(self.tmp.name, "no-registry-here", "registry.sqlite3")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = R.main(["reopen", "--db", missing, "--record-id", "no-such-record"])
        answer = json.loads(out.getvalue().splitlines()[-1])
        self.assertEqual(code, 3)
        self.assertEqual(answer, {"error": "no Operator registry on this host"})
        self.assertFalse(os.path.exists(missing))

    def test_the_reopen_command_answers_a_database_fault_as_json(self):
        # A database that faults is a refusal the caller can read, never a traceback:
        # the far host would answer with something unparseable and the hop would
        # report an unknown outcome instead of the refusal it owes.
        broken = os.path.join(self.tmp.name, "broken.sqlite3")
        with open(broken, "wb") as handle:
            handle.write(b"this is not a database")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = R.main(["reopen", "--db", broken, "--record-id", "no-such-record"])
        answer = json.loads(out.getvalue().splitlines()[-1])
        self.assertEqual(code, 2)
        self.assertEqual(answer["code"], "reopen_failed")
        self.assertEqual(answer["outcome"], "reopen_refused")


class SeatGuardTest(unittest.TestCase):
    """AFC-772:  a platform's chat is filed under one of that platform's seats.

    Three Claude Code chats registered as platform "claude" under the seat ZC, because their host
    exported ZCode's seat and the letter hook passed it on.  The registry took them, the wake router
    (which picks only rows of its own seat) skipped them, and nothing could move them.  Now the
    registry refuses the pair before it writes anything.  The pairs are fleet-apps.json `seatApps`
    (identity.platform_seats), so there is no second table here to keep equal.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = os.path.join(self.tmp.name, "registry.sqlite3")

    def register(self, platform, seat, native="native-1", **kw):
        return R.register_session(
            self.db,
            platform=platform,
            verified_host="mac",
            profile_scope="default",
            native_session_id=native,
            seat=seat,
            source="test",
            issuer=R.ISSUER_OPERATOR,
            **kw,
        )

    def relabel(self, platform, seat, native="native-1", **kw):
        return R.relabel_session(
            self.db,
            platform=platform,
            verified_host="mac",
            profile_scope="default",
            native_session_id=native,
            seat=seat,
            source="test",
            issuer=R.ISSUER_OPERATOR,
            **kw,
        )

    def legacy(self, platform, seat, native="native-1"):
        """A row as one was filed before the check existed:  the pair went in unquestioned."""
        with mock.patch.object(R, "check_seat_platform", lambda *_: None):
            return self.register(platform, seat, native)

    def sql(self, statement, *args):
        conn = sqlite3.connect(self.db)
        try:
            with conn:
                return conn.execute(statement, args).fetchall()
        finally:
            conn.close()

    def counts(self):
        return tuple(
            self.sql(f"SELECT COUNT(*) FROM {table}")[0][0]
            for table in ("sessions", "alias_assignments", "events", "export_outbox")
        )

    def where(self, platform="claude", native="native-1"):
        return {
            "platform": platform,
            "verified_host": "mac",
            "profile_scope": "default",
            "native_session_id": native,
        }

    # ---- register ----

    def test_a_claude_chat_under_zc_is_refused_and_nothing_is_written(self):
        with self.assertRaises(R.SeatPlatformMismatch) as caught:
            self.register("claude", "ZC")
        refusal = caught.exception
        self.assertIsInstance(
            refusal, ValueError
        )  # so every caller that catches ValueError still does
        self.assertEqual(
            refusal.answer(),
            {
                "error": "platform claude registers under seat CLAUDE, not ZC",
                "code": "seat_platform_mismatch",
                "platform": "claude",
                "seat": "ZC",
                "expected": ["CLAUDE"],
            },
        )
        self.assertFalse(os.path.exists(self.db), "refused before the store is opened")
        R.SessionRegistry(self.db).close_db()
        with self.assertRaises(R.SeatPlatformMismatch):
            self.register("claude", "ZC")
        self.assertEqual(self.counts(), (0, 0, 0, 0))

    def test_the_one_place_a_row_is_made_refuses_it_too(self):
        reg = R.SessionRegistry(self.db)
        self.addCleanup(reg.close_db)
        with self.assertRaises(R.SeatPlatformMismatch):
            reg.resolve_or_register(ident(seat="ZC", platform="claude"))
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.assertEqual(reg.resolve_or_register(ident(platform="claude"))["label"], "AA")

    def test_each_platforms_own_seat_is_accepted(self):
        for platform, seat in (
            ("claude", "CLAUDE"),
            ("zcode", "ZC"),
            ("codex", "CODEX"),
            ("cursor", "CURSOR"),
            ("grok-leader", "GROK"),
            ("minimax", "MM"),
            ("muse", "MC"),
            ("antigravity", "AG"),
            ("fx", "FX"),
            ("opencode", "OPENCODE"),
            ("clutch", "CLUTCH"),
        ):
            with self.subTest(platform=platform):
                out = self.register(platform, seat)
                self.assertEqual((out["outcome"], out["label"]), ("registered", "AA"))
                self.assertEqual(self.register(platform, seat)["outcome"], "resumed")

    def test_a_platform_the_table_does_not_know_registers_under_any_seat(self):
        # Letters are liberal:  only a known platform under another seat is refused.
        labels = [
            self.register("testbed", seat, native=f"native-{index}")["label"]
            for index, seat in enumerate(("ZC", "CLAUDE", "BF-FIXER", "SOMETHING-NEW"))
        ]
        self.assertEqual(labels, ["AA", "BB", "CC", "DD"])

    def test_a_launchers_bots_match_a_pattern_and_not_one_seat(self):
        self.assertEqual(self.register("botfleet", "BF-FIXER", "bf-1")["label"], "AA")
        self.assertEqual(self.register("botfleet", "bf-oracle", "bf-2")["label"], "BB")
        with self.assertRaises(R.SeatPlatformMismatch) as caught:
            self.register("botfleet", "CLAUDE", "bf-3")
        self.assertEqual(caught.exception.expected, ("BF-*",))
        self.assertEqual(self.register("grok-bot", "GB-COMPILER", "gb-1")["label"], "AA")
        self.assertEqual(self.register("grok-bot", "GROK-BOT", "gb-2")["label"], "BB")
        with self.assertRaises(R.SeatPlatformMismatch) as caught:
            self.register("grok-bot", "BF-COMPILER", "gb-3")
        self.assertEqual(caught.exception.expected, ("GROK-BOT", "GB-*"))
        self.assertEqual(self.sql("SELECT COUNT(*) FROM sessions")[0][0], 4)

    def test_case_aliases_and_adapter_names_do_not_get_around_it(self):
        for platform, seat, namespace in (
            ("Claude", "zc", "claude"),
            (" CLAUDE ", "Zc", "claude"),
            ("cursor-cloud", "ZC", "cursor"),
            ("cursor-local", "CLAUDE", "cursor"),
            ("grok", "ZC", "grok-leader"),
            ("GROK-BUILD", "MM", "grok-leader"),
            ("minimax", "MINIMAX", "minimax"),  # the tag is MM, and identity has no such alias
        ):
            with self.subTest(platform=platform, seat=seat):
                with self.assertRaises(R.SeatPlatformMismatch) as caught:
                    self.register(platform, seat)
                self.assertEqual(caught.exception.platform, namespace)
        self.assertFalse(os.path.exists(self.db))
        # ... and the same spellings of the right seat are that seat.
        for index, (platform, seat, namespace, tag) in enumerate(
            (
                ("Claude", "claude", "claude", "CLAUDE"),
                ("claude", " CLAUDE ", "claude", "CLAUDE"),
                ("cursor-cloud", "Cursor", "cursor", "CURSOR"),
                # The Grok leader is Grok Build, so its seat is GROK.
                ("grok-build", "Grok Build", "grok-leader", "GROK"),
                ("grok", "GROK-BUILD", "grok-leader", "GROK"),
            )
        ):
            with self.subTest(platform=platform, seat=seat):
                native = f"right-{index}"
                out = self.register(platform, seat, native=native)
                self.assertEqual(out["outcome"], "registered")
                # It is stored as the tag, not as typed:  a row filed as "Grok Build" could never be
                # resumed under GROK, and the wake router would not read it as a seat.
                self.assertEqual(
                    self.sql(
                        "SELECT platform, seat FROM sessions WHERE record_id = ?", out["record_id"]
                    ),
                    [(namespace, tag)],
                )
                self.assertEqual(
                    self.sql(
                        "SELECT DISTINCT actor FROM events WHERE session_record_id = ?",
                        out["record_id"],
                    ),
                    [(tag,)],
                )
                for spelling in (tag, seat):
                    again = self.register(namespace, spelling, native=native)
                    self.assertEqual(
                        (again["outcome"], again["record_id"]), ("resumed", out["record_id"])
                    )

    def test_a_platform_the_table_does_not_know_keeps_its_seat_as_given(self):
        made = self.register("testbed", "some-seat")
        self.assertEqual(self.sql("SELECT seat FROM sessions"), [("some-seat",)])
        self.assertEqual(self.register("testbed", "some-seat")["outcome"], "resumed")
        self.assertEqual(self.register("testbed", "SOME-SEAT")["outcome"], "seat_mismatch")
        self.assertEqual(self.sql("SELECT seat FROM sessions"), [("some-seat",)])
        self.assertEqual(made["label"], "AA")

    def test_what_is_not_a_seat_tag_is_not_repeated_back(self):
        with self.assertRaises(R.SeatPlatformMismatch) as caught:
            self.register("claude", "not a seat; rm -rf")
        self.assertEqual(caught.exception.seat, "")
        self.assertEqual(
            str(caught.exception), "platform claude registers under seat CLAUDE, not the seat given"
        )

    # ---- relabel ----

    def test_a_relabel_into_a_mismatch_is_refused(self):
        made = self.legacy("claude", "ZC")
        self.sql("UPDATE alias_assignments SET letter = 'A' WHERE record_id = ?", made["record_id"])
        before = self.counts()
        with self.assertRaises(R.SeatPlatformMismatch) as caught:
            self.relabel("claude", "ZC", letter_space="double")
        self.assertEqual(caught.exception.answer()["code"], "seat_platform_mismatch")
        # Naming the platform's own seat does not move another seat's row either (the rule from before).
        with self.assertRaises(ValueError) as other:
            self.relabel("claude", "CLAUDE", letter_space="double")
        self.assertNotIsInstance(other.exception, R.SeatPlatformMismatch)
        self.assertIn("belongs to seat ZC", str(other.exception))
        self.assertEqual(self.counts(), before)
        live = self.sql("SELECT letter FROM alias_assignments WHERE retired_at IS NULL")
        self.assertEqual([row[0] for row in live], ["A"])
        # A row under its platform's own seat still moves onto a pair.
        good = self.register("claude", "CLAUDE", native="native-2")
        self.sql("UPDATE alias_assignments SET letter = 'B' WHERE record_id = ?", good["record_id"])
        self.assertEqual(
            self.relabel("claude", "CLAUDE", native="native-2")["outcome"], "relabelled"
        )

    def test_a_relabel_names_the_seat_in_any_spelling_the_check_accepts(self):
        # The seat is stored as the tag, so the relabel compares the tag:  "claude" is CLAUDE's row.
        for index, (platform, seat, tag) in enumerate(
            (("claude", "claude", "CLAUDE"), ("grok", "Grok Build", "GROK"))
        ):
            with self.subTest(platform=platform, seat=seat):
                native = f"spelled-{index}"
                made = self.register(platform, tag, native=native)
                self.sql(
                    "UPDATE alias_assignments SET letter = ? WHERE record_id = ?",
                    "CD"[index],
                    made["record_id"],
                )
                moved = self.relabel(platform, seat, native=native)
                self.assertEqual(moved["outcome"], "relabelled")
                actors = self.sql(
                    "SELECT DISTINCT actor FROM events WHERE session_record_id = ?",
                    made["record_id"],
                )
                self.assertEqual(actors, [(tag,)])

    # ---- a row filed under a wrong seat before the check existed ----

    def test_a_row_filed_under_a_wrong_seat_earlier_is_still_seen_updated_and_ended(self):
        made = self.legacy("claude", "ZC")
        where = self.where()
        self.assertEqual(R.touch_session(self.db, **where)["outcome"], "seen")
        self.assertEqual(
            R.update_session(self.db, title="Still Here", **where)["outcome"], "updated"
        )
        found = R.resolve_label(self.db, "claude", made["label"])
        self.assertEqual((found["seat"], found["record_id"]), ("ZC", made["record_id"]))
        # (A register under the right seat would heal it:  see the reseat tests below.)
        ended = R.close_session(self.db, reason="session ended", **where)
        self.assertEqual((ended["closed"], ended["label"]), (True, made["label"]))
        self.assertEqual(R.touch_session(self.db, **where)["outcome"], "closed")
        self.assertIsNone(R.resolve_label(self.db, "claude", made["label"]))

    def test_a_live_row_filed_under_a_wrong_seat_earlier_is_still_resumed_until_it_is_closed(self):
        # A refused resume left the chat's hook with a failed note, and a failed note never closes
        # its row:  the letter stayed live with nothing left to end it.  So the resume of a live row
        # under the seat it is stored under goes through, as it did before the check.
        made = self.legacy("claude", "ZC")
        before = self.sql("SELECT last_seen_at FROM sessions")[0][0]
        self.sql("UPDATE sessions SET last_seen_at = '2026-01-01T00:00:00.000Z'")
        for spelling in ("ZC", "zc"):
            again = self.register("claude", spelling)
            self.assertEqual(
                (again["outcome"], again["record_id"], again["label"]),
                ("resumed", made["record_id"], made["label"]),
            )
        self.assertGreaterEqual(self.sql("SELECT last_seen_at FROM sessions")[0][0], before)
        self.assertEqual(self.sql("SELECT seat FROM sessions"), [("ZC",)])
        # Only that row, and only under that seat:  another wrong seat, and a new chat under the
        # same wrong seat, are refused as before, with nothing written.
        counts = self.counts()
        with self.assertRaises(R.SeatPlatformMismatch):
            self.register("claude", "MM")
        with self.assertRaises(R.SeatPlatformMismatch):
            self.register("claude", "ZC", native="native-2")
        self.assertEqual(self.counts(), counts)
        # Closed, it is not brought back under the wrong seat (a resume of a closed row leads to a
        # reopen, and with it a new letter under ZC).
        R.close_session(self.db, reason="session ended", **self.where())
        counts = self.counts()
        with self.assertRaises(R.SeatPlatformMismatch):
            self.register("claude", "ZC")
        self.assertEqual(self.counts(), counts)
        self.assertEqual(self.sql("SELECT state FROM sessions"), [("closed",)])

    def test_the_stale_sweep_still_ends_a_row_filed_under_a_wrong_seat(self):
        old = self.legacy("claude", "ZC", "old-1")
        self.sql(
            "UPDATE sessions SET last_seen_at = '2026-01-01T00:00:00.000Z' WHERE record_id = ?",
            old["record_id"],
        )
        self.legacy("claude", "ZC", "new-1")
        ended = R.sweep_stale(self.db, platform="claude", seat="ZC", stale_after_minutes=120)
        self.assertEqual([row["record_id"] for row in ended], [old["record_id"]])

    # ---- the heal:  a register under the platform's own seat re-seats a row filed under another ----

    def reseat_events(self):
        return self.sql(
            "SELECT event, actor, outcome, details, label FROM events WHERE event = 'reseat'"
        )

    def test_a_register_under_the_platforms_own_seat_heals_a_row_filed_under_another(self):
        # The shape of the three live rows:  platform claude, seat ZC, open, host mac.
        made = self.legacy("claude", "ZC")
        self.assertEqual(
            self.sql("SELECT seat, state, verified_host FROM sessions"), [("ZC", "active", "mac")]
        )
        before = self.sql(
            "SELECT record_id, registered_at, closed_at, state, platform FROM sessions"
        )
        events = len(self.sql("SELECT 1 FROM events"))
        again = self.register("claude", "CLAUDE")
        self.assertEqual(
            (
                again["outcome"],
                again["record_id"],
                again["label"],
                again["generation"],
                again["created"],
            ),
            ("resumed", made["record_id"], made["label"], made["generation"], False),
        )
        # The seat is the one thing that changed, and the record, letter and state are the same row's.
        self.assertEqual(self.sql("SELECT seat FROM sessions"), [("CLAUDE",)])
        self.assertEqual(
            self.sql("SELECT record_id, registered_at, closed_at, state, platform FROM sessions"),
            before,
        )
        self.assertEqual(R.resolve_label(self.db, "claude", made["label"])["seat"], "CLAUDE")
        # One audit event of kind reseat, naming the old and the new seat, then the usual resume.
        self.assertEqual(
            self.reseat_events(),
            [("reseat", "CLAUDE", "reseated", "seat ZC to CLAUDE", made["label"])],
        )
        self.assertEqual(len(self.sql("SELECT 1 FROM events")), events + 2)
        # ... and the outbox row for the changed row carries the new seat, with the event's own.
        outbox = [
            (kind, json.loads(payload)["cells"])
            for kind, payload in self.sql(
                "SELECT kind, payload FROM export_outbox ORDER BY created_at, rowid"
            )
        ]
        upserts = [cells for kind, cells in outbox if kind == "session_upsert"]
        self.assertIn("CLAUDE", upserts[-1])
        self.assertNotIn("ZC", upserts[-1])
        self.assertTrue(any(kind == "event_append" and "reseat" in cells for kind, cells in outbox))
        # Healed once:  the next register is a plain resume.
        self.assertEqual(self.register("claude", "CLAUDE")["outcome"], "resumed")
        self.assertEqual(len(self.reseat_events()), 1)

    def test_the_heal_reads_the_seat_the_caller_sends_in_any_spelling_the_check_accepts(self):
        made = self.legacy("claude", "ZC")
        again = self.register("Claude", " claude ")
        self.assertEqual((again["outcome"], again["record_id"]), ("resumed", made["record_id"]))
        self.assertEqual(self.sql("SELECT seat FROM sessions"), [("CLAUDE",)])

    def test_the_heal_never_goes_the_other_way(self):
        # The row has the right seat and the caller sends a wrong one:  still refused, nothing written.
        self.register("claude", "CLAUDE")
        counts = self.counts()
        with self.assertRaises(R.SeatPlatformMismatch):
            self.register("claude", "ZC")
        self.assertEqual(self.counts(), counts)
        self.assertEqual(self.sql("SELECT seat FROM sessions"), [("CLAUDE",)])
        self.assertEqual(self.reseat_events(), [])
        # A wrong seat against a wrongly filed live row is not a heal either:  it is the resume the
        # earlier test describes, and the stored seat stays.
        self.legacy("claude", "ZC", "native-2")
        self.assertEqual(self.register("claude", "ZC", native="native-2")["outcome"], "resumed")
        self.assertEqual(
            self.sql("SELECT seat FROM sessions WHERE native_session_id = 'native-2'"), [("ZC",)]
        )
        self.assertEqual(self.reseat_events(), [])

    def test_a_platform_the_table_does_not_know_is_not_healed(self):
        self.register("testbed", "some-seat")
        counts = self.counts()
        out = self.register("testbed", "other-seat")
        self.assertEqual((out["outcome"], out["label"]), ("seat_mismatch", "AA"))
        self.assertEqual(self.sql("SELECT seat FROM sessions"), [("some-seat",)])
        self.assertEqual(self.reseat_events(), [])
        # What it always wrote for a mismatch (one resolve event), and no row changed.
        self.assertEqual(self.counts()[:2], counts[:2])
        self.assertEqual(
            self.sql("SELECT outcome FROM events WHERE event = 'resolve'"), [("seat_mismatch",)]
        )

    def test_a_platform_with_no_single_seat_is_not_healed(self):
        # A launcher's bots each have a seat (a role pattern), and instinct is two tags:  there is no one
        # right seat to move a row to, so the seat_mismatch answer stands.
        for platform, first, second in (
            ("botfleet", "BF-FIXER", "BF-ORACLE"),
            ("grok-bot", "GB-FIXER", "GB-ORACLE"),
            ("instinct", "ECHO", "WISE"),
        ):
            with self.subTest(platform=platform):
                native = f"native-{platform}"
                self.register(platform, first, native=native)
                out = self.register(platform, second, native=native)
                self.assertEqual(out["outcome"], "seat_mismatch")
                self.assertEqual(
                    self.sql("SELECT seat FROM sessions WHERE native_session_id = ?", native),
                    [(first,)],
                )
        self.assertEqual(self.reseat_events(), [])

    def test_a_closed_row_filed_under_a_wrong_seat_is_reseated_and_left_closed(self):
        made = self.legacy("claude", "ZC")
        R.close_session(self.db, reason="session ended", **self.where())
        closed_at = self.sql("SELECT closed_at FROM sessions")[0][0]
        out = self.register("claude", "CLAUDE")
        # The same answer a closed row of the right seat gets, with no letter, and it is not reopened.
        self.assertEqual(
            (out["outcome"], out["record_id"], out["label"], out["created"]),
            ("resumed_closed", made["record_id"], "", False),
        )
        self.assertEqual(
            self.sql("SELECT seat, state, closed_at FROM sessions"),
            [("CLAUDE", "closed", closed_at)],
        )
        self.assertEqual(len(self.reseat_events()), 1)
        self.assertEqual(
            self.sql("SELECT COUNT(*) FROM alias_assignments WHERE retired_at IS NULL"), [(0,)]
        )

    def test_two_rows_are_healed_one_after_the_other(self):
        first = self.legacy("claude", "ZC", "native-1")
        second = self.legacy("claude", "ZC", "native-2")
        a = self.register("claude", "CLAUDE", native="native-1")
        b = self.register("claude", "CLAUDE", native="native-2")
        self.assertEqual(
            [(a["record_id"], a["label"]), (b["record_id"], b["label"])],
            [(first["record_id"], first["label"]), (second["record_id"], second["label"])],
        )
        self.assertNotEqual(a["label"], b["label"])
        self.assertEqual(self.sql("SELECT DISTINCT seat FROM sessions"), [("CLAUDE",)])
        self.assertEqual(len(self.reseat_events()), 2)

    def test_the_heal_is_atomic(self):
        made = self.legacy("claude", "ZC")
        counts = self.counts()
        failing = mock.patch.object(
            R.SessionRegistry,
            "_enqueue_session_upsert",
            side_effect=RuntimeError("after the reseat"),
        )
        with failing, self.assertRaises(RuntimeError):
            self.register("claude", "CLAUDE")
        self.assertEqual(self.sql("SELECT seat FROM sessions"), [("ZC",)])
        self.assertEqual(self.counts(), counts)
        self.assertEqual(self.reseat_events(), [])
        # Nothing half-done is left in the way:  the next register heals it.
        self.assertEqual(self.register("claude", "CLAUDE")["record_id"], made["record_id"])
        self.assertEqual(self.sql("SELECT seat FROM sessions"), [("CLAUDE",)])

    def test_no_heal_while_the_table_cannot_be_read(self):
        self.legacy("claude", "ZC")
        missing = os.path.join(self.tmp.name, "no-such", "fleet-apps.json")
        with mock.patch.dict(os.environ, {"FLEET_APPS_JSON": missing}):
            self.assertEqual(R.fixed_seat("claude"), "")
            self.assertEqual(self.register("claude", "CLAUDE")["outcome"], "seat_mismatch")
        self.assertEqual(self.sql("SELECT seat FROM sessions"), [("ZC",)])
        self.assertEqual(self.register("claude", "CLAUDE")["outcome"], "resumed")

    def test_a_cursor_row_under_another_seat_is_healed_through_either_adapter(self):
        # cursor-cloud and cursor-local fold to the one namespace "cursor", whose only seat is CURSOR (the
        # Cursor Cloud registrar registers platform cursor, seat CURSOR).
        for adapter in ("cursor", "cursor-cloud", "cursor-local"):
            self.assertEqual(R.normalize_platform(adapter), "cursor")
            self.assertEqual(R.fixed_seat(adapter), "CURSOR")
        made = self.legacy("cursor-cloud", "MM")
        self.assertEqual(self.sql("SELECT platform, seat FROM sessions"), [("cursor", "MM")])
        again = self.register("cursor-local", "CURSOR")
        self.assertEqual((again["outcome"], again["record_id"]), ("resumed", made["record_id"]))
        self.assertEqual(self.sql("SELECT platform, seat FROM sessions"), [("cursor", "CURSOR")])
        self.assertEqual(self.reseat_events()[0][3], "seat MM to CURSOR")

    def test_the_fixed_seat_of_each_platform(self):
        for platform, seat in (
            ("claude", "CLAUDE"),
            ("Claude", "CLAUDE"),
            ("zcode", "ZC"),
            ("grok", "GROK"),
            ("grok-leader", "GROK"),
            ("minimax", "MM"),
            ("botfleet", ""),
            ("grok-bot", ""),
            ("instinct", ""),
            ("testbed", ""),
            ("", ""),
        ):
            with self.subTest(platform=platform):
                self.assertEqual(R.fixed_seat(platform), seat)
        self.assertEqual(R.fixed_seat(None), "")

    # ---- the table ----

    def test_the_check_is_off_and_says_so_while_the_table_cannot_be_read(self):
        missing = os.path.join(self.tmp.name, "no-such", "fleet-apps.json")
        self.assertEqual(R.seat_table_problem(), "")
        with mock.patch.dict(os.environ, {"FLEET_APPS_JSON": missing}):
            self.assertIn("cannot read", R.seat_table_problem())
            # Liberal:  a letter is still issued, for any pair, rather than none for every platform.
            self.assertEqual(self.register("claude", "ZC")["outcome"], "registered")
        self.assertEqual(R.seat_table_problem(), "")
        with self.assertRaises(R.SeatPlatformMismatch):
            self.register("claude", "ZC", native="native-2")

    def test_every_letter_client_and_the_router_name_a_pair_the_registry_takes(self):
        # The registry holds no copy of these:  each client says its own platform and seat, the
        # router's table says its own, and all of them have to be pairs fleet-apps.json agrees with,
        # or a platform's own chats would be refused a letter.
        import importlib
        import pkgutil

        import mm_session_letter
        import simple_tools
        from simple_tools import claude_letter, claude_wake, wake_service
        from simple_tools import identity as I

        pairs = {
            ("claude_letter", claude_letter.PLATFORM, claude_letter.SEAT_DEFAULT),
            ("claude_wake", claude_wake.PLATFORM, claude_wake.SEAT),
            ("mm_session_letter", mm_session_letter.PLATFORM, mm_session_letter.SEAT),
        }
        for seat, builder in wake_service.BUILDERS.items():
            pairs.add(("wake_service.BUILDERS", builder.platform, seat))
        for info in pkgutil.iter_modules(simple_tools.__path__):
            if info.name.startswith("platform_letter_"):
                spec = importlib.import_module("simple_tools." + info.name).SPEC
                if isinstance(spec.seat, str):  # a launcher's spec reads each bot's seat instead
                    pairs.add((info.name, spec.platform, spec.seat))
        self.assertGreaterEqual(len({platform for _source, platform, _seat in pairs}), 11)
        for source, platform, seat in sorted(pairs):
            with self.subTest(source=source, platform=platform, seat=seat):
                R.check_seat_platform(platform, seat)  # raises when the registry would refuse it
                owners = I.platform_seats(R.normalize_platform(platform))
                self.assertIsNotNone(
                    owners,
                    f"the registry cannot check {platform}:  give its seat a row in fleet-apps.json"
                    " seatApps (and identity.PLATFORM_APPS when the namespace is not the app's name)",
                )
                self.assertEqual(owners.seats, (seat,))

    # ---- the CLI ----

    def cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = R.main(list(argv), env={})
        return code, json.loads(out.getvalue().splitlines()[-1])

    def test_the_cli_answers_the_refusal_as_json_with_its_code(self):
        R.SessionRegistry(self.db).close_db()
        identity = [
            "--db",
            self.db,
            "--platform",
            "claude",
            "--location",
            "mac",
            "--scope",
            "default",
        ]
        identity += ["--session-id", "native-1", "--source", "test"]
        code, answer = self.cli("register", *identity, "--seat", "ZC")
        self.assertEqual(code, 2)
        self.assertEqual(
            (answer["code"], answer["platform"], answer["seat"], answer["expected"]),
            ("seat_platform_mismatch", "claude", "ZC", ["CLAUDE"]),
        )
        self.assertEqual(self.counts(), (0, 0, 0, 0))
        self.legacy("claude", "ZC")
        code, answer = self.cli("relabel", *identity, "--seat", "ZC")
        self.assertEqual((code, answer["code"]), (2, "seat_platform_mismatch"))
        code, answer = self.cli(
            "register", *identity, "--seat", "CLAUDE", "--session-id", "native-2"
        )
        self.assertEqual((code, answer["outcome"]), (0, "registered"))
