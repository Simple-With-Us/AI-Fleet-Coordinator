"""Replies wake the chat they answer (owner, Nov 10 2026).

The owner's words:  "if anything I'd want it to be more liberal with what it considered a wake like a
reply to a post that didn't have address to any chat thread", "too many wakes ais not my concern and I
long for the day that I am annoyed by too many wakes", and for the core case "if someone comments a reply
to someone that tagged tehm".  Chat A (seat S, header letter AA) tags seat T in a topic, T's chat answers
there without tagging anyone, and that wakes chat A:  seat S, letter AA.

Three layers:  the ledger (`topic_ledger.py`), the router (`router.classify`, `prefilter`, `route`), and
the daemon end to end against the fake realm, where the routine seat's wake POST is read off the wire.
"""
from __future__ import annotations

import json
import secrets
import textwrap
import threading
import unittest
from typing import Any

from simple_tools import adapters as A
from simple_tools import live as L
from simple_tools import router as R
from simple_tools import wakes as W
from simple_tools.live import topic_key
from simple_tools.tests.fake_routine import FakeRoutine
from simple_tools.tests.fake_zulip import EPOCH
from simple_tools.tests.harness import write_rc
from simple_tools.tests.test_daemon import DaemonHarness
from simple_tools.topic_ledger import (
    MAX_TOPICS,
    POSTS_PER_TOPIC,
    ReplyTarget,
    TopicLedger,
    TopicPost,
)

NOW = 10_000.0
OWNER, CODEX, CURSOR, SENTRY = 12, 11, 13, 40
ME = R.SeatIdentity("CLAUDE", 10, "Claude", "claude-bot@zulip.test")
EMAIL = {
    10: "claude-bot@zulip.test",
    CODEX: "codex-bot@zulip.test",
    CURSOR: "cursor-bot@zulip.test",
    OWNER: "jay@zulip.test",
    SENTRY: "sentry-bot@zulip.test",
}
SEATS = {10: "CLAUDE", CODEX: "CODEX", CURSOR: "CURSOR"}
NAMES = {"claude": "CLAUDE", "codex": "CODEX", "cursor": "CURSOR"}
KEY = topic_key("simple-tools", "t")

CHAT_A = "[CLAUDE: AA.1.S 📁AFC→CODEX] @**Codex** please look at the cutover"
CHAT_A_UNLETTERED = "[CLAUDE 📁AFC→CODEX] @**Codex** please look at the cutover"
CODEX_REPLY = "[CODEX: BB.2.S 📁AFC] looked, it is fine"


def msg(
    sender: int = CODEX,
    content: str = CODEX_REPLY,
    *,
    mid: int = 101,
    topic: str = "t",
    channel: str = "simple-tools",
    ts: float = NOW - 10,
    flags: tuple[str, ...] = (),
    dm: bool = False,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "id": mid,
        "sender_id": sender,
        "sender_email": EMAIL[sender],
        "content": content,
        "flags": list(flags),
        "timestamp": ts,
        "client": "website" if sender == OWNER else "ZulipPython",
        "sender_full_name": "x",
    }
    if dm:
        message.update(
            {"type": "private", "display_recipient": [{"id": sender}, {"id": 10}], "subject": ""}
        )
    else:
        message.update({"type": "stream", "display_recipient": channel, "subject": topic})
    return message


def ctx(ledger: TopicLedger | None, **kw: Any) -> R.Context:
    options: dict[str, Any] = dict(
        owner_user_id=OWNER,
        owner_clients=["website", "ZulipMobile"],
        eligible_user_ids=[10, CODEX, CURSOR],
        is_bot={10: True, CODEX: True, CURSOR: True, OWNER: False, SENTRY: True},
        now=NOW,
        stale_after=7200.0,
        presence=[("simple-tools", "roll call"), ("alerts", "*")],
        topic_ledger=ledger,
    )
    options.update(kw)
    return R.Context(**options)


def keep(
    ledger: TopicLedger,
    sender: int,
    content: str,
    mid: int,
    *,
    topic: str = "t",
    channel: str = "simple-tools",
) -> TopicPost:
    """Put one post in the ledger the way the daemon does."""
    message = msg(sender, content, mid=mid, topic=topic, channel=channel)
    post = R.topic_post(message, seat=SEATS.get(sender, ""), names=NAMES)
    assert post is not None
    ledger.record(topic_key(channel, topic), post)
    return post


def after_chat_a(letter_post: str = CHAT_A) -> TopicLedger:
    """A ledger where seat CLAUDE's chat AA tagged CODEX in topic t (message 100)."""
    ledger = TopicLedger()
    keep(ledger, 10, letter_post, 100)
    return ledger


def routed(message: dict[str, Any], ledger: TopicLedger | None, **kw: Any):
    return R.route(message, ME, ctx(ledger, **kw), [])


# --------------------------------------------------------------------------------------------
# The ledger
# --------------------------------------------------------------------------------------------


class TopicLedgerTests(unittest.TestCase):
    def post(self, mid: int, *, seat: str = "CLAUDE", letter: str = "AA", tagged=(), sender=10):
        return TopicPost(mid, sender, seat, letter, frozenset(tagged))

    def test_a_topic_keeps_its_newest_posts_oldest_first_and_only_that_many(self) -> None:
        ledger = TopicLedger(per_topic=5)
        for mid in range(1, 9):
            ledger.record(KEY, self.post(mid))
        self.assertEqual([p.message_id for p in ledger.posts(KEY)], [4, 5, 6, 7, 8])

    def test_the_defaults_are_forty_posts_in_each_of_two_thousand_topics(self) -> None:
        self.assertEqual((POSTS_PER_TOPIC, MAX_TOPICS), (40, 2000))
        ledger = TopicLedger()
        for mid in range(1, 101):
            ledger.record(KEY, self.post(mid))
        self.assertEqual(len(ledger.posts(KEY)), 40)
        self.assertEqual(ledger.posts(KEY)[0].message_id, 61)

    def test_topics_are_bounded_and_the_least_recently_written_goes_first(self) -> None:
        ledger = TopicLedger(max_topics=3)
        for n in range(4):
            ledger.record("k%d" % n, self.post(n + 1))
        self.assertEqual(sorted(ledger.keys()), ["k1", "k2", "k3"])
        ledger.record("k1", self.post(10))  # k1 is written again, so k2 is now the oldest
        ledger.record("k4", self.post(11))
        self.assertEqual(sorted(ledger.keys()), ["k1", "k3", "k4"])
        self.assertEqual(ledger.stats(), {"topics": 3, "posts": 4})

    def test_posts_arriving_out_of_order_are_kept_in_message_order(self) -> None:
        ledger = TopicLedger()
        for mid in (105, 101, 103, 102, 104):
            ledger.record(KEY, self.post(mid))
        self.assertEqual([p.message_id for p in ledger.posts(KEY)], [101, 102, 103, 104, 105])

    def test_a_post_older_than_a_full_topic_is_not_kept(self) -> None:
        ledger = TopicLedger(per_topic=3)
        for mid in (10, 11, 12):
            ledger.record(KEY, self.post(mid))
        self.assertFalse(ledger.record(KEY, self.post(5)))
        self.assertEqual([p.message_id for p in ledger.posts(KEY)], [10, 11, 12])
        self.assertTrue(ledger.record(KEY, self.post(13)))

    def test_one_message_is_kept_once_whichever_queue_or_retry_brings_it(self) -> None:
        ledger = TopicLedger()
        self.assertTrue(ledger.record(KEY, self.post(100)))
        self.assertFalse(ledger.record(KEY, self.post(100, letter="ZZ")), "the first copy stands")
        self.assertEqual([(p.message_id, p.letter) for p in ledger.posts(KEY)], [(100, "AA")])
        self.assertTrue(ledger.seen(KEY, 100))
        self.assertFalse(ledger.seen(KEY, 101))
        self.assertFalse(ledger.seen("other", 100))

    def test_only_posts_strictly_earlier_than_the_message_are_read(self) -> None:
        ledger = TopicLedger()
        for mid, letter in ((100, "AA"), (101, "BB"), (102, "CC")):
            ledger.record(KEY, self.post(mid, letter=letter))
        self.assertEqual([p.message_id for p in ledger.posts(KEY, before_id=101)], [100])
        self.assertEqual([p.message_id for p in ledger.posts(KEY, before_id=100)], [])
        # The message itself is in the ledger when it is judged (another seat's queue was first), and a
        # later post is too, and neither is ever the answer.
        self.assertEqual(ledger.reply_target(KEY, "CLAUDE", 10, 101), ReplyTarget("CLAUDE", "AA", 100))
        self.assertIsNone(ledger.reply_target(KEY, "CLAUDE", 10, 100))

    def test_the_answer_is_the_most_recent_post_of_the_seat_and_its_letter(self) -> None:
        ledger = TopicLedger()
        ledger.record(KEY, self.post(100, letter="AA"))
        ledger.record(KEY, self.post(101, seat="CODEX", letter="BB", sender=CODEX))
        ledger.record(KEY, self.post(102, letter="AB"))
        self.assertEqual(ledger.reply_target(KEY, "CLAUDE", 10, 200), ReplyTarget("CLAUDE", "AB", 102))
        self.assertEqual(ledger.reply_target(KEY, "CODEX", CODEX, 200), ReplyTarget("CODEX", "BB", 101))
        self.assertIsNone(ledger.reply_target(KEY, "CURSOR", CURSOR, 200), "it never posted here")
        self.assertIsNone(ledger.reply_target("other", "CLAUDE", 10, 200), "nor in another topic")

    def test_the_chat_that_tagged_the_sender_is_preferred_over_a_more_recent_one(self) -> None:
        ledger = TopicLedger()
        ledger.record(KEY, self.post(100, letter="AA", tagged=["CODEX"]))
        ledger.record(KEY, self.post(101, letter="AB", tagged=["CURSOR"]))
        ledger.record(KEY, self.post(102, letter="AC"))
        self.assertEqual(
            ledger.reply_target(KEY, "CLAUDE", 10, 200, sender_seat="CODEX").letter, "AA"
        )
        self.assertEqual(
            ledger.reply_target(KEY, "CLAUDE", 10, 200, sender_seat="CURSOR").letter, "AB"
        )
        self.assertEqual(
            ledger.reply_target(KEY, "CLAUDE", 10, 200, sender_seat="GROK").letter, "AC",
            "nobody tagged this sender:  the most recent",
        )
        self.assertEqual(ledger.reply_target(KEY, "CLAUDE", 10, 200).letter, "AC", "the owner has no seat")

    def test_another_bot_that_signs_as_the_seat_counts_as_the_seat(self) -> None:
        ledger = TopicLedger()
        ledger.record(KEY, self.post(100, seat="GROK", letter="GG", sender=14))
        self.assertEqual(ledger.reply_target(KEY, "GROK", 99, 200).letter, "GG")
        self.assertEqual(ledger.reply_target(KEY, "", 14, 200).letter, "GG", "or its own bot by id")

    def test_a_renamed_topic_keeps_its_posts_and_merges_with_one_that_exists(self) -> None:
        ledger = TopicLedger(per_topic=3)
        for mid in (1, 2):
            ledger.record("old", self.post(mid))
        for mid in (2, 3, 4):
            ledger.record("new", self.post(mid))
        ledger.rename("old", "new")
        self.assertEqual(ledger.keys(), ["new"])
        self.assertEqual([p.message_id for p in ledger.posts("new")], [2, 3, 4], "merged, one copy, bounded")
        ledger.rename("old", "new")  # every seat's queue brings the event:  the second is a no-op
        self.assertEqual([p.message_id for p in ledger.posts("new")], [2, 3, 4])

    def test_it_is_safe_to_use_from_many_threads(self) -> None:
        ledger = TopicLedger(max_topics=50, per_topic=10)
        errors: list[BaseException] = []

        def work(offset: int) -> None:
            try:
                for n in range(300):
                    ledger.record("k%d" % (n % 80), self.post(offset + n))
                    ledger.reply_target("k%d" % (n % 80), "CLAUDE", 10, offset + n + 1)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(i * 1000,)) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        stats = ledger.stats()
        self.assertLessEqual(stats["topics"], 50)
        self.assertLessEqual(stats["posts"], 50 * 10)


class TopicPostBuildTests(unittest.TestCase):
    def test_the_letter_and_the_tagged_seats_come_from_the_header_and_the_loud_mentions(self) -> None:
        post = R.topic_post(msg(10, CHAT_A, mid=100), seat="CLAUDE", names=NAMES)
        self.assertEqual(post, TopicPost(100, 10, "CLAUDE", "AA", frozenset({"CODEX"})))
        post = R.topic_post(
            msg(10, "[CLAUDE: bb.3.M 📁AFC] @**Cursor** and @**Codex|11** look, @_**Grok** fyi", mid=7),
            seat="CLAUDE",
            names=NAMES,
        )
        self.assertEqual(post.letter, "BB")
        self.assertEqual(post.tagged, frozenset({"CURSOR", "CODEX"}), "a silent mention tags nobody")

    def test_a_mention_in_code_or_a_quote_tags_nobody(self) -> None:
        post = R.topic_post(
            msg(10, "[CLAUDE: AA.1.S 📁AFC] use `@**Codex**` to ping\n> @**Cursor** said so", mid=7),
            seat="CLAUDE",
            names=NAMES,
        )
        self.assertEqual(post.tagged, frozenset())

    def test_a_post_without_a_header_has_no_letter_and_a_legacy_tag_names_its_peers(self) -> None:
        self.assertEqual(R.topic_post(msg(10, "just text", mid=7), seat="CLAUDE").letter, "")
        legacy = R.topic_post(msg(10, "[CLAUDE·aaaa1111→CODEX] hi", mid=8), seat="CLAUDE", names=NAMES)
        self.assertEqual((legacy.letter, legacy.tagged), ("", frozenset({"CODEX"})))
        marked = R.topic_post(msg(10, "👀[CLAUDE: AA.1.S 📁AFC] quiet", mid=9), seat="CLAUDE")
        self.assertEqual(marked.letter, "AA", "the no-wake marker does not hide the header")

    def test_a_header_letter_is_a_short_token_or_none(self) -> None:
        self.assertEqual(R.header_letter(": AA.1.S"), "AA")
        self.assertEqual(R.header_letter(" : b-1-M"), "B")
        self.assertEqual(R.header_letter(""), "")
        self.assertEqual(R.header_letter(": ABCDEFGH.1.S"), "ABCDEFGH")
        self.assertEqual(R.header_letter(": ABCDEFGHI.1.S"), "", "longer than 8 is no letter")
        post = R.topic_post(msg(10, "[CLAUDE: %s.1.S 📁AFC] hi" % ("Z" * 500), mid=7), seat="CLAUDE")
        self.assertEqual(post.letter, "")

    def test_a_bot_cannot_sign_as_another_seats_chat(self) -> None:
        post = R.topic_post(msg(CODEX, "[CLAUDE: AA.1.S 📁AFC] i am claude", mid=7), seat="CODEX")
        self.assertEqual(post.letter, "", "the header names a seat other than the sender's")

    def test_a_direct_message_or_a_message_without_an_id_is_never_kept(self) -> None:
        self.assertIsNone(R.topic_post(msg(10, "hi", dm=True), seat="CLAUDE"))
        self.assertIsNone(R.topic_post({"content": "hi", "type": "stream"}, seat="CLAUDE"))
        self.assertIsNone(R.topic_post(dict(msg(10, "hi"), id=True), seat="CLAUDE"))

    def test_what_tags_anyone(self) -> None:
        for content in (
            "[CODEX 📁AFC→CLAUDE] done",
            "[CODEX·aaaa→CLAUDE] done",
            "@**Jay Wedgeworth** done",
            "@**Claude|10** done",
            "@*ops* done",
            "@**all** done",
            "@all done",
            "@everyone done",
            "@**topic** done",
            "👀[CODEX 📁AFC→CLAUDE] done",
        ):
            with self.subTest(content=content):
                self.assertTrue(R.tags_anyone(content))
        for content in (
            "done",
            "[CODEX: BB.2.S 📁AFC] done",
            "[CODEX·aaaa] done",
            "@_**Claude** done",
            "use `@**Claude**` to ping",
            "> @**Claude** said so\ndone",
            "mail@**x**",
            "\\@**Claude** done",
            "email me at a@b.com",
        ):
            with self.subTest(content=content):
                self.assertFalse(R.tags_anyone(content))


# --------------------------------------------------------------------------------------------
# The router
# --------------------------------------------------------------------------------------------


class ReplyClassTests(unittest.TestCase):
    def test_an_untagged_reply_to_a_chat_that_tagged_the_sender_wakes_that_chat(self) -> None:
        ledger = after_chat_a()
        c, d = routed(msg(CODEX, CODEX_REPLY), ledger)
        self.assertEqual(c.reply_to_chat, ReplyTarget("CLAUDE", "AA", 100))
        self.assertIn("reply", c.labels())
        self.assertEqual(R.prefilter(c), ("peer", None))
        self.assertEqual((d.wake, d.wake_block, d.wake_owner, d.wake_worthy), ("peer", None, False, True))
        self.assertTrue(d.seat_inbox)
        self.assertEqual((d.linked_by, d.reply_target_letter), ("reply", "AA"))
        shown = d.describe()
        self.assertEqual((shown["linked_by"], shown["reply_target_letter"]), ("reply", "AA"))
        self.assertEqual(shown["wake"], "peer")

    def test_any_later_untagged_post_wakes_the_earlier_poster_a_third_bot_or_the_owner(self) -> None:
        ledger = after_chat_a()
        keep(ledger, CODEX, CODEX_REPLY, 101)
        for who, content in (
            (CURSOR, "[CURSOR 📁AFC] nice"),
            (CURSOR, "plain words with no header"),
            (CODEX, "[CODEX: BB.2.S 📁AFC] one more thing"),
            (OWNER, "thanks, ship it"),
        ):
            with self.subTest(who=who, content=content):
                c, d = routed(msg(who, content, mid=150), ledger)
                self.assertEqual(c.reply_to_chat, ReplyTarget("CLAUDE", "AA", 100))
                self.assertIsNotNone(d.wake)
                self.assertTrue(d.seat_inbox)

    def test_the_owners_untagged_reply_wakes_as_the_owner(self) -> None:
        ledger = after_chat_a()
        c, d = routed(msg(OWNER, "looks good to me", mid=101), ledger)
        self.assertTrue(c.owner)
        self.assertEqual(R.prefilter(c), ("owner", None))
        self.assertEqual((d.wake, d.wake_owner, d.wake_worthy), ("owner", True, True))
        self.assertEqual(d.reply_target_letter, "AA")

    def test_a_bots_reply_is_a_peer_wake_never_an_owner_one(self) -> None:
        c, d = routed(msg(CODEX, "ok", mid=101), after_chat_a())
        self.assertEqual((d.wake, d.wake_owner), ("peer", False))

    def test_a_seats_own_post_never_wakes_it(self) -> None:
        ledger = after_chat_a()
        keep(ledger, 10, "[CLAUDE: AB.1.S 📁AFC] a second chat of mine", 101)
        c, d = routed(msg(10, "[CLAUDE: AB.1.S 📁AFC] and again", mid=102), ledger)
        self.assertTrue(c.own)
        self.assertIsNone(c.reply_to_chat)
        self.assertEqual((d.wake, d.drop_reason, d.seat_inbox), (None, "own", False))

    def test_a_sibling_bot_that_signs_as_the_seat_is_its_own_voice_and_never_wakes_it(self) -> None:
        ledger = after_chat_a()
        sibling = dict(msg(CURSOR, "hello", mid=101), sender_email="claude-bot@zulip.test")
        c, d = routed(sibling, ledger, eligible_user_ids=[10, CODEX, CURSOR])
        self.assertIsNone(c.reply_to_chat)
        self.assertIsNone(d.wake)

    def test_a_topic_where_the_seat_never_posted_stays_not_a_mention(self) -> None:
        ledger = TopicLedger()
        keep(ledger, CODEX, "[CODEX: BB.2.S 📁AFC] hello", 100)
        keep(ledger, CURSOR, "[CURSOR 📁AFC] hi", 101)
        for name, topic_ledger, message in (
            ("nobody else posted", ledger, msg(CODEX, "more", mid=102)),
            ("an empty ledger", TopicLedger(), msg(CODEX, "more", mid=102)),
            ("no ledger at all", None, msg(CODEX, "more", mid=102)),
            ("the seat posted in another topic", after_chat_a(), msg(CODEX, "more", mid=102, topic="u")),
            ("the seat posted only later", after_chat_a(), msg(CODEX, "more", mid=99)),
            ("the seat posted in the same topic of another channel", after_chat_a(),
             msg(CODEX, "more", mid=102, channel="other")),
        ):
            with self.subTest(name):
                c, d = routed(message, topic_ledger)
                self.assertIsNone(c.reply_to_chat)
                self.assertEqual(R.prefilter(c), (None, "not_a_mention"))
                self.assertEqual((d.wake, d.drop_reason, d.seat_inbox), (None, "chatter", False))
                self.assertIsNone(d.linked_by)
                self.assertNotIn("reply", c.labels())

    def test_an_earlier_post_without_a_letter_wakes_with_no_letter(self) -> None:
        for earlier in (CHAT_A_UNLETTERED, "[CLAUDE·aaaa1111] looking", "no header at all"):
            with self.subTest(earlier=earlier):
                c, d = routed(msg(CODEX, "answer"), after_chat_a(earlier))
                self.assertEqual(c.reply_to_chat, ReplyTarget("CLAUDE", "", 100))
                self.assertEqual((d.wake, d.linked_by, d.reply_target_letter), ("peer", "reply", ""))
                self.assertEqual(d.describe()["reply_target_letter"], "")

    def test_the_chat_that_tagged_the_sender_is_the_one_woken(self) -> None:
        ledger = TopicLedger()
        keep(ledger, 10, CHAT_A, 100)  # AA tagged Codex
        keep(ledger, 10, "[CLAUDE: AB.1.S 📁AFC→CURSOR] @**Cursor** look too", 101)
        keep(ledger, 10, "[CLAUDE: AC.1.S 📁AFC] thinking aloud", 102)
        for who, letter in ((CODEX, "AA"), (CURSOR, "AB"), (OWNER, "AC")):
            with self.subTest(who=who):
                self.assertEqual(routed(msg(who, "ok", mid=110), ledger)[0].reply_to_chat.letter, letter)

    def test_a_later_post_in_the_ledger_never_changes_the_answer(self) -> None:
        # Another seat's queue was ahead:  the ledger already holds the seat's later chat (message 103),
        # and then message 101 itself, when 101 is judged.  The owner has no seat, so nothing "tagged"
        # prefers chat AA:  only the strictly-earlier rule can keep ZZ out.
        ledger = after_chat_a()
        keep(ledger, 10, "[CLAUDE: ZZ.1.S 📁AFC] a later chat", 103)
        c, _ = routed(msg(OWNER, "answer", mid=101), ledger)
        self.assertEqual(c.reply_to_chat, ReplyTarget("CLAUDE", "AA", 100))
        keep(ledger, OWNER, "answer", 101)
        self.assertEqual(routed(msg(OWNER, "answer", mid=101), ledger)[0].reply_to_chat.letter, "AA")
        self.assertEqual(routed(msg(OWNER, "answer", mid=104), ledger)[0].reply_to_chat.letter, "ZZ")

    def test_posts_that_never_wake_anyone_never_wake_by_reply_either(self) -> None:
        ledger = after_chat_a()
        keep(ledger, 10, "[CLAUDE 📁AFC] roll", 90, topic="roll call")
        cases = {
            "a wake reply": dict(content="[CODEX·wake] re=100\nanswer"),
            "a route post": dict(content="[CODEX·route] re=100\npaging"),
            "a route post with an arrow": dict(content="[CODEX·route→CURSOR] re=100\npaging"),
            "the no-wake marker": dict(content="👀 just so you can see"),
            "the marker before an intro": dict(content="👀[CODEX 📁AFC] visibility only"),
            "a stale post": dict(content="late answer", ts=NOW - 8000),
            "a stale post from the owner": dict(sender=OWNER, content="late answer", ts=NOW - 8000),
            "a peer in an OWNER topic": dict(content="answer", topic="OWNER decide the thing"),
            "a presence topic": dict(content="answer", topic="roll call"),
            "@everyone": dict(content="@**everyone** hi", flags=("wildcard_mentioned",)),
            "@everyone typed plain": dict(content="@everyone hi"),
            "@all": dict(content="@**all** hi", flags=("wildcard_mentioned",)),
            "@topic": dict(content="@**topic** hi", flags=("topic_wildcard_mentioned",)),
            "a direct mention of the seat": dict(content="@**Claude** hi", flags=("mentioned",)),
            "a mention of someone else": dict(content="@**Cursor** can you look?"),
            "a mention of the owner": dict(content="@**Jay Wedgeworth** done"),
            "a user group": dict(content="@*ops* done"),
            "an arrow in its header": dict(content="[CODEX: BB.2.S 📁AFC→CURSOR] over to you"),
            "an integration bot": dict(sender=SENTRY, content="deploy failed"),
            "an API post with the owner's key": dict(sender=OWNER, content="hi", client="ZulipPython"),
            "a direct message": dict(content="hi", dm=True),
        }
        # An OWNER topic and a presence topic need the seat's earlier post in them, or the rule would be
        # what stops the wake and not the topic.
        keep(ledger, 10, CHAT_A, 80, topic="OWNER decide the thing")
        for name, kw in cases.items():
            with self.subTest(name):
                options = dict(kw)
                client = options.pop("client", None)
                message = msg(**{"mid": 101, **options})
                if client:
                    message["client"] = client
                c, d = routed(message, ledger)
                self.assertIsNone(c.reply_to_chat, c.labels())
                self.assertIsNone(d.linked_by)
                self.assertNotIn("reply", c.labels())
                self.assertFalse(d.wake and d.linked_by)

    def test_a_silent_mention_or_a_mention_in_code_still_tags_nobody(self) -> None:
        ledger = after_chat_a()
        for content in ("@_**Cursor** fyi, done", "use `@**Cursor**` to ping", "> @**Cursor** said\ndone"):
            with self.subTest(content=content):
                self.assertEqual(routed(msg(CODEX, content), ledger)[0].reply_to_chat.letter, "AA")

    def test_the_owner_in_an_owner_topic_wakes_the_seat_that_posted_there(self) -> None:
        ledger = TopicLedger()
        keep(ledger, 10, CHAT_A, 80, topic="OWNER decide the thing")
        c, d = routed(msg(OWNER, "do it", mid=101, topic="OWNER decide the thing"), ledger)
        self.assertEqual((c.owner_topic, c.reply_to_chat.letter, d.wake), (True, "AA", "owner"))

    def test_a_muted_topic_drops_it_like_any_mention_that_is_not_direct(self) -> None:
        c, d = routed(msg(CODEX, "ok"), after_chat_a(), muted=[KEY])
        self.assertEqual((d.drop_reason, d.wake, d.seat_inbox), ("muted", None, False))

    def test_the_operators_seat_never_gets_a_reply_wake(self) -> None:
        operator = R.SeatIdentity("OPERATOR", 90, "Operator", "operator-bot@zulip.test")
        ledger = TopicLedger()
        ledger.record(KEY, TopicPost(100, 90, "OPERATOR", "", frozenset()))
        c, d = R.route(msg(OWNER, "thanks", mid=101), operator, ctx(ledger), [])
        self.assertIsNone(c.reply_to_chat)
        self.assertIsNone(d.wake)

    def test_a_context_with_no_ledger_routes_exactly_as_before(self) -> None:
        c, d = routed(msg(OWNER, "thanks", mid=101), None)
        self.assertEqual((c.reply_to_chat, d.wake, d.linked_by), (None, None, None))
        self.assertEqual(sorted(d.describe()), ["delivered_to", "drop", "inbox", "leases", "wake", "wake_block"])

    # ---- the leases and the routine seat's router, as for a direct mention ----------------------

    def lease(self, **kw: Any) -> R.LeaseView:
        options: dict[str, Any] = dict(
            platform="claude-code", topics=[KEY], mentions=True, wake_capable=True, posted=[100], prefix=None
        )
        options.update(kw)
        return R.LeaseView("L1", **options)

    def test_a_session_that_follows_the_topic_is_interrupted_like_for_a_direct_mention(self) -> None:
        c, d = R.route(msg(CODEX, "ok"), ME, ctx(after_chat_a()), [self.lease()])
        self.assertEqual(d.lease_items, [("L1", R.INTERRUPT, "message")])
        direct, dd = R.route(msg(CODEX, "@**Claude** ok", flags=("mentioned",)), ME, ctx(None), [self.lease()])
        self.assertEqual(dd.lease_items, d.lease_items)
        _, plain = R.route(msg(CODEX, "ok"), ME, ctx(None), [self.lease()])
        self.assertEqual(plain.lease_items, [("L1", R.PASSIVE, "message")], "control:  no ledger, passive")

    def test_a_presence_topic_stays_passive_for_every_session(self) -> None:
        ledger = TopicLedger()
        keep(ledger, 10, "[CLAUDE 📁AFC] hi", 90, topic="roll call")
        key = topic_key("simple-tools", "roll call")
        c, d = R.route(msg(CODEX, "ok", topic="roll call"), ME, ctx(ledger), [self.lease(topics=[key])])
        self.assertEqual(d.lease_items, [("L1", R.PASSIVE, "message")])
        self.assertIsNone(c.reply_to_chat)

    def test_an_unleased_topic_goes_to_one_wake_capable_session_for_a_seat_without_a_routine(self) -> None:
        elsewhere = self.lease(topics=[])
        c, d = R.route(msg(CODEX, "ok"), ME, ctx(after_chat_a()), [elsewhere])
        self.assertEqual((d.delivered_to, d.wake, d.wake_block), ("L1", None, "delivered_to_lease"))
        self.assertTrue(d.seat_inbox)
        self.assertEqual((d.linked_by, d.reply_target_letter), ("reply", "AA"))

    def test_a_routine_seat_never_hands_it_to_a_session_but_to_its_router(self) -> None:
        for leases in ([self.lease(topics=[])], [self.lease()]):
            with self.subTest(follows_the_topic=bool(leases[0].topics)):
                c, d = R.route(msg(CODEX, "ok"), ME, ctx(after_chat_a(), seat_has_routine=True), leases)
                self.assertTrue(d.seat_inbox)
                self.assertIsNone(d.delivered_to)
                self.assertEqual((d.wake, d.linked_by), ("peer", "reply"))
                self.assertNotIn(R.INTERRUPT, [klass for _, klass, _ in d.lease_items])

    def test_the_stale_rule_and_the_no_wake_marker_decide_exactly_as_the_class_says(self) -> None:
        ledger = after_chat_a()
        _, d = routed(msg(CODEX, "ok", ts=NOW - 8000), ledger)
        self.assertEqual((d.wake, d.seat_inbox), (None, False))
        _, d = routed(msg(CODEX, "👀 ok"), ledger)
        self.assertEqual((d.wake, d.seat_inbox, d.wake_worthy), (None, False, False))


# --------------------------------------------------------------------------------------------
# The wake's own record
# --------------------------------------------------------------------------------------------


class WakeRecordTests(unittest.TestCase):
    def reply_item(self, mid: int, letter: str, *, owner: bool = False) -> dict[str, Any]:
        return {
            "id": mid,
            "channel": "simple-tools",
            "topic": "t",
            "type": "stream",
            "classes": ["eligible", "reply"],
            "reply_target_letter": letter,
            "owner": owner,
            "ts": NOW,
        }

    def pending(self) -> W.Pending:
        return W.Pending("w1", "CLAUDE", {"channel": "simple-tools", "topic": "t"}, owner=False, now=NOW, due=NOW)

    def test_the_wake_row_says_it_was_linked_by_a_reply_and_to_which_chat(self) -> None:
        pending = self.pending()
        pending.add(self.reply_item(101, "AA"), False)
        row = pending.row("queued", NOW)
        self.assertEqual((row["linked_by"], row["reply_target_letter"]), ("reply", "AA"))
        pending.add({"id": 102, "classes": ["eligible", "direct"], "ts": NOW}, False)
        row = pending.row("accepted", NOW)
        self.assertNotIn("linked_by", row, "the newest trigger is an explicit mention:  not linked by a reply")
        self.assertNotIn("linked_by", self.pending().row("queued", NOW))

    def test_the_trigger_the_routine_is_told_about_decides_the_link(self) -> None:
        pending = self.pending()
        pending.add(self.reply_item(101, "AA", owner=True), True)
        pending.add({"id": 102, "classes": ["eligible", "direct"], "ts": NOW}, False)
        self.assertEqual(pending.reply_link(), ("reply", "AA"), "the newest OWNER trigger is the one")
        pending.add(self.reply_item(103, "AB"), False)
        self.assertEqual(pending.reply_link(), ("reply", "AA"))

    def test_a_merge_and_a_reload_keep_the_link(self) -> None:
        newer = self.pending()
        newer.add(self.reply_item(105, "AB"), False)
        older = self.pending()
        older.add({"id": 101, "classes": ["eligible", "direct"], "ts": NOW}, False)
        older.merge(newer)
        self.assertEqual(older.reply_link(), ("reply", "AB"))
        view = dict(older.row("queued", NOW))
        again = W.Pending.from_view(view)
        self.assertEqual(again.reply_link(), ("reply", "AB"))
        self.assertIsNone(W.Pending.from_view(dict(self.pending().row("queued", NOW))).reply_link())

    def test_the_routine_body_carries_the_letter_and_linked_by_only_for_a_reply(self) -> None:
        def body(row: dict[str, Any]) -> dict[str, Any]:
            return A.routine_body(
                seat="CLAUDE", wake_id="w1", trigger_ids=[101], row=row, bot_user_id=10,
                realm="https://z.test", now=1790000000.0,
            )

        base = {"id": 101, "type": "stream", "channel": "simple-tools", "topic": "t", "stream_id": 7,
                "sender_id": CODEX, "content": "ok"}
        plain = body(dict(base, classes=["eligible", "direct"], content="@**Claude** AB can you check"))
        self.assertNotIn("linked_by", plain, "an explicit letter in the text is the router's to read")
        self.assertNotIn("letter", plain)
        reply = body(dict(base, classes=["eligible", "reply"], reply_target_letter="AA"))
        self.assertEqual((reply["letter"], reply["linked_by"]), ("AA", "reply"))
        unlettered = body(dict(base, classes=["eligible", "reply"], reply_target_letter=""))
        self.assertEqual(unlettered["linked_by"], "reply")
        self.assertNotIn("letter", unlettered, "the post it answers had no letter:  the wake carries none")
        for junk in ("A" * 500, "A B", "A\nB", "A/../B"):
            self.assertNotIn("letter", body(dict(base, classes=["eligible", "reply"], reply_target_letter=junk)))
        for row in (plain, reply, unlettered):
            self.assertEqual(row["contract"], A.ROUTINE_CONTRACT)
            self.assertLess(len(A.routine_bytes(row)), A.ROUTINE_MAX_BODY)
        # Everything the contract already had is still there, byte for byte, in the reply's body.
        self.assertTrue(set(plain) <= set(reply))
        self.assertEqual(set(reply) - set(plain), {"letter", "linked_by"})


# --------------------------------------------------------------------------------------------
# The daemon, end to end
# --------------------------------------------------------------------------------------------


class ReplyWakeDaemonTests(DaemonHarness):
    ROUTINE_SEAT = textwrap.dedent("""\
        [seat.CLAUDE]
        bot = "Claude"
        wake = "http"
        routine = {}
        """)

    def settle(self, daemon, seat: str = "CLAUDE") -> None:
        daemon.pump(seat, timeout=0.5)

    def chat_a_tags_codex(self, daemon, content: str = CHAT_A, topic: str = "t") -> int:
        mid = self.fake.add_message("Claude", "simple-tools", topic, content)
        self.settle(daemon)
        return mid

    def routine_daemon(self):
        routine = FakeRoutine()
        routine.start()
        self.addCleanup(routine.stop)
        self.write_config(rewake=True, seats=self.ROUTINE_SEAT)
        daemon = self.started(
            env=self.env(
                CLAUDE_ROUTINE_URL=routine.url + "/hook?token=t", CLAUDE_ROUTINE_KEY="rk-test-key"
            )
        )
        return daemon, routine

    # ---- the core case ---------------------------------------------------------------------------
    def test_chat_a_tags_codex_and_codexs_untagged_reply_wakes_seat_claude_with_letter_aa(self) -> None:
        daemon = self.started()
        asked = self.chat_a_tags_codex(daemon)
        self.assertEqual((self.inbox(), self.ledger()), ([], []), "its own post wakes nobody")
        answered = self.fake.add_message("Codex", "simple-tools", "t", CODEX_REPLY)
        self.pump_until(daemon, lambda: len(self.ledger()) >= 1)
        row = self.inbox()[0]
        self.assertEqual(row["id"], answered)
        self.assertIn("reply", row["classes"])
        self.assertTrue(row["wake_worthy"] and not row["wake_owner"])
        self.assertEqual((row["reply_target_letter"], row["reply_target_id"]), ("AA", asked))
        queued = self.ledger()[0]
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(queued["trigger_ids"], [answered])
        self.assertEqual((queued["linked_by"], queued["reply_target_letter"]), ("reply", "AA"))
        self.wake_cycle(daemon, 25)
        states = [(r["state"], r.get("linked_by"), r.get("reply_target_letter")) for r in self.ledger()]
        self.assertEqual(states[-1], ("done", "reply", "AA"), states)
        self.assertEqual(self.bot_posts(), [], "the daemon answers nobody")
        routes = [e for e in self.log_events("route") if e["id"] == answered]
        self.assertEqual(routes[0]["decision"]["linked_by"], "reply")
        self.assertEqual(routes[0]["decision"]["reply_target_letter"], "AA")
        self.assertIn("reply", routes[0]["classes"])

    def test_the_wake_post_to_the_router_carries_the_letter_and_linked_by(self) -> None:
        daemon, routine = self.routine_daemon()
        self.chat_a_tags_codex(daemon)
        answered = self.fake.add_message("Codex", "simple-tools", "t", CODEX_REPLY)
        self.pump_until(daemon, lambda: len(self.inbox()) >= 1)
        self.wake_cycle(daemon)
        self.assertTrue(routine.wait_for(1, timeout=10), "the routine was never posted to")
        body = routine.received[0].json()
        self.assertEqual(body["message_id"], answered)
        self.assertEqual((body["letter"], body["linked_by"]), ("AA", "reply"))
        self.assertEqual(body["contract"], "simple-tools-wake/1")
        self.assertNotIn("skipped", [r["state"] for r in self.ledger()], "no session took it over")
        done = [r for r in self.ledger() if r["state"] == "done"][-1]
        self.assertEqual((done["linked_by"], done["reply_target_letter"]), ("reply", "AA"))

    def test_an_explicit_letter_in_the_text_sends_no_linked_by(self) -> None:
        daemon, routine = self.routine_daemon()
        self.chat_a_tags_codex(daemon)  # the seat posted here too:  the explicit address still wins
        mid = self.fake.add_message("Codex", "simple-tools", "t", "@**Claude** AB can you check this?")
        self.pump_until(daemon, lambda: len(self.inbox()) >= 1)
        self.wake_cycle(daemon)
        self.assertTrue(routine.wait_for(1, timeout=10))
        body = routine.received[0].json()
        self.assertEqual(body["message_id"], mid)
        self.assertNotIn("linked_by", body)
        self.assertNotIn("letter", body, "the router reads the letter out of the text")
        self.assertNotIn("reply", self.inbox()[0]["classes"])
        done = [r for r in self.ledger() if r["state"] == "done"][-1]
        self.assertNotIn("linked_by", done)

    def test_a_reply_to_a_post_with_no_letter_posts_no_letter(self) -> None:
        daemon, routine = self.routine_daemon()
        self.chat_a_tags_codex(daemon, CHAT_A_UNLETTERED)
        self.fake.add_message("Codex", "simple-tools", "t", "answer")
        self.pump_until(daemon, lambda: len(self.inbox()) >= 1)
        self.wake_cycle(daemon)
        self.assertTrue(routine.wait_for(1, timeout=10))
        body = routine.received[0].json()
        self.assertEqual(body["linked_by"], "reply")
        self.assertNotIn("letter", body)
        self.assertEqual(self.ledger()[0]["reply_target_letter"], "")

    # ---- who wakes it ------------------------------------------------------------------------------
    def test_a_third_bot_and_the_owner_wake_it_too_and_the_owner_wakes_as_the_owner(self) -> None:
        daemon = self.started()
        self.chat_a_tags_codex(daemon)
        self.fake.add_message("Cursor", "simple-tools", "t", "[CURSOR 📁AFC] my two cents")
        self.pump_until(daemon, lambda: len(self.inbox()) >= 1)
        self.fake.add_message("Jay Wedgeworth", "simple-tools", "t", "thanks, ship it")
        self.pump_until(daemon, lambda: len(self.inbox()) >= 2)
        peer, owner = self.inbox()
        self.assertEqual((peer["wake_owner"], owner["wake_owner"]), (False, True))
        self.assertEqual((peer["reply_target_letter"], owner["reply_target_letter"]), ("AA", "AA"))
        self.assertEqual([bool(r["owner"]) for r in self.ledger() if r["state"] == "queued"][-1], True)

    def test_an_untagged_post_in_a_topic_the_seat_never_posted_in_is_plain_chatter(self) -> None:
        daemon = self.started()
        self.chat_a_tags_codex(daemon, topic="other topic")
        self.fake.add_message("Codex", "simple-tools", "t", CODEX_REPLY)
        self.fake.add_message("Jay Wedgeworth", "simple-tools", "t2", "thanks")
        self.settle(daemon)
        daemon.pump("CLAUDE", timeout=0.5)
        self.assertEqual(self.ledger(), [])
        self.assertEqual([r["id"] for r in self.inbox() if "reply" in r["classes"]], [])

    def test_a_tagged_reply_wakes_by_the_tag_alone_and_never_by_reply(self) -> None:
        daemon = self.started()
        self.chat_a_tags_codex(daemon)
        self.fake.add_message("Codex", "simple-tools", "t", "[CODEX: BB.2.S 📁AFC→CLAUDE] @**Claude** your move")
        self.pump_until(daemon, lambda: len(self.inbox()) >= 1)
        row = self.inbox()[0]
        self.assertIn("direct", row["classes"])
        self.assertNotIn("reply", row["classes"])
        self.assertNotIn("linked_by", self.ledger()[0])

    def test_a_wake_reply_a_marker_post_and_a_stale_post_never_wake(self) -> None:
        daemon = self.started()
        self.chat_a_tags_codex(daemon)
        self.fake.add_message("Codex", "simple-tools", "t", "[CODEX·wake] re=100\nan answer")
        self.fake.add_message("Codex", "simple-tools", "t", "👀[CODEX 📁AFC] for the record")
        self.fake.add_message("Codex", "simple-tools", "t", "ancient answer", timestamp=EPOCH - 100000)
        self.fake.add_message("Codex", "simple-tools", "t", "@**everyone** all hands")
        self.settle(daemon)
        daemon.pump("CLAUDE", timeout=0.5)
        self.assertEqual([r["id"] for r in self.inbox() if "reply" in r["classes"]], [])
        self.assertEqual([r for r in self.ledger() if r["state"] == "queued"], [])

    def test_a_presence_topic_never_wakes(self) -> None:
        daemon = self.started()
        # All of #alerts is a presence topic (live.DEFAULT_PRESENCE), and the seat posted there first.
        self.fake.add_message("Claude", "alerts", "disk space", "[CLAUDE 📁AFC] checking")
        self.settle(daemon)
        self.fake.add_message("Codex", "alerts", "disk space", "[CODEX 📁AFC] seen")
        self.fake.add_message("Jay Wedgeworth", "alerts", "disk space", "thanks")
        self.settle(daemon)
        daemon.pump("CLAUDE", timeout=0.5)
        self.assertEqual(self.ledger(), [])
        self.assertEqual([r["id"] for r in self.inbox() if "reply" in r["classes"]], [])
        self.assertEqual(len(daemon.topic_ledger.posts(L.topic_key("alerts", "disk space"))), 3)

    def test_an_owner_topic_wakes_for_the_owners_reply_and_never_for_a_peers(self) -> None:
        daemon = self.started()
        self.chat_a_tags_codex(daemon, topic="OWNER decide the thing")
        self.fake.add_message("Codex", "simple-tools", "OWNER decide the thing", "my view:  yes")
        self.settle(daemon)
        daemon.pump("CLAUDE", timeout=0.5)
        self.assertEqual([r for r in self.ledger() if r["state"] == "queued"], [], "a peer wakes nobody there")
        self.fake.add_message("Jay Wedgeworth", "simple-tools", "OWNER decide the thing", "do it")
        self.pump_until(daemon, lambda: any(r["state"] == "queued" for r in self.ledger()))
        self.assertTrue(self.ledger()[0]["owner"])

    # ---- the ledger is fed exactly once, and strictly in order ------------------------------------
    def test_a_direct_message_is_never_in_the_ledger_and_never_a_reply(self) -> None:
        daemon = self.started()
        self.chat_a_tags_codex(daemon)
        before = daemon.topic_ledger.stats()
        self.fake.add_direct_message("Codex", "private words for Claude", deliver=True)
        self.fake.add_direct_message(
            "Claude", "private words from Claude", recipients=[self.fake.user_named("Codex")], deliver=True
        )
        self.settle(daemon)
        daemon.pump("CLAUDE", timeout=0.5)
        self.assertEqual(daemon.topic_ledger.stats(), before)
        for key in daemon.topic_ledger.keys():
            self.assertTrue(key.startswith("simple-tools\u0000"), key)
        dm = [r for r in self.inbox() if r["type"] == "private"]
        self.assertEqual(len(dm), 1)
        self.assertNotIn("reply", dm[0]["classes"])
        self.assertNotIn("linked_by", self.ledger()[0])

    def test_every_stream_message_is_recorded_once_whichever_seat_or_retry_brings_it(self) -> None:
        codex_key = secrets.token_hex(16)
        self.extra_keys.append(("codex-bot@zulip.test", codex_key))
        self.fake.add_bot("codex-bot@zulip.test", "Codex", codex_key)
        write_rc(self.secrets_dir / "Codex-zuliprc", email="codex-bot@zulip.test", key=codex_key, site=self.fake.url)
        self.write_config(
            seats='[seat.CLAUDE]\nbot = "Claude"\nwake = "claude"\n\n[seat.CODEX]\nbot = "Codex"\nwake = "inbox"\n'
        )
        daemon = self.started(env=self.env(SIMPLE_TOOLS_PARTITION=self.two_seat_partition()))
        self.assertTrue(daemon.connect_seat("CODEX"))
        first = self.fake.add_message("Claude", "simple-tools", "t", "[CLAUDE: AA.1.S 📁AFC] starting on it")
        second = self.fake.add_message("Cursor", "simple-tools", "t", "[CURSOR 📁AFC] seen")
        self.pump_until(daemon, lambda: len(daemon.topic_ledger.posts(KEY)) >= 2)
        daemon.pump("CODEX", timeout=0.5)
        daemon.pump("CLAUDE", timeout=0.5)
        daemon.route_message(daemon.seats["CLAUDE"], self.fake.messages[-1])  # a replay
        daemon.record_topic_post(daemon.seats["CODEX"], self.fake.messages[-1])
        self.assertEqual([p.message_id for p in daemon.topic_ledger.posts(KEY)], [first, second])
        self.assertEqual(daemon.topic_ledger.posts(KEY)[0].letter, "AA")
        # The ledger is shared:  CODEX's seat, which never posted, is not woken, and CLAUDE's chat is.
        self.assertEqual(self.inbox("CODEX"), [])
        self.assertEqual([r["id"] for r in self.inbox() if "reply" in r["classes"]], [second])

    def test_a_ledger_failure_never_costs_the_message_its_routing(self) -> None:
        daemon = self.started()

        def boom(*args: Any, **kw: Any) -> None:
            raise RuntimeError("ledger down")

        daemon.topic_ledger.record = boom
        mid = self.fake.add_message("Codex", "simple-tools", "t", "@**Claude** are you there?")
        self.pump_until(daemon, lambda: len(self.inbox()) >= 1)
        self.assertEqual([r["id"] for r in self.inbox()], [mid])
        self.assertTrue(self.log_events("topic-ledger-error"))
        self.assertEqual(self.log_events("quarantined"), [])

    def test_a_renamed_topic_keeps_the_chat_a_reply_answers(self) -> None:
        daemon = self.started()
        self.chat_a_tags_codex(daemon)
        self.fake.rename_topic("simple-tools", "t", "t renamed")
        self.settle(daemon)
        answered = self.fake.add_message("Codex", "simple-tools", "t renamed", CODEX_REPLY)
        self.pump_until(daemon, lambda: any(r["id"] == answered for r in self.inbox()))
        row = [r for r in self.inbox() if r["id"] == answered][0]
        self.assertEqual(row["reply_target_letter"], "AA")

    # ---- a restart empties the ledger, and the seat's own recent posts put the chats back -----------
    def test_a_restart_seeds_the_ledger_from_the_seats_own_recent_posts_and_only_those(self) -> None:
        asked = self.fake.add_message("Claude", "simple-tools", "t", CHAT_A)
        self.fake.add_message("Codex", "simple-tools", "t", CODEX_REPLY)  # not the seat's own:  not seeded
        self.fake.add_message("Claude", "other", "elsewhere", "[CLAUDE: AB.1.S 📁AFC] a second topic")
        daemon = self.started()
        self.assertEqual(daemon.topic_ledger.stats(), {"topics": 2, "posts": 2})
        self.assertEqual(
            [(p.message_id, p.letter, sorted(p.tagged)) for p in daemon.topic_ledger.posts(KEY)],
            [(asked, "AA", ["CODEX"])],
        )
        self.assertEqual(self.log_events("topic-ledger-seed")[0]["posts"], 2, "only its own two were read")
        self.assertEqual((self.inbox(), self.ledger()), ([], []), "seeding routes nothing and wakes nothing")
        answered = self.fake.add_message("Codex", "simple-tools", "t", "[CODEX: BB.2.S 📁AFC] fine after all")
        self.pump_until(daemon, lambda: any(r["id"] == answered for r in self.inbox()))
        self.assertEqual(self.inbox()[0]["reply_target_letter"], "AA")

    def own_reads(self) -> list[Any]:
        """The GET /messages requests that read the seat's own posts (the backfill's owner read has the
        owner's id as its operand, not the seat's)."""
        own = [{"operator": "sender", "operand": 10}]
        return [
            r for r in self.fake.requests_to("GET", "messages") if json.loads(r.params.get("narrow", "[]")) == own
        ]

    def test_the_seed_is_one_bounded_request_and_runs_once_per_seat(self) -> None:
        self.fake.add_message("Claude", "simple-tools", "t", CHAT_A)
        daemon = self.started()
        reads = self.own_reads()
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0].params["anchor"], "newest")
        self.assertEqual((int(reads[0].params["num_before"]), int(reads[0].params["num_after"])), (200, 0))
        self.fake.expire_queues()
        daemon.pump("CLAUDE", timeout=1.0)
        self.assertTrue(daemon.connect_seat("CLAUDE"))
        self.assertEqual(len(self.own_reads()), 1, "a reconnect does not read it again")

    def test_a_failed_seed_read_is_logged_and_never_stops_the_seat(self) -> None:
        self.fake.add_message("Claude", "simple-tools", "t", CHAT_A)
        self.fake.inject("GET", "messages", status=500, times=1)
        daemon = self.started()
        self.assertTrue(daemon.seats["CLAUDE"].connected)
        self.assertEqual(len(self.log_events("topic-ledger-seed-error")), 1)
        self.assertEqual(daemon.topic_ledger.stats()["posts"], 0, "that restart's gap, and nothing worse")
        self.fake.add_message("Claude", "simple-tools", "t", "[CLAUDE: AA.1.S 📁AFC] posting again")
        self.pump_until(daemon, lambda: daemon.topic_ledger.stats()["posts"] >= 1)

    # ---- the leases, the stale rule and the routine seat's router, as for a direct mention --------
    def test_a_session_that_follows_the_topic_is_interrupted_and_a_routine_seats_session_is_not(self) -> None:
        daemon = self.started()
        asked = self.fake.add_message("Claude", "simple-tools", "t", CHAT_A)
        self.write_lease("L1", topics=[("simple-tools", "t")], posted=[asked])
        self.settle(daemon)
        answered = self.fake.add_message("Codex", "simple-tools", "t", CODEX_REPLY)
        self.pump_until(daemon, lambda: any(i["id"] == answered for i in self.live_items("L1")))
        self.assertEqual(
            [(i["id"], i["class"]) for i in self.live_items("L1") if i["id"] == answered],
            [(answered, "interrupt")],
        )
        self.assertEqual(self.inbox(), [], "the topic's session took it:  no seat-inbox copy and no wake")

    def test_a_routine_seats_session_gets_a_passive_item_and_the_router_gets_the_wake(self) -> None:
        daemon, routine = self.routine_daemon()
        asked = self.fake.add_message("Claude", "simple-tools", "t", CHAT_A)
        self.write_lease("L1", topics=[("simple-tools", "t")], posted=[asked])
        self.settle(daemon)
        answered = self.fake.add_message("Codex", "simple-tools", "t", CODEX_REPLY)
        self.pump_until(daemon, lambda: len(self.inbox()) >= 1)
        self.assertEqual(
            [(i["id"], i["class"]) for i in self.live_items("L1") if i["id"] == answered],
            [(answered, "passive")],
        )
        self.wake_cycle(daemon)
        self.assertTrue(routine.wait_for(1, timeout=10))
        self.assertEqual(routine.received[0].json()["letter"], "AA")

    def test_a_dead_sessions_reply_item_goes_back_to_the_seat_inbox_and_the_wake(self) -> None:
        daemon = self.started()
        row = {
            "kind": "message", "id": 4242, "channel": "simple-tools", "topic": "t", "type": "stream",
            "sender_id": CODEX, "classes": ["eligible", "reply"], "wake_worthy": True,
            "wake_owner": False, "reply_target_letter": "AA", "stale": False, "ts": self.clock.time(),
        }
        daemon._requeue(daemon.seats["CLAUDE"], [row], "returned_from", "L1")
        self.assertEqual([r["id"] for r in self.inbox()], [4242])
        self.assertEqual(self.ledger()[0]["linked_by"], "reply")
        chatter = dict(row, id=4243, classes=["eligible"])
        daemon._requeue(daemon.seats["CLAUDE"], [chatter], "returned_from", "L1")
        self.assertEqual([r["id"] for r in self.inbox()], [4242], "plain chatter still goes nowhere")

    # ---- nothing throttles it ----------------------------------------------------------------------
    def test_it_adds_no_budget_cap_or_loop_guard_state_and_a_burst_wakes_every_time(self) -> None:
        daemon = self.started()
        guard_file = daemon.loopguard.path
        self.chat_a_tags_codex(daemon)
        before = (L.read_json(guard_file), daemon.loopguard.count(KEY))
        for n in range(4):
            self.fake.add_message("Codex", "simple-tools", "t", "[CODEX: BB.2.S 📁AFC] reply number %d" % n)
            self.pump_until(daemon, lambda n=n: sum(1 for r in self.ledger() if r["state"] == "queued") >= n + 1)
            self.wake_cycle(daemon, 95)
        self.assertEqual((L.read_json(guard_file), daemon.loopguard.count(KEY)), before)
        self.assertEqual(daemon.loopguard.count(KEY), 0, "only a `·wake` reply or a `·route` post counts")
        self.assertEqual([r for r in self.ledger() if r["state"] in ("held_back", "dropped")], [])
        self.assertEqual(len(self.dumps()), 4, "four replies, four wakes")
        budget = daemon.status()["seats"]["CLAUDE"]["budget"]
        self.assertFalse([k for k in budget if "reply" in k], "no budget of its own")
        self.assertEqual(W.LoopGuard.LIMIT, 3)

    def test_the_seats_own_existing_cap_still_holds_it_back_like_any_peer_wake(self) -> None:
        self.write_config(budget="per_topic_per_hour = 1")
        daemon = self.started()
        self.chat_a_tags_codex(daemon)
        for n in range(2):
            self.fake.add_message("Codex", "simple-tools", "t", "[CODEX: BB.2.S 📁AFC] reply %d" % n)
            self.pump_until(daemon, lambda n=n: sum(1 for r in self.ledger() if r["state"] == "queued") >= n + 1)
            self.wake_cycle(daemon, 95)
        reasons = [r.get("reason") for r in self.ledger() if r["state"] == "held_back"]
        self.assertEqual(reasons, ["per_topic_per_hour"])
        self.assertEqual(len(self.dumps()), 1)

    def test_the_existing_loop_guard_is_unchanged_and_a_reply_post_counts_nothing_on_it(self) -> None:
        daemon = self.started()
        self.chat_a_tags_codex(daemon)
        for n in range(3):
            self.fake.add_message("Codex", "simple-tools", "t", "[CODEX·wake] re=%d\nreply" % n)
        self.pump_until(daemon, lambda: daemon.loopguard.count(KEY) == 3)
        answered = self.fake.add_message("Codex", "simple-tools", "t", CODEX_REPLY)
        self.pump_until(daemon, lambda: any(r["id"] == answered for r in self.inbox()))
        self.assertEqual(daemon.loopguard.count(KEY), 3, "the reply counted nothing")
        self.assertTrue(daemon.loopguard.blocked(KEY), "and the guard that was there still holds the topic")
        drops = [r for r in self.ledger() if r["state"] == "dropped"]
        self.assertEqual([(r["reason"], r["trigger_ids"]) for r in drops], [("loop_guard", [answered])])
        self.assertEqual(drops[0]["guard_count"], 3)
        self.fake.add_message("Jay Wedgeworth", "simple-tools", "t", "carry on")
        self.pump_until(daemon, lambda: not daemon.loopguard.blocked(KEY))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
