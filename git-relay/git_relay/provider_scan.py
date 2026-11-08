"""Provider-key shapes without confusing ordinary longer words with keys.

Distinctive provider formats match inside other text. Ambiguous bare prefixes
need a lexical boundary or credential assignment context. This is heuristic
classification, not proof a key is real or a zero-false-negative guarantee.
Existing workflow loaded-bearer and provenance-patch known-value checks are
independent and unchanged.
"""

import re

# No maximum key length is assumed. Scoped markers come from vendor docs and
# the established OpenAI scanner corpus; the legacy marker is supplemental,
# never mandatory for a boundary-start legacy key.
PROVIDER_PATTERN = (
    r"sk-(?:ant-|proj-|svcacct-|admin-)[A-Za-z0-9_-]{20,}"
    r"|sk_(?:live|test)_[A-Za-z0-9]{16,}"
    r"|sk-[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20}"
    r"|(?<![\w])sk-[A-Za-z0-9_-]{20,}"
    r"|(?i:\b(?:[A-Z][A-Z0-9_]*_)?(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD)"
    r"['\"]?\s*[:=]\s*['\"]?[A-Za-z0-9_-]*sk-[A-Za-z0-9_-]{20,})"
)
_PROVIDER = re.compile(PROVIDER_PATTERN)


def provider_token_text(value):
    if not isinstance(value, str):
        return False
    return bool(_PROVIDER.search(value))
