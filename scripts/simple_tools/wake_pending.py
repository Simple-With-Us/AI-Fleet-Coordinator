"""Parked wakes for held chats (AFC-694, the Muse wake adapter).

When a chat is held, delivery cannot run a headless turn in it, so the adapter parks the wake here and answers `busy`:  the router waits on `busy` without using up an attempt.  The chat's own UserPromptSubmit hook takes parked wakes as prompt context on its next turn, which hands the wake over in process;  a later retry finds the wake id consumed (or in the transcript) and answers `sent` without running anything twice.

The spool is one JSON file per wake, `<state>/pending/<session-id>/<wake-id>.json`, plus one consumed ledger, `<state>/consumed.json`.  Everything is 0600 under a 0700 directory and is written atomically, standard library only.  A missing directory is no wakes, never an error;  a corrupt pending file is skipped and removed (it was written by this host's own delivery, so there is nothing to recover);  a corrupt ledger is forgotten (a wake may be parked again, never lost).

Python 3.11+, standard library only.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from collections.abc import Mapping
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


def state_dir(env: Mapping[str, str]) -> str:
    """The wake state dir for this host:  the same rule `muse_wake.state_dir` uses, so the
    adapter and the hook resolve the same spool from their own environments."""
    base = env.get("MUSE_WAKE_STATE") or os.path.join(
        str(env.get("HOME") or os.path.expanduser("~")), ".local", "state"
    )
    return os.path.join(base, "muse-wake")


def _safe_name(value: object, pattern: re.Pattern[str], what: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError("bad %s" % what)
    return value


def _private_dir(path: str) -> None:
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError:
        return
    try:
        if stat.S_IMODE(os.stat(path).st_mode) != 0o700:
            os.chmod(path, 0o700)
    except OSError:
        pass


def _atomic_write_json(path: str, record: Mapping[str, Any]) -> None:
    data = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
    tmp = path + ".tmp-%d" % os.getpid()
    with open(tmp, "wb") as fh:
        fh.write(data)
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def pending_dir(state: str) -> str:
    return os.path.join(state, PENDING_DIRNAME)


def _ledger_path(state: str) -> str:
    return os.path.join(state, CONSUMED_FILENAME)


def _read_ledger(state: str) -> dict[str, float]:
    try:
        with open(_ledger_path(state), encoding="utf-8") as fh:
            record = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(record, dict):
        return {}
    return {k: float(v) for k, v in record.items() if isinstance(v, (int, float))}


def is_consumed(state: str, wake_id: str) -> bool:
    """Whether this wake was already handed to its chat through the hook."""
    _safe_name(wake_id, _WAKE_ID_RE, "wake id")
    return wake_id in _read_ledger(state)


def mark_consumed(state: str, wake_id: str, now: float | None = None) -> None:
    """Record a handed-over wake, pruning entries older than the TTL."""
    _safe_name(wake_id, _WAKE_ID_RE, "wake id")
    at = time.time() if now is None else now
    _private_dir(state)
    ledger = {k: v for k, v in _read_ledger(state).items() if at - v < CONSUMED_TTL_SECONDS}
    ledger[wake_id] = at
    _atomic_write_json(_ledger_path(state), ledger)


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
    _private_dir(folder)
    _atomic_write_json(os.path.join(folder, wake_id + ".json"), {"wake_id": wake_id, "text": text})
    return True


def take(state: str, session_id: str) -> list[tuple[str, str]]:
    """Take and consume every parked wake for one chat, oldest filename first.  A file
    that cannot be read or parsed is removed and skipped, never handed over."""
    _safe_name(session_id, _SESSION_RE, "session id")
    folder = os.path.join(pending_dir(state), session_id)
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return []
    taken: list[tuple[str, str]] = []
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
            try:
                os.unlink(path)
            except OSError:
                pass
            continue
        text = record.get("text") if isinstance(record, dict) else None
        if not isinstance(text, str) or not text:
            try:
                os.unlink(path)
            except OSError:
                pass
            continue
        try:
            os.unlink(path)
        except OSError:
            continue
        mark_consumed(state, wake_id)
        taken.append((wake_id, text))
    return taken
