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
  - no issue keys in a topic means no comment and no ledger entry, and text that merely
    looks key-shaped (UTF-8, COVID-19, a lowercase key) names nothing;
  - nothing is written until the public-stream set has loaded, and a history read that
    fails never blocks a create.

Nothing here touches the network:  the Linear client and the Zulip client are faked.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_sync import linear_relay as lr
from agent_sync import operator_linear as opl

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
    """Answers the streams call and pages a scripted topic history."""

    def __init__(self, topic_messages=(), streams_error=None) -> None:
        self.topic_messages = sorted(topic_messages, key=lambda m: m["id"])
        self.streams_error = streams_error
        self.message_calls: list[dict] = []

    def get(self, path, params=None):
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
            anchor = params.get("anchor")
            start = 0
            if isinstance(anchor, int):
                start = next(
                    (i + 1 for i, m in enumerate(self.topic_messages) if m["id"] == anchor),
                    len(self.topic_messages),
                )
            count = int(params.get("num_after") or 0)
            return {"messages": [dict(m) for m in self.topic_messages[start : start + count]]}
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


class LinearCommentsConfigTests(unittest.TestCase):
    def test_section_parsing(self) -> None:
        from agent_sync import config as C

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


if __name__ == "__main__":
    unittest.main()
