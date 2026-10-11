"""needs-owner list and status:  what is read back, what is not, and the JSON golden file."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from .world import World

GOLDEN = Path(__file__).resolve().parent / "fixtures" / "list_golden.json"
NOW = "2026-10-11T12:00:00Z"


class ListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.w = World(self)
        patcher = mock.patch.dict(os.environ, {"NEEDS_OWNER_TEST_NOW": NOW})
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        self.assertEqual(self.w.leftover_temp_files(), [])

    def reply(self, ident: str, text: str, tag: str = "[CLAUDE·notify01]") -> None:
        self.w.add_comment(ident, "%s %s" % (tag, text))

    # ---- what counts as an item --------------------------------------------------------------
    def test_only_kind_agent_report_with_the_label_is_an_item(self) -> None:
        self.w.seed_issue("OWNER DECISION: imported from GitHub", labels=("Kind/github-issue",), reporter=None)  # title, no label
        self.w.seed_issue("OWNER DECISION: right title, wrong kind", labels=("Kind/github-issue", "needs-owner"))
        self.w.seed_issue("An ordinary finding", labels=("Kind/agent-report",))
        ident = self.w.seed_issue("Labelled by hand, never adopted")
        listed = self.w.run("list", "--json").json()
        self.assertEqual([(r["identifier"], r["state"]) for r in listed], [(ident, "unadopted")])
        self.assertFalse(listed[0]["block_ok"])
        self.assertEqual(listed[0]["problem"], "no needs-owner block")
        self.assertFalse(listed[0]["seat_verified"])
        status = self.w.run("status", "AFC-300")
        self.assertEqual(status.code, 3)
        self.assertIn("not a needs-owner item", status.err)

    def test_status_of_something_that_is_not_there(self) -> None:
        self.assertEqual(self.w.run("status", "AFC-999").code, 3)
        self.assertEqual(self.w.run("status", "not an id").code, 3)

    def test_the_block_survives_linears_markdown_rewrite(self) -> None:
        q = "Close [old] PRs - all ~ of them?"
        self.assertEqual(self.w.file(q, "[yes] close", "- no ~ keep").code, 0)
        raw = self.w.issue("AFC-300")["description"]
        self.assertIn("```needs-owner", raw)
        got = self.w.run("status", "AFC-300", "--json").json()
        self.assertEqual((got["question"], got["options"]), (q, ["[yes] close", "- no ~ keep"]))
        self.assertTrue(got["block_ok"] and not got["edited"] and got["seat_verified"])

    def test_an_edited_block_is_flagged_and_a_broken_one_is_unadopted(self) -> None:
        self.w.file("Revive PR 761 or close it?", "Revive it", "Close it")
        world = self.w.board_state()
        node = world["issues"]["AFC-300"]
        node["description"] = node["description"].replace("Option 2: Close it", "Option 2: Delete everything")
        self.w.save_board(world)
        got = self.w.run("status", "AFC-300", "--json").json()
        self.assertTrue(got["block_ok"])
        self.assertTrue(got["edited"], "the options no longer fit the uid hash")
        node["description"] = node["description"].replace("Filed by: CLAUDE", "Filed by: CLAUDE\nFiled by: CODEX")
        self.w.save_board(world)
        broken = self.w.run("status", "AFC-300", "--json").json()
        self.assertEqual((broken["state"], broken["block_ok"]), ("unadopted", False))
        self.assertIn("appears twice", broken["problem"])

    # ---- states and filters ------------------------------------------------------------------
    def test_replies_are_recorded_as_ids_never_as_words(self) -> None:
        self.w.file("Decision one?", "Yes", "No", extra=("--low-stakes", "1,2"))
        self.w.file("Decision two?", "Yes", "No")
        self.w.file("Decision three?", "Yes", "No")
        self.w.file("Decision four?", "Yes", "No")
        self.reply("AFC-300", "owner replied:  reaction 2 on msg 630000099 — option 2, please just do it")
        self.reply("AFC-301", "owner replied:  msg 630000100")
        self.reply("AFC-302", "owner replied:  comment 5f1c-uuid\nthe real answer is secret text")
        opened = self.w.run("list", "--json").json()
        self.assertEqual([r["identifier"] for r in opened], ["AFC-303"])
        replied = self.w.run("list", "--replied", "--json")
        rows = {r["identifier"]: r for r in replied.json()}
        self.assertEqual(sorted(rows), ["AFC-300", "AFC-301", "AFC-302"])
        self.assertEqual(rows["AFC-300"]["replied"], {"kind": "reaction", "id": "630000099", "option": 2, "by": "CLAUDE"})
        self.assertEqual(rows["AFC-301"]["replied"], {"kind": "msg", "id": "630000100", "option": None, "by": "CLAUDE"})
        self.assertEqual(rows["AFC-302"]["replied"]["kind"], "comment")
        for word in ("just do it", "secret text", "please"):
            self.assertNotIn(word, replied.out)
        self.assertEqual(rows["AFC-300"]["state"], "replied")
        everything = {r["identifier"] for r in self.w.run("list", "--all", "--json").json()}
        self.assertEqual(everything, {"AFC-300", "AFC-301", "AFC-302", "AFC-303"})

    def test_closed_items_and_the_seat_filter(self) -> None:
        self.w.file("Claude one?", "Yes", "No")
        self.w.run("file", "--by", "CODEX", "--app", "AFC", "--question", "Codex one?", "--option", "Yes", "--option", "No",
                   env=self.w.env(AGENT_SEAT="CODEX"))
        self.w.file("Claude two?", "Yes", "No")
        self.w.close("AFC-302", keep_label=True)  # closed in Linear's UI:  the label stays
        self.assertEqual([r["identifier"] for r in self.w.run("list", "--json").json()], ["AFC-300", "AFC-301"])
        self.assertEqual([r["identifier"] for r in self.w.run("list", "--seat", "CODEX", "--json").json()], ["AFC-301"])
        everything = self.w.run("list", "--all", "--json").json()
        self.assertEqual([(r["identifier"], r["state"]) for r in everything], [("AFC-300", "notified"), ("AFC-301", "notified"), ("AFC-302", "closed")])
        # a closed item that was closed with `board status` lost its label and is gone from every list
        self.w.close("AFC-300")
        self.assertEqual([r["identifier"] for r in self.w.run("list", "--all", "--json").json()], ["AFC-301", "AFC-302"])

    def test_oldest_first_and_a_text_listing(self) -> None:
        self.w.file("First question?", "Yes", "No")
        self.w.file("Second question?", "Yes", "No")
        self.assertEqual([r["identifier"] for r in self.w.run("list", "--json").json()], ["AFC-300", "AFC-301"])
        text = self.w.run("list")
        self.assertEqual(text.code, 0)
        self.assertIn("AFC-300", text.out)
        self.assertIn("First question?", text.out)
        self.assertLess(text.out.index("AFC-300"), text.out.index("AFC-301"))
        self.assertEqual(self.w.run("list", "--seat", "NOBODY").out.strip(), "no decisions waiting on Jay")

    def test_a_failed_read_is_an_error_not_an_empty_list(self) -> None:
        r = self.w.run("list", "--json", env=self.w.env(FAKE_BOARD_FAIL="list"))
        self.assertEqual(r.code, 1)
        self.assertEqual(r.out, "")

    # ---- the golden file ---------------------------------------------------------------------
    def test_list_json_golden(self) -> None:
        self.w.set_zulip(no_credential=["BF-PLUMBER"])
        self.assertEqual(self.w.file("Revive PR 761 or close it?", "Revive it", "Close it", extra=(
            "--low-stakes", "1", "--recommend", "1", "--context", "PR 761 has been stale for a month")).code, 0)
        plumber = self.w.env(AGENT_SEAT=None, AGENT_LAUNCHER="botfleet", AGENT_LAUNCH_SEAT="BF-PLUMBER")
        self.assertEqual(self.w.file("Rotate the backup key now?", "Rotate", "Wait", seat="BF-PLUMBER", env=plumber, extra=("--urgent",)).code, 0)
        self.assertEqual(self.w.run("file", "--by", "CODEX", "--app", "AFC", "--question", "Which logo?", "--option", "Round",
                                    "--option", "Square", "--low-stakes", "1,2", "--json", env=self.w.env(AGENT_SEAT="CODEX")).code, 0)
        self.reply("AFC-302", "owner replied:  reaction 2 on msg 630000500")
        self.w.seed_issue("Antigravity quota labels", reporter="CLAUDE")
        out = self.w.run("list", "--all", "--json")
        self.assertEqual(out.code, 0, out.err)
        got = json.loads(out.out)
        if os.environ.get("UPDATE_GOLDEN"):
            GOLDEN.parent.mkdir(exist_ok=True)
            GOLDEN.write_text(json.dumps(got, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        self.assertEqual(got, json.loads(GOLDEN.read_text(encoding="utf-8")))
        # the schema every consumer relies on
        fields = {"id", "identifier", "url", "seat", "seat_verified", "app", "question", "options", "low_stakes", "severity", "parent",
                  "topic", "created_at", "notified_msg_id", "replied", "state", "age_hours", "over_five"}
        for row in got:
            self.assertTrue(fields <= set(row), fields - set(row))


if __name__ == "__main__":
    unittest.main()
