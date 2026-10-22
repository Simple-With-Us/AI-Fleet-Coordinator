"""The DM rule and the Side-Chats route (owner decision, Oct 22 2026).

Covers the plan's five cases:

  (f)   an ambiguous wake opens exactly one clarifying DM;  a clean wake opens none
  (f2)  a seat-initiated DM is refused and audited
  (f3)  a seat-to-seat message lands in Side-Chats and no DM is created
  (f4)  a seat's ping to Jay goes to Pending-for-Jay in Side-Chats, and no DM is created
  (f5)  an unanswered clarifying DM drops the wake, logs it, and a late reply does not revive it

The rule is one module (dm_policy.py) so the stdio tools, the CLI and the hosted Worker all enforce
the same thing;  these tests exercise the rule itself plus the clarifying queue, and the transport
tests live in test_mcp_stdio.py.
"""

from __future__ import annotations

import json
import os
import tempfile
import types
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from agent_sync import clarify
from agent_sync import dm_policy as DMP
from agent_sync import zulip as Z
from agent_sync.mcp import tools as T


class FakeZulip:
    """Just enough client to see what a post became."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        self.posts.append({"path": path, **body})
        return {"id": 1000 + len(self.posts)}


def bot(user_id: int, name: str = "Bot") -> dict[str, Any]:
    return {"user_id": user_id, "full_name": name, "is_bot": True}


def human(user_id: int, name: str = "Human") -> dict[str, Any]:
    return {"user_id": user_id, "full_name": name, "is_bot": False}


class ClarifyingWakeTests(unittest.TestCase):
    """(f)  an ambiguous wake asks exactly one question;  a clean one asks nothing."""

    def test_a_clean_wake_opens_no_dm(self) -> None:
        self.assertFalse(DMP.needs_clarification(["CODEX"]))
        self.assertFalse(DMP.needs_clarification(["codex"]))  # one seat, any spelling

    def test_an_ambiguous_wake_needs_one_question(self) -> None:
        for candidates in (["CLAUDE", "MM"], [], ["CLAUDE", "MM", "CODEX"]):
            with self.subTest(candidates=candidates):
                self.assertTrue(DMP.needs_clarification(candidates))
        self.assertIn("CODEX", DMP.clarifying_question(["CLAUDE", "CODEX"]))

    def test_one_question_per_message_even_when_asked_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = str(tmp)
            clarify.ask(root, message_id=42, sender_user_id=7, sender="Jay", now=100.0,
                        timeout_seconds=1800.0, question=DMP.clarifying_question(["CLAUDE", "MM"]))
            clarify.ask(root, message_id=42, sender_user_id=7, sender="Jay", now=110.0,
                        timeout_seconds=1800.0, question=DMP.clarifying_question(["CLAUDE", "MM"]))
            entries = clarify.pending_entries(root)
            self.assertEqual(len(entries), 1, "one entry, so at most one DM was opened")
            self.assertEqual(entries[0]["message_id"], 42)
            self.assertEqual(entries[0]["deadline"], 1910.0, "the second ask replaces the entry, not adds one")


class SeatDmRefusedTests(unittest.TestCase):
    """(f2)  a seat-initiated DM is refused and audited."""

    def test_a_seat_may_not_open_a_dm_to_a_seat(self) -> None:
        with self.assertRaises(DMP.DmRefused) as caught:
            DMP.check_new_dm({}, identity="CLAUDE", recipients=[bot(7)])
        self.assertEqual(caught.exception.reason, "seat")
        self.assertIn("Side-Chats", str(caught.exception))
        self.assertIn(str(DMP.SIDE_CHATS_STREAM_ID), str(caught.exception))

    def test_a_seat_may_not_open_a_dm_to_jay(self) -> None:
        with self.assertRaises(DMP.DmRefused) as caught:
            DMP.check_new_dm({}, identity="CODEX", recipients=[human(12)], owner_user_id=12)
        self.assertEqual(caught.exception.reason, "owner")
        self.assertIn(DMP.PENDING_FOR_JAY_TOPIC, str(caught.exception))

    def test_the_refusal_is_audited_with_ids_and_no_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            written = DMP.audit_refusal(
                {"AGENT_SYNC_STATE_DIR": tmp}, None, transport="stdio", identity="CLAUDE",
                recipients=[bot(7)], reason="seat", message="this seat may not open a new direct message",
            )
            self.assertTrue(written)
            row = json.loads((Path(tmp) / "logs" / "identity.jsonl").read_text().strip())
            self.assertEqual(row["tool"], "zulip_dm_send")
            self.assertEqual(row["outcome"], "dm_refused:seat")
            self.assertEqual(row["identity"], "CLAUDE")
            self.assertEqual(row["transport"], "stdio")

    def test_a_reply_inside_a_dm_operator_opened_is_allowed(self) -> None:
        # Nothing to refuse:  the conversation is Operator's and the seat is answering in it.
        DMP.check_new_dm({}, identity="CLAUDE", recipients=[bot(DMP.Z.OPERATOR_USER_ID)],
                         operator_opened=True)

    def test_operator_may_open_only_a_clarifying_question(self) -> None:
        DMP.check_new_dm({}, identity=DMP.OPERATOR_SEAT, recipients=[human(12)],
                         clarifying=True, owner_user_id=12)
        with self.assertRaises(DMP.DmRefused) as caught:
            DMP.check_new_dm({}, identity=DMP.OPERATOR_SEAT, recipients=[human(12)], owner_user_id=12)
        self.assertEqual(caught.exception.reason, "operator_not_clarifying")

    def test_the_block_defaults_on_and_one_flag_turns_it_off(self) -> None:
        self.assertTrue(DMP.rule_enabled({}))
        self.assertTrue(DMP.rule_enabled({"AGENT_SYNC_DM_RULE": "on"}))
        for off in ("off", "0", "false", "no", "NONE", " disabled "):
            with self.subTest(off=off):
                self.assertFalse(DMP.rule_enabled({"AGENT_SYNC_DM_RULE": off}))
        # And with the block off, an ordinary seat DM is allowed again.
        DMP.check_new_dm({"AGENT_SYNC_DM_RULE": "off"}, identity="CLAUDE", recipients=[human(12)])


class SideChatsTests(unittest.TestCase):
    """(f3) and (f4):  bot-to-seat and seat-to-Jay land in Side-Chats, never in a DM."""

    def test_a_seat_to_seat_message_lands_in_side_chats_and_no_dm(self) -> None:
        client = FakeZulip()
        participants = ["CLAUDE", "CODEX"]
        topic = DMP.side_chats_topic(participants)
        message_id = DMP.side_chats_post(client, topic=topic, content="[CLAUDE] handing this to CODEX")
        self.assertEqual(message_id, 1001)
        self.assertEqual(len(client.posts), 1, "exactly one post, and it is not a DM")
        post = client.posts[0]
        self.assertEqual(post["type"], "stream", "never type=direct")
        self.assertEqual(post["to"], str(DMP.SIDE_CHATS_STREAM_ID), "keyed on the stream id")
        self.assertEqual(post["topic"], "CLAUDE <-> CODEX")
        # The topic names only the participants:  nobody else is in it.
        for name in ("MM", "Jay", "ECHO"):
            self.assertNotIn(name, post["topic"])

    def test_a_seat_ping_to_jay_goes_to_pending_for_jay_in_side_chats(self) -> None:
        client = FakeZulip()
        DMP.side_chats_post(client, topic=DMP.pending_for_jay_topic(),
                            content="[CLAUDE] AFC-1 needs Jay")
        post = client.posts[0]
        self.assertEqual(post["type"], "stream")
        self.assertEqual(post["to"], str(DMP.SIDE_CHATS_STREAM_ID))
        self.assertEqual(post["topic"], DMP.PENDING_FOR_JAY_TOPIC)
        self.assertEqual(len(client.posts), 1, "no DM was created alongside the post")

    def test_the_topic_is_built_from_seat_tags_only(self) -> None:
        self.assertEqual(DMP.side_chats_topic(["coDEX", "CLAUDE", "CODEX"]), "CLAUDE <-> CODEX")
        self.assertEqual(DMP.side_chats_topic([]), DMP.SIDE_CHATS_NAME)


class TimeoutDropTests(unittest.TestCase):
    """(f5)  an unanswered clarifying DM drops the wake;  a late reply does not revive it."""

    def test_the_timeout_defaults_to_thirty_minutes_and_is_configurable(self) -> None:
        self.assertEqual(DMP.clarify_timeout_seconds({}), 30 * 60.0)
        self.assertEqual(DMP.clarify_timeout_seconds({"AGENT_SYNC_DM_CLARIFY_TIMEOUT_MINUTES": "5"}), 300.0)
        self.assertEqual(DMP.clarify_timeout_seconds({"AGENT_SYNC_DM_CLARIFY_TIMEOUT_MINUTES": "junk"}), 1800.0)

    def test_an_unanswered_question_drops_the_wake_and_logs_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = str(tmp)
            clarify.ask(root, message_id=4242, sender_user_id=7, sender="Jay", now=1000.0,
                        timeout_seconds=1800.0, question=DMP.clarifying_question(["CLAUDE", "MM"]))
            self.assertEqual(clarify.expire(root, now=2000.0), [], "not due yet")
            dropped = clarify.expire(root, now=3000.0)
            self.assertEqual(len(dropped), 1)
            row = dropped[0]
            self.assertEqual(row["message_id"], 4242)
            self.assertEqual(row["sender"], "Jay")
            self.assertEqual(row["reason"], clarify.REASON_NO_REPLY)

            logged: list[tuple[str, dict[str, Any]]] = []
            clarify.log_dropped(lambda event, **fields: logged.append((event, fields)), row)
            self.assertEqual(logged[0][0], "wake_dropped")
            fields = logged[0][1]
            self.assertEqual(fields["message_id"], 4242)
            self.assertEqual(fields["sender"], "Jay")
            self.assertEqual(fields["reason"], "no_reply")
            self.assertEqual(clarify.pending_entries(root), [], "nothing is still pending")

    def test_a_late_reply_does_not_revive_a_dropped_wake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = str(tmp)
            clarify.ask(root, message_id=9, sender_user_id=7, sender="Jay", now=0.0, timeout_seconds=60.0)
            clarify.expire(root, now=120.0)
            self.assertEqual(clarify.answer(root, message_id=9, seat="CODEX", now=130.0), "ignored")
            self.assertEqual(clarify.pending_entries(root), [])
            self.assertEqual(clarify.due(root, now=10_000.0), [], "a dropped entry is never due again")

    def test_a_reply_inside_the_timeout_routes_the_wake(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = str(tmp)
            clarify.ask(root, message_id=11, sender_user_id=7, sender="Jay", now=0.0, timeout_seconds=1800.0)
            self.assertEqual(clarify.answer(root, message_id=11, seat="CODEX", now=60.0), "routed")
            self.assertEqual(clarify.expire(root, now=10_000.0), [], "an answered question is never dropped")
            self.assertEqual(clarify.answer(root, message_id=11, seat="MM", now=70.0), "ignored")


class OperatorOpenedDmTests(unittest.TestCase):
    def test_only_a_conversation_operator_opened_counts(self) -> None:
        people = [bot(DMP.Z.OPERATOR_USER_ID)]
        opened = [{"type": "private", "id": 5, "sender_id": DMP.Z.OPERATOR_USER_ID,
                   "display_recipient": [{"id": DMP.Z.OPERATOR_USER_ID}, {"id": 7}]}]
        self.assertTrue(DMP.operator_opened_dm(people, opened, self_id=7))
        # A seat opened it:  that is a new DM the rule refuses.
        seat_opened = [{"type": "private", "id": 5, "sender_id": 7,
                        "display_recipient": [{"id": DMP.Z.OPERATOR_USER_ID}, {"id": 7}]}]
        self.assertFalse(DMP.operator_opened_dm(people, seat_opened, self_id=7))
        # No messages yet:  nothing exists to reply inside.
        self.assertFalse(DMP.operator_opened_dm(people, [], self_id=7))

    def test_a_group_conversation_is_not_the_one_to_one_dm(self) -> None:
        # A conversation that merely contains the same people is a different conversation:  answering
        # in it is not the reply-inside-an-Operator-DM the rule allows, so it must not open the door.
        people = [bot(DMP.Z.OPERATOR_USER_ID)]
        group = [{"type": "private", "id": 5, "sender_id": DMP.Z.OPERATOR_USER_ID,
                  "display_recipient": [{"id": DMP.Z.OPERATOR_USER_ID}, {"id": 7}, {"id": 11}]}]
        self.assertFalse(DMP.operator_opened_dm(people, group, self_id=7))


class RefusalExitCodeTests(unittest.TestCase):
    """A refusal is not a malformed command:  the CLI must exit 3 so a caller can tell them apart."""

    def test_the_refusal_is_an_agentsync_error_with_exit_code_three(self) -> None:
        self.assertTrue(issubclass(DMP.DmRefused, Z.AgentSyncError))
        self.assertEqual(DMP.DmRefused.exit_code, 3)
        self.assertEqual(DMP.DmRefused("no", reason="seat").exit_code, 3)


class ToolRefusalContextTests(unittest.TestCase):
    """A refusal carries which seat was authenticated and which identity it claimed."""

    def _tools(self, home: str) -> Any:
        class StubAgent:
            seat = "CODEX"
            tag = "CODEX"
            rt = types.SimpleNamespace(env={"HOME": home})

            def users(self) -> list[dict[str, Any]]:
                return [{"user_id": 9, "is_bot": True}]

            def fetch(self, operands: Any, **kw: Any) -> list[dict[str, Any]]:
                return []

        tools = T.Tools.__new__(T.Tools)
        tools.agent = StubAgent()
        tools.me = {"user_id": 7}
        tools.seat = "CODEX"
        tools.owner_user_id = None
        return tools

    def test_a_refusal_names_the_seat_and_the_claimed_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tools = self._tools(tmp)
            with self.assertRaises(T.ToolError) as caught:
                tools._check_dm_rule([{"user_id": 9, "is_bot": True}], {})
            self.assertEqual(caught.exception.code, "dm_not_allowed")
            self.assertEqual(caught.exception.extra["authenticated_seat"], "CODEX")
            self.assertEqual(caught.exception.extra["acting_identity"], "CODEX")
            self.assertEqual(caught.exception.extra["replacement"], "side_chats")

    def test_a_scheduled_direct_message_cannot_step_around_the_rule(self) -> None:
        # The rule runs where every `direct` target is built, so scheduling the same message for
        # later is refused exactly as sending it now would be.
        with tempfile.TemporaryDirectory() as tmp:
            tools = self._tools(tmp)
            checked: list[Any] = []
            tools._people = lambda ids, emails, active=True: [{"user_id": int(i)} for i in ids]
            tools._check_dm_rule = lambda people, args: checked.append(people)
            self.assertEqual(tools._target({"user_ids": [9]}), {"type": "direct", "to": [9]})
            self.assertEqual(len(checked), 1, "building a direct target must run the DM rule")

    def test_a_scheduled_direct_message_is_refused_when_the_rule_is_on(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tools = self._tools(tmp)
            tools._people = lambda ids, emails, active=True: [{"user_id": int(i)} for i in ids]
            with self.assertRaises(T.ToolError) as caught:
                tools._target({"user_ids": [9]})
            self.assertEqual(caught.exception.code, "dm_not_allowed")


class ClarifyQueueHardeningTests(unittest.TestCase):
    """The queue is durable, validated, scanned and written under one lock."""

    def test_a_sender_carrying_a_secret_is_never_stored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = str(tmp)
            with self.assertRaises(clarify.ClarifyRefused) as caught:
                clarify.ask(root, message_id=5, sender_user_id=7,
                            sender="Jay token=abcdefghijklmnopqrstuvwx", now=0.0, timeout_seconds=60.0)
            self.assertEqual(caught.exception.field, "sender")
            self.assertEqual(clarify.pending_entries(root), [], "nothing may reach the file")
            with self.assertRaises(clarify.ClarifyRefused) as caught:
                clarify.ask(root, message_id=5, sender_user_id=7, sender="Jay", now=0.0,
                            timeout_seconds=60.0, question="which seat?  token=abcdefghijklmnopqrstuvwx")
            self.assertEqual(caught.exception.field, "question")
            self.assertEqual(clarify.pending_entries(root), [])

    def test_a_malformed_entry_is_refused_by_name(self) -> None:
        import json

        for bad, expected in (
            ({"message_id": 5, "sender_user_id": 7, "sender": "Jay", "state": "pending",
              "asked_at": 0.0}, "deadline"),
            ({"message_id": 5, "sender_user_id": 7, "sender": "Jay", "state": "wat",
              "asked_at": 0.0, "deadline": 60.0}, "state"),
            ({"message_id": 5, "sender_user_id": 7, "sender": "Jay", "state": "pending",
              "asked_at": 0.0, "deadline": 60.0, "extra": 1}, "extra"),
            ({"message_id": 5, "sender_user_id": 7, "sender": "Jay", "state": "dropped",
              "asked_at": 0.0, "deadline": 60.0, "reason": "because", "dropped_at": 1.0}, "because"),
        ):
            with self.subTest(expected=expected):
                with tempfile.TemporaryDirectory() as tmp:
                    path = clarify.state_path(str(tmp))
                    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
                    with open(path, "w", encoding="utf-8") as fh:
                        json.dump({"entries": {"5": bad}}, fh)
                    with self.assertRaises(ValueError) as caught:
                        clarify.load(str(tmp))
                    self.assertIn(expected, str(caught.exception))

    def test_the_queue_file_is_private_and_leaves_no_temporary_behind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = str(tmp)
            clarify.ask(root, message_id=5, sender_user_id=7, sender="Jay", now=0.0, timeout_seconds=60.0)
            path = clarify.state_path(root)
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            self.assertEqual([n for n in os.listdir(os.path.dirname(path)) if ".tmp" in n], [])

    def test_an_expiry_racing_a_reply_does_not_both_win(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = str(tmp)
            clarify.ask(root, message_id=7, sender_user_id=7, sender="Jay", now=0.0, timeout_seconds=60.0)

            def drop() -> list[Any]:
                return clarify.expire(root, now=120.0)

            def reply() -> str:
                return clarify.answer(root, message_id=7, seat="CODEX", now=120.0)

            with ThreadPoolExecutor(max_workers=2) as pool:
                dropped, routed = pool.submit(drop), pool.submit(reply)
                dropped_rows, outcome = dropped.result(), routed.result()
            self.assertLessEqual(len(dropped_rows), 1)
            self.assertIn(outcome, ("routed", "ignored"))
            entries = clarify.load(root)["entries"]["7"]
            if outcome == "routed":
                self.assertEqual(entries["state"], clarify.ROUTED)
            elif dropped_rows:
                self.assertEqual(entries["state"], clarify.DROPPED)


class DmExistsFetchFailureTests(unittest.TestCase):
    """A failed conversation lookup is an empty conversation, not a crash.

    `_dm_exists` caught `Z.APIError`, but zulip.py has always defined `ApiError` and `NetworkError`,
    so the except clause itself raised AttributeError and the DM rule crashed on any API or network
    failure instead of treating the conversation as new."""

    @staticmethod
    def _tools(raising: Exception) -> Any:
        class RaisingAgent:
            seat = "CODEX"
            calls: list[dict[str, Any]] = []

            def fetch(self, operands: Any, **kw: Any) -> list[dict[str, Any]]:
                RaisingAgent.calls.append({"operands": operands, **kw})
                raise raising

        tools = T.Tools.__new__(T.Tools)
        tools.agent = RaisingAgent()
        tools.me = {"user_id": 7}
        return tools

    def test_an_api_error_means_no_existing_messages(self) -> None:
        for exc in (Z.ApiError("zulip said no"), Z.NetworkError("connection reset")):
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(self._tools(exc)._dm_exists([9]), [])

    def test_the_except_clause_names_classes_that_exist(self) -> None:
        # Pins the bug itself:  a typo'd name here fails only when an exception is actually raised.
        self.assertTrue(hasattr(Z, "ApiError"))
        self.assertTrue(hasattr(Z, "NetworkError"))
        self.assertEqual(self._tools(Z.ApiError("boom"))._dm_exists([]), [], "empty id list short-circuits")

    def test_a_working_fetch_still_reports_the_conversation(self) -> None:
        class WorkingAgent:
            seat = "CODEX"

            def fetch(self, operands: Any, **kw: Any) -> list[dict[str, Any]]:
                return [
                    {"type": "private", "id": 5, "sender_id": Z.OPERATOR_USER_ID},
                    {"type": "stream", "id": 6},
                ]

        tools = T.Tools.__new__(T.Tools)
        tools.agent = WorkingAgent()
        tools.me = {"user_id": 7}
        self.assertEqual([m["id"] for m in tools._dm_exists([9])], [5], "only private messages, unchanged")


class ClarifyingFlagOnTheCliTests(unittest.TestCase):
    """`agent-sync dm` must be able to say "this is the one clarifying question".

    The DM check read `args.clarifying_question`, but the `dm` parser never registered
    `--clarifying-question`, so the attribute was always absent and Operator's clarifying DM was
    refused on the CLI no matter what it passed."""

    @staticmethod
    def _parser() -> Any:
        from agent_sync import cli

        return cli.build_parser()

    def test_the_dm_parser_registers_the_flag(self) -> None:
        args = self._parser().parse_args(["dm", "--clarifying-question", "which seat?"])
        self.assertTrue(args.clarifying_question, "the flag must reach the DM check")

    def test_without_the_flag_it_is_false(self) -> None:
        args = self._parser().parse_args(["dm", "which seat?"])
        self.assertFalse(args.clarifying_question, "an ordinary DM is not a clarifying question")

    def test_the_operator_allowance_reads_that_attribute(self) -> None:
        # The attribute name the check reads is the one argparse now sets.
        args = self._parser().parse_args(["dm", "--clarifying-question"])
        self.assertEqual(getattr(args, "clarifying_question", None), True)


if __name__ == "__main__":
    unittest.main()