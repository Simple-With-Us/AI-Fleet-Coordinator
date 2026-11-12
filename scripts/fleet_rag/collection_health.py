"""Weekly read-only health report for the configured Qdrant collection (AFC-762).

Scrolls the collection once and writes JSON and Markdown under
``~/.local/state/fleet-rag-collection-health/``.  The report counts points and
documents per source, the oldest and newest ``updated_at`` per source, chunks
whose ``doc_id`` is absent from the latest ingest state file when that file can
be read, and the change since the previous report.

No embedding call and no model call.  The process never upserts, deletes, or
sets payload.  It stays off until ``FLEET_RAG_COLLECTION_HEALTH`` is exactly
``1``, the same gate the nightly ingest uses.

    python3 -I scripts/fleet_rag/collection_health.py
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import fcntl
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any, Iterator

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fleet_rag.core import FleetRagError, Qdrant, infisical_fetch
from simple_tools.secretscan import scan as scan_secrets

# Same names nightly_job.MACHINE_IDENTITY_ENV refuses.  A test locks the two tuples together.
MACHINE_IDENTITY_ENV = (
    "INFISICAL_UNIVERSAL_AUTH_CLIENT_ID",
    "INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET",
    "INFISICAL_CLIENT_ID",
    "INFISICAL_CLIENT_SECRET",
    "INFISICAL_AUTOMATION_CLIENT_ID",
    "INFISICAL_AUTOMATION_CLIENT_SECRET",
    "INFISICAL_SHARED_CLIENT_ID",
    "INFISICAL_SHARED_CLIENT_SECRET",
)

ENABLE_ENV = "FLEET_RAG_COLLECTION_HEALTH"
STATE_ENV = "FLEET_RAG_INGEST_STATE"
DIR_ENV = "FLEET_RAG_COLLECTION_HEALTH_DIR"
REPORT_DIR_NAME = "fleet-rag-collection-health"
STATE_NAME = "ingest-state.json"
LATEST_JSON = "latest.json"
LATEST_MD = "latest.md"
ERROR_JSON = "last-error.json"
LOCK_NAME = "report.lock"

PAYLOAD_FIELDS = ("source", "doc_id", "updated_at")
SCROLL_PAGE = 256
ORPHAN_LIST_CAP = 100
MAX_LABEL = 256
MAX_UPDATED_AT_MS = 10**16
MISSING = "(missing)"
SCHEMA = 1

READ_NAMES = (
    "QDRANT_URL",
    "QDRANT_FLEET_COLLECTION",
    "QDRANT_READONLY_API_KEY",
    "QDRANT_API_KEY",
)

GAP = "\u00a0 "


class CollectionHealthError(FleetRagError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def enabled(env: dict[str, str]) -> bool:
    return (env.get(ENABLE_ENV) or "").strip() == "1"


def _home(env: dict[str, str]) -> Path:
    raw = (env.get("HOME") or "").strip()
    return Path(raw).expanduser() if raw else Path.home()


def report_dir(env: dict[str, str]) -> Path:
    raw = (env.get(DIR_ENV) or "").strip()
    if raw:
        return Path(raw).expanduser()
    return _home(env) / ".local" / "state" / REPORT_DIR_NAME


def manifest_path(env: dict[str, str]) -> Path:
    raw = (env.get(STATE_ENV) or "").strip()
    if raw:
        return Path(raw).expanduser()
    return _home(env) / "apps" / "fleet-rag" / "state" / STATE_NAME


def _machine_identity(env: dict[str, str]) -> str | None:
    for name in MACHINE_IDENTITY_ENV:
        if (env.get(name) or "").strip():
            return name
    return None


def _kept_config(values: dict[str, Any]) -> dict[str, str]:
    kept: dict[str, str] = {}
    for name in READ_NAMES:
        value = values.get(name)
        if isinstance(value, str) and value.strip():
            kept[name] = value.strip()
    return kept


def _missing_names(cfg: dict[str, str]) -> list[str]:
    missing = [name for name in ("QDRANT_URL", "QDRANT_FLEET_COLLECTION") if name not in cfg]
    if "QDRANT_READONLY_API_KEY" not in cfg and "QDRANT_API_KEY" not in cfg:
        missing.append("QDRANT_READ_KEY")
    return missing


def read_config(env: dict[str, str], fetch=None) -> dict[str, str]:
    """Qdrant URL, collection, and a read key.  No embedder key is requested."""
    blocked = _machine_identity(env)
    if blocked:
        raise CollectionHealthError("machine-identity:" + blocked)
    found = _kept_config(env)
    if not _missing_names(found):
        return found
    loader = fetch if fetch is not None else _fetch_qdrant_keys
    loaded = loader(READ_NAMES)
    if not isinstance(loaded, dict):
        raise CollectionHealthError("credentials-unreadable")
    found = _kept_config({**loaded, **found})
    missing = _missing_names(found)
    if missing:
        raise CollectionHealthError("missing:" + ",".join(missing))
    return found


def _fetch_qdrant_keys(names: tuple[str, ...]) -> dict[str, str]:
    try:
        found = infisical_fetch(names)
    except FleetRagError as exc:
        raise CollectionHealthError(public_failure(exc)) from None
    if not isinstance(found, dict):
        raise CollectionHealthError("credentials-unreadable")
    return found


def open_client(cfg: dict[str, str]) -> Qdrant:
    return Qdrant(cfg)


def public_failure(exc: BaseException) -> str:
    """A reason safe to write.  HTTP bodies and secret-shaped text stay out."""
    if isinstance(exc, CollectionHealthError):
        return exc.reason
    if not isinstance(exc, FleetRagError):
        return type(exc).__name__
    text = str(exc)
    if text.startswith("HTTP "):
        head, sep, _detail = text.partition(": ")
        text = head if sep else text
    if scan_secrets(text):
        return "FleetRagError"
    return text[:300]


def _label(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > MAX_LABEL or any(ch in text for ch in "\n\r\x00"):
        return None
    return text


def _ms(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    number: int | None = None
    if isinstance(value, int):
        number = value
    elif isinstance(value, float) and value.is_integer():
        number = int(value)
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    if number is None or number < 0 or number > MAX_UPDATED_AT_MS:
        return None
    return number


def _payload(point: Any) -> dict:
    if not isinstance(point, dict):
        raise CollectionHealthError("malformed-point")
    payload = point.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise CollectionHealthError("malformed-payload")
    return payload


def manifest_doc_ids(state: dict) -> set[str]:
    """Document ids stored by ingest.  Cursor rows such as ``_linear_sync`` are not documents."""
    found: set[str] = set()
    for key, record in state.items():
        if not isinstance(key, str) or not isinstance(record, dict):
            continue
        if "doc_hash" in record or "chunk_ids" in record:
            found.add(key)
    return found


def load_manifest(path: Path) -> tuple[set[str] | None, dict[str, Any]]:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None, {"available": False, "reason": "missing"}
    except OSError:
        return None, {"available": False, "reason": "unreadable"}
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None, {"available": False, "reason": "unreadable"}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, {"available": False, "reason": "unreadable"}
    if not isinstance(parsed, dict):
        return None, {"available": False, "reason": "unreadable"}
    ids = manifest_doc_ids(parsed)
    return ids, {"available": True, "documents": len(ids)}


def _touch_updated(bucket: dict[str, Any], updated: int | None) -> None:
    if updated is None:
        return
    oldest = bucket["oldest_updated_at"]
    newest = bucket["newest_updated_at"]
    if oldest is None or updated < oldest:
        bucket["oldest_updated_at"] = updated
    if newest is None or updated > newest:
        bucket["newest_updated_at"] = updated


def _source_bucket() -> dict[str, Any]:
    return {"points": 0, "docs": 0, "oldest_updated_at": None, "newest_updated_at": None}


def _listed_ids(ids: set[str]) -> dict[str, Any]:
    ordered = sorted(ids)
    listed = ordered[:ORPHAN_LIST_CAP]
    return {
        "doc_ids": listed,
        "doc_ids_listed": len(listed),
        "doc_ids_truncated": len(ordered) > ORPHAN_LIST_CAP,
    }


def aggregate(points: Iterator[Any], manifest_ids: set[str] | None) -> dict[str, Any]:
    sources: dict[str, dict[str, Any]] = {}
    docs_by_source: dict[str, set[str]] = {}
    all_docs: set[str] = set()
    orphan_docs: dict[str, set[str]] = {}
    orphan_points: dict[str, int] = {}
    missing_by_source: dict[str, int] = {}
    total_points = 0
    missing_points = 0

    for point in points:
        payload = _payload(point)
        source = _label(payload.get("source")) or MISSING
        doc_id = _label(payload.get("doc_id"))
        updated = _ms(payload.get("updated_at"))
        bucket = sources.setdefault(source, _source_bucket())
        bucket["points"] += 1
        total_points += 1
        _touch_updated(bucket, updated)
        if not doc_id:
            missing_points += 1
            missing_by_source[source] = missing_by_source.get(source, 0) + 1
            if manifest_ids is not None:
                orphan_points[source] = orphan_points.get(source, 0) + 1
            continue
        docs_by_source.setdefault(source, set()).add(doc_id)
        all_docs.add(doc_id)
        if manifest_ids is not None and doc_id not in manifest_ids:
            orphan_docs.setdefault(source, set()).add(doc_id)
            orphan_points[source] = orphan_points.get(source, 0) + 1

    for source, ids in docs_by_source.items():
        sources[source]["docs"] = len(ids)

    if manifest_ids is None:
        orphans: dict[str, Any] = {"available": False}
    else:
        by_source: dict[str, Any] = {}
        for source in sorted(set(orphan_points) | set(orphan_docs) | set(missing_by_source)):
            ids = orphan_docs.get(source, set())
            row = {
                "points": orphan_points.get(source, 0),
                "docs": len(ids),
                "missing_doc_id_points": missing_by_source.get(source, 0),
            }
            row.update(_listed_ids(ids))
            by_source[source] = row
        orphans = {
            "available": True,
            "points": sum(orphan_points.values()),
            "docs": len({doc_id for ids in orphan_docs.values() for doc_id in ids}),
            "missing_doc_id_points": missing_points,
            "by_source": by_source,
        }

    return {
        "points": total_points,
        "docs": len(all_docs),
        "missing_doc_id_points": missing_points,
        "by_source": {name: sources[name] for name in sorted(sources)},
        "orphans": orphans,
    }


def _int_pair(bucket: Any) -> tuple[int, int] | None:
    if not isinstance(bucket, dict):
        return None
    points = bucket.get("points")
    docs = bucket.get("docs")
    if type(points) is not int or type(docs) is not int:
        return None
    return points, docs


def _previous_usable(previous: dict | None) -> dict | None:
    if not isinstance(previous, dict) or previous.get("ok") is not True:
        return None
    if "schema" in previous and previous.get("schema") != SCHEMA:
        return None
    if type(previous.get("points")) is not int or type(previous.get("docs")) is not int:
        return None
    by_source = previous.get("by_source")
    if not isinstance(by_source, dict):
        return None
    for bucket in by_source.values():
        if _int_pair(bucket) is None:
            return None
    return previous


def growth(current: dict[str, Any], previous: dict | None) -> dict[str, Any]:
    usable = _previous_usable(previous)
    if usable is None:
        return {"available": False}
    by_source: dict[str, dict[str, int]] = {}
    names = set(current["by_source"]) | set(usable["by_source"])
    for name in sorted(names):
        cur = _int_pair(current["by_source"].get(name)) or (0, 0)
        prev = _int_pair(usable["by_source"].get(name)) or (0, 0)
        by_source[name] = {"points": cur[0] - prev[0], "docs": cur[1] - prev[1]}
    report: dict[str, Any] = {
        "available": True,
        "previous_generated_at": usable.get("generated_at")
        if isinstance(usable.get("generated_at"), str)
        else None,
        "points": current["points"] - usable["points"],
        "docs": current["docs"] - usable["docs"],
        "by_source": by_source,
        "orphans": None,
    }
    cur_orphans = current.get("orphans") if isinstance(current.get("orphans"), dict) else {}
    prev_orphans = usable.get("orphans") if isinstance(usable.get("orphans"), dict) else {}
    if cur_orphans.get("available") is True and prev_orphans.get("available") is True:
        if type(cur_orphans.get("points")) is int and type(prev_orphans.get("points")) is int:
            if type(cur_orphans.get("docs")) is int and type(prev_orphans.get("docs")) is int:
                report["orphans"] = {
                    "points": cur_orphans["points"] - prev_orphans["points"],
                    "docs": cur_orphans["docs"] - prev_orphans["docs"],
                }
    return report


def _iso_ms(value: int | None) -> str:
    if value is None:
        return ""
    moment = _dt.datetime.fromtimestamp(value / 1000, _dt.timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Fleet Agents Collection Health",
        "",
        GAP.join(
            [
                f"Generated {report['generated_at']}.",
                f"Collection `{report['collection']}`.",
                "Read-only.",
                "This report does not call a model.",
            ]
        ),
        "",
        "## Totals",
        "",
        f"- Points: {report['points']}",
        f"- Documents: {report['docs']}",
        f"- Points with no doc_id: {report['missing_doc_id_points']}",
        "",
        "## By Source",
        "",
        "| Source | Points | Documents | Oldest updated_at | Newest updated_at |",
        "| --- | ---: | ---: | --- | --- |",
    ]
    for name, bucket in report["by_source"].items():
        lines.append(
            "| {source} | {points} | {docs} | {oldest} | {newest} |".format(
                source=_cell(name),
                points=bucket["points"],
                docs=bucket["docs"],
                oldest=_iso_ms(bucket["oldest_updated_at"]),
                newest=_iso_ms(bucket["newest_updated_at"]),
            )
        )
    if not report["by_source"]:
        lines.append("| (none) | 0 | 0 |  |  |")

    lines.extend(["", "## Orphaned Chunks", ""])
    orphans = report["orphans"]
    if not orphans.get("available"):
        reason = orphans.get("reason") or "unavailable"
        lines.append(
            GAP.join(
                [
                    f"The ingest manifest was not available ({reason}).",
                    "Orphaned chunks were not computed.",
                ]
            )
        )
    else:
        lines.append(
            GAP.join(
                [
                    f"The ingest manifest lists {report['manifest']['documents']} documents.",
                    (
                        f"{orphans['points']} points across {orphans['docs']} documents "
                        "are not in it."
                    ),
                    (
                        "A point with no doc_id counts as an orphan point and not as a document."
                    ),
                    (
                        "The ingest sentinel meta/ingest-status is not an ingest state row, "
                        "so it counts here when it is present."
                    ),
                ]
            )
        )
        if orphans["by_source"]:
            lines.extend(
                [
                    "",
                    "| Source | Points | Documents | Listed doc_ids |",
                    "| --- | ---: | ---: | --- |",
                ]
            )
            for name, row in orphans["by_source"].items():
                shown = ", ".join(row["doc_ids"])
                if row["doc_ids_truncated"]:
                    shown = f"{shown} ({row['doc_ids_listed']} listed)"
                lines.append(
                    f"| {_cell(name)} | {row['points']} | {row['docs']} | {_cell(shown)} |"
                )

    lines.extend(["", "## Growth", ""])
    delta = report["growth"]
    if not delta.get("available"):
        lines.append(
            GAP.join(
                [
                    "No previous report was available.",
                    "Growth was not computed.",
                ]
            )
        )
    else:
        when = delta.get("previous_generated_at") or "an undated report"
        lines.append(
            GAP.join(
                [
                    f"Compared with the report from {when}.",
                    f"Points changed by {delta['points']:+d} and documents changed by {delta['docs']:+d}.",
                ]
            )
        )
        changed = [
            (name, row)
            for name, row in delta["by_source"].items()
            if row["points"] or row["docs"]
        ]
        if changed:
            lines.extend(
                [
                    "",
                    "| Source | Points | Documents |",
                    "| --- | ---: | ---: |",
                ]
            )
            for name, row in changed:
                lines.append(f"| {_cell(name)} | {row['points']:+d} | {row['docs']:+d} |")
        else:
            lines.append("")
            lines.append("Counts match the previous report.")
        orphan_delta = delta.get("orphans")
        if isinstance(orphan_delta, dict):
            lines.append("")
            lines.append(
                "Orphan points changed by {points:+d} and orphan documents changed by {docs:+d}.".format(
                    points=orphan_delta["points"], docs=orphan_delta["docs"]
                )
            )
    lines.append("")
    return "\n".join(lines)


def load_previous(path: Path) -> dict | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _chmod_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _write_private(path: Path, text: str) -> None:
    _chmod_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, text.encode("utf-8"))
    finally:
        os.close(fd)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    _write_private(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


@contextlib.contextmanager
def report_lock(path: Path):
    _chmod_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.fchmod(fd, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _now_iso(now: _dt.datetime | None) -> str:
    moment = now or _dt.datetime.now(_dt.timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=_dt.timezone.utc)
    return moment.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_report(
    client: Any,
    *,
    manifest_ids: set[str] | None,
    manifest_meta: dict[str, Any],
    previous: dict | None,
    generated_at: str,
) -> dict[str, Any]:
    collection = getattr(client, "collection", "")
    if not isinstance(collection, str) or not collection:
        raise CollectionHealthError("collection-unset")
    counted = aggregate(
        client.scroll(
            None,
            limit=SCROLL_PAGE,
            with_payload=list(PAYLOAD_FIELDS),
            with_vector=False,
        ),
        manifest_ids,
    )
    if manifest_ids is None and isinstance(counted["orphans"], dict):
        reason = manifest_meta.get("reason")
        if isinstance(reason, str):
            counted["orphans"]["reason"] = reason
    report = {
        "schema": SCHEMA,
        "ok": True,
        "enabled": True,
        "ran": True,
        "read_only": True,
        "generated_at": generated_at,
        "collection": collection,
        "manifest": manifest_meta,
        **counted,
    }
    report["growth"] = growth(report, previous)
    return report


def _error_payload(reason: str, generated_at: str) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "ok": False,
        "enabled": True,
        "ran": True,
        "read_only": True,
        "generated_at": generated_at,
        "error": reason,
    }


def _iter_points_and_write(
    client: Any,
    directory: Path,
    manifest_ids: set[str] | None,
    manifest_meta: dict[str, Any],
    generated_at: str,
) -> dict[str, Any]:
    previous = load_previous(directory / LATEST_JSON)
    report = build_report(
        client,
        manifest_ids=manifest_ids,
        manifest_meta=manifest_meta,
        previous=previous,
        generated_at=generated_at,
    )
    json_path = directory / LATEST_JSON
    md_path = directory / LATEST_MD
    _write_json(json_path.with_name(LATEST_JSON + ".next"), report)
    _write_private(md_path.with_name(LATEST_MD + ".next"), render_markdown(report))
    os.replace(json_path.with_name(LATEST_JSON + ".next"), json_path)
    os.replace(md_path.with_name(LATEST_MD + ".next"), md_path)
    error_path = directory / ERROR_JSON
    if error_path.exists() and not error_path.is_symlink():
        error_path.unlink()
    return report


def execute(
    env: dict[str, str],
    *,
    client: Any = None,
    fetch=None,
    now: _dt.datetime | None = None,
) -> dict[str, Any]:
    if not enabled(env):
        return {"enabled": False, "ok": True, "ran": False}
    directory = report_dir(env)
    generated_at = _now_iso(now)
    with report_lock(directory / LOCK_NAME) as held:
        if not held:
            return {"enabled": True, "ok": True, "ran": False, "lock": "busy"}
        try:
            active = client if client is not None else open_client(read_config(env, fetch))
            manifest_ids, manifest_meta = load_manifest(manifest_path(env))
            return _iter_points_and_write(
                active, directory, manifest_ids, manifest_meta, generated_at
            )
        except FleetRagError as exc:
            reason = public_failure(exc)
        _write_json(directory / ERROR_JSON, _error_payload(reason, generated_at))
        return {
            "enabled": True,
            "ok": False,
            "ran": True,
            "read_only": True,
            "error": reason,
            "generated_at": generated_at,
        }


def main(argv: list[str] | None = None) -> int:
    del argv
    env = {key: value for key, value in os.environ.items()}
    try:
        report = execute(env)
    except OSError as exc:
        sys.stderr.write(f"collection-health: {type(exc).__name__}\n")
        return 1
    if not report.get("enabled") or report.get("lock") == "busy":
        return 0
    if report.get("ok"):
        return 0
    sys.stderr.write("collection-health: " + str(report.get("error") or "failed") + "\n")
    return 1


if __name__ == "__main__":
    sys.exit(main())
