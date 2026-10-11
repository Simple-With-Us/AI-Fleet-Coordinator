"""The checks a digest DM must pass before `open-loops send` will send it.  Pure:  text and facts in, a list of
problems out (empty means it passes).  A problem names the rule and where, never quotes the text.

  * not empty, and under 4,000 characters (at most 3,999);
  * no `@**` or `@_**` (a hand-typed mention) and no line that starts a `/poll` or `/todo` widget;
  * every link is one the gatherer made (`facts["urls"]`);  a bare `www.` address or any other scheme counts
    as a link;
  * the F3 secret scanner finds nothing;
  * sentence gaps:  the text is run through agent-sync's own `sentence_gap` (two spaces become U+00A0 plus a
    space), and then any sentence that still ends in a single space outside code, links and fences is a problem;
  * the facts id is in the text (the duplicate check looks for it);
  * the full-list link is in the text;
  * "Work Not on GitHub Yet" is in the text when the facts have work that is not on GitHub;
  * "Missing:" is in the text when the facts have a source that could not be read.

`prepare(text)` returns the text agent-sync will actually send (gaps converted), which `send` sends as it is.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from fleet_facts import safety

from . import render

__all__ = ["MAX_CHARS", "prepare", "lint", "find_urls"]

MAX_CHARS = 3999
_URL = re.compile(r"(?i)(?:\b[a-z][a-z0-9+.-]{1,15}://|\bwww\.)[^\s<>\]\)\"'`]+")
_FENCE = re.compile(r"(?s)(```|~~~).*?(?:\1|\Z)")
_SPAN = re.compile(r"`[^`\n]*`")
_BOUNDARY = re.compile(r"(?<=[A-Za-z0-9)\]])[.!?][\"')\]*_]* (?=[A-Z])")
_ABBREVIATIONS = frozenset({"e.g", "i.e", "vs", "etc", "approx", "cf", "no", "mr", "mrs", "dr", "st", "inc", "jr", "sr", "fig"})
_WIDGET = re.compile(r"(?im)^\s*(?:>\s*)*/+(?:poll|todo)(?![a-z])")


def prepare(text: str) -> str:
    """`text` with every two-space sentence gap turned into U+00A0 plus a space (idempotent)."""
    return safety.textfmt().sentence_gap(text)


def find_urls(text: str) -> list[str]:
    return [m.group(0).rstrip(".,;:!?*_") for m in _URL.finditer(text)]


def _prose(text: str) -> str:
    text = _FENCE.sub(" ", text)
    text = _SPAN.sub(" ", text)
    return _URL.sub(" ", text)


def _single_space_boundaries(text: str) -> list[int]:
    """Line numbers of sentence ends followed by one ASCII space and a capital letter (prose only)."""
    lines = []
    prose = _prose(text)
    for match in _BOUNDARY.finditer(prose):
        before = prose[:match.start()]
        word = re.search(r"[A-Za-z0-9.]+$", before)
        token = (word.group(0).lower().rstrip(".") if word else "")
        if token in _ABBREVIATIONS or len(token) == 1 or token.isdigit():
            continue
        lines.append(prose.count("\n", 0, match.start()) + 1)
    return lines


def lint(text: str, facts: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    if not text.strip():
        return ["the DM is empty"]
    ready = prepare(text)
    if len(ready) > MAX_CHARS:
        problems.append("the DM is %d characters;  the limit is under 4,000 (at most %d)" % (len(ready), MAX_CHARS))
    if "@**" in ready or "@_**" in ready:
        problems.append("the DM has a hand-typed mention (@** or @_**);  names in the facts are already inert")
    if _WIDGET.search(ready):
        problems.append("a line starts a /poll or /todo widget")
    known = set(facts.get("urls") or [])
    for url in dict.fromkeys(find_urls(ready)):
        if url not in known:
            problems.append("a link the gatherer did not make:  %s" % url[:100])
    kind = safety.secretscan().scan(ready)
    if kind:
        problems.append("secret-shaped text (%s)" % kind)
    bad = _single_space_boundaries(ready)
    if bad:
        shown = ", ".join(str(n) for n in dict.fromkeys(bad[:5]))
        problems.append("%d sentence%s end%s in one space (line %s);  put two spaces after each sentence, "
                        "agent-sync turns them into U+00A0 plus a space" % (len(bad), "" if len(bad) == 1 else "s", "s" if len(bad) == 1 else "", shown))
    if facts.get("facts_id") not in ready:
        problems.append("the facts id %s is not in the DM (the duplicate check needs it)" % facts.get("facts_id"))
    if facts.get("full_list_url") not in ready:
        problems.append("the full-list link is not in the DM")
    off = (facts.get("not_on_github") or {}).get("count")
    if isinstance(off, int) and off > 0 and render.section_title(3).lower() not in ready.lower():
        problems.append("the facts have %d item%s not on GitHub but the DM has no \"%s\" section" % (off, "" if off == 1 else "s", render.section_title(3)))
    if facts.get("missing") and "Missing:" not in ready:
        problems.append("the facts have %d source%s that could not be read but the DM has no \"Missing:\" line" % (
            len(facts["missing"]), "" if len(facts["missing"]) == 1 else "s"))
    return problems
