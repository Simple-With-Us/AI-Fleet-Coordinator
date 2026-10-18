"""The Mac listener's #notes-relay consumer (AFC-331):  who may send, what runs, what is answered.

The consumer runs against a fake Zulip client and an injected script runner, so nothing touches Notes.app.
Every refusal is asserted to run nothing, to react with a cross mark and to reply with a reason.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_sync import notes as N
from agent_sync import notes_relay as NR
from agent_sync import zulip as Z

OWNER_ID = 1000
READER_ID = 10
TITLE = "[AFC, Claude] relay check"


class FakeClient:
    realm = "https://realm.test"

    def __init__(self):
        self.posts: list[tuple[str, dict]] = []
        self.messages: dict[int, dict] = {}
        self.files: dict[str, dict] = {}
        self.react_fails: set[str] = set()
        self.react_error: Exception | None = None  # any other failure of the reaction endpoint
        self.backlog: list[
            dict
        ] = []  # other people's messages in the topic, newer than the request
        self.message_gets: list[dict] = []

    def get(self, path, params=None):
        if (
            path == "messages"
        ):  # the topic after the anchor:  the backlog, then the replies this bot has posted
            self.message_gets.append(dict(params or {}))
            topic = list(self.backlog) + [
                {"id": 9000 + i, "sender_id": READER_ID, "content": a["content"]}
                for i, (p, a) in enumerate(self.posts)
                if p == "messages"
            ]
            newer = sorted(
                (m for m in topic if m["id"] > int(params["anchor"])), key=lambda m: m["id"]
            )
            page = newer[: int(params["num_after"])]
            return {"messages": page, "found_newest": len(newer) <= int(params["num_after"])}
        if path.startswith("messages/"):
            return {"message": self.messages[int(path.split("/")[1])]}
        if path.startswith("user_uploads/"):
            return {"url": "/user_uploads/signed/" + path[len("user_uploads/") :] + "?token=x"}
        raise AssertionError(path)

    def post(self, path, params=None):
        if path.endswith("/reactions") and params["emoji_name"] in self.react_fails:
            raise Z.ApiError(
                "Emoji '%s' does not exist" % params["emoji_name"], code="BAD_REQUEST", status=400
            )
        if path.endswith("/reactions") and self.react_error is not None:
            raise self.react_error
        self.posts.append((path, dict(params or {})))
        return {}

    @property
    def reactions(self):
        return [
            (int(p.split("/")[1]), a["emoji_name"])
            for p, a in self.posts
            if p.endswith("/reactions")
        ]

    @property
    def replies(self):
        return [a["content"] for p, a in self.posts if p == "messages"]


def person(user_id, email, *, bot=True, name="X"):
    return {"user_id": user_id, "email": email, "full_name": name, "is_bot": bot}


USERS = {
    READER_ID: person(READER_ID, "claude-bot@zulip.test"),
    11: person(11, "cursor-bot@zulip.test"),
    12: person(12, "grok-web-bot@zulip.test"),
    13: person(13, "bf-deployer-bot@zulip.test"),
    14: person(14, "fixer-grok-bot@zulip.test"),
    15: person(15, "sentry-bot@zulip.test"),
    16: person(16, "monet-bot@zulip.test"),
    OWNER_ID: person(OWNER_ID, "jay@zulip.test", bot=False),
}
SENDERS = {"CURSOR": 11, "GROK-WEB": 12, "BF-DEPLOYER": 13, "GB-FIXER": 14}


def fake_run_factory(calls, code=0, stderr=""):
    def run(argv, **kwargs):
        calls.append({"argv": list(argv), "input": kwargs.get("input"), "env": kwargs.get("env")})
        return subprocess.CompletedProcess(argv, code, b"created note id=n1\n", stderr.encode())

    return run


class RelayHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = self.tmp.name
        self.client = FakeClient()
        self.calls: list[dict] = []
        self.logs: list[tuple] = []
        self.script_ok = True
        self.runner = SimpleNamespace(
            seat="CLAUDE",
            users=dict(USERS),
            client=self.client,
            me=SimpleNamespace(user_id=READER_ID),
        )
        self.consumer = NR.Consumer(
            seat="CLAUDE",
            state_dir=self.state,
            script=lambda: Path("/fake/script.sh") if self.script_ok else None,
            env={"HOME": "/h", "PATH": "/usr/bin"},
            owner_user_id=lambda: OWNER_ID,
            runner=fake_run_factory(self.calls),
            log=lambda event, **f: self.logs.append((event, f)),
        )
        self.next_id = 100

    def message(self, sender_id, content, *, channel="notes-relay", topic=TITLE, type_="stream"):
        self.next_id += 1
        user = USERS[sender_id]
        message = {
            "id": self.next_id,
            "type": type_,
            "display_recipient": channel,
            "subject": topic,
            "stream_id": 77,
            "sender_id": sender_id,
            "sender_email": user["email"],
            "content": content,
        }
        self.client.messages[self.next_id] = message
        return message

    def request(
        self,
        identity="CURSOR",
        title="[AFC, Cursor] relay check",
        rid="request-0001",
        action="create",
        markdown="hello body",
        flags=None,
        claimed=None,
    ):
        env = N.make_envelope(action, title, markdown, flags or {}, rid, claimed or identity)
        return self.message(
            SENDERS[identity], "[%s·ab12cd34] %s" % (identity, N.render_message(env))
        )

    def process(self, message):
        return self.consumer.process(self.runner, message)

    def assert_refused(self, message, fragment):
        self.assertEqual(self.process(message), "refused")
        self.assertEqual(self.calls, [], "a refused request ran the script")
        self.assertIn((message["id"], "cross_mark"), self.client.reactions)
        self.assertNotIn((message["id"], "check"), self.client.reactions)
        reply = self.client.replies[-1]
        self.assertTrue(reply.startswith(N.REFUSAL_PREFIX), reply)
        self.assertIn(fragment, reply)


class SenderTests(RelayHarness):
    def test_a_fleet_agent_with_its_own_identity_and_agent_name_runs_and_gets_a_check_mark(self):
        message = self.request()
        self.assertEqual(self.process(message), "done")
        self.assertEqual(len(self.calls), 1)
        body = self.calls[0]["input"].decode()
        self.assertTrue(
            body.startswith("hello body\n\n---\n\n*Relayed for CURSOR by the notes relay on "), body
        )
        self.assertIn(
            "[request message](https://simplewithus.zulipchat.com/#narrow/id/%d)" % message["id"],
            body,
        )
        self.assertEqual(self.calls[0]["argv"][-2:], ["--title", "[AFC, Cursor] relay check"])
        self.assertIn((message["id"], "check"), self.client.reactions)
        self.assertEqual(self.client.replies, [])

    def test_pin_carries_no_footer_and_html_gets_an_html_one(self):
        self.assertEqual(
            self.process(self.request(rid="pin-request-001", action="pin", markdown="")), "done"
        )
        self.assertFalse(self.calls[-1]["input"], "pin sends no body, so no footer")
        env = N.with_provenance(
            N.make_envelope(
                "create", "[AFC, Cursor] t", "<p>x</p>", {"html": True}, "html-request-01", "CURSOR"
            ),
            "CURSOR",
            42,
        )
        self.assertIn(
            '<a href="https://simplewithus.zulipchat.com/#narrow/id/42">request message</a>',
            env["markdown"],
        )
        self.assertTrue(env["markdown"].startswith("<p>x</p><hr>"))

    def test_every_kind_of_agent_bot_may_send(self):
        titles = {
            "CURSOR": "[AFC, Cursor] t",
            "GROK-WEB": "[AFC, Grok Web] t",
            "BF-DEPLOYER": "[BF, BF-Deployer] t",
            "GB-FIXER": "[AFC, gb-fixer] t",
        }
        for identity, title in titles.items():
            with self.subTest(identity):
                self.assertEqual(
                    self.process(
                        self.request(identity, title, rid="rid-" + identity.lower() + "-01")
                    ),
                    "done",
                )

    def test_the_owner_account_is_refused(self):
        env = N.make_envelope("create", TITLE, "x", {}, "owner-request-1", "CLAUDE")
        self.assert_refused(self.message(OWNER_ID, N.render_message(env)), "owner")

    def test_an_integration_bot_a_retired_seat_and_an_unknown_user_are_refused(self):
        def env(
            rid,
        ):  # a refusal is labelled with its request id, and a reply with that label is posted once
            return N.render_message(N.make_envelope("create", TITLE, "x", {}, rid, "CLAUDE"))

        self.assert_refused(self.message(15, env("other-request-1")), "not a fleet agent identity")
        self.assert_refused(self.message(16, env("other-request-2")), "not a fleet agent identity")
        self.runner.users.pop(16)
        self.assert_refused(
            self.message(16, env("other-request-3")), "not a user this listener knows"
        )

    def test_the_identity_in_the_envelope_must_be_the_sender(self):
        self.assert_refused(
            self.request("CURSOR", "[AFC, Cursor] t", claimed="CLAUDE"), "is not the sender"
        )

    def test_the_agent_in_the_title_must_be_the_senders_name(self):
        self.assert_refused(
            self.request("CURSOR", "[AFC, Claude] t", rid="rid-wrong-agent-1"),
            "which is not CURSOR",
        )
        self.assert_refused(
            self.request("GB-FIXER", "[AFC, GB-Deployer] t", rid="rid-wrong-agent-2"),
            "which is not GB-FIXER",
        )
        self.assert_refused(
            self.request("CURSOR", "[AFC] no agent", rid="rid-wrong-agent-3"), "must read"
        )

    def test_a_reply_cannot_mention_anyone_through_the_sender_chosen_text(self):
        env = N.render_message(
            N.make_envelope("create", TITLE, "x", {}, "mention-request1", "@**all**")
        )
        message = self.message(SENDERS["CURSOR"], env)
        self.assertEqual(self.process(message), "refused")
        self.assertNotIn("@**all**", self.client.replies[-1])

    def test_a_message_that_is_not_an_envelope_is_refused_not_run(self):
        self.assert_refused(self.message(SENDERS["CURSOR"], "hello there"), "not a notes request")


class IdempotencyAndFailureTests(RelayHarness):
    def test_the_same_request_id_never_runs_twice(self):
        first = self.request(rid="dup-request-0001")
        again = self.request(rid="dup-request-0001")
        self.assertEqual(self.process(first), "done")
        self.assertEqual(self.process(again), "duplicate")
        self.assertEqual(len(self.calls), 1)
        self.assertIn((again["id"], "check"), self.client.reactions)

    def test_a_request_id_is_per_sender(self):
        self.assertEqual(
            self.process(self.request("CURSOR", "[AFC, Cursor] t", rid="shared-request-1")), "done"
        )
        self.assertEqual(
            self.process(self.request("GROK-WEB", "[AFC, Grok Web] t", rid="shared-request-1")),
            "done",
        )
        self.assertEqual(len(self.calls), 2)

    def test_a_request_found_started_after_a_restart_is_refused_not_rerun(self):
        message = self.request(rid="crash-request-01")
        self.consumer.processed.begin(
            "CURSOR", "crash-request-01", message["id"]
        )  # the process died after this
        self.assertEqual(self.process(message), "duplicate")
        self.assertEqual(self.calls, [])
        self.assertIn("interrupted", self.client.replies[-1])

    def test_the_processed_file_is_mode_600_and_capped(self):
        self.process(self.request(rid="mode-request-001"))
        path = Path(self.state) / NR.RELAY_DIR / NR.STATE_FILE
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        rows = {
            f"CURSOR:cap-{i}": {"outcome": "done", "ts": i} for i in range(NR.PROCESSED_LIMIT + 50)
        }
        self.consumer.processed._write(rows)
        self.consumer.processed.begin("CURSOR", "cap-new-0001", 1)
        kept = json.loads(path.read_text())["rows"]
        self.assertLessEqual(len(kept), NR.PROCESSED_LIMIT)
        self.assertIn("CURSOR:cap-new-0001", kept)

    def test_a_script_failure_is_a_cross_mark_with_the_reason_and_is_not_retried(self):
        self.consumer.runner = fake_run_factory(self.calls, code=3)
        message = self.request(rid="fail-request-001", action="pin", markdown="")
        self.assertEqual(self.process(message), "refused")
        self.assertIn("no note with that title", self.client.replies[-1])
        self.assertEqual(
            self.process(self.request(rid="fail-request-001", action="pin", markdown="")),
            "duplicate",
        )

    def test_no_script_on_this_mac_is_refused_with_a_reason(self):
        self.script_ok = False
        message = self.request()
        self.assertEqual(self.process(message), "refused")
        self.assertIn("cannot run the notes script", self.client.replies[-1])

    def test_a_cross_mark_that_zulip_does_not_know_falls_back_to_the_next_name(self):
        self.client.react_fails = {"cross_mark"}
        self.assert_refused_with_alias = None
        message = self.request("CURSOR", "[AFC, Claude] wrong agent")
        self.process(message)
        self.assertIn((message["id"], "x"), self.client.reactions)


class AttachmentTests(RelayHarness):
    def test_an_attached_body_is_fetched_checked_and_run(self):
        body = "a long sentence.  " * 700
        data = body.encode()
        base = N.make_envelope("create", "[AFC, Cursor] big", "", {}, "attach-request-1", "CURSOR")
        ref = N.with_upload(base, url="/user_uploads/2/ab/9/note.md", data=data)
        message = self.message(SENDERS["CURSOR"], N.render_message(ref))
        fetched = []
        self.consumer._download = lambda runner, path: fetched.append(path) or data
        self.assertEqual(self.process(message), "done")
        self.assertEqual(fetched, ["/user_uploads/2/ab/9/note.md"])
        self.assertTrue(
            self.calls[0]["input"].startswith(data + b"\n\n---\n\n*Relayed for "),
            "body then footer",
        )

    def test_the_consumer_scans_an_inline_and_an_uploaded_body_and_refuses_a_secret_without_quoting_it(
        self,
    ):
        token = "ghp_" + "EXAMPLE0" * 5
        body = "notes about a deploy.  the token is %s and it must not be written." % token
        inline = self.request(
            "CURSOR", "[AFC, Cursor] inline", rid="secret-inline-01", markdown=body
        )
        data = body.encode()
        ref = N.with_upload(
            N.make_envelope("create", "[AFC, Cursor] big", "", {}, "secret-upload-01", "CURSOR"),
            url="/user_uploads/2/ab/9/note.md",
            data=data,
        )
        uploaded = self.message(SENDERS["CURSOR"], N.render_message(ref))
        self.consumer._download = lambda runner, path: data
        for name, message, code in (("inline", inline, "secret"), ("uploaded", uploaded, "secret")):
            with self.subTest(name):
                self.assertEqual(self.process(message), "refused")
                self.assertIn((message["id"], "cross_mark"), self.client.reactions)
                reply = self.client.replies[-1]
                self.assertIn("looked like it contains a secret", reply)
                self.assertIn("markdown", reply)
                self.assertNotIn(token, reply)
                self.assertNotIn(token[:8], reply)
                self.assertIn(
                    ("notes-relay-failed", {"id": message["id"], "code": code}), self.logs
                )
        self.assertEqual(self.calls, [], "nothing was written")
        self.assertNotIn(token, json.dumps(self.logs, default=str))

    def test_a_secret_in_the_summary_is_refused_too(self):
        token = "ghp_" + "EXAMPLE0" * 5
        message = self.request(
            "CURSOR",
            "[AFC, Cursor] summary",
            rid="secret-summary-01",
            flags={"summary": "see " + token},
        )
        self.assertEqual(self.process(message), "refused")
        self.assertIn("summary", self.client.replies[-1])
        self.assertNotIn(token, self.client.replies[-1])
        self.assertEqual(self.calls, [])

    def test_a_tampered_attachment_is_refused(self):
        data = b"original body"
        ref = N.with_upload(
            N.make_envelope("create", "[AFC, Cursor] big", "", {}, "tamper-request-1", "CURSOR"),
            url="/user_uploads/2/ab/9/note.md",
            data=data,
        )
        message = self.message(SENDERS["CURSOR"], N.render_message(ref))
        self.consumer._download = lambda runner, path: b"tampered body"
        self.assertEqual(self.process(message), "refused")
        self.assertEqual(self.calls, [])
        self.assertIn("digest", self.client.replies[-1])

    def test_download_stays_on_the_realm_and_in_user_uploads(self):
        with self.assertRaises(N.NotesError):
            NR.Consumer._download(self.consumer, self.runner, "/user_uploads/../../etc/passwd")
        self.client.get = lambda path, params=None: {"url": "https://evil.test/x"}
        with self.assertRaises(N.NotesError):
            NR.Consumer._download(self.consumer, self.runner, "/user_uploads/2/ab/9/note.md")


class PublishFailureTests(RelayHarness):
    """The note is written once;  a failure to publish the result is retried without running it again."""

    def failing_reactions(self):
        original = self.client.post
        state = {"down": True}

        def post(path, params=None):
            if state["down"] and path.endswith("/reactions"):
                raise Z.NetworkError("zulip is unreachable")
            return original(path, params)

        self.client.post = post
        return state

    def test_a_failed_check_mark_keeps_the_spool_and_the_retry_only_posts_the_check_mark(self):
        self.consumer.start = lambda: None
        message = self.request(rid="publish-request-1")
        self.consumer.offer(self.runner, message)
        state = self.failing_reactions()
        self.assertEqual(self.consumer.drain(), 0)
        self.assertEqual(len(self.calls), 1, "the note was written")
        self.assertEqual(self.consumer.spool.ids(), [message["id"]], "the request stays pending")
        self.assertEqual(self.client.reactions, [])
        row = json.loads(Path(self.consumer.processed.path).read_text())["rows"][
            "CURSOR:publish-request-1"
        ]
        self.assertEqual(row["outcome"], "done_pending")
        state["down"] = False
        self.assertEqual(self.consumer.drain(), 1)
        self.assertEqual(len(self.calls), 1, "the retry did not write the note again")
        self.assertIn((message["id"], "check"), self.client.reactions)
        self.assertEqual(self.consumer.spool.ids(), [])
        row = json.loads(Path(self.consumer.processed.path).read_text())["rows"][
            "CURSOR:publish-request-1"
        ]
        self.assertEqual(row["outcome"], "done")

    def test_a_failed_refusal_is_retried_with_its_recorded_reason(self):
        self.consumer.start = lambda: None
        self.consumer.runner = fake_run_factory(self.calls, code=3)
        message = self.request(rid="publish-request-2", action="pin", markdown="")
        self.consumer.offer(self.runner, message)
        state = self.failing_reactions()
        self.assertEqual(self.consumer.drain(), 0)
        self.assertEqual(self.consumer.spool.ids(), [message["id"]])
        state["down"] = False
        self.assertEqual(self.consumer.drain(), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertIn("no note with that title", self.client.replies[-1])
        self.assertIn((message["id"], "cross_mark"), self.client.reactions)

    def test_a_duplicate_of_a_published_refusal_never_downgrades_its_row(self):
        self.consumer.runner = fake_run_factory(self.calls, code=3)
        first = self.request(rid="refused-request-9", action="pin", markdown="")
        self.assertEqual(self.process(first), "refused")
        replies = len(self.client.replies)
        state = self.failing_reactions()
        again = self.request(rid="refused-request-9", action="pin", markdown="")
        with self.assertRaises(Z.NetworkError):
            self.process(again)
        self.assertEqual(
            self.consumer.processed.begin("CURSOR", "refused-request-9", again["id"]), "refused"
        )
        state["down"] = False
        self.assertEqual(self.process(again), "duplicate")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(
            len(self.client.replies), replies
        )  # one refusal reply per request id, already posted
        self.assertIn((again["id"], "cross_mark"), self.client.reactions)

    def test_a_download_that_fails_on_the_network_retries_from_a_clean_start(self):
        data = b"attached body"
        ref = N.with_upload(
            N.make_envelope("create", "[AFC, Cursor] big", "", {}, "download-retry-1", "CURSOR"),
            url="/user_uploads/2/ab/9/note.md",
            data=data,
        )
        message = self.message(SENDERS["CURSOR"], N.render_message(ref))
        outcomes = [Z.NetworkError("down"), data]

        def download(runner, path):
            result = outcomes.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        self.consumer._download = download
        with self.assertRaises(Z.NetworkError):
            self.process(message)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.process(message), "done", "the second attempt ran, not 'interrupted'")
        self.assertEqual(len(self.calls), 1)

    def test_the_worker_retries_a_pending_request_on_its_own_timer(self):
        self.consumer.retry_seconds = 0.05
        message = self.request(rid="timer-request-001")
        state = self.failing_reactions()
        self.assertTrue(self.consumer.offer(self.runner, message))
        for _ in range(100):  # the first attempt fails
            if self.consumer.spool.ids() == [message["id"]] and self.calls:
                break
            threading.Event().wait(0.02)
        state["down"] = False
        for _ in range(200):
            if (message["id"], "check") in self.client.reactions:
                break
            threading.Event().wait(0.02)
        self.consumer.stop()
        self.assertIn((message["id"], "check"), self.client.reactions)
        self.assertEqual(len(self.calls), 1)


class ApiFailureTests(RelayHarness):
    """Only an unknown emoji falls through to the alias;  a retryable or permission API failure keeps the request."""

    def test_retryable_api_failures_keep_the_request_spooled_for_both_marks_and_replies(self):
        for status in (429, 500, 502, 403, 401):
            with self.subTest(status=status):
                self.client.posts.clear()
                self.calls.clear()
                self.consumer.processed._write({})
                self.consumer.start = lambda: None
                message = self.request(rid="api-failure-%d" % status)
                self.consumer.offer(self.runner, message)
                self.client.react_error = Z.ApiError("down", code="X", status=status)
                self.assertEqual(self.consumer.drain(), 0, "the request must stay pending")
                self.assertEqual(self.consumer.spool.ids(), [message["id"]])
                self.assertEqual(self.client.reactions, [])
                self.client.react_error = None
                self.assertEqual(self.consumer.drain(), 1)
                self.assertEqual(len(self.calls), 1, "written once, published on the retry")
                self.assertIn((message["id"], "check"), self.client.reactions)

    def test_a_failing_refusal_reply_is_retried_not_swallowed(self):
        self.consumer.start = lambda: None
        message = self.request("CURSOR", "[AFC, Claude] wrong agent", rid="api-reply-0001")
        self.consumer.offer(self.runner, message)
        original = self.client.post

        def reply_down(path, params=None):
            if path == "messages":
                raise Z.ApiError("busy", code="RATE_LIMIT_HIT", status=429)
            return original(path, params)

        self.client.post = reply_down
        self.assertEqual(self.consumer.drain(), 0)
        self.assertEqual(self.consumer.spool.ids(), [message["id"]])
        self.client.post = original
        self.assertEqual(self.consumer.drain(), 1)
        self.assertEqual(len(self.client.replies), 1)

    def test_only_an_unknown_emoji_falls_through_to_the_alias(self):
        self.client.react_fails = {"check"}  # Zulip: "Emoji 'check' does not exist"
        message = self.request(rid="alias-request-001")
        self.assertEqual(self.process(message), "done")
        self.assertIn((message["id"], "white_check_mark"), self.client.reactions)

    def test_a_permanent_refusal_is_logged_and_not_retried_forever(self):
        self.client.react_error = Z.ApiError("Invalid message(s)", code="BAD_REQUEST", status=400)
        message = self.request(rid="permanent-request-1")
        self.assertEqual(self.process(message), "done")
        self.assertIn("notes-relay-reaction-refused", [event for event, _ in self.logs])

    def test_classification(self):
        self.assertTrue(
            NR.unknown_emoji(Z.ApiError("Emoji 'x' does not exist", code="BAD_REQUEST", status=400))
        )
        self.assertFalse(
            NR.unknown_emoji(Z.ApiError("rate limited", code="RATE_LIMIT_HIT", status=429))
        )
        self.assertFalse(
            NR.unknown_emoji(Z.ApiError("Invalid message(s)", code="BAD_REQUEST", status=400))
        )
        for status in (401, 403, 408, 429, 500, 503, None):
            self.assertTrue(NR.retryable(Z.ApiError("x", status=status)), status)
        for status in (400, 404):
            self.assertFalse(NR.retryable(Z.ApiError("x", status=status)), status)


class OwnershipTests(RelayHarness):
    """One owner of the spool:  across consumers (a reload's replacement) and across processes (two daemons)."""

    def second(self):
        other = NR.Consumer(
            seat="CLAUDE",
            state_dir=self.state,
            script=lambda: Path("/fake/script.sh"),
            env={},
            owner_user_id=lambda: OWNER_ID,
            runner=fake_run_factory(self.calls),
            log=lambda event, **f: self.logs.append((event, f)),
        )
        other.start = lambda: None
        other.reader = self.runner
        return other

    def test_a_second_consumer_cannot_drain_while_the_first_holds_the_spool(self):
        self.consumer.start = lambda: None
        message = self.request(rid="owner-request-001")
        self.consumer.offer(self.runner, message)
        other = self.second()
        with self.consumer._owner_lock() as owned:
            self.assertTrue(owned)
            self.assertEqual(
                other.drain(), 0, "the lock is held:  the other consumer claims nothing"
            )
            self.assertEqual(self.calls, [])
            self.assertIn("notes-relay-busy", [event for event, _ in self.logs])
        self.assertEqual(other.drain(), 1, "released:  the spool is drained once")
        self.assertEqual(len(self.calls), 1)

    def test_two_drains_in_a_row_never_run_a_request_twice(self):
        self.consumer.start = lambda: None
        self.consumer.offer(self.runner, self.request(rid="owner-request-002"))
        other = self.second()
        self.assertEqual(self.consumer.drain(), 1)
        self.assertEqual(other.drain(), 0)
        self.assertEqual(len(self.calls), 1)

    def test_stop_waits_for_the_worker_and_reports_when_it_will_not_leave(self):
        gate, inside = threading.Event(), threading.Event()

        def slow(argv, **kwargs):
            inside.set()
            gate.wait(10)
            return subprocess.CompletedProcess(argv, 0, b"created note id=n1\n", b"")

        self.consumer.runner = slow
        self.assertTrue(self.consumer.offer(self.runner, self.request(rid="owner-request-003")))
        self.assertTrue(inside.wait(5), "the worker is inside the script")
        self.assertFalse(self.consumer.stop(0.2), "still inside a request:  not stopped")
        gate.set()
        self.assertTrue(self.consumer.stop(5), "left once the request finished")
        self.assertFalse(self.consumer.thread.is_alive())

    def test_changing_the_consumer_seat_strands_nothing(self):
        """The spool and the processed ids are seat-independent:  a pending request of the old consumer seat (its
        cursor has long moved on) is drained by the new one."""
        self.consumer.start = lambda: None
        message = self.request(rid="seat-change-0001")
        self.assertTrue(self.consumer.offer(self.runner, message))
        pending = Path(self.state) / NR.RELAY_DIR / NR.SPOOL_DIR / ("%d.json" % message["id"])
        self.assertTrue(pending.exists())
        self.assertFalse(
            (Path(self.state) / "CLAUDE").exists(), "nothing is kept under the old seat's directory"
        )
        # The knob (and the local config) now name CODEX:  a new consumer for another seat on the same state root.
        new_runner = SimpleNamespace(
            seat="CODEX", users=dict(USERS), client=self.client, me=SimpleNamespace(user_id=11)
        )
        successor = NR.Consumer(
            seat="CODEX",
            state_dir=self.state,
            script=lambda: Path("/fake/script.sh"),
            env={},
            owner_user_id=lambda: OWNER_ID,
            runner=fake_run_factory(self.calls),
            log=lambda event, **f: self.logs.append((event, f)),
        )
        successor.start = lambda: None
        self.assertEqual(successor.spool.path, self.consumer.spool.path)
        self.assertEqual(successor.processed.path, self.consumer.processed.path)
        successor.resume(new_runner)
        self.assertEqual(successor.drain(), 1)
        self.assertEqual(len(self.calls), 1, "the pending request ran once, under the new consumer")
        self.assertIn((message["id"], "check"), self.client.reactions)
        self.assertFalse(pending.exists())
        # And the old consumer's record of it stays valid:  the same request id is a duplicate, not a second write.
        self.assertEqual(self.process(self.request(rid="seat-change-0001")), "duplicate")
        self.assertEqual(len(self.calls), 1)

    def test_a_stopped_worker_leaves_the_rest_of_the_spool_for_its_successor(self):
        self.consumer.start = lambda: None
        for i in range(3):
            self.consumer.offer(self.runner, self.request(rid="owner-request-1%02d" % i))
        self.consumer.stopped.set()
        self.assertEqual(self.consumer.drain(), 0)
        self.assertEqual(len(self.consumer.spool.ids()), 3)


class TimeoutTests(RelayHarness):
    """A script that hangs may already have written the note:  that is an unknown outcome, never a safe failure."""

    def hang(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), "input": kwargs.get("input")})
        raise subprocess.TimeoutExpired(argv, 1)

    def test_a_timeout_is_recorded_as_unknown_and_a_retry_never_runs_the_action_again(self):
        self.consumer.runner = self.hang
        message = self.request(rid="timeout-request-01")
        self.assertEqual(self.process(message), "refused")
        self.assertEqual(len(self.calls), 1)
        self.assertIn("may or may not", self.client.replies[-1])
        row = json.loads(Path(self.consumer.processed.path).read_text())["rows"][
            "CURSOR:timeout-request-01"
        ]
        self.assertEqual(row["outcome"], "unknown", "not a definite refusal")
        # The sender (or a backfill) delivers the same request again, with a script that would now work:
        self.consumer.runner = fake_run_factory(self.calls)
        again = self.request(rid="timeout-request-01")
        self.assertEqual(self.process(again), "duplicate")
        self.assertEqual(len(self.calls), 1, "the retry did not run the script")
        self.assertIn((again["id"], "cross_mark"), self.client.reactions)
        self.assertEqual(len(self.client.replies), 1, "the earlier reply already says it")

    def test_a_failed_publish_of_an_unknown_outcome_keeps_the_request_spooled(self):
        self.consumer.start = lambda: None
        self.consumer.runner = self.hang
        message = self.request(rid="timeout-request-02")
        self.consumer.offer(self.runner, message)
        original = self.client.post

        def down(path, params=None):
            raise Z.NetworkError("zulip is unreachable")

        self.client.post = down
        self.assertEqual(self.consumer.drain(), 0)
        self.assertEqual(self.consumer.spool.ids(), [message["id"]])
        self.client.post = original
        self.consumer.runner = fake_run_factory(self.calls)
        self.assertEqual(self.consumer.drain(), 1)
        self.assertEqual(len(self.calls), 1, "still only the one (timed out) run")
        self.assertIn("may or may not", self.client.replies[-1])


class SelfSentTests(RelayHarness):
    """A session of the reading seat that runs elsewhere (the server) relays as that same bot."""

    def own_request(self, rid="self-request-0001", title=TITLE):
        env = N.make_envelope("create", title, "from the server", {}, rid, "CLAUDE")
        return self.message(READER_ID, "[CLAUDE\u00b7ab12cd34] " + N.render_message(env))

    def test_the_reader_takes_its_own_envelopes_and_runs_them(self):
        message = self.own_request()
        self.assertTrue(self.consumer.wants(self.runner, message))
        self.assertEqual(self.process(message), "done")
        self.assertTrue(
            self.calls[0]["input"].startswith(b"from the server\n\n---\n\n*Relayed for CLAUDE ")
        )
        self.assertIn((message["id"], "check"), self.client.reactions)

    def test_its_own_refusals_acks_and_chatter_are_never_taken(self):
        for text in (
            "notes-relay refused abc-request-01: because",
            "  notes-relay refused (message 5): x",
            "hello",
            "```json\n{not json}\n```",
        ):
            self.assertFalse(self.consumer.wants(self.runner, self.message(READER_ID, text)), text)

    def test_its_own_envelope_still_obeys_the_title_rule(self):
        self.assertEqual(
            self.process(self.own_request("self-request-0002", "[AFC, Codex] wrong agent")),
            "refused",
        )
        self.assertEqual(self.calls, [])


class WorkerTests(RelayHarness):
    def test_wants_only_the_relay_channel_of_the_reading_seat_but_every_runner_sees_it_in_the_channel(
        self,
    ):
        message = self.request(rid="thread-request-01")
        self.assertTrue(self.consumer.wants(self.runner, message))
        other_channel = self.message(SENDERS["CURSOR"], "x", channel="agent-sync")
        self.assertFalse(self.consumer.in_channel(other_channel))
        own = self.message(READER_ID, "notes-relay refused x: y")
        self.assertFalse(self.consumer.wants(self.runner, own))
        dm = self.message(SENDERS["CURSOR"], "x", type_="private")
        self.assertFalse(self.consumer.in_channel(dm))
        other_seat = SimpleNamespace(
            seat="CODEX", users={}, client=None, me=SimpleNamespace(user_id=11)
        )
        self.assertTrue(
            self.consumer.in_channel(message),
            "any seat's runner treats a relay message as consumed",
        )
        self.assertFalse(
            self.consumer.wants(other_seat, message), "only the configured seat acts on it"
        )

    def test_offer_spools_the_request_durably_then_a_worker_thread_answers_and_clears_it(self):
        message = self.request(rid="thread-request-02")
        done = threading.Event()
        original = self.consumer.process

        def process(runner, msg):
            try:
                return original(runner, msg)
            finally:
                done.set()

        self.consumer.process = process
        self.assertTrue(self.consumer.offer(self.runner, message))
        spooled = Path(self.state) / NR.RELAY_DIR / NR.SPOOL_DIR / ("%d.json" % message["id"])
        self.assertTrue(done.wait(5))
        self.consumer.stop()
        self.assertEqual(len(self.calls), 1)
        for _ in range(100):  # the worker removes the file just after process returns
            if not spooled.exists():
                break
            threading.Event().wait(0.02)
        self.assertFalse(spooled.exists())

    def test_a_spooled_request_is_mode_600_and_survives_without_a_worker(self):
        self.consumer.start = lambda: None  # no worker thread:  nothing drains
        messages = [self.request(rid="queued-request-%02d" % i) for i in range(5)]
        for message in messages:
            self.assertTrue(self.consumer.offer(self.runner, message))
        spool = Path(self.state) / NR.RELAY_DIR / NR.SPOOL_DIR
        self.assertEqual(stat.S_IMODE(os.stat(spool).st_mode), 0o700)
        for message in messages:
            self.assertEqual(
                stat.S_IMODE(os.stat(spool / ("%d.json" % message["id"])).st_mode), 0o600
            )
        self.assertEqual(self.calls, [])
        self.assertEqual(self.consumer.drain(), 5)
        self.assertEqual(len(self.calls), 5)
        self.assertEqual(self.consumer.spool.ids(), [])

    def test_a_full_signal_queue_never_drops_a_request(self):
        """The old in-memory queue dropped past 200;  now the queue is only a wake-up and the spool holds the work."""
        self.consumer.start = lambda: None
        count = 250
        for i in range(count):
            self.assertTrue(
                self.consumer.offer(self.runner, {"id": 5000 + i, "type": "stream", "x": i})
            )
        self.assertEqual(self.consumer.dropped, 0)
        self.assertEqual(len(self.consumer.spool.ids()), count)
        self.assertEqual(self.logs, [])

    def test_a_request_that_cannot_be_kept_is_reported_so_the_caller_holds_its_cursor(self):
        self.consumer.start = lambda: None
        self.consumer.spool.limit = 2
        results = [
            self.consumer.offer(self.runner, {"id": 7000 + i, "type": "stream"}) for i in range(4)
        ]
        self.assertEqual(results, [True, True, False, False])
        self.assertEqual(self.consumer.dropped, 2)
        self.assertEqual([event for event, _ in self.logs], ["notes-relay-not-accepted"] * 2)
        self.assertFalse(
            self.consumer.offer(self.runner, {"type": "stream"}),
            "a message without an id cannot be kept",
        )

    def test_a_restart_drains_what_the_last_run_left(self):
        self.consumer.start = lambda: None
        message = self.request(rid="restart-request-1")
        self.assertTrue(self.consumer.offer(self.runner, message))
        fresh = NR.Consumer(
            seat="CLAUDE",
            state_dir=self.state,
            script=lambda: Path("/fake/script.sh"),
            env={},
            owner_user_id=lambda: OWNER_ID,
            runner=fake_run_factory(self.calls),
            log=lambda e, **f: None,
        )
        fresh.start = lambda: None
        fresh.resume(self.runner)
        self.assertEqual(fresh.drain(), 1)
        self.assertEqual(len(self.calls), 1)
        self.assertIn((message["id"], "check"), self.client.reactions)

    def test_a_failure_to_answer_keeps_the_request_for_the_next_wake_up(self):
        self.consumer.start = lambda: None
        message = self.request(rid="retry-request-001")
        self.consumer.offer(self.runner, message)
        original = self.consumer.process
        self.consumer.process = mock.Mock(side_effect=Z.NetworkError("zulip is down"))
        self.assertEqual(self.consumer.drain(), 0)
        self.assertEqual(self.consumer.spool.ids(), [message["id"]])
        self.assertIn("notes-relay-retry", [event for event, _ in self.logs])
        self.consumer.process = original
        self.assertEqual(self.consumer.drain(), 1)
        self.assertEqual(self.consumer.spool.ids(), [])


class RefusalReplayTests(RelayHarness):
    """Zulip's send-message API takes no idempotency key, so a replayed refusal is made safe by looking first."""

    def test_replaying_a_refused_message_posts_one_reply(self):
        message = self.request("CURSOR", "[AFC, Claude] wrong agent", rid="replay-request-01")
        self.assertEqual(self.process(message), "refused")
        self.assertEqual(len(self.client.replies), 1)
        # A crash after the reply but before the cursor moved:  the same message is processed again.
        self.assertEqual(self.process(message), "refused")
        self.assertEqual(
            len(self.client.replies), 1, "the replay found the earlier reply and posted nothing"
        )
        self.assertEqual(
            self.client.reactions.count((message["id"], "cross_mark")),
            2,
            "the reaction is idempotent in Zulip",
        )

    def test_a_crash_between_the_reaction_and_the_reply_still_gets_its_reply(self):
        message = self.request("CURSOR", "[AFC, Claude] wrong agent", rid="replay-request-02")
        original_post = self.client.post

        def crashing(path, params=None):
            if path == "messages":
                raise Z.NetworkError("died before the reply")
            return original_post(path, params)

        self.client.post = crashing
        with self.assertRaises(
            Z.NetworkError
        ):  # not swallowed:  in the listener the request stays spooled
            self.process(message)
        self.assertEqual(self.client.replies, [])
        self.client.post = original_post
        self.process(message)
        self.assertEqual(len(self.client.replies), 1)

    def test_a_reply_posted_after_a_long_backlog_is_found_and_not_posted_twice(self):
        message = self.request("CURSOR", "[AFC, Claude] wrong agent", rid="replay-request-03")
        self.assertEqual(self.process(message), "refused")
        self.assertEqual(len(self.client.replies), 1)
        # Far more than one page of other messages arrive in the topic after the request, newer than the refusal's
        # position in this fake's ordering, so the refusal is not in the first page of 30 (or of 100).
        self.client.backlog = [
            {"id": 2000 + i, "sender_id": OWNER_ID, "content": "chatter %d" % i} for i in range(250)
        ]
        self.assertEqual(self.process(message), "refused")
        self.assertEqual(
            len(self.client.replies), 1, "the replay found the refusal beyond the first 30 messages"
        )
        self.assertGreater(len(self.client.message_gets), 1)

    def test_the_scan_pages_forward_until_the_topics_newest_message(self):
        message = self.request("CURSOR", "[AFC, Claude] wrong agent", rid="replay-request-04")
        self.client.backlog = [
            {"id": 2000 + i, "sender_id": READER_ID, "content": "other bot post %d" % i}
            for i in range(250)
        ]
        self.assertEqual(self.process(message), "refused")
        self.assertEqual(
            len(self.client.replies), 1, "no refusal existed yet, so exactly one was posted"
        )
        anchors = [g["anchor"] for g in self.client.message_gets]
        self.assertEqual(anchors[0], message["id"])
        self.assertEqual(
            anchors[1:3], [2099, 2199], "each page continues after the last id of the one before"
        )

    def test_a_scan_that_hits_its_bound_logs_and_answers(self):
        message = self.request("CURSOR", "[AFC, Claude] wrong agent", rid="replay-request-05")
        self.client.backlog = [
            {"id": 2000 + i, "sender_id": READER_ID, "content": "x"} for i in range(60)
        ]
        with (
            mock.patch.object(NR, "REPLY_SCAN_PAGE", 10),
            mock.patch.object(NR, "REPLY_SCAN_PAGES", 3),
        ):
            self.assertEqual(self.process(message), "refused")
        self.assertEqual(len(self.client.replies), 1)
        self.assertEqual(len(self.client.message_gets), 3)
        self.assertIn("notes-relay-reply-scan-capped", [e for e, _ in self.logs])

    def test_a_refusal_of_a_parsed_envelope_is_labelled_with_its_request_id_for_notes_status(self):
        cases = {
            "wrong-agent-title": self.request("CURSOR", "[AFC, Claude] t", rid="label-request-01"),
            "wrong-identity": self.request(
                "CURSOR", "[AFC, Cursor] t", rid="label-request-02", claimed="CLAUDE"
            ),
        }
        env = N.render_message(
            N.make_envelope("create", TITLE, "x", {}, "label-request-03", "CLAUDE")
        )
        cases["owner"] = self.message(OWNER_ID, env)
        cases["integration-bot"] = self.message(
            15,
            N.render_message(
                N.make_envelope("create", TITLE, "x", {}, "label-request-04", "CLAUDE")
            ),
        )
        wanted = {
            "wrong-agent-title": "label-request-01",
            "wrong-identity": "label-request-02",
            "owner": "label-request-03",
            "integration-bot": "label-request-04",
        }
        for name, message in cases.items():
            with self.subTest(name):
                self.assertEqual(self.process(message), "refused")
                reply = self.client.replies[-1]
                self.assertTrue(reply.startswith(N.REFUSAL_PREFIX + wanted[name] + ":"), reply)
                self.assertNotIn("(message", reply)

    def test_a_message_with_no_parsing_envelope_still_falls_back_to_its_message_id(self):
        message = self.message(SENDERS["CURSOR"], "no fence here at all")
        self.assertEqual(self.process(message), "refused")
        self.assertTrue(
            self.client.replies[-1].startswith(N.REFUSAL_PREFIX + "(message %d)" % message["id"])
        )

    def test_a_refusal_reply_for_a_longer_request_id_is_not_taken_for_this_ones(self):
        longer = self.request("CURSOR", "[AFC, Claude] t", rid="exact-request-12")
        shorter = self.request("CURSOR", "[AFC, Claude] t", rid="exact-request-1")
        self.assertEqual(self.process(longer), "refused")
        self.assertEqual(self.process(shorter), "refused")
        self.assertEqual(len(self.client.replies), 2, "each request gets its own reply")
        self.assertTrue(self.client.replies[0].startswith("notes-relay refused exact-request-12:"))
        self.assertTrue(self.client.replies[1].startswith("notes-relay refused exact-request-1:"))
        self.assertEqual(N.refusal_label("exact-request-1"), "notes-relay refused exact-request-1:")

    def test_a_token_shaped_identity_reaches_neither_the_log_nor_the_reply_nor_the_stored_reason(
        self,
    ):
        token = "ghp_" + "EXAMPLE0" * 5
        env = N.render_message(N.make_envelope("create", TITLE, "x", {}, "token-request-01", token))
        message = self.message(SENDERS["CURSOR"], env)
        self.assertEqual(self.process(message), "refused")
        seen = json.dumps(self.logs, default=str) + "\n".join(self.client.replies)
        self.assertNotIn(token, seen)
        self.assertNotIn(token[:12], seen)
        self.assertIn(
            ("notes-relay-refused", {"id": message["id"], "code": "identity_mismatch"}), self.logs
        )
        self.assertIn("withheld", self.client.replies[-1])
        stored = Path(self.state) / "notes-relay" / "processed.json"
        if stored.exists():
            self.assertNotIn(token, stored.read_text())

    def test_every_validation_refusal_logs_a_fixed_code_and_nothing_the_sender_typed(self):
        env = N.render_message(
            N.make_envelope("create", TITLE, "x", {}, "code-request-001", "CLAUDE")
        )
        cases = {
            "sender_owner": self.message(OWNER_ID, env),
            "sender_identity": self.message(15, env),
            "identity_mismatch": self.request(
                "CURSOR", "[AFC, Cursor] t", rid="code-request-002", claimed="CLAUDE"
            ),
            "title": self.request("CURSOR", "[AFC, Claude] t", rid="code-request-003"),
            "envelope": self.message(SENDERS["CURSOR"], "no fence here"),
        }
        for code, message in cases.items():
            with self.subTest(code):
                self.assertEqual(self.process(message), "refused")
                self.assertIn(
                    ("notes-relay-refused", {"id": message["id"], "code": code}), self.logs
                )

    def test_a_refusal_without_a_request_id_is_keyed_by_its_message(self):
        first = self.message(SENDERS["CURSOR"], "hello there")
        second = self.message(SENDERS["CURSOR"], "hello again")
        self.process(first)
        self.process(second)
        self.assertEqual(
            len(self.client.replies),
            2,
            "two different bad messages in one topic each get their reply",
        )
        self.assertIn("(message %d)" % first["id"], self.client.replies[0])
        self.assertIn("(message %d)" % second["id"], self.client.replies[1])
        self.process(first)
        self.assertEqual(len(self.client.replies), 2)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
