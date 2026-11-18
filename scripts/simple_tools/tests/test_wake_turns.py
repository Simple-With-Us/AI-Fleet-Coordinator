"""Turns (AFC-831):  one idle turn of a chat may carry several of its pending wakes.

The registry is the fake from test_wake_router, a platform is a fake that records what it was
handed, and the Mac is `wake-deliver` itself, run in this process.  No test reaches a live chat,
the live registry or a real ssh.  A few tests pin today's behaviour (a platform that does not opt
in, the receipt of a wake outside a turn, the wire without a turn) and pass without the change;
the rest need it.  New names of the router are only ever reached inside a test's body, so this
module imports on a router from before turns.
"""

from __future__ import annotations

import importlib
import io
import itertools
import json
import re
import subprocess
import sys
import threading
import time
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from simple_tools import claude_wake as CW
from simple_tools import wake_router as WR
from simple_tools import wake_service as SVC
from simple_tools.tests.test_wake_router import (
    BODY_MARK,
    SID_A,
    SID_B,
    Base,
    Killed,
    RemoteBase,
    row,
)

LETTER_NOTE = WR.LINK_NOTES["letter"]
#: The contract name as text, for the tests that must also pass on a router from before turns.
CONTRACT_TEXT = "simple-tools-turn/1"
TURN_ID = "t-0123456789abcdef"
#: The keys of a receipt before turns (wake_router.Router._receipt):  a wake outside a turn
#: keeps exactly these 28.
RECEIPT_KEYS = frozenset(
    {
        "receipt_id",
        "wake_id",
        "seat",
        "at",
        "test",
        "status",
        "letter",
        "letter_live",
        "letter_other_seat",
        "picked",
        "picked_letter",
        "picked_generation",
        "session_id",
        "host",
        "thread_link",
        "reason",
        "candidates",
        "link_note",
        "delivered",
        "route",
        "ack",
        "code",
        "note",
        "attempts",
        "replayed",
        "channel",
        "topic",
        "zulip_link",
    }
)
#: The listener's receipt reader (AFC-824, scripts/simple_tools/wakes.py), copied as data and
#: never imported from that lane:  the words it reads as terminal and the shape of a code.
LISTENER_TERMINAL = frozenset({"delivered", "dead", "held", "refused", "unknown"})
LISTENER_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")
LISTENER_WORDS = ("code", "route", "ack", "host", "reason")
#: What a pick with each reason says it was picked by, as Router.pick writes it.
PICKED = {
    "sole-live-chat": "most-recent",
    "consultant-pick": "consultant",
    "linear-issue-tie": "linear-issue",
    "topic-tie": "topic-match",
}
UNCERTAIN = (
    "sole-live-chat",
    "most-recent",
    "consultant-pick",
    "linear-issue",
    "linear-issue-tie",
    "topic-match",
    "topic-tie",
)
#: The kinds in the live fixture:  L letter, R reply, S session id, U sole live chat.
KINDS = {"L": "letter", "R": "reply", "S": "session-id", "U": "sole-live-chat"}
TURN_MODULE = "simple_tools.tests.no_such_turn_wake"  # only ever the fake put in sys.modules


def load_tests(loader, standard_tests, pattern):
    """Only the test classes this module defines:  the bases it imports run in their own."""
    suite = unittest.TestSuite()
    for value in list(globals().values()):
        if (
            isinstance(value, type)
            and issubclass(value, unittest.TestCase)
            and value.__module__ == __name__
        ):
            suite.addTests(loader.loadTestsFromTestCase(value))
    return suite


class TurnBase(Base):
    """A router for one seat whose fake platform records each call:  ("D", wake id) for one
    wake, ("T", head id, (rider ids)) for a turn."""

    def setUp(self) -> None:
        super().setUp()
        self.registry.rows = [row(SID_A, "AA", "letters"), row(SID_B, "BB", "other")]
        self.calls: list[tuple] = []
        self.sources: list[int] = []
        self.turn_args: list[tuple] = []
        self.answers: list = []  # what each turn answers, in order;  a callable is called
        self.inside = None  # called inside each turn with (head, riders, turn_id)
        self.counter = itertools.count(1)

    @property
    def target_a(self) -> WR.Target:
        return WR.target_from_row(row(SID_A, "AA", "letters"), "CLAUDE", CW.SESSION_RE)

    def deliver_one(self, target: WR.Target, wake: WR.Wake) -> WR.Delivery:
        self.calls.append(("D", wake.wake_id))
        self.sources.append(wake.message_id)
        if self.results:
            return self.results.pop(0)
        return WR.Delivery("completed", "fake", ack="confirmed")

    def deliver_turn(self, target, head, riders, turn_id):
        self.calls.append(("T", head.wake_id, tuple(r.wake_id for r in riders)))
        self.sources.extend([head.message_id, *(r.message_id for r in riders)])
        self.turn_args.append((target, head, tuple(riders), turn_id))
        if self.inside is not None:
            self.inside(head, riders, turn_id)
        if self.answers:
            answer = self.answers.pop(0)
            return answer(head, riders, turn_id) if callable(answer) else answer
        carried = tuple(r.wake_id for r in riders)
        return WR.Turn(WR.Delivery("completed", "fake", ack="confirmed"), carried)

    def turns(self, deliver_turn=None, state: Path | None = None, **config) -> WR.Router:
        """A router whose platform declares turns, with the knob at 8 unless `config` says."""
        platform = WR.Platform(
            seat="CLAUDE",
            platform="claude",
            mention="Claude",
            target_re=CW.SESSION_RE,
            deliver=self.deliver_one,
            deliver_turn=deliver_turn if deliver_turn is not None else self.deliver_turn,
        )
        return WR.Router(
            platform,
            self.router_env(),
            self.config(**{"turn_max_wakes": 8, **config}),
            state or self.state,
            self.key_source,
            clock=self.clock,
        )

    def plain(self, deliver=None, **config) -> WR.Router:
        """A router whose platform has no deliver_turn, as every seat's but Muse Code's."""
        platform = WR.Platform(
            seat="CLAUDE",
            platform="claude",
            mention="Claude",
            target_re=CW.SESSION_RE,
            deliver=deliver or self.deliver_one,
        )
        return WR.Router(
            platform,
            self.router_env(),
            self.config(**config),
            self.state,
            self.key_source,
            clock=self.clock,
        )

    def fresh(self) -> None:
        """A state folder of its own and no calls yet, for one subtest."""
        self.state = Path(self.tmp) / ("s-%d" % next(self.counter))
        self.state.mkdir(mode=0o700)
        self.calls.clear()
        self.sources.clear()
        self.turn_args.clear()
        self.answers = []
        self.inside = None

    def letters(self, router, *ids, letter="AA", start=1001, **over) -> None:
        """Accept these wakes in this order, one second apart."""
        for offset, wake_id in enumerate(ids):
            self.clock.now += 1
            wake = self.wake(wake_id, letter=letter, message_id=start + offset, **over)
            status, _answer = router.accept(wake)
            self.assertEqual(status, 202)

    def prime(self, router, only=None) -> None:
        """Pick every pending wake now, as the workers do while an earlier one holds the chat."""
        for entry in list(router._entries.values()):
            if entry["pick"] is None and (only is None or entry["wake"]["wake_id"] in only):
                entry["pick"] = asdict(router.pick(WR.Wake(**entry["wake"])))
                router._save(entry)

    def plant(self, router, wake_id: str, reason: str, target=None, **over) -> None:
        """A pick with this reason, as the router would have written it, or changed by `over`."""
        pick = WR.Pick(
            target=target or self.target_a,
            picked=PICKED.get(reason, reason),
            reason=reason,
            letter_live=True,
            link_note=WR.LINK_NOTES[reason].format(issue="AFC-831"),
        )
        entry = router._entries[wake_id]
        entry["pick"] = asdict(replace(pick, **over))
        router._save(entry)

    def kept(self, wake_id: str) -> dict:
        return json.loads(self.pending(wake_id).read_text(encoding="utf-8"))

    def flaky(self, armed: dict, error: BaseException | None = None):
        """WR.atomic_write, failing once for each pending file named in `armed`."""
        real = WR.atomic_write

        def write(path, obj):
            stem = path.name[: -len(".json")] if path.parent.name == "pending" else ""
            if armed.get(stem):
                armed[stem] -= 1
                raise error or OSError("disk full")
            return real(path, obj)

        return mock.patch.object(WR, "atomic_write", write)

    def ledger(self) -> list[dict]:
        path = self.state / "wakes.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def assertClean(self, router, wake_id: str) -> None:
        """A wake that is back in pending with nothing used and no mark of a turn."""
        kept = self.kept(wake_id)
        self.assertEqual((kept["attempts"], kept["failures"]), (0, 0), wake_id)
        self.assertFalse({"in_flight", "ride", "turn"} & set(kept), wake_id)
        self.assertFalse(router._entries[wake_id].get("running"), wake_id)


class Defaults(TurnBase):
    def test_a_platform_without_deliver_turn_delivers_one_wake_per_try(self) -> None:
        files: list[dict] = []

        def deliver(target, wake):
            folder = self.state / "pending"
            files.extend(json.loads(p.read_text(encoding="utf-8")) for p in folder.glob("*.json"))
            return self.deliver_one(target, wake)

        router = self.plain(deliver=deliver, turn_max_wakes=8)
        self.letters(router, "w-1", "w-2", "w-3")
        self.prime(router)
        self.drain(router)
        self.assertEqual(self.calls, [("D", "w-1"), ("D", "w-2"), ("D", "w-3")])
        self.assertTrue(files)
        for kept in files:
            self.assertFalse({"turn", "ride"} & set(kept))
        for wake_id in ("w-1", "w-2", "w-3"):
            record = self.done(wake_id)
            self.assertEqual(record["attempts"], 1)
            self.assertEqual(set(record), RECEIPT_KEYS)

    def test_knob_default_keeps_an_opted_in_platform_on_todays_path(self) -> None:
        router = self.turns()
        router.config = self.config()  # ROUTER_DEFAULTS, with delivery on
        self.letters(router, "w-1", "w-2", "w-3")
        self.prime(router)
        self.drain(router)
        self.assertEqual(self.calls, [("D", "w-1"), ("D", "w-2"), ("D", "w-3")])
        self.assertEqual(self.turn_args, [])

    def test_router_defaults_turn_max_wakes_is_one(self) -> None:
        self.assertEqual(WR.ROUTER_DEFAULTS["turn_max_wakes"], 1)
        self.assertEqual((WR.TURN_MAX_WAKES, WR.TURN_MAX_PROMPT_CHARS), (8, 3500))

    def test_command_platform_still_builds_positionally(self) -> None:
        platform = WR.command_platform(
            "CLAUDE", "claude", "Claude", CW.SESSION_RE, ["/bin/true", "{session_id}"]
        )
        self.assertIsInstance(platform, WR.Platform)
        self.assertEqual(
            (platform.seat, platform.platform, platform.mention), ("CLAUDE", "claude", "Claude")
        )
        self.assertIsNone(getattr(platform, "deliver_turn", None))

    def test_a_non_callable_deliver_turn_or_a_non_int_knob_runs_todays_path(self) -> None:
        cases = {
            "deliver_turn is a string": ("x", 8),
            "the knob is a string": (None, "8"),
            "the knob is a bool": (None, True),
            "the knob is zero": (None, 0),
        }
        for label, (deliver_turn, knob) in cases.items():
            with self.subTest(label):
                self.fresh()
                platform = WR.Platform(
                    seat="CLAUDE",
                    platform="claude",
                    mention="Claude",
                    target_re=CW.SESSION_RE,
                    deliver=self.deliver_one,
                    deliver_turn=deliver_turn if deliver_turn is not None else self.deliver_turn,
                )
                router = WR.Router(
                    platform,
                    self.router_env(),
                    {**self.config(), "turn_max_wakes": knob},
                    self.state,
                    self.key_source,
                    clock=self.clock,
                )
                self.letters(router, "w-1", "w-2")
                self.prime(router)
                self.drain(router)
                self.assertEqual(self.calls, [("D", "w-1"), ("D", "w-2")])

    def test_uncertain_picks_still_go_one_by_one_when_armed(self) -> None:
        router = self.turns()
        self.letters(router, "w-1", "w-2", "w-3", letter="")
        for wake_id in ("w-1", "w-2", "w-3"):
            self.plant(router, wake_id, "sole-live-chat")
        self.drain(router)
        self.assertEqual(self.calls, [("D", "w-1"), ("D", "w-2"), ("D", "w-3")])
        self.assertEqual(self.turn_args, [])

    def test_an_interrupt_outside_a_turn_is_still_dead(self) -> None:
        def killed(target, wake):
            raise Killed()

        router = self.plain(deliver=killed)
        router.accept(self.wake("w-1", letter="AA", message_id=1001))
        with self.assertRaises(Killed):
            router.run_once()
        again = self.plain()
        again.replay()
        self.drain(again)
        record = self.done("w-1")
        self.assertEqual(
            (record["status"], record["delivered"], record["code"]), ("dead", None, WR.INTERRUPTED)
        )
        self.assertNotIn("turn", record)
        self.assertEqual(self.calls, [])

    def test_a_busy_answer_adds_no_mark_where_turns_are_off(self) -> None:
        for label in ("no deliver_turn", "the knob at its default"):
            with self.subTest(label):
                self.fresh()
                if label == "no deliver_turn":
                    router = self.plain(turn_max_wakes=8)
                else:
                    router = self.turns()
                    router.config = self.config()
                self.results = [WR.Delivery("busy", code="chat-live")]
                self.letters(router, "w-1")
                self.assertTrue(router.run_once())
                kept = self.kept("w-1")
                self.assertEqual(kept["attempts"], 1)
                self.assertFalse({"busy_seen", "turn", "ride", "in_flight"} & set(kept))


class Gather(TurnBase):
    def test_certain_same_target_wakes_share_one_turn(self) -> None:
        router = self.turns()
        self.letters(router, "w-1", "w-2", "w-3", "w-4")
        self.prime(router)
        self.drain(router)
        self.assertEqual(self.calls, [("T", "w-1", ("w-2", "w-3", "w-4"))])
        turn_id = self.turn_args[0][3]
        records = {w: self.done(w) for w in ("w-1", "w-2", "w-3", "w-4")}
        self.assertEqual(len({r["receipt_id"] for r in records.values()}), 4)
        for wake_id, record in records.items():
            self.assertEqual(record["wake_id"], wake_id)
            self.assertEqual((record["status"], record["attempts"]), ("delivered", 1))
        self.assertEqual(
            records["w-1"]["turn"],
            {
                "id": turn_id,
                "role": "head",
                "members": ["w-1", "w-2", "w-3", "w-4"],
                "message_ids": [1001, 1002, 1003, 1004],
                "carried": ["w-2", "w-3", "w-4"],
            },
        )
        for index, wake_id in enumerate(("w-2", "w-3", "w-4"), start=1):
            self.assertEqual(
                records[wake_id]["turn"],
                {"id": turn_id, "role": "rider", "head": "w-1", "index": index},
            )

    def test_turn_text_is_pointer_only_and_bounded(self) -> None:
        router = self.turns()
        for n in range(1, 6):
            self.clock.now += 1
            router.accept(
                self.wake(
                    "w-%d" % n,
                    letter="AA",
                    message_id=7000000 + 11 * n,
                    topic="OPS-1 " + BODY_MARK,
                    sender="Sender " + BODY_MARK,
                    channel="chan-" + BODY_MARK,
                    zulip_link="https://zulip.example/#" + BODY_MARK,
                )
            )
        self.prime(router)
        self.drain(router)
        _target, head, riders, turn_id = self.turn_args[0]
        text = WR.turn_text("[CLAUDE wake] ", head, riders, turn_id)
        members = [head, *riders]
        self.assertEqual(len(members), 5)
        self.assertEqual(re.findall(r"wake id (\S+):", text), [w.wake_id for w in members])
        self.assertEqual(
            re.findall(r"message_id (\d+)\.", text), [str(w.message_id) for w in members]
        )
        for wake in members:
            self.assertEqual(text.count(str(wake.message_id)), 1)
        for word in (BODY_MARK, "OPS-1", "Sender", "chan-", "zulip.example"):
            self.assertNotIn(word, text)
        self.assertIn("not quoted here on purpose", text)
        self.assertIn(turn_id, text)
        self.assertLessEqual(len(text), WR.TURN_MAX_PROMPT_CHARS)

    def test_exact_target_required(self) -> None:
        cases = {
            "another generation": WR.Target(SID_A, "AA", "server", "letters", "", 2),
            "another letter": WR.Target(SID_A, "AB", "server", "letters", "", 1),
            "another host": WR.Target(SID_A, "AA", "mac", "letters", "", 1),
            "another session": WR.Target(SID_B, "BB", "server", "other", "", 1),
        }
        for label, target in cases.items():
            with self.subTest(label):
                self.fresh()
                self.registry.rows = [row(SID_A, "AA", "letters"), row(SID_B, "BB", "other")]
                self.registry.letters = {"AB": row(SID_A, "AB", "letters")}
                router = self.turns()
                self.letters(router, "w-1", "w-2", "x-3")
                self.prime(router, only={"w-1", "w-2"})
                self.plant(router, "x-3", "letter", target=target)
                self.assertTrue(router.run_once())
                self.assertEqual(self.calls, [("T", "w-1", ("w-2",))])
                if target.generation == 2:  # the letter moves on;  its new holder is x-3's
                    self.registry.rows[0]["generation"] = 2
                self.drain(router)
                self.assertEqual(self.calls, [("T", "w-1", ("w-2",)), ("D", "x-3")])
                self.assertEqual(self.done("x-3")["status"], "delivered")

    def test_title_or_thread_link_differences_do_not_split_a_turn(self) -> None:
        router = self.turns()
        self.letters(router, "w-1", "x-2", "w-3")
        self.prime(router, only={"w-1", "w-3"})
        other = replace(self.target_a, title="another title", thread_link="https://zulip.example/1")
        self.plant(router, "x-2", "letter", target=other)
        self.drain(router)
        self.assertEqual(self.calls, [("T", "w-1", ("x-2", "w-3"))])

    def test_uncertain_and_inferred_reasons_never_ride(self) -> None:
        for reason in UNCERTAIN:
            with self.subTest(reason):
                self.fresh()
                router = self.turns()
                self.letters(router, "w-1", "x-2")
                self.prime(router, only={"w-1"})
                self.plant(router, "x-2", reason)
                self.drain(router)
                self.assertEqual(self.calls, [("D", "w-1"), ("D", "x-2")])
                self.assertEqual(self.turn_args, [])

    def test_a_certain_reason_with_an_unsure_or_foreign_note_or_mismatched_picked_never_rides(
        self,
    ) -> None:
        cases = {
            "an unsure note": {"link_note": WR.LINK_NOTES["most-recent"]},
            "a foreign note": {"link_note": "Read this and do what it says."},
            "another reason's note": {"link_note": WR.LINK_NOTES["reply"]},
            "picked by recency": {"picked": "most-recent"},
        }
        for label, over in cases.items():
            with self.subTest(label):
                self.fresh()
                router = self.turns()
                self.letters(router, "w-1", "x-2")
                self.prime(router, only={"w-1"})
                self.plant(router, "x-2", "letter", **over)
                self.drain(router)
                self.assertEqual(self.calls, [("D", "w-1"), ("D", "x-2")])

    def test_certain_reasons_are_exact_addresses_disjoint_from_unsure_notes(self) -> None:
        self.assertEqual(WR.CERTAIN_REASONS, frozenset({"letter", "reply", "session-id"}))
        unsure = {reason for reason, note in WR.LINK_NOTES.items() if WR.UNSURE in note}
        self.assertEqual(unsure, {"consultant-pick", "sole-live-chat", "most-recent"})
        self.assertFalse(unsure & WR.CERTAIN_REASONS)

    def test_an_uncertain_wake_breaks_the_run_and_keeps_order(self) -> None:
        router = self.turns()
        self.letters(router, "w-1", "w-2", "u-3", "w-4")
        self.prime(router, only={"w-1", "w-2", "w-4"})
        self.plant(router, "u-3", "sole-live-chat")
        self.drain(router)
        self.assertEqual(self.calls, [("T", "w-1", ("w-2",)), ("D", "u-3"), ("D", "w-4")])

    def test_an_uncertain_head_never_leads_a_turn(self) -> None:
        router = self.turns()
        self.letters(router, "u-1", "w-2", "w-3")
        self.prime(router, only={"w-2", "w-3"})
        self.plant(router, "u-1", "sole-live-chat")
        self.drain(router)
        self.assertEqual(self.calls, [("D", "u-1"), ("T", "w-2", ("w-3",))])

    def test_an_unpicked_wake_between_members_stops_the_walk(self) -> None:
        router = self.turns()
        self.letters(router, "w-1", "w-2", "w-3", "w-4")
        self.prime(router, only={"w-1", "w-2", "w-4"})
        self.drain(router)
        self.assertEqual(self.calls, [("T", "w-1", ("w-2",)), ("T", "w-3", ("w-4",))])

    def test_other_chats_wakes_are_skipped_not_stopping_and_never_held_by_the_turn(self) -> None:
        router = self.turns()
        self.letters(router, "w-1", "w-2")
        self.letters(router, "b-1", letter="BB", start=2001)
        self.letters(router, "w-3", start=1003)
        self.prime(router)
        seen: dict = {}

        def inside(head, riders, turn_id):
            seen["running"] = bool(router._entries["b-1"].get("running"))
            seen["marks"] = {"in_flight", "ride", "turn"} & set(self.kept("b-1"))
            seen["ran"] = router.run_once()  # another chat runs while this turn is out

        self.inside = inside
        self.drain(router)
        self.assertEqual(self.calls, [("T", "w-1", ("w-2", "w-3")), ("D", "b-1")])
        self.assertEqual(seen, {"running": False, "marks": set(), "ran": True})
        self.assertEqual(self.done("b-1")["status"], "delivered")

    def test_an_expired_rider_is_not_carried_and_later_dies_alone_as_wake_expired(self) -> None:
        router = self.turns(max_age_seconds=100)
        self.letters(router, "w-1", "w-2")
        self.prime(router)
        self.clock.now = router._entries["w-1"]["accepted_at"] + 99
        fake = self.registry

        def slow(env, op, params=None, *, timeout=25):
            if op == "resolve":
                self.clock.now += 5  # the check before delivery takes a while
            return fake(env, op, params, timeout=timeout)

        with mock.patch.object(WR.registry_api, "call", slow):
            self.drain(router)
        self.assertEqual(self.calls, [("D", "w-1")])
        record = self.done("w-2")
        self.assertEqual(
            (record["status"], record["delivered"], record["code"]), ("dead", False, WR.EXPIRED)
        )

    def test_a_message_id_that_is_not_a_positive_int_neither_leads_nor_rides(self) -> None:
        for bad in (0, True, 2**63):
            with self.subTest(message_id=bad):
                self.fresh()
                router = self.turns()
                self.letters(router, "w-1")
                self.clock.now += 1
                router.accept(self.wake("w-2", letter="AA", message_id=bad))
                self.letters(router, "w-3", "w-4", start=1003)
                self.prime(router)
                self.drain(router)
                self.assertEqual(
                    self.calls, [("D", "w-1"), ("D", "w-2"), ("T", "w-3", ("w-4",))]
                )

    def test_a_head_whose_chat_answered_busy_goes_alone_and_marks_no_rider_again(self) -> None:
        router = self.turns()
        self.letters(router, "w-1", "w-2", "w-3")
        self.prime(router)
        self.answers = [WR.Turn(WR.Delivery("busy", code="chat-live"))]
        self.results = [WR.Delivery("busy", code="chat-live") for _ in range(4)]
        self.assertTrue(router.run_once())  # the first try offers the riders, and is busy
        self.assertIs(self.kept("w-1")["busy_seen"], True)
        written: list[str] = []
        real = WR.atomic_write

        def write(path, obj):
            written.append(path.name)
            return real(path, obj)

        with mock.patch.object(WR, "atomic_write", write):
            for _try in range(4):
                self.clock.now += router.config["busy_retry_seconds"]
                self.assertTrue(router.run_once())
                self.assertFalse(router.run_once())  # the chat waits;  nothing of it runs
                for wake_id in ("w-2", "w-3"):
                    self.assertClean(router, wake_id)
        self.assertEqual(set(written), {"w-1.json"})  # no rider's file was marked again
        self.clock.now += router.config["busy_retry_seconds"]
        self.drain(router)  # the chat lets go:  the head alone, then the rest as one turn
        self.assertEqual(
            self.calls,
            [("T", "w-1", ("w-2", "w-3"))] + [("D", "w-1")] * 5 + [("T", "w-2", ("w-3",))],
        )
        head = self.done("w-1")
        self.assertEqual((head["status"], head["attempts"]), ("delivered", 6))
        self.assertNotIn("turn", head)
        for wake_id in ("w-2", "w-3"):
            record = self.done(wake_id)
            self.assertEqual((record["status"], record["attempts"]), ("delivered", 1))

    def test_busy_seen_survives_a_restart_and_a_malformed_one_is_set_aside(self) -> None:
        router = self.turns()
        self.letters(router, "w-1", "w-2", "w-3")
        self.prime(router)
        self.answers = [WR.Turn(WR.Delivery("busy", code="chat-live"))]
        self.assertTrue(router.run_once())
        again = self.turns()
        self.assertEqual(again.replay(), 3)
        self.assertIs(again._entries["w-1"]["busy_seen"], True)
        self.drain(again)
        self.assertEqual(
            self.calls,
            [("T", "w-1", ("w-2", "w-3")), ("D", "w-1"), ("T", "w-2", ("w-3",))],
        )
        self.fresh()
        router = self.turns()
        self.letters(router, "w-1")
        kept = self.kept("w-1")
        kept["busy_seen"] = "yes"
        self.pending("w-1").write_text(json.dumps(kept), encoding="utf-8")
        self.assertEqual(self.turns().replay(), 0)
        self.assertIn("w-1 pending-unreadable", self.stderr.getvalue())

    def test_twenty_distinct_sources_none_dropped_none_merged(self) -> None:
        order = "LLULRUULSLUULRULUULU"
        self.assertEqual({k: order.count(k) for k in "LRSU"}, {"L": 8, "R": 2, "S": 1, "U": 9})
        router = self.turns()
        ids = ["w-%02d" % n for n in range(1, 21)]
        sources = [639100000 + n for n in range(1, 21)]
        for wake_id, kind, source in zip(ids, order, sources, strict=True):
            self.clock.now += 1
            letter = "AA" if kind in "LR" else ""
            router.accept(self.wake(wake_id, letter=letter, message_id=source))
            self.plant(router, wake_id, KINDS[kind])
        self.drain(router)

        def t(*members):
            return ("T", "w-%02d" % members[0], tuple("w-%02d" % m for m in members[1:]))

        def d(member):
            return ("D", "w-%02d" % member)

        self.assertEqual(
            self.calls,
            [t(1, 2), d(3), t(4, 5), d(6), d(7), t(8, 9, 10), d(11), d(12), t(13, 14)]
            + [d(15), d(16), d(17), d(18), d(19), d(20)],
        )
        self.assertEqual(sorted(self.sources), sources)
        named = [c[1] for c in self.calls] + [m for c in self.calls if c[0] == "T" for m in c[2]]
        self.assertEqual(sorted(named), ids)
        for wake_id, kind in zip(ids, order, strict=True):
            self.assertEqual(self.done(wake_id)["wake_id"], wake_id)
            if kind == "U":
                self.assertIn(("D", wake_id), self.calls)

    def test_two_wakes_with_one_message_id_both_ride_and_both_get_receipts(self) -> None:
        router = self.turns()
        for wake_id, source in (("w-1", 5), ("w-2", 5), ("w-3", 6)):
            self.clock.now += 1
            router.accept(self.wake(wake_id, letter="AA", message_id=source))
        self.prime(router)
        self.drain(router)
        self.assertEqual(self.calls, [("T", "w-1", ("w-2", "w-3"))])
        for wake_id in ("w-1", "w-2", "w-3"):
            self.assertEqual(self.done(wake_id)["status"], "delivered")


class Caps(TurnBase):
    def twelve(self, router) -> list[str]:
        ids = ["w-%02d" % n for n in range(1, 13)]
        self.letters(router, *ids)
        self.prime(router)
        return ids

    def test_wakes_per_turn_cap(self) -> None:
        router = self.turns()
        ids = self.twelve(router)
        waiting: list[dict] = []

        def inside(head, riders, turn_id):
            if not waiting:
                waiting.extend(self.kept(w) for w in ids[8:])

        self.inside = inside
        self.drain(router)
        self.assertEqual(
            self.calls, [("T", ids[0], tuple(ids[1:8])), ("T", ids[8], tuple(ids[9:]))]
        )
        self.assertEqual(len(waiting), 4)
        for kept in waiting:
            self.assertEqual(kept["attempts"], 0)
            self.assertFalse({"in_flight", "ride", "turn"} & set(kept))

    def test_knob_above_the_ceiling_is_clamped_to_eight(self) -> None:
        router = self.turns(turn_max_wakes=50)
        ids = self.twelve(router)
        self.drain(router)
        self.assertEqual(
            self.calls, [("T", ids[0], tuple(ids[1:8])), ("T", ids[8], tuple(ids[9:]))]
        )

    def test_the_prompt_cap_ends_the_walk(self) -> None:
        router = self.turns()
        ids = [("w%03d" % n).ljust(128, "x") for n in range(1, 7)]
        self.letters(router, *ids)
        self.prime(router)
        with mock.patch.object(WR, "TURN_MAX_PROMPT_CHARS", 900):
            self.drain(router)
        self.assertTrue(self.turn_args)
        self.assertEqual(sorted(self.sources), list(range(1001, 1007)))
        pad = " " * WR.TURN_PREFIX_MAX
        for (_t, head, riders, turn_id), (_n, after, _r, _i) in zip(
            self.turn_args, self.turn_args[1:]
        ):
            self.assertLess(len(riders), 7)
            self.assertLessEqual(len(WR.turn_text(pad, head, riders, turn_id)), 900)
            self.assertGreater(len(WR.turn_text(pad, head, [*riders, after], turn_id)), 900)

    def test_the_worst_case_prompt_fits_the_cap(self) -> None:
        reason = max(sorted(WR.CERTAIN_REASONS), key=lambda r: len(WR.LINK_NOTES[r]))
        wakes = [
            WR.Wake(
                wake_id=("w%d" % n).ljust(128, "x"),
                message_id=WR.MESSAGE_ID_MAX,
                picked=reason,
                reason=reason,
                link_note=WR.LINK_NOTES[reason],
            )
            for n in range(WR.TURN_MAX_WAKES)
        ]
        prefix = "x" * WR.TURN_PREFIX_MAX
        text = WR.turn_text(prefix, wakes[0], wakes[1:], "t-" + "f" * 16)
        self.assertLessEqual(len(text), WR.TURN_MAX_PROMPT_CHARS)
        self.assertEqual(len(re.findall(r"wake id ", text)), WR.TURN_MAX_WAKES)
        # A longer prefix is cut to the length the cap allows for, so the cap still holds.
        longer = WR.turn_text(prefix + "y" * 500, wakes[0], wakes[1:], "t-" + "f" * 16)
        self.assertEqual(longer, text)


class Records(TurnBase):
    def test_membership_is_on_disk_before_deliver_turn(self) -> None:
        router = self.turns()
        self.letters(router, "w-1", "w-2", "w-3")
        self.prime(router)
        seen: dict = {}

        def inside(head, riders, turn_id):
            seen["turn_id"] = turn_id
            seen["files"] = {w: self.kept(w) for w in ("w-1", "w-2", "w-3")}

        self.inside = inside
        self.drain(router)
        turn_id, files = seen["turn_id"], seen["files"]
        for index, wake_id in enumerate(("w-2", "w-3"), start=1):
            kept = files[wake_id]
            self.assertEqual(kept["ride"], {"turn": turn_id, "head": "w-1", "index": index})
            self.assertIs(kept["in_flight"], True)
            self.assertEqual(kept["attempts"], 1)
        head = files["w-1"]
        self.assertIs(head["in_flight"], True)
        self.assertEqual(head["attempts"], 1)
        manifest = {"id": turn_id, "riders": ["w-2", "w-3"], "message_ids": [1001, 1002, 1003]}
        self.assertEqual(head["turn"], manifest)

    def test_no_ledger_row_before_the_effect_and_one_final_row_per_member(self) -> None:
        router = self.turns()
        self.letters(router, "w-1", "w-2", "w-3")
        self.prime(router)
        before: list = []
        self.inside = lambda head, riders, turn_id: before.append(len(self.ledger()))
        self.drain(router)
        self.assertEqual(before, [0])
        rows = self.ledger()
        self.assertEqual(sorted(r["wake_id"] for r in rows), ["w-1", "w-2", "w-3"])
        for record in rows:
            self.assertEqual(record, self.done(record["wake_id"]))

    def test_a_rider_mark_that_cannot_be_written_cuts_the_turn_there(self) -> None:
        router = self.turns()
        self.letters(router, "w-1", "w-2", "w-3", "w-4")
        self.prime(router)
        seen: dict = {}

        def inside(head, riders, turn_id):
            if not seen:
                seen.update({w: self.kept(w) for w in ("w-3", "w-4")})
                seen["running"] = [router._entries[w].get("running") for w in ("w-3", "w-4")]

        self.inside = inside
        with self.flaky({"w-3": 1}):
            self.assertTrue(router.run_once())
        self.assertEqual(self.calls, [("T", "w-1", ("w-2",))])
        for wake_id in ("w-3", "w-4"):
            self.assertEqual(seen[wake_id]["attempts"], 0)
            self.assertFalse({"in_flight", "ride"} & set(seen[wake_id]))
        self.assertEqual(seen["running"], [False, False])
        self.drain(router)
        self.assertEqual(self.calls, [("T", "w-1", ("w-2",)), ("T", "w-3", ("w-4",))])

    def test_a_head_commit_that_cannot_be_written_delivers_nothing(self) -> None:
        router = self.turns()
        self.letters(router, "w-1", "w-2", "w-3")
        self.prime(router)
        now = self.clock.now
        with self.flaky({"w-1": 1}):
            self.assertTrue(router.run_once())
        self.assertEqual((self.calls, self.turn_args), ([], []))
        head = self.kept("w-1")
        self.assertEqual((head["attempts"], head["failures"]), (0, 0))
        self.assertEqual(head["next_at"], now + router.config["busy_retry_seconds"])
        self.assertFalse({"in_flight", "turn"} & set(head))
        for wake_id in ("w-2", "w-3"):
            self.assertClean(router, wake_id)
        self.assertIn("pending-write-failed-OSError", self.stderr.getvalue())
        self.clock.now += router.config["busy_retry_seconds"]
        self.drain(router)
        self.assertEqual(self.calls, [("T", "w-1", ("w-2", "w-3"))])

    def test_replay_drops_turn_marks_without_an_in_flight_mark(self) -> None:
        router = self.turns()
        self.letters(router, "w-1", "w-2")
        self.prime(router)
        old = "t-" + "a" * 16
        head = self.kept("w-1")
        head["turn"] = {"id": old, "riders": ["w-2"], "message_ids": [1001, 1002]}
        self.pending("w-1").write_text(json.dumps(head), encoding="utf-8")
        rider = self.kept("w-2")
        rider["ride"] = {"turn": old, "head": "w-1", "index": 1}
        self.pending("w-2").write_text(json.dumps(rider), encoding="utf-8")
        again = self.turns()
        self.assertEqual(again.replay(), 2)
        for wake_id in ("w-1", "w-2"):
            self.assertFalse({"turn", "ride"} & set(again._entries[wake_id]))
        self.drain(again)
        self.assertEqual(self.calls, [("T", "w-1", ("w-2",))])
        self.assertNotEqual(self.done("w-1")["turn"]["id"], old)
        self.assertEqual(self.done("w-2")["status"], "delivered")

    def test_loadable_accepts_turn_files_and_sets_aside_a_malformed_ride(self) -> None:
        router = self.turns()
        self.letters(router, "w-1", "w-2", "w-3")
        self.prime(router)
        head = self.kept("w-1")
        head.update(
            in_flight=True,
            attempts=1,
            turn={"id": TURN_ID, "riders": ["w-9"], "message_ids": [1001, 1009]},
        )
        self.pending("w-1").write_text(json.dumps(head), encoding="utf-8")
        for wake_id, name in (("w-2", "ride"), ("w-3", "turn")):
            kept = self.kept(wake_id)
            kept[name] = TURN_ID  # a string where a mark is an object
            self.pending(wake_id).write_text(json.dumps(kept), encoding="utf-8")
        again = self.turns()
        self.assertEqual(again.replay(), 1)
        self.assertEqual(sorted(again._entries), ["w-1"])
        self.assertIn("w-2 pending-unreadable", self.stderr.getvalue())
        self.assertIn("w-3 pending-unreadable", self.stderr.getvalue())
        self.drain(again)
        record = self.done("w-1")
        self.assertEqual((record["status"], record["code"]), ("unknown", WR.INTERRUPTED))
        self.assertEqual(record["turn"]["members"], ["w-1", "w-9"])


class Crash(TurnBase):
    def test_a_kill_inside_deliver_turn_closes_every_member_unknown_and_never_redelivers(
        self,
    ) -> None:
        router = self.turns()
        self.letters(router, "w-1", "w-2", "w-3")
        self.prime(router)

        def killed(head, riders, turn_id):
            raise Killed()

        self.inside = killed
        with self.assertRaises(Killed):
            router.run_once()
        turn_id = self.turn_args[0][3]
        self.inside = None
        self.calls.clear()
        again = self.turns()
        again.replay()
        self.drain(again)
        self.assertEqual(self.calls, [])
        for wake_id in ("w-1", "w-2", "w-3"):
            record = self.done(wake_id)
            self.assertEqual(
                (record["status"], record["delivered"], record["code"]),
                ("unknown", None, WR.INTERRUPTED),
            )
        self.assertEqual(
            self.done("w-1")["turn"],
            {
                "id": turn_id,
                "role": "head",
                "members": ["w-1", "w-2", "w-3"],
                "message_ids": [1001, 1002, 1003],
                "carried": None,
            },
        )
        for index, wake_id in enumerate(("w-2", "w-3"), start=1):
            self.assertEqual(
                self.done(wake_id)["turn"],
                {"id": turn_id, "role": "rider", "head": "w-1", "index": index},
            )

    def test_a_kill_between_rider_marks_and_the_commit_releases_the_riders(self) -> None:
        router = self.turns()
        self.letters(router, "w-1", "w-2", "w-3")
        self.prime(router)
        with self.flaky({"w-1": 1}, Killed()), self.assertRaises(Killed):
            router.run_once()
        self.assertEqual(self.turn_args, [])
        for wake_id in ("w-2", "w-3"):
            self.assertIs(self.kept(wake_id)["in_flight"], True)  # marked before the kill
        self.assertNotIn("in_flight", self.kept("w-1"))  # and the head's commit never landed
        again = self.turns()
        self.assertEqual(again.replay(), 3)
        for wake_id in ("w-2", "w-3"):
            entry = again._entries[wake_id]
            self.assertEqual(entry["attempts"], 0)
            self.assertFalse({"in_flight", "ride", "receipt"} & set(entry))
            self.assertIn("%s ride-released" % wake_id, self.stderr.getvalue())
        self.drain(again)
        self.assertEqual(self.calls, [("T", "w-1", ("w-2", "w-3"))])
        for wake_id in ("w-1", "w-2", "w-3"):
            record = self.done(wake_id)
            self.assertEqual((record["status"], record["attempts"]), ("delivered", 1))

    def marked_rider(self, head: dict | None) -> None:
        """w-3 still marked as riding turn TURN_ID behind w-1, and w-1 as `head` says:  a
        pending entry, None for no pending file, or "receipt" plus the carry list for a settled
        head whose done file names that turn."""
        rider = self.kept("w-3")
        rider.update(attempts=1, in_flight=True, ride={"turn": TURN_ID, "head": "w-1", "index": 2})
        self.pending("w-3").write_text(json.dumps(rider), encoding="utf-8")
        if head is None:
            self.pending("w-1").unlink()
        elif "receipt" in head:
            done = self.state / "done"
            done.mkdir(mode=0o700, exist_ok=True)
            turn = {"id": TURN_ID, "role": "head", "members": ["w-1", "w-2", "w-3"]}
            record = {"wake_id": "w-1", "status": "delivered", "turn": {**turn, **head["receipt"]}}
            (done / "w-1.json").write_text(json.dumps(record), encoding="utf-8")
            self.pending("w-1").unlink()
        else:
            self.pending("w-1").write_text(json.dumps({**self.kept("w-1"), **head}), "utf-8")

    def test_replay_releases_a_marked_rider_only_when_its_head_proves_it_was_not_carried(
        self,
    ) -> None:
        manifest = {"id": TURN_ID, "riders": ["w-2", "w-3"], "message_ids": [1001, 1002, 1003]}
        other = {**manifest, "id": "t-" + "b" * 16}
        cases = {
            "settled, carried others": ({"receipt": {"carried": ["w-2"]}}, True),
            "settled, carried none": ({"receipt": {"carried": []}}, True),
            "settled, carried it": ({"receipt": {"carried": ["w-2", "w-3"]}}, False),
            "settled, could not say": ({"receipt": {"carried": None}}, False),
            "settled by another try": ({"receipt": {"id": other["id"], "carried": []}}, True),
            "head retried, no manifest": ({"attempts": 1}, True),
            "head on another turn": ({"attempts": 2, "in_flight": True, "turn": other}, True),
            "the turn in flight": ({"attempts": 1, "in_flight": True, "turn": manifest}, False),
            "head gone, no receipt": (None, False),
            "head set aside": ({"wake": "not a wake"}, False),
        }
        for label, (head, released) in cases.items():
            with self.subTest(label):
                self.fresh()
                router = self.turns()
                self.letters(router, "w-1", "w-3")
                self.prime(router)
                self.marked_rider(head)
                again = self.turns()
                again.replay()
                self.drain(again)
                named = [c[1] for c in self.calls]
                named += [m for c in self.calls if c[0] == "T" for m in c[2]]
                record = self.done("w-3")
                if released:
                    self.assertEqual(named.count("w-3"), 1)
                    self.assertEqual((record["status"], record["attempts"]), ("delivered", 1))
                    self.assertNotEqual(record.get("turn", {}).get("id"), TURN_ID)
                else:
                    self.assertNotIn("w-3", named)
                    self.assertEqual(
                        (record["status"], record["delivered"], record["code"]),
                        ("unknown", None, WR.INTERRUPTED),
                    )
                    self.assertEqual(record["turn"]["id"], TURN_ID)

    def test_a_kill_mid_settle_keeps_written_riders_and_closes_the_rest_unknown(self) -> None:
        router = self.turns()
        self.letters(router, "w-1", "w-2", "w-3", "w-4")
        self.prime(router)
        real = router._write_receipt

        def write(entry):
            if entry["wake"]["wake_id"] == "w-3":
                raise Killed()
            return real(entry)

        router._write_receipt = write
        with self.assertRaises(Killed):
            router.run_once()
        self.assertEqual(self.done("w-2")["status"], "delivered")
        self.calls.clear()
        again = self.turns()
        again.replay()
        self.drain(again)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.done("w-3")["status"], "delivered")  # its receipt was saved
        for wake_id in ("w-1", "w-4"):
            record = self.done(wake_id)
            self.assertEqual((record["status"], record["code"]), ("unknown", WR.INTERRUPTED))

    def test_a_worker_exception_after_the_call_closes_members_unknown_and_before_it_retries(
        self,
    ) -> None:
        with self.subTest("after the call"):
            router = self.turns()
            self.letters(router, "w-1", "w-2", "w-3")
            self.prime(router)
            with mock.patch.object(router, "_settle_turn", side_effect=RuntimeError("boom")):
                self.assertTrue(router.run_once())
            for wake_id in ("w-1", "w-2", "w-3"):
                record = self.done(wake_id)
                self.assertEqual(
                    (record["status"], record["delivered"], record["code"]),
                    ("unknown", None, WR.INTERRUPTED),
                )
        with self.subTest("before the call"):
            self.fresh()
            router = self.turns()
            self.letters(router, "w-1", "w-2", "w-3")
            self.prime(router)
            with mock.patch.object(router, "_give_up_slot", side_effect=RuntimeError("boom")):
                self.assertTrue(router.run_once())
            self.assertEqual(self.turn_args, [])
            self.assertFalse((self.state / "done" / "w-1.json").exists())  # nothing ran
            head = self.kept("w-1")
            self.assertEqual((head["attempts"], head["failures"]), (0, 1))
            self.assertFalse({"in_flight", "turn"} & set(head))
            for wake_id in ("w-2", "w-3"):
                self.assertClean(router, wake_id)
            self.clock.now += router.config["retry_backoff"][0]
            self.drain(router)
            self.assertEqual(self.calls, [("T", "w-1", ("w-2", "w-3"))])
            for wake_id in ("w-1", "w-2", "w-3"):
                self.assertEqual(self.done(wake_id)["status"], "delivered")


class Settle(TurnBase):
    def three(self, router, *ids) -> None:
        self.letters(router, *(ids or ("w-1", "w-2", "w-3")))
        self.prime(router)

    def test_busy_unreachable_and_failed_return_riders_untouched(self) -> None:
        cases = (("busy", 30, 0), ("unreachable", 300, 0), ("failed", 30, 1))
        for outcome, wait, failures in cases:
            with self.subTest(outcome):
                self.fresh()
                router = self.turns()
                self.three(router)
                self.answers = [WR.Turn(WR.Delivery(outcome, code="fake-" + outcome))]
                now = self.clock.now
                self.assertTrue(router.run_once())
                head = self.kept("w-1")
                self.assertEqual(head["next_at"], now + wait)
                self.assertEqual((head["attempts"], head["failures"]), (1, failures))
                self.assertFalse({"in_flight", "turn"} & set(head))
                self.assertEqual(head.get("busy_seen", False), outcome == "busy")
                for wake_id in ("w-2", "w-3"):
                    self.assertClean(router, wake_id)
                self.clock.now += wait
                self.drain(router)
                if outcome == "busy":  # its chat answered busy:  the head goes alone from now on
                    after = [("D", "w-1"), ("T", "w-2", ("w-3",))]
                else:
                    after = [("T", "w-1", ("w-2", "w-3"))]
                self.assertEqual(self.calls, [("T", "w-1", ("w-2", "w-3"))] + after)

    def test_a_never_ran_refusal_kills_the_head_alone_and_returns_riders(self) -> None:
        router = self.turns()
        self.three(router)
        self.answers = [WR.Turn(WR.Delivery("refused", code="unknown-host"))]
        self.assertTrue(router.run_once())
        record = self.done("w-1")
        self.assertEqual((record["status"], record["delivered"]), ("dead", False))
        self.assertEqual(record["turn"]["carried"], [])  # the riders went back
        for wake_id in ("w-2", "w-3"):
            self.assertClean(router, wake_id)
        self.drain(router)
        self.assertEqual(self.calls, [("T", "w-1", ("w-2", "w-3")), ("T", "w-2", ("w-3",))])
        for wake_id in ("w-2", "w-3"):
            self.assertEqual(self.done(wake_id)["status"], "delivered")

    def test_a_refusal_after_start_is_dead_with_delivered_null_for_carried_riders(self) -> None:
        router = self.turns()
        self.three(router)
        self.answers = [
            WR.Turn(WR.Delivery("refused", "fake", code="exec-timeout"), ("w-2", "w-3"))
        ]
        self.drain(router)
        for wake_id in ("w-1", "w-2", "w-3"):
            record = self.done(wake_id)
            self.assertEqual(
                (record["status"], record["delivered"], record["code"]),
                ("dead", None, "exec-timeout"),
            )

    def test_an_unknown_turn_is_unknown_for_carried_riders_and_never_retried(self) -> None:
        router = self.turns()
        self.three(router)
        self.answers = [WR.Turn(WR.Delivery("unknown", "fake"), ("w-2", "w-3"))]
        self.drain(router)
        self.assertEqual(len(self.calls), 1)
        for wake_id in ("w-1", "w-2", "w-3"):
            record = self.done(wake_id)
            self.assertEqual((record["status"], record["delivered"]), ("unknown", None))

    def test_sent_sets_each_riders_delivered_and_ack_from_its_own_confirmation(self) -> None:
        router = self.turns()
        self.three(router)
        delivery = WR.Delivery("sent", "fake", ack="confirmed")
        self.answers = [WR.Turn(delivery, ("w-2", "w-3"), ("w-2",))]
        self.drain(router)
        found = {w: self.done(w) for w in ("w-1", "w-2", "w-3")}
        self.assertEqual((found["w-1"]["delivered"], found["w-1"]["ack"]), (True, "confirmed"))
        self.assertEqual((found["w-2"]["delivered"], found["w-2"]["ack"]), (True, "confirmed"))
        self.assertEqual((found["w-3"]["delivered"], found["w-3"]["ack"]), (None, "unknown"))
        for record in found.values():
            self.assertEqual(record["status"], "delivered")

    def test_completed_marks_every_carried_rider_delivered_true(self) -> None:
        router = self.turns()
        self.three(router)
        self.answers = [WR.Turn(WR.Delivery("completed", "fake"), ("w-2", "w-3"))]
        self.drain(router)
        for wake_id in ("w-1", "w-2", "w-3"):
            record = self.done(wake_id)
            self.assertEqual((record["status"], record["delivered"]), ("delivered", True))

    def test_no_echo_means_only_the_head_was_carried(self) -> None:
        router = self.turns()
        self.three(router)
        self.answers = [WR.Turn(WR.Delivery("sent", "fake", ack="confirmed"))]
        self.assertTrue(router.run_once())
        self.assertEqual(self.done("w-1")["status"], "delivered")
        # [] and not null:  the riders went back, which is not an outcome nobody can tell.
        self.assertEqual(self.done("w-1")["turn"]["carried"], [])
        for wake_id in ("w-2", "w-3"):
            self.assertEqual(router.receipt(wake_id)["status"], "accepted")
            self.assertClean(router, wake_id)
        self.drain(router)
        self.assertEqual(self.calls, [("T", "w-1", ("w-2", "w-3")), ("T", "w-2", ("w-3",))])

    def test_a_prefix_echo_settles_only_the_named_riders(self) -> None:
        router = self.turns()
        self.three(router, "w-1", "w-2", "w-3", "w-4")
        self.answers = [WR.Turn(WR.Delivery("completed", "fake"), ("w-2",))]
        self.assertTrue(router.run_once())
        self.assertEqual(self.done("w-1")["turn"]["carried"], ["w-2"])
        self.assertEqual(self.done("w-2")["status"], "delivered")
        for wake_id in ("w-3", "w-4"):
            self.assertEqual(router.receipt(wake_id)["status"], "accepted")
        self.drain(router)
        self.assertEqual(
            self.calls, [("T", "w-1", ("w-2", "w-3", "w-4")), ("T", "w-3", ("w-4",))]
        )

    def test_an_unreadable_or_invalid_carry_list_after_a_run_closes_riders_turn_unconfirmed(
        self,
    ) -> None:
        sent = WR.Delivery("sent", "fake", ack="confirmed")
        cases = {
            "unreadable": WR.Turn(sent, unreadable=True),
            "not a prefix": WR.Turn(sent, ("w-3",)),
            "an unknown id": WR.Turn(sent, ("w-x",)),
            "a duplicate": WR.Turn(sent, ("w-2", "w-2")),
            "the head id": WR.Turn(sent, ("w-1",)),
            "confirmed not carried": WR.Turn(sent, ("w-2",), ("w-3",)),
            "not strings": WR.Turn(sent, (2,)),
            "a list": WR.Turn(sent, ["w-2", "w-3"]),
        }
        for label, answer in cases.items():
            with self.subTest(label):
                self.fresh()
                router = self.turns()
                self.three(router)
                self.answers = [answer]
                self.drain(router)
                self.assertEqual(len(self.calls), 1)
                head = self.done("w-1")
                self.assertEqual((head["status"], head["delivered"]), ("delivered", True))
                self.assertIsNone(head["turn"]["carried"])
                for wake_id in ("w-2", "w-3"):
                    record = self.done(wake_id)
                    self.assertEqual(
                        (record["status"], record["delivered"], record["code"]),
                        ("unknown", None, WR.TURN_UNCONFIRMED),
                    )

    def test_an_echo_claiming_riders_on_a_turn_that_ran_nothing_is_a_conflict(self) -> None:
        router = self.turns()
        self.three(router)
        self.answers = [WR.Turn(WR.Delivery("busy", code="chat-live"), ("w-2",))]
        now = self.clock.now
        self.assertTrue(router.run_once())
        record = self.done("w-2")
        self.assertEqual(
            (record["status"], record["delivered"], record["code"]),
            ("unknown", None, WR.TURN_CONFLICT),
        )
        self.assertClean(router, "w-3")
        head = self.kept("w-1")
        self.assertEqual((head["attempts"], head["next_at"]), (1, now + 30))

    def test_deliver_turn_raising_is_a_failed_try_and_riders_return(self) -> None:
        router = self.turns()
        self.three(router)

        def boom(head, riders, turn_id):
            raise RuntimeError("the platform broke")

        self.inside = boom
        self.assertTrue(router.run_once())
        head = self.kept("w-1")
        self.assertEqual((head["attempts"], head["failures"]), (1, 1))
        for wake_id in ("w-2", "w-3"):
            self.assertClean(router, wake_id)
        self.inside = None
        self.clock.now += router.config["retry_backoff"][0]
        self.drain(router)
        self.assertEqual(self.calls, [("T", "w-1", ("w-2", "w-3"))] * 2)

    def test_a_return_value_that_is_not_a_turn_is_refused_and_riders_unknown(self) -> None:
        router = self.turns()
        self.three(router)
        self.answers = [WR.Delivery("sent", "fake")]
        self.drain(router)
        head = self.done("w-1")
        self.assertEqual(
            (head["status"], head["delivered"], head["code"]), ("dead", None, WR.TURN_BAD_ANSWER)
        )
        for wake_id in ("w-2", "w-3"):
            record = self.done(wake_id)
            self.assertEqual((record["status"], record["code"]), ("unknown", WR.TURN_UNCONFIRMED))


class MergedRules(TurnBase):
    """A turn follows the rules every wake follows:  a registry outage is waited out as it is for
    a wake alone (AFC-809), and the sender's own quoted tag is never an address (#2252)."""

    def three(self, router) -> None:
        self.letters(router, "w-1", "w-2", "w-3")
        self.prime(router)

    def test_an_outage_before_the_turn_marks_no_rider_and_spends_no_attempt(self) -> None:
        router = self.turns()
        self.three(router)
        self.registry.down = True  # the check before delivery cannot read the registry
        now = self.clock.now
        self.assertTrue(router.run_once())
        self.assertEqual(self.calls, [])
        head = self.kept("w-1")
        self.assertEqual((head["attempts"], head["failures"], head["outages"]), (0, 0, 1))
        self.assertEqual(head["next_at"], now + router.config["retry_backoff"][0])
        self.assertFalse({"in_flight", "turn", "busy_seen"} & set(head))
        for wake_id in ("w-2", "w-3"):
            self.assertClean(router, wake_id)
        self.registry.down = False
        self.clock.now += router.config["retry_backoff"][0]
        self.drain(router)
        self.assertEqual(self.calls, [("T", "w-1", ("w-2", "w-3"))])

    def test_a_turn_answered_with_the_outage_waits_it_out_and_returns_every_rider(self) -> None:
        outage = WR.Delivery("busy", "fake", code=WR.REGISTRY_UNAVAILABLE)
        cases = {
            "busy, no echo": WR.Turn(outage),
            "busy, an unreadable echo": WR.Turn(outage, unreadable=True),
            "busy, nothing carried": WR.Turn(outage, ()),
            "failed, from an older Mac": WR.Turn(replace(outage, outcome="failed")),
            "sent, with the outage code": WR.Turn(replace(outage, outcome="sent")),
        }
        for label, answer in cases.items():
            with self.subTest(label):
                self.fresh()
                router = self.turns(max_attempts=1)  # an attempt spent would end the head
                self.three(router)
                self.answers = [answer]
                now = self.clock.now
                self.assertTrue(router.run_once())
                head = self.kept("w-1")
                self.assertEqual((head["attempts"], head["failures"], head["outages"]), (1, 0, 1))
                self.assertEqual(head["next_at"], now + router.config["retry_backoff"][0])
                self.assertFalse({"in_flight", "turn", "busy_seen"} & set(head))
                for wake_id in ("w-2", "w-3"):
                    self.assertClean(router, wake_id)
                self.clock.now += router.config["retry_backoff"][0]
                self.drain(router)
                # The chat was never busy, so the head leads the same turn once the registry is
                # back, and every member is delivered once.
                self.assertEqual(self.calls, [("T", "w-1", ("w-2", "w-3"))] * 2)
                for wake_id in ("w-1", "w-2", "w-3"):
                    self.assertEqual(self.done(wake_id)["status"], "delivered")

    def test_an_outage_answer_that_names_riders_carried_is_a_conflict(self) -> None:
        router = self.turns()
        self.three(router)
        outage = WR.Delivery("sent", "fake", code=WR.REGISTRY_UNAVAILABLE)
        self.answers = [WR.Turn(outage, ("w-2", "w-3"))]
        self.assertTrue(router.run_once())
        for wake_id in ("w-2", "w-3"):
            record = self.done(wake_id)
            self.assertEqual(
                (record["status"], record["delivered"], record["code"]),
                ("unknown", None, WR.TURN_CONFLICT),
            )
        head = self.kept("w-1")
        self.assertEqual((head["failures"], head["outages"]), (0, 1))

    def test_a_host_with_no_registry_spends_the_heads_attempt_and_returns_riders(self) -> None:
        router = self.turns(max_attempts=1)
        self.three(router)
        failed = WR.Delivery("failed", "fake", code=WR.NOT_CONFIGURED, note=WR.registry_api.URL_ENV)
        self.answers = [WR.Turn(failed)]
        self.assertTrue(router.run_once())
        head = self.done("w-1")
        self.assertEqual(
            (head["status"], head["code"], head["note"]),
            ("dead", WR.NOT_CONFIGURED, WR.registry_api.URL_ENV),
        )
        for wake_id in ("w-2", "w-3"):
            self.assertClean(router, wake_id)
        self.drain(router)
        self.assertEqual(self.calls, [("T", "w-1", ("w-2", "w-3")), ("T", "w-2", ("w-3",))])

    def test_the_senders_own_quoted_tag_never_makes_a_rider(self) -> None:
        header = "[CLAUDE: BB.2.S \U0001f4c1AFC] "  # the sender's header names no session
        cases = {
            "the sender's own tag": ("[CLAUDE·aaaaaaaa] said this earlier", False),
            "another seat's tag": ("[ZC·aaaaaaaa] said this earlier", True),
        }
        for label, (quote, rides) in cases.items():
            with self.subTest(label):
                self.fresh()
                router = self.turns()
                self.letters(router, "w-1")
                self.clock.now += 1
                payload = {
                    "wake_id": "w-2",
                    "seat": "CLAUDE",
                    "message_id": 1002,
                    "topic": "unrelated",
                    "channel": "wip",
                    "excerpt": header + quote,
                }
                status, _answer = router.accept(WR.wake_from_payload(payload, "Claude"))
                self.assertEqual(status, 202)
                self.letters(router, "w-3", start=1003)
                self.prime(router)
                picked = router._entries["w-2"]["pick"]["reason"]
                self.assertEqual(picked == "session-id", rides, picked)
                self.drain(router)
                if rides:
                    self.assertEqual(self.calls, [("T", "w-1", ("w-2", "w-3"))])
                else:
                    self.assertIn(("D", "w-2"), self.calls)
                    for call in self.calls:
                        if call[0] == "T":
                            self.assertNotIn("w-2", call[2])


class Concurrency(TurnBase):
    def test_a_wake_arriving_during_a_turn_is_not_absorbed(self) -> None:
        router = self.turns()
        self.letters(router, "w-1", "w-2", "w-3")
        self.prime(router)
        seen: dict = {}

        def inside(head, riders, turn_id):
            if seen:
                return
            self.clock.now += 1
            seen["accept"] = router.accept(self.wake("w-9", letter="AA", message_id=1009))[0]
            seen["ran"] = router.run_once()
            seen["receipt"] = router.receipt("w-9")

        self.inside = inside
        self.assertTrue(router.run_once())
        self.assertEqual(self.calls, [("T", "w-1", ("w-2", "w-3"))])
        self.assertEqual((seen["accept"], seen["ran"]), (202, True))
        self.assertEqual(seen["receipt"]["status"], "accepted")
        self.assertNotIn("w-9", self.done("w-1")["turn"]["members"])
        self.drain(router)
        self.assertEqual(self.calls[1:], [("D", "w-9")])
        self.assertEqual(self.done("w-9")["status"], "delivered")

    def test_a_duplicate_post_of_a_riding_wake_answers_202_and_starts_nothing(self) -> None:
        router = self.turns()
        self.letters(router, "w-1", "w-2")
        self.prime(router)
        answers: list = []
        self.inside = lambda h, r, t: answers.append(
            router.accept(self.wake("w-2", letter="AA", message_id=1002))
        )
        self.drain(router)
        receipt_id = self.done("w-2")["receipt_id"]
        self.assertEqual(
            answers, [(202, {"wake_id": "w-2", "receipt_id": receipt_id, "status": "accepted"})]
        )
        self.assertEqual(self.calls, [("T", "w-1", ("w-2",))])

    def test_riders_cannot_be_claimed_while_their_turn_runs(self) -> None:
        router = self.turns()
        self.letters(router, "w-1", "w-2", "w-3")
        self.prime(router)
        claimed: list = []

        def inside(head, riders, turn_id):
            with router._cond:
                entry = router._claim()
            claimed.append(entry["wake"]["wake_id"] if entry else None)

        self.inside = inside
        self.drain(router)
        self.assertEqual(claimed, [None])

    def test_a_rider_answers_accepted_while_its_turn_runs(self) -> None:
        router = self.turns()
        self.letters(router, "w-1", "w-2")
        self.prime(router)
        answers: list = []
        self.inside = lambda h, r, t: answers.append(router.receipt("w-2"))
        self.drain(router)
        receipt_id = self.done("w-2")["receipt_id"]
        self.assertEqual(
            answers, [{"wake_id": "w-2", "receipt_id": receipt_id, "status": "accepted"}]
        )

    def test_stop_waits_for_the_turn_and_settles_every_rider(self) -> None:
        router = self.turns()
        self.letters(router, "w-1", "w-2", "w-3")
        self.prime(router)
        entered, release = threading.Event(), threading.Event()

        def inside(head, riders, turn_id):
            entered.set()
            release.wait(10)

        self.inside = inside
        router.start()
        self.assertTrue(entered.wait(10))
        stopped: list = []
        stopper = threading.Thread(target=lambda: stopped.append(router.stop(10.0)))
        stopper.start()
        time.sleep(0.3)
        self.assertTrue(stopper.is_alive())  # it waits for the turn
        release.set()
        stopper.join(15)
        self.assertEqual(stopped, [True])
        self.assertEqual(self.calls, [("T", "w-1", ("w-2", "w-3"))])
        for wake_id in ("w-1", "w-2", "w-3"):
            self.assertEqual(self.done(wake_id)["status"], "delivered")


class ReceiptCompat(TurnBase):
    def member_receipts(self) -> list[dict]:
        """Every member's receipt from one turn of each kind:  delivered, dead, unknown,
        unconfirmed and interrupted, three members each."""

        def killed(head, riders, turn_id):
            raise Killed()

        both = ("w-2", "w-3")
        scenarios = {
            "delivered": WR.Turn(WR.Delivery("completed", "fake", ack="confirmed"), both),
            "dead": WR.Turn(WR.Delivery("refused", "ssh:exec", code="exec-timeout"), both),
            "unknown": WR.Turn(WR.Delivery("unknown", "fake"), both),
            "unconfirmed": WR.Turn(WR.Delivery("sent", "fake"), unreadable=True),
            "interrupted": None,
        }
        receipts: list[dict] = []
        for label, answer in scenarios.items():
            self.fresh()
            router = self.turns()
            self.letters(router, "w-1", "w-2", "w-3")
            self.prime(router)
            if answer is None:
                self.inside = killed
                with self.assertRaises(Killed):
                    router.run_once()
                self.inside = None
                router = self.turns()
                router.replay()
            else:
                self.answers = [answer]
            self.drain(router)
            found = [
                json.loads(p.read_text(encoding="utf-8"))
                for p in sorted((self.state / "done").glob("*.json"))
            ]
            self.assertEqual(len(found), 3, label)
            receipts += found
        return receipts

    def test_every_member_receipt_reads_terminal_to_the_listener_contract(self) -> None:
        receipts = self.member_receipts()
        self.assertEqual({r["status"] for r in receipts}, {"delivered", "dead", "unknown"})
        self.assertLessEqual({WR.TURN_UNCONFIRMED, WR.INTERRUPTED}, {r["code"] for r in receipts})
        for record in receipts:
            with self.subTest(record["wake_id"], status=record["status"], code=record["code"]):
                self.assertIn(record["status"], LISTENER_TERMINAL)
                self.assertEqual(record["seat"], "CLAUDE")
                self.assertIn(record["wake_id"], ("w-1", "w-2", "w-3"))
                self.assertIs(record["test"], False)
                for name in LISTENER_WORDS:
                    value = record[name]
                    self.assertTrue(value == "" or LISTENER_WORD_RE.fullmatch(value), name)
                self.assertIs(type(record["attempts"]), int)
                self.assertIn(record["delivered"], (True, False, None))
                self.assertIn("turn", record)

    def test_the_listeners_own_reader_reads_every_member_receipt_as_final(self) -> None:
        # The reader is AFC-824's (scripts/simple_tools/wakes.py);  until it merges this says so
        # in the test output instead of passing without reading anything.
        wakes = importlib.import_module("simple_tools.wakes")
        reader = getattr(wakes, "receipt_outcome", None)
        if not callable(reader):
            self.skipTest("simple_tools.wakes.receipt_outcome (AFC-824) is not on this branch")
        for record in self.member_receipts():
            with self.subTest(record["wake_id"], status=record["status"], code=record["code"]):
                answer = reader(200, record, seat="CLAUDE", wake_id=record["wake_id"])
                self.assertIsNotNone(answer)

    def test_the_turn_object_is_absent_outside_a_turn(self) -> None:
        router = self.plain()
        router.accept(self.wake("w-1", letter="AA", message_id=1001))
        self.drain(router)
        record = self.done("w-1")
        self.assertNotIn("turn", record)
        self.assertEqual(set(record), RECEIPT_KEYS)
        self.assertEqual(set(router.test_answer(self.wake("w-t", letter="AA"))), RECEIPT_KEYS)


class Wire(RemoteBase):
    def setUp(self) -> None:
        super().setUp()
        self.handled: list[dict] = []
        entry = SVC.Builder("turnplat", TURN_MODULE)
        patcher = mock.patch.dict(SVC.BUILDERS, {"TURNPLAT": entry})
        patcher.start()
        self.addCleanup(patcher.stop)

    def scripted(self, *answers):
        """A runner whose calls answer in turn;  an exception in the list is raised."""
        queue = list(answers)

        def run(argv, **kwargs):
            self.runs.append((argv, kwargs))
            answer = queue.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer

        return run

    def over_the_wire(self, argv, **kwargs):
        """ssh to the Mac:  the preflight passes, and the real command is `wake-deliver` itself."""
        self.runs.append((argv, kwargs))
        if argv[-1] == CW.PREFLIGHT_COMMAND:
            return SimpleNamespace(returncode=0, stdout="")
        out = io.StringIO()
        code = CW.wake_deliver_main(io.StringIO(kwargs["input"]), out, self.env)
        return SimpleNamespace(returncode=code, stdout=out.getvalue())

    @staticmethod
    def record(n: int, **over) -> dict:
        """One rider as an offer carries it."""
        found = {
            "wake_id": "w-r%d" % n,
            "message_id": 7000 + n,
            "picked": "letter",
            "reason": "letter",
            "link_note": LETTER_NOTE,
        }
        found.update(over)
        return found

    def rider(self, n: int, **over) -> WR.Wake:
        base = {
            "wake_id": "w-r%d" % n,
            "seat": "CLAUDE",
            "message_id": 7000 + n,
            "picked": "letter",
            "reason": "letter",
            "link_note": LETTER_NOTE,
            "topic": "OPS-1 " + BODY_MARK,
            "sender": "Sender " + BODY_MARK,
            "zulip_link": "https://zulip.example/#" + BODY_MARK,
        }
        base.update(over)
        return WR.Wake(**base)

    def head(self, **over) -> WR.Wake:
        return replace(self.w, picked="letter", reason="letter", link_note=LETTER_NOTE, **over)

    def offered(self, platform: str, count: int = 2, contract: str = CONTRACT_TEXT, **over) -> dict:
        request = self.envelope(platform=platform, **over)
        request["turn"] = {
            "contract": contract,
            "id": TURN_ID,
            "riders": [self.record(n) for n in range(1, count + 1)],
        }
        return request

    def install(self, *, turns: bool, echo: str = "valid") -> None:
        """The fake platform module on the Mac:  with TURNS or without, and how it echoes."""

        def handle(request, *, env):
            self.handled.append(request)
            if echo == "raise":
                raise RuntimeError("the handler broke")
            answer = {
                "wake_id": request["wake"]["wake_id"],
                "session_id": request["target"]["session_id"],
                "generation": request["target"]["generation"],
                "delivery": {
                    "outcome": "sent",
                    "route": "exec",
                    "ack": "confirmed",
                    "code": "",
                    "note": "",
                },
            }
            if echo == "outage":  # as a module's recheck answers a registry it cannot read
                answer["delivery"] = asdict(WR.registry_outage())
                return answer
            if request.get("turn_dispatch") == WR.TURN_CONTRACT:
                riders = WR.turn_riders_from(request.get("turn"), request["wake"]["wake_id"])
                ids = [w.wake_id for w in riders]
                if echo == "valid":
                    answer["turn"] = WR.turn_echo(request["turn"]["id"], ids, ids[:1])
                elif echo == "invalid":
                    answer["turn"] = WR.turn_echo(request["turn"]["id"], ids[1:], [])
            return answer

        module = SimpleNamespace(deliver_request=handle)
        if turns:
            module.TURNS = WR.TURN_CONTRACT
        patcher = mock.patch.dict(sys.modules, {TURN_MODULE: module})
        patcher.start()
        self.addCleanup(patcher.stop)

    def unreadable(self) -> dict:
        return {"contract": WR.TURN_CONTRACT, "id": TURN_ID, "unreadable": True}

    def test_deliver_remote_without_a_turn_sends_exactly_platform_target_wake(self) -> None:
        CW.deliver_remote(
            self.env, self.cfg, self.target, self.w, platform="muse", runner=self.remote
        )
        stdin = self.runs[1][1]["input"]
        expected = {
            "platform": "muse",
            "target": {"session_id": SID_A, "letter": "AA", "host": "mac", "generation": 3},
            "wake": asdict(self.w),
        }
        self.assertEqual(stdin, json.dumps(expected, ensure_ascii=False))

    def test_deliver_remote_turn_sends_a_compact_offer_under_the_stdin_limit(self) -> None:
        riders = tuple(self.rider(n) for n in (1, 2, 3))
        turn = CW.deliver_remote_turn(
            self.env, self.cfg, self.target, self.head(), riders, TURN_ID, platform="muse",
            runner=self.remote,
        )
        self.assertIsNone(turn.carried)  # this Mac's answer carries no echo
        stdin = self.runs[1][1]["input"]
        sent = json.loads(stdin)
        self.assertEqual(set(sent), {"platform", "target", "wake", "turn"})
        self.assertEqual(
            (sent["turn"]["contract"], sent["turn"]["id"]), (WR.TURN_CONTRACT, TURN_ID)
        )
        self.assertEqual([r["wake_id"] for r in sent["turn"]["riders"]], ["w-r1", "w-r2", "w-r3"])
        for record in sent["turn"]["riders"]:
            self.assertEqual(set(record), set(WR.TURN_MEMBER_FIELDS))
        offer = json.dumps(sent["turn"])
        for word in (BODY_MARK, '"topic"', '"sender"', '"zulip_link"', "OPS-1", "Sender "):
            self.assertNotIn(word, offer)
        self.assertNotIn("turn_dispatch", stdin)
        # The worst case:  seven riders with the longest ids and every head field at its
        # longest, in four-byte characters.
        big = "\U0001f600"
        head = self.head(
            wake_id="h" * 128,
            channel=big * WR.NAME_CHARS,
            topic=big * WR.NAME_CHARS,
            sender=big * WR.NAME_CHARS,
            zulip_link=big * 500,
            issue_ids=tuple("AFC-%07d" % n for n in range(WR.MAX_WAKE_ISSUES)),
            session_fragments=tuple(
                "%08x-0000-4000-8000-000000000000" % n for n in range(WR.MAX_FRAGMENTS)
            ),
        )
        riders = tuple(
            self.rider(n, wake_id=("r%d" % n).ljust(128, "x"), message_id=WR.MESSAGE_ID_MAX)
            for n in range(1, WR.TURN_MAX_WAKES)
        )
        self.runs.clear()
        CW.deliver_remote_turn(
            self.env, self.cfg, self.target, head, riders, TURN_ID, platform="muse",
            runner=self.remote,
        )
        stdin = self.runs[1][1]["input"]
        self.assertEqual(len(json.loads(stdin)["turn"]["riders"]), 7)
        self.assertLessEqual(len(stdin.encode("utf-8")), CW.STDIN_LIMIT - CW.TURN_STDIN_MARGIN)

    def test_the_turn_waits_the_resume_limit_once_per_member(self) -> None:
        resume = int(self.cfg["resume_timeout_seconds"])
        riders = (self.rider(1), self.rider(2))
        CW.deliver_remote_turn(
            self.env, self.cfg, self.target, self.head(), riders, TURN_ID, platform="muse",
            runner=self.remote,
        )
        self.assertEqual(self.runs[1][1]["timeout"], resume * 3 + CW.SSH_MARGIN_SECONDS)
        self.runs.clear()
        with mock.patch.object(CW, "TURN_STDIN_MARGIN", 16000):  # the head alone
            CW.deliver_remote_turn(
                self.env, self.cfg, self.target, self.head(), riders, TURN_ID, platform="muse",
                runner=self.remote,
            )
        self.assertEqual(self.runs[1][1]["timeout"], resume + CW.SSH_MARGIN_SECONDS)
        self.runs.clear()
        CW.deliver_remote(
            self.env, self.cfg, self.target, self.w, platform="muse", runner=self.remote
        )
        self.assertEqual(self.runs[1][1]["timeout"], resume + CW.SSH_MARGIN_SECONDS)

    def test_an_oversized_offer_falls_back_to_the_head_alone(self) -> None:
        riders = (self.rider(1), self.rider(2))
        with mock.patch.object(CW, "TURN_STDIN_MARGIN", 16000):
            turn = CW.deliver_remote_turn(
                self.env, self.cfg, self.target, self.head(), riders, TURN_ID, platform="claude",
                runner=self.remote,
            )
        self.assertEqual(set(json.loads(self.runs[1][1]["input"])), {"platform", "target", "wake"})
        self.assertEqual(
            (turn.delivery.outcome, turn.carried, turn.unreadable), ("sent", None, False)
        )

    def test_after_send_failures_make_the_turn_unreadable(self) -> None:
        good = SimpleNamespace(returncode=0, stdout="")
        cases = {
            "ssh-timeout": (subprocess.TimeoutExpired("ssh", 5), "ssh-timeout"),
            "ssh-error": (OSError("from communicate"), "ssh-error-OSError"),
            "ssh-dropped": (SimpleNamespace(returncode=255, stdout=""), "ssh-dropped"),
            "remote-exit": (SimpleNamespace(returncode=3, stdout="not json\n"), "remote-exit-3"),
            "remote-mismatch": (
                SimpleNamespace(returncode=0, stdout=self.reply(wake_id="w-other")),
                "remote-mismatch",
            ),
        }
        riders = (self.rider(1), self.rider(2))
        for label, (second, code) in cases.items():
            with self.subTest(label):
                turn = CW.deliver_remote_turn(
                    self.env, self.cfg, self.target, self.head(), riders, TURN_ID,
                    platform="claude", runner=self.scripted(good, second),
                )
                self.assertEqual(
                    (turn.delivery.outcome, turn.delivery.code, turn.unreadable, turn.carried),
                    ("refused", code, True, None),
                )

    def test_preflight_failures_and_remote_needs_update_return_riders(self) -> None:
        good = SimpleNamespace(returncode=0, stdout="")
        old_mac = SimpleNamespace(returncode=2, stdout=json.dumps(CW.OLD_MAC_REFUSAL) + "\n")
        cases = {
            "unreachable": ([SimpleNamespace(returncode=255, stdout="")], "ssh-unreachable"),
            "preflight timeout": ([subprocess.TimeoutExpired("ssh", 5)], "ssh-preflight-timeout"),
            "preflight oserror": ([FileNotFoundError()], "ssh-FileNotFoundError"),
            "preflight exit": ([SimpleNamespace(returncode=1, stdout="")], "ssh-preflight-exit-1"),
            "an old mac": ([good, old_mac], "remote-needs-update"),
        }
        riders = (self.rider(1), self.rider(2))
        for label, (answers, code) in cases.items():
            with self.subTest(label):
                turn = CW.deliver_remote_turn(
                    self.env, self.cfg, self.target, self.head(), riders, TURN_ID,
                    platform="claude", runner=self.scripted(*answers),
                )
                self.assertEqual(
                    (turn.delivery.code, turn.carried, turn.confirmed, turn.unreadable),
                    (code, None, (), False),
                )
        with self.subTest("no destination"):
            turn = CW.deliver_remote_turn(
                self.env, {**self.cfg, "mac_ssh": ""}, self.target, self.head(), riders,
                TURN_ID, platform="claude", runner=self.scripted(),
            )
            self.assertEqual(
                (turn.delivery.code, turn.carried, turn.unreadable),
                ("no-ssh-destination", None, False),
            )

    def test_the_dispatcher_strips_turn_and_sender_turn_dispatch_for_a_module_without_TURNS(
        self,
    ) -> None:
        self.install(turns=False)
        request = self.offered("turnplat")
        request["turn_dispatch"] = CONTRACT_TEXT  # a sender may not set this itself
        code, answer = self.run_main(request)
        self.assertEqual((code, answer["delivery"]["outcome"]), (0, "sent"))
        self.assertEqual(len(self.handled), 1)
        self.assertFalse({"turn", "turn_dispatch"} & set(self.handled[0]))
        self.assertNotIn("turn", answer)

    def test_the_dispatcher_marks_turn_dispatch_only_for_a_TURNS_module_with_a_valid_offer(
        self,
    ) -> None:
        self.install(turns=True)
        request = self.offered("turnplat")
        _code, answer = self.run_main(request)
        self.assertEqual(self.handled[-1]["turn_dispatch"], WR.TURN_CONTRACT)
        self.assertEqual(self.handled[-1]["turn"], request["turn"])
        self.assertIn("turn", answer)
        for label, bad in {
            "another contract": self.offered("turnplat", contract="simple-tools-turn/9"),
            "no riders": self.offered("turnplat", count=0),
            "a bad first rider": self.offered("turnplat", count=1),
        }.items():
            with self.subTest(label):
                if label == "a bad first rider":
                    bad["turn"]["riders"][0]["reason"] = "most-recent"
                bad["turn_dispatch"] = WR.TURN_CONTRACT
                self.handled.clear()
                _code, answer = self.run_main(bad)
                self.assertFalse({"turn", "turn_dispatch"} & set(self.handled[0]))
                self.assertNotIn("turn", answer)

    def test_the_dispatcher_turns_a_missing_invalid_or_raising_echo_into_unreadable(self) -> None:
        for echo in ("missing", "invalid", "raise"):
            with self.subTest(echo):
                self.install(turns=True, echo=echo)
                _code, answer = self.run_main(self.offered("turnplat"))
                self.assertEqual(answer["turn"], self.unreadable())
                if echo == "raise":
                    self.assertEqual(
                        (answer["delivery"]["outcome"], answer["delivery"]["code"]),
                        ("refused", "platform-error-RuntimeError"),
                    )

    def test_the_dispatcher_passes_a_valid_echo_through(self) -> None:
        self.install(turns=True)
        _code, answer = self.run_main(self.offered("turnplat"))
        self.assertEqual(
            answer["turn"],
            {
                "contract": WR.TURN_CONTRACT,
                "id": TURN_ID,
                "carried": ["w-r1", "w-r2"],
                "confirmed": ["w-r1"],
            },
        )

    def test_codex_and_minimax_handlers_still_see_exactly_three_keys_when_a_turn_is_sent(
        self,
    ) -> None:
        from simple_tools import codex_wake, minimax_wake

        seen: list[set] = []

        def spy(request, *, env):
            seen.append(set(request))
            target = request["target"]
            return {
                "wake_id": request["wake"]["wake_id"],
                "session_id": target["session_id"],
                "generation": target["generation"],
                "delivery": {"outcome": "busy", "ack": "unknown", "code": "", "note": ""},
            }

        for module, platform, session_id in (
            (codex_wake, "codex", SID_A),
            (minimax_wake, "minimax", "mvs_" + "a" * 32),
        ):
            with self.subTest(platform), mock.patch.object(module, "deliver_request", spy):
                request = self.offered(platform)
                request["target"]["session_id"] = session_id
                request["turn_dispatch"] = CONTRACT_TEXT
                _code, answer = self.run_main(request)
                self.assertEqual(seen[-1], {"platform", "target", "wake"})
                self.assertEqual(answer["delivery"]["outcome"], "busy")
                self.assertNotIn("turn", answer)

    def test_the_claude_handler_never_gets_a_turn_and_answers_no_echo(self) -> None:
        request = self.envelope()
        request["turn"] = {"contract": CONTRACT_TEXT, "id": TURN_ID, "riders": [self.record(1)]}
        request["turn_dispatch"] = CONTRACT_TEXT
        with mock.patch.object(
            CW, "_deliver_here", return_value=WR.Delivery("sent", "socket")
        ) as here:
            code, answer = self.run_main(request)
        self.assertEqual((code, answer["delivery"]["outcome"]), (0, "sent"))
        self.assertNotIn("turn", answer)
        here.assert_called_once()
        self.assertEqual(here.call_args.args[2].wake_id, self.w.wake_id)

    def test_end_to_end_over_the_wire_with_a_fake_turn_module(self) -> None:
        self.install(turns=True)
        riders = (self.rider(1), self.rider(2))
        turn = CW.deliver_remote_turn(
            self.env, self.cfg, self.target, self.head(), riders, TURN_ID, platform="turnplat",
            runner=self.over_the_wire,
        )
        self.assertEqual((turn.delivery.outcome, turn.delivery.route), ("sent", "ssh:exec"))
        self.assertEqual(
            (turn.carried, turn.confirmed, turn.unreadable), (("w-r1", "w-r2"), ("w-r1",), False)
        )
        handled = self.handled[0]
        self.assertEqual(handled["turn_dispatch"], WR.TURN_CONTRACT)
        self.assertEqual([r["wake_id"] for r in handled["turn"]["riders"]], ["w-r1", "w-r2"])

    def test_a_mac_outage_comes_back_as_the_outage_with_no_rider_carried(self) -> None:
        self.install(turns=True, echo="outage")
        riders = (self.rider(1), self.rider(2))
        turn = CW.deliver_remote_turn(
            self.env, self.cfg, self.target, self.head(), riders, TURN_ID, platform="turnplat",
            runner=self.over_the_wire,
        )
        self.assertEqual(
            (turn.delivery.outcome, turn.delivery.code), ("busy", WR.REGISTRY_UNAVAILABLE)
        )
        # The module ran nothing and said so with no echo:  no rider counts as carried, and the
        # router waits the outage out with the head (MergedRules).
        self.assertEqual((turn.carried, turn.confirmed, turn.unreadable), (None, (), True))

    def test_a_lost_loader_on_the_mac_hands_the_module_no_turn(self) -> None:
        self.install(turns=True)
        riders = (self.rider(1), self.rider(2))
        with mock.patch.object(WR, "registry_loader_lost", return_value=True):
            turn = CW.deliver_remote_turn(
                self.env, self.cfg, self.target, self.head(), riders, TURN_ID,
                platform="turnplat", runner=self.over_the_wire,
            )
        self.assertEqual(self.handled, [])  # nothing reached the handler
        self.assertEqual(
            (turn.delivery.outcome, turn.delivery.code, turn.delivery.note),
            ("failed", WR.NOT_CONFIGURED, WR.REGISTRY_LOADER_SOURCE),
        )
        self.assertEqual((turn.carried, turn.confirmed, turn.unreadable), (None, (), False))

    def test_an_answer_without_a_turn_echo_returns_riders(self) -> None:
        riders = (self.rider(1), self.rider(2))
        turn = CW.deliver_remote_turn(
            self.env, self.cfg, self.target, self.head(), riders, TURN_ID, platform="claude",
            runner=self.remote,  # a Mac from before turns:  its answer has no echo
        )
        self.assertEqual(
            (turn.delivery.outcome, turn.carried, turn.confirmed, turn.unreadable),
            ("sent", None, (), False),
        )


if __name__ == "__main__":
    unittest.main()
