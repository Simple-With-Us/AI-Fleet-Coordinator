"""Parked wakes for held chats (AFC-694, the Muse wake adapter).

When a chat is held, delivery cannot run a headless turn in it, so the adapter parks the wake here and answers `busy`:  the router waits on `busy` without using up an attempt.  The chat's own UserPromptSubmit hook takes parked wakes as prompt context on its next turn, which hands the wake over in process;  a later retry finds the wake id consumed (or in the transcript) and answers `sent` without running anything twice.

The spool is one JSON file per wake, `<state>/pending/<session-id>/<wake-id>.json`, plus one consumed ledger, `<state>/consumed.json`.  Everything is 0600 under a 0700 directory and is written atomically, standard library only.  A missing directory is no wakes, never an error;  a corrupt pending file is skipped and removed (it was written by this host's own delivery, so there is nothing to recover);  a corrupt ledger is forgotten (a wake may be parked again, never lost).

Python 3.11+, standard library only.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
import time
from typing import Any

#: A pending file bigger than this is left alone:  wakes are short texts, and the hook
#: must never spend its timeout on a runaway file.
MAX_FILE_BYTES = 1 << 20
#: How long a consumed wake id is remembered, so a retry long after hand-over still
#: answers `sent` instead of running the turn again.
CONSUMED_TTL_SECONDS = 7 * 24 * 3600
PENDING_DIRNAME = "pending"
CONSUMED_FILENAME = "consumed.json"

_SESSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_WAKE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def state_dir(home: str) -> str:
    """The wake state dir for one host user:  a pure function of the home directory, so the adapter and the
    hook resolve the same spool no matter what each process carries in its environment.  (An environment
    override could split them:  the hook runs with a cleared environment, so no override is read here.)"""
    return os.path.join(home, ".local", "state", "muse-wake")


def _safe_name(value: object, pattern: re.Pattern[str], what: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError("bad %s" % what)
    return value


def _private_dir(path: str, base: str) -> None:
    """Make sure every level from `base` down to `path` exists and is 0700, refusing symlinks at each level.
    Nothing above `base` is created or touched.  Errors propagate:  the adapter answers a retryable `failed`
    and the hook skips the wake, never silently using a wrong directory."""
    if os.path.islink(base) or os.path.islink(path):
        raise OSError("refusing symlinked state dir")
    rest = os.path.relpath(path, base)
    if rest.startswith(".."):
        raise OSError("state dir escapes its base")
    head = base
    for part in [None, *([] if rest == "." else rest.split(os.sep))]:
        if part is not None:
            if part in (".", ".."):
                raise OSError("state dir escapes its base")
            _safe_name(part, _SESSION_RE, "path part")
            head = os.path.join(head, part)
        if os.path.islink(head):
            raise OSError("refusing symlinked state dir")
        os.makedirs(head, mode=0o700, exist_ok=True)
        if os.path.islink(head):
            raise OSError("refusing symlinked state dir")
        if stat.S_IMODE(os.stat(head).st_mode) != 0o700:
            os.chmod(head, 0o700)


def _atomic_write_json(directory: str, path: str, record: dict[str, Any]) -> None:
    """Write one JSON record atomically:  a unique temp file in the destination directory, flushed and fsynced
    before the rename, then the directory itself fsynced, so a crash cannot leave a truncated record behind."""
    data = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def pending_dir(state: str) -> str:
    return os.path.join(state, PENDING_DIRNAME)


def _ledger_path(state: str) -> str:
    return os.path.join(state, CONSUMED_FILENAME)


def _read_ledger_unlocked(state: str) -> dict[str, float]:
    try:
        with open(_ledger_path(state), encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(record, dict):
        return {}
    return {k: float(v) for k, v in record.items() if isinstance(v, (int, float))}


def _with_ledger_lock(state: str, fn: Any) -> Any:
    """Run `fn` with the ledger's host-local lock held, so the hook's take and the adapter's delivery cannot
    lose each other's wake ids in a concurrent read-modify-write."""
    _private_dir(state, state)
    lock_path = os.path.join(state, "consumed.lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return fn()
    finally:
        os.close(fd)


def _read_ledger(state: str) -> dict[str, float]:
    try:
        return _with_ledger_lock(state, lambda: _read_ledger_unlocked(state))
    except OSError:
        return {}


def is_consumed(state: str, wake_id: str) -> bool:
    """Whether this wake was already handed to its chat through the hook."""
    _safe_name(wake_id, _WAKE_ID_RE, "wake id")
    return wake_id in _read_ledger(state)


def mark_delivered(state: str, session_id: str, wake_id: str) -> bool:
    """Record a handed-over wake and remove its pending file, if any.  True when a
    pending file was removed.  The ledger mark stops a later retry from running the
    turn again, and removing the file stops the next prompt from injecting it again."""
    _safe_name(session_id, _SESSION_RE, "session id")
    _safe_name(wake_id, _WAKE_ID_RE, "wake id")
    removed = False
    path = os.path.join(pending_dir(state), session_id, wake_id + ".json")
    try:
        os.unlink(path)
        removed = True
    except OSError:
        pass
    mark_consumed(state, wake_id)
    return removed


def mark_consumed(state: str, wake_id: str, now: float | None = None) -> None:
    """Record a handed-over wake, pruning entries older than the TTL.  The read-prune-write runs under the
    ledger lock, so concurrent markers merge instead of losing each other."""
    _safe_name(wake_id, _WAKE_ID_RE, "wake id")
    at = time.time() if now is None else now

    def rewrite() -> None:
        ledger = {
            k: v for k, v in _read_ledger_unlocked(state).items() if at - v < CONSUMED_TTL_SECONDS
        }
        ledger[wake_id] = at
        _atomic_write_json(state, _ledger_path(state), ledger)

    _with_ledger_lock(state, rewrite)


def park(state: str, session_id: str, wake_id: str, text: str) -> bool:
    """Park one wake for a held chat.  True when parked (idempotent:  parking the same
    wake twice keeps one file);  False when the wake was already consumed, so the
    caller verifies the transcript instead of parking again."""
    _safe_name(session_id, _SESSION_RE, "session id")
    _safe_name(wake_id, _WAKE_ID_RE, "wake id")
    if not isinstance(text, str) or not text:
        raise ValueError("bad wake text")
    if is_consumed(state, wake_id):
        return False
    folder = os.path.join(pending_dir(state), session_id)
    _private_dir(folder, state)
    _atomic_write_json(
        folder, os.path.join(folder, wake_id + ".json"), {"wake_id": wake_id, "text": text}
    )
    return True


def _read_pending(state: str, session_id: str) -> list[tuple[str, str, str]]:
    """(wake_id, text, path) of every readable parked wake for one chat, oldest filename first.  Unreadable,
    oversized and empty files are left alone here:  `take` removes them, `peek` only skips them."""
    _safe_name(session_id, _SESSION_RE, "session id")
    folder = os.path.join(pending_dir(state), session_id)
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    found: list[tuple[str, str, str]] = []
    for name in names:
        if not name.endswith(".json"):
            continue
        wake_id = name[: -len(".json")]
        if not _WAKE_ID_RE.fullmatch(wake_id):
            continue
        path = os.path.join(folder, name)
        try:
            if os.path.getsize(path) > MAX_FILE_BYTES:
                continue
            with open(path, encoding="utf-8") as fh:
                record = json.load(fh)
        except (OSError, ValueError):
            continue
        text = record.get("text") if isinstance(record, dict) else None
        if not isinstance(text, str) or not text:
            continue
        found.append((wake_id, text, path))
    return found


def peek(state: str, session_id: str) -> list[tuple[str, str]]:
    """The parked wakes for one chat, without consuming anything:  what the hook shows before it emits."""
    return [(wake_id, text) for wake_id, text, _ in _read_pending(state, session_id)]


def consume(state: str, session_id: str, wake_ids: list[str]) -> None:
    """Consume parked wakes after they were handed over:  remove their files and mark the removed ids.  Unknown
    or already-gone ids are ignored, so emitting first and consuming after is safe to repeat, and a wake that
    was never shown is never marked delivered."""
    _safe_name(session_id, _SESSION_RE, "session id")
    for wake_id in wake_ids:
        _safe_name(wake_id, _WAKE_ID_RE, "wake id")
        try:
            os.unlink(os.path.join(pending_dir(state), session_id, wake_id + ".json"))
        except OSError:
            continue
        mark_consumed(state, wake_id)


def take(state: str, session_id: str) -> list[tuple[str, str]]:
    """Take and consume every parked wake for one chat, oldest filename first.  A file
    that cannot be read or parsed is removed and skipped, never handed over."""
    pending = _read_pending(state, session_id)
    folder = os.path.join(pending_dir(state), session_id)
    try:
        names = set(os.listdir(folder))
    except OSError:
        names = set()
    for _, _, path in pending:
        try:
            os.unlink(path)
        except OSError:
            pass
    for name in sorted(names):
        if not name.endswith(".json"):
            continue
        wake_id = name[: -len(".json")]
        if _WAKE_ID_RE.fullmatch(wake_id) and all(wake_id != seen for seen, _, _ in pending):
            try:
                os.unlink(os.path.join(folder, name))
            except OSError:
                pass
    for wake_id, _, _ in pending:
        mark_consumed(state, wake_id)
    return [(wake_id, text) for wake_id, text, _ in pending]
