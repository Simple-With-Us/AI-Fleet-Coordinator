"""The last-known-good copy of the non-secret knobs, ``~/.local/state/fleet-knobs/lkg.json``.

A short-lived job that cannot reach Infisical reads this file instead and marks its report
stale.  Rules, all enforced here:

  * the directory is 0700 and the file 0600, both owned by the current user;  a file that is
    a symlink, owned by someone else, readable by group or other, or not a regular file is
    ignored, never trusted
  * only registered NON-SECRET knobs are written, as the raw strings Infisical held, and a
    secret value is checked for in the serialised bytes before they are written
  * a copy older than MAX_AGE_SECONDS is ignored, so an emergency brake set in Infisical
    cannot be hidden for long by an unreachable Infisical
  * writes are atomic (a temp file in the same directory, then rename)
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from collections.abc import Iterable

from .source import ENVIRONMENT, PROJECT_ID

SCHEMA = 1
MAX_AGE_SECONDS = 48 * 3600
REFRESH_SECONDS = 3600  # rewrite an unchanged copy this often to keep its age honest
MAX_BYTES = 256 * 1024


def state_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".local", "state", "fleet-knobs")


def path() -> str:
    return os.path.join(state_dir(), "lkg.json")


def read(now: float | None = None) -> tuple[dict | None, str]:
    """(document, note).  The document is None when there is no usable copy;  the note says
    why in one token (``absent``, ``unsafe``, ``unreadable``, ``wrong-project``, ``expired``)
    or is ``ok``."""
    now = time.time() if now is None else now
    target = path()
    try:
        fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None, "absent"
    except OSError:
        return None, "unsafe"
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > MAX_BYTES
        ):
            return None, "unsafe"
        if not _dir_is_private():
            return None, "unsafe"
        with os.fdopen(fd, "rb", closefd=False) as fh:
            raw = fh.read(MAX_BYTES + 1)
    except OSError:
        return None, "unreadable"
    finally:
        os.close(fd)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, "unreadable"
    if (
        not isinstance(doc, dict)
        or doc.get("schema") != SCHEMA
        or not isinstance(doc.get("knobs"), dict)
        or not isinstance(doc.get("saved_epoch"), (int, float))
    ):
        return None, "unreadable"
    if doc.get("project") != PROJECT_ID or doc.get("environment") != ENVIRONMENT:
        return None, "wrong-project"
    age = now - float(doc["saved_epoch"])
    if age < -300 or age > MAX_AGE_SECONDS:
        return None, "expired"
    return doc, "ok"


def age_seconds(doc: dict, now: float | None = None) -> int:
    now = time.time() if now is None else now
    return max(0, int(now - float(doc["saved_epoch"])))


def _dir_is_private() -> bool:
    try:
        info = os.lstat(state_dir())
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077


def _ensure_dir() -> None:
    target = state_dir()
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    try:
        os.mkdir(target, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(target)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise OSError("state directory is not ours")
    if info.st_mode & 0o077:
        os.chmod(target, 0o700)


def write(
    knobs: dict[str, str], secret_values: Iterable[str] = (), now: float | None = None
) -> None:
    """Atomically replace the copy with `knobs` (name -> raw string).

    Raises ValueError, naming no value, if any of `secret_values` appears in the bytes."""
    now = time.time() if now is None else now
    doc = {
        "schema": SCHEMA,
        "project": PROJECT_ID,
        "environment": ENVIRONMENT,
        "saved_epoch": now,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "knobs": dict(sorted(knobs.items())),
    }
    blob = json.dumps(doc, indent=1, sort_keys=True).encode("utf-8") + b"\n"
    for secret in secret_values:
        if secret and len(secret) >= 8 and secret.encode("utf-8") in blob:
            raise ValueError("a secret value would be written to the last-known-good copy")
    _ensure_dir()
    fd, tmp = tempfile.mkstemp(prefix=".lkg.", suffix=".tmp", dir=state_dir())
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path())
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- negative cache
#
# After a failed fetch, ``fetch-failed.json`` beside the copy records when and why (a reason
# token, never output and never a secret).  For FAILURE_TTL_SECONDS a sibling process skips
# its own fetch and goes straight to the copy, so a broken auth path (a locked keychain under
# launchd) costs one timeout across a burst of processes instead of one each.  Same trust
# rules as the copy:  a marker that is a symlink, someone else's, readable by group or other
# or malformed is ignored, and so the fetch runs.

FAILURE_TTL_SECONDS = 60.0
FAILURE_MAX_BYTES = 1024
_REASON_OK = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_:")


def failure_path() -> str:
    return os.path.join(state_dir(), "fetch-failed.json")


def _clean_reason(reason: str) -> str:
    text = str(reason or "unknown").lower()[:64]
    return "".join(c if c in _REASON_OK else "-" for c in text) or "unknown"


def recent_failure(now: float | None = None) -> str | None:
    """The reason token of a failed fetch less than FAILURE_TTL_SECONDS old, else None."""
    now = time.time() if now is None else now
    try:
        fd = os.open(failure_path(), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_size > FAILURE_MAX_BYTES
        ):
            return None
        if not _dir_is_private():
            return None
        with os.fdopen(fd, "rb", closefd=False) as fh:
            raw = fh.read(FAILURE_MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(doc, dict) or not isinstance(doc.get("failed_epoch"), (int, float)):
        return None
    age = now - float(doc["failed_epoch"])
    if age < -5 or age >= FAILURE_TTL_SECONDS:
        return None
    return _clean_reason(doc.get("reason", "unknown"))


def record_failure(reason: str, now: float | None = None) -> None:
    """Atomically write the marker (0600):  a timestamp and a reason token, nothing else."""
    now = time.time() if now is None else now
    blob = json.dumps({"failed_epoch": now, "reason": _clean_reason(reason)}).encode("utf-8")
    _ensure_dir()
    fd, tmp = tempfile.mkstemp(prefix=".failed.", suffix=".tmp", dir=state_dir())
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob + b"\n")
        os.replace(tmp, failure_path())
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def clear_failure() -> None:
    """Forget the marker after a good fetch."""
    try:
        os.unlink(failure_path())
    except FileNotFoundError:
        pass
