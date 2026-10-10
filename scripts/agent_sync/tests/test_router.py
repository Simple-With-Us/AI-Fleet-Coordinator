"""Routing rules, one by one:  classes, the wake prefilter rows, leases, mention leases, siblings,
muted and followed topics, and the owner rule (owner id AND a human client)."""
from __future__ import annotations

import unittest

from agent_sync import config as C
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
# The fleet-wide wake (owner 2026-10-09):  @**all** in #agent-sync > fleet.  Zulip sets the stream wildcard flag.
FLEET_KW = dict(topic="fleet", flags=["wildcard_mentioned", "stream_wildcard_mentioned"])


class ClassifyTests(unittest.TestCase):
    def test_direct_needs_the_flag_and_the_raw_name(self) -> None:
        self.assertTrue(R.classify(msg(content=DIRECT[0], flags=DIRECT[1]), ME, ctx()).direct)
        self.assertTrue(R.classify(msg(content="@**Claude|10** hi", flags=["mentioned"]), ME, ctx()).direct)
        self.assertFalse(R.classify(msg(content="@*ops* all hands", flags=["mentioned"]), ME, ctx()).direct,
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
        self.assertTrue(R.classify(msg(dm=True), ME, ctx()).dm)
        self.assertTrue(R.classify(msg(sender=10), ME, ctx()).own)
        self.assertEqual(R.classify(msg(content="[CLAUDE·ab] hi re=5"), ME, ctx()).reply_to, 5)
        self.assertIsNone(R.classify(msg(content="hi\nre=5 on line two"), ME, ctx()).reply_to)

    def test_at_all_in_agent_sync_fleet_is_the_fleet_wake(self) -> None:
        for name, kw in {
            "flags and literal": dict(content="@**all** HALT", **FLEET_KW),
            "stream flag only": dict(content="HALT", topic="fleet", flags=["stream_wildcard_mentioned"]),
            "legacy flag only": dict(content="HALT", topic="fleet", flags=["wildcard_mentioned"]),
            "literal only (no flags on the event)": dict(content="[CODEX·ab] @**all** HALT", topic="fleet"),
            "topic and channel without case": dict(content="@**all** HALT", topic="Fleet", channel="Agent-Sync"),
            "@**everyone** is the same stream wildcard": dict(content="@**everyone** HALT", **FLEET_KW),
        }.items():
            with self.subTest(name):
                c = R.classify(msg(**kw), ME, ctx())
                self.assertTrue(c.fleet, "fleet")
        # A real @**all** also carries the wildcard class, as it always did.
        c = R.classify(msg(content="@**all** HALT", **FLEET_KW), ME, ctx())
        self.assertTrue(c.fleet and c.wildcard and "fleet" in c.labels() and "wildcard" in c.labels())

    def test_a_wildcard_anywhere_else_is_only_a_wildcard(self) -> None:
        for name, kw in {
            "another topic": dict(content="@**all** hi", topic="t", flags=["stream_wildcard_mentioned"]),
            "another channel, topic fleet": dict(content="@**all** hi", topic="fleet", channel="general",
                                                  flags=["stream_wildcard_mentioned"]),
            "roll call": dict(content="@**all** hi", topic="roll call", flags=["stream_wildcard_mentioned"]),
            "literal in another topic": dict(content="@**all** hi", topic="t"),
        }.items():
            with self.subTest(name):
                c = R.classify(msg(**kw), ME, ctx())
                self.assertFalse(c.fleet, "fleet")
        self.assertTrue(R.classify(msg(content="@**all** hi", topic="t", flags=["stream_wildcard_mentioned"]),
                                   ME, ctx()).wildcard)

    def test_fleet_needs_a_real_stream_wildcard(self) -> None:
        for name, kw in {
            "plain chatter in fleet": dict(content="standup notes", topic="fleet"),
            "@**topic** reaches only the topic's participants": dict(
                content="@**topic** hi", topic="fleet", flags=["wildcard_mentioned", "topic_wildcard_mentioned"]),
            "@**all** in a code span": dict(content="use `@**all**` to wake everyone", topic="fleet"),
            "@**all** in a code block": dict(content="```\n@**all**\n```", topic="fleet"),
            "@**all** in a quote": dict(content="> @**all** said earlier\nok", topic="fleet"),
            "escaped @**all**": dict(content="\\@**all** is how you ping", topic="fleet"),
            "the old group text": dict(content="@*fleet* wake up", topic="fleet"),
            "@**all** glued to a word": dict(content="mail@**all**", topic="fleet"),
        }.items():
            with self.subTest(name):
                self.assertFalse(R.classify(msg(**kw), ME, ctx()).fleet, "fleet")
        # The old group text is no longer a fleet wake anywhere, so it cannot trigger an owner wake.
        d = R.route(msg(sender=OWNER, content="@*fleet* wake up", topic="t"), ME, ctx(), [])[1]
        self.assertIsNone(d.wake)

    def test_unknown_senders_count_as_bots_and_are_not_eligible(self) -> None:
        c = R.classify(msg(sender=999), ME, ctx())
        self.assertTrue(c.sender_is_bot)
        self.assertFalse(c.eligible)


def realm_bot(uid, email, *, bot_type=1, active=True, is_bot=True, bot_owner=OWNER):
    return {"user_id": uid, "email": email, "full_name": email, "is_bot": is_bot, "is_active": active,
            "bot_type": bot_type, "bot_owner_id": bot_owner}


class FleetBotIdsTests(unittest.TestCase):
    """Owner 2026-10-09:  every fleet bot is an eligible sender, computed at runtime from the realm user
    list and the partition's tags, so a new seat needs no re-init.  Integrations stay out."""

    def setUp(self) -> None:
        partition, error = C.load_partition(C.partition_path({}))
        self.assertIsNone(error)
        self.tags = C.fleet_tags(partition)

    def test_every_kind_of_fleet_bot_counts(self) -> None:
        users = {
            10: realm_bot(10, "claude-bot@zulip.test"),
            21: realm_bot(21, "compiler-grok-bot@zulip.test"),   # GB-COMPILER
            22: realm_bot(22, "instinct-bat-bot@zulip.test"),    # ECHO
            23: realm_bot(23, "instinct-owl-bot@zulip.test"),    # INSTINCT
            24: realm_bot(24, "bf-fixer-bot@zulip.test"),        # BF-FIXER (partition: none)
            25: realm_bot(25, "grok-web-bot@zulip.test"),        # GROK-WEB
            26: realm_bot(26, "openai-dot-bot@zulip.test"),      # JET
            27: realm_bot(27, "muse-assist-bot@zulip.test"),     # MA
            28: realm_bot(28, "grok-build-bot@zulip.test"),      # GROK
        }
        self.assertEqual(R.fleet_bot_ids(users, self.tags, OWNER), set(users))

    def test_integrations_people_unknown_and_inactive_bots_do_not(self) -> None:
        users = {
            40: realm_bot(40, "sentry-bot@zulip.test", bot_type=2),
            41: realm_bot(41, "pagerduty-bot@zulip.test", bot_type=2),
            42: realm_bot(42, "linear-bot@zulip.test", bot_type=2),
            43: realm_bot(43, "codex-bot@zulip.test", bot_type=2),       # a webhook bot never counts, whatever its email
            44: realm_bot(44, "cursor-bot@zulip.test", bot_type=3),
            45: realm_bot(45, "mystery-bot@zulip.test"),                 # a generic bot with no fleet tag
            46: realm_bot(46, "mm-bot@zulip.test", active=False),
            12: realm_bot(12, "codex@zulip.test", is_bot=False),         # a person, even with a seat-like name
        }
        self.assertEqual(R.fleet_bot_ids(users, self.tags, OWNER), set())

    def test_a_bot_the_owner_does_not_own_is_not_a_fleet_bot(self) -> None:
        """The trust anchor:  a bot someone else created under a fleet-looking email never counts, and with no
        owner pinned nothing counts."""
        users = {
            30: realm_bot(30, "gb-spoof-grok-bot@zulip.test", bot_owner=CODEX),
            31: realm_bot(31, "codex-bot@zulip.test", bot_owner=None),
            32: realm_bot(32, "codex-bot@zulip.test"),
        }
        self.assertEqual(R.fleet_bot_ids(users, self.tags, OWNER), {32})
        self.assertEqual(R.fleet_bot_ids(users, self.tags, 0), set(), "no owner pinned, no fleet bots")

    def test_the_tags_cover_the_partition_and_the_fleet_seats(self) -> None:
        for tag in ("CLAUDE", "JET", "GB-DIRECTOR", "BF-BUILDER", "ECHO", "INSTINCT", "GROK-WEB", "GROK-BUILD", "MA", "OPENCODE"):
            with self.subTest(tag=tag):
                self.assertIn(tag, self.tags)
        self.assertEqual(C.fleet_tags(None), frozenset(C.FLEET_SEATS), "no partition, only the fixed seats")


class PrefilterTests(unittest.TestCase):
    """Every row of the wake prefilter table (design section 2, step 7)."""

    def wake(self, message, **kw):
        c, d = R.route(message, ME, ctx(**kw), [])
        return d.seat_inbox, d.wake, d.wake_block

    def test_owner_direct_dm_or_fleet_wakes_as_owner(self) -> None:
        self.assertEqual(self.wake(msg(sender=OWNER, content=DIRECT[0], flags=DIRECT[1])), (True, "owner", None))
        self.assertEqual(self.wake(msg(sender=OWNER, dm=True)), (True, "owner", None))
        self.assertEqual(self.wake(msg(sender=OWNER, content="@**all** everyone stop", **FLEET_KW)), (True, "owner", None))
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
        self.assertEqual(self.wake(msg(sender=JET, content="@**all** standup", **FLEET_KW), **pinned),
                         (True, None, "group_or_wildcard"))
        # Owner 2026-10-09:  an eligible bot's DM wakes like a direct mention.
        self.assertEqual(self.wake(msg(sender=JET, dm=True), **pinned), (True, "peer", None))
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
        # A peer's fleet wake lands in the inbox and never starts a turn:  it would wake about 12 seats.
        self.assertEqual(self.wake(msg(content="@**all** standup", **FLEET_KW)), (True, None, "group_or_wildcard"))
        self.assertEqual(self.wake(msg(content="@**all** heads up", flags=["stream_wildcard_mentioned"])),
                         (True, None, "group_or_wildcard"))
        self.assertEqual(self.wake(msg(content="chatter"), followed=[topic_key("agent-sync", "t")]),
                         (True, None, "not_a_mention"))

    def test_wake_tag_and_stale_peer_direct_do_not_wake(self) -> None:
        self.assertEqual(self.wake(msg(content="[CODEX·wake] re=1 @**Claude** hi", flags=["mentioned"])),
                         (True, None, "wake_tag"))
        self.assertEqual(self.wake(msg(content=DIRECT[0], flags=DIRECT[1], ts=NOW - 9000)), (True, None, "stale"))

    def test_an_eligible_bot_dm_wakes_as_a_peer(self) -> None:
        """Owner 2026-10-09:  "everyone should be able to DM to wake anyone else".  A DM from an eligible bot
        wakes like a direct mention, within the peer budgets, and never as the owner."""
        self.assertEqual(self.wake(msg(dm=True)), (True, "peer", None))
        c = R.classify(msg(dm=True), ME, ctx())
        self.assertTrue(c.dm and c.eligible and c.sender_is_bot and not c.owner)

    def test_a_dm_keeps_every_other_rail(self) -> None:
        self.assertEqual(self.wake(msg(dm=True, content="[CODEX·wake] re=7\nthanks")), (True, None, "wake_tag"),
                         "a wake reply by DM never wakes, so two bots cannot ping-pong on wake replies")
        self.assertEqual(self.wake(msg(dm=True, ts=NOW - 9000)), (True, None, "stale"))
        for sender in (SENTRY, NEW_BOT, 999):
            with self.subTest(sender=sender):
                self.assertEqual(self.wake(msg(sender=sender, dm=True)), (True, None, "not_eligible"))
        self.assertEqual(self.wake(msg(sender=OWNER, client="ZulipPython", dm=True)), (True, None, "owner_api"))
        self.assertEqual(self.wake(msg(sender=10, dm=True)), (False, None, None), "a seat's own DM never wakes it")
        self.assertEqual(R.route(msg(sender=10, dm=True), ME, ctx(), [])[1].drop_reason, "own")

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

    def test_an_owner_fleet_wake_goes_to_one_wake_capable_lease_like_a_mention(self) -> None:
        old = lease("old", last_prompt=50.0, created=1.0)
        recent = lease("recent", last_prompt=90.0, created=2.0)
        c, d = R.route(msg(sender=OWNER, content="@**all** HALT", **FLEET_KW), ME, ctx(), [old, recent])
        self.assertTrue(c.fleet and c.owner)
        self.assertEqual(d.lease_items, [("recent", "interrupt", "message")])
        self.assertEqual((d.seat_inbox, d.delivered_to, d.wake_block), (True, "recent", "delivered_to_lease"))
        self.assertTrue(d.wake_owner)
        # With no wake-capable lease it falls to the seat inbox and the owner wake.
        c, d = R.route(msg(sender=OWNER, content="@**all** HALT", **FLEET_KW), ME, ctx(), [lease("nc", capable=False)])
        self.assertEqual((d.lease_items, d.seat_inbox, d.wake), ([], True, "owner"))

    def test_a_peer_fleet_wake_reaches_the_inbox_and_never_a_lease_or_a_wake(self) -> None:
        c, d = R.route(msg(content="@**all** HALT", **FLEET_KW), ME, ctx(), [lease("L")])
        self.assertTrue(c.fleet and c.eligible and not c.owner)
        self.assertEqual((d.lease_items, d.seat_inbox, d.wake, d.wake_block), ([], True, None, "group_or_wildcard"))

    def test_an_owner_wildcard_outside_the_fleet_topic_keeps_the_wildcard_handling(self) -> None:
        for topic in ("t", "roll call", "AFC 1a2b3c4d work"):
            with self.subTest(topic=topic):
                c, d = R.route(msg(sender=OWNER, content="@**all** hi", topic=topic,
                                   flags=["wildcard_mentioned", "stream_wildcard_mentioned"]), ME, ctx(), [])
                self.assertFalse(c.fleet)
                self.assertTrue(c.wildcard)
                self.assertEqual((d.seat_inbox, d.wake, d.wake_block), (True, None, "group_or_wildcard"))

    def test_with_no_wake_capable_lease_a_mention_falls_to_the_inbox_and_the_wake(self) -> None:
        c, d = R.route(msg(content=DIRECT[0], flags=DIRECT[1]), ME, ctx(), [lease("nc", capable=False)])
        self.assertEqual(d.lease_items, [])
        self.assertEqual((d.seat_inbox, d.wake), (True, "peer"))

    def test_muted_topics_drop_unless_direct_or_dm(self) -> None:
        muted = ctx(muted=[topic_key("agent-sync", "t")])
        self.assertEqual(R.route(msg(content="@**all** x", **FLEET_KW), ME,
                                 ctx(muted=[topic_key("agent-sync", "fleet")]), [])[1].drop_reason, "muted")
        self.assertEqual(R.route(msg(content="@**all** x", flags=["stream_wildcard_mentioned"]), ME, muted, [])[1].drop_reason,
                         "muted")
        self.assertTrue(R.route(msg(content=DIRECT[0], flags=DIRECT[1]), ME, muted, [])[1].seat_inbox)

    def test_topics_match_without_case(self) -> None:
        a = lease("A", topics=[("AGENT-SYNC", "AFC Work")])
        self.assertEqual(R.route(msg(topic="afc work"), ME, ctx(), [a])[1].lease_items, [("A", "passive", "message")])


if __name__ == "__main__":
    unittest.main()
