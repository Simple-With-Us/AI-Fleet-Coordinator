#!/usr/bin/env python3
"""Zulip's message content as readable Markdown.

Zulip hands a message over in two shapes:  the rendered HTML a browser sees, and the
Markdown source the sender typed.  Both arrive at the fleet relay (the event queue and
the topic history are both read with `apply_markdown=false`, so the source is what the
relay sees; a host that reads the messages API with its default gets the HTML), and
this module turns either one into Markdown a reader can follow:  the post keeps its
paragraphs, its lists, its quotes, its code and its links instead of collapsing into one
line of escaped punctuation.

The conversion is text to text, and it never fetches anything:  the standard library's
HTML parser does the work, and the module has no dependency outside it.

The result is Markdown, not a finished comment body.  A caller that puts it where
untrusted text could act on a reader (a Linear comment) still has to defang it:  the
relay owns that pass, in `linear_relay`.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser

# The characters that would otherwise open a link, start a code span, or hide the rest
# of a comment behind an HTML comment.  They are escaped with a backslash, which a
# Markdown renderer drops, so the reader sees the character and not the markup.  No
# credential carries one of them, so the escaping cannot hide a credential from the
# scanner that runs over the converted text.
_TEXT_SPECIALS = "\\`[]<"

# The kinds of part the typed buffers hold.  A text part is escaped once, when the
# buffer that holds it closes or when it lands in the prose;  a code part keeps the
# markers of a code span;  a raw part is markup the converter itself generates, such as
# the brackets of a placeholder, kept as it is (Kody, PR 2340).
_PART_TEXT = "text"
_PART_CODE = "code"
_PART_RAW = "raw"

# The signature of Zulip's rendered HTML, used to tell the two shapes apart.  It names
# the tags and attributes Zulip's own renderer emits, so a post that merely talks about
# HTML is not mistaken for HTML.
_HTML_SIGNATURE = re.compile(
    r"<(?:p[ >]|/p>|br[ />]|div[ >]|div class=|span[ >]|span class=|/div>|/span>|a href=|pre[ >]"
    r"|blockquote[ >]|/blockquote>|ul[ >]|ol[ >]|li[ >]|h[1-6][ >]|img[ >]|hr[ >]"
    r"|table[ >]|/table>|section[ >]|/section>|article[ >]|/article>|thead[ >]|/thead>"
    r"|tbody[ >]|/tbody>|tr[ >]|/tr>)"
)

# A rendered post closes what it opens, so a closing tag is what tells the two shapes
# apart when a source post opens with a tag of its own (Kody, PR 2340).
_HTML_CLOSER = re.compile(
    r"</(?:p|div|span|a|pre|code|blockquote|ul|ol|li|h[1-6]|img|hr|table|section|article"
    r"|thead|tbody|tr|br)>"
)

# The void elements, the ones that are closed by themselves:  a post of them alone has no
# closing tag, and it is still rendered HTML (Kody, PR 2340).
_HTML_VOID = re.compile(r"<(?:br|img|hr)[ />]")

# A Markdown link in the source:  the text, then the target in parentheses.
_LINK = re.compile(r"\[([^\]\n]*)\]\(\s*((?:[^()\s]|\([^()\s]*\))*)(?:\s+\"[^\"]*\")?\s*\)")

# A Zulip mention in the source:  the display name in bold, after the at sign.
_MENTION = re.compile(r"@\*\*([^*\n]+)\*\*")

# A run of backticks, the fence a code span or a code block is written with.
_BACKTICKS = re.compile(r"`+")


def to_markdown(content: str) -> str:
    """One Zulip post as Markdown:  its paragraphs, its lists, its code and its links,
    with its line structure kept.

    The input may be Zulip's rendered HTML or the Markdown source the sender typed.
    The shape is detected and each one has its own reader.  The result is Markdown:  a
    caller that embeds it where untrusted text could act on a reader still has to
    defang it.
    """
    text = content or ""
    if not text.strip():
        return ""
    if _looks_like_html(text):
        return _html_to_markdown(text)
    return _source_to_markdown(text)


def _looks_like_html(text: str) -> bool:
    """Whether the content is Zulip's rendered HTML rather than the sender's own source.

    The shape is read from the start of the post and not from a match anywhere in it:  a
    source post that talks about markup ("a <p> tag") or shows it inside a code block
    keeps its own text, and only a post that opens with a tag is read as HTML (Kody, PR
    2340)."""
    stripped = text.lstrip()
    if stripped.startswith("```") or stripped.startswith("~~~"):
        return False  # a code block:  its body may hold markup
    if not _HTML_SIGNATURE.match(stripped):
        return False  # it does not open with a tag the reader knows
    if _HTML_VOID.match(stripped):
        return True  # a void element is closed by itself
    return bool(_HTML_CLOSER.search(text))  # and it closes what it opens


def close_code_fence(text: str) -> str:
    """Close a code fence the text leaves open.

    A post that is cut in the middle of a fenced code block would otherwise render the
    rest of the comment as code, so the fence is closed where the text stops."""
    fence = ""
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped.startswith("```"):
            continue
        run = len(stripped) - len(stripped.lstrip("`"))
        if not fence:
            fence = "`" * run
        elif run >= len(fence):
            fence = ""
    if not fence:
        return text
    return "%s\n%s" % (text.rstrip("\n"), fence)


def split_code(text: str) -> list[tuple[bool, str]]:
    """The text in pieces:  (True, code) for a fenced block or a code span, and
    (False, prose) for everything else.

    A reader sees code as the sender wrote it, so whatever a caller does to the prose of
    a post (escaping a link opener, breaking a live mention) must not touch the code
    (Kody, PR 2340)."""
    pieces: list[tuple[bool, str]] = []
    fence = ""
    # The line breaks are kept:  the pieces are the text itself, cut in two.
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if fence:
            pieces.append((True, line))
            if stripped.startswith(fence):
                fence = ""
            continue
        if stripped.startswith("```"):
            fence = "`" * (len(stripped) - len(stripped.lstrip("`")))
            pieces.append((True, line))
            continue
        pieces.extend(_split_spans(line))
    return pieces


def _split_spans(line: str) -> list[tuple[bool, str]]:
    """One line in pieces:  the code spans it holds and the prose around them."""
    pieces: list[tuple[bool, str]] = []
    prose: list[str] = []
    index = 0
    length = len(line)
    while index < length:
        char = line[index]
        if char != "`":
            prose.append(char)
            index += 1
            continue
        run = 1
        while index + run < length and line[index + run] == "`":
            run += 1
        marker = "`" * run
        close = _closing_run(line, marker, index + run)
        if close == -1:
            prose.append(line[index : index + run])  # an unclosed run:  prose, not code
            index += run
            continue
        if prose:
            pieces.append((False, "".join(prose)))
            prose = []
        pieces.append((True, line[index : close + run]))
        index = close + run
    if prose:
        pieces.append((False, "".join(prose)))
    return pieces


def _escape_text(text: str) -> str:
    """Text as Markdown text:  only the characters that would change the shape of the
    document are escaped, and the reader still sees them as they are."""
    return "".join("\\" + ch if ch in _TEXT_SPECIALS else ch for ch in text)


def _escape_url(url: str) -> str:
    """A URL as a Markdown link target:  its own markdown specials escaped, so it cannot
    close the link early and become a link of its own."""
    text = (url or "").strip()
    text = text.replace("\r", "").replace("\n", "")
    text = text.replace("\\", "\\\\")
    text = text.replace("(", "\\(").replace(")", "\\)")
    text = text.replace("<", "\\<").replace(">", "\\>")
    return text.replace(" ", "%20")


def _strip_control(value: str) -> str:
    """An attribute value without the characters that would break the line."""
    return "".join(ch for ch in (value or "") if ch not in "\r\n\t")


def _plain_attribute(value: str) -> str:
    """An attribute value as plain text:  no line break, and no character that would
    change the shape of the comment.

    The attributes of a rendered post are the sender's own text and a sender can put
    anything in them, so they are escaped the way the rest of the text is (Kody, PR
    2340)."""
    return _escape_text(_strip_control(value))


def _indent(text: str, width: int = 2) -> str:
    """A block indented under the line above it, so a nested list reads as nested."""
    pad = " " * width
    return "\n".join(pad + line if line else line for line in text.split("\n"))


def _fence(code: str) -> str:
    """A code block as a fenced block, with a fence longer than any run of backticks
    inside it, so the code cannot close the fence early."""
    runs = [len(run) for run in _BACKTICKS.findall(code)]
    marker = "`" * max(3, (max(runs) if runs else 0) + 1)
    return "%s\n%s\n%s" % (marker, code, marker)


def _inline_code(text: str) -> str:
    """A code span, with a marker longer than any run of backticks inside it."""
    runs = [len(run) for run in _BACKTICKS.findall(text)]
    longest = max(runs) if runs else 0
    marker = "`" * max(1, longest + 1)
    if text.startswith("`") or text.endswith("`"):
        marker = "`" * (longest + 2)
    return "%s%s%s" % (marker, text, marker)


class _Typed:
    """One typed buffer:  a mention or a link, and the parts it holds."""

    __slots__ = ("href", "kind", "parts")

    def __init__(self, kind: str, href: str = "") -> None:
        self.kind = kind
        self.parts: list[tuple[str, str]] = []
        self.href = href


class _List:
    """One list being read:  its finished items and the item being read."""

    def __init__(self, ordered: bool) -> None:
        self.ordered = ordered
        self.items: list[str] = []
        self.item: list[str] | None = None
        self.marker = 2  # the width of the marker of the item being read


class _Converter(HTMLParser):
    """Zulip's rendered HTML as Markdown blocks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.buf: list[str] = []
        self.pre: list[str] | None = None
        self.code: list[str] | None = None
        self.typed: list[_Typed] = []
        self.embed: list[str] | None = None
        self.embed_url = ""
        self.lists: list[_List] = []
        self.quote = 0

    # -- the block and the inline buffers ------------------------------------------------
    def _sink(self) -> list[str]:
        """The plain buffer the text belongs to:  the code, the pre block, the item of a
        list, the embedded content, or the paragraph.

        The typed buffers (a mention and a link) are not here:  they hold parts, and the
        text that goes in them is escaped when they close (Kody, PR 2340)."""
        if self.code is not None:
            return self.code
        if self.pre is not None:
            return self.pre
        if self.lists and self.lists[-1].item is not None:
            return self.lists[-1].item
        if self.embed is not None:
            return self.embed
        return self.buf

    def _parts(self) -> list[tuple[str, str]] | None:
        """The typed buffer the text belongs to:  the innermost one that is open,  whether
        it is a mention or a link (Kody, PR 2340)."""
        return self.typed[-1].parts if self.typed else None

    def _open_typed(self, kind: str, href: str = "") -> None:
        """Open a typed buffer:  a mention or a link."""
        self.typed.append(_Typed(kind, href))

    def _append_part(self, text: str, kind: str = _PART_TEXT) -> None:
        """Put a part of text where it belongs.

        Code is code:  it is never escaped.  A part that goes into a typed buffer is kept
        as it is and escaped when that buffer closes,  so every part is escaped exactly
        once;  a part that lands in the prose is escaped here.  A raw part is markup the
        converter itself generates and is left as it is (Kody, PR 2340)."""
        if self.code is not None or self.pre is not None:
            self._sink().append(text)  # code is code:  it is not markdown
            return
        if self.embed is not None:
            # The content of an embedded message is dropped:  the placeholder names it.
            self._sink().append(_escape_text(text) if kind == _PART_TEXT else text)
            return
        parts = self._parts()
        if parts is not None:
            parts.append((text, kind))
            return
        self._sink().append(_escape_text(text) if kind == _PART_TEXT else text)

    def _link_text(self, parts: list[tuple[str, str]]) -> str:
        """The text of a link:  its text parts escaped once, and its code and raw parts
        left as they are, so a code span inside a link stays a code span (Kody, PR
        2340)."""
        text = "".join(_escape_text(part) if kind == _PART_TEXT else part for part, kind in parts)
        return text.strip()

    def _append_text(self, text: str) -> None:
        """Put text where it belongs:  it is escaped exactly once (Kody, PR 2340)."""
        self._append_part(text, _PART_TEXT)

    def _append_code(self, marker: str) -> None:
        """Put a code span where it belongs:  its markers are never escaped (Kody, PR
        2340)."""
        self._append_part(marker, _PART_CODE)

    def _close_code(self) -> None:
        """Close an open code span:  its text becomes a code span, and inside a link it
        keeps its markers (Kody, PR 2340)."""
        if self.code is None:
            return
        text = "".join(self.code)
        self.code = None
        if text:
            self._append_code(_inline_code(text))

    def _close_mention(self, parts: list[tuple[str, str]]) -> None:
        """Close a mention:  its name becomes plain text, and the parts it holds move to
        the buffer that is open now, keeping their kind, so the name is escaped exactly
        once and a code span inside it keeps its markers (Kody, PR 2340)."""
        if not parts:
            return
        # The edges of the name are trimmed on its text parts, so a code span at the
        # edge of a mention is not cut in half (Kody, PR 2340).
        texts = [index for index, (_part, kind) in enumerate(parts) if kind == _PART_TEXT]
        if texts:
            head_index = texts[0]
            tail_index = texts[-1]
            if head_index == tail_index:
                head, kind = parts[head_index]
                parts[head_index] = (head.strip(), kind)
            else:
                head, kind = parts[head_index]
                parts[head_index] = (head.lstrip(), kind)
                tail, kind = parts[tail_index]
                parts[tail_index] = (tail.rstrip(), kind)
        name = "".join(text for text, _kind in parts).strip()
        if not name:
            return
        if not name.startswith("@"):
            if texts:
                head_index = texts[0]
                head, kind = parts[head_index]
                parts[head_index] = ("@" + head, kind)
            else:
                parts.insert(0, ("@", _PART_TEXT))
        for text, kind in parts:
            self._append_part(text, kind)

    def _close_typed(self) -> None:
        """Close the innermost typed buffer and put its text where it belongs."""
        if not self.typed:
            return
        buffer = self.typed.pop()
        if buffer.kind == "mention":
            self._close_mention(buffer.parts)
            return
        text = self._link_text(buffer.parts)
        if not text or self.embed is not None:
            return
        # The label is finished Markdown:  it is kept as it is wherever it lands.
        self._append_part("[%s](%s)" % (text, _escape_url(buffer.href)), _PART_RAW)

    def _close_inline(self) -> None:
        """Close the open inline elements into the place their text belongs.

        The innermost element closes first, so the text of a code span inside a link lands
        in the link and the link keeps its URL (Kody, PR 2340)."""
        self._close_code()
        while self.typed:
            self._close_typed()

    def _close_list(self) -> None:
        """Close the innermost open list:  its items become blocks.

        A list the sender never closed keeps its items in a buffer that is never
        emptied, and the post would lose them (Kody, PR 2340)."""
        if not self.lists:
            return
        self._close_inline()
        ctx = self.lists[-1]
        if ctx.item is not None:
            text = "".join(ctx.item).strip()
            ctx.item = None
            marker = "%d. " % (len(ctx.items) + 1) if ctx.ordered else "- "
            ctx.items.append(marker + text)
        items = ctx.items
        self.lists.pop()
        if not items:
            return
        rendered = "\n".join(items)
        if self.lists and self.lists[-1].item is not None:
            # A nested list belongs under the item that holds it, and is indented by
            # the width of that item's marker, so it reads as part of it whether the
            # marker is "- " or "1. " (Kody, PR 2340).
            width = self.lists[-1].marker + 1
            parent = " " * self.lists[-1].marker
            # The text that follows the nested list starts on a line of its own, at
            # the indent of the item that holds it (Kody, PR 2340).
            self.lists[-1].item.append("\n" + _indent(rendered, width) + "\n" + parent)
        else:
            self.blocks.append(self._quoted(rendered))

    def _flush(self, keep_inline: bool = False) -> None:
        """End the paragraph being read.

        The inline elements close with it,  unless the caller keeps them open:  a link
        that holds an embedded link keeps it,  so the URL of the sender is still shown
        (Kody, PR 2340)."""
        if not keep_inline:
            self._close_inline()
        if not self.buf:
            return
        block = "".join(self.buf).strip()
        self.buf = []
        if block:
            self.blocks.append(self._quoted(block))

    def _quoted(self, block: str) -> str:
        """A block inside a quote, with the quote marker on each of its lines."""
        if not self.quote:
            return block
        return "\n".join("> " + line for line in block.split("\n"))

    # -- the tags ------------------------------------------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrd = {key: (value or "") for key, value in attrs}
        classes = attrd.get("class", "")
        if tag == "div" and "codehilite" in classes:
            self._flush()
            self.pre = []
            return
        if tag == "div" and "message_embed" in classes:
            self._flush(keep_inline=True)
            self.embed = []
            self.embed_url = ""
            return
        if tag in ("p", "div", "section", "article", "table", "tr", "thead", "tbody"):
            self._flush()
            return
        if tag == "br":
            self.buf.append("\n")
            return
        if tag == "pre":
            self._flush()
            self.pre = []
            return
        if tag == "code":
            if self.pre is None:
                self.code = []
            return
        if tag == "a":
            if self.embed is not None and not self.embed_url:
                self.embed_url = attrd.get("href", "")
            self._open_typed("link", attrd.get("href", ""))
            return
        if tag == "span":
            if "user-mention" in classes or "user-group-mention" in classes:
                self._open_typed("mention")
                return
            if "emoji" in classes:
                name = _strip_control(self._emoji_name(attrd, classes))
                self._append_part(":%s:" % name, _PART_TEXT)
                return
            return
        if tag == "blockquote":
            self._flush()
            self.quote += 1
            return
        if tag in ("ul", "ol"):
            self._flush()
            self.lists.append(_List(tag == "ol"))
            return
        if tag == "li":
            if not self.lists:
                self.lists.append(_List(False))
            ctx = self.lists[-1]
            ctx.item = []
            marker = "%d. " % (len(ctx.items) + 1) if ctx.ordered else "- "
            ctx.marker = len(marker)
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush()
            self.buf.append("%s " % ("#" * int(tag[1])))
            return
        if tag == "hr":
            self._flush()
            self.blocks.append(self._quoted("---"))
            return
        if tag == "img":
            alt = _strip_control(attrd.get("alt", "").strip())
            # The placeholder is one text part:  its brackets are escaped, so a link
            # that holds it still shows as a link and never as a live one (Kody, PR 2340).
            self._append_part("[image: %s]" % alt if alt else "[image]", _PART_TEXT)
            return

    def handle_endtag(self, tag: str) -> None:
        if tag == "pre":
            if self.pre is not None:
                code = "".join(self.pre)
                self.pre = None
                self.blocks.append(self._quoted(_fence(code)))
            return
        if tag == "code":
            self._close_code()
            return
        if tag == "a":
            # The link closes last:  a code span inside it lands in the text of the link
            # first, and the link keeps its URL.  The buffers it holds close with it,  so
            # the text after the end tag lands outside the link (Kody, PR 2340).
            self._close_code()
            index = next(
                (i for i in range(len(self.typed) - 1, -1, -1) if self.typed[i].kind == "link"),
                None,
            )
            if index is not None:
                while len(self.typed) > index:
                    self._close_typed()
            return
        if tag == "span":
            # Only a mention closes what is open:  the end of an emoji span says nothing
            # about the code span that may hold it.  The buffers the mention holds close
            # with it (Kody, PR 2340).
            index = next(
                (i for i in range(len(self.typed) - 1, -1, -1) if self.typed[i].kind == "mention"),
                None,
            )
            if index is not None:
                self._close_code()
                while len(self.typed) > index:
                    self._close_typed()
            return
        if tag == "div":
            if self.embed is not None:
                url = self.embed_url
                self.embed = None
                self.embed_url = ""
                if url:
                    self._append_part("[embedded link: %s]" % _strip_control(url), _PART_TEXT)
                else:
                    self._append_part("[embedded content]", _PART_TEXT)
            return
        if tag == "blockquote":
            self._flush()
            if self.quote:
                self.quote -= 1
            return
        if tag == "li":
            if not self.lists:
                return
            self._close_inline()  # the text of an open link stays in the item
            ctx = self.lists[-1]
            if ctx.item is None:
                return
            text = "".join(ctx.item).strip()
            ctx.item = None
            marker = "%d. " % (len(ctx.items) + 1) if ctx.ordered else "- "
            ctx.items.append(marker + text)
            return
        if tag in ("ul", "ol"):
            self._flush()
            self._close_list()
            return
        if tag in ("p", "tr", "table", "section", "article", "thead", "tbody"):
            self._flush()
            return

    def handle_data(self, data: str) -> None:
        if self.pre is not None or self.code is not None:
            self._sink().append(data)  # code is code: it is not markdown
            return
        # A text part is escaped once:  by the typed buffer that holds it when that
        # buffer closes,  or here when it lands in the prose (Kody, PR 2340).
        self._append_text(data)

    def _finish(self) -> None:
        """Close what the post left open.

        A tag the sender never closed sends its text into a buffer that is never
        emptied, and the post would read as empty;  the text is put back where a reader
        can see it (Kody, PR 2340)."""
        self._close_inline()
        while self.lists:
            self._close_list()
        self._close_inline()
        if self.pre is not None:
            code = "".join(self.pre)
            self.pre = None
            if code:
                self.blocks.append(self._quoted(_fence(code)))
        if self.embed is not None:
            url = self.embed_url
            self.embed = None
            self.embed_url = ""
            self.buf.append(
                "[embedded link: %s]" % _plain_attribute(url) if url else "[embedded content]"
            )

    def _emoji_name(self, attrd: dict[str, str], classes: str) -> str:
        """The name of an emoji span:  its title, else its label, else the id in its class."""
        name = attrd.get("title") or attrd.get("aria-label")
        if not name:
            for token in classes.split():
                if token.startswith("emoji-") and len(token) > len("emoji-"):
                    name = token[len("emoji-") :]
                    break
        return (name or "emoji").strip()


def _html_to_markdown(text: str) -> str:
    """Zulip's rendered HTML as Markdown."""
    parser = _Converter()
    parser.feed(text)
    parser.close()
    parser._finish()
    parser._flush()
    return "\n\n".join(parser.blocks).strip()


def _source_to_markdown(source: str) -> str:
    """Zulip's Markdown source as Markdown:  the same document, with its mentions in
    plain form and its unbalanced code fences closed."""
    out: list[str] = []
    fence: str | None = None
    for line in source.split("\n"):
        stripped = line.strip()
        if fence is None:
            if stripped.startswith("```"):
                # The whole run opens the fence:  a fence of four backticks is closed by
                # four backticks and not by three (Kody, PR 2340).
                fence = "`" * (len(stripped) - len(stripped.lstrip("`")))
                out.append(line)
                continue
            out.append(_source_line(line))
            continue
        out.append(line)  # inside a fence the text is code: it is kept as it is
        if stripped.startswith(fence):
            fence = None
    if fence is not None:
        out.append(fence)  # the source left the fence open: it is closed here
    return "\n".join(out)


def _source_line(line: str) -> str:
    """One line of Markdown source as Markdown:  its links kept as links, its code spans
    kept as code spans, and everything else escaped so it cannot change the shape of the
    document."""
    text = _MENTION.sub(r"@\1", line)
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char == "`":
            run = 1
            while index + run < length and text[index + run] == "`":
                run += 1
            marker = "`" * run
            close = _closing_run(text, marker, index + run)
            if close == -1:
                out.append("\\`" * run)  # an unclosed run: escaped, it swallows nothing
            else:
                out.append(marker + text[index + run : close] + marker)
                index = close + run
                continue
            index += run
            continue
        if char == "[":
            match = _LINK.match(text, index)
            if match:
                out.append("[%s](%s)" % (_escape_text(match.group(1)), _escape_url(match.group(2))))
                index = match.end()
                continue
            out.append("\\[")
            index += 1
            continue
        out.append("\\" + char if char in _TEXT_SPECIALS else char)
        index += 1
    return "".join(out)


def _closing_run(text: str, marker: str, start: int) -> int:
    """Where a run of backticks closes, or -1 when it never does.  A longer run is not a
    closer:  it is part of the code."""
    index = text.find(marker, start)
    while index != -1:
        after = index + len(marker)
        if after < len(text) and text[after] == "`":
            index = text.find(marker, after)
            continue
        return index
    return -1
