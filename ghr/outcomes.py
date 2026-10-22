"""File-backed ok / refused / unknown outcome records for relay writes.

A non-idempotent GitHub write (open a PR, comment, merge, dispatch) can fail
with its outcome unknown:  a transport error or 5xx can arrive after GitHub
accepted it, and a blind retry would duplicate the write.  Every write action
gets a stable idempotency key (seat + action + canonical arguments);  the
relay records the outcome here and refuses to replay a key whose outcome is
unknown.  A definite refusal releases the key, so a rate-limited or rejected
call can simply be tried again.  A successful replay returns the stored
result without touching GitHub twice.

The file is rewritten atomically (temp file, fsync, rename, fsync of the
directory).  A crash between the GitHub call and the record leaves the key
"pending", which is loaded as "unknown":  fail closed, never replay.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time

MAX_ENTRIES = 5000
STATES = ("pending", "ok", "refused", "unknown")


def key_for(seat: str, action: str, args: dict) -> str:
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{seat}\0{action}\0{canonical}".encode()).hexdigest()


class OutcomeStore:
    """Small durable map:  key -> {state, result, ts}.  A None path keeps it in memory."""

    def __init__(self, path: str | None = None, clock=time.time):
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._rows: dict[str, dict] = {}
        if path and os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    raw = json.load(fh)
            except (OSError, ValueError):
                raw = {}
            if isinstance(raw, dict):
                for key, row in raw.items():
                    if isinstance(row, dict) and row.get("state") in STATES:
                        self._rows[key] = row
            for row in self._rows.values():
                if row["state"] == "pending":
                    row["state"] = "unknown"  # crashed mid-write: the outcome is unknown

    def begin(self, key: str) -> dict | None:
        """Mark a key in flight;  return the existing row when the key is already taken."""
        with self._lock:
            existing = self._rows.get(key)
            if existing and existing["state"] in ("ok", "unknown", "pending"):
                return existing
            self._rows[key] = {"state": "pending", "ts": self._clock()}
            self._save()
            return None

    def finish(self, key: str, state: str, result: dict | None = None) -> None:
        with self._lock:
            row: dict = {"state": state, "ts": self._clock()}
            if result is not None:
                row["result"] = result
            self._rows[key] = row
            self._trim()
            self._save()

    def _trim(self) -> None:
        if len(self._rows) > MAX_ENTRIES:
            oldest = sorted(self._rows.items(), key=lambda kv: kv[1].get("ts", 0))
            for key, _ in oldest[: len(self._rows) - MAX_ENTRIES]:
                del self._rows[key]

    def _save(self) -> None:
        if not self._path:
            return
        tmp = self._path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(self._rows, sort_keys=True).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self._path)
        dirfd = os.open(os.path.dirname(os.path.abspath(self._path)) or ".", os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
