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

from agent_sync import listener_cli as CLI
from agent_sync import live as L
from agent_sync import linear_projects_hook as H
from agent_sync import router as R
from agent_sync import zulip as Z
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

    def test_redrive_never_expires_on_its_own(self) -> None:
        """No response class proves deletion:  Zulip answers an inaccessible message with
        the same 4xx as a deleted one, so even a definite miss from the origin keeps the
        kind parked and visible (GATE 2; the operator investigates the named message)."""
        daemon = self.started()
        c = types.SimpleNamespace(channel="fleet", topic="t")
        daemon._defer_broadcast_effect({"id": 99999999}, c, "fanout", seat="CLAUDE")
        daemon._redrive_deferred_broadcasts()
        self.assertIn("99999999", L.read_json(daemon._deferred_broadcasts_path()))

    def test_redrive_bounds_fetches_per_tick(self) -> None:
        """F8:  N parked ids must not block one pass on N synchronous fetches.  The pass
        completes at most REDRIVE_FETCHES_PER_TICK probes;  the rest wait, and id rotation
        reaches them on a later pass once their backoff lets go."""
        from agent_sync.daemon import REDRIVE_FETCHES_PER_TICK

        daemon = self.started()
        c = types.SimpleNamespace(channel="fleet", topic="t")
        for i in range(REDRIVE_FETCHES_PER_TICK + 2):
            daemon._defer_broadcast_effect({"id": 90000000 + i}, c, "fanout", seat="CLAUDE")
        seen = []

        def probe(runner, mid):
            seen.append(mid)
            return None

        with mock.patch.object(daemon, "_probe_deferred_payload", side_effect=probe):
            daemon._redrive_deferred_broadcasts()
            self.assertEqual(len(seen), REDRIVE_FETCHES_PER_TICK)
            first_pass = list(seen)
            # Past the backoff, rotation probes the ids the bound skipped FIRST.
            self.clock.advance(31)
            seen.clear()
            daemon._redrive_deferred_broadcasts()
            skipped = [m for m in seen if m not in first_pass]
            self.assertEqual(len(skipped), 2)
        parked = L.read_json(daemon._deferred_broadcasts_path())
        self.assertEqual(len(parked), REDRIVE_FETCHES_PER_TICK + 2)

    def test_redrive_attempt_budget_caps_runner_fanout(self) -> None:
        """F8:  the per-pass bound counts REST ATTEMPTS, not ids:  R connected runners must
        not multiply one pass's synchronous cost to R x timeout per id."""
        from agent_sync.daemon import REDRIVE_ATTEMPTS_PER_TICK

        daemon = self.started()
        runner = daemon.seats["CLAUDE"]
        for i in range(3):
            blind = types.SimpleNamespace(
                connected=True, client=mock.Mock(), me=runner.me, seat="BLIND%d" % i
            )
            daemon.seats["BLIND%d" % i] = blind
        try:
            c = types.SimpleNamespace(channel="fleet", topic="t")
            daemon._defer_broadcast_effect({"id": 90000030}, c, "fanout", seat="CLAUDE")
            with mock.patch.object(
                daemon, "_probe_deferred_payload", return_value=None
            ) as fetch:
                daemon._redrive_deferred_broadcasts()
                # 4 connected runners, but the pass spends only the attempt budget.
                self.assertEqual(fetch.call_count, REDRIVE_ATTEMPTS_PER_TICK)
            self.assertIn("90000030", L.read_json(daemon._deferred_broadcasts_path()))
        finally:
            for i in range(3):
                daemon.seats.pop("BLIND%d" % i, None)

    def test_redrive_rotates_the_first_runner(self) -> None:
        """F8:  the first probed runner rotates per probe, so a dead or slow first runner
        is never every pass's opening cost."""
        daemon = self.started()
        runner = daemon.seats["CLAUDE"]
        blind = types.SimpleNamespace(
            connected=True, client=mock.Mock(), me=runner.me, seat="BLIND"
        )
        daemon.seats["BLIND"] = blind
        try:
            c = types.SimpleNamespace(channel="fleet", topic="t")
            daemon._defer_broadcast_effect({"id": 90000040}, c, "fanout", seat="CLAUDE")
            order = []

            def probe(r, mid):
                order.append(r.seat)
                return None

            with mock.patch.object(daemon, "_probe_deferred_payload", side_effect=probe):
                daemon._redrive_deferred_broadcasts()
                self.assertEqual(order, ["CLAUDE", "BLIND"])
                self.clock.advance(31)
                order.clear()
                daemon._redrive_deferred_broadcasts()
                self.assertEqual(order[0], "BLIND")
        finally:
            daemon.seats.pop("BLIND", None)

    def test_redrive_backs_off_unknown_probes(self) -> None:
        """F8:  an id whose payload stays unknown is not re-probed every tick.  The backoff
        paces the next probe (doubling to the cap);  it never expires the durable row."""
        daemon = self.started()
        c = types.SimpleNamespace(channel="fleet", topic="t")
        daemon._defer_broadcast_effect({"id": 90000010}, c, "fanout", seat="CLAUDE")
        with mock.patch.object(
            daemon, "_probe_deferred_payload", return_value=None
        ) as fetch:
            daemon._redrive_deferred_broadcasts()
            self.assertEqual(fetch.call_count, 1)
            # Inside the 30s base backoff:  no new probe.
            self.clock.advance(29)
            daemon._redrive_deferred_broadcasts()
            self.assertEqual(fetch.call_count, 1)
            # Past it:  one probe, and the backoff doubles.
            self.clock.advance(2)
            daemon._redrive_deferred_broadcasts()
            self.assertEqual(fetch.call_count, 2)
            self.clock.advance(59)
            daemon._redrive_deferred_broadcasts()
            self.assertEqual(fetch.call_count, 2)
            self.clock.advance(2)
            daemon._redrive_deferred_broadcasts()
            self.assertEqual(fetch.call_count, 3)
        self.assertIn("90000010", L.read_json(daemon._deferred_broadcasts_path()))

    def test_redrive_skips_probe_with_no_connected_runner(self) -> None:
        """F8:  with no connected seat a pass performs no fetch and sets no backoff."""
        daemon = self.started()
        c = types.SimpleNamespace(channel="fleet", topic="t")
        daemon._defer_broadcast_effect({"id": 90000020}, c, "fanout", seat="CLAUDE")
        runner = daemon.seats["CLAUDE"]
        runner.connected = False
        try:
            with mock.patch.object(
                daemon, "_probe_deferred_payload", return_value=None
            ) as fetch:
                daemon._redrive_deferred_broadcasts()
                self.assertEqual(fetch.call_count, 0)
            self.assertFalse(getattr(daemon, "_deferred_redrive_retry_at", {}))
        finally:
            runner.connected = True
        self.assertIn("90000020", L.read_json(daemon._deferred_broadcasts_path()))

    def test_inbox_and_audit_park_under_drain(self) -> None:
        daemon = self.started()
        mid, message, c = self._owner_broadcast(daemon)
        set_pause(
            daemon.root, None, wakes_only=False, by="drain", now=self.clock.time(), defer=True
        )
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


    def test_ordinary_pause_does_not_park_broadcasts(self) -> None:
        """An ordinary global pause carries no drain marker:  broadcast admissions keep their
        pre-AFC-652 behavior and start immediately.  Only the drain window parks."""
        daemon = self.started()
        mid, message, c = self._owner_broadcast(daemon)
        with mock.patch.object(daemon, "fanout") as fan, mock.patch.object(
            daemon, "_deliver_inbox"
        ), mock.patch.object(daemon, "_run_broadcast_audit"):
            set_pause(daemon.root, None, wakes_only=False, by="cli", now=self.clock.time())
            daemon.start_fanout(message, c)
            daemon.start_inbox_delivery(message, c)
            daemon.schedule_broadcast_audit(message, c)
            self.assertTrue(wait_for(lambda: fan.called))
        self.assertIn(mid, list(daemon._fanout_seen))
        self.assertIn(mid, list(daemon._inbox_seen))
        self.assertIn(mid, daemon._broadcast_audits)
        self.assertFalse(L.read_json(daemon._deferred_broadcasts_path()))
        clear_pause(daemon.root, None)

    def test_redrive_stays_parked_when_origin_loses_channel_access(self) -> None:
        """The access-revoked private-channel case:  the origin bot is removed from the
        channel mid-window, so its 4xx now means "cannot see", not "deleted".  With every
        other runner blind too, the re-drive must keep the real broadcast parked."""
        daemon = self.started()
        runner = daemon.seats["CLAUDE"]
        blind = types.SimpleNamespace(
            connected=True, client=mock.Mock(), me=runner.me, seat="BLIND"
        )
        blind.client.get.side_effect = Z.ApiError("not found", code="BAD_MSG_ID", status=404)
        daemon.seats["BLIND"] = blind
        c = types.SimpleNamespace(channel="fleet", topic="t")
        mid, message, _c = self._owner_broadcast(daemon)
        orig_client = runner.client
        try:
            daemon._defer_broadcast_effect(message, c, "fanout", seat="CLAUDE")
            # The origin now answers the same 400 an inaccessible private message gets.
            runner.client = mock.Mock()
            runner.client.get.side_effect = Z.ApiError(
                "invalid message", code="BAD_REQUEST", status=400
            )
            daemon._redrive_deferred_broadcasts()
            self.assertIn(str(mid), L.read_json(daemon._deferred_broadcasts_path()))
            # A runner that CAN still fetch the payload re-drives it normally, once the
            # unknown-probe backoff (F8) has passed.
            runner.client = orig_client
            self.clock.advance(31)
            with mock.patch.object(daemon, "fanout") as fan:
                daemon._redrive_deferred_broadcasts()
                self.assertTrue(wait_for(lambda: fan.called))
            self.assertNotIn(str(mid), L.read_json(daemon._deferred_broadcasts_path()) or {})
        finally:
            runner.client = orig_client
            daemon.seats.pop("BLIND", None)

class RunWakeDrainTests(DaemonHarness):
    """The run-time recheck (F7 extension):  a wake admitted before the drain whose turn
    lands inside the window re-arms instead of dropping, exactly like the finalize path."""

    def _take_offered(self, daemon):
        mid = self.fake.add_message("Codex", "agent-sync", "t", MENTION, deliver=False)
        daemon.route_message(
            daemon.seats["CLAUDE"], dict(self.fake.messages[-1], flags=["mentioned"])
        )
        ready = daemon.coalescer.take_due(self.clock.time() + 60)
        self.assertEqual(len(ready), 1)
        return ready[0]

    def test_run_wake_recheck_defers_under_drain(self) -> None:
        daemon = self.started()
        runner = daemon.seats["CLAUDE"]
        pending = self._take_offered(daemon)
        set_pause(
            daemon.root, None, wakes_only=False, by="drain", now=self.clock.time(), defer=True
        )
        daemon.run_wake(runner, pending)
        # Re-armed, not dropped:  the pending wake is back in the coalescer, no drop row.
        self.assertEqual(len(daemon.coalescer.pending), 1)
        self.assertEqual([r for r in self.ledger() if r["state"] == "dropped"], [])
        # The re-armed wake runs once the marker lifts.
        clear_pause(daemon.root, None)
        self.clock.advance(30)
        daemon.tick()
        self.assertEqual(len(daemon.coalescer.pending), 0)
        self.assertEqual(len(daemon.seats["CLAUDE"].jobs), 1)

    def test_run_wake_recheck_drops_under_an_ordinary_pause(self) -> None:
        daemon = self.started()
        runner = daemon.seats["CLAUDE"]
        pending = self._take_offered(daemon)
        set_pause(daemon.root, None, wakes_only=False, by="cli", now=self.clock.time())
        daemon.run_wake(runner, pending)
        self.assertEqual(len(daemon.coalescer.pending), 0)
        drops = [r for r in self.ledger() if r["state"] == "dropped"]
        self.assertEqual([r["reason"] for r in drops], ["paused"])
        clear_pause(daemon.root, None)


class DeferScopeTests(unittest.TestCase):
    """--defer is fleet-wide drain semantics;  the CLI refuses a per-seat drain marker."""

    def test_defer_with_seat_is_refused(self) -> None:
        with self.assertRaises(Z.UsageError):
            CLI._check_defer_scope("CLAUDE", True)

    def test_global_defer_and_plain_seat_pause_pass(self) -> None:
        CLI._check_defer_scope(None, True)
        CLI._check_defer_scope("CLAUDE", False)
        CLI._check_defer_scope(None, False)


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
