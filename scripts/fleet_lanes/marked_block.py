"""marked_block: one named block of `#`-comment lines inside a text file (YAML or TOML) that a tool owns.

    # fleet:begin <name> (managed by <tool>)
    ...lines...
    # fleet:end <name>

The installers that edit a config they do not own (a cordis patch, a Kimi config.toml) put exactly one such
block per concern into it and touch nothing outside it.  Everything here is pure text in, text out.

Python 3.9 safe (the lane-tools stable copy runs under macOS's /usr/bin/python3).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

__all__ = ["BlockError", "BlockLoc", "find_block", "render_block", "upsert_block", "remove_block", "block_body"]

_BEGIN = re.compile(r"^[ \t]*# fleet:begin (?P<name>[A-Za-z0-9._-]+)(?: .*)?$")
_END = re.compile(r"^[ \t]*# fleet:end (?P<name>[A-Za-z0-9._-]+)[ \t]*$")


class BlockError(ValueError):
    """The markers in a file cannot be trusted, so the caller must refuse rather than guess."""


@dataclass(frozen=True)
class BlockLoc:
    begin: int      # index of the begin marker line in text.split("\n")
    end: int        # index of the end marker line


def find_block(text: str, name: str) -> Optional[BlockLoc]:
    """The block called `name`, or None.  Raises BlockError for a begin with no end, an end with no begin, an
    end before its begin, or the same name twice.  Blocks of other names are ignored."""
    begins = []
    ends = []
    for i, raw in enumerate(text.split("\n")):
        line = raw.rstrip("\r")
        m = _BEGIN.match(line)
        if m and m.group("name") == name:
            begins.append(i)
            continue
        m = _END.match(line)
        if m and m.group("name") == name:
            ends.append(i)
    if not begins and not ends:
        return None
    if len(begins) > 1 or len(ends) > 1:
        raise BlockError("found %d begin and %d end markers for %r; expected one block" % (len(begins), len(ends), name))
    if not begins:
        raise BlockError("end marker for %r on line %d has no begin marker" % (name, ends[0] + 1))
    if not ends:
        raise BlockError("begin marker for %r on line %d has no end marker" % (name, begins[0] + 1))
    if ends[0] < begins[0]:
        raise BlockError("end marker for %r (line %d) comes before its begin marker (line %d)"
                         % (name, ends[0] + 1, begins[0] + 1))
    return BlockLoc(begins[0], ends[0])


def render_block(name: str, tool: str, body: Sequence[str]) -> Tuple[str, ...]:
    """The block as lines: begin marker, body, end marker."""
    return ("# fleet:begin %s (managed by %s)" % (name, tool),) + tuple(body) + ("# fleet:end %s" % name,)


def block_body(text: str, name: str) -> Optional[Tuple[str, ...]]:
    """The lines between the markers (CR stripped), or None when there is no such block."""
    loc = find_block(text, name)
    if loc is None:
        return None
    lines = text.split("\n")
    return tuple(ln.rstrip("\r") for ln in lines[loc.begin + 1:loc.end])


def upsert_block(text: Optional[str], name: str, tool: str, body: Sequence[str]) -> Tuple[str, str]:
    """(new text, action): action is create (no file yet), append, replace or none.  Bytes outside the block
    are never changed, and a file with CRLF line endings keeps them."""
    block = render_block(name, tool, body)
    if text is None or not text.strip():
        return "\n".join(block) + "\n", ("create" if text is None else "append")
    crlf = "\r\n" in text
    eol = "\r\n" if crlf else "\n"
    loc = find_block(text, name)
    if loc is None:
        base = text if text.endswith("\n") else text + eol
        if not base.endswith(eol + eol):
            base += eol
        return base + eol.join(block) + eol, "append"
    lines = text.split("\n")
    old = tuple(ln.rstrip("\r") for ln in lines[loc.begin:loc.end + 1])
    if old == block:
        return text, "none"
    cr = "\r" if crlf else ""
    new_block = [ln + cr for ln in block]
    if loc.end == len(lines) - 1 and cr:
        new_block[-1] = block[-1]
    return "\n".join(lines[:loc.begin] + new_block + lines[loc.end + 1:]), "replace"


def remove_block(text: str, name: str) -> Tuple[str, str]:
    """(new text, action): action is remove or none.  The blank line the block was appended after goes too."""
    loc = find_block(text, name)
    if loc is None:
        return text, "none"
    lines = text.split("\n")
    start = loc.begin
    if start > 0 and lines[start - 1].strip() == "":
        start -= 1
    kept = lines[:start] + lines[loc.end + 1:]
    new = "\n".join(kept)
    if new.strip() == "":
        return "", "remove"
    return (new if new.endswith("\n") else new + "\n"), "remove"
