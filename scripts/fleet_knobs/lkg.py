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

Each Infisical folder has its own copy and its own failed-fetch marker, because each is fetched
on its own and each copy's age must be the age of its own fetch.  The root folder keeps the
original names (``lkg.json``, ``fetch-failed.json``) and shape;  another folder's files carry its
name (``lkg-wake-router.json`` for /wake-router), and its copy records the folder and is refused
for any other.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from collections.abc import Iterable

from .registry import FOLDER_PATTERN, ROOT_FOLDER
from .source import ENVIRONMENT, PROJECT_ID

SCHEMA = 1
MAX_AGE_SECONDS = 48 * 3600
REFRESH_SECONDS = 3600  # rewrite an unchanged copy this often to keep its age honest
MAX_BYTES = 256 * 1024


def state_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".local", "state", "fleet-knobs")


def _suffix(folder: str) -> str:
    """The empty string for the root folder, else "-" and the folder's name (``-wake-router``).

    Segments are joined with ".", which FOLDER_PATTERN never allows inside a segment, so two
    folders never share a file:  /a/b is ``-a.b`` and /a--b is ``-a--b``."""
    if not FOLDER_PATTERN.match(folder):
        raise ValueError("not a knob folder")
    return "" if folder == ROOT_FOLDER else "-" + folder.strip("/").replace("/", ".")


def path(*, folder: str = ROOT_FOLDER) -> str:
    return os.path.join(state_dir(), "lkg%s.json" % _suffix(folder))


def read(now: float | None = None, *, folder: str = ROOT_FOLDER) -> tuple[dict | None, str]:
    """(document, note) for one folder's copy.  The document is None when there is no usable
    copy;  the note says why in one token (``absent``, ``unsafe``, ``unreadable``,
    ``wrong-project``, ``wrong-folder``, ``expired``) or is ``ok``."""
    now = time.time() if now is None else now
    target = path(folder=folder)
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
    # The root copy has never carried a folder, so a missing one means the root.
    if doc.get("path", ROOT_FOLDER) != folder:
        return None, "wrong-folder"
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
    knobs: dict[str, str],
    secret_values: Iterable[str] = (),
    now: float | None = None,
    *,
    folder: str = ROOT_FOLDER,
) -> None:
    """Atomically replace one folder's copy with `knobs` (name -> raw string).

    Raises ValueError, naming no value, if any of `secret_values` appears in the bytes."""
    now = time.time() if now is None else now
    target = path(folder=folder)
    doc = {
        "schema": SCHEMA,
        "project": PROJECT_ID,
        "environment": ENVIRONMENT,
        "saved_epoch": now,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "knobs": dict(sorted(knobs.items())),
    }
    if folder != ROOT_FOLDER:
        doc["path"] = folder
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
        os.replace(tmp, target)
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
# its own fetch and goes straight to the copy, so a broken service-token path costs one timeout
# across a burst of processes instead of one each.  Same trust
# rules as the copy:  a marker that is a symlink, someone else's, readable by group or other
# or malformed is ignored, and so the fetch runs.

FAILURE_TTL_SECONDS = 60.0
FAILURE_MAX_BYTES = 1024
_REASON_OK = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_:")


def failure_path(*, folder: str = ROOT_FOLDER) -> str:
    return os.path.join(state_dir(), "fetch-failed%s.json" % _suffix(folder))


def _clean_reason(reason: str) -> str:
    # 128 keeps the whole reason token:  the automation-env token is 68 characters, and the
    # old 64-character cap cut the missing variable's name off mid-word.
    text = str(reason or "unknown").lower()[:128]
    return "".join(c if c in _REASON_OK else "-" for c in text) or "unknown"


def recent_failure(now: float | None = None, *, folder: str = ROOT_FOLDER) -> str | None:
    """The reason token of a failed fetch of `folder` less than FAILURE_TTL_SECONDS old, else
    None."""
    now = time.time() if now is None else now
    try:
        fd = os.open(failure_path(folder=folder), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
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


def record_failure(reason: str, now: float | None = None, *, folder: str = ROOT_FOLDER) -> None:
    """Atomically write one folder's marker (0600):  a timestamp and a reason token, nothing
    else."""
    now = time.time() if now is None else now
    target = failure_path(folder=folder)
    blob = json.dumps({"failed_epoch": now, "reason": _clean_reason(reason)}).encode("utf-8")
    _ensure_dir()
    fd, tmp = tempfile.mkstemp(prefix=".failed.", suffix=".tmp", dir=state_dir())
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob + b"\n")
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def clear_failure(*, folder: str = ROOT_FOLDER) -> None:
    """Forget one folder's marker after a good fetch of it."""
    try:
        os.unlink(failure_path(folder=folder))
    except FileNotFoundError:
        pass
