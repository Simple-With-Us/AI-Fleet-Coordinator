"""The DM lint:  every rule has a passing and a failing case."""
import unittest

from open_loops import lint
from open_loops.facts import build_facts
from open_loops.lint import MAX_CHARS, lint as run_lint, prepare

from .builders import NOW, Sandbox
from .scenes import standard_world

GAP = "  "


class Lint(Sandbox):
    def setUp(self):
        super().setUp()
        self.facts = build_facts(run=standard_world(self.home, self.bin).runner(), now=NOW, sleep=lambda s: None)
        self.fid = self.facts["facts_id"]
        self.pr = self.facts["stuck_prs"]["items"][0]["url"]

    def dm(self, extra="", *, section3=True, missing=False, full=True, fid=True):
        parts = ["Good morning." + GAP + "Two decisions wait on you."]
        parts.append("Stuck:  [PR 1](%s)." % self.pr)
        if section3:
            parts.append("Work Not on GitHub Yet:  3 items.")
        if missing:
            parts.append("Missing:  nothing.")
        if full:
            parts.append("Full list:  " + self.facts["full_list_url"])
        if fid:
            parts.append("Facts " + self.fid + ".")
        if extra:
            parts.append(extra)
        return "\n".join(parts) + "\n"

    def problems(self, text, facts=None):
        return run_lint(text, facts or self.facts)

    def test_a_good_dm_passes(self):
        self.assertEqual(self.problems(self.dm()), [])

    def test_the_budget_is_under_4000_characters(self):
        base = self.dm()
        pad = "x" * (MAX_CHARS - len(base) - 1)
        exactly = base + pad + "\n"
        self.assertEqual(len(exactly), MAX_CHARS)
        self.assertEqual(self.problems(exactly), [])
        over = base + pad + "x\n"
        self.assertEqual(len(over), MAX_CHARS + 1)
        found = self.problems(over)
        self.assertEqual(len(found), 1)
        self.assertIn("4,000", found[0])

    def test_empty_is_refused(self):
        self.assertEqual(self.problems("  \n"), ["the DM is empty"])

    def test_a_link_the_gatherer_did_not_make_is_refused(self):
        found = self.problems(self.dm("See https://evil.test/login now"))
        self.assertTrue(any("did not make" in p and "evil.test" in p for p in found), found)
        self.assertTrue(any("did not make" in p for p in self.problems(self.dm("see www.evil.test/x"))))
        self.assertTrue(any("did not make" in p for p in self.problems(self.dm("[click](ftp://evil.test/x)"))))
        # a link from the facts is fine, with punctuation after it or inside a Markdown link
        self.assertEqual(self.problems(self.dm("See (%s)." % self.pr)), [])
        self.assertEqual(self.problems(self.dm("[the list](%s), please" % self.facts["full_list_url"])), [])

    def test_a_link_with_one_character_changed_is_refused(self):
        self.assertTrue(self.problems(self.dm(self.pr + "9")))

    def test_hand_typed_mentions_are_refused(self):
        for mention in ("@**all**", "@_**Jay**", "hello @**Jay Doe** there"):
            found = self.problems(self.dm(mention))
            self.assertTrue(any("mention" in p for p in found), mention)
        self.assertEqual(self.problems(self.dm("inert: ＠**all**")), [])          # what render_untrusted writes

    def test_widgets_are_refused(self):
        for line in ("/poll Lunch?", "  /todo Buy milk", "> /poll x"):
            self.assertTrue(any("widget" in p for p in self.problems(self.dm(line))), line)
        self.assertEqual(self.problems(self.dm("a path like /pollster is fine")), [])

    def test_secret_shaped_text_is_refused_without_quoting_it(self):
        token = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        found = self.problems(self.dm("token " + token))
        self.assertTrue(any("secret-shaped" in p for p in found))
        self.assertFalse(any(token in p for p in found))

    def test_sentence_gaps(self):
        good = "One good gap." + GAP + "Then another."
        self.assertEqual(self.problems(self.dm(good)), [])
        bad = "One good gap." + GAP + "Then a bad one. Right here."
        found = self.problems(self.dm(bad))
        self.assertEqual(len(found), 1, found)
        self.assertIn("1 sentence ends in one space", found[0])
        self.assertIn("(line 6)", found[0])

    def test_two_ascii_spaces_are_a_gap_simple_chat_will_convert(self):
        self.assertEqual(self.problems(self.dm("Two spaces.  Then more.")), [])
        self.assertEqual(prepare("Two spaces.  Then more."), "Two spaces." + GAP + "Then more.")
        self.assertEqual(prepare(prepare("A.  B.")), prepare("A.  B."))                 # idempotent

    def test_what_is_not_a_sentence_boundary_is_left_alone(self):
        for text in ("e.g. Something", "i.e. Something", "see 1. Item one", "Mr. Smith", "v1.2.3 Release", "approx. Ten", "vs. Them",
                     "`code. Inside` stays", "a link " + "https://x.test/a.B c", "x.\nNext line starts here"):
            self.assertEqual(lint._single_space_boundaries(text), [], text)
        self.assertEqual(lint._single_space_boundaries("Ends here. Starts there."), [1])
        self.assertEqual(lint._single_space_boundaries("```\nin a fence. Not prose\n```"), [])

    def test_the_facts_id_is_required(self):
        self.assertTrue(any("facts id" in p for p in self.problems(self.dm(fid=False))))

    def test_the_full_list_link_is_required(self):
        self.assertTrue(any("full-list" in p for p in self.problems(self.dm(full=False))))

    def test_section_3_is_required_when_work_is_not_on_github(self):
        self.assertGreater(self.facts["not_on_github"]["count"], 0)
        found = self.problems(self.dm(section3=False))
        self.assertTrue(any("Work Not on GitHub Yet" in p for p in found), found)
        self.assertEqual(self.problems(self.dm("work not on github yet")), [])
        quiet = dict(self.facts, not_on_github=dict(self.facts["not_on_github"], count=0))
        self.assertEqual(self.problems(self.dm(section3=False), quiet), [])
        unknown = dict(self.facts, not_on_github=dict(self.facts["not_on_github"], count=None))
        self.assertEqual(self.problems(self.dm(section3=False), unknown), [])        # unknown is the Missing line's job

    def test_missing_is_required_when_a_source_could_not_be_read(self):
        facts = dict(self.facts, missing=[{"source": "github", "reason": "gh exited 1"}])
        self.assertTrue(any("Missing:" in p for p in self.problems(self.dm(missing=False), facts)))
        self.assertEqual(self.problems(self.dm(missing=True), facts), [])

    def test_a_problem_never_quotes_the_dm(self):
        secret_text = "very private sentence 12345"
        found = self.problems(self.dm(secret_text + " https://evil.test/x"))
        self.assertFalse(any(secret_text in p for p in found))


if __name__ == "__main__":
    unittest.main()
