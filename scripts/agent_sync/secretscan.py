"""A small secret scanner for text about to be posted to Zulip.

`post` and `reply` refuse text it flags, and the listener daemon refuses a wake reply it flags.
It knows common key prefixes, private-key blocks and Zulip-shaped API keys (32 mixed-case
letters and digits), and it always matches any key the caller hands it exactly (the bot keys
the process has loaded).  It reports the kind of match only, never the matched text.

Standard library only (re), so it is safe on the hook fast path.
"""
from __future__ import annotations

import base64
import re
from typing import Iterable

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("Anthropic or OpenAI key", re.compile(r"\bsk-(ant-)?[A-Za-z0-9_-]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Stripe key", re.compile(r"\b(sk|rk)_(live|test)_[A-Za-z0-9]{16,}\b")),
    ("bearer token", re.compile(r"(?i)\bauthorization:\s*(basic|bearer)\s+[A-Za-z0-9+/=._-]{16,}")),
    ("assigned secret", re.compile(r"(?i)\b(api[_-]?key|secret|token|password)\s*[=:]\s*['\"]?[A-Za-z0-9+/=._-]{16,}")),
]
_ZULIP_SHAPED = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{32}(?![A-Za-z0-9])")


def _zulip_shaped(token: str) -> bool:
    return any(c.isupper() for c in token) and any(c.islower() for c in token) and any(c.isdigit() for c in token)


def scan(text: str, known: Iterable[str] = ()) -> str | None:
    """The kind of secret found in `text`, or None.  `known` are exact values to refuse (and their
    base64 Basic-auth forms are caught by the caller passing them too)."""
    for value in known:
        if value and len(value) >= 8 and value in text:
            return "a loaded credential"
    for label, pattern in _PATTERNS:
        if pattern.search(text):
            return label
    for match in _ZULIP_SHAPED.finditer(text):
        if _zulip_shaped(match.group()):
            return "a Zulip-shaped API key"
    return None


def basic_forms(email: str, key: str) -> list[str]:
    """The key and its base64 Basic-auth token, for `known`."""
    return [key, base64.b64encode(("%s:%s" % (email, key)).encode("utf-8")).decode("ascii")]
