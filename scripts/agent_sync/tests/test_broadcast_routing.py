"""Owner `@all` vs `@everyone` broadcast wake routing (Jay, 2026-10-24).

Zulip sets the same wildcard flags for both tokens; only the raw message token may decide."""
from __future__ import annotations

import unittest

from agent_sync import router as R
from agent_sync.live import topic_key

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
        )
        for text in positives:
            with self.subTest(text=text):
                tokens = R.broadcast_tokens(text)
                self.assertTrue(tokens & {"all", "everyone"}, text)

    def test_negative_spellings(self) -> None:
        negatives = (
            "mail@allstate.com",
            "@alloy",
            "`@all`",
            "> @all quoted\nok",
            "```\n@all\n```",
            "\\@all escaped",
            "mail@**all**",
            "@channel",
            "@**channel**",
            "@stream",
            "@**topic**",
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

    def test_peer_at_all_still_does_not_wake(self) -> None:
        peer = msg(sender=11, content="@all look", client="ZulipPython", **POLICY)
        d = R.route(peer, ME, ctx(eligible_user_ids=[11], is_bot={11: True, 20: True}), [])[1]
        self.assertEqual((d.seat_inbox, d.wake, d.wake_block), (True, None, "group_or_wildcard"))


class OwnerEveryoneLeaseTests(unittest.TestCase):
    def test_everyone_only_does_not_interrupt_a_leased_topic(self) -> None:
        key = topic_key("policy", "general")
        on = lease("A", topics=[("policy", "general")])
        message = msg(content="@**everyone** FYI", **POLICY)
        d = R.route(message, ME, ctx(), [on])[1]
        self.assertEqual(d.lease_items, [("A", "passive", "message")])

    def test_at_all_still_interrupts_a_leased_topic(self) -> None:
        on = lease("A", topics=[("policy", "general")])
        message = msg(content="@all FYI", **POLICY)
        d = R.route(message, ME, ctx(), [on])[1]
        self.assertEqual(d.lease_items[0][1], "interrupt")


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


if __name__ == "__main__":
    unittest.main()
