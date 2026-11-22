"""The Operator's Zulip -> Linear living comments (operator_linear.py): the relay, hosted.

These tests pin the contract the owner ruled on (2026-10-21, 2026-10-23 and 2026-11-03):

  - just one comment per Zulip topic:  keyed by (stream, topic), one living comment per
    Linear issue mentioned in the topic, created on the first mention in the topic and
    edited in place as the topic grows;  a second issue in the same topic gets its own
    single comment, and the same issue in another topic gets a separate one;
  - the comment covers the WHOLE topic:  a first mention in the third message pulls the
    first two in, and every edit reflects the whole current thread;
  - a restart recovers the (topic, issue) -> comment mapping from the ledger, so nothing
    is ever created twice;
  - a restart catches up (AFC-821):  the messages a topic got while the listener was down
    are read after the newest one its comment had recorded, and join the comment before
    the live one;  so does a seat queue that registers again, and each history read has
    one deadline for all its pages;
  - no issue keys in a topic means no comment and no ledger entry, and text that merely
    looks key-shaped (UTF-8, COVID-19, a lowercase key) names nothing;
  - nothing is written until the public-stream set has loaded, and a history read that
    fails never blocks a create.

Nothing here touches the network:  the Linear client and the Zulip client are faked.
"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from simple_tools import linear_relay as lr
from simple_tools import operator_linear as opl

REALM = "https://z.example"
OWN = "operator@x"


class FakeLinear:
    """The Linear client the worker talks to (the shape of test_linear_relay's fake)."""

    def __init__(self, known=("AFC-1", "OPS-9")) -> None:
        self.known = set(known)
        self.created: list[tuple[str, str]] = []
        self.updated: list[tuple[str, str]] = []
        self.by_marker: dict[str, str] = {}
        self.dry_run = False
        self.create_calls = 0

    def issue_id(self, identifier: str) -> str:
        if identifier not in self.known:
            raise lr.LinearNotFound(identifier)
        return "uuid-" + identifier

    def create_comment(self, issue_uuid: str, body: str) -> str:
        self.create_calls += 1
        self.created.append((issue_uuid, body))
        cid = "c%d" % len(self.created)
        self.by_marker[body.split("\n", 1)[0]] = cid
        return cid

    def update_comment(self, comment_id: str, body: str) -> None:
        self.updated.append((comment_id, body))

    def find_comment(self, issue_uuid: str, marker: str) -> str | None:
        return self.by_marker.get(marker)


def api_message(mid, content, sender="a@x", name="Al", ts=None):
    """One message as the Zulip messages API returns it (the history reader's input)."""
    return {
        "id": mid,
        "sender_email": sender,
        "sender_full_name": name,
        "content": content,
        "timestamp": (900 + mid) if ts is None else ts,
    }


def event_message(mid, topic, content, sender="a@x", name="Al", stream_id=5):
    """One routed stream message as the daemon hands it to route_message."""
    return {
        "id": mid,
        "type": "stream",
        "stream_id": stream_id,
        "display_recipient": "builds",
        "subject": topic,
        "content": content,
        "sender_email": sender,
        "sender_full_name": name,
        "timestamp": 900 + mid,
    }


class History:
    """A scripted topic-history reader that counts its calls."""

    def __init__(self, messages=(), error=None) -> None:
        self.messages = list(messages)
        self.error = error
        self.calls = 0

    def __call__(self, stream_id, topic):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.messages)


class WorkerHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.t = [1000.0]
        self.lin = FakeLinear()
        self.logs: list[str] = []
        self.dir = Path(tempfile.mkdtemp())

    def worker(self, history=None, ledger=None) -> lr.RelayWorker:
        led = ledger or lr.Ledger(self.dir / "state.json")
        if ledger is None:
            led.open()
        return lr.RelayWorker(
            REALM,
            OWN,
            self.lin,
            led,
            lr.build_regex(lr.DEFAULT_TEAMS),
            clock=lambda: self.t[0],
            monotonic=lambda: self.t[0],
            log=self.logs.append,
            public_streams={5: "builds", 6: "deploys"},
            history=history,
            byline="the Operator",
        )

    def feed(self, worker, message) -> None:
        worker.handle_event({"type": "message", "message": message})

    def test_mention_in_the_third_message_covers_the_whole_topic(self) -> None:
        history = History([api_message(1, "setting up"), api_message(2, "still no issue")])
        w = self.worker(history)
        self.feed(w, event_message(1, "deploy thread", "setting up"))
        self.feed(w, event_message(2, "deploy thread", "still no issue"))
        self.assertEqual(self.lin.created, [])  # nothing named an issue yet
        self.feed(w, event_message(3, "deploy thread", "this is AFC-1, by the way"))
        self.assertEqual(len(self.lin.created), 1)
        uuid, body = self.lin.created[0]
        self.assertEqual(uuid, "uuid-AFC-1")
        entry = next(iter(w.ledger.entries.values()))
        self.assertEqual(entry["count"], 3)
        for mid in (1, 2, 3):
            self.assertIn("near/%d" % mid, body)
        self.assertIn("setting up", body)
        self.assertIn("still no issue", body)
        self.assertIn("by the Operator", body)  # the hosted byline, not the relay's

    def test_one_comment_per_topic_and_issue(self) -> None:
        w = self.worker(History())
        self.feed(w, event_message(1, "topic a", "AFC-1 first"))
        self.feed(w, event_message(2, "topic a", "AFC-1 again, plus OPS-9"))
        self.feed(w, event_message(3, "topic a", "AFC-1 a third time"))
        self.feed(w, event_message(4, "topic b", "AFC-1 in another topic"))
        self.assertEqual(self.lin.create_calls, 3)
        per_issue = {}
        for uuid, _body in self.lin.created:
            per_issue[uuid] = per_issue.get(uuid, 0) + 1
        # One comment for AFC-1 in topic a, one for OPS-9 in topic a, one for AFC-1 in b.
        self.assertEqual(per_issue, {"uuid-AFC-1": 2, "uuid-OPS-9": 1})
        markers = [body.split("\n", 1)[0] for _uuid, body in self.lin.created]
        self.assertEqual(len(set(markers)), 3)  # three distinct living comments

    def test_edits_are_in_place_and_reflect_the_whole_thread(self) -> None:
        w = self.worker(History([api_message(1, "before the issue")]))
        self.feed(w, event_message(2, "thread", "AFC-1 appears"))
        self.assertEqual(len(self.lin.created), 1)
        cid = self.lin.created and "c1"
        self.t[0] += 60
        self.feed(w, event_message(3, "thread", "more context"))
        self.assertEqual(self.lin.updated, [])  # inside the batching interval
        self.t[0] += 600
        w.tick()
        self.assertEqual(len(self.lin.updated), 1)
        updated_cid, body = self.lin.updated[0]
        self.assertEqual(updated_cid, cid)
        self.assertEqual(self.lin.create_calls, 1)  # still the one comment
        for mid in (1, 2, 3):
            self.assertIn("near/%d" % mid, body)

    def test_restart_recovers_the_mapping_and_never_creates_twice(self) -> None:
        path = self.dir / "state.json"
        led1 = lr.Ledger(path)
        led1.open()
        w1 = self.worker(History(), ledger=led1)
        self.feed(w1, event_message(1, "thread", "AFC-1"))
        self.assertEqual(self.lin.create_calls, 1)
        led1.save()

        # A restart reads the same ledger back (copied to a fresh path here, as the
        # relay's own suite does, because the first ledger still holds its flock).
        copy = self.dir / "state-restarted.json"
        copy.write_text(path.read_text("utf-8"), "utf-8")
        led2 = lr.Ledger(copy)
        led2.open()
        w2 = self.worker(History(), ledger=led2)
        self.feed(w2, event_message(2, "thread", "still AFC-1"))
        self.t[0] += 700
        w2.tick()
        self.assertEqual(self.lin.create_calls, 1)  # adopted, not stacked
        self.assertEqual(len(self.lin.updated), 1)
        self.assertEqual(self.lin.updated[0][0], "c1")  # the original comment id

    def test_no_issue_keys_no_comment_no_entry(self) -> None:
        w = self.worker(History())
        self.feed(w, event_message(1, "plain thread", "nothing to mirror"))
        self.feed(w, event_message(2, "plain thread", "still nothing"))
        w.tick()
        self.assertEqual(self.lin.created, [])
        self.assertEqual(w.ledger.entries, {})

    def test_a_tracked_topic_takes_every_later_message_key_or_not(self) -> None:
        w = self.worker(History())
        self.feed(w, event_message(1, "tracked", "this is AFC-1"))
        self.feed(w, event_message(2, "tracked", "no key in this one"))
        self.feed(w, event_message(3, "untracked", "no key here either"))
        entry = next(iter(w.ledger.entries.values()))
        self.assertEqual(entry["count"], 2)  # both messages of the tracked topic
        self.assertEqual(len(w.ledger.entries), 1)  # the untracked topic got no entry
        self.assertEqual(self.lin.create_calls, 1)

    def test_key_shaped_text_that_names_no_team_is_ignored(self) -> None:
        w = self.worker(History())
        for mid, text in enumerate(
            (
                "the file is UTF-8 encoded",
                "COVID-19 cases are up",
                "an A320-200 just landed",
                "HELLO-123 is not an issue",
                "lowercase afc-12 names nothing",
                "see XY-42 for details",
            ),
            start=1,
        ):
            self.feed(w, event_message(mid, "noise", text))
        self.assertEqual(self.lin.created, [])
        self.assertEqual(w.ledger.entries, {})

    def test_backfill_skips_the_writers_own_posts(self) -> None:
        history = History(
            [
                api_message(1, "a human starts"),
                api_message(2, "the Operator nudges", sender=OWN, name="Operator"),
                api_message(3, "a human replies"),
            ]
        )
        w = self.worker(history)
        self.feed(w, event_message(4, "thread", "filed as AFC-1"))
        self.assertEqual(len(self.lin.created), 1)
        entry = next(iter(w.ledger.entries.values()))
        self.assertEqual(entry["count"], 3)  # messages 1, 3 and the mentioning 4
        self.assertNotIn("near/2", self.lin.created[0][1])

    def test_backfill_orders_links_oldest_first(self) -> None:
        history = History(
            [api_message(3, "third"), api_message(1, "first"), api_message(2, "second")]
        )
        w = self.worker(history)
        self.feed(w, event_message(4, "thread", "AFC-1"))
        body = self.lin.created[0][1]
        positions = [body.index("near/%d" % mid) for mid in (1, 2, 3, 4)]
        self.assertEqual(positions, sorted(positions))

    def test_one_history_fetch_serves_every_issue_one_message_names(self) -> None:
        history = History([api_message(1, "earlier")])
        w = self.worker(history)
        self.feed(w, event_message(2, "thread", "AFC-1 and OPS-9"))
        self.assertEqual(self.lin.create_calls, 2)
        self.assertEqual(history.calls, 1)

    def test_a_failed_history_read_never_blocks_the_create(self) -> None:
        w = self.worker(History(error=RuntimeError("zulip is down")))
        self.feed(w, event_message(1, "thread", "AFC-1"))
        self.assertEqual(len(self.lin.created), 1)  # created from the mention on
        self.assertTrue(any("could not be read" in line for line in self.logs))

    def test_topic_title_mention_backfills_from_the_first_routed_message(self) -> None:
        history = History([api_message(1, "one"), api_message(2, "two")])
        w = self.worker(history)
        self.feed(w, event_message(3, "AFC-1 relay work", "no key in this body"))
        self.assertEqual(len(self.lin.created), 1)
        entry = next(iter(w.ledger.entries.values()))
        self.assertEqual(entry["count"], 3)


class FakeZulip:
    """Answers the streams call and pages a scripted topic history as Zulip does:  an integer
    anchor is a place in the topic, not a message the page must find, so an id no message in
    the topic carries still pages on from where it would be, and "oldest" is the place before
    the first message.  The messages call raises messages_error while it is set.  Each
    (after_id, item) in junk puts an item that is not a well-formed message into the pages,
    just after message after_id, where it counts toward num_after as a message would."""

    def __init__(self, topic_messages=(), streams_error=None) -> None:
        self.topic_messages = sorted(topic_messages, key=lambda m: m["id"])
        self.streams_error = streams_error
        self.messages_error: Exception | None = None
        self.junk: list[tuple[int, object]] = []
        self.message_calls: list[dict] = []
        self.message_options: list[dict] = []  # the keyword options of each messages call
        # Zulip places "oldest" before every message id.  True places it at the topic's first
        # message instead, the reading of the API Kody took on PR 2298, so a test can show the
        # read does not depend on which one a server takes.
        self.oldest_is_first = False

    def get(self, path, params=None, **options):
        params = params or {}
        if path == "streams":
            if self.streams_error is not None:
                raise self.streams_error
            return {
                "streams": [
                    {"stream_id": 5, "name": "builds", "invite_only": False},
                    {"stream_id": 9, "name": "secret", "invite_only": True},
                ]
            }
        if path == "messages":
            self.message_calls.append(dict(params))
            self.message_options.append(dict(options))
            if self.messages_error is not None:
                raise self.messages_error
            anchor = params.get("anchor")
            if anchor == "oldest" and self.oldest_is_first and self.topic_messages:
                start = self.topic_messages[0]["id"]
            elif anchor == "oldest":
                start = 0  # the server reads "oldest" as 0, before every message id
            elif isinstance(anchor, int) and not isinstance(anchor, bool):
                start = anchor
            else:
                raise AssertionError("unexpected anchor %r" % (anchor,))
            inclusive = params.get("include_anchor") is True
            # Each item at its place in the topic:  a message at its id, junk just after one.
            items = [(m["id"], m) for m in self.topic_messages]
            items += [(after_id + 0.5, item) for after_id, item in self.junk]
            items.sort(key=lambda pair: pair[0])
            after = [
                item
                for place, item in items
                if place > start or (inclusive and place == start)
            ]
            count = int(params.get("num_after") or 0)
            page = [dict(m) if isinstance(m, dict) else m for m in after[:count]]
            return {"messages": page}
        raise AssertionError("unexpected path %s" % path)


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.t = [1000.0]
        self.lin = FakeLinear()
        self.logs: list[str] = []
        self.dir = Path(tempfile.mkdtemp())

    def engine(self, zulip, path=None) -> opl.OperatorLinearComments:
        led = lr.Ledger(path or (self.dir / "operator" / "linear-comments.json"))
        return opl.OperatorLinearComments(
            realm=REALM,
            own_email=OWN,
            linear=self.lin,
            ledger=led,
            log=self.logs.append,
            clock=lambda: self.t[0],
            monotonic=lambda: self.t[0],
        )

    def test_history_pages_forward_from_the_topics_start(self) -> None:
        zulip = FakeZulip([api_message(mid, "m%d" % mid) for mid in range(1, 6)])
        with mock.patch.object(opl, "PAGE", 2):
            got = opl.fetch_topic_history(zulip, 5, "thread")
        self.assertEqual([m["id"] for m in got], [1, 2, 3, 4, 5])
        self.assertGreaterEqual(len(zulip.message_calls), 3)  # it really paged
        narrow = zulip.message_calls[0]["narrow"]
        self.assertEqual(narrow[0], {"operator": "channel", "operand": 5})
        self.assertEqual(narrow[1], {"operator": "topic", "operand": "thread"})

    def test_history_after_an_id_starts_after_that_message(self) -> None:
        zulip = FakeZulip([api_message(mid, "m%d" % mid) for mid in range(1, 8)])
        with mock.patch.object(opl, "PAGE", 2):
            got = opl.fetch_topic_history(zulip, 5, "thread", after_id=3)
        self.assertEqual([m["id"] for m in got], [4, 5, 6, 7])
        first = zulip.message_calls[0]
        self.assertEqual(first["anchor"], 3)  # the first page is anchored at that message
        self.assertIs(first["include_anchor"], False)  # and leaves it out
        self.assertEqual(first["num_before"], 0)
        self.assertEqual(
            first["narrow"],
            [{"operator": "channel", "operand": 5}, {"operator": "topic", "operand": "thread"}],
        )
        self.assertGreaterEqual(len(zulip.message_calls), 2)  # and it paged on from there
        # Without an id the read still starts at the topic's start.
        zulip.message_calls.clear()
        self.assertEqual(opl.fetch_topic_history(zulip, 5, "thread")[0]["id"], 1)
        self.assertEqual(zulip.message_calls[0]["anchor"], "oldest")

    def test_history_after_an_id_below_one_reads_from_the_topics_oldest_message(self) -> None:
        # The worker's catch-up of a topic whose first read failed starts after 0 (Kody, PR
        # 2298).  No message has that id, so the read asks for the topic's start by the API's
        # own name.  It includes the anchor there (Kody, PR 2298, again), so the first message
        # is read whether the server places "oldest" before it, as Zulip does, or at it.
        zulip = FakeZulip([api_message(mid, "m%d" % mid) for mid in range(1, 6)])
        for oldest_is_first in (False, True):
            zulip.oldest_is_first = oldest_is_first
            for after in (None, 0, -4):
                with self.subTest(oldest_is_first=oldest_is_first, after=after):
                    zulip.message_calls.clear()
                    with mock.patch.object(opl, "PAGE", 2):
                        got = opl.fetch_topic_history(zulip, 5, "thread", after_id=after)
                    self.assertEqual([m["id"] for m in got], [1, 2, 3, 4, 5])
                    calls = zulip.message_calls
                    self.assertEqual(calls[0]["anchor"], "oldest")
                    self.assertIs(calls[0]["include_anchor"], True)
                    # The later pages are anchored at a message, and leave it out.
                    self.assertGreater(len(calls), 1)
                    self.assertTrue(all(call["include_anchor"] is False for call in calls[1:]))

    def test_history_after_an_id_no_longer_in_the_topic_reads_on_from_its_place(self) -> None:
        # Message 3 was moved out of the topic (or deleted) after the entry recorded it.
        zulip = FakeZulip([api_message(mid, "m%d" % mid) for mid in (1, 2, 4, 5)])
        got = opl.fetch_topic_history(zulip, 5, "thread", after_id=3)
        self.assertEqual([m["id"] for m in got], [4, 5])
        self.assertEqual(zulip.message_calls[0]["anchor"], 3)

    def test_engine_fills_in_an_entry_whose_first_read_failed_from_the_topics_start(
        self,
    ) -> None:
        zulip = FakeZulip(
            [api_message(1, "m1"), api_message(2, "m2"), api_message(3, "this is AFC-1")]
        )
        eng = self.engine(zulip)
        zulip.messages_error = OSError("zulip is down")
        eng.feed_message(zulip, event_message(3, "thread", "this is AFC-1"))
        self.assertEqual(len(self.lin.created), 1)  # a create is never held for history
        entry = next(iter(eng.worker.ledger.entries.values()))
        self.assertEqual(entry["seen"], [3])
        zulip.messages_error = None
        zulip.topic_messages.append(api_message(4, "m4"))
        zulip.message_calls.clear()
        self.t[0] += 3600
        eng.feed_message(zulip, event_message(4, "thread", "m4"))
        # The catch-up read the topic from its first message, not from a message 0.
        self.assertEqual([call["anchor"] for call in zulip.message_calls], ["oldest"])
        self.assertEqual(entry["seen"], [1, 2, 3, 4])

    # -- an item a page carries that is not a message (the review check, PR 2298) -----------
    def test_history_leaves_out_an_item_it_cannot_place_and_pages_on(self) -> None:
        zulip = FakeZulip([api_message(mid, "m%d" % mid) for mid in range(1, 6)])
        zulip.junk = [
            (1, "not a message"),
            (2, {"id": "3", "content": "an id that is not an integer"}),
            (3, {"id": True, "content": "a bool is not an id"}),
            (4, None),
        ]
        with mock.patch.object(opl, "PAGE", 2):
            got = opl.fetch_topic_history(zulip, 5, "thread")
        self.assertEqual([m["id"] for m in got], [1, 2, 3, 4, 5])
        self.assertGreaterEqual(len(zulip.message_calls), 5)  # it paged on past each one
        # A message with an integer id is passed on as it came, for the worker to check.
        bad = dict(api_message(6, "x"), content=None)
        zulip.topic_messages.append(bad)
        self.assertEqual(opl.fetch_topic_history(zulip, 5, "thread")[-1], bad)

    def test_a_read_stopped_at_its_cap_answers_the_cap_with_such_items_left_out(self) -> None:
        # The worker tells a read cut short at its cap by its length:  were an item it cannot
        # place to count, the read would end one short and look like the topic's end.
        zulip = FakeZulip([api_message(mid, "m%d" % mid) for mid in range(1, 9)])
        zulip.junk = [(1, {"id": "2b", "content": "an id that is not an integer"})]
        got = opl.fetch_topic_history(zulip, 5, "thread", max_messages=3)
        self.assertEqual([m["id"] for m in got], [1, 2, 3])
        self.assertEqual([call["num_after"] for call in zulip.message_calls], [3, 1])

    def test_history_fails_on_a_full_page_with_no_message_to_page_on_from(self) -> None:
        zulip = FakeZulip([api_message(mid, "m%d" % mid) for mid in (1, 2)])
        zulip.junk = [(0, "not a message"), (0, {"id": None})]
        with mock.patch.object(opl, "PAGE", 2):
            with self.assertRaises(ValueError):
                opl.fetch_topic_history(zulip, 5, "thread")
        # A short page of such items is the topic's end:  the read answers what came before.
        zulip = FakeZulip([api_message(1, "m1")])
        zulip.junk = [(1, "not a message")]
        with mock.patch.object(opl, "PAGE", 2):
            got = opl.fetch_topic_history(zulip, 5, "thread")
        self.assertEqual([m["id"] for m in got], [1])
        # And an answer that is not a list of messages still fails the read.
        for answer in ({"messages": None}, {"messages": {"id": 1}}, None, [api_message(1, "m")]):
            with self.subTest(answer=answer):
                client = mock.Mock()
                client.get.return_value = answer
                with self.assertRaises(ValueError):
                    opl.fetch_topic_history(client, 5, "thread")

    def test_engine_catch_up_reads_past_an_item_that_is_not_a_message(self) -> None:
        # One such item used to fail the whole read, at every later message, from the same
        # point:  message 3 was never recorded.
        zulip = FakeZulip([api_message(1, "this is AFC-1")])
        eng = self.engine(zulip)
        eng.feed_message(zulip, event_message(1, "thread", "this is AFC-1"))
        zulip.topic_messages += [
            dict(api_message(2, "x"), content=None),
            api_message(3, "missed"),
            api_message(4, "live"),
        ]
        zulip.junk = [(1, "not a message"), (2, {"id": "x"})]
        eng.queue_registered()
        zulip.message_calls.clear()
        eng.feed_message(zulip, event_message(4, "thread", "live"))
        entry = next(iter(eng.worker.ledger.entries.values()))
        self.assertEqual(entry["seen"], [1, 3, 4])
        self.assertEqual([call["anchor"] for call in zulip.message_calls], [1])
        self.assertFalse(any("could not be read" in line for line in self.logs))
        self.assertIn(
            "a topic history read answered 1 malformed message(s); they are left out of the "
            "comment",
            self.logs,
        )
        # Settled:  the next message reads nothing.
        zulip.topic_messages.append(api_message(5, "and on"))
        eng.feed_message(zulip, event_message(5, "thread", "and on"))
        self.assertEqual(len(zulip.message_calls), 1)
        self.assertEqual(entry["seen"], [1, 3, 4, 5])

    def test_engine_restores_an_archived_comment_past_an_item_that_is_not_a_message(
        self,
    ) -> None:
        # One such item used to fail the restore's read every time, so the archived comment
        # was never restored and every later message of its topic was dropped.
        zulip = FakeZulip([api_message(1, "this is AFC-1")])
        eng = self.engine(zulip)
        eng.feed_message(zulip, event_message(1, "thread", "this is AFC-1"))
        led = eng.worker.ledger
        with mock.patch.object(lr, "MAX_ENTRIES", 0):
            led.save()  # pruned exactly as the entry bound prunes it
        self.assertEqual((len(led.entries), len(led.archive)), (0, 1))
        zulip.topic_messages += [api_message(2, "m2"), api_message(3, "m3")]
        zulip.junk = [(1, "not a message")]
        self.t[0] += 86400
        eng.feed_message(zulip, event_message(3, "thread", "m3"))
        self.assertEqual((len(led.entries), len(led.archive)), (1, 0))
        entry = next(iter(led.entries.values()))
        self.assertEqual(entry["seen"], [1, 2, 3])
        self.assertEqual(self.lin.create_calls, 1)  # the standing comment, not a second one

    def test_engine_catches_up_after_a_restart(self) -> None:
        zulip = FakeZulip([api_message(1, "this is AFC-1")])
        path = self.dir / "operator" / "linear-comments.json"
        eng = self.engine(zulip, path)
        self.assertEqual(eng.worker.history_since, eng._history_since)  # the reader is wired
        eng.feed_message(zulip, event_message(1, "thread", "this is AFC-1"))
        self.assertEqual(len(self.lin.created), 1)

        # The listener is down.  Two plain messages land in the topic, and the daemon's own
        # backfill replays neither;  the first message after the restart is a new one.
        zulip.topic_messages += [
            api_message(2, "missed one"),
            api_message(3, "missed two"),
            api_message(4, "live again"),
        ]
        copy = self.dir / "operator" / "linear-comments-restarted.json"
        copy.write_text(path.read_text("utf-8"), "utf-8")
        restarted = self.engine(zulip, copy)
        zulip.message_calls.clear()
        self.t[0] += 700
        restarted.feed_message(zulip, event_message(4, "thread", "live again"))
        self.assertEqual(self.lin.create_calls, 1)  # still the one comment
        self.assertEqual(len(self.lin.updated), 1)
        body = self.lin.updated[0][1]
        for mid in (1, 2, 3, 4):
            self.assertIn("near/%d" % mid, body)
        self.assertIn("missed one", body)
        # One read, from the newest message the comment had recorded before the restart.
        self.assertEqual([call["anchor"] for call in zulip.message_calls], [1])

    def test_a_history_read_with_no_client_fails_instead_of_answering_empty(self) -> None:
        # An empty answer would tell the worker the topic got nothing:  the catch-up would mark
        # it caught up, and a restore would rewrite its standing comment down to one message.
        eng = self.engine(FakeZulip())
        with self.assertRaises(RuntimeError):
            eng._history_since(5, "thread", 1)
        with self.assertRaises(RuntimeError):
            eng._history(5, "thread")

    def _assert_the_read_is_bounded(self, zulip, eng, read) -> None:
        """Both readers bound a read the same way, and both bounds are asserted here.

        A caller's deadline that is shorter than the Operator's own budget is the bound
        the read shows, and a caller's deadline that is later is clamped down to the
        budget.  Both are checked against a bound that differs from the caller's own
        value, so neither check passes on its own (Kody, PR 2340)."""
        # The caller's deadline is shorter than the Operator's own budget, so the read
        # must show the caller's bound and not the budget (Kody, PR 2340).
        before = time.monotonic()
        caller = before + opl.HISTORY_READ_BUDGET / 4
        read(caller)
        deadlines = [options["deadline"] for options in zulip.message_options]
        self.assertTrue(deadlines)
        for deadline in deadlines:
            self.assertGreater(deadline, before)
            self.assertLessEqual(deadline, caller)
            # The budget is later than the caller's bound, so a read that showed the
            # budget instead of the caller's deadline fails here (Kody, PR 2340).
            self.assertLess(deadline, before + opl.HISTORY_READ_BUDGET)
        # A caller's deadline that is later than the Operator's own budget is clamped
        # down to the budget, so the read shows the budget and not the caller's value
        # (Kody, PR 2340).
        zulip.message_options.clear()
        before = time.monotonic()
        caller = before + opl.HISTORY_READ_BUDGET * 10
        read(caller)
        after = time.monotonic()
        deadlines = [options["deadline"] for options in zulip.message_options]
        self.assertTrue(deadlines)
        for deadline in deadlines:
            self.assertGreater(deadline, before)
            self.assertLessEqual(deadline, after + opl.HISTORY_READ_BUDGET)
            # The budget, not the caller's far-future value (Kody, PR 2340).
            self.assertLess(deadline, caller)

    def test_a_caller_deadline_bounds_the_whole_topic_read_too(self) -> None:
        # The whole-topic reader takes the deadline the caller's read shares, and the
        # Operator's own budget bounds it as well (Kody, PR 2340).
        zulip = FakeZulip([api_message(mid, "m%d" % mid) for mid in range(1, 4)])
        eng = self.engine(zulip)
        eng.reload_streams(zulip)  # the client the readers read with
        self._assert_the_read_is_bounded(
            zulip, eng, lambda deadline: eng._history(5, "thread", deadline=deadline)
        )

    def test_a_caller_deadline_never_gets_more_time_than_the_operator_budget(self) -> None:
        # A walk that shares its own deadline does not get more time out of a read than
        # a read on its own gets (Kody, PR 2340).
        zulip = FakeZulip([api_message(mid, "m%d" % mid) for mid in range(1, 4)])
        eng = self.engine(zulip)
        eng.reload_streams(zulip)  # the client the readers read with
        self._assert_the_read_is_bounded(
            zulip, eng, lambda deadline: eng._history_since(5, "thread", 1, deadline=deadline)
        )

    def test_each_history_read_has_one_deadline_for_all_its_pages(self) -> None:
        zulip = FakeZulip([api_message(mid, "m%d" % mid) for mid in range(1, 6)])
        eng = self.engine(zulip)
        eng.reload_streams(zulip)  # the client the readers read with
        before = time.monotonic()
        with mock.patch.object(opl, "PAGE", 2):
            eng._history(5, "thread")
            whole = len(zulip.message_options)
            eng._history_since(5, "thread", 2)
        after = time.monotonic()
        self.assertGreater(whole, 1)  # it paged
        self.assertGreater(len(zulip.message_options), whole)
        deadlines = [options["deadline"] for options in zulip.message_options]
        for deadline in deadlines:
            self.assertGreater(deadline, before)
            self.assertLessEqual(deadline, after + opl.HISTORY_READ_BUDGET)
        # One deadline per read, shared by its pages, so a slow Zulip cannot hold the routing
        # thread for a page timeout and its retries again and again.
        self.assertEqual(len(set(deadlines[:whole])), 1)
        self.assertEqual(len(set(deadlines[whole:])), 1)
        # Called without one, the read passes none on (the shape the paging tests use).
        zulip.message_options.clear()
        opl.fetch_topic_history(zulip, 5, "thread")
        self.assertEqual(zulip.message_options, [{}])

    def test_a_queue_that_registers_again_reopens_the_catch_up(self) -> None:
        zulip = FakeZulip([api_message(1, "this is AFC-1")])
        eng = self.engine(zulip)
        eng.feed_message(zulip, event_message(1, "thread", "this is AFC-1"))
        # A seat's queue expires and registers again; 2 lands in between, and the daemon's own
        # backfill does not replay it.
        zulip.topic_messages += [api_message(2, "missed"), api_message(3, "live")]
        eng.queue_registered()
        zulip.message_calls.clear()
        eng.feed_message(zulip, event_message(3, "thread", "live"))
        self.assertEqual([call["anchor"] for call in zulip.message_calls], [1])
        entry = next(iter(eng.worker.ledger.entries.values()))
        self.assertEqual(entry["seen"], [1, 2, 3])

    def test_engine_carries_the_live_entry_and_the_archived_record_over_renames(self) -> None:
        # The rename path the daemon feeds (feed_event) re-keys a live entry and an archived
        # record alike, so the topic's next message under the newest name edits the one
        # comment (Kody, PR 2298).
        def rename(old: str, new: str) -> dict:
            return {
                "type": "update_message",
                "stream_id": 5,
                "orig_subject": old,
                "subject": new,
                "propagate_mode": "change_all",
                "message_ids": [1],
            }

        zulip = FakeZulip([api_message(1, "this is AFC-1")])
        eng = self.engine(zulip)
        led = eng.worker.ledger
        eng.feed_message(zulip, event_message(1, "thread", "this is AFC-1"))
        eng.feed_event(zulip, rename("thread", "thread two"))
        self.assertEqual(list(led.entries), [lr.ledger_key("AFC-1", 5, "thread two")])
        with mock.patch.object(lr, "MAX_ENTRIES", 0):
            led.save()  # pruned exactly as the entry bound prunes it
        eng.feed_event(zulip, rename("thread two", "thread three"))
        self.assertEqual(list(led.archive), [lr.ledger_key("AFC-1", 5, "thread three")])
        zulip.topic_messages.append(api_message(2, "more"))
        self.t[0] += 86400
        eng.feed_message(zulip, event_message(2, "thread three", "more"))
        self.assertEqual(self.lin.create_calls, 1)  # no second comment under a new name
        self.assertEqual([cid for cid, _body in self.lin.updated], ["c1"])
        self.assertEqual(list(led.entries), [lr.ledger_key("AFC-1", 5, "thread three")])
        self.assertEqual(led.archive, {})

    def test_engine_end_to_end_and_across_a_restart(self) -> None:
        history = [api_message(1, "first"), api_message(2, "second")]
        zulip = FakeZulip(history)
        path = self.dir / "operator" / "linear-comments.json"
        eng = self.engine(zulip, path)
        eng.feed_message(zulip, event_message(1, "thread", "first"))
        eng.feed_message(zulip, event_message(2, "thread", "second"))
        eng.feed_message(zulip, event_message(3, "thread", "this is AFC-1"))
        self.assertEqual(len(self.lin.created), 1)
        self.assertIn("near/1", self.lin.created[0][1])

        # A restarted engine reads the same ledger back (copied to a fresh path, as in
        # the worker-level restart test, because the first engine still holds its flock).
        copy = self.dir / "operator" / "linear-comments-restarted.json"
        copy.write_text(path.read_text("utf-8"), "utf-8")
        restarted = self.engine(zulip, copy)
        restarted.feed_message(zulip, event_message(4, "thread", "and more on AFC-1"))
        self.t[0] += 700
        restarted.tick(zulip)
        self.assertEqual(self.lin.create_calls, 1)  # the restart stacked nothing
        self.assertEqual(len(self.lin.updated), 1)
        self.assertIn("near/4", self.lin.updated[0][1])

    def test_engine_is_silent_until_the_public_stream_set_loads(self) -> None:
        zulip = FakeZulip(streams_error=RuntimeError("no streams yet"))
        eng = self.engine(zulip)
        eng.feed_message(zulip, event_message(1, "thread", "AFC-1"))
        self.assertEqual(self.lin.created, [])
        zulip.streams_error = None
        eng.feed_message(zulip, event_message(2, "thread", "AFC-1 again"))
        self.assertEqual(len(self.lin.created), 1)

    def test_private_streams_are_never_commented(self) -> None:
        zulip = FakeZulip()
        eng = self.engine(zulip)
        secret = event_message(1, "thread", "AFC-1", stream_id=9)
        eng.feed_message(zulip, secret)
        self.assertEqual(self.lin.created, [])

    def test_dry_run_writes_nothing_to_linear(self) -> None:
        self.lin.dry_run = True
        led = lr.Ledger(self.dir / "operator" / "linear-comments.dry-run.json")
        eng = opl.OperatorLinearComments(
            realm=REALM,
            own_email=OWN,
            linear=self.lin,
            ledger=led,
            log=self.logs.append,
            clock=lambda: self.t[0],
            monotonic=lambda: self.t[0],
        )
        zulip = FakeZulip()
        eng.feed_message(zulip, event_message(1, "thread", "AFC-1"))
        self.assertEqual(self.lin.created, [])
        self.assertEqual(self.lin.updated, [])
        self.assertTrue(any("would create" in line for line in self.logs))

    def test_state_path_places_the_ledger_beside_the_registry(self) -> None:
        live = opl.state_path("/state", {}, False)
        self.assertEqual(live, Path("/state/operator/linear-comments.json"))
        dry = opl.state_path("/state", {}, True)
        self.assertEqual(dry, Path("/state/operator/linear-comments.dry-run.json"))


class FakeHttp:
    """Scripted HTTP for the credential loader:  routes by URL, records every call.
    Nothing here touches the network."""

    def __init__(self, record=None, secrets=None, statuses=None) -> None:
        self.record = (
            {
                "permissions": ["read"],
                "scopes": [{"environment": "prod", "secretPath": "/linear"}],
                "workspace": opl.INFISICAL_PROJECT_ID,
            }
            if record is None
            else record
        )
        self.secrets = (
            {
                "secrets": [
                    {"secretKey": "LINEAR_OPERATOR_DEVELOPER_TOKEN", "secretValue": "dev-key"},
                    {"secretKey": "LINEAR_MA_CLIENT_ID", "secretValue": "not-the-operator"},
                ]
            }
            if secrets is None
            else secrets
        )
        self.statuses = statuses or {}
        self.calls: list[tuple[str, str, dict, bytes | None]] = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers or {}), body))
        if url.endswith("/v2/service-token"):
            return self.statuses.get("record", 200), self.record
        if "/v3/secrets/raw" in url:
            return self.statuses.get("secrets", 200), self.secrets
        raise AssertionError("unexpected url %s" % url)


class CredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())
        self.logs: list[str] = []

    # never-a-credential: the synthetic fixture values are assembled at runtime
    def token_file(self, content="st-" + "from-file", mode=0o600) -> Path:
        path = self.dir / "service-token"
        path.write_text(content, "utf-8")
        path.chmod(mode)
        return path

    # -- where the service token comes from -------------------------------------------
    def test_service_token_env_wins_over_file(self) -> None:
        path = self.token_file()
        env = {opl.SERVICE_TOKEN_ENV: "  st-" + "from-env  "}  # never-a-credential
        self.assertEqual(opl.service_token(env, str(path)), "st-" + "from-env")

    def test_service_token_reads_the_configured_file(self) -> None:
        path = self.token_file()
        self.assertEqual(opl.service_token({}, str(path)), "st-" + "from-file")

    def test_service_token_missing_file_is_unconfigured(self) -> None:
        self.assertEqual(opl.service_token({}, str(self.dir / "absent")), "")

    def test_service_token_refuses_a_group_readable_file(self) -> None:
        path = self.token_file(mode=0o644)
        with self.assertRaises(opl.CredentialRefused) as caught:
            opl.service_token({}, str(path))
        self.assertEqual(caught.exception.reason, "token_file_permissions")
        self.assertNotIn("st-" + "from-file", str(caught.exception))

    # -- the key fetch ----------------------------------------------------------------------
    def test_key_fetch_verifies_scope_then_reads_the_key(self) -> None:
        http = FakeHttp()
        key = opl.fetch_api_key("st-test", http=http)
        self.assertEqual(key, "dev-key")
        record_call, secrets_call = http.calls
        self.assertEqual(record_call[2]["Authorization"], "Bearer st-test")
        query = secrets_call[1].split("?", 1)[1]
        self.assertIn("workspaceId=" + opl.INFISICAL_PROJECT_ID, query)
        self.assertIn("environment=prod", query)
        self.assertIn("secretPath=%2Flinear", query)

    def test_key_fetch_refuses_a_write_capable_token(self) -> None:
        http = FakeHttp(
            record={
                "permissions": ["read", "write"],
                "scopes": [{"environment": "prod", "secretPath": "/linear"}],
                "workspace": opl.INFISICAL_PROJECT_ID,
            }
        )
        with self.assertRaises(opl.CredentialRefused) as caught:
            opl.fetch_api_key("st-test", http=http)
        self.assertEqual(caught.exception.reason, "token_not_read_only")
        self.assertEqual(len(http.calls), 1)  # refused before any secret was read

    def test_key_fetch_refuses_a_wider_scope(self) -> None:
        for scopes in (
            [{"environment": "prod", "secretPath": "/"}],
            [
                {"environment": "prod", "secretPath": "/linear"},
                {"environment": "prod", "secretPath": "/zulip"},
            ],
        ):
            http = FakeHttp(
                record={
                    "permissions": ["read"],
                    "scopes": scopes,
                    "workspace": opl.INFISICAL_PROJECT_ID,
                }
            )
            with self.assertRaises(opl.CredentialRefused) as caught:
                opl.fetch_api_key("st-test", http=http)
            self.assertEqual(caught.exception.reason, "token_scope_mismatch")

    def test_key_fetch_refuses_another_projects_token(self) -> None:
        http = FakeHttp(
            record={
                "permissions": ["read"],
                "scopes": [{"environment": "prod", "secretPath": "/linear"}],
                "workspace": "00000000-0000-0000-0000-000000000000",
            }
        )
        with self.assertRaises(opl.CredentialRefused) as caught:
            opl.fetch_api_key("st-test", http=http)
        self.assertEqual(caught.exception.reason, "token_scope_mismatch")

    def test_key_fetch_refuses_a_missing_key(self) -> None:
        http = FakeHttp(
            secrets={"secrets": [{"secretKey": "LINEAR_MA_CLIENT_ID", "secretValue": "x"}]}
        )
        with self.assertRaises(opl.CredentialRefused) as caught:
            opl.fetch_api_key("st-test", http=http)
        self.assertEqual(caught.exception.reason, "key_missing")
        self.assertIn("LINEAR_OPERATOR_DEVELOPER_TOKEN", str(caught.exception))
        self.assertNotIn("dev-key", str(caught.exception))

    def test_key_fetch_refuses_on_an_http_error(self) -> None:
        http = FakeHttp(statuses={"record": 500})
        with self.assertRaises(opl.CredentialRefused) as caught:
            opl.fetch_api_key("st-test", http=http)
        self.assertEqual(caught.exception.reason, "infisical_error")
        self.assertIn("HTTP 500", str(caught.exception))

    # -- the whole load, and build ---------------------------------------------------------
    def test_load_api_key_runs_the_whole_chain(self) -> None:
        http = FakeHttp()
        hidden: list[str] = []
        env = {opl.SERVICE_TOKEN_ENV: "st-test"}
        self.assertEqual(opl.load_api_key(env, http=http, hide=hidden.extend), "dev-key")
        self.assertEqual(len(http.calls), 2)  # record, then the key — no OAuth exchange
        self.assertTrue(http.calls[0][1].endswith("/v2/service-token"))
        self.assertIn("/v3/secrets/raw", http.calls[1][1])
        self.assertEqual(hidden, ["dev-key"])  # registered for log scrubbing at fetch

    def test_the_key_fetch_prefers_the_store_over_the_bound_copy(self) -> None:
        # The store is the authority:  a hand-set copy never replaces the reviewed grant.
        path = self.token_file(content="st-" + "from-file")  # never-a-credential: synthetic fixture assembled at runtime
        env = {opl.SERVICE_TOKEN_ENV: "st-" + "from-env"}  # never-a-credential: synthetic fixture assembled at runtime
        http = FakeHttp()
        self.assertEqual(opl.load_api_key(env, str(path), http=http), "dev-key")
        bearer = http.calls[0][2].get("Authorization", "")
        self.assertIn("st-" + "from-file", bearer)
        self.assertNotIn("st-" + "from-env", bearer)

    def test_load_api_key_unconfigured_is_none(self) -> None:
        http = FakeHttp()
        self.assertIsNone(opl.load_api_key({}, str(self.dir / "absent"), http=http))
        self.assertEqual(http.calls, [])

    def test_build_wires_the_developer_token_into_the_engine(self) -> None:
        http = FakeHttp()
        env = {opl.SERVICE_TOKEN_ENV: "st-test"}
        engine = opl.build(env, self.dir, log=self.logs.append, http=http)
        self.assertIsNotNone(engine)
        self.assertIsInstance(engine.worker.linear, lr.LinearClient)
        self.assertEqual(engine.worker.linear.auth_scheme, "api_key")
        engine.close()

    def test_build_without_a_service_token_is_off(self) -> None:
        engine = opl.build(
            {},
            self.dir,
            log=self.logs.append,
            service_token_file=str(self.dir / "absent"),
            http=FakeHttp(),
        )
        self.assertIsNone(engine)

    def test_build_propagates_a_refused_credential(self) -> None:
        http = FakeHttp(statuses={"secrets": 403})
        env = {opl.SERVICE_TOKEN_ENV: "st-test"}
        with self.assertRaises(opl.CredentialRefused):
            opl.build(env, self.dir, log=self.logs.append, http=http)


class _StubResponse:
    def __init__(self, payload) -> None:
        import json

        self._raw = json.dumps(payload).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _StubOpener:
    """Captures the requests a LinearClient sends; answers a canned GraphQL payload."""

    def __init__(self) -> None:
        self.requests: list = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        return _StubResponse({"data": {"ok": True}})


class LinearAuthFormTests(unittest.TestCase):
    """The developer token goes out bare (Linear's personal-key form);  the default
    OAuth form keeps its Bearer prefix for the standalone relay."""

    def test_api_key_scheme_sends_the_token_without_a_prefix(self) -> None:
        opener = _StubOpener()
        client = lr.LinearClient("dev-key", opener=opener, auth_scheme="api_key")
        client._gql("query { ok }", {})
        self.assertEqual(opener.requests[0].get_header("Authorization"), "dev-key")

    def test_default_scheme_sends_a_bearer_token(self) -> None:
        opener = _StubOpener()
        client = lr.LinearClient("oauth-token", opener=opener)
        client._gql("query { ok }", {})
        self.assertEqual(
            opener.requests[0].get_header("Authorization"), "Bearer oauth-token"
        )

    def test_an_unknown_scheme_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            lr.LinearClient("x", auth_scheme="basic")


class LedgerCloseTests(unittest.TestCase):
    def test_close_saves_entries_dirtied_since_the_last_save(self) -> None:
        import json

        path = Path(tempfile.mkdtemp()) / "state.json"
        led = lr.Ledger(path)
        led.open()
        led.mark_dirty()
        led.close()  # must flush the dirty state before releasing the flock
        self.assertTrue(path.exists())
        self.assertIn("entries", json.loads(path.read_text("utf-8")))
        again = lr.Ledger(path)
        again.open()  # the flock was released, so a rebuilt engine can open it
        again.close()


class HistoryPagingEdgeTests(unittest.TestCase):
    def test_a_repeated_page_neither_duplicates_nor_pages_forever(self) -> None:
        class DupZulip:
            def __init__(self) -> None:
                self.calls = 0

            def get(self, path, params=None):
                self.calls += 1
                return {"messages": [api_message(1, "one"), api_message(2, "two")]}

        zulip = DupZulip()
        with mock.patch.object(opl, "PAGE", 2):
            got = opl.fetch_topic_history(zulip, 5, "thread")
        self.assertEqual([m["id"] for m in got], [1, 2])
        self.assertEqual(zulip.calls, 2)  # one repeat, recognised, and done

    def test_a_read_after_an_id_keeps_the_repeat_page_guards(self) -> None:
        class DupZulip:
            def __init__(self) -> None:
                self.anchors: list = []

            def get(self, path, params=None):
                self.anchors.append(params["anchor"])
                return {"messages": [api_message(4, "four"), api_message(5, "five")]}

        # The API repeats one page whatever the anchor:  it is taken once, and the read ends.
        zulip = DupZulip()
        with mock.patch.object(opl, "PAGE", 2):
            got = opl.fetch_topic_history(zulip, 5, "thread", after_id=3)
        self.assertEqual([m["id"] for m in got], [4, 5])
        self.assertEqual(zulip.anchors, [3, 5])
        # A full page that ends at the id the read started after cannot move the anchor on:
        # the read stops there instead of asking for the same page again.
        zulip = DupZulip()
        with mock.patch.object(opl, "PAGE", 2):
            got = opl.fetch_topic_history(zulip, 5, "thread", after_id=5)
        self.assertEqual([m["id"] for m in got], [4, 5])
        self.assertEqual(zulip.anchors, [5])


class _StubLog:
    def __init__(self) -> None:
        self.rows: list[tuple[str, dict]] = []

    def write(self, kind, **fields) -> None:
        self.rows.append((kind, fields))

    def kinds(self) -> list[str]:
        return [kind for kind, _fields in self.rows]


class _StubClock:
    def time(self) -> float:
        return 1000.0

    def monotonic(self) -> float:
        return 1000.0


class DaemonConfigureTests(unittest.TestCase):
    """The daemon's _configure_linear_comments, driven on a stub self:  the engine is
    built from the service token, kept across a reload that leaves the token alone,
    and the feature — never the daemon — fails closed on a refused credential."""

    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp())

    def stub(self, env, token_file=""):
        import types

        from simple_tools import daemon as D

        stub = types.SimpleNamespace()
        stub.config = types.SimpleNamespace(
            linear_comments_enabled=True,
            linear_comments_infisical_token_file=token_file,
        )
        stub.env = env
        stub.root = self.dir
        stub.log = _StubLog()
        stub.clock = _StubClock()
        stub.hidden: list[str] = []
        stub.hide_values = lambda values: stub.hidden.extend(values)
        stub.scrub = lambda text: text
        stub.linear_comments = None
        stub._linear_comments_spec = None
        stub.configure = lambda: D.Daemon._configure_linear_comments(stub)
        return stub

    def test_no_service_token_logs_off_and_builds_nothing(self) -> None:
        stub = self.stub({}, token_file=str(self.dir / "absent"))
        stub.configure()
        self.assertIsNone(stub.linear_comments)
        self.assertIn("linear-comments-off", stub.log.kinds())

    def test_engine_builds_from_the_service_token_and_survives_a_reload(self) -> None:
        http = FakeHttp()
        stub = self.stub({opl.SERVICE_TOKEN_ENV: "st-test"})
        with mock.patch.object(opl, "_default_http", http):
            stub.configure()
            engine = stub.linear_comments
            self.assertIsNotNone(engine)
            self.assertEqual(stub._linear_comments_spec, ("st-test",))
            self.assertIn("st-test", stub.hidden)  # the log scrubber knows the value
            self.assertIn("dev-key", stub.hidden)  # and the fetched developer token
            stub.configure()  # a reload with the same token keeps the engine
            self.assertIs(stub.linear_comments, engine)
            self.assertEqual(len(http.calls), 2)  # no second fetch
        engine.close()

    def test_a_stopped_engine_is_rebuilt_on_reload(self) -> None:
        http = FakeHttp()
        stub = self.stub({opl.SERVICE_TOKEN_ENV: "st-test"})
        with mock.patch.object(opl, "_default_http", http):
            stub.configure()
            engine = stub.linear_comments
            self.assertIsNotNone(engine)
            engine.stopped = "linear refused the credential"  # an auth stop
            stub.configure()  # the same service token, but the engine is stopped
            rebuilt = stub.linear_comments
            self.assertIsNotNone(rebuilt)  # the old ledger lock was released first
            self.assertIsNot(rebuilt, engine)
            self.assertIsNone(rebuilt.stopped)
        rebuilt.close()

    def test_a_group_readable_token_file_fails_closed_in_the_log(self) -> None:
        path = self.dir / "service-token"
        path.write_text("st-" + "from-file", "utf-8")
        path.chmod(0o644)
        stub = self.stub({}, token_file=str(path))
        stub.configure()
        self.assertIsNone(stub.linear_comments)
        self.assertIn("linear-comments-error", stub.log.kinds())
        self.assertNotIn("linear-comments-off", stub.log.kinds())


class DaemonReconnectTests(unittest.TestCase):
    """The daemon's route_backfill, driven on a stub self:  every seat connect tells the living
    comments that a queue registered, before the backfill is routed, and a failure there is
    this feature's logged error, never the backfill's."""

    def test_a_seat_backfill_tells_the_engine_a_queue_registered(self) -> None:
        import types

        from simple_tools import daemon as D

        calls: list = []
        engine = types.SimpleNamespace(queue_registered=lambda: calls.append("registered"))
        stub = types.SimpleNamespace(linear_comments=engine, log=_StubLog(), scrub=lambda t: t)
        stub._observe_linear_reconnect = lambda runner: D.Daemon._observe_linear_reconnect(
            stub, runner
        )
        stub.route_message = lambda runner, message: calls.append(message["id"])
        runner = types.SimpleNamespace(
            seat="CLAUDE", gaps=[], flush_cursor=lambda force=False: calls.append("flushed")
        )
        D.Daemon.route_backfill(stub, runner, [{"id": 8}, {"id": 7}])
        self.assertEqual(calls, ["registered", 7, 8, "flushed"])

        def broken() -> None:
            raise RuntimeError("the engine failed")

        engine.queue_registered = broken
        calls.clear()
        D.Daemon.route_backfill(stub, runner, [{"id": 9}])
        self.assertEqual(calls, [9, "flushed"])  # the backfill is routed all the same
        self.assertEqual(stub.log.kinds(), ["linear-comments-error"])

        stub.linear_comments = None  # the feature is off:  nothing to tell
        calls.clear()
        D.Daemon.route_backfill(stub, runner, [])
        self.assertEqual(calls, ["flushed"])
        self.assertEqual(stub.log.kinds(), ["linear-comments-error"])


class DaemonEventOrderTests(unittest.TestCase):
    """The daemon's handle_events, driven on a stub self:  the living comments see a rename,
    and a stream event, even when the daemon's own handling of that event fails (Kody, PR
    2298).  Both used to run after it in one try, so a failure there skipped the comments, and
    the standing comment's entry, or its archived record, stayed under the old topic name."""

    def test_the_comments_see_the_event_when_the_daemons_own_handling_fails(self) -> None:
        import types

        from simple_tools import daemon as D

        calls: list = []
        engine = types.SimpleNamespace(
            feed_event=lambda client, event: calls.append(("comments", event["type"])),
            reload_streams=lambda client: calls.append(("comments", "reload")),
        )
        stub = types.SimpleNamespace(linear_comments=engine, log=_StubLog(), scrub=lambda t: t)
        stub._observe_linear_event = lambda runner, event: D.Daemon._observe_linear_event(
            stub, runner, event
        )
        stub._observe_linear_stream_event = (
            lambda runner, event: D.Daemon._observe_linear_stream_event(stub, runner, event)
        )

        def broken(runner, event) -> None:
            calls.append(("daemon", event["type"]))
            raise OSError("a lease file could not be written")

        stub.handle_update = broken
        stub.handle_stream_event = broken
        runner = types.SimpleNamespace(
            seat="CLAUDE", client=None, flush_cursor=lambda force=False: calls.append("flushed")
        )
        rename = {
            "type": "update_message",
            "id": 3,
            "stream_id": 5,
            "orig_subject": "thread",
            "subject": "thread two",
            "propagate_mode": "change_all",
            "message_ids": [1],
        }
        private = {"type": "stream", "id": 4, "op": "update", "property": "invite_only",
                   "value": True, "stream_id": 5}
        D.Daemon.handle_events(stub, runner, [rename, private])
        self.assertEqual(
            calls,
            [
                ("comments", "update_message"),
                ("daemon", "update_message"),
                ("comments", "reload"),
                ("daemon", "stream"),
                "flushed",
            ],
        )
        # Each failure is the daemon's own logged event error, and stops nothing after it.
        self.assertEqual(stub.log.kinds(), ["event-error", "event-error"])


class LinearCommentsConfigTests(unittest.TestCase):
    def test_section_parsing(self) -> None:
        from simple_tools import config as C

        self.assertFalse(C.from_dict({}).linear_comments_enabled)
        cfg = C.from_dict({"linear_comments": "nope"})
        self.assertIn("[linear_comments] must be a table", cfg.errors)
        self.assertFalse(cfg.linear_comments_enabled)
        cfg = C.from_dict({"linear_comments": {"enabled": "yes"}})
        self.assertIn("linear_comments.enabled must be true or false", cfg.errors)
        self.assertFalse(cfg.linear_comments_enabled)
        cfg = C.from_dict({"linear_comments": {"enabled": True, "bogus": 1}})
        self.assertIn("[linear_comments] has unknown setting(s): bogus", cfg.errors)
        self.assertFalse(cfg.linear_comments_enabled)

    def test_infisical_token_file_parsing(self) -> None:
        from simple_tools import config as C

        self.assertEqual(C.from_dict({}).linear_comments_infisical_token_file, "")
        cfg = C.from_dict({"linear_comments": {"infisical_token_file": " /data/tok "}})
        self.assertEqual(cfg.linear_comments_infisical_token_file, "/data/tok")
        self.assertNotIn(
            "linear_comments.infisical_token_file must be a string", cfg.errors
        )
        cfg = C.from_dict({"linear_comments": {"infisical_token_file": 7}})
        self.assertIn("linear_comments.infisical_token_file must be a string", cfg.errors)
        self.assertFalse(cfg.linear_comments_enabled)


if __name__ == "__main__":
    unittest.main()
