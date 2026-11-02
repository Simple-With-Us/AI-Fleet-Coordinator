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
import re
import threading
import time

from .secret_scan import find_secret, looks_like_credential

# A detail key is sensitive when any name segment carries credential meaning,
# so client_secret, X-Api-Key, signing_key and a nested refresh_token all
# hash even though only title/body/token were named historically.  Segment
# "key" covers api_key, access_key and private_key spellings alike.
_SENSITIVE_SEGMENTS = frozenset(
    {
        "title", "body", "token", "secret", "secrets", "password", "passwd",
        "pwd", "apikey", "key", "authorization", "auth", "credential",
        "credentials", "private", "session", "cookie", "cookies", "bearer",
    }
)
# Exact keys whose values are already safe by construction: the masked
# first-eight/last-four preview of a refused credential, and digests.
_SAFE_KEYS = frozenset({"credential_preview"})
_KEY_SEGMENT = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")


def _key_segments(key: str) -> list[str]:
    return [part.lower() for part in _KEY_SEGMENT.findall(str(key))]


def _key_is_sensitive(key: str) -> bool:
    segments = _key_segments(key)
    if "_".join(segments) in _SAFE_KEYS:
        return False
    if segments and segments[-1] in ("sha256", "len", "digest"):
        return False
    return any(segment in _SENSITIVE_SEGMENTS for segment in segments)


def _hash_of(text: str) -> dict:
    return {"_sha256": hashlib.sha256(text.encode()).hexdigest(), "_len": len(text)}
MAX_DEPTH = 8
MAX_ENTRIES_PER_LEVEL = 64
MAX_NODES = 2000
MAX_TEXT_BYTES = 256 * 1024

_OVER_LIMIT = "<over audit detail limit>"


def _clean(value, key: str, depth: int, budget, sensitive: bool = False):
    """Bound one detail value by cumulative node and text-byte budgets.

    Request arguments never reach here over-limit (the secret scan refuses
    them first), so overflow means a server-side result blob;  it degrades to
    a marker because an audit row must never fail outright.  A value hashes
    when its key carries credential meaning, when an ancestor key does, when
    it sits under `inputs` (workflow_dispatch input values are
    caller-chosen and may be secrets by design), or when the value itself
    looks credential-shaped - the default branch never passes a plausibly
    credential value through verbatim.
    """
    budget[0] -= 1
    if budget[0] < 0:
        return _OVER_LIMIT
    if depth > MAX_DEPTH:
        return "<depth limit>"
    sensitive = sensitive or _key_is_sensitive(key)
    if isinstance(value, str):
        budget[1] -= len(value.encode("utf-8", "replace"))
        if budget[1] < 0:
            return _OVER_LIMIT
        if sensitive or find_secret(value) or looks_like_credential(value):
            return _hash_of(value)
        return value
    if isinstance(value, dict):
        under_inputs = _key_segments(key) == ["inputs"]
        return {
            str(k): _clean(v, str(k), depth + 1, budget, sensitive or under_inputs)
            for k, v in list(value.items())[:MAX_ENTRIES_PER_LEVEL]
        }
    if isinstance(value, (list, tuple)):
        return [
            _clean(v, key, depth + 1, budget, sensitive)
            for v in list(value)[:MAX_ENTRIES_PER_LEVEL]
        ]
    if sensitive and isinstance(value, (int, float)) and not isinstance(value, bool):
        return _hash_of(repr(value))
    return value


class UnauthorizedAuditor:
    """Rate-limited audit rows for 401s: one bounded row, seat unknown.

    An unauthenticated spray must not be able to fill the audit log, so at
    most `limit` rows land per rolling window.  No presented token material
    is ever recorded - only a sanitized action name.
    """

    def __init__(self, limit: int = 10, window_s: float = 60.0, clock=time.monotonic):
        self._limit = limit
        self._window_s = window_s
        self._clock = clock
        self._stamps: list[float] = []
        self._lock = threading.Lock()

    def record(self, audit: "AuditLog", action: object) -> bool:
        now = self._clock()
        with self._lock:
            self._stamps = [t for t in self._stamps if now - t < self._window_s]
            if len(self._stamps) >= self._limit:
                return False
            self._stamps.append(now)
        safe_action = action if isinstance(action, str) else ""
        if find_secret(safe_action):
            safe_action = "<credential-shaped>"
        try:
            audit.record("unknown", safe_action[:64], "", "unauthenticated", {})
        except OSError:
            return False
        return True


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte or raise:  os.write may legally write short."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write to audit log")
        view = view[written:]


class AuditLog:
    def __init__(self, path: str):
        self._path = path

    def record(self, seat: str, action: str, repo: str, result: str, detail: dict) -> None:
        safe_detail = {}
        budget = [MAX_NODES, MAX_TEXT_BYTES]
        for key, value in detail.items():
            cleaned = _clean(value, key, 0, budget)
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
            _write_all(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
