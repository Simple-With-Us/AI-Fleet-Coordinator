"""The Zulip -> Linear relay:  one standing Linear comment per (issue, topic), edited in place.

These tests pin the behaviour the owner ruled on and the findings Echo's reviewer raised
(2026-10-23):

  - one comment per (issue, topic), created once and edited in place, at most one edit every ten
    minutes, and never stacked;
  - a message is mirrored once, so a replay is idempotent;
  - only public streams are relayed: never a direct message, never a private stream, never the
    relay's own post;
  - a topic rename carries the standing comment over instead of starting a second one;
  - a create whose answer was lost is reconciled and adopted, not stacked;
  - a dry run writes nothing to Linear and never touches the live ledger;
  - the ledger is bounded;
  - copied Zulip text cannot carry a credential or break out of the comment;
  - a missing issue is not an auth failure, and a transient error is retried while a not-found is
    not.

Nothing here touches the network:  the Linear client is faked, and the one test that exercises the
HTTP layer patches `urllib.request.urlopen`.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from agent_sync import linear_relay as lr


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

    def issue_id(self, identifier: str) -> str:
        self.lookups.append(identifier)
        if identifier not in self.known:
            raise lr.LinearNotFound(identifier)
        return "uuid-" + identifier

    def create_comment(self, issue_uuid: str, body: str) -> str:
        self.created.append((issue_uuid, body))
        cid = "c%d" % len(self.created)
        self.by_marker[body.split("\n", 1)[0]] = cid
        return cid

    def update_comment(self, comment_id: str, body: str) -> None:
        self.updated.append((comment_id, body))

    def find_comment(self, issue_uuid: str, marker: str) -> str | None:
        if self.fail_find:
            raise lr.LinearError("boom")
        return self.by_marker.get(marker)

    def viewer(self) -> str:
        return "Zulip Relay"


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


class RelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.t = [1000.0]
        self.lin = FakeLinear()
        self.led = lr.Ledger(Path(tempfile.mkdtemp()) / "state.json")
        self.led.open()
        self.w = lr.RelayWorker(
            "https://z.example", "relay@x", self.lin, self.led,
            lr.build_regex(lr.DEFAULT_TEAMS), clock=lambda: self.t[0],
            log=lambda m: None, public_streams={5},
        )

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
        self.assertEqual(self.led.entries["AFC-1|5|AFC-1 relay"]["count"], 1)

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
        self.w.public_streams = set()
        self.w.handle_event(message(9, "AFC-1 relay", "hello", stream_id=5))
        self.assertEqual(self.lin.created, [])

    def test_a_rename_that_adds_an_issue_creates_that_issue_comment(self) -> None:
        self.w.handle_event({
            "type": "update_message", "orig_subject": "plain", "subject": "OPS-9 plain",
            "stream_id": 5, "stream_name": "builds", "message_ids": [7]})
        self.assertEqual(len(self.lin.created), 1)
        self.assertIn("OPS-9|5|OPS-9 plain", self.led.entries)

    def test_a_topic_rename_carries_the_standing_comment_over(self) -> None:
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.w.handle_event({
            "type": "update_message", "orig_subject": "AFC-1 relay",
            "subject": "AFC-1 relay renamed", "stream_id": 5, "stream_name": "builds",
            "message_ids": [1]})
        self.assertEqual(len(self.lin.created), 1)  # no second comment
        self.assertIn("AFC-1|5|AFC-1 relay renamed", self.led.entries)
        self.assertNotIn("AFC-1|5|AFC-1 relay", self.led.entries)
        # and the marker is unchanged, so the comment is still found under its old key
        entry = self.led.entries["AFC-1|5|AFC-1 relay renamed"]
        self.assertEqual(entry["marker"], lr.marker_for("AFC-1", 1))

    def test_a_rename_in_a_private_stream_is_not_relayed(self) -> None:
        # The rename path honours the same public-streams guard as messages: a private
        # stream's names and links never reach Linear.
        self.w.handle_event({
            "type": "update_message", "orig_subject": "plain", "subject": "OPS-9 plain",
            "stream_id": 9, "stream_name": "secret-stream", "message_ids": [7]})
        self.assertEqual(self.lin.created, [])
        self.assertEqual(dict(self.led.entries), {})

    def test_the_permalink_names_the_message_and_nothing_else(self) -> None:
        # The topic is untrusted, so it never goes into the URL.  Zulip's `near` narrow
        # resolves a message id on its own.
        self.assertEqual(
            lr.permalink("https://z.example", 42), "https://z.example/#narrow/near/42"
        )

    def test_reconcile_survives_a_linear_outage(self) -> None:
        entry = lr.new_entry("AFC-1", 5, "builds", "AFC-1 relay", 1)
        entry["pending"] = True
        self.led.entries["AFC-1|5|AFC-1 relay"] = entry
        self.lin.fail_find = True
        self.w.reconcile()  # must not raise
        self.assertIsNone(entry["comment_id"])
        self.assertTrue(entry["pending"])  # still pending for the next registration

    # -- the uncertain write -------------------------------------------------------------
    def test_a_create_whose_answer_was_lost_is_adopted_not_stacked(self) -> None:
        # The create landed, but the process died before it could save the comment id.
        entry = lr.new_entry("AFC-1", 5, "builds", "AFC-1 relay", 1)
        entry["pending"] = True
        entry["dirty"] = True
        self.led.entries["AFC-1|5|AFC-1 relay"] = entry
        self.lin.by_marker[entry["marker"]] = "c-lost"
        self.w.reconcile()
        self.assertEqual(entry["comment_id"], "c-lost")
        self.assertEqual(self.lin.created, [])  # nothing new was created

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

    # -- untrusted text ------------------------------------------------------------------
    def test_copied_text_is_redacted_and_cannot_break_out_of_the_comment(self) -> None:
        quote = lr.neutralise('token: ghp_abcdefghijklmnopqrstuvwxyz012345 and "*emphasis*"')
        self.assertNotIn("ghp_abcdefghijklmnopqrstuvwxyz012345", quote)
        self.assertIn("redacted", quote)
        self.assertNotIn("*emphasis*", quote)  # every asterisk is escaped
        self.assertNotIn('"', quote.replace('\\"', ""))  # the quote delimiter cannot be closed early

    def test_a_rendered_comment_carries_the_marker_and_the_permalink(self) -> None:
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        body = self.lin.created[0][1]
        self.assertTrue(body.startswith(lr.MARKER))
        # The link names the message id and carries no topic text.
        self.assertIn("https://z.example/#narrow/near/1", body)
        self.assertIn("Mirrored one way from Zulip", body)

    def test_a_secret_in_the_topic_never_reaches_the_comment_body(self) -> None:
        # The topic is untrusted and can carry a credential.  The value is redacted in the
        # heading, and the topic never goes into the URL, so none of it survives into Linear.
        self.w.handle_event(message(1, "AFC-1 password=hunter2 relay", "hello"))
        body = self.lin.created[0][1]
        self.assertNotIn("hunter2", body)
        link = body.split("](", 1)[1].split(")", 1)[0]
        self.assertEqual(link, "https://z.example/#narrow/near/1")

    # -- a stream that turns private --------------------------------------------------------
    def test_a_migration_in_a_private_stream_is_refused(self) -> None:
        # The rename path refuses a private stream, and the migration refuses it too, so a
        # private topic's standing comment can never be carried over.
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.w._migrate_topic(9, "secret-stream", "AFC-1 relay", "AFC-1 relay renamed")
        self.assertIn("AFC-1|5|AFC-1 relay", self.led.entries)
        self.assertNotIn("AFC-1|9|AFC-1 relay renamed", self.led.entries)

    def test_an_entry_whose_stream_turns_private_is_dropped_not_written(self) -> None:
        # The stream was public when the entry was written and is private now, so the
        # standing comment is neither created nor edited, and the entry leaves the ledger.
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.assertEqual(len(self.lin.created), 1)
        entry = self.led.entries["AFC-1|5|AFC-1 relay"]
        entry["dirty"] = True
        self.w.public_streams = {7}  # the stream is not in the public set any more
        self.t[0] += 600
        self.w.tick()
        self.assertEqual(self.lin.updated, [])
        self.assertNotIn("AFC-1|5|AFC-1 relay", self.led.entries)

    def test_a_pending_entry_whose_stream_turns_private_is_not_reconciled(self) -> None:
        entry = lr.new_entry("AFC-1", 5, "builds", "AFC-1 relay", 1)
        entry["pending"] = True
        self.led.entries["AFC-1|5|AFC-1 relay"] = entry
        self.lin.by_marker[entry["marker"]] = "c-lost"
        self.w.public_streams = {7}
        self.w.reconcile()
        self.assertIsNone(entry["comment_id"])
        self.assertNotIn("AFC-1|5|AFC-1 relay", self.led.entries)

    def test_nothing_is_dropped_before_the_public_streams_are_known(self) -> None:
        # An empty set means the set has not been loaded yet.  It proves nothing about a
        # stream, so no entry is dropped on that account.
        self.w.handle_event(message(1, "AFC-1 relay", "hello"))
        self.w.public_streams = set()
        entry = self.led.entries["AFC-1|5|AFC-1 relay"]
        entry["dirty"] = True
        self.w.tick()
        self.assertIn("AFC-1|5|AFC-1 relay", self.led.entries)

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

    def test_a_transient_error_is_retried_and_a_not_found_is_not(self) -> None:
        sleeps: list[float] = []
        client = lr.LinearClient("tok", sleep=sleeps.append)
        calls: list[str] = []

        def flaky(req, timeout=None):
            calls.append(req.full_url)
            if len(calls) == 1:
                raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, io.BytesIO(b""))

            class Response:
                def read(self):
                    return json.dumps({"data": {"viewer": {"id": "u", "name": "Relay"}}}).encode()

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    return False

            return Response()

        with mock.patch("urllib.request.urlopen", flaky):
            self.assertEqual(client.viewer(), "Relay")
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(sleeps), 1)

        not_found_calls: list[str] = []

        def not_found(req, timeout=None):
            not_found_calls.append(req.full_url)
            raise urllib.error.HTTPError(
                req.full_url, 400, "bad", {}, io.BytesIO(b'{"msg":"issue not found"}'))

        with mock.patch("urllib.request.urlopen", not_found):
            with self.assertRaises(lr.LinearNotFound):
                client.issue_id("AFC-9")
        self.assertEqual(len(not_found_calls), 1)  # a not-found is final

    def test_the_default_teams_match_the_linkifier(self) -> None:
        import zulip_linkifiers

        self.assertEqual(set(lr.DEFAULT_TEAMS), set(zulip_linkifiers.LINEAR_TEAMS))


if __name__ == "__main__":
    unittest.main()
