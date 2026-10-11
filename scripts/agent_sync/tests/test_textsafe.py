"""textsafe.render_untrusted:  text from Linear, PRs, branches and people made inert before it is echoed to Zulip."""
from __future__ import annotations

import re
import unittest

from agent_sync.textsafe import ELLIPSIS, EMPTY, FULLWIDTH_AT, render_untrusted

from agent_sync.tests.fake_zulip import visible_markup

# Constructed here, not typed, so no line of this file is a mention or a widget command.
AT = "@"
ALL = AT + "**all**"


def inside(rendered: str) -> str:
    """The text between the code span's backticks."""
    assert rendered.startswith("`") and rendered.endswith("`") and rendered.count("`") == 2, rendered
    return rendered[1:-1]


class InertnessTests(unittest.TestCase):
    def test_a_fleet_wide_mention_is_inert(self) -> None:
        out = render_untrusted("please ping " + ALL + " now", 200)
        self.assertNotIn(AT, out)
        self.assertIn(FULLWIDTH_AT + "**all**", out)
        # and the fake server's own mention rules agree:  it sets no wildcard or mention flag from this
        self.assertNotIn("**all**", visible_markup(out))

    def test_every_mention_form_loses_its_at_sign(self) -> None:
        for text in (AT + "**Jay Wedgeworth**", AT + "_**Claude**", AT + "*team*", AT + "**Claude|10**", "a" + AT + "b.test"):
            with self.subTest(text=text):
                self.assertNotIn(AT, render_untrusted(text, 100))

    def test_a_backtick_cannot_break_out_of_the_span(self) -> None:
        for text in ("x` " + ALL, "``` \n" + ALL, "`" * 9, "a`b`c", "end`", "`start"):
            with self.subTest(text=text):
                out = render_untrusted(text, 100)
                self.assertEqual(out.count("`"), 2, out)
                self.assertTrue(out.startswith("`") and out.endswith("`"))
                self.assertNotIn("`", inside(out))

    def test_a_removed_backtick_cannot_rejoin_what_the_other_rules_looked_past(self) -> None:
        for text in ("https:`//evil.test", "/`poll Q", "[a]`(https://x.test)", AT + "`**all**", "ht`tps://x"):
            with self.subTest(text=text):
                out = inside(render_untrusted(text, 100))
                self.assertNotIn("://", out)
                self.assertNotIn("/poll", out.lower())
                self.assertNotIn("](", out)
                self.assertNotIn(AT, out)

    def test_poll_and_todo_widgets_are_stripped(self) -> None:
        for text in ("/poll Lunch?\nA\nB", "/POLL x", "//poll x", "ok /todo list", "/ poll", "/​poll x", "x\n/poll y"):
            with self.subTest(text=text):
                out = inside(render_untrusted(text, 100)).lower()
                self.assertNotIn("/poll", out)
                self.assertNotIn("/todo", out)
        self.assertEqual(inside(render_untrusted("/poll Lunch?", 100)), "poll Lunch?")

    def test_words_that_only_start_like_a_widget_are_left_alone(self) -> None:
        self.assertEqual(inside(render_untrusted("/polling stations", 100)), "/polling stations")
        self.assertEqual(inside(render_untrusted("docs/pollster", 100)), "docs/pollster")

    def test_markdown_links_and_urls_are_defanged(self) -> None:
        out = inside(render_untrusted("see [the fix](https://evil.test/p?q=1) and ![img](http://x.test/a.png \"t\")", 200))
        self.assertNotIn("](", out)
        self.assertNotIn("://", out)
        self.assertIn("the fix https[:]//evil.test/p?q=1", out)
        self.assertIn("img http[:]//x.test/a.png", out)
        self.assertNotIn("://", inside(render_untrusted("bare https://evil.test and ftp://x", 100)))

    def test_nested_and_unclosed_links(self) -> None:
        for text in ("[[a](u1)](u2)", "[a](b", "[a]( b c )", "[](x)", "]( x"):
            with self.subTest(text=text):
                self.assertNotIn("](", inside(render_untrusted(text, 100)))

    def test_newlines_tabs_and_unicode_whitespace_collapse_to_one_line(self) -> None:
        out = inside(render_untrusted("a\nb\r\nc\td e f g   h\x0bi", 100))
        self.assertEqual(out, "a b c d e f g h i")

    def test_controls_and_invisible_format_characters_are_removed(self) -> None:
        out = inside(render_untrusted("a\x00b\x1b[31mc​d‮e⁦f﻿g\x7fh", 100))
        self.assertEqual(out, "ab[31mcdefgh")
        for char in out:
            self.assertGreaterEqual(ord(char), 0x20)

    def test_a_hidden_character_cannot_split_a_pattern_apart(self) -> None:
        out = inside(render_untrusted("/pol​l x " + AT + "​**all**", 100))
        self.assertNotIn("/poll", out)
        self.assertNotIn(AT, out)

    def test_the_result_is_a_single_code_span_on_one_line(self) -> None:
        for text in ("plain", "two\nlines", "a" * 1000, "`" + ALL + "`", "[x](y)"):
            with self.subTest(text=text[:20]):
                out = render_untrusted(text, 80)
                self.assertNotIn("\n", out)
                self.assertTrue(re.fullmatch(r"`[^`]+`", out), out)


class CapTests(unittest.TestCase):
    def test_the_whole_result_never_exceeds_the_cap(self) -> None:
        for cap in (3, 4, 5, 10, 40, 200):
            with self.subTest(cap=cap):
                self.assertLessEqual(len(render_untrusted("x" * 500, cap)), cap)
                self.assertLessEqual(len(render_untrusted("https://a.test/" + "y" * 500, cap)), cap)

    def test_a_long_text_ends_with_an_ellipsis_and_a_short_one_does_not(self) -> None:
        self.assertEqual(render_untrusted("x" * 50, 10), "`" + "x" * 7 + ELLIPSIS + "`")
        self.assertEqual(render_untrusted("short", 10), "`short`")
        self.assertEqual(render_untrusted("exactly8", 10), "`exactly8`")
        self.assertTrue(render_untrusted("nine char", 10).endswith(ELLIPSIS + "`"))

    def test_the_cap_counts_what_the_rewrites_add(self) -> None:
        # "://" grows to "[:]//", so the cap must be applied after the rewrite
        self.assertLessEqual(len(render_untrusted("://" * 100, 30)), 30)

    def test_without_a_code_span_the_cap_is_on_the_bare_text(self) -> None:
        self.assertEqual(render_untrusted("x" * 50, 10, code_span=False), "x" * 9 + ELLIPSIS)
        self.assertEqual(render_untrusted("hello " + ALL, 100, code_span=False), "hello " + FULLWIDTH_AT + "**all**")
        self.assertNotIn("`", render_untrusted("a`b", 10, code_span=False))

    def test_a_cap_too_small_to_hold_the_span_is_refused(self) -> None:
        for cap in (0, 1, 2, -5, True, 2.5, "9"):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                render_untrusted("x", cap)  # type: ignore[arg-type]
        self.assertEqual(render_untrusted("xyz", 1, code_span=False), ELLIPSIS)


class TotalityTests(unittest.TestCase):
    def test_empty_and_non_text_inputs(self) -> None:
        self.assertEqual(render_untrusted("", 50), "`%s`" % EMPTY)
        self.assertEqual(render_untrusted(None, 50), "`%s`" % EMPTY)
        self.assertEqual(render_untrusted("  \n\t ", 50), "`%s`" % EMPTY)
        self.assertEqual(render_untrusted("`", 50), "`%s`" % EMPTY)
        self.assertEqual(render_untrusted(42, 50), "`42`")
        self.assertEqual(render_untrusted(["a", "b"], 50), "`['a', 'b']`")
        self.assertEqual(render_untrusted("", 5, code_span=False), EMPTY[:5])

    def test_it_is_idempotent(self) -> None:
        samples = ("plain", ALL + " [x](https://y.test) `z` /poll q", "a" * 300, "https:`//x", "[[a](u1)](u2)", "",
                   "ünï​code", "x" * 38 + "/poll")
        for text in samples:
            for cap in (12, 60, 200):
                with self.subTest(text=text[:20], cap=cap):
                    once = render_untrusted(text, cap)
                    self.assertEqual(render_untrusted(once, cap), once)

    def test_it_does_not_normalize_the_full_width_at_sign_back_into_a_real_one(self) -> None:
        self.assertNotIn(AT, render_untrusted(FULLWIDTH_AT + "**all**", 50))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
