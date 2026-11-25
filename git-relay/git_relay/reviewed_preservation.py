"""Exact reviewed fixture preservation, scoped to approved source/output pairs.

No raw-upload exemption. The server must fetch the pinned same-path base.
Dedicated content stays refused unless an immutable source/output pair is
recorded here after owner policy approval AND source review. Loaded values,
provider guard hits, new matches and changed source lines remain refused.
"""

from __future__ import annotations

from array import array
from hashlib import sha256

from . import patch_secretscan, secret_scan
from .provenance_patch import MAX_BYTES, apply_patch

# (repository, destination path, complete source SHA256, complete result SHA256).
# Source review does not itself approve the security-policy change.
REVIEWED_PRESERVATIONS: frozenset[tuple[str, str, str, str]] = frozenset(
    {
        # Owner-approved AFC-797 public test signatures; complete review and
        # provenance: docs/security/reviewed-muster-vector-preservation.md.
        (
            "Simple-With-Us/AI-Fleet-Coordinator",
            "scripts/fixtures/muster-vectors-v5.json",
            "0fca2cb4b79206e4eca627bfa6b9632db743ef2c3ff0ada0152c4bdcead32ab0",
            "73fd3d71ee62c617c802423e4dc7490387152776c0e8270c7d9cc3e87e43bc49",
        ),
        # Owner-approved AFC-793 synthetic fixture test pairs; complete review:
        # docs/security/reviewed-fixture-pairs-preservation.md.
        (
            "Simple-With-Us/AI-Fleet-Coordinator",
            "scripts/fleet_rag/tests/test_ingest.py",
            "e970e451e414d3efd963abed5d544f0713fe2b2934901c032fe8b01898085d2e",
            "1eaa804f7514122721888bd9f49c41a4088d827a28855391739d97c334008634",
        ),
        # RENEWED (Jay, Nov 23, 2026, in chat): the Nov 21 recall tuple (base b78c76d6…,
        # result d9cd1d0a…) is RETIRED — OPS-1102 #2473 drifted that base, and a renewal
        # replaces, not extends:  same fixtures, same refusal flips, live base only.
        (
            "Simple-With-Us/AI-Fleet-Coordinator",
            "scripts/fleet_rag/tests/test_recall.py",
            "8a6911c37d2f27d75be7193552b1b9402c4f4f5f3777c2ff350aded37ab6b32e",
            "de6b309011f0d0f043dfb209584d71989f6a599128d7eaa3ae5703d54dc5aa31",
        ),
    }
)


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
        if patch_secretscan.scan(text) is not None or secret_scan.find_secret(text) is not None:
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
    """Default is unchanged policy. A registered record admits only its exact
    owner-approved result bytes; every other result is refused.

    Origin is reconstructed from edits over the server-fetched base, never
    accepted from caller data or inferred from equal text elsewhere.
    """
    if not reviewed_base(repo, path, base):
        return apply_patch(base, edits, known=known)
    result, _origins = _render(base, edits)
    record = (repo, path, sha256(base).hexdigest(), sha256(result).hexdigest())
    if record not in REVIEWED_PRESERVATIONS:
        raise ValueError("unreviewed result")
    base_text = base.decode("utf-8", "strict")
    text = result.decode("utf-8", "strict")
    known = tuple(v for v in known if v)
    if any(v in base_text or v in text for v in known):
        raise ValueError("loaded credential")
    # A registered record pins the exact result bytes the owner approved after
    # source review, so that result is admitted as reviewed content even where
    # its own lines match a scanner pattern (the AFC-793 refusal-flip fixtures
    # are the standing example: their changed lines cannot satisfy preserved-
    # line provenance, and the pair was approved knowing that).  Unregistered
    # results never reach this point.
    return result
