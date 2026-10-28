"""File-backed ok / refused / unknown outcome records for relay writes.

A non-idempotent GitHub write (open a PR, comment, merge, dispatch) can fail
with its outcome unknown:  a transport error or 5xx can arrive after GitHub
accepted it, and a blind retry would duplicate the write.  Every write action
gets a stable idempotency key (seat + action + canonical arguments);  the
relay records the outcome here and refuses to replay a key whose outcome is
unknown.  A definite refusal releases the key, so a rate-limited or rejected
call can simply be tried again.  A successful replay returns the stored
result without touching GitHub twice.

Durability rules:

- Writes are atomic (temp file, fsync, rename, fsync of the directory).
- A crash between the GitHub call and the record leaves the key "pending",
  which is loaded as "unknown":  fail closed, never replay.
- A registry that cannot be read or fails validation blocks startup with an
  OutcomeStoreError naming the path and key - an empty stand-in would let a
  prior side effect run twice.  Before giving up, the loader tries the
  complete fsynced temp sibling left behind by an interrupted rename;  a
  good one is promoted into place while the corrupt file is preserved next
  to it as <path>.corrupt-<ts> for reconciliation.
- Capacity trimming deletes only releasable rows:  "ok" rows past the 24 h
  replay window and explicitly releasable "refused" rows, oldest first.
  pending/unknown rows are never evicted:  losing one would re-arm a side
  effect.  A store full of held rows refuses new writes (fail closed) until
  an operator reconciles the unknowns.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time

MAX_ENTRIES = 5000
OK_TTL_SECONDS = 24 * 3600
STATES = ("pending", "ok", "refused", "unknown")

_KEY_HEX = set("0123456789abcdef")


class OutcomeStoreError(RuntimeError):
    """The persisted registry is unreadable or invalid;  refuse to continue."""


def key_for(seat: str, action: str, args: dict) -> str:
    if action == "wf_file_delete":
        args = {k: args[k] for k in ("repo", "path", "branch", "prior_sha")}
    if action == "wf_labels_add":
        args = {**args, "labels": sorted(x.casefold() for x in args["labels"])}
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{seat}\0{action}\0{canonical}".encode()).hexdigest()


def _validate_row(path: str, key: object, row: object) -> dict:
    if not isinstance(key, str) or len(key) != 64 or any(ch not in _KEY_HEX for ch in key):
        raise OutcomeStoreError(f"outcome store {path!r} has an invalid key {key!r}")
    if not isinstance(row, dict):
        raise OutcomeStoreError(f"outcome store {path!r} has an invalid row for {key!r}")
    state = row.get("state")
    ts = row.get("ts")
    result = row.get("result")
    if (
        not isinstance(state, str)
        or state not in STATES
        or isinstance(ts, bool)
        or not isinstance(ts, (int, float))
        or (result is not None and not isinstance(result, dict))
    ):
        raise OutcomeStoreError(f"outcome store {path!r} has an invalid row for {key!r}")
    return row


def _load_rows(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, dict):
        raise OutcomeStoreError(f"outcome store {path!r} must contain a JSON object")
    return {key: _validate_row(path, key, row) for key, row in raw.items()}


class OutcomeStore:
    """Small durable map:  key -> {state, result, ts}.  A None path keeps it in memory."""

    def __init__(self, path: str | None = None, clock=time.time):
        self._path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._rows: dict[str, dict] = {}
        if path and os.path.exists(path):
            self._rows = self._load_with_recovery(path)
            for row in self._rows.values():
                if row["state"] == "pending":
                    row["state"] = "unknown"  # crashed mid-write: the outcome is unknown

    def _load_with_recovery(self, path: str) -> dict:
        try:
            return _load_rows(path)
        except (OSError, ValueError, OutcomeStoreError) as exc:
            corrupt = exc
        # The final name is damaged.  A write that was interrupted between the
        # fsynced temp file and the rename leaves a complete sibling:  promote
        # it, preserving the bad file for reconciliation.  Anything else fails
        # closed so a prior side effect can never be silently re-armed.
        tmp = path + ".tmp"
        try:
            rows = _load_rows(tmp)
        except (OSError, ValueError, OutcomeStoreError):
            if isinstance(corrupt, OutcomeStoreError):
                # The registry parsed far enough to name the precise bad key:
                # surface that error so reconciliation starts from specifics.
                raise corrupt
            raise OutcomeStoreError(
                f"outcome store {path!r} is unreadable or corrupt; refusing to continue"
            ) from corrupt
        preserved = f"{path}.corrupt-{int(self._clock())}"
        os.replace(path, preserved)
        self._rows = rows
        self._save()
        return rows

    def begin(self, key: str, *, reapply_ok: bool = False) -> dict | None:
        """Mark a key in flight;  return the existing row when the key is already taken.

        Refuses (OutcomeStoreError) when the registry is at capacity with no
        evictable row:  every remaining row is a held pending/unknown side
        effect, and silently dropping one could re-arm a duplicate write.
        """
        with self._lock:
            existing = self._rows.get(key)
            if existing and (
                existing["state"] in ("unknown", "pending")
                or existing["state"] == "ok"
                and not reapply_ok
            ):
                return existing
            if existing and existing["state"] == "ok" and reapply_ok:
                # Only a verified success is replaceable.  Pending/unknown never re-arm.
                self._rows[key] = {"state": "pending", "ts": self._clock()}
                self._save()
                return None
            self._trim(MAX_ENTRIES - 1)
            if len(self._rows) >= MAX_ENTRIES:
                raise OutcomeStoreError(
                    f"outcome store holds {MAX_ENTRIES} rows with none evictable; "
                    "reconcile the held unknown outcomes before new writes"
                )
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

    def _trim(self, limit: int | None = None) -> None:
        # ok rows outlive their usefulness once a real retry window has
        # passed;  evicting them keeps the registry bounded without re-arming
        # any side effect (an ok row never protects an unresolved write).
        cutoff = self._clock() - OK_TTL_SECONDS
        stale = [
            key
            for key, row in self._rows.items()
            if row.get("state") == "ok" and row.get("ts", 0) < cutoff
        ]
        for key in stale:
            del self._rows[key]
        excess = len(self._rows) - (MAX_ENTRIES if limit is None else limit)
        if excess <= 0:
            return
        removable = sorted(
            (row.get("ts", 0), key)
            for key, row in self._rows.items()
            if row.get("state") == "refused"
        )
        for _, key in removable[:excess]:
            del self._rows[key]

    def _save(self) -> None:
        if not self._path:
            return
        tmp = self._path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            # os.write may legally write short (ENOSPC, signals):  a truncated
            # payload renamed over the registry would corrupt it, so write the
            # whole buffer or fail loudly before the rename.
            view = memoryview(json.dumps(self._rows, sort_keys=True).encode())
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OutcomeStoreError(
                        f"short write to outcome store {self._path!r}; registry left untouched"
                    )
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self._path)
        dirfd = os.open(os.path.dirname(os.path.abspath(self._path)) or ".", os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
