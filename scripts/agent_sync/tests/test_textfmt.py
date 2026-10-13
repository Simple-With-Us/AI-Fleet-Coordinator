"""The outbound sentence-gap safety net:  the pure function, the client hook and the send paths."""
from __future__ import annotations

import json
import re
import unittest

from agent_sync import secretscan
from agent_sync import zulip as Z
from agent_sync.textfmt import GAP, NBSP, sentence_gap
from agent_sync.tests.harness import TAG, Harness

G = GAP  # U+00A0 then an ASCII space

# (name, text, expected).  Every rule in the textfmt docstring has at least one row.
CONVERTED = (
    ("two spaces after a period", "One.  Two.", "One.%sTwo." % G),
    ("exclamation and question", "Wow!  Really?  Yes.", "Wow!%sReally?%sYes." % (G, G)),
    ("three spaces become one gap", "x.   y", "x.%sy" % G),
    ("five spaces become one gap", "x.     y", "x.%sy" % G),
    ("straight double quote closes", 'He said "Go."  Then left.', 'He said "Go."%sThen left.' % G),
    ("straight single quote closes", "It was 'done.'  Next.", "It was 'done.'%sNext." % G),
    ("curly double quote closes", "“Done.”  Next.", "“Done.”%sNext." % G),
    ("curly single quote closes", "‘Done.’  Next.", "‘Done.’%sNext." % G),
    ("parenthesis closes", "Fine (really.)  Next.", "Fine (really.)%sNext." % G),
    ("bracket closes", "See [note.]  Next.", "See [note.]%sNext." % G),
    ("bold closes", "**Bold.**  Next.", "**Bold.**%sNext." % G),
    ("italic star closes", "*Em.*  Next.", "*Em.*%sNext." % G),
    ("italic underscore closes", "_Em._  Next.", "_Em._%sNext." % G),
    ("ellipsis", "Hmm...  Next.", "Hmm...%sNext." % G),
    ("every line converts", "A.  B.\nC.  D.", "A.%sB.\nC.%sD." % (G, G)),
    ("blockquote line", "> Quote.  More.", "> Quote.%sMore." % G),
    ("list item", "- Item.  More.", "- Item.%sMore." % G),
    ("gap just before a code span", "Run.  `x`", "Run.%s`x`" % G),
    ("gap just after a code span", "Run `x`.  Next.", "Run `x`.%sNext." % G),
    ("text around a double-backtick span", "``a ` b.  c``.  d", "``a ` b.  c``.%sd" % G),
    ("number then period mid-sentence", "Step 1.  Do it.", "Step 1.%sDo it." % G),
    ("the sentence after a list marker", "1.  One.  Two.", "1.  One.%sTwo." % G),
    ("an unmatched backtick is literal", "A ` lone.  tick.", "A ` lone.%stick." % G),
    ("an unmatched code opener protects nothing", "Done.  `unclosed", "Done.%s`unclosed" % G),
    ("a code span cannot run across a blank line", "`a\n\nb.  c`", "`a\n\nb.%sc`" % G),
    ("an unmatched double dollar is literal", "Costs $$5.  Next.", "Costs $$5.%sNext." % G),
    ("a mention does not shield the rest", "@**Dr.  Foo** hi.  there", "@**Dr.  Foo** hi.%sthere" % G),
    ("the seat tag and mention stay", "[CLAUDE·%s→CODEX] @**Codex** Hi.  Next." % TAG,
     "[CLAUDE·%s→CODEX] @**Codex** Hi.%sNext." % (TAG, G)),
    ("after a closed fence", "```\nx.  y\n```\nz.  w", "```\nx.  y\n```\nz.%sw" % G),
    ("after a fence closed by a longer fence", "```\nx.  y\n`````\nz.  w", "```\nx.  y\n`````\nz.%sw" % G),
    ("a fence opener with a backtick in its info is a code span", "```a``` b.  c", "```a``` b.%sc" % G),
    ("a mid-line triple backtick is not a fence", "see ```\na.  b", "see ```\na.%sb" % G),
    ("CRLF line endings", "One.  Two.\r\nThree.  \r\nFour.", "One.%sTwo.\r\nThree.  \r\nFour." % G),
)

# Text that must come back exactly as it went in.
UNCHANGED = (
    ("empty", ""),
    ("no gap at all", "Plain text."),
    ("single space after a period", "Hello. World."),
    ("abbreviation", "Use e.g. this one."),
    ("version number", "v1.2.3 is out."),
    ("brand period", "Congress.Trade and Socratic.Trade."),
    ("no terminator", "Note:  two spaces here, and  here"),
    ("comma and colon", "a,  b:  c"),
    ("trailing spaces are a hard break", "Line one.  \nLine two."),
    ("trailing spaces at the very end", "End.  "),
    ("trailing spaces then a blank line", "End.   \n\nNext paragraph."),
    ("tab after a period", "One.\tTwo."),
    ("existing gap stays exactly as it is", "One.%sTwo." % G),
    ("an existing gap followed by more", "One.%s Two." % NBSP),
    ("backtick fence", "```\nx.  y\n```"),
    ("backtick fence with a language", "```python\nx = 1.  y\n```"),
    ("tilde fence", "~~~\nx.  y\n~~~"),
    ("a longer fence holds a shorter one", "````\n```\ninner.  x\n```\n````"),
    ("a different fence character does not close", "```\nx.  y\n~~~\nstill.  code\n```"),
    ("an unclosed fence runs to the end", "```\nunclosed.  fence\nand.  more"),
    ("fence indented in a list", "  - item\n\n    ```\n    x.  y\n    ```\n"),
    ("fence opened on the list marker line", "1. ```bash\n   x.  y\n   ```"),
    ("fence inside a blockquote", "> ```\n> x.  y\n> ```"),
    ("quote fence holds someone else's words", "```quote\nThey said.  Twice.\n```"),
    ("spoiler fence", "```spoiler Title\nHidden.  Text.\n```"),
    ("math fence", "```math\nx.  y\n```"),
    ("inline code span", "Run `a.  b` now"),
    ("inline code span with two gaps", "`a.  b.  c`"),
    ("double-backtick span", "Run ``a ` b.  c`` now"),
    ("triple-backtick inline span", "Run ```a.  b``` now"),
    ("code span across a line break", "`a.\nb.  c` end"),
    ("inline math", "$$a.  b$$"),
    ("inline math inside prose", "so $$x.  y$$ holds"),
    ("a mention with a gap in the name", "@**Dr.  Foo**"),
    ("a group mention with a gap in the name", "@*Dr.  Fleet* hi"),
    ("ordered list marker", "1.  First\n2.  Second"),
    ("ordered list marker, indented", "   10.  Tenth"),
    ("ordered list marker in a quote", "> 3.  Third"),
)


class SentenceGapTests(unittest.TestCase):
    def test_converted(self) -> None:
        for name, text, expected in CONVERTED:
            with self.subTest(name):
                self.assertEqual(sentence_gap(text), expected)

    def test_closers_chain_and_anything_else_ends_the_run(self) -> None:
        self.assertEqual(sentence_gap("Done.)\"  Next"), "Done.)\"%sNext" % G)
        self.assertEqual(sentence_gap("Done.\u201d)**  Next"), "Done.\u201d)**%sNext" % G)
        self.assertEqual(sentence_gap("Done.}  Next"), "Done.}  Next", "a brace is not a closer")

    def test_unchanged(self) -> None:
        for name, text in UNCHANGED:
            with self.subTest(name):
                self.assertEqual(sentence_gap(text), text)

    def test_idempotent(self) -> None:
        for name, text, _ in CONVERTED:
            with self.subTest(name):
                once = sentence_gap(text)
                self.assertEqual(sentence_gap(once), once)

    def test_exactly_two_spaces_keep_the_length_and_longer_runs_only_shrink(self) -> None:
        for name, text, _ in CONVERTED:
            with self.subTest(name):
                self.assertLessEqual(len(sentence_gap(text)), len(text))
        self.assertEqual(len(sentence_gap("One.  Two.")), len("One.  Two."))

    def test_only_spaces_change(self) -> None:
        strip = re.compile("[ %s]+" % NBSP)
        for name, text, _ in CONVERTED:
            with self.subTest(name):
                self.assertEqual(strip.sub("", sentence_gap(text)), strip.sub("", text))

    def test_text_without_a_double_space_is_returned_as_is(self) -> None:
        text = "nothing to do here"
        self.assertIs(sentence_gap(text), text)

    def test_a_gap_in_a_long_message_is_converted_everywhere_outside_code(self) -> None:
        body = "".join("Sentence %d.  " % n for n in range(500)) + "\n```\nkeep.  this\n```\nEnd.  Done."
        out = sentence_gap(body)
        self.assertEqual(out.count(NBSP), 500, "499 gaps between sentences plus the one after the fence")
        self.assertIn("keep.  this", out)

    def test_pathological_backtick_input_finishes(self) -> None:
        text = "".join("`" * n + " a.  " for n in range(1, 120))
        self.assertIsInstance(sentence_gap(text), str)


class ScannerAndCapAreUnaffectedTests(unittest.TestCase):
    """The conversion runs in the client, after the CLI's secret scanner and length cap.  That is
    safe because it changes neither verdict:  these samples prove it."""

    @staticmethod
    def samples() -> list[str]:
        mixed = ("aB3" * 11)[:32]  # Zulip-shaped:  32 letters and digits with upper, lower and a digit
        secrets_ = [
            "AKIA" + "Q" * 16,
            "gh" + "p_" + "a1" * 18,
            "sk-" + "a" * 30,
            "xox" + "b-" + "1" * 12,
            mixed,
            "password = " + "z" * 20,
            "Authorization: Bearer " + "t" * 24,
            "-----BEGIN " + "PRIVATE KEY-----",
        ]
        texts = ["Plain.  Nothing here.", "Done.  Next.  And more.", "A.   B."]
        for secret in secrets_:
            texts += ["Sorry.  %s  Also.  Done." % secret, "%s.  Then." % secret, "x.  token: %s" % secret]
        return texts

    def test_scan_verdict_is_the_same_before_and_after(self) -> None:
        for text in self.samples():
            with self.subTest(text=text[:20]):
                self.assertEqual(secretscan.scan(sentence_gap(text)), secretscan.scan(text))

    def test_a_known_value_is_still_caught(self) -> None:
        known = ["Kn0wn" + "x" * 20]
        text = "Oops.  here it is: %s.  Bye." % known[0]
        self.assertEqual(secretscan.scan(sentence_gap(text), known), "a loaded credential")

    def test_length_never_grows(self) -> None:
        for text in self.samples():
            self.assertLessEqual(len(sentence_gap(text)), len(text))
        flat = "a." + " " * 2 + "b" * 9996
        self.assertEqual(len(sentence_gap(flat)), len(flat))

    def test_every_sample_has_both_kinds_of_verdict(self) -> None:
        verdicts = {secretscan.scan(text) is None for text in self.samples()}
        self.assertEqual(verdicts, {True, False}, "the samples should include flagged and clean text")


class OutboundParamsTests(unittest.TestCase):
    def test_a_new_message_gets_the_gap_and_the_topic_is_untouched(self) -> None:
        params = {"type": "stream", "to": "agent-sync", "topic": "A.  B", "content": "One.  Two."}
        out = Z.outbound_params("POST", "messages", params)
        self.assertEqual(out["content"], "One.%sTwo." % G)
        self.assertEqual({k: v for k, v in out.items() if k != "content"},
                         {k: v for k, v in params.items() if k != "content"})
        self.assertEqual(params["content"], "One.  Two.", "the caller's dict is not changed")

    def test_other_outbound_text_endpoints(self) -> None:
        for method, path in (("PATCH", "messages/123"), ("POST", "/messages"), ("POST", "scheduled_messages"),
                             ("PATCH", "scheduled_messages/9")):
            with self.subTest(method=method, path=path):
                self.assertEqual(Z.outbound_params(method, path, {"content": "A.  B"})["content"], "A.%sB" % G)

    def test_reads_and_other_endpoints_are_left_alone(self) -> None:
        params = {"content": "A.  B", "narrow": "x"}
        for method, path in (("GET", "messages"), ("GET", "messages/5"), ("DELETE", "messages/5"),
                             ("POST", "messages/5/reactions"), ("POST", "messages/render"),
                             ("POST", "users/me/subscriptions"), ("POST", "user_topics")):
            with self.subTest(method=method, path=path):
                self.assertIs(Z.outbound_params(method, path, params), params)

    def test_missing_or_nonstring_content(self) -> None:
        for params in (None, {}, {"content": None}, {"content": 5}, {"content": ["A.  B"]}):
            with self.subTest(params=params):
                self.assertIs(Z.outbound_params("POST", "messages", params), params)


class SendPathTests(Harness):
    """Through the fake Zulip server:  what arrives is converted, what is read is not."""

    def last_form(self) -> dict[str, str]:
        return self.fake.requests_to("POST", "messages")[-1].form

    def test_post_arrives_with_the_gap_and_the_tag_and_topic_are_untouched(self) -> None:
        result = self.run_cli("post", "--topic", "AFC 18f61cf4 Gap check", "First.  Second.  Third.")
        self.assertEqual(result.code, 0, result.err)
        form = self.last_form()
        self.assertEqual(form["content"], "[CLAUDE·%s] First.%sSecond.%sThird." % (TAG, G, G))
        self.assertEqual(form["topic"], "AFC 18f61cf4 Gap check")
        self.assertEqual(self.fake.messages[-1]["content"], form["content"])
        self.assertIn(NBSP, self.fake.messages[-1]["content"])

    def test_post_to_a_peer_keeps_the_tag_and_mention(self) -> None:
        self.run_cli("post", "--topic", "t", "--to", "codex", "Please look.  Soon.")
        self.assertEqual(self.last_form()["content"],
                         "[CLAUDE·%s→CODEX] @**Codex** Please look.%sSoon." % (TAG, G))

    def test_post_leaves_code_and_trailing_spaces_alone(self) -> None:
        text = "Run this.  Then wait.\n```\nx.  y\n```\nEnd.  \nLast.  Line."
        self.run_cli("post", "--topic", "t", "--no-tag", "--", text)
        self.assertEqual(self.last_form()["content"],
                         "Run this.%sThen wait.\n```\nx.  y\n```\nEnd.  \nLast.%sLine." % (G, G))

    def test_text_from_stdin_is_converted_too(self) -> None:
        self.run_cli("post", "--topic", "t", "--no-tag", "-", stdin="From stdin.  Two.\n")
        self.assertIn("From stdin.%sTwo." % G, self.last_form()["content"])

    def test_reply_arrives_with_the_gap(self) -> None:
        original = self.fake.add_message("Codex", "other", "AFC 12345678 Work", "question?")
        self.run_cli("reply", "--id", str(original), "Answer one.  Answer two.")
        form = self.last_form()
        self.assertEqual((form["to"], form["topic"]), ("other", "AFC 12345678 Work"))
        self.assertEqual(form["content"], "[CLAUDE·%s] Answer one.%sAnswer two." % (TAG, G))

    def test_dm_owner_arrives_with_the_gap(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "listener.toml").write_text("[daemon]\nowner_user_id = 12\n")
        result = self.run_cli("dm", "--owner", "--", "Done.  Please review.")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(self.last_form()["content"], "[CLAUDE·%s→OWNER] Done.%sPlease review." % (TAG, G))

    def test_read_shows_other_peoples_messages_exactly_as_stored(self) -> None:
        peer = "Peer says.  Two spaces.  And a trailing pair.  "
        self.fake.add_message("Codex", "agent-sync", "t", peer)
        shown = self.run_cli("read", "--topic", "t").out
        self.assertIn("Peer says.  Two spaces.  And a trailing pair.", shown)
        self.assertNotIn(NBSP, shown)
        as_json = json.loads(self.run_cli("read", "--topic", "t", "--json").out.splitlines()[0])
        self.assertEqual(as_json["content"], peer)

    def test_reading_back_what_we_sent_is_not_changed_again(self) -> None:
        self.run_cli("post", "--topic", "t", "--no-tag", "One.  Two.")
        shown = self.run_cli("read", "--topic", "t", "--include-self").out
        self.assertIn("One.%sTwo." % G, shown)
        self.assertEqual(shown.count(NBSP), 1)

    def test_unicode_text_survives(self) -> None:
        self.run_cli("post", "--topic", "t", "--no-tag", "--", "Caf\u00e9.  \u00dcn\u00ef.  \u65e5\u672c\u8a9e\u3002  \U0001f600.  Done!")
        self.assertEqual(self.last_form()["content"],
                         "Caf\u00e9.%s\u00dcn\u00ef.%s\u65e5\u672c\u8a9e\u3002  \U0001f600.%sDone!" % (G, G, G))


if __name__ == "__main__":
    unittest.main()
