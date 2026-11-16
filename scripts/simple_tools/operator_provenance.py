"""Notes-queue and token-broker audit events for the Operator registry (AFC-412 PR3;  the notes events follow the
Operator notes queue, AFC-486).

A typed allowlist:  only the event names below can be written through this path, each with its
required detail keys, and any detail key or value that looks like a secret is refused outright.
Provenance events carry metadata - note ids, target seats, outcomes - never credential material.

Standard library only, Python 3.11+.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from . import secretscan
from .operator_registry import SessionRegistry

# event name -> required detail keys
ALLOWED_EVENTS = {
    "notes_request": ("note_id",),
    "notes_result": ("note_id",),
    "token_mint": ("target_seat",),
    "token_revoke": ("target_seat",),
    "token_scope_check": ("target_seat",),
}

SECRET_KEY_RE = re.compile(
    r"(secret|token_value|api[_-]?key|password|credential|private[_-]?key|bearer)", re.IGNORECASE
)
SECRET_VALUE_RE = re.compile(
    r"(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"sk-[A-Za-z0-9]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----)"
)


class UnknownProvenanceEvent(ValueError):
    """The event name is not on the allowlist."""


class SecretInDetails(ValueError):
    """A detail key or value looks like credential material.  Never written anywhere."""


def _check_details(event: str, details: Mapping[str, Any]) -> None:
    for key in ALLOWED_EVENTS[event]:
        if key not in details:
            raise ValueError(f"{event} requires detail {key!r}")
    _check_detail_container(details, "")


def _check_detail_container(container: Any, path: str) -> None:
    """Walk every node json.dumps would serialize, including inside nested dicts, lists
    and tuples, so a nested credential is refused before any write.  Keys and values both go
    through the fleet secret scanner, which knows the formats the local regexes do not
    (JWTs, AWS keys, Infisical tokens, assigned credentials)."""
    if isinstance(container, Mapping):
        for key, value in container.items():
            label = f"{path}.{key}" if path else str(key)
            name = str(key)
            if SECRET_KEY_RE.search(name) or secretscan.scan(name):
                raise SecretInDetails(f"detail key refused: {label!r}")
            _check_detail_container(value, label)
    elif isinstance(container, (list, tuple)):
        for item in container:
            _check_detail_container(item, path)
    elif isinstance(container, str):
        if SECRET_VALUE_RE.search(container) or secretscan.scan(container):
            raise SecretInDetails(f"detail value for {path!r} matches a credential pattern")


def emit_provenance_event(
    registry: SessionRegistry,
    record_id: str,
    event: str,
    actor: str,
    outcome: str,
    source: str,
    details: Mapping[str, Any] | None = None,
) -> str:
    """Write one allowlisted audit event through the registry.  Raises before any write when the
    event is unknown, a required detail is missing, or a detail looks like a secret."""
    if event not in ALLOWED_EVENTS:
        raise UnknownProvenanceEvent(event)
    details = dict(details or {})
    _check_details(event, details)
    return registry.record_event(
        record_id,
        event=event,
        actor=actor,
        outcome=outcome,
        source=source,
        details=json.dumps(details, sort_keys=True),
    )
