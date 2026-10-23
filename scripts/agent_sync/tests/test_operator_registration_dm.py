"""The Operator's registration-question DM:  same sending rules as the candidate list.

The properties pinned here are the owner rulings the candidate list established (Oct 21-22
2026):  the question goes out as the pinned Operator bot and never as a seat's own bot, a
missing credential or an unresolvable recipient is a clean refusal rather than an exception,
and the body carries the question and its ref - never a secret.  The objects are the real
ones:  a real ZulipClient against the fake realm and the real service_identity checks.
"""

from __future__ import annotations

import base64
import secrets
import shutil
import tempfile
import unittest
from pathlib import Path

from agent_sync import operator_registration as OREG
from agent_sync import operator_registration_dm as ORDM
from agent_sync import textfmt
from agent_sync.tests.fake_zulip import FakeZulip

OPERATOR_ID = 9101
OPERATOR_EMAIL = "operator-bot@zulip.test"
SEAT_EMAIL = "claude-bot@zulip.test"
OWNER_ID = 12
AFC_PROJECT = "fixture-afc-project"

ENTRY = {
    "entry_id": "reg-abc123def456",
    "platform": "minimax",
    "profile_scope": "default",
    "session_id": "sess-aaaaaaaa",
    "seat": "MM",
    "requester": "catalog",
    "evidence_location": "",
    "state": OREG.STATE_ASKED,
    "asked_at": 1000.0,
    "deadline": 1000.0 + OREG.DEFAULT_TIMEOUT_SECONDS,
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
        self.tmp = Path(tempfile.mkdtemp(prefix="operator-registration-dm-test-"))
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
            recipient=OWNER_ID,
            env=self.env(),
            owner_user_id=OWNER_ID,
            fetch=self.fetch,
            now=1000.0,
        )
        kwargs.update(overrides)
        return ORDM.send_location_question(**kwargs)

    def auth_for(self, email: str, key: str) -> str:
        return "Basic " + base64.b64encode(("%s:%s" % (email, key)).encode()).decode()

    def direct_messages(self) -> list[dict]:
        return [m for m in self.fake.messages if m["type"] == "private"]

    def test_the_question_goes_out_as_the_operator_bot_to_the_owner(self) -> None:
        out = self.send()
        self.assertEqual(out["status"], "sent", out["reason"])
        messages = self.direct_messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["sender_email"], OPERATOR_EMAIL)
        self.assertEqual(messages[0]["display_recipient"][0]["id"], OWNER_ID)
        self.assertEqual(messages[0]["content"], textfmt.sentence_gap(QUESTION))
        self.assertIn(ENTRY["entry_id"], messages[0]["content"])
        # Every request went out with the Operator's own credential.
        operator_auth = self.auth_for(OPERATOR_EMAIL, self.operator_key)
        posts = [r for r in self.fake.requests if r.method == "POST" and r.path == "messages"]
        self.assertTrue(posts)
        for request in posts:
            self.assertEqual(request.headers.get("Authorization"), operator_auth)

    def test_two_spaces_between_the_sentences(self) -> None:
        out = self.send()
        body = self.direct_messages()[0]["content"]
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
        self.assertEqual(self.direct_messages(), [])

    def test_an_unpinned_owner_is_refused(self) -> None:
        out = self.send(owner_user_id=0)
        self.assertEqual(out["status"], "refused")
        self.assertEqual(self.direct_messages(), [])

    def test_an_unresolvable_recipient_is_refused(self) -> None:
        out = self.send(recipient=999999)
        self.assertEqual(out["status"], "refused")
        self.assertEqual(self.direct_messages(), [])

    def test_a_secret_shaped_question_is_withheld(self) -> None:
        out = self.send(question="api_key=sk-abcdef0123456789abcdef0123456789 (ref %s)" % ENTRY["entry_id"])
        self.assertEqual(out["status"], "refused")
        self.assertEqual(self.direct_messages(), [])

    def test_an_empty_question_is_refused(self) -> None:
        out = self.send(question="")
        self.assertEqual(out["status"], "refused")
        self.assertEqual(self.direct_messages(), [])


if __name__ == "__main__":
    unittest.main()
