"""Regression tests for the #1462 review findings on the drain branch (AFC-652).

Three sentry findings, one test each:

- HIGH:  a trigger handed to a lease and then released/requeued was dropped by the
  seat-inbox dedupe, because the requeued copy rebuilt the original row's inbox key.
- MEDIUM:  a drain-deferred wake re-armed the coalescer from a per-seat worker
  thread;  the coalescer is event-thread-only.
- LOW:  a fanout whose durable admit returned None (the OSError path in
  _broadcast_admit) was treated as "already driving" and the owner broadcast fell
  through with no fallback wake.
"""
from __future__ import annotations

import unittest
from unittest import mock

from agent_sync import daemon as D
from agent_sync import live as L
from agent_sync import router as R
from agent_sync.tests.test_chat_fanout import FakeCatalog, cloud
from agent_sync.tests.test_daemon import DaemonHarness, MENTION

ST_URL = "https://github.com/example-org/Socratic-Trade"


class RequeuedTriggerSurvivesInboxDedupeTests(DaemonHarness):
    def test_a_requeued_trigger_survives_dedupe_and_its_replay_still_dedupes(self) -> None:
        daemon = self.started()
        mid = self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.clock.advance(25)
        daemon.tick()
        self.write_lease("late", topics=[("agent-sync", "t")])
        daemon.run_jobs("CLAUDE")
        self.assertEqual([i["id"] for i in self.live_items("late")], [mid])

        runner = daemon.seats["CLAUDE"]
        items = L.release(self.seat_paths(), "late", field="released")
        self.assertEqual([i["id"] for i in items], [mid])
        daemon._requeue(runner, items, "released_from", "late")
        requeued = [r for r in self.inbox() if r.get("released_from") == "late"]
        self.assertEqual(
            [r["id"] for r in requeued],
            [mid],
            "the requeued copy must not dedupe against the original append",
        )

        # A crash-replay of the same requeue batch (same items, same marker) must not
        # append a second copy.
        daemon._requeue(runner, items, "released_from", "late")
        self.assertEqual(
            [r for r in self.inbox() if r.get("released_from") == "late"],
            requeued,
            "replaying the requeue batch must not duplicate the row",
        )


class DeferredWakeRearmThreadingTests(DaemonHarness):
    def test_a_drain_deferred_wake_rearms_on_the_event_thread(self) -> None:
        daemon = self.started()
        self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.clock.advance(25)
        daemon.tick()  # finalize accepts the wake into the seat's jobs
        D.set_pause(self.root, None, wakes_only=True, by="test", now=self.clock.time(), defer=True)

        daemon.run_jobs("CLAUDE")  # run_wake on the worker path hits the drain defer
        self.assertEqual(
            daemon.coalescer.pending,
            {},
            "the worker must not mutate the coalescer; the re-arm waits for the event thread",
        )
        self.assertFalse(daemon._deferred_restores.empty(), "the deferred wake is queued")

        daemon.tick()  # the event thread drains the queue before take_due
        self.assertTrue(daemon._deferred_restores.empty())
        self.assertEqual(
            sorted(daemon.coalescer.pending),
            [("CLAUDE", L.topic_key("agent-sync", "t"))],
            "tick restored the deferred wake with its re-armed due time",
        )


class FanoutAdmitNoneFallbackTests(DaemonHarness):
    def test_an_admit_returning_none_still_queues_the_fallback_wake(self) -> None:
        daemon = self.started()
        row = dict(
            cloud(1, 0, ST_URL),
            seat="CLAUDE",
            host="7",
            updated_at=self.clock.time(),
        )
        daemon._chat_catalog = FakeCatalog([row])
        message_id = self.fake.add_message(
            "Jay Wedgeworth", "work-in-progress", "fleet", "@**all** for ST: deploy"
        )
        message = next(m for m in self.fake.messages if m["id"] == message_id)
        c = R.Classes()
        c.owner, c.broadcast_all, c.eligible = True, True, True
        c.channel, c.topic, c.key = "work-in-progress", "fleet", L.topic_key(
            "work-in-progress", "fleet"
        )

        # The OSError path in _broadcast_admit:  no durable row, no fanout thread.
        with mock.patch.object(daemon, "_broadcast_admit", return_value=None):
            daemon.start_fanout(message, c, seat="CLAUDE")
        self.assertFalse(
            daemon._broadcast_wakes.empty(),
            "an admit that returns None must fall back to the plain wake hand-back",
        )
        queued_message, nudged = daemon._broadcast_wakes.get_nowait()
        self.assertEqual(queued_message["id"], message_id)
        self.assertEqual(nudged, frozenset())


if __name__ == "__main__":
    unittest.main()
