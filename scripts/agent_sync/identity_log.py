"""The identity audit log for stdio MCP and CLI writes (AFC-331, docs/protocols/per-call-identity.md
section 2, rule 8).

One JSON line per write in <state>/logs/identity.jsonl (the state dir is AGENT_SYNC_STATE_DIR or
~/.agent-sync), mode 600:  when, which transport and process, which MCP client (from `initialize`
clientInfo), the connection's default identity, the identity the call claimed, the tool or command,
the outcome and the message id when there is one.  Ids only:  never a body, a topic, a key or any
other Zulip-authored text.  A failure to write the log never fails the write it records.

Python 3.11+, standard library only.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping

from .state import state_root

LOG_NAME = "identity.jsonl"
_SAFE = re.compile(r"[^A-Za-z0-9._@+:/ -]")
FIELD_LIMIT = 60


def log_path(env: Mapping[str, str], home: Path | None = None) -> Path:
    return state_root(env, home) / "logs" / LOG_NAME


def safe_text(value: Any, limit: int = FIELD_LIMIT) -> str | None:
    """A short label made of plain characters (a client name, a version, a seat), else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    return _SAFE.sub("?", value.strip())[:limit]


def client_label(info: Any) -> dict[str, str | None] | None:
    """{name, version} from an MCP clientInfo object, cleaned;  None when there is none."""
    if not isinstance(info, Mapping):
        return None
    name, version = safe_text(info.get("name")), safe_text(info.get("version"))
    if name is None and version is None:
        return None
    return {"name": name, "version": version}


def append(env: Mapping[str, str], home: Path | None, *, transport: str, default_identity: str | None,
           identity: str | None, tool: str, outcome: str, message_id: int | None = None,
           client: Mapping[str, Any] | None = None, clock: Any = time.time) -> bool:
    """Append one row.  True when it was written."""
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(clock())),
        "transport": transport,
        "pid": os.getpid(),
        "client": client_label(client) if client is not None else None,
        "default_identity": safe_text(default_identity),
        "identity": safe_text(identity),
        "tool": safe_text(tool),
        "outcome": safe_text(outcome),
        "message_id": message_id if isinstance(message_id, int) and not isinstance(message_id, bool) else None,
    }
    path = log_path(env, home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, (json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii"))
        finally:
            os.close(fd)
    except OSError:
        return False
    return True
