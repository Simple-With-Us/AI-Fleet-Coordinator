"""Markdown-aware chunker for the fleet corpus.

bge-m3 accepts 4096 tokens but retrieval quality is best with focused chunks, so we target
~1,600 characters (~400 tokens) with ~200 characters of overlap, splitting on headings first,
then paragraphs, then sentences.  A heading trail is prepended to every chunk so a fragment of
"§ Secret handoff → Handoff-file grep trap" still carries its context when retrieved alone.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

TARGET = 1600
HARD_MAX = 2400
OVERLAP = 200
MIN_CHUNK = 200

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])")


@dataclass
class Chunk:
    text: str
    index: int
    heading: str  # "A › B › C" trail, may be empty


def _sections(md: str) -> list[tuple[list[str], str]]:
    """Split markdown into (heading_trail, body) sections."""
    trail: list[tuple[int, str]] = []
    sections: list[tuple[list[str], list[str]]] = [([], [])]
    in_fence = False
    for line in md.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        m = None if in_fence else _HEADING.match(line)
        if m:
            level, title = len(m.group(1)), m.group(2).strip()
            trail = [(lv, t) for lv, t in trail if lv < level] + [(level, title)]
            sections.append(([t for _, t in trail], []))
        else:
            sections[-1][1].append(line)
    return [(h, "\n".join(b).strip()) for h, b in sections if "\n".join(b).strip()]


def _paragraphs(body: str) -> list[str]:
    out: list[str] = []
    buf: list[str] = []
    in_fence = False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            buf.append(line)
            continue
        if not in_fence and not line.strip():
            if buf:
                out.append("\n".join(buf).strip())
                buf = []
        else:
            buf.append(line)
    if buf:
        out.append("\n".join(buf).strip())
    return [p for p in out if p]


def _split_long(par: str) -> list[str]:
    """Split a single over-long paragraph on sentence boundaries, then hard-wrap."""
    if len(par) <= HARD_MAX:
        return [par]
    pieces: list[str] = []
    cur = ""
    for sent in _SENTENCE.split(par):
        if len(cur) + len(sent) + 1 > TARGET and cur:
            pieces.append(cur.strip())
            cur = cur[-OVERLAP:] + " " + sent if OVERLAP else sent
        else:
            cur = (cur + " " + sent).strip()
    if cur.strip():
        pieces.append(cur.strip())
    final: list[str] = []
    for p in pieces:
        while len(p) > HARD_MAX:
            final.append(p[:HARD_MAX])
            p = p[HARD_MAX - OVERLAP:]
        final.append(p)
    return final


def chunk_markdown(md: str, prefix: str = "") -> list[Chunk]:
    """Chunk a markdown document.  `prefix` (e.g. a document title) leads every chunk's trail."""
    chunks: list[Chunk] = []
    idx = 0
    for trail, body in _sections(md):
        heading = " › ".join(([prefix] if prefix else []) + trail)
        units: list[str] = []
        for par in _paragraphs(body):
            units.extend(_split_long(par))
        cur = ""
        for unit in units:
            if cur and len(cur) + len(unit) + 2 > TARGET:
                chunks.append(Chunk(_with_heading(heading, cur), idx, heading))
                idx += 1
                cur = (cur[-OVERLAP:].split("\n", 1)[-1] + "\n\n" + unit) if OVERLAP else unit
            else:
                cur = (cur + "\n\n" + unit).strip() if cur else unit
        if cur.strip():
            # Merge a tiny tail into the previous chunk of the same section when possible.
            if chunks and chunks[-1].heading == heading and len(cur) < MIN_CHUNK \
                    and len(chunks[-1].text) + len(cur) < HARD_MAX:
                chunks[-1] = Chunk(chunks[-1].text + "\n\n" + cur, chunks[-1].index, heading)
            else:
                chunks.append(Chunk(_with_heading(heading, cur), idx, heading))
                idx += 1
    return chunks


def chunk_plain(text: str, prefix: str = "") -> list[Chunk]:
    """Chunk plain text (no headings) by paragraphs and sentences."""
    return chunk_markdown(text, prefix)


def _with_heading(heading: str, body: str) -> str:
    return f"[{heading}]\n{body}" if heading else body
