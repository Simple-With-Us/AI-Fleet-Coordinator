"""The Operator's one-to-one status message (owner ruling, Thu, Oct 29 2026, about 3:35 AM).

The owner's words:  "DM one to one since from bot to another bot but since deterministic bot not
something about like the project or details I want to be able to see".  What this file pins:

  - the text comes from a fixed vocabulary, so nothing about a project, a topic or a quoted message
    can reach the sender;
  - the message goes out one to one from the Operator bot, with the owner NOT in the conversation,
    and it is the only direct message that skips the owner-in-the-conversation guard (AFC-469);
  - the skip is narrow and is enforced where the request is made:  a client that was not verified
    as the pinned Operator, a client whose credential changed or whose owner pin was removed or
    rotated after it was verified, a seat's own bot, a second recipient, a draft, a scheduled
    message, a PATCH, and any content that is not exactly a text the vocabulary builds (a tag with
    free text under it, a mention) are all refused, even when a caller passes the exemption flag
    on some other call;
  - the router never wakes the recipient for it and never reads it as a wake reply, a route or a
    note, so it cannot start a loop;
  - a repeat costs no request, a delivered message is reported once, and a write whose outcome is
    unknown is not replayed.

The objects are the real ones:  a real `ZulipClient` against the fake realm and the real
`service_identity` helpers.
"""

from __future__ import annotations

import os
import secrets
import shutil
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from agent_sync import config as CFG
from agent_sync import dm_policy as DMP
from agent_sync import operator_candidates as OC
from agent_sync import operator_send as OS
from agent_sync import operator_status_text as OST
from agent_sync import router as R
from agent_sync import zulip as Z
from agent_sync.tests.fake_zulip import FakeZulip

# Synthetic fixture ids only.  The fake realm: 10 is the seat bot (Claude), 11 is Codex, 12 is the
# owner, 13 and 14 are other realm users.  The Operator is added with an id of its own.
OPERATOR_ID = 9101
OPERATOR_EMAIL = "operator-bot@zulip.test"
OWNER_ID = 12
CODEX_ID = 11
AFC_PROJECT = "fixture-afc-project"
TAGGED = "[OPERATOR·status] re=1\nYour message tagged MM.  MM was woken."


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.seat_key = secrets.token_hex(16)
        self.operator_key = secrets.token_hex(16)
        self.fake = FakeZulip(self.seat_key, heartbeat=0.1)
        self.fake.start()
        self.addCleanup(self.fake.stop)
        self.fake.add_bot(OPERATOR_EMAIL, "Operator", self.operator_key, user_id=OPERATOR_ID)
        self.tmp = Path(tempfile.mkdtemp(prefix="operator-send-test-"))
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
        return {
            "ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL,
            "ZULIP_OPERATOR_API_KEY": self.operator_key,
        }

    def send(self, **overrides):
        kwargs = dict(
            recipient_user_id=CODEX_ID,
            reply_to=1234,
            seat="MM",
            outcome="woken",
            env=self.env(),
            owner_user_id=OWNER_ID,
            fetch=self.fetch,
            now=1000.0,
        )
        kwargs.update(overrides)
        return OS.send_status_dm(**kwargs)

    def direct_messages(self) -> list[dict]:
        return [m for m in self.fake.messages if m["type"] == "private"]

    def operator_client(self):
        link = OS.connect_operator(env=self.env(), owner_user_id=OWNER_ID, fetch=self.fetch)
        self.assertEqual(link["status"], "ok", link.get("reason"))
        return link["client"]

    def recipients_of(self, message: dict) -> set[int]:
        return {r["id"] if isinstance(r, dict) else r for r in message["display_recipient"]}


class TextTests(unittest.TestCase):
    def test_a_woken_status_is_two_sentences_under_the_status_tag(self) -> None:
        text = OS.build_text("status", reply_to=1234, seat="MM", outcome="woken")
        self.assertEqual(
            text, "[OPERATOR·status] re=1234\nYour message tagged MM.  MM was woken."
        )

    def test_a_dropped_status_carries_one_fixed_reason(self) -> None:
        text = OS.build_text(
            "status", reply_to=7, seat="GB-ORACLE", outcome="dropped", reason="budget"
        )
        self.assertIn("It was not delivered to GB-ORACLE.", text)
        self.assertTrue(text.endswith("  The seat is over its wake budget."))

    def test_a_seat_tag_is_upper_cased_and_nothing_else_is_changed(self) -> None:
        text = OS.build_text("status", reply_to=1, seat=" mm ", outcome="woken")
        self.assertIn("Your message tagged MM.", text)

    def test_every_outcome_and_reason_builds(self) -> None:
        for outcome in OS.OUTCOMES:
            OS.build_text("status", reply_to=1, seat="MM", outcome=outcome)
        for outcome in OS.REASON_OUTCOMES:
            for reason in OS.REASONS:
                OS.build_text("status", reply_to=1, seat="MM", outcome=outcome, reason=reason)

    def test_a_reason_goes_only_with_a_held_or_dropped_wake(self) -> None:
        for outcome in ("woken", "alerted", "queued", "paused"):
            with self.assertRaises(ValueError, msg=outcome):
                OS.build_text("status", reply_to=1, seat="MM", outcome=outcome, reason="budget")

    def test_nothing_outside_the_vocabulary_is_accepted(self) -> None:
        bad = [
            dict(kind="wake", reply_to=1, seat="MM", outcome="woken"),
            dict(kind="clarify", reply_to=1, seat="MM", outcome="woken"),
            dict(kind="status", reply_to=1, seat="MM", outcome="woken, see the budget thread"),
            dict(kind="status", reply_to=1, seat="MM", outcome="dropped", reason="the topic was X"),
            dict(kind="status", reply_to=1, seat="MM\nsecond line", outcome="woken"),
            dict(kind="status", reply_to=1, seat="M M", outcome="woken"),
            dict(kind="status", reply_to=1, seat="@**Jay**", outcome="woken"),
            dict(kind="status", reply_to=1, seat="A" * 40, outcome="woken"),
            dict(kind="status", reply_to=0, seat="MM", outcome="woken"),
            dict(kind="status", reply_to=OST.MAX_MESSAGE_ID + 1, seat="MM", outcome="woken"),
            dict(kind="status", reply_to=True, seat="MM", outcome="woken"),
            dict(kind="status", reply_to="12", seat="MM", outcome="woken"),
            dict(kind="status", reply_to=1, seat=None, outcome="woken"),
            dict(kind="status", reply_to=1, seat="MM", outcome=None),
        ]
        for kwargs in bad:
            with self.assertRaises(ValueError, msg=repr(kwargs)):
                OS.build_text(**kwargs)

    def test_the_router_does_not_read_a_status_as_a_wake_reply_a_route_or_a_note(self) -> None:
        text = OS.build_text("status", reply_to=1, seat="MM", outcome="woken")
        self.assertIsNone(R._WAKE_TAG_RE.match(text))
        self.assertIsNone(R._ROUTE_TAG_RE.match(text))
        self.assertIsNone(R._NOTE_TAG_RE.match(text))


class ClientGuardTests(Base):
    """The exemption is enforced where the request is made, and it is narrow."""

    def seat_client(self, key: str | None = None, email: str = OPERATOR_EMAIL):
        return Z.ZulipClient(
            Z.Credentials(email, key or self.operator_key, self.fake.url, "test"),
            self.fake.url,
            owner_user_id=OWNER_ID,
        )

    def test_an_unverified_client_cannot_send_a_status_message(self) -> None:
        client = self.seat_client()
        with self.assertRaises(Z.DmGuardRefused) as caught:
            client.operator_status_dm(CODEX_ID, TAGGED)
        self.assertEqual(caught.exception.code, "dm_operator_not_verified")
        with self.assertRaises(Z.DmGuardRefused) as caught:
            client.request(
                "POST",
                "messages",
                {"type": "direct", "to": [CODEX_ID], "content": TAGGED},
                owner_guard_exempt=True,
            )
        self.assertEqual(caught.exception.code, "dm_operator_not_verified")
        self.assertEqual(self.direct_messages(), [])

    def test_a_client_whose_credential_changed_after_it_was_verified_stops_qualifying(self) -> None:
        client = self.operator_client()
        client.creds = Z.Credentials(
            "claude-bot@zulip.test", self.seat_key, self.fake.url, "test"
        )
        with self.assertRaises(Z.DmGuardRefused) as caught:
            client.operator_status_dm(CODEX_ID, TAGGED)
        self.assertEqual(caught.exception.code, "dm_operator_not_verified")
        self.assertEqual(self.direct_messages(), [])

    def test_the_marker_needs_a_real_user_id_and_an_address(self) -> None:
        client = self.seat_client()
        for bad in (True, 0, -4, "9101", None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                client.mark_operator_verified(bad, OPERATOR_EMAIL, OWNER_ID)
        client.mark_operator_verified(OPERATOR_ID, "", OWNER_ID)
        with self.assertRaises(Z.DmGuardRefused):
            client.operator_status_dm(CODEX_ID, TAGGED)

    def test_a_verified_client_stops_qualifying_when_the_owner_pin_is_removed(self) -> None:
        client = self.operator_client()
        client.owner_user_id = 0
        with self.assertRaises(Z.DmGuardRefused) as caught:
            client.operator_status_dm(CODEX_ID, TAGGED)
        self.assertEqual(caught.exception.code, "dm_owner_not_pinned")
        self.assertEqual(self.direct_messages(), [])

    def test_a_verified_client_stops_qualifying_when_the_owner_pin_is_rotated(self) -> None:
        client = self.operator_client()
        client.owner_user_id = 99
        with self.assertRaises(Z.DmGuardRefused) as caught:
            client.operator_status_dm(CODEX_ID, TAGGED)
        self.assertEqual(caught.exception.code, "dm_operator_not_verified")
        self.assertEqual(self.direct_messages(), [])

    def test_a_client_built_against_another_owner_pin_cannot_be_marked(self) -> None:
        for pin, code in ((0, "dm_owner_not_pinned"), (99, "dm_operator_not_verified")):
            client = Z.ZulipClient(
                Z.Credentials(OPERATOR_EMAIL, self.operator_key, self.fake.url, "test"),
                self.fake.url,
                owner_user_id=pin,
            )
            with self.assertRaises(Z.DmGuardRefused, msg=pin) as caught:
                client.mark_operator_verified(OPERATOR_ID, OPERATOR_EMAIL, OWNER_ID)
            self.assertEqual(caught.exception.code, code)
            with self.assertRaises(Z.DmGuardRefused):
                client.operator_status_dm(CODEX_ID, TAGGED)
        for unpinned in (0, None, "x"):
            with self.assertRaises(Z.DmGuardRefused, msg=repr(unpinned)):
                self.seat_client().mark_operator_verified(OPERATOR_ID, OPERATOR_EMAIL, unpinned)
        self.assertEqual(self.direct_messages(), [])

    def test_the_verified_client_still_cannot_dm_anyone_else_without_the_owner(self) -> None:
        client = self.operator_client()
        with self.assertRaises(Z.DmGuardRefused) as caught:
            client.post("messages", {"type": "direct", "to": [CODEX_ID], "content": "hello"})
        self.assertEqual(caught.exception.code, "dm_owner_not_participant")
        self.assertEqual(self.direct_messages(), [])

    def test_the_exemption_flag_keeps_the_exact_shape_on_every_other_call(self) -> None:
        client = self.operator_client()
        draft = {"type": "private", "to": [CODEX_ID, 13], "content": TAGGED, "timestamp": 1}
        cases = [
            ("POST", "messages", {"type": "direct", "to": [CODEX_ID, 13], "content": TAGGED}),
            ("POST", "messages", {"type": "direct", "to": [CODEX_ID], "content": "free text"}),
            ("POST", "messages", {"type": "private", "to": [CODEX_ID], "content": TAGGED}),
            ("POST", "messages", {"type": "direct", "to": [CODEX_ID], "content": TAGGED, "topic": "t"}),
            ("POST", "scheduled_messages", {"type": "direct", "to": [CODEX_ID], "content": TAGGED}),
            ("POST", "drafts", {"drafts": [draft]}),
            ("PATCH", "drafts/5", {"draft": draft}),
            ("PATCH", "messages/7", {"content": TAGGED}),
            ("POST", "messages", {"type": "stream", "to": "general", "topic": "t", "content": TAGGED}),
        ]
        for method, path, params in cases:
            with self.assertRaises(Z.DmGuardRefused, msg=(method, path)) as caught:
                client.request(method, path, params, owner_guard_exempt=True)
            self.assertIn(caught.exception.code, ("dm_status_content", "dm_status_recipient"))
            with self.assertRaises(Z.DmGuardRefused, msg=("post/patch", method, path)):
                client.post(path, params, owner_guard_exempt=True) if method == "POST" else client.patch(
                    path, params, owner_guard_exempt=True
                )
        self.assertEqual(self.direct_messages(), [])

    def test_only_exactly_true_turns_the_exemption_on(self) -> None:
        client = self.operator_client()
        params = {"type": "direct", "to": [CODEX_ID, 13], "content": "group"}
        for flag in ("no", 1, "false", [], {"x": 1}):
            with self.assertRaises(Z.DmGuardRefused, msg=repr(flag)) as caught:
                client.request("POST", "messages", params, owner_guard_exempt=flag)
            self.assertEqual(caught.exception.code, "dm_status_content")
        self.assertEqual(self.direct_messages(), [])

    def test_a_status_message_has_exactly_one_recipient_by_user_id(self) -> None:
        client = self.operator_client()
        for bad in (True, 0, -3, "11", None, [CODEX_ID, OWNER_ID]):
            with self.assertRaises(Z.DmGuardRefused, msg=repr(bad)) as caught:
                client.operator_status_dm(bad, TAGGED)
            self.assertEqual(caught.exception.code, "dm_status_recipient")
        self.assertEqual(self.direct_messages(), [])

    def test_a_status_message_must_open_with_the_tag_and_mention_nobody(self) -> None:
        client = self.operator_client()
        for bad in (
            "hello",
            "[CLAUDE·wake] re=1\nhi",
            "[OPERATOR·wake] re=1\nhi",
            "[OPERATOR·clarify] re=1\nwhich seat?",
            "[OPERATOR·status] re=1\nping @**Jay Wedgeworth**",
            "[OPERATOR·status] re=1\n" + "x" * Z.OPERATOR_STATUS_MAX_CHARS,
            None,
        ):
            with self.assertRaises(Z.DmGuardRefused, msg=repr(bad)[:40]) as caught:
                client.operator_status_dm(CODEX_ID, bad)
            self.assertEqual(caught.exception.code, "dm_status_content")
        self.assertEqual(self.direct_messages(), [])

    def test_free_text_under_the_tag_is_refused(self) -> None:
        """The tag alone is not the key:  anything but the vocabulary's own text is refused."""
        client = self.operator_client()
        woken = "[OPERATOR·status] re=1\nYour message tagged MM.  MM was woken."
        self.assertEqual(woken, TAGGED)
        for bad in (
            woken + "  The AFC budget thread is blocked on the Q3 plan.",
            woken + "\nThe AFC budget thread is blocked on the Q3 plan.",
            woken + "\n",
            woken + " ",
            woken.replace("  ", " "),
            woken.replace("  ", "  "),
            woken.replace("\n", "\r\n"),
            woken.replace("was woken", "was woken, see the budget thread"),
            woken.replace("MM was woken", "Everyone was woken"),
            woken.replace("tagged MM", "tagged mm"),
            woken.replace("re=1", "re=01"),
            woken.replace("re=1", "re=0"),
            woken.replace("re=1", "re=" + "9" * 19),
            woken.replace("re=1", "re=1 topic=budget"),
            woken.replace("Your message", "Your note"),
            "[OPERATOR·status] re=1\nProject plan: ship the thing on Friday.",
            "[OPERATOR·status]",
            "[OPERATOR·status] re=1\nYour message tagged MM.  It was not delivered to MM.  Because of the budget.",
            "[OPERATOR·status] re=1\nYour message tagged MM.  MM was woken.  The seat is over its wake budget.",
            "[OPERATOR•status] re=1\nYour message tagged MM.  MM was woken.",
        ):
            with self.assertRaises(Z.DmGuardRefused, msg=repr(bad)[:60]) as caught:
                client.operator_status_dm(CODEX_ID, bad)
            self.assertEqual(caught.exception.code, "dm_status_content")
            self.assertFalse(OST.is_status_text(bad), repr(bad)[:60])
        self.assertEqual(self.direct_messages(), [])

    def test_every_text_the_vocabulary_builds_passes_the_guard(self) -> None:
        params = {"type": "direct", "to": [CODEX_ID]}
        for seat in ("MM", "CLAUDE", "GB-ORACLE", "A" * 32):
            for outcome in OS.OUTCOMES:
                reasons = [None, *OS.REASONS] if outcome in OS.REASON_OUTCOMES else [None]
                for reason in reasons:
                    text = OS.build_text(
                        "status", reply_to=987654321, seat=seat, outcome=outcome, reason=reason
                    )
                    self.assertTrue(OST.is_status_text(text), text)
                    Z.check_operator_status_send("POST", "messages", dict(params, content=text))
        text = OS.build_text("status", reply_to=OST.MAX_MESSAGE_ID, seat="MM", outcome="woken")
        self.assertTrue(OST.is_status_text(text))
        for not_text in (None, 7, b"x", ["x"], ""):
            self.assertFalse(OST.is_status_text(not_text), repr(not_text))


class IdentityTests(Base):
    def test_a_bot_that_users_me_does_not_confirm_is_never_marked(self) -> None:
        creds = Z.Credentials(OPERATOR_EMAIL, self.operator_key, self.fake.url, "test")
        client = Z.ZulipClient(creds, self.fake.url, owner_user_id=OWNER_ID)
        for env, owner in ((self.env(OPERATOR_ZULIP_USER_ID="4242"), OWNER_ID), (self.env(), 99)):
            out = OS.connect_operator(env=env, owner_user_id=owner, client=client, credentials=creds)
            self.assertEqual(out["status"], "refused")
            self.assertIn("not the pinned Operator", out["reason"])
            self.assertNotIn("client", out)
        with self.assertRaises(Z.DmGuardRefused) as caught:
            client.operator_status_dm(CODEX_ID, TAGGED)
        self.assertEqual(caught.exception.code, "dm_operator_not_verified")
        self.assertEqual(self.direct_messages(), [])

    def test_an_account_that_is_not_the_pinned_operator_sends_nothing(self) -> None:
        # The credential is a real bot's, but its Zulip user id is not the pinned one:  only the
        # identity check on users/me stands between this and a status sent by the wrong bot.
        out = self.send(env=self.env(OPERATOR_ZULIP_USER_ID="4242"))
        self.assertEqual(out["status"], "refused")
        self.assertIn("not the pinned Operator", out["reason"])
        out = self.send(owner_user_id=99, reply_to=77)
        self.assertEqual(out["status"], "refused")
        self.assertIn("not the pinned Operator", out["reason"])
        self.assertEqual(self.direct_messages(), [])

    def test_the_seats_key_cannot_be_presented_under_the_operators_address(self) -> None:
        def fetch(names):
            return {"ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL, "ZULIP_OPERATOR_API_KEY": self.seat_key}

        out = self.send(fetch=fetch)
        self.assertEqual(out["status"], "refused")
        self.assertEqual(out["code"], "credential_rejected")
        self.assertEqual(self.direct_messages(), [])

    def test_the_seats_own_credential_is_refused_at_the_credential_check(self) -> None:
        def seat_fetch(names):
            return {"ZULIP_OPERATOR_EMAIL": "claude-bot@zulip.test", "ZULIP_OPERATOR_API_KEY": self.seat_key}

        out = self.send(fetch=seat_fetch)
        self.assertEqual(out["status"], "refused")
        self.assertEqual(self.direct_messages(), [])

    def test_a_passed_in_client_with_another_owner_pin_is_refused(self) -> None:
        creds = Z.Credentials(OPERATOR_EMAIL, self.operator_key, self.fake.url, "test")
        client = Z.ZulipClient(creds, self.fake.url, owner_user_id=99)
        link = OS.connect_operator(
            env=self.env(), owner_user_id=OWNER_ID, client=client, credentials=creds
        )
        self.assertEqual(link["status"], "refused")
        self.assertIn("could not be marked", link["reason"])
        with self.assertRaises(Z.DmGuardRefused):
            client.operator_status_dm(CODEX_ID, TAGGED)
        self.assertEqual(self.direct_messages(), [])

    def test_the_owner_must_be_pinned_so_the_operator_can_be_checked(self) -> None:
        for bad in (0, -1, None, True):
            out = self.send(owner_user_id=bad)
            self.assertEqual(out["status"], "refused", bad)
        self.assertEqual(self.direct_messages(), [])

    def test_a_rejected_credential_on_users_me_is_masked(self) -> None:
        bad = secrets.token_hex(16)
        out = self.send(
            fetch=lambda names: {"ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL, "ZULIP_OPERATOR_API_KEY": bad}
        )
        self.assertEqual(out["code"], "credential_rejected")
        self.assertNotIn(bad, repr(out))
        self.assertIn(bad[:8], out["reason"])
        self.assertEqual(self.direct_messages(), [])

    def test_a_missing_operator_credential_is_a_clean_refusal(self) -> None:
        def broken(names):
            raise RuntimeError("secret store down")

        out = self.send(fetch=broken)
        self.assertEqual(out["status"], "refused")
        self.assertNotIn("secret store down", out["reason"])

    def test_a_home_given_as_a_string_is_accepted(self) -> None:
        with mock.patch.object(OS.SI, "operator_credentials", wraps=OS.SI.operator_credentials) as read:
            out = self.send(home=str(self.tmp))
        self.assertEqual(out["status"], "sent", out["reason"])
        self.assertIsInstance(read.call_args.kwargs["home"], Path)


class SendTests(Base):
    def test_the_status_goes_one_to_one_from_the_operator_without_the_owner(self) -> None:
        out = self.send()
        self.assertEqual(out["status"], "sent", out["reason"])
        sent = self.direct_messages()
        self.assertEqual(len(sent), 1)
        self.assertEqual(self.recipients_of(sent[0]), {OPERATOR_ID, CODEX_ID})
        self.assertNotIn(OWNER_ID, self.recipients_of(sent[0]))
        self.assertEqual(sent[0]["sender_id"], OPERATOR_ID)
        self.assertTrue(sent[0]["content"].startswith("[OPERATOR·status] re=1234"))

    def test_the_sentence_gap_is_the_owners_gap_on_the_way_out(self) -> None:
        self.send()
        content = self.direct_messages()[0]["content"]
        self.assertIn("MM.  MM was woken.", content)
        self.assertNotIn("  ", content)

    def test_the_message_carries_nothing_about_the_project(self) -> None:
        self.send(seat="GB-ORACLE", outcome="dropped", reason="budget")
        content = self.direct_messages()[0]["content"]
        self.assertEqual(content.count("\n"), 1, "a header line and one body line, nothing quoted")
        for word in ("topic", "channel", "stream", "http", "```", ">"):
            self.assertNotIn(word, content)

    def test_the_owner_may_be_the_recipient_when_the_owner_sent_the_message(self) -> None:
        out = self.send(recipient_user_id=OWNER_ID)
        self.assertEqual(out["status"], "sent", out["reason"])
        self.assertEqual(self.recipients_of(self.direct_messages()[0]), {OPERATOR_ID, OWNER_ID})

    def test_a_recipient_is_a_user_id_never_a_name_or_an_address(self) -> None:
        for who in ("Codex", "11", "codex-bot@zulip.test", "Jay Wedgeworth", 11.0, None, True, 0, -5):
            out = self.send(recipient_user_id=who)
            self.assertEqual(out["status"], "refused", who)
            self.assertEqual(out["code"], "recipient", who)
        self.assertEqual(self.direct_messages(), [])

    def test_an_unknown_deactivated_or_unreachable_recipient_is_refused(self) -> None:
        self.fake.user_by_id(13)["is_active"] = False
        self.fake.user_by_id(14)["bot_type"] = 2
        for who in (987654, 13, 14):
            self.assertEqual(self.send(recipient_user_id=who)["status"], "refused", who)
        self.assertEqual(self.direct_messages(), [])

    def test_an_outgoing_webhook_seat_bot_can_take_its_status(self) -> None:
        self.fake.user_by_id(14)["bot_type"] = 3
        out = self.send(recipient_user_id=14)
        self.assertEqual(out["status"], "sent", out["reason"])
        self.assertEqual(self.recipients_of(self.direct_messages()[0]), {OPERATOR_ID, 14})

    def test_the_operator_does_not_message_itself(self) -> None:
        out = self.send(recipient_user_id=OPERATOR_ID)
        self.assertEqual(out["status"], "refused")
        self.assertIn("the Operator itself", out["reason"])

    def test_a_malformed_users_response_is_refused(self) -> None:
        for body in (
            {"result": "success", "members": "nope"},
            {"result": "success", "members": [{"user_id": "11"}]},
            {"result": "success"},
        ):
            self.fake.inject("GET", "users", status=200, body=body)
            out = self.send(reply_to=50 + len(self.fake.faults))
            self.assertEqual(out["status"], "refused", body)
            self.assertIn("malformed", out["reason"])
        self.assertEqual(self.direct_messages(), [])

    def test_text_outside_the_vocabulary_is_refused_before_anything_connects(self) -> None:
        with mock.patch.object(OS, "connect_operator") as connect:
            out = self.send(outcome="woken, and here is the budget thread")
        self.assertEqual(out["status"], "refused")
        self.assertEqual(out["code"], "vocabulary")
        connect.assert_not_called()

    def test_a_secret_shaped_seat_tag_is_withheld_and_not_echoed(self) -> None:
        shaped = "AKIAIOSFODNN7EXAMPLE"
        out = self.send(seat=shaped)
        self.assertEqual(out["status"], "refused")
        self.assertEqual(out["code"], "secret")
        self.assertNotIn(shaped, repr(out))
        self.assertEqual(self.direct_messages(), [])


class DeliveryTests(Base):
    def test_a_delivered_outcome_is_reported_once_for_a_day(self) -> None:
        first = self.send()
        second = self.send(now=1100.0)
        self.assertEqual(second["status"], "sent")
        self.assertEqual(second["message_id"], first["message_id"])
        later = self.send(now=1000.0 + OS.DEDUPE_WINDOW - 1)
        self.assertEqual(later["message_id"], first["message_id"])
        self.assertEqual(len(self.direct_messages()), 1)

    def test_a_different_outcome_or_message_is_a_different_report(self) -> None:
        self.send()
        self.assertEqual(self.send(outcome="queued", now=1100.0)["status"], "sent")
        self.assertEqual(self.send(reply_to=1235, now=1200.0)["status"], "sent")
        self.assertEqual(self.send(recipient_user_id=OWNER_ID, now=1300.0)["status"], "sent")
        self.assertEqual(len(self.direct_messages()), 4)

    def test_the_same_status_after_the_window_is_sent_again(self) -> None:
        self.send()
        self.assertEqual(self.send(now=1000.0 + OS.DEDUPE_WINDOW + 1)["status"], "sent")
        self.assertEqual(len(self.direct_messages()), 2)

    def test_a_repeat_costs_no_connection(self) -> None:
        self.send()
        with mock.patch.object(OS, "connect_operator") as connect:
            again = self.send(now=1100.0)
        self.assertEqual(again["status"], "sent")
        connect.assert_not_called()

    def test_a_pending_reservation_is_a_duplicate_and_nothing_is_sent(self) -> None:
        env = self.env()
        key = OS._fingerprint(CODEX_ID, "status", 1234, "MM", "woken", None)
        OC._reserve(OS.state_path(env), key, 1000.0, OS.DEDUPE_WINDOW, CODEX_ID)
        out = self.send(now=1010.0)
        self.assertEqual(out["status"], "duplicate")
        self.assertEqual(out["delivery_status"], OC.DELIVERY_UNKNOWN)
        self.assertEqual(self.direct_messages(), [])

    def test_eight_simultaneous_identical_calls_send_once(self) -> None:
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(self.send()["status"])) for _ in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(self.direct_messages()), 1)
        self.assertNotIn("refused", results)

    def test_an_unknown_outcome_is_not_replayed(self) -> None:
        client = self.operator_client()
        with mock.patch.object(
            client, "operator_status_dm", side_effect=Z.NetworkError("reset", maybe_sent=True)
        ):
            first = self.send(client=client)
        self.assertEqual(first["status"], "refused")
        self.assertEqual(first["delivery_status"], OC.DELIVERY_UNKNOWN)
        again = self.send(client=client, now=1100.0)
        self.assertEqual(again["status"], "refused")
        self.assertEqual(again["delivery_status"], OC.DELIVERY_UNKNOWN)
        self.assertEqual(self.direct_messages(), [])

    def test_a_send_that_never_left_may_be_tried_again(self) -> None:
        client = self.operator_client()
        with mock.patch.object(
            client, "operator_status_dm", side_effect=Z.NetworkError("refused to connect")
        ):
            first = self.send(client=client)
        self.assertEqual(first["delivery_status"], OC.DELIVERY_RETRYABLE)
        again = self.send(client=client, now=1100.0)
        self.assertEqual(again["status"], "sent", again["reason"])

    def test_a_gateway_error_on_the_post_is_unknown_and_not_replayed(self) -> None:
        for status in (502, 504, 500, 408):
            self.fake.inject("POST", "messages", status=status, body={"result": "error", "msg": "gw"})
            out = self.send(reply_to=900 + status)
            self.assertEqual(out["delivery_status"], OC.DELIVERY_UNKNOWN, status)
            again = self.send(reply_to=900 + status, now=1100.0)
            self.assertNotEqual(again["status"], "sent", status)

    def test_a_definite_refusal_by_zulip_gives_the_key_back(self) -> None:
        self.fake.inject("POST", "messages", status=400, body={"result": "error", "msg": "bad"})
        first = self.send()
        self.assertEqual(first["delivery_status"], OC.DELIVERY_REFUSED)
        self.assertEqual(self.send(now=1100.0)["status"], "sent")

    def test_a_rate_limit_that_outlasts_the_clients_retries_is_retryable(self) -> None:
        client = self.operator_client()
        with mock.patch.object(
            client, "operator_status_dm", side_effect=Z.ApiError("slow down", status=429)
        ):
            first = self.send(client=client)
        self.assertEqual(first["status"], "refused")
        self.assertEqual(first["delivery_status"], OC.DELIVERY_RETRYABLE)
        self.assertEqual(self.send(client=client, now=1100.0)["status"], "sent")

    def test_a_rejected_credential_on_the_post_is_masked_and_the_key_is_released(self) -> None:
        self.fake.inject(
            "POST", "messages", status=403, body={"result": "error", "msg": "no", "code": "UNAUTHORIZED"}
        )
        out = self.send()
        self.assertEqual(out["code"], "credential_rejected")
        self.assertEqual(out["delivery_status"], OC.DELIVERY_REFUSED)
        self.assertNotIn(self.operator_key, repr(out))
        self.assertEqual(self.send(now=1100.0)["status"], "sent")

    def test_a_failure_to_record_a_delivered_send_still_reports_sent_and_blocks_replay(self) -> None:
        real = OC._record

        def fail_on_ok(path, key, at, window, rid, mid, state):
            if state == OC.DELIVERY_OK:
                raise OSError("disk full")
            return real(path, key, at, window, rid, mid, state)

        with mock.patch.object(OC, "_record", side_effect=fail_on_ok):
            out = self.send()
            again = self.send(now=1100.0)
        self.assertEqual(out["status"], "sent")
        self.assertIsNotNone(out["message_id"])
        self.assertEqual(again["delivery_status"], OC.DELIVERY_UNKNOWN)
        self.assertEqual(len(self.direct_messages()), 1)

    def test_an_unexpected_failure_after_the_call_is_unknown_and_not_replayed(self) -> None:
        client = self.operator_client()
        with mock.patch.object(client, "operator_status_dm", side_effect=RuntimeError("x")):
            first = self.send(client=client)
        again = self.send(client=client, now=1100.0)
        self.assertEqual(first["delivery_status"], OC.DELIVERY_UNKNOWN)
        self.assertEqual(again["delivery_status"], OC.DELIVERY_UNKNOWN)
        self.assertEqual(self.direct_messages(), [])

    def test_the_state_file_is_the_operators_own_and_private(self) -> None:
        self.send()
        path = OS.state_path(self.env())
        self.assertTrue(path.startswith(str(self.state / "OPERATOR")))
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode), 0o700)

    def test_an_unwritable_state_directory_sends_nothing_and_does_not_raise(self) -> None:
        blocker = self.tmp / "blocker"
        blocker.write_text("x")
        out = self.send(env=self.env(AGENT_SYNC_STATE_DIR=str(blocker)))
        self.assertEqual(out["status"], "refused")
        self.assertEqual(self.direct_messages(), [])


class RoutingTests(Base):
    def test_the_router_drops_the_status_dm_without_waking_the_recipient(self) -> None:
        self.assertEqual(self.send()["status"], "sent")
        msg = dict(self.direct_messages()[0], flags=[])
        users = {u["user_id"]: u for u in self.fake.users}
        eligible = R.fleet_bot_ids(users, CFG.fleet_tags(None), OWNER_ID)
        me = R.SeatIdentity("CODEX", CODEX_ID, "Codex", "codex-bot@zulip.test", OWNER_ID)
        ctx = R.Context(
            owner_user_id=OWNER_ID,
            owner_clients=set(),
            eligible_user_ids=eligible,
            is_bot={u["user_id"]: u["is_bot"] for u in self.fake.users},
            now=msg["timestamp"] + 5,
            stale_after=600,
            presence=[],
        )
        _, decision = R.route(msg, me, ctx, [])
        self.assertIsNone(decision.wake)
        self.assertEqual(decision.wake_block, "not_eligible")


class PolicyTests(unittest.TestCase):
    def test_the_operator_may_open_a_status_dm(self) -> None:
        DMP.check_new_dm(
            {}, identity="OPERATOR", recipients=[{"user_id": 11, "is_bot": True}], status=True
        )

    def test_the_operator_may_still_ask_a_clarifying_question(self) -> None:
        DMP.check_new_dm({}, identity="OPERATOR", recipients=[], clarifying=True)

    def test_the_operator_without_a_reason_still_may_not_open_a_dm(self) -> None:
        with self.assertRaises(DMP.DmRefused) as caught:
            DMP.check_new_dm({}, identity="OPERATOR", recipients=[])
        self.assertEqual(caught.exception.reason, "operator_not_clarifying")

    def test_a_seat_cannot_claim_the_status_exemption(self) -> None:
        for seat in ("MM", "CLAUDE", "CODEX"):
            with self.assertRaises(DMP.DmRefused, msg=seat):
                DMP.check_new_dm(
                    {},
                    identity=seat,
                    recipients=[{"user_id": 11, "is_bot": True}],
                    status=True,
                )

    def test_the_status_exemption_is_the_operators_alone(self) -> None:
        self.assertTrue(DMP.status_exempt("OPERATOR", status=True))
        self.assertFalse(DMP.status_exempt("OPERATOR", status=False))
        self.assertFalse(DMP.status_exempt("MM", status=True))


if __name__ == "__main__":
    unittest.main()
