"""The Operator's candidate list:  the one direct message that tells the sender which sessions a
tagged address could mean.

The properties this file pins are the ones the owner ruled on (Oct 21 2026):

  - the list goes out as the Operator bot, never as the asking seat's own bot, so the seat's
    thread stays clean and the seat's credential never leaves its lane;
  - a missing Operator credential or an unresolvable recipient is a clean refusal, never an
    exception into the wake path;
  - an identical list to the same recipient inside the window is not sent twice;
  - the wake prompt tells the seat that the ask never blocks and that it proceeds on its default
    letter.

The objects are the real ones:  a real `ZulipClient` against the fake realm, the real
`service_identity` helpers and the real registry rows, so nothing here proves a stub.
"""

from __future__ import annotations

import base64
import os
import secrets
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agent_sync import operator_candidates as OC
from agent_sync import service_identity as SI
from agent_sync import textfmt
from agent_sync import wakes as W
from agent_sync import zulip as Z
from agent_sync.tests.fake_zulip import FakeZulip

# Synthetic fixture ids only:  no committed test carries a real realm user id.  The emails stay
# on the shared fake server's reserved .test domain (fake_zulip.BOT_EMAIL), never a real address.
OPERATOR_ID = 9101
OPERATOR_EMAIL = "operator-bot@zulip.test"
SEAT_EMAIL = "claude-bot@zulip.test"
OWNER_ID = 12
AFC_PROJECT = "fixture-afc-project"

CANDIDATES = [
    {"label": "A", "seat": "MM", "native_session_id": "mvs_aaa", "title": "AFC-412 candidates"},
    {"label": "B", "seat": "MM", "native_session_id": "mvs_bbb", "title": "AFC-412 closeout"},
]


class CandidateListTests(unittest.TestCase):
    """The real Operator bot, the real credential path, the real realm."""

    def setUp(self) -> None:
        self.seat_key = secrets.token_hex(16)
        self.operator_key = secrets.token_hex(16)
        self.fake = FakeZulip(self.seat_key, heartbeat=0.1)
        self.fake.start()
        self.addCleanup(self.fake.stop)
        # The Operator bot:  a service identity of its own, with its own key.
        self.fake.add_bot(OPERATOR_EMAIL, "Operator", self.operator_key, user_id=OPERATOR_ID)
        self.tmp = Path(tempfile.mkdtemp(prefix="operator-candidates-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.state = self.tmp / "state"

    # ---- helpers -----------------------------------------------------------------------

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
        """The approved named-secret reader, faked:  it hands back the Operator's own two names."""
        self.assertEqual(
            tuple(names), ("ZULIP_OPERATOR_EMAIL", "ZULIP_OPERATOR_API_KEY")
        )
        return {
            "ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL,
            "ZULIP_OPERATOR_API_KEY": self.operator_key,
        }

    def send(self, **overrides):
        kwargs = dict(
            seat="MM",
            platform="minimax",
            letters=["Z"],
            candidates=CANDIDATES,
            recipient=OWNER_ID,
            default_letter="A",
            env=self.env(),
            owner_user_id=OWNER_ID,
            fetch=self.fetch,
            now=1000.0,
        )
        kwargs.update(overrides)
        return OC.send_candidate_list(**kwargs)

    def auth_for(self, email: str, key: str) -> str:
        return "Basic " + base64.b64encode(("%s:%s" % (email, key)).encode()).decode()

    def direct_messages(self) -> list[dict]:
        return [m for m in self.fake.messages if m["type"] == "private"]

    # ---- the list -----------------------------------------------------------------------

    def test_the_list_names_every_candidate_letter_seat_and_title(self) -> None:
        out = self.send()
        self.assertEqual(out["status"], "sent", out["reason"])
        body = out["body"]
        for candidate in CANDIDATES:
            self.assertIn(candidate["label"], body)
            self.assertIn(candidate["seat"], body)
            self.assertIn(candidate["title"], body)
        # One sentence per candidate, and the seat's default letter is named.
        self.assertIn("A is the MM session", body)
        self.assertIn("B is the MM session", body)
        self.assertIn("MM is using letter A by default.", body)
        # The ask never blocks, and the seat says which letter it used.
        self.assertIn("The ask never blocks", body)
        self.assertIn("names the letter it used", body)

    def test_two_spaces_between_the_sentences_of_the_body(self) -> None:
        out = self.send()
        body = out["body"]
        self.assertNotIn(". ", body.replace(".  ", ""))
        self.assertIn(".  A is the MM session", body)
        self.assertIn(".  MM is using letter A by default.", body)

    def test_the_list_goes_out_as_the_operator_bot_and_not_the_seats_own_bot(self) -> None:
        out = self.send()
        self.assertEqual(out["status"], "sent", out["reason"])
        messages = self.direct_messages()
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["sender_email"], OPERATOR_EMAIL)
        self.assertEqual(messages[0]["sender_id"], OPERATOR_ID)
        self.assertEqual(messages[0]["display_recipient"][0]["id"], OWNER_ID)
        # The client turns the source body's ASCII gap into the Zulip gap on the way out, so the
        # message that lands is the body, converted and nothing else.
        self.assertEqual(messages[0]["content"], textfmt.sentence_gap(out["body"]))
        # Every request went out with the Operator's own credential:  the seat's key was never used.
        seat_auth = self.auth_for(SEAT_EMAIL, self.seat_key)
        operator_auth = self.auth_for(OPERATOR_EMAIL, self.operator_key)
        posts = [r for r in self.fake.requests if r.method == "POST" and r.path == "messages"]
        self.assertTrue(posts)
        for request in posts:
            self.assertEqual(request.headers.get("Authorization"), operator_auth)
            self.assertNotEqual(request.headers.get("Authorization"), seat_auth)

    # ---- refusals -----------------------------------------------------------------------

    def test_a_missing_operator_credential_is_a_clean_refusal(self) -> None:
        out = self.send(env=self.env(OPERATOR_ZULIP_USER_ID="", OPERATOR_ZULIP_EMAIL=""))
        self.assertEqual(out["status"], "refused")
        self.assertIn("Operator", out["reason"])
        self.assertEqual(self.direct_messages(), [], "nothing was sent")

    def test_an_unresolvable_recipient_is_a_clean_refusal(self) -> None:
        out = self.send(recipient=987654)
        self.assertEqual(out["status"], "refused")
        self.assertIn("not resolvable", out["reason"])
        self.assertEqual(self.direct_messages(), [], "nothing was sent")

    def test_the_seats_own_credentials_are_refused(self) -> None:
        # The asking seat holds a key of its own:  it may never send the list, so the identity
        # check refuses it even though the credential works.
        out = self.send(credentials=Z.Credentials(
            email=SEAT_EMAIL, key=self.seat_key, site=self.fake.url, source="test"
        ))
        self.assertEqual(out["status"], "refused")
        self.assertIn("not the pinned Operator", out["reason"])
        self.assertEqual(self.direct_messages(), [], "nothing was sent")

    def test_a_credential_that_is_not_the_pinned_bot_is_refused(self) -> None:
        out = self.send(fetch=lambda names: {
            "ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL,
            "ZULIP_OPERATOR_API_KEY": self.seat_key,
        })
        self.assertEqual(out["status"], "refused")
        self.assertEqual(self.direct_messages(), [], "nothing was sent")

    def test_a_missing_candidate_is_refused_rather_than_guessed(self) -> None:
        out = self.send(candidates=[])
        self.assertEqual(out["status"], "refused")
        self.assertEqual(self.direct_messages(), [], "nothing was sent")

    def test_an_unpinned_owner_is_refused(self) -> None:
        out = self.send(owner_user_id=0)
        self.assertEqual(out["status"], "refused")
        self.assertIn("owner is not pinned", out["reason"])
        self.assertEqual(self.direct_messages(), [], "nothing was sent")

    # ---- the dedupe window --------------------------------------------------------------

    def test_the_same_ask_inside_the_window_does_not_send_twice(self) -> None:
        first = self.send()
        self.assertEqual(first["status"], "sent", first["reason"])
        again = self.send(now=1000.0 + 60.0)
        self.assertIn("already went", again["reason"])
        self.assertEqual(len(self.direct_messages()), 1, "the sender was not told twice")

    def test_a_retry_of_an_accepted_send_returns_the_cached_result(self) -> None:
        """An `ok` row keeps its message id, so a retry is answerable without sending again."""
        first = self.send()
        self.assertEqual(first["delivery_status"], OC.DELIVERY_OK)
        again = self.send(now=1000.0 + 60.0)
        self.assertEqual(again["status"], "sent")
        self.assertEqual(again["delivery_status"], OC.DELIVERY_OK)
        self.assertEqual(again["message_id"], first["message_id"])
        self.assertEqual(len(self.direct_messages()), 1)

    def test_the_same_ask_after_the_window_sends_again(self) -> None:
        self.send()
        later = self.send(now=1000.0 + OC.DEDUPE_WINDOW + 1.0)
        self.assertEqual(later["status"], "sent", later["reason"])
        self.assertEqual(len(self.direct_messages()), 2)

    def test_the_state_lives_in_the_seats_own_private_directory(self) -> None:
        self.send()
        seat_dir = self.state / "MM"
        self.assertTrue((seat_dir / OC.STATE_FILE).exists())
        self.assertEqual(stat.S_IMODE(os.stat(seat_dir).st_mode), 0o700)
        self.assertEqual(
            stat.S_IMODE(os.stat(seat_dir / OC.STATE_FILE).st_mode), 0o600
        )


class RecipientTests(unittest.TestCase):
    """A recipient that cannot be resolved is refused, never guessed."""

    MEMBERS = [
        {"user_id": 12, "email": "jay@zulip.test", "full_name": "Jay Wedgeworth", "is_bot": False},
        {"user_id": 13, "email": "mm-bot@zulip.test", "full_name": "MiniMax", "is_bot": True,
         "bot_type": 1},
        {"user_id": 14, "email": "mm2-bot@zulip.test", "full_name": "MiniMax Two", "is_bot": True,
         "bot_type": 1},
        {"user_id": 15, "email": "old@zulip.test", "full_name": "Gone", "is_bot": False,
         "is_active": False},
        {"user_id": 16, "email": "twin-a@zulip.test", "full_name": "Twin", "is_bot": False},
        {"user_id": 17, "email": "twin-b@zulip.test", "full_name": "Twin", "is_bot": False},
    ]

    def test_a_user_id_resolves(self) -> None:
        self.assertEqual(OC.resolve_recipient(self.MEMBERS, 12), (12, ""))

    def test_a_full_name_resolves(self) -> None:
        self.assertEqual(OC.resolve_recipient(self.MEMBERS, "Jay Wedgeworth"), (12, ""))

    def test_an_email_resolves(self) -> None:
        self.assertEqual(OC.resolve_recipient(self.MEMBERS, "jay@zulip.test"), (12, ""))

    def test_a_seat_tag_resolves(self) -> None:
        self.assertEqual(OC.resolve_recipient(self.MEMBERS, "MM"), (13, ""))

    def test_an_unknown_name_is_refused(self) -> None:
        user_id, why = OC.resolve_recipient(self.MEMBERS, "Nobody")
        self.assertIsNone(user_id)
        self.assertIn("no user", why)

    def test_two_names_that_match_are_refused(self) -> None:
        user_id, why = OC.resolve_recipient(self.MEMBERS, "Twin")
        self.assertIsNone(user_id)
        self.assertIn("matches 2 users", why)

    def test_a_deactivated_user_is_refused(self) -> None:
        user_id, why = OC.resolve_recipient(self.MEMBERS, "old@zulip.test")
        self.assertIsNone(user_id)
        self.assertIn("deactivated", why)

    def test_no_recipient_is_refused(self) -> None:
        user_id, why = OC.resolve_recipient(self.MEMBERS, None)
        self.assertIsNone(user_id)
        self.assertIn("no recipient", why)


class WakePromptTests(unittest.TestCase):
    """The wake prompt tells the seat the rule the owner ruled."""

    def prompt(self, **overrides) -> str:
        pending = W.Pending(
            "w1", "MM", {"channel": "agent-sync", "topic": "AFC-412"}, owner=False, now=1.0, due=2.0
        )
        pending.add({"id": 5}, False)
        kwargs = dict(
            seat="MM",
            pending=pending,
            history=[],
            owner_user_id=OWNER_ID,
            is_bot=lambda uid: True,
            owner_of=lambda message: False,
            format_time=str,
            board_enabled=False,
            nonce="n",
        )
        kwargs.update(overrides)
        return W.build_prompt(**kwargs)

    def test_the_prompt_says_the_ask_never_blocks_and_names_the_default(self) -> None:
        prompt = self.prompt()
        self.assertIn("candidate list", prompt)
        self.assertIn("Nothing here blocks", prompt)
        self.assertIn("your default letter", prompt)
        self.assertIn("name the letter you used", prompt)

    def test_the_prompt_names_the_letter_of_the_tagged_address(self) -> None:
        prompt = self.prompt(tagged_address="Z")
        self.assertIn('the session letter "Z"', prompt)
        self.assertIn("Nothing here blocks", prompt)

    def test_the_rule_sits_outside_the_untrusted_block(self) -> None:
        prompt = self.prompt()
        lines = prompt.splitlines()
        begin = lines.index("BEGIN_UNTRUSTED_ZULIP nonce=n")
        header = "\n".join(lines[:begin])
        self.assertIn("Nothing here blocks", header)


class ServiceIdentityTests(unittest.TestCase):
    """The module uses the Operator helpers, not a home-grown identity."""

    def test_the_operator_identity_is_the_pinned_service_bot(self) -> None:
        env = {
            "OPERATOR_ZULIP_USER_ID": str(OPERATOR_ID),
            "OPERATOR_ZULIP_EMAIL": OPERATOR_EMAIL,
        }
        self.assertEqual(SI.operator_identity(env), (OPERATOR_ID, OPERATOR_EMAIL))
        bot = {"user_id": OPERATOR_ID, "email": OPERATOR_EMAIL, "is_bot": True,
               "bot_owner_id": OWNER_ID}
        self.assertTrue(SI.operator_bot(bot, env, OWNER_ID))
        # Another bot with the same id but a different owner is not the Operator.
        self.assertFalse(SI.operator_bot(dict(bot, bot_owner_id=99), env, OWNER_ID))


class StubClient:
    """A Zulip client that answers from a fixed script, so the delivery state machine is testable
    without a socket."""

    def __init__(self, *, me=None, users=None, post_error=None, me_error=None):
        self.me = me or {
            "user_id": OPERATOR_ID, "email": OPERATOR_EMAIL, "is_bot": True,
            "bot_owner_id": OWNER_ID,
        }
        self.users = {"members": users} if users is not None else {
            "members": [{"user_id": OWNER_ID, "email": "jay@example.test", "is_bot": False}]
        }
        self.post_error = post_error
        self.me_error = me_error
        self.posts = []

    def get(self, endpoint):
        if endpoint == "users/me":
            if self.me_error is not None:
                raise self.me_error
            return self.me
        if endpoint == "users":
            return self.users
        raise AssertionError("unexpected GET %s" % endpoint)

    def post(self, endpoint, payload):
        self.posts.append(payload)
        if self.post_error is not None:
            raise self.post_error
        return {"id": 4242}


class DeliveryStateTests(unittest.TestCase):
    """`ok` / `unknown` / `refused`, so a retry never resends and never loses a retryable send."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="operator-delivery-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.key = secrets.token_hex(16)
        self.env = {
            "HOME": str(self.tmp),
            "AGENT_SYNC_STATE_DIR": str(self.tmp / "state"),
            "OPERATOR_ZULIP_USER_ID": str(OPERATOR_ID),
            "OPERATOR_ZULIP_EMAIL": OPERATOR_EMAIL,
        }
        self.creds = SimpleNamespace(
            site="https://example.test", email=OPERATOR_EMAIL, key=self.key
        )

    def send(self, client, now=1000.0, **overrides):
        kwargs = dict(
            seat="MM", platform="minimax", letters=["Z"], candidates=CANDIDATES,
            recipient=OWNER_ID, default_letter="A", env=self.env,
            owner_user_id=OWNER_ID, credentials=self.creds, client=client, now=now,
        )
        kwargs.update(overrides)
        return OC.send_candidate_list(**kwargs)

    def test_an_accepted_send_is_ok_and_caches_the_message_id(self) -> None:
        client = StubClient()
        first = self.send(client)
        self.assertEqual(first["status"], "sent")
        self.assertEqual(first["delivery_status"], OC.DELIVERY_OK)
        again = self.send(client, now=1060.0)
        self.assertEqual(again["message_id"], first["message_id"])
        self.assertEqual(len(client.posts), 1, "an acknowledged send is never repeated")

    def test_a_provably_unsent_failure_is_retryable(self) -> None:
        client = StubClient(post_error=Z.NetworkError("connect refused", maybe_sent=False))
        out = self.send(client)
        # A retryable delivery is its own state, not a sibling flag on a
        # terminal refusal:  anything that reads delivery_status alone can tell
        # them apart.
        self.assertEqual(out["delivery_status"], OC.DELIVERY_RETRYABLE)
        # The key was released, so a clean retry inside the window is not blocked as a duplicate.
        again = self.send(StubClient(), now=1060.0)
        self.assertEqual(again["status"], "sent")

    def test_an_unknown_outcome_is_kept_and_not_replayed(self) -> None:
        client = StubClient(post_error=Z.NetworkError("timed out", maybe_sent=True))
        out = self.send(client)
        self.assertEqual(out["delivery_status"], OC.DELIVERY_UNKNOWN)
        again = self.send(client, now=1060.0)
        self.assertEqual(again["delivery_status"], OC.DELIVERY_UNKNOWN)
        self.assertEqual(len(client.posts), 1, "a message that may exist is never sent twice")

    def test_a_rejected_credential_is_named_by_a_masked_preview_only(self) -> None:
        error = Z.AgentSyncError("unauthorized")
        error.status = 401
        out = self.send(StubClient(post_error=error))
        self.assertEqual(out["delivery_status"], OC.DELIVERY_REFUSED)
        self.assertEqual(out["code"], "credential_rejected")
        self.assertNotIn(self.key, out["reason"])
        preview = OC._masked(self.key)
        self.assertIn(preview[:8], out["reason"])
        self.assertIn(preview[-4:], out["reason"])

    def test_a_secret_scan_finding_is_never_echoed(self) -> None:
        secret = "sk-" + secrets.token_hex(12)
        candidates = [
            {"label": "A", "seat": "MM", "native_session_id": "mvs_x", "title": secret}
        ]
        out = self.send(StubClient(), candidates=candidates)
        self.assertEqual(out["code"], "secret")
        self.assertEqual(out["field"], "candidate_list")
        self.assertNotIn(secret, out["reason"])

    def test_a_malformed_users_response_is_refused(self) -> None:
        for payload in ({"members": "nope"}, {"users": []}, {"members": {"a": 1}}):
            with self.subTest(payload=payload):
                out = self.send(StubClient(users=payload))
                self.assertEqual(out["status"], "refused")
                self.assertIn("malformed", out["reason"])

    def test_the_count_is_not_cut_to_the_shortlist(self) -> None:
        many = [
            {"label": chr(65 + i % 26), "seat": "MM%d" % i,
             "native_session_id": "mvs_%d" % i, "title": "T%d" % i}
            for i in range(OC.MAX_CANDIDATES + 6)
        ]
        out = self.send(StubClient(), candidates=many)
        self.assertEqual(out["status"], "sent")
        self.assertIn(
            "%d sessions could hold it" % (OC.MAX_CANDIDATES + 6), out["body"]
        )

    def test_a_rejected_credential_on_the_identity_call_is_masked_too(self) -> None:
        # The identity check is the FIRST call the credential makes, so a bad key
        # is refused there and never reaches the POST path.  The preview has to
        # be on both paths or the key is named in full on the first one.
        error = Z.AgentSyncError("unauthorized")
        error.status = 401
        out = self.send(StubClient(me_error=error))
        self.assertEqual(out["delivery_status"], OC.DELIVERY_REFUSED)
        self.assertEqual(out["code"], "credential_rejected")
        self.assertNotIn(self.key, out["reason"])
        preview = OC._masked(self.key)
        self.assertIn(preview[:8], out["reason"])
        self.assertIn(preview[-4:], out["reason"])

    def test_a_failure_after_the_post_keeps_the_key(self) -> None:
        # The POST returned and the state write failed.  The message may be in
        # the recipient's inbox, so the key is kept and the next ask is refused
        # rather than sending a second copy of a list that did land.
        real_record = OC._record

        def fail_on_ok(path, key, at, window, recipient_id, message_id, state):
            if state == OC.DELIVERY_OK:
                raise OSError("state write failed")
            return real_record(path, key, at, window, recipient_id, message_id, state)

        with mock.patch.object(OC, "_record", side_effect=fail_on_ok):
            out = self.send(StubClient())
        # The send itself succeeded, and the id is what proves it.
        self.assertEqual(out["status"], "sent")
        self.assertEqual(out["message_id"], 4242)
        # The key was kept, so the next ask is refused rather than resending.
        again = self.send(StubClient(), now=1060.0)
        self.assertEqual(again["delivery_status"], OC.DELIVERY_UNKNOWN)
        self.assertEqual(len(StubClient().posts), 0)

    def test_a_failure_after_the_post_does_not_send_twice(self) -> None:
        real_record = OC._record

        def fail_on_ok(path, key, at, window, recipient_id, message_id, state):
            if state == OC.DELIVERY_OK:
                raise OSError("state write failed")
            return real_record(path, key, at, window, recipient_id, message_id, state)

        client = StubClient()
        with mock.patch.object(OC, "_record", side_effect=fail_on_ok):
            self.send(client)
            again = self.send(client, now=1060.0)
        self.assertEqual(len(client.posts), 1, "a list that may have landed is never sent twice")
        self.assertEqual(again["delivery_status"], OC.DELIVERY_UNKNOWN)
