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

# The signature of Zulip's rendered HTML, used to tell the two shapes apart.  It names
# the tags and attributes Zulip's own renderer emits, so a post that merely talks about
# HTML is not mistaken for HTML.
_HTML_SIGNATURE = re.compile(
    r"<(?:p[ >]|/p>|br[ />]|div class=|span class=|/div>|/span>|a href=|pre[ >]"
    r"|blockquote[ >]|/blockquote>|ul[ >]|ol[ >]|li[ >]|h[1-6][ >]|img[ >]|hr[ >])"
)

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
    if _HTML_SIGNATURE.search(text):
        return _html_to_markdown(text)
    return _source_to_markdown(text)


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


def _escape_text(text: str) -> str:
    """Text as Markdown text:  only the characters that would change the shape of the
    document are escaped, and the reader still sees them as they are."""
    return "".join("\\" + ch if ch in _TEXT_SPECIALS else ch for ch in text)


def _escape_url(url: str) -> str:
    """A URL as a Markdown link target:  its own markdown specials escaped, so it cannot
    close the link early and become a link of its own."""
    text = (url or "").strip()
    text = text.replace("\\", "\\\\")
    text = text.replace("(", "\\(").replace(")", "\\)")
    text = text.replace("<", "\\<").replace(">", "\\>")
    return text.replace(" ", "%20")


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


class _Converter(HTMLParser):
    """Zulip's rendered HTML as Markdown blocks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.buf: list[str] = []
        self.pre: list[str] | None = None
        self.code: list[str] | None = None
        self.link: list[str] | None = None
        self.href = ""
        self.mention: list[str] | None = None
        self.embed: list[str] | None = None
        self.embed_url = ""
        self.item: list[str] | None = None
        self.items: list[str] | None = None
        self.ordered = False
        self.quote = 0

    # -- the block and the inline buffers ------------------------------------------------
    def _sink(self) -> list[str]:
        """Where the text of the element being read goes."""
        if self.item is not None:
            return self.item
        if self.embed is not None:
            return self.embed
        if self.mention is not None:
            return self.mention
        if self.link is not None:
            return self.link
        if self.code is not None:
            return self.code
        if self.pre is not None:
            return self.pre
        return self.buf

    def _flush(self) -> None:
        """End the paragraph being read."""
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
            self._flush()
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
            self.link = []
            self.href = attrd.get("href", "")
            return
        if tag == "span":
            if "user-mention" in classes or "user-group-mention" in classes:
                self.mention = []
                return
            if "emoji" in classes:
                self.buf.append(":%s:" % self._emoji_name(attrd, classes))
                return
            return
        if tag == "blockquote":
            self._flush()
            self.quote += 1
            return
        if tag in ("ul", "ol"):
            self._flush()
            self.items = []
            self.ordered = tag == "ol"
            return
        if tag == "li":
            if self.items is None:
                self.items = []
            self.item = []
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
            alt = attrd.get("alt", "").strip()
            self.buf.append("[image: %s]" % alt if alt else "[image]")
            return

    def handle_endtag(self, tag: str) -> None:
        if tag == "pre":
            if self.pre is not None:
                code = "".join(self.pre)
                self.pre = None
                self.blocks.append(self._quoted(_fence(code)))
            return
        if tag == "code":
            if self.code is not None:
                text = "".join(self.code)
                self.code = None
                self.buf.append(_inline_code(text))
            return
        if tag == "a":
            if self.link is not None:
                text = "".join(self.link).strip()
                self.link = None
                if self.embed is None:
                    self.buf.append("[%s](%s)" % (_escape_text(text), _escape_url(self.href)))
            return
        if tag == "span":
            if self.mention is not None:
                name = "".join(self.mention).strip()
                self.mention = None
                if not name.startswith("@"):
                    name = "@" + name
                self.buf.append(name)
            return
        if tag == "div":
            if self.embed is not None:
                url = self.embed_url
                self.embed = None
                self.embed_url = ""
                self.buf.append("[embedded link: %s]" % url if url else "[embedded content]")
            return
        if tag == "blockquote":
            self._flush()
            if self.quote:
                self.quote -= 1
            return
        if tag == "li":
            if self.item is not None:
                text = "".join(self.item).strip()
                self.item = None
                if self.items is not None:
                    marker = "%d. " % (len(self.items) + 1) if self.ordered else "- "
                    self.items.append(self._quoted(marker + text))
            return
        if tag in ("ul", "ol"):
            self._flush()
            items = self.items or []
            self.items = None
            if items:
                self.blocks.append("\n".join(items))
            return
        if tag in ("p", "tr", "table", "section", "article", "thead", "tbody"):
            self._flush()
            return

    def handle_data(self, data: str) -> None:
        if self.pre is not None or self.code is not None:
            self._sink().append(data)  # code is code: it is not markdown
            return
        if self.mention is not None:
            self.mention.append(data)
            return
        self._sink().append(_escape_text(data))

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
                fence = stripped[:3]
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
