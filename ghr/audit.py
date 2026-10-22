"""Append-only audit log for the PR relay.

One JSON object per line, fsynced after every write.  Bodies and titles are
never logged (they can carry secrets a caller pasted by mistake); the log
keeps their length and SHA-256 so an action is still attributable.  Any
other detail value that matches a known credential shape is hashed the same
way, whatever key it arrived under - including values nested inside dicts
and lists.  Detail rows are bounded (8 levels deep, 64 entries per level).
"""

from __future__ import annotations

import hashlib
import json
import os
import time

from .secret_scan import find_secret

SENSITIVE_KEYS = ("title", "body", "token")
MAX_DEPTH = 8
MAX_ENTRIES_PER_LEVEL = 64


def _clean(value, key: str, depth: int):
    if depth > MAX_DEPTH:
        return "<depth limit>"
    if isinstance(value, str):
        if key in SENSITIVE_KEYS or find_secret(value):
            return {
                "_sha256": hashlib.sha256(value.encode()).hexdigest(),
                "_len": len(value),
            }
        return value
    if isinstance(value, dict):
        return {
            str(k): _clean(v, str(k), depth + 1)
            for k, v in list(value.items())[:MAX_ENTRIES_PER_LEVEL]
        }
    if isinstance(value, (list, tuple)):
        return [_clean(v, key, depth + 1) for v in list(value)[:MAX_ENTRIES_PER_LEVEL]]
    return value


class AuditLog:
    def __init__(self, path: str):
        self._path = path

    def record(self, seat: str, action: str, repo: str, result: str, detail: dict) -> None:
        safe_detail = {}
        for key, value in detail.items():
            cleaned = _clean(value, key, 0)
            if isinstance(cleaned, dict) and set(cleaned) == {"_sha256", "_len"}:
                safe_detail[f"{key}_sha256"] = cleaned["_sha256"]
                safe_detail[f"{key}_len"] = cleaned["_len"]
            else:
                safe_detail[key] = cleaned
        # Reserved fields come AFTER the seat-supplied detail so no detail key
        # can overwrite seat/action/result/ts.
        row = {
            **safe_detail,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "seat": seat,
            "action": action,
            "repo": repo,
            "result": result,
        }
        line = json.dumps(row, sort_keys=True) + "\n"
        fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
