"""AFC-652 drain soundness:  the defer marker, wake defer (F7), broadcast parking and
re-drive (F2), the Linear hook gate (F6), the webhook high-water (F3), and the
wake-offer fence (F4).

Run:  cd scripts && python3 -m unittest simple_tools.tests.test_drain
"""

from __future__ import annotations

import os
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from simple_tools import listener_cli as CLI
from simple_tools import live as L
from simple_tools import linear_projects_hook as H
from simple_tools import router as R
from simple_tools import broadcast_audit as BAUD
from simple_tools import inbox_delivery as IDEL
from simple_tools import zulip as Z
from simple_tools.daemon import INBOX_RETRY_BACKOFF, Ring, clear_pause, drain_deferred, set_pause
from simple_tools.tests.test_daemon import MENTION, DaemonHarness
from simple_tools.tests.test_linear_projects_hook import SECRET, project_event, signed


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
            set_pause(root, "CLAUDE", wakes_only=True, by="cli", now=1.0)
            self.assertFalse(drain_deferred(root))

    def test_per_seat_defer_is_refused(self) -> None:
        """K1:  drain semantics are fleet-wide (wake defer, broadcast parking and the hook
        gate all key on ANY marker), so a per-seat defer entry would arm a fleet-wide drain
        under one seat's name.  The CLI already refuses `pause --defer` with `--seat`;  the
        primitive refuses it too."""
        with tempfile.TemporaryDirectory() as td:
            root = str(Path(td) / "listener")
            with self.assertRaises(ValueError):
                set_pause(root, "CLAUDE", wakes_only=False, by="cli", now=1.0, defer=True)
            self.assertFalse(drain_deferred(root))

    def test_drain_marker_cache_tracks_writes_and_direct_edits(self) -> None:
        """K3:  drain_deferred caches pause.json by (mtime, size):  repeated checks hit the
        cache, set/clear writes invalidate it, and a pause.json replaced behind the cache's
        back is still picked up, because every atomic replace moves the mtime."""
        with tempfile.TemporaryDirectory() as td:
            root = str(Path(td) / "listener")
            self.assertFalse(drain_deferred(root))
            set_pause(root, None, wakes_only=False, by="drain", now=1.0, defer=True)
            self.assertTrue(drain_deferred(root))
            self.assertTrue(drain_deferred(root))  # served from the cache
            clear_pause(root, None)
            self.assertFalse(drain_deferred(root))
            L.write_json(
                os.path.join(L.listener_dir(root), "pause.json"),
                {"*": {"wakes_only": False, "by": "hand", "ts": 2.0, "message_id": None, "defer": True}},
            )
            self.assertTrue(drain_deferred(root))


class WakeDeferTests(DaemonHarness):
    def _offer_one(self, daemon) -> int:
        mid = self.fake.add_message("Codex", "simple-tools", "t", MENTION, deliver=False)
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
        # Deferred, not dropped:  the wake survives the drain (parked in the deferred-restore
        # queue until a tick restores it to the coalescer) and no drop row exists.
        self.assertEqual(
            len(daemon.coalescer.pending) + daemon._deferred_restores.qsize(), 1
        )
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

    def _park(self, daemon, message, kind, seat: str = "CLAUDE") -> None:
        """Plant a parked kind in the durable ledger, as a drain-window admission left it."""
        mid = str(message["id"])

        def plant(data: dict) -> None:
            data[mid] = {
                "mid": message["id"],
                "channel": "fleet",
                "topic": "t",
                "stream_id": message.get("stream_id"),
                "origin_seat": seat,
                "admitted_at": 1.0,
                "snapshot": daemon._broadcast_snapshot(message),
                "effects": {kind: {"state": "pending", "reason": None, "detail": {}}},
                "claims": {},
                "changed_content": [],
            }

        L.update_json(daemon._broadcast_ledger_path(), plant)

    def test_fanout_parks_under_pause_and_redrives_once(self) -> None:
        daemon = self.started()
        mid, message, c = self._owner_broadcast(daemon)
        with mock.patch.object(daemon, "fanout") as fan:
            set_pause(daemon.root, None, wakes_only=False, by="drain", now=self.clock.time(), defer=True)
            daemon.start_fanout(message, c)
            self.assertFalse(fan.called)
            self.assertNotIn(mid, list(daemon._fanout_seen))
            parked = L.read_json(daemon._broadcast_ledger_path())
            self.assertEqual(parked[str(mid)]["effects"]["fanout"]["state"], "pending")
            clear_pause(daemon.root, None)
            daemon._redrive_deferred_broadcasts()
            self.assertTrue(wait_for(lambda: fan.called))
            # The kind finishes when the drive ENDS (R2);  the row persists as the durable
            # seen-ring, so a late route of the same message starts nothing new.
            self.assertTrue(
                wait_for(
                    lambda: L.read_json(daemon._broadcast_ledger_path())
                    .get(str(mid), {})
                    .get("effects", {})
                    .get("fanout", {})
                    .get("state")
                    == "done"
                )
            )
            # A second re-drive finds nothing parked and starts nothing new.
            fan.reset_mock()
            daemon._redrive_deferred_broadcasts()
            self.assertFalse(wait_for(lambda: fan.called, seconds=0.3))

    def test_redrive_never_expires_on_its_own(self) -> None:
        """No response class proves deletion:  Zulip answers an inaccessible message with
        the same 4xx as a deleted one, so even a definite miss from the origin keeps the
        kind parked and visible (GATE 2; the operator investigates the named message)."""
        daemon = self.started()
        self._park(daemon, {"id": 99999999}, "fanout")
        daemon._redrive_deferred_broadcasts()
        self.assertIn("99999999", L.read_json(daemon._broadcast_ledger_path()))

    def test_redrive_bounds_fetches_per_tick(self) -> None:
        """F8:  N parked ids must not block one pass on N synchronous fetches.  The pass
        completes at most REDRIVE_FETCHES_PER_TICK probes;  the rest wait, and id rotation
        reaches them on a later pass once their backoff lets go."""
        from simple_tools.daemon import REDRIVE_FETCHES_PER_TICK

        daemon = self.started()
        for i in range(REDRIVE_FETCHES_PER_TICK + 2):
            self._park(daemon, {"id": 90000000 + i}, "fanout")
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
        parked = L.read_json(daemon._broadcast_ledger_path())
        self.assertEqual(len(parked), REDRIVE_FETCHES_PER_TICK + 2)

    def test_redrive_attempt_budget_caps_runner_fanout(self) -> None:
        """F8:  the per-pass bound counts REST ATTEMPTS, not ids:  R connected runners must
        not multiply one pass's synchronous cost to R x timeout per id."""
        from simple_tools.daemon import REDRIVE_ATTEMPTS_PER_TICK

        daemon = self.started()
        runner = daemon.seats["CLAUDE"]
        for i in range(3):
            blind = types.SimpleNamespace(
                connected=True, client=mock.Mock(), me=runner.me, seat="BLIND%d" % i
            )
            daemon.seats["BLIND%d" % i] = blind
        try:
            self._park(daemon, {"id": 90000030}, "fanout")
            with mock.patch.object(
                daemon, "_probe_deferred_payload", return_value=None
            ) as fetch:
                daemon._redrive_deferred_broadcasts()
                # 4 connected runners, but the pass spends only the attempt budget.
                self.assertEqual(fetch.call_count, REDRIVE_ATTEMPTS_PER_TICK)
            self.assertIn("90000030", L.read_json(daemon._broadcast_ledger_path()))
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
            self._park(daemon, {"id": 90000040}, "fanout")
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
        self._park(daemon, {"id": 90000010}, "fanout")
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
        self.assertIn("90000010", L.read_json(daemon._broadcast_ledger_path()))

    def test_redrive_skips_probe_with_no_connected_runner(self) -> None:
        """F8:  with no connected seat a pass performs no fetch and sets no backoff."""
        daemon = self.started()
        self._park(daemon, {"id": 90000020}, "fanout")
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
        self.assertIn("90000020", L.read_json(daemon._broadcast_ledger_path()))

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
        parked = L.read_json(daemon._broadcast_ledger_path())
        effects = parked[str(mid)]["effects"]
        self.assertEqual(effects["inbox"]["state"], "pending")
        self.assertEqual(effects["audit"]["state"], "pending")
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
        # Nothing is parked:  the fanout finished, the (mocked) delivery and the audit are
        # driving in the durable ledger, which is now the admission record for EVERY
        # broadcast, drain window or not (R1).
        row = L.read_json(daemon._broadcast_ledger_path())[str(mid)]
        self.assertTrue(wait_for(lambda: L.read_json(daemon._broadcast_ledger_path()).get(
            str(mid), {}).get("effects", {}).get("fanout", {}).get("state") == "done"))
        effects = L.read_json(daemon._broadcast_ledger_path())[str(mid)]["effects"]
        self.assertEqual(effects["inbox"]["state"], "driving")
        self.assertEqual(effects["audit"]["state"], "driving")
        clear_pause(daemon.root, None)

    def test_redrive_never_probes_a_non_numeric_id(self) -> None:
        """K4:  the parked id names a REST path segment, so it is validated before it reaches
        one.  A corrupt or hostile row gets no request at all and becomes a terminal failure
        with its reason, so it stays surfaced instead of parked and re-logged forever."""
        daemon = self.started()
        path = daemon._broadcast_ledger_path()

        def plant(data: dict) -> None:
            data["../etc/passwd"] = {
                "mid": None,
                "channel": "fleet",
                "topic": "t",
                "stream_id": None,
                "origin_seat": "CLAUDE",
                "admitted_at": 1.0,
                "snapshot": {},
                "effects": {"fanout": {"state": "pending", "reason": None, "detail": {}}},
                "claims": {},
                "changed_content": [],
            }

        L.update_json(path, plant)
        runner = daemon.seats["CLAUDE"]
        calls = []
        real_get = runner.client.get

        def spy(*a, **kw):
            calls.append(a)
            return real_get(*a, **kw)

        with mock.patch.object(runner.client, "get", side_effect=spy):
            daemon._redrive_deferred_broadcasts()
        self.assertEqual(calls, [])
        effect = L.read_json(path)["../etc/passwd"]["effects"]["fanout"]
        self.assertEqual(effect["state"], "failed")
        self.assertIn("not a message id", effect["reason"])

    def test_a_failing_drive_releases_its_claim_and_stays_parked(self) -> None:
        """K2:  an exception while starting one parked kind releases its claim (never
        stranded) and never aborts the pass:  the next parked id still drives."""
        daemon = self.started()
        mid1, message1, _ = self._owner_broadcast(daemon)
        mid2, message2, _ = self._owner_broadcast(daemon)
        self._park(daemon, message1, "fanout")
        self._park(daemon, message2, "fanout")
        real_start = daemon.start_fanout

        def flaky(message, cc, seat=None):
            if message.get("id") == mid1:
                raise RuntimeError("boom")
            return real_start(message, cc, seat=seat)

        # This tests drive admission/claim recovery, not installed chat discovery.
        # Keep the healthy fanout synthetic so a developer machine cannot read
        # connected chat accounts while running this unit test.
        with (
            mock.patch.object(daemon, "start_fanout", side_effect=flaky),
            mock.patch.object(daemon, "fanout"),
        ):
            daemon._redrive_deferred_broadcasts()
            self.assertTrue(
                wait_for(
                    lambda: L.read_json(daemon._broadcast_ledger_path())
                    .get(str(mid2), {}).get("effects", {}).get("fanout", {}).get("state")
                    == "done"
                )
            )
        data = L.read_json(daemon._broadcast_ledger_path())
        # The failed kind is parked again with no claim held;  the healthy id drove.
        self.assertEqual(data[str(mid1)]["effects"]["fanout"]["state"], "pending")
        self.assertEqual(data[str(mid1)].get("claims"), {})
        self.assertTrue(
            wait_for(
                lambda: L.read_json(daemon._broadcast_ledger_path())
                .get(str(mid2), {})
                .get("effects", {})
                .get("fanout", {})
                .get("state")
                == "done"
            )
        )

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
        mid, message, _c = self._owner_broadcast(daemon)
        orig_client = runner.client
        try:
            self._park(daemon, message, "fanout")
            # The origin now answers the same 400 an inaccessible private message gets.
            runner.client = mock.Mock()
            runner.client.get.side_effect = Z.ApiError(
                "invalid message", code="BAD_REQUEST", status=400
            )
            daemon._redrive_deferred_broadcasts()
            self.assertIn(str(mid), L.read_json(daemon._broadcast_ledger_path()))
            # A runner that CAN still fetch the payload re-drives it normally, once the
            # unknown-probe backoff (F8) has passed.
            runner.client = orig_client
            self.clock.advance(31)
            with mock.patch.object(daemon, "fanout") as fan:
                daemon._redrive_deferred_broadcasts()
                self.assertTrue(wait_for(lambda: fan.called))
            self.assertTrue(
                wait_for(
                    lambda: L.read_json(daemon._broadcast_ledger_path())
                    .get(str(mid), {})
                    .get("effects", {})
                    .get("fanout", {})
                    .get("state")
                    == "done"
                )
            )
        finally:
            runner.client = orig_client
            daemon.seats.pop("BLIND", None)

class RunWakeDrainTests(DaemonHarness):
    """The run-time recheck (F7 extension):  a wake admitted before the drain whose turn
    lands inside the window re-arms instead of dropping, exactly like the finalize path."""

    def _take_offered(self, daemon):
        mid = self.fake.add_message("Codex", "simple-tools", "t", MENTION, deliver=False)
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
        # Re-armed, not dropped:  the wake survives (handed to the deferred-restore queue;
        # the next tick restores it to the coalescer) and no drop row exists.
        self.assertEqual(
            len(daemon.coalescer.pending) + daemon._deferred_restores.qsize(), 1
        )
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
        mid1 = self.fake.add_message("Codex", "simple-tools", "t", "plain one", deliver=False)
        daemon._route_message(runner, self.fake.messages[-1], advance_cursor=False)
        self.assertEqual(runner.webhook_high_water, mid1)
        mid2 = self.fake.add_message("Codex", "simple-tools", "t", "plain two", deliver=False)
        daemon._route_message(runner, self.fake.messages[-1], advance_cursor=True)
        self.assertEqual(runner.webhook_high_water, mid1)
        status = daemon.status()
        self.assertEqual(status["seats"]["CLAUDE"]["webhook_high_water"], mid1)
        self.assertFalse(status["drain"]["armed"])
        self.assertIn("spool", status["drain"]["outgoing_webhook"])
        set_pause(daemon.root, None, wakes_only=False, by="drain", now=self.clock.time(), defer=True)
        self.assertTrue(daemon.status()["drain"]["armed"])
        clear_pause(daemon.root, None)


class SeatInboxDedupTests(DaemonHarness):
    """R6 (AFC-652):  the seat inbox never appends the same message id:kind twice - in
    particular not when a crash fsynced the rows but lost the meta write, and the replay
    that follows re-appends what is already durable."""

    def test_replay_after_lost_meta_write_appends_no_duplicate(self) -> None:
        daemon = self.started()
        runner = daemon.seats["CLAUDE"]
        paths = runner.paths
        row = {
            "id": 424242,
            "kind": "message",
            "channel": "fleet",
            "topic": "t",
            "sender": "Codex",
            "ts": 1,
            "content": "x",
            "classes": [],
        }
        daemon._append_seat_inbox(runner, [dict(row)])
        self.assertEqual(len(L.read_jsonl(paths.seat_inbox)), 1)
        # The crash window: rows fsynced, meta write lost (stale next_seq, no recent keys).
        L.write_json(paths.seat_inbox_meta, {"next_seq": 1})
        daemon._append_seat_inbox(runner, [dict(row)])
        rows = L.read_jsonl(paths.seat_inbox)
        self.assertEqual(len(rows), 1)
        # The tail reconcile repaired the sequence counter as well.
        meta = L.read_json(paths.seat_inbox_meta)
        self.assertGreaterEqual(int(meta.get("next_seq") or 0), 2)
        # A genuinely new message still appends.
        daemon._append_seat_inbox(runner, [dict(row, id=424243)])
        self.assertEqual(len(L.read_jsonl(paths.seat_inbox)), 2)

    def test_a_row_bigger_than_64k_still_dedupes(self) -> None:
        """Boundary:  one row larger than 64 KB (a content-capped message runs ~130 KB) must
        still be inside the tail window, or its replay after a lost meta write duplicates."""
        daemon = self.started()
        runner = daemon.seats["CLAUDE"]
        paths = runner.paths
        row = {
            "id": 424245,
            "kind": "message",
            "channel": "fleet",
            "topic": "t",
            "sender": "Codex",
            "ts": 1,
            "content": "x" * 150000,
            "classes": [],
        }
        daemon._append_seat_inbox(runner, [dict(row)])
        L.write_json(paths.seat_inbox_meta, {"next_seq": 1})
        daemon._append_seat_inbox(runner, [dict(row)])
        self.assertEqual(len(L.read_jsonl(paths.seat_inbox)), 1)

    def test_multi_row_call_bigger_than_the_tail_still_dedupes(self) -> None:
        """Structural window:  a call carrying more rows than the tail holds (the requeue
        batch path) loses its final meta write to a crash;  the replay still dedupes every
        row, because the chunked flush bounds the lost-meta window to one chunk plus one row
        and the surviving meta's remembered keys cover everything before it."""
        daemon = self.started()
        runner = daemon.seats["CLAUDE"]
        paths = runner.paths

        def row(i: int) -> dict:
            return {
                "id": 700000 + i,
                "kind": "message",
                "channel": "fleet",
                "topic": "t",
                "sender": "Codex",
                "ts": 1,
                "content": "y" * 2000,
                "classes": [],
            }

        daemon._append_seat_inbox(runner, [row(i) for i in range(250)])  # ~500 KB, many chunks
        surviving_meta = L.read_json(paths.seat_inbox_meta)
        daemon._append_seat_inbox(runner, [row(i) for i in range(250, 300)])
        # Crash: the second call's rows are durable but its meta writes are lost;  the meta
        # that survives is the one written by the first call's last flush.
        L.write_json(paths.seat_inbox_meta, surviving_meta)
        daemon._append_seat_inbox(runner, [row(i) for i in range(250, 300)])
        self.assertEqual(len(L.read_jsonl(paths.seat_inbox)), 300)

    def test_replay_older_than_keys_and_tail_is_outside_the_window(self) -> None:
        """Boundary, stated honestly:  a replay whose key fell out of the remembered 2000 and
        whose row scrolled past the 256 KB tail is NOT caught by the file dedup.  No caller
        replays from that far back (cursor, spool and the broadcast ledger all re-drive
        through their own durable records);  this test pins the exact boundary so the
        guarantee is never overstated."""
        daemon = self.started()
        runner = daemon.seats["CLAUDE"]
        paths = runner.paths
        old = {
            "id": 424246,
            "kind": "message",
            "channel": "fleet",
            "topic": "t",
            "sender": "Codex",
            "ts": 1,
            "content": "old",
            "classes": [],
        }
        daemon._append_seat_inbox(runner, [dict(old)])
        later = [
            {
                "id": 800000 + i,
                "kind": "message",
                "channel": "fleet",
                "topic": "t",
                "sender": "Codex",
                "ts": 1,
                "content": "z" * 100,
                "classes": [],
            }
            for i in range(2100)  # evicts the key (>2000) and scrolls the row past the tail
        ]
        daemon._append_seat_inbox(runner, later)
        daemon._append_seat_inbox(runner, [dict(old)])
        self.assertEqual(len(L.read_jsonl(paths.seat_inbox)), 2102)  # old row appears twice

    def test_same_id_with_another_kind_is_a_distinct_row(self) -> None:
        daemon = self.started()
        runner = daemon.seats["CLAUDE"]
        paths = runner.paths
        base = {
            "id": 424244,
            "channel": "fleet",
            "topic": "t",
            "sender": "Codex",
            "ts": 1,
            "content": "x",
            "classes": [],
        }
        rows = [dict(base, kind="message"), dict(base, kind="throttle")]
        daemon._append_seat_inbox(runner, rows)
        daemon._append_seat_inbox(runner, rows)
        self.assertEqual(len(L.read_jsonl(paths.seat_inbox)), 2)


if __name__ == "__main__":
    unittest.main()
class BroadcastLedgerCrashTests(DaemonHarness):
    """R9 crash-boundary tests for the durable broadcast-effect ledger:  admission survives a
    crash before any effect (R1), a mid-drive crash re-drives (R2), an ack lost after the
    audit's external send is terminal UNKNOWN and never resent (R5), failures are durable and
    surfaced (R4), and the changed-content decision is recorded (R8)."""

    def _plant(self, daemon, mid, effects, claims=None, **row_kw) -> None:
        row = {
            "mid": mid if isinstance(mid, int) else None,
            "channel": "fleet",
            "topic": "t",
            "stream_id": 104,
            "origin_seat": "CLAUDE",
            "admitted_at": 1.0,
            "snapshot": {},
            "effects": {
                kind: {"state": state, "reason": None, "detail": detail}
                for kind, state, detail in effects
            },
            "claims": claims or {},
            "changed_content": [],
        }
        row.update(row_kw)

        def plant(data: dict) -> None:
            data[str(mid)] = row

        L.update_json(daemon._broadcast_ledger_path(), plant)

    def _effect(self, daemon, mid, kind) -> dict:
        return L.read_json(daemon._broadcast_ledger_path())[str(mid)]["effects"][kind]

    def test_crash_mid_drive_re_parks_and_redrives(self) -> None:
        """Crash after admission, mid-drive:  the claim died with the process, so boot drops
        it and re-parks the kind, and the next re-drive completes it (R1/R2)."""
        daemon = self.started()
        mid, message, _c = BroadcastDeferralTests._owner_broadcast(self, daemon)
        self._plant(
            daemon,
            mid,
            [("fanout", "driving", {})],
            claims={"fanout": 1.0},
            snapshot=daemon._broadcast_snapshot(message),
        )
        daemon._broadcast_ledger_boot()
        row = L.read_json(daemon._broadcast_ledger_path())[str(mid)]
        self.assertEqual(row["claims"], {})
        self.assertEqual(row["effects"]["fanout"]["state"], "pending")
        with mock.patch.object(daemon, "fanout") as fan:
            daemon._redrive_deferred_broadcasts()
            self.assertTrue(wait_for(lambda: fan.called))
            self.assertTrue(
                wait_for(lambda: self._effect(daemon, mid, "fanout")["state"] == "done")
            )

    def test_crash_after_audit_send_attempt_is_unknown_never_resent(self) -> None:
        """R5:  the send-attempt marker survived but no completion did, so boot turns the
        kind UNKNOWN - the reminder may be in the owner's inbox, and no code path sends a
        second one."""
        daemon = self.started()
        self._plant(
            daemon,
            424242,
            [("audit", "driving", {"send_attempted_at": 1.0, "due_at": 1.0})],
            claims={"audit": 1.0},
        )
        daemon._broadcast_ledger_boot()
        effect = self._effect(daemon, 424242, "audit")
        self.assertEqual(effect["state"], "unknown")
        self.assertIn("ack was lost", effect["reason"])
        with mock.patch.object(daemon, "_run_broadcast_audit") as run:
            daemon._start_due_broadcast_audits(self.clock.time() + 10**9)
            self.assertFalse(run.called)
        # R4:  the terminal row is surfaced with its reason.
        status = daemon._drain_status()
        self.assertEqual(status["broadcast_effects"]["unknown"], 1)
        self.assertEqual(
            status["broadcast_effects_terminal"]["rows"][0]["id"], "424242"
        )

    def test_crash_before_audit_send_attempt_reschedules(self) -> None:
        """A driving audit with NO send attempt never sent anything:  boot keeps it driving
        and its durable due time re-runs it."""
        daemon = self.started()
        self._plant(
            daemon,
            424243,
            [
                (
                    "audit",
                    "driving",
                    {
                        "due_at": 2.0,
                        "attempts": 0,
                        "sender_seat": None,
                        "sender_email": "codex@zulip.test",
                        "timestamp": 1.0,
                    },
                )
            ],
            claims={"audit": 1.0},
        )
        daemon._broadcast_ledger_boot()
        self.assertEqual(self._effect(daemon, 424243, "audit")["state"], "driving")
        with mock.patch.object(daemon, "_run_broadcast_audit") as run:
            daemon._start_due_broadcast_audits(self.clock.time())
            self.assertEqual(run.call_count, 1)
            # The in-process fence:  a second due pass does not spawn a second thread.
            daemon._start_due_broadcast_audits(self.clock.time())
            self.assertEqual(run.call_count, 1)

    def test_audit_send_with_lost_ack_is_terminal_unknown(self) -> None:
        """R5, live path:  the send's failure class says the request may have reached the
        server, so the kind is UNKNOWN at once and no due pass ever resends it."""
        daemon = self.started()
        mid = 424244
        self._plant(
            daemon,
            mid,
            [
                (
                    "audit",
                    "driving",
                    {
                        "due_at": 2.0,
                        "attempts": 0,
                        "sender_seat": None,
                        "sender_email": "codex@zulip.test",
                        "timestamp": 1.0,
                    },
                )
            ],
            claims={"audit": self.clock.time()},
        )
        broadcast = BAUD.Broadcast(
            message_id=mid,
            sender_seat=None,
            sender_email="codex@zulip.test",
            channel="fleet",
            topic="t",
            timestamp=1.0,
        )
        with mock.patch.object(
            daemon,
            "_operator_send",
            side_effect=Z.NetworkError("read timed out", timeout=True, maybe_sent=True),
        ):
            daemon._run_broadcast_audit(str(mid), broadcast, 0)
        effect = self._effect(daemon, mid, "audit")
        self.assertEqual(effect["state"], "unknown")
        self.assertIn("never resent", effect["reason"])
        with mock.patch.object(daemon, "_run_broadcast_audit") as run:
            daemon._start_due_broadcast_audits(self.clock.time() + 10**9)
            self.assertFalse(run.called)

    def test_due_pass_after_boot_runs_the_real_body_exactly_once(self) -> None:
        """Regression (#1462 review):  boot clears the stale claim of a driving audit with
        no send attempt.  The due pass must re-claim the kind before running the REAL
        body - without the claim every body write no-ops, and the reminder is sent on
        every tick forever with no marker.  Exactly one send, terminal done, marker
        durable."""
        daemon = self.started()
        mid = 424245
        self._plant(
            daemon,
            mid,
            [
                (
                    "audit",
                    "driving",
                    {
                        "due_at": 2.0,
                        "attempts": 0,
                        "sender_seat": None,
                        "sender_email": "codex@zulip.test",
                        "timestamp": 1.0,
                    },
                )
            ],
            claims={"audit": 1.0},
        )
        daemon._broadcast_ledger_boot()
        self.assertEqual(self._effect(daemon, mid, "audit")["state"], "driving")
        with mock.patch.object(daemon, "_operator_send", return_value={"id": 7}) as send:
            for _ in range(3):
                daemon._start_due_broadcast_audits(self.clock.time() + 10**9)
                self.assertTrue(
                    wait_for(lambda: mid not in daemon._audit_in_flight)
                )
            self.assertEqual(send.call_count, 1)
        effect = self._effect(daemon, mid, "audit")
        self.assertEqual(effect["state"], "done")
        self.assertIn("send_attempted_at", effect["detail"])  # the marker is durable
        self.assertEqual(effect["detail"]["receipt"]["outcome"], BAUD.REMINDED)
        self.assertEqual(effect["detail"]["receipt"]["reminder_id"], 7)

    def test_inbox_partial_terminal_lands_per_target_receipts(self) -> None:
        """R2/R4, partial:  one seat delivered, one terminal - the kind ends FAILED with the
        terminal seat named and every seat's outcome in the durable receipts."""
        daemon = self.started()
        mid, message, c = BroadcastDeferralTests._owner_broadcast(self, daemon)
        with mock.patch.object(daemon, "_deliver_inbox"):
            daemon.start_inbox_delivery(message, c)
        daemon._inbox_finish(
            mid, outcomes={"CODEX": IDEL.TERMINAL, "GROK": IDEL.DELIVERED}
        )
        effect = self._effect(daemon, mid, "inbox")
        self.assertEqual(effect["state"], "failed")
        self.assertIn("CODEX", effect["reason"])
        self.assertEqual(
            effect["detail"]["targets"], {"CODEX": IDEL.TERMINAL, "GROK": IDEL.DELIVERED}
        )
        status = daemon._drain_status()
        self.assertEqual(status["broadcast_effects"]["failed"], 1)

    def test_inbox_give_up_is_a_durable_failure(self) -> None:
        """R4:  the retry queue giving up used to drop the message after one log line;  now
        the kind ends FAILED with the seats and their outcomes as the durable reason."""
        daemon = self.started()
        mid, message, c = BroadcastDeferralTests._owner_broadcast(self, daemon)
        with mock.patch.object(daemon, "_deliver_inbox"):
            daemon.start_inbox_delivery(message, c)
        state = daemon._inbox_pending[mid]
        state.attempts = len(INBOX_RETRY_BACKOFF)
        daemon._inbox_finish(mid, outcomes={"CODEX": IDEL.UNKNOWN})
        effect = self._effect(daemon, mid, "inbox")
        self.assertEqual(effect["state"], "failed")
        self.assertIn("gave up", effect["reason"])
        self.assertIn("CODEX=unknown", effect["reason"])

    def test_changed_content_decision_is_recorded_on_redrive(self) -> None:
        """R8:  the re-fetched payload no longer matches the parked snapshot, so the re-drive
        records the old and new hashes and the reclassify decision before driving."""
        daemon = self.started()
        mid, message, _c = BroadcastDeferralTests._owner_broadcast(self, daemon)
        BroadcastDeferralTests._park(self, daemon, message, "fanout")
        edited = dict(message, content="@all hands, changed while parked")
        with mock.patch.object(
            daemon, "_probe_deferred_payload", return_value=edited
        ), mock.patch.object(daemon, "fanout") as fan:
            daemon._redrive_deferred_broadcasts()
            self.assertTrue(wait_for(lambda: fan.called))
        row = L.read_json(daemon._broadcast_ledger_path())[str(mid)]
        self.assertEqual(len(row["changed_content"]), 1)
        record = row["changed_content"][0]
        self.assertEqual(record["decision"], "reclassified-after-content-change")
        self.assertNotEqual(record["old"], record["new"])
        self.assertEqual(
            row["snapshot"]["content_sha256"],
            daemon._broadcast_snapshot(edited)["content_sha256"],
        )

    def test_boot_prunes_only_old_all_done_rows(self) -> None:
        """The prune boundary, stated exactly:  ONLY a row whose every kind is done AND whose
        admission is older than the keep window is pruned.  Failed and unknown kinds are
        unresolved loss:  they never age out (R4).  A pending kind is GATE 2:  it never
        expires, however old.  A recent all-done row stays as the durable seen-ring."""
        daemon = self.started()
        now = self.clock.time()
        old = now - 31 * 86400
        self._plant(daemon, 910001, [("fanout", "done", {})], admitted_at=old)
        self._plant(daemon, 910002, [("fanout", "failed", {})], admitted_at=old)
        self._plant(daemon, 910003, [("audit", "unknown", {})], admitted_at=old)
        self._plant(daemon, 910004, [("fanout", "pending", {})], admitted_at=old)
        self._plant(
            daemon,
            910005,
            [("fanout", "done", {}), ("inbox", "pending", {})],
            admitted_at=old,
        )
        self._plant(daemon, 910006, [("fanout", "done", {})], admitted_at=now)
        daemon._broadcast_ledger_boot()
        data = L.read_json(daemon._broadcast_ledger_path())
        self.assertNotIn("910001", data)  # old + all done:  pruned
        for kept in ("910002", "910003", "910004", "910005", "910006"):
            self.assertIn(kept, data)

    def test_crash_after_inbox_effect_before_ack_redelivers_idempotently(self) -> None:
        """Crash after the delivery's effect but before its completion receipt:  boot
        re-parks the kind, the re-drive delivers again - safe because every inbox write is
        idempotent (a subscription and a read flag) - and the second completion lands the
        per-seat receipts (R2)."""
        daemon = self.started()
        mid, message, _c = BroadcastDeferralTests._owner_broadcast(self, daemon)
        self._plant(
            daemon,
            mid,
            [("inbox", "driving", {"targets": {"CODEX": "delivered"}})],
            claims={"inbox": 1.0},
            snapshot=daemon._broadcast_snapshot(message),
        )
        daemon._broadcast_ledger_boot()
        self.assertEqual(self._effect(daemon, mid, "inbox")["state"], "pending")
        with mock.patch.object(daemon, "_deliver_inbox"):
            daemon._redrive_deferred_broadcasts()
            self.assertTrue(
                wait_for(lambda: self._effect(daemon, mid, "inbox")["state"] == "driving")
            )
        daemon._inbox_finish(mid, outcomes={"CODEX": IDEL.DELIVERED, "GROK": IDEL.DELIVERED})
        effect = self._effect(daemon, mid, "inbox")
        self.assertEqual(effect["state"], "done")
        self.assertEqual(
            effect["detail"]["receipt"]["targets"],
            {"CODEX": "delivered", "GROK": "delivered"},
        )

    def test_fanout_walk_with_failed_enqueues_is_not_done(self) -> None:
        """The walk completing is not the targets succeeding:  a catalog failure or failed
        enqueues end the kind FAILED with the per-target reasons, never done."""
        daemon = self.started()
        mid, message, c = BroadcastDeferralTests._owner_broadcast(self, daemon)
        with mock.patch.object(
            daemon,
            "fanout",
            return_value={
                "queued": 1,
                "failed": 1,
                "targets": {"GROK:tmux:a": "queued", "CODEX:tmux:b": "failed:refused"},
            },
        ):
            daemon.start_fanout(message, c)
            self.assertTrue(
                wait_for(lambda: self._effect(daemon, mid, "fanout")["state"] == "failed")
            )
        effect = self._effect(daemon, mid, "fanout")
        self.assertIn("1 chat enqueue", effect["reason"])
        self.assertEqual(
            effect["detail"]["receipt"]["targets"]["CODEX:tmux:b"], "failed:refused"
        )

    def test_boot_migrates_the_old_deferred_store(self) -> None:
        """The pre-#1462 park store imports as admitted rows (unfinished kinds pending, done
        kinds done) and the old file is set aside, never re-read."""
        daemon = self.started()

        def plant(data: dict) -> None:
            data["777"] = {
                "channel": "fleet",
                "topic": "t",
                "stream_id": 104,
                "seat": "CLAUDE",
                "ts": 1.0,
                "effects": {"fanout": False, "inbox": True},
                "claims": {"fanout": 1.0},
            }

        L.update_json(daemon._deferred_broadcasts_path(), plant)
        daemon._broadcast_ledger_boot()
        row = L.read_json(daemon._broadcast_ledger_path())["777"]
        self.assertEqual(row["effects"]["fanout"]["state"], "pending")
        self.assertEqual(row["effects"]["inbox"]["state"], "done")
        self.assertEqual(row["claims"], {})
        self.assertEqual(row["origin_seat"], "CLAUDE")
        self.assertFalse(os.path.exists(daemon._deferred_broadcasts_path()))
        self.assertTrue(os.path.exists(daemon._deferred_broadcasts_path() + ".migrated"))
        # A second boot reads no old store and changes nothing.
        daemon._broadcast_ledger_boot()
        self.assertEqual(
            L.read_json(daemon._broadcast_ledger_path())["777"]["effects"]["fanout"]["state"],
            "pending",
        )
