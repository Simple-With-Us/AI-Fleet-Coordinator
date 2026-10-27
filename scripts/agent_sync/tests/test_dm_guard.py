"""The owner-in-the-conversation guard (AFC-469).

Jay's ruling (Oct 23 2026):  a bot may send a direct message only when Jay is an actual participant
in the conversation.  Participation is the authenticated Zulip **user id**:  a display name that
looks like the owner is not participation, and mentioning Jay in the message text is not
participation either.

The guard sits on the Zulip client, so one check covers the CLI, the stdio MCP, the listener and
the hosted Worker together.  It fails closed:  with no owner id pinned a direct send is refused.

The older seat DM rule (AFC-473, `AGENT_SYNC_DM_RULE`) is a different rule and it is off in this
harness, so every case below is the new guard alone.  The two rules stack:  with the older rule on,
the CLI refuses a `dm` outright, and the new guard still holds the paths that rule never saw (the
daemon's wake reply, the hosted client, drafts and scheduled messages).
"""
from __future__ import annotations

import json
import unittest

from agent_sync import zulip as Z
from agent_sync.tests.fake_zulip import BOT_EMAIL
from agent_sync.tests.harness import Harness

# The fake realm:  10 is this bot (Claude), 11 is Codex, 12 is Jay Wedgeworth (the owner),
# 14 is Grok.  A production user id is private inventory, so none is committed here.
OWNER_ID = 12
CODEX_ID = 11
GROK_ID = 14


class GuardShapeTests(unittest.TestCase):
    """What counts as a direct send, and who counts as a recipient."""

    def test_a_channel_send_is_not_a_direct_message(self) -> None:
        self.assertIsNone(Z.direct_recipient_sets({"type": "stream", "to": "agent-sync", "topic": "t"}))
        self.assertIsNone(Z.direct_recipient_sets({"type": "stream", "to": 7, "topic": "t"}))
        self.assertIsNone(Z.direct_recipient_sets({}))
        self.assertIsNone(Z.direct_recipient_sets(None))

    def test_a_direct_send_names_its_recipients(self) -> None:
        self.assertEqual(Z.direct_recipient_sets({"type": "direct", "to": [CODEX_ID, OWNER_ID]}), [[CODEX_ID, OWNER_ID]])
        self.assertEqual(Z.direct_recipient_sets({"type": "private", "to": [CODEX_ID]}), [[CODEX_ID]])
        self.assertEqual(Z.direct_recipient_sets({"type": "direct", "to": "[11, 12]"}), [[CODEX_ID, OWNER_ID]])

    def test_a_draft_names_its_recipients_inside_the_drafts_list(self) -> None:
        self.assertEqual(
            Z.direct_recipient_sets({"drafts": [{"type": "private", "to": [CODEX_ID, OWNER_ID]}]}),
            [[CODEX_ID, OWNER_ID]],
        )

    def test_a_batch_names_a_recipient_set_for_every_direct_draft(self) -> None:
        #  One set per conversation:  a union would let one owner-addressed draft authorise the
        #  bot to bot draft beside it (AFC-469).
        self.assertEqual(
            Z.direct_recipient_sets(
                {
                    "drafts": [
                        {"type": "private", "to": [CODEX_ID, OWNER_ID]},
                        {"type": "private", "to": [CODEX_ID]},
                    ]
                }
            ),
            [[CODEX_ID, OWNER_ID], [CODEX_ID]],
        )

    def test_a_draft_key_does_not_hide_a_top_level_direct_message(self) -> None:
        #  A body that carries a draft key and names a direct message at the top level is two
        #  conversations, and both are checked:  the draft branch must not short-circuit the
        #  top-level one (AFC-469).
        self.assertEqual(
            Z.direct_recipient_sets(
                {"draft": {"type": "stream", "to": [7], "topic": "t"}, "type": "direct", "to": [CODEX_ID]}
            ),
            [[CODEX_ID]],
        )
        self.assertEqual(
            Z.direct_recipient_sets(
                {
                    "drafts": [{"type": "stream", "to": [7], "topic": "t"}],
                    "type": "direct",
                    "to": [CODEX_ID],
                }
            ),
            [[CODEX_ID]],
        )

    def test_a_malformed_draft_is_refused_with_its_index(self) -> None:
        #  A draft nobody can read is not skipped:  an unreadable draft could reach the request
        #  builder while another owner-addressed draft satisfied the guard.
        with self.assertRaises(ValueError) as caught:
            Z.direct_recipient_sets({"drafts": [{"type": "private", "to": [CODEX_ID, OWNER_ID]}, "nope"]})
        self.assertIn("drafts[1]", str(caught.exception))
        #  A drafts list with no direct draft in it is not a direct message at all.
        self.assertIsNone(Z.direct_recipient_sets({"drafts": [{"type": "stream", "to": [7], "topic": "t"}]}))
        self.assertIsNone(Z.direct_recipient_sets({"drafts": []}))

    def test_an_unreadable_recipient_set_is_an_empty_one(self) -> None:
        #  A `to` nobody can read fails the guard closed:  a recipient set nobody can read is a
        #  recipient set nobody may claim to include the owner.
        self.assertEqual(Z.direct_recipient_sets({"type": "direct", "to": "agent-sync"}), [[]])
        self.assertEqual(Z.direct_recipient_sets({"type": "direct", "to": ["x"]}), [[]])


class GuardDecisionTests(unittest.TestCase):
    """The verdict, and the reason the caller gets."""

    def test_a_direct_send_without_the_owner_in_it_is_refused(self) -> None:
        with self.assertRaises(Z.DmGuardRefused) as caught:
            Z.check_dm_guard("messages", {"type": "direct", "to": [CODEX_ID]}, OWNER_ID)
        self.assertEqual(caught.exception.code, "dm_owner_not_participant")
        self.assertEqual(caught.exception.exit_code, 3)
        self.assertIn("dm_owner_not_participant", str(caught.exception))

    def test_the_owner_in_the_recipients_is_allowed(self) -> None:
        Z.check_dm_guard("messages", {"type": "direct", "to": [CODEX_ID, OWNER_ID]}, OWNER_ID)

    def test_no_pinned_owner_fails_closed(self) -> None:
        for owner in (0, None, "", "x"):
            with self.subTest(owner=owner):
                with self.assertRaises(Z.DmGuardRefused) as caught:
                    Z.check_dm_guard("messages", {"type": "direct", "to": [CODEX_ID]}, owner)
                self.assertEqual(caught.exception.code, "dm_owner_not_pinned")
                self.assertEqual(caught.exception.exit_code, 3)

    def test_a_channel_post_is_never_refused(self) -> None:
        #  No owner id at all, and a channel post still goes out:  the guard is about direct
        #  messages only.
        Z.check_dm_guard("messages", {"type": "stream", "to": "agent-sync", "topic": "t"}, 0)

    def test_a_path_that_cannot_carry_a_message_is_never_refused(self) -> None:
        Z.check_dm_guard("users/me", {"type": "direct", "to": [CODEX_ID]}, 0)


class ClientGuardTests(Harness):
    """The guard on the client, where every send goes through."""

    def client(self, owner_user_id: int = 0) -> Z.ZulipClient:
        creds = Z.Credentials(email=BOT_EMAIL, key=self.key, site=self.fake.url, source="env")
        return Z.ZulipClient(creds, self.fake.url, timeout=2, owner_user_id=owner_user_id)

    def test_a_direct_send_with_no_owner_id_is_refused_and_nothing_is_sent(self) -> None:
        client = self.client()
        with self.assertRaises(Z.DmGuardRefused) as caught:
            client.post("messages", {"type": "direct", "to": [CODEX_ID], "content": "hello"})
        self.assertEqual(caught.exception.code, "dm_owner_not_pinned")
        self.assertEqual(self.fake.requests_to("POST", "messages"), [])

    def test_owner_user_id_zero_fails_closed(self) -> None:
        client = self.client(0)
        with self.assertRaises(Z.DmGuardRefused) as caught:
            client.post("messages", {"type": "direct", "to": [CODEX_ID], "content": "hello"})
        self.assertEqual(caught.exception.code, "dm_owner_not_pinned")
        self.assertEqual(self.fake.requests_to("POST", "messages"), [])

    def test_a_stream_post_is_unaffected(self) -> None:
        client = self.client(0)
        result = client.post(
            "messages", {"type": "stream", "to": "agent-sync", "topic": "t", "content": "hello"}
        )
        self.assertIn("id", result)
        self.assertEqual(len(self.fake.requests_to("POST", "messages")), 1)

    def test_a_direct_send_with_the_owner_goes_out(self) -> None:
        client = self.client(OWNER_ID)
        client.post("messages", {"type": "direct", "to": [CODEX_ID, OWNER_ID], "content": "hello"})
        self.assertEqual(len(self.fake.requests_to("POST", "messages")), 1)

    def test_a_direct_send_without_the_owner_is_refused_and_nothing_is_sent(self) -> None:
        client = self.client(OWNER_ID)
        with self.assertRaises(Z.DmGuardRefused) as caught:
            client.post("messages", {"type": "direct", "to": [CODEX_ID], "content": "hello"})
        self.assertEqual(caught.exception.code, "dm_owner_not_participant")
        self.assertEqual(self.fake.requests_to("POST", "messages"), [])

    def test_a_scheduled_direct_message_is_guarded_too(self) -> None:
        client = self.client(OWNER_ID)
        with self.assertRaises(Z.DmGuardRefused) as caught:
            client.post(
                "scheduled_messages",
                {
                    "type": "direct",
                    "to": [CODEX_ID],
                    "content": "hello",
                    "scheduled_delivery_timestamp": 4102444800,
                },
            )
        self.assertEqual(caught.exception.code, "dm_owner_not_participant")
        self.assertEqual(self.fake.requests_to("POST", "scheduled_messages"), [])

    def test_a_draft_key_does_not_hide_a_top_level_direct_message(self) -> None:
        client = self.client(OWNER_ID)
        with self.assertRaises(Z.DmGuardRefused) as caught:
            client.post(
                "messages",
                {
                    "draft": {"type": "stream", "to": [7], "topic": "t"},
                    "type": "direct",
                    "to": [CODEX_ID],
                    "content": "hello",
                },
            )
        self.assertEqual(caught.exception.code, "dm_owner_not_participant")
        self.assertEqual(self.fake.requests_to("POST", "messages"), [])

    def test_a_batched_draft_cannot_borrow_the_owners_participation(self) -> None:
        #  One owner-addressed draft beside a bot to bot draft does not authorise the second one
        #  (AFC-469):  every direct conversation in the batch has to carry the owner.
        client = self.client(OWNER_ID)
        with self.assertRaises(Z.DmGuardRefused) as caught:
            client.post(
                "drafts",
                {
                    "drafts": [
                        {"type": "private", "to": [CODEX_ID, OWNER_ID], "topic": "", "content": "ok"},
                        {"type": "private", "to": [CODEX_ID], "topic": "", "content": "not ok"},
                    ]
                },
            )
        self.assertEqual(caught.exception.code, "dm_owner_not_participant")
        self.assertEqual(self.fake.requests_to("POST", "drafts"), [])

    def test_a_malformed_draft_entry_is_refused(self) -> None:
        client = self.client(OWNER_ID)
        with self.assertRaises(ValueError) as caught:
            client.post(
                "drafts",
                {"drafts": [{"type": "private", "to": [CODEX_ID, OWNER_ID], "topic": "", "content": "ok"}, "nope"]},
            )
        self.assertIn("drafts[1]", str(caught.exception))
        self.assertEqual(self.fake.requests_to("POST", "drafts"), [])

    def test_a_direct_draft_is_guarded_too(self) -> None:
        client = self.client(OWNER_ID)
        with self.assertRaises(Z.DmGuardRefused) as caught:
            client.post(
                "drafts",
                {"drafts": [{"type": "private", "to": [CODEX_ID], "topic": "", "content": "hello"}]},
            )
        self.assertEqual(caught.exception.code, "dm_owner_not_participant")
        self.assertEqual(self.fake.requests_to("POST", "drafts"), [])

    def test_editing_a_direct_draft_is_guarded_too(self) -> None:
        #  A draft's recipient set is rewritten by a PATCH, and a draft is a message nobody has
        #  sent yet, so the edit is checked exactly like the create is (AFC-469).
        client = self.client(OWNER_ID)
        with self.assertRaises(Z.DmGuardRefused) as caught:
            client.patch(
                "drafts/42",
                {"draft": {"type": "private", "to": [CODEX_ID], "topic": "", "content": "hello"}},
            )
        self.assertEqual(caught.exception.code, "dm_owner_not_participant")
        self.assertEqual(self.fake.requests_to("PATCH", "drafts/42"), [])

    def test_editing_a_direct_draft_with_the_owner_goes_out(self) -> None:
        client = self.client(OWNER_ID)
        made = client.post(
            "drafts",
            {"drafts": [{"type": "private", "to": [CODEX_ID, OWNER_ID], "topic": "", "content": "hi"}]},
        )
        draft_id = made["ids"][0]
        result = client.patch(
            "drafts/%d" % draft_id,
            {"draft": {"type": "private", "to": [CODEX_ID, OWNER_ID], "topic": "", "content": "hi"}},
        )
        self.assertEqual(result["result"], "success")
        self.assertEqual(len(self.fake.requests_to("PATCH", "drafts/%d" % draft_id)), 1)


class CliGuardTests(Harness):
    """`simple-chat dm`:  the guard on the recipient set the CLI resolves."""

    def test_cmd_dm_refuses_a_bot_only_recipient_set(self) -> None:
        self.pin_owner()
        result = self.run_cli("dm", "--to", "Codex", "--", "Review is up.")
        self.assertEqual(result.code, 3, result.err)
        self.assertIn("dm_owner_not_participant", result.err)
        self.assertEqual(self.fake.requests_to("POST", "messages"), [])

    def test_cmd_dm_refuses_a_bot_and_bot_recipient_set(self) -> None:
        self.pin_owner()
        result = self.run_cli("dm", "--to", "Codex", "--to", "Grok", "--", "Review is up.")
        self.assertEqual(result.code, 3, result.err)
        self.assertIn("dm_owner_not_participant", result.err)
        self.assertEqual(self.fake.requests_to("POST", "messages"), [])

    def test_cmd_dm_refuses_a_recipient_whose_name_looks_like_the_owner(self) -> None:
        #  The name is not participation:  only the authenticated user id is.
        self.pin_owner()
        self.fake.add_user("jay-jr@zulip.test", "Jay Wedgeworth Jr.", is_bot=True)
        result = self.run_cli("dm", "--to", "Jay Wedgeworth Jr.", "--", "Are you the owner?")
        self.assertEqual(result.code, 3, result.err)
        self.assertIn("dm_owner_not_participant", result.err)
        self.assertEqual(self.fake.requests_to("POST", "messages"), [])

    def test_cmd_dm_allows_a_bot_and_the_owner_by_id(self) -> None:
        self.pin_owner()
        result = self.run_cli("dm", "--owner", "--to", "Codex", "--", "Review is up.")
        self.assertEqual(result.code, 0, result.err)
        form = self.fake.requests_to("POST", "messages")[-1].form
        self.assertEqual(form["type"], "direct")
        self.assertEqual(sorted(json.loads(form["to"])), sorted([OWNER_ID, CODEX_ID]))

    def test_cmd_dm_refuses_everything_when_no_owner_is_pinned(self) -> None:
        #  No pinned owner id:  the guard fails closed, so even a DM to the owner by name is
        #  refused rather than sent unchecked.
        result = self.run_cli("dm", "--to", "Jay Wedgeworth", "--", "Review is up.")
        self.assertEqual(result.code, 3, result.err)
        self.assertIn("dm_owner_not_pinned", result.err)
        self.assertEqual(self.fake.requests_to("POST", "messages"), [])


if __name__ == "__main__":
    unittest.main()
