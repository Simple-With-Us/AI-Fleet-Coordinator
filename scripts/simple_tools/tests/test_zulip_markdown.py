"""Zulip's message content as readable Markdown.

The relay shows a Zulip post whole, as a reader can follow it:  its paragraphs, its
lists, its quotes, its code and its links, with the line structure it was sent with.
These tests pin that conversion, in both of the shapes Zulip hands a message over in
(the rendered HTML and the Markdown source), and the few rules that keep the converted
text from changing the shape of the document it is put in.
"""

from __future__ import annotations

import unittest

from simple_tools import zulip_markdown as zm


class HtmlTests(unittest.TestCase):
    """Zulip's rendered HTML becomes Markdown."""

    def test_a_paragraph_becomes_a_block_and_a_break_stays_a_line(self) -> None:
        out = zm.to_markdown("<p>first line<br>second line</p><p>second paragraph</p>")
        self.assertEqual(out, "first line\nsecond line\n\nsecond paragraph")

    def test_a_code_block_becomes_a_fenced_block(self) -> None:
        html = '<div class="codehilite"><pre><span></span><code>print(1)\nprint(2)\n</code></pre></div>'
        out = zm.to_markdown(html)
        self.assertTrue(out.startswith("```\n"))
        self.assertIn("print(1)\nprint(2)", out)
        self.assertTrue(out.endswith("\n```"))

    def test_a_pre_block_becomes_a_fenced_block(self) -> None:
        out = zm.to_markdown("<pre><code>ls -l\n</code></pre>")
        self.assertIn("ls -l", out)
        self.assertTrue(out.startswith("```"))

    def test_inline_code_becomes_a_code_span(self) -> None:
        out = zm.to_markdown("<p>run <code>make test</code> now</p>")
        self.assertEqual(out, "run `make test` now")

    def test_a_link_becomes_a_markdown_link_with_its_url_escaped(self) -> None:
        out = zm.to_markdown('<p><a href="https://example.com/a(b)">the docs</a></p>')
        self.assertEqual(out, "[the docs](https://example.com/a\\(b\\))")

    def test_a_mention_becomes_plain_text(self) -> None:
        html = '<p><span class="user-mention" data-user-id="123456789012345678">@Claude</span></p>'
        self.assertEqual(zm.to_markdown(html), "@Claude")

    def test_a_group_mention_becomes_plain_text(self) -> None:
        html = '<p><span class="user-group-mention" data-user-id="9">@fleet</span></p>'
        self.assertEqual(zm.to_markdown(html), "@fleet")

    def test_an_emoji_becomes_its_name(self) -> None:
        html = '<p><span class="emoji emoji-1z602" title="thumbs up"></span></p>'
        self.assertEqual(zm.to_markdown(html), ":thumbs up:")

    def test_an_emoji_without_a_title_becomes_its_id(self) -> None:
        html = '<p><span class="emoji emoji-1z602"></span></p>'
        self.assertEqual(zm.to_markdown(html), ":1z602:")

    def test_an_embedded_message_becomes_a_short_placeholder_naming_the_link(self) -> None:
        html = '<div class="message_embed"><a href="https://example.com/embed">Preview</a></div>'
        out = zm.to_markdown(html)
        self.assertIn("https://example.com/embed", out)
        self.assertNotIn("Preview", out)
        self.assertLess(len(out), 80)

    def test_a_list_becomes_a_list(self) -> None:
        out = zm.to_markdown("<ul><li>one</li><li>two</li></ul>")
        self.assertEqual(out, "- one\n- two")

    def test_a_quote_keeps_its_marker(self) -> None:
        out = zm.to_markdown("<blockquote><p>quoted</p></blockquote>")
        self.assertEqual(out, "> quoted")

    def test_the_markup_attributes_never_reach_the_text(self) -> None:
        html = '<p><span class="user-mention" data-user-id="123456789012345678">@Claude</span></p>'
        out = zm.to_markdown(html)
        self.assertNotIn("123456789012345678", out)
        self.assertNotIn("data-user-id", out)

    def test_a_link_that_is_only_an_attribute_keeps_its_url_in_the_text(self) -> None:
        # The URL stays in the converted text, so a credential inside one is still caught by
        # the scanner that runs over the converted text.  The token is built at run time, so
        # no credential-shaped literal sits in the source.
        token = "ghp_" + "fake" * 9
        html = '<p><a href="https://example.com/%s">link</a></p>' % token
        self.assertIn(token, zm.to_markdown(html))

    def test_a_tag_that_is_not_markup_is_escaped(self) -> None:
        # The angle bracket is escaped, so the text cannot open an HTML tag in the comment.
        out = zm.to_markdown("<p>a &lt;b&gt; tag</p>")
        self.assertEqual(out, "a \\<b> tag")


class SourceTests(unittest.TestCase):
    """Zulip's Markdown source stays Markdown, with the line structure it was sent with."""

    def test_the_line_structure_is_kept(self) -> None:
        source = "first line\nsecond line\n\nsecond paragraph"
        self.assertEqual(zm.to_markdown(source), source)

    def test_a_mention_becomes_plain_text(self) -> None:
        self.assertEqual(zm.to_markdown("hello @**MiniMax** SS"), "hello @MiniMax SS")

    def test_a_link_stays_a_link(self) -> None:
        out = zm.to_markdown("see [the docs](https://example.com/x)")
        self.assertEqual(out, "see [the docs](https://example.com/x)")

    def test_a_link_url_is_escaped(self) -> None:
        out = zm.to_markdown("see [the docs](https://example.com/a(b))")
        self.assertEqual(out, "see [the docs](https://example.com/a\\(b\\))")

    def test_a_bracket_that_is_not_a_link_is_escaped(self) -> None:
        out = zm.to_markdown("[CLAUDE:  SS.1.S  AFC]")
        self.assertEqual(out, "\\[CLAUDE:  SS.1.S  AFC\\]")

    def test_a_code_span_stays_a_code_span(self) -> None:
        out = zm.to_markdown("run `make test` now")
        self.assertEqual(out, "run `make test` now")

    def test_an_unclosed_backtick_is_escaped(self) -> None:
        out = zm.to_markdown("a lone ` backtick")
        self.assertEqual(out, "a lone \\` backtick")

    def test_a_code_fence_is_kept_and_closed(self) -> None:
        out = zm.to_markdown("```python\nprint(1)\n```")
        self.assertEqual(out, "```python\nprint(1)\n```")

    def test_a_fence_the_source_left_open_is_closed(self) -> None:
        out = zm.to_markdown("```python\nprint(1)")
        self.assertEqual(out, "```python\nprint(1)\n```")

    def test_a_credential_in_a_link_url_stays_in_the_text(self) -> None:
        token = "ghp_" + "fake" * 9
        out = zm.to_markdown("[link](https://example.com/%s)" % token)
        self.assertIn(token, out)

    def test_empty_content_is_empty(self) -> None:
        self.assertEqual(zm.to_markdown(""), "")
        self.assertEqual(zm.to_markdown("   \n  "), "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
