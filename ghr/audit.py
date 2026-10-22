"""Append-only audit log for the PR relay.

One JSON object per line, fsynced after every write.  Bodies and titles are
never logged (they can carry secrets a caller pasted by mistake); the log
keeps their length and SHA-256 so an action is still attributable.  Any
other detail value that matches a known credential shape is hashed the same
way, whatever key it arrived under.
"""

from __future__ import annotations

import hashlib
import json
import os
import time

from .secret_scan import find_secret


class AuditLog:
    def __init__(self, path: str):
        self._path = path

    def record(self, seat: str, action: str, repo: str, result: str, detail: dict) -> None:
        safe_detail = {}
        for key, value in detail.items():
            if isinstance(value, str) and (key in ("title", "body", "token") or find_secret(value)):
                safe_detail[f"{key}_sha256"] = hashlib.sha256(value.encode()).hexdigest()
                safe_detail[f"{key}_len"] = len(value)
            else:
                safe_detail[key] = value
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
