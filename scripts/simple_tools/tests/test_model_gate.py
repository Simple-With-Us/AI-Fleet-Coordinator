"""The model gate, phase 1 (record only):  every model start records why a model was needed, and nothing
else about the start changes.  The pure verdict table, the ledger fields and their restore, the counts,
the daemon starts (primary, fallback, chat inspection, routine), a gate that
fails, and a replay showing the same message sequence decides the same with the gate, without it and
with it broken.
"""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
import subprocess
import tempfile
import textwrap
import time
import types
import unittest
from collections import Counter
from typing import Any
from unittest import mock

from simple_tools import adapters as A
from simple_tools import daemon as D
from simple_tools import listener_cli as LC
from simple_tools import live as L
from simple_tools import router as R
from simple_tools import wakes as W
from simple_tools.tests.harness import write_rc
from simple_tools.tests.listener_harness import ListenerHarness, reply_output
from simple_tools.tests.test_chat_inspection import (
    CHAT_ID,
    FakeCatalog,
    FakeMuseRunner,
    final_output,
    inspect_output,
    offered,
)
from simple_tools.tests.test_daemon import MENTION, DaemonHarness
from simple_tools.tests.test_openrouter_daemon import FALLBACK, OpenRouterHarness
from simple_tools.tests.test_openrouter_wake import FALLBACK_MODEL
from simple_tools.tests.test_server import SEAT as ROUTINE_SEAT
from simple_tools.tests.test_server import ServerHarness


class Boom:
    """A runner that fails the test the moment anything starts a model."""

    def run(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a model was started")

    deliver = run


class GateLog:
    """The `model-gate` events of a listener test's log."""

    def gate_events(self) -> list[dict[str, Any]]:
        path = self.state_dir / "logs" / "listener.log"
        text = path.read_text() if path.exists() else ""
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        return [row for row in rows if row.get("event") == "model-gate"]


# --------------------------------------------------------------------------------------------
# The pure verdict
# --------------------------------------------------------------------------------------------

TABLE = [
    # (label, purpose, facts, code, avoidable)
    ("eyes marker", "compose_reply", {"eyes": True}, "det_eyes", True),
    ("live lease", "compose_reply", {"live_lease": True}, "det_live_lease", True),
    ("bound chat", "routine", {"bound_chat": True}, "det_bound_chat", True),
    (
        "tagged letter, one candidate on the tagged seat",
        "compose_reply",
        {"letter": "Z", "letter_candidates": 1, "letter_holder_on_seat": True},
        "det_tagged_letter_single",
        True,
    ),
    (
        "exact chat id, no risk answer needed",
        "select_destination",
        {"exact_chat_ids": 1, "risk_screen_needed": False},
        "det_exact_chat_id",
        True,
    ),
    ("a routine forward is a delivery", "routine", {}, "det_routine_forward", False),
    (
        "tagged letter, two candidates",
        "compose_reply",
        {"letter": "Z", "letter_candidates": 2},
        "model_letter_ambiguous",
        False,
    ),
    (
        "tagged letter, three candidates, destination",
        "select_destination",
        {"letter": "Z", "letter_candidates": 3},
        "model_letter_ambiguous",
        False,
    ),
    (
        "letter, one candidate, but not on the tagged seat",
        "compose_reply",
        {"letter": "Z", "letter_candidates": 1, "letter_holder_on_seat": False},
        "model_compose_reply",
        False,
    ),
    (
        "letter, one candidate, holder's seat not known",
        "select_destination",
        {"letter": "Z", "letter_candidates": 1},
        "model_select_destination",
        False,
    ),
    ("letter, candidates not known", "compose_reply", {"letter": "Z"}, "model_compose_reply", False),
    (
        "letter nobody holds",
        "select_destination",
        {"letter": "Z", "letter_candidates": 0},
        "model_select_destination",
        False,
    ),
    (
        "exact chat id, but a risk answer is needed (a peer)",
        "select_destination",
        {"exact_chat_ids": 1, "risk_screen_needed": True},
        "model_select_destination",
        False,
    ),
    (
        "exact chat id, risk need not known",
        "select_destination",
        {"exact_chat_ids": 1},
        "model_select_destination",
        False,
    ),
    (
        "two exact ids",
        "select_destination",
        {"exact_chat_ids": 2, "risk_screen_needed": False},
        "model_select_destination",
        False,
    ),
    ("a seat reply", "compose_reply", {}, "model_compose_reply", False),
    ("an unresolved addressee", "select_destination", {}, "model_select_destination", False),
    ("a chat inspection", "inspect", {}, "model_select_destination", False),
    ("a fallback", "fallback", {}, "model_fallback", False),
    # A fact that is not exactly True, or a count that is not an int, is not known.
    (
        "truthy but not True",
        "compose_reply",
        {"eyes": "yes", "live_lease": 1, "bound_chat": "x"},
        "model_compose_reply",
        False,
    ),
    (
        "a bool is not a count",
        "select_destination",
        {"exact_chat_ids": True, "risk_screen_needed": False},
        "model_select_destination",
        False,
    ),
    (
        "a negative count",
        "select_destination",
        {"exact_chat_ids": -1, "risk_screen_needed": False},
        "model_select_destination",
        False,
    ),
    (
        "a risk need that is not exactly False",
        "select_destination",
        {"exact_chat_ids": 1, "risk_screen_needed": 0},
        "model_select_destination",
        False,
    ),
    (
        "a blank letter",
        "compose_reply",
        {"letter": "  ", "letter_candidates": 1, "letter_holder_on_seat": True},
        "model_compose_reply",
        False,
    ),
    # A deterministic rule still wins for a routine forward:  a forward that a rule says should not
    # have happened is recorded as that rule, and avoidable.
    ("a forward with an eyes marker", "routine", {"eyes": True}, "det_eyes", True),
]


class VerdictTableTests(unittest.TestCase):
    """`model_gate_verdict` is a function of its two arguments:  facts in, a verdict out, nothing started."""

    def test_every_situation_gets_its_code_and_avoidable_flag(self) -> None:
        for label, purpose, facts, code, avoidable in TABLE:
            with self.subTest(label):
                verdict = W.model_gate_verdict(purpose, facts)
                self.assertEqual((verdict.code, verdict.avoidable), (code, avoidable))
                self.assertEqual(verdict.rule, W.GATE_RULES[code])
                # Every det_* code is avoidable except a delivery, which is never a rule a model start
                # could have been replaced by.
                self.assertEqual(
                    verdict.avoidable,
                    verdict.code.startswith("det_") and verdict.code not in W.GATE_DELIVERY_CODES,
                )
                self.assertIn(verdict.code, W.GATE_CODES)

    def test_the_table_covers_every_deterministic_and_every_model_code_but_the_markers(self) -> None:
        seen = {row[3] for row in TABLE}
        every = {c for c in W.GATE_CODES if c.startswith(("det_", "model_"))}
        self.assertEqual(seen, every)
        self.assertEqual(set(W.GATE_CODES) - every, {"gate_error"})
        for gone in ("bridge_consult", "model_bridge_consult", "no_model", "det_native_only"):
            self.assertNotIn(gone, set(W.GATE_CODES) | set(W.GATE_PURPOSES), "the Muse inbox bridge was deleted")

    def test_every_purpose_is_accepted_and_nothing_else_is(self) -> None:
        for purpose in W.GATE_PURPOSES:
            self.assertIsInstance(W.model_gate_verdict(purpose, {}), W.GateVerdict)
        for bad in ("", "reply", None, 3, "COMPOSE_REPLY"):
            with self.assertRaises(ValueError):
                W.model_gate_verdict(bad, {})  # type: ignore[arg-type]

    def test_the_first_rule_that_applies_wins_in_the_documented_order(self) -> None:
        everything = {
            "eyes": True,
            "live_lease": True,
            "bound_chat": True,
            "letter": "Z",
            "letter_candidates": 1,
            "letter_holder_on_seat": True,
            "exact_chat_ids": 1,
            "risk_screen_needed": False,
        }
        order = [
            (("eyes",), "det_eyes"),
            (("live_lease",), "det_live_lease"),
            (("bound_chat",), "det_bound_chat"),
            (("letter", "letter_candidates", "letter_holder_on_seat"), "det_tagged_letter_single"),
            (("exact_chat_ids",), "det_exact_chat_id"),
        ]
        facts = dict(everything)
        for names, code in order:
            self.assertEqual(W.model_gate_verdict("select_destination", facts).code, code)
            for name in names:
                facts.pop(name)
        self.assertEqual(W.model_gate_verdict("select_destination", facts).code, "model_select_destination")

    def test_the_facts_are_not_changed_and_none_or_junk_facts_are_not_known(self) -> None:
        facts = {"eyes": True, "letter": "Z"}
        before = json.dumps(facts, sort_keys=True)
        W.model_gate_verdict("compose_reply", facts)
        self.assertEqual(json.dumps(facts, sort_keys=True), before)
        for junk in (None, "eyes", ["eyes"], 7):
            self.assertEqual(
                W.model_gate_verdict("compose_reply", junk).code,  # type: ignore[arg-type]
                "model_compose_reply",
            )

    def test_a_fallback_verdict_names_its_reason_and_only_a_known_one(self) -> None:
        for reason in W.GATE_FALLBACK_REASONS:
            verdict = W.model_gate_verdict("fallback", {"fallback_reason": reason})
            self.assertEqual((verdict.code, verdict.rule), ("model_fallback", reason))
        self.assertEqual(
            W.model_gate_verdict("fallback", {"fallback_reason": "made up"}).rule, "primary_failed"
        )

    def test_the_verdict_is_immutable_and_its_tokens_are_bounded(self) -> None:
        verdict = W.model_gate_verdict("compose_reply", {})
        with self.assertRaises(AttributeError):
            verdict.code = "det_eyes"  # type: ignore[misc]
        for code, rule in W.GATE_RULES.items():
            self.assertRegex(code, r"[a-z][a-z0-9_]{0,39}")
            self.assertRegex(rule, r"[a-z][a-z0-9_]{0,59}")

    def test_the_safe_wrapper_turns_any_failure_into_gate_error(self) -> None:
        self.assertEqual(
            W.gate_verdict_safe("nope", {}), W.GateVerdict("gate_error", False, "gate_failed")
        )
        with mock.patch.object(W, "model_gate_verdict", side_effect=RuntimeError("boom")):
            self.assertEqual(W.gate_verdict_safe("compose_reply", {}).code, "gate_error")
        self.assertEqual(W.gate_verdict_safe("compose_reply", {}).code, "model_compose_reply")


# --------------------------------------------------------------------------------------------
# The facts, read from a real daemon whose every runner fails the test if it is called
# --------------------------------------------------------------------------------------------


class FactSituationTests(GateLog, DaemonHarness):
    def setUp(self) -> None:
        super().setUp()
        self.daemon_ = self.daemon(
            claude_runner=Boom(),
            muse_runner=Boom(),
            mcode_runner=Boom(),
            openrouter_runner=Boom(),
            routine_runner=Boom(),
        )
        self.runner = self.daemon_.seats["CLAUDE"]
        for seat in ("CURSOR", "CODEX"):  # destinations a listed chat can belong to
            self.daemon_.seats[seat] = types.SimpleNamespace(
                seat=seat, cfg=types.SimpleNamespace(enabled=True, wake="inbox")
            )
            self.addCleanup(self.daemon_.seats.pop, seat)  # shutdown disconnects every real seat

    def pending(
        self, *, owner: bool = False, ids: tuple = (5,), kind: str = "stream"
    ) -> W.Pending:
        pending = W.Pending(
            "w1",
            "CLAUDE",
            {"channel": "simple-tools", "topic": "t", "type": kind},
            owner=owner,
            now=1.0,
            due=2.0,
        )
        for message_id in ids:
            pending.add({"id": message_id}, owner)
        return pending

    def verdict(
        self,
        purpose: str,
        text: str = "hi",
        *,
        pending: W.Pending | None = None,
        history: list | None = None,
        sender_email: str | None = None,
        **kw: Any,
    ) -> W.GateVerdict:
        message: dict[str, Any] = {"id": 5, "content": text}
        if sender_email:
            message["sender_email"] = sender_email
        return self.daemon_._model_gate(
            self.runner,
            pending or self.pending(),
            purpose,
            history=[message] if history is None else history,
            **kw,
        )

    # ---- the eyes marker --------------------------------------------------------------------
    def test_an_eyes_marker_in_the_trigger_is_det_eyes(self) -> None:
        for text in (":eyes: not for the seat", "  :eyes: leading space", "\U0001f440 look"):
            self.assertEqual(self.verdict("compose_reply", text).code, "det_eyes", text)

    def test_an_eyes_in_the_middle_of_the_text_is_prose_and_a_non_trigger_is_not_read(self) -> None:
        self.assertEqual(self.verdict("compose_reply", "a :eyes: b").code, "model_compose_reply")
        code = self.verdict(
            "compose_reply",
            history=[{"id": 4, "content": ":eyes: an older message"}, {"id": 5, "content": "hi"}],
        ).code
        self.assertEqual(code, "model_compose_reply", "only a trigger message is read")

    def test_a_coalesced_wake_is_eyes_only_when_every_trigger_is_marked(self) -> None:
        both = self.pending(ids=(5, 6))
        marked = {"id": 5, "content": ":eyes: one"}
        plain = {"id": 6, "content": "a plain message that did wake the seat"}
        also = {"id": 6, "content": ":eyes: two"}
        self.assertEqual(
            self.verdict("compose_reply", pending=both, history=[marked, plain]).code,
            "model_compose_reply",
            "one marked trigger does not make the wake avoidable",
        )
        self.assertEqual(
            self.verdict("compose_reply", pending=both, history=[marked, also]).code, "det_eyes"
        )
        self.assertEqual(
            self.verdict("compose_reply", pending=both, history=[]).code,
            "model_compose_reply",
            "no trigger text found is not a marker",
        )

    def test_the_marker_is_read_with_the_senders_seat_tag_like_the_router(self) -> None:
        text = "[MM] :eyes: a seat bot's own signal"
        self.assertEqual(
            self.verdict("compose_reply", text, sender_email="mm-bot@zulip.test").code, "det_eyes"
        )
        self.assertEqual(self.verdict("compose_reply", text).code, "model_compose_reply")
        self.assertEqual(
            self.verdict("compose_reply", text, sender_email="codex-bot@zulip.test").code,
            "model_compose_reply",
            "a bracket that is another seat's tag is not the sender's signal",
        )

    # ---- the tagged letter ------------------------------------------------------------------
    def test_a_tagged_letter_is_single_on_the_seat_ambiguous_elsewhere_or_unheld(self) -> None:
        cases = [
            (1, True, "det_tagged_letter_single"),
            (1, False, "model_compose_reply"),  # one holder, but on another seat or platform
            (2, False, "model_letter_ambiguous"),
            (0, False, "model_compose_reply"),
            (None, None, "model_compose_reply"),  # the registry was never asked
        ]
        for candidates, on_seat, code in cases:
            with self.subTest(candidates=candidates, on_seat=on_seat):
                pending = self.pending()
                pending.letter_candidates = candidates
                pending.letter_holder_on_seat = on_seat
                self.assertEqual(
                    self.verdict("compose_reply", "@**Claude** Z hi", pending=pending, letter="Z").code,
                    code,
                )

    def test_the_gate_never_opens_the_registry_itself(self) -> None:
        pending = self.pending()
        pending.letter_candidates, pending.letter_holder_on_seat = 1, True
        with mock.patch.object(
            self.daemon_, "operator_candidates", side_effect=AssertionError("a second registry open")
        ):
            verdict = self.verdict("compose_reply", "@**Claude** Z hi", pending=pending, letter="Z")
        self.assertEqual(verdict.code, "det_tagged_letter_single")

    def test_the_step_that_asks_the_registry_hands_its_answer_to_the_gate(self) -> None:
        mine = {"seat": "CLAUDE", "platform": "claude"}
        other = {"seat": "MM", "platform": "minimax"}
        for found, expected in (
            ([mine], (1, True)),
            ([other], (1, False)),
            ([mine, other], (2, False)),
            ([], (0, False)),
            # a registry row that is not a mapping:  the count is still known, the holder is not, and
            # the ask itself neither raises nor changes (it returned at once for one candidate before)
            ([None], (1, None)),
            ([("CLAUDE",)], (1, None)),
            ([{"seat": None}], (1, False)),
        ):
            with self.subTest(found=found):
                pending = self.pending()
                with mock.patch.object(
                    self.daemon_, "operator_candidates", return_value=found
                ) as asked:
                    answer = self.daemon_.ask_operator_for_candidates(
                        self.runner, pending, "Z", "", 11
                    )
                self.assertIsNone(answer)
                self.assertEqual(asked.call_count, 1, "one registry open for the whole wake")
                self.assertEqual((pending.letter_candidates, pending.letter_holder_on_seat), expected)
                # and the gate reads "holder not known" as no deterministic rule
                kw: dict[str, Any] = dict(messages=[], letter="Z", chat_context=None)
                facts = self.daemon_._gate_facts(self.runner, pending, **kw)
                self.assertEqual(
                    W.model_gate_verdict("compose_reply", facts).code == "det_tagged_letter_single",
                    expected == (1, True),
                )

    # ---- a live lease -----------------------------------------------------------------------
    def test_a_live_lease_fact_is_det_live_lease_and_the_gate_probes_nothing(self) -> None:
        with (
            mock.patch.object(self.daemon_, "takeover", side_effect=AssertionError("probed")),
            mock.patch.object(self.daemon_, "lease_views", side_effect=AssertionError("probed")),
        ):
            verdict = self.verdict("fallback", live_lease=True)
            self.assertEqual((verdict.code, verdict.avoidable), ("det_live_lease", True))
            self.assertEqual(self.verdict("fallback").code, "model_fallback")

    def test_a_live_lease_is_not_known_unless_the_caller_states_it(self) -> None:
        pending = self.pending()
        kw: dict[str, Any] = dict(messages=[], letter="", chat_context=None)
        self.assertNotIn("live_lease", self.daemon_._gate_facts(self.runner, pending, **kw))
        self.assertIs(
            self.daemon_._gate_facts(self.runner, pending, live_lease=False, **kw)["live_lease"], False
        )

    # ---- an exact chat id -------------------------------------------------------------------
    def test_a_listed_chat_id_is_det_exact_chat_id_only_for_one_legal_destination_and_no_risk_screen(
        self,
    ) -> None:
        row = offered()
        other = dict(offered(), id="b" * 64)
        text = "please wake %s now" % CHAT_ID
        owner = self.pending(owner=True)
        peer = self.pending(owner=False)
        self.assertEqual(
            self.verdict("select_destination", text, pending=owner, chat_context=[row, other]).code,
            "det_exact_chat_id",
        )
        self.assertEqual(
            self.verdict("select_destination", text, pending=peer, chat_context=[row, other]).code,
            "model_select_destination",
            "a peer-triggered selection still needs the model's risk answer",
        )
        both = "wake %s and %s" % (CHAT_ID, "b" * 64)
        self.assertEqual(
            self.verdict("select_destination", both, pending=owner, chat_context=[row, other]).code,
            "model_select_destination",
        )
        self.assertEqual(
            self.verdict("select_destination", "no id here", pending=owner, chat_context=[row]).code,
            "model_select_destination",
        )

    def test_a_chat_that_is_not_a_legal_destination_is_never_counted(self) -> None:
        text = "please wake %s now" % CHAT_ID
        owner = self.pending(owner=True)
        illegal = {
            "stale": offered(stale=True),
            "not wake-capable": dict(offered(), capabilities={"read": True, "wake": False}),
            "stale not known": dict(offered(), stale=None),
            "seat not enabled on this listener": dict(offered(), seat="GROK"),
            "seat not on this listener at all": dict(offered(), seat="NOSUCH"),
        }
        for label, row in illegal.items():
            with self.subTest(label):
                self.assertEqual(
                    self.verdict("select_destination", text, pending=owner, chat_context=[row]).code,
                    "model_select_destination",
                )
        self.daemon_.seats["CURSOR"].cfg.enabled = False
        self.assertEqual(
            self.verdict("select_destination", text, pending=owner, chat_context=[offered()]).code,
            "model_select_destination",
            "a seat that is switched off cannot be the destination",
        )

    def test_a_private_message_may_only_name_a_chat_of_its_own_seat(self) -> None:
        text = "wake %s" % CHAT_ID
        direct = self.pending(owner=True, kind="private")
        cross = offered()  # a CURSOR chat, while this is the CLAUDE seat's direct message
        own = dict(offered(), seat="CLAUDE")
        self.assertEqual(
            self.verdict("select_destination", text, pending=direct, chat_context=[cross]).code,
            "model_select_destination",
            "a private message is not readable by a different seat",
        )
        self.assertEqual(
            self.verdict("select_destination", text, pending=direct, chat_context=[own]).code,
            "det_exact_chat_id",
        )

    # ---- a bound chat -----------------------------------------------------------------------
    def test_a_topic_bound_to_a_chat_is_det_bound_chat_and_the_topic_is_compared_the_way_the_router_does(
        self,
    ) -> None:
        pending = self.pending()
        binding = {"channel": "Simple-Tools", "topic": "T", "platform": "codex"}
        queue = types.SimpleNamespace(
            snapshot=lambda: {"bindings": {L.topic_key("simple-tools", "t"): binding}, "items": []}
        )
        codex = types.SimpleNamespace(seat="CODEX")
        with mock.patch.object(self.daemon_, "chat_queue", return_value=queue):
            self.assertEqual(
                self.daemon_._model_gate(codex, pending, "routine", messages=[{"content": "hi"}]).code,
                "det_bound_chat",
                "a casefolded channel and topic are the same topic, as in ChatNudges.enqueue",
            )
            pending.topic = "elsewhere"
            self.assertEqual(
                self.daemon_._model_gate(codex, pending, "routine", messages=[{"content": "hi"}]).code,
                "det_routine_forward",
            )
            pending.topic = "t"
            direct = self.pending(kind="private")
            self.assertEqual(
                self.daemon_._model_gate(codex, direct, "routine", messages=[{"content": "hi"}]).code,
                "det_routine_forward",
                "a direct message has no channel topic to bind",
            )
            self.assertEqual(
                self.daemon_._model_gate(
                    self.runner, pending, "compose_reply", messages=[{"content": "hi"}]
                ).code,
                "model_compose_reply",
                "the CLAUDE seat has no chat bindings to consult",
            )

    # ---- the rest ---------------------------------------------------------------------------
    def test_a_plain_mention_is_a_reply_composition(self) -> None:
        verdict = self.verdict("compose_reply", MENTION)
        self.assertEqual(
            verdict, W.GateVerdict("model_compose_reply", False, "reply_composition_is_the_model_job")
        )

    def test_a_peer_trigger_needs_a_risk_answer_and_an_owner_trigger_does_not(self) -> None:
        kw: dict[str, Any] = dict(messages=[], letter="", chat_context=None)
        peer = self.daemon_._gate_facts(self.runner, self.pending(owner=False), **kw)
        owner = self.daemon_._gate_facts(self.runner, self.pending(owner=True), **kw)
        self.assertIs(peer["risk_screen_needed"], True)
        self.assertIs(owner["risk_screen_needed"], False)

    def test_a_broadcast_trigger_skips_the_bound_chat_fact_and_the_nudge_file_is_not_read(self) -> None:
        pending = self.pending()
        binding = {"channel": "simple-tools", "topic": "t", "platform": "codex"}
        queue = types.SimpleNamespace(
            snapshot=lambda: {"bindings": {L.topic_key("simple-tools", "t"): binding}, "items": []}
        )
        codex = types.SimpleNamespace(seat="CODEX")
        with mock.patch.object(self.daemon_, "chat_queue", return_value=queue) as asked:
            for text in ("@**all** please look", "@everyone look", "heads up @channel"):
                verdict = self.daemon_._model_gate(codex, pending, "routine", messages=[{"content": text}])
                self.assertEqual(verdict.code, "det_routine_forward", text)
            self.assertEqual(asked.call_count, 0, "a broadcast never reads the nudge file")
            plain = self.daemon_._model_gate(codex, pending, "routine", messages=[{"content": "look"}])
            self.assertEqual(plain.code, "det_bound_chat")
            self.assertEqual(asked.call_count, 1)

    def test_the_gate_leaves_the_pending_the_history_and_the_chat_context_alone(self) -> None:
        pending = self.pending(owner=True, ids=(5, 6))
        pending.letter_candidates, pending.letter_holder_on_seat = 2, False
        history = [
            {"id": 4, "content": "older"},
            {"id": 5, "content": ":eyes: one " + CHAT_ID, "sender_email": "mm-bot@zulip.test"},
            {"id": 6, "content": "two"},
        ]
        chat_context = [offered(), dict(offered(), id="b" * 64, capabilities={"read": True, "wake": False})]
        before = copy.deepcopy((vars(pending), history, chat_context))
        for purpose in W.GATE_PURPOSES:
            for kind in ("stream", "private"):
                pending.type = kind
                self.daemon_._model_gate(
                    self.runner,
                    pending,
                    purpose,
                    history=history,
                    letter="Z",
                    chat_context=chat_context,
                    live_lease=False,
                    fallback_reason="primary_failed",
                )
                # after EVERY call, not only at the end:  a gate that reversed the triggers on each
                # call would put them back after an even number of calls
                self.assertEqual(pending.trigger_ids, [5, 6], (purpose, kind))
                self.assertEqual(pending.owner_ids, [5, 6], (purpose, kind))
        pending.type = before[0]["type"]
        self.assertEqual(vars(pending), before[0], "the pending, trigger order included")
        self.assertEqual(history, before[1])
        self.assertEqual(chat_context, before[2])

    def test_no_situation_started_a_model(self) -> None:
        # Every runner of this daemon raises when called:  computing a verdict never reaches one.
        self.verdict("compose_reply", ":eyes: x")
        self.verdict("fallback", "hi", fallback_reason="primary_failed")
        self.assertEqual(self.gate_events(), [], "computing a verdict logs nothing:  only a start does")


# --------------------------------------------------------------------------------------------
# The ledger fields and their restore
# --------------------------------------------------------------------------------------------


def pending_for(seat: str = "CLAUDE") -> W.Pending:
    pending = W.Pending(
        "w9", seat, {"channel": "simple-tools", "topic": "t", "type": "stream"}, owner=False, now=10.0, due=11.0
    )
    pending.add({"id": 7, "ts": 9.0, "sender_id": 11}, False)
    return pending


class PendingRowTests(unittest.TestCase):
    def test_a_pending_with_no_gate_writes_none_of_the_fields(self) -> None:
        row = pending_for().row("queued", 10.0)
        self.assertFalse([k for k in row if k.startswith("gate")])

    def test_the_primary_fields_ride_on_every_row_after_the_start(self) -> None:
        pending = pending_for()
        pending.note_gate(W.GateVerdict("det_tagged_letter_single", True, "x"))
        for state in ("started", "done", "failed"):
            row = pending.row(state, 11.0)
            self.assertEqual((row["gate"], row["gate_avoidable"]), ("det_tagged_letter_single", True))
            self.assertNotIn("gate_fallback", row)
            self.assertNotIn("gate_inspect", row)

    def test_the_fallback_row_keeps_the_primary_code_and_adds_its_own_reason(self) -> None:
        pending = pending_for()
        pending.note_gate(W.GateVerdict("model_compose_reply", False, "x"))
        pending.note_gate_fallback(W.GateVerdict("model_fallback", False, "primary_failed"), "primary_failed")
        pending.note_gate_inspect(W.GateVerdict("model_select_destination", False, "x"))
        row = pending.row("done", 12.0)
        self.assertEqual(
            {k: v for k, v in row.items() if k.startswith("gate")},
            {
                "gate": "model_compose_reply",
                "gate_avoidable": False,
                "gate_fallback": "model_fallback",
                "gate_fallback_reason": "primary_failed",
                "gate_inspect": "model_select_destination",
            },
        )

    def test_the_letter_hand_off_between_two_steps_is_never_written_to_the_ledger(self) -> None:
        pending = pending_for()
        pending.note_gate(W.GateVerdict("model_compose_reply", False, "x"))
        pending.letter_candidates, pending.letter_holder_on_seat = 2, False
        row = pending.row("started", 11.0)
        self.assertFalse([k for k in row if k.startswith("letter")])
        self.assertIsNone(W.Pending.from_view(row).letter_candidates)

    def test_an_explicit_extra_still_reaches_the_row(self) -> None:
        pending = pending_for()
        pending.note_gate(W.GateVerdict("model_compose_reply", False, "x"))
        self.assertEqual(pending.row("done", 12.0, reason="r")["reason"], "r")

    def test_a_wake_restored_from_the_ledger_keeps_the_fields_it_was_written_with(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ledger = W.Ledger(os.path.join(tmp, "wakes.jsonl"))
            pending = pending_for()
            ledger.append(pending.row("queued", 10.0))
            pending.note_gate(W.GateVerdict("det_exact_chat_id", True, "x"))
            pending.note_gate_fallback(W.GateVerdict("model_fallback", False, "x"), "primary_unavailable")
            pending.note_gate_inspect(W.GateVerdict("model_select_destination", False, "x"))
            ledger.append(pending.row("started", 11.0))
            # A new Ledger over the same file is a restart:  nothing is held in memory.
            view = W.Ledger(ledger.path).wakes()["w9"]
            restored = W.Pending.from_view(view)
            self.assertEqual(
                (
                    restored.gate,
                    restored.gate_avoidable,
                    restored.gate_fallback,
                    restored.gate_fallback_reason,
                    restored.gate_inspect,
                ),
                (
                    "det_exact_chat_id",
                    True,
                    "model_fallback",
                    "primary_unavailable",
                    "model_select_destination",
                ),
            )
            again = restored.row("done", 12.0)
            self.assertEqual(
                {k: v for k, v in again.items() if k.startswith("gate")},
                {k: v for k, v in view.items() if k.startswith("gate")},
            )

    def test_a_row_from_before_the_gate_restores_with_none_and_junk_is_dropped(self) -> None:
        old = W.Pending.from_view({"wake_id": "w1", "seat": "CLAUDE", "state": "accepted", "ts": 1.0})
        self.assertEqual((old.gate, old.gate_avoidable, old.gate_fallback, old.gate_inspect), (None,) * 4)
        junk = W.Pending.from_view(
            {
                "wake_id": "w2",
                "seat": "CLAUDE",
                "state": "accepted",
                "ts": 1.0,
                "gate": "Not A Token!",
                "gate_avoidable": "yes",
                "gate_fallback": 7,
                "gate_inspect": ["x"],
            }
        )
        self.assertEqual((junk.gate, junk.gate_avoidable, junk.gate_fallback, junk.gate_inspect), (None,) * 4)
        self.assertFalse([k for k in junk.row("queued", 2.0) if k.startswith("gate")])


# --------------------------------------------------------------------------------------------
# The counts
# --------------------------------------------------------------------------------------------

NOW = 1_000_000.0


def started(seat: str, ago: float, gate: str | None, avoidable: bool | None = None, **extra: Any) -> dict:
    row: dict[str, Any] = {"ts": NOW - ago, "wake_id": "w%s" % ago, "seat": seat, "state": "started"}
    if gate is not None:
        row["gate"] = gate
    if avoidable is not None:
        row["gate_avoidable"] = avoidable
    row.update(extra)
    return row


class GateCountsTests(unittest.TestCase):
    def test_codes_and_avoidable_starts_are_counted_per_seat_inside_the_window(self) -> None:
        rows = [
            started("CLAUDE", 100, "model_compose_reply", False),
            started("CLAUDE", 200, "model_compose_reply", False),
            started("CLAUDE", 300, "det_tagged_letter_single", True),
            started("MM", 400, "model_select_destination", False),
            started("MM", 500, "det_exact_chat_id", True),
            started("MM", W.DAY + 1, "model_compose_reply", False),  # outside the window
        ]
        counts = W.gate_counts(rows, NOW, W.DAY)
        self.assertEqual(
            counts["CLAUDE"],
            {
                "starts": 3,
                "avoidable": 1,
                "codes": {"model_compose_reply": 2, "det_tagged_letter_single": 1},
                "fallbacks": 0,
                "inspects": 0,
                "forwards": 0,
                "forwards_avoidable": 0,
            },
        )
        self.assertEqual((counts["MM"]["starts"], counts["MM"]["avoidable"]), (2, 1))
        self.assertEqual(sorted(counts), ["CLAUDE", "MM"])

    def test_the_window_edges(self) -> None:
        rows = [started("A", 3599.0, "model_compose_reply"), started("A", 3600.0, "model_compose_reply")]
        self.assertEqual(W.gate_counts(rows, NOW, 3600.0)["A"]["starts"], 1)
        self.assertEqual(W.gate_counts(rows, NOW, 3601.0)["A"]["starts"], 2)
        future = [started("A", -5.0, "model_compose_reply")]
        self.assertEqual(W.gate_counts(future, NOW, W.DAY), {}, "a row from the future is not counted")

    def test_rows_without_the_field_or_that_are_not_starts_are_ignored(self) -> None:
        rows = [
            started("CLAUDE", 10, None),  # a row from before the gate
            started("CLAUDE", 20, "model_compose_reply", False, state="queued"),
            started("CLAUDE", 30, "model_compose_reply", False, state="accepted"),
            {"ts": NOW - 40, "seat": "CLAUDE", "state": "done", "gate": "model_compose_reply"},
            {"ts": NOW - 40, "seat": "CLAUDE", "state": "failed", "gate": "model_compose_reply"},
            started("CLAUDE", 50, "Not A Token!", False),
            started("CLAUDE", 60, 7),  # type: ignore[arg-type]
            "not a row",  # type: ignore[list-item]
            {"state": "started", "gate": "model_compose_reply", "ts": NOW - 5},  # no seat
            {"state": "started", "gate": "model_compose_reply", "seat": "A"},  # no time
            {"state": "started", "gate": "model_compose_reply", "seat": "A", "ts": True},
        ]
        self.assertEqual(W.gate_counts(rows, NOW, W.DAY), {})

    def test_an_avoidable_flag_that_is_not_exactly_true_does_not_count(self) -> None:
        rows = [started("A", 1, "det_eyes", "yes"), started("A", 2, "det_eyes", 1)]  # type: ignore[arg-type]
        self.assertEqual(W.gate_counts(rows, NOW)["A"]["avoidable"], 0)

    def test_a_fallback_and_an_inspection_count_apart_from_the_start(self) -> None:
        rows = [
            started("CLAUDE", 100, "model_compose_reply", False),
            {
                "ts": NOW - 90,
                "seat": "CLAUDE",
                "state": "done",
                "gate": "model_compose_reply",
                "gate_fallback": "model_fallback",
                "gate_fallback_reason": "primary_failed",
                "gate_inspect": "model_select_destination",
            },
            started("CLAUDE", 80, "model_compose_reply", False),
            {"ts": NOW - 70, "seat": "CLAUDE", "state": "done", "gate": "model_compose_reply"},
        ]
        counts = W.gate_counts(rows, NOW)["CLAUDE"]
        self.assertEqual((counts["starts"], counts["fallbacks"], counts["inspects"]), (2, 1, 1))

    def test_the_status_line_reads_like_the_other_counts(self) -> None:
        self.assertEqual(W.gate_line(None), "model starts 24h 0 (avoidable 0)")
        self.assertEqual(
            W.gate_line({"starts": 5, "avoidable": 2}), "model starts 24h 5 (avoidable 2)"
        )
        self.assertEqual(
            W.gate_line({"starts": 5, "avoidable": 2, "fallbacks": 1, "inspects": 3, "forwards": 7}),
            "model starts 24h 5 (avoidable 2), fallbacks 1, inspections 3, routine forwards 7",
        )

    def test_a_routine_forward_is_a_delivery_counted_apart_from_the_model_starts(self) -> None:
        rows = [
            started("JET", 10, "det_routine_forward", False, adapter="http"),
            started("JET", 20, "det_routine_forward", False, adapter="http"),
            # a forward that a rule says should not have happened is still a forward, and still avoidable
            started("JET", 30, "det_eyes", True, adapter="http"),
            # an http row is a forward whatever its code, a claude row with the same code is a start
            started("JET", 40, "det_eyes", True, adapter="claude"),
        ]
        counts = W.gate_counts(rows, NOW)["JET"]
        self.assertEqual((counts["forwards"], counts["forwards_avoidable"]), (3, 1))
        self.assertEqual((counts["starts"], counts["avoidable"]), (1, 1))
        self.assertEqual(counts["codes"], {"det_eyes": 1}, "forwards are not in the start codes")

    def test_a_row_with_no_state_is_not_counted(self) -> None:
        # The deleted Mac bridge's decision records had no `state`;  such a row is never a start now.
        stateless = {"kind": "chat", "model_attempted": True, "gate": "model_compose_reply", "at": NOW - 5,
                     "ts": NOW - 5, "seat": "JET", "source_seat": "JET"}
        self.assertEqual(W.gate_counts([stateless], NOW), {})

    def test_a_fallback_is_counted_in_the_window_of_its_closing_row_not_its_start(self) -> None:
        rows = [
            started("CLAUDE", W.DAY + 5, "model_compose_reply", False),  # the start, just outside
            {
                "ts": NOW - 5,  # the closing row, inside
                "seat": "CLAUDE",
                "state": "done",
                "gate": "model_compose_reply",
                "gate_fallback": "model_fallback",
                "gate_fallback_reason": "primary_failed",
            },
        ]
        counts = W.gate_counts(rows, NOW)["CLAUDE"]
        self.assertEqual((counts["starts"], counts["fallbacks"]), (0, 1))

    def test_a_failed_closing_row_counts_its_fallback_and_inspection_too(self) -> None:
        rows = [
            {
                "ts": NOW - 10,
                "seat": "CLAUDE",
                "state": "failed",
                "gate": "model_compose_reply",
                "gate_fallback": "model_fallback",
                "gate_fallback_reason": "primary_failed",
                "gate_inspect": "model_select_destination",
            },
            {"ts": NOW - 20, "seat": "CLAUDE", "state": "failed", "gate": "model_compose_reply"},
        ]
        counts = W.gate_counts(rows, NOW)["CLAUDE"]
        self.assertEqual((counts["fallbacks"], counts["inspects"], counts["starts"]), (1, 1, 0))

    def test_a_timestamp_that_cannot_be_a_float_is_ignored_and_never_raises(self) -> None:
        for bad in (10**400, -(10**400), float("nan"), float("inf"), -float("inf"), "5", True, None, [1]):
            with self.subTest(bad=repr(bad)[:20]):
                self.assertIsNone(W._gate_number(bad))
                rows = [started("A", 1, "model_compose_reply", False, ts=bad)]
                self.assertEqual(W.gate_counts(rows, NOW), {})
        self.assertEqual(W._gate_number(5), 5.0)
        self.assertEqual(W._gate_number(2.5), 2.5)
        self.assertEqual(W.gate_counts([started("A", 1, "model_compose_reply", False)], 10**400), {})


class RowsSinceTests(unittest.TestCase):
    """`rows_since` reads a time-ordered ledger from its end, down to the window."""

    def write(self, directory: str, rows: list, *, tail: bytes = b"") -> str:
        path = os.path.join(directory, "wakes.jsonl")
        with open(path, "wb") as fh:
            for row in rows:
                fh.write((row if isinstance(row, bytes) else json.dumps(row).encode()) + b"\n")
            fh.write(tail)
        return path

    def test_it_returns_the_rows_inside_the_window_in_order_and_stops_at_older_ones(self) -> None:
        rows = [{"ts": float(t), "n": t} for t in range(0, 100)]
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, rows)
            got = W.rows_since(path, 60.0, slack=0.0)
            self.assertEqual([r["n"] for r in got], list(range(60, 100)))
            # with slack the read goes a little further back, and the caller's window still decides
            self.assertEqual([r["n"] for r in W.rows_since(path, 60.0, slack=5.0)], list(range(55, 100)))
            self.assertEqual(len(W.rows_since(path, -1.0)), 100)
            self.assertEqual(W.rows_since(path, 1e12, slack=0.0), [])

    def test_a_small_chunk_gives_the_same_answer_as_a_big_one_even_across_a_long_line(self) -> None:
        rows = [{"ts": float(t), "pad": "x" * (t * 7 % 300)} for t in range(200)]
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, rows)
            want = W.rows_since(path, 120.0, slack=0.0)
            for chunk in (1, 7, 64, 100, 1 << 16):
                self.assertEqual(W.rows_since(path, 120.0, chunk=chunk, slack=0.0), want, chunk)
            self.assertEqual([r["ts"] for r in want], [float(t) for t in range(120, 200)])

    def test_junk_lines_a_partial_last_line_and_rows_with_no_ts_do_not_stop_or_raise(self) -> None:
        rows = [
            {"ts": 1.0},
            b"not json at all",
            b"[1, 2]",
            b"",
            {"no_ts": True},
            {"ts": "later"},
            {"ts": 50.0},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, rows, tail=b'{"ts": 60.0, "half')  # a writer mid-append
            got = W.rows_since(path, 40.0, slack=0.0)
            self.assertEqual(got, [{"no_ts": True}, {"ts": "later"}, {"ts": 50.0}])

    def test_a_missing_or_empty_ledger_is_no_rows_and_an_unreadable_one_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(W.rows_since(os.path.join(tmp, "absent.jsonl"), 0.0), [])
            plain = os.path.join(tmp, "plain")
            with open(plain, "w") as fh:
                fh.write("x")
            self.assertEqual(
                W.rows_since(os.path.join(plain, "wakes.jsonl"), 0.0), [], "a path under a file is not there"
            )
            self.assertEqual(W.rows_since(self.write(tmp, []), 0.0), [], "an empty ledger is a real zero")
            self.assertIsNone(W.rows_since(tmp, 0.0), "a directory is not a ledger that can be read")
            self.assertIsNone(W.rows_since(self.write(tmp, [{"ts": 1.0}]), float("nan")))
            self.assertIsNone(W.rows_since(self.write(tmp, [{"ts": 1.0}]), None))  # type: ignore[arg-type]

    def test_a_ledger_with_no_permission_is_none_not_no_rows(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root reads a file with no permission")
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, [{"ts": 1.0}])
            os.chmod(path, 0)
            try:
                self.assertIsNone(W.rows_since(path, 0.0))
            finally:
                os.chmod(path, 0o600)
            self.assertEqual(len(W.rows_since(path, 0.0) or []), 1)

    def test_a_line_nested_too_deep_is_skipped_and_the_rows_around_it_are_kept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, [{"ts": 1.0, "n": 1}, b"[" * 100_000, {"ts": 2.0, "n": 2}])
            self.assertEqual([r["n"] for r in W.rows_since(path, 0.0) or []], [1, 2])
            path = self.write(tmp, [{"ts": 1.0, "n": 1}, b'{"a":' * 100_000, {"ts": 2.0, "n": 2}])
            self.assertEqual([r["n"] for r in W.rows_since(path, 0.0) or []], [1, 2])

    def test_the_slack_keeps_a_row_stepped_back_half_an_hour_and_stops_at_one_stepped_back_over_an_hour(self) -> None:
        t = 1_000_000.0
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(
                tmp,
                [
                    {"ts": t - 100_000.0, "n": "old"},
                    {"ts": t + 100.0, "n": "a"},
                    {"ts": t - 1800.0, "n": "stepped back 30 minutes"},
                    {"ts": t + 200.0, "n": "b"},
                ],
            )
            got = W.rows_since(path, t) or []
            self.assertEqual([r["n"] for r in got], ["a", "stepped back 30 minutes", "b"])
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(
                tmp,
                [
                    {"ts": t - 100_000.0, "n": "old"},
                    {"ts": t + 100.0, "n": "a"},
                    {"ts": t - 3660.0, "n": "stepped back 61 minutes"},
                    {"ts": t + 200.0, "n": "b"},
                ],
            )
            got = W.rows_since(path, t) or []
            self.assertEqual([r["n"] for r in got], ["b"], "the read stops at a row more than the slack back")

    def test_a_row_whose_ts_is_too_big_for_a_float_does_not_raise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write(tmp, [{"ts": 10**400}, {"ts": 5.0}])
            self.assertEqual(len(W.rows_since(path, 0.0)), 2)


class BigLedgerStatusTests(unittest.TestCase):
    """`status` must not parse a whole 90-day ledger for its gate line."""

    ROWS = 40_000

    def test_a_forty_thousand_row_ledger_costs_only_its_last_day(self) -> None:
        now = 1_800_000_000.0
        with tempfile.TemporaryDirectory() as root:
            paths = L.SeatPaths(root, "CLAUDE")
            L.private_dir(os.path.dirname(paths.wakes))
            step = 90 * 86400 / self.ROWS  # 40,000 rows spread over the 90 days a ledger keeps
            with open(paths.wakes, "w", encoding="utf-8") as fh:
                for n in range(self.ROWS):
                    ts = now - (self.ROWS - n) * step
                    row = {"ts": ts, "wake_id": "w%d" % (n // 3), "seat": "CLAUDE",
                           "state": ("queued", "accepted", "started")[n % 3], "topic": "t" * 60,
                           "trigger_ids": [n], "owner": False}
                    if row["state"] == "started":
                        row.update(gate="model_compose_reply", gate_avoidable=n % 2 == 0)
                    fh.write(json.dumps(row, sort_keys=True) + "\n")
            size = os.path.getsize(paths.wakes)
            self.assertGreater(size, 5_000_000, "a realistically big ledger")
            read = []
            real_pread = os.pread

            def counting(fd: int, length: int, offset: int) -> bytes:
                data = real_pread(fd, length, offset)
                read.append(len(data))
                return data

            started_at = time.perf_counter()
            with mock.patch.object(W.os, "pread", counting):
                lines = LC._gate_status_lines(root, "CLAUDE", now, ledger=True)
            elapsed = time.perf_counter() - started_at
            in_day = [n for n in range(self.ROWS) if (self.ROWS - n) * step < 86400 and n % 3 == 2]
            self.assertEqual(
                lines,
                [
                    "model starts 24h %d (avoidable %d)"
                    % (len(in_day), len([n for n in in_day if n % 2 == 0]))
                ],
            )
            self.assertGreater(sum(read), 0, "the tail reader did the reading")
            self.assertLess(sum(read), size // 20, "read about a day of the file, not the file")
            self.assertLess(elapsed, 1.0, "generous for a loaded CI host;  it is milliseconds in practice")


class StatusLineTests(unittest.TestCase):
    def test_the_line_comes_from_the_seat_ledger(self) -> None:
        now = time.time()
        with tempfile.TemporaryDirectory() as root:
            paths = L.SeatPaths(root, "CLAUDE")
            L.append_jsonl(
                paths.wakes,
                [
                    {"ts": now - 60, "wake_id": "a", "seat": "CLAUDE", "state": "started", "gate": "model_compose_reply", "gate_avoidable": False},
                    {"ts": now - 50, "wake_id": "b", "seat": "CLAUDE", "state": "started", "gate": "det_eyes", "gate_avoidable": True},
                    {"ts": now - 40, "wake_id": "c", "seat": "CLAUDE", "state": "started"},
                ],
            )
            alone = LC._gate_status_lines(root, "CLAUDE", now, ledger=True)
            self.assertEqual(alone, ["model starts 24h 2 (avoidable 1)"])
            nothing = LC._gate_status_lines(root, "NOSEAT", now, ledger=True)
            self.assertEqual(nothing, ["model starts 24h 0 (avoidable 0)"])
            self.assertEqual(LC._gate_status_lines(root, "X", now, ledger=False), [])

    def test_a_ledger_that_cannot_be_read_leaves_the_line_out_and_never_raises(self) -> None:
        now = time.time()
        with tempfile.TemporaryDirectory() as root:
            with mock.patch.object(W, "rows_since", side_effect=OSError("unreadable")):
                self.assertEqual(
                    LC._gate_status_lines(root, "CLAUDE", now, ledger=True), []
                )


class StatusCommandTests(ListenerHarness):
    """`simple-chat status` end to end: the real command over a status file and ledgers."""

    def seat(self, **extra: Any) -> dict[str, Any]:
        return dict({"bot": "x", "connected": True, "wake": "inbox", "budget": {}}, **extra)

    def write_status(self, seats: dict[str, Any], instance: str = "mac") -> None:
        L.write_json(
            os.path.join(L.listener_dir(self.root), "status.json"),
            {"updated": time.time(), "instance": instance, "owner_user_id": 12, "seats": seats},
        )

    def started_rows(self, seat: str, gates: list[tuple], adapter: str | None = None) -> list[dict]:
        now = time.time()
        rows = []
        for n, (gate, avoidable) in enumerate(gates):
            row = {"ts": now - 60 + n, "wake_id": "%s%d" % (seat, n), "seat": seat, "state": "started", "gate": gate, "gate_avoidable": avoidable}
            if adapter:
                row["adapter"] = adapter
            rows.append(row)
        return rows

    def write_ledger(self, seat: str, rows: list) -> None:
        L.append_jsonl(L.SeatPaths(self.root, seat).wakes, rows)

    def status_lines(self) -> list[str]:
        result = self.run_cli("status")
        self.assertEqual(result.code, 0, result.err)
        return result.out.splitlines()

    @staticmethod
    def under(lines: list[str], seat: str) -> list[str]:
        index = next(i for i, line in enumerate(lines) if line.startswith(seat + " "))
        tail = []
        for line in lines[index + 1 :]:
            if not line.startswith("  "):
                break
            tail.append(line)
        return tail

    def gate_lines(self, lines: list[str], seat: str) -> list[str]:
        return [ln for ln in self.under(lines, seat) if "model starts" in ln]

    def no_permission(self, path: str) -> None:
        if os.geteuid() == 0:
            self.skipTest("root reads a file with no permission, so there is nothing to test")
        os.chmod(path, 0)
        self.addCleanup(lambda: os.path.exists(path) and os.chmod(path, 0o600))

    # ---- the whole picture -------------------------------------------------------------------
    def test_status_prints_the_starts_and_the_forwards(self) -> None:
        self.write_status(
            {
                "CLAUDE": self.seat(wake="claude"),
                "PLAIN": self.seat(),
                "ROUTINE": self.seat(wake="http", routine={"ready": True, "host": "routine.test"}),
            }
        )
        self.write_ledger("CLAUDE", self.started_rows("CLAUDE", [("model_compose_reply", False), ("det_tagged_letter_single", True)]))
        self.write_ledger("ROUTINE", self.started_rows("ROUTINE", [("det_routine_forward", False)] * 2, adapter="http"))
        lines = self.status_lines()
        self.assertIn("  model starts 24h 2 (avoidable 1)", self.under(lines, "CLAUDE"))
        self.assertEqual(self.gate_lines(lines, "PLAIN"), [], "an inbox seat starts no model")
        self.assertIn("  model starts 24h 0 (avoidable 0), routine forwards 2", self.under(lines, "ROUTINE"))
        self.assertFalse([ln for ln in lines if "bridge" in ln.lower()])

    def test_a_status_file_from_an_older_listener_with_the_bridge_keys_still_prints(self) -> None:
        # A listener that has not restarted onto this build still writes the bridge keys.
        self.write_status({"CLAUDE": self.seat(wake="claude", muse_bridge=True)})
        path = os.path.join(L.listener_dir(self.root), "status.json")
        status = L.read_json(path)
        status["muse_bridge"] = {"enabled": True, "remote_seats": ["ECHO"], "pin": "ok"}
        L.write_json(path, status)
        lines = self.status_lines()
        self.assertEqual(self.gate_lines(lines, "CLAUDE"), ["  model starts 24h 0 (avoidable 0)"])
        self.assertFalse([ln for ln in lines if "bridge" in ln.lower()])

    def test_a_ledger_that_cannot_be_read_leaves_that_seats_line_out_and_the_others_stay(self) -> None:
        self.write_status({"CLAUDE": self.seat(wake="claude"), "OTHER": self.seat(wake="claude")})
        self.write_ledger("OTHER", self.started_rows("OTHER", [("model_compose_reply", False)]))
        mine = L.SeatPaths(self.root, "CLAUDE").wakes
        with self.subTest("no permission"):
            self.write_ledger("CLAUDE", self.started_rows("CLAUDE", [("model_compose_reply", False)]))
            self.no_permission(mine)
            lines = self.status_lines()
            self.assertEqual(self.gate_lines(lines, "CLAUDE"), [])
            self.assertEqual(self.gate_lines(lines, "OTHER"), ["  model starts 24h 1 (avoidable 0)"])
        # Outside the subtest above on purpose: as root `no_permission` skips, a skip inside a subtest
        # only ends that subtest, and the ledger file must still be gone before it becomes a directory.
        if os.path.lexists(mine):
            os.chmod(mine, 0o600)
            os.unlink(mine)
        with self.subTest("a directory"):
            os.makedirs(mine)
            lines = self.status_lines()
            self.assertEqual(self.gate_lines(lines, "CLAUDE"), [])
            self.assertEqual(self.gate_lines(lines, "OTHER"), ["  model starts 24h 1 (avoidable 0)"])

    def test_a_corrupt_line_in_a_ledger_does_not_cost_the_seat_its_line(self) -> None:
        self.write_status({"CLAUDE": self.seat(wake="claude")})
        rows = self.started_rows("CLAUDE", [("model_compose_reply", False), ("det_eyes", True)])
        path = L.SeatPaths(self.root, "CLAUDE").wakes
        L.private_dir(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(rows[0]) + "\n" + "[" * 100_000 + "\n" + json.dumps(rows[1]) + "\n")
        self.assertEqual(
            self.gate_lines(self.status_lines(), "CLAUDE"), ["  model starts 24h 2 (avoidable 1)"]
        )


# --------------------------------------------------------------------------------------------
# The daemon's starts
# --------------------------------------------------------------------------------------------


class GateWakeTests(GateLog, DaemonHarness):
    def wake(self, daemon: D.Daemon, text: str = MENTION, topic: str = "t") -> int:
        before = len(self.ledger())
        mid = self.fake.add_message("Codex", "simple-tools", topic, text)
        self.pump_until(daemon, lambda: len(self.ledger()) > before)
        self.wake_cycle(daemon)
        return mid

    def test_a_seat_wake_runs_once_as_before_and_its_rows_carry_the_gate_through_a_restart(self) -> None:
        daemon = self.started()
        self.wake(daemon)
        rows = self.ledger()
        self.assertEqual([r["state"] for r in rows], ["queued", "accepted", "started", "done"])
        self.assertEqual(len(self.dumps()), 1, "the model ran exactly once, as it always did")
        self.assertEqual(self.terminal_wakes()[-1]["action"], "reply")
        self.assertEqual(self.bot_posts(), [], "the Operator never answers on behalf of a platform")
        self.assertEqual(self.terminal_wakes()[-1]["post_error"], "operator_never_answers")
        for row in rows[:2]:
            self.assertFalse([k for k in row if k.startswith("gate")], "decided at the start, not before")
        for row in rows[2:]:
            self.assertEqual((row["gate"], row["gate_avoidable"]), ("model_compose_reply", False))
            self.assertFalse([k for k in row if k.startswith("gate_f") or k == "gate_inspect"])
        (event,) = self.gate_events()
        self.assertEqual(
            {k: event[k] for k in ("seat", "wake_id", "purpose", "code", "avoidable", "rule")},
            {
                "seat": "CLAUDE",
                "wake_id": rows[0]["wake_id"],
                "purpose": "compose_reply",
                "code": "model_compose_reply",
                "avoidable": False,
                "rule": "reply_composition_is_the_model_job",
            },
        )
        # A restart reads the file, not memory:  a new daemon sees the same record.
        daemon.shutdown()
        again = self.started()
        view = again.seats["CLAUDE"].ledger.wakes()[rows[0]["wake_id"]]
        self.assertEqual((view["gate"], view["gate_avoidable"]), ("model_compose_reply", False))
        counts = W.gate_counts(L.read_jsonl(self.seat_paths().wakes), self.clock.time())
        self.assertEqual((counts["CLAUDE"]["starts"], counts["CLAUDE"]["avoidable"]), (1, 0))

    def test_a_tagged_letter_with_one_session_is_recorded_avoidable_and_the_model_still_runs(self) -> None:
        """The first known violation:  the letter names one session, so code could have decided, and
        the model runs anyway.  Phase 1 records it and changes nothing."""
        daemon = self.started()
        one = [{"seat": "CLAUDE", "platform": "claude", "native_session_id": "s1"}]
        with mock.patch.object(daemon, "operator_candidates", return_value=one):
            self.wake(daemon, "@**Claude** Z can you check the cutover?")
        self.assertEqual(len(self.dumps()), 1, "the model still ran")
        row = [r for r in self.ledger() if r["state"] == "started"][0]
        self.assertEqual((row["gate"], row["gate_avoidable"]), ("det_tagged_letter_single", True))
        self.assertEqual(self.terminal_wakes()[-1]["state"], "done")

    def test_a_tagged_letter_with_two_sessions_is_ambiguous_not_avoidable(self) -> None:
        daemon = self.started()
        two = [{"seat": "CLAUDE", "platform": "claude"}, {"seat": "MM", "platform": "minimax"}]
        with mock.patch.object(daemon, "operator_candidates", return_value=two) as asked:
            self.wake(daemon, "@**Claude** Z can you check the cutover?")
        self.assertEqual(asked.call_count, 1, "the registry is opened once for the whole wake")
        row = [r for r in self.ledger() if r["state"] == "started"][0]
        self.assertEqual((row["gate"], row["gate_avoidable"]), ("model_letter_ambiguous", False))

    def test_a_tagged_letter_whose_one_holder_is_on_another_seat_is_not_avoidable(self) -> None:
        daemon = self.started()
        elsewhere = [{"seat": "MM", "platform": "minimax"}]
        with mock.patch.object(daemon, "operator_candidates", return_value=elsewhere):
            self.wake(daemon, "@**Claude** Z can you check the cutover?")
        row = [r for r in self.ledger() if r["state"] == "started"][0]
        self.assertEqual((row["gate"], row["gate_avoidable"]), ("model_compose_reply", False))
        self.assertEqual(len(self.dumps()), 1)

    def test_a_wake_that_reached_the_model_with_an_eyes_marker_is_recorded_not_blocked(self) -> None:
        """The router never offers a marker message, so this one is made by hand:  were one to leak
        through, phase 1 would record it as avoidable and the model would still run."""
        daemon = self.started()
        daemon.history = lambda _runner, _pending: [
            {"id": 55, "sender_id": 12, "client": "web", "content": ":eyes: not for the seat"}
        ]
        now = self.clock.time()
        pending = W.Pending(
            "leak", "CLAUDE", {"channel": "simple-tools", "topic": "t", "type": "stream"}, owner=True, now=now, due=now
        )
        pending.add({"id": 55, "ts": now}, True)
        daemon.run_wake(daemon.seats["CLAUDE"], pending)
        self.assertEqual(len(self.dumps()), 1)
        row = [r for r in self.ledger() if r["state"] == "started"][0]
        self.assertEqual((row["gate"], row["gate_avoidable"]), ("det_eyes", True))

    def test_a_wake_a_live_session_took_starts_no_model_and_records_no_gate(self) -> None:
        daemon = self.started()
        mid = self.fake.add_message("Codex", "simple-tools", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.clock.advance(25)
        daemon.tick()
        self.write_lease("late", topics=[("simple-tools", "t")])
        daemon.run_jobs("CLAUDE")
        self.assertEqual((self.ledger()[-1]["state"], self.ledger()[-1]["reason"]), ("skipped", "leased"))
        self.assertEqual(self.dumps(), [])
        self.assertEqual(self.gate_events(), [])
        self.assertFalse([k for r in self.ledger() for k in r if k.startswith("gate")])
        self.assertEqual([i["id"] for i in self.live_items("late")], [mid])

    def test_a_wake_with_no_primary_and_no_fallback_starts_nothing_and_records_no_gate(self) -> None:
        daemon = self.started()
        daemon._primary_state = lambda _runner: (None, "not_pinned", None)
        self.wake(daemon)
        self.assertEqual(self.gate_events(), [])
        self.assertFalse([k for r in self.ledger() for k in r if k.startswith("gate")])

    def test_a_primary_that_failed_with_no_fallback_still_has_its_start_recorded_and_no_fallback_field(self) -> None:
        self.set_claude(mode="error")
        daemon = self.started()
        asked: list[str] = []
        real = daemon._model_gate

        def counting(runner: Any, pending: Any, purpose: str, **kw: Any) -> Any:
            asked.append(purpose)
            return real(runner, pending, purpose, **kw)

        daemon._model_gate = counting
        self.wake(daemon)
        rows = self.ledger()
        self.assertEqual([r["state"] for r in rows][-2:], ["started", "failed"])
        for row in rows[-2:]:
            self.assertEqual(row["gate"], "model_compose_reply")
            self.assertNotIn("gate_fallback", row)
        self.assertEqual(len(self.gate_events()), 1)
        self.assertEqual(asked, ["compose_reply"], "no fallback is configured, so none is gated")


class GateNeverBlocksTests(GateLog, DaemonHarness):
    def run_one(self) -> D.Daemon:
        daemon = self.started()
        self.fake.add_message("Codex", "simple-tools", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.wake_cycle(daemon)
        return daemon

    def assert_the_wake_ran_as_before(self) -> None:
        self.assertEqual([r["state"] for r in self.ledger()], ["queued", "accepted", "started", "done"])
        self.assertEqual(len(self.dumps()), 1)
        self.assertEqual(self.bot_posts(), [], "the Operator never answers on behalf of a platform")

    def test_a_gate_that_raises_records_gate_error_and_the_wake_runs(self) -> None:
        with mock.patch.object(W, "model_gate_verdict", side_effect=RuntimeError("boom")):
            self.run_one()
        self.assert_the_wake_ran_as_before()
        started_row = self.ledger()[2]
        self.assertEqual((started_row["gate"], started_row["gate_avoidable"]), ("gate_error", False))
        (event,) = self.gate_events()
        self.assertEqual((event["code"], event["avoidable"], event["rule"]), ("gate_error", False, "gate_failed"))

    def test_facts_that_raise_are_a_gate_error_too(self) -> None:
        with mock.patch.object(D.Daemon, "_gate_facts", side_effect=RuntimeError("boom")):
            self.run_one()
        self.assert_the_wake_ran_as_before()
        self.assertEqual(self.ledger()[2]["gate"], "gate_error")

    def test_one_fact_that_cannot_be_read_leaves_the_others_standing(self) -> None:
        daemon = self.started()
        runner = daemon.seats["CLAUDE"]
        pending = W.Pending("w", "CLAUDE", {"channel": "c", "topic": "t"}, owner=False, now=1.0, due=2.0)
        pending.letter_candidates = 1
        with mock.patch.object(R, "strip_no_wake", side_effect=RuntimeError("boom")):
            facts = daemon._gate_facts(
                runner, pending, messages=[{"content": "hi"}], letter="Z", chat_context=None
            )
        self.assertNotIn("eyes", facts)
        self.assertEqual(facts["letter_candidates"], 1)
        self.assertIs(facts["risk_screen_needed"], True)

    def test_a_gate_event_that_cannot_be_logged_never_stops_the_wake(self) -> None:
        daemon = self.started()
        original = daemon.log.write

        def write(event: str, **fields: Any) -> Any:
            if event == "model-gate":
                raise OSError("disk full")
            return original(event, **fields)

        daemon.log.write = write
        self.fake.add_message("Codex", "simple-tools", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.wake_cycle(daemon)
        self.assert_the_wake_ran_as_before()
        self.assertEqual(self.ledger()[2]["gate"], "model_compose_reply", "the record still landed")

    def test_the_gate_error_is_counted_as_a_start(self) -> None:
        with mock.patch.object(W, "model_gate_verdict", side_effect=RuntimeError("boom")):
            self.run_one()
        counts = W.gate_counts(L.read_jsonl(self.seat_paths().wakes), self.clock.time())
        self.assertEqual(counts["CLAUDE"]["codes"], {"gate_error": 1})


# --------------------------------------------------------------------------------------------
# The same messages decide the same, with the gate, without it and with it broken
# --------------------------------------------------------------------------------------------


class Replay:
    """Mixin for a scenario that is replayed three times: with the gate, without it (the daemon as it
    was before the gate) and with the gate broken.  Not a test: `play` is run by hand, once per mode, so
    each mode gets its own fake Zulip, state directory and clock.

    `instrument` also counts the calls the gate must never add: `takeover`, `lease_views`,
    `operator_candidates`, ledger writes, JSONL appends and process spawns.  Every one has to come out
    the same in all three modes."""

    mode = "on"
    outcome: dict[str, Any]
    letters: dict[str, list] | None = None  # what the registry answers, by letter, when a scenario fixes it

    def patch(self, owner: Any, name: str, value: Any) -> None:
        patcher = mock.patch.object(owner, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)

    def count(self, owner: Any, name: str) -> None:
        original = getattr(owner, name)
        replay = self

        def counted(self_: Any, *args: Any, **kwargs: Any) -> Any:
            replay.calls[name] += 1
            if name == "operator_candidates" and replay.letters is not None:
                return list(replay.letters.get(args[1], []))
            return original(self_, *args, **kwargs)

        self.patch(owner, name, counted)

    def instrument(self) -> None:
        self.calls: Counter = Counter()
        for owner, name in (
            (D.Daemon, "takeover"),
            (D.Daemon, "lease_views"),
            (D.Daemon, "operator_candidates"),
            (D.Daemon, "history"),  # a fetch of the topic over the network
            (W.Ledger, "append"),
        ):
            self.count(owner, name)
        real_append = L.append_jsonl

        def append_jsonl(*args: Any, **kwargs: Any) -> Any:
            self.calls["append_jsonl"] += 1
            return real_append(*args, **kwargs)

        self.patch(L, "append_jsonl", append_jsonl)
        replay = self

        class CountingPopen(subprocess.Popen):
            def __init__(self_, *args: Any, **kwargs: Any) -> None:
                replay.calls["spawns"] += 1
                super().__init__(*args, **kwargs)

        self.patch(subprocess, "Popen", CountingPopen)
        if self.mode == "off":
            # The daemon as it was before the gate: nothing is computed, logged or put on a row.
            self.patch(D.Daemon, "_model_gate", lambda *a, **k: W.GateVerdict("off", False, "off"))
            self.patch(D.Daemon, "_gate_log", lambda *a, **k: None)
            self.patch(W.Pending, "note_gate", lambda self_, verdict: None)
            self.patch(W.Pending, "note_gate_fallback", lambda self_, verdict, reason: None)
            self.patch(W.Pending, "note_gate_inspect", lambda self_, verdict: None)
        elif self.mode == "broken":
            self.patch(W, "model_gate_verdict", mock.Mock(side_effect=RuntimeError("boom")))

    def snapshot(self, seats: tuple = ("CLAUDE",), **extras: Any) -> None:
        ids: dict[str, int] = {}
        rows = []
        fallback_codes = []
        started_rows = []
        for seat in seats:
            for row in self.ledger(seat):
                if row["state"] == "started":
                    started_rows.append(row)
                if row["state"] in ("done", "failed"):
                    fallback_codes.append(row.get("gate_fallback"))
                kept = {
                    k: v
                    for k, v in row.items()
                    if k not in ("ts", "due", "secs") and not k.startswith("gate")
                }
                kept["wake"] = ids.setdefault(kept.pop("wake_id"), len(ids))
                rows.append(kept)
        log = (self.state_dir / "logs" / "listener.log").read_text()
        events = [json.loads(line).get("event") for line in log.splitlines() if line.strip()]
        # The daemon's own reads of topics from the fake Zulip, which a gate that fetched history
        # again would add to.
        self.calls["zulip_get_messages"] = len(self.fake.requests_to("GET", "messages"))
        self.outcome = {
            "rows": rows,
            "posts": [(p["type"], p["subject"], p["content"]) for p in self.operator_posts()],
            "prompts": [re.sub(r"nonce=[0-9a-f]+", "nonce=N", d["stdin"]) for d in self.dumps()],
            "events": Counter(e for e in events if e != "model-gate"),
            "extras": extras,
            "calls": dict(self.calls),
            # what only the gate writes, kept apart so the comparison above never sees it
            "gates": [r.get("gate") for r in started_rows],
            "fallback_codes": fallback_codes,
            "gate_events": events.count("model-gate"),
        }


def replay(case_class: type) -> dict[str, dict[str, Any]]:
    outcomes = {}
    for mode in ("on", "off", "broken"):
        case = case_class("play")
        case.mode = mode
        result = unittest.TestResult()
        case.run(result)
        problems = result.errors + result.failures
        if problems:
            raise AssertionError("the %s replay failed:\n%s" % (mode, problems[0][1]))
        outcomes[mode] = case.outcome
    return outcomes


class BasicScenario(Replay, DaemonHarness):
    """The plain CLAUDE seat: a reply, an owner DM, a refused answer, a failed run and a wake a session took."""

    def play(self) -> None:
        self.instrument()
        daemon = self.started()

        def step(text: str, topic: str, seconds: float = 25.0) -> None:
            before = len(self.ledger())
            self.fake.add_message("Codex", "simple-tools", topic, text)
            self.pump_until(daemon, lambda: len(self.ledger()) > before)
            self.wake_cycle(daemon, seconds)

        step(MENTION, "t")  # a peer is answered
        before = len(self.ledger())
        self.fake.add_direct_message("Jay Wedgeworth", "status of the cutover?", deliver=True)
        self.pump_until(daemon, lambda: len(self.ledger()) > before)
        self.wake_cycle(daemon, 6)  # the owner is answered by direct message
        self.set_claude(mode="ok", output={"action": "nuke"})
        step(MENTION, "u")  # an answer the validator refuses
        self.set_claude(mode="error")
        step(MENTION, "v")  # a run that fails
        self.set_claude(mode="ok", output=reply_output())
        before = len(self.ledger())
        self.fake.add_message("Codex", "simple-tools", "w", MENTION)
        self.pump_until(daemon, lambda: len(self.ledger()) > before)
        self.clock.advance(25)
        daemon.tick()
        self.write_lease("late", topics=[("simple-tools", "w")])
        daemon.run_jobs("CLAUDE")  # a session takes the topic before the run
        self.snapshot()


class TaggedLetterScenario(Replay, DaemonHarness):
    """A letter held by the tagged seat's own session, by two sessions, by another seat's, and by nobody."""

    def play(self) -> None:
        mine = {"seat": "CLAUDE", "platform": "claude"}
        other = {"seat": "MM", "platform": "minimax"}
        self.letters = {"Z": [mine], "Y": [mine, other], "X": [other], "W": []}
        self.instrument()
        daemon = self.started()
        for letter, topic in (("Z", "z"), ("Y", "y"), ("X", "x"), ("W", "w")):
            before = len(self.ledger())
            self.fake.add_message("Codex", "simple-tools", topic, "@**Claude** %s can you check the cutover?" % letter)
            self.pump_until(daemon, lambda: len(self.ledger()) > before)
            self.wake_cycle(daemon)
        self.snapshot()


class FallbackScenario(Replay, OpenRouterHarness):
    """A primary that hits its cap, one whose answer is refused, one that errors, and one that answers."""

    def setUp(self) -> None:
        super().setUp()
        claude_fallback_setup(self)

    def play(self) -> None:
        self.instrument()
        daemon = self.started()
        limited = A.RunResult()
        limited.error = "You've hit your weekly limit"
        limited.limited = True
        real_run = daemon.claude_runner.run

        def wake(topic: str) -> None:
            mid = self.fake.add_message("Codex", "simple-tools", topic, MENTION)
            self.pump_until(daemon, lambda: any(mid in r.get("trigger_ids", []) for r in self.ledger()))
            self.wake_cycle(daemon)

        daemon.claude_runner.run = lambda *a, **k: limited
        wake("a")
        daemon.claude_runner.run = real_run
        self.set_claude(mode="ok", output={"action": "reply"})
        wake("b")
        self.set_claude(mode="error")
        wake("c")
        self.set_claude(mode="ok", output=reply_output())
        wake("d")
        self.snapshot(models=self.models_asked(), router_calls=len(self.router.requests()))


class InspectScenario(Replay, ListenerHarness):
    """The second Muse call, for an owner and for a peer whose text names the chat, and a rejected one."""

    def setUp(self) -> None:
        super().setUp()
        self.write_config(wake="muse")

    def play(self) -> None:
        self.instrument()
        muse = FakeMuseRunner(
            [
                (inspect_output(), 1.0, 0.1),
                (final_output(), 1.0, 0.1),
                (inspect_output(), 1.0, 0.1),
                (final_output(), 1.0, 0.1),
                (inspect_output("c" * 64), 1.0, 0.1),
            ]
        )
        daemon = self.daemon(muse_runner=muse)
        self.assertTrue(daemon.connect_seat("CLAUDE"), daemon.seats["CLAUDE"].error)
        daemon.seats["CURSOR"] = types.SimpleNamespace(
            seat="CURSOR", cfg=types.SimpleNamespace(enabled=True, wake="inbox")
        )
        self.addCleanup(daemon.seats.pop, "CURSOR")
        daemon._pinned_muse = lambda _runner: ("muse-binary", None, None)
        texts = {55: "inspect the relevant chat", 56: "read chat %s please" % CHAT_ID, 57: "inspect it"}
        daemon.history = lambda _runner, pending: [
            {"id": pending.trigger_ids[0], "sender_id": 12, "client": "web", "content": texts[pending.trigger_ids[0]]}
        ]
        catalog = FakeCatalog([offered()], [{"role": "user", "text": "context"}])
        daemon._chat_catalog = catalog
        now = self.clock.time()
        for n, (message_id, owner) in enumerate(((55, True), (56, False), (57, True))):
            pending = W.Pending(
                "inspect-%d" % n,
                "CLAUDE",
                {"channel": "simple-tools", "topic": "inspection %d" % n, "type": "stream"},
                owner=owner,
                now=now,
                due=now,
            )
            pending.add({"id": message_id, "ts": now}, owner)
            daemon.run_wake(daemon.seats["CLAUDE"], pending)
        prompts = [re.sub(r"nonce=[0-9a-f]+", "nonce=N", call["prompt"]) for call in muse.calls]
        self.snapshot(muse_calls=len(muse.calls), excerpts=catalog.excerpt_calls, muse_prompts=prompts)


class BoundChatScenario(Replay, DaemonHarness):
    """A CODEX seat whose topic gets bound to an existing chat after the wake was accepted."""

    def setUp(self) -> None:
        super().setUp()
        codex_key = secrets.token_hex(16)
        self.extra_keys.append(("codex-bot@zulip.test", codex_key))
        self.fake.add_bot("codex-bot@zulip.test", "Codex", codex_key)
        write_rc(self.secrets_dir / "Codex-zuliprc", email="codex-bot@zulip.test", key=codex_key, site=self.fake.url)
        self.write_config(seats='[seat.CODEX]\nbot = "Codex"\nwake = "claude"\nmodel = "sonnet"\n')
        self.pin("CODEX")

    def play(self) -> None:
        self.instrument()
        daemon = self.daemon(env=self.env(SIMPLE_TOOLS_PARTITION=self.two_seat_partition()))
        self.assertTrue(daemon.connect_seat("CODEX"), daemon.seats["CODEX"].error)

        def wake(topic: str, bind: bool) -> None:
            before = len(self.ledger("CODEX"))
            self.fake.add_message("Cursor", "simple-tools", topic, "@**Codex** please look")
            self.pump_until(daemon, lambda: len(self.ledger("CODEX")) > before, seat="CODEX")
            self.clock.advance(25)
            daemon.tick()
            if bind:  # the binding appears after the router offered the wake, so the wake still runs
                daemon.chat_queue("CODEX").bind(
                    "SIMPLE-TOOLS", topic.upper(), "codex", "mac", "session-1", "/tmp"
                )
            daemon.run_jobs("CODEX")

        wake("free", False)
        wake("bound", True)
        self.snapshot(("CODEX",))


class RoutineScenario(Replay, ServerHarness):
    """An `http` routine seat that is woken twice."""

    def play(self) -> None:
        self.instrument()
        daemon = self.started()
        for topic in ("t", "u"):
            before = len(self.ledger(ROUTINE_SEAT))
            self.fake.add_message("Codex", "simple-tools", topic, "@**GB-Compiler** please look")
            self.pump_until(daemon, lambda: len(self.ledger(ROUTINE_SEAT)) > before, seat=ROUTINE_SEAT)
            self.wake_cycle(daemon)
        self.snapshot((ROUTINE_SEAT,), routine_calls=len(self.routine.received))


def claude_fallback_setup(case: OpenRouterHarness) -> None:
    """The CLAUDE seat with an OpenRouter fallback behind it, for the fallback tests and the replay."""
    case.write_config(
        seats=textwrap.dedent("""\
            [seat.CLAUDE]
            bot = "Claude"
            wake = "claude"
            model = "sonnet"
            provider = "Claude Code"
            fallback = "openrouter"
            """)
    )
    case.pin()
    case.pin_openrouter(seat="CLAUDE", role=FALLBACK)


#: The daemon's own call pattern in each replay, measured on the clean base (927fd6eea, with the gate
#: hooks stubbed out, which is the base) and pinned as literals.  The on, off and broken replays must
#: agree with each other AND with these, so work that a gate does in a call's argument expression, where
#: stubbing the call cannot remove it, still shows.  A key that is absent was never called.  When a change
#: to the daemon legitimately changes its own pattern, re-measure on the new base: a scratch copy of this
#: module with `create=True` on `Replay.patch`, one `off` replay per scenario, `calls` printed.
BASE_CALLS = {
    "basic": {"append": 19, "append_jsonl": 25, "history": 4, "lease_views": 20, "spawns": 5, "takeover": 12, "zulip_get_messages": 4},
    "fallback": {"append": 16, "append_jsonl": 20, "history": 4, "lease_views": 18, "spawns": 3, "takeover": 12, "zulip_get_messages": 4},
    "inspect": {"append": 6, "append_jsonl": 7, "lease_views": 5, "takeover": 5, "zulip_get_messages": 0},
    "tagged": {"append": 16, "append_jsonl": 20, "history": 4, "lease_views": 18, "operator_candidates": 4, "spawns": 4, "takeover": 12, "zulip_get_messages": 4},
    "bound": {"append": 8, "append_jsonl": 10, "history": 2, "lease_views": 9, "spawns": 2, "takeover": 6, "zulip_get_messages": 2},
    "routine": {"append": 8, "append_jsonl": 10, "lease_views": 7, "takeover": 4, "zulip_get_messages": 0},
}


class NoBehaviourChangeTests(unittest.TestCase):
    """The same messages decide the same with the gate, without it and with it broken, and the gate
    adds no call that the daemon did not already make: no lease scan, no registry open, no history
    fetch, no ledger write, no spawn.  The counts are compared with each other and with the base's."""

    def assert_replays_agree(
        self,
        outcomes: dict[str, dict[str, Any]],
        gates: list,
        calls: dict[str, int],
        fallback_codes: list | None = None,
    ) -> dict[str, Any]:
        on, off, broken = outcomes["on"], outcomes["off"], outcomes["broken"]
        for key in ("rows", "posts", "prompts", "events", "extras", "calls"):
            self.assertEqual(on[key], off[key], "%s differ with the gate off" % key)
            self.assertEqual(on[key], broken[key], "%s differ with the gate broken" % key)
        # The calls are also the base's own, as literals: stubbing the gate out cannot remove work
        # that a gate did while its arguments were being evaluated.
        for mode in ("on", "off", "broken"):
            self.assertEqual(outcomes[mode]["calls"], calls, "the %s calls are not the base's" % mode)
        # ... so the three differ ONLY in what the gate records, and that is not nothing.
        self.assertEqual(on["gates"], gates)
        self.assertEqual(off["gates"], [None] * len(gates))
        self.assertEqual(broken["gates"], ["gate_error"] * len(gates))
        self.assertGreater(on["gate_events"], 0)
        self.assertEqual((off["gate_events"], broken["gate_events"]), (0, on["gate_events"]))
        if fallback_codes is not None:
            self.assertEqual([c for c in on["fallback_codes"] if c], fallback_codes)
            self.assertEqual([c for c in off["fallback_codes"] if c], [])
            self.assertEqual([c for c in broken["fallback_codes"] if c], ["gate_error"] * len(fallback_codes))
        return on

    def test_the_plain_seat_wakes_decide_the_same_and_the_gate_adds_no_call(self) -> None:
        on = self.assert_replays_agree(
            replay(BasicScenario), ["model_compose_reply"] * 4, BASE_CALLS["basic"]
        )
        # The sequence is varied enough to mean something:  a reply, an owner DM, a refused answer, a
        # failed run and a wake a session took.
        states = Counter(r["state"] for r in on["rows"] if r["state"] not in ("queued", "accepted"))
        self.assertEqual(states, Counter({"done": 2, "failed": 2, "skipped": 1, "started": 4}))
        self.assertEqual(on["posts"], [], "the Operator never answers on behalf of a platform")

    def test_a_fallback_path_decides_the_same_and_the_gate_adds_no_call(self) -> None:
        on = self.assert_replays_agree(
            replay(FallbackScenario),
            ["model_compose_reply"] * 4,
            BASE_CALLS["fallback"],
            ["model_fallback"] * 3,
        )
        self.assertEqual(on["extras"]["router_calls"], 3, "three wakes were answered by the fallback")

    def test_an_inspect_path_decides_the_same_and_the_gate_adds_no_call(self) -> None:
        on = self.assert_replays_agree(
            replay(InspectScenario),
            ["model_select_destination", "model_select_destination", "model_select_destination"],
            BASE_CALLS["inspect"],
        )
        # two completed inspections and one rejected, so five Muse calls in all
        self.assertEqual(on["extras"]["muse_calls"], 5)

    def test_a_tagged_letter_path_decides_the_same_and_the_registry_is_asked_once_per_wake(self) -> None:
        on = self.assert_replays_agree(
            replay(TaggedLetterScenario),
            ["det_tagged_letter_single", "model_letter_ambiguous", "model_compose_reply", "model_compose_reply"],
            BASE_CALLS["tagged"],
        )
        self.assertEqual(on["calls"]["operator_candidates"], 4, "once per wake, with the gate on")

    def test_a_bound_chat_path_decides_the_same_and_the_gate_adds_no_call(self) -> None:
        on = self.assert_replays_agree(
            replay(BoundChatScenario), ["model_compose_reply", "det_bound_chat"], BASE_CALLS["bound"]
        )
        self.assertEqual(on["posts"], [], "the bound wake still ran, and nothing was posted for it")

    def test_a_routine_path_decides_the_same_and_the_gate_adds_no_call(self) -> None:
        on = self.assert_replays_agree(
            replay(RoutineScenario), ["det_routine_forward"] * 2, BASE_CALLS["routine"]
        )
        self.assertEqual(on["extras"]["routine_calls"], 2)


# --------------------------------------------------------------------------------------------
# The fallback, the chat inspection and the routine
# --------------------------------------------------------------------------------------------


class ClaudeFallbackGateTests(GateLog, OpenRouterHarness):
    """The CLAUDE seat with an OpenRouter fallback behind it (the same set-up as the fallback tests)."""

    def setUp(self) -> None:
        super().setUp()
        self.write_config(
            seats=textwrap.dedent("""\
                [seat.CLAUDE]
                bot = "Claude"
                wake = "claude"
                model = "sonnet"
                provider = "Claude Code"
                fallback = "openrouter"
                """)
        )
        self.pin()
        self.pin_openrouter(seat="CLAUDE", role=FALLBACK)

    def wake(self, daemon: D.Daemon, topic: str = "t") -> None:
        mid = self.fake.add_message("Codex", "simple-tools", topic, MENTION)
        self.pump_until(daemon, lambda: any(mid in r.get("trigger_ids", []) for r in self.ledger()))
        self.wake_cycle(daemon)

    def test_a_fallback_row_keeps_the_primary_code_and_adds_its_reason(self) -> None:
        daemon = self.started()
        limited = A.RunResult()
        limited.error = "You've hit your weekly limit"
        limited.limited = True
        daemon.claude_runner.run = lambda *a, **k: limited
        self.wake(daemon)
        self.assertEqual(self.models_asked(), [FALLBACK_MODEL])
        rows = self.ledger()
        started_row = [r for r in rows if r["state"] == "started"][0]
        done = [r for r in rows if r["state"] == "done"][0]
        self.assertEqual((started_row["gate"], started_row["gate_avoidable"]), ("model_compose_reply", False))
        self.assertNotIn("gate_fallback", started_row, "the fallback is decided later than the start")
        self.assertEqual(
            {k: v for k, v in done.items() if k.startswith("gate")},
            {
                "gate": "model_compose_reply",
                "gate_avoidable": False,
                "gate_fallback": "model_fallback",
                "gate_fallback_reason": "primary_failed",
            },
        )
        self.assertEqual(
            [(e["purpose"], e["code"], e["rule"]) for e in self.gate_events()],
            [
                ("compose_reply", "model_compose_reply", "reply_composition_is_the_model_job"),
                ("fallback", "model_fallback", "primary_failed"),
            ],
        )
        counts = W.gate_counts(L.read_jsonl(self.seat_paths().wakes), self.clock.time())
        self.assertEqual((counts["CLAUDE"]["starts"], counts["CLAUDE"]["fallbacks"]), (1, 1))

    def test_an_answer_the_validator_refuses_is_its_own_reason(self) -> None:
        self.set_claude(mode="ok", output={"action": "reply"})
        daemon = self.started()
        self.wake(daemon)
        done = [r for r in self.ledger() if r["state"] == "done"][0]
        self.assertEqual(done["gate_fallback_reason"], "primary_invalid_result")
        self.assertEqual([e["rule"] for e in self.gate_events()][-1], "primary_invalid_result")

    def test_a_fallback_that_never_started_leaves_no_fallback_field_and_no_event(self) -> None:
        self.set_claude(mode="error")
        daemon = self.started()
        daemon.run_fallback = lambda *args: None  # every hold of run_fallback returns None
        self.wake(daemon)
        failed = [r for r in self.ledger() if r["state"] == "failed"][0]
        self.assertEqual(failed["gate"], "model_compose_reply")
        self.assertNotIn("gate_fallback", failed)
        self.assertEqual([e["purpose"] for e in self.gate_events()], ["compose_reply"])

    def test_the_fallback_is_gated_after_the_primary_and_the_gate_probes_no_lease(self) -> None:
        limited = A.RunResult()
        limited.error = "limit"
        limited.limited = True
        daemon = self.started()
        daemon.claude_runner.run = lambda *a, **k: limited
        asked: list[str] = []
        real = daemon._model_gate

        def counting(runner: Any, pending: Any, purpose: str, **kw: Any) -> Any:
            asked.append(purpose)
            if purpose == "fallback":
                self.assertNotIn("live_lease", kw, "a lease is not probed again for a fallback")
            return real(runner, pending, purpose, **kw)

        daemon._model_gate = counting
        self.wake(daemon)
        self.assertEqual(asked, ["compose_reply", "fallback"])


class MuseFallbackGateTests(GateLog, OpenRouterHarness):
    def setUp(self) -> None:
        super().setUp()
        self.mm_config("muse", extra='fallback = "openrouter"')
        self.pin_muse()
        self.pin_openrouter(role=FALLBACK)

    def test_a_primary_that_cannot_run_starts_nothing_and_only_the_fallback_is_a_start(self) -> None:
        os.unlink(self.seat_paths("MM").wake_pin_muse)
        daemon = self.mm()
        self.mm_wake(daemon)
        self.assertEqual(self.muse_runs(), 0)
        done = self.final()
        self.assertEqual(done["state"], "done")
        self.assertEqual(
            (done["gate"], done["gate_fallback"], done["gate_fallback_reason"]),
            ("model_compose_reply", "model_fallback", "primary_unavailable"),
        )
        self.assertEqual(
            [e["purpose"] for e in self.gate_events()], ["fallback"], "a skipped primary logs no start"
        )

    def test_a_muse_wake_that_was_offered_chats_is_a_destination_selection(self) -> None:
        daemon = self.mm()
        self.mm_wake(daemon, "no-chats")
        self.assertEqual(self.final()["gate"], "model_compose_reply", "no catalog rows, so a reply")
        daemon.chat_context = lambda *a, **k: [offered()]
        self.mm_wake(daemon, "chats")
        self.assertEqual(self.final()["gate"], "model_select_destination")


class FallbackContextGateTests(GateLog, ListenerHarness):
    """A fallback start is gated with the same trigger text and chat context as the primary was."""

    def setUp(self) -> None:
        super().setUp()
        self.write_config(
            seats=textwrap.dedent("""\
                [seat.CLAUDE]
                bot = "Claude"
                wake = "muse"
                fallback = "openrouter"
                """)
        )

    def test_a_fallback_for_an_owner_message_that_names_one_chat_is_recorded_avoidable(self) -> None:
        muse = FakeMuseRunner([(final_output(), 1.0, 0.1, "the primary exploded")])
        daemon = self.daemon(muse_runner=muse)
        self.assertTrue(daemon.connect_seat("CLAUDE"), daemon.seats["CLAUDE"].error)
        daemon.seats["CURSOR"] = types.SimpleNamespace(
            seat="CURSOR", cfg=types.SimpleNamespace(enabled=True, wake="inbox")
        )
        self.addCleanup(daemon.seats.pop, "CURSOR")
        daemon._pinned_muse = lambda _runner: ("muse-binary", None, None)
        daemon.history = lambda _runner, _pending: [
            {"id": 55, "sender_id": 12, "client": "web", "content": "wake chat %s please" % CHAT_ID}
        ]
        daemon._chat_catalog = FakeCatalog([offered()])
        answer = A.RunResult()
        answer.parsed = final_output()
        answer.result = {"structured_output": answer.parsed}
        answer.exit = 0
        answer.models = ["fallback-model"]
        daemon.run_fallback = lambda *args: answer
        now = self.clock.time()
        pending = W.Pending(
            "fallback-wake",
            "CLAUDE",
            {"channel": "simple-tools", "topic": "fallback", "type": "stream"},
            owner=True,
            now=now,
            due=now,
        )
        pending.add({"id": 55, "ts": now}, True)
        daemon.run_wake(daemon.seats["CLAUDE"], pending)
        closing = [r for r in daemon.seats["CLAUDE"].ledger.rows() if r["state"] in ("done", "failed")][-1]
        self.assertEqual(closing["gate"], "det_exact_chat_id")
        self.assertEqual(
            closing["gate_fallback"], "det_exact_chat_id", "the fallback sees the same context and text"
        )
        self.assertEqual(closing["gate_fallback_reason"], "primary_failed")

class InspectionGateTests(GateLog, ListenerHarness):
    def setUp(self) -> None:
        super().setUp()
        self.write_config(wake="muse")

    def setup_wake(
        self, outputs: list, text: str, *, owner: bool = True
    ) -> tuple[D.Daemon, FakeMuseRunner, W.Pending]:
        muse = FakeMuseRunner(outputs)
        daemon = self.daemon(muse_runner=muse)
        self.assertTrue(daemon.connect_seat("CLAUDE"), daemon.seats["CLAUDE"].error)
        # The listed chat is a CURSOR chat, and its seat has to be switched on here to be a destination.
        daemon.seats["CURSOR"] = types.SimpleNamespace(
            seat="CURSOR", cfg=types.SimpleNamespace(enabled=True, wake="inbox")
        )
        self.addCleanup(daemon.seats.pop, "CURSOR")  # shutdown disconnects every real seat
        daemon._pinned_muse = lambda _runner: ("muse-binary", None, None)
        daemon.takeover = lambda _runner, _pending: None
        daemon.history = lambda _runner, _pending: [
            {"id": 55, "sender_id": 12, "client": "web", "content": text}
        ]
        daemon._chat_catalog = FakeCatalog([offered()], [{"role": "user", "text": "context"}])
        now = self.clock.time()
        pending = W.Pending(
            "inspect-wake",
            "CLAUDE",
            {"channel": "simple-tools", "topic": "inspection", "type": "stream"},
            owner=owner,
            now=now,
            due=now,
        )
        pending.add({"id": 55, "ts": now}, owner)
        return daemon, muse, pending

    def test_the_second_muse_call_is_a_second_recorded_start(self) -> None:
        daemon, muse, pending = self.setup_wake(
            [(inspect_output(), 1.0, 0.1), (final_output(), 1.0, 0.1)], "inspect the relevant chat"
        )
        daemon.run_wake(daemon.seats["CLAUDE"], pending)
        self.assertEqual(len(muse.calls), 2, "both calls ran, as before")
        rows = daemon.seats["CLAUDE"].ledger.rows()
        done = [r for r in rows if r["state"] == "done"][-1]
        self.assertEqual(
            (done["gate"], done["gate_avoidable"], done["gate_inspect"]),
            ("model_select_destination", False, "model_select_destination"),
        )
        self.assertEqual([e["purpose"] for e in self.gate_events()], ["select_destination", "inspect"])
        counts = W.gate_counts(rows, self.clock.time())
        self.assertEqual((counts["CLAUDE"]["starts"], counts["CLAUDE"]["inspects"]), (1, 1))

    def test_an_inspection_the_text_already_named_exactly_is_recorded_avoidable(self) -> None:
        daemon, muse, pending = self.setup_wake(
            [(inspect_output(), 1.0, 0.1), (final_output(), 1.0, 0.1)], "read chat %s please" % CHAT_ID
        )
        daemon.run_wake(daemon.seats["CLAUDE"], pending)
        self.assertEqual(len(muse.calls), 2)
        done = [r for r in daemon.seats["CLAUDE"].ledger.rows() if r["state"] == "done"][-1]
        self.assertEqual((done["gate"], done["gate_avoidable"], done["gate_inspect"]), ("det_exact_chat_id", True, "det_exact_chat_id"))

    def test_the_same_inspection_for_a_peer_still_needs_the_risk_answer_so_it_is_not_avoidable(self) -> None:
        daemon, muse, pending = self.setup_wake(
            [(inspect_output(), 1.0, 0.1), (final_output(), 1.0, 0.1)],
            "read chat %s please" % CHAT_ID,
            owner=False,
        )
        daemon.run_wake(daemon.seats["CLAUDE"], pending)
        self.assertEqual(len(muse.calls), 2, "the model still ran, as before")
        done = [r for r in daemon.seats["CLAUDE"].ledger.rows() if r["state"] == "done"][-1]
        self.assertEqual(
            (done["gate"], done["gate_avoidable"], done["gate_inspect"]),
            ("model_select_destination", False, "model_select_destination"),
        )

    def test_an_inspection_that_was_rejected_started_no_second_model_and_records_none(self) -> None:
        daemon, muse, pending = self.setup_wake([(inspect_output("c" * 64), 1.0, 0.1)], "inspect it")
        daemon.run_wake(daemon.seats["CLAUDE"], pending)
        self.assertEqual(len(muse.calls), 1)
        self.assertEqual([e["purpose"] for e in self.gate_events()], ["select_destination"])
        rows = daemon.seats["CLAUDE"].ledger.rows()
        self.assertFalse([r for r in rows if "gate_inspect" in r])


class RoutineGateTests(GateLog, ServerHarness):
    def inbox_message(self, daemon: Any, text: str, topic: str) -> int:
        mid = self.fake.add_message("Codex", "simple-tools", topic, text)
        self.pump_until(
            daemon, lambda: any(r["id"] == mid for r in self.inbox(ROUTINE_SEAT)), seat=ROUTINE_SEAT
        )
        return mid

    def run_routine_wake(self, daemon: Any, mid: int, topic: str) -> None:
        now = self.clock.time()
        pending = W.Pending(
            "routine-%d" % mid,
            ROUTINE_SEAT,
            {"channel": "simple-tools", "topic": topic, "type": "stream"},
            owner=False,
            now=now,
            due=now,
        )
        pending.add({"id": mid, "ts": now}, False)
        daemon.run_wake(daemon.seats[ROUTINE_SEAT], pending)

    def test_a_routine_start_reads_its_trigger_for_the_eyes_marker(self) -> None:
        """The router never offers a marker message, so these wakes are made by hand:  the routine's
        gate has to read the trigger row (its text, and its sender for the router's seat-tag rule), and
        the routine is still called, as it always was."""
        daemon = self.started()
        plain = self.inbox_message(daemon, ":eyes: @**GB-Compiler** please look", "one")
        self.run_routine_wake(daemon, plain, "one")
        tagged = self.inbox_message(daemon, "[CODEX] :eyes: @**GB-Compiler** please look", "two")
        self.run_routine_wake(daemon, tagged, "two")
        ordinary = self.inbox_message(daemon, "@**GB-Compiler** please look", "three")
        self.run_routine_wake(daemon, ordinary, "three")
        self.assertEqual(len(self.routine.received), 3, "all three forwards happened, as before")
        gates = [r["gate"] for r in self.ledger(ROUTINE_SEAT) if r["state"] == "started"]
        self.assertEqual(gates, ["det_eyes", "det_eyes", "det_routine_forward"])

    def test_a_routine_forward_is_recorded_as_a_delivery_and_counted_as_a_forward(self) -> None:
        daemon = self.started()
        self.fake.add_message("Codex", "simple-tools", "t", "@**GB-Compiler** please look")
        self.pump_until(daemon, lambda: len(self.ledger(ROUTINE_SEAT)) >= 1, seat=ROUTINE_SEAT)
        self.wake_cycle(daemon)
        self.assertEqual(len(self.routine.received), 1, "the routine was called, as before")
        rows = self.ledger(ROUTINE_SEAT)
        started_row = [r for r in rows if r["state"] == "started"][0]
        self.assertEqual((started_row["gate"], started_row["gate_avoidable"]), ("det_routine_forward", False))
        self.assertTrue(all(r.get("gate") == "det_routine_forward" for r in rows[-2:]))
        (event,) = self.gate_events()
        self.assertEqual((event["purpose"], event["seat"]), ("routine", ROUTINE_SEAT))
        counts = W.gate_counts(rows, self.clock.time())[ROUTINE_SEAT]
        self.assertEqual((counts["forwards"], counts["starts"]), (1, 0), "a forward is not a model start")


if __name__ == "__main__":
    unittest.main()
