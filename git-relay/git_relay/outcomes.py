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
  effect.  When the store is full of held rows, the oldest unknown rows move
  to an append-only archive (evidence preserved verbatim) and their keys to
  a durable index that begin() still refuses on, so capacity no longer
  strangles new writes and no side effect is ever re-armed.  Only an
  explicit reconciliation finish() releases an archived key;  nothing is
  dropped because time passed or state merely looks settled.
- Every accepted write attempt carries a random correlation id ("corr"),
  recorded in its row and available for the audit log:  an exact,
  non-disclosing join between an audit entry and its outcome row that no
  argument content can be brute-forced from.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid

MAX_ENTRIES = 5000
MAX_CAPACITY = 50_000
OK_TTL_SECONDS = 24 * 3600
# Rows archived in one capacity event:  enough headroom that a host at the
# wall does not pay archive I/O on every write, small enough that the live
# map stays the working set.
ARCHIVE_BATCH = 250
STATES = ("pending", "ok", "refused", "unknown")

_KEY_HEX = set("0123456789abcdef")


class OutcomeStoreError(RuntimeError):
    """The persisted registry is unreadable or invalid;  refuse to continue."""


def key_for(seat: str, action: str, args: dict) -> str:
    if action == "workflow_dispatch" and args.get("intent_id"):
        args = {"intent_id": args["intent_id"]}
    if action == "wf_file_delete":
        args = {k: args[k] for k in ("repo", "path", "branch", "prior_sha")}
    if action == "wf_labels_add":
        args = {**args, "labels": sorted(x.casefold() for x in args["labels"])}
    if action == "wf_label_update":
        args = {
            k: args[k] for k in ("repo", "expected_label_id", "new_name", "color", "description")
        }
        args = {**args, "color": args["color"].lower()}
    if action == "wf_file_rename":
        args = {
            k: args[k]
            for k in (
                "repo",
                "source_path",
                "destination_path",
                "branch",
                "expected_head_sha",
                "prior_sha",
            )
        }
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

    def __init__(self, path: str | None = None, clock=time.time, *, max_entries=None):
        if max_entries is None:
            max_entries = MAX_ENTRIES
        if type(max_entries) is not int or not 0 < max_entries <= MAX_CAPACITY:
            raise ValueError("max_entries must be a positive integer within the supported limit")
        self._path = path
        self._clock = clock
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._rows: dict[str, dict] = {}
        self._archive_path = f"{path}.archive.jsonl" if path else None
        self._index_path = f"{path}.archive-index.json" if path else None
        # key -> {"state", "ts", "corr"?} for every archived row:  the durable
        # duplicate-denial set.  Rebuilt from the archive itself when the index
        # is missing or unreadable;  a key here refuses exactly as a live held
        # row does.
        self._archived: dict[str, dict] = {}
        if path and os.path.exists(path):
            self._rows = self._load_with_recovery(path)
            for row in self._rows.values():
                if row["state"] == "pending":
                    row["state"] = "unknown"  # crashed mid-write: the outcome is unknown
        if path and (os.path.exists(self._index_path) or os.path.exists(self._archive_path)):
            self._archived = self._load_archive()

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

    def _archive_meta(self, key: str, row: dict) -> dict:
        meta = {"state": row.get("state"), "ts": row.get("ts", 0)}
        if row.get("corr"):
            meta["corr"] = row["corr"]
        return meta

    def _load_archive(self) -> dict:
        """The archived-keys denial set, folded from the archive log and the index.

        The archive is the evidence of record, a log folded in order:  an
        entry's row holds the key, a release marker (appended by a
        reconciliation finish, fsynced before the index dropped the key)
        releases it again.  The archive is validated on every load:  a torn
        final line (an interrupted append) is quarantined beside the archive
        and truncated away in place, so a later append never glues onto it;
        corruption anywhere else fails closed naming the path.  Rows in a torn
        tail were never index-confirmed, so their live rows survived the crash
        and still refuse through the live map loaded above - never re-armed.

        The effective denial set is (index union archive-held) minus
        archive-released:  an index that lost keys never admits their
        duplicates, a stale index entry left by a crash before the index write
        never denies a reconciled key forever, and a key with no archive trace
        at all stays denied (its evidence may sit in a quarantined tail).  No
        archived row is ever silently dropped, and no archived key is ever
        silently released.
        """
        indexed: dict[str, dict] | None = None
        try:
            with open(self._index_path, encoding="utf-8") as fh:
                raw = json.load(fh)
            if not isinstance(raw, dict):
                raise ValueError("index is not a JSON object")
            out = {}
            for key, meta in raw.items():
                if (
                    not isinstance(key, str)
                    or len(key) != 64
                    or any(ch not in _KEY_HEX for ch in key)
                    or not isinstance(meta, dict)
                    or meta.get("state") not in STATES
                    or isinstance(meta.get("ts"), bool)
                    or not isinstance(meta.get("ts"), (int, float))
                ):
                    raise ValueError(f"bad index entry for {key!r}")
                out[key] = meta
            indexed = out
        except (OSError, ValueError) as index_exc:
            if not os.path.exists(self._archive_path):
                if isinstance(index_exc, OSError) and not os.path.exists(self._index_path):
                    return {}
                raise OutcomeStoreError(
                    f"archive index {self._index_path!r} is unreadable and no archive "
                    "exists to rebuild it from; refusing to continue"
                ) from index_exc
        if not os.path.exists(self._archive_path):
            raise OutcomeStoreError(
                f"archive index {self._index_path!r} exists but archive "
                f"{self._archive_path!r} is missing; archived evidence cannot be "
                "verified, refusing to continue"
            )
        held: dict[str, dict] = {}
        released: set[str] = set()
        with open(self._archive_path, "rb") as fh:
            data = fh.read()
        lines = data.split(b"\n")
        # The split always leaves a final fragment:  empty when the file ends
        # clean, the torn line when an append was interrupted.  Neither is a row.
        tail_ok = not lines or lines[-1] == b""
        body = lines[:-1]
        for i, raw_line in enumerate(body):
            if not raw_line:
                continue
            try:
                entry = json.loads(raw_line.decode("utf-8"))
                key = entry["key"]
                if (
                    not isinstance(key, str)
                    or len(key) != 64
                    or any(ch not in _KEY_HEX for ch in key)
                ):
                    raise OutcomeStoreError(f"invalid key {key!r}")
                if "released_at" in entry:
                    released_at = entry.get("released_at")
                    marker_state = entry.get("state")
                    if (
                        isinstance(released_at, bool)
                        or not isinstance(released_at, (int, float))
                        or marker_state not in STATES
                    ):
                        raise OutcomeStoreError(f"invalid release marker for {key!r}")
                    held.pop(key, None)
                    released.add(key)
                else:
                    row = _validate_row(self._archive_path, key, entry["row"])
                    corr = row.get("corr")
                    if corr is not None and (not isinstance(corr, str) or not corr):
                        raise OutcomeStoreError(f"invalid archived corr for {key!r}")
                    held[key] = self._archive_meta(key, row)
                    released.discard(key)
            except (ValueError, KeyError, TypeError, OutcomeStoreError) as exc:
                raise OutcomeStoreError(
                    f"archive {self._archive_path!r} is corrupt at line {i + 1}: {exc}; "
                    "refusing to continue (evidence file untouched)"
                ) from exc
        if not tail_ok:
            self._quarantine(lines[-1])
            keep = sum(len(line) + 1 for line in body)
            fd = os.open(self._archive_path, os.O_WRONLY)
            try:
                os.ftruncate(fd, keep)
                os.fsync(fd)
            finally:
                os.close(fd)
        if indexed is None:
            self._archived = held
            self._save_index()
            return held
        folded = dict(indexed)
        for key, meta in held.items():
            folded.setdefault(key, meta)
        for key in released:
            folded.pop(key, None)
        if folded != indexed:
            self._archived = folded
            self._save_index()
        return folded

    def _quarantine(self, fragment: bytes) -> str:
        """Preserve an unreadable fragment beside the archive, durably and without
        ever overwriting earlier evidence:  a unique name (clock + uuid, exclusive
        create), fsynced before the caller mutates the log it came from."""
        while True:
            quarantined = f"{self._archive_path}.corrupt-{int(self._clock())}-{uuid.uuid4().hex}"
            try:
                fd = os.open(quarantined, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                break
            except FileExistsError:
                continue
        try:
            view = memoryview(fragment)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OutcomeStoreError(f"short write to quarantine {quarantined!r}")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        dirfd = os.open(os.path.dirname(os.path.abspath(quarantined)) or ".", os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
        return quarantined

    def _save_index(self) -> None:
        tmp = self._index_path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            view = memoryview(json.dumps(self._archived, sort_keys=True).encode())
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OutcomeStoreError(f"short write to archive index {self._index_path!r}")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self._index_path)
        dirfd = os.open(os.path.dirname(os.path.abspath(self._index_path)) or ".", os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)

    def _append_archive(self, payloads: list[bytes]) -> None:
        """Append complete lines to the archive log, fsynced, all-or-nothing.

        Two interruption guards.  A prior interrupted write can leave a torn
        tail (no trailing newline):  appending now would glue valid rows onto
        the fragment and strand the log mid-file-corrupt, so the fragment is
        quarantined and the log truncated back to its last intact line first.
        And if this append itself is interrupted, the log is truncated back to
        the offset recorded before its first write, so a batch lands whole or
        not at all and no later append can glue onto its fragment.  Callers
        hold the store lock.
        """
        fd = os.open(self._archive_path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            offset = os.lseek(fd, 0, os.SEEK_END)
            if offset:
                tail = os.pread(fd, min(offset, 1 << 20), max(0, offset - (1 << 20)))
                if not tail.endswith(b"\n"):
                    cut = tail.rfind(b"\n")
                    good_end = (offset - len(tail)) + cut + 1 if cut >= 0 else 0
                    self._quarantine(os.pread(fd, offset - good_end, good_end))
                    os.ftruncate(fd, good_end)
                    os.fsync(fd)
                    offset = good_end
            try:
                for payload in payloads:
                    view = memoryview(payload)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise OutcomeStoreError(
                                f"short write to archive {self._archive_path!r}"
                            )
                        view = view[written:]
                os.fsync(fd)
            except BaseException:
                os.ftruncate(fd, offset)
                os.fsync(fd)
                raise
        finally:
            os.close(fd)

    def corr_of(self, key: str) -> str | None:
        """The correlation id of the key's current row, live or archived, when it has one."""
        row = self._rows.get(key)
        if row is not None:
            return row.get("corr")
        meta = self._archived.get(key)
        return meta.get("corr") if meta else None

    def _archive_for_room(self) -> None:
        """Move the oldest unknown rows to the archive so begin() can admit one more write.

        Order is the safety contract:  archive evidence is appended and fsynced,
        the duplicate-denial index is written and fsynced, and only then are the
        rows removed from the live map.  A crash before the live save leaves the
        rows live (begin still refuses on them);  a crash after it leaves the
        keys in the index (begin still refuses).  Nothing is re-armed at any
        interruption point.
        """
        candidates = sorted(
            (row.get("ts", 0), key)
            for key, row in self._rows.items()
            if row.get("state") == "unknown"
        )[:ARCHIVE_BATCH]
        if not candidates:
            return
        payloads = [
            json.dumps(
                {"key": key, "archived_at": self._clock(), "row": self._rows[key]},
                sort_keys=True,
            ).encode()
            + b"\n"
            for _, key in candidates
        ]
        self._append_archive(payloads)
        for _, key in candidates:
            self._archived[key] = self._archive_meta(key, self._rows[key])
        self._save_index()
        for _, key in candidates:
            del self._rows[key]

    def begin(self, key: str, *, reapply_ok: bool = False, corr: str | None = None) -> dict | None:
        """Mark a key in flight;  return the existing row when the key is already taken.

        Refuses (OutcomeStoreError) when the registry is at capacity with no
        evictable row and no unknown row left to archive:  every remaining row
        is a held pending side effect, and silently dropping one could re-arm
        a duplicate write.
        """
        with self._lock:
            existing = self._rows.get(key)
            if existing and (
                existing["state"] in ("unknown", "pending")
                or existing["state"] == "ok"
                and not reapply_ok
            ):
                return existing
            if existing is None and key in self._archived:
                # An archived held row still refuses:  archival is storage,
                # never a release.  Only unknown rows are ever archived.
                meta = self._archived[key]
                held = {"state": meta["state"], "ts": meta["ts"], "archived": True}
                if meta.get("corr"):
                    held["corr"] = meta["corr"]
                if held["state"] in ("unknown", "pending"):
                    return held
            if existing and existing["state"] == "ok" and reapply_ok:
                # Only a verified success is replaceable.  Pending/unknown never re-arm.
                self._rows[key] = {
                    "state": "pending",
                    "ts": self._clock(),
                    "corr": corr or uuid.uuid4().hex,
                }
                self._save()
                return None
            self._trim(self._max_entries - 1)
            if len(self._rows) >= self._max_entries and self._path:
                self._archive_for_room()
            if len(self._rows) >= self._max_entries:
                raise OutcomeStoreError(
                    f"outcome store holds {self._max_entries} rows with none evictable; "
                    "reconcile the held unknown outcomes before new writes"
                )
            self._rows[key] = {
                "state": "pending",
                "ts": self._clock(),
                "corr": corr or uuid.uuid4().hex,
            }
            self._save()
            return None

    def finish(self, key: str, state: str, result: dict | None = None) -> None:
        with self._lock:
            row: dict = {"state": state, "ts": self._clock()}
            corr = (self._rows.get(key) or {}).get("corr") or (self._archived.get(key) or {}).get(
                "corr"
            )
            if corr:
                row["corr"] = corr
            if result is not None:
                row["result"] = result
            if key in self._archived:
                # A reconciliation outcome for an archived write.  Order:  the
                # release marker is appended to the archive log and fsynced
                # BEFORE the index drops the key, so a crash can only leave the
                # key denied (fail closed), never silently released;  an index
                # rebuilt from the archive folds the marker and stays released.
                if self._archive_path:
                    # On any failure the marker append rolls back and this
                    # method raises before the index drop:  the key stays
                    # denied (fail closed).
                    self._append_archive(
                        [
                            json.dumps(
                                {
                                    "key": key,
                                    "released_at": self._clock(),
                                    "state": state,
                                },
                                sort_keys=True,
                            ).encode()
                            + b"\n"
                        ]
                    )
                del self._archived[key]
                if self._index_path:
                    self._save_index()
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
        excess = len(self._rows) - (self._max_entries if limit is None else limit)
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
