"""Owner `@all` vs `@everyone` broadcast wake routing (Jay, 2026-10-24).

Zulip sets the same wildcard flags for both tokens; only the raw message token may decide."""
from __future__ import annotations

import json
import secrets
import threading
import unittest
from unittest import mock

from agent_sync import chat_fanout as F
from agent_sync import live as L
from agent_sync import router as R
from agent_sync.live import topic_key
from agent_sync.tests.harness import write_rc
from agent_sync.tests.test_chat_fanout import FakeCatalog, cloud
from agent_sync.tests.fake_zulip import FAKE_OWNER_ID
from agent_sync.tests.test_daemon import DaemonHarness

ST_URL = "https://github.com/example-org/Socratic-Trade"

ME = R.SeatIdentity("GB-ENGINEER", 20, "GB Engineer", "gb-engineer-bot@zulip.test")
OWNER = 12
NOW = 10_000.0
POLICY = dict(channel="policy", topic="general", flags=["wildcard_mentioned", "stream_wildcard_mentioned"])
FLEET_KW = dict(channel="work-in-progress", topic="fleet", flags=["wildcard_mentioned", "stream_wildcard_mentioned"])


def ctx(**kw):
    options = dict(
        owner_user_id=OWNER,
        owner_clients=["website"],
        eligible_user_ids=[20],
        is_bot={20: True, OWNER: False},
        now=NOW,
        stale_after=7200.0,
        presence=[],
    )
    options.update(kw)
    return R.Context(**options)


def msg(sender=OWNER, content="hello", *, client="website", flags=(), topic="t", channel="agent-sync", mid=900):
    return {
        "id": mid,
        "sender_id": sender,
        "content": content,
        "flags": list(flags),
        "timestamp": NOW - 10,
        "client": client,
        "type": "stream",
        "display_recipient": channel,
        "subject": topic,
    }


def lease(lease_id="L1", *, topics=()):
    return R.LeaseView(
        lease_id,
        platform="claude-code",
        topics=[topic_key(c, t) for c, t in topics],
        mentions=True,
        wake_capable=True,
        posted=(),
        prefix=None,
    )


class BroadcastTokenParserTests(unittest.TestCase):
    def test_positive_spellings(self) -> None:
        positives = (
            "@all please",
            "@**all** please",
            "@ALL please",
            "@everyone please",
            "@**everyone** please",
            "ST @all look",
            "prefix @**all** suffix",
            "@channel please",
            "@**channel** please",
            "@stream please",
            "@**stream** please",
        )
        for text in positives:
            with self.subTest(text=text):
                tokens = R.broadcast_tokens(text)
                self.assertTrue(tokens & {"all", "everyone", "channel", "stream"}, text)

    def test_negative_spellings(self) -> None:
        negatives = (
            "mail@allstate.com",
            "@alloy",
            "`@all`",
            "> @all quoted\nok",
            "```\n@all\n```",
            "\\@all escaped",
            "mail@**all**",
            "@**topic**",
            "@channelish",
            "@streams",
        )
        for text in negatives:
            with self.subTest(text=text):
                self.assertEqual(R.broadcast_tokens(text), frozenset(), text)

    def test_both_tokens_imply_all(self) -> None:
        tokens = R.broadcast_tokens("@**everyone** and @all")
        self.assertEqual(tokens, frozenset({"all", "everyone"}))
        c = R.classify(msg(content="@**everyone** and @all", **POLICY), ME, ctx())
        self.assertTrue(c.broadcast_all)
        self.assertFalse(c.everyone_inert)

    def test_wildcard_flags_without_tokens_do_not_count(self) -> None:
        c = R.classify(msg(content="HALT", **POLICY), ME, ctx())
        self.assertFalse(c.broadcast_all)
        self.assertFalse(c.everyone_inert)
        self.assertTrue(c.wildcard)


class OwnerAllWakeTests(unittest.TestCase):
    def wake(self, message):
        return R.route(message, ME, ctx(), [])[1]

    def test_owner_at_all_anywhere_wakes(self) -> None:
        d = self.wake(msg(content="@all heads up", **POLICY))
        self.assertEqual((d.seat_inbox, d.wake, d.wake_block), (True, "owner", None))

    def test_owner_at_everyone_only_does_not_wake(self) -> None:
        d = self.wake(msg(content="@**everyone** FYI", **POLICY))
        self.assertEqual((d.seat_inbox, d.wake, d.wake_block), (True, None, "group_or_wildcard"))

    def test_peer_at_all_wakes_as_a_peer(self) -> None:
        """Jay's Oct 27 2026 rule:  a fleet bot's `@all` wakes like the owner's."""
        peer = msg(sender=11, content="@all look", client="ZulipPython", **POLICY)
        c, d = R.route(peer, ME, ctx(eligible_user_ids=[11], is_bot={11: True, 20: True}), [])
        self.assertEqual((d.seat_inbox, d.wake, d.wake_block), (True, "peer", None))
        self.assertTrue(c.peer_broadcast)
        self.assertIn("peer_broadcast", c.labels())


class OwnerEveryoneLeaseTests(unittest.TestCase):
    def test_everyone_only_does_not_interrupt_a_leased_topic(self) -> None:
        key = topic_key("policy", "general")
        on = lease("A", topics=[("policy", "general")])
        message = msg(content="@**everyone** FYI", **POLICY)
        d = R.route(message, ME, ctx(), [on])[1]
        self.assertEqual(d.lease_items, [("A", "passive", "message")])

    def test_everyone_with_reply_to_posted_id_stays_passive(self) -> None:
        on = lease("A", topics=[("policy", "general")])
        on.posted = (123,)
        message = msg(content="re=123 @**everyone** FYI", **POLICY)
        d = R.route(message, ME, ctx(), [on])[1]
        self.assertEqual(d.lease_items, [("A", "passive", "message")])

    def test_at_all_still_interrupts_a_leased_topic(self) -> None:
        on = lease("A", topics=[("policy", "general")])
        message = msg(content="@all FYI", **POLICY)
        d = R.route(message, ME, ctx(), [on])[1]
        self.assertEqual(d.lease_items[0][1], "interrupt")


class OwnerAllFanoutDedupTests(DaemonHarness):
    def test_owner_at_all_fans_out_once_without_a_parallel_offer_wake(self) -> None:
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
        queue = daemon.chat_queue("CLAUDE")

        def settled() -> bool:
            daemon.tick()  # drains the fanout's hand-back on the event thread
            return bool(queue.snapshot()["items"]) and daemon._broadcast_wakes.empty()

        self.pump_until(daemon, settled, rounds=40)
        daemon.tick()
        self.assertEqual(len(queue.snapshot()["items"]), 1)
        self.assertEqual(
            self.wakes_for(message_id), [], "the chat nudge owns the wake; offer_wake must not double it"
        )

    def owner_all(self) -> int:
        return self.fake.add_message(
            "Jay Wedgeworth", "work-in-progress", "fleet", "@**all** for ST: deploy"
        )

    def log_rows(self, daemon) -> list[dict]:
        return L.read_jsonl(daemon.log.path)

    def wakes_for(self, message_id: int) -> list[str]:
        """Distinct wake ids in the seat ledger that carry this message (rows key it as trigger_ids)."""
        ids = {
            str(w.get("wake_id"))
            for w in self.ledger()
            if message_id in (w.get("trigger_ids") or [])
        }
        return sorted(ids)

    def settle(self, daemon, message_id: int) -> None:
        """Route the message, let the fanout thread hand back, and drain it on the event thread."""

        def woke() -> bool:
            daemon.tick()
            return bool(self.wakes_for(message_id))

        self.pump_until(daemon, woke, rounds=40)

    def test_a_seat_with_no_chat_nudge_still_gets_one_wake(self) -> None:
        daemon = self.started()
        daemon._chat_catalog = FakeCatalog([])  # no chat to nudge
        message_id = self.owner_all()
        self.settle(daemon, message_id)
        daemon.tick()
        self.assertEqual(len(self.wakes_for(message_id)), 1, "one wake for the un-nudged seat")

    def test_a_catalog_failure_still_wakes_the_seat_once(self) -> None:
        daemon = self.started()

        class BrokenCatalog:
            def discover(self, *args, **kwargs):
                raise RuntimeError("synthetic discovery failure")

        daemon._chat_catalog = BrokenCatalog()
        message_id = self.owner_all()
        self.settle(daemon, message_id)
        daemon.tick()
        self.assertEqual(len(self.wakes_for(message_id)), 1, "discovery failure must not lose the wake")
        events = [e.get("event") for e in self.log_rows(daemon)]
        self.assertIn("fanout-error", events)

    def test_the_fanout_thread_never_offers_a_wake_itself(self) -> None:
        daemon = self.started()
        daemon._chat_catalog = FakeCatalog([])
        message = {
            "id": 990001,
            "sender_id": FAKE_OWNER_ID,
            "content": "@**all** deploy",
            "client": "website",
            "type": "stream",
            "display_recipient": "work-in-progress",
            "subject": "fleet",
            "timestamp": int(self.clock.time()),
            "flags": ["stream_wildcard_mentioned"],
        }
        daemon.fanout(990001, "work-in-progress", "fleet", F.Scope(), F.ChatPlaces(), message)
        self.assertEqual(daemon.coalescer.pending, {}, "only the event thread may touch the coalescer")
        self.assertEqual(self.wakes_for(990001), [])
        daemon.tick()
        self.assertEqual(len(self.wakes_for(990001)), 1)

    def test_one_seat_failure_does_not_stop_the_broadcast_wake(self) -> None:
        from types import SimpleNamespace

        daemon = self.started()
        daemon._chat_catalog = FakeCatalog([])
        message_id = self.owner_all()
        self.pump_until(daemon, lambda: message_id in daemon._fanout_seen)
        message = next(m for m in self.fake.messages if m["id"] == message_id)
        broken = SimpleNamespace(cfg=SimpleNamespace(enabled=True), me=object(), seat="BROKEN")
        real = daemon.seats
        daemon.seats = {"BROKEN": broken, **real}
        try:
            daemon._broadcast_wake_after_fanout(dict(message, flags=["stream_wildcard_mentioned"]), set())
        finally:
            daemon.seats = real
        rows = [e for e in self.log_rows(daemon) if e.get("event") == "broadcast-wake-failed"]
        self.assertEqual([r.get("seat") for r in rows], ["BROKEN"])
        self.assertGreaterEqual(len(self.wakes_for(message_id)), 1, "CLAUDE still woke after BROKEN failed")

    def test_a_fanout_thread_that_cannot_start_still_wakes_the_seat_once(self) -> None:
        """A fanout that never started hands the wake to the event thread:  the id stays in the
        seen ring, because the hand-off owns that wake now (AFC-938)."""
        daemon = self.started()
        daemon._chat_catalog = FakeCatalog([])
        message_id = self.owner_all()
        real_start = threading.Thread.start

        def boom(thread, *args, **kwargs):
            if thread.name == "chat-fanout":
                raise RuntimeError("can't start new thread")
            return real_start(thread, *args, **kwargs)

        with mock.patch.object(threading.Thread, "start", boom):
            self.settle(daemon, message_id)
        daemon.tick()
        self.assertEqual(len(self.wakes_for(message_id)), 1, "the wake must not be lost")
        self.assertIn(
            message_id,
            daemon._fanout_seen,
            "the hand-off owns the wake, so the id stays seen",
        )
        events = [e.get("event") for e in self.log_rows(daemon)]
        self.assertIn("fanout-start-failed", events)
        row = next(e for e in self.log_rows(daemon) if e.get("event") == "fanout-start-failed")
        self.assertEqual(row.get("id"), message_id)
        self.assertEqual(row.get("error"), "RuntimeError")
        self.assertNotIn("deploy", json.dumps(row), "the log never holds the content")

    def test_a_failed_fanout_start_wakes_once_across_two_seats(self) -> None:
        """The second seat's route of the same message must not fan out again, or the seat
        wakes twice:  the id stays in the seen ring, because the hand-off owns the wake."""
        codex_key = secrets.token_hex(16)
        self.extra_keys.append(("codex-bot@zulip.test", codex_key))
        self.fake.add_bot("codex-bot@zulip.test", "Codex", codex_key)
        write_rc(
            self.secrets_dir / "Codex-zuliprc",
            email="codex-bot@zulip.test",
            key=codex_key,
            site=self.fake.url,
        )
        self.write_config(
            seats='[seat.CLAUDE]\nbot = "Claude"\nwake = "claude"\n\n'
            '[seat.CODEX]\nbot = "Codex"\nwake = "inbox"\n'
        )
        daemon = self.started(env=self.env(AGENT_SYNC_PARTITION=self.two_seat_partition()))
        self.assertTrue(daemon.connect_seat("CODEX"))
        daemon._chat_catalog = FakeCatalog([])
        message_id = self.owner_all()
        real_start = threading.Thread.start

        def boom(thread, *args, **kwargs):
            if thread.name == "chat-fanout":
                raise RuntimeError("can't start new thread")
            return real_start(thread, *args, **kwargs)

        with mock.patch.object(threading.Thread, "start", boom):
            self.settle(daemon, message_id)
            self.pump_until(
                daemon,
                lambda: self.routed_by(daemon, "CODEX", message_id),
                seat="CODEX",
            )
        daemon.tick()
        self.assertEqual(len(self.wakes_for(message_id)), 1, "exactly one wake")
        rows = [
            e for e in self.log_rows(daemon) if e.get("event") == "fanout-start-failed"
        ]
        self.assertEqual(len(rows), 1, "the second seat must not fan out again")
        self.assertIn(message_id, daemon._fanout_seen, "the hand-off owns the wake")

    def routed_by(self, daemon, seat: str, message_id: int) -> bool:
        return any(
            row.get("event") == "route"
            and row.get("seat") == seat
            and row.get("id") == message_id
            for row in self.log_rows(daemon)
        )


class NonOwnerAllChatNudgeTests(DaemonHarness):
    """Only the owner broadcast hands its wake to the fanout;  a peer's direct mention that also
    carries `@all` keeps the working chat's nudge (Kody 4368578511)."""

    def routed(self, *, owner: bool) -> tuple[mock.MagicMock, mock.MagicMock]:
        daemon = self.started()
        runner = daemon.seats["CLAUDE"]
        runner.seat = "GROK"
        c = R.Classes()
        c.owner, c.broadcast_all, c.eligible, c.direct = owner, True, True, not owner
        c.channel, c.topic, c.key = "work-in-progress", "t", topic_key("work-in-progress", "t")
        d = R.Decision()
        d.seat_inbox, d.wake = True, "owner" if owner else "peer"
        queue = mock.MagicMock()
        queue.enqueue.return_value = True
        message = {
            "id": 880001,
            "type": "stream",
            "display_recipient": "work-in-progress",
            "subject": "t",
            "content": "@**GB Engineer** @all",
            "sender_id": 11,
            "timestamp": int(self.clock.time()),
        }
        with (
            mock.patch.object(R, "route", return_value=(c, d)),
            mock.patch.object(daemon, "chat_queue", return_value=queue),
            mock.patch.object(daemon, "offer_wake") as offer,
            mock.patch.object(daemon, "start_fanout"),
        ):
            daemon._route(runner, message)
        return queue, offer

    def test_peer_direct_mention_with_at_all_keeps_the_chat_nudge(self) -> None:
        queue, offer = self.routed(owner=False)
        queue.enqueue.assert_called_once()
        offer.assert_not_called()

    def test_owner_at_all_is_handed_to_the_fanout(self) -> None:
        queue, offer = self.routed(owner=True)
        queue.enqueue.assert_not_called()
        offer.assert_not_called()


class RouteModeMuseTests(unittest.TestCase):
    def test_muse_wake_is_headless_not_inbox_only(self) -> None:
        from types import SimpleNamespace

        from agent_sync.daemon import Daemon

        daemon = Daemon.__new__(Daemon)
        cfg = SimpleNamespace(wake="muse")
        daemon.config = SimpleNamespace(seats={"MM": cfg}, instance="mac")
        daemon.partition = {"MM": "mac"}
        daemon.seats = {"MM": SimpleNamespace(cfg=cfg)}
        daemon.lease_views = lambda _runner: []
        self.assertEqual(daemon.route_mode("MM"), ("headless wake", None))


class OwnerTopicBroadcastTests(unittest.TestCase):
    OWNER_TOPIC = "OWNER AFC-12 decision"

    def test_at_all_in_owner_topic_does_not_wake(self) -> None:
        message = msg(
            content="@**all** pick one",
            topic=self.OWNER_TOPIC,
            channel="agent-sync",
            flags=["stream_wildcard_mentioned"],
        )
        d = R.route(message, ME, ctx(), [])[1]
        self.assertIsNone(d.wake)


class PeerBroadcastRuleTests(unittest.TestCase):
    """Jay's Oct 27 2026 rule, at the router level:  a fleet bot's `@all` wakes like the owner's."""

    def peer_ctx(self, **kw):
        return ctx(eligible_user_ids=[11], is_bot={11: True, 20: True}, **kw)

    def peer(self, content, **kw):
        return msg(sender=11, content=content, client="ZulipPython", **kw)

    def test_a_stale_peer_at_all_does_not_wake(self) -> None:
        stale = dict(self.peer("@all look", **POLICY), timestamp=NOW - 9999)
        d = R.route(stale, ME, self.peer_ctx(stale_after=100.0), [])[1]
        self.assertEqual((d.seat_inbox, d.wake, d.wake_block), (True, None, "stale"))

    def test_a_peer_at_all_in_an_owner_topic_does_not_wake(self) -> None:
        peer = self.peer(
            "@all look", topic="OWNER AFC-12 decision", channel="agent-sync"
        )
        c, d = R.route(peer, ME, self.peer_ctx(), [])
        self.assertIsNone(d.wake)
        self.assertEqual(d.wake_block, "owner_topic")
        self.assertFalse(c.peer_broadcast)

    def test_the_owner_at_channel_and_at_stream_wake_the_owner(self) -> None:
        for token in ("@channel", "@stream", "@**channel**", "@**stream**"):
            with self.subTest(token=token):
                d = R.route(msg(content="%s heads up" % token, **POLICY), ME, ctx(), [])[1]
                self.assertEqual((d.seat_inbox, d.wake, d.wake_block), (True, "owner", None))

    def test_a_peer_at_channel_wakes_as_a_peer(self) -> None:
        d = R.route(self.peer("@channel look", **POLICY), ME, self.peer_ctx(), [])[1]
        self.assertEqual((d.seat_inbox, d.wake, d.wake_block), (True, "peer", None))

    def test_a_bot_at_everyone_does_not_wake(self) -> None:
        d = R.route(self.peer("@**everyone** FYI", **POLICY), ME, self.peer_ctx(), [])[1]
        self.assertEqual((d.seat_inbox, d.wake, d.wake_block), (True, None, "group_or_wildcard"))

    def test_an_eyes_prefixed_peer_at_all_wakes_nobody(self) -> None:
        c, d = R.route(self.peer(":eyes: @all look", **POLICY), ME, self.peer_ctx(), [])
        self.assertTrue(c.no_wake)
        self.assertEqual(d.drop_reason, "no_wake")

    def test_a_silent_at_all_is_not_a_broadcast(self) -> None:
        """`@_**all**` is the escaped mention:  it notifies nobody, so it is not a broadcast."""
        c, d = R.route(self.peer("@_**all** look", **POLICY), ME, self.peer_ctx(), [])
        self.assertFalse(c.broadcast_all)
        self.assertFalse(c.peer_broadcast)
        self.assertIsNone(d.wake)

    def test_a_peer_at_all_interrupts_a_leased_topic(self) -> None:
        on = lease("A", topics=[("policy", "general")])
        d = R.route(self.peer("@all FYI", **POLICY), ME, self.peer_ctx(), [on])[1]
        self.assertEqual(d.lease_items[0][1], "interrupt")


class PeerBroadcastDaemonTests(DaemonHarness):
    """A fleet bot's `@all` takes the owner's road (owner, Oct 27 2026), and the listener keeps the
    book:  one audit per broadcast, and none for the owner."""

    def peer_all(self) -> int:
        return self.fake.add_message("Codex", "agent-sync", "t", "@all the deploy is broken")

    def log_rows(self, daemon) -> list[dict]:
        return L.read_jsonl(daemon.log.path)

    def test_a_peer_at_all_fans_out_and_lands_in_the_inboxes_once(self) -> None:
        daemon = self.started()
        daemon._chat_catalog = FakeCatalog([])
        message_id = self.peer_all()
        self.pump_until(daemon, lambda: message_id in daemon._fanout_seen)
        self.pump_until(daemon, lambda: message_id in daemon._inbox_seen)
        self.assertIn(message_id, daemon._fanout_seen)
        self.assertIn(message_id, daemon._inbox_seen)
        # A second seat's route of the same message must not start a second fanout or delivery.
        message = next(m for m in self.fake.messages if m["id"] == message_id)
        daemon.route_message(
            daemon.seats["CLAUDE"], dict(message, flags=["stream_wildcard_mentioned"])
        )
        self.assertEqual(list(daemon._fanout_seen).count(message_id), 1)
        self.assertEqual(list(daemon._inbox_seen).count(message_id), 1)

    def test_a_peer_at_all_schedules_exactly_one_audit(self) -> None:
        daemon = self.started()
        daemon._chat_catalog = FakeCatalog([])
        message_id = self.peer_all()
        self.pump_until(daemon, lambda: message_id in daemon._fanout_seen)
        self.assertEqual(list(daemon._broadcast_audits), [message_id])
        message = next(m for m in self.fake.messages if m["id"] == message_id)
        daemon.route_message(
            daemon.seats["CLAUDE"], dict(message, flags=["stream_wildcard_mentioned"])
        )
        self.assertEqual(len(daemon._broadcast_audits), 1)

    def test_the_owner_at_all_schedules_no_audit(self) -> None:
        daemon = self.started()
        daemon._chat_catalog = FakeCatalog([])
        message_id = self.fake.add_message(
            "Jay Wedgeworth", "agent-sync", "t", "@all the deploy is broken"
        )
        self.pump_until(daemon, lambda: message_id in daemon._fanout_seen)
        self.assertEqual(daemon._broadcast_audits, {})

    def test_the_audit_reminds_the_owner_when_nothing_explained_the_broadcast(self) -> None:
        daemon = self.started()
        daemon._chat_catalog = FakeCatalog([])
        message_id = self.peer_all()
        self.pump_until(daemon, lambda: message_id in daemon._fanout_seen)
        self.assertEqual(len(daemon._broadcast_audits), 1)
        # The audit is due ten minutes later.
        self.clock.advance(11 * 60)
        daemon.tick()
        self.pump_until(
            daemon,
            lambda: any(
                row.get("event") == "peer-broadcast-all" for row in self.log_rows(daemon)
            ),
        )
        rows = [r for r in self.log_rows(daemon) if r.get("event") == "peer-broadcast-all"]
        self.assertEqual(rows[-1].get("outcome"), "reminded")
        self.assertEqual(rows[-1].get("id"), message_id)
        self.assertNotIn("broken", json.dumps(rows[-1]), "the log never holds the content")



if __name__ == "__main__":
    unittest.main()
