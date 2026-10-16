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

    def get(self, path, params=None):
        if path.startswith("messages/"):
            return {"message": self.messages[int(path.split("/")[1])]}
        if path.startswith("user_uploads/"):
            return {"url": "/user_uploads/signed/" + path[len("user_uploads/"):] + "?token=x"}
        raise AssertionError(path)

    def post(self, path, params=None):
        if path.endswith("/reactions") and params["emoji_name"] in self.react_fails:
            raise Z.ApiError("no such emoji", code="BAD_REQUEST", status=400)
        self.posts.append((path, dict(params or {})))
        return {}

    @property
    def reactions(self):
        return [(int(p.split("/")[1]), a["emoji_name"]) for p, a in self.posts if p.endswith("/reactions")]

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
        self.runner = SimpleNamespace(seat="CLAUDE", users=dict(USERS), client=self.client,
                                      me=SimpleNamespace(user_id=READER_ID))
        self.consumer = NR.Consumer(
            seat="CLAUDE", state_dir=self.state, script=lambda: Path("/fake/script.sh") if self.script_ok else None,
            env={"HOME": "/h", "PATH": "/usr/bin"}, owner_user_id=lambda: OWNER_ID,
            runner=fake_run_factory(self.calls), log=lambda event, **f: self.logs.append((event, f)))
        self.next_id = 100

    def message(self, sender_id, content, *, channel="notes-relay", topic=TITLE, type_="stream"):
        self.next_id += 1
        user = USERS[sender_id]
        message = {"id": self.next_id, "type": type_, "display_recipient": channel, "subject": topic, "stream_id": 77,
                   "sender_id": sender_id, "sender_email": user["email"], "content": content}
        self.client.messages[self.next_id] = message
        return message

    def request(self, identity="CURSOR", title="[AFC, Cursor] relay check", rid="request-0001", action="create",
                markdown="hello body", flags=None, claimed=None):
        env = N.make_envelope(action, title, markdown, flags or {}, rid, claimed or identity)
        return self.message(SENDERS[identity], "[%s·ab12cd34] %s" % (identity, N.render_message(env)))

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
        self.assertEqual(self.calls[0]["input"], b"hello body")
        self.assertEqual(self.calls[0]["argv"][-2:], ["--title", "[AFC, Cursor] relay check"])
        self.assertIn((message["id"], "check"), self.client.reactions)
        self.assertEqual(self.client.replies, [])

    def test_every_kind_of_agent_bot_may_send(self):
        titles = {"CURSOR": "[AFC, Cursor] t", "GROK-WEB": "[AFC, Grok Web] t", "BF-DEPLOYER": "[BF, BF-Deployer] t",
                  "GB-FIXER": "[AFC, gb-fixer] t"}
        for identity, title in titles.items():
            with self.subTest(identity):
                self.assertEqual(self.process(self.request(identity, title, rid="rid-" + identity.lower() + "-01")), "done")

    def test_the_owner_account_is_refused(self):
        env = N.make_envelope("create", TITLE, "x", {}, "owner-request-1", "CLAUDE")
        self.assert_refused(self.message(OWNER_ID, N.render_message(env)), "owner")

    def test_an_integration_bot_a_retired_seat_and_an_unknown_user_are_refused(self):
        env = N.render_message(N.make_envelope("create", TITLE, "x", {}, "other-request-1", "CLAUDE"))
        self.assert_refused(self.message(15, env), "not a fleet agent identity")
        self.assert_refused(self.message(16, env), "not a fleet agent identity")
        self.runner.users.pop(16)
        self.assert_refused(self.message(16, env), "not a user this listener knows")

    def test_the_identity_in_the_envelope_must_be_the_sender(self):
        self.assert_refused(self.request("CURSOR", "[AFC, Cursor] t", claimed="CLAUDE"), "is not the sender")

    def test_the_agent_in_the_title_must_be_the_senders_name(self):
        self.assert_refused(self.request("CURSOR", "[AFC, Claude] t"), "which is not CURSOR")
        self.assert_refused(self.request("GB-FIXER", "[AFC, GB-Deployer] t", rid="rid-wrong-agent"), "which is not GB-FIXER")
        self.assert_refused(self.request("CURSOR", "[AFC] no agent"), "must read")

    def test_a_reply_cannot_mention_anyone_through_the_sender_chosen_text(self):
        env = N.render_message(N.make_envelope("create", TITLE, "x", {}, "mention-request1", "@**all**"))
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
        self.assertEqual(self.process(self.request("CURSOR", "[AFC, Cursor] t", rid="shared-request-1")), "done")
        self.assertEqual(self.process(self.request("GROK-WEB", "[AFC, Grok Web] t", rid="shared-request-1")), "done")
        self.assertEqual(len(self.calls), 2)

    def test_a_request_found_started_after_a_restart_is_refused_not_rerun(self):
        message = self.request(rid="crash-request-01")
        self.consumer.processed.begin("CURSOR", "crash-request-01", message["id"])  # the process died after this
        self.assertEqual(self.process(message), "duplicate")
        self.assertEqual(self.calls, [])
        self.assertIn("interrupted", self.client.replies[-1])

    def test_the_processed_file_is_mode_600_and_capped(self):
        self.process(self.request(rid="mode-request-001"))
        path = Path(self.state) / "CLAUDE" / NR.STATE_FILE
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        rows = {f"CURSOR:cap-{i}": {"outcome": "done", "ts": i} for i in range(NR.PROCESSED_LIMIT + 50)}
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
        self.assertEqual(self.process(self.request(rid="fail-request-001", action="pin", markdown="")), "duplicate")

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
        body = ("a long sentence.  " * 700)
        data = body.encode()
        base = N.make_envelope("create", "[AFC, Cursor] big", "", {}, "attach-request-1", "CURSOR")
        ref = N.with_upload(base, url="/user_uploads/2/ab/9/note.md", data=data)
        message = self.message(SENDERS["CURSOR"], N.render_message(ref))
        fetched = []
        self.consumer._download = lambda runner, path: fetched.append(path) or data
        self.assertEqual(self.process(message), "done")
        self.assertEqual(fetched, ["/user_uploads/2/ab/9/note.md"])
        self.assertEqual(self.calls[0]["input"], data)

    def test_a_tampered_attachment_is_refused(self):
        data = b"original body"
        ref = N.with_upload(N.make_envelope("create", "[AFC, Cursor] big", "", {}, "tamper-request-1", "CURSOR"),
                            url="/user_uploads/2/ab/9/note.md", data=data)
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


class WorkerTests(RelayHarness):
    def test_offer_hands_off_to_a_worker_thread_and_wants_only_the_relay_channel_of_the_reading_seat(self):
        message = self.request(rid="thread-request-01")
        self.assertTrue(self.consumer.wants(self.runner, message))
        other_channel = self.message(SENDERS["CURSOR"], "x", channel="agent-sync")
        self.assertFalse(self.consumer.wants(self.runner, other_channel))
        own = self.message(READER_ID, "notes-relay refused x: y")
        self.assertFalse(self.consumer.wants(self.runner, own))
        dm = self.message(SENDERS["CURSOR"], "x", type_="private")
        self.assertFalse(self.consumer.wants(self.runner, dm))
        self.assertFalse(self.consumer.wants(SimpleNamespace(seat="CODEX", users={}, client=None, me=None), message))
        done = threading.Event()
        original = self.consumer.process

        def process(runner, msg):
            try:
                return original(runner, msg)
            finally:
                done.set()
        self.consumer.process = process
        self.consumer.offer(self.runner, message)
        self.assertTrue(done.wait(5))
        self.consumer.stop()
        self.assertEqual(len(self.calls), 1)

    def test_a_full_queue_drops_with_a_log_line_instead_of_blocking(self):
        consumer = NR.Consumer(seat="CLAUDE", state_dir=self.state, script=lambda: None, env={},
                               log=lambda event, **f: self.logs.append((event, f)))
        consumer.start = lambda: None  # no worker:  the queue fills
        for i in range(NR.QUEUE_LIMIT + 3):
            consumer.offer(self.runner, {"id": i})
        self.assertEqual(consumer.dropped, 3)
        self.assertIn("notes-relay-dropped", [event for event, _ in self.logs])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
