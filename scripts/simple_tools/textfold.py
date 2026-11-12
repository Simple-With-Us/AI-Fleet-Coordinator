"""NFKC folding for untrusted read paths (AFC-729 / AFC-758).

Hosted ST-MCP and the stdio Python stack share one contract:  fold compatibility forms,
drop format characters, but keep Unicode space separators (category Zs) as written so
sentence-gap U+00A0 and U+202F survive fenced reads.
"""

from __future__ import annotations

import unicodedata


def fold(text: str) -> str:
    """NFKC on non-space runs, drop format characters, leave Zs code points unchanged."""
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if not buf:
            return
        chunk = unicodedata.normalize("NFKC", "".join(buf))
        chunk = "".join(ch for ch in chunk if unicodedata.category(ch) != "Cf")
        out.append(chunk)
        buf.clear()

    for ch in text:
        if unicodedata.category(ch) == "Zs":
            flush()
            out.append(ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)
