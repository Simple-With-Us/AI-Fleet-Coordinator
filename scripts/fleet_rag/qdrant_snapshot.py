"""Pre-prune Qdrant snapshots for the fleet collection (AFC-754).

Before a non-dry-run ingest deletes anything, take one consistent Qdrant snapshot,
retain the last N copies (default 3, ``FLEET_RAG_PRUNE_SNAPSHOT_KEEP``), and return
metadata for ``last-run.json`` / the nightly ``last.json`` summary.

On the Hetzner server, archived files live under the fleet-rag-nightly state directory
(next to ``last.json`` by default).  When ``FLEET_RAG_QDRANT_SNAPSHOT_VOLUME`` points at
Qdrant's storage root, the snapshot file is moved off the live volume like
``fleet-qdrant-snapshot.sh``.  Without a volume path, snapshots stay in Qdrant and
retention uses the snapshots API.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import os
import shutil
from pathlib import Path

from fleet_rag.core import FleetRagError, Qdrant

KEEP_ENV = "FLEET_RAG_PRUNE_SNAPSHOT_KEEP"
DIR_ENV = "FLEET_RAG_PRUNE_SNAPSHOT_DIR"
VOLUME_ENV = "FLEET_RAG_QDRANT_SNAPSHOT_VOLUME"
NIGHTLY_SUMMARY_ENV = "FLEET_RAG_NIGHTLY_SUMMARY"
DEFAULT_STATE_SUBDIR = "qdrant-prune-snapshots"


class PruneSnapshotError(FleetRagError):
    """A pre-prune snapshot could not be taken.  Prune must not run."""


def snapshot_keep() -> int:
    raw = (os.environ.get(KEEP_ENV) or "3").strip()
    try:
        count = int(raw)
    except ValueError:
        count = 3
    return max(1, min(count, 50))


def snapshot_dir(default_parent: Path | None = None) -> Path:
    explicit = (os.environ.get(DIR_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    summary = (os.environ.get(NIGHTLY_SUMMARY_ENV) or "").strip()
    if summary:
        return Path(summary).expanduser().parent / DEFAULT_STATE_SUBDIR
    if default_parent is not None:
        return default_parent / DEFAULT_STATE_SUBDIR
    home = Path(os.environ.get("HOME") or Path.home())
    return home / ".local" / "state" / "fleet-rag-nightly" / DEFAULT_STATE_SUBDIR


def _prune_local_files(directory: Path, keep: int) -> None:
    files = sorted(
        directory.glob("*.snapshot"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in files[keep:]:
        old.unlink(missing_ok=True)
        sidecar = Path(f"{old}.sha256")
        sidecar.unlink(missing_ok=True)


def _prune_remote_snapshots(qd: Qdrant, keep: int) -> None:
    rows = qd.list_snapshots()

    def _sort_key(row: dict) -> str:
        return str(row.get("creation_time") or row.get("name") or "")

    rows = sorted(rows, key=_sort_key, reverse=True)
    names = [str(row.get("name") or "") for row in rows if row.get("name")]
    for old in names[keep:]:
        with contextlib.suppress(FleetRagError):
            qd.delete_snapshot(old)


def take_prune_snapshot(qd: Qdrant, *, state_parent: Path | None = None) -> dict:
    """Create one snapshot and enforce retention.  Raises PruneSnapshotError on failure."""
    keep = snapshot_keep()
    try:
        result = qd.create_snapshot(wait=True)
    except FleetRagError as exc:
        raise PruneSnapshotError(str(exc)) from None
    name = str(result.get("name") or "").strip()
    if not name:
        raise PruneSnapshotError("Qdrant snapshot API returned no name")

    collection = qd.collection
    volume_root = (os.environ.get(VOLUME_ENV) or "").strip()
    archive_dir = snapshot_dir(state_parent)
    if archive_dir.exists() and not archive_dir.is_dir():
        raise PruneSnapshotError(f"snapshot dir path is not a directory: {archive_dir}")
    archive_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(archive_dir, 0o700)

    archived_path: str | None = None
    if volume_root:
        src = Path(volume_root).expanduser() / "snapshots" / collection / name
        if not src.is_file() or src.stat().st_size <= 0:
            with contextlib.suppress(FleetRagError):
                qd.delete_snapshot(name)
            raise PruneSnapshotError(f"snapshot file missing on volume: {src}")
        stamp = _dt.datetime.now(_dt.UTC).strftime("%Y%m%dT%H%M%SZ")
        dest = archive_dir / f"{collection}-{stamp}.snapshot"
        shutil.move(str(src), dest)
        os.chmod(dest, 0o600)
        with contextlib.suppress(FleetRagError):
            qd.delete_snapshot(name)
        _prune_local_files(archive_dir, keep)
        archived_path = str(dest)
    else:
        _prune_remote_snapshots(qd, keep)

    return {
        "name": name,
        "collection": collection,
        "path": archived_path,
        "dir": str(archive_dir),
        "keep": keep,
    }


def maybe_snapshot_before_prune(
    *,
    prune: bool,
    dry_run: bool,
    pending_prunes: list,
    report: dict,
    qd: Qdrant | None,
    state_path: Path,
    log,
) -> bool:
    """Take a pre-prune snapshot when appropriate.  Return False to block all prunes."""
    if not prune or dry_run or not pending_prunes or report.get("errors"):
        return True
    if qd is None:
        msg = "prune aborted: pre-prune Qdrant snapshot failed: no Qdrant client"
        report["errors"].append(msg)
        report["ok"] = False
        report.setdefault("guard_trips", []).append(
            {"source": "qdrant", "reason": "snapshot-failed"}
        )
        log(f"[prune] ERROR {msg}")
        return False
    try:
        meta = take_prune_snapshot(qd, state_parent=state_path.parent)
    except PruneSnapshotError as exc:
        msg = f"prune aborted: pre-prune Qdrant snapshot failed: {exc}"
        report["errors"].append(msg)
        report["ok"] = False
        report.setdefault("guard_trips", []).append(
            {"source": "qdrant", "reason": "snapshot-failed"}
        )
        log(f"[prune] ERROR {msg}")
        return False
    report["qdrant_prune_snapshot"] = meta
    log(f"[prune] snapshot {meta['name']} (keep={meta['keep']})")
    return True
