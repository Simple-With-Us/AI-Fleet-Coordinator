"""Durable, private metadata-only delivery for notes committed to Apple Notes.

The spool contains canonical note IDs and generations, never note bodies or credentials.
Delivery is at least once; canonical note IDs and compare-before-ack prevent duplicate
documents and prevent an older acknowledgement from removing a newer update.
"""

import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

MAX_BODY = 400 * 1024
MAX_RECORD = 512 * 1024
MAX_PENDING = 1000
MAX_SPOOL = 64 * 1024 * 1024
PENDING = re.compile(r"^[0-9a-f]{64}\.json$")


class OutboxError(RuntimeError):
    pass


class DeliveryError(OutboxError):
    """Only fixed diagnostic codes cross the delivery boundary."""
    def __init__(self, category, code):
        self.category = category if category in {"retryable", "permanent", "outcome-unknown"} else "outcome-unknown"
        self.code = code if code in {"provider-contract-invalid", "delivery-http-retryable", "delivery-http-permanent", "delivery-outcome-unknown", "note-secret-gate-refused"} else "delivery-outcome-unknown"
        super().__init__(self.code)


def delivery_failure(error):
    if isinstance(error, DeliveryError):
        return error
    pending = [error]
    seen = set()
    for _ in range(8):
        if not pending:
            break
        current_error = pending.pop(0)
        if id(current_error) in seen:
            continue
        seen.add(id(current_error))
        for field in ("status", "code"):
            status = getattr(current_error, field, None)
            if type(status) is int and (status in (408, 429) or 500 <= status <= 599):
                return DeliveryError("retryable", "delivery-http-retryable")
            if type(status) is int and 400 <= status <= 499:
                return DeliveryError("permanent", "delivery-http-permanent")
        for field in ("__cause__", "__context__"):
            nested = getattr(current_error, field, None)
            if isinstance(nested, BaseException):
                pending.append(nested)
    return DeliveryError("outcome-unknown", "delivery-outcome-unknown")


def default_root():
    return Path.home() / ".local/state/fleet-rag-note-outbox"


def _key(doc_id):
    if not isinstance(doc_id, str) or not re.fullmatch(r"note/[A-Za-z0-9./_-]{1,220}", doc_id):
        raise OutboxError("invalid-note-id")
    return hashlib.sha256(doc_id.encode()).hexdigest()


@contextmanager
def _locked(root):
    root = Path(root).absolute()
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    lock = None
    try:
        for component in root.parts[1:]:
            try:
                child = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
            except FileNotFoundError:
                os.mkdir(component, mode=0o700, dir_fd=directory)
                child = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
            metadata = os.fstat(child)
            if metadata.st_uid not in (0, os.geteuid()):
                os.close(child)
                raise OutboxError("unsafe-outbox-parent")
            os.close(directory)
            directory = child
        metadata = os.fstat(directory)
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
            raise OutboxError("unsafe-outbox-root")
        lock = os.open(
            "outbox.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        metadata = os.fstat(lock)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
        ):
            raise OutboxError("unsafe-outbox-lock")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield directory
    finally:
        if lock is not None:
            os.close(lock)
        os.close(directory)


def _read(directory, name, maximum=MAX_RECORD):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or info.st_size > maximum
        ):
            raise OutboxError("unsafe-outbox-record")
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise OutboxError("oversized-outbox-record")
    try:
        return json.loads(data)
    except (ValueError, UnicodeError):
        raise OutboxError("invalid-outbox-record") from None


def _pending_record(name, record):
    allowed = {
        "schema",
        "doc_id",
        "version",
        "attempts",
        "next_attempt",
        "queued_at",
        "cancelled",
        "state",
        "failure_code",
        "delivery_state",
    }
    if not isinstance(record, dict) or set(record) - allowed or record.get("schema") != 2:
        raise OutboxError("invalid-outbox-record")
    if _key(record.get("doc_id")) + ".json" != name or not re.fullmatch(
        r"[0-9a-f]{32}", str(record.get("version", ""))
    ):
        raise OutboxError("invalid-outbox-record")
    for field in ("next_attempt", "queued_at"):
        value = record.get(field, 0)
        if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
            raise OutboxError("invalid-outbox-record")
    attempts = record.get("attempts", 0)
    if type(attempts) is not int or not 0 <= attempts <= 1000:
        raise OutboxError("invalid-outbox-record")
    return record


def _quarantine(directory, name):
    target = ".quarantine-" + uuid.uuid4().hex
    os.rename(name, target, src_dir_fd=directory, dst_dir_fd=directory)
    _atomic(
        directory,
        "quarantine-" + name,
        {
            "schema": 2,
            "record": name,
            "state": "quarantined",
            "reason": "invalid-or-unsafe-outbox-record",
            "preserved_as": target,
        },
    )


def _atomic(directory, name, record, maximum=MAX_RECORD):
    data = json.dumps(record, sort_keys=True, ensure_ascii=False).encode()
    if len(data) > maximum:
        raise OutboxError("oversized-outbox-record")
    temporary = ".pending-" + uuid.uuid4().hex
    fd = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory
    )
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory)
        except FileNotFoundError:
            pass


def prepare_document(document):
    from simple_tools.secretscan import scan

    from .scrub import gitleaks_flagged, scrub

    data = asdict(document)
    original = [data]
    while original:
        value = original.pop()
        if isinstance(value, str):
            # JSON escaping changes newlines and can hide header/assignment
            # patterns, so inspect each exact original string before encoding.
            if scan(value):
                raise DeliveryError("permanent", "note-secret-gate-refused")
        elif isinstance(value, dict):
            original.extend(value.keys())
            original.extend(value.values())
        elif isinstance(value, (list, tuple)):
            original.extend(value)
    data.pop("extra", None)
    for name, value in data.items():
        if isinstance(value, str):
            data[name] = scrub(value)[0]
    if data.get("source") != "apple-note" or len(data["text_markdown"].encode()) > MAX_BODY:
        raise OutboxError("invalid-note-document")
    _key(data["doc_id"])
    serialized = json.dumps(data, sort_keys=True, ensure_ascii=False)
    if scan(serialized) or shutil.which("gitleaks") is None:
        raise OutboxError("note-secret-gate-unavailable-or-refused")
    # The existing gitleaks interface needs a private staged file; only scrubbed text reaches it.
    with tempfile.TemporaryDirectory(prefix="fleet-note-scan-") as temporary:
        target = Path(temporary) / "note.jsonl"
        target.write_text(serialized + "\n")
        target.chmod(0o600)
        if gitleaks_flagged(str(target)):
            raise OutboxError("note-secret-gate-refused")
    return data


def enqueue_id(doc_id, root=None, now=None):
    root = default_root() if root is None else Path(root)
    from simple_tools.secretscan import scan

    key = _key(doc_id)
    if scan(doc_id):
        raise OutboxError("note-id-secret-gate-refused")
    version = uuid.uuid4().hex
    stamp = time.time() if now is None else now
    record = {
        "schema": 2,
        "doc_id": doc_id,
        "version": version,
        "attempts": 0,
        "next_attempt": 0,
        "queued_at": stamp,
    }
    with _locked(root) as directory:
        _enqueue_locked(directory, key, record)
    return {"state": "queued", "key": key, "version": version}


def _enqueue_locked(directory, key, record):
    """Enqueue one record while the spool lock is already held.

    Orphaned cancelled-sentinel temp files (a crash between create and
    rename in cancel()) are removed here and never count against the spool.
    """
    try:
        os.unlink("cancelled-" + key, dir_fd=directory)
    except FileNotFoundError:
        pass
    entries = os.listdir(directory)
    # First pass:  collect the cancelled keys before any counting, so a
    # pending file listed before its sentinel is still filtered.
    cancelled_keys = set()
    for name in entries:
        if name.startswith("cancelled-") and ".tmp-" in name:
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass
            continue
        if name.startswith("cancelled-"):
            cancelled_keys.add(name[10:])
    names = []
    size = 0
    for name in entries:
        if name == key + ".json":
            continue
        if name.startswith("cancelled-"):
            # Sentinels are empty markers, but their bytes still count
            # toward the spool so creating them is never free.
            size += os.stat(name, dir_fd=directory, follow_symlinks=False).st_size
            continue
        if PENDING.fullmatch(name) and name[:-5] in cancelled_keys:
            continue
        size += os.stat(name, dir_fd=directory, follow_symlinks=False).st_size
        if PENDING.fullmatch(name):
            names.append(name)
    if len(names) >= MAX_PENDING or size + len(json.dumps(record).encode()) > MAX_SPOOL:
        raise OutboxError("note-outbox-full")
    _atomic(directory, key + ".json", record)


def current(root, key, version):
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        return False
    with _locked(root) as directory:
        record = _read(directory, key + ".json")
        return bool(record and record.get("version") == version and not record.get("cancelled"))


@contextmanager
def _delivery_lock(root, key):
    # Separate from the short spool lock: held through provider reads, upsert, proof and ack.
    with _locked(root) as directory:
        fd = os.open(
            key + ".delivery.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=directory
        )
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            os.close(fd)
            raise OutboxError("unsafe-delivery-lock")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def drain(deliver, root=None, now=None, limit=2, only_key=None):
    """Deliver bounded due records; ``deliver`` must return receiver-verified True only."""
    root = default_root() if root is None else Path(root)
    stamp = time.time() if now is None else now
    quarantined = 0
    item_failed = 0
    permanent = 0
    if only_key is not None and not re.fullmatch(r"[0-9a-f]{64}", only_key):
        raise OutboxError("invalid-outbox-key")
    with _locked(root) as directory:
        names = sorted(name for name in os.listdir(directory) if PENDING.fullmatch(name))[
            :MAX_PENDING
        ]
        pending = []
        for name in names:
            if only_key is not None and name != only_key + ".json":
                continue
            try:
                record = _pending_record(name, _read(directory, name))
            except (OSError, ValueError, TypeError, OutboxError):
                try:
                    _quarantine(directory, name)
                    quarantined += 1
                except Exception:
                    item_failed += 1
                continue
            except Exception:
                item_failed += 1
                continue
            if record and record.get("cancelled"):
                try:
                    os.unlink(name, dir_fd=directory)
                    os.fsync(directory)
                except Exception:
                    item_failed += 1
                continue
            if record and record.get("delivery_state") == "permanent":
                permanent += 1
            if record and record.get("delivery_state") != "permanent" and record.get("next_attempt", 0) <= stamp:
                pending.append((name, record))
            if len(pending) >= limit:
                break
    result = {"verified": 0, "pending": 0, "superseded": 0, "quarantined": quarantined}
    if item_failed:
        result["item_failed"] = item_failed
    if permanent:
        result["permanent"] = permanent
    for name, record in pending:
        key = name[:-5]
        try:
            with _delivery_lock(root, key):
                if not current(root, key, record["version"]):
                    result["superseded"] += 1
                    continue
                failure = None
                try:
                    verified = deliver(record) is True
                except Exception as error:
                    failure = delivery_failure(error)
                    verified = False
                with _locked(root) as directory:
                    latest = _read(directory, name)
                    if not latest or latest.get("version") != record.get("version"):
                        result["superseded"] += 1
                        continue
                    if verified:
                        # Receipt first: a crash before unlink retries an idempotent canonical upsert.
                        _atomic(
                            directory,
                            "receipt-" + name,
                            {
                                "schema": 2,
                                "doc_id": record["doc_id"],
                                "version": record["version"],
                                "state": "receiver-verified",
                                "verified_at": stamp,
                            },
                        )
                        os.unlink(name, dir_fd=directory)
                        os.fsync(directory)
                        result["verified"] += 1
                    else:
                        if failure is not None:
                            record["failure_code"] = failure.code
                            record["delivery_state"] = failure.category
                            result[failure.category] = result.get(failure.category, 0) + 1
                        record["attempts"] = min(int(record.get("attempts", 0)) + 1, 1000)
                        record["next_attempt"] = stamp + min(
                            3600, 30 * 2 ** min(record["attempts"] - 1, 7)
                        )
                        _atomic(directory, name, record)
                        result["pending"] += 1
        except BlockingIOError:
            result["pending"] += 1
        except Exception:
            # Fixed metadata only; an arbitrary exception may contain provider bodies or secrets.
            result["pending"] += 1
            result["item_failed"] = result.get("item_failed", 0) + 1
    return result


def cancel(doc_id, root=None):
    """A committed explicit Notes delete prevents a queued older body from being retried."""
    root = default_root() if root is None else Path(root)
    key = _key(doc_id)
    with _locked(root) as directory:
        _atomic(
            directory,
            key + ".json",
            {
                "schema": 2,
                "doc_id": doc_id,
                "version": uuid.uuid4().hex,
                "cancelled": True,
                "state": "source-deleted",
            },
        )
        # The sentinel is a delete marker:  write it through a temp file and
        # an atomic rename so a crash never leaves a torn marker behind.
        sentinel_tmp = "cancelled-" + key + ".tmp-" + uuid.uuid4().hex
        fd = os.open(sentinel_tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.rename(sentinel_tmp, "cancelled-" + key, src_dir_fd=directory, dst_dir_fd=directory)
        except BaseException:
            try:
                os.unlink(sentinel_tmp, dir_fd=directory)
            except FileNotFoundError:
                pass
            raise
        os.fsync(directory)


def reconcile_coding(root=None, metadata_reader=None, limit=100):
    """Observe Coding metadata only; queue changed IDs without bulk reading note bodies.

    The first pass establishes a baseline, leaving historical backfill an explicit action.
    Missing IDs never authorize source deletion or corpus pruning.
    """
    from .notes_export import list_current_coding_metadata

    root = default_root() if root is None else Path(root)
    rows = (list_current_coding_metadata if metadata_reader is None else metadata_reader)()
    if not isinstance(rows, list) or len(rows) > 10000:
        raise OutboxError("coding-metadata-too-large")
    observed = {}
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"id", "name", "modified", "created"}
            or not all(isinstance(value, str) and len(value) <= 4096 for value in row.values())
        ):
            raise OutboxError("coding-metadata-invalid")
        doc_id = "note/" + row["id"].removeprefix("x-coredata://")
        _key(doc_id)
        if doc_id in observed:
            raise OutboxError("coding-metadata-duplicate-id")
        observed[doc_id] = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
    with _locked(root) as directory:
        previous = _read(directory, "coding-metadata.json", maximum=2 * 1024 * 1024)
        if previous is None:
            _atomic(
                directory,
                "coding-metadata.json",
                {"schema": 1, "fingerprints": observed, "historical_backfill_required": True},
                maximum=2 * 1024 * 1024,
            )
            return {"baseline_only": True, "observed": len(observed), "queued": 0}
        if not isinstance(previous, dict) or not isinstance(previous.get("fingerprints"), dict):
            raise OutboxError("coding-metadata-state-invalid")
        fingerprints = previous["fingerprints"]
        changed = [
            doc_id for doc_id in sorted(observed) if observed[doc_id] != fingerprints.get(doc_id)
        ]
    queued = 0
    for doc_id in changed[:limit]:
        key = _key(doc_id)
        # Read, decide and enqueue under one lock:  an interleaved cancel or
        # drain between the read and the enqueue must not resurrect a note.
        with _locked(root) as directory:
            pending = _read(directory, key + ".json")
            if (
                not pending
                or pending.get("cancelled")
                or pending.get("delivery_state") == "permanent"
            ):
                # A permanently failed delivery must not block a later update
                # to the same note from being enqueued again.
                _enqueue_locked(
                    directory,
                    key,
                    {
                        "schema": 2,
                        "doc_id": doc_id,
                        "version": uuid.uuid4().hex,
                        "attempts": 0,
                        "next_attempt": 0,
                        "queued_at": time.time(),
                    },
                )
                queued += 1
        with _locked(root) as directory:
            latest = _read(directory, "coding-metadata.json", maximum=2 * 1024 * 1024)
            if not isinstance(latest, dict) or not isinstance(latest.get("fingerprints"), dict):
                raise OutboxError("coding-metadata-state-invalid")
            latest["fingerprints"][doc_id] = observed[doc_id]
            _atomic(directory, "coding-metadata.json", latest, maximum=2 * 1024 * 1024)
    return {
        "baseline_only": False,
        "observed": len(observed),
        "changed": len(changed),
        "queued": queued,
    }
