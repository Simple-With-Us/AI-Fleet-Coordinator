"""Ingest sentinel: the dead-man switch for the nightly ingest routine.

After the full nightly ingest run the Mac writes one point with doc_id ``meta/ingest-status`` into
the fleet-agents collection.  The Hetzner box's fleet-health-verify.sh (every 15 minutes) reads it
and pages through the existing Pushover path when it is older than 30 hours or ``ok`` is false.
No credential has to leave the box for that check, and no new monitor is needed.

Only the nightly pass attests it:  a partial or unpruned run ingests and logs but leaves the
sentinel and last-run.json untouched, so it cannot make a stale or failed night look healthy.

The sentinel carries source="meta" so recall_search excludes it from results.
"""
from __future__ import annotations

import json
from typing import Any

from .core import Qdrant, build_point, embed, now_ms

SENTINEL_DOC_ID = "meta/ingest-status"
SENTINEL_TEXT = "fleet-rag ingest status sentinel (not knowledge; excluded from search)"


def write_ingest_sentinel(cfg: dict[str, str], qdrant: Qdrant, report: dict[str, Any]) -> str:
    """Upsert the sentinel with the run's outcome.  Returns the point id."""
    ok = bool(report.get("ok", False))
    point = build_point(SENTINEL_TEXT, {
        "source": "meta", "app": "fleet", "category": "meta", "seat": "FLEET",
        "doc_id": SENTINEL_DOC_ID, "chunk_index": 0, "chunk_count": 1, "heading": "",
        "title": "ingest status", "url": "", "path": "",
        "created_at": int(report.get("started_at") or now_ms()),
        "updated_at": int(report.get("finished_at") or now_ms()),
        "ingest_run": str(report.get("run_id", "")),
        "ok": ok,
        "summary": json.dumps({k: report.get(k) for k in ("per_source", "errors") if k in report})[:4000],
    })
    point["vector"] = embed(cfg, [SENTINEL_TEXT])[0]
    qdrant.upsert([point], wait=True)
    return point["id"]


def read_ingest_sentinel(qdrant: Qdrant) -> dict[str, Any] | None:
    for p in qdrant.scroll({"must": [{"key": "doc_id", "match": {"value": SENTINEL_DOC_ID}}]}, limit=1):
        return p.get("payload") or {}
    return None


def sentinel_age_hours(qdrant: Qdrant) -> float | None:
    pl = read_ingest_sentinel(qdrant)
    if not pl or not pl.get("updated_at"):
        return None
    return (now_ms() - int(pl["updated_at"])) / 3_600_000
