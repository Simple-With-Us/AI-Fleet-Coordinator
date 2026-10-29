"""Opt-in destination-volume guard. Probe transport and byte policy are operator-owned.

A client filesystem is never evidence about a remote Qdrant volume. No probe is
cached, no shell is used, and probe failures never fall back to unguarded writes.
"""

from __future__ import annotations

import json
import math
import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass


class StorageGuardError(RuntimeError):
    pass


MAX_PROBE_BYTES = 16384


def bounded_probe(argv, timeout):
    """Bound stdout while running, discard stderr, terminate/reap on every exit."""
    if os.name != "posix":
        raise OSError("bounded probe requires POSIX process-group cleanup")
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, shell=False, start_new_session=True
    )
    deadline = time.monotonic() + timeout
    output = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                if not selector.select(remaining):
                    raise subprocess.TimeoutExpired(argv, timeout)
                chunk = os.read(proc.stdout.fileno(), min(4096, MAX_PROBE_BYTES + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > MAX_PROBE_BYTES:
                    raise ValueError("probe output limit exceeded")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(argv, timeout)
        if proc.wait(timeout=remaining) != 0:
            raise ValueError("probe failed")
        return bytes(output)
    finally:
        # The launcher may already have exited while descendants still hold stdout.
        # Our new session's group is private to this invocation. Clean it on success
        # as well as failure so a valid result cannot leave probe workers behind.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        finally:
            proc.wait()
            proc.stdout.close()


@dataclass(frozen=True)
class StorageGuard:
    endpoint: str
    collection: str
    storage_path: str
    device: str
    mount: str
    reserve_bytes: int
    write_allowance_bytes: int
    max_request_bytes: int
    probe_argv: tuple[str, ...]
    probe_timeout_seconds: float
    max_age_seconds: float

    @classmethod
    def from_env(cls, endpoint: str, collection: str):
        raw = os.environ.get("FLEET_RAG_STORAGE_GUARD")
        if raw is None:
            if os.environ.get("FLEET_RAG_STORAGE_GUARD_REQUIRED") == "1":
                raise StorageGuardError("destination storage guard configuration required")
            return None
        try:
            cfg = json.loads(raw)
            if not isinstance(cfg, dict):
                raise ValueError()
            guard = cls(**cfg)
            for key in ("reserve_bytes", "write_allowance_bytes", "max_request_bytes"):
                value = getattr(guard, key)
                if type(value) is not int or value <= 0:
                    raise ValueError()
            for key in ("probe_timeout_seconds", "max_age_seconds"):
                value = getattr(guard, key)
                if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                    raise ValueError()
            for key in ("endpoint", "collection", "storage_path", "device", "mount"):
                if not isinstance(getattr(guard, key), str) or not getattr(guard, key):
                    raise ValueError()
            if not guard.storage_path.startswith("/") or not guard.mount.startswith("/"):
                raise ValueError()
            if not isinstance(guard.probe_argv, (list, tuple)) or not guard.probe_argv:
                raise ValueError()
            if any(not isinstance(arg, str) or not arg for arg in guard.probe_argv):
                raise ValueError()
            if guard.endpoint != endpoint or guard.collection != collection:
                raise ValueError()
            return guard
        except (TypeError, ValueError):
            raise StorageGuardError(
                "invalid or mismatched destination storage guard policy"
            ) from None

    def check(self, request_bytes: int = 0):
        if request_bytes > self.max_request_bytes:
            raise StorageGuardError("write request exceeds configured storage allowance")
        try:
            output = bounded_probe(self.probe_argv, self.probe_timeout_seconds)
            info = json.loads(output)
            if not isinstance(info, dict):
                raise ValueError()
            for key in ("endpoint", "collection", "storage_path", "device", "mount"):
                if info.get(key) != getattr(self, key):
                    raise ValueError()
            stamp = info.get("observed_at")
            if type(stamp) not in (int, float) or not math.isfinite(stamp):
                raise ValueError()
            age = time.time() - stamp
            if age < 0 or age > self.max_age_seconds:
                raise ValueError()
            free = info.get("available_bytes")
            if type(free) is not int or free < 0:
                raise ValueError()
        except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
            raise StorageGuardError(
                "destination storage evidence unavailable, stale or mismatched"
            ) from None
        if free < self.reserve_bytes + self.write_allowance_bytes:
            raise StorageGuardError(
                "destination storage headroom below configured reserve plus write allowance"
            )
        return free
