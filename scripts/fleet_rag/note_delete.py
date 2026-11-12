"""Remove one fleet Coding-folder note from the Fleet RAG corpus (AFC-728).

After the Apple Note is gone (or already missing on a safe retry), the Mac runs this module
(``python3 -m fleet_rag.note_delete``) with one JSON object on stdin holding ``note_id``.
It deletes Qdrant points and drops the ingest state row.  Credentials come only from
``FLEET_RECALL_INFISICAL_TOKEN`` or an explicit environment (see ``fleet_rag.core.load_config``).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from . import core, ingest
from .core import FleetRagError, eprint
from .ingest import STATE_PATH, IngestLock, IngestLocked, Writer, load_state, save_state
from .note_upsert import DEFAULT_LOCK_TIMEOUT_S, note_doc_id


def delete_written_note(
    raw_note_id: str,
    *,
    lock_timeout_s: float = DEFAULT_LOCK_TIMEOUT_S,
    log=eprint,
) -> dict[str, Any]:
    """Delete one note's RAG doc by Apple Note id.  Never raises to callers."""
    try:
        doc_id = note_doc_id(raw_note_id)
    except FleetRagError as exc:
        return {"ok": False, "error": str(exc)}
    state_path = STATE_PATH
    try:
        cfg = core.load_config(need_write=True)
        qd = core.Qdrant(cfg)
    except FleetRagError as exc:
        log(f"[apple-note] rag delete skipped: {exc}")
        return {"ok": False, "error": str(exc)}
    try:
        with IngestLock(state_path.parent / "ingest.lock", timeout_s=lock_timeout_s):
            state = load_state(state_path)
            rec = state.get(doc_id)
            if not isinstance(rec, dict):
                return {"ok": True, "skipped": True, "reason": "no state row", "doc_id": doc_id}
            chunk_ids = list(rec.get("chunk_ids") or [])
            keep: set[str] = set()
            for did, row in state.items():
                if did == doc_id or not isinstance(row, dict):
                    continue
                keep.update(row.get("chunk_ids") or [])
            victims = [pid for pid in chunk_ids if pid not in keep]
            writer = Writer(cfg, qd, dry_run=False, log=log)
            writer.delete(victims)
            del state[doc_id]
            save_state(state, state_path)
            return {
                "ok": True,
                "doc_id": doc_id,
                "chunks_deleted": len(victims),
            }
    except IngestLocked:
        msg = "another ingest holds the lock"
        log(f"[apple-note] rag delete deferred: {msg}")
        return {"ok": False, "deferred": True, "error": msg}
    except FleetRagError as exc:
        log(f"[apple-note] rag delete failed: {exc}")
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        log(f"[apple-note] rag delete failed: {type(exc).__name__}")
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
        default=DEFAULT_LOCK_TIMEOUT_S,
        help="seconds to wait for ingest.lock (default "
        f"{DEFAULT_LOCK_TIMEOUT_S:g}; 0 fails fast when the lock is held)",
    )
    args = ap.parse_args(argv)
    try:
        payload = _read_stdin()
    except (FleetRagError, ValueError, UnicodeDecodeError) as exc:
        eprint(f"[apple-note] rag delete refused: {exc}")
        return 1
    note_id = payload.get("note_id")
    if not isinstance(note_id, str) or not note_id.strip():
        eprint("[apple-note] rag delete refused: need note_id")
        return 1
    result = delete_written_note(note_id, lock_timeout_s=args.lock_timeout)
    if result.get("skipped"):
        return 0
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
