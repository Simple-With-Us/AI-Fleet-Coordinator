"""The notice:  pending, maybe-sent, reruns and the Grok Bot mirror."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from needs_owner import block as blockmod
from needs_owner import comment_keys
from needs_owner import items as itemsmod
from needs_owner import ledger
from needs_owner import links
from needs_owner import notify as notifymod
from needs_owner import records
from needs_owner import text as T
from needs_owner import verbs
from needs_owner.errors import BoardFailed
from needs_owner.proc import Result

from .world import REALM_LINK, SCRIPTS, World

Q = "Revive PR 761 or close it?"
UID = "needs-owner:CLAUDE:abc12345"


def body_with_ref(uid: str = UID) -> str:
    return "[CLAUDE·needsown] @**Jay** **Decision needed** from CLAUDE:  `Q?`\n\nLinear:  https://linear.app/x/AFC-1\n\nRef:  `%s`\n" % uid


def angle(body: str) -> str:
    """An older client's quote-and-reply:  every line of the original behind `> `."""
    return "\n".join("> " + line for line in body.splitlines()) + "\n\nyes"


def quoted(body: str, message_id: int = 630000000) -> str:
    """Zulip's quote-and-reply:  the original inside a ```quote fence, no `>` on its lines."""
    return ("@_**Jay|7** [said](https://simplewithus.zulipchat.com/#narrow/channel/12-work-in-progress/topic/x/near/%d):\n"
            "```quote\n%s\n```\n\nyes please" % (message_id, body.rstrip("\n")))


class Reader:
    """A runner that answers `simple-chat read` with fixed rows (the real `message_json` keys)."""

    def __init__(self, seat: str, rows: list) -> None:
        self.seat = seat
        self.rows = rows
        self.calls: list = []

    def agent_sync(self, *args: str, stdin=None) -> Result:
        self.calls.append(args)
        return Result(0, "\n".join(json.dumps(r) if isinstance(r, dict) else r for r in self.rows) + "\n")


def row(message_id, seat, content, *, is_bot=True) -> dict:
    return {"id": message_id, "channel": "work-in-progress", "topic": "OWNER AFC-1 x", "content": content, "is_bot": is_bot, "seat": seat}


class Stub:
    """A runner that answers with fixed results:  the board fails the way a test says, and the post lands.

    The board CLI's exit codes are the contract the classifier reads, so a stub
    pins them without a child process."""

    def __init__(self, result: Result, *, seat: str = "CLAUDE", environ: dict | None = None,
                 msg_id: int = 630000000) -> None:
        self.seat = seat
        self.env = environ if environ is not None else {}
        self.result = result
        self.msg_id = msg_id
        self.board_calls: list[str] = []
        self.written: list[str] = []
        self.sends: list[tuple] = []

    def board(self, *args: str) -> Result:
        self.board_calls.append(args[0])
        return self.result

    def agent_sync(self, *args: str, stdin=None) -> Result:
        self.sends.append(args)
        return Result(0, json.dumps({"id": self.msg_id}) + "\n")

    @contextlib.contextmanager
    def private_file(self, text: str):
        self.written.append(text)
        yield "/tmp/a-private-file"


class ReferenceLineTests(unittest.TestCase):
    def test_the_reference_is_a_line_of_its_own(self) -> None:
        self.assertTrue(notifymod.carries_ref(body_with_ref(), UID))
        self.assertTrue(notifymod.carries_ref(body_with_ref().replace("Ref:  ", "Ref:  ").rstrip("\n"), UID))
        self.assertTrue(notifymod.carries_ref("Ref: `%s`   " % UID, UID))
        for content in ("see Ref:  `%s` above" % UID, "Ref:  `%s2`" % UID, "Ref:  `x%s`" % UID, "Ref:  %s" % UID, "Ref:  `%s` yes" % UID, ""):
            self.assertFalse(notifymod.carries_ref(content, UID), content)

    def test_a_quote_does_not_count(self) -> None:
        body = body_with_ref()
        self.assertFalse(notifymod.carries_ref(quoted(body), UID), "quote-and-reply (a ```quote fence)")
        self.assertFalse(notifymod.carries_ref("\n".join("> " + line for line in body.splitlines()), UID), "a > quote")
        self.assertFalse(notifymod.carries_ref("   > Ref:  `%s`" % UID, UID))
        self.assertFalse(notifymod.carries_ref("~~~\n%s\n~~~" % body, UID), "a ~~~ fence")
        self.assertFalse(notifymod.carries_ref("````quote\n```\ninner\n```\n%s\n````\nok" % body, UID), "a longer outer fence")
        self.assertTrue(notifymod.carries_ref("%s\n```quote\nother words\n```\n" % body, UID), "a quote of something else beside it")

    def test_a_fence_that_never_closes_hides_nothing(self) -> None:
        self.assertTrue(notifymod.carries_ref("```\nopened and left open\n\nRef:  `%s`\n" % UID, UID))

    def test_the_reference_survives_a_question_full_of_markup(self) -> None:
        world = World(self)
        question = "Keep ``` and ~~~ and > quotes in a question?"
        self.assertEqual(world.file(question, "Yes", "No").code, 0)
        (filed,) = [c for c in world.calls("simple-board") if c["argv"][0] == "file"]
        uid = filed["argv"][filed["argv"].index("--uid") + 1]
        (message,) = world.zulip()["messages"]
        self.assertTrue(notifymod.carries_ref(message["content"], uid), message["content"])


class FindNoticeTests(unittest.TestCase):
    def find(self, seat: str, rows: list) -> int | None:
        return notifymod.find_notice(Reader(seat, rows), "OWNER AFC-1 x", UID)

    def test_only_a_bot_of_the_filers_seat_or_claude_counts(self) -> None:
        body = body_with_ref()
        self.assertEqual(self.find("CODEX", [row(5, "CODEX", body)]), 5)
        self.assertEqual(self.find("BF-DIRECTOR", [row(6, "CLAUDE", body)]), 6, "CLAUDE's fallback notifier posts for any seat")
        self.assertEqual(self.find("GROK", [row(7, "GROK-BUILD", body)]), 7, "every spelling of the seat")
        for rejected in (row(8, "CODEX", body), row(9, None, body, is_bot=False), row(10, "CLAUDE", body, is_bot=False),
                         row(11, "CLAUDE", body, is_bot=None), row(12, "BF-PLUMBER", body)):
            self.assertIsNone(self.find("BF-DIRECTOR", [rejected]), rejected)

    def test_the_oldest_match_is_the_notice(self) -> None:
        body = body_with_ref()
        rows = [row(30, "CLAUDE", body), row(20, "BF-DIRECTOR", body), row(40, "BF-DIRECTOR", body)]
        self.assertEqual(self.find("BF-DIRECTOR", rows), 20)

    def test_a_quote_by_an_allowed_bot_is_not_the_notice(self) -> None:
        body = body_with_ref()
        rows = [row(20, "CLAUDE", quoted(body)), row(21, "CLAUDE", angle(body)), row(22, "CLAUDE", "I will post it again soon")]
        self.assertIsNone(self.find("CLAUDE", rows))
        self.assertEqual(self.find("CLAUDE", rows + [row(23, "CLAUDE", body)]), 23)

    def test_odd_rows_are_skipped(self) -> None:
        body = body_with_ref()
        rows = ["not json", "[1, 2]", {"id": "7", "is_bot": True, "seat": "CLAUDE", "content": body},
                {"id": True, "is_bot": True, "seat": "CLAUDE", "content": body}, {"is_bot": True, "seat": "CLAUDE", "content": body},
                row(9, "CLAUDE", body)]
        self.assertEqual(self.find("CLAUDE", rows), 9)

    def test_it_asks_for_the_real_read_with_the_json_flag(self) -> None:
        reader = Reader("CLAUDE", [])
        notifymod.find_notice(reader, "OWNER AFC-1 x", UID)
        (args,) = reader.calls
        self.assertEqual(args[0], "read")
        for flag in ("--json", "--include-self"):
            self.assertIn(flag, args)

    def test_a_failed_read_finds_nothing(self) -> None:
        class Broken(Reader):
            def agent_sync(self, *args, stdin=None):
                return Result(5, "", "boom")

        self.assertIsNone(notifymod.find_notice(Broken("CLAUDE", []), "t", UID))


class NoticeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.w = World(self)

    def tearDown(self) -> None:
        self.assertEqual(self.w.leftover_temp_files(), [])

    def _records(self, ident: str = "AFC-300") -> list[str]:
        return [c for c in self.w.comments(ident) if "[needs-owner]" in c]

    # ---- a seat that cannot send -------------------------------------------------------------
    def test_a_seat_with_no_credential_leaves_notify_pending(self) -> None:
        self.w.set_zulip(no_credential=["BF-DIRECTOR"])
        env = self.w.env(AGENT_SEAT="BF-DIRECTOR")
        r = self.w.file(Q, "Yes", "No", seat="BF-DIRECTOR", env=env)
        self.assertEqual(r.code, 0, r.err)
        got = r.json()
        self.assertEqual((got["state"], got["reason"]), ("notify_pending", "no_credential"))
        self.assertIsNone(got["notified_msg_id"])
        self.assertEqual(len(self._records()), 1)
        self.assertIn("[needs-owner] notify_pending", self._records()[0])
        self.assertIn("reason: no_credential", self._records()[0])
        self.assertEqual(self.w.zulip()["messages"], [])
        listed = self.w.run("list", "--json").json()
        self.assertEqual(listed[0]["state"], "notify_pending")

    def test_a_rerun_sends_a_pending_notice_and_records_once(self) -> None:
        self.w.set_zulip(post_mode="exit3")
        self.assertEqual(self.w.file(Q, "Yes", "No").json()["state"], "notify_pending")
        self.assertEqual(self.w.file(Q, "Yes", "No").json()["state"], "notify_pending")
        self.assertEqual(len([c for c in self._records() if "notify_pending" in c]), 1, "a rerun piles no second pending record")
        self.w.set_zulip(post_mode="ok")
        done = self.w.file(Q, "Yes", "No").json()
        self.assertEqual(done["state"], "notified")
        self.assertEqual(len(self.w.board_state()["issues"]), 1)
        self.assertEqual(len(self.w.zulip()["messages"]), 1)
        self.assertEqual(self.w.run("status", "AFC-300", "--json").json()["state"], "notified")

    def test_other_send_failures_are_pending_too(self) -> None:
        for n, mode in enumerate(("exit2", "exit3")):
            self.w.set_zulip(post_mode=mode)
            got = self.w.file("Question number %d?" % n, "Yes", "No").json()
            self.assertEqual(got["state"], "notify_pending", mode)
        self.assertEqual(self.w.run("list", "--json").json()[0]["notified_msg_id"], None)

    def test_no_agent_sync_is_pending(self) -> None:
        self.w.remove_tool("simple-chat")
        got = self.w.file(Q, "Yes", "No").json()
        self.assertEqual((got["state"], got["reason"]), ("notify_pending", "no_agent_sync"))
        self.assertEqual(len(self.w.board_state()["issues"]), 1)

    # ---- maybe sent --------------------------------------------------------------------------
    def test_exit_6_that_landed_is_found_by_its_reference_and_not_retried(self) -> None:
        for mode in ("exit6_landed", "exit5_landed"):
            self.w.set_zulip(post_mode=mode)
            q = "Landed question %s?" % mode
            got = self.w.file(q, "Yes", "No").json()
            self.assertEqual(got["state"], "notified", mode)
            self.assertIsInstance(got["notified_msg_id"], int)
        posts = [c for c in self.w.calls("simple-chat") if c["argv"][0] == "post"]
        self.assertEqual(len(posts), 2, "one post per item:  nothing was retried")
        self.assertEqual(len(self.w.zulip()["messages"]), 2)
        reads = [c for c in self.w.calls("simple-chat") if c["argv"][0] == "read"]
        self.assertEqual(len(reads), 2)
        self.assertIn("--include-self", reads[0]["argv"])

    def test_exit_6_that_did_not_land_is_pending_and_a_rerun_posts_once(self) -> None:
        self.w.set_zulip(post_mode="exit6_lost")
        got = self.w.file(Q, "Yes", "No").json()
        self.assertEqual((got["state"], got["reason"]), ("notify_pending", "send_failed"))
        self.w.set_zulip(post_mode="ok")
        self.assertEqual(self.w.file(Q, "Yes", "No").json()["state"], "notified")
        self.assertEqual(len(self.w.zulip()["messages"]), 1)

    def test_a_read_that_fails_never_turns_into_a_second_post(self) -> None:
        self.w.set_zulip(post_mode="exit6_landed", read_fails=True)
        got = self.w.file(Q, "Yes", "No").json()
        self.assertEqual(got["state"], "notify_pending")
        self.assertEqual(len(self.w.zulip()["messages"]), 1)
        # the rerun looks in the topic first, finds the reference, records it and posts nothing
        self.w.set_zulip(post_mode="ok", read_fails=False)
        again = self.w.file(Q, "Yes", "No").json()
        self.assertEqual(again["state"], "notified")
        self.assertEqual(len(self.w.zulip()["messages"]), 1)

    def test_a_post_that_landed_but_whose_record_failed_is_not_posted_twice(self) -> None:
        """The record write failed and its outcome is unknown, so the record may already be on the issue:  it is
        not written again, and the notice is not posted a second time."""
        r = self.w.file(Q, "Yes", "No", env=self.w.env(FAKE_BOARD_FAIL="comment"))
        self.assertEqual(r.code, 0, r.err)
        self.assertEqual((r.json()["state"], r.json()["reason"]), ("notify_pending", "outcome_unknown"))
        self.assertFalse(r.json()["recorded"])
        self.assertEqual(len(self.w.zulip()["messages"]), 1)
        again = self.w.file(Q, "Yes", "No")
        self.assertEqual(again.code, 0, again.err)
        self.assertEqual(again.json()["notified_msg_id"], 630000000, "the notice is found by its reference")
        self.assertEqual(again.json()["state"], "notify_pending", "the record may have landed:  the item is pending")
        self.assertEqual(len(self.w.zulip()["messages"]), 1)
        self.assertEqual(len([c for c in self._records() if "notified msg" in c]), 0, "the record is not written again")
        self.assertEqual(len([c for c in self._records() if "notify_pending" in c]), 1, "the pending record is on the issue")

    def test_a_post_without_an_id_is_found_by_its_reference(self) -> None:
        self.w.set_zulip(post_mode="nojson")
        got = self.w.file(Q, "Yes", "No").json()
        self.assertEqual(got["state"], "notified")
        self.assertEqual(len(self.w.zulip()["messages"]), 1)

    # ---- who may be taken for the notice ------------------------------------------------------
    def _uid(self) -> str:
        (filed,) = [c for c in self.w.calls("simple-board") if c["argv"][0] == "file"]
        return filed["argv"][filed["argv"].index("--uid") + 1]

    def _posts(self) -> list:
        return [c for c in self.w.calls("simple-chat") if c["argv"][0] in ("post", "reply")]

    def test_a_quote_or_a_copied_reference_is_never_taken_for_the_notice(self) -> None:
        """The post landed, its record could not be written, and the topic then fills with look-alikes.  The
        rerun takes the bot's own first post for the notice, posts nothing, and writes no record."""
        first = self.w.file(Q, "Yes", "No", env=self.w.env(FAKE_BOARD_FAIL="comment"))
        self.assertEqual(first.code, 0, first.err)
        self.assertFalse(first.json()["recorded"])
        (original,) = self.w.zulip()["messages"]
        self.assertEqual(original["id"], 630000000)
        topic, body = original["topic"], original["content"]
        self.assertTrue(notifymod.carries_ref(body, self._uid()))
        impostors = [
            self.w.add_message(topic, quoted(body)),  # Jay's quote-and-reply
            self.w.add_message(topic, angle(body)),  # an older client's quote
            self.w.add_message(topic, body, seat="CODEX"),  # a peer copies the public uid onto a line of its own
            self.w.add_message(topic, quoted(body), seat="CLAUDE"),  # CLAUDE quotes the notice
            self.w.add_message(topic, body, seat="CLAUDE"),  # a later full copy by a bot that is allowed:  the oldest wins
        ]
        again = self.w.file(Q, "Yes", "No")
        self.assertEqual(again.code, 0, again.err)
        got = again.json()
        self.assertEqual(got["notified_msg_id"], 630000000, "the bot's own first post is the notice, not a look-alike")
        self.assertEqual(got["state"], "notify_pending", "the record may have landed:  the item is still pending")
        self.assertEqual(len([c for c in self._records() if "notified msg" in c]), 0, "the record is not written twice")
        for stray in impostors:
            self.assertNotEqual(str(stray), str(got["notified_msg_id"]))
        self.assertEqual(len(self._posts()), 1, "one post across both runs")
        listed = self.w.run("list", "--json").json()
        self.assertEqual(listed[0]["notified_msg_id"], None, "no record is on the issue")

    def test_a_copied_reference_does_not_stop_the_notice_from_being_sent(self) -> None:
        """The post never landed.  A peer (or Jay's quote) carries the uid, and the rerun still posts."""
        self.w.set_zulip(post_mode="exit6_lost")
        self.assertEqual(self.w.file(Q, "Yes", "No").json()["state"], "notify_pending")
        topic, body = T.topic_for("AFC-300", Q), body_with_ref(self._uid())
        for message in (quoted(body), angle(body)):
            self.w.add_message(topic, message)
        copied = self.w.add_message(topic, body, seat="CODEX")
        self.w.set_zulip(post_mode="ok")
        again = self.w.file(Q, "Yes", "No").json()
        self.assertEqual(again["state"], "notified")
        self.assertGreater(again["notified_msg_id"], copied)
        mine = [m for m in self.w.zulip()["messages"] if m.get("seat") == "CLAUDE" and m["is_bot"]]
        self.assertEqual([m["id"] for m in mine], [again["notified_msg_id"]])
        self.assertEqual(len([p for p in self._posts() if p["argv"][0] == "post"]), 2, "the lost post, then one that landed")

    def test_the_fallback_notifiers_post_for_another_seat_is_found_and_not_repeated(self) -> None:
        self.w.set_zulip(no_credential=["BF-DIRECTOR"])
        env = self.w.env(AGENT_SEAT="BF-DIRECTOR")
        pending = self.w.file(Q, "Yes", "No", seat="BF-DIRECTOR", env=env)
        self.assertEqual(pending.json()["state"], "notify_pending")
        sent = self.w.add_message(T.topic_for("AFC-300", Q), body_with_ref(self._uid()), seat="CLAUDE")  # what C6-2 posts as CLAUDE
        self.w.set_zulip(no_credential=[])
        before = len(self._posts())
        done = self.w.file(Q, "Yes", "No", seat="BF-DIRECTOR", env=env).json()
        self.assertEqual((done["state"], done["notified_msg_id"]), ("notified", sent))
        self.assertEqual(len(self._posts()), before, "nothing was posted again")

    def test_the_fake_prints_the_keys_of_the_real_message_json(self) -> None:
        self.w.file(Q, "Yes", "No")
        topic = self.w.zulip()["messages"][0]["topic"]

        def read() -> list:
            ran = subprocess.run([str(self.w.bin / "simple-chat"), "read", "--as", "CLAUDE", "--json", "--channel", links.CHANNEL,
                                  "--topic", topic, "--include-self"], capture_output=True, text=True, env=self.w.env(), timeout=60)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            return [json.loads(line) for line in ran.stdout.splitlines()]

        shown = read()[0]
        code = ("import json, sys; sys.path.insert(0, %r)\nfrom agent_sync import cli as C\n"
                "class Probe(C.Agent):\n    def __init__(self): pass\n    def sender_is_bot(self, m): return True\n"
                "    def sender_seat(self, m, b): return ('CLAUDE', None)\n    def is_sibling(self, m): return False\n"
                "    def is_mention(self, m): return False\n"
                "print(json.dumps(sorted(Probe().message_json({'id': 1}))))" % str(SCRIPTS))
        real = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=60)
        self.assertEqual(real.returncode, 0, real.stderr)
        self.assertEqual(sorted(shown), json.loads(real.stdout))
        self.assertEqual((shown["is_bot"], shown["seat"]), (True, "CLAUDE"))
        human = self.w.add_message(topic, "a person")
        (person,) = [m for m in read() if m["id"] == human]
        self.assertEqual((person["is_bot"], person["seat"]), (False, None))

    # ---- the Grok Bot mirror -----------------------------------------------------------------
    def _mirror(self, message_id: int = 777, topic_enc: str = "OWNER.20AFC-9.20Plumber.20needs.20a.20key", **kw) -> object:
        return self.w.run("file", "--by", "CLAUDE", "--app", "AFC", "--question", "Plumber needs a key", "--option", "Approve",
                          "--option", "Decline", "--context", kw.get("context", "for GB-PLUMBER"), "--mirror-of",
                          REALM_LINK % (topic_enc, message_id), "--json")

    def test_mirror_files_once_posts_no_new_topic_and_links_back(self) -> None:
        self.w.set_zulip(topics_by_id={"777": "OWNER AFC-9 Plumber needs a key"})
        r = self._mirror()
        self.assertEqual(r.code, 0, r.err)
        got = r.json()
        self.assertEqual((got["state"], got["notified_msg_id"], got["linkback"]), ("notified", 777, "ok"))
        self.assertEqual(got["topic"], "OWNER AFC-9 Plumber needs a key")
        sends = self.w.calls("simple-chat")
        self.assertEqual([c["argv"][0] for c in sends], ["reply"], "no post:  the persona's topic is the notice")
        self.assertEqual(sends[0]["argv"], ["reply", "--as", "CLAUDE", "--session", "needsown", "--json", "--id", "777", "--", "-"])
        self.assertIn("Filed in Linear as AFC-300", sends[0]["stdin"])
        self.assertIn("https://linear.app/test-fleet/issue/AFC-300", sends[0]["stdin"])
        record = self._records()[0]
        self.assertIn("[needs-owner] notified msg 777", record)
        self.assertIn("topic: OWNER AFC-9 Plumber needs a key", record)
        (filed,) = [c for c in self.w.calls("simple-board") if c["argv"][0] == "file"]
        text = filed["files"]["--desc-file"]["text"]
        self.assertIn("Context: for GB-PLUMBER", text)
        self.assertIn("Mirror of: " + REALM_LINK % ("OWNER.20AFC-9.20Plumber.20needs.20a.20key", 777), text)
        # a second run files nothing, replies nowhere
        again = self._mirror()
        self.assertEqual(again.code, 0, again.err)
        self.assertTrue(again.json()["already_filed"])
        self.assertEqual(len(self.w.board_state()["issues"]), 1)
        self.assertEqual(len([c for c in self.w.calls("simple-chat")]), 1)
        listed = self.w.run("list", "--json").json()
        self.assertEqual((listed[0]["seat"], listed[0]["for_seat"], listed[0]["notified_msg_id"]), ("CLAUDE", "GB-PLUMBER", 777))
        self.assertEqual(listed[0]["topic"], "OWNER AFC-9 Plumber needs a key")

    def test_two_personas_asking_one_question_are_two_items(self) -> None:
        self.assertEqual(self._mirror(777).code, 0)
        self.assertEqual(self._mirror(888, context="for GB-DEPLOYER").code, 0)
        self.assertEqual(len(self.w.board_state()["issues"]), 2)

    def test_a_failed_linkback_is_reported_and_the_record_stands(self) -> None:
        self.w.set_zulip(post_mode="exit3")
        got = self._mirror().json()
        self.assertEqual((got["state"], got["linkback"]), ("notified", "failed"))
        self.assertIn("notified msg 777", self._records()[0])

class BoardWriteTests(unittest.TestCase):
    """The classifier:  what a failed board write means, and where it goes.

    The board CLI's exit codes are the contract, and a stub pins them without a
    child process:  every board call answers with the same result."""

    def setUp(self) -> None:
        self.home = tempfile.mkdtemp(prefix="nowner-keys-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.env = {"HOME": self.home}
        self.item = itemsmod.Item(
            identifier="AFC-300", id="id-300", url="https://linear.app/test-fleet/issue/AFC-300",
            title="Revive PR 761 or close it?", status="Todo", severity=None, app="AFC", created_at=None,
            labels=[itemsmod.LABEL], kind=itemsmod.KIND, reported_by="CLAUDE",
            block=blockmod.Block(uid="needs-owner:CLAUDE:abc12345", question=Q, options=["Yes", "No"],
                                 filed_by="CLAUDE"),
        )
        self.topic = T.topic_for("AFC-300", Q)

    def stub(self, result: Result) -> Stub:
        return Stub(result, environ=self.env)

    def notice(self, result: Result) -> tuple:
        runner = self.stub(result)
        return runner, notifymod.notify(runner, self.item)

    def notified_text(self) -> str:
        return records.notified_text(630000000, self.topic)

    def notified_key(self) -> str:
        return comment_keys.key_for("AFC-300", self.notified_text())

    def test_a_refused_held_record_stops_the_run_with_the_boards_own_reason(self) -> None:
        # `adopt --no-notify` has already filed the item when it writes the held mark, so a
        # terminal failure on that comment must not leave the item silently deliverable:
        # the run stops with the board's own exit code, and the reason reaches the operator.
        runner = self.stub(Result(4, "", "board: no Fleet credentials  credential_field=LINEAR_CLIENT_ID"))
        out, err = io.StringIO(), io.StringIO()
        with self.assertRaises(BoardFailed) as raised:
            verbs.finish(
                verbs.Streams(runner.env, out, err), runner, argparse.Namespace(json=False),
                "CLAUDE", self.item, [], new=True, rescan=False,
                words=("Filed", "Adopted"), extra={}, no_notify=True,
            )
        self.assertEqual(raised.exception.exit_code, 4)
        self.assertIn("the held record could not be written", str(raised.exception))
        self.assertIn("LINEAR_CLIENT_ID", str(raised.exception))

    def test_a_retryable_held_record_stays_pending_and_only_warns(self) -> None:
        # A retryable failure may be written by a later run, so the item stays pending and
        # the run carries on: only a terminal or an unknown outcome stops it.
        runner = self.stub(Result(3, "", "board: AFC-300 is In Progress, claimed by BF-DIRECTOR"))
        out, err = io.StringIO(), io.StringIO()
        code = verbs.finish(
            verbs.Streams(runner.env, out, err), runner, argparse.Namespace(json=False),
            "CLAUDE", self.item, [], new=True, rescan=False,
            words=("Filed", "Adopted"), extra={}, no_notify=True,
        )
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("the held record could not be written", err.getvalue())

    # ---- the table ------------------------------------------------------------------------
    def test_the_classifier_reads_the_board_codes(self) -> None:
        table = {2: notifymod.TERMINAL, 3: notifymod.RETRYABLE, 4: notifymod.TERMINAL,
                 5: notifymod.RETRYABLE, 6: notifymod.TERMINAL}
        for code, want in table.items():
            self.assertEqual(notifymod._classify(Result(code, "", "board: refused")), want, code)
        self.assertEqual(notifymod._classify(Result(0, "commented")), notifymod.OK)
        self.assertEqual(notifymod._classify(Result(124, "", "timed out", timed_out=True)), notifymod.UNKNOWN)
        self.assertEqual(notifymod._classify(Result(1, "", "board: Linear refused the comment: x")), notifymod.UNKNOWN)
        # Exit 7 is retired:  the board does not return it, and an unclassified code is unknown.
        self.assertEqual(notifymod._classify(Result(7, "", "board: refused")), notifymod.UNKNOWN)

    # ---- retryable ------------------------------------------------------------------------
    def test_exit_3_is_retryable_and_leaves_the_item_pending(self) -> None:
        runner, notice = self.notice(
            Result(3, "", "board: AFC-300 (AFC-300) is In Progress, claimed by BF-DIRECTOR.")
        )
        self.assertEqual((notice.state, notice.reason), ("notify_pending", "claim_held"))
        self.assertIn("reason=claim_held", notice.detail)
        self.assertIsNone(comment_keys.get(self.notified_key(), self.env), "the row is released")

    def test_exit_5_is_retryable_and_keeps_the_retry_timing(self) -> None:
        err = ("board: Linear rate limit: the Fleet app's budget resets at 3:42pm.  "
               "This request was not applied; rerun it after the reset.")
        runner, notice = self.notice(Result(5, "", err))
        self.assertEqual((notice.state, notice.reason), ("notify_pending", "rate_limited"))
        # The retry metadata is the reason word, which the pending record reads back on its own
        # line.  The detail beside it is allow-listed, so it carries the reason token and not the
        # board's sentence about the reset.
        self.assertIn("reason=rate_limited", notice.detail)
        self.assertNotIn("budget resets", notice.detail, "the board's prose is not copied")
        self.assertIsNone(comment_keys.get(self.notified_key(), self.env), "the row is released")

    def test_a_later_run_writes_the_record_after_a_retryable_failure(self) -> None:
        runner, notice = self.notice(Result(3, "", "board: claimed by BF-DIRECTOR"))
        again = self.stub(Result(0, "commented"))
        wrote = notifymod._write_record(again, self.item, self.notified_text())
        self.assertEqual(wrote.outcome, "ok")
        self.assertEqual(again.board_calls, ["comment"])

    # ---- terminal -------------------------------------------------------------------------
    def test_exit_4_names_the_missing_variable_and_no_value(self) -> None:
        # The board owns the credential, so it names the variables in a structured field
        # of its own, and the notifier reads that field and not the sentence around it.
        # The prose carries a hint and a backend name, and neither reaches the operator.
        err = ("board: no Fleet credentials: FLEET_LINEAR_CLIENT_ID and FLEET_LINEAR_CLIENT_SECRET are missing "
               "from Infisical Fleet-OPS /fleet/linear (`fleet-secrets get`), and LINEAR_CLIENT_ID and "
               "LINEAR_CLIENT_SECRET are not set.  Check `fleet-secrets doctor`.  Until then use "
               "BOARD_BACKEND=mac-collab.  credential_field=FLEET_LINEAR_CLIENT_ID,"
               "FLEET_LINEAR_CLIENT_SECRET,LINEAR_CLIENT_ID,LINEAR_CLIENT_SECRET")
        runner = self.stub(Result(4, "", err))
        with self.assertRaises(BoardFailed) as raised:
            notifymod.notify(runner, self.item)
        self.assertEqual(raised.exception.exit_code, 4)
        self.assertEqual(
            str(raised.exception),
            "board comment failed (exit 4):  credential variable: FLEET_LINEAR_CLIENT_ID, "
            "FLEET_LINEAR_CLIENT_SECRET, LINEAR_CLIENT_ID, LINEAR_CLIENT_SECRET",
        )
        self.assertNotIn("mac-collab", str(raised.exception), "a value never reaches the operator")

    def test_a_value_never_comes_out_of_the_missing_variables(self) -> None:
        # The board names the variable it owns in a structured field, and the value it
        # printed in the sentence around it is never read and never reaches the operator.
        err = ("board: no Fleet credentials: FLEET_LINEAR_CLIENT_ID is missing and "
               "LINEAR_CLIENT_SECRET=hunter2 is set.  credential_field=FLEET_LINEAR_CLIENT_ID")
        runner = self.stub(Result(4, "", err))
        with self.assertRaises(BoardFailed) as raised:
            notifymod.notify(runner, self.item)
        message = str(raised.exception)
        self.assertIn("FLEET_LINEAR_CLIENT_ID", message)
        self.assertNotIn("LINEAR_CLIENT_SECRET", message, "a name with a value is not a missing variable")
        self.assertNotIn("hunter2", message)

    def test_exits_2_and_6_are_terminal(self) -> None:
        for code in (2, 6):
            runner = self.stub(Result(code, "", "board: refused"))
            with self.assertRaises(BoardFailed) as raised:
                notifymod.notify(runner, self.item)
            self.assertIn("exit %d" % code, str(raised.exception))
            # errors.py passes the board's own code through for 4 to 6, and 1 for the rest.
            self.assertEqual(raised.exception.exit_code, code if code in (4, 5, 6) else 1)

    def test_exit_7_is_not_a_terminal_code_anymore(self) -> None:
        # Exit 7 was the writes-off switch.  The board retired it:  a write with no
        # bearer-verified seat exits 6.  A stale 7 must not read as a definite refusal.
        self.assertNotIn(7, notifymod.BOARD_TERMINAL_CODES)
        self.assertNotIn(7, notifymod.BOARD_RETRYABLE_CODES)

    def test_a_terminal_failure_is_not_tried_twice(self) -> None:
        # A terminal failure is decided before the board writes anything, and it is the same on a
        # second try:  the claim is not held, the rate limit is not in play and the write is not
        # going to be allowed.  A second board call buys nothing and only delays the operator.
        runner = self.stub(
            Result(4, "", "board: no Fleet credentials: FLEET_LINEAR_CLIENT_ID is missing")
        )
        with self.assertRaises(BoardFailed):
            notifymod.notify(runner, self.item)
        self.assertEqual(runner.board_calls, ["comment"], "a terminal failure is tried once")

    def test_a_retryable_failure_is_still_tried_twice(self) -> None:
        runner = self.stub(Result(3, "", "board: claimed by BF-DIRECTOR"))
        wrote = notifymod._write_record(runner, self.item, self.notified_text())
        self.assertEqual(runner.board_calls, ["comment", "comment"], "a retryable failure may be tried again")
        self.assertEqual(wrote.outcome, notifymod.RETRYABLE)

    def test_a_record_reports_a_terminal_failure_instead_of_raising(self) -> None:
        # `adopt --no-notify` has already filed the issue when it writes the held mark, so a
        # terminal board failure there must not abandon the rest of the flow and leave the mark
        # unwritten:  the fallback notifier would then announce an item that was held.  The
        # outcome is returned, and it keeps the board's own reason.
        runner = self.stub(
            Result(4, "", "board: no Fleet credentials  credential_field=FLEET_LINEAR_CLIENT_ID")
        )
        wrote = notifymod.record(runner, self.item, records.pending_text("held"))
        self.assertEqual(wrote.outcome, notifymod.TERMINAL)
        self.assertFalse(wrote, "a record that was not written is false, as it always was")
        self.assertIn("FLEET_LINEAR_CLIENT_ID", wrote.detail)

    def test_a_terminal_record_is_not_a_retryable_one(self) -> None:
        # Rule 51:  a retryable failure and a terminal one stay apart, so a caller can
        # tell "try again later" from "an operator has to act".
        terminal = self.stub(
            Result(4, "", "board: no Fleet credentials: FLEET_LINEAR_CLIENT_ID is missing")
        )
        wrote = notifymod.record(terminal, self.item, records.pending_text("held"))
        self.assertEqual(wrote.outcome, notifymod.TERMINAL)
        self.assertEqual(wrote.code, 4)
        retryable = self.stub(Result(3, "", "board: claimed by BF-DIRECTOR"))
        again = notifymod.record(retryable, self.item, records.pending_text("held"))
        self.assertEqual(again.outcome, notifymod.RETRYABLE)
        self.assertNotEqual(again.outcome, wrote.outcome)
        self.assertNotEqual(again.detail, wrote.detail)

    def test_a_rejected_credential_reaches_the_caller_of_a_record(self) -> None:
        # Rule 17:  the rejected-key path stops and reports the first-eight/last-four
        # preview.  A record that swallowed the failure lost the only diagnosis.
        err = ("board: Linear refused a freshly minted Fleet token; check the app in Linear's API settings  "
               "credential=lin_api_…wxyz")
        runner = self.stub(Result(6, "", err))
        wrote = notifymod.record(runner, self.item, records.pending_text("held"))
        self.assertEqual(wrote.outcome, notifymod.TERMINAL)
        self.assertEqual(wrote.code, 6)
        self.assertIn("credential rejected: lin_api_…wxyz", wrote.detail)
        self.assertNotIn("lin_api_", wrote.detail.replace("lin_api_…wxyz", ""))

    def test_a_real_shaped_linear_preview_reaches_the_operator(self) -> None:
        # A Linear credential starts with lin_api_, lin_oauth_ or lin_wh_, so the board's
        # preview carries an underscore.  A shape that does not allow one turns every
        # refusal into "no preview", and the operator is left with nothing to look at.
        for secret in ("lin_api_" + "z" * 30, "lin_oauth_" + "z" * 30, "lin_wh_" + "z" * 30):
            preview = secret[:8] + "…" + secret[-4:]  # the shape the board's own preview has
            err = "board: Linear refused the token  credential=%s" % preview
            runner = self.stub(Result(6, "", err))
            with self.assertRaises(BoardFailed) as raised:
                notifymod.notify(runner, self.item)
            self.assertIn("credential rejected: %s" % preview, str(raised.exception))

    def test_the_short_preview_of_a_short_credential_is_taken_too(self) -> None:
        # A credential under twenty-four characters shows one leading character and the
        # ellipsis alone, and that is a preview as well.
        err = "board: Linear refused the token  credential=l…"
        runner = self.stub(Result(6, "", err))
        with self.assertRaises(BoardFailed) as raised:
            notifymod.notify(runner, self.item)
        self.assertIn("credential rejected: l…", str(raised.exception))

    def test_the_missing_variable_comes_from_the_structured_field(self) -> None:
        # The board owns the credential, so the board hands back the name of the variable
        # as a field, and the field is read first.
        err = "board: no Fleet credentials  credential_field=LINEAR_CLIENT_ID,LINEAR_CLIENT_SECRET"
        runner = self.stub(Result(4, "", err))
        with self.assertRaises(BoardFailed) as raised:
            notifymod.notify(runner, self.item)
        self.assertEqual(
            str(raised.exception),
            "board comment failed (exit 4):  credential variable: LINEAR_CLIENT_ID, LINEAR_CLIENT_SECRET",
        )

    def test_the_prose_is_not_read_for_a_name_any_more(self) -> None:
        # The board owns the credential, so the board names the variable in its own field.
        # The prose is not read for a name: a name read out of prose is not a contract, and
        # a guess would send the operator after the wrong secret.
        err = ("board: no Fleet credentials: FLEET_LINEAR_CLIENT_ID and FLEET_LINEAR_CLIENT_SECRET are missing")
        runner = self.stub(Result(4, "", err))
        with self.assertRaises(BoardFailed) as raised:
            notifymod.notify(runner, self.item)
        message = str(raised.exception)
        self.assertNotIn("credential variable:", message)
        self.assertIn("the board named no credential variable", message)

    def test_no_placeholder_is_written_when_no_variable_is_named(self) -> None:
        # Rule 3:  a missing-secret failure identifies the variable.  When nothing names
        # one, the detail says so rather than pretending a variable was named.
        runner = self.stub(Result(4, "", "board: no Fleet credentials"))
        with self.assertRaises(BoardFailed) as raised:
            notifymod.notify(runner, self.item)
        message = str(raised.exception)
        self.assertNotIn("(not named)", message)
        self.assertNotIn("credential variable:", message)
        self.assertIn("the board named no credential variable", message)

    def test_the_boards_own_fields_reach_the_detail(self) -> None:
        # The board hands back the error name, the status and the request id as fields of
        # their own, and the detail quotes them back.
        err = ("board: whoami failed  error_type=GqlError http=401 request_id=req-abc12345  "
               "credential=lin_api_…wxyz")
        runner = self.stub(Result(6, "", err))
        with self.assertRaises(BoardFailed) as raised:
            notifymod.notify(runner, self.item)
        message = str(raised.exception)
        self.assertIn("credential rejected: lin_api_…wxyz", message)
        self.assertIn("error_type=GqlError", message)
        self.assertIn("http=401", message)
        self.assertIn("request_id=req-abc12345", message)

    def test_the_channel_comes_from_the_runner_and_not_from_the_process(self) -> None:
        # An ambient AGENT_SYNC_CHANNEL must not decide where the notice goes:  the
        # runner's own environment does.
        with mock.patch.dict(os.environ, {"AGENT_SYNC_CHANNEL": "ambient-channel"}):
            runner = self.stub(Result(0, "commented"))
            notifymod.notify(runner, self.item)
        channels = [args[args.index("--channel") + 1] for args in runner.sends if "--channel" in args]
        self.assertTrue(channels, "the notice was posted")
        for channel in channels:
            self.assertNotEqual(channel, "ambient-channel")

    # ---- unknown --------------------------------------------------------------------------
    def test_an_exit_1_graphql_refusal_is_unknown(self) -> None:
        runner, notice = self.notice(Result(1, "", "board: Linear refused the comment: entity not found"))
        self.assertEqual((notice.state, notice.reason), ("notify_pending", "outcome_unknown"))
        self.assertIn("reason=entity_not_found", notice.detail)
        self.assertEqual(comment_keys.get(self.notified_key(), self.env), comment_keys.UNKNOWN)

    def test_a_transport_failure_is_unknown(self) -> None:
        runner, notice = self.notice(
            Result(1, "", "board: no usable response from Linear (URLError); the last request was not confirmed.")
        )
        self.assertEqual((notice.state, notice.reason), ("notify_pending", "outcome_unknown"))
        self.assertIn("reason=no_usable_response", notice.detail)

    def test_an_exit_1_failure_with_no_marker_is_unknown(self) -> None:
        runner, notice = self.notice(Result(1, "", "board: fake board failure for comment"))
        self.assertEqual((notice.state, notice.reason), ("notify_pending", "outcome_unknown"))
        self.assertEqual(comment_keys.get(self.notified_key(), self.env), comment_keys.UNKNOWN)

    def test_a_timeout_is_unknown(self) -> None:
        runner, notice = self.notice(Result(124, "", "simple-board timed out after 240 seconds", timed_out=True))
        self.assertEqual((notice.state, notice.reason), ("notify_pending", "outcome_unknown"))
        self.assertEqual(comment_keys.get(self.notified_key(), self.env), comment_keys.UNKNOWN)

    def test_an_unknown_record_is_never_written_twice(self) -> None:
        runner, notice = self.notice(Result(1, "", "board: Linear refused the comment: boom"))
        again = self.stub(Result(0, "commented"))
        wrote = notifymod._write_record(again, self.item, self.notified_text())
        self.assertEqual(wrote.outcome, "unknown")
        self.assertEqual(again.board_calls, [], "the row says it may have landed:  nothing is written")

    # ---- the record ------------------------------------------------------------------------
    def test_the_store_lives_beside_the_notifier_state_and_not_inside_it(self) -> None:
        state = ledger.state_dir(self.env)
        store = comment_keys._path(self.env)
        self.assertFalse(store.startswith(state + os.sep), "the store is inside the notifier's own state")
        self.assertNotEqual(os.path.dirname(store), state, "the store is the notifier's own state")
        runner, notice = self.notice(Result(0, "commented"))
        self.assertFalse(os.path.exists(state), "a record does not create the notifier's state")
        self.assertTrue(os.path.exists(store), "the store is written")

    def test_the_reason_word_is_read_back_and_the_detail_is_not_part_of_it(self) -> None:
        runner, notice = self.notice(Result(3, "", "board: AFC-300 is In Progress, claimed by BF-DIRECTOR."))
        pending = [text for text in runner.written if "notify_pending" in text]
        self.assertEqual(len(pending), 2, "a definite failure is tried twice")
        self.assertEqual(records.parse_pending(pending[0]), "claim_held")
        self.assertIn("detail: ", pending[0])
        self.assertIn("reason=claim_held", pending[0])

    def test_the_detail_of_an_unknown_outcome_keeps_the_code_and_the_timing(self) -> None:
        runner, notice = self.notice(Result(1, "", "board: Linear answered HTTP 503 until LINEAR_BOARD_MAX_WAIT ran out"))
        self.assertEqual(notice.reason, "outcome_unknown")
        self.assertIn("http=503", notice.detail)
        self.assertIn("name=LINEAR_BOARD_MAX_WAIT", notice.detail)

    def test_the_detail_is_built_from_allow_listed_fields_and_never_the_boards_prose(self) -> None:
        # The board's prose is third-party text and it is persisted in a Linear comment, so it is
        # matched and tokenized here and never copied.  A third-party error body that carries a
        # credential must not reach the operator through this string.
        secret = "lin_api_" + "z" * 40
        err = ("board: Linear refused the comment: entity not found: the request carried "
               f"credential={secret} and a customer email someone@example.test")
        runner, notice = self.notice(Result(1, "", err))
        self.assertNotIn(secret, notice.detail, "a credential never reaches the operator")
        self.assertNotIn("someone@example.test", notice.detail, "a third party's PII never reaches it either")
        self.assertNotIn("Linear refused the comment", notice.detail, "the prose is not copied")
        self.assertIn("reason=entity_not_found", notice.detail)
        self.assertIn("board write failed (code=1)", notice.detail)

    def test_a_rejected_credential_carries_the_masks_preview_and_never_the_credential(self) -> None:
        # The board owns the credential, so it is the board that hands back the masked preview.
        err = ("board: Linear refused a freshly minted Fleet token; check the app in Linear's API settings  "
               "credential=abcdefgh…wxyz")
        runner = self.stub(Result(6, "", err))
        with self.assertRaises(BoardFailed) as raised:
            notifymod.notify(runner, self.item)
        message = str(raised.exception)
        self.assertEqual(raised.exception.exit_code, 6)
        self.assertIn("credential rejected: abcdefgh…wxyz", message)
        self.assertNotIn("abcdefgh", message.replace("abcdefgh…wxyz", ""))

    def test_a_credential_preview_that_is_not_shaped_like_one_is_ignored(self) -> None:
        err = "board: the token was refused  credential=this-is-the-whole-secret-value"
        runner = self.stub(Result(6, "", err))
        with self.assertRaises(BoardFailed) as raised:
            notifymod.notify(runner, self.item)
        message = str(raised.exception)
        self.assertNotIn("this-is-the-whole-secret-value", message, "a value that is not a preview is not taken")
        self.assertIn("credential rejected: (no preview)", message)


if __name__ == "__main__":
    unittest.main()
