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

from agent_sync import operator_registration as OREG
from agent_sync import operator_registration_ask as ORASK
from agent_sync import textfmt
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


class LocationQuestionTests(unittest.TestCase):
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
        out = self.send(question="api_key=sk-abcdef0123456789abcdef0123456789 (ref %s)" % ENTRY["entry_id"])
        self.assertEqual(out["status"], "refused")
        self.assertEqual(self.topic_posts(), [])

    def test_an_empty_question_is_refused(self) -> None:
        out = self.send(question="")
        self.assertEqual(out["status"], "refused")
        self.assertEqual(self.topic_posts(), [])


if __name__ == "__main__":
    unittest.main()
