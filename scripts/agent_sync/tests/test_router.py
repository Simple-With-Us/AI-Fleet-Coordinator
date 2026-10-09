"""Routing rules, one by one:  classes, the wake prefilter rows, leases, mention leases, siblings,
muted and followed topics, and the owner rule (owner id AND a human client)."""
from __future__ import annotations

import unittest

from agent_sync import router as R
from agent_sync.live import topic_key

ME = R.SeatIdentity("CLAUDE", 10, "Claude", "claude-bot@zulip.test")
OWNER, CODEX, CURSOR, SENTRY, NEW_BOT, JET = 12, 11, 13, 40, 41, 42
NOW = 10_000.0


def ctx(**kw):
    options = dict(owner_user_id=OWNER, owner_clients=["website", "ZulipMobile"], eligible_user_ids=[10, CODEX, CURSOR],
                   is_bot={10: True, CODEX: True, CURSOR: True, OWNER: False, SENTRY: True, NEW_BOT: True, JET: True},
                   now=NOW, stale_after=7200.0, presence=[("agent-sync", "roll call"), ("alerts", "*")])
    options.update(kw)
    return R.Context(**options)


def msg(sender=CODEX, content="hello", *, topic="t", channel="agent-sync", flags=(), client=None, mid=500,
        ts=NOW - 10, dm=False):
    message = {"id": mid, "sender_id": sender, "content": content, "flags": list(flags), "timestamp": ts,
               "client": client or ("website" if sender == OWNER else "ZulipPython"), "sender_full_name": "x"}
    if dm:
        message.update({"type": "private", "display_recipient": [{"id": sender}, {"id": 10}], "subject": ""})
    else:
        message.update({"type": "stream", "display_recipient": channel, "subject": topic})
    return message


def lease(lease_id="L1", *, topics=(), platform="claude-code", mentions=True, capable=True, posted=(), prefix=None,
          last_prompt=0.0, created=0.0, room=None):
    return R.LeaseView(lease_id, platform=platform, topics=[topic_key(c, t) for c, t in topics], mentions=mentions,
                       wake_capable=capable, posted=posted, prefix=prefix, last_prompt=last_prompt, created=created,
                       room=room)


DIRECT = ("@**Claude** please look", ["mentioned"])


class ClassifyTests(unittest.TestCase):
    def test_direct_needs_the_flag_and_the_raw_name(self) -> None:
        self.assertTrue(R.classify(msg(content=DIRECT[0], flags=DIRECT[1]), ME, ctx()).direct)
        self.assertTrue(R.classify(msg(content="@**Claude|10** hi", flags=["mentioned"]), ME, ctx()).direct)
        self.assertFalse(R.classify(msg(content="@*fleet* all hands", flags=["mentioned"]), ME, ctx()).direct,
                         "a group mention sets the flag but is not direct")
        self.assertFalse(R.classify(msg(content="@**Claude** hi", flags=[]), ME, ctx()).direct)

    def test_a_mention_inside_a_code_span_or_quote_is_not_direct(self) -> None:
        for content in ("`@**Claude**` is how you ping", "> @**Claude** said earlier\nok", "```\n@**Claude**\n```"):
            with self.subTest(content=content):
                # Even if a server set the flag, the raw check outside code and quotes fails.
                self.assertFalse(R.classify(msg(content=content, flags=["mentioned"]), ME, ctx()).direct)

    def test_owner_needs_the_owner_id_and_a_human_client(self) -> None:
        human = R.classify(msg(sender=OWNER, client="website"), ME, ctx())
        self.assertTrue(human.owner and human.eligible and not human.owner_api)
        api = R.classify(msg(sender=OWNER, client="ZulipPython"), ME, ctx())
        self.assertFalse(api.owner)
        self.assertFalse(api.eligible, "an API post with the owner's key is not eligible either")
        self.assertTrue(api.owner_api)
        unpinned = R.classify(msg(sender=OWNER), ME, ctx(owner_user_id=0))
        self.assertFalse(unpinned.owner)

    def test_wake_tag_stale_reply_to_wildcard_fleet_dm_own(self) -> None:
        c = R.classify(msg(content="[CODEX·wake] re=77\nanswer"), ME, ctx())
        self.assertTrue(c.wake_tag)
        self.assertEqual(c.reply_to, 77)
        self.assertTrue(R.classify(msg(ts=NOW - 8000), ME, ctx()).stale)
        self.assertTrue(R.classify(msg(flags=["stream_wildcard_mentioned"]), ME, ctx()).wildcard)
        self.assertTrue(R.classify(msg(content="@*fleet* wake up"), ME, ctx()).fleet)
        self.assertTrue(R.classify(msg(dm=True), ME, ctx()).dm)
        self.assertTrue(R.classify(msg(sender=10), ME, ctx()).own)
        self.assertEqual(R.classify(msg(content="[CLAUDE·ab] hi re=5"), ME, ctx()).reply_to, 5)
        self.assertIsNone(R.classify(msg(content="hi\nre=5 on line two"), ME, ctx()).reply_to)

    def test_unknown_senders_count_as_bots_and_are_not_eligible(self) -> None:
        c = R.classify(msg(sender=999), ME, ctx())
        self.assertTrue(c.sender_is_bot)
        self.assertFalse(c.eligible)


class PrefilterTests(unittest.TestCase):
    """Every row of the wake prefilter table (design section 2, step 7)."""

    def wake(self, message, **kw):
        c, d = R.route(message, ME, ctx(**kw), [])
        return d.seat_inbox, d.wake, d.wake_block

    def test_owner_direct_dm_or_fleet_wakes_as_owner(self) -> None:
        self.assertEqual(self.wake(msg(sender=OWNER, content=DIRECT[0], flags=DIRECT[1])), (True, "owner", None))
        self.assertEqual(self.wake(msg(sender=OWNER, dm=True)), (True, "owner", None))
        self.assertEqual(self.wake(msg(sender=OWNER, content="@*fleet* everyone stop")), (True, "owner", None))
        self.assertEqual(self.wake(msg(sender=OWNER, content=DIRECT[0], flags=DIRECT[1], ts=NOW - 9000)),
                         (True, "owner", None), "a stale owner item still wakes (once per topic, in the daemon)")

    def test_eligible_peer_direct_wakes_within_peer_budgets(self) -> None:
        self.assertEqual(self.wake(msg(content=DIRECT[0], flags=DIRECT[1])), (True, "peer", None))

    def test_ineligible_senders_never_wake(self) -> None:
        for sender in (SENTRY, NEW_BOT, 999):
            with self.subTest(sender=sender):
                self.assertEqual(self.wake(msg(sender=sender, content=DIRECT[0], flags=DIRECT[1])),
                                 (True, None, "not_eligible"))
        self.assertEqual(self.wake(msg(sender=OWNER, client="ZulipPython", content=DIRECT[0], flags=DIRECT[1])),
                         (True, None, "owner_api"))

    def test_jet_is_eligible_once_pinned_and_wakes_within_the_peer_budgets(self) -> None:
        """Owner 2026-10-09:  Jet is a fleet seat bot, so `daemon init` pins its id like the others.  Configured, a
        direct mention wakes as a peer (never as the owner); not configured, it is an unknown bot like any other."""
        pinned = dict(eligible_user_ids=[10, CODEX, CURSOR, JET])
        direct = msg(sender=JET, content=DIRECT[0], flags=DIRECT[1])
        c = R.classify(direct, ME, ctx(**pinned))
        self.assertTrue(c.eligible and c.sender_is_bot and c.direct)
        self.assertFalse(c.owner or c.owner_api, "Jet is never the owner")
        self.assertEqual(self.wake(direct, **pinned), (True, "peer", None))
        self.assertEqual(self.wake(direct), (True, None, "not_eligible"), "no pin, no wake")
        # Only a direct mention wakes:  every other Jet post behaves like any other peer's.
        self.assertEqual(self.wake(msg(sender=JET, content="@*fleet* standup"), **pinned), (True, None, "group_or_wildcard"))
        self.assertEqual(self.wake(msg(sender=JET, dm=True), **pinned), (True, None, "dm_from_bot"))
        self.assertEqual(self.wake(msg(sender=JET, content="[JET·wake] re=1 @**Claude** hi", flags=["mentioned"]), **pinned),
                         (True, None, "wake_tag"))
        self.assertEqual(self.wake(msg(sender=JET, content=DIRECT[0], flags=DIRECT[1], ts=NOW - 9000), **pinned),
                         (True, None, "stale"))
        # Pinning Jet pins no one else.
        self.assertEqual(self.wake(msg(sender=NEW_BOT, content=DIRECT[0], flags=DIRECT[1]), **pinned),
                         (True, None, "not_eligible"))

    def test_a_jet_interrupt_in_a_leased_topic_counts_against_the_peer_budget(self) -> None:
        pinned = ctx(eligible_user_ids=[10, CODEX, CURSOR, JET])
        direct = msg(sender=JET, content=DIRECT[0], flags=DIRECT[1])
        open_room = lease("A", topics=[("agent-sync", "t")])
        self.assertEqual(R.route(direct, ME, pinned, [open_room])[1].lease_items, [("A", "interrupt", "message")])
        asked = []

        def full(key, owner):
            asked.append(owner)
            return "per_hour"

        d = R.route(direct, ME, pinned, [lease("A", topics=[("agent-sync", "t")], room=full)])[1]
        self.assertEqual(d.lease_items[0][1], "passive")
        self.assertEqual(d.throttled, [("A", "per_hour")])
        self.assertEqual(asked, [False], "the room check runs as a non-owner turn, never the owner's")

    def test_peer_fleet_wildcard_and_followed_chatter_do_not_wake(self) -> None:
        self.assertEqual(self.wake(msg(content="@*fleet* standup")), (True, None, "group_or_wildcard"))
        self.assertEqual(self.wake(msg(content="@**all** heads up", flags=["stream_wildcard_mentioned"])),
                         (True, None, "group_or_wildcard"))
        self.assertEqual(self.wake(msg(content="chatter"), followed=[topic_key("agent-sync", "t")]),
                         (True, None, "not_a_mention"))

    def test_wake_tag_dm_from_a_bot_and_stale_peer_direct_do_not_wake(self) -> None:
        self.assertEqual(self.wake(msg(content="[CODEX·wake] re=1 @**Claude** hi", flags=["mentioned"])),
                         (True, None, "wake_tag"))
        self.assertEqual(self.wake(msg(dm=True)), (True, None, "dm_from_bot"))
        self.assertEqual(self.wake(msg(content=DIRECT[0], flags=DIRECT[1], ts=NOW - 9000)), (True, None, "stale"))

    def test_plain_chatter_is_dropped(self) -> None:
        c, d = R.route(msg(content="just talking"), ME, ctx(), [])
        self.assertEqual((d.seat_inbox, d.drop_reason), (False, "chatter"))


class LeaseRoutingTests(unittest.TestCase):
    def test_leased_topic_goes_to_every_lease_and_never_to_the_inbox(self) -> None:
        a, b = lease("A", topics=[("agent-sync", "T")]), lease("B", topics=[("Agent-Sync", "t")], platform="codex")
        c, d = R.route(msg(content=DIRECT[0], flags=DIRECT[1]), ME, ctx(), [a, b])
        self.assertEqual(d.lease_items, [("A", "interrupt", "message"), ("B", "interrupt", "message")])
        self.assertFalse(d.seat_inbox)
        self.assertTrue(d.wake_worthy, "the grace release uses this later")

    def test_interrupt_versus_passive(self) -> None:
        a = lease("A", topics=[("agent-sync", "t")], posted=[400])
        cases = [(msg(sender=OWNER), "interrupt"), (msg(content=DIRECT[0], flags=DIRECT[1]), "interrupt"),
                 (msg(content="[CODEX] re=400 done"), "interrupt"), (msg(content="[CODEX] re=401 done"), "passive"),
                 (msg(content="peer chatter"), "passive"), (msg(sender=SENTRY, content=DIRECT[0], flags=DIRECT[1]), "passive"),
                 (msg(sender=OWNER, client="ZulipPython"), "passive")]
        for message, klass in cases:
            with self.subTest(content=message["content"], sender=message["sender_id"]):
                self.assertEqual(R.route(message, ME, ctx(), [a])[1].lease_items[0][1], klass)

    def test_presence_topics_are_passive_and_budget_turns_interrupts_passive(self) -> None:
        roll = lease("A", topics=[("agent-sync", "roll call")])
        d = R.route(msg(sender=OWNER, topic="roll call"), ME, ctx(), [roll])[1]
        self.assertEqual(d.lease_items[0][1], "passive")
        full = lease("A", topics=[("agent-sync", "t")], room=lambda key, owner: "per_hour")
        d = R.route(msg(content=DIRECT[0], flags=DIRECT[1]), ME, ctx(), [full])[1]
        self.assertEqual(d.lease_items[0][1], "passive")
        self.assertEqual(d.throttled, [("A", "per_hour")])

    def test_own_posts_go_to_sibling_leases_only(self) -> None:
        poster = lease("A", topics=[("agent-sync", "t")], posted=[500])
        tagged = lease("B", topics=[("agent-sync", "t")], prefix="[CLAUDE·aaaa1111")
        sibling = lease("C", topics=[("agent-sync", "t")], prefix="[CLAUDE·bbbb2222")
        own = msg(sender=10, content="[CLAUDE·aaaa1111] working", mid=500)
        c, d = R.route(own, ME, ctx(), [poster, tagged, sibling])
        self.assertEqual(d.lease_items, [("C", "passive", "sibling")])
        self.assertFalse(d.seat_inbox)
        self.assertIsNone(d.wake)
        c, d = R.route(msg(sender=10, content="@**Claude** talking to myself", flags=["mentioned"]), ME, ctx(), [])
        self.assertEqual((d.seat_inbox, d.drop_reason), (False, "own"))

    def test_one_mention_lease_is_chosen_latest_prompt_then_newest(self) -> None:
        old = lease("old", last_prompt=50.0, created=1.0)
        recent = lease("recent", last_prompt=90.0, created=2.0)
        not_capable = lease("nc", last_prompt=99.0, capable=False)
        no_mentions = lease("nm", last_prompt=99.0, mentions=False)
        codex = lease("cx", platform="codex", last_prompt=99.0)
        c, d = R.route(msg(content=DIRECT[0], flags=DIRECT[1]), ME, ctx(), [old, recent, not_capable, no_mentions, codex])
        self.assertEqual(d.lease_items, [("recent", "interrupt", "message")])
        self.assertTrue(d.seat_inbox)
        self.assertEqual(d.delivered_to, "recent")
        self.assertIsNone(d.wake, "no headless wake after a live delivery")
        tie = R.route(msg(content=DIRECT[0], flags=DIRECT[1]), ME, ctx(),
                      [lease("a", created=1.0), lease("b", created=5.0)])[1]
        self.assertEqual(tie.lease_items[0][0], "b")

    def test_with_no_wake_capable_lease_a_mention_falls_to_the_inbox_and_the_wake(self) -> None:
        c, d = R.route(msg(content=DIRECT[0], flags=DIRECT[1]), ME, ctx(), [lease("nc", capable=False)])
        self.assertEqual(d.lease_items, [])
        self.assertEqual((d.seat_inbox, d.wake), (True, "peer"))

    def test_muted_topics_drop_unless_direct_or_dm(self) -> None:
        muted = ctx(muted=[topic_key("agent-sync", "t")])
        self.assertEqual(R.route(msg(content="@*fleet* x"), ME, muted, [])[1].drop_reason, "muted")
        self.assertTrue(R.route(msg(content=DIRECT[0], flags=DIRECT[1]), ME, muted, [])[1].seat_inbox)

    def test_topics_match_without_case(self) -> None:
        a = lease("A", topics=[("AGENT-SYNC", "AFC Work")])
        self.assertEqual(R.route(msg(topic="afc work"), ME, ctx(), [a])[1].lease_items, [("A", "passive", "message")])


if __name__ == "__main__":
    unittest.main()
