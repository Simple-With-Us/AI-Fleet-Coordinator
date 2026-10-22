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
