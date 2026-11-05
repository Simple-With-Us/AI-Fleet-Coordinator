"""Outbound text hygiene for git-relay:  keep pasted credentials out of relayed content.

Seats supply free text (PR titles, bodies, review replies).  A seat can paste a
token into that text by mistake - or a compromised seat could try to exfiltrate
one through a PR authored under the owner's token.  git-relay refuses any
outbound text that matches a known credential shape before it leaves for
GitHub, and the audit log hashes any detail value that matches one, whatever
key it arrived under.
"""

from __future__ import annotations

import re

_PATTERNS = (
    ("github_fine_grained_pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("github_token", re.compile(r"gh[opusr]_[A-Za-z0-9]{20,}")),
    (
        "private_key_block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    ),
    ("infisical_service_token", re.compile(r"\bst\.[A-Za-z0-9]{20,}")),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}|\bxapp-[A-Za-z0-9-]{10,}")),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    ("bearer_credential", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}")),
)

_SAFE_VALUE_SHAPES = (
    re.compile(r"^[0-9a-f]{7,64}$"),  # commit SHAs and hex digests
    re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE),
    re.compile(r"^PRRT_[A-Za-z0-9_-]{6,128}$"),  # review-thread ids
    re.compile(r"^https?://", re.IGNORECASE),
)
_TOKEN_LIKE = re.compile(r"^[A-Za-z0-9._~+/=-]+$")


def looks_like_credential(value: object) -> bool:
    """Audit-side heuristic for values under keys nobody classified.

    Long, space-free, mixed-class token-shaped strings are treated as
    plausibly credential and hashed by the audit log;  known identifier
    shapes (hex SHAs and digests, UUIDs, thread ids, URLs) stay readable.
    This never gates outbound text - find_secret alone does that - so a
    false positive here only hashes one audit field.
    """
    if not isinstance(value, str) or len(value) < 40:
        return False
    if any(shape.search(value) for shape in _SAFE_VALUE_SHAPES):
        return False
    if not _TOKEN_LIKE.match(value):
        return False
    classes = sum(
        bool(re.search(pattern, value)) for pattern in (r"[a-z]", r"[A-Z]", r"[0-9]", r"[._~+/=-]")
    )
    return classes >= 3


def find_secret(text: object) -> str | None:
    """Return the matching pattern name if text looks like it holds a credential."""
    if not isinstance(text, str):
        return None
    for name, pattern in _PATTERNS:
        if pattern.search(text):
            return name
    return None


def reject_secrets(field: str, value: object) -> None:
    """Raise ValueError if a seat-supplied field carries a credential shape."""
    name = find_secret(value)
    if name:
        raise ValueError(
            f"{field} looks like it contains a credential ({name}); refusing to relay it"
        )


MAX_SCAN_DEPTH = 8
MAX_SCAN_ENTRIES_PER_LEVEL = 64
MAX_SCAN_NODES = 2000
MAX_SCAN_BYTES = 256 * 1024


def mask_credential(value: object) -> str:
    """First-eight/last-four preview for a rejected credential, nothing else.

    The fleet convention for reporting a refused key:  enough to identify which
    credential failed, never enough to use it.  Short values are fully hidden.
    """
    if not isinstance(value, str) or len(value) < 16:
        return "<masked>"
    return value[:8] + "\u2026" + value[-4:]


class SecretInputError(ValueError):
    """A seat-supplied field anywhere in the request carries a credential shape.

    Carries only the dotted field path and the pattern name - never the value.
    """

    def __init__(self, field_path: str, pattern_name: str):
        super().__init__(
            f"{field_path} looks like it contains a credential ({pattern_name}); "
            "refusing to relay it"
        )
        self.field_path = field_path
        self.pattern_name = pattern_name


class InputTooComplexError(ValueError):
    """The request exceeds a scan bound (depth, breadth, nodes, or bytes).

    Over-limit input is REFUSED, never silently skipped:  a skipped branch
    could carry a credential the dispatcher would still relay.
    """


def scan_args(
    value: object, path: str = "args", _depth: int = 0, _budget=None, _decoded_elsewhere: bool = False
) -> None:
    """Refuse a request when any nested string field matches a credential shape.

    With `_decoded_elsewhere`, a `content_b64` field is left to its verb, which scans the decoded bytes:  base64 of
    ordinary content can look like a token, and it hides a real one.  Its length still counts against the bound.

    Bounded (8 levels, 64 entries per level, 2000 nodes and 256 KiB of text
    total) so a hostile payload cannot make the scan itself unbounded;  every
    bound fails closed by refusing the request.
    """
    if _budget is None:
        _budget = [MAX_SCAN_NODES, MAX_SCAN_BYTES]
    _budget[0] -= 1
    if _budget[0] < 0:
        raise InputTooComplexError(f"{path}: request exceeds the {MAX_SCAN_NODES}-node bound")
    if isinstance(value, str):
        _budget[1] -= len(value.encode("utf-8", "replace"))
        if _budget[1] < 0:
            raise InputTooComplexError(f"{path}: request exceeds the text byte bound")
        name = find_secret(value)
        if name:
            raise SecretInputError(path, name)
        return
    if _depth > MAX_SCAN_DEPTH:
        raise InputTooComplexError(f"{path}: request nests deeper than {MAX_SCAN_DEPTH} levels")
    if isinstance(value, dict):
        if len(value) > MAX_SCAN_ENTRIES_PER_LEVEL:
            raise InputTooComplexError(
                f"{path}: more than {MAX_SCAN_ENTRIES_PER_LEVEL} entries at one level"
            )
        for key, item in value.items():
            if _decoded_elsewhere and key == "content_b64" and isinstance(item, str):
                _budget[1] -= len(item)
                if _budget[1] < 0:
                    raise InputTooComplexError(f"{path}: request exceeds the text byte bound")
                continue
            scan_args(item, f"{path}.{key}", _depth + 1, _budget, _decoded_elsewhere)
        return
    if isinstance(value, (list, tuple)):
        if len(value) > MAX_SCAN_ENTRIES_PER_LEVEL:
            raise InputTooComplexError(
                f"{path}: more than {MAX_SCAN_ENTRIES_PER_LEVEL} entries at one level"
            )
        for index, item in enumerate(value):
            scan_args(item, f"{path}[{index}]", _depth + 1, _budget, _decoded_elsewhere)
