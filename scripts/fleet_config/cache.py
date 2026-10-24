"""The last-known-good copy of the latest report, ``~/.local/state/fleet-config/report.json``.

A status run that cannot finish must not answer with nothing, and it must not answer with
a stale copy dressed up as fresh.  So the last good report is kept on disk and served
marked stale, with the fail-closed reason code ``stale`` on every result that is past its
TTL.

The trust rules mirror ``fleet_knobs/lkg.py`` exactly, because the same reasoning applies
to a file another process wrote:

  * the directory is 0700 and the file 0600, both owned by the current user;  a file that
    is a symlink, owned by someone else, readable by group or other, or not a regular file
    is ignored, never trusted
  * the copy is written atomically (a temp file in the same directory, then rename), and
    the directory is flushed after the rename, so a crash cannot lose the new entry
  * a copy older than ``MAX_AGE_SECONDS`` is ignored, so a broken run cannot hide behind
    an old copy forever
  * the copy never holds a secret value.  ``CheckResult`` has no field that could hold
    one, so the cached bytes cannot carry a credential, and nothing here adds a field
    that could.

Stdlib only, and Python 3.9 safe, matching ``fleet_knobs``.
"""

from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
import time
from datetime import datetime, timezone
from typing import Any

from . import contract
from .types import (
    CheckResult,
    CheckState,
    CheckType,
    Collector,
    ConfigKind,
    ExpectedState,
    NotImplementedRule,
    ObservedState,
    ProofLevel,
    ReasonCode,
    StatusReport,
    UncoveredSurface,
)

__all__ = [
    "CACHE_SCHEMA",
    "MAX_AGE_SECONDS",
    "MAX_BYTES",
    "cache_path",
    "cached_age_seconds",
    "load_report",
    "report_is_stale",
    "save_report",
    "state_dir",
]

# The pinned cache format version.  A breaking change to the stored shape raises it, and
# an older file is then ignored rather than half-read.
CACHE_SCHEMA = 1

# How long the copy is trusted, in seconds.  The per-result TTL still decides whether a
# result is stale;  this cap keeps a copy from being served at all once it is old.
MAX_AGE_SECONDS = 3600

# The largest copy that is read.  A report is small;  anything bigger is not one.
MAX_BYTES = 4 * 1024 * 1024


def state_dir() -> str:
    """The per-user state directory, ``~/.local/state/fleet-config``."""
    return os.path.join(os.path.expanduser("~"), ".local", "state", "fleet-config")


def cache_path() -> str:
    """The full path of the cached report."""
    return os.path.join(state_dir(), "report.json")


def _ensure_dir() -> None:
    """Create the state directory with mode 0700, and refuse a directory that is not ours.

    The mode is repaired when it is too wide, because a directory that other users can
    list is a directory another user can replace a file in.
    """
    target = state_dir()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    try:
        os.mkdir(target, 0o700)
    except FileExistsError:
        pass
    info = os.lstat(target)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise OSError("the fleet-config state directory is not ours")
    if info.st_mode & 0o077:
        os.chmod(target, 0o700)


def _dir_is_private() -> bool:
    """True when the state directory is a private directory that belongs to us."""
    try:
        info = os.lstat(state_dir())
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077


def _expected_to_dict(expected: ExpectedState) -> dict[str, Any]:
    """The stored form of an expectation.  Set fields only, and names and booleans only."""
    return contract.expected_to_dict(expected)


def _expected_from_dict(raw: Any) -> ExpectedState:
    """Read an expectation back, ignoring anything the shape does not carry."""
    data = raw if isinstance(raw, dict) else {}
    return ExpectedState(
        description=str(data.get("description") or ""),
        present=data.get("present"),
        sha256=data.get("sha256"),
        names=tuple(data.get("names") or ()),
        mode=data.get("mode"),
        timeout_seconds=data.get("timeoutSeconds"),
    )


def _observed_from_dict(raw: Any) -> ObservedState:
    """Read an observation back.  It can only hold stat fields and names, never contents."""
    data = raw if isinstance(raw, dict) else {}
    return ObservedState(
        present=data.get("present"),
        sha256=data.get("sha256"),
        names=tuple(data.get("names") or ()),
        mode=data.get("mode"),
        size_bytes=data.get("sizeBytes"),
        mtime=data.get("mtime"),
        timeout_seconds=data.get("timeoutSeconds"),
    )


def _result_to_dict(result: CheckResult) -> dict[str, Any]:
    """The stored form of one result.  It carries no secret value by construction."""
    return {
        "ruleId": result.rule_id,
        "seat": result.seat,
        "platform": result.platform,
        "kind": result.kind.value,
        "state": result.state.value,
        "reason": result.reason,
        "reasonCode": result.reason_code.value,
        "expected": _expected_to_dict(result.expected),
        "observed": contract.observed_to_dict(result.observed),
        "proofLevelRequired": result.proof_level_required.value,
        "proofLevelAchieved": (
            result.proof_level_achieved.value if result.proof_level_achieved else None
        ),
        "collector": result.collector.value,
        "checkType": result.check_type.value,
        "checkedAt": result.checked_at,
        "expiresAt": result.expires_at,
        "ttlSeconds": result.ttl_seconds,
        "stale": result.stale,
    }


def _result_from_dict(raw: Any) -> CheckResult:
    """Read one stored result back into its dataclass.

    A field that cannot be read raises ``ValueError``, and the caller then treats the
    whole copy as unreadable rather than serving half a report.
    """
    if not isinstance(raw, dict):
        raise ValueError("a stored result is not an object")
    kind = ConfigKind(raw["kind"])
    state = CheckState(raw["state"])
    reason_code = ReasonCode(raw["reasonCode"])
    required = ProofLevel(raw["proofLevelRequired"])
    achieved_raw = raw.get("proofLevelAchieved")
    achieved = ProofLevel(achieved_raw) if achieved_raw else None
    return CheckResult(
        rule_id=str(raw["ruleId"]),
        seat=str(raw["seat"]),
        platform=str(raw["platform"]),
        kind=kind,
        state=state,
        reason=str(raw.get("reason") or ""),
        reason_code=reason_code,
        expected=_expected_from_dict(raw.get("expected")),
        observed=_observed_from_dict(raw.get("observed")),
        proof_level_required=required,
        proof_level_achieved=achieved,
        collector=Collector(raw["collector"]),
        check_type=CheckType(raw["checkType"]),
        checked_at=str(raw["checkedAt"]),
        expires_at=str(raw["expiresAt"]),
        ttl_seconds=int(raw["ttlSeconds"]),
        stale=bool(raw.get("stale")),
    )


def _uncovered_to_dict(item: UncoveredSurface) -> dict[str, Any]:
    """The stored form of one uncovered surface."""
    return {"seat": item.seat, "surface": item.surface, "reason": item.reason}


def _not_implemented_to_dict(item: NotImplementedRule) -> dict[str, Any]:
    """The stored form of one rule the engine cannot run yet."""
    return {"ruleId": item.rule_id, "ownerSeat": item.owner_seat, "reason": item.reason}


def _not_implemented_from_dict(raw: Any) -> NotImplementedRule:
    """Read one stored not-implemented rule back."""
    if not isinstance(raw, dict):
        raise ValueError("the stored not-implemented rule is not an object")
    # Every field is checked rather than coerced:  a cache entry with a missing or
    # wrong-typed field is a damaged cache,  and it is rejected like any other.
    for key in ("ruleId", "ownerSeat", "reason"):
        if not isinstance(raw.get(key), str):
            raise ValueError("the stored not-implemented rule has no text %s" % key)
    return NotImplementedRule(
        rule_id=raw["ruleId"],
        owner_seat=raw["ownerSeat"],
        reason=raw["reason"],
    )


def _uncovered_from_dict(raw: Any) -> UncoveredSurface:
    """Read one stored uncovered surface back."""
    if not isinstance(raw, dict):
        raise ValueError("the stored uncovered surface is not an object")
    return UncoveredSurface(
        surface=str(raw.get("surface") or ""),
        reason=str(raw.get("reason") or ""),
        seat=str(raw.get("seat") or ""),
    )


def _report_to_dict(report: StatusReport) -> dict[str, Any]:
    """The stored form of a whole report."""
    return {
        "schemaVersion": report.schema_version,
        "generatedAt": report.generated_at,
        "manifestSource": report.manifest_source,
        "notes": list(report.notes),
        "results": [_result_to_dict(result) for result in report.results],
        "uncovered": [_uncovered_to_dict(item) for item in report.uncovered],
        "notImplemented": [
            _not_implemented_to_dict(item) for item in report.not_implemented
        ],
    }


def _report_from_dict(raw: Any) -> StatusReport:
    """Read a stored report back and rebuild its summary with the shared math."""
    if not isinstance(raw, dict):
        raise ValueError("the stored report is not an object")
    results = tuple(_result_from_dict(item) for item in raw.get("results") or ())
    return StatusReport(
        schema_version=int(raw.get("schemaVersion") or contract.SCHEMA_VERSION),
        generated_at=str(raw.get("generatedAt") or ""),
        results=results,
        summary=contract.summarize(results),
        manifest_source=raw.get("manifestSource"),
        notes=tuple(str(note) for note in raw.get("notes") or ()),
        uncovered=tuple(_uncovered_from_dict(item) for item in raw.get("uncovered") or ()),
        not_implemented=tuple(
            _not_implemented_from_dict(item) for item in raw.get("notImplemented") or ()
        ),
    )


def _fsync_dir(path: str) -> None:
    """Flush the directory's own entries to disk.

    Renaming a file into a directory does not persist the new directory entry until the
    directory itself is flushed,  so a crash right after the rename could lose the
    copy.  The descriptor is closed in a ``finally`` block,  so it is never leaked.

    A filesystem that cannot fsync a directory at all is not a reason to lose the write,
    so only the "this operation is not supported here" errors are best effort.  Every
    other failure is a real I/O error:  the rename is then not durable, and a caller told
    nothing went wrong would be trusting a last-known-good report that a crash can lose.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        try:
            os.fsync(fd)
        except OSError as exc:
            if exc.errno not in (errno.EINVAL, errno.ENOTSUP, errno.EBADF, errno.EOPNOTSUPP):
                raise
    finally:
        os.close(fd)


def save_report(report: StatusReport, now: float | None = None) -> None:
    """Atomically replace the cached copy with `report`.

    The copy is written to a temp file in the same directory and renamed over the old
    one, so a reader never sees a half-written report.  The file is 0600 and the
    directory 0700, and the bytes hold no secret value because a result cannot hold one.
    The directory is flushed after the rename,  so the new directory entry survives a
    crash.
    """
    epoch = time.time() if now is None else now
    document = {
        "schema": CACHE_SCHEMA,
        "saved_epoch": epoch,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch)),
        "report": _report_to_dict(report),
    }
    blob = json.dumps(document, indent=1, sort_keys=True).encode("utf-8") + b"\n"
    if len(blob) > MAX_BYTES:
        raise ValueError("the report is too large to cache")
    _ensure_dir()
    fd, tmp = tempfile.mkstemp(prefix=".report.", suffix=".tmp", dir=state_dir())
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, cache_path())
        # The rename is only durable once the directory entry is on disk too,  or a
        # crash can lose the last-known-good report the fsync above protected.
        _fsync_dir(state_dir())
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


def load_report(now: float | None = None) -> tuple[StatusReport | None, str]:
    """(report, note).  The report is None when there is no usable copy.

    The note says why in one token (``absent``, ``unsafe``, ``unreadable``,
    ``wrong-schema`` or ``expired``) or is ``ok``.
    """
    epoch = time.time() if now is None else now
    target = cache_path()
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
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(MAX_BYTES + 1)
    except OSError:
        return None, "unreadable"
    finally:
        os.close(fd)
    try:
        document = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, "unreadable"
    if (
        not isinstance(document, dict)
        or document.get("schema") != CACHE_SCHEMA
        or not isinstance(document.get("saved_epoch"), (int, float))
    ):
        return None, "wrong-schema"
    age = epoch - float(document["saved_epoch"])
    if age < -300 or age > MAX_AGE_SECONDS:
        return None, "expired"
    try:
        report = _report_from_dict(document.get("report"))
    except (KeyError, TypeError, ValueError):
        return None, "unreadable"
    return report, "ok"


def report_is_stale(report: StatusReport, now: datetime | None = None) -> bool:
    """True when any result in `report` is past its TTL at `now`.

    A report with no results is not stale;  it is empty, and an empty run is
    ``UNKNOWN``, never a pass.
    """
    from . import aggregator

    moment = now if now is not None else datetime.now(timezone.utc)
    return any(aggregator.result_is_expired(result, moment) for result in report.results)


def cached_age_seconds(now: float | None = None) -> int | None:
    """How old the cached copy is in seconds, or None when there is no copy."""
    epoch = time.time() if now is None else now
    try:
        with open(cache_path(), "rb") as handle:
            document = json.loads(handle.read(MAX_BYTES + 1).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    if not isinstance(document, dict) or not isinstance(document.get("saved_epoch"), (int, float)):
        return None
    return max(0, int(epoch - float(document["saved_epoch"])))
