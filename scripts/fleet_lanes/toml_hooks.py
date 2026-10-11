"""toml_hooks: find, adopt and replace a tool's `[[hooks]]` entry in a TOML file by what it SAYS, not by comment markers.

Kimi Code rewrites its own `config.toml` through a TOML writer (`atomicWrite(path, stringify(configToTomlData(...)))`
on any settings change, a model switch or a login), and that writer keeps `[[hooks]]` but drops every comment.  So the
`# fleet:begin` / `# fleet:end` markers that `marked_block` writes around our entry do not survive the owner's next
Kimi settings change (the live file already has no comment lines at all).  Everything that matters about our entry is
therefore read from the entry itself:  a `[[hooks]]` table whose `command` is one of ours (`is_ours`), with the four
fields Kimi allows (event, matcher, command, timeout).  The markers stay as a convenience for a person reading the
file, nothing more.

    plan(text, name, tool, body, desired, is_ours) -> (new text, action, notes)
    inspect(text, name, desired, is_ours)          -> Inspection

`action` is create, append, replace or none (as in marked_block).  The rules:

  * markers present:  the block is the entry, replaced when it differs;  any further entry of ours OUTSIDE the block is
    a duplicate and is removed.
  * no markers, one entry of ours that already has exactly the wanted fields:  `none`.  Nothing is rewritten, because
    Kimi would strip a marker again at its next write.
  * no markers, an entry of ours with other fields (or several of them):  the first becomes the marked block in place,
    the others are removed.
  * nothing of ours:  the block is appended.
  * the hook command appears anywhere else (an inline `hooks = [{ ... }]`, another table, a multi-line value we do
    not parse):  `BlockError`.  The caller refuses, because appending a second entry could run the hook twice.

A `[[hooks]]` entry spans from its header to its last content line (not the blank or comment lines after it), and
ends at the next line that starts a table header.  Values are read the way Kimi writes them (`"basic"` or 'literal'
strings, integers);  a value that runs over several lines marks the entry `complex`, and a complex entry that names
our command is refused rather than guessed at.  Pure text in, text out.  Python 3.9 safe (the stable copy runs under
macOS's /usr/bin/python3, which has no tomllib).  Tests: fleet_lanes/tests/test_toml_hooks.py.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import marked_block as MB

__all__ = ["HookEntry", "Inspection", "scan_hooks", "inspect", "plan", "fields_match"]

_HEADER = re.compile(r"^[ \t]*\[")                       # any table header, [x] or [[x]]
_HOOKS_HEADER = re.compile(r"^[ \t]*\[\[[ \t]*hooks[ \t]*\]\][ \t]*(?:#.*)?$")
_KEY = re.compile(r"^[ \t]*(?P<key>[A-Za-z0-9_-]+|\"[^\"\n]*\"|'[^'\n]*')[ \t]*=[ \t]*(?P<rest>.*)$")


@dataclass(frozen=True)
class HookEntry:
    start: int                                  # index of the `[[hooks]]` header line in text.split("\n")
    end: int                                    # index of its last content line
    fields: Dict[str, object] = field(default_factory=dict)
    complex: bool = False                       # a value ran over several lines or was not a plain string or integer

    @property
    def command(self) -> Optional[str]:
        value = self.fields.get("command")
        return value if isinstance(value, str) else None


@dataclass(frozen=True)
class Inspection:
    state: str                                  # block, stripped or none
    ours: Tuple[HookEntry, ...] = ()            # every entry of ours, in file order
    inside: Tuple[HookEntry, ...] = ()          # the ones within the markers
    outside: Tuple[HookEntry, ...] = ()         # the ones outside them (all of `ours` when there are no markers)
    current: bool = False                       # state is block or stripped, one entry, and its fields are the wanted ones
    other_refs: Tuple[int, ...] = ()            # line numbers (1-based) of other mentions of the hook command


def _scan_string(token: str) -> Tuple[Optional[str], str]:
    """(decoded string or None, text after the closing quote) for a token that starts with a quote."""
    quote = token[0]
    if token.startswith(quote * 3):
        return None, ""                         # a multi-line string: not read
    i = 1
    while i < len(token):
        ch = token[i]
        if quote == '"' and ch == "\\":
            i += 2
            continue
        if ch == quote:
            raw = token[1:i]
            if quote == "'":
                return raw, token[i + 1:]
            try:
                return json.loads('"' + raw + '"'), token[i + 1:]
            except ValueError:
                return None, ""
        i += 1
    return None, ""


def _parse_value(rest: str) -> Tuple[bool, object]:
    """(ok, value).  ok is False for anything that is not a one-line string or integer."""
    rest = rest.strip()
    if not rest:
        return False, None
    if rest[0] in "\"'":
        value, after = _scan_string(rest)
        if value is None:
            return False, None
        after = after.strip()
        if after and not after.startswith("#"):
            return False, None
        return True, value
    token = re.split(r"[ \t]+#|[ \t]", rest, maxsplit=1)[0]
    if re.fullmatch(r"[+-]?\d+(?:_\d+)*", token):
        return True, int(token.replace("_", ""))
    if token in ("true", "false"):
        return True, token == "true"
    return False, None


def scan_hooks(text: str) -> List[HookEntry]:
    """Every `[[hooks]]` table in the text, in file order."""
    lines = text.split("\n")
    entries: List[HookEntry] = []
    i = 0
    while i < len(lines):
        if not _HOOKS_HEADER.match(lines[i].rstrip("\r")):
            i += 1
            continue
        start, last, j = i, i, i + 1
        fields: Dict[str, object] = {}
        complex_ = False
        while j < len(lines):
            line = lines[j].rstrip("\r")
            if _HEADER.match(line) or MB.is_marker(line):
                break
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                last = j
                m = _KEY.match(line)
                if m is None:
                    complex_ = True
                else:
                    key = m.group("key").strip("\"'")
                    ok, value = _parse_value(m.group("rest"))
                    if ok:
                        fields[key] = value
                    else:
                        complex_ = True
            j += 1
        entries.append(HookEntry(start, last, fields, complex_))
        i = j
    return entries


def fields_match(entry: HookEntry, desired: Dict[str, object]) -> bool:
    """True when the entry has exactly the wanted fields (same keys, same values) and nothing it could not read."""
    return (not entry.complex) and entry.fields == dict(desired)


def _is_comment(line: str) -> bool:
    return line.strip().startswith("#")


def _other_refs(lines: Sequence[str], spans: Sequence[Tuple[int, int]], is_ours: Callable[[str], bool]) -> List[int]:
    """Lines that mention our command outside every span (comments are not mentions)."""
    refs = []
    for i, raw in enumerate(lines):
        if any(a <= i <= b for a, b in spans):
            continue
        line = raw.rstrip("\r")
        if _is_comment(line):
            continue
        if is_ours(line):
            refs.append(i + 1)
    return refs


def inspect(text: Optional[str], name: str, desired: Dict[str, object], is_ours: Callable[[str], bool]) -> Inspection:
    """What the file holds of ours.  Raises MB.BlockError for markers that cannot be trusted."""
    if text is None or not text.strip():
        return Inspection("none")
    entries = scan_hooks(text)
    ours = tuple(e for e in entries if e.command is not None and is_ours(e.command))
    loc = MB.find_block(text, name)
    if loc is None:
        outside, inside = ours, ()
    else:
        inside = tuple(e for e in ours if loc.begin < e.start and e.end < loc.end)
        outside = tuple(e for e in ours if e not in inside)
    spans = [(e.start, e.end) for e in ours]
    if loc is not None:
        spans.append((loc.begin, loc.end))
    lines = text.split("\n")
    refs = _other_refs(lines, spans, is_ours)
    state = "block" if loc is not None else ("stripped" if ours else "none")
    current = state != "none" and len(ours) == 1 and fields_match(ours[0], desired)
    return Inspection(state, ours, inside, outside, current, tuple(refs))


def _eol(text: str) -> Tuple[str, str]:
    return ("\r\n", "\r") if "\r\n" in text else ("\n", "")


def _remove_span(lines: List[str], start: int, end: int) -> None:
    """Delete lines[start..end] and the one blank line that came before them (as marked_block.remove_block does)."""
    cut = start
    if cut > 0 and lines[cut - 1].strip() == "":
        cut -= 1
    del lines[cut:end + 1]


def plan(text: Optional[str], name: str, tool: str, body: Sequence[str], desired: Dict[str, object],
         is_ours: Callable[[str], bool]) -> Tuple[str, str, List[str]]:
    """(new text, action, notes).  See the module docstring for the rules.  Raises MB.BlockError when the file cannot
    be edited safely."""
    notes: List[str] = []
    if text is None or not text.strip():
        new, action = MB.upsert_block(text, name, tool, body)
        return new, action, notes
    info = inspect(text, name, desired, is_ours)
    if info.other_refs:
        raise MB.BlockError("the fleet hook is also named on line %s outside any [[hooks]] entry this tool can edit "
                            "(an inline hooks array or another table); not adding a second one"
                            % ", ".join(str(n) for n in info.other_refs))
    for e in info.ours:
        if e.complex:
            raise MB.BlockError("the fleet [[hooks]] entry at line %d has a value this tool does not read; "
                                "not editing it" % (e.start + 1))
    loc = MB.find_block(text, name)
    cr = _eol(text)[1]
    lines = text.split("\n")
    if loc is not None:
        for e in sorted(info.outside, key=lambda x: x.start, reverse=True):
            _remove_span(lines, e.start, e.end)
        if info.outside:
            notes.append("removed %d duplicate fleet [[hooks]] entr%s outside the block"
                         % (len(info.outside), "y" if len(info.outside) == 1 else "ies"))
        new_text, action = MB.upsert_block("\n".join(lines), name, tool, body)
        if info.outside and action == "none":
            action = "replace"
        return new_text, action, notes
    if not info.ours:
        new, action = MB.upsert_block(text, name, tool, body)
        return new, action, notes
    if info.current:
        notes.append("the fleet entry is current but has lost its markers (Kimi rewrote the file); left as it is")
        return text, "none", notes
    first, rest = info.ours[0], info.ours[1:]
    block = [ln + cr for ln in MB.render_block(name, tool, body)]
    for e in sorted(info.ours, key=lambda x: x.start, reverse=True):     # bottom up, so earlier indexes stay valid
        if e is first:
            lines[e.start:e.end + 1] = block
        else:
            _remove_span(lines, e.start, e.end)
    if rest:
        notes.append("removed %d duplicate fleet [[hooks]] entr%s" % (len(rest), "y" if len(rest) == 1 else "ies"))
    notes.append("the fleet entry had lost its markers and its fields were not the current ones; replaced in place")
    return "\n".join(lines), "replace", notes
