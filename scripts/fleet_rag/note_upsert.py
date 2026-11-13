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
from .ingest import STATE_PATH, IngestLock, IngestLocked, load_state, process_group
from .notes_export import html_to_text, strip_title_line

MIN_BODY = 20  # matches iter_apple_notes
DEFAULT_LOCK_TIMEOUT_S = 120.0  # wait for nightly ingest.lock before deferring (AFC-685)


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
    lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
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
            run_id = f"note-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
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


def receiver_verified(doc, qd, chunk_ids):
    """Independently check the receiver's canonical ID and every expected chunk body."""
    rows, _count = ingest.doc_rows(doc, 'note-receiver-proof')
    expected = {content_hash(ingest._chunk_body(row['payload']['text'])) for row in rows}
    if not expected or not chunk_ids:
        return False
    actual = set()
    for point_id in set(chunk_ids):
        point = qd.retrieve(point_id, with_payload=True)
        payload = point.get('payload') if isinstance(point, dict) else None
        if not isinstance(point, dict) or point.get('id') != point_id or not isinstance(payload, dict) or payload.get('doc_id') != doc.doc_id or payload.get('source') != 'apple-note' or payload.get('title') != doc.title or payload.get('app') != doc.app or payload.get('seat') != doc.seat or not isinstance(payload.get('text'), str):
            return False
        actual.add(content_hash(ingest._chunk_body(payload['text'])))
    return actual == expected


def deliver_queued_note(record, root=None, lock_timeout_s=0, read_current=None):
    """Deliver provider-current Coding content; prove provider stability before ack."""
    from . import note_outbox
    from .notes_export import read_current_coding_note
    root = note_outbox.default_root() if root is None else root
    reader = read_current_coding_note if read_current is None else read_current
    try:
        if record.get('schema') != 2 or record.get('cancelled'):
            return False
        snapshot = reader(record['doc_id'])
        text = snapshot['text'].strip()
        text_kind = 'body' if text else 'title-only'
        text = text or snapshot['name'].strip()
        if not text:
            return False
        app, seat = sources.parse_note_title(snapshot['name'])
        doc = sources.Doc(note_doc_id(snapshot['id']), snapshot['name'], text,
                          'apple-note', app, 'note', seat,
                          url='notes://' + snapshot['id'],
                          updated_at_ms=sources.parse_ts_ms(snapshot.get('modified')) or now_ms(),
                          extra={'text_kind': text_kind})
        if doc.doc_id != record['doc_id']:
            return False
        doc = sources.Doc(**note_outbox.prepare_document(doc))
        doc.extra['text_kind'] = text_kind
        key = note_outbox._key(doc.doc_id)
        cfg = core.load_config(need_write=True)
        qd = core.Qdrant(cfg)
        with IngestLock(STATE_PATH.parent / 'ingest.lock', timeout_s=lock_timeout_s):
            if not note_outbox.current(root, key, record['version']):
                return False
            state = load_state(STATE_PATH)
            previous = state.get(doc.doc_id, {})
            digest = content_hash(doc.text_markdown)
            if previous.get('doc_hash') == digest and receiver_verified(doc, qd, previous.get('chunk_ids', [])):
                return reader(doc.doc_id)['revision'] == snapshot['revision']
            writer = ingest.Writer(cfg, qd, dry_run=False, log=lambda *_args: None)
            stats = ingest._empty_stats()
            process_group([(doc, digest)], state, writer, 'note-outbox-' + uuid.uuid4().hex,
                          stats, STATE_PATH, live_owners={doc.doc_id})
            return (receiver_verified(doc, qd, state.get(doc.doc_id, {}).get('chunk_ids', []))
                    and reader(doc.doc_id)['revision'] == snapshot['revision'])
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--drain-outbox', action='store_true')
    ap.add_argument('--outbox-key')
    ap.add_argument('--reconcile-coding', action='store_true')
    ap.add_argument(
        "--lock-timeout",
        type=float,
        default=DEFAULT_LOCK_TIMEOUT_S,
        help="seconds to wait for ingest.lock before deferring (default "
        f"{DEFAULT_LOCK_TIMEOUT_S:g}; 0 fails fast when the lock is held)",
    )
    args = ap.parse_args(argv)
    if args.drain_outbox:
        from . import note_outbox
        try:
            reconciliation = None
            if args.reconcile_coding:
                try:
                    reconciliation = note_outbox.reconcile_coding()
                except Exception:
                    reconciliation = {'failed': True, 'reason': 'coding-metadata-unavailable'}
            result = note_outbox.drain(lambda record: deliver_queued_note(record, lock_timeout_s=0),
                                       limit=1, only_key=args.outbox_key)
            if reconciliation is not None:
                result['coding_metadata'] = reconciliation
            print(json.dumps(result, sort_keys=True))
            return 0
        except Exception:
            print(json.dumps({'failed': True, 'reason': 'outbox-unavailable'}))
            return 1
    if args.outbox_key:
        ap.error('--outbox-key requires --drain-outbox')
    if args.reconcile_coding:
        ap.error('--reconcile-coding requires --drain-outbox')
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
