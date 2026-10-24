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
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from agent_sync import linear_relay as lr
from agent_sync.zulip import _NoRedirect


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
        live = str(Path(lr.LIVE_STATE).expanduser())
        with self.assertRaises(SystemExit):
            lr.state_path({"LINEAR_RELAY_STATE": live}, True)
        self.assertNotEqual(lr.state_path({}, True), Path(lr.LIVE_STATE).expanduser())
        self.assertEqual(lr.state_path({}, False), Path(lr.LIVE_STATE).expanduser())

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
                       {"streams": [{"stream_id": "5", "invite_only": False}]}):
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
            code = lr.run_check(None, CheckLinear(lr.LinearNotFound("AFC-9")), env,
                                "https://z.example")
        self.assertEqual(code, 0)
        with redirect_stdout(io.StringIO()):
            code = lr.run_check(None, CheckLinear(lr.LinearAuthError("no token")), env,
                                "https://z.example")
        self.assertEqual(code, 1)
        # A malformed id Linear refuses as invalid input is an issue problem, not an auth one.
        out = io.StringIO()
        with redirect_stdout(out):
            code = lr.run_check(None, CheckLinear(lr.LinearPermanentError("INVALID_INPUT")), env,
                                "https://z.example")
        self.assertEqual(code, 0)
        self.assertIn("the token works", out.getvalue())

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
               "ZULIP_SITE": "https://other.example", "AGENT_SYNC_REALM": "https://z.example"}
        with self.assertRaises(SystemExit) as caught:
            lr.run(env)
        self.assertIn("AGENT_SYNC_REALM", str(caught.exception))

    def test_the_default_teams_match_the_linkifier(self) -> None:
        import zulip_linkifiers

        self.assertEqual(set(lr.DEFAULT_TEAMS), set(zulip_linkifiers.LINEAR_TEAMS))


if __name__ == "__main__":
    unittest.main()
