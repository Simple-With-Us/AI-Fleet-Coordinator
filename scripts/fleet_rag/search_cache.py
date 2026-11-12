"""In-process LRU for identical recall_search calls.

A hit returns the previous result and does not embed or query Qdrant.  Size and TTL
come from Infisical (RECALL_SEARCH_CACHE_SIZE, RECALL_SEARCH_CACHE_TTL_SECONDS),
then the process environment, then the defaults of 256 entries and 600 seconds.
Zero disables storage.

The key is the normalized query, the filters, the limit, and the compact and full
flags, plus the collection and the switches that change the result.  The embedder
and the reranker still receive the caller's casing.  Entries also carry a corpus
token.  note_ingest_complete() and note_corpus_change() bump that token and write
search-cache-epoch.json.  The next lookup misses when that file or last-run.json
changes, including when another process that shares the state directory wrote it.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

from . import infisical_settings

DEFAULT_MAX_ENTRIES = 256
DEFAULT_TTL_S = 600.0
MAX_ENTRIES_CAP = 100_000
MAX_TTL_S = 24 * 60 * 60
SIZE_KEY = "RECALL_SEARCH_CACHE_SIZE"
TTL_KEY = "RECALL_SEARCH_CACHE_TTL_SECONDS"
EPOCH_NAME = "search-cache-epoch.json"
EPOCH_ENV = "FLEET_RAG_SEARCH_CACHE_EPOCH"
STATE_DIR = pathlib.Path.home() / "apps" / "fleet-rag" / "state"
DEFAULT_EPOCH = STATE_DIR / EPOCH_NAME
DEFAULT_LAST_RUN = STATE_DIR / "last-run.json"

# expires_at, corpus token, result
_Slot = tuple[float, str, dict]


def normalize_query(query: str) -> str:
    """Collapse whitespace and casefold so identical questions share one entry."""
    return " ".join(query.split()).casefold()


def make_key(
    *,
    query: str,
    limit: int,
    category: str | None,
    app: str | None,
    source: str | None,
    seat: str | None,
    since_days: int | None,
    per_doc: int,
    prefer_lessons: bool,
    rerank: bool,
    include_retracted: bool,
    compact: bool,
    full: bool,
    collection: str,
    groups: bool,
    rerank_live: bool,
    embed_model: str,
) -> str:
    payload = {
        "q": query,
        "limit": limit,
        "category": category,
        "app": app,
        "source": source,
        "seat": seat,
        "since_days": since_days,
        "per_doc": per_doc,
        "prefer_lessons": prefer_lessons,
        "rerank": rerank,
        "include_retracted": include_retracted,
        "compact": compact,
        "full": full,
        "collection": collection,
        "groups": groups,
        "rerank_live": rerank_live,
        "embed_model": embed_model,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _setting(name: str) -> str | None:
    managed = infisical_settings.get(name)
    if managed is not None and str(managed).strip():
        return str(managed).strip()
    env = os.environ.get(name)
    if env is not None and env.strip():
        return env.strip()
    return None


def _parse_size(raw: str | None) -> int:
    if raw is None:
        return DEFAULT_MAX_ENTRIES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_ENTRIES
    if value < 0 or value > MAX_ENTRIES_CAP:
        return DEFAULT_MAX_ENTRIES
    return value


def _parse_ttl(raw: str | None) -> float:
    if raw is None:
        return DEFAULT_TTL_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TTL_S
    if value < 0 or value > MAX_TTL_S or value != value:
        return DEFAULT_TTL_S
    return value


def read_settings() -> tuple[int, float]:
    """(max entries, ttl seconds) from Infisical, else the environment, else the defaults."""
    return _parse_size(_setting(SIZE_KEY)), _parse_ttl(_setting(TTL_KEY))


def _file_token(path: pathlib.Path) -> str:
    """Fingerprint the marker bytes.  mtime and size can stay still across writes."""
    try:
        data = path.read_bytes()
    except OSError:
        return "absent"
    return hashlib.sha256(data).hexdigest()[:16]


def _write_epoch(path: pathlib.Path, report: dict) -> None:
    body = json.dumps(
        {
            "run_id": str(report.get("run_id") or ""),
            "finished_at": report.get("finished_at"),
            "ok": bool(report.get("ok")),
            "seq": uuid.uuid4().hex,
        },
        sort_keys=True,
    )
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(body + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


class SearchCache:
    """One process's search results.  Thread-safe.  Not shared across processes."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: OrderedDict[str, _Slot] = OrderedDict()
        self.hits = 0
        self.misses = 0
        self._generation = 0
        self._max_entries = DEFAULT_MAX_ENTRIES
        self._ttl_s = DEFAULT_TTL_S
        self._bounds_override: tuple[int, float] | None = None
        self.isolated = False
        self.marker_mode = "default"
        self.marker_path: pathlib.Path | None = None
        self._now: Callable[[], float] = time.monotonic

    def reset(self) -> None:
        """Test isolation: empty counts, ignore the home state directory and live settings."""
        with self._lock:
            self._data.clear()
            self.hits = 0
            self.misses = 0
            self._generation = 0
            self._max_entries = DEFAULT_MAX_ENTRIES
            self._ttl_s = DEFAULT_TTL_S
            self._bounds_override = None
            self.isolated = True
            self.marker_mode = "off"
            self.marker_path = None
            self._now = time.monotonic

    def configure(
        self,
        *,
        max_entries: int | None = None,
        ttl_s: float | None = None,
        now: Callable[[], float] | None = None,
        marker_path: pathlib.Path | str | None = None,
        isolated: bool | None = None,
    ) -> None:
        with self._lock:
            if isolated is not None:
                self.isolated = isolated
            if max_entries is not None or ttl_s is not None:
                size, ttl = self._bounds_locked()
                if max_entries is not None:
                    size = max_entries
                if ttl_s is not None:
                    ttl = ttl_s
                self._bounds_override = (size, ttl)
                self._data.clear()
            if now is not None:
                self._now = now
            if marker_path is not None:
                self.marker_mode = "file"
                self.marker_path = pathlib.Path(marker_path)
                self._data.clear()

    def bounds(self) -> tuple[int, float]:
        with self._lock:
            self._sync_bounds_locked()
            return self._max_entries, self._ttl_s

    def _bounds_locked(self) -> tuple[int, float]:
        if self._bounds_override is not None:
            return self._bounds_override
        if self.isolated:
            return DEFAULT_MAX_ENTRIES, DEFAULT_TTL_S
        return read_settings()

    def _sync_bounds_locked(self) -> None:
        size, ttl = self._bounds_locked()
        if size != self._max_entries or ttl != self._ttl_s:
            self._max_entries = size
            self._ttl_s = ttl
            self._data.clear()

    def _token_locked(self) -> str:
        generation = f"g{self._generation}"
        if self.marker_mode == "off":
            return generation
        if self.marker_mode == "file":
            path = self.marker_path or DEFAULT_EPOCH
            return generation + "|" + _file_token(path)
        return (
            generation + "|e:" + _file_token(DEFAULT_EPOCH) + "|l:" + _file_token(DEFAULT_LAST_RUN)
        )

    def lookup(self, key: str) -> tuple[dict | None, str]:
        """Return (copy, token) on a hit, or (None, token) on a miss.  Counts either way."""
        with self._lock:
            self._sync_bounds_locked()
            token = self._token_locked()
            now = self._now()
            slot = self._data.get(key)
            if slot is not None and slot[0] > now and slot[1] == token:
                self._data.move_to_end(key)
                self.hits += 1
                return copy.deepcopy(slot[2]), token
            if slot is not None:
                del self._data[key]
            self.misses += 1
            return None, token

    def store(self, key: str, value: dict, token: str) -> None:
        """Keep a copy when the corpus token is still the one lookup() observed."""
        with self._lock:
            self._sync_bounds_locked()
            if self._token_locked() != token:
                return
            if self._max_entries <= 0 or self._ttl_s <= 0:
                return
            now = self._now()
            self._data[key] = (now + self._ttl_s, token, copy.deepcopy(value))
            self._data.move_to_end(key)
            expired = [k for k, slot in self._data.items() if slot[0] <= now]
            for stale in expired:
                del self._data[stale]
            while len(self._data) > self._max_entries:
                self._data.popitem(last=False)

    def invalidate(self) -> None:
        """Drop every entry.  The generation moves so a result still in flight is not stored."""
        with self._lock:
            self._generation += 1
            self._data.clear()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            self._sync_bounds_locked()
            return {
                "hits": self.hits,
                "misses": self.misses,
                "entries": len(self._data),
                "max_entries": self._max_entries,
                "ttl_seconds": self._ttl_s,
            }


CACHE = SearchCache()


def reset() -> None:
    CACHE.reset()


def configure(**kwargs: Any) -> None:
    CACHE.configure(**kwargs)


def lookup(key: str) -> tuple[dict | None, str]:
    return CACHE.lookup(key)


def store(key: str, value: dict, token: str) -> None:
    CACHE.store(key, value, token)


def invalidate() -> None:
    CACHE.invalidate()


def stats() -> dict[str, Any]:
    return CACHE.stats()


def note_ingest_complete(report: dict, epoch_path: pathlib.Path | None = None) -> None:
    """Drop this process's entries and publish the run so other processes miss too.

    epoch_path is where this run's marker is written.  When it is omitted, the marker
    goes to FLEET_RAG_SEARCH_CACHE_EPOCH or the default state directory, and a missing
    parent is left alone so a unit test does not create ~/apps.  An isolated cache
    (the fake backend) skips that write unless the caller passes epoch_path.
    """
    invalidate()
    path = epoch_path
    if path is None:
        if CACHE.isolated:
            return
        override = os.environ.get(EPOCH_ENV, "").strip()
        path = pathlib.Path(override) if override else DEFAULT_EPOCH
        if not path.parent.is_dir():
            return
    _write_epoch(path, report)


def note_corpus_change(kind: str, doc_id: str = "") -> None:
    """Publish the same epoch after a contribution, correction, or retraction.

    Other processes that can see the state directory miss on their next search.
    A duplicate, an unchanged correction, and an already-retracted lesson do not
    call this.
    """
    label = kind if not doc_id else f"{kind}:{doc_id}"
    note_ingest_complete({"run_id": label, "finished_at": int(time.time() * 1000), "ok": True})
