"""Outbound text hygiene for GHR:  keep pasted credentials out of relayed content.

Seats supply free text (PR titles, bodies, review replies).  A seat can paste a
token into that text by mistake - or a compromised seat could try to exfiltrate
one through a PR authored under the owner's token.  The relay refuses any
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
)


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


def scan_args(value: object, path: str = "args", _depth: int = 0) -> None:
    """Refuse a request when any nested string field matches a credential shape.

    Bounded like the audit cleaner (8 levels, 64 entries per level) so a
    hostile payload cannot make the scan itself unbounded.
    """
    if _depth > MAX_SCAN_DEPTH:
        return
    if isinstance(value, str):
        name = find_secret(value)
        if name:
            raise SecretInputError(path, name)
        return
    if isinstance(value, dict):
        for key, item in list(value.items())[:MAX_SCAN_ENTRIES_PER_LEVEL]:
            scan_args(item, f"{path}.{key}", _depth + 1)
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(list(value)[:MAX_SCAN_ENTRIES_PER_LEVEL]):
            scan_args(item, f"{path}[{index}]", _depth + 1)
