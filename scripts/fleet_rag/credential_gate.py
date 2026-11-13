"""Refuse credential-shaped recall input before transport, logging or persistence."""
from __future__ import annotations

from .core import FleetRagError

_FIELDS = frozenset({'query', 'text', 'title', 'url'})


def _scanner():
    # Every supported installation ships this existing, standard-library scanner.
    # Import lazily so a missing dependency refuses recall input without disabling
    # unrelated fleet tools, and never replaces the scanner with a weaker fallback.
    from simple_tools.secretscan import scan
    return scan


def require_safe_fields(**fields):
    """Scan exact string inputs; expose only fixed field names and refusal codes.

    Type/length validation remains with each API.
    Non-string optional inputs are left for that validation; no normalization or
    redaction occurs in this gate.
    """
    if set(fields) - _FIELDS:
        raise FleetRagError('credential_scan_unavailable:arguments')
    for field, value in fields.items():
        if not isinstance(value, str):
            continue
        try:
            scanner = _scanner()
            found = scanner(value)
        except Exception:
            raise FleetRagError('credential_scan_unavailable:' + field) from None
        if found is not None:
            raise FleetRagError('refused_secret:' + field)
