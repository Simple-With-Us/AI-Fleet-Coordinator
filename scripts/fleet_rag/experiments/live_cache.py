"""Disk-cached wrappers around the live Qdrant/TEI calls (embed, the three prefetch legs, and
rerank), so a run against the live Hetzner box touches the network AT MOST ONCE per
(query, leg-kind) or (query, candidate text) no matter how many retrieval variants, rerank
on/off passes, or re-runs of this script reuse the same query. The box has been slow and
sometimes unreachable this session (see the results doc's Operational note) -- this cache is
what makes "75 queries is small" actually true: pay the network cost once, reuse it everywhere.

Every call already goes through core.http_json's own bounded retries (see run_live_ab.py, which
tunes core.DEFAULT_TIMEOUT / core.RETRIES down to a per-call budget before importing this
module) -- this module's job is caching + recording permanent failures, not retrying itself.
A query/leg/text that still fails after core's own retries is recorded in `failures` and the
caller gets an explicit empty/None result back (never an exception), so one bad query never
aborts a 75-query run.

Cache files live under experiments/.cache/live/ (gitignored, same directory as the corpus
cache) and are flushed to disk after every new entry, so a killed run loses at most the one
in-flight call.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import time
from typing import Callable

from .. import core

CACHE_DIR = pathlib.Path(__file__).with_name(".cache") / "live"


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class LiveCache:
    def __init__(self, name: str = "default"):
        self.dir = CACHE_DIR / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.embed = self._load("embed.json")
        self.legs = self._load("legs.json")
        self.rerank = self._load("rerank.json")
        self.failures: dict[str, list[dict]] = self._load("failures.json")

    def _path(self, fname: str) -> pathlib.Path:
        return self.dir / fname

    def _load(self, fname: str) -> dict:
        p = self._path(fname)
        if p.exists():
            try:
                return json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save(self, fname: str, data: dict) -> None:
        p = self._path(fname)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(p)

    def _record_failure(self, kind: str, key: str, error: str) -> None:
        self.failures.setdefault(kind, []).append({"key": key, "error": error, "at": time.time()})
        self._save("failures.json", self.failures)

    # -- embed --------------------------------------------------------------------
    def cached_embed(self, cfg: dict, query: str) -> list[float] | None:
        key = _h(query)
        if key in self.embed:
            return self.embed[key]
        try:
            vec = core.embed(cfg, [query])[0]
        except core.FleetRagError as e:
            self._record_failure("embed", query, str(e))
            return None
        self.embed[key] = vec
        self._save("embed.json", self.embed)
        return vec

    # -- legs (dense / keyword / lesson) --------------------------------------------
    def cached_leg(self, cache_key: str, fetch: Callable[[], list[dict]]) -> list[dict]:
        key = _h(cache_key)
        if key in self.legs:
            return self.legs[key]
        try:
            hits = fetch()
        except core.FleetRagError as e:
            self._record_failure("leg", cache_key, str(e))
            return []
        self.legs[key] = hits
        self._save("legs.json", self.legs)
        return hits

    # -- rerank ---------------------------------------------------------------------
    def cached_rerank(self, cfg: dict, query: str, texts: list[str]) -> list[float] | None:
        """Per-(query,text) cached cross-encoder score. A cross-encoder scores each pair
        independently (batching is only a network/compute optimisation), so caching at the
        individual-text level is exact -- unlike caching the whole candidate set, it is reused
        correctly even when a later variant's fused order presents a different subset or order
        of the same texts. Returns None (never partial) when any text's score could not be
        obtained, matching recall_api._apply_rerank's "fused order untouched" contract."""
        if not texts:
            return []
        qh = _h(query)
        scores: list[float | None] = [None] * len(texts)
        need_idx: list[int] = []
        for i, t in enumerate(texts):
            k = f"{qh}:{_h(t)}"
            if k in self.rerank:
                scores[i] = self.rerank[k]
            else:
                need_idx.append(i)
        if need_idx:
            try:
                got = core.rerank(cfg, query, [texts[i] for i in need_idx])
            except core.FleetRagError as e:
                self._record_failure("rerank", query, str(e))
            else:
                if len(got) == len(need_idx):
                    for i, sc in zip(need_idx, got):
                        self.rerank[f"{qh}:{_h(texts[i])}"] = float(sc)
                        scores[i] = float(sc)
                    self._save("rerank.json", self.rerank)
        if any(s is None for s in scores):
            return None
        return scores  # type: ignore[return-value]

    def failure_count(self) -> int:
        return sum(len(v) for v in self.failures.values())
