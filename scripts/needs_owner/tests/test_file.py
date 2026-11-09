"""needs-owner file:  filing, the notice, idempotence and every refusal."""
from __future__ import annotations

import json
import unittest

from needs_owner import links
from needs_owner import text as T

from .world import REALM_LINK, World

Q = "Revive PR 761 or close it?"


class FileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.w = World(self)

    def tearDown(self) -> None:
        self.assertEqual(self.w.leftover_temp_files(), [], "a private temp file was left behind")

    # ---- the happy path ----------------------------------------------------------------------
    def test_files_announces_and_records(self) -> None:
        r = self.w.file(Q, "Revive it", "Close it", extra=("--low-stakes", "1", "--recommend", "1", "--context", "PR 761 is stale"))
        self.assertEqual(r.code, 0, r.err)
        got = r.json()
        self.assertEqual(got["identifier"], "AFC-300")
        self.assertEqual(got["state"], "notified")
        self.assertEqual(got["topic"], "OWNER AFC-300 Revive PR 761 or close it?")
        self.assertIsInstance(got["notified_msg_id"], int)
        self.assertTrue(got["uid"].startswith("needs-owner:CLAUDE:"))
        # the board filing:  Kind, label, uid, seat, severity, title, and a private description file
        (filed,) = [c for c in self.w.calls("simple-board") if c["argv"][0] == "file"]
        argv = filed["argv"]
        for pair in (["--kind", "agent-report"], ["--label", "needs-owner"], ["--by", "CLAUDE"], ["--severity", "P2"],
                     ["--uid", got["uid"]], ["--title", "OWNER DECISION:  " + Q], ["--app", "AFC"]):
            self.assertIn(pair[0], argv)
            self.assertEqual(argv[argv.index(pair[0]) + 1], pair[1])
        self.assertEqual(filed["files"]["--desc-file"]["mode"], "0600")
        description = filed["files"]["--desc-file"]["text"]
        self.assertIn("```needs-owner", description)
        self.assertIn("Question: " + Q, description)
        self.assertIn("Option 1 (low-stakes): Revive it", description)
        self.assertIn("Recommendation: option 1", description)
        self.assertIn("Filed by: CLAUDE", description)
        self.assertNotIn("default", description.lower())
        # the notice:  the filer's bot, the fixed session label, the channel the notifier resolved
        # from its own environment, the owner mention, the body on stdin
        (post,) = self.w.calls("simple-chat")
        self.assertEqual(post["argv"], ["post", "--as", "CLAUDE", "--session", "needsown", "--json",
                                        "--channel", links.CHANNEL, "--topic", got["topic"],
                                        "--to-owner", "--", "-"])
        body = post["stdin"]
        self.assertIn("1️⃣ Option 1:", body)
        self.assertIn("(Tap to answer)", body)
        self.assertIn("Option 2:", body)
        self.assertIn("Recommendation:  option 1.", body)
        self.assertIn("https://linear.app/test-fleet/issue/AFC-300", body)
        self.assertIn("Answer here or as a comment on the Linear issue.", body)
        self.assertIn("`%s`" % got["uid"], body)
        # the record
        notice = self.w.comments("AFC-300")[-1]
        self.assertIn("[needs-owner] notified msg %d" % got["notified_msg_id"], notice)
        self.assertIn("topic: " + got["topic"], notice)

    def test_urgent_files_p1_and_marks_the_post(self) -> None:
        r = self.w.file(Q, "Revive it", "Close it", extra=("--urgent",))
        self.assertEqual(r.code, 0, r.err)
        (filed,) = [c for c in self.w.calls("simple-board") if c["argv"][0] == "file"]
        self.assertEqual(filed["argv"][filed["argv"].index("--severity") + 1], "P1")
        self.assertIn("**Urgent decision needed**", self.w.calls("simple-chat")[0]["stdin"])

    def test_emoji_follow_the_option_position(self) -> None:
        r = self.w.file("Which one?", "A", "B", "C", extra=("--low-stakes", "3"))
        self.assertEqual(r.code, 0, r.err)
        body = self.w.calls("simple-chat")[0]["stdin"]
        self.assertIn("3️⃣ Option 3:", body)
        self.assertNotIn("1️⃣", body)
        self.assertNotIn("2️⃣", body)

    def test_text_in_the_post_is_made_inert(self) -> None:
        r = self.w.file("@**all** [fix](https://x.test/p) now?", "`rm -rf ~`", "/poll no")
        self.assertEqual(r.code, 0, r.err)
        body = self.w.calls("simple-chat")[0]["stdin"]
        self.assertNotIn("@**all**", body)
        self.assertNotIn("](https", body)
        self.assertNotIn("/poll", body)
        self.assertNotIn("rm -rf ~`\n", body.replace("`rm -rf ~`", ""))
        topic = self.w.calls("simple-chat")[0]["argv"][self.w.calls("simple-chat")[0]["argv"].index("--topic") + 1]
        self.assertNotIn("@", topic)
        self.assertLessEqual(len(topic), 60)

    def test_a_backtick_in_the_question_cannot_close_the_block(self) -> None:
        r = self.w.file("Use ```code``` now?", "yes", "no")
        self.assertEqual(r.code, 0, r.err)
        (filed,) = [c for c in self.w.calls("simple-board") if c["argv"][0] == "file"]
        description = filed["files"]["--desc-file"]["text"]
        self.assertEqual(description.count("```"), 2)
        item = json.loads(self.w.run("status", "AFC-300", "--json").out)
        self.assertEqual(item["question"], "Use '''code''' now?")
        self.assertTrue(item["block_ok"])

    # ---- identity ----------------------------------------------------------------------------
    def test_uid_is_stable_across_nbsp_and_case(self) -> None:
        a = T.make_uid("CLAUDE", "Revive PR 761 or close it?", ["Revive it", "Close it"])
        b = T.make_uid("CLAUDE", "revive PR  761\tOR close it?", ["REVIVE IT", " close it "])
        self.assertEqual(a, b)
        self.assertNotEqual(a, T.make_uid("CLAUDE", Q, ["Close it", "Revive it"]))  # option order matters
        self.assertNotEqual(a, T.make_uid("AFC-9", Q, ["Revive it", "Close it"]))  # the scope is part of the uid
        self.assertRegex(a, r"^needs-owner:CLAUDE:[0-9a-f]{8}$")

    def test_the_same_request_twice_files_once_and_posts_once(self) -> None:
        first = self.w.file(Q, "Revive it", "Close it").json()
        second = self.w.file("revive pr 761 OR close it?", "REVIVE IT", "close it")
        self.assertEqual(second.code, 0, second.err)
        again = second.json()
        self.assertEqual(again["identifier"], first["identifier"])
        self.assertTrue(again["already_filed"])
        self.assertTrue(again["already_notified"])
        self.assertEqual(len(self.w.board_state()["issues"]), 1)
        self.assertEqual(len(self.w.zulip()["messages"]), 1)
        self.assertEqual(len([c for c in self.w.calls("simple-board") if c["argv"][0] == "file"]), 0 + 1)

    # ---- changes to an item that exists ------------------------------------------------------
    def test_changed_options_after_notice_are_refused_unless_again(self) -> None:
        first = self.w.file(Q, "Revive it", "Close it").json()
        r = self.w.file(Q, "Revive it", "Park it")
        self.assertEqual(r.code, 2)
        self.assertIn("AFC-300", r.err)
        self.assertIn("--again", r.err)
        self.assertEqual(len(self.w.board_state()["issues"]), 1)
        self.assertEqual(len(self.w.zulip()["messages"]), 1)
        again = self.w.file(Q, "Revive it", "Park it", extra=("--again",))
        self.assertEqual(again.code, 0, again.err)
        second = again.json()
        self.assertEqual(second["identifier"], "AFC-301")
        self.assertNotEqual(second["uid"], first["uid"])
        description = self.w.calls("simple-board")[-1]  # the comment record
        filed = [c for c in self.w.calls("simple-board") if c["argv"][0] == "file"][-1]["files"]["--desc-file"]["text"]
        self.assertIn("Version: 2", filed)
        self.assertIn("Again of: AFC-300 msg %d" % first["notified_msg_id"], filed)
        body = self.w.calls("simple-chat")[-1]["stdin"]
        self.assertIn("Asking again.  The earlier question was AFC-300 (message %d)." % first["notified_msg_id"], body)
        # the earlier item is not touched
        self.assertEqual(self.w.issue("AFC-300")["state"]["name"], "Todo")
        self.assertIsNotNone(description)

    def test_again_run_twice_is_one_item(self) -> None:
        self.w.file(Q, "Revive it", "Close it")
        self.w.file(Q, "Revive it", "Park it", extra=("--again",))
        third = self.w.file(Q, "Revive it", "Park it", extra=("--again",))
        self.assertEqual(third.code, 0, third.err)
        self.assertEqual(len(self.w.board_state()["issues"]), 2)
        self.assertEqual(len(self.w.zulip()["messages"]), 2)

    def test_changed_marks_with_the_same_options_are_refused(self) -> None:
        self.w.file(Q, "Revive it", "Close it", extra=("--low-stakes", "1"))
        for change in (("--low-stakes", "2"), (), ("--recommend", "2"), ("--context", "new background"), ("--urgent",)):
            r = self.w.file(Q, "Revive it", "Close it", extra=change)
            self.assertEqual(r.code, 2, (change, r.out, r.err))
            self.assertIn("--again", r.err)
        self.assertEqual(len(self.w.board_state()["issues"]), 1)

    def test_a_closed_item_needs_again(self) -> None:
        self.w.file(Q, "Revive it", "Close it")
        self.w.close("AFC-300")
        r = self.w.file(Q, "Revive it", "Close it")
        self.assertEqual(r.code, 2)
        self.assertIn("closed", r.err)
        self.assertIn("--again", r.err)
        again = self.w.file(Q, "Revive it", "Close it", extra=("--again",))
        self.assertEqual(again.code, 0, again.err)
        self.assertEqual(again.json()["identifier"], "AFC-301")
        self.assertEqual(len(self.w.zulip()["messages"]), 2)

    def test_a_changed_question_is_a_new_item(self) -> None:
        self.w.file(Q, "Revive it", "Close it")
        r = self.w.file("Revive PR 762 or close it?", "Revive it", "Close it")
        self.assertEqual(r.code, 0, r.err)
        self.assertEqual(r.json()["identifier"], "AFC-301")

    # ---- --parent ----------------------------------------------------------------------------
    def test_parent_must_be_claimed_by_the_filer(self) -> None:
        other = self.w.seed_issue("Someone else's work", labels=("Kind/agent-report", "claimed:CODEX"))
        r = self.w.file(Q, "Revive it", "Close it", extra=("--parent", other))
        self.assertEqual(r.code, 2)
        self.assertIn("not claimed by CLAUDE", r.err)
        self.assertEqual([c for c in self.w.calls("simple-board") if c["argv"][0] == "file"], [])
        self.assertEqual(self.w.calls("simple-chat"), [])

    def test_parent_scopes_the_uid_and_gets_one_comment(self) -> None:
        mine = self.w.seed_issue("My work", labels=("Kind/agent-report", "claimed:CLAUDE"))
        r = self.w.file(Q, "Revive it", "Close it", extra=("--parent", mine))
        self.assertEqual(r.code, 0, r.err)
        got = r.json()
        self.assertTrue(got["uid"].startswith("needs-owner:%s:" % mine))
        self.assertIn("waiting on owner:  AFC-301", self.w.comments(mine)[-1])
        (filed,) = [c for c in self.w.calls("simple-board") if c["argv"][0] == "file"]
        self.assertIn("Parent: " + mine, filed["files"]["--desc-file"]["text"])
        self.w.file(Q, "Revive it", "Close it", extra=("--parent", mine))  # a rerun adds no second comment
        self.assertEqual(len([c for c in self.w.comments(mine) if "waiting on owner" in c]), 1)

    # ---- what is refused before anything is sent ---------------------------------------------
    def _assert_nothing_sent(self, r) -> None:
        self.assertEqual(r.code, 2, (r.out, r.err))
        self.assertEqual([c for c in self.w.calls("simple-board") if c["argv"][0] in ("file", "comment", "label")], [])
        self.assertEqual(self.w.calls("simple-chat"), [])

    def test_a_secret_is_refused_and_never_echoed(self) -> None:
        key = "lin_api_" + "A1b2C3d4E5" * 3
        for where in ("question", "option", "context"):
            q, opts, extra = Q, ("Yes", "No"), ()
            if where == "question":
                q = "Rotate " + key + "?"
            elif where == "option":
                opts = ("Yes", key)
            else:
                extra = ("--context", "token is " + key)
            r = self.w.file(q, *opts, extra=extra)
            self._assert_nothing_sent(r)
            self.assertNotIn(key, r.err + r.out)
            self.assertIn("secret", r.err)

    def test_personal_data_is_refused(self) -> None:
        for text, kind in (("Mail jay@example.com about it?", "email"), ("Call 512-555-0123 about it?", "phone"),
                           ("Is 4815162342 the id?", "six or more digits"), ("Pay $400 for it?", "dollar")):
            r = self.w.file(text, "Yes", "No")
            self._assert_nothing_sent(r)
            self.assertIn(kind, r.err)

    def test_low_stakes_never_goes_with_urgent(self) -> None:
        r = self.w.file(Q, "Revive it", "Close it", extra=("--low-stakes", "1", "--urgent"))
        self._assert_nothing_sent(r)
        self.assertIn("--urgent", r.err)

    def test_bad_requests_are_refused(self) -> None:
        cases = {
            "no options": self.w.run("file", "--by", "CLAUDE", "--app", "AFC", "--question", Q),
            "six options": self.w.file(Q, "1", "2", "3", "4", "5", "6"),
            "same option twice": self.w.file(Q, "Yes", " yes "),
            "empty question": self.w.file("  ", "Yes", "No"),
            "low-stakes out of range": self.w.file(Q, "Yes", "No", extra=("--low-stakes", "3")),
            "recommend out of range": self.w.file(Q, "Yes", "No", extra=("--recommend", "0")),
            "question too long": self.w.file("x" * 201, "Yes", "No"),
        }
        for name, r in cases.items():
            self.assertEqual(r.code, 2, (name, r.out, r.err))
        self.assertEqual(self.w.calls("simple-chat"), [])
        self.assertEqual(self.w.board_state()["issues"], {})

    def test_dry_run_changes_nothing(self) -> None:
        r = self.w.file(Q, "Revive it", "Close it", extra=("--dry-run",))
        self.assertEqual(r.code, 0, r.err)
        self.assertTrue(r.json()["dry_run"])
        self.assertEqual(self.w.board_state()["issues"], {})
        self.assertEqual(self.w.calls("simple-chat"), [])

    # ---- argv --------------------------------------------------------------------------------
    def test_argv_never_holds_the_description(self) -> None:
        """The question is the issue title and the head of the topic, so it rides on `--title` and `--topic`;  nothing else the
        filer wrote touches argv."""
        question = "Should zebra quokka migrate the ledger?"
        r = self.w.file(question, "Migrate wombat", "Leave wombat", extra=("--context", "kangaroo background", "--recommend", "2"))
        self.assertEqual(r.code, 0, r.err)
        for call in self.w.calls():
            argv = list(call["argv"])
            if "--title" in argv:  # the question is the issue title
                i = argv.index("--title")
                self.assertEqual(argv[i + 1], "OWNER DECISION:  " + question)
                del argv[i:i + 2]
            if "--topic" in argv:  # and the first 40 characters of it are the topic's subject
                i = argv.index("--topic")
                self.assertEqual(argv[i + 1], "OWNER AFC-300 " + question)
                del argv[i:i + 2]
            line = " ".join(argv)
            for word in ("zebra", "quokka", "wombat", "kangaroo", "ledger", "Recommendation"):
                self.assertNotIn(word, line, call["argv"])
        (filed,) = [c for c in self.w.calls("simple-board") if c["argv"][0] == "file"]
        text = filed["files"]["--desc-file"]["text"]
        self.assertIn("kangaroo", text)
        self.assertIn("wombat", text)
        self.assertIn("zebra", self.w.calls("simple-chat")[0]["stdin"])
        for comment in [c for c in self.w.calls("simple-board") if c["argv"][0] == "comment"]:
            self.assertIn("--text-file", comment["argv"])

    # ---- no cap ------------------------------------------------------------------------------
    def test_seven_items_from_one_seat_all_land(self) -> None:
        warnings = []
        for n in range(1, 8):
            r = self.w.file("Decision number %d about the plan?" % n, "Yes", "No")
            self.assertEqual(r.code, 0, (n, r.err))
            warnings.append("more than 5" in r.err)
            self.assertEqual(r.json()["over_five"], n > 5)
        self.assertEqual(warnings, [False] * 5 + [True, True])
        self.assertEqual(len(self.w.board_state()["issues"]), 7)
        self.assertEqual(len(self.w.zulip()["messages"]), 7)
        rows = self.w.run("list", "--json").json()
        self.assertEqual(len(rows), 7)
        self.assertTrue(all(row["over_five"] for row in rows))

    def test_over_five_counts_only_the_seats_own(self) -> None:
        for n in range(1, 7):
            self.w.file("Claude decision %d?" % n, "Yes", "No")
        self.w.run("file", "--by", "CODEX", "--app", "AFC", "--question", "Codex decision?", "--option", "Yes", "--option", "No",
                   env=self.w.env(AGENT_SEAT="CODEX"))
        rows = {row["seat"]: row["over_five"] for row in self.w.run("list", "--json").json()}
        self.assertTrue(rows["CLAUDE"])
        self.assertFalse(rows["CODEX"])

    # ---- identity ----------------------------------------------------------------------------
    def test_a_launched_seat_reaches_every_child(self) -> None:
        env = self.w.env(AGENT_SEAT="BF-PLUMBER", AGENT_LAUNCHER="botfleet")
        r = self.w.run("file", "--by", "BF-PLUMBER", "--app", "AFC", "--question", Q, "--option", "Yes", "--option", "No", "--json", env=env)
        self.assertEqual(r.code, 0, r.err)
        for call in self.w.calls():
            self.assertEqual(call["seat"], "BF-PLUMBER", call["argv"])
            self.assertIsNone(call["env_seat"], "no child gets an identity variable")
            self.assertIsNone(call["launch_seat"])
            self.assertEqual(call["launcher"], "botfleet")
        filed = [c for c in self.w.calls("simple-board") if c["argv"][0] == "file"][0]
        self.assertEqual(filed["argv"][filed["argv"].index("--by") + 1], "BF-PLUMBER")
        post = self.w.calls("simple-chat")[0]
        self.assertEqual(post["argv"][post["argv"].index("--as") + 1], "BF-PLUMBER")
        self.assertIn("Filed by: BF-PLUMBER", filed["files"]["--desc-file"]["text"])

    def test_seat_refusals(self) -> None:
        base = ["file", "--app", "AFC", "--question", Q, "--option", "Yes", "--option", "No"]
        cases = [
            ("launcher without a seat", self.w.env(AGENT_SEAT=None, AGENT_LAUNCHER="botfleet"), "BF-PLUMBER"),
            ("launch seat against AGENT_SEAT", self.w.env(AGENT_LAUNCHER="botfleet", AGENT_LAUNCH_SEAT="BF-PLUMBER", AGENT_SEAT="CLAUDE"), "BF-PLUMBER"),
            ("--by is another seat", self.w.env(), "BF-PLUMBER"),
            ("no seat at all", self.w.env(AGENT_SEAT=None), "CLAUDE"),
            ("--by against a launch seat", self.w.env(AGENT_SEAT=None, AGENT_LAUNCHER="botfleet", AGENT_LAUNCH_SEAT="BF-PLUMBER"), "CLAUDE"),
        ]
        for name, env, by in cases:
            r = self.w.run(*base, "--by", by, env=env)
            self.assertEqual(r.code, 2, (name, r.out, r.err))
        self.assertEqual(self.w.calls(), [])

    # ---- what Linear does to the fence -------------------------------------------------------
    def test_a_bare_fence_still_reads(self) -> None:
        """Linear may drop the info string of a code block it does not know:  the block is found by its content."""
        env = self.w.env(FAKE_BOARD_STRIP_FENCE_INFO="1")
        first = self.w.file(Q, "Revive it", "Close it", env=env)
        self.assertEqual(first.code, 0, first.err)
        self.assertNotIn("```needs-owner", self.w.issue("AFC-300")["description"])
        got = self.w.run("status", "AFC-300", "--json").json()
        self.assertEqual((got["state"], got["block_ok"], got["seat_verified"]), ("notified", True, True))
        again = self.w.file(Q, "Revive it", "Close it", env=env)
        self.assertEqual(again.code, 0, again.err)
        self.assertTrue(again.json()["already_filed"])
        self.assertEqual((len(self.w.board_state()["issues"]), len(self.w.zulip()["messages"])), (1, 1))
        changed = self.w.file(Q, "Revive it", "Park it", env=env)
        self.assertEqual(changed.code, 2)
        self.assertIn("AFC-300", changed.err)

    def test_a_description_that_loses_the_block_is_repaired_with_a_comment(self) -> None:
        env = self.w.env(FAKE_BOARD_DROP_FENCE="1")
        r = self.w.file(Q, "Revive it", "Close it", env=env)
        self.assertEqual(r.code, 0, r.err)
        self.assertIn("posting the block as a comment", r.err)
        self.assertEqual(r.json()["state"], "notified")
        got = self.w.run("status", "AFC-300", "--json").json()
        self.assertEqual((got["block_ok"], got["seat_verified"], got["state"]), (True, True, "notified"))
        again = self.w.file(Q, "Revive it", "Close it", env=env)
        self.assertEqual(again.code, 0, again.err)
        self.assertTrue(again.json()["already_filed"])
        self.assertEqual((len(self.w.board_state()["issues"]), len(self.w.zulip()["messages"])), (1, 1))
        self.assertEqual(len([c for c in self.w.comments("AFC-300") if "[needs-owner] block" in c]), 1)

    def test_a_filed_item_whose_repair_failed_is_repaired_by_the_rerun(self) -> None:
        r = self.w.file(Q, "Revive it", "Close it", env=self.w.env(FAKE_BOARD_DROP_FENCE="1", FAKE_BOARD_FAIL="comment"))
        self.assertEqual(r.code, 1)
        self.assertEqual(self.w.zulip()["messages"], [], "nothing is announced without a readable block")
        self.assertEqual(self.w.run("list", "--json").json()[0]["state"], "unadopted")
        again = self.w.file(Q, "Revive it", "Close it", env=self.w.env(FAKE_BOARD_DROP_FENCE="1"))
        self.assertEqual(again.code, 0, again.err)
        self.assertEqual(again.json()["state"], "notified")
        self.assertEqual(len(self.w.board_state()["issues"]), 1)

    # ---- failures ----------------------------------------------------------------------------
    def test_a_write_with_no_bearer_passes_the_exit_code_through(self) -> None:
        r = self.w.file(Q, "Yes", "No", env=self.w.env(FAKE_BOARD_WRITES_OFF="1"))
        self.assertEqual(r.code, 6, r.err)
        self.assertEqual(self.w.calls("simple-chat"), [])

    def test_no_board_is_an_error(self) -> None:
        self.w.remove_tool("simple-board")
        r = self.w.file(Q, "Yes", "No")
        self.assertEqual(r.code, 2, "with no board there is no bearer to prove a seat:  it refuses")
        self.assertIn("board", r.err)

    def test_mirror_link_shapes(self) -> None:
        bad = {
            "other realm": "https://evil.example/#narrow/channel/12-work-in-progress/topic/OWNER.20x/near/5",
            "no message": "https://simplewithus.zulipchat.com/#narrow/channel/12-work-in-progress/topic/OWNER.20x",
            "not an OWNER topic": REALM_LINK % ("CONSULT.20x", 5),
            "other channel": "https://simplewithus.zulipchat.com/#narrow/channel/9-general/topic/OWNER.20x/near/5",
            "http": "http://simplewithus.zulipchat.com/#narrow/channel/12-work-in-progress/topic/OWNER.20x/near/5",
        }
        for name, link in bad.items():
            r = self.w.file(Q, "Yes", "No", extra=("--mirror-of", link))
            self.assertEqual(r.code, 2, (name, r.err))
        self.assertEqual(self.w.board_state()["issues"], {})


if __name__ == "__main__":
    unittest.main()
