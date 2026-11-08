"""Exact reviewed fixture preservation. Proposal only: registry is empty.

No raw-upload exemption. The server must fetch the pinned same-path base.
Dedicated content stays refused unless an immutable source/output pair is
recorded here after owner policy approval AND source review. Loaded values,
provider guard hits, new matches and changed source lines remain refused.
"""

from __future__ import annotations

from array import array
from hashlib import sha256

from . import patch_secretscan
from .provenance_patch import MAX_BYTES, apply_patch

# (repository, destination path, complete source SHA256, complete result SHA256).
# Source review does not itself approve the security-policy change.
REVIEWED_PRESERVATIONS: frozenset[tuple[str, str, str, str]] = frozenset()


def reviewed_base(repo: str, path: str, base: bytes) -> bool:
    digest = sha256(base).hexdigest()
    return any(r == repo and p == path and b == digest for r, p, b, _ in REVIEWED_PRESERVATIONS)


def _render(base: bytes, edits: list[dict]) -> tuple[bytes, array]:
    if len(base) > MAX_BYTES or not isinstance(edits, list) or not 1 <= len(edits) <= 32:
        raise ValueError("invalid patch")
    base.decode("utf-8", "strict")
    out = bytearray()
    origins = array("i")
    cursor, previous = 0, -1
    for edit in edits:
        if not isinstance(edit, dict) or set(edit) != {"start", "end", "text"}:
            raise ValueError("invalid edit")
        start, end, text = edit["start"], edit["end"], edit["text"]
        if (
            type(start) is not int
            or type(end) is not int
            or not cursor <= start <= end <= len(base)
            or start <= previous
            or not isinstance(text, str)
        ):
            raise ValueError("invalid edit interval")
        base[:start].decode("utf-8", "strict")
        base[:end].decode("utf-8", "strict")
        if patch_secretscan.scan(text) is not None:
            raise ValueError("credential-shaped insertion")
        inserted = text.encode("utf-8", "strict")
        out.extend(base[cursor:start])
        origins.extend(range(cursor, start))
        out.extend(inserted)
        origins.extend([-1] * len(inserted))
        if len(out) > MAX_BYTES:
            raise ValueError("patch too large")
        cursor, previous = end, start
    out.extend(base[cursor:])
    origins.extend(range(cursor, len(base)))
    result = bytes(out)
    if len(result) > MAX_BYTES or result == base:
        raise ValueError("patch too large or unchanged")
    result.decode("utf-8", "strict")
    return result, origins


def apply_reviewed_patch(repo: str, path: str, base: bytes, edits: list[dict], known=()) -> bytes:
    """Default is unchanged policy. A record cannot bless new or moved matches.

    Origin is reconstructed from edits over the server-fetched base, never
    accepted from caller data or inferred from equal text elsewhere.
    """
    if not reviewed_base(repo, path, base):
        return apply_patch(base, edits, known=known)
    result, origins = _render(base, edits)
    record = (repo, path, sha256(base).hexdigest(), sha256(result).hexdigest())
    if record not in REVIEWED_PRESERVATIONS:
        raise ValueError("unreviewed result")
    base_text = base.decode("utf-8", "strict")
    text = result.decode("utf-8", "strict")
    known = tuple(v for v in known if v)
    if any(v in base_text or v in text for v in known):
        raise ValueError("loaded credential")
    offsets = array("i", [0])
    for char in text:
        offsets.append(offsets[-1] + len(char.encode("utf-8")))
    checked = set()
    matches = []
    for _, pattern in patch_secretscan._PATTERNS:
        matches.extend(pattern.finditer(text))
        if len(matches) > 4096:
            raise ValueError("pattern match budget exceeded")
    matches.extend(
        m
        for m in patch_secretscan._ZULIP_SHAPED.finditer(text)
        if patch_secretscan._zulip_shaped(m.group())
    )
    if len(matches) > 4096:
        raise ValueError("pattern match budget exceeded")
    for match in matches:
        left = text.rfind("\n", 0, match.start()) + 1
        right = text.find("\n", match.end())
        right = len(text) if right < 0 else right + 1
        a, b = offsets[left], offsets[right]
        if (a, b) in checked:
            continue
        checked.add((a, b))
        span = origins[a:b]
        if (
            not span
            or span[0] < 0
            or span != array("i", range(span[0], span[0] + len(span)))
            or base[span[0] : span[0] + len(span)] != result[a:b]
        ):
            raise ValueError("new, replaced or moved credential line")
        # Containing line boundaries must be boundaries in the original base.
        # A copied suffix or a merged line must not inherit a valid match.
        if span[0] and base[span[0] - 1 : span[0]] != b"\n":
            raise ValueError("changed containing source line")
        end = span[0] + len(span)
        if end < len(base) and base[end - 1 : end] != b"\n":
            raise ValueError("changed containing source line")
    return result
