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
        self.assertEqual(out, "```\nprint(1)\nprint(2)\n\n```")

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


class FenceTests(unittest.TestCase):
    """A code fence the text leaves open is closed where the text stops."""

    def test_an_open_fence_is_closed(self) -> None:
        self.assertEqual(zm.close_code_fence("```\ncode"), "```\ncode\n```")

    def test_a_closed_fence_is_left_alone(self) -> None:
        self.assertEqual(zm.close_code_fence("```\ncode\n```"), "```\ncode\n```")

    def test_text_without_a_fence_is_left_alone(self) -> None:
        self.assertEqual(zm.close_code_fence("no code here"), "no code here")

    def test_a_shorter_run_inside_the_block_does_not_close_it(self) -> None:
        text = "````\n```\nstill code\n"
        self.assertEqual(zm.close_code_fence(text), "````\n```\nstill code\n````")


class ReviewTests(unittest.TestCase):
    """The findings of the Kody review on PR 2340.

    Each test here pins one finding:  the shape of a post is read from its start, the
    sender's own attribute values are escaped like the rest of the text, a nested list
    keeps the text of the item that holds it, and a link's text is escaped once.
    """

    def test_a_source_post_that_talks_about_markup_keeps_its_text(self) -> None:
        # The shape is read from the start of the post:  a post that mentions a <p> tag
        # is the sender's own text and is not read as HTML.
        out = zm.to_markdown("a <p> tag opens a paragraph")
        self.assertIn("\\<p>", out)
        self.assertIn("tag opens a paragraph", out)

    def test_a_code_block_that_shows_markup_keeps_its_text(self) -> None:
        out = zm.to_markdown("```html\n<p>hello</p>\n```")
        self.assertIn("<p>hello</p>", out)

    def test_rendered_html_is_still_read_as_html(self) -> None:
        self.assertEqual(zm.to_markdown("<p>first</p><p>second</p>"), "first\n\nsecond")

    def test_an_image_alt_cannot_carry_raw_html_into_the_text(self) -> None:
        out = zm.to_markdown('<p><img src="x" alt="a < b"></p>')
        self.assertNotIn("a < b", out)
        self.assertIn("a \\< b", out)

    def test_an_emoji_name_cannot_carry_raw_html_into_the_text(self) -> None:
        html = '<p><span class="emoji emoji-1z602" title="a < b"></span></p>'
        out = zm.to_markdown(html)
        self.assertNotIn("a < b", out)
        self.assertIn(":a \\< b:", out)

    def test_an_embedded_url_cannot_carry_raw_html_into_the_text(self) -> None:
        html = '<div class="message_embed"><a href="https://example.com/a<b">x</a></div>'
        out = zm.to_markdown(html)
        self.assertNotIn("a<b", out)
        self.assertIn("a\\<b", out)

    def test_a_nested_list_keeps_the_text_of_the_item_that_holds_it(self) -> None:
        out = zm.to_markdown("<ul><li>one<ul><li>a</li></ul></li></ul>")
        self.assertIn("one", out)
        self.assertIn("a", out)
        self.assertIn("- one", out)

    def test_a_link_text_is_escaped_once(self) -> None:
        out = zm.to_markdown('<p><a href="https://example.com/x">a [b] c</a></p>')
        self.assertEqual(out, "[a \\[b\\] c](https://example.com/x)")


class LinkPartsTests(unittest.TestCase):
    """The parts of a link:  a mention and a code span inside it stay what they are."""

    def test_a_mention_inside_a_link_is_escaped_once(self) -> None:
        # The link escapes the name of the mention once, and the reader sees no stray
        # backslash (Kody, PR 2340).
        html = '<p><a href="https://x"><span class="user-mention">@A[B</span></a></p>'
        self.assertEqual(zm.to_markdown(html), "[@A\\[B](https://x)")

    def test_an_unclosed_mention_inside_a_link_keeps_the_url(self) -> None:
        # A mention the sender never closed still lands in the text of the link, and the
        # link keeps its URL (Kody, PR 2340).
        html = '<p><a href="https://x"><span class="user-mention">@A[B</a></p>'
        self.assertEqual(zm.to_markdown(html), "[@A\\[B](https://x)")

    def test_a_code_span_inside_a_link_keeps_its_markers(self) -> None:
        # The code span stays a code span inside the link:  its markers are not escaped
        # (Kody, PR 2340).
        html = '<p><a href="https://x">the <code>docs</code></a></p>'
        self.assertEqual(zm.to_markdown(html), "[the `docs`](https://x)")

    def test_an_emoji_inside_a_link_keeps_its_name(self) -> None:
        html = '<p><a href="https://x"><span class="emoji" title="tada"></span></a></p>'
        self.assertEqual(zm.to_markdown(html), "[:tada:](https://x)")

    def test_an_image_inside_a_link_keeps_its_placeholder(self) -> None:
        # The brackets of the placeholder are escaped, so the link opener survives the
        # scan that defangs a link and the link never goes live (Kody, PR 2340).
        html = '<p><a href="https://x"><img src="s" alt="pic"></a></p>'
        self.assertEqual(zm.to_markdown(html), "[\\[image: pic\\]](https://x)")

    def test_a_mention_and_a_code_span_inside_a_link_keep_their_own_shape(self) -> None:
        html = (
            '<p><a href="https://x"><span class="user-mention">@A[B</span>'
            " and <code>run</code></a></p>"
        )
        self.assertEqual(zm.to_markdown(html), "[@A\\[B and `run`](https://x)")


class BufferTests(unittest.TestCase):
    """Every buffer takes the text that belongs in it, and each part is escaped once."""

    def test_a_code_span_inside_a_mention_keeps_its_place(self) -> None:
        # The innermost buffer that is open takes the code marker, so the words of the
        # post keep their order (Kody, PR 2340).  The name in the markup carries no at
        # sign, so the at sign in the result is the mention's own work:  the test fails
        # if the mention buffer or its place in the stack is broken.
        html = '<p><a href="https://x"><span class="user-mention">A<code>c</code></span></a></p>'
        self.assertEqual(zm.to_markdown(html), "[@A`c`](https://x)")

    def test_a_code_block_keeps_its_text_inside_the_fence(self) -> None:
        # The text of a code span inside a pre block stays in the pre block, so the fence
        # holds the code and the paragraph does not (Kody, PR 2340).
        html = '<div class="codehilite"><pre><span></span><code>print(1)\n</code></pre></div>'
        out = zm.to_markdown(html)
        self.assertEqual(out, "```\nprint(1)\n\n```")

    def test_an_image_inside_a_code_span_stays_in_the_code(self) -> None:
        # The code buffer wins over the link, so the placeholder stays in the code span
        # (Kody, PR 2340).
        html = '<p><a href="https://x"><code>a<img src="s" alt="p">b</code></a></p>'
        self.assertEqual(zm.to_markdown(html), "[`a[image: p]b`](https://x)")

    def test_an_emoji_inside_a_code_span_stays_in_the_code(self) -> None:
        html = '<p><a href="https://x"><code>a<span class="emoji" title="tada"></span>b</code></a></p>'
        self.assertEqual(zm.to_markdown(html), "[`a:tada:b`](https://x)")


class NestingTests(unittest.TestCase):
    """The buffer that is open takes the text, and the buffers close from the inside out."""

    def test_a_block_inside_an_embedded_message_keeps_the_link(self) -> None:
        # A block tag read inside an embedded message keeps the link open, so the URL
        # of the sender is not lost (Kody, PR 2340).
        html = '<p><a href="https://x"><div class="message_embed"><p>quoted</p></div></a></p>'
        self.assertEqual(zm.to_markdown(html), "[\\[embedded content\\]](https://x)")

    def test_an_embedded_message_keeps_its_paragraph_whole(self) -> None:
        # The paragraph does not split around the embedded message (Kody, PR 2340).
        html = '<p>Hi <a href="https://x">L<div class="message_embed">P</div></a></p>'
        self.assertEqual(zm.to_markdown(html), "Hi [L\\[embedded content\\]](https://x)")

    def test_the_content_of_an_embedded_message_is_dropped(self) -> None:
        # The content of an embedded message never reaches the comment:  the placeholder
        # names it (Kody, PR 2340).
        html = "<ul><li><div class=\"message_embed\">P</div></li></ul>"
        self.assertEqual(zm.to_markdown(html), "- \\[embedded content\\]")

    def test_a_mention_closes_with_the_paragraph_an_embedded_message_breaks(self) -> None:
        # Only a link stays open for the embedded message:  a mention closes with the
        # paragraph it is in (Kody, PR 2340).
        html = '<p><span class="user-mention">@A</span><div class="message_embed">E</div></p>'
        self.assertEqual(zm.to_markdown(html), "@A\\[embedded content\\]")

    def test_an_unbalanced_mention_inside_a_link_closes_with_it(self) -> None:
        # The buffers a link holds close with it, so the text after the end tag lands
        # outside the link (Kody, PR 2340).
        html = '<p><a href="https://x">L<span class="user-mention">@A</a> tail</p>'
        self.assertEqual(zm.to_markdown(html), "[L@A](https://x) tail")

    def test_a_link_inside_a_mention_keeps_both(self) -> None:
        # The innermost buffer that is open takes the text, so the mention keeps its
        # name and the link keeps its URL (Kody, PR 2340).
        html = '<p><span class="user-mention">@A<a href="https://x">B</a></span></p>'
        self.assertEqual(zm.to_markdown(html), "@A[B](https://x)")

    def test_a_mention_whose_name_has_no_at_sign_gets_one(self) -> None:
        out = zm.to_markdown('<p><span class="user-mention">Alice</span></p>')
        self.assertEqual(out, "@Alice")

    def test_the_edges_of_a_mention_are_trimmed(self) -> None:
        out = zm.to_markdown('<p><span class="user-mention">  Alice  </span></p>')
        self.assertEqual(out, "@Alice")

    def test_an_embedded_placeholder_keeps_its_brackets_escaped(self) -> None:
        # The brackets of the placeholder are escaped, so a link that holds one shows as
        # a link and never as a live one (Kody, PR 2340).
        html = '<p><a href="https://x"><div class="message_embed">P</div></a></p>'
        out = zm.to_markdown(html)
        self.assertEqual(out, "[\\[embedded content\\]](https://x)")


class CodeTests(unittest.TestCase):
    """The code of a post is shown as the sender wrote it."""

    def test_a_fence_of_four_backticks_is_not_closed_by_three(self) -> None:
        # The whole run opens the fence:  a fence of four backticks is closed by four
        # backticks and not by three (Kody, PR 2340).
        out = zm.to_markdown("````\n```\nstill code\n````\nafter")
        self.assertTrue(out.endswith("after"), out)  # the fence closed on the four
        self.assertNotIn("\n```\nafter", out)  # and not on the three

    def test_the_code_of_a_post_is_piece_of_its_own(self) -> None:
        pieces = zm.split_code("prose `code` prose\n```\nblock\n```\nend")
        kinds = [is_code for is_code, _chunk in pieces]
        self.assertTrue(any(kinds))
        self.assertTrue(not all(kinds))
        code = "".join(chunk for is_code, chunk in pieces if is_code)
        self.assertIn("`code`", code)
        self.assertIn("block", code)
        self.assertNotIn("prose", code)

    def test_the_pieces_are_the_text_itself(self) -> None:
        # The line breaks are kept:  the pieces are the text, cut in two.
        text = "before\n```\ninside\n```\nafter"
        pieces = zm.split_code(text)
        self.assertEqual("".join(chunk for _is_code, chunk in pieces), text)
        code = "".join(chunk for is_code, chunk in pieces if is_code)
        prose = "".join(chunk for is_code, chunk in pieces if not is_code)
        self.assertIn("inside", code)
        self.assertNotIn("inside", prose)
        self.assertIn("before", prose)


class ListTests(unittest.TestCase):
    """The lists of a post keep their text and their shape."""

    def test_a_quoted_list_is_quoted_once(self) -> None:
        out = zm.to_markdown("<blockquote><ul><li>a</li></ul></blockquote>")
        self.assertEqual(out, "> - a")

    def test_a_nested_list_under_an_ordered_item_is_indented_to_nest(self) -> None:
        # The indent follows the width of the marker the nested list sits under, so the
        # list nests under "1. " as well as under "- " (Kody, PR 2340).
        out = zm.to_markdown("<ol><li>one<ol><li>a</li></ol></li></ol>")
        self.assertEqual(out, "1. one\n    1. a")

    def test_a_nested_list_under_a_bullet_is_indented_to_nest(self) -> None:
        out = zm.to_markdown("<ul><li>one<ul><li>a</li></ul></li></ul>")
        self.assertEqual(out, "- one\n   - a")


class ShapeTests(unittest.TestCase):
    """The shape of a post is read from its start, and the reader keeps what it finds."""

    def test_a_rendered_table_is_read_as_html(self) -> None:
        out = zm.to_markdown("<table><tr><td>cell</td></tr></table>")
        self.assertEqual(out, "cell")

    def test_a_source_post_that_opens_with_a_tag_keeps_its_text(self) -> None:
        # A source post that opens with a tag and never closes it is the sender's own
        # text, and keeps it (Kody, PR 2340).
        out = zm.to_markdown("<p> is a paragraph tag")
        self.assertIn("\\<p>", out)
        self.assertIn("is a paragraph tag", out)

    def test_an_unclosed_inline_element_does_not_swallow_its_text(self) -> None:
        # A tag the sender never closed sends its text into a buffer that is never
        # emptied; the text is put back where a reader can see it (Kody, PR 2340).
        out = zm.to_markdown('<p>text with an <a href="https://x">unclosed link')
        self.assertIn("unclosed link", out)

    def test_the_code_inside_a_list_item_is_code(self) -> None:
        out = zm.to_markdown("<ul><li>run <code>make test</code> now</li></ul>")
        self.assertEqual(out, "- run `make test` now")

    def test_the_text_after_a_nested_list_starts_on_its_own_line(self) -> None:
        out = zm.to_markdown("<ul><li>one<ul><li>a</li></ul>tail</li></ul>")
        self.assertEqual(out, "- one\n   - a\n  tail")


class UnclosedTests(unittest.TestCase):
    """What a post leaves open keeps its text."""

    def test_a_post_of_void_elements_is_read_as_html(self) -> None:
        # A void element is closed by itself, so a post of them alone has no closing
        # tag and is still rendered HTML (Kody, PR 2340).
        out = zm.to_markdown('<img src="x" alt="pic">')
        self.assertEqual(out, "\\[image: pic\\]")

    def test_the_text_of_an_unclosed_link_keeps_its_place(self) -> None:
        # The text of a link the sender never closed stays where the sender wrote it,
        # before the blocks that follow it (Kody, PR 2340).
        out = zm.to_markdown('<p>first <a href="https://x">second<ul><li>item</li></ul>')
        self.assertTrue(out.startswith("first "), out)
        self.assertIn("[second](https://x)", out)
        self.assertLess(out.index("second"), out.index("item"))

    def test_an_unclosed_list_keeps_its_items(self) -> None:
        out = zm.to_markdown("<ul><li>one</li><li>two")
        self.assertEqual(out, "- one\n- two")

    def test_a_code_span_inside_an_unclosed_link_keeps_the_url(self) -> None:
        # The innermost element closes first, so the code's text lands in the link and
        # the link keeps its URL (Kody, PR 2340).
        out = zm.to_markdown('<p>see <a href="https://example.com/x">the <code>docs</p>')
        self.assertEqual(out, "see [the `docs`](https://example.com/x)")

    def test_an_unclosed_link_in_a_list_item_keeps_its_url(self) -> None:
        out = zm.to_markdown('<ul><li>see <a href="https://example.com/x">the docs</li></ul>')
        self.assertIn("the docs", out)
        self.assertIn("https://example.com/x", out)
        self.assertIn("- see", out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
