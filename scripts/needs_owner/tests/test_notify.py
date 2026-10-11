"""The notice:  pending, maybe-sent, reruns and the Grok Bot mirror."""
from __future__ import annotations

import unittest

from .world import REALM_LINK, World

Q = "Revive PR 761 or close it?"


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
        self.w.remove_tool("agent-sync")
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
        posts = [c for c in self.w.calls("agent-sync") if c["argv"][0] == "post"]
        self.assertEqual(len(posts), 2, "one post per item:  nothing was retried")
        self.assertEqual(len(self.w.zulip()["messages"]), 2)
        reads = [c for c in self.w.calls("agent-sync") if c["argv"][0] == "read"]
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
        sends = self.w.calls("agent-sync")
        self.assertEqual([c["argv"][0] for c in sends], ["reply"], "no post:  the persona's topic is the notice")
        self.assertEqual(sends[0]["argv"], ["reply", "--as", "CLAUDE", "--session", "needsown", "--json", "--id", "777", "--", "-"])
        self.assertIn("Filed in Linear as AFC-300", sends[0]["stdin"])
        self.assertIn("https://linear.app/simple-with-us/issue/AFC-300", sends[0]["stdin"])
        record = self._records()[0]
        self.assertIn("[needs-owner] notified msg 777", record)
        self.assertIn("topic: OWNER AFC-9 Plumber needs a key", record)
        (filed,) = [c for c in self.w.calls("board") if c["argv"][0] == "file"]
        text = filed["files"]["--desc-file"]["text"]
        self.assertIn("Context: for GB-PLUMBER", text)
        self.assertIn("Mirror of: " + REALM_LINK % ("OWNER.20AFC-9.20Plumber.20needs.20a.20key", 777), text)
        # a second run files nothing, replies nowhere
        again = self._mirror()
        self.assertEqual(again.code, 0, again.err)
        self.assertTrue(again.json()["already_filed"])
        self.assertEqual(len(self.w.board_state()["issues"]), 1)
        self.assertEqual(len([c for c in self.w.calls("agent-sync")]), 1)
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
