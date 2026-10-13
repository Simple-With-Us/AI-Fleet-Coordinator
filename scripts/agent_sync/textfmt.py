"""Outbound text fix-ups for what agent-sync sends to Zulip.

`sentence_gap` is a safety net, not a substitute for writing the gap:  authors still write two
spaces after a sentence, and Zulip's Markdown renderer then collapses them to one.  A real
U+00A0 plus an ASCII space survives, so the client swaps the one for the other on the way out.
It is applied to message content that this process is sending and never to anything it reads.

The rules, all of them conservative (a missed conversion leaves the text as written, a wrong one
would corrupt it):

  * After `.`, `!` or `?`, optionally followed by closing characters  " ' ” ’ ) ] * _
    two or more ASCII spaces that precede a non-space character on the same line become exactly
    U+00A0 plus one ASCII space.
  * Single spaces stay (`e.g. x`, `v1.2.3`), and so do trailing spaces at the end of a line (a
    Markdown hard break).
  * Fenced code blocks (``` and ~~~, indented or inside a list or quote, unclosed ones running to
    the end), inline code spans (a run of N backticks closes on a run of exactly N), Zulip math
    (`$$...$$`) and @-mentions are left alone.  So are the contents of ```quote and ```spoiler
    fences:  a quote block holds someone else's words.
  * A list marker such as `1.  item` is left alone:  U+00A0 after the marker would end the list.
  * Idempotent, and length-preserving for exactly two spaces (two characters become two
    characters; a longer run only gets shorter).  The 10,000-character cap and the secret scanner
    therefore reach the same verdict on the text before and after.

Python 3.11+, standard library only.
"""
from __future__ import annotations

import re

__all__ = ["NBSP", "GAP", "sentence_gap"]

NBSP = " "
GAP = NBSP + " "

# A sentence terminator, closing characters, then a run of ASCII spaces.  `[ ]` and not `\s`:  NBSP is
# whitespace to `\s`, and matching it would stop an existing gap from being left exactly as it is.
_GAP_RE = re.compile(r"(?P<end>[.!?][\"'”’)\]*_]*)(?P<spaces>[ ]{2,})(?=[^\s])")
_LIST_MARKER_PREFIX_RE = re.compile(r"[ \t>]*\d{1,9}")

# An opening fence may sit behind indentation, quote markers and list markers ("  - ```", "1. ```").
_FENCE_OPEN_RE = re.compile(
    r"(?P<prefix>(?:[ \t]*(?:>|[-+*]|\d{1,9}[.)]))*[ \t]*)(?P<fence>`{3,}|~{3,})(?P<info>.*)")
_FENCE_CLOSE_RE = re.compile(r"[ \t]*(?:>[ \t]*)*(?P<fence>`{3,}|~{3,})[ \t\r]*")

# Inline spans, found left to right so the earlier opener wins.  None of them may run across a blank
# line, and an opener with no closer is literal text that protects nothing.
_INLINE_RE = re.compile(
    r"(?P<code>(?<!`)(?P<ticks>`+)(?!`)(?:(?!\n[ \t]*\n).)+?(?<!`)(?P=ticks)(?!`))"
    r"|(?P<math>\$\$(?:(?!\n[ \t]*\n).)+?\$\$)"
    r"|(?P<mention>@_?\*{1,2}[^*\n]+\*{1,2})",
    re.DOTALL,
)

_MASK = ""


def _split_fences(text: str) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """(fenced, open) line-aligned ranges of `text`.  `fenced` covers every fenced block from its
    opening fence line through its closing fence line (or the end of the text when unclosed);
    `open` is the complement, the runs that can hold prose."""
    fenced: list[tuple[int, int]] = []
    prose: list[tuple[int, int]] = []
    pos = 0
    prose_start = 0
    fence_start = 0
    fence_char = ""
    fence_len = 0
    for line in text.split("\n"):
        end = pos + len(line) + 1  # past the newline (one past the end for the last line)
        if fence_char:
            closer = _FENCE_CLOSE_RE.fullmatch(line)
            if closer and closer.group("fence")[0] == fence_char and len(closer.group("fence")) >= fence_len:
                fenced.append((fence_start, min(end, len(text))))
                fence_char = ""
                prose_start = min(end, len(text))
        else:
            opener = _FENCE_OPEN_RE.fullmatch(line)
            if opener and not (opener.group("fence")[0] == "`" and "`" in opener.group("info")):
                if prose_start < pos:
                    prose.append((prose_start, pos))
                fence_start = pos
                fence_char = opener.group("fence")[0]
                fence_len = len(opener.group("fence"))
        pos = end
    if fence_char:
        fenced.append((fence_start, len(text)))
    elif prose_start < len(text):
        prose.append((prose_start, len(text)))
    return fenced, prose


def _protected(text: str) -> list[tuple[int, int]]:
    """Every range `sentence_gap` must not look inside."""
    fenced, prose = _split_fences(text)
    ranges = list(fenced)
    for start, end in prose:
        chunk = text[start:end]
        for match in _INLINE_RE.finditer(chunk):
            ranges.append((start + match.start(), start + match.end()))
    return ranges


def sentence_gap(text: str) -> str:
    """`text` with each ASCII-space sentence gap turned into U+00A0 plus one space.  See the module
    docstring for what is left alone."""
    if "  " not in text:
        return text
    masked = list(text)
    for start, end in _protected(text):
        for index in range(start, end):
            if masked[index] not in "\n\r":
                masked[index] = _MASK
    pieces: list[str] = []
    last = 0
    for match in _GAP_RE.finditer("".join(masked)):
        if match.group("end") == ".":
            line_start = text.rfind("\n", 0, match.start()) + 1
            if _LIST_MARKER_PREFIX_RE.fullmatch(text[line_start:match.start()]):
                continue
        pieces.append(text[last:match.start("spaces")])
        pieces.append(GAP)
        last = match.end("spaces")
    if not pieces:
        return text
    pieces.append(text[last:])
    return "".join(pieces)
