"""Individual retrieval legs, fetched separately so rrf.rrf_fuse can recombine them.

Production fuses its three prefetches *inside one Qdrant Query-API call*
(core.Qdrant._prefetch + query_hybrid), so there is no way to add or remove a leg without
either editing core.py (out of scope: this must not touch production code or behavior) or
fetching each leg on its own and fusing in Python.  This module does the latter -- read-only
`POST /points/search` calls, nothing core.Qdrant.search_dense doesn't already send for the
production leg it mirrors.
"""
from __future__ import annotations

from .. import core
from ..core import LESSON_SCORE_THRESHOLD, lesson_filter


def dense_leg(q: core.Qdrant, vector: list[float], flt: dict | None, limit: int) -> list[dict]:
    """Leg 1: plain dense search, restricted only by the caller's filter."""
    return q.search_dense(vector, limit, flt)


def keyword_dense_leg(q: core.Qdrant, vector: list[float], terms: list[str], flt: dict | None,
                       limit: int) -> list[dict]:
    """Leg 2 (production's "keyword" leg): dense search restricted to points whose full-text
    index contains ANY of `terms` -- still ranked by cosine, not term frequency.  This is the
    leg the experiment either adds BM25 alongside or replaces outright.  Empty when there are
    no terms, exactly like core.Qdrant._prefetch."""
    if not terms:
        return []
    kw_filter: dict = {"should": [{"key": "text", "match": {"text": t}} for t in terms]}
    if flt:
        kw_filter = {"must": [flt, kw_filter]}
    return q.search_dense(vector, limit, kw_filter)


def lesson_leg(q: core.Qdrant, vector: list[float], flt: dict | None, limit: int) -> list[dict]:
    """Leg 3: dense search over agent-contribution lessons with the production score floor.
    core.Qdrant.search_dense has no score_threshold parameter, so this issues the same
    POST /points/search core.Qdrant itself would (via its own _call/_cpath helpers), with
    that one extra field -- still read-only, still the same request shape."""
    body = {"vector": vector, "limit": limit, "with_payload": True,
            "filter": lesson_filter(flt), "score_threshold": LESSON_SCORE_THRESHOLD}
    return q._call(q._cpath("/points/search"), body)["result"]  # noqa: SLF001
