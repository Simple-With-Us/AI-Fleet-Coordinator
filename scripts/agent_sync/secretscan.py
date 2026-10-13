"""A small secret scanner for text about to be posted to Zulip.

`post` and `reply` refuse text it flags, and the listener daemon refuses a wake reply it flags.
It knows common key prefixes, private-key blocks and Zulip-shaped API keys (32 mixed-case
letters and digits), and it always matches any key the caller hands it exactly (the bot keys
the process has loaded).  It reports the kind of match only, never the matched text.

The fleet's own services are covered by name (open-loops F3):  Linear API keys (`lin_api_`,
`lin_oauth_`, `lin_wh_`) and Linear client secrets (which have no prefix, so only an assignment
that names Linear catches them), Sentry auth tokens (`sntrys_`, `sntryu_`), xAI keys (`xai-`),
Infisical service tokens (`st.` plus three parts) and access tokens (a JWT), Cloudflare API
tokens (`cfut_`, `cfat_`, `cfk_`, or a 37 to 40 character key assigned to a name that says
Cloudflare), Coolify tokens (`N|` plus 36 or more letters and digits) and any ALL-CAPS name ending
SECRET, TOKEN, PASSWORD or API_KEY assigned a 16 character value.  `scripts/check-routine-prompts.py`
keeps a local fallback list of the main kinds (not `lin_wh_`, the JWT or the Infisical assignment) for
when it runs outside the repo and cannot load this file;  inside the repo this scanner is the one that
runs, and whatever the fallback flags, this flags too (test_secretscan_fleet pins it).
The hosted MCP Worker keeps a JavaScript copy of the list (`scripts/agent-sync-mcp/src/contract.js`);
`scripts/agent-sync-mcp/test/secretscan-parity.test.mjs` runs this suite's samples through both, so add a
kind to both in one change.

`redact_fields(obj)` is the per-field form for structured data that is about to be written
somewhere durable (the signed facts published to the private fleet-loops-data repo, a ledger):  it
replaces each secret-shaped string field with `[withheld: secret-shaped]` and says where.

Standard library only (`re` and `base64`;  `json` is imported inside `_key_path`, the one place that needs
it) and no imports from this package:  it is safe on the hook fast path, and
`scripts/check-routine-prompts.py` loads it by file path.
"""
from __future__ import annotations

import base64
import re
from typing import Any, Iterable

__all__ = ["scan", "basic_forms", "redact_fields", "WITHHELD"]

WITHHELD = "[withheld: secret-shaped]"

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b")),
    ("Anthropic or OpenAI key", re.compile(r"\bsk-(ant-)?[A-Za-z0-9_-]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b")),
    ("GitLab token", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Stripe key", re.compile(r"\b(sk|rk)_(live|test)_[A-Za-z0-9]{16,}\b")),
    # The fleet's own services (open-loops F3).  Specific kinds come before the generic assignment rows
    # below, so the answer names the service.
    ("Linear API key", re.compile(r"\blin_(api|oauth|wh)_[A-Za-z0-9]{20,}")),
    ("Linear client secret", re.compile(r"(?i)\blinear[\w -]{0,24}secret\s*[=:]\s*['\"]?[A-Za-z0-9]{24,}")),
    ("Sentry token", re.compile(r"\bsntry[su]_[A-Za-z0-9+/=_-]{20,}")),
    ("xAI key", re.compile(r"\bxai-[A-Za-z0-9]{20,}")),
    ("Infisical service token", re.compile(r"\bst\.[A-Za-z0-9]{8,}\.[A-Za-z0-9]{8,}\.[A-Za-z0-9]{8,}")),
    ("Infisical credential", re.compile(r"(?i)\binfisical[\w .-]{0,40}?(token|secret)['\"]?\s*[=:]\s*['\"]?[A-Za-z0-9+/=._-]{24,}")),
    ("access token (JWT)", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("Cloudflare token", re.compile(r"\bcf(ut|at|k)_[A-Za-z0-9]{30,}")),
    ("Cloudflare token", re.compile(r"(?i)\bcloudflare[\w ]{0,24}(token|key)\s*[=:]\s*['\"]?[A-Za-z0-9_-]{37,}")),
    ("Coolify token", re.compile(r"(?<![A-Za-z0-9])\d{1,5}\|[A-Za-z0-9]{36,}(?![A-Za-z0-9])")),
    ("assigned credential", re.compile(r"\b[A-Z][A-Z0-9_]*(SECRET|TOKEN|PASSWORD|API_?KEY)\s*=\s*['\"]?[A-Za-z0-9+/=._-]{16,}")),
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


# --------------------------------------------------------------------------------------------
# Per-field redaction
# --------------------------------------------------------------------------------------------

_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
MAX_DEPTH = 64


def _key_path(path: str, key: Any) -> str:
    """`path.name` for a dict key that is a plain identifier, else `path["key"]` (JSON quoted)."""
    if isinstance(key, str) and _IDENT_RE.fullmatch(key):
        return "%s.%s" % (path, key)
    import json  # lazy:  the hook fast path imports this module and never needs it

    return "%s[%s]" % (path, json.dumps(key, ensure_ascii=True, default=str))


def redact_fields(obj: Any, known: Iterable[str] = ()) -> tuple[Any, list[str]]:
    """(redacted copy of `obj`, the paths that were redacted).

    Every string that `scan` flags (with `known` as the exact values to refuse) is replaced whole by
    `[withheld: secret-shaped]`;  nothing else changes.  dicts, lists and tuples are walked, and a
    secret-shaped dict KEY is replaced too (then numbered, `[withheld: secret-shaped] #2`, if that
    would collide).  Anything else (numbers, None, bytes, sets, other objects) is returned as it is.
    A structure nested deeper than MAX_DEPTH is withheld whole (it may be cyclic), and the path says
    so.  `obj` itself is never changed.

    A path is `$` for the root, `.name` for a dict key that is a plain identifier, `["key"]` (JSON
    quoted) for any other key and `[n]` for a list or tuple index:  `$.items[2].title`.  A path never
    holds a secret, because a secret-shaped key is written as `["[withheld: secret-shaped]"]`.  The
    values it replaced are not returned, logged or kept."""
    known_list = [v for v in known if v]
    paths: list[str] = []

    def hit(value: str) -> bool:
        return scan(value, known_list) is not None

    def walk(node: Any, path: str, depth: int) -> Any:
        if isinstance(node, str):
            if hit(node):
                paths.append(path)
                return WITHHELD
            return node
        if not isinstance(node, (dict, list, tuple)):
            return node
        if depth >= MAX_DEPTH:
            paths.append(path)
            return WITHHELD
        if isinstance(node, dict):
            out: dict[Any, Any] = {}
            for key, value in node.items():
                if isinstance(key, str) and hit(key):
                    new_key, n = WITHHELD, 1
                    while new_key in out:
                        n += 1
                        new_key = "%s #%d" % (WITHHELD, n)
                    child = _key_path(path, WITHHELD)
                    paths.append(child)
                    out[new_key] = walk(value, child, depth + 1)
                else:
                    out[key] = walk(value, _key_path(path, key), depth + 1)
            return out
        items = [walk(value, "%s[%d]" % (path, i), depth + 1) for i, value in enumerate(node)]
        return tuple(items) if isinstance(node, tuple) else items

    clean = walk(obj, "$", 0)
    return clean, list(dict.fromkeys(paths))
