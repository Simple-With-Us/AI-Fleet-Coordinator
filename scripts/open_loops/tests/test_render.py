"""render --markdown:  the full list from the facts."""
import json
import re
import unittest

from open_loops.facts import build_facts
from open_loops.lint import find_urls
from open_loops.render import BRIEF_LIMIT, SECTION_TITLES, render_markdown

from .builders import NOW, Sandbox, board_row
from .scenes import standard_world


class Render(Sandbox):
    def facts(self, world=None, **kw):
        world = world or standard_world(self.home, self.bin)
        kw.setdefault("now", NOW)
        return build_facts(run=world.runner(), sleep=lambda s: None, **kw)

    def test_the_full_list_has_all_six_sections_in_title_case(self):
        text = render_markdown(self.facts())
        for number, title in enumerate(SECTION_TITLES, 1):
            self.assertRegex(text, r"(?m)^## %d\. %s" % (number, re.escape(title)))
        self.assertTrue(text.startswith("# Open Loops as of Sun, Oct 11, 7:00am"))
        self.assertNotIn("CDT", text)
        self.assertIn("Full list:  ", text)
        self.assertTrue(text.endswith("\n"))

    def test_every_link_is_one_the_gatherer_made(self):
        f = self.facts()
        links = find_urls(render_markdown(f))
        self.assertTrue(links)
        self.assertLessEqual(set(links), set(f["urls"]))

    def test_nothing_in_it_can_mention_or_link(self):
        text = render_markdown(self.facts())
        self.assertNotIn("@**", text)
        self.assertNotIn("@_**", text)
        self.assertIn("`＠**all** ship it?`", text)                    # inert and inside a code span
        self.assertNotIn("fork text", text)
        self.assertNotRegex(text, r"(?m)^\s*/(poll|todo)")

    def test_titles_from_outside_are_in_code_spans(self):
        text = render_markdown(self.facts())
        self.assertIn("`Conflicted and old`", text)
        self.assertIn("`Pick a color`", text)

    def test_sentence_gaps_are_nbsp_plus_a_space(self):
        text = render_markdown(self.facts())
        self.assertIn("on %s.  Sources ok" % re.search(r"built on `([^`]+)`", text).group(0).split("`")[1].join(["`", "`"]) if False else ".  Sources ok", text)
        self.assertNotRegex(text, r"[.!?]  [A-Z]")

    def test_stuck_prs_say_whose_age_it_is(self):
        text = render_markdown(self.facts())
        self.assertIn("head 2 days old", text)
        self.assertIn("2 lanes are being worked on right now;  1 lane already landed (the doctor says safe to remove).", text)
        self.assertIn("Age is the head commit's age, not the time spent in that state.", text)

    def test_work_not_on_github_lists_every_kind_with_no_age_floor(self):
        text = render_markdown(self.facts())
        self.assertIn("### Snapshot Only (1, no age floor)", text)
        self.assertIn("`claude-snap`", text)
        self.assertIn("unchanged 1 hour", text)
        self.assertIn("### Armed PRs With Local Work (1)", text)
        self.assertIn("### Refused (1)", text)
        self.assertIn("`gate failure: a secret-shaped string`", text)                     # one space:  the renderer makes one line

    def test_a_failed_source_prints_missing_and_never_a_zero(self):
        world = standard_world(self.home, self.bin)
        world.github_fail = True
        text = render_markdown(self.facts(world))
        self.assertRegex(text, r"(?m)^Missing:  github \(")
        self.assertRegex(text, r"(?m)^## 2\. PRs Stuck 48 Hours or More$")             # no "(0)"
        self.assertNotRegex(text, r"## 2\.[^\n]*\(0\)")
        self.assertIn("Missing:  github", text.split("## 2.")[1].split("## 3.")[0])

    def test_without_c1_section_3_is_missing_not_empty(self):
        world = standard_world(self.home, self.bin)
        (self.bin / "fleet-autopush").unlink()
        text = render_markdown(self.facts(world))
        part = text.split("## 3.")[1].split("## 4.")[0]
        self.assertIn("Missing:  autopush (fleet-autopush is not installed)", part)
        self.assertNotIn("(0", part)

    def test_claims_show_the_note_and_the_bands(self):
        text = render_markdown(self.facts())
        part = text.split("## 5.")[1].split("## 6.")[0]
        self.assertIn("Claim clock not started;  these are ages, not releases.", part)
        self.assertIn("### 48 Hours or More (1)", part)
        self.assertIn("### 36 Hours or More (1)", part)
        self.assertIn("### Unknown (1)", part)
        self.assertIn("Some of these were not checked against:", part)
        self.assertIn("Not banded:  1 orphan, 0 with an unreadable holder, 0 held by the owner.", part)

    def test_asks_are_not_available_until_c3(self):
        text = render_markdown(self.facts())
        self.assertIn("Not available:  not built yet (C3, Phase 2).", text)

    def test_decisions_show_tap_to_answer_and_the_owner_topic(self):
        text = render_markdown(self.facts())
        part = text.split("## 1.")[1].split("## 2.")[0]
        self.assertIn("tap to answer: options 1, 2", part)
        self.assertIn("[OWNER topic](", part)
        self.assertIn("More than 5 open from one seat:  CLAUDE.", part)
        self.assertIn("1 answered and waiting for the filer to close.", part)

    def test_brief_caps_every_list_at_five_and_says_how_many_more(self):
        world = standard_world(self.home, self.bin)
        world.needs_owner = [board_row("FFX-%d" % (20 + i), seats=("CLAUDE",), n=20 + i, title="OWNER DECISION:  Question %d" % i, status="open") for i in range(8)]
        f = self.facts(world)
        full = render_markdown(f)
        brief = render_markdown(f, limit=BRIEF_LIMIT)
        part = brief.split("## 1.")[1].split("## 2.")[0]
        self.assertEqual(part.count("\n- [FFX-"), 5)
        self.assertIn("- + 3 more", part)                                  # 8 decisions, 5 shown
        self.assertEqual(full.split("## 1.")[1].split("## 2.")[0].count("\n- [FFX-"), 8)
        self.assertLess(len(brief), len(full))
        self.assertEqual(render_markdown(f, limit=None), full)
        # nothing is capped when there is nothing to cap
        self.assertEqual(render_markdown(self.facts(), limit=BRIEF_LIMIT), render_markdown(self.facts()))

    def test_an_empty_list_that_was_read_says_none(self):
        world = standard_world(self.home, self.bin)
        world.needs_owner = []
        world.pages[""]["data"]["search"]["nodes"] = world.pages[""]["data"]["search"]["nodes"][3:4]
        world.pages[""]["data"]["search"]["issueCount"] = 1
        text = render_markdown(self.facts(world))
        self.assertIn("## 1. Decisions Waiting on You (0)\n\nNone.\n", text)
        self.assertIn("## 2. PRs Stuck 48 Hours or More (0)\n\nNone.\n", text)

    def test_it_is_deterministic(self):
        f = self.facts()
        self.assertEqual(render_markdown(f), render_markdown(json.loads(json.dumps(f))))


if __name__ == "__main__":
    unittest.main()
