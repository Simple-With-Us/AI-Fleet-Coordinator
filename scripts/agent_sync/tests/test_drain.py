"""AFC-652 drain soundness:  the defer marker, wake defer (F7), broadcast parking and
re-drive (F2), the Linear hook gate (F6), the webhook high-water (F3), and the
wake-offer fence (F4).

Run:  cd scripts && python3 -m unittest agent_sync.tests.test_drain
"""

from __future__ import annotations

import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from agent_sync import live as L
from agent_sync import linear_projects_hook as H
from agent_sync import router as R
from agent_sync.daemon import Ring, clear_pause, drain_deferred, set_pause
from agent_sync.tests.test_daemon import MENTION, DaemonHarness
from agent_sync.tests.test_linear_projects_hook import SECRET, project_event, signed


def wait_for(predicate, seconds: float = 5.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class DrainMarkerTests(unittest.TestCase):
    """The "defer" flag is the drain window's only distinguishing bit;  an ordinary
    pause has no marker and keeps its recorded-drop semantics."""

    def test_defer_marker_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = str(Path(td) / "listener")
            set_pause(root, None, wakes_only=False, by="drain", now=1.0, defer=True)
            self.assertTrue(drain_deferred(root))
            clear_pause(root, None)
            self.assertFalse(drain_deferred(root))

    def test_ordinary_pause_carries_no_marker(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = str(Path(td) / "listener")
            set_pause(root, None, wakes_only=False, by="cli", now=1.0)
            self.assertFalse(drain_deferred(root))
            set_pause(root, "CLAUDE", wakes_only=True, by="cli", now=1.0, defer=True)
            self.assertTrue(drain_deferred(root))


class WakeDeferTests(DaemonHarness):
    def _offer_one(self, daemon) -> int:
        mid = self.fake.add_message("Codex", "agent-sync", "t", MENTION, deliver=False)
        daemon.route_message(
            daemon.seats["CLAUDE"], dict(self.fake.messages[-1], flags=["mentioned"])
        )
        self.assertEqual(len(daemon.coalescer.pending), 1)
        return mid

    def test_queued_wake_defers_under_drain_pause_and_runs_after(self) -> None:
        daemon = self.started()
        self._offer_one(daemon)
        set_pause(daemon.root, None, wakes_only=False, by="drain", now=self.clock.time(), defer=True)
        self.clock.advance(30)
        daemon.tick()
        # Deferred, not dropped:  the pending wake is still parked and no drop row exists.
        self.assertEqual(len(daemon.coalescer.pending), 1)
        self.assertEqual([r for r in self.ledger() if r["state"] == "dropped"], [])
        clear_pause(daemon.root, None)
        self.clock.advance(30)
        daemon.tick()
        self.assertEqual(len(daemon.coalescer.pending), 0)
        self.assertEqual(len(daemon.seats["CLAUDE"].jobs), 1)

    def test_queued_wake_still_drops_under_an_ordinary_pause(self) -> None:
        daemon = self.started()
        self._offer_one(daemon)
        set_pause(daemon.root, None, wakes_only=False, by="cli", now=self.clock.time())
        self.clock.advance(30)
        daemon.tick()
        self.assertEqual(len(daemon.coalescer.pending), 0)
        drops = [r for r in self.ledger() if r["state"] == "dropped"]
        self.assertEqual([r["reason"] for r in drops], ["paused"])
        clear_pause(daemon.root, None)

    def test_replayed_message_does_not_offer_a_second_wake(self) -> None:
        daemon = self.started()
        runner = daemon.seats["CLAUDE"]
        mid = self._offer_one(daemon)
        self.assertTrue(daemon._wake_already_recorded(runner, mid))
        self.assertFalse(daemon._wake_already_recorded(runner, mid + 1000))
        # A post-restart replay:  the ring is empty again while the durable ledger persists.
        runner.ring = Ring()
        daemon.route_message(runner, dict(self.fake.messages[-1], flags=["mentioned"]))
        queued = [
            r for r in self.ledger() if r["state"] == "queued" and mid in (r.get("trigger_ids") or [])
        ]
        self.assertEqual(len(queued), 1)


class BroadcastDeferralTests(DaemonHarness):
    def _owner_broadcast(self, daemon):
        runner = daemon.seats["CLAUDE"]
        mid = self.fake.add_message(
            "Jay Wedgeworth", "fleet", "t", "@all hands", deliver=False
        )
        message = dict(
            self.fake.messages[-1],
            flags=["wildcard_mentioned", "stream_wildcard_mentioned"],
        )
        c, _d = R.route(message, runner.me, daemon.context(runner), daemon.lease_views(runner))
        return mid, message, c

    def test_fanout_parks_under_pause_and_redrives_once(self) -> None:
        daemon = self.started()
        mid, message, c = self._owner_broadcast(daemon)
        with mock.patch.object(daemon, "fanout") as fan:
            set_pause(daemon.root, None, wakes_only=False, by="drain", now=self.clock.time(), defer=True)
            daemon.start_fanout(message, c)
            self.assertFalse(fan.called)
            self.assertNotIn(mid, list(daemon._fanout_seen))
            parked = L.read_json(daemon._deferred_broadcasts_path())
            self.assertEqual(parked[str(mid)]["effects"], {"fanout": False})
            clear_pause(daemon.root, None)
            daemon._redrive_deferred_broadcasts()
            self.assertTrue(wait_for(lambda: fan.called))
            self.assertEqual(L.read_json(daemon._deferred_broadcasts_path()), {})
            # A second re-drive finds nothing parked and starts nothing new.
            fan.reset_mock()
            daemon._redrive_deferred_broadcasts()
            self.assertFalse(wait_for(lambda: fan.called, seconds=0.3))

    def test_redrive_expires_a_message_that_is_gone(self) -> None:
        daemon = self.started()
        c = types.SimpleNamespace(channel="fleet", topic="t")
        daemon._defer_broadcast_effect({"id": 99999999}, c, "fanout")
        daemon._redrive_deferred_broadcasts()
        self.assertEqual(L.read_json(daemon._deferred_broadcasts_path()), {})

    def test_inbox_and_audit_park_under_pause(self) -> None:
        daemon = self.started()
        mid, message, c = self._owner_broadcast(daemon)
        set_pause(daemon.root, None, wakes_only=False, by="cli", now=self.clock.time())
        with mock.patch.object(daemon, "_deliver_inbox"), mock.patch.object(
            daemon, "_run_broadcast_audit"
        ):
            daemon.start_inbox_delivery(message, c)
            daemon.schedule_broadcast_audit(message, c)
        self.assertNotIn(mid, list(daemon._inbox_seen))
        self.assertNotIn(mid, daemon._broadcast_audits)
        parked = L.read_json(daemon._deferred_broadcasts_path())
        self.assertEqual(
            parked[str(mid)]["effects"], {"inbox": False, "audit": False}
        )
        clear_pause(daemon.root, None)


class HookGateTests(unittest.TestCase):
    def _hook(self, armed: bool):
        runs = []
        hook = H.LinearProjectsHook(
            lambda: {"LINEAR_WEBHOOK_SECRET": SECRET},
            lambda secrets: runs.append(secrets),
            clock=time.time,
            log=lambda *a, **k: None,
            drain_armed=lambda: armed,
        )
        return hook, runs

    def test_503_while_drained_then_200_after(self) -> None:
        hook, runs = self._hook(True)
        body, heads = signed(project_event())
        status, answer = hook.handle("POST", H.PATH, heads, body)
        self.assertEqual((status, answer), (503, {"error": "drain"}))
        self.assertEqual(runs, [])
        self.assertFalse(hook.queued)
        hook._drain_armed = lambda: False
        status, answer = hook.handle("POST", H.PATH, heads, body)
        self.assertEqual((status, answer), (200, {"ok": True, "queued": True}))

    def test_default_hook_never_gates(self) -> None:
        runs = []
        hook = H.LinearProjectsHook(
            lambda: {"LINEAR_WEBHOOK_SECRET": SECRET},
            lambda secrets: runs.append(secrets),
            clock=time.time,
            log=lambda *a, **k: None,
        )
        body, heads = signed(project_event())
        self.assertEqual(hook.handle("POST", H.PATH, heads, body)[0], 200)


class HighWaterTests(DaemonHarness):
    def test_webhook_high_water_tracks_non_cursor_routes_only(self) -> None:
        daemon = self.started()
        runner = daemon.seats["CLAUDE"]
        mid1 = self.fake.add_message("Codex", "agent-sync", "t", "plain one", deliver=False)
        daemon._route_message(runner, self.fake.messages[-1], advance_cursor=False)
        self.assertEqual(runner.webhook_high_water, mid1)
        mid2 = self.fake.add_message("Codex", "agent-sync", "t", "plain two", deliver=False)
        daemon._route_message(runner, self.fake.messages[-1], advance_cursor=True)
        self.assertEqual(runner.webhook_high_water, mid1)
        status = daemon.status()
        self.assertEqual(status["seats"]["CLAUDE"]["webhook_high_water"], mid1)
        self.assertFalse(status["drain"]["armed"])
        self.assertIn("spool", status["drain"]["outgoing_webhook"])
        set_pause(daemon.root, None, wakes_only=False, by="drain", now=self.clock.time(), defer=True)
        self.assertTrue(daemon.status()["drain"]["armed"])
        clear_pause(daemon.root, None)


if __name__ == "__main__":
    unittest.main()
