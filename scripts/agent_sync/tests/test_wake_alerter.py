"""Tests for the Mac wake alerter (hosted-listener thin client)."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error

from agent_sync import wake_alerter as WA

KEY = "r" * 40
SEAT = "ECHO"
BASE = "http://server.boa-roygbiv.ts.net:8788"


def payload(items):
    return {"contract": "agent-sync-read/1", "seat": SEAT, "record": "wakes",
            "view": {"seat": SEAT, "total": len(items), "by_state": {}, "items": items}}


class Resp:
    def __init__(self, data):
        self._raw = json.dumps(data).encode()

    def read(self, _n):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class AlerterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = os.path.join(self.temp.name, "state.json")
        self.ran = []

    def poll(self, items=None, opener=None):
        if opener is None:
            def opener(req, timeout=0):
                return Resp(payload(items or []))
        return WA.poll_once(BASE, SEAT, KEY, self.state, runner=self.ran.append, opener=opener)

    def test_new_wake_banners_once(self):
        items = [{"wake_id": "w1", "state": "new", "channel": "agent-sync", "topic": "fleet"}]
        self.assertEqual(self.poll(items), 1)
        self.assertEqual(len(self.ran), 1)
        argv = self.ran[0]
        self.assertEqual(argv[0], "/usr/bin/osascript")
        self.assertIn("wake in agent-sync / fleet", argv)
        self.assertIn("agent-sync wake (ECHO)", argv)
        # second poll with the same wake: no repeat banner
        self.assertEqual(self.poll(items), 0)
        self.assertEqual(len(self.ran), 1)

    def test_dm_without_channel_banners_dm(self):
        self.assertEqual(self.poll([{"wake_id": "w2", "state": "new", "dm": True}]), 1)
        self.assertIn("wake in DM", self.ran[0])

    def test_seen_state_survives_restart(self):
        self.assertEqual(self.poll([{"wake_id": "w3", "state": "new"}]), 1)
        self.ran.clear()
        # a fresh "process" (new runner list, same state file) does not repeat
        self.assertEqual(self.poll([{"wake_id": "w3", "state": "new"}]), 0)
        self.assertEqual(self.ran, [])

    def test_server_down_banners_nothing_and_keeps_state(self):
        def boom(req, timeout=0):
            raise urllib.error.URLError("refused")

        self.assertEqual(self.poll([{"wake_id": "w4", "state": "new"}]), 1)
        self.assertEqual(self.poll(opener=boom), 0)
        self.assertEqual(self.poll([{"wake_id": "w4", "state": "new"}]), 0)

    def test_malformed_items_are_skipped(self):
        items = [None, "x", {"state": "new"}, {"wake_id": 7}, {"wake_id": "w5", "state": "new"}]
        self.assertEqual(self.poll(items), 1)
        self.assertEqual(len(self.ran), 1)

    def test_request_is_signed_for_the_exact_query(self):
        seen_req = []

        def opener(req, timeout=0):
            seen_req.append(req)
            return Resp(payload([]))

        self.poll(opener=opener)
        req = seen_req[0]
        path = "/listener/v1/ECHO/wakes"
        stamp = req.headers["X-agent-sync-timestamp"]
        from agent_sync.listener_api import sign

        self.assertEqual(req.headers["X-agent-sync-signature"], sign(KEY, path, WA.QUERY, stamp))
        self.assertTrue(req.full_url.endswith(path + "?" + WA.QUERY))


if __name__ == "__main__":
    unittest.main()
