"""The notice:  pending, maybe-sent, reruns and the Grok Bot mirror."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest

from needs_owner import notify as notifymod
from needs_owner import text as T
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
    return ("@_**Jay|7** [said](https://simplewithus.zulipchat.com/#narrow/channel/12-agent-sync/topic/x/near/%d):\n"
            "```quote\n%s\n```\n\nyes please" % (message_id, body.rstrip("\n")))


class Reader:
    """A runner that answers `agent-sync read` with fixed rows (the real `message_json` keys)."""

    def __init__(self, seat: str, rows: list) -> None:
        self.seat = seat
        self.rows = rows
        self.calls: list = []

    def agent_sync(self, *args: str, stdin=None) -> Result:
        self.calls.append(args)
        return Result(0, "\n".join(json.dumps(r) if isinstance(r, dict) else r for r in self.rows) + "\n")


def row(message_id, seat, content, *, is_bot=True) -> dict:
    return {"id": message_id, "channel": "agent-sync", "topic": "OWNER AFC-1 x", "content": content, "is_bot": is_bot, "seat": seat}


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
        r = self.w.file(Q, "Yes", "No", env=self.w.env(FAKE_BOARD_FAIL="comment"))
        self.assertEqual(r.code, 0, r.err)
        self.assertIn("did not", r.err)
        self.assertFalse(r.json()["recorded"])
        self.assertEqual(len(self.w.zulip()["messages"]), 1)
        again = self.w.file(Q, "Yes", "No")
        self.assertEqual(again.code, 0, again.err)
        self.assertEqual(again.json()["state"], "notified")
        self.assertTrue(again.json()["recorded"])
        self.assertEqual(len(self.w.zulip()["messages"]), 1)
        self.assertEqual(len([c for c in self._records() if "notified msg" in c]), 1)

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
        """The post landed, both attempts to record it failed, and the topic then fills with look-alikes.  The
        rerun records the bot's own first post and posts nothing."""
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
        self.assertEqual((got["state"], got["notified_msg_id"], got["recorded"]), ("notified", 630000000, True))
        records = [c for c in self._records() if "notified msg" in c]
        self.assertEqual(len(records), 1)
        self.assertIn("notified msg 630000000", records[0])
        for stray in impostors:
            self.assertNotIn(str(stray), records[0])
        self.assertEqual(len(self._posts()), 1, "one post across both runs")
        listed = self.w.run("list", "--json").json()
        self.assertEqual(listed[0]["notified_msg_id"], 630000000)

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
            ran = subprocess.run([str(self.w.bin / "simple-chat"), "read", "--as", "CLAUDE", "--json", "--channel", "agent-sync",
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
        self.assertIn("https://linear.app/simple-with-us/issue/AFC-300", sends[0]["stdin"])
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


if __name__ == "__main__":
    unittest.main()
