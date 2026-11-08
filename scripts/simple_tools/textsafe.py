"""Untrusted text, made inert before a fleet component echoes it into Zulip.

Linear titles, PR titles, branch names, lane names and the words of other people are data, never
instructions, and some of them are written by someone who wants a message to do something it should
not:  wake every seat (`@**all**`), open a poll or to-do widget (`/poll`), send the reader to a link
(`[click](https://...)`), or break out of the quoting around them (a backtick).  `render_untrusted`
turns such text into one short line inside an inline code span, where Zulip's Markdown renders
nothing, and also removes the pieces that would still act if a caller later pasted the result
somewhere the code span does not protect.

    render_untrusted("@**all** [fix](https://x.test/p) `rm -rf ~`", 120)
    -> "`＠**all** fix https[:]//x.test/p rm -rf ~`"

What it does, in order:

  1. Control characters, invisible format characters (zero-width and joiner characters, the bidi
     overrides and isolates, the byte-order mark) and the Unicode line and paragraph separators are
     removed or, for the separators, treated as whitespace.  All whitespace, newlines and tabs
     included, collapses to a single space, so the result is one line.
  2. Every backtick is deleted, so the text cannot close its own code span.
  3. A Markdown link or image, `[text](url)` or `![alt](url)`, becomes `text url`, and any `](` left
     over gets a space.
  4. Every `/` that starts `poll` or `todo` (any letter case, any number of slashes) is removed:
     these are the slash commands Zulip turns into widgets.
  5. `://` becomes `[:]//`, so no URL stays clickable or auto-linked.
  6. Every `@` becomes the full-width at sign U+FF20, which Zulip does not read as a mention.
  7. Steps 2 to 6 repeat until nothing changes, so a rewrite can never join pieces that an earlier
     step already looked past.
  8. The length is capped, with an ellipsis, and the result is wrapped in one inline code span.

The cap is on the whole result, backticks included, so a caller that budgets a message can rely on
it.  With `code_span=False` the text is returned bare (for a place that renders no Markdown, such as
a topic name) and the cap is on the bare text.  Nothing is normalized (no NFKC), because that would
turn the full-width at sign back into a real one.  The function is idempotent, and total:  anything
that is not text is turned into text first (`None` is empty).

Python 3.11+, standard library only, and no imports from this package:  the secret scanner and the
routine-text checker load their siblings by path.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any

__all__ = ["render_untrusted", "FULLWIDTH_AT", "ELLIPSIS", "EMPTY"]

FULLWIDTH_AT = "\uff20"
ELLIPSIS = "\u2026"
EMPTY = "(empty)"

# Controls (Cc), invisible format characters (Cf:  zero-width, joiners, bidi overrides and isolates, the
# byte-order mark, the soft hyphen), surrogates (Cs) and private use (Co) are dropped.  Every kind of
# space and line break (str.isspace:  newline, tab, NBSP, the Unicode line and paragraph separators) is
# whitespace, which collapses to one space.
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co"})
_WIDGET_SLASH_RE = re.compile(r"/+(?=(?:poll|todo)(?![a-z]))", re.IGNORECASE)
_LINK_RE = re.compile(r"!?\[([^\]\[]*)\]\(([^()\s]*)(?:\s+(?:\"[^\"]*\"|'[^']*'))?\)")
_MAX_PASSES = 8


def _clean(text: str) -> str:
    kept: list[str] = []
    for char in text:
        if char.isspace():
            kept.append(" ")
        elif unicodedata.category(char) not in _DROPPED_CATEGORIES:
            kept.append(char)
    return " ".join("".join(kept).split())


def _once(text: str) -> str:
    """One pass of every rewrite.  Backticks go first, so removing one cannot join `:` and `//` (or `/`
    and `poll`) into something the later steps already looked past."""
    text = text.replace("`", "")
    while True:  # a link nested inside another link's text is exposed once the inner one is gone
        flat = _LINK_RE.sub(lambda m: (m.group(1) + " " + m.group(2)).strip(), text)
        if flat == text:
            break
        text = flat
    text = text.replace("](", "] (")
    text = _WIDGET_SLASH_RE.sub("", text)
    text = text.replace("://", "[:]//")
    text = text.replace("@", FULLWIDTH_AT)
    return " ".join(text.split())


def render_untrusted(s: Any, cap: int = 200, *, code_span: bool = True) -> str:
    """`s` as one inert line.  See the module docstring.  `cap` is the most characters returned:  at
    least 3 with a code span (the two backticks and one character), at least 1 without."""
    floor = 3 if code_span else 1
    if not isinstance(cap, int) or isinstance(cap, bool) or cap < floor:
        raise ValueError("cap must be a whole number of at least %d" % floor)
    text = _clean("" if s is None else (s if isinstance(s, str) else str(s)))
    for _ in range(_MAX_PASSES):  # a fixed point, so the output is its own input (idempotent)
        again = _once(text)
        if again == text:
            break
        text = again
    room = cap - 2 if code_span else cap
    if not text:
        text = EMPTY[:room]
    elif len(text) > room:
        text = (text[: room - 1].rstrip() + ELLIPSIS) if room > 1 else ELLIPSIS
    return "`%s`" % text if code_span else text
