"""Write-time upsert of fleet Coding-folder notes into the Fleet RAG corpus (AFC-685).

After a successful create or update through the shared notes writer, the Mac runs this module
(``python3 -m fleet_rag.note_upsert``) with one JSON object on stdin.  It reuses the ingest
chunk/scrub/embed/upsert path; credentials come only from ``FLEET_RECALL_INFISICAL_TOKEN`` or an
explicit environment (see ``fleet_rag.core.load_config``).  Failures are logged and never affect
the note write.
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from . import core, ingest, sources
from .core import FleetRagError, content_hash, eprint, now_ms
from .ingest import IngestLock, IngestLocked, STATE_PATH, load_state, process_group
from .notes_export import html_to_text, strip_title_line

MIN_BODY = 20  # matches iter_apple_notes


def note_doc_id(raw_note_id: str) -> str:
    """Canonical doc_id for an Apple Note id, matching nightly ``iter_apple_notes``."""
    nid = (raw_note_id or "").strip().replace("x-coredata://", "")
    if not nid:
        raise FleetRagError("missing note_id")
    return "note/" + nid


def body_text(request: Mapping[str, Any]) -> str:
    """Plain text for RAG from the request body the writer sent to Apple Notes."""
    title = str(request.get("title") or "").strip()
    raw = str(request.get("markdown") or "")
    is_html = bool((request.get("flags") or {}).get("html"))
    if is_html:
        text = strip_title_line(html_to_text(raw), title)
    else:
        text = raw.strip()
        if title and text.startswith(title):
            text = text[len(title) :].lstrip()
    return text


def build_doc(request: Mapping[str, Any], note_id: str) -> sources.Doc | None:
    """A ``sources.Doc`` for the written note, or None when the body is too small to index."""
    text = body_text(request)
    if len(text.strip()) < MIN_BODY:
        return None
    title = str(request.get("title") or "").strip() or "Untitled note"
    app, seat = sources.parse_note_title(title)
    identity = str(request.get("identity") or request.get("requester") or "").strip()
    if identity:
        found = sources.seat_tag(identity)
        if found and found != "FLEET":
            seat = found
    stamp = now_ms()
    return sources.Doc(
        doc_id=note_doc_id(note_id),
        title=title,
        text_markdown=text,
        source="apple-note",
        app=app,
        category="note",
        seat=seat,
        url="",
        path="",
        created_at_ms=stamp,
        updated_at_ms=stamp,
    )


def upsert_written_note(
    request: Mapping[str, Any],
    note_id: str,
    *,
    lock_timeout_s: float = 0,
    log=eprint,
) -> dict[str, Any]:
    """Upsert one note into Qdrant and the ingest state file.  Never raises to callers."""
    action = request.get("action")
    if action not in ("create", "update"):
        return {"skipped": True, "reason": "not a body write"}
    doc = build_doc(request, note_id)
    if doc is None:
        return {"skipped": True, "reason": "body too short"}
    doc_hash = content_hash(doc.text_markdown)
    state_path = STATE_PATH
    try:
        cfg = core.load_config(need_write=True)
        qd = core.Qdrant(cfg)
    except FleetRagError as exc:
        log(f"[apple-note] rag upsert skipped: {exc}")
        return {"ok": False, "error": str(exc)}
    try:
        with IngestLock(state_path.parent / "ingest.lock", timeout_s=lock_timeout_s):
            state = load_state(state_path)
            prev = state.get(doc.doc_id)
            if isinstance(prev, dict) and prev.get("doc_hash") == doc_hash:
                return {"ok": True, "skipped": True, "reason": "unchanged"}
            run_id = (
                f"note-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
            )
            writer = ingest.Writer(cfg, qd, dry_run=False, log=log)
            stats = {
                "docs_seen": 0,
                "docs_changed": 0,
                "docs_pruned": 0,
                "chunks_new": 0,
                "chunks_deleted": 0,
                "chunks_repointed": 0,
                "chunks_dropped_by_gitleaks": 0,
                "chunks_scrubbed": 0,
            }
            process_group(
                [(doc, doc_hash)],
                state,
                writer,
                run_id,
                stats,
                state_path,
                live_owners={doc.doc_id},
            )
            return {"ok": True, "doc_id": doc.doc_id, "stats": stats}
    except IngestLocked:
        msg = "another ingest holds the lock; the note is in Apple Notes but not in the RAG yet"
        log(f"[apple-note] rag upsert deferred: {msg}")
        return {"ok": False, "deferred": True, "error": msg}
    except FleetRagError as exc:
        log(f"[apple-note] rag upsert failed: {exc}")
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — must never escape to the notes writer
        log(f"[apple-note] rag upsert failed: {type(exc).__name__}")
        return {"ok": False, "error": type(exc).__name__}


def _read_stdin() -> dict[str, Any]:
    raw = sys.stdin.buffer.read()
    if not raw:
        raise FleetRagError("empty stdin")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise FleetRagError("stdin JSON must be an object")
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--lock-timeout",
        type=float,
        default=0.0,
        help="seconds to wait for ingest.lock (default 0: skip when nightly ingest runs)",
    )
    args = ap.parse_args(argv)
    try:
        payload = _read_stdin()
    except (FleetRagError, ValueError, UnicodeDecodeError) as exc:
        eprint(f"[apple-note] rag upsert refused: {exc}")
        return 1
    request = payload.get("request")
    note_id = payload.get("note_id")
    if not isinstance(request, dict) or not isinstance(note_id, str) or not note_id.strip():
        eprint("[apple-note] rag upsert refused: need request object and note_id")
        return 1
    result = upsert_written_note(request, note_id, lock_timeout_s=args.lock_timeout)
    if result.get("skipped"):
        return 0
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
