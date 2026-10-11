"""needs-owner adopt:  the day-0 items (live step L0b) become needs-owner items without being re-filed."""
from __future__ import annotations

import unittest

from .world import World


class AdoptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.w = World(self)
        self.args = ("--by", "CLAUDE", "--option", "Revive it", "--option", "Close it", "--low-stakes", "1", "--recommend", "2", "--json")

    def tearDown(self) -> None:
        self.assertEqual(self.w.leftover_temp_files(), [])

    def test_round_trip(self) -> None:
        ident = self.w.seed_issue("Tonight: revive PR 761 or close it?")
        before = self.w.run("list", "--json").json()
        self.assertEqual([(r["identifier"], r["state"], r["seat"], r["options"]) for r in before], [(ident, "unadopted", None, [])])
        self.assertEqual(before[0]["question"], "Tonight: revive PR 761 or close it?")

        r = self.w.run("adopt", ident, *self.args)
        self.assertEqual(r.code, 0, r.err)
        got = r.json()
        self.assertTrue(got["adopted"])
        self.assertEqual((got["state"], got["topic"]), ("notified", "OWNER %s Tonight: revive PR 761 or close it?" % ident))
        self.assertTrue(got["uid"].startswith("needs-owner:CLAUDE:"))

        # no re-filing:  the board saw a label check, a comment with the block, and the notice record
        verbs = [c["argv"][0] for c in self.w.calls("board")]
        self.assertNotIn("file", verbs)
        block_comment = [c for c in self.w.comments(ident) if "[needs-owner] block" in c][0]
        self.assertIn("```needs-owner", block_comment)
        self.assertIn("Option 1 (low-stakes): Revive it", block_comment)
        self.assertIn("Recommendation: option 2", block_comment)

        after = self.w.run("status", ident, "--json").json()
        self.assertEqual((after["state"], after["seat"], after["seat_verified"]), ("notified", "CLAUDE", True))
        self.assertEqual((after["options"], after["low_stakes"], after["recommend"]), (["Revive it", "Close it"], [1], 2))
        self.assertEqual(after["uid"], got["uid"])
        (post,) = [c for c in self.w.calls("agent-sync") if c["argv"][0] == "post"]
        self.assertIn("1️⃣ Option 1:", post["stdin"])

    def test_a_second_adopt_changes_nothing(self) -> None:
        ident = self.w.seed_issue("Tonight: revive PR 761 or close it?")
        self.w.run("adopt", ident, *self.args)
        comments = len(self.w.comments(ident))
        r = self.w.run("adopt", ident, *self.args)
        self.assertEqual(r.code, 0, r.err)
        got = r.json()
        self.assertTrue(got["already_adopted"])
        self.assertTrue(got["already_notified"])
        self.assertEqual(len(self.w.comments(ident)), comments)
        self.assertEqual(len(self.w.zulip()["messages"]), 1)

    def test_a_dry_run_of_an_item_already_adopted_posts_and_changes_nothing(self) -> None:
        """--dry-run must not announce:  an adopted item whose notice is still to come used to fall through to notify."""
        ident = self.w.seed_issue("Tonight: revive PR 761 or close it?")
        self.assertEqual(self.w.run("adopt", ident, *self.args, "--no-notify").code, 0)
        def board_writes() -> int:
            return len([c for c in self.w.calls("board") if c["argv"][0] in ("comment", "label", "file", "status")])

        comments, writes = len(self.w.comments(ident)), board_writes()
        plain = tuple(a for a in self.args if a != "--json")
        r = self.w.run("adopt", ident, *plain, "--dry-run")
        self.assertEqual(r.code, 0, r.err)
        self.assertIn("dry run", r.out)
        self.assertIn("already adopted", r.out)
        self.assertEqual(self.w.zulip()["messages"], [])
        self.assertEqual([c for c in self.w.calls("agent-sync") if c["argv"][0] in ("post", "reply")], [])
        self.assertEqual(len(self.w.comments(ident)), comments)
        self.assertEqual(board_writes(), writes)

    def test_adopt_adds_a_missing_label(self) -> None:
        ident = self.w.seed_issue("Tonight: logo?", labels=("Kind/agent-report",))
        self.assertEqual(self.w.run("list", "--json").json(), [])  # no label, not an item
        r = self.w.run("adopt", ident, *self.args)
        self.assertEqual(r.code, 0, r.err)
        labels = [c["argv"] for c in self.w.calls("board") if c["argv"][0] == "label"]
        self.assertEqual(labels, [["label", "add", ident, "needs-owner", "--by", "CLAUDE"]])
        self.assertEqual(len(self.w.run("list", "--json").json()), 1)

    def test_no_notify_adopts_and_a_later_adopt_announces(self) -> None:
        ident = self.w.seed_issue("Tonight: logo?")
        r = self.w.run("adopt", ident, *self.args, "--no-notify")
        self.assertEqual(r.code, 0, r.err)
        self.assertEqual(r.json()["state"], "notify_pending")
        self.assertEqual(self.w.zulip()["messages"], [])
        later = self.w.run("adopt", ident, *self.args)
        self.assertEqual(later.json()["state"], "notified")
        self.assertEqual(len(self.w.zulip()["messages"]), 1)
        self.assertEqual(len([c for c in self.w.comments(ident) if "[needs-owner] block" in c]), 1)

    def test_a_seat_adopts_only_its_own_issues(self) -> None:
        ident = self.w.seed_issue("Codex's decision", reporter="CODEX")
        r = self.w.run("adopt", ident, *self.args)
        self.assertEqual(r.code, 2)
        self.assertIn("CODEX", r.err)
        self.assertEqual(self.w.calls("agent-sync"), [])

    def test_an_adopted_item_is_not_changed(self) -> None:
        ident = self.w.seed_issue("Tonight: logo?")
        self.w.run("adopt", ident, *self.args)
        r = self.w.run("adopt", ident, "--by", "CLAUDE", "--option", "Revive it", "--option", "Park it", "--json")
        self.assertEqual(r.code, 2)
        self.assertIn("not changed", r.err)

    def test_refusals(self) -> None:
        wrong_kind = self.w.seed_issue("An import", labels=("Kind/github-issue", "needs-owner"))
        closed = self.w.seed_issue("Closed one", state="Done")
        secret = self.w.seed_issue("Has a secret")
        for name, argv in {
            "not agent-report": ("adopt", wrong_kind, *self.args),
            "closed": ("adopt", closed, *self.args),
            "no options": ("adopt", secret, "--by", "CLAUDE"),
            "a secret": ("adopt", secret, "--by", "CLAUDE", "--option", "key lin_api_" + "Zx9Yw8Vv7U" * 3, "--option", "no"),
            "another seat's --by": ("adopt", secret, "--by", "CODEX", "--option", "a", "--option", "b"),
        }.items():
            r = self.w.run(*argv)
            self.assertEqual(r.code, 2, (name, r.out, r.err))
        self.assertEqual(self.w.run("adopt", "AFC-999", *self.args).code, 3)
        self.assertEqual(self.w.calls("agent-sync"), [])
        self.assertEqual([c for c in self.w.calls("board") if c["argv"][0] in ("comment", "label")], [])

    def test_an_adopted_item_filed_by_the_same_question_cannot_be_filed_again(self) -> None:
        ident = self.w.seed_issue("Revive PR 761 or close it?")
        self.w.run("adopt", ident, "--by", "CLAUDE", "--option", "Revive it", "--option", "Close it", "--json")
        r = self.w.run("file", "--by", "CLAUDE", "--app", "AFC", "--question", "Revive PR 761 or close it?",
                       "--option", "Revive it", "--option", "Close it", "--json")
        self.assertEqual(r.code, 0, r.err)
        self.assertEqual(r.json()["identifier"], ident, "the adopted item is found by its uid, not filed twice")
        self.assertEqual(len(self.w.board_state()["issues"]), 1)
        changed = self.w.run("file", "--by", "CLAUDE", "--app", "AFC", "--question", "Revive PR 761 or close it?",
                             "--option", "Revive it", "--option", "Park it", "--json")
        self.assertEqual(changed.code, 2)
        self.assertIn(ident, changed.err)


if __name__ == "__main__":
    unittest.main()
