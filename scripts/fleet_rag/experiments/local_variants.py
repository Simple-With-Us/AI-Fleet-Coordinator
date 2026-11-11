"""Search-callables built on the LOCAL dense fallback (local_dense.LocalIndex) instead of the
live core.Qdrant + TEI path -- same eval.py contract as variants.make_variant, same RRF fusion /
doc-id grouping / rerank steps, so the two are directly comparable retrieval-variant-for-
retrieval-variant. See local_dense.py's module docstring for why these numbers are labelled
"local re-embedding" rather than treated as a reproduction of the live numbers.
"""
from __future__ import annotations

from typing import Callable

import numpy as np

from .. import recall_api
from ..core import query_terms
from .bm25_index import Bm25Corpus
from .local_dense import LocalEmbedder, LocalIndex
from .rrf import flatten_groups, group_by_doc, rrf_fuse

GROUP_DEPTH = recall_api.GROUP_DEPTH
LEG_NAMES = ("dense", "keyword", "lesson", "bm25")


def make_local_variant(idx: LocalIndex, embedder: LocalEmbedder, *, legs: tuple[str, ...], name: str,
                       bm25: Bm25Corpus | None = None,
                       rerank_fn: Callable[[str, list[str]], list[float]] | None = None,
                       qcache: dict[str, np.ndarray] | None = None
                       ) -> Callable[..., dict]:
    """qcache: an optional shared dict, reused across every make_local_variant() call in one run,
    so the same golden query is only ever encoded once by the local embedder no matter how many
    dense-needing variants or rerank on/off passes hit it (the 75 golden queries recur many times
    across Parts A/B/C)."""
    bad = set(legs) - set(LEG_NAMES)
    if bad:
        raise ValueError(f"unknown leg(s): {bad}")
    if "bm25" in legs and bm25 is None:
        raise ValueError("legs includes 'bm25' but no Bm25Corpus was given")
    cache = qcache if qcache is not None else {}

    def _embed_cached(query: str) -> np.ndarray:
        if query not in cache:
            cache[query] = embedder.encode([query])[0]
        return cache[query]

    def search(query: str, limit: int = 5, prefer_lessons: bool = True, rerank: bool = True,
              per_doc: int = 1) -> dict:
        terms = query_terms(query)
        need_vector = any(l in legs for l in ("dense", "keyword", "lesson"))
        vector: np.ndarray | None = _embed_cached(query) if need_vector else None
        will_rerank = bool(rerank) and rerank_fn is not None
        n_cand = recall_api.candidate_count(limit) if will_rerank else limit
        depth = max(GROUP_DEPTH, per_doc)
        pl = n_cand * depth

        named: dict[str, list[dict]] = {}
        if "dense" in legs:
            named["dense"] = idx.dense_leg(vector, pl)
        if "keyword" in legs and terms:
            named["keyword"] = idx.keyword_leg(vector, terms, pl)
        if "lesson" in legs and prefer_lessons:
            named["lesson"] = idx.lesson_leg(vector, pl)
        if "bm25" in legs:
            named["bm25"] = bm25.search(query, pl)

        fused = rrf_fuse(named)
        groups = group_by_doc(fused, depth=depth)[:n_cand]
        flat = flatten_groups(groups, per_doc=per_doc)

        hits = [recall_api._hit(pt) for pt in flat]  # noqa: SLF001 - production's own hit shape
        mode = "+".join(legs) + "-local"
        if will_rerank and hits:
            texts = [h["text"] for h in hits]
            scores = rerank_fn(query, texts)
            if len(scores) == len(hits):
                for h, sc in zip(hits, scores):
                    h["fused_score"], h["rerank_score"] = h["score"], round(float(sc), 4)
                hits.sort(key=lambda h: -h["rerank_score"])
                for h in hits:
                    h["score"] = h["rerank_score"]
                mode += "+rerank"
        return {"hits": hits[:limit], "mode": mode}

    search.__name__ = name  # type: ignore[attr-defined]
    return search
