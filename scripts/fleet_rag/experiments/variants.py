"""Search-callables for the ablation, each matching eval.py's `search(query, limit, **kwargs)`
contract exactly (see fleet_rag/tests/test_eval.py: fake_search / _search), so every variant
is scored by the *unmodified* eval.run_eval / run_compare harness -- no changes to eval.py.

Pipeline, same shape as recall_api.recall_search: embed the query (the real TEI call
production makes) -> fetch this variant's legs -> RRF-fuse them (rrf.rrf_fuse) -> group by
doc_id (rrf.group_by_doc) -> flatten to per-doc hits (rrf.flatten_groups) -> optional rerank
-> convert to recall_api._hit shape -> first `limit` hits.
"""
from __future__ import annotations

from typing import Callable

from .. import core, recall_api
from ..core import query_terms
from . import legs as leg_fns
from .bm25_index import Bm25Corpus
from .rrf import flatten_groups, group_by_doc, rrf_fuse

GROUP_DEPTH = recall_api.GROUP_DEPTH
LEG_NAMES = ("dense", "keyword", "lesson", "bm25")


def make_variant(cfg: dict, q: core.Qdrant, *, legs: tuple[str, ...], name: str,
                 bm25: Bm25Corpus | None = None,
                 rerank_fn: Callable[[str, list[str]], list[float]] | None = None
                 ) -> Callable[..., dict]:
    """Build one search-callable.

    legs: which named legs to fetch and fuse this run -- any of LEG_NAMES.  Passing a
    "bm25" leg without a `bm25` index, or omitting "keyword"/"lesson", is how the ablation
    expresses "BM25 added" (dense, keyword, lesson, bm25) vs "BM25 replacing the keyword
    leg" (dense, bm25, lesson) vs plain retrieval-only baseline (dense, keyword, lesson).

    rerank_fn(query, texts) -> scores reorders the fused/grouped hits, exactly like
    recall_api._apply_rerank; pass None to leave the fused order alone regardless of the
    caller's rerank=True/False (used for the pure-retrieval variants in Part A).
    """
    bad = set(legs) - set(LEG_NAMES)
    if bad:
        raise ValueError(f"unknown leg(s): {bad}")
    if "bm25" in legs and bm25 is None:
        raise ValueError("legs includes 'bm25' but no Bm25Corpus was given")

    def search(query: str, limit: int = 5, prefer_lessons: bool = True, rerank: bool = True,
              per_doc: int = 1) -> dict:
        flt = recall_api.build_filter()
        # A pure "bm25" variant (legs == ("bm25",)) touches neither the dense, keyword nor
        # lesson leg below, so skip the live TEI embed call entirely -- it would otherwise be a
        # wasted network round-trip on every golden query, and a needless point of failure when
        # the box is unreachable (see the results doc's Operational note).
        need_vector = any(l in legs for l in ("dense", "keyword", "lesson"))
        vector = core.embed(cfg, [query])[0] if need_vector else None
        terms = query_terms(query)
        will_rerank = bool(rerank) and rerank_fn is not None
        n_cand = recall_api.candidate_count(limit) if will_rerank else limit
        depth = max(GROUP_DEPTH, per_doc)
        pl = n_cand * depth

        named: dict[str, list[dict]] = {}
        if "dense" in legs:
            named["dense"] = leg_fns.dense_leg(q, vector, flt, pl)
        if "keyword" in legs and terms:
            named["keyword"] = leg_fns.keyword_dense_leg(q, vector, terms, flt, pl)
        if "lesson" in legs and prefer_lessons:
            named["lesson"] = leg_fns.lesson_leg(q, vector, flt, pl)
        if "bm25" in legs:
            named["bm25"] = bm25.search(query, pl)

        fused = rrf_fuse(named)
        groups = group_by_doc(fused, depth=depth)[:n_cand]
        flat = flatten_groups(groups, per_doc=per_doc)

        hits = [recall_api._hit(pt) for pt in flat]  # noqa: SLF001 - production's own hit shape
        mode = "+".join(legs)
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


def production_rerank_fn(cfg: dict) -> Callable[[str, list[str]], list[float]]:
    """The REAL, currently-deployed TEI reranker (ms-marco-MiniLM-L-6-v2) -- used for Part A
    so retrieval-variant comparisons measure the retrieval change alone, against production's
    actual rerank step, not a local reimplementation of it."""
    def fn(query: str, texts: list[str]) -> list[float]:
        return core.rerank(cfg, query, texts)
    return fn
