"""AFC-649 known-cap mode: the bridge-level integration contract.

Absent declaration      -> the primary consult path runs, byte-identical to before.
Malformed configured    -> HOLD: no claim, no consult, cursor stays behind, signal logged.
Expired declaration     -> HOLD the same way (review-due), never reverting to the capped primary.
Active + key unreadable -> HOLD (pre-charge), nothing sent, cursor stays behind.
Active + consult runs   -> decision rows name runtime="openrouter"; an ambiguous attempt (sent,
                           no definitive response) settles as a reviewed hold and is never
                           retried; a pre-charge failure unwinds the claim so the row stays
                           pending.
"""

from __future__ import annotations

import os
import tempfile
import time
import types
import unittest

from agent_sync import adapters as A
from agent_sync import backup_mode as BM
from agent_sync import config as C
from agent_sync import live as L
from agent_sync import muse_bridge_mac as MBM


def _cfg(backup: object = None) -> C.Config:
    raw = {
        "daemon": {"instance": "mac", "owner_user_id": 12, "eligible_user_ids": [12]},
        "muse_bridge": {"enabled": True},
        "seat": {"ECHO": {"bot": "Echo", "wake": "inbox", "muse_bridge": True}},
    }
    if backup is not None:
        raw["muse_bridge"]["consult_backup"] = backup
    cfg = C.from_dict(raw)
    assert not cfg.errors, cfg.errors
    return cfg


def _item(seq: int = 7, message_id: int = 42) -> dict:
    return {
        "source_seat": "ECHO",
        "id": message_id,
        "seq": seq,
        "type": "stream",
        "channel": "agent-sync",
        "topic": "t",
        "content": "hi",
        "sender": "peer",
        "owner": False,
        "ts": time.time(),
        "stale": False,
    }


GOOD_MODEL = "@preset/operator-inbox-router"


def _good(now: float) -> dict:
    return {
        "runtime": "openrouter",
        "model": GOOD_MODEL,
        "reason": "owner declared the primary capped",
        "declared_at": now - 60,
        "review_by": now + 3600,
    }


class _FakeKey:
    def __init__(self, exc: Exception | None = None) -> None:
        self.exc = exc

    def get(self) -> str:
        if self.exc is not None:
            raise self.exc
        return "the-operator-openrouter-key"


class _FakeRunner:
    def __init__(self, result) -> None:
        self.result = result
        self.calls: list = []

    def run(self, model, prompt, schema, key, *, system=None):
        self.calls.append(
            {"model": model, "prompt": prompt, "schema": schema, "key": key, "system": system}
        )
        return self.result


def _result(error=None, http_status=None, parsed=None, killed=False):
    return types.SimpleNamespace(
        error=error, http_status=http_status, parsed=parsed, killed=killed
    )


class BridgeBackupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.logged: list = []
        logged = self.logged

        class FakeLog:
            def write(self, event: str, **fields: object) -> None:
                logged.append((event, dict(fields)))

        class FakeExchange:
            submits: list = []

            def request(self, command: str, body: object) -> dict:
                if command == "submit":
                    self.submits.append(body)
                return {"receipt": "r1"}

        self.now = time.time()
        self.decide_calls: list = []
        bridge = MBM.MacBridge.__new__(MBM.MacBridge)
        bridge.daemon = types.SimpleNamespace(
            root=self.root,
            log=FakeLog(),
            clock=types.SimpleNamespace(time=lambda: self.now),
            env={},
            home=self.root,
            wake_path=self.root,
            scrub=lambda text: text,
            openrouter_key=_FakeKey(),
            openrouter_runner=_FakeRunner(_result()),
        )
        bridge.path = os.path.join(L.listener_dir(self.root), MBM.STATE_NAME)
        bridge.exchange = FakeExchange()

        def fake_decide_inner(item, binary, model, consult_runtime, backup_model=None):
            self.decide_calls.append((consult_runtime, backup_model))
            return {"kind": "hold", "reason": "no_target", "model_attempted": True}

        bridge._decide_inner = fake_decide_inner
        self.bridge = bridge

    def events(self, name: str) -> list:
        return [fields for event, fields in self.logged if event == name]

    def run_envelope(self, backup: object = None) -> dict:
        self.bridge.daemon.config = _cfg(backup)
        state: dict = {"cursors": {}, "decisions": {}}
        envelope = {
            "gap": False,
            "next": 8,
            "items": [_item()],
            "budget": dict(C.BUDGET_DEFAULTS),
        }
        self.bridge._process_envelope(
            state, state["cursors"], state["decisions"], "ECHO", envelope,
            remote=True, binary="muse", model="m",
        )
        return state

    def test_absent_declaration_runs_primary_unchanged(self) -> None:
        state = self.run_envelope(None)
        self.assertEqual(self.decide_calls, [(BM.MUSE, None)])
        self.assertEqual(state["cursors"]["ECHO"], 8)
        (row,) = state["decisions"].values()
        self.assertNotIn("runtime", row)

    def test_malformed_configured_declaration_holds_closed(self) -> None:
        for bad in ({}, {"runtime": "openrouter"}, {**_good(self.now), "runtime": "minimax"}, "openrouter"):
            self.decide_calls.clear()
            self.logged.clear()
            self.bridge._last_backup_hold = None  # reset the per-reason log dedup between cases
            state = self.run_envelope(bad)
            self.assertEqual(self.decide_calls, [])
            self.assertEqual(state["decisions"], {})
            self.assertNotIn("ECHO", state["cursors"])
            (held,) = self.events("muse-bridge-backup-held")
            self.assertEqual(held["reason"], BM.MISCONFIGURED)

    def test_expired_declaration_holds_for_review(self) -> None:
        expired = {**_good(self.now), "review_by": self.now - 1}
        state = self.run_envelope(expired)
        self.assertEqual(self.decide_calls, [])
        self.assertEqual(state["decisions"], {})
        self.assertNotIn("ECHO", state["cursors"])
        (held,) = self.events("muse-bridge-backup-held")
        self.assertEqual(held["reason"], BM.REVIEW_DUE)

    def test_active_mode_stamps_the_backup_runtime_and_advances(self) -> None:
        state = self.run_envelope(_good(self.now))
        self.assertEqual(self.decide_calls, [(BM.OPENROUTER, GOOD_MODEL)])
        self.assertEqual(state["cursors"]["ECHO"], 8)
        (row,) = state["decisions"].values()
        self.assertEqual(row["runtime"], "openrouter")
        self.assertEqual(row["delivery"], "done")

    def test_pre_charge_failure_unwinds_the_claim(self) -> None:
        def pending(item, binary, model, consult_runtime, backup_model=None):
            raise MBM.BridgeBackupPending("openrouter key unavailable")

        self.bridge._decide_inner = pending
        state = self.run_envelope(_good(self.now))
        self.assertEqual(state["decisions"], {})
        self.assertNotIn("ECHO", state["cursors"])
        (held,) = self.events("muse-bridge-backup-held")
        self.assertIn("openrouter key unavailable", held["reason"])

    def test_unreadable_operator_key_holds_before_any_request(self) -> None:
        # End to end through the real _decide_inner:  the Operator key cannot be read, so no
        # HTTP request is made, the claim unwinds, and the hold log names the reason.
        del self.bridge._decide_inner
        self.bridge._resources = lambda item: []
        self.bridge.daemon.openrouter_key = _FakeKey(
            A.OpenRouterUnavailable("the Operator's OpenRouter key is unavailable: test")
        )
        state = self.run_envelope(_good(self.now))
        self.assertEqual(self.bridge.daemon.openrouter_runner.calls, [])
        self.assertEqual(state["decisions"], {})
        self.assertNotIn("ECHO", state["cursors"])
        (held,) = self.events("muse-bridge-backup-held")
        self.assertIn("OpenRouter key", held["reason"])

    def test_held_rows_stay_pending_across_ticks(self) -> None:
        # A second tick with the mode repaired decides the same row exactly once.
        outcomes = [MBM.BridgeBackupPending("openrouter key unavailable"), None]

        def flaky(item, binary, model, consult_runtime, backup_model=None):
            out = outcomes.pop(0)
            if out is not None:
                raise out
            self.decide_calls.append((consult_runtime, backup_model))
            return {"kind": "hold", "reason": "no_target", "model_attempted": True}

        self.bridge._decide_inner = flaky
        first = self.run_envelope(_good(self.now))
        self.assertEqual(first["decisions"], {})
        self.bridge.daemon.config = _cfg(_good(self.now))
        envelope = {
            "gap": False,
            "next": 8,
            "items": [_item()],
            "budget": dict(C.BUDGET_DEFAULTS),
        }
        self.bridge._process_envelope(
            first, first["cursors"], first["decisions"], "ECHO", envelope,
            remote=True, binary="muse", model="m",
        )
        self.assertEqual(self.decide_calls, [(BM.OPENROUTER, GOOD_MODEL)])
        self.assertEqual(first["cursors"]["ECHO"], 8)


class ConsultBackupUnitTest(unittest.TestCase):
    """The real _consult_backup against fake key/runner:  outcome mapping and request shape."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        bridge = MBM.MacBridge.__new__(MBM.MacBridge)
        bridge.daemon = types.SimpleNamespace(
            root=self.tmp.name,
            env={},
            scrub=lambda text: text,
            openrouter_key=_FakeKey(),
            openrouter_runner=_FakeRunner(_result()),
        )
        bridge._resources = lambda item: []
        self.bridge = bridge

    def test_answered_returns_the_parsed_decision(self) -> None:
        parsed = {"action": "none", "board": None, "owner_note": None, "reply": None, "risk": None}
        self.bridge.daemon.openrouter_runner = _FakeRunner(_result(parsed=parsed))
        outcome, got = self.bridge._consult_backup(_item(message_id=42), GOOD_MODEL)
        self.assertEqual(outcome, "answered")
        self.assertEqual(got, parsed)

    def test_the_contract_is_the_system_message_not_the_prompt(self) -> None:
        runner = _FakeRunner(_result(parsed={"action": "none"}))
        self.bridge.daemon.openrouter_runner = runner
        self.bridge._consult_backup(_item(message_id=42), GOOD_MODEL)
        (call,) = runner.calls
        self.assertEqual(call["model"], GOOD_MODEL)
        self.assertIn("tool-less decision runtime", call["system"])
        self.assertIn("TRUSTED LISTENER METADATA", call["prompt"])
        self.assertNotIn("tool-less decision runtime", call["prompt"])
        self.assertEqual(call["key"], "the-operator-openrouter-key")
        import json

        self.assertIsInstance(json.loads(call["schema"]), dict)

    def test_timeout_after_sending_is_ambiguous(self) -> None:
        self.bridge.daemon.openrouter_runner = _FakeRunner(
            _result(error="timed out after 60 seconds", killed=True)
        )
        self.assertEqual(self.bridge._consult_backup(_item(), GOOD_MODEL), ("ambiguous", None))

    def test_a_5xx_is_ambiguous(self) -> None:
        self.bridge.daemon.openrouter_runner = _FakeRunner(
            _result(error="provider error", http_status=502)
        )
        self.assertEqual(self.bridge._consult_backup(_item(), GOOD_MODEL), ("ambiguous", None))

    def test_a_4xx_is_pre_charge(self) -> None:
        self.bridge.daemon.openrouter_runner = _FakeRunner(
            _result(error="HTTP 401", http_status=401)
        )
        with self.assertRaises(MBM.BridgeBackupPending):
            self.bridge._consult_backup(_item(), GOOD_MODEL)

    def test_an_unreadable_key_is_pre_charge(self) -> None:
        self.bridge.daemon.openrouter_key = _FakeKey(A.OpenRouterUnavailable("no key: test"))
        with self.assertRaises(MBM.BridgeBackupPending):
            self.bridge._consult_backup(_item(), GOOD_MODEL)
        self.assertEqual(self.bridge.daemon.openrouter_runner.calls, [])

    def test_request_key_is_deterministic_per_row(self) -> None:
        self.assertEqual(
            MBM.backup_request_key(_item(message_id=42)),
            MBM.backup_request_key(_item(message_id=42)),
        )
        self.assertNotEqual(
            MBM.backup_request_key(_item(message_id=42)),
            MBM.backup_request_key(_item(message_id=43)),
        )


class BackupAnswerShapeTest(unittest.TestCase):
    """The backup consult yields a parsed decision object; the bridge validates THAT object
    directly.  A JSON text answer is not the contract and must fail closed as invalid."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = time.time()
        bridge = MBM.MacBridge.__new__(MBM.MacBridge)
        bridge.daemon = types.SimpleNamespace(
            root=self.tmp.name,
            log=types.SimpleNamespace(write=lambda event, **fields: None),
            clock=types.SimpleNamespace(time=lambda: self.now),
            env={},
            home=self.tmp.name,
            wake_path=self.tmp.name,
            scrub=lambda text: text,
            config=_cfg(_good(self.now)),
        )
        bridge.path = os.path.join(L.listener_dir(self.tmp.name), MBM.STATE_NAME)
        bridge._resources = lambda item: []
        self.bridge = bridge

    def test_parsed_backup_answer_validates_directly(self) -> None:
        self.bridge._consult_backup = lambda item, model: (
            "answered",
            {"action": "none", "board": None, "owner_note": None, "reply": None, "risk": None},
        )
        decision = self.bridge._decide_inner(
            _item(message_id=78), "muse", "m", BM.OPENROUTER, GOOD_MODEL
        )
        self.assertEqual(decision["reason"], "no_target")

    def test_text_backup_answer_is_invalid_not_routed(self) -> None:
        self.bridge._consult_backup = lambda item, model: (
            "answered",
            '{"action":"none","board":null,"owner_note":null,"reply":null,"risk":null}',
        )
        decision = self.bridge._decide_inner(
            _item(message_id=77), "muse", "m", BM.OPENROUTER, GOOD_MODEL
        )
        self.assertEqual(decision["reason"], "invalid_decision")


class ChargedReviewTest(unittest.TestCase):
    """Crash-point semantics for an ambiguous backup attempt (AFC-649)."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.logged: list = []
        self.submits: list = []
        self.now = time.time()
        self.consult_calls: list = []
        self.bridge = self._new_bridge()

    def _new_bridge(self):
        bridge = MBM.MacBridge.__new__(MBM.MacBridge)
        bridge.daemon = types.SimpleNamespace(
            root=self.root,
            log=types.SimpleNamespace(
                write=lambda event, **fields: self.logged.append((event, dict(fields)))
            ),
            clock=types.SimpleNamespace(time=lambda: self.now),
            env={},
            home=self.root,
            wake_path=self.root,
            scrub=lambda text: text,
            config=_cfg(_good(self.now)),
        )
        bridge.path = os.path.join(L.listener_dir(self.root), MBM.STATE_NAME)
        bridge.exchange = types.SimpleNamespace(
            request=lambda command, body: (
                self.submits.append(body) or {"receipt": "r1"}
            )
            if command == "submit"
            else {"receipt": "r1"}
        )
        bridge._resources = lambda item: []
        consult_calls = self.consult_calls

        def fake_consult(item, model):
            consult_calls.append(item["id"])
            if item["id"] == 42:  # sent, ambiguous
                return "ambiguous", None
            # 43: answered with the parsed decision object (the real consult shape),
            # validating to a no_target hold
            return "answered", {
                "action": "none",
                "board": None,
                "owner_note": None,
                "reply": None,
                "risk": None,
            }

        bridge._consult_backup = fake_consult
        return bridge

    def events(self, name: str) -> list:
        return [fields for event, fields in self.logged if event == name]

    def _envelope(self) -> dict:
        return {
            "gap": False,
            "next": 9,
            "items": [_item(seq=7, message_id=42), _item(seq=8, message_id=43)],
            "budget": dict(C.BUDGET_DEFAULTS),
        }

    def test_ambiguous_row_pending_review_independent_row_unaffected(self) -> None:
        state: dict = {"cursors": {}, "decisions": {}}
        self.bridge._process_envelope(
            state, state["cursors"], state["decisions"], "ECHO", self._envelope(),
            remote=True, binary="muse", model="m",
        )
        self.assertEqual(sorted(self.consult_calls), [42, 43])
        rows = {row["id"]: row for row in state["decisions"].values()}
        charged, answered = rows[42], rows[43]
        # Ambiguous row: durable pending-review retention, never delivered, never final.
        self.assertEqual(charged["reason"], "backup_consult_charged_ambiguous")
        self.assertEqual(charged["review"], "pending")
        self.assertEqual(charged["runtime"], "openrouter")
        self.assertEqual(charged["request_key"], MBM.backup_request_key(_item(message_id=42)))
        self.assertNotIn("delivery", charged)
        # The independent row behind it is processed normally, not silently consumed.
        self.assertEqual(answered["reason"], "no_target")
        self.assertEqual(answered["delivery"], "done")
        self.assertEqual([body["id"] for body in self.submits], [43])
        # Cursor advances: retention lives in the durable decisions map, not the cursor.
        self.assertEqual(state["cursors"]["ECHO"], 9)
        (review,) = self.events("muse-bridge-backup-review-required")
        self.assertEqual(review["id"], 42)
        self.assertEqual(review["request_key"], charged["request_key"])

    def test_restart_readback_retains_review_row_and_never_reconsults(self) -> None:
        state: dict = {"cursors": {}, "decisions": {}}
        self.bridge._process_envelope(
            state, state["cursors"], state["decisions"], "ECHO", self._envelope(),
            remote=True, binary="muse", model="m",
        )
        # Restart: a fresh bridge over the same state root reads the same durable rows.
        restarted = self._new_bridge()
        readback = restarted._state()
        rows = {row["id"]: row for row in readback["decisions"].values()}
        self.assertEqual(rows[42]["review"], "pending")
        self.assertEqual(rows[42]["request_key"], MBM.backup_request_key(_item(message_id=42)))
        # An export that overlaps the already-retained row must not re-consult or redeliver.
        self.consult_calls.clear()
        self.submits.clear()
        restarted._process_envelope(
            readback, readback["cursors"], readback["decisions"], "ECHO", self._envelope(),
            remote=True, binary="muse", model="m",
        )
        self.assertEqual(self.consult_calls, [])
        self.assertEqual(self.submits, [])
        rows = {row["id"]: row for row in readback["decisions"].values()}
        self.assertNotIn("delivery", rows[42])

    def test_crash_after_claim_before_consult_is_retained_not_retried(self) -> None:
        # Pre-seed the claim row a crashed tick would leave (model_interrupted):  the row is
        # held for review by the existing discipline and the backup is never re-issued.
        state: dict = {"cursors": {}, "decisions": {}}
        key = MBM.MacBridge._key(_item(message_id=42))
        state["decisions"][key] = {
            "kind": "hold",
            "reason": "model_interrupted",
            "model_attempted": True,
            "at": self.now,
            "source_seat": "ECHO",
            "id": 42,
            "owner": False,
            "topic_key": "agent-sync/t",
        }
        self.bridge._process_envelope(
            state, state["cursors"], state["decisions"], "ECHO", self._envelope(),
            remote=True, binary="muse", model="m",
        )
        self.assertEqual(self.consult_calls, [43])  # 42 never re-consulted
        rows = {row["id"]: row for row in state["decisions"].values()}
        self.assertEqual(rows[42]["reason"], "model_interrupted")


class ReviewLocatorTest(unittest.TestCase):
    """The operator review/recovery surface:  durable locator, resolution, no re-charge."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.now = time.time()
        self.consult_calls: list = []
        self.submits: list = []
        self.bridge = self._new_bridge()

    def _new_bridge(self):
        bridge = MBM.MacBridge.__new__(MBM.MacBridge)
        bridge.daemon = types.SimpleNamespace(
            root=self.root,
            log=types.SimpleNamespace(write=lambda event, **fields: None),
            clock=types.SimpleNamespace(time=lambda: self.now),
            env={},
            home=self.root,
            wake_path=self.root,
            scrub=lambda text: text,
            config=_cfg(_good(self.now)),
        )
        bridge.path = os.path.join(L.listener_dir(self.root), MBM.STATE_NAME)
        submits = self.submits
        bridge.exchange = types.SimpleNamespace(
            request=lambda command, body: (
                submits.append(body) if command == "submit" else None
            )
            or {"receipt": "r1"}
        )
        bridge._resources = lambda item: []
        consult_calls = self.consult_calls

        def fake_consult(item, model):
            consult_calls.append(item["id"])
            return "ambiguous", None  # every consult: sent, ambiguous

        bridge._consult_backup = fake_consult
        return bridge

    def _run_one(self) -> dict:
        state: dict = {"cursors": {}, "decisions": {}}
        envelope = {
            "gap": False,
            "next": 9,
            "items": [_item(seq=7, message_id=42)],
            "budget": dict(C.BUDGET_DEFAULTS),
        }
        self.bridge._process_envelope(
            state, state["cursors"], state["decisions"], "ECHO", envelope,
            remote=True, binary="muse", model="m",
        )
        return state

    def test_locator_finds_the_retained_row_with_its_receipt(self) -> None:
        self.assertEqual(MBM.list_pending_reviews(self.root), [])
        state = self._run_one()
        (found,) = MBM.list_pending_reviews(self.root)
        self.assertEqual(found["seat"], "ECHO")
        self.assertEqual(found["id"], 42)
        self.assertEqual(found["request_key"], MBM.backup_request_key(_item(message_id=42)))
        self.assertEqual(found["ledger"], self.bridge.path)
        (row,) = state["decisions"].values()
        self.assertEqual(found["key"], MBM.MacBridge._key(_item(message_id=42)))

    def test_resolve_finalizes_next_tick_without_reconsulting(self) -> None:
        self._run_one()
        out = MBM.resolve_pending_review(self.root, "ECHO", 42)
        self.assertEqual(out["seat"], "ECHO")
        self.assertEqual(MBM.list_pending_reviews(self.root), [])
        # Next tick over a re-exported row: no consult, the hold finalizes through delivery.
        state: dict = {"cursors": {"ECHO": 9}, "decisions": {}}
        restarted = self._new_bridge()
        readback = restarted._state()
        envelope = {
            "gap": False,
            "next": 9,
            "items": [_item(seq=7, message_id=42)],
            "budget": dict(C.BUDGET_DEFAULTS),
        }
        self.consult_calls.clear()
        restarted._process_envelope(
            readback, readback["cursors"], readback["decisions"], "ECHO", envelope,
            remote=True, binary="muse", model="m",
        )
        self.assertEqual(self.consult_calls, [])
        self.assertEqual([body["id"] for body in self.submits], [42])
        (row,) = readback["decisions"].values()
        self.assertEqual(row["review"], "resolved")
        self.assertEqual(row["delivery"], "done")

    def test_resolve_finalizes_from_state_without_reexport(self) -> None:
        self._run_one()
        key = MBM.MacBridge._key(_item(message_id=42))
        row = self.bridge._state()["decisions"][key]
        # The retained row carries the item payload: the cursor has passed it, so resolution
        # must reach it from durable state, never from a re-export.
        self.assertEqual(row["item"]["id"], 42)
        self.assertIs(row["remote"], True)
        MBM.resolve_pending_review(self.root, "ECHO", 42)
        restarted = self._new_bridge()
        state = restarted._state()
        self.consult_calls.clear()
        restarted._finalize_resolved_reviews(state, state["decisions"])
        self.assertEqual(self.consult_calls, [])  # never re-consulted
        self.assertEqual([body["id"] for body in self.submits], [42])
        self.assertEqual(state["decisions"][key]["delivery"], "done")
        # Idempotent: a later pass over the same state does not deliver twice.
        self.submits.clear()
        restarted._finalize_resolved_reviews(state, state["decisions"])
        self.assertEqual(self.submits, [])

    def test_resolve_waits_for_the_tick_lock(self) -> None:
        import threading

        self._run_one()
        outcome: dict = {}
        with MBM._state_file_lock(self.bridge.path):
            worker = threading.Thread(
                target=lambda: outcome.setdefault(
                    "result", MBM.resolve_pending_review(self.root, "ECHO", 42)
                )
            )
            worker.start()
            worker.join(0.5)
            self.assertTrue(worker.is_alive())  # blocked behind the tick's lock
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcome["result"]["id"], 42)

    def test_resolve_unknown_row_refuses(self) -> None:
        with self.assertRaises(ValueError):
            MBM.resolve_pending_review(self.root, "ECHO", 999)


class ConfigParseTest(unittest.TestCase):
    def test_consult_backup_key_stored_raw(self) -> None:
        good = {
            "runtime": "openrouter",
            "model": GOOD_MODEL,
            "reason": "r",
            "declared_at": 1,
            "review_by": 2,
        }
        self.assertEqual(_cfg(good).muse_bridge_consult_backup, good)
        self.assertIsNone(_cfg().muse_bridge_consult_backup)
        # A non-table value is normalized to a malformed table:  fail closed at consult time.
        self.assertEqual(_cfg("openrouter").muse_bridge_consult_backup, {})


if __name__ == "__main__":
    unittest.main()
