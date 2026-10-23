"""The Operator's registration question in the chat's own topic:  same sending rules as the candidate list.

The properties pinned here are the owner rulings the candidate list established (Oct 21-22
2026), applied to the owner direction of Oct 23 2026 that the registering chat is who is
asked:  the question goes out as the pinned Operator bot and never as a seat's own bot, to
the chat's channel/topic and never as an owner DM, a missing credential or a missing route
is a clean refusal rather than an exception, and the body carries the question and its ref -
never a secret.  The objects are the real ones:  a real ZulipClient against the fake realm
and the real service_identity checks.
"""

from __future__ import annotations

import base64
import secrets
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_sync import operator_candidates as OC
from agent_sync import operator_registration as OREG
from agent_sync import operator_registration_ask as ORASK
from agent_sync import textfmt
from agent_sync import zulip as Z
from agent_sync.tests.fake_zulip import FakeZulip

OPERATOR_ID = 9101
OPERATOR_EMAIL = "operator-bot@zulip.test"
SEAT_EMAIL = "claude-bot@zulip.test"
OWNER_ID = 12
AFC_PROJECT = "fixture-afc-project"
CHANNEL = "agent-sync"
TOPIC = "AFC Work"

ENTRY = {
    "entry_id": "reg-abc123def456",
    "platform": "claude",
    "profile_scope": "default",
    "session_id": "sess-aaaaaaaa",
    "seat": "CLAUDE",
    "requester": "catalog",
    "evidence_location": "",
    "state": OREG.STATE_ASKED,
    "asked_at": 1000.0,
    "deadline": 1000.0 + OREG.DEFAULT_TIMEOUT_SECONDS,
    "route": {"channel": CHANNEL, "topic": TOPIC},
}
QUESTION = OREG.question_for(ENTRY)


class _LocationQuestionFixture:
    def setUp(self) -> None:
        self.seat_key = secrets.token_hex(16)
        self.operator_key = secrets.token_hex(16)
        self.fake = FakeZulip(self.seat_key, heartbeat=0.1)
        self.fake.start()
        self.addCleanup(self.fake.stop)
        self.fake.add_bot(OPERATOR_EMAIL, "Operator", self.operator_key, user_id=OPERATOR_ID)
        self.tmp = Path(tempfile.mkdtemp(prefix="operator-registration-ask-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = self.tmp / "state"

    def env(self, **overrides: str) -> dict[str, str]:
        base = {
            "HOME": str(self.tmp),
            "AGENT_SYNC_STATE_DIR": str(self.state),
            "AGENT_SYNC_INFISICAL": "1",
            "INFISICAL_FLEET_OPS_PROJECT_ID": AFC_PROJECT,
            "OPERATOR_ZULIP_USER_ID": str(OPERATOR_ID),
            "OPERATOR_ZULIP_EMAIL": OPERATOR_EMAIL,
            "ZULIP_SITE": self.fake.url,
        }
        base.update(overrides)
        return base

    def fetch(self, names):
        self.assertEqual(
            tuple(names), ("ZULIP_OPERATOR_EMAIL", "ZULIP_OPERATOR_API_KEY")
        )
        return {
            "ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL,
            "ZULIP_OPERATOR_API_KEY": self.operator_key,
        }

    def send(self, **overrides):
        kwargs = dict(
            question=QUESTION,
            entry_id=ENTRY["entry_id"],
            channel=CHANNEL,
            topic=TOPIC,
            env=self.env(),
            owner_user_id=OWNER_ID,
            fetch=self.fetch,
            now=1000.0,
        )
        kwargs.update(overrides)
        return ORASK.send_location_question(**kwargs)

    def auth_for(self, email: str, key: str) -> str:
        return "Basic " + base64.b64encode(("%s:%s" % (email, key)).encode()).decode()

    def topic_posts(self) -> list[dict]:
        return [m for m in self.fake.messages if m["type"] == "stream"]



class LocationQuestionTests(_LocationQuestionFixture, unittest.TestCase):
    """The send rules, against the fake realm."""

    def test_the_question_goes_out_as_the_operator_bot_in_the_chats_topic(self) -> None:
        out = self.send()
        self.assertEqual(out["status"], "sent", out["reason"])
        posts = self.topic_posts()
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["sender_email"], OPERATOR_EMAIL)
        self.assertEqual(posts[0]["display_recipient"], CHANNEL)
        self.assertEqual(posts[0]["subject"], TOPIC)
        self.assertEqual(posts[0]["content"], textfmt.sentence_gap(QUESTION))
        self.assertIn(ENTRY["entry_id"], posts[0]["content"])
        # Nothing goes to the owner by DM, and every request used the Operator credential.
        self.assertEqual([m for m in self.fake.messages if m["type"] == "private"], [])
        operator_auth = self.auth_for(OPERATOR_EMAIL, self.operator_key)
        requests = [r for r in self.fake.requests if r.method == "POST" and r.path == "messages"]
        self.assertTrue(requests)
        for request in requests:
            self.assertEqual(request.headers.get("Authorization"), operator_auth)

    def test_two_spaces_between_the_sentences(self) -> None:
        out = self.send()
        body = self.topic_posts()[0]["content"]
        converted = textfmt.sentence_gap(body)
        self.assertNotIn(". ", converted.replace(".  ", ""))

    def test_the_seats_own_credentials_are_refused(self) -> None:
        out = self.send(
            fetch=lambda names: {
                "ZULIP_OPERATOR_EMAIL": SEAT_EMAIL,
                "ZULIP_OPERATOR_API_KEY": self.seat_key,
            }
        )
        self.assertEqual(out["status"], "refused")
        self.assertEqual(self.topic_posts(), [])

    def test_an_unpinned_owner_is_refused(self) -> None:
        out = self.send(owner_user_id=0)
        self.assertEqual(out["status"], "refused")
        self.assertEqual(self.topic_posts(), [])

    def test_a_missing_route_is_refused(self) -> None:
        for override in ({"channel": ""}, {"topic": ""}, {"channel": "", "topic": ""}):
            out = self.send(**override)
            self.assertEqual(out["status"], "refused", override)
        self.assertEqual(self.topic_posts(), [])

    def test_a_secret_shaped_question_is_withheld(self) -> None:
        out = self.send(question="api_key=sk-" + "abcdef0123456789" * 2 + " (ref %s)" % ENTRY["entry_id"])
        self.assertEqual(out["status"], "refused")
        self.assertEqual(self.topic_posts(), [])

    def test_a_secret_shaped_route_is_withheld(self) -> None:
        # The queue scans the route at persistence;  the send boundary scans it again, so
        # a hand-built caller gets the same refusal (never the value, only the field).
        for override in ({"channel": "sk-" + "placeholder" * 2}, {"topic": "sk-" + "placeholder" * 2}):
            out = self.send(**override)
            self.assertEqual(out["status"], "refused", override)
            self.assertEqual(out["code"], "secret")
        self.assertEqual(self.topic_posts(), [])

    def test_an_empty_question_is_refused(self) -> None:
        out = self.send(question="")
        self.assertEqual(out["status"], "refused")
        self.assertEqual(self.topic_posts(), [])


class _StubClient:
    """A scripted client, so the delivery ledger's state machine is testable without a socket."""

    def __init__(self, *, post_error=None, me_error=None, messages=(), messages_error=None):
        self.post_error = post_error
        self.me_error = me_error
        self.messages = list(messages)
        self.messages_error = messages_error
        self.posts = []

    def get(self, endpoint, params=None):
        if endpoint == "messages":
            if self.messages_error is not None:
                raise self.messages_error
            params = params or {}
            rows = sorted(self.messages, key=lambda m: m["id"])
            anchor = params.get("anchor", "newest")
            if anchor != "newest":
                rows = [m for m in rows if m["id"] < int(anchor)]
            before = int(params.get("num_before", 0))
            return {"result": "success", "messages": rows[-before:] if before else []}
        assert endpoint == "users/me", endpoint
        if self.me_error is not None:
            raise self.me_error
        return {
            "user_id": OPERATOR_ID,
            "email": OPERATOR_EMAIL,
            "is_bot": True,
            "bot_owner_id": OWNER_ID,
        }

    def post(self, endpoint, params):
        assert endpoint == "messages", endpoint
        if self.post_error is not None:
            raise self.post_error
        self.posts.append(params)
        return {"result": "success", "id": 4200 + len(self.posts)}


class LocationQuestionDeliveryTests(_LocationQuestionFixture, unittest.TestCase):
    """The delivery ledger (same machinery as the candidate list):  a landed question is
    never posted twice, an unknown outcome is reconciled by reading the topic back for
    the question's unique ref - never resent on a timer - and only a definite refusal,
    a provably-unsent failure, or a proven-absent read-back frees the send."""

    def test_a_landed_question_is_never_posted_twice(self) -> None:
        first = self.send()
        self.assertEqual(first["status"], "sent", first["reason"])
        second = self.send(now=1000.0 + OC.DEDUPE_WINDOW + 1)
        self.assertEqual(second["status"], "sent")
        self.assertEqual(second["message_id"], first["message_id"])
        self.assertIn("already went", second["reason"])
        self.assertEqual(len(self.topic_posts()), 1)

    def test_a_landed_but_unconfirmed_question_is_recovered_by_readback(self) -> None:
        # The post landed but its outcome was unknown:  the ledger says unknown, the
        # topic holds the question.  The retry must recover, not resend.
        landed = [{"id": 9876, "content": QUESTION, "sender_id": OPERATOR_ID}]
        failing = _StubClient(
            post_error=Z.NetworkError("timed out", timeout=True, maybe_sent=True)
        )
        out = self.send(client=failing)
        self.assertEqual(out["delivery_status"], OC.DELIVERY_UNKNOWN)
        working = _StubClient(messages=landed)
        again = self.send(client=working, now=1001.0)
        self.assertEqual(again["status"], "sent", again["reason"])
        self.assertEqual(again["message_id"], 9876)
        self.assertEqual(again["delivery_status"], OC.DELIVERY_OK)
        self.assertEqual(working.posts, [])

    def test_a_ref_quoted_by_another_sender_is_not_the_landed_question(self) -> None:
        failing = _StubClient(
            post_error=Z.NetworkError("timed out", timeout=True, maybe_sent=True)
        )
        self.assertEqual(self.send(client=failing)["delivery_status"], OC.DELIVERY_UNKNOWN)
        # An answer or a quote carrying the ref, posted by anyone but the Operator bot,
        # never counts as the landed question.
        quoted = [{"id": 51, "content": "mac %s" % ENTRY["entry_id"], "sender_id": OWNER_ID}]
        working = _StubClient(messages=quoted)
        again = self.send(client=working, now=1001.0)
        self.assertEqual(again["status"], "sent", again["reason"])
        self.assertEqual(len(working.posts), 1)

    def test_a_buried_question_is_found_on_a_later_page(self) -> None:
        failing = _StubClient(
            post_error=Z.NetworkError("timed out", timeout=True, maybe_sent=True)
        )
        self.assertEqual(self.send(client=failing)["delivery_status"], OC.DELIVERY_UNKNOWN)
        newer = [
            {"id": 100 + i, "content": "unrelated chatter %d" % i, "sender_id": OPERATOR_ID}
            for i in range(ORASK.READ_BACK_PAGE)
        ]
        buried = [{"id": 42, "content": QUESTION, "sender_id": OPERATOR_ID}]
        working = _StubClient(messages=buried + newer)
        again = self.send(client=working, now=1001.0)
        self.assertEqual(again["status"], "sent", again["reason"])
        self.assertEqual(again["message_id"], 42)
        self.assertEqual(working.posts, [])

    def test_an_unexhausted_history_is_not_absence(self) -> None:
        failing = _StubClient(
            post_error=Z.NetworkError("timed out", timeout=True, maybe_sent=True)
        )
        self.assertEqual(self.send(client=failing)["delivery_status"], OC.DELIVERY_UNKNOWN)
        chatter = [
            {"id": 1 + i, "content": "unrelated chatter %d" % i, "sender_id": OPERATOR_ID}
            for i in range(ORASK.READ_BACK_PAGE * ORASK.READ_BACK_MAX_PAGES)
        ]
        working = _StubClient(messages=chatter)
        again = self.send(client=working, now=1001.0)
        self.assertEqual(again["status"], "refused")
        self.assertEqual(again["delivery_status"], OC.DELIVERY_UNKNOWN)
        self.assertEqual(working.posts, [])

    def test_a_rate_limit_is_retryable_and_the_key_is_released(self) -> None:
        limited = _StubClient(
            post_error=Z.ApiError("rate limited", code="RATE_LIMIT_HIT", status=429)
        )
        first = self.send(client=limited)
        self.assertEqual(first["status"], "refused")
        self.assertEqual(first["delivery_status"], OC.DELIVERY_RETRYABLE)
        # The refusal was definite (no post happened), so the very next pass may post.
        working = _StubClient(messages=[])
        again = self.send(client=working, now=1001.0)
        self.assertEqual(again["status"], "sent", again["reason"])
        self.assertEqual(len(working.posts), 1)

    def test_a_server_error_leaves_delivery_unknown_and_keeps_the_key(self) -> None:
        failing = _StubClient(
            post_error=Z.ApiError("internal error", status=503)
        )
        first = self.send(client=failing)
        self.assertEqual(first["status"], "refused")
        self.assertEqual(first["delivery_status"], OC.DELIVERY_UNKNOWN)
        # A 5xx can follow a processed create, so no blind repost:  the read-back
        # reconciliation decides - proven absent here, so exactly one resend.
        working = _StubClient(messages=[])
        again = self.send(client=working, now=1001.0)
        self.assertEqual(again["status"], "sent", again["reason"])
        self.assertEqual(len(working.posts), 1)

    def test_an_unknown_delivery_is_resent_once_proven_absent(self) -> None:
        failing = _StubClient(
            post_error=Z.NetworkError("timed out", timeout=True, maybe_sent=True)
        )
        out = self.send(client=failing)
        self.assertEqual(out["delivery_status"], OC.DELIVERY_UNKNOWN)
        # The topic read-back finds no trace of the ref:  resending is safe, inside or
        # outside the window - the window's expiry is not what proves it.
        working = _StubClient(messages=[])
        again = self.send(client=working, now=1001.0)
        self.assertEqual(again["status"], "sent", again["reason"])
        self.assertEqual(len(working.posts), 1)

    def test_an_unknown_delivery_waits_when_the_topic_cannot_be_checked(self) -> None:
        failing = _StubClient(
            post_error=Z.NetworkError("timed out", timeout=True, maybe_sent=True)
        )
        self.assertEqual(self.send(client=failing)["delivery_status"], OC.DELIVERY_UNKNOWN)
        blind = _StubClient(messages_error=Z.NetworkError("connect refused", maybe_sent=False))
        again = self.send(client=blind, now=1001.0)
        self.assertEqual(again["status"], "refused")
        self.assertEqual(again["delivery_status"], OC.DELIVERY_UNKNOWN)
        self.assertEqual(blind.posts, [])

    def test_a_provably_unsent_failure_is_retryable(self) -> None:
        failing = _StubClient(post_error=Z.NetworkError("connect refused", maybe_sent=False))
        out = self.send(client=failing)
        self.assertEqual(out["status"], "refused")
        self.assertEqual(out["delivery_status"], OC.DELIVERY_RETRYABLE)
        working = _StubClient(messages=[])
        again = self.send(client=working, now=1001.0)
        self.assertEqual(again["status"], "sent", again["reason"])
        self.assertEqual(len(working.posts), 1)

    def test_an_indeterminate_answer_leaves_delivery_unknown(self) -> None:
        # Zulip answered, but with no HTTP status the outcome is indeterminate:  the post
        # may have been processed, so this is unknown - never a definite refusal.
        failing = _StubClient(post_error=Z.AgentSyncError("odd reply"))
        first = self.send(client=failing)
        self.assertEqual(first["status"], "refused")
        self.assertEqual(first["delivery_status"], OC.DELIVERY_UNKNOWN)
        working = _StubClient(messages=[])
        again = self.send(client=working, now=1001.0)
        self.assertEqual(again["status"], "sent", again["reason"])
        self.assertEqual(len(working.posts), 1)

    def test_a_landed_ref_with_a_malformed_id_fails_the_readback(self) -> None:
        failing = _StubClient(
            post_error=Z.NetworkError("timed out", timeout=True, maybe_sent=True)
        )
        self.assertEqual(self.send(client=failing)["delivery_status"], OC.DELIVERY_UNKNOWN)
        # The ref is in the topic but its id is unusable:  not absence, not a recovery -
        # the read-back failed and the delivery stays unknown.
        broken = [{"id": True, "content": QUESTION, "sender_id": OPERATOR_ID}]
        working = _StubClient(messages=broken)
        again = self.send(client=working, now=1001.0)
        self.assertEqual(again["status"], "refused")
        self.assertEqual(again["delivery_status"], OC.DELIVERY_UNKNOWN)
        self.assertEqual(working.posts, [])

    def test_a_malformed_cursor_id_fails_the_readback(self) -> None:
        failing = _StubClient(
            post_error=Z.NetworkError("timed out", timeout=True, maybe_sent=True)
        )
        self.assertEqual(self.send(client=failing)["delivery_status"], OC.DELIVERY_UNKNOWN)
        chatter = [
            {"id": 100 + i, "content": "unrelated chatter %d" % i, "sender_id": OPERATOR_ID}
            for i in range(ORASK.READ_BACK_PAGE)
        ]
        chatter[0]["id"] = -7
        working = _StubClient(messages=chatter)
        again = self.send(client=working, now=1001.0)
        self.assertEqual(again["status"], "refused")
        self.assertEqual(again["delivery_status"], OC.DELIVERY_UNKNOWN)
        self.assertEqual(working.posts, [])

    def test_the_readback_pages_share_one_deadline(self) -> None:
        failing = _StubClient(
            post_error=Z.NetworkError("timed out", timeout=True, maybe_sent=True)
        )
        self.assertEqual(self.send(client=failing)["delivery_status"], OC.DELIVERY_UNKNOWN)
        with mock.patch.object(ORASK, "READ_BACK_DEADLINE_SECONDS", -1):
            working = _StubClient(messages=[])
            again = self.send(client=working, now=1001.0)
        self.assertEqual(again["status"], "refused")
        self.assertEqual(again["delivery_status"], OC.DELIVERY_UNKNOWN)
        self.assertIn("ran out of time", again["reason"])
        self.assertEqual(working.posts, [])

    def test_a_rejected_credential_is_named_without_any_secret(self) -> None:
        error = Z.AgentSyncError("unauthorized")
        error.status = 401
        out = self.send(client=_StubClient(me_error=error))
        self.assertEqual(out["status"], "refused")
        self.assertEqual(out["delivery_status"], OC.DELIVERY_REFUSED)
        self.assertEqual(out["code"], "credential_rejected")
        # The credential is named by its identity and the server's code - no part of
        # the key, not even a masked prefix or suffix.
        self.assertIn(OPERATOR_EMAIL, out["reason"])
        self.assertIn("401", out["reason"])
        self.assertNotIn(self.operator_key, out["reason"])
        self.assertNotIn(self.operator_key[:8], out["reason"])
        self.assertNotIn(self.operator_key[-4:], out["reason"])


if __name__ == "__main__":
    unittest.main()
