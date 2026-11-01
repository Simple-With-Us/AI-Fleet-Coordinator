"""Ingest orchestrator for the fleet-agents corpus.

    python3 -m fleet_rag.ingest --all [--dry-run] [--limit N] [--prune]
    python3 -m fleet_rag.ingest --source board,doc [--since 2026-08-01 | --since 7d]
    python3 -m fleet_rag.ingest --fix-seeds

Pipeline per document (see docs/RAG-FLEET-INFRA.md, payload schema v2):
  1. skip when the document hash is unchanged since the last run (state file)
  2. chunk_markdown -> scrub each chunk -> payload v2 -> deterministic point id
  3. gitleaks gate over a staged JSONL of the new rows; flagged rows are dropped.  The gate
     fails CLOSED: a GitleaksError (gitleaks present but the scan did not complete) is a
     source error — the group is not written, the run reports it, and the exit code is 1.
  4. embed only the point ids that are not already in the collection (cheap re-runs)
  5. upsert in batches of 64 (wait=true), delete the document's stale chunk ids, write state

Stale chunk ids for a changed document are the union of the state file's ids and whatever the
collection currently holds under that doc_id (so ids that never made it into state — a crash
after upsert, a hand-seeded point — are still retired).  `--prune` additionally deletes the
chunks of documents a source no longer yields at all, and drops their state rows; it refuses
to run with `--limit` because a truncated source would look like mass deletion.

Documents are processed in small groups so one gitleaks run and one existence check cover
many documents; state is written after every group, so an interrupted run resumes cleanly
and re-embeds nothing that already landed.  A non-blocking flock on
~/apps/fleet-rag/state/ingest.lock keeps two ingests from interleaving (exit 3 when held).

A full untruncated `ingest --all --prune` run ends by upserting the ingest sentinel
(fleet_rag.health) with ok=true/false so the Hetzner health rows see the outcome;  the
heartbeat-URL GET is an optional extra.  That nightly pass is the only run that attests health:
a partial or unpruned one still writes its log and state, but leaves the sentinel and
last-run.json alone, so it cannot hide a stale or failed night.

Known limitation — shared chunks: identical chunk text collapses to ONE point (the id is the
content hash), and a point carries a single doc_id.  When a group writes a row whose id is
already present but whose stored doc_id is not a document seen in this run (a renamed or
removed document), the point's document fields are refreshed to the current writer.  If two
live documents share a chunk, the payload names whichever wrote it last; the state file still
credits the id to both so neither document's edit deletes it.

Report: ~/apps/fleet-rag/state/last-run.json (written by the nightly pass only) plus one line in
~/apps/fleet-rag/logs/ingest.log (every run).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import errno
import fcntl
import json
import os
import pathlib
import re
import sys
import tempfile
import time
import traceback
import urllib.request
import uuid
from collections.abc import Callable, Iterable
from typing import Any

from . import core, health, sources, telemetry
from .chunk import chunk_markdown
from .core import FleetRagError, Qdrant, build_point, content_hash, eprint, match_filter, now_ms
from .scrub import GitleaksError, gitleaks_flagged, scrub

RAG_DIR = pathlib.Path.home() / "apps" / "fleet-rag"
STATE_DIR = RAG_DIR / "state"
LOG_DIR = RAG_DIR / "logs"
STATE_PATH = STATE_DIR / "ingest-state.json"
LAST_RUN_PATH = STATE_DIR / "last-run.json"
LOCK_PATH = STATE_DIR / "ingest.lock"
LOG_PATH = LOG_DIR / "ingest.log"
HEARTBEAT_KEY = "FLEET_RAG_INGEST_HEARTBEAT_URL"

SEED_DOC_ID = "seed/fleet-standards"
SEED_ISO = "2026-08-31T22:00:00+00:00"  # the real seed time
SEED_MS = int(_dt.datetime.fromisoformat(SEED_ISO).timestamp() * 1000)  # 1788213600000

UPSERT_BATCH = 64
DELETE_BATCH = 256
GROUP_DOCS = 48  # documents per gitleaks / existence-check / embed group
GROUP_CHUNKS = 512
EMBED_RATE = 24.0  # texts per second budget for the shared TEI box
PROGRESS_EVERY = 200
EXIT_LOCKED = 3
#  The marker a pending nightly leaves behind, so a partial refresh that arrives while the
#  nightly is still waiting cannot steal the lock in the gap between the waiter's polls and
#  then hold it for its whole budget.  The file holds the nightley's deadline in epoch ms;
#  an expired marker is treated as absent, so a killed nightly cannot wedge the fleet.
PENDING_NIGHTLY_NAME = "ingest.nightly.pending"

# Payload fields that describe the owning document (everything except the content fields).
_CONTENT_FIELDS = ("text", "content_hash", "embed_model")

# Injection points for tests (monkeypatch these names on the module).
embed: Callable[[dict, list[str]], list[list[float]]] = core.embed
load_config = core.load_config
QdrantClient = Qdrant
gitleaks_gate = gitleaks_flagged
write_sentinel = health.write_ingest_sentinel
GENERATORS = sources.GENERATORS


class IngestLocked(FleetRagError):
    """Another ingest holds the lock file."""


class NightlyPending(FleetRagError):
    """A nightly ingest is pending, so a partial refresh must not take the lock."""


# --------------------------------------------------------------------------- lock


class IngestLock:
    """Exclusive flock; raises IngestLocked when another process still holds the lock
    after timeout_s (0 = non-blocking, the partial-refresh behavior)."""

    def __init__(self, path: pathlib.Path, timeout_s: float = 0):
        self.path = pathlib.Path(path)
        self.timeout_s = timeout_s
        self._fd: int | None = None

    def __enter__(self) -> IngestLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_s
        while True:
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                os.close(fd)
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                    if time.monotonic() >= deadline:
                        raise IngestLocked(
                            f"another ingest is running (lock held: {self.path}); "
                            f"wait for it or remove a stale lock, exit {EXIT_LOCKED}"
                        ) from None
                    time.sleep(min(5.0, max(0.1, deadline - time.monotonic())))
                    continue
                raise
        try:
            os.ftruncate(fd, 0)
            os.write(
                fd, f"{os.getpid()} {_dt.datetime.now().isoformat(timespec='seconds')}\n".encode()
            )
        except OSError:
            pass
        self._fd = fd
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
            self._fd = None


# --------------------------------------------------------------------------- state


def load_state(path: pathlib.Path = STATE_PATH) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict, path: pathlib.Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=0, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _write_json(path: pathlib.Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


# --------------------------------------------------------------------------- qdrant helpers


def existing_points(qd: Qdrant, ids: list[str], batch: int = 256) -> dict[str, str | None]:
    """{id: stored doc_id} for the ids already present (POST /collections/{c}/points, read key)."""
    found: dict[str, str | None] = {}
    for i in range(0, len(ids), batch):
        res = qd._call(
            qd._cpath("/points"),
            {"ids": ids[i : i + batch], "with_payload": ["doc_id"], "with_vector": False},
        )
        for p in res.get("result", []):
            pl = p.get("payload") or {}
            found[str(p.get("id"))] = pl.get("doc_id") if isinstance(pl, dict) else None
    return found


def existing_ids(qd: Qdrant, ids: list[str], batch: int = 256) -> set[str]:
    """Ids already present in the collection."""
    return set(existing_points(qd, ids, batch))


def collection_ids_for_doc(qd: Qdrant | None, doc_id: str) -> list[str]:
    """Point ids the collection currently holds under doc_id (read-only scroll, no payload)."""
    if qd is None or not doc_id:
        return []
    flt = match_filter(doc_id=doc_id)
    if not flt:
        return []
    return [str(p.get("id")) for p in qd.scroll(flt, with_payload=False)]


def _embed_throttled(cfg: dict, texts: list[str]) -> list[list[float]]:
    t0 = time.monotonic()
    vecs = embed(cfg, texts)
    budget = len(texts) / EMBED_RATE
    spent = time.monotonic() - t0
    if spent < budget:
        time.sleep(budget - spent)
    return vecs


# --------------------------------------------------------------------------- rows


def _payload(doc: sources.Doc, ch, text: str, kinds: list[str], count: int, run_id: str) -> dict:
    p = {
        "source": doc.source,
        "app": doc.app,
        "category": doc.category,
        "seat": doc.seat,
        "doc_id": doc.doc_id,
        "chunk_index": ch.index,
        "chunk_count": count,
        "heading": ch.heading,
        "title": doc.title,
        "url": doc.url or "",
        "path": doc.path or "",
        "created_at": int(doc.created_at_ms or doc.updated_at_ms or 0),
        "updated_at": int(doc.updated_at_ms or doc.created_at_ms or 0),
        "ingest_run": run_id,
    }
    if kinds:
        p["scrubbed"] = kinds
    return p


def doc_fields(payload: dict) -> dict:
    """The document-describing part of a payload (used to re-point a shared chunk)."""
    return {k: v for k, v in payload.items() if k not in _CONTENT_FIELDS}


def doc_rows(doc: sources.Doc, run_id: str) -> tuple[list[dict], int]:
    """Chunk + scrub one document into points (without vectors).  Returns (rows, scrubbed_count)."""
    chunks = chunk_markdown(doc.text_markdown, prefix=doc.title)
    rows: list[dict] = []
    scrubbed = 0
    for ch in chunks:
        text, kinds = scrub(ch.text)
        if kinds:
            scrubbed += 1
        rows.append(build_point(text, _payload(doc, ch, text, kinds, len(chunks), run_id)))
    return rows, scrubbed


def gitleaks_drop(rows: list[dict]) -> tuple[list[dict], int]:
    """Stage rows as JSONL, run the gitleaks gate, drop the flagged lines.

    Raises GitleaksError (fail closed) when gitleaks is present but could not complete.
    """
    if not rows:
        return rows, 0
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8") as fh:
        for r in rows:
            fh.write(
                json.dumps(
                    {"doc_id": r["payload"]["doc_id"], "text": r["payload"]["text"]},
                    ensure_ascii=False,
                )
                + "\n"
            )
        staged = fh.name
    try:
        flagged = gitleaks_gate(staged)
    finally:
        try:
            os.unlink(staged)
        except OSError:
            pass
    if not flagged:
        return rows, 0
    kept = [r for n, r in enumerate(rows, 1) if n not in flagged]
    return kept, len(rows) - len(kept)


# --------------------------------------------------------------------------- core write path


class Writer:
    """Embeds + upserts point rows, honouring dry-run and the existence check."""

    def __init__(self, cfg: dict | None, qd: Qdrant | None, dry_run: bool, log=eprint):
        self.cfg, self.qd, self.dry_run, self.log = cfg, qd, dry_run, log
        self.embedded = 0
        self.repointed = 0
        self._progress_mark = 0

    def missing(self, rows: list[dict], live_owners: set[str] | None = None) -> list[dict]:
        """Rows whose id is not in the collection yet.

        A present id whose stored doc_id is not in `live_owners` (documents seen so far this
        run) belongs to a renamed/removed document: its document fields are refreshed to the
        row's document so the point is not left pointing at a doc_id that no longer exists.
        `live_owners=None` disables the refresh (raw-row ingest has no run context).
        """
        if not rows or self.qd is None:
            return rows
        present = existing_points(self.qd, [r["id"] for r in rows])
        out: list[dict] = []
        for r in rows:
            if r["id"] not in present:
                out.append(r)
                continue
            owner = present[r["id"]]
            if (
                live_owners is not None
                and owner
                and owner != r["payload"]["doc_id"]
                and owner not in live_owners
            ):
                self.repoint(r)
        return out

    def repoint(self, row: dict) -> None:
        self.repointed += 1
        if not self.dry_run and self.qd is not None:
            self.qd.set_payload([row["id"]], doc_fields(row["payload"]), wait=True)

    def write(self, rows: list[dict]) -> int:
        """Embed + upsert rows (already filtered to missing).  Returns the number written."""
        if not rows or self.dry_run:
            return len(rows)
        for i in range(0, len(rows), UPSERT_BATCH):
            batch = rows[i : i + UPSERT_BATCH]
            vecs = _embed_throttled(self.cfg, [r["payload"]["text"] for r in batch])
            points = [
                {"id": r["id"], "vector": v, "payload": r["payload"]} for r, v in zip(batch, vecs)
            ]
            self.qd.upsert(points, wait=True)
            self.embedded += len(batch)
            if self.embedded - self._progress_mark >= PROGRESS_EVERY:
                self._progress_mark = self.embedded
                self.log(f"  ... {self.embedded} chunks embedded")
        return len(rows)

    def delete(self, ids: list[str]) -> None:
        """Delete by explicit id only (never by filter, so an empty filter can never wipe)."""
        if not ids or self.dry_run or self.qd is None:
            return
        for i in range(0, len(ids), DELETE_BATCH):
            self.qd.delete_ids(ids[i : i + DELETE_BATCH], wait=True)


def _empty_stats() -> dict:
    return {
        "docs_seen": 0,
        "docs_changed": 0,
        "docs_pruned": 0,
        "chunks_new": 0,
        "chunks_deleted": 0,
        "chunks_repointed": 0,
        "chunks_dropped_by_gitleaks": 0,
        "chunks_scrubbed": 0,
    }


def _parse_since(since: str | int | None) -> int | None:
    if since is None or since == "":
        return None
    if isinstance(since, int):
        return since
    s = str(since).strip()
    m = re.fullmatch(r"(\d+)d", s)
    if m:
        return now_ms() - int(m.group(1)) * 86_400_000
    if re.fullmatch(r"\d{13}", s):
        return int(s)
    ts = sources.parse_ts_ms(s)
    if not ts:
        raise FleetRagError(
            f"--since must be YYYY-MM-DD, an ISO stamp, Nd, or epoch ms (got {s!r})"
        )
    return ts


def _dedupe_rows(rows: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        out.append(r)
    return out


def process_group(
    docs: list[tuple[sources.Doc, str]],
    state: dict,
    writer: Writer,
    run_id: str,
    stats: dict,
    state_path: pathlib.Path,
    live_owners: set[str] | None = None,
) -> None:
    """Chunk/scrub/gate/embed/upsert a group of changed documents, then update state."""
    per_doc: list[tuple[sources.Doc, str, list[dict]]] = []
    all_rows: list[dict] = []
    for doc, doc_hash in docs:
        rows, scrubbed = doc_rows(doc, run_id)
        stats["chunks_scrubbed"] += scrubbed
        per_doc.append((doc, doc_hash, rows))
        all_rows.extend(rows)
    kept, dropped = gitleaks_drop(_dedupe_rows(all_rows))  # raises GitleaksError: fail closed
    stats["chunks_dropped_by_gitleaks"] += dropped
    kept_ids = {r["id"] for r in kept}
    before = writer.repointed
    new_rows = writer.missing(kept, live_owners)
    stats["chunks_repointed"] += writer.repointed - before
    stats["chunks_new"] += len(new_rows)
    writer.write(new_rows)

    # Ids owned by other documents must survive a stale-chunk delete (identical text collapses
    # to one point shared by several documents).
    group_ids = {d.doc_id for d, _, _ in per_doc}
    shared: set[str] = set()
    for did, rec in state.items():
        if did not in group_ids:
            shared.update(rec.get("chunk_ids", []))
    for d, _, rows in per_doc:
        shared.update(r["id"] for r in rows if r["id"] in kept_ids)

    for doc, doc_hash, rows in per_doc:
        new_ids = [r["id"] for r in rows if r["id"] in kept_ids]
        old_ids = list(state.get(doc.doc_id, {}).get("chunk_ids", []))
        for pid in collection_ids_for_doc(writer.qd, doc.doc_id):
            if pid not in old_ids:
                old_ids.append(pid)
        new_set = set(new_ids)
        stale = [i for i in old_ids if i not in new_set and i not in shared]
        writer.delete(stale)
        stats["chunks_deleted"] += len(stale)
        if not writer.dry_run:
            state[doc.doc_id] = {
                "doc_hash": doc_hash,
                "chunk_ids": new_ids,
                "updated_at": int(doc.updated_at_ms or 0),
                "refreshed_at": now_ms(),
                "source": doc.source,
            }
    if not writer.dry_run:
        save_state(state, state_path)


def prune_source(
    name: str, yielded: set[str], state: dict, writer: Writer, stats: dict, state_path: pathlib.Path
) -> list[str]:
    """Delete the chunks of documents `name` no longer yields and drop their state rows.

    Ids still credited to any surviving state row are kept (shared chunks).  Returns the
    pruned doc_ids.  Only ever deletes by explicit id.
    """
    gone = sorted(
        did
        for did, rec in state.items()
        if isinstance(rec, dict) and rec.get("source") == name and did not in yielded
    )
    if not gone:
        return []
    gone_set = set(gone)
    keep: set[str] = set()
    for did, rec in state.items():
        if did not in gone_set:
            keep.update(rec.get("chunk_ids", []))
    victims: list[str] = []
    seen: set[str] = set()
    for did in gone:
        for pid in state[did].get("chunk_ids", []):
            if pid not in keep and pid not in seen:
                seen.add(pid)
                victims.append(pid)
    writer.delete(victims)
    stats["docs_pruned"] += len(gone)
    stats["chunks_deleted"] += len(victims)
    if not writer.dry_run:
        for did in gone:
            del state[did]
        save_state(state, state_path)
    return gone


def _pending_nightly_dir(state_path: pathlib.Path) -> pathlib.Path:
    """Where the pending-nightly claims live, beside the lock they guard."""
    return pathlib.Path(state_path).parent / PENDING_NIGHTLY_NAME


def _pending_owner() -> str:
    """A name for one run's claim, unique per process and per run."""
    return f"{os.getpid()}-{uuid.uuid4().hex[:12]}"


def _write_pending_nightly(directory: pathlib.Path, deadline_ms: int, owner: str) -> bool:
    """Claim the queue for a nightly that is about to wait for the lock.

    Each claim is its own file, named for the run that made it, so two runs can never
    overwrite each other's claim and neither can clear the other's.  One shared marker
    file could only name one owner, so the run that finished first cleared the claim of
    the run that was still waiting, and a partial could then take the lock from it
    (Sentry review, PR #813).
    """
    try:
        directory.mkdir(parents=True, exist_ok=True)
        _prune_expired_claims(directory)
        path = directory / owner
        tmp = directory / f".{owner}.tmp"
        tmp.write_text(
            json.dumps({"pid": os.getpid(), "deadline_ms": deadline_ms}) + "\n", encoding="utf-8"
        )
        os.replace(tmp, path)
        return True
    except OSError:
        #  A claim that cannot be written must not stop the nightly:  the lock still
        #  serializes the runs, and a partial that slips in is no worse than before.
        return False


def _clear_pending_nightly(directory: pathlib.Path, owner: str) -> bool:
    """Release one run's claim.  Only that run's claim:  the path is named for it."""
    try:
        (directory / owner).unlink()
        return True
    except OSError:
        return False


def _claim_deadline_ms(entry: pathlib.Path) -> int | None:
    """The deadline of one claim file, or None when it cannot be read."""
    try:
        payload = json.loads(entry.read_text(encoding="utf-8"))
        return int(payload.get("deadline_ms", 0))
    except (OSError, ValueError, TypeError):
        return None


def _prune_expired_claims(directory: pathlib.Path) -> None:
    """Drop the claims that have expired, so a killed nightly leaves nothing behind."""
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.name.startswith("."):
            #  A temp file on its way in, not a claim.
            continue
        deadline_ms = _claim_deadline_ms(entry)
        if deadline_ms is None or deadline_ms <= now_ms():
            try:
                entry.unlink()
            except OSError:
                pass


def _nightly_pending(directory: pathlib.Path) -> bool:
    """Whether any nightly has claimed the queue and its claim has not expired.

    An expired claim is treated as absent, so a nightly that was killed leaves nothing
    behind that wedges the partial refreshes.
    """
    try:
        entries = list(directory.iterdir())
    except OSError:
        return False
    for entry in entries:
        if entry.name.startswith("."):
            #  A temp file on its way in, not a claim.
            continue
        deadline_ms = _claim_deadline_ms(entry)
        if deadline_ms is not None and deadline_ms > now_ms():
            return True
    return False


def _resolve_names(source_names: Iterable[str] | str | None) -> list[str]:
    """The source list a run will actually use.

    One resolver, so every caller decides on the same list.  The command line needs it to
    answer "is this run a health-attesting nightly?" before the run starts, and the run
    needs it to decide the same thing after it resolves the names (AFC-423).
    """
    if source_names in ("all", None):
        return list(sources.NIGHTLY_SOURCES)
    if isinstance(source_names, str):
        return [s.strip() for s in source_names.split(",") if s.strip()]
    return [s.strip() for s in source_names if s.strip()]


def _attests_health(
    names: list[str],
    prune: bool,
    since_ms: int | None,
    limit: int | None,
    dry_run: bool = False,
) -> bool:
    """Whether this run writes the sentinel and last-run.json, and so can attest health.

    One predicate, used by the run that writes the sentinel and by the command line that
    decides the wait budget.  The two used to disagree -- the sentinel was keyed on the
    resolved source set and the wait on the `--all` flag -- so an explicitly enumerated
    nightly run attested health and still failed at once with exit 3 when the lock was
    held, which is the exact hole the wait was added to close (AFC-423).

    A dry run attests nothing:  the sentinel write sits behind `if not dry_run:`, so a dry
    run writes no sentinel and no last-run.json.  It must therefore be given no wait and
    must not claim the queue either, or a diagnostic would lock the partial refreshes
    out for the length of the claim (Sentry review, PR #813).
    """
    return (
        not dry_run
        and prune
        and set(names) == set(sources.NIGHTLY_SOURCES)
        and since_ms is None
        and limit is None
    )


def run(
    source_names: Iterable[str] | str = "all",
    dry_run: bool = False,
    since: str | int | None = None,
    limit: int | None = None,
    state_path: pathlib.Path = STATE_PATH,
    last_run_path: pathlib.Path = LAST_RUN_PATH,
    log_path: pathlib.Path = LOG_PATH,
    cfg: dict | None = None,
    qd: Qdrant | None = None,
    log=eprint,
    heartbeat: bool = True,
    prune: bool = False,
    lock_path: pathlib.Path | None = None,
    lock_timeout_s: float = 0,
) -> dict:
    """Run the ingest over the named sources.  Returns the report dict (also written to disk).

    Raises IngestLocked (exit 3 from the CLI) when another ingest holds the lock.
    """
    names = _resolve_names(source_names)
    unknown = [n for n in names if n not in GENERATORS]
    if unknown:
        raise FleetRagError(
            f"unknown source(s): {', '.join(unknown)}; known: {', '.join(GENERATORS)}"
        )
    if prune and "linear" in names and set(names) != set(sources.NIGHTLY_SOURCES):
        raise FleetRagError(
            "Linear prune requires a separately verified complete deletion inventory; "
            "this generator is incremental even with FLEET_RAG_LINEAR_FULL. "
            "Use --source linear without --prune to refresh."
        )
    if prune and limit:
        raise FleetRagError(
            "--prune cannot be combined with --limit: a truncated source would prune "
            "every document past the limit"
        )
    since_ms = _parse_since(since)
    lock_path = lock_path or (state_path.parent / "ingest.lock")
    pending_dir = _pending_nightly_dir(state_path)
    attests = _attests_health(names, prune, since_ms, limit, dry_run)
    #  Claim the queue BEFORE the lock, but only when this run will actually wait for it.
    #  A run that waits for no time, or one that attests nothing, must not lock the partial
    #  refreshes out for the length of the claim.
    owner = _pending_owner()
    claimed = False
    if attests and lock_timeout_s > 0:
        #  A partial refresh that arrives while this run is still waiting would otherwise
        #  take the lock in the gap between the waiter's polls and then hold it for its
        #  whole budget, which is how a nightly gets locked out even though it is willing
        #  to wait (AFC-423).  The claim expires, so a killed nightly leaves nothing behind.
        claimed = _write_pending_nightly(
            pending_dir,
            now_ms() + int((2 * max(lock_timeout_s, 3600)) * 1000),
            owner,
        )
    if not attests and _nightly_pending(pending_dir):
        #  A partial refresh never waits:  it fails fast, because the sheet sync's ingest is
        #  best-effort and a long wait there would hold its own budget open.  It must not take
        #  the lock from a nightly that is waiting for it either.  A dry run is in the same
        #  case:  it writes nothing, but it would still take the lock from the waiter.
        raise NightlyPending(
            f"a nightly ingest is pending ({pending_dir}); this run must not take the lock, "
            f"exit {EXIT_LOCKED}"
        )
    try:
        with IngestLock(lock_path, timeout_s=lock_timeout_s):
            return _run_locked(
                names,
                dry_run,
                since_ms,
                limit,
                state_path,
                last_run_path,
                log_path,
                cfg,
                qd,
                log,
                heartbeat,
                prune,
            )
    finally:
        if claimed:
            #  Release this run's claim and only this run's claim:  another nightly may be
            #  waiting for the same lock, and its claim is its own file.
            _clear_pending_nightly(pending_dir, owner)


def _run_locked(
    names: list[str],
    dry_run: bool,
    since_ms: int | None,
    limit: int | None,
    state_path: pathlib.Path,
    last_run_path: pathlib.Path,
    log_path: pathlib.Path,
    cfg: dict | None,
    qd: Qdrant | None,
    log,
    heartbeat: bool,
    prune: bool,
) -> dict:
    run_id = f"{_dt.datetime.now(_dt.UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    started = now_ms()
    report: dict[str, Any] = {
        "run_id": run_id,
        "started_at": started,
        "finished_at": None,
        "ok": True,
        "dry_run": dry_run,
        "prune": prune,
        "sources": names,
        "per_source": {},
        "errors": [],
        "warnings": [],
    }

    if cfg is None or (qd is None and not dry_run):
        try:
            cfg = cfg or load_config(need_write=not dry_run)
        except FleetRagError as e:
            if dry_run:
                log(f"dry-run without credentials ({e}); every chunk counts as new")
                cfg = None
            else:
                raise
    if qd is None and cfg is not None:
        try:
            qd = QdrantClient(cfg)
        except FleetRagError as e:
            if not dry_run:
                raise
            log(f"dry-run without Qdrant ({e}); every chunk counts as new")
            qd = None
    writer = Writer(cfg, qd, dry_run, log)
    state = load_state(state_path)
    state_touched = False
    t_run = time.monotonic()
    seen_run: dict[str, str] = {}  # doc_id -> source, for the duplicate guard
    live_owners: set[str] = set()  # doc_ids yielded so far (shared-chunk re-pointing)
    sources.take_warnings()  # drop anything left over from an earlier call

    for name in names:
        stats = _empty_stats()
        report["per_source"][name] = stats
        t0 = time.monotonic()
        log(f"[{name}] start")
        group: list[tuple[sources.Doc, str]] = []
        group_chunks = 0
        yielded: set[str] = set()
        source_ok = True
        linear_receipt = (
            {} if name == "linear" and GENERATORS[name] is sources.iter_linear else None
        )
        if linear_receipt is not None:
            report["linear_read_receipt"] = linear_receipt
        try:
            kwargs = {"limit": limit}
            if linear_receipt is not None:
                kwargs["receipt"] = linear_receipt
            for doc in GENERATORS[name](**kwargs):
                stats["docs_seen"] += 1
                if doc.doc_id in seen_run:
                    raise FleetRagError(
                        f"duplicate doc_id {doc.doc_id!r}: already yielded by "
                        f"source {seen_run[doc.doc_id]!r} in this run"
                    )
                seen_run[doc.doc_id] = name
                yielded.add(doc.doc_id)
                live_owners.add(doc.doc_id)
                if since_ms and (doc.updated_at_ms or doc.created_at_ms) < since_ms:
                    continue
                doc_hash = content_hash(doc.text_markdown)
                prev = state.get(doc.doc_id)
                if prev and prev.get("doc_hash") == doc_hash:
                    # Content verified unchanged against the committed corpus:  the hash
                    # match is proof the corpus holds this exact content, so record the
                    # verification and carry the provider timestamp forward.  A state row
                    # advances only through this hash check or a committed upsert - a
                    # fetched-but-uncommitted doc (failed upsert, dry run, crashed run)
                    # advances nothing and is retried.
                    prev["refreshed_at"] = now_ms()
                    doc_updated = int(doc.updated_at_ms or 0)
                    if doc_updated > int(prev.get("updated_at") or 0):
                        prev["updated_at"] = doc_updated
                    state_touched = True
                    continue
                stats["docs_changed"] += 1
                group.append((doc, doc_hash))
                group_chunks += max(len(doc.text_markdown) // 1400, 1)
                if len(group) >= GROUP_DOCS or group_chunks >= GROUP_CHUNKS:
                    process_group(group, state, writer, run_id, stats, state_path, live_owners)
                    group, group_chunks = [], 0
            if group:
                process_group(group, state, writer, run_id, stats, state_path, live_owners)
            if (
                linear_receipt is not None
                and linear_receipt.get("full")
                and any(
                    row.get("status") == "skipped"
                    for row in linear_receipt.get("issues", {}).values()
                )
            ):
                raise sources.LinearSourceError(
                    "linear: FULL read skipped issues; replacement is incomplete"
                )
        except Exception as e:  # noqa: BLE001 — one bad source must not sink the others
            source_ok = False
            msg = f"{name}: {type(e).__name__}: {e}"
            report["errors"].append(msg)
            report["ok"] = False
            log(f"[{name}] ERROR {msg}")
            telemetry.capture_exception(e, operation="ingest", source=name)
            if os.environ.get("FLEET_RAG_DEBUG"):
                traceback.print_exc()
        if linear_receipt is not None:
            # Read completeness is not deletion authority. State verification describes
            # committed document hashes only, not independent Qdrant/chunk coverage.
            verified = 0
            for uid, row in linear_receipt.get("issues", {}).items():
                prev = state.get(f"linear/{uid}", {})
                matches = (
                    not dry_run
                    and row.get("status") == "fetched"
                    and prev.get("doc_hash") == row.get("content_sha256")
                )
                row["state_hash_verified"] = bool(matches)
                verified += bool(matches)
            linear_receipt["state_hash_verified_count"] = verified
            linear_receipt["source_ok"] = source_ok
            linear_receipt["deletion_authorized"] = False
            if not source_ok:
                linear_receipt["source_complete"] = False
        for w in sources.take_warnings():
            report["warnings"].append(f"{name}: {w}")
            log(f"[{name}] WARNING {w}")
        # Linear is a delta stream, not an authoritative deletion inventory. Even
        # FULL refetch can skip incomplete/truncated issues. Never prune its
        # unyielded rows from this pipeline; explicit migration reconciliation
        # owns deletion separately after per-document coverage verification.
        if prune and source_ok and name == "linear":
            report["warnings"].append(
                "linear: pruning skipped; generator is not a complete deletion inventory"
            )
        if prune and source_ok and name != "linear":
            try:
                gone = prune_source(name, yielded, state, writer, stats, state_path)
                if gone:
                    log(
                        f"[{name}] {'would prune' if dry_run else 'pruned'} {len(gone)} document(s): "
                        + ", ".join(gone[:10])
                        + (" ..." if len(gone) > 10 else "")
                    )
            except Exception as e:  # noqa: BLE001
                msg = f"{name}: prune: {type(e).__name__}: {e}"
                report["errors"].append(msg)
                report["ok"] = False
                log(f"[{name}] ERROR {msg}")
                telemetry.capture_exception(e, operation="ingest-prune", source=name)
        log(f"[{name}] done in {time.monotonic() - t0:.1f}s: {json.dumps(stats)}")

    if state_touched and not dry_run:
        save_state(state, state_path)
    report["finished_at"] = now_ms()
    report["wall_s"] = round(time.monotonic() - t_run, 1)
    if not dry_run:
        # Only an untruncated full nightly-source pass can attest global health.
        # Partial/manual refreshes keep their log and state without hiding a stale night.
        nightly = _attests_health(names, prune, since_ms, limit, dry_run)
        if nightly:
            _write_sentinel_safely(cfg, qd, report, log)
            _write_json(last_run_path, report)
            if report["ok"] and heartbeat:
                _heartbeat()
        _append_log(log_path, report)
    return report


def _write_sentinel_safely(cfg: dict | None, qd: Qdrant | None, report: dict, log) -> None:
    """Upsert the ingest sentinel with the run outcome (ok=false on a failed run too)."""
    if cfg is None or qd is None:
        report["sentinel"] = None
        return
    try:
        report["sentinel"] = write_sentinel(cfg, qd, report)
    except Exception as e:  # noqa: BLE001 — the health rows will page on a stale sentinel
        msg = f"sentinel: {type(e).__name__}: {e}"
        report["errors"].append(msg)
        report["ok"] = False
        report["sentinel"] = None
        log(f"ERROR {msg}")
        telemetry.capture_exception(e, operation="ingest-sentinel")


def _append_log(log_path: pathlib.Path, report: dict) -> None:
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        totals = {
            k: sum(s.get(k, 0) for s in report["per_source"].values()) for k in _empty_stats()
        }
        line = (
            f"{_dt.datetime.now().isoformat(timespec='seconds')} run={report['run_id']} "
            f"ok={report['ok']} wall={report.get('wall_s')}s sources={','.join(report['sources'])} "
            f"{json.dumps(totals)} errors={len(report['errors'])} "
            f"warnings={len(report.get('warnings', []))}\n"
        )
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def _heartbeat() -> None:
    """GET the heartbeat URL (environment, then Infisical `fleet-secrets get`) if defined.  Never
    prints the URL.  No credential file is read (AFC-359)."""
    url = (os.environ.get(HEARTBEAT_KEY) or "").strip()
    if not url:
        url = (core.fleet_secret(HEARTBEAT_KEY) or "").strip()
    if not url:
        return
    try:
        with urllib.request.urlopen(url, timeout=10):
            pass
    except Exception:  # noqa: BLE001 — heartbeat failures are never fatal
        pass


# --------------------------------------------------------------------------- raw JSONL rows


def ingest_rows(
    rows: list[dict],
    defaults: dict,
    dry_run: bool = False,
    cfg: dict | None = None,
    qd: Qdrant | None = None,
    log=eprint,
) -> dict:
    """Scrub + gitleaks-gate + upsert pre-chunked rows (the `fleet-rag.py ingest FILE` path).

    Each row needs `text`; other payload v2 fields fall back to `defaults`.  No state tracking
    — raw rows are addressed purely by content hash.  Raises GitleaksError (nothing written)
    when the gate cannot complete.
    """
    run_id = f"rows-{_dt.datetime.now(_dt.UTC).strftime('%Y%m%dT%H%M%SZ')}"
    if cfg is None:
        cfg = load_config(need_write=not dry_run)
    if qd is None:
        qd = QdrantClient(cfg)
    writer = Writer(cfg, qd, dry_run, log)
    stamp = now_ms()
    points: list[dict] = []
    scrubbed = 0
    for i, row in enumerate(rows):
        text, kinds = scrub(str(row.get("text", "")))
        if kinds:
            scrubbed += 1
        created = int(row.get("created_at") or defaults.get("created_at") or stamp)
        payload = {
            "source": row.get("source", defaults.get("source", "doc")),
            "app": sources.app_slug(row.get("app", defaults.get("app", "fleet"))),
            "category": row.get("category", defaults.get("category", "lesson")),
            "seat": sources.seat_tag(row.get("seat", defaults.get("seat", "CLAUDE"))),
            "doc_id": row.get("doc_id", defaults.get("doc_id", "rows/unnamed")),
            "chunk_index": int(row.get("chunk_index", i)),
            "chunk_count": int(row.get("chunk_count", len(rows))),
            "heading": row.get("heading", ""),
            "title": row.get("title", defaults.get("title", "")),
            "url": row.get("url", ""),
            "path": row.get("path", ""),
            "created_at": created,
            "updated_at": int(row.get("updated_at") or created),
            "ingest_run": run_id,
        }
        if kinds:
            payload["scrubbed"] = kinds
        points.append(build_point(text, payload))
    kept, dropped = gitleaks_drop(_dedupe_rows(points))
    new_rows = writer.missing(kept)
    writer.write(new_rows)
    return {
        "rows": len(rows),
        "scrubbed": scrubbed,
        "dropped_by_gitleaks": dropped,
        "already_present": len(kept) - len(new_rows),
        "written": 0 if dry_run else len(new_rows),
        "would_write": len(new_rows) if dry_run else 0,
        "run_id": run_id,
    }


# --------------------------------------------------------------------------- seeds


def fix_seeds(
    dry_run: bool = False, cfg: dict | None = None, qd: Qdrant | None = None, log=eprint
) -> int:
    """Stamp the hand-seeded points (doc_id seed/fleet-standards) with the real seed time."""
    if cfg is None:
        cfg = load_config(need_write=not dry_run)
    qd = qd or QdrantClient(cfg)
    ids = [str(p["id"]) for p in qd.scroll(match_filter(doc_id=SEED_DOC_ID), with_payload=False)]
    if not ids:
        log("fix-seeds: no seed points found")
        return 0
    if dry_run:
        log(f"fix-seeds: would stamp {len(ids)} points with created_at/updated_at={SEED_MS}")
        return len(ids)
    qd.set_payload(ids, {"created_at": SEED_MS, "updated_at": SEED_MS})
    log(f"fix-seeds: stamped {len(ids)} points")
    return len(ids)


# --------------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python3 -m fleet_rag.ingest",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = ap.add_mutually_exclusive_group()
    g.add_argument(
        "--all",
        action="store_true",
        help="nightly sources (linear, doc, skill, memory, apple-note; "
        "excludes chat-log — pass --source chat-log for the rare policy scan)",
    )
    g.add_argument("--source", help="comma-separated: " + ",".join(GENERATORS))
    g.add_argument(
        "--fix-seeds",
        action="store_true",
        help=f"stamp {SEED_DOC_ID} points with the real seed time",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="chunk/scrub/gate and count, but never embed or write",
    )
    ap.add_argument("--limit", type=int, help="max documents per source")
    ap.add_argument("--since", help="only documents updated since YYYY-MM-DD | ISO | Nd | epoch ms")
    ap.add_argument(
        "--prune",
        action="store_true",
        help="after a source finishes cleanly, delete chunks of documents it no longer yields "
        "(refused with --limit)",
    )
    ap.add_argument("--no-heartbeat", action="store_true")
    ap.add_argument(
        "--lock-timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="wait up to SECONDS for the ingest lock instead of failing at once "
        "(default: 0 for a partial refresh; 6000 for a full `--all --prune` nightly, "
        "so a 90-minute sheet-sync ingest can never lock out the health-attesting run)",
    )
    args = ap.parse_args(argv)
    try:
        if args.fix_seeds:
            fix_seeds(dry_run=args.dry_run)
            return 0
        if not args.all and not args.source:
            ap.error("choose --all, --source, or --fix-seeds")
        # The nightly is the only run that attests health (sentinel + last-run.json),
        # so it waits out a partial refresh instead of exiting 3 into a stale sentinel.
        # A partial refresh keeps the fast exit and itself backs off (Kody review, PR #740).
        # The wait is keyed on the SAME predicate that gates the sentinel, not on the
        # `--all` flag, so an explicitly enumerated nightly run waits too (Kody review,
        # PR #800):  keying them differently let a health-attesting run be locked out.
        names = _resolve_names("all" if args.all else args.source)
        attests = _attests_health(
            names, args.prune, _parse_since(args.since), args.limit, args.dry_run
        )
        lock_timeout = (
            args.lock_timeout if args.lock_timeout is not None else (6000.0 if attests else 0.0)
        )
        report = run(
            "all" if args.all else args.source,
            dry_run=args.dry_run,
            since=args.since,
            limit=args.limit,
            heartbeat=not args.no_heartbeat,
            prune=args.prune,
            lock_timeout_s=lock_timeout,
        )
    except (IngestLocked, NightlyPending) as e:
        #  Both are the same class of refusal —  this run must not hold the lock now —  so
        #  both exit 3, which is what the sheet sync's best-effort ingest expects.
        eprint(f"error: {e}")
        return EXIT_LOCKED
    except (FleetRagError, GitleaksError) as e:
        eprint(f"error: {e}")
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
