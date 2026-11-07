"""Exact-byte edits with server-fetched provenance, never caller attestations.

Residual risk: existing generic assignment matches stay present at the same path.
Dedicated token shapes and loaded values are refused even in unchanged base bytes.
"""

from __future__ import annotations

import importlib.util
from array import array
from pathlib import Path

MAX_BYTES = 2 * 1024 * 1024
GENERIC = frozenset({"assigned credential", "assigned secret"})


def _scanner():
    path = Path(__file__).resolve().parents[2] / "scripts" / "agent_sync" / "secretscan.py"
    spec = importlib.util.spec_from_file_location("_patch_scanner", path)
    if spec is None or spec.loader is None:
        raise ValueError("scanner unavailable")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def apply_patch(base: bytes, edits: list[dict], known=()) -> bytes:
    """Offsets are UTF-8 BYTES in original base, sorted, disjoint; insertions are new."""
    if len(base) > MAX_BYTES or not isinstance(edits, list) or not 1 <= len(edits) <= 32:
        raise ValueError("invalid patch")
    base.decode("utf-8", "strict")
    scanner = _scanner()
    base_text = base.decode("utf-8", "strict")
    if any(value and len(value) >= 8 and value in base_text for value in known):
        raise ValueError("loaded credential in base")
    for label, pattern in scanner._PATTERNS:
        if label not in GENERIC and pattern.search(base_text):
            raise ValueError("dedicated credential in base")
    if any(scanner._zulip_shaped(m.group()) for m in scanner._ZULIP_SHAPED.finditer(base_text)):
        raise ValueError("credential-shaped base")
    for edit in edits:
        if (
            isinstance(edit, dict)
            and isinstance(edit.get("text"), str)
            and scanner.scan(edit["text"], known=known) is not None
        ):
            raise ValueError("credential-shaped inserted text")
    output = bytearray()
    origins = array("i")
    cursor = 0
    previous_start = -1
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"start", "end", "text"}:
            raise ValueError("invalid edit")
        start, end, text = edit["start"], edit["end"], edit["text"]
        if (
            type(start) is not int
            or type(end) is not int
            or not cursor <= start <= end <= len(base)
            or start <= previous_start
            or not isinstance(text, str)
        ):
            raise ValueError("invalid edit interval")
        # Refuse splitting a codepoint, even if the final concatenation would decode.
        base[:start].decode("utf-8", "strict")
        base[:end].decode("utf-8", "strict")
        inserted = text.encode("utf-8", "strict")
        output.extend(base[cursor:start])
        origins.extend(range(cursor, start))
        output.extend(inserted)
        origins.extend([-1] * len(inserted))
        if len(output) > MAX_BYTES:
            raise ValueError("patch too large")
        cursor, previous_start = end, start
    output.extend(base[cursor:])
    origins.extend(range(cursor, len(base)))
    if len(output) > MAX_BYTES or bytes(output) == base:
        raise ValueError("patch too large or unchanged")
    result = bytes(output)
    text = result.decode("utf-8", "strict")
    scanner = _scanner()
    if any(value and len(value) >= 8 and value in text for value in known):
        raise ValueError("loaded credential")
    # Full-output pattern enumeration, not scan() first-hit or changed-span scans.
    for label, pattern in scanner._PATTERNS:
        for match in pattern.finditer(text):
            # Only assignment patterns have residual baseline semantics. A dedicated
            # Cloudflare token shares its label but not the named assignment capture.
            assignment = label in GENERIC and (
                "value" in pattern.groupindex
                or "=" in pattern.pattern
                or r"\s*[=:]" in pattern.pattern
            )
            if not assignment:
                raise ValueError("credential-shaped content")
            if (
                "value" in pattern.groupindex
                and hasattr(scanner, "_source_assignment")
                and scanner._source_assignment(text, match)
            ):
                continue
            # Require the ENTIRE containing source line unchanged and contiguous,
            # not merely matching text elsewhere, nor copied/replaced bytes.
            left = text.rfind("\n", 0, match.start()) + 1
            right = text.find("\n", match.end())
            right = len(text) if right < 0 else right + 1
            a, b = len(text[:left].encode()), len(text[:right].encode())
            segment = origins[a:b]
            if (
                not segment
                or segment[0] < 0
                or segment != array("i", range(segment[0], segment[0] + len(segment)))
                or base[segment[0] : segment[0] + len(segment)] != result[a:b]
            ):
                raise ValueError("new credential assignment")
    for match in scanner._ZULIP_SHAPED.finditer(text):
        if scanner._zulip_shaped(match.group()):
            raise ValueError("credential-shaped content")
    return result
