"""The Zulip -> Linear relay:  one standing Linear comment per (issue, topic), edited in place.

These tests pin the behaviour the owner ruled on and the findings Echo's reviewer and Kody raised
(2026-10-23):

  - one comment per (issue, topic), created once and edited in place, at most one edit every ten
    minutes, and never stacked;
  - a message is mirrored once, so a replay is idempotent;
  - only public streams are relayed: never a direct message, never a private stream, never the
    relay's own post, and nothing at all until the public-stream set has loaded;
  - a topic rename or a stream move carries the standing comment over (change_all only), and
    nothing that lived in a private stream is ever named in Linear;
  - a create whose answer was lost is reconciled and adopted, not stacked, and a create is never
    re-sent blind;
  - a dry run writes nothing to Linear and never touches the live ledger;
  - the ledger is bounded, owner-only, validated on load, and never stores a topic title;
  - the comment map outlives that bound (AFC-821): a pruned entry leaves its root and comment id
    in the ledger's archive, the topic's next message restores it and edits the same comment, and
    an archived record follows a topic move and a stream's privacy as a live entry does;
  - a restart catches up (AFC-821): given the reader for it, a standing entry records the messages
    its topic got while the worker was not running, or not receiving it, once each, before the
    live one, and a read cut short or failed is continued by a later message;
  - a Zulip field that carries anything credential-shaped is withheld whole;
  - a missing issue is not an auth failure, a transient error is retried while a not-found is
    not, and an error never carries the response body or the token.

Nothing here touches the network:  the Linear client is faked, and the tests that exercise the
HTTP layer hand the client a fake opener.
"""

from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import time
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from simple_tools import linear_relay as lr
from simple_tools.zulip import ApiError, NetworkError, _NoRedirect


class StreamsZulip:
    """A Zulip client for `check`: it answers the streams call, or raises what it is given."""

    def __init__(self, answer=None) -> None:
        self.answer = answer if answer is not None else {"streams": [
            {"stream_id": 5, "name": "builds", "invite_only": False},
            {"stream_id": 9, "name": "secret", "invite_only": True},
        ]}

    def get(self, path, params=None):
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


class FakeLinear:
    """The Linear client the worker talks to."""

    def __init__(self, known=("AFC-1", "OPS-9")) -> None:
        self.known = set(known)
        self.created: list[tuple[str, str]] = []
        self.updated: list[tuple[str, str]] = []
        self.by_marker: dict[str, str] = {}
        self.dry_run = False
        self.lookups: list[str] = []
        self.fail_find = False
        self.create_error: Exception | None = None
        self.create_lands = False  # with create_error: the comment landed all the same
        self.create_calls = 0
        self.update_error: Exception | None = None
        self.find_error: Exception | None = None

    def issue_id(self, identifier: str) -> str:
        self.lookups.append(identifier)
        if identifier not in self.known:
            raise lr.LinearNotFound(identifier)
        return "uuid-" + identifier

    def create_comment(self, issue_uuid: str, body: str) -> str:
        self.create_calls += 1
        if self.create_error is not None:
            if self.create_lands:
                self.by_marker[body.split("\n", 1)[0]] = "c-landed"
            raise self.create_error
        self.created.append((issue_uuid, body))
        cid = "c%d" % len(self.created)
        self.by_marker[body.split("\n", 1)[0]] = cid
        return cid

    def update_comment(self, comment_id: str, body: str) -> None:
        if self.update_error is not None:
            raise self.update_error
        self.updated.append((comment_id, body))

    def find_comment(self, issue_uuid: str, marker: str) -> str | None:
        if self.fail_find:
            raise lr.LinearError("boom")
        if self.find_error is not None:
            raise self.find_error
        return self.by_marker.get(marker)

    def viewer(self) -> str:
        return "Zulip Relay"


class FakeResponse:
    def __init__(self, payload) -> None:
        self.payload = payload

    def read(self) -> bytes:
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Answers the Linear client's requests in order.  An answer is a JSON-able payload, raw
    bytes, an exception, or a callable that returns or raises one of those."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.requests: list = []

    def open(self, req, timeout=None):
        self.requests.append(req)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if callable(answer):
            answer = answer()
        if isinstance(answer, BaseException):
            raise answer
        return FakeResponse(answer)


def http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(lr.LINEAR_URL, code, "error", {}, io.BytesIO(body))


def message(mid: int, topic: str, content: str, mtype: str = "stream", sender: str = "a@x",
            stream_id: int = 5) -> dict:
    return {
        "type": "message",
        "message": {
            "id": mid,
            "type": mtype,
            "stream_id": stream_id,
            "display_recipient": "builds",
            "subject": topic,
            "content": content,
            "sender_email": sender,
            "sender_full_name": "Al",
        },
    }


def move(old_sid: int, orig: str, *, new_sid=None, subject=None, mode="change_all",
         ids=(1,), stream_name="builds") -> dict:
    """An update_message event shaped like Zulip's get-events schema: stream_id and stream_name
    are the PRE-edit stream, new_stream_id the POST-edit one, subject absent when the topic did
    not change."""
    event = {
        "type": "update_message",
        "stream_id": old_sid,
        "stream_name": stream_name,
        "orig_subject": orig,
        "propagate_mode": mode,
        "message_ids": list(ids),
    }
    if new_sid is not None:
        event["new_stream_id"] = new_sid
    if subject is not None:
        event["subject"] = subject
    return event


def past(mid: int, content: str, sender: str = "a@x") -> dict:
    """One message as the Zulip messages API returns it: what a history reader hands the worker."""
    return {
        "id": mid,
        "sender_email": sender,
        "sender_full_name": "Al",
        "content": content,
        "timestamp": 900 + mid,
    }


class Topic:
    """A scripted Zulip topic behind the two readers a hosting gives the worker: `whole` is the
    history reader (every message), `since` the catch-up reader (the messages after an id).
    Each records its calls; `whole` raises `whole_error` and `since` raises `error` while it is
    set, and both answer at most `cap` messages when it is set, as the hosting's reader stops at
    MAX_BACKFILL.  A topic named in `topics` has its own messages; any other reads `messages`."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.topics: dict[str, list[dict]] = {}
        self.whole_calls = 0
        self.since_calls: list[tuple[int, str, int]] = []
        self.error: Exception | None = None
        self.whole_error: Exception | None = None
        self.sloppy = False  # answer `since` with the whole topic, as if the id were ignored
        self.cap: int | None = None

    def whole(self, stream_id: int, topic: str) -> list[dict]:
        self.whole_calls += 1
        if self.whole_error is not None:
            raise self.whole_error
        return list(self.topics.get(topic, self.messages))[: self.cap]

    def since(self, stream_id: int, topic: str, after_id: int) -> list[dict]:
        self.since_calls.append((stream_id, topic, after_id))
        if self.error is not None:
            raise self.error
        messages = self.topics.get(topic, self.messages)
        return [m for m in messages if self.sloppy or m["id"] > after_id][: self.cap]


def first_line(body: str) -> str:
    """The marker line of a rendered comment."""
    return body.split("\n", 1)[0]


class RelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.t = [1000.0]
        self.lin = FakeLinear()
        self.logs: list[str] = []
        self.led = lr.Ledger(Path(tempfile.mkdtemp()) / "state.json")
        self.led.open()
        self.w = lr.RelayWorker(
            "https://z.example", "relay@x", self.lin, self.led,
            lr.build_regex(lr.DEFAULT_TEAMS), clock=lambda: self.t[0],
            log=self.logs.append, public_streams={5: "builds", 6: "deploys"},
        )

    def key(self, issue: str, sid: int, topic: str) -> str:
        return lr.ledger_key(issue, sid, topic)

    # -- the shape of the mirror ---------------------------------------------------------
    def test_the_regex_finds_the_team_keys_and_leaves_the_others_alone(self) -> None:
        r = lr.build_regex(lr.DEFAULT_TEAMS)
        self.assertEqual(lr.find_issue_ids(r, "see CC-12 and C-3, ABC-9, afc-7"), ["CC-12", "C-3"])

    def test_one_comment_per_issue_and_topic_then_batched_edits(self) -> None:
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.assertEqual(len(self.lin.created), 1)
        self.t[0] += 60
        self.w.handle_event(message(2, "AFC-1 relay", "again"))
        self.assertEqual(self.lin.updated, [])  # inside ten minutes: batched
        self.t[0] += 600
        self.w.tick()
        self.assertEqual(len(self.lin.updated), 1)
        self.assertEqual(len(self.lin.created), 1)  # edited in place, never stacked

    def test_a_replayed_message_is_not_mirrored_twice(self) -> None:
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.assertEqual(len(self.lin.created), 1)
        self.assertEqual(self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")]["count"], 1)

    def test_direct_messages_our_own_posts_and_unknown_issues_are_not_relayed(self) -> None:
        self.w.handle_event(message(3, "x", "AFC-1", mtype="private"))
        self.w.handle_event(message(4, "x", "AFC-1", sender="relay@x"))
        self.w.handle_event(message(5, "x", "OPS-99999"))
        self.assertEqual(self.lin.created, [])

    def test_a_message_in_a_private_stream_is_not_relayed(self) -> None:
        # The bot is a member of a private stream, so the event queue hands it the message.
        self.w.handle_event(message(9, "AFC-1 relay", "secret", stream_id=9))
        self.assertEqual(self.lin.created, [])

    def test_nothing_is_relayed_before_the_public_streams_are_known(self) -> None:
        self.w.set_public_streams(None)
        self.w.handle_event(message(9, "AFC-1 relay", "hello", stream_id=5))
        self.assertEqual(self.lin.created, [])

    def test_the_footer_carries_the_sentence_gap(self) -> None:
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        body = self.lin.created[0][1]
        self.assertIn("by the relay.\u00a0 Edit in Zulip", body)

    # -- renames and moves ---------------------------------------------------------------
    def test_a_rename_that_adds_an_issue_creates_that_issue_comment(self) -> None:
        self.w.handle_event(move(5, "plain", subject="OPS-9 plain", ids=[7]))
        self.assertEqual(len(self.lin.created), 1)
        self.assertIn(self.key("OPS-9", 5, "OPS-9 plain"), self.led.entries)

    def test_a_rename_names_the_stream_from_the_loaded_set(self) -> None:
        # Zulip's update_message event carries no reliable name for the stream the messages
        # are in now, so the heading takes it from the loaded set by id.
        event = move(5, "plain", subject="OPS-9 plain", ids=[7])
        del event["stream_name"]
        self.w.handle_event(event)
        self.assertIn("#builds > OPS-9 plain", self.lin.created[0][1])

    def test_a_topic_rename_carries_the_standing_comment_over(self) -> None:
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.w.handle_event(move(5, "AFC-1 relay", subject="AFC-1 relay renamed"))
        self.assertEqual(len(self.lin.created), 1)  # no second comment
        self.assertIn(self.key("AFC-1", 5, "AFC-1 relay renamed"), self.led.entries)
        self.assertNotIn(self.key("AFC-1", 5, "AFC-1 relay"), self.led.entries)
        # and the marker is unchanged, so the comment is still found under its old key
        entry = self.led.entries[self.key("AFC-1", 5, "AFC-1 relay renamed")]
        self.assertEqual(entry["marker"], lr.marker_for("AFC-1", 1))

    def test_a_rename_in_a_private_stream_is_not_relayed(self) -> None:
        # The rename path honours the same public-streams guard as messages: a private
        # stream's names and links never reach Linear.
        self.w.handle_event(move(9, "plain", subject="OPS-9 plain", ids=[7],
                                 stream_name="secret-stream"))
        self.assertEqual(self.lin.created, [])
        self.assertEqual(dict(self.led.entries), {})

    def test_a_move_from_a_private_to_a_public_stream_names_nothing_private(self) -> None:
        # stream_name is the PRE-edit (private) stream.  Neither its name nor the private
        # topic title may reach Linear, so the move records nothing at all.
        self.w.handle_event(move(9, "plan", new_sid=5, subject="OPS-9 plan", ids=[7],
                                 stream_name="private-sec"))
        self.assertEqual(self.lin.created, [])
        self.assertEqual(dict(self.led.entries), {})

    def test_a_channel_only_move_to_a_private_stream_closes_the_entry(self) -> None:
        # No subject: the topic kept its name and moved to another stream.
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")]["dirty"] = True
        self.w.handle_event(move(5, "AFC-1 relay", new_sid=9))
        self.assertEqual(dict(self.led.entries), {})
        self.t[0] += 600
        self.w.tick()
        self.assertEqual(self.lin.updated, [])  # the comment is never edited again

    def test_a_move_between_public_streams_rekeys_the_entry(self) -> None:
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.w.handle_event(move(5, "AFC-1 relay", new_sid=6, subject="AFC-1 moved",
                                 stream_name="builds"))
        self.assertNotIn(self.key("AFC-1", 5, "AFC-1 relay"), self.led.entries)
        entry = self.led.entries[self.key("AFC-1", 6, "AFC-1 moved")]
        self.assertEqual((entry["stream_id"], entry["stream"]), (6, "deploys"))
        self.t[0] += 600
        self.w.tick()
        self.assertEqual(len(self.lin.created), 1)
        body = self.lin.updated[-1][1]
        self.assertIn("#deploys > AFC-1 moved", body)  # the new stream's name, by id
        self.assertNotIn("#builds", body)

    def test_a_partial_move_leaves_the_old_topic_comment_standing(self) -> None:
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.w.handle_event(message(2, "AFC-1 relay", "again"))
        self.w.handle_event(move(5, "AFC-1 relay", subject="AFC-1 split", mode="change_one",
                                 ids=[2]))
        entry = self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")]
        self.assertEqual(entry["count"], 2)
        self.assertNotIn(self.key("AFC-1", 5, "AFC-1 split"), self.led.entries)
        self.w.handle_event(message(3, "AFC-1 relay", "more"))
        self.assertEqual(len(self.lin.created), 1)  # the old topic keeps its one comment

    def test_a_partial_move_to_a_private_stream_takes_the_moved_messages_out(self) -> None:
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.w.handle_event(message(2, "AFC-1 relay", "again"))
        self.w.handle_event(move(5, "AFC-1 relay", new_sid=9, mode="change_later", ids=[2]))
        entry = self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")]
        self.assertEqual([item["mid"] for item in entry["links"]], [1])
        self.assertEqual(entry["count"], 1)
        self.assertTrue(entry["dirty"])

    def test_a_content_only_edit_is_ignored(self) -> None:
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        before = json.dumps(self.led.entries, sort_keys=True)
        self.w.handle_event({"type": "update_message", "stream_id": 5, "message_id": 1,
                             "message_ids": [1], "content": "edited"})
        self.assertEqual(json.dumps(self.led.entries, sort_keys=True), before)

    def test_the_permalink_names_the_message_and_nothing_else(self) -> None:
        # The topic is untrusted, so it never goes into the URL.  Zulip's `near` narrow
        # resolves a message id on its own.
        self.assertEqual(
            lr.permalink("https://z.example", 42), "https://z.example/#narrow/near/42"
        )

    # -- the uncertain write -------------------------------------------------------------
    def test_reconcile_survives_a_linear_outage(self) -> None:
        entry = lr.new_entry("AFC-1", 5, "builds", "AFC-1 relay", 1)
        entry["pending"] = True
        self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")] = entry
        self.lin.fail_find = True
        self.w.reconcile()  # must not raise
        self.assertIsNone(entry["comment_id"])
        self.assertTrue(entry["pending"])  # still pending for the next registration

    def test_a_create_whose_answer_was_lost_is_adopted_not_stacked(self) -> None:
        # The create landed, but the process died before it could save the comment id.
        entry = lr.new_entry("AFC-1", 5, "builds", "AFC-1 relay", 1)
        entry["pending"] = True
        entry["dirty"] = True
        self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")] = entry
        self.lin.by_marker[entry["marker"]] = "c-lost"
        self.w.reconcile()
        self.assertEqual(entry["comment_id"], "c-lost")
        self.assertEqual(self.lin.created, [])  # nothing new was created

    def test_an_uncertain_create_stays_pending_and_is_adopted_next_time(self) -> None:
        self.lin.create_error = lr.LinearUncertain("linear create outcome unknown")
        self.lin.create_lands = True
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        entry = self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")]
        self.assertTrue(entry["pending"])
        self.assertIsNone(entry["comment_id"])
        self.lin.create_error = None
        self.t[0] += 1000
        self.w.tick()
        self.assertEqual(entry["comment_id"], "c-landed")
        self.assertFalse(entry["pending"])
        self.assertEqual(self.lin.create_calls, 1)  # never sent a second time

    def test_a_refused_token_stops_the_relay(self) -> None:
        self.lin.create_error = lr.LinearAuthError("linear refused LINEAR_RELAY_TOKEN")
        with self.assertRaises(SystemExit):
            self.w.handle_event(message(1, "AFC-1 relay", "hello"))

    def test_a_permanent_refusal_is_not_retried(self) -> None:
        self.lin.create_error = lr.LinearPermanentError("linear refused the request")
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        entry = self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")]
        self.assertFalse(entry["dirty"])
        self.assertFalse(entry["pending"])
        self.assertEqual(entry["retry_after"], 0.0)

    def test_a_refused_edit_keeps_a_comment_that_is_still_there(self) -> None:
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.lin.update_error = lr.LinearRejected("linear refused the edit")
        self.w.handle_event(message(2, "AFC-1 relay", "again"))
        self.t[0] += 600
        self.w.tick()
        entry = self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")]
        self.assertEqual(len(self.lin.created), 1)  # no second comment
        self.assertEqual(entry["comment_id"], "c1")
        # success false is a definite answer: not retried until a new message arrives
        self.assertFalse(entry["dirty"])
        self.assertEqual(entry["retry_after"], 0.0)

    def test_an_edit_whose_stored_id_missed_goes_to_the_comment_the_scan_found(self) -> None:
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        entry = self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")]
        self.lin.by_marker[entry["marker"]] = "c-found"  # the comment answers to another id
        self.lin.update_error = lr.LinearNotFound("linear: not found")
        self.w.handle_event(message(2, "AFC-1 relay", "again"))
        self.t[0] += 600
        self.w.tick()
        self.assertEqual(entry["comment_id"], "c-found")
        self.assertEqual(len(self.lin.created), 1)
        self.assertTrue(entry["dirty"])  # retried, against the id the scan found
        self.lin.update_error = None
        self.t[0] = entry["retry_after"] + 600
        self.w.tick()
        self.assertEqual(self.lin.updated[-1][0], "c-found")

    def test_a_403_on_an_edit_keeps_the_relay_running(self) -> None:
        # A bare 403 refuses that one request, not the token: the relay does not stop.
        self.assertIsInstance(
            lr.LinearClient("tok", opener=FakeOpener(lambda: http_error(403)))._http_error(
                http_error(403)
            ),
            lr.LinearPermanentError,
        )
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.lin.update_error = lr.LinearPermanentError("linear refused the request (http 403)")
        self.w.handle_event(message(2, "AFC-1 relay", "again"))
        self.t[0] += 600
        self.w.tick()  # must not raise SystemExit
        self.assertEqual(len(self.lin.created), 1)

    def test_an_issue_named_before_it_exists_is_looked_up_again_later(self) -> None:
        self.w.handle_event(message(1, "AFC-7 early", "hello"))
        self.assertEqual(self.lin.created, [])
        self.lin.known.add("AFC-7")
        self.w.handle_event(message(2, "AFC-7 early", "again"))
        self.assertEqual(self.lin.created, [])  # still inside the wait
        self.t[0] += lr.MISSING_TTL + 1
        self.w.handle_event(message(3, "AFC-7 early", "now it exists"))
        self.assertEqual(len(self.lin.created), 1)

    def test_a_tick_stops_when_its_budget_is_spent(self) -> None:
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        entry = self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")]
        entry["dirty"] = True
        self.t[0] += 600
        with mock.patch.object(lr, "TICK_BUDGET", -1.0):
            self.w.tick()
        self.assertEqual(self.lin.updated, [])
        self.assertTrue(entry["dirty"])  # waits for the next cycle
        self.w.tick()
        self.assertEqual(len(self.lin.updated), 1)

    def test_a_failed_ledger_write_does_not_stop_the_tick(self) -> None:
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))

        def broken() -> None:
            raise OSError("disk full")

        self.led.save = broken
        self.led.mark_dirty()
        self.w.tick()  # must not raise
        self.assertTrue(any("could not write the relay ledger" in line for line in self.logs))

    def test_merging_two_topics_adds_their_counts(self) -> None:
        self.w.handle_event(message(1, "AFC-1 a", "one"))
        self.w.handle_event(message(2, "AFC-1 a", "two"))
        self.w.handle_event(message(3, "AFC-1 b", "three"))
        self.w.handle_event(move(5, "AFC-1 a", subject="AFC-1 b", ids=[1, 2]))
        self.assertNotIn(self.key("AFC-1", 5, "AFC-1 a"), self.led.entries)
        self.assertEqual(self.led.entries[self.key("AFC-1", 5, "AFC-1 b")]["count"], 3)

    def test_a_message_that_came_in_by_a_rename_has_no_empty_quote(self) -> None:
        self.w.handle_event(move(5, "plain", subject="OPS-9 plain", ids=[7]))
        body = self.lin.created[0][1]
        self.assertIn("topic rename", body)
        self.assertNotIn('""', body)

    def test_a_permanently_refused_edit_is_not_retried(self) -> None:
        # An edit the app may not make: the comment is kept, and waiting changes nothing.
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.lin.update_error = lr.LinearPermanentError("linear refused the request (FORBIDDEN)")
        self.w.handle_event(message(2, "AFC-1 relay", "again"))
        self.t[0] += 600
        self.w.tick()
        entry = self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")]
        self.assertEqual(entry["comment_id"], "c1")
        self.assertEqual(len(self.lin.created), 1)
        self.assertFalse(entry["dirty"])
        self.assertEqual(entry["retry_after"], 0.0)

    def test_a_reconcile_of_a_deleted_issue_releases_the_entry(self) -> None:
        entry = lr.new_entry("AFC-1", 5, "builds", "AFC-1 relay", 1)
        entry["pending"] = True
        self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")] = entry
        self.lin.find_error = lr.LinearNotFound("linear: not found")
        self.w.reconcile()
        self.assertFalse(entry["pending"])
        self.lin.find_error = lr.LinearError("must not be asked again")
        self.w.reconcile()  # released: not probed again
        self.assertFalse(any("must not" in line for line in self.logs))

    def test_an_edit_of_a_deleted_comment_creates_it_again(self) -> None:
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.lin.update_error = lr.LinearNotFound("linear: not found")
        self.lin.by_marker.clear()  # the comment really is gone
        self.w.handle_event(message(2, "AFC-1 relay", "again"))
        self.t[0] += 600
        self.w.tick()
        entry = self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")]
        self.assertEqual(len(self.lin.created), 2)
        self.assertEqual(entry["comment_id"], "c2")

    def test_a_dry_run_writes_nothing_to_linear(self) -> None:
        self.lin.dry_run = True
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.assertEqual(self.lin.created, [])
        self.assertEqual(self.lin.updated, [])

    def test_a_dry_run_never_uses_the_live_ledger(self) -> None:
        default = Path(lr.LIVE_STATE).expanduser()
        self.assertEqual(lr.state_path({}, False), default)
        self.assertEqual(lr.state_path({}, True), Path(lr.DRY_STATE).expanduser())
        # A configured live ledger on a volume: the dry run writes a sibling, never the file the
        # live run will use, so it cannot mark topics written that were never created.
        env = {"LINEAR_RELAY_STATE": "/tmp/fixture/linear-relay.json"}
        self.assertEqual(lr.state_path(env, False), Path("/tmp/fixture/linear-relay.json"))
        self.assertEqual(lr.state_path(env, True), Path("/tmp/fixture/linear-relay.dry-run.json"))
        # A path with no file name is refused by name, not with a traceback, live and dry alike.
        for bad in ("/", ".", ".."):
            for dry in (False, True):
                with self.subTest(path=bad, dry_run=dry):
                    with self.assertRaisesRegex(SystemExit, "LINEAR_RELAY_STATE must name"):
                        lr.state_path({"LINEAR_RELAY_STATE": bad}, dry)

    def test_one_failing_entry_does_not_stop_the_tick(self) -> None:
        self.w.handle_event(message(1, "AFC-1 a", "hello"))
        self.w.handle_event(message(2, "AFC-1 b", "hello"))
        for entry in self.led.entries.values():
            entry["dirty"] = True
        real = self.w._flush
        calls: list[str] = []

        def flaky(key, now):
            calls.append(key)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return real(key, now)

        self.w._flush = flaky
        self.t[0] += 600
        self.w.tick()  # must not raise
        self.assertEqual(len(self.lin.updated), 1)

    # -- the ledger ----------------------------------------------------------------------
    def test_the_ledger_is_bounded(self) -> None:
        for i in range(lr.MAX_ENTRIES + 10):
            topic = "topic %d" % i
            key = lr.ledger_key("AFC-1", 5, topic)
            entry = lr.new_entry("AFC-1", 5, "builds", topic, i + 1)
            entry["last_touch"] = float(i)
            self.led.entries[key] = entry
        self.led.save()
        self.assertLessEqual(len(self.led.entries), lr.MAX_ENTRIES)

    def test_a_ledger_that_cannot_be_read_stops_the_relay(self) -> None:
        path = Path(tempfile.mkdtemp()) / "broken.json"
        path.write_text("{not json", "utf-8")
        with self.assertRaises(SystemExit):
            lr.Ledger(path).open()

    def test_a_ledger_with_a_malformed_entry_stops_the_relay(self) -> None:
        path = Path(tempfile.mkdtemp()) / "malformed.json"
        entry = lr.new_entry("AFC-1", 5, "builds", "AFC-1 relay", 1)
        del entry["seen"]
        data = {"version": lr.LEDGER_VERSION,
                "entries": {lr.ledger_key("AFC-1", 5, "AFC-1 relay"): entry}}
        path.write_text(json.dumps(data), "utf-8")
        with self.assertRaises(SystemExit):
            lr.Ledger(path).open()
        entry["seen"] = ["not an id"]
        path.write_text(json.dumps(data), "utf-8")
        with self.assertRaises(SystemExit):
            lr.Ledger(path).open()

    def test_a_saved_ledger_loads_again(self) -> None:
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.led.save()
        again = lr.Ledger(Path(tempfile.mkdtemp()) / "copy.json")
        again.path.write_text(self.led.path.read_text("utf-8"), "utf-8")
        again.open()
        self.assertEqual(again.entries, self.led.entries)

    def test_the_ledger_and_its_new_directory_are_owner_only(self) -> None:
        base = Path(tempfile.mkdtemp())
        led = lr.Ledger(base / "state" / "relay.json")
        led.open()
        led.save()
        self.assertEqual(stat.S_IMODE(os.stat(base / "state").st_mode) & 0o077, 0)
        self.assertEqual(stat.S_IMODE(os.stat(led.path).st_mode), 0o600)

    def test_a_symlinked_state_directory_is_refused(self) -> None:
        base = Path(tempfile.mkdtemp())
        (base / "real").mkdir()
        (base / "link").symlink_to(base / "real")
        with self.assertRaises(SystemExit):
            lr.Ledger(base / "link" / "relay.json").open()

    def test_a_relayed_message_does_not_rewrite_the_ledger_each_time(self) -> None:
        saves: list[int] = []
        real = self.led.save

        def counting() -> None:
            saves.append(1)
            real()

        self.led.save = counting
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))  # the create writes ahead
        self.w.handle_event(message(2, "AFC-1 relay", "again"))  # batched: no write
        self.assertEqual(len(saves), 1)
        self.w.tick()  # one write for the cycle
        self.assertEqual(len(saves), 2)

    # -- untrusted text ------------------------------------------------------------------
    def test_copied_text_with_a_credential_is_withheld_whole(self) -> None:
        # Built at run time, so no credential-shaped literal sits in the source.
        fixture_token = "ghp_" + "fake" * 9
        quote = lr.neutralise("token: %s and the rest of the line" % fixture_token)
        self.assertNotIn(fixture_token, quote)
        self.assertNotIn("rest of the line", quote)  # the whole field, not just the match
        self.assertIn("withheld", quote)
        # A passphrase runs on past the first word, so a partial redaction would leak it.
        self.assertNotIn("horse", lr.neutralise("password: correct horse battery staple"))

    def test_copied_text_cannot_break_out_of_the_comment(self) -> None:
        quote = lr.neutralise('see "*emphasis*" and [a](b)')
        self.assertNotIn("*emphasis*", quote)  # every asterisk is escaped
        self.assertNotIn('"', quote.replace('\\"', ""))  # the quote delimiter cannot be closed early
        self.assertNotIn("[a](b)", quote)

    def test_the_withheld_placeholder_is_stable(self) -> None:
        self.assertFalse(lr.has_secret(lr.WITHHELD))
        self.assertEqual(lr.safe_text(lr.WITHHELD), lr.WITHHELD)

    def test_a_rendered_comment_carries_the_marker_and_the_permalink(self) -> None:
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        body = self.lin.created[0][1]
        self.assertTrue(body.startswith(lr.MARKER))
        # The link names the message id and carries no topic text.
        self.assertIn("https://z.example/#narrow/near/1", body)
        self.assertIn("Mirrored one way from Zulip", body)

    def test_a_secret_in_the_topic_never_reaches_the_comment_the_ledger_or_the_log(self) -> None:
        # The topic is untrusted and can carry a credential.  It is withheld in the heading,
        # it never goes into the URL, the ledger keys on a digest of it, and the log names
        # the issue and the message id only.
        self.w.handle_event(message(1, "AFC-1 password=hunter2 relay", "hello"))
        body = self.lin.created[0][1]
        self.assertNotIn("hunter2", body)
        link = body.split("](", 1)[1].split(")", 1)[0]
        self.assertEqual(link, "https://z.example/#narrow/near/1")
        self.t[0] += 600
        self.w.handle_event(message(2, "AFC-1 password=hunter2 relay", "again"))
        self.w.tick()
        self.led.save()
        self.assertNotIn("hunter2", self.led.path.read_text("utf-8"))
        self.assertFalse(any("hunter2" in line for line in self.logs))
        self.assertTrue(any("withheld topic" in line for line in self.logs))

    # -- a stream that turns private, and a set that has not loaded ------------------------
    def test_a_migration_in_a_private_stream_is_refused(self) -> None:
        # The move path refuses a private stream, and the migration refuses it too, so a
        # private topic's standing comment can never be carried over.
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.w._migrate_topic(5, 9, "secret-stream", "AFC-1 relay", "AFC-1 relay renamed")
        self.assertIn(self.key("AFC-1", 5, "AFC-1 relay"), self.led.entries)
        self.assertNotIn(self.key("AFC-1", 9, "AFC-1 relay renamed"), self.led.entries)

    def test_an_entry_whose_stream_turns_private_is_dropped_not_written(self) -> None:
        # The stream was public when the entry was written and is private now, so the
        # standing comment is neither created nor edited, and the entry leaves the ledger.
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.assertEqual(len(self.lin.created), 1)
        entry = self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")]
        entry["dirty"] = True
        self.w.set_public_streams({7: "other"})  # the stream is not in the public set any more
        self.t[0] += 600
        self.w.tick()
        self.assertEqual(self.lin.updated, [])
        self.assertNotIn(self.key("AFC-1", 5, "AFC-1 relay"), self.led.entries)

    def test_a_pending_entry_whose_stream_turns_private_is_not_reconciled(self) -> None:
        entry = lr.new_entry("AFC-1", 5, "builds", "AFC-1 relay", 1)
        entry["pending"] = True
        self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")] = entry
        self.lin.by_marker[entry["marker"]] = "c-lost"
        self.w.set_public_streams({7: "other"})
        self.w.reconcile()
        self.assertIsNone(entry["comment_id"])
        self.assertNotIn(self.key("AFC-1", 5, "AFC-1 relay"), self.led.entries)

    def test_an_unloaded_stream_set_writes_nothing_and_drops_nothing(self) -> None:
        # Unloaded is not "no public streams": nothing is written, nothing is dropped, and the
        # entry keeps its state until the set loads.
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        entry = self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")]
        entry["dirty"] = True
        self.w.set_public_streams(None)
        self.t[0] += 600
        self.w.tick()
        self.w.reconcile()
        self.assertEqual(self.lin.updated, [])
        self.assertIn(self.key("AFC-1", 5, "AFC-1 relay"), self.led.entries)
        self.assertTrue(entry["dirty"])
        self.w.set_public_streams({5: "builds"})  # loaded, with the stream: the write proceeds
        self.w.tick()
        self.assertEqual(len(self.lin.updated), 1)

    def test_a_loaded_set_without_the_stream_drops_the_entry(self) -> None:
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")]["dirty"] = True
        self.w.set_public_streams({})  # loaded, and the realm has no public stream
        self.t[0] += 600
        self.w.tick()
        self.assertEqual(self.lin.updated, [])
        self.assertEqual(dict(self.led.entries), {})

    def test_archived_public_streams_count_as_public(self) -> None:
        asked: list = []

        class FakeZulip:
            def get(self, path, params=None):
                asked.append((path, params))
                return {"streams": [
                    {"stream_id": 5, "name": "builds", "invite_only": False},
                    {"stream_id": 6, "name": "old", "invite_only": False, "is_archived": True},
                    {"stream_id": 9, "name": "secret", "invite_only": True},
                ]}

        self.assertEqual(lr.load_public_streams(FakeZulip()), {5: "builds", 6: "old"})
        self.assertEqual(asked, [("streams", {"exclude_archived": False})])

    def test_a_malformed_streams_answer_fails_the_load(self) -> None:
        # A missing list is not "no public streams", and a stream without its privacy flag is
        # not public: the load fails, and the relay relays nothing.
        for answer in ({}, {"streams": None}, {"streams": [{"stream_id": 5, "name": "b"}]},
                       {"streams": [{"stream_id": "5", "name": "b", "invite_only": False}]},
                       {"streams": [{"stream_id": 0, "name": "b", "invite_only": False}]},
                       {"streams": [{"stream_id": 5, "name": " ", "invite_only": False}]},
                       {"streams": [{"stream_id": 5, "invite_only": False}]}):
            class FakeZulip:
                def get(self, path, params=None, answer=answer):
                    return answer

            with self.assertRaises(ValueError):
                lr.load_public_streams(FakeZulip())

    def test_the_scanner_knows_service_prefixes_and_leaves_prose_alone(self) -> None:
        # Built at run time, so no credential-shaped literal sits in the source.
        linear_key = "lin_" + "api_" + "a1B2" * 10
        self.assertTrue(lr.has_secret("pasted %s by mistake" % linear_key))
        self.assertTrue(lr.has_secret("Authorization: Bearer " + "x1" * 12))
        self.assertTrue(lr.has_secret("password: correct horse battery staple"))
        for prose in ("AFC-12 token refresh fails", "password reset flow",
                      "credential rotation plan", "secret santa"):
            self.assertFalse(lr.has_secret(prose), prose)

    # -- the Linear client ---------------------------------------------------------------
    def test_a_missing_issue_is_not_an_auth_failure(self) -> None:
        class CheckLinear:
            def __init__(self, exc=None):
                self.exc = exc

            def viewer(self):
                return "Zulip Relay"

            def issue_id(self, ident):
                if self.exc is not None:
                    raise self.exc
                return "uuid"

        env = {"LINEAR_RELAY_CHECK_ISSUE": "AFC-9"}
        with redirect_stdout(io.StringIO()):
            code = lr.run_check(StreamsZulip(), CheckLinear(lr.LinearNotFound("AFC-9")), env,
                                "https://z.example")
        self.assertEqual(code, 0)
        with redirect_stdout(io.StringIO()):
            code = lr.run_check(StreamsZulip(), CheckLinear(lr.LinearAuthError("no token")), env,
                                "https://z.example")
        self.assertEqual(code, 1)
        # A malformed id Linear refuses as invalid input is an issue problem, not an auth one.
        out = io.StringIO()
        with redirect_stdout(out):
            code = lr.run_check(StreamsZulip(), CheckLinear(lr.LinearPermanentError("INVALID_INPUT")),
                                env, "https://z.example")
        self.assertEqual(code, 0)
        self.assertIn("the token works", out.getvalue())
        self.assertIn("public-stream set ok: 1 public stream(s)", out.getvalue())

    def test_check_fails_when_the_public_stream_set_cannot_load(self) -> None:
        # run() relays nothing until the set loads, so check tries the same load and fails on it,
        # before it asks Linear anything.
        class NoLinear:
            def viewer(self):
                raise AssertionError("linear must not be asked")

        for answer in (ApiError("denied"), NetworkError("down"), {"no": "streams"}):
            out = io.StringIO()
            with self.subTest(answer=type(answer).__name__), redirect_stdout(out):
                code = lr.run_check(StreamsZulip(answer), NoLinear(), {}, "https://z.example")
            self.assertEqual(code, 1)
            self.assertIn("public-stream set failed to load", out.getvalue())
            # each failure names its kind and its own text, so an auth failure, a network
            # failure and a malformed answer read differently
            expected = {
                "ApiError": "ApiError: denied",
                "NetworkError": "NetworkError: down",
                "dict": "ValueError: ",
            }[type(answer).__name__]
            self.assertIn(expected, out.getvalue())

    def test_linear_error_codes_are_sorted_by_what_waiting_can_change(self) -> None:
        def classify(code: str) -> lr.LinearError:
            return lr.classify_errors([{"message": "x", "extensions": {"code": code}}])

        for code in ("INVALID_INPUT", "FEATURE_NOT_ACCESSIBLE", "USAGE_LIMIT_EXCEEDED",
                     "FORBIDDEN", "INPUT_ERROR", "BAD_USER_INPUT"):
            self.assertIsInstance(classify(code), lr.LinearPermanentError, code)
        self.assertIsInstance(classify("AUTHENTICATION_ERROR"), lr.LinearAuthError)
        self.assertIs(type(classify("RATELIMITED")), lr.LinearRateLimited)
        self.assertIs(type(classify("INTERNAL_ERROR")), lr.LinearError)
        typed = lr.classify_errors([{"message": "x", "extensions": {"type": "invalid input"}}])
        self.assertIsInstance(typed, lr.LinearPermanentError)

    def test_a_transient_error_is_retried_and_a_not_found_is_not(self) -> None:
        sleeps: list[float] = []
        opener = FakeOpener(lambda: http_error(500),
                            {"data": {"viewer": {"id": "u", "name": "Relay"}}})
        client = lr.LinearClient("tok", sleep=sleeps.append, opener=opener)
        self.assertEqual(client.viewer(), "Relay")
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(len(sleeps), 1)

        not_found = json.dumps({"errors": [{
            "message": "Entity not found: Issue",
            "extensions": {"type": "invalid input", "code": "INPUT_ERROR"},
        }]}).encode()
        opener = FakeOpener(lambda: http_error(400, not_found))
        client = lr.LinearClient("tok", sleep=sleeps.append, opener=opener)
        with self.assertRaises(lr.LinearNotFound):
            client.issue_id("AFC-9")
        self.assertEqual(len(opener.requests), 1)  # a not-found is final

    def test_a_create_is_never_sent_twice_by_the_client(self) -> None:
        unmapped = {"errors": [{"message": "internal", "extensions": {"code": "INTERNAL_ERROR"}}]}
        for failure in (lambda: http_error(500), lambda: TimeoutError("slow"),
                        b"<html>gateway</html>", unmapped):
            opener = FakeOpener(failure)
            client = lr.LinearClient("tok", sleep=lambda s: None, opener=opener)
            with self.assertRaises(lr.LinearUncertain):
                client.create_comment("uuid", "body")
            self.assertEqual(len(opener.requests), 1)
        # A rate limit is a definite "not done": sent once, and not read as unknown.
        opener = FakeOpener(lambda: http_error(429))
        client = lr.LinearClient("tok", sleep=lambda s: None, opener=opener)
        with self.assertRaises(lr.LinearRateLimited):
            client.create_comment("uuid", "body")
        self.assertEqual(len(opener.requests), 1)

    def test_a_rate_limited_create_releases_the_pending_entry(self) -> None:
        self.lin.create_error = lr.LinearRateLimited("linear rate limited the request (http 429)")
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        entry = self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")]
        self.assertFalse(entry["pending"])  # nothing landed
        self.assertTrue(entry["dirty"])  # retried later
        self.lin.create_error = None
        self.t[0] = entry["retry_after"] + 1
        self.w.tick()
        self.assertEqual(len(self.lin.created), 1)

    def test_a_malformed_answer_is_a_linear_error(self) -> None:
        for payload in ([], {"no": "data"}, {"data": []}, {"data": {"viewer": "x"}}):
            client = lr.LinearClient("tok", sleep=lambda s: None, opener=FakeOpener(payload))
            with self.assertRaises(lr.LinearError):
                client.viewer()

    def test_the_client_never_follows_a_redirect(self) -> None:
        client = lr.LinearClient("tok")
        self.assertTrue(any(isinstance(h, _NoRedirect) for h in client._opener.handlers))

    def test_requests_carry_a_user_agent(self) -> None:
        opener = FakeOpener({"data": {"viewer": {"id": "u", "name": "Relay"}}})
        lr.LinearClient("tok", opener=opener).viewer()
        self.assertEqual(opener.requests[0].get_header("User-agent"), lr.USER_AGENT)

    def test_an_error_carries_neither_the_body_nor_the_token(self) -> None:
        token = "tok-" + "x" * 12
        body = json.dumps({"errors": [{
            "message": "Entity not found: Issue BODY-TEXT",
            "extensions": {"code": "INPUT_ERROR", "userPresentableMessage": "BODY-TEXT"},
        }]}).encode()
        client = lr.LinearClient(token, opener=FakeOpener(lambda: http_error(400, body)))
        with self.assertRaises(lr.LinearError) as caught:
            client.issue_id("AFC-9")
        self.assertNotIn("BODY-TEXT", str(caught.exception))
        client = lr.LinearClient(token, opener=FakeOpener(lambda: http_error(401, b"BODY-TEXT")))
        with self.assertRaises(lr.LinearAuthError) as caught:
            client.viewer()
        self.assertIn("LINEAR_RELAY_TOKEN", str(caught.exception))
        self.assertNotIn(token, str(caught.exception))
        self.assertNotIn("BODY-TEXT", str(caught.exception))
        # Synthetic only: neither a long-key prefix/suffix nor short-key fragment is output.
        for key in ("tokA" + "1" * 20 + "wxyz", "qRS"):
            for failure in (lambda: http_error(401), {"errors": [{"message": "not authenticated", "extensions": {"code": "AUTHENTICATION_ERROR"}}]}):
                with self.subTest(key_length=len(key)):
                    client = lr.LinearClient(key, opener=FakeOpener(failure))
                    with self.assertRaises(lr.LinearAuthError) as caught:
                        client.viewer()
                    diagnostic = str(caught.exception)
                    exit_text = str(lr._stop_on_auth(caught.exception))
                    for output in (diagnostic, exit_text):
                        self.assertIn("LINEAR_RELAY_TOKEN", output)
                        self.assertNotIn(key, output)
                        self.assertNotIn(key[:8], output)
                        self.assertNotIn(key[-4:], output)
                        self.assertNotIn("key ", output)

    def test_a_refused_lookup_is_logged_not_silent(self) -> None:
        def refused(ident):
            raise lr.LinearPermanentError("linear refused the request (http 403)")

        self.lin.issue_id = refused
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.assertEqual(self.lin.created, [])
        self.assertTrue(any("refused the lookup of AFC-1" in line for line in self.logs))

    def test_a_create_made_while_handling_a_message_shares_the_cycle_budget(self) -> None:
        with mock.patch.object(lr, "TICK_BUDGET", -1.0):
            self.w.start_cycle()
            self.w.handle_event(message(1, "AFC-1 relay", "hello"))
            self.assertEqual(self.lin.created, [])  # the budget is spent: it waits
            self.w.tick()  # closes the spent cycle
        entry = self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")]
        self.assertTrue(entry["dirty"])
        self.w.tick()  # the next cycle has its own budget
        self.assertEqual(len(self.lin.created), 1)

    def test_the_startup_reconcile_shares_the_cycle_budget(self) -> None:
        entry = lr.new_entry("AFC-1", 5, "builds", "AFC-1 relay", 1)
        entry["pending"] = True
        entry["dirty"] = True
        self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")] = entry
        self.lin.by_marker[entry["marker"]] = "c-lost"
        with mock.patch.object(lr, "TICK_BUDGET", -1.0):
            self.w.start_cycle()
            self.w.reconcile()  # the budget is spent: no scan before the first poll
        self.assertIsNone(entry["comment_id"])
        self.assertTrue(entry["pending"])
        self.w.start_cycle()  # run() opens a fresh cycle once the first poll returns
        self.w.tick()  # the next write scans first and adopts, never stacks
        self.assertEqual(entry["comment_id"], "c-lost")
        self.assertEqual(self.lin.created, [])

    def test_the_cycle_budget_ignores_a_wall_clock_step(self) -> None:
        # The budget runs on a monotonic clock: a wall clock stepped back an hour (NTP, a host
        # resume) cannot push the deadline out of reach.
        mono = [100.0]
        w = lr.RelayWorker(
            "https://z.example", "relay@x", self.lin, self.led,
            lr.build_regex(lr.DEFAULT_TEAMS), clock=lambda: self.t[0],
            monotonic=lambda: mono[0], log=self.logs.append, public_streams={5: "builds"},
        )
        w.start_cycle()
        self.t[0] -= 3600  # the wall clock steps back
        self.assertFalse(w._budget_spent())
        mono[0] += lr.TICK_BUDGET + 1
        self.assertTrue(w._budget_spent())

    def test_a_stream_event_routed_to_the_worker_is_logged(self) -> None:
        self.w.handle_event({"type": "stream", "op": "update", "stream_id": 5})
        self.assertTrue(any("set_public_streams" in line for line in self.logs))

    def test_the_stream_event_log_matches_the_ops_run_reloads_on(self) -> None:
        # run() reloads the set only on these ops, so the worker's advice names only these too.
        self.assertEqual(lr.STREAM_RELOAD_OPS, ("create", "update", "delete"))
        for op in lr.STREAM_RELOAD_OPS:
            self.logs.clear()
            self.w.handle_event({"type": "stream", "op": op, "stream_id": 5})
            self.assertEqual(len(self.logs), 1, op)
        self.logs.clear()
        self.w.handle_event({"type": "stream", "op": "some_other_op", "stream_id": 5})
        self.assertEqual(self.logs, [])

    def test_the_marker_matches_a_whole_line_only(self) -> None:
        # The marker of root 100 is a prefix of the marker of root 1000.
        page = {"data": {"issue": {"comments": {
            "nodes": [{"id": "other", "body": lr.marker_for("AFC-1", 1000) + "\nbody"}],
            "pageInfo": {"hasNextPage": False, "endCursor": None},
        }}}}
        client = lr.LinearClient("tok", opener=FakeOpener(page))
        self.assertIsNone(client.find_comment("uuid", lr.marker_for("AFC-1", 100)))
        client = lr.LinearClient("tok", opener=FakeOpener(page))
        self.assertEqual(client.find_comment("uuid", lr.marker_for("AFC-1", 1000)), "other")

    def test_a_bad_cursor_fails_the_scan_instead_of_ending_it(self) -> None:
        page = {"data": {"issue": {"comments": {
            "nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": None},
        }}}}
        client = lr.LinearClient("tok", opener=FakeOpener(page))
        with self.assertRaises(lr.LinearError):
            client.find_comment("uuid", lr.marker_for("AFC-1", 1))

    def test_the_comment_scan_shares_one_deadline(self) -> None:
        now = [0.0]

        def slow_page():
            now[0] += 40.0
            return {"data": {"issue": {"comments": {
                "nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": "next"},
            }}}}

        opener = FakeOpener(slow_page)
        client = lr.LinearClient("tok", opener=opener, monotonic=lambda: now[0],
                                 sleep=lambda s: None)
        with self.assertRaises(lr.LinearError):
            client.find_comment("uuid", lr.marker_for("AFC-1", 1))
        self.assertEqual(len(opener.requests), 3)  # 90 seconds for the whole scan

    def test_a_realm_that_does_not_match_the_site_names_the_variable(self) -> None:
        env = {"ZULIP_RELAY_EMAIL": "relay@x", "ZULIP_RELAY_API_KEY": "fake",
               "ZULIP_SITE": "https://other.example", "SIMPLE_TOOLS_REALM": "https://z.example"}
        with self.assertRaises(SystemExit) as caught:
            lr.run(env)
        self.assertIn("SIMPLE_TOOLS_REALM", str(caught.exception))

    def test_the_default_teams_match_the_linkifier(self) -> None:
        import zulip_linkifiers

        self.assertEqual(set(lr.DEFAULT_TEAMS), set(zulip_linkifiers.LINEAR_TEAMS))


class WorkerCase(unittest.TestCase):
    """A fresh ledger, a faked Linear and a scripted topic, for the AFC-821 tests below."""

    def setUp(self) -> None:
        self.t = [1000.0]
        self.lin = FakeLinear()
        self.logs: list[str] = []
        self.led = lr.Ledger(Path(tempfile.mkdtemp()) / "state.json")
        self.led.open()
        self.topic = Topic()

    def worker(self, **readers) -> lr.RelayWorker:
        """A worker on the ledger.  A second one on the same ledger is a restart: it starts with
        the ledger as the first one left it, and with nothing in memory.  Both of its clocks are
        the test's, so a test moves the retry interval along with the time."""
        return lr.RelayWorker(
            "https://z.example", "relay@x", self.lin, self.led,
            lr.build_regex(lr.DEFAULT_TEAMS), clock=lambda: self.t[0],
            monotonic=lambda: self.t[0], log=self.logs.append,
            public_streams={5: "builds", 6: "deploys"}, **readers,
        )

    def key(self, issue: str, sid: int, topic: str) -> str:
        return lr.ledger_key(issue, sid, topic)

    def prune(self, count: int = 1) -> None:
        """Save with the entry bound `count` under what the ledger holds, so the oldest entries
        are pruned exactly as MAX_ENTRIES prunes them in production."""
        with mock.patch.object(lr, "MAX_ENTRIES", len(self.led.entries) - count):
            self.led.save()


class ArchiveTests(WorkerCase):
    """AFC-821, the comment map.  Pruning used to delete an entry whole, so the topic's next
    mention started a second comment under a new marker, and a message that did not name the
    issue recorded nothing.  Now a pruned entry leaves its root and comment id in the ledger's
    archive, and the topic's next message restores it."""

    def setUp(self) -> None:
        super().setUp()
        self.w = self.worker(history=self.topic.whole)

    # -- pruning keeps the map -------------------------------------------------------------
    def test_pruning_moves_a_commented_entry_into_the_archive(self) -> None:
        self.w.handle_event(message(1, "AFC-1 old", "hello"))
        self.t[0] += 10
        self.w.handle_event(message(2, "AFC-1 new", "hello"))
        with mock.patch.object(lr, "MAX_ENTRIES", 1):
            self.led.save()
        old = self.key("AFC-1", 5, "AFC-1 old")
        self.assertEqual(list(self.led.entries), [self.key("AFC-1", 5, "AFC-1 new")])  # bounded
        # The record is the root and the comment id, and no topic text, stream name or quote.
        self.assertEqual(
            self.led.archive,
            {old: {"root": 1, "comment_id": "c1", "pending": False, "at": 1000.0}},
        )
        text = self.led.path.read_text("utf-8")
        self.assertEqual(json.loads(text)["archive"], self.led.archive)
        self.assertNotIn("AFC-1 old", text)
        # The file round-trips: a restart reads the same map back, index included.
        again = lr.Ledger(Path(tempfile.mkdtemp()) / "copy.json")
        again.path.write_text(text, "utf-8")
        again.open()
        self.assertEqual(again.archive, self.led.archive)
        self.assertEqual(again.entries, self.led.entries)
        self.assertEqual(again.archived_for(5, lr.topic_digest("AFC-1 old")), [old])

    def test_a_pruned_entry_without_a_comment_leaves_no_record(self) -> None:
        # Linear has no AFC-7, so the entry never got a comment: there is nothing to find again,
        # and the entry is simply dropped, as it always was.
        self.w.handle_event(message(1, "AFC-7 early", "hello"))
        self.assertEqual(len(self.led.entries), 1)
        self.prune()
        self.assertEqual(json.loads(self.led.path.read_text("utf-8")),
                         {"version": lr.LEDGER_VERSION, "entries": {}})

    # -- the next message restores the entry -----------------------------------------------
    def test_a_pruned_topic_that_names_its_issue_again_edits_the_same_comment(self) -> None:
        self.topic.messages = [past(1, "setting up"), past(2, "still no issue")]
        self.w.handle_event(message(3, "deploy thread", "this is AFC-1"))
        marker = lr.marker_for("AFC-1", 3)
        self.assertEqual(first_line(self.lin.created[0][1]), marker)
        self.prune()
        self.assertEqual(self.led.entries, {})
        self.topic.messages += [past(3, "this is AFC-1"), past(4, "said while it was pruned")]
        self.t[0] += 86400
        self.w.handle_event(message(5, "deploy thread", "back to AFC-1"))
        self.assertEqual(self.lin.create_calls, 1)  # no second comment
        self.assertEqual([cid for cid, _body in self.lin.updated], ["c1"])  # the old one, edited
        body = self.lin.updated[0][1]
        self.assertEqual(first_line(body), marker)  # under the marker it always had
        entry = self.led.entries[self.key("AFC-1", 5, "deploy thread")]
        self.assertEqual((entry["root"], entry["comment_id"]), (3, "c1"))
        self.assertEqual(self.led.archive, {})  # the record left the archive with the entry
        # Backfilled from the whole topic before the live message, as a new entry is.
        self.assertEqual(entry["seen"], [1, 2, 3, 4, 5])
        for mid in (1, 2, 3, 4, 5):
            self.assertIn("near/%d" % mid, body)
        restored = [line for line in self.logs if "restored the relay entry" in line]
        self.assertEqual(restored, ["restored the relay entry for AFC-1 (conversation 3) from "
                                    "the archive"])

    def test_a_message_that_does_not_name_the_issue_restores_a_pruned_topic_too(self) -> None:
        self.topic.messages = [past(1, "this is AFC-1")]
        self.w.handle_event(message(1, "deploy thread", "this is AFC-1"))
        self.prune()
        self.t[0] += 86400
        self.topic.messages.append(past(2, "no key in this one"))
        self.w.handle_event(message(2, "deploy thread", "no key in this one"))
        self.assertEqual([cid for cid, _body in self.lin.updated], ["c1"])  # the standing comment
        body = self.lin.updated[0][1]
        self.assertEqual(first_line(body), lr.marker_for("AFC-1", 1))
        # Refilled before the edit: the comment keeps what it listed and gains the new message.
        self.assertIn("near/1", body)
        self.assertIn("near/2", body)
        self.assertEqual(self.lin.create_calls, 1)
        # A topic the archive does not know still records nothing until a real mention.
        self.w.handle_event(message(3, "another thread", "no key here either"))
        self.assertEqual(list(self.led.entries), [self.key("AFC-1", 5, "deploy thread")])

    def test_a_pruned_uncertain_create_is_adopted_by_its_original_marker(self) -> None:
        # The create landed, its answer was lost, and the entry was pruned while still pending.
        self.lin.create_error = lr.LinearUncertain("linear create outcome unknown")
        self.lin.create_lands = True
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.lin.create_error = None
        self.prune()
        self.assertEqual(self.led.entries, {})
        self.t[0] += 86400
        self.w.handle_event(message(2, "AFC-1 relay", "again"))
        entry = self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")]
        # Found by the marker of root 1, not looked for (and missed) under a marker of root 2.
        self.assertEqual(entry["comment_id"], "c-landed")
        self.assertEqual(entry["marker"], lr.marker_for("AFC-1", 1))
        self.assertFalse(entry["pending"])
        self.assertEqual(self.lin.create_calls, 1)  # never created a second time
        # The adopted comment shows what the lost create wrote, not the message that brought it
        # back: the entry stays dirty, and the next tick edits the adopted comment.
        self.assertTrue(entry["dirty"])
        self.w.tick()
        self.assertEqual([cid for cid, _body in self.lin.updated], ["c-landed"])
        self.assertIn("near/2", self.lin.updated[0][1])

    def test_a_restored_entry_whose_comment_is_gone_recreates_it_once(self) -> None:
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.prune()
        self.lin.by_marker.clear()  # the comment was deleted on the Linear side
        self.lin.update_error = lr.LinearNotFound("linear: not found")
        self.t[0] += 86400
        self.w.handle_event(message(2, "AFC-1 relay", "again"))
        self.assertEqual(len(self.lin.created), 2)
        self.assertEqual(first_line(self.lin.created[1][1]), lr.marker_for("AFC-1", 1))
        entry = self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")]
        self.assertEqual(entry["comment_id"], "c2")
        # Exactly once: the next message edits the recreated comment and creates nothing.
        self.lin.update_error = None
        self.t[0] += 700
        self.w.handle_event(message(3, "AFC-1 relay", "more"))
        self.assertEqual(len(self.lin.created), 2)
        self.assertEqual(self.lin.updated[-1][0], "c2")

    def test_a_restore_whose_history_cannot_be_read_leaves_the_comment_as_it_is(self) -> None:
        self.topic.messages = [past(mid, "m%d" % mid) for mid in (1, 2, 3)]
        for mid in (1, 2, 3):
            self.w.handle_event(message(mid, "AFC-1 relay", "m%d" % mid))
        key = self.key("AFC-1", 5, "AFC-1 relay")
        self.prune()
        self.topic.whole_error = OSError("zulip is down")
        self.topic.messages.append(past(4, "m4"))
        self.t[0] += 86400
        reads = self.topic.whole_calls
        self.w.handle_event(message(4, "AFC-1 relay", "m4"))
        self.w.handle_event(message(4, "AFC-1 relay", "m4"))  # the same message, another seat
        # Restored without its history, the entry would rewrite the comment down to message 4.
        # It waits instead: nothing is written, and the record stays in the archive.
        self.assertEqual(self.lin.updated, [])
        self.assertEqual(list(self.led.archive), [key])
        self.assertEqual(self.led.entries, {})
        self.assertEqual(self.topic.whole_calls, reads + 1)  # one read, not one per feed
        failed = [line for line in self.logs if "its archived comment is left as it is" in line]
        self.assertEqual(len(failed), 1)
        self.assertIn("(OSError)", failed[0])
        # Inside the retry interval a further message reads nothing either.
        self.topic.messages.append(past(5, "m5"))
        self.t[0] += 10
        self.w.handle_event(message(5, "AFC-1 relay", "m5"))
        self.assertEqual(self.topic.whole_calls, reads + 1)
        # Later, with the read working, the next message restores the entry with the whole
        # topic, the messages that waited included, and edits the one comment.
        self.topic.whole_error = None
        self.topic.messages.append(past(6, "m6"))
        self.t[0] += lr.CATCH_UP_RETRY
        self.w.handle_event(message(6, "AFC-1 relay", "m6"))
        self.assertEqual(self.lin.create_calls, 1)
        self.assertEqual([cid for cid, _body in self.lin.updated], ["c1"])
        self.assertEqual(self.led.entries[key]["seen"], [1, 2, 3, 4, 5, 6])
        for mid in range(1, 7):
            self.assertIn("near/%d" % mid, self.lin.updated[0][1])

    def test_a_restore_puts_its_entry_in_place_before_anything_can_fail(self) -> None:
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.prune()

        def log(line: str) -> None:
            if "restored" in line:
                raise OSError("stderr is closed")
            self.logs.append(line)

        self.w.log = log
        self.t[0] += 86400
        with self.assertRaises(OSError):
            self.w.handle_event(message(2, "AFC-1 relay", "again"))
        # The record left the archive and its entry took its place, so no later save can lose
        # the comment and stack a second one at the topic's next mention.
        self.assertEqual(self.led.archive, {})
        entry = self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")]
        self.assertEqual((entry["root"], entry["comment_id"]), (1, "c1"))

    # -- renames and moves ---------------------------------------------------------------
    def test_a_topic_rename_carries_the_archived_record(self) -> None:
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.prune()
        self.w.handle_event(move(5, "AFC-1 relay", subject="AFC-1 relay renamed"))
        self.t[0] += 86400
        self.w.handle_event(message(2, "AFC-1 relay renamed", "again"))
        self.assertEqual(self.lin.create_calls, 1)  # no second comment under the new name
        self.assertEqual([cid for cid, _body in self.lin.updated], ["c1"])
        self.assertEqual(first_line(self.lin.updated[0][1]), lr.marker_for("AFC-1", 1))
        self.assertEqual(list(self.led.entries), [self.key("AFC-1", 5, "AFC-1 relay renamed")])
        self.assertEqual(self.led.archive, {})

    def test_a_rename_that_newly_names_an_archived_issue_waits_for_the_next_message(self) -> None:
        self.topic.messages = [past(1, "this is AFC-1"), past(2, "m2"), past(3, "m3")]
        for item in self.topic.messages:
            self.w.handle_event(message(item["id"], "discussion", item["content"]))
        self.prune()
        self.t[0] += 86400
        self.w.handle_event(move(5, "discussion", subject="AFC-1 discussion", ids=[1, 2, 3]))
        # The record moves with the topic, and nothing is written: restored by the first moved
        # message, the entry would have no history before it, and its edit would cut the
        # comment down to one "topic rename" bullet.
        key = self.key("AFC-1", 5, "AFC-1 discussion")
        self.assertEqual(list(self.led.archive), [key])
        self.assertEqual((self.lin.create_calls, self.lin.updated), (1, []))
        self.topic.messages.append(past(4, "m4"))
        self.w.handle_event(message(4, "AFC-1 discussion", "m4"))
        self.assertEqual([cid for cid, _body in self.lin.updated], ["c1"])
        body = self.lin.updated[0][1]
        for mid in (1, 2, 3, 4):
            self.assertIn("near/%d" % mid, body)
        self.assertNotIn("topic rename", body)

    def test_a_move_between_public_streams_carries_the_archived_record(self) -> None:
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.prune()
        self.w.handle_event(move(5, "AFC-1 relay", new_sid=6, subject="AFC-1 moved"))
        self.t[0] += 86400
        self.w.handle_event(message(2, "AFC-1 moved", "again", stream_id=6))
        self.assertEqual(self.lin.create_calls, 1)
        self.assertEqual([cid for cid, _body in self.lin.updated], ["c1"])
        entry = self.led.entries[self.key("AFC-1", 6, "AFC-1 moved")]
        self.assertEqual((entry["root"], entry["stream_id"]), (1, 6))
        self.assertEqual(self.led.archive, {})

    def test_a_partial_move_leaves_the_archived_record_with_the_old_topic(self) -> None:
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.w.handle_event(message(2, "AFC-1 relay", "again"))
        self.prune()
        self.w.handle_event(move(5, "AFC-1 relay", subject="AFC-1 split", mode="change_one",
                                 ids=[2]))
        self.t[0] += 86400
        self.w.handle_event(message(3, "AFC-1 relay", "more"))
        self.assertEqual(self.lin.create_calls, 1)  # the old topic keeps its one comment
        self.assertEqual([cid for cid, _body in self.lin.updated], ["c1"])
        self.assertNotIn(self.key("AFC-1", 5, "AFC-1 split"), self.led.entries)

    def test_an_archived_record_that_moves_onto_a_live_entry_gives_way(self) -> None:
        self.w.handle_event(message(1, "AFC-1 a", "one"))
        self.prune()
        self.t[0] += 10
        self.w.handle_event(message(2, "AFC-1 b", "two"))
        self.w.handle_event(move(5, "AFC-1 a", subject="AFC-1 b"))
        # One record for (AFC-1, the stream, topic b): the live one, with its own comment.
        self.assertEqual(self.led.archive, {})
        self.assertEqual(list(self.led.entries), [self.key("AFC-1", 5, "AFC-1 b")])
        self.assertEqual(self.led.entries[self.key("AFC-1", 5, "AFC-1 b")]["comment_id"], "c2")
        self.assertTrue(any("merged the archived AFC-1 (conversation 1)" in line
                            for line in self.logs))

    def test_a_live_entry_that_moves_onto_an_archived_record_replaces_it(self) -> None:
        self.w.handle_event(message(1, "AFC-1 b", "one"))
        self.prune()
        self.t[0] += 10
        self.w.handle_event(message(2, "AFC-1 a", "two"))
        self.w.handle_event(move(5, "AFC-1 a", subject="AFC-1 b", ids=[2]))
        self.assertEqual(self.led.archive, {})
        self.assertEqual(list(self.led.entries), [self.key("AFC-1", 5, "AFC-1 b")])
        entry = self.led.entries[self.key("AFC-1", 5, "AFC-1 b")]
        self.assertEqual((entry["root"], entry["comment_id"]), (2, "c2"))

    def test_two_archived_records_that_meet_leave_the_destinations(self) -> None:
        self.w.handle_event(message(1, "AFC-1 a", "one"))
        self.t[0] += 10
        self.w.handle_event(message(2, "AFC-1 b", "two"))
        self.prune(2)
        self.w.handle_event(move(5, "AFC-1 a", subject="AFC-1 b"))
        kept = self.key("AFC-1", 5, "AFC-1 b")
        self.assertEqual(list(self.led.archive), [kept])
        self.assertEqual(self.led.archive[kept]["comment_id"], "c2")
        self.assertEqual(self.led.archived_for(5, lr.topic_digest("AFC-1 a")), [])
        self.assertEqual(self.led.archived_for(5, lr.topic_digest("AFC-1 b")), [kept])

    # -- private streams -------------------------------------------------------------------
    def test_a_topic_that_moves_to_a_private_stream_drops_its_archived_record(self) -> None:
        self.w.handle_event(message(1, "deploy thread", "this is AFC-1"))
        self.prune()
        self.w.handle_event(move(5, "deploy thread", new_sid=9))
        self.assertEqual(self.led.archive, {})
        self.assertTrue(any("closed the archived relay record for AFC-1" in line
                            for line in self.logs))
        # Nothing is restored or written: not where the topic went, and not where it was.
        self.t[0] += 86400
        self.w.handle_event(message(2, "deploy thread", "no key in this one", stream_id=9))
        self.w.handle_event(message(3, "deploy thread", "nor in this one"))
        self.w.tick()
        self.assertEqual((self.lin.create_calls, self.lin.updated), (1, []))
        self.assertEqual(self.led.entries, {})

    def test_a_stream_that_turns_private_drops_its_archived_records(self) -> None:
        self.w.handle_event(message(1, "deploy thread", "this is AFC-1"))
        self.prune()
        key = self.key("AFC-1", 5, "deploy thread")
        self.w.set_public_streams(None)  # unloaded proves nothing about any stream
        self.w.tick()
        self.w.reconcile()
        self.assertEqual(list(self.led.archive), [key])
        self.w.set_public_streams({6: "deploys"})  # loaded, and the stream is not public now
        self.w.tick()
        self.assertEqual(self.led.archive, {})
        self.assertIn("dropped 1 archived relay record(s): their stream is no longer public",
                      self.logs)
        # Public again later: the old comment is not found again, as a live entry's is not.
        self.w.set_public_streams({5: "builds", 6: "deploys"})
        self.t[0] += 86400
        self.w.handle_event(message(2, "deploy thread", "no key in this one"))
        self.w.tick()
        self.assertEqual((self.lin.create_calls, self.lin.updated), (1, []))
        self.assertEqual(self.led.entries, {})

    def test_the_startup_reconcile_drops_a_private_streams_archived_records(self) -> None:
        self.w.handle_event(message(1, "deploy thread", "this is AFC-1"))
        self.prune()
        self.w.set_public_streams({6: "deploys"})
        self.w.reconcile()
        self.assertEqual(self.led.archive, {})

    def test_a_tick_drops_what_its_own_save_archived_for_a_private_stream(self) -> None:
        # An entry that needs a create, walked first; then an older commented entry of stream
        # 6, which has just turned private.  The create's write-ahead save prunes the older
        # entry into the archive after the tick's first sweep has run.
        fresh = lr.new_entry("AFC-1", 5, "builds", "AFC-1 new", 2)
        fresh.update(dirty=True, last_touch=2000.0)
        self.led.entries[self.key("AFC-1", 5, "AFC-1 new")] = fresh
        old = lr.new_entry("AFC-1", 6, "deploys", "AFC-1 old", 1)
        old.update(comment_id="c-old", last_touch=1000.0)
        self.led.entries[self.key("AFC-1", 6, "AFC-1 old")] = old
        self.w.set_public_streams({5: "builds"})
        with mock.patch.object(lr, "MAX_ENTRIES", 1):
            self.w.tick()
        self.assertEqual(self.lin.create_calls, 1)
        # Dropped in the same tick, as the live rule drops the entry, so no record of a private
        # stream outlives the tick, even if the stream is made public again before the next.
        self.assertEqual(self.led.archive, {})
        self.assertIn("dropped 1 archived relay record(s): their stream is no longer public",
                      self.logs)
        self.assertNotIn("archive", json.loads(self.led.path.read_text("utf-8")))

    # -- the bounds ------------------------------------------------------------------------
    def test_the_archive_is_bounded_and_the_log_says_what_it_dropped(self) -> None:
        keys = [self.key("AFC-1", 5, "topic %d" % i) for i in range(4)]
        for i, key in enumerate(keys):
            self.led.archive_put(key, {"root": i + 1, "comment_id": "c%d" % (i + 1),
                                       "pending": False, "at": float(i)})
        self.led.mark_dirty()
        with mock.patch.object(lr, "MAX_ARCHIVE", 2):
            self.w.tick()
        self.assertEqual(sorted(self.led.archive), sorted(keys[2:]))  # the two oldest went
        self.assertEqual(self.led.archive_dropped, 2)
        # The index follows: a dropped record is not found, a kept one is.
        self.assertEqual(self.led.archived_for(5, lr.topic_digest("topic 0")), [])
        self.assertEqual(self.led.archived_for(5, lr.topic_digest("topic 3")), [keys[3]])
        said = [line for line in self.logs if "dropped 2 archived record(s)" in line]
        self.assertEqual(len(said), 1)
        self.assertFalse(any(key in line for key in keys for line in self.logs))  # a count only
        self.logs.clear()
        self.led.mark_dirty()
        self.w.tick()
        self.assertEqual(self.logs, [])  # said once, not at every save

    def test_the_byte_bound_still_converges_with_an_archive(self) -> None:
        for mid, name in ((1, "AFC-1 a"), (2, "AFC-1 b"), (3, "AFC-1 c")):
            self.w.handle_event(message(mid, name, "hello"))
            self.t[0] += 10
        keys = [self.key("AFC-1", 5, name) for name in ("AFC-1 a", "AFC-1 b", "AFC-1 c")]
        self.led.save()
        whole = len(self.led.path.read_bytes())
        # One byte too many: the oldest entry gives way, and it goes to the archive.
        with mock.patch.object(lr, "MAX_LEDGER_BYTES", whole - 1):
            self.led.save()
        self.assertEqual(list(self.led.entries), keys[1:])
        self.assertEqual(list(self.led.archive), keys[:1])
        self.assertLess(len(self.led.path.read_bytes()), whole)
        # Room for one record and no entry, measured from a saved ledger that holds just the
        # newest entry's record, so the bound sits between one record and two whatever size a
        # record renders to.  Every entry is archived, then the oldest records are dropped
        # until the file fits.
        newest = self.led.entries[keys[2]]
        one = lr.Ledger(Path(tempfile.mkdtemp()) / "one.json")
        one.open()
        one.archive_put(keys[2], {"root": newest["root"], "comment_id": newest["comment_id"],
                                  "pending": newest["pending"], "at": newest["last_touch"]})
        one.save()
        room = len(one.path.read_bytes()) + 10
        with mock.patch.object(lr, "MAX_LEDGER_BYTES", room):
            self.led.save()
        self.assertEqual(self.led.entries, {})
        self.assertEqual(list(self.led.archive), keys[2:])  # the newest record is the one kept
        self.assertEqual(self.led.archive_dropped, 2)
        self.assertLessEqual(len(self.led.path.read_bytes()), room)
        # A bound nothing fits in still ends, with nothing left to drop.
        with mock.patch.object(lr, "MAX_LEDGER_BYTES", 1):
            self.led.save()
        self.assertEqual((self.led.entries, self.led.archive), ({}, {}))

    def whole_renders(self, spy: mock.MagicMock) -> int:
        """How many times a save under `spy` rendered the whole ledger."""
        return sum(1 for call in spy.dumps.call_args_list
                   if call.args and call.args[0] is self.led.data)

    def test_the_byte_bound_renders_the_ledger_once_a_round_not_once_an_entry(self) -> None:
        # Kody, PR 2298: every entry the byte bound retired rendered the whole ledger again,
        # which at 8 MB holds the routing thread for seconds.  The same entries go as when
        # they went one at a time, and the ledger is rendered once a round.
        names = ["AFC-1 topic %02d" % mid for mid in range(1, 13)]
        for mid, name in enumerate(names, 1):
            self.w.handle_event(message(mid, name, "hello " * 40))
            self.t[0] += 10
        self.led.save()
        bound = len(self.led.path.read_bytes()) // 3
        # The one-at-a-time rule, on a copy: retire the oldest, render, and again.
        ref = lr.Ledger(Path(tempfile.mkdtemp()) / "ref.json")
        ref.open()
        ref.data = json.loads(json.dumps(self.led.data))
        ref._index_archive()
        with mock.patch.object(lr, "MAX_ARCHIVE", 3):
            while len(json.dumps(ref.data, indent=1, sort_keys=True)) > bound:
                ref._retire(min(ref.entries, key=lambda k: ref.entries[k]["last_touch"]))
                ref._prune_archive()
            with mock.patch.object(lr, "MAX_LEDGER_BYTES", bound):
                with mock.patch.object(lr, "json", wraps=json) as spy:
                    self.led.save()
        retired = len(names) - len(ref.entries)
        self.assertTrue(6 <= retired < len(names))
        self.assertEqual(list(self.led.entries), list(ref.entries))  # the newest, as before
        self.assertEqual(sorted(self.led.archive), sorted(ref.archive))
        self.assertEqual(self.led.archive_dropped, ref.archive_dropped)  # full archive: credited
        self.assertLessEqual(len(self.led.path.read_bytes()), bound)
        # One render over the bound and one after the round, where one render for every entry
        # retired made seven or more.
        self.assertEqual(self.whole_renders(spy), 2)

    def test_with_no_entry_left_the_archive_is_cut_once_a_round_too(self) -> None:
        keys = [self.key("AFC-1", 5, "topic %d" % i) for i in range(40)]
        for i, key in enumerate(keys):
            self.led.archive_put(key, {"root": i + 1, "comment_id": "c%d" % (i + 1),
                                       "pending": False, "at": float(i)})
        self.led.save()
        bound = len(self.led.path.read_bytes()) // 4
        with mock.patch.object(lr, "MAX_LEDGER_BYTES", bound):
            with mock.patch.object(lr, "json", wraps=json) as spy:
                self.led.save()
        kept = len(self.led.archive)
        self.assertEqual(sorted(self.led.archive), sorted(keys[len(keys) - kept:]))  # newest
        self.assertEqual(self.led.archive_dropped, len(keys) - kept)
        self.assertLessEqual(len(self.led.path.read_bytes()), bound)
        # No record went that the bound did not need gone: with the newest dropped one back,
        # the file would not fit.
        back = json.loads(json.dumps(self.led.data))
        back["archive"][keys[len(keys) - kept - 1]] = {
            "root": len(keys) - kept, "comment_id": "c%d" % (len(keys) - kept),
            "pending": False, "at": float(len(keys) - kept - 1),
        }
        self.assertGreater(len(json.dumps(back, indent=1, sort_keys=True)), bound)
        self.assertGreater(self.led.archive_dropped, 20)
        self.assertEqual(self.whole_renders(spy), 2)  # not one more for every record dropped

    # -- the file --------------------------------------------------------------------------
    def test_a_ledger_without_an_archive_opens_and_saves_as_before(self) -> None:
        key = self.key("AFC-1", 5, "AFC-1 relay")
        entry = lr.new_entry("AFC-1", 5, "builds", "AFC-1 relay", 1)
        entry["comment_id"] = "c1"
        data = {"version": lr.LEDGER_VERSION, "entries": {key: entry}}
        path = Path(tempfile.mkdtemp()) / "before.json"
        path.write_text(json.dumps(data), "utf-8")
        led = lr.Ledger(path)
        led.open()
        self.assertEqual(led.entries, {key: entry})
        led.save()
        self.assertEqual(json.loads(path.read_text("utf-8")), data)  # no archive table appears

    def test_a_malformed_archive_stops_the_relay_without_echoing_it(self) -> None:
        digest = lr.topic_digest("AFC-1 relay")
        key = self.key("AFC-1", 5, "AFC-1 relay")
        good = {"root": 1, "comment_id": "comment-id-in-the-file", "pending": False, "at": 5.0}

        def without(field: str) -> dict:
            return {name: value for name, value in good.items() if name != field}

        cases = (
            [],  # not a table
            None,
            {"no separators": good},
            {"AFC-1|five|%s" % digest: good},  # the stream id is not an integer
            {"AFC-1|05|%s" % digest: good},  # not the key ledger_key writes: never looked up
            {"AFC-1|5|%s" % digest.upper(): good},  # not a lowercase hex digest
            {"AFC-1|5|abc": good},
            {key: "not a record"},
            {key: {**good, "root": "1"}},
            {key: {**good, "root": True}},
            {key: {**good, "comment_id": ""}},
            {key: {**good, "comment_id": 7}},
            {key: {**good, "pending": "no"}},
            {key: {**good, "at": "yesterday"}},
            {key: without("at")},
            # Every field must be there: a record with no comment id would pass for one whose
            # create never landed, and its restore would fail after it left the archive.
            {key: without("comment_id")},
            {key: without("root")},
            {key: without("pending")},
            # A time the archive cannot be ordered by: the bounds would raise on it, or order
            # the records arbitrarily, at every save.
            {key: {**good, "at": float("nan")}},
            {key: {**good, "at": float("inf")}},
            {key: {**good, "at": 10**400}},
        )
        base = Path(tempfile.mkdtemp())
        for number, archive in enumerate(cases):
            # A file of its own for each case, so no case can pass on another's held lock.
            path = base / ("case-%d.json" % number)
            path.write_text(json.dumps(
                {"version": lr.LEDGER_VERSION, "entries": {}, "archive": archive}), "utf-8")
            with self.subTest(case=number):
                with self.assertRaises(SystemExit) as caught:
                    lr.Ledger(path).open()
                said = str(caught.exception)
                self.assertIn("archive", said)
                # The position at most: never the key, the issue, the digest or a field.
                for secret in ("AFC-1", digest, digest.upper(), "comment-id-in-the-file",
                               "no separators", "yesterday"):
                    self.assertNotIn(secret, said)
        # The same record, well formed, opens.
        path = base / "good.json"
        path.write_text(json.dumps(
            {"version": lr.LEDGER_VERSION, "entries": {}, "archive": {key: good}}), "utf-8")
        led = lr.Ledger(path)
        led.open()
        self.assertEqual(led.archive, {key: good})


class CatchUpTests(WorkerCase):
    """AFC-821, the restart.  A message posted while the worker was not running never reached
    it, and an existing comment missed it for good.  Given the catch-up reader, the first
    message that reaches a standing entry after a restart records what the topic got in the
    gap, once each, before the live one."""

    def restart(self) -> lr.RelayWorker:
        return self.worker(history=self.topic.whole, history_since=self.topic.since)

    def entry(self, issue: str = "AFC-1", topic: str = "AFC-1 relay") -> dict:
        return self.led.entries[self.key(issue, 5, topic)]

    def test_a_restart_records_what_the_topic_got_while_the_worker_was_down(self) -> None:
        first = self.restart()
        first.handle_event(message(1, "AFC-1 relay", "hello"))
        # Down: two plain messages and one of the writer's own land in the topic.
        self.topic.messages = [
            past(1, "hello"), past(2, "missed one"), past(3, "the relay's own", sender="relay@x"),
            past(4, "missed two"), past(5, "live again"),
        ]
        second = self.restart()
        self.t[0] += 3600
        second.handle_event(message(5, "AFC-1 relay", "live again"))
        entry = self.entry()
        self.assertEqual(entry["seen"], [1, 2, 4, 5])  # once each, before the live one
        self.assertEqual(entry["count"], 4)
        self.assertEqual([item["mid"] for item in entry["links"]], [1, 2, 4, 5])
        # One read, from the newest message the entry had seen, and no whole-topic read.
        self.assertEqual(self.topic.since_calls, [(5, "AFC-1 relay", 1)])
        self.assertEqual(self.topic.whole_calls, 1)  # the first worker's, for the new entry
        # A missed message keeps its own time, not the time it was caught up at.
        self.assertEqual(entry["links"][1]["when"],
                         time.strftime("%b %d %H:%M UTC", time.gmtime(902)))
        self.assertEqual(self.lin.create_calls, 1)
        body = self.lin.updated[-1][1]
        self.assertIn("near/2", body)
        self.assertIn("near/4", body)
        self.assertNotIn("near/3", body)  # the writer's own post is skipped, as on the live path
        self.assertIn("caught up 2 missed message(s) for AFC-1 (conversation 1)", self.logs)
        # Caught up: the next message reads nothing.
        second.handle_event(message(6, "AFC-1 relay", "and on"))
        self.assertEqual(len(self.topic.since_calls), 1)
        self.assertEqual(entry["seen"], [1, 2, 4, 5, 6])

    def test_a_message_the_entry_has_seen_is_not_counted_twice(self) -> None:
        first = self.restart()
        first.handle_event(message(1, "AFC-1 relay", "hello"))
        first.handle_event(message(2, "AFC-1 relay", "again"))
        self.topic.messages = [past(1, "hello"), past(2, "again"), past(3, "missed"),
                               past(4, "live")]
        self.topic.sloppy = True  # the reader answers with messages the entry already has
        second = self.restart()
        second.handle_event(message(4, "AFC-1 relay", "live"))
        self.assertEqual(self.entry()["seen"], [1, 2, 3, 4])
        self.assertEqual(self.entry()["count"], 4)
        # A host that replays a message the catch-up recorded changes nothing either.
        second.handle_event(message(3, "AFC-1 relay", "missed"))
        self.assertEqual(self.entry()["count"], 4)

    def test_a_failed_catch_up_read_is_logged_and_tried_again(self) -> None:
        first = self.restart()
        first.handle_event(message(1, "AFC-1 relay", "hello"))
        self.topic.messages = [past(mid, "m%d" % mid) for mid in (1, 2, 3, 4, 5)]
        self.topic.error = RuntimeError("zulip is down, and this text stays out of the log")
        second = self.restart()
        second.handle_event(message(3, "AFC-1 relay", "m3"))
        self.assertEqual(self.entry()["seen"], [1, 3])  # the live message is recorded all the same
        failed = [line for line in self.logs if "could not be read" in line]
        self.assertEqual(len(failed), 1)
        self.assertIn("(RuntimeError)", failed[0])
        self.assertFalse(any("stays out of the log" in line for line in self.logs))
        # The same message fed again for another seat, and the next message inside the retry
        # interval, read nothing: a failing read is not repeated for every feed, on the
        # host's routing thread.
        second.handle_event(message(3, "AFC-1 relay", "m3"))
        self.t[0] += 10
        second.handle_event(message(4, "AFC-1 relay", "m4"))
        self.assertEqual(len(self.topic.since_calls), 1)
        self.topic.error = None
        self.t[0] += lr.CATCH_UP_RETRY
        second.handle_event(message(5, "AFC-1 relay", "m5"))
        # Tried again from the same point, so the missed message is recovered, and in id order.
        self.assertEqual([after for _sid, _topic, after in self.topic.since_calls], [1, 1])
        self.assertEqual(self.entry()["seen"], [1, 2, 3, 4, 5])
        self.assertEqual([item["mid"] for item in self.entry()["links"]], [1, 2, 3, 4, 5])
        self.assertEqual(self.entry()["count"], 5)
        second.handle_event(message(6, "AFC-1 relay", "and on"))
        self.assertEqual(len(self.topic.since_calls), 2)  # settled: no third read

    def test_a_retried_read_counts_each_message_once_however_many_came_live(self) -> None:
        # More live messages arrive while the read keeps failing than the entry's `seen` list
        # holds, so `seen` alone can no longer tell which of them are recorded.
        with mock.patch.object(lr, "MAX_SEEN", 4):
            first = self.restart()
            first.handle_event(message(1, "AFC-1 relay", "m1"))
            self.topic.messages = [past(mid, "m%d" % mid) for mid in range(1, 14)]
            self.topic.error = RuntimeError("zulip history is down")
            second = self.restart()
            for mid in range(3, 13):  # 2 was missed; 3 to 12 arrive live
                second.handle_event(message(mid, "AFC-1 relay", "m%d" % mid))
            self.topic.error = None
            self.t[0] += 3600  # an hour on, the read works again
            second.handle_event(message(13, "AFC-1 relay", "m13"))
        entry = self.entry()
        self.assertEqual(entry["count"], 13)  # 1 to 13, each once
        self.assertEqual([item["mid"] for item in entry["links"]], list(range(1, 14)))
        self.assertIn("caught up 1 missed message(s) for AFC-1 (conversation 1)", self.logs)

    def test_a_catch_up_still_open_after_its_bound_gives_up_and_says_so(self) -> None:
        with mock.patch.object(lr, "MAX_BACKFILL", 3):
            first = self.restart()
            first.handle_event(message(1, "AFC-1 relay", "m1"))
            self.topic.messages = [past(mid, "m%d" % mid) for mid in range(1, 9)]
            self.topic.error = RuntimeError("zulip history is down")
            second = self.restart()
            for mid in range(3, 7):
                second.handle_event(message(mid, "AFC-1 relay", "m%d" % mid))
            gave_up = [line for line in self.logs if "gave up catching up" in line]
            self.assertEqual(gave_up, ["gave up catching up AFC-1 (conversation 1) after 3 later "
                                       "messages; what its topic got while the relay was not "
                                       "receiving it stays missing"])
            # Given up: the memory is released, and no read is made for it again.
            self.topic.error = None
            self.t[0] += lr.CATCH_UP_RETRY
            second.handle_event(message(7, "AFC-1 relay", "m7"))
        self.assertEqual(len(self.topic.since_calls), 1)
        self.assertEqual(self.entry()["seen"], [1, 3, 4, 5, 6, 7])

    def test_a_read_cut_short_at_its_cap_is_continued(self) -> None:
        first = self.restart()
        first.handle_event(message(1, "AFC-1 relay", "m1"))
        # Down for longer than one read holds: 2 to 7 land, and a read returns three at most.
        self.topic.messages = [past(mid, "m%d" % mid) for mid in range(1, 9)]
        self.topic.cap = 3
        second = self.restart()
        with mock.patch.object(lr, "MAX_BACKFILL", 3):
            second.handle_event(message(8, "AFC-1 relay", "m8"))
            self.assertEqual(self.entry()["seen"], [1, 2, 3, 4, 8])
            for mid in (9, 10, 11):
                self.topic.messages.append(past(mid, "m%d" % mid))
                second.handle_event(message(mid, "AFC-1 relay", "m%d" % mid))
        # Each read went on from the newest message the last one returned, until one reached
        # the live message; then the entry was settled and the next message read nothing.
        self.assertEqual([after for _sid, _topic, after in self.topic.since_calls], [1, 4, 7])
        self.assertEqual(self.entry()["seen"], list(range(1, 12)))
        self.assertEqual(self.entry()["count"], 11)

    def test_a_whole_topic_read_cut_short_at_its_cap_is_continued(self) -> None:
        worker = self.restart()
        self.topic.messages = [past(mid, "m%d" % mid) for mid in range(1, 7)]
        self.topic.cap = 3
        with mock.patch.object(lr, "MAX_BACKFILL", 3):
            # The first message to name the issue comes after more than one read holds: the
            # entry gets the topic's first three, and its catch-up goes on from there.
            worker.handle_event(message(6, "AFC-1 relay", "m6"))
            self.assertEqual(self.entry()["seen"], [1, 2, 3, 6])
            for mid in (7, 8):
                self.topic.messages.append(past(mid, "m%d" % mid))
                worker.handle_event(message(mid, "AFC-1 relay", "m%d" % mid))
        self.assertEqual([after for _sid, _topic, after in self.topic.since_calls], [3, 6])
        self.assertEqual(self.entry()["seen"], list(range(1, 9)))
        self.assertEqual(self.entry()["count"], 8)

    # -- a read that ends short of the live message (Kody, PR 2298) -------------------------
    def test_a_read_that_stops_short_of_the_live_message_is_continued(self) -> None:
        # The reader stopped early (a page the API repeated, say), below its cap and below the
        # live message.  Such a read used to settle the entry, so 4 and 5 were never read.
        first = self.restart()
        first.handle_event(message(1, "AFC-1 relay", "m1"))
        self.topic.messages = [past(mid, "m%d" % mid) for mid in range(1, 7)]
        stops = [3]  # the next read stops after this message, once

        def since(stream_id: int, topic: str, after_id: int) -> list[dict]:
            got = self.topic.since(stream_id, topic, after_id)
            if stops:
                limit = stops.pop()
                got = [item for item in got if item["id"] <= limit]
            return got

        second = self.worker(history=self.topic.whole, history_since=since)
        second.handle_event(message(6, "AFC-1 relay", "m6"))
        self.assertEqual(self.entry()["seen"], [1, 2, 3, 6])
        self.topic.messages.append(past(7, "m7"))
        second.handle_event(message(7, "AFC-1 relay", "m7"))
        # The next read went on from where the short one stopped, and reached the live message.
        self.assertEqual([after for _sid, _topic, after in self.topic.since_calls], [1, 3])
        self.assertEqual(self.entry()["seen"], list(range(1, 8)))
        self.assertEqual(self.entry()["count"], 7)
        self.topic.messages.append(past(8, "m8"))
        second.handle_event(message(8, "AFC-1 relay", "m8"))
        self.assertEqual(len(self.topic.since_calls), 2)  # settled: no third read

    def test_reads_that_never_reach_the_live_message_give_up_and_say_so(self) -> None:
        # The reader answers nothing past the entry's point, however many live messages come
        # (each left the topic after it was routed, say).  Three such reads in a row settle the
        # entry, so the topic does not read again at every message.
        first = self.restart()
        first.handle_event(message(1, "AFC-1 relay", "m1"))
        self.topic.messages = [past(1, "m1")]
        second = self.restart()
        for mid in (2, 3, 4):
            second.handle_event(message(mid, "AFC-1 relay", "m%d" % mid))
        self.assertEqual([after for _sid, _topic, after in self.topic.since_calls], [1, 1, 1])
        gave_up = [line for line in self.logs if "gave up catching up" in line]
        self.assertEqual(gave_up, ["gave up catching up AFC-1 (conversation 1): 3 reads in a row "
                                   "stopped short of the live message and brought nothing new; "
                                   "what lies between stays missing"])
        second.handle_event(message(5, "AFC-1 relay", "m5"))
        self.assertEqual(len(self.topic.since_calls), 3)  # settled: no fourth read
        self.assertEqual(self.entry()["seen"], [1, 2, 3, 4, 5])  # every live one recorded

    # -- the bounds on what a read answers (Kody, PR 2298) -------------------------------------
    def test_a_read_past_its_bound_is_cut_and_read_on_from(self) -> None:
        first = self.restart()
        first.handle_event(message(1, "AFC-1 relay", "m1"))
        self.topic.messages = [past(mid, "m%d" % mid) for mid in range(1, 9)]
        second = self.restart()
        with mock.patch.object(lr, "MAX_BACKFILL", 3):
            # The reader answers 2 to 8, seven messages, where three is the most a read takes.
            second.handle_event(message(8, "AFC-1 relay", "m8"))
            self.assertEqual(self.entry()["seen"], [1, 2, 3, 4, 8])
            self.assertIn(
                "a topic history read answered 4 message(s) past the 3 it takes; a later read "
                "goes on from the newest one taken",
                self.logs,
            )
            for mid in (9, 10):
                self.topic.messages.append(past(mid, "m%d" % mid))
                second.handle_event(message(mid, "AFC-1 relay", "m%d" % mid))
        self.assertEqual([after for _sid, _topic, after in self.topic.since_calls], [1, 4, 7])
        self.assertEqual(self.entry()["seen"], list(range(1, 11)))
        self.assertEqual(self.entry()["count"], 10)

    def test_a_history_message_with_an_oversized_field_is_skipped_and_counted(self) -> None:
        first = self.restart()
        first.handle_event(message(1, "AFC-1 relay", "hello"))
        bound = lr.MAX_HISTORY_FIELD_BYTES
        long = "x" * (bound + 1)
        wide = "é" * (bound // 2 + 1)  # inside the bound in characters, past it in UTF-8
        self.topic.messages = [
            past(1, "hello"),
            past(2, long),
            {**past(3, "x"), "sender_full_name": wide},
            {**past(4, "x"), "sender_email": long},
            past(5, "y" * bound),  # exactly the bound is taken
            past(6, "live"),
        ]
        second = self.restart()
        second.handle_event(message(6, "AFC-1 relay", "live"))
        self.assertEqual(self.entry()["seen"], [1, 5, 6])
        self.assertIn(
            "a topic history read answered 3 malformed message(s); they are left out of the "
            "comment",
            self.logs,
        )
        # A lone surrogate, which JSON can carry, is measured, not raised on.
        self.assertIsNotNone(lr._checked_message(past(7, "a \ud800 b")))

    def test_a_credential_in_a_missed_message_is_withheld_whole_and_logged(self) -> None:
        # A missed message is recorded as the live path records one: a field the detector flags
        # is withheld whole, and the log names the fields as the live path's does.  The detector
        # is stubbed so that a plain marker stands in for a credential;  has_secret's own tests
        # cover what it matches.
        marker = "PLAIN-MARKER-NOT-A-CREDENTIAL"
        first = self.restart()
        first.handle_event(message(1, "AFC-1 relay", "hello"))
        self.topic.messages = [
            past(1, "hello"),
            {**past(2, "the key is " + marker), "sender_full_name": "Al " + marker},
            past(3, "live"),
        ]
        second = self.restart()
        self.t[0] += 3600
        with mock.patch.object(lr, "has_secret", side_effect=lambda value: marker in value):
            second.handle_event(message(3, "AFC-1 relay", "live"))
        self.assertEqual(self.entry()["seen"], [1, 2, 3])
        body = self.lin.updated[-1][1]
        self.assertIn("near/2", body)
        self.assertNotIn(marker, body)
        self.assertGreaterEqual(body.count(lr.WITHHELD), 2)  # the quote and the sender
        self.assertIn("message 2: withheld content, sender (credential-shaped text)", self.logs)
        self.assertFalse(any(marker in line for line in self.logs))
        self.led.save()
        self.assertNotIn(marker, self.led.path.read_text("utf-8"))

    def test_an_entry_made_by_a_replayed_older_message_reads_on_from_it(self) -> None:
        worker = self.restart()
        # The host replays message 2 (an owner post, say) after 3 and 4 were posted, and the
        # first entry for the issue is made by it.
        self.topic.messages = [past(mid, "m%d" % mid) for mid in (1, 2, 3, 4)]
        worker.handle_event(message(2, "AFC-1 relay", "m2"))
        self.assertEqual(self.entry()["seen"], [1, 2])
        # Its whole-topic read showed 3 and 4 after it, so the topic's next message reads on.
        self.topic.messages.append(past(5, "m5"))
        worker.handle_event(message(5, "AFC-1 relay", "m5"))
        self.assertEqual(self.entry()["seen"], [1, 2, 3, 4, 5])
        self.assertEqual(self.topic.since_calls, [(5, "AFC-1 relay", 2)])

    def test_an_entry_restored_by_a_replayed_older_message_reads_on_from_it(self) -> None:
        first = self.restart()
        self.topic.messages = [past(1, "this is AFC-1")]
        first.handle_event(message(1, "AFC-1 relay", "this is AFC-1"))
        self.prune()
        # Down: 2 (an owner post), 3 and 4 land, and the host's own backfill replays 2 alone.
        self.topic.messages += [past(2, "m2"), past(3, "m3"), past(4, "m4")]
        second = self.restart()
        self.t[0] += 86400
        second.handle_event(message(2, "AFC-1 relay", "m2"))
        self.assertEqual(self.entry()["seen"], [1, 2])
        self.topic.messages.append(past(5, "m5"))
        second.handle_event(message(5, "AFC-1 relay", "m5"))
        self.assertEqual(self.entry()["seen"], [1, 2, 3, 4, 5])
        self.assertEqual(self.topic.since_calls, [(5, "AFC-1 relay", 2)])
        self.assertEqual(self.lin.create_calls, 1)

    def test_a_new_entry_whose_history_cannot_be_read_is_filled_in_later(self) -> None:
        worker = self.restart()
        self.topic.messages = [past(1, "m1"), past(2, "m2"), past(3, "this is AFC-1")]
        self.topic.whole_error = OSError("zulip is down")
        worker.handle_event(message(3, "deploy thread", "this is AFC-1"))
        self.assertEqual(len(self.lin.created), 1)  # a create is never held for history
        self.assertEqual(self.entry("AFC-1", "deploy thread")["seen"], [3])
        self.topic.whole_error = None
        self.topic.messages.append(past(4, "m4"))
        self.t[0] += 3600  # an hour on, the read works again
        worker.handle_event(message(4, "deploy thread", "m4"))
        # The catch-up read the topic from its beginning, so the comment's next edit has the
        # messages the failed read would have pulled in.
        self.assertEqual(self.entry("AFC-1", "deploy thread")["seen"], [1, 2, 3, 4])
        self.assertEqual(self.topic.since_calls, [(5, "deploy thread", 0)])
        self.assertTrue(any("fills in the rest" in line for line in self.logs))

    # -- the shape of what a history reader answers (Kody, PR 2298) ------------------------
    def test_a_malformed_message_in_a_catch_up_read_is_skipped_and_the_rest_recorded(
        self,
    ) -> None:
        first = self.restart()
        first.handle_event(message(1, "AFC-1 relay", "hello"))
        no_time = {name: value for name, value in past(5, "x").items() if name != "timestamp"}
        self.topic.messages = [
            past(1, "hello"),
            {**past(2, "x"), "content": None},
            {**past(3, "x"), "sender_full_name": ["a list, not a name"]},
            {**past(4, "x"), "sender_email": 7},
            no_time,
            {**past(6, "x"), "timestamp": "906"},
            {**past(7, "x"), "timestamp": True},
            "not a message",
            {"id": "8", "content": "an id that is not an integer"},
            past(9, "missed"),
            past(10, "live"),
        ]
        # The reader hands every item back as it came, the ones without an integer id too.
        self.topic.sloppy = True
        second = self.restart()
        self.t[0] += 3600
        second.handle_event(message(10, "AFC-1 relay", "live"))
        entry = self.entry()
        # The well-formed ones are recorded, in id order; no malformed one reaches the comment.
        self.assertEqual(entry["seen"], [1, 9, 10])
        self.assertEqual(entry["count"], 3)
        body = self.lin.updated[-1][1]
        self.assertIn("near/9", body)
        for mid in range(2, 8):
            self.assertNotIn("near/%d)" % mid, body)
        self.assertNotIn("a list, not a name", body)
        # Counted in the log, and no field of a malformed message is written there.
        self.assertIn(
            "a topic history read answered 8 malformed message(s); they are left out of the "
            "comment",
            self.logs,
        )
        for text in ("a list, not a name", "not a message", "an id that is not an integer"):
            self.assertFalse(any(text in line for line in self.logs))
        # Settled: a malformed message does not hold the catch-up open, so the next message
        # reads nothing.
        second.handle_event(message(11, "AFC-1 relay", "and on"))
        self.assertEqual(len(self.topic.since_calls), 1)
        self.assertEqual(entry["seen"], [1, 9, 10, 11])

    def test_a_message_whose_time_cannot_be_rendered_does_not_stop_the_topic(self) -> None:
        # Before the check, such a timestamp raised in the append, the live message went
        # unrecorded, and the same read raised again at every later message in the topic.
        first = self.restart()
        first.handle_event(message(1, "AFC-1 relay", "hello"))
        self.topic.messages = [
            past(1, "hello"),
            {**past(2, "x"), "timestamp": 10**30},
            {**past(3, "x"), "timestamp": 10**18},
            {**past(4, "x"), "timestamp": float("inf")},
            {**past(5, "x"), "timestamp": float("nan")},
            {**past(6, "x"), "timestamp": -1},
            {**past(7, "x"), "timestamp": 253402300800},  # a second past the end of 9999
            past(8, "missed"),
            past(9, "live"),
        ]
        second = self.restart()
        second.handle_event(message(9, "AFC-1 relay", "live"))
        self.assertEqual(self.entry()["seen"], [1, 8, 9])
        self.assertIn(
            "a topic history read answered 6 malformed message(s); they are left out of the "
            "comment",
            self.logs,
        )

    def test_a_malformed_message_in_a_backfill_is_skipped_and_the_rest_recorded(self) -> None:
        worker = self.restart()
        self.topic.messages = [
            past(1, "m1"),
            {**past(2, "x"), "timestamp": 10**30},
            {**past(3, "x"), "content": {"not": "text"}},
            past(4, "m4"),
            past(5, "this is AFC-1"),
        ]
        worker.handle_event(message(5, "deploy thread", "this is AFC-1"))
        self.assertEqual(self.entry("AFC-1", "deploy thread")["seen"], [1, 4, 5])
        self.assertIn(
            "a topic history read answered 2 malformed message(s); they are left out of the "
            "comment",
            self.logs,
        )

    def test_a_malformed_message_does_not_hold_an_archived_comment_back(self) -> None:
        # Were it to fail the whole read, the restore would wait for a read that succeeds, and
        # one that answers the same message every time never does.
        first = self.restart()
        self.topic.messages = [past(1, "this is AFC-1")]
        first.handle_event(message(1, "AFC-1 relay", "this is AFC-1"))
        self.prune()
        self.topic.messages += [{**past(2, "x"), "sender_email": None}, past(3, "m3")]
        second = self.restart()
        self.t[0] += 86400
        second.handle_event(message(3, "AFC-1 relay", "m3"))
        self.assertEqual(self.entry()["seen"], [1, 3])  # restored at once, without the bad one
        self.assertEqual(self.lin.create_calls, 1)  # the standing comment, not a second one

    def test_a_malformed_message_keeps_its_place_in_a_read_cut_short_at_its_cap(self) -> None:
        first = self.restart()
        first.handle_event(message(1, "AFC-1 relay", "m1"))
        self.topic.messages = [past(mid, "m%d" % mid) for mid in range(1, 9)]
        self.topic.messages[2] = {**past(3, "x"), "content": None}
        self.topic.cap = 3
        second = self.restart()
        with mock.patch.object(lr, "MAX_BACKFILL", 3):
            second.handle_event(message(8, "AFC-1 relay", "m8"))
            for mid in (9, 10, 11):
                self.topic.messages.append(past(mid, "m%d" % mid))
                second.handle_event(message(mid, "AFC-1 relay", "m%d" % mid))
        # The first read answered its cap, the malformed message among them, so it still reads
        # as cut short and the catch-up goes on past it: nothing after it is dropped.
        self.assertEqual([after for _sid, _topic, after in self.topic.since_calls], [1, 4, 7])
        self.assertEqual(self.entry()["seen"], [1, 2, 4, 5, 6, 7, 8, 9, 10, 11])

    def test_a_read_that_is_not_a_list_of_messages_fails(self) -> None:
        first = self.restart()
        first.handle_event(message(1, "AFC-1 relay", "hello"))
        self.topic.messages = [past(1, "hello"), past(2, "missed"), past(3, "live")]
        # A mapping would otherwise be iterated as its keys, read as no messages, and settle
        # the catch-up with the gap still open.
        second = self.worker(
            history=self.topic.whole,
            history_since=lambda *_args: {"messages": list(self.topic.messages)},
        )
        second.handle_event(message(3, "AFC-1 relay", "live"))
        self.assertEqual(self.entry()["seen"], [1, 3])  # the live message is recorded
        failed = [line for line in self.logs if "could not be read" in line]
        self.assertEqual(len(failed), 1)
        self.assertIn("(ValueError)", failed[0])

    def test_a_catch_up_read_that_answers_no_list_at_all_fails_and_is_tried_again(self) -> None:
        # None, an empty mapping or an empty string used to become an empty list before the
        # check saw it, so the catch-up settled with message 2 lost for good, and nothing was
        # logged (the review check of Kody's findings, PR 2298).
        answers = [None, {}, "", b"", bytearray(), 0]
        first = self.restart()
        for n in range(len(answers)):
            topic = "AFC-1 case %d" % n
            self.topic.topics[topic] = [past(10 * n + 1, "hello")]
            first.handle_event(message(10 * n + 1, topic, "hello"))
        bad: list = []

        def since(stream_id: int, topic: str, after_id: int) -> object:
            return bad[0] if bad else self.topic.since(stream_id, topic, after_id)

        second = self.worker(history=self.topic.whole, history_since=since)
        for n, answer in enumerate(answers):
            topic = "AFC-1 case %d" % n
            ids = [10 * n + k for k in (1, 2, 3, 4)]
            with self.subTest(answer=answer):
                self.topic.topics[topic] += [past(ids[1], "missed"), past(ids[2], "live")]
                bad[:] = [answer]
                self.logs.clear()
                second.handle_event(message(ids[2], topic, "live"))
                self.assertEqual(self.entry("AFC-1", topic)["seen"], [ids[0], ids[2]])
                failed = [line for line in self.logs if "could not be read" in line]
                self.assertEqual(len(failed), 1)
                self.assertIn("(ValueError)", failed[0])
                # Not settled: once the reader answers, a later message fills the gap in.
                bad.clear()
                self.topic.topics[topic].append(past(ids[3], "on"))
                self.t[0] += lr.CATCH_UP_RETRY + 1
                second.handle_event(message(ids[3], topic, "on"))
                self.assertEqual(self.entry("AFC-1", topic)["seen"], ids)

    def test_a_restore_whose_read_answers_no_list_leaves_the_comment_as_it_is(self) -> None:
        # Read as no messages, None restored the entry from the live message alone, and the
        # next edit rewrote the standing comment down to that one message.
        first = self.restart()
        self.topic.messages = [past(1, "this is AFC-1"), past(2, "m2")]
        first.handle_event(message(1, "AFC-1 relay", "this is AFC-1"))
        first.handle_event(message(2, "AFC-1 relay", "m2"))
        key = self.key("AFC-1", 5, "AFC-1 relay")
        self.prune()
        whole: list = [None]
        second = self.worker(
            history=lambda *args: whole[0] if whole else self.topic.whole(*args),
            history_since=self.topic.since,
        )
        self.topic.messages.append(past(3, "m3"))
        self.t[0] += 86400
        second.handle_event(message(3, "AFC-1 relay", "m3"))
        second.tick()
        self.assertEqual(list(self.led.archive), [key])  # held, not restored
        self.assertEqual(self.led.entries, {})
        self.assertEqual(self.lin.updated, [])
        failed = [line for line in self.logs if "its archived comment is left as it is" in line]
        self.assertEqual(len(failed), 1)
        self.assertIn("(ValueError)", failed[0])
        # With the read answering again, the next message restores the whole topic.
        whole.clear()
        self.topic.messages.append(past(4, "m4"))
        self.t[0] += lr.CATCH_UP_RETRY + 1
        second.handle_event(message(4, "AFC-1 relay", "m4"))
        self.assertEqual(self.led.entries[key]["seen"], [1, 2, 3, 4])
        self.assertEqual(self.lin.create_calls, 1)

    def test_a_queue_that_registers_again_reopens_the_catch_up(self) -> None:
        worker = self.restart()
        self.topic.messages = [past(1, "m1")]
        worker.handle_event(message(1, "AFC-1 relay", "m1"))
        # Every seat's queue expires and registers again in the same process; 2 and 3 land
        # in between, and the host's own backfill replays neither.
        self.topic.messages += [past(2, "m2"), past(3, "m3"), past(4, "m4")]
        worker.reopen_catch_up()
        worker.handle_event(message(4, "AFC-1 relay", "m4"))
        self.assertEqual(self.entry()["seen"], [1, 2, 3, 4])
        self.assertEqual(self.topic.since_calls, [(5, "AFC-1 relay", 1)])

    def test_a_message_dropped_while_the_stream_set_is_unloaded_is_read_for_later(self) -> None:
        worker = self.restart()
        self.topic.messages = [past(1, "m1")]
        worker.handle_event(message(1, "AFC-1 relay", "m1"))
        self.topic.messages += [past(2, "m2"), past(3, "m3")]
        worker.set_public_streams(None)  # a stream event: the set is being reloaded
        worker.handle_event(message(2, "AFC-1 relay", "m2"))  # dropped: nothing is known yet
        worker.set_public_streams({5: "builds", 6: "deploys"})
        worker.handle_event(message(3, "AFC-1 relay", "m3"))
        self.assertEqual(self.entry()["seen"], [1, 2, 3])
        self.assertEqual(self.topic.since_calls, [(5, "AFC-1 relay", 1)])

    def test_two_entries_that_merge_keep_the_earlier_catch_up_point(self) -> None:
        first = self.restart()
        self.topic.topics["AFC-1 b"] = [past(1, "m1")]
        first.handle_event(message(1, "AFC-1 b", "m1"))
        # Down: 2 and 3 land in topic b.  After the restart a new topic a names the issue, and
        # is then renamed onto b, so b's entry takes a's in.
        self.topic.topics["AFC-1 b"] += [past(2, "m2"), past(3, "m3")]
        second = self.restart()
        self.topic.topics["AFC-1 a"] = [past(4, "m4")]
        second.handle_event(message(4, "AFC-1 a", "m4"))
        second.handle_event(move(5, "AFC-1 a", subject="AFC-1 b", ids=[4]))
        self.topic.topics["AFC-1 b"] += [past(4, "m4"), past(5, "m5")]
        second.handle_event(message(5, "AFC-1 b", "m5"))
        # b's own gap is read from b's own point, not from a's newer one.
        self.assertEqual(self.topic.since_calls, [(5, "AFC-1 b", 1)])
        self.assertEqual(self.entry("AFC-1", "AFC-1 b")["seen"], [1, 2, 3, 4, 5])

    def test_a_replayed_older_message_does_not_settle_the_catch_up(self) -> None:
        first = self.restart()
        for mid in (1, 2, 3):
            self.topic.messages.append(past(mid, "m%d" % mid))  # in the topic when routed
            first.handle_event(message(mid, "AFC-1 relay", "m%d" % mid))
        self.topic.messages += [past(4, "m4"), past(5, "m5")]
        second = self.restart()
        # After its own restart the host replays a message the entry already has.  It proves
        # nothing about what came after the entry's newest, so it costs no read and settles
        # nothing.
        second.handle_event(message(2, "AFC-1 relay", "m2"))
        self.assertEqual(self.topic.since_calls, [])
        second.handle_event(message(5, "AFC-1 relay", "m5"))
        self.assertEqual(self.topic.since_calls, [(5, "AFC-1 relay", 3)])
        self.assertEqual(self.entry()["seen"], [1, 2, 3, 4, 5])

    def test_a_message_that_is_not_the_topics_newest_leaves_the_catch_up_open(self) -> None:
        first = self.restart()
        first.handle_event(message(1, "AFC-1 relay", "m1"))
        # Down: 2, 3 and 4 land.  The host's own backfill replays 3 alone (an owner post, say).
        self.topic.messages = [past(mid, "m%d" % mid) for mid in (1, 2, 3, 4)]
        second = self.restart()
        second.handle_event(message(3, "AFC-1 relay", "m3"))
        self.assertEqual(self.entry()["seen"], [1, 2, 3])  # what lay before it is recorded
        # 4 was posted in the gap too and is never replayed: the next message picks it up.
        self.topic.messages.append(past(5, "m5"))
        second.handle_event(message(5, "AFC-1 relay", "m5"))
        self.assertEqual(self.entry()["seen"], [1, 2, 3, 4, 5])
        self.assertEqual([after for _sid, _topic, after in self.topic.since_calls], [1, 3])
        second.handle_event(message(6, "AFC-1 relay", "m6"))
        self.assertEqual(len(self.topic.since_calls), 2)

    def test_several_issues_in_one_topic_are_caught_up_with_one_read(self) -> None:
        first = self.restart()
        for mid, content in ((1, "this is AFC-1"), (2, "and OPS-9 too"), (3, "more")):
            self.topic.messages.append(past(mid, content))  # in the topic when routed
            first.handle_event(message(mid, "deploy thread", content))
        afc, ops = self.entry("AFC-1", "deploy thread"), self.entry("OPS-9", "deploy thread")
        self.assertEqual((afc["seen"], ops["seen"]), ([1, 2, 3], [1, 2, 3]))
        self.assertEqual(self.topic.since_calls, [])
        # Make the two entries stop at different messages, to show what one read serves.
        ops["seen"], ops["links"], ops["count"] = [1, 2], ops["links"][:2], 2
        self.topic.messages += [past(mid, "m%d" % mid) for mid in (4, 5, 6)]
        second = self.restart()
        second.handle_event(message(6, "deploy thread", "live"))
        # One read for the topic, from the older of the two points; each entry takes only what
        # is newer than its own.
        self.assertEqual(self.topic.since_calls, [(5, "deploy thread", 2)])
        self.assertEqual((afc["seen"], afc["count"]), ([1, 2, 3, 4, 5, 6], 6))
        self.assertEqual((ops["seen"], ops["count"]), ([1, 2, 3, 4, 5, 6], 6))
        second.handle_event(message(7, "deploy thread", "and on"))
        self.assertEqual(len(self.topic.since_calls), 1)

    def test_an_entry_the_shared_read_has_not_reached_is_not_given_up(self) -> None:
        # One read serves both entries, from OPS-9's point, far behind AFC-1's.  A read that
        # stops before AFC-1's point brought it nothing, but it moved on for OPS-9, and the next
        # read starts nearer AFC-1's point.  Counted as a read that brought nothing new (Kody,
        # PR 2298, asked for that), AFC-1 gave up at the third, before any read reached it, and
        # never got 11 to 14.
        first = self.restart()
        for mid in range(1, 11):
            content = {1: "this is AFC-1", 2: "and OPS-9 too"}.get(mid, "m%d" % mid)
            self.topic.messages.append(past(mid, content))  # in the topic when routed
            first.handle_event(message(mid, "deploy thread", content))
        afc, ops = self.entry("AFC-1", "deploy thread"), self.entry("OPS-9", "deploy thread")
        ops["seen"], ops["links"], ops["count"] = [1, 2], ops["links"][:2], 2
        self.topic.messages += [past(mid, "m%d" % mid) for mid in range(11, 15)]  # missed
        capped = [2, 2, 2, 2]  # the next four reads stop after two messages

        def since(stream_id: int, topic: str, after_id: int) -> list[dict]:
            got = self.topic.since(stream_id, topic, after_id)
            return got[: capped.pop()] if capped else got

        second = self.worker(history=self.topic.whole, history_since=since)
        for mid in range(15, 20):
            self.topic.messages.append(past(mid, "m%d" % mid))
            second.handle_event(message(mid, "deploy thread", "m%d" % mid))
        self.assertEqual([after for _sid, _topic, after in self.topic.since_calls],
                         [2, 4, 6, 8, 10])
        self.assertFalse(any("gave up catching up" in line for line in self.logs))
        self.assertEqual(afc["seen"], list(range(1, 20)))
        self.assertEqual(ops["seen"], list(range(1, 20)))
        self.topic.messages.append(past(20, "m20"))
        second.handle_event(message(20, "deploy thread", "m20"))
        self.assertEqual(len(self.topic.since_calls), 5)  # both settled at the live message

    def test_an_entry_made_or_restored_in_this_process_is_not_read_for(self) -> None:
        worker = self.restart()
        # Each message is in the topic when it is routed, as in Zulip: a whole-topic read that
        # does not reach the message it was made for no longer settles the entry.
        self.topic.messages = [past(1, "hello")]
        worker.handle_event(message(1, "AFC-1 relay", "hello"))  # brand-new: backfilled whole
        self.topic.messages.append(past(2, "again"))
        worker.handle_event(message(2, "AFC-1 relay", "again"))
        self.prune()
        self.t[0] += 86400
        self.topic.messages.append(past(3, "back"))
        worker.handle_event(message(3, "AFC-1 relay", "back"))  # restored: backfilled whole
        self.topic.messages.append(past(4, "more"))
        worker.handle_event(message(4, "AFC-1 relay", "more"))
        self.assertEqual(self.topic.since_calls, [])
        self.assertEqual(self.topic.whole_calls, 2)
        self.assertEqual(self.lin.create_calls, 1)

    def test_without_the_reader_a_restart_catches_nothing_up(self) -> None:
        # The standalone relay passes no reader: nothing is read, and nothing breaks.
        first = self.worker()
        first.handle_event(message(1, "AFC-1 relay", "hello"))
        second = self.worker()
        second.handle_event(message(5, "AFC-1 relay", "live again"))
        self.assertEqual(self.entry()["seen"], [1, 5])


class ReadablePostTests(WorkerCase):
    """AFC-847:  the comment carries the whole post, as readable text.

    The relay used to cut every post at 280 characters, collapse its line structure into
    one line and escape its markup, so a reader of the Linear comment saw a fragment of
    a post, not the post.  Now the post is converted to Markdown, shown whole up to a
    bound, and every cut says what it left out.
    """

    def setUp(self) -> None:
        super().setUp()
        self.w = self.worker(history=self.topic.whole)

    def old_entry(self, quote: str = "the old cut text") -> dict:
        """An entry as an older build wrote it:  no render format, and a quote that was
        cut at 280 characters."""
        entry = lr.new_entry("AFC-1", 5, "builds", "AFC-1 relay", 1)
        del entry["format"]
        entry["comment_id"] = "c1"
        entry["count"] = 1
        entry["seen"] = [1]
        entry["links"] = [
            {"mid": 1, "sender": "Al", "when": "Nov 18 05:07 UTC", "quote": quote}
        ]
        self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")] = entry
        return entry

    # -- the whole post ------------------------------------------------------------------
    def test_a_post_is_shown_whole_and_keeps_its_line_structure(self) -> None:
        post = "first line\n\nsecond line\n\n" + "x" * 500
        self.w.handle_event(message(1, "AFC-1 relay", post))
        body = self.lin.created[0][1]
        self.assertIn("first line", body)
        self.assertIn("second line", body)
        self.assertIn("x" * 500, body)  # the whole post, not a fragment of it

    def test_a_valid_post_over_four_thousand_characters_is_shown_whole(self) -> None:
        # Zulip accepts a message of 10,000 characters, and the mirror shows one whole.
        self.w.handle_event(message(1, "AFC-1 relay", "p" * 6000))
        body = self.lin.created[0][1]
        self.assertIn("p" * 6000, body)
        self.assertNotIn("characters of this message are not shown.", body)

    def test_a_topic_of_several_long_posts_fits_the_comment(self) -> None:
        # Five posts of 9,000 characters are 45,000 characters, inside the comment's bound.
        for mid in range(1, 6):
            self.w.handle_event(message(mid, "AFC-1 relay", "p" * 9000))
        self.t[0] += 600
        self.w.tick()
        body = self.lin.updated[-1][1]
        self.assertLessEqual(len(body), lr.COMMENT_CHARS)
        for mid in range(1, 6):
            self.assertIn("near/%d)" % mid, body)
        self.assertNotIn("characters of this topic are not shown.", body)

    def test_the_ledger_holds_bounded_text_per_entry(self) -> None:
        # Twenty posts of 9,000 characters would be 180,000 characters, and no comment can
        # show that, so the entry holds only the text a comment can show.
        for mid in range(1, 21):
            self.w.handle_event(message(mid, "AFC-1 relay", "p" * 9000))
        entry = self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")]
        held = sum(len(item["quote"]) for item in entry["links"])
        self.assertLessEqual(held, lr.MAX_ENTRY_TEXT)
        self.assertLess(len(entry["links"]), lr.MAX_LINKS_SHOWN)
        self.assertEqual(entry["count"], 20)  # every message was mirrored

    def test_a_post_past_the_message_bound_is_cut_and_says_so(self) -> None:
        self.w.handle_event(message(1, "AFC-1 relay", "y" * (lr.MESSAGE_CHARS + 100)))
        body = self.lin.created[0][1]
        self.assertIn("100 characters of this message are not shown.", body)
        self.assertNotIn("y" * (lr.MESSAGE_CHARS + 1), body)

    def test_the_comment_body_is_bounded_and_says_what_it_left_out(self) -> None:
        for mid in range(1, 11):
            self.w.handle_event(message(mid, "AFC-1 relay", "z" * 9000))
        self.t[0] += 600
        self.w.tick()
        body = self.lin.updated[-1][1]
        self.assertLessEqual(len(body), lr.COMMENT_CHARS)
        self.assertRegex(body, r"_\d+ characters of this topic are not shown\._")
        self.assertIn("near/10)", body)  # the newest post is in
        self.assertLess(body.count("near/"), 10)  # the oldest ones are out, and it says so

    def test_a_link_in_a_post_is_shown_and_never_becomes_a_live_link(self) -> None:
        self.w.handle_event(message(1, "AFC-1 relay", "see [the docs](https://example.com/x)"))
        body = self.lin.created[0][1]
        self.assertIn("the docs", body)
        self.assertIn("https://example.com/x", body)
        self.assertNotIn("see [the docs]", body)  # not a link:  the opener is escaped

    def test_a_mention_in_a_post_never_becomes_a_live_mention(self) -> None:
        self.w.handle_event(message(1, "AFC-1 relay", "hello @**MiniMax**"))
        body = self.lin.created[0][1]
        self.assertIn("@\u200bMiniMax", body)

    def test_a_link_behind_an_escaped_backslash_is_never_live(self) -> None:
        # Two backslashes in front of a link do not escape it:  the bracket has to be
        # escaped again, or the link is live (Sentry, PR 2340).
        raw = "a " + chr(92) * 2 + "[b](http://evil) and [ok](http://x)"
        text = lr.post_text(raw)
        for marker in ("[b]", "[ok]"):
            index = text.index(marker)
            slashes = 0
            back = index - 1
            while back >= 0 and text[back] == chr(92):
                slashes += 1
                back -= 1
            self.assertEqual(slashes % 2, 1, marker)  # an odd count escapes the bracket

    def test_a_cut_inside_a_code_block_closes_the_fence(self) -> None:
        # A post cut in the middle of a code block must not leave the fence open:  the
        # rest of the comment would render as code (Sentry, PR 2340).
        raw = "```python\n" + ("print(1)\n" * 2000)
        text = lr.post_text(raw)
        self.assertEqual(text.count("```"), 2)  # the fence is closed where the post stops
        self.assertTrue(text.endswith("characters of this message are not shown._"))

    # -- the credential scan runs over the converted text ---------------------------------
    def test_a_markup_attribute_cannot_trip_the_credential_scan(self) -> None:
        # A long hex run in an attribute is markup, not text:  the conversion drops it, so
        # it can no longer withhold a post whole.
        sha = "a" * 40
        html = '<p>the commit is here</p><p><span class="mention" data-sha="%s">@x</span></p>' % sha
        self.w.handle_event(message(1, "AFC-1 relay", html))
        body = self.lin.created[0][1]
        self.assertNotIn(lr.WITHHELD, body)
        self.assertIn("the commit is here", body)
        self.assertNotIn(sha, body)

    def test_a_long_hex_run_in_the_text_is_still_withheld_whole(self) -> None:
        # The text itself is still scanned:  this is the row AFC-834 is about, and this
        # change does not touch it.
        sha = "a" * 40
        self.w.handle_event(message(1, "AFC-1 relay", "the commit is %s" % sha))
        body = self.lin.created[0][1]
        self.assertIn(lr.WITHHELD, body)
        self.assertNotIn(sha, body)

    def test_a_credential_inside_a_link_url_is_still_withheld_whole(self) -> None:
        token = "ghp_" + "fake" * 9
        html = '<p><a href="https://example.com/%s">the link</a></p>' % token
        self.w.handle_event(message(1, "AFC-1 relay", html))
        body = self.lin.created[0][1]
        self.assertIn(lr.WITHHELD, body)
        self.assertNotIn(token, body)

    # -- the repair of the comments an older build wrote ----------------------------------
    def test_a_comment_an_older_build_wrote_is_re_rendered_from_zulip(self) -> None:
        entry = self.old_entry()
        self.topic.messages = [past(1, "the whole post\n\nwith its lines")]
        self.w.handle_event(message(2, "AFC-1 relay", "a new message"))
        self.assertEqual(entry["format"], lr.RENDER_VERSION)
        self.assertEqual(entry["links"][0]["quote"], "the whole post\n\nwith its lines")
        self.t[0] += 600
        self.w.tick()
        body = self.lin.updated[-1][1]
        self.assertIn("the whole post", body)
        self.assertIn("with its lines", body)
        self.assertNotIn("the old cut text", body)
        self.assertIn("Re-rendered from Zulip", body)  # the comment says what happened
        self.assertTrue(any("re-rendered the quotes" in line for line in self.logs))

    def test_the_repair_keeps_the_mention_and_the_credential_protections(self) -> None:
        # A post that is rendered again from Zulip is protected the way a new post is:  a
        # mention is never live, and a credential-shaped post is withheld whole.
        entry = self.old_entry()
        self.topic.messages = [past(1, "hello @**MiniMax**")]
        self.w.handle_event(message(2, "AFC-1 relay", "a new message"))
        self.assertIn("@\u200bMiniMax", entry["links"][0]["quote"])
        token = "ghp_" + "fake" * 9
        entry = self.old_entry()
        self.topic.messages = [past(1, "token: %s" % token)]
        self.w.handle_event(message(2, "AFC-1 relay", "a new message"))
        self.assertEqual(entry["links"][0]["quote"], lr.WITHHELD)
        self.assertNotIn(token, entry["links"][0]["quote"])

    def test_a_stale_comment_is_re_rendered_by_the_cycle(self) -> None:
        # A topic that stays silent is repaired anyway:  the cycle reads it back.
        self.old_entry()
        self.topic.messages = [past(1, "the whole post")]
        self.w.tick()
        entry = self.led.entries[self.key("AFC-1", 5, "AFC-1 relay")]
        self.assertEqual(entry["format"], lr.RENDER_VERSION)
        self.assertEqual(entry["links"][0]["quote"], "the whole post")
        self.assertIn("the whole post", self.lin.updated[-1][1])

    def test_a_repair_that_cannot_read_the_topic_leaves_the_comment_as_it_is(self) -> None:
        entry = self.old_entry()
        self.topic.whole_error = RuntimeError("the topic cannot be read")
        self.w.tick()
        self.assertNotEqual(entry.get("format"), lr.RENDER_VERSION)  # it waits for a later try
        self.assertEqual(entry["links"][0]["quote"], "the old cut text")
        self.assertEqual(self.lin.updated, [])

    def test_a_repair_of_another_topic_changes_nothing(self) -> None:
        # The stored topic is the whitespace-collapsed form, so a read can be of another
        # topic:  the quotes are then left as they are.
        entry = self.old_entry()
        self.topic.messages = [past(9, "a different topic")]
        self.w.tick()
        self.assertNotEqual(entry.get("format"), lr.RENDER_VERSION)
        self.assertEqual(entry["links"][0]["quote"], "the old cut text")

    def test_the_repair_reads_one_topic_a_cycle_and_a_topic_it_cannot_read_waits(self) -> None:
        # A topic the read does not cover is not read again in the next cycle, and it does
        # not make the cycle read every stale entry (Sentry, PR 2340).
        self.old_entry()
        second = lr.new_entry("AFC-1", 6, "builds", "AFC-1 other", 2)
        del second["format"]
        second["comment_id"] = "c2"
        second["count"] = 1
        second["seen"] = [2]
        second["links"] = [{"mid": 2, "sender": "Al", "when": "Nov 18 05:07 UTC", "quote": "old"}]
        self.led.entries[self.key("AFC-1", 6, "AFC-1 other")] = second
        self.topic.messages = [past(9, "a different topic")]
        self.w.tick()
        self.assertEqual(self.topic.whole_calls, 1)  # one read a cycle, not one an entry
        self.w.tick()
        self.assertEqual(self.topic.whole_calls, 2)  # the second entry is read next
        self.w.tick()
        self.assertEqual(self.topic.whole_calls, 2)  # both wait before they are tried again

    def test_a_link_whose_text_holds_a_bracket_is_never_live(self) -> None:
        # The text of a link can hold a bracket of its own, escaped by the conversion:  the
        # opener is still found and escaped, so the link is shown and never live
        # (Kody, PR 2340).
        text = lr.post_text('<p><a href="https://evil.example">a] b</a></p>')
        self.assertTrue(text.startswith("\\["), text)
        self.assertIn("a\\] b", text)

    def test_the_cut_says_how_many_characters_are_really_left_out(self) -> None:
        # The count covers the whitespace the cut trims as well as the tail (Kody, PR
        # 2340).
        raw = "y" * (lr.MESSAGE_CHARS - 2) + "   " + "z" * 50
        text = lr.post_text(raw)
        self.assertIn("53 characters of this message are not shown.", text)

    def test_the_code_of_a_post_is_not_defanged(self) -> None:
        # Code is shown as the sender wrote it:  the at sign inside it is left alone
        # (Kody, PR 2340).
        text = lr.post_text("run `mail @MiniMax` and mail @**MiniMax**")
        self.assertIn("`mail @MiniMax`", text)
        self.assertIn("@\u200bMiniMax", text)  # the prose mention is still broken

    def test_a_fenced_block_is_not_defanged(self) -> None:
        text = lr.post_text("```\nmail @**MiniMax**\n```")
        self.assertIn("mail @**MiniMax**", text)

    def test_a_repair_holds_the_same_text_bound_as_a_new_message(self) -> None:
        # A repaired entry holds as much text as a new message lets it hold:  the whole
        # posts do not push the entry past what a comment can show (Kody, PR 2340).
        entry = self.old_entry()
        entry["links"] = [
            {"mid": mid, "sender": "Al", "when": "Nov 18 05:07 UTC", "quote": "old"}
            for mid in range(1, 11)
        ]
        entry["seen"] = list(range(1, 11))
        self.topic.messages = [past(mid, "p" * 9000) for mid in range(1, 11)]
        self.w.tick()
        held = sum(len(item["quote"]) for item in entry["links"])
        self.assertLessEqual(held, lr.MAX_ENTRY_TEXT)
        self.assertGreater(held, 0)

    def test_a_topic_that_cannot_be_read_is_not_read_again_for_every_message(self) -> None:
        # A topic that cannot be read waits before it is tried again, so every message of
        # it does not read it again (Kody, PR 2340).
        self.old_entry()
        self.topic.whole_error = RuntimeError("the topic cannot be read")
        self.w.handle_event(message(2, "AFC-1 relay", "a new message"))
        self.assertEqual(self.topic.whole_calls, 1)
        self.w.handle_event(message(3, "AFC-1 relay", "another message"))
        self.assertEqual(self.topic.whole_calls, 1)  # it waits

    def test_a_post_full_of_brackets_does_not_stall_the_relay(self) -> None:
        # The scan for the link openers is one pass over the text, so a post full of
        # brackets cannot stall the relay poll loop (Kody, PR 2340).
        raw = "[" * 20000 + "](x)"
        start = time.monotonic()
        text = lr.post_text(raw)
        self.assertLess(time.monotonic() - start, 5.0)  # a quadratic scan would not
        self.assertTrue(text.startswith("\\["), text[:40])  # the opener is escaped

    def test_without_a_reader_nothing_is_re_rendered(self) -> None:
        # The standalone relay has no history reader:  its comments keep what they have.
        worker = self.worker()
        entry = self.old_entry()
        worker.tick()
        self.assertNotIn("format", entry)

    def test_the_repair_reads_a_topic_once_for_two_entries_of_it(self) -> None:
        # Two entries of one topic share the read of that topic (Kody, PR 2340).
        self.old_entry()
        second = lr.new_entry("AFC-2", 5, "builds", "AFC-1 relay", 2)
        del second["format"]
        second["comment_id"] = "c2"
        second["count"] = 1
        second["seen"] = [1]
        second["links"] = [
            {"mid": 1, "sender": "Al", "when": "Nov 18 05:07 UTC", "quote": "old"}
        ]
        self.led.entries[self.key("AFC-2", 5, "AFC-1 relay")] = second
        self.topic.messages = [past(1, "the whole post")]
        with mock.patch.object(lr, "REFRESH_PER_CYCLE", 2):
            self.w.tick()
        self.assertEqual(self.topic.whole_calls, 1)  # one read for the topic, not one an entry


if __name__ == "__main__":
    unittest.main()
