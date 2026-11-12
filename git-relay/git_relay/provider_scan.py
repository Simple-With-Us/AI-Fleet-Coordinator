"""Provider-key shapes without confusing ordinary longer words with keys.

Supported complete provider formats match inside other text. Ambiguous prefixes
need a lexical boundary or credential assignment context. This is heuristic
classification, not proof a key is real or a zero-false-negative guarantee.
Existing workflow loaded-bearer and provenance-patch known-value checks are
independent and unchanged.
"""

import re

# No maximum key length is assumed by the boundary/context heuristics. Complete
# embedded formats below follow the gitleaks v8.30.1 OpenAI/Anthropic corpus, not
# an exhaustive vendor promise. Undocumented markerless embedded formats remain
# uncertain; loaded-value and credential-context guards still apply.
PROVIDER_PATTERN = (
    r"sk-ant-(?:api03|admin01)-[A-Za-z0-9_-]{93}AA"
    r"|sk-(?:proj|svcacct|admin)-(?:[A-Za-z0-9_-]{58}|[A-Za-z0-9_-]{74})"
    r"T3BlbkFJ(?:[A-Za-z0-9_-]{58}|[A-Za-z0-9_-]{74})"
    r"|(?<![\w])sk-(?:ant-|proj-|svcacct-|admin-)[A-Za-z0-9_-]{20,}"
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
