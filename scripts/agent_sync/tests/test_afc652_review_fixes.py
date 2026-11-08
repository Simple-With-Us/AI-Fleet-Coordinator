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
from agent_sync import wakes as W
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


class DeferredRestoreMergeTests(DaemonHarness):
    """A trigger offered while a deferred restore waits must never be overwritten."""

    def test_a_deferred_restore_merges_into_the_newer_offer_and_closes_its_row(self) -> None:
        daemon = self.started()
        mid1 = self.fake.add_message("Codex", "agent-sync", "t", MENTION)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        self.clock.advance(25)
        daemon.tick()  # the first wake is accepted into the seat's jobs
        D.set_pause(
            self.root, None, wakes_only=True, by="test", now=self.clock.time(), defer=True
        )
        daemon.run_jobs("CLAUDE")  # run_wake hits the drain defer; the restore is queued
        self.assertFalse(daemon._deferred_restores.empty())
        old_wake = self.ledger()[0]["wake_id"]

        # A new trigger for the same topic is offered while the restore waits:  the
        # blind overwrite dropped its trigger id and stranded its queued row.
        mid2 = self.fake.add_message("Codex", "agent-sync", "t", MENTION + " again")
        daemon.pump("CLAUDE", timeout=0.5)  # route only; no tick, so the restore still waits
        self.assertEqual(len(self.ledger()), 3)  # queued + accepted (mid1), queued (mid2)
        self.assertEqual(len(daemon.coalescer.pending), 1)
        self.assertEqual(daemon._deferred_restores.qsize(), 1)
        new_wake = self.ledger()[2]["wake_id"]

        daemon.tick()  # the event thread drains the restore
        survivors = list(daemon.coalescer.pending.values())
        self.assertEqual(len(survivors), 1, "the restore must merge, not overwrite")
        survivor = survivors[0]
        self.assertEqual(survivor.wake_id, old_wake, "the older wake absorbs the newer")
        self.assertEqual(survivor.trigger_ids, [mid1, mid2])
        merged = [r for r in self.ledger() if r["state"] == "merged"]
        self.assertEqual([(r["wake_id"], r["merged_into"]) for r in merged], [(new_wake, old_wake)])
        refreshed = [
            r for r in self.ledger() if r["wake_id"] == old_wake and r.get("merged_from")
        ]
        self.assertEqual([r["trigger_ids"] for r in refreshed], [[mid1, mid2]])

        # A crash recovery re-offers the union exactly once, never the absorbed wake.
        reload, _ = daemon.seats["CLAUDE"].ledger.recover(self.clock.time())
        self.assertEqual([v["wake_id"] for v in reload], [old_wake])
        self.assertEqual(reload[0]["trigger_ids"], [mid1, mid2])

        # The merged wake still runs once the drain clears, with both triggers.
        D.clear_pause(self.root, None)
        self.clock.advance(30)
        daemon.tick()
        daemon.run_jobs("CLAUDE")
        done = [r for r in self.ledger() if r["wake_id"] == old_wake and r["state"] == "done"]
        self.assertEqual(len(done), 1)
        self.assertEqual(done[0]["trigger_ids"], [mid1, mid2])


class RecoveryMergesQueuedWakePairTests(DaemonHarness):
    def test_recovery_folds_two_queued_wakes_for_one_topic_durably(self) -> None:
        daemon = self.started()
        runner = daemon.seats["CLAUDE"]
        now = self.clock.time()
        item = {"channel": "agent-sync", "topic": "t", "type": "stream"}
        p1 = W.Pending("w-old", "CLAUDE", item, owner=False, now=now - 5, due=now + 20)
        p1.add({"id": 101, "ts": now - 5}, False)
        p2 = W.Pending("w-new", "CLAUDE", item, owner=False, now=now, due=now + 20)
        p2.add({"id": 102, "ts": now}, False)
        runner.ledger.append(p1.row("queued", now - 5))
        runner.ledger.append(p2.row("queued", now))

        # The crash-persisted race:  two open queued rows for one (seat, topic).
        daemon._recover_ledgers()
        survivors = list(daemon.coalescer.pending.values())
        self.assertEqual(len(survivors), 1)
        self.assertEqual(
            (survivors[0].wake_id, survivors[0].trigger_ids), ("w-old", [101, 102])
        )
        merged = [r for r in runner.ledger.rows() if r.get("state") == "merged"]
        self.assertEqual([(r["wake_id"], r["merged_into"]) for r in merged], [("w-new", "w-old")])

        # The fold is durable:  a second recovery rebuilds the same single wake once.
        daemon.coalescer.pending.clear()
        daemon._recover_ledgers()
        survivors = list(daemon.coalescer.pending.values())
        self.assertEqual(len(survivors), 1)
        self.assertEqual(
            (survivors[0].wake_id, survivors[0].trigger_ids), ("w-old", [101, 102])
        )
        self.assertEqual(
            len([r for r in runner.ledger.rows() if r.get("state") == "merged"]), 1
        )


class EditedRouteSurvivesInboxDedupeTests(DaemonHarness):
    def test_an_edited_reroute_is_a_distinct_inbox_row_and_its_replay_dedupes(self) -> None:
        daemon = self.started()
        runner = daemon.seats["CLAUDE"]
        base = {
            "id": 424299,
            "channel": "agent-sync",
            "topic": "t",
            "sender": "Codex",
            "ts": 1,
            "content": "original",
            "classes": [],
        }
        daemon._append_seat_inbox(runner, [dict(base, kind="message")])
        edited = dict(base, kind="message", edited=True, content="edited in a mention")
        daemon._append_seat_inbox(runner, [edited])
        rows = L.read_jsonl(runner.paths.seat_inbox)
        self.assertEqual(
            len(rows), 2, "the edited re-route is a new delivery, not a replay duplicate"
        )
        self.assertEqual([r.get("edited") for r in rows], [None, True])
        # A crash-replay of the edited batch still dedupes against its own append.
        daemon._append_seat_inbox(runner, [edited])
        self.assertEqual(len(L.read_jsonl(runner.paths.seat_inbox)), 2)


if __name__ == "__main__":
    unittest.main()
