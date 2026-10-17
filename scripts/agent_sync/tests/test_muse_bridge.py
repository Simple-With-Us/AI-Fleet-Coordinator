"""AFC-326: the default-off Muse inbox bridge keeps source-qualified receipts.

Same Zulip message ID under two source seats must never collapse into one
decision, one Mac delivery, or one chat nudge.  A crashed native claim must
stay uncertain instead of being delivered twice on restart.
"""
from __future__ import annotations

import io
import json
import os
import tempfile
import time
import types
import unittest
from unittest import mock

from .. import chat_nudges as CN
from .. import config as C
from .. import live as L
from .. import muse_bridge as MB
from .. import muse_bridge_mac as MBM


def _server_config(*seats: str, wake: str = "inbox", bridge: bool = True) -> C.Config:
    raw: dict = {
        "daemon": {"instance": "server", "owner_user_id": 12, "eligible_user_ids": [12]},
        "muse_bridge": {"enabled": True},
        "seat": {},
    }
    for seat in seats:
        section: dict = {"bot": seat.title(), "wake": wake, "muse_bridge": bridge}
        if wake == "http":
            section["routine"] = {}
        raw["seat"][seat] = section
    cfg = C.from_dict(raw)
    assert not cfg.errors, cfg.errors
    return cfg


def _mac_config(*seats: str, bridge: bool = True) -> C.Config:
    raw: dict = {
        "daemon": {"instance": "mac", "owner_user_id": 12, "eligible_user_ids": [12]},
        "muse_bridge": {"enabled": True},
        "seat": {},
    }
    for seat in seats:
        raw["seat"][seat] = {"bot": seat.title(), "wake": "inbox", "muse_bridge": bridge}
    cfg = C.from_dict(raw)
    assert not cfg.errors, cfg.errors
    return cfg


def _row(seq: int, message_id: int, **extra) -> dict:
    row = {"seq": seq, "id": message_id, "type": "stream", "channel": "agent-sync",
           "topic": "AFC-326 Universal Muse Inbox Routing", "content": "hello %d" % seq,
           "sender": "peer", "owner": False, "ts": time.time(),
           "muse_bridge": True, "wake_worthy": True, "delivered_to": None}
    row.update(extra)
    return row


class BridgeExchangeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.cfg = _server_config("ECHO", "JET")

    def _write(self, seat: str, rows: list[dict]) -> None:
        L.append_jsonl(L.SeatPaths(self.root, seat).seat_inbox, rows)

    def test_export_offers_only_marked_rows_with_capped_content(self) -> None:
        self._write("ECHO", [_row(1, 101), _row(2, 102, muse_bridge=False),
                             _row(3, 103, content="x" * (MB.MAX_EXPORTED_CONTENT + 50))])
        out = MB.export(self.root, self.cfg, {})
        items = out["seats"]["ECHO"]["items"]
        self.assertEqual([row["id"] for row in items], [101, 103])
        self.assertEqual(len(items[1]["content"]), MB.MAX_EXPORTED_CONTENT)
        self.assertFalse(out["seats"]["ECHO"]["gap"])

    def test_export_reports_rotation_gap_without_advancing(self) -> None:
        self._write("ECHO", [_row(9, 109), _row(10, 110)])
        out = MB.export(self.root, self.cfg, {"ECHO": 3})
        seat = out["seats"]["ECHO"]
        self.assertTrue(seat["gap"])
        self.assertEqual(seat["items"], [])
        self.assertEqual(seat["next"], 3)

    def test_export_reports_internal_gap_between_rotated_and_current(self) -> None:
        paths = L.SeatPaths(self.root, "ECHO")
        L.append_jsonl(paths.seat_inbox + ".1", [_row(1, 101), _row(2, 102), _row(3, 103),
                                                 _row(4, 104), _row(5, 105)])
        self._write("ECHO", [_row(11, 111), _row(12, 112)])
        out = MB.export(self.root, self.cfg, {"ECHO": 5})
        seat = out["seats"]["ECHO"]
        self.assertTrue(seat["gap"])
        self.assertEqual(seat["items"], [])
        self.assertEqual(seat["next"], 5)
        local = MB.export_local(self.root, _mac_config("ECHO"), {"ECHO": 5})
        mac = local["seats"]["ECHO"]
        self.assertTrue(mac["gap"])
        self.assertEqual(mac["items"], [])
        self.assertEqual(mac["next"], 5)

    def test_export_without_gap_still_advances_past_contiguous_rows(self) -> None:
        paths = L.SeatPaths(self.root, "ECHO")
        L.append_jsonl(paths.seat_inbox + ".1", [_row(1, 101), _row(2, 102)])
        self._write("ECHO", [_row(3, 103), _row(4, 104)])
        out = MB.export(self.root, self.cfg, {"ECHO": 2})
        seat = out["seats"]["ECHO"]
        self.assertFalse(seat["gap"])
        self.assertEqual([row["id"] for row in seat["items"]], [103, 104])
        self.assertEqual(seat["next"], 4)

    def test_export_disabled_bridge_raises(self) -> None:
        cfg = _server_config("ECHO")
        cfg.muse_bridge_enabled = False
        with self.assertRaises(ValueError):
            MB.export(self.root, cfg, {})

    def test_find_source_ignores_delivered_rows(self) -> None:
        self._write("ECHO", [_row(1, 111, delivered_to="muse")])
        self.assertIsNone(MB.find_source(self.root, self.cfg, "ECHO", 111))

    def test_submit_is_idempotent_for_same_kind_but_rejects_conflict(self) -> None:
        self._write("ECHO", [_row(4, 114)])
        first = MB.submit(self.root, self.cfg, {"source_seat": "ECHO", "id": 114,
                                                "seq": 4, "kind": "chat"})
        second = MB.submit(self.root, self.cfg, {"source_seat": "ECHO", "id": 114,
                                                 "seq": 4, "kind": "chat"})
        self.assertEqual(first, second)
        self.assertEqual(first["state"], "chat_selected")
        with self.assertRaises(ValueError):
            MB.submit(self.root, self.cfg, {"source_seat": "ECHO", "id": 114,
                                            "seq": 4, "kind": "hold"})

    def test_submit_rejects_changed_sequence(self) -> None:
        self._write("ECHO", [_row(5, 115)])
        with self.assertRaises(ValueError):
            MB.submit(self.root, self.cfg, {"source_seat": "ECHO", "id": 115,
                                            "seq": 6, "kind": "hold"})

    def test_decisions_are_source_qualified(self) -> None:
        self._write("ECHO", [_row(7, 120)])
        self._write("JET", [_row(7, 120)])
        echo = MB.submit(self.root, self.cfg, {"source_seat": "ECHO", "id": 120,
                                               "seq": 7, "kind": "hold"})
        jet = MB.submit(self.root, self.cfg, {"source_seat": "JET", "id": 120,
                                              "seq": 7, "kind": "hold"})
        self.assertNotEqual(echo["receipt"], jet["receipt"])

    def test_native_claim_runs_once_and_crash_stays_uncertain(self) -> None:
        cfg = _server_config("ECHO", wake="http")
        self._write("ECHO", [_row(8, 121)])
        receipt = MB.submit(self.root, cfg, {"source_seat": "ECHO", "id": 121,
                                             "seq": 8, "kind": "native"})["receipt"]
        claimed = MB.claim_native(self.root)
        assert claimed is not None
        self.assertEqual(claimed["receipt"], receipt)
        # A restart after the claim must not reclaim the same routine.
        self.assertIsNone(MB.claim_native(self.root))
        stored = L.read_json(os.path.join(L.listener_dir(self.root), MB.DECISIONS_FILE))
        self.assertEqual(stored[receipt]["state"], "dispatching")
        MB.finish_native(self.root, receipt, "done")
        stored = L.read_json(os.path.join(L.listener_dir(self.root), MB.DECISIONS_FILE))
        self.assertEqual(stored[receipt]["state"], "done")
        self.assertIsNone(MB.claim_native(self.root))

    def test_native_rejected_without_configured_routine(self) -> None:
        self._write("JET", [_row(9, 122)])
        with self.assertRaises(ValueError):
            MB.submit(self.root, self.cfg, {"source_seat": "JET", "id": 122,
                                            "seq": 9, "kind": "native"})

    def test_native_target_id_is_stable_and_seat_scoped(self) -> None:
        self.assertEqual(MB.native_target_id("ECHO"), MB.native_target_id("ECHO"))
        self.assertNotEqual(MB.native_target_id("ECHO"), MB.native_target_id("JET"))

    def _main(self, command: str, request: dict) -> tuple[int, dict]:
        out = io.StringIO()
        stdin = types.SimpleNamespace(buffer=io.BytesIO(json.dumps(request).encode()))
        with mock.patch.object(MB.L, "state_root", return_value=self.root), \
                mock.patch.object(MB.C, "load", return_value=self.cfg), \
                mock.patch.object(MB.sys, "stdin", stdin), mock.patch.object(MB.sys, "stdout", out):
            code = MB.main([command])
        return code, json.loads(out.getvalue())

    def test_submit_refusal_is_a_definite_answer_not_an_outage(self) -> None:
        self._write("ECHO", [_row(5, 115)])
        with self.assertRaises(MB.Refused):
            MB.submit(self.root, self.cfg, {"source_seat": "ECHO", "id": 115, "seq": 6, "kind": "hold"})
        # The exchange exits 0 with {"refused": ...} so the Mac can tell it from a dead transport.
        code, answer = self._main("submit", {"source_seat": "ECHO", "id": 115, "seq": 6, "kind": "hold"})
        self.assertEqual(code, 0)
        self.assertEqual(answer, {"refused": "source inbox row is missing or changed"})
        code, answer = self._main("submit", {"source_seat": "ECHO", "id": 115, "seq": 5, "kind": "hold"})
        self.assertEqual((code, answer["state"]), (0, "held"))

    def test_unreadable_decision_ledger_still_fails_the_exchange(self) -> None:
        self._write("ECHO", [_row(5, 115)])
        path = os.path.join(L.listener_dir(self.root), MB.DECISIONS_FILE)
        L.private_dir(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{}")
        with self.assertRaises(SystemExit):
            self._main("submit", {"source_seat": "ECHO", "id": 115, "seq": 5, "kind": "hold"})


class NativeOutcomeTest(unittest.TestCase):
    def test_worker_integer_counts_map_to_done_and_held(self) -> None:
        self.assertEqual(MB.native_outcome({"state": "done", "surfaced": True, "routine_delivered": 1}), "done")
        self.assertEqual(MB.native_outcome({"state": "done", "surfaced": False, "routine_delivered": 0}), "held")

    def test_non_worker_routine_that_surfaced_is_done(self) -> None:
        self.assertEqual(MB.native_outcome({"state": "done", "surfaced": True}), "done")

    def test_unsurfaced_answer_without_a_count_stays_uncertain(self) -> None:
        self.assertEqual(MB.native_outcome({"state": "done", "surfaced": False, "routine_delivered": None}),
                         "uncertain")
        self.assertEqual(MB.native_outcome({"state": "done", "surfaced": False, "routine_delivered": False}),
                         "uncertain")

    def test_non_done_states(self) -> None:
        self.assertEqual(MB.native_outcome({"state": "failed"}), "failed")
        for state in ("held_back", "dropped", "skipped"):
            self.assertEqual(MB.native_outcome({"state": state}), "held")
        self.assertEqual(MB.native_outcome({"state": "started"}), "uncertain")
        self.assertEqual(MB.native_outcome({}), "uncertain")


class BridgeDedupeTest(unittest.TestCase):
    BINDING = {"platform": "cursor", "host": "mac", "session_id": "s1", "cwd": "/tmp"}

    def test_mac_decision_key_is_source_qualified(self) -> None:
        echo = MBM.MacBridge._key({"source_seat": "ECHO", "id": 130})
        jet = MBM.MacBridge._key({"source_seat": "JET", "id": 130})
        self.assertNotEqual(echo, jet)

    def test_chat_nudge_dedupe_is_source_qualified(self) -> None:
        echo = CN.ChatNudges._dedupe_key("130", self.BINDING, "ECHO")
        jet = CN.ChatNudges._dedupe_key("130", self.BINDING, "JET")
        plain = CN.ChatNudges._dedupe_key("130", self.BINDING, None)
        self.assertNotEqual(echo, jet)
        self.assertNotEqual(echo, plain)


class BridgeWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.logged: list[tuple[str, dict]] = []
        logged = self.logged

        class FakeLog:
            def write(self, event: str, **fields: object) -> None:
                logged.append((event, dict(fields)))

        self.daemon = types.SimpleNamespace(root=self.root, log=FakeLog())
        self.bridge = MBM.MacBridge.__new__(MBM.MacBridge)
        self.bridge.daemon = self.daemon
        self.bridge.path = os.path.join(L.listener_dir(self.root), MBM.STATE_NAME)

    def test_bad_bridge_item_is_skipped_and_cursor_advances(self) -> None:
        state: dict = {"cursors": {}, "decisions": {}}
        envelope = {"gap": False, "next": 6,
                    "items": [{"source_seat": "WRONG", "id": 99, "seq": 6}, "not-a-dict"],
                    "budget": {}}
        self.bridge._process_envelope(state, state["cursors"], state["decisions"], "ECHO",
                                      envelope, remote=True, binary="muse", model="m")
        self.assertEqual(state["cursors"]["ECHO"], 6)
        bad = [row for row in self.logged if row[0] == "muse-bridge-bad-item"]
        self.assertEqual(len(bad), 2)
        self.assertEqual(state["decisions"], {})

    def test_deliver_reuses_cached_catalog_without_forced_rescan(self) -> None:
        fresh_row = {"id": "t1", "seat": "GROK", "platform": "cursor", "host": "mac",
                     "session_id": "s1", "cwd": "/tmp", "title": "t",
                     "status": "available", "stale": False,
                     "capabilities": {"read": True, "wake": True}}

        class FakeCatalog:
            def __init__(self) -> None:
                self.forced = 0

            def resolve(self, record_id: object) -> dict | None:
                return dict(fresh_row) if record_id == "t1" else None

            def discover(self, force: bool = False, **kwargs: object) -> list:
                if force:
                    self.forced += 1
                return []

        catalog = FakeCatalog()
        enqueued: list = []

        class FakeQueue:
            def enqueue_target(self, binding: dict, message_id: object, now: float,
                               **kwargs: object) -> bool:
                enqueued.append((binding, message_id, kwargs))
                return True

        class FakeExchange:
            def request(self, command: str, body: object) -> dict:
                return {"receipt": "r1"}

        cfg = _mac_config("ECHO", "GROK")
        self.daemon.config = cfg
        self.daemon.clock = types.SimpleNamespace(time=time.time)
        self.daemon.chat_queue = lambda seat: FakeQueue()
        self.daemon.seats = {"GROK": types.SimpleNamespace(cfg=types.SimpleNamespace(enabled=True))}
        self.bridge.catalog = catalog
        self.bridge.exchange = FakeExchange()
        item = {"source_seat": "ECHO", "id": 7, "seq": 7, "type": "stream",
                "channel": "agent-sync", "topic": "t", "content": "hi",
                "owner": False, "ts": time.time(), "stale": False}
        decision = {"kind": "chat", "target_id": "t1",
                    "target": {"platform": "cursor", "host": "mac",
                               "session_id": "s1", "cwd": "/tmp"},
                    "destination_seat": "GROK"}
        self.bridge._deliver(item, decision, remote=True)
        self.assertEqual(catalog.forced, 0)
        self.assertEqual(len(enqueued), 1)
        self.assertEqual(enqueued[0][2].get("source_seat"), "ECHO")
        # A cache miss still forces exactly one rescan before refusing the delivery.
        missing = dict(decision, target_id="gone")
        with self.assertRaises(ValueError):
            self.bridge._deliver(item, missing, remote=True)
        self.assertEqual(catalog.forced, 1)

    def _recorded(self, state: dict, item: dict, kind: str = "hold") -> str:
        key = MBM.MacBridge._key(item)
        state["decisions"][key] = {"kind": kind, "reason": "test", "model_attempted": True,
                                   "at": time.time(), "source_seat": item["source_seat"], "id": item["id"]}
        return key

    def test_refused_delivery_is_recorded_and_the_cursor_moves_past_it(self) -> None:
        submitted: list = []
        answers = [{"refused": "source message became stale"}, {"receipt": "r2"}]

        class FakeExchange:
            def request(self, command: str, body: dict) -> dict:
                submitted.append(body["id"])
                return answers.pop(0)

        self.daemon.config = _mac_config("ECHO")
        self.bridge.exchange = FakeExchange()
        first = {"source_seat": "ECHO", "id": 31, "seq": 3, "owner": False, "ts": time.time()}
        second = {"source_seat": "ECHO", "id": 32, "seq": 4, "owner": False, "ts": time.time()}
        state: dict = {"cursors": {"ECHO": 2}, "decisions": {}}
        k1, k2 = self._recorded(state, first), self._recorded(state, second)
        envelope = {"gap": False, "next": 4, "items": [first, second], "budget": {}}
        self.bridge._process_envelope(state, state["cursors"], state["decisions"], "ECHO",
                                      envelope, remote=True, binary="muse", model="m")
        self.assertEqual(state["cursors"]["ECHO"], 4)
        self.assertEqual(submitted, [31, 32])
        self.assertEqual(state["decisions"][k1]["delivery"], "refused")
        self.assertIn("became stale", state["decisions"][k1]["refused_reason"])
        self.assertEqual(state["decisions"][k2]["delivery"], "done")
        self.assertEqual([row[1]["id"] for row in self.logged if row[0] == "muse-bridge-delivery-refused"], [31])
        # A re-exported batch never sends a settled decision again, refused or done.
        self.bridge._process_envelope(state, state["cursors"], state["decisions"], "ECHO",
                                      envelope, remote=True, binary="muse", model="m")
        self.assertEqual(submitted, [31, 32])

    def test_ineligible_recorded_decision_is_refused_before_any_submit(self) -> None:
        submitted: list = []

        class FakeExchange:
            def request(self, command: str, body: dict) -> dict:
                submitted.append(body["id"])
                return {"receipt": "r"}

        self.daemon.config = _mac_config("ECHO")
        self.bridge.exchange = FakeExchange()
        stale = {"source_seat": "ECHO", "id": 41, "seq": 5, "owner": False, "ts": time.time(), "stale": True}
        blocked = {"source_seat": "ECHO", "id": 42, "seq": 6, "owner": False, "ts": time.time(), "blocked": True}
        state: dict = {"cursors": {}, "decisions": {}}
        keys = [self._recorded(state, stale, "native"), self._recorded(state, blocked, "native")]
        envelope = {"gap": False, "next": 6, "items": [stale, blocked], "budget": {}}
        self.bridge._process_envelope(state, state["cursors"], state["decisions"], "ECHO",
                                      envelope, remote=True, binary="muse", model="m")
        self.assertEqual(submitted, [])
        self.assertEqual(state["cursors"]["ECHO"], 6)
        self.assertEqual([state["decisions"][k]["delivery"] for k in keys], ["refused", "refused"])

    def test_fresh_decision_keeps_its_budget_fields_after_delivery(self) -> None:
        class FakeExchange:
            def request(self, command: str, body: dict) -> dict:
                return {"receipt": "r"}

        self.daemon.config = _mac_config("ECHO")
        self.bridge.exchange = FakeExchange()
        self.bridge._budget_reason = lambda *args: None
        self.bridge._decide = lambda *args: {"kind": "hold", "reason": "no_target", "model_attempted": True}
        item = {"source_seat": "ECHO", "id": 61, "seq": 8, "owner": False, "ts": time.time(),
                "channel": "agent-sync", "topic": "t"}
        state: dict = {"cursors": {}, "decisions": {}}
        envelope = {"gap": False, "next": 8, "items": [item], "budget": {}}
        self.bridge._process_envelope(state, state["cursors"], state["decisions"], "ECHO",
                                      envelope, remote=True, binary="muse", model="m")
        stored = state["decisions"][MBM.MacBridge._key(item)]
        self.assertEqual((stored["source_seat"], stored["id"], stored["delivery"]), ("ECHO", 61, "done"))
        self.assertIsInstance(stored["at"], float)
        self.assertEqual(stored["topic_key"], L.topic_key("agent-sync", "t"))
        self.assertTrue(stored["model_attempted"])

    def test_transport_failure_leaves_the_cursor_for_a_retry(self) -> None:
        class DeadExchange:
            def request(self, command: str, body: dict) -> dict:
                raise RuntimeError("server bridge exchange unavailable")

        self.daemon.config = _mac_config("ECHO")
        self.bridge.exchange = DeadExchange()
        item = {"source_seat": "ECHO", "id": 51, "seq": 7, "owner": False, "ts": time.time()}
        state: dict = {"cursors": {"ECHO": 6}, "decisions": {}}
        key = self._recorded(state, item)
        envelope = {"gap": False, "next": 7, "items": [item], "budget": {}}
        with self.assertRaises(RuntimeError):
            self.bridge._process_envelope(state, state["cursors"], state["decisions"], "ECHO",
                                          envelope, remote=True, binary="muse", model="m")
        self.assertEqual(state["cursors"]["ECHO"], 6)
        self.assertNotIn("delivery", state["decisions"][key])


class BridgeBinaryTest(unittest.TestCase):
    def _bindir(self, *names: str) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name in names:
            path = os.path.join(tmp.name, name)
            with open(path, "w", encoding="utf-8"):
                pass
            os.chmod(path, 0o755)
        return tmp.name

    def test_prefers_newest_versioned_binary_over_shim(self) -> None:
        bindir = self._bindir("muse", "muse-bin-1.4.7-R6101.1", "muse-bin-1.4.9-R6297.1")
        self.assertEqual(MBM.resolve_bridge_muse(bindir),
                         os.path.realpath(os.path.join(bindir, "muse-bin-1.4.9-R6297.1")))

    def test_orders_versions_numerically_not_lexically(self) -> None:
        bindir = self._bindir("muse-bin-1.4.9-R6297.1", "muse-bin-1.4.10-R6427.1")
        self.assertEqual(MBM.resolve_bridge_muse(bindir),
                         os.path.realpath(os.path.join(bindir, "muse-bin-1.4.10-R6427.1")))

    def test_falls_back_without_versioned_binary(self) -> None:
        self.assertIsNone(MBM.resolve_bridge_muse(self._bindir()))


if __name__ == "__main__":
    raise SystemExit(unittest.main())
