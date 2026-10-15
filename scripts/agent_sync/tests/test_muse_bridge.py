"""AFC-326: the default-off Muse inbox bridge keeps source-qualified receipts.

Same Zulip message ID under two source seats must never collapse into one
decision, one Mac delivery, or one chat nudge.  A crashed native claim must
stay uncertain instead of being delivered twice on restart.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest

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


if __name__ == "__main__":
    raise SystemExit(unittest.main())
