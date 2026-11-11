"""Search-callables built on the LIVE Hetzner Qdrant/TEI path, but routed through
live_cache.LiveCache so each (query, leg) and (query, candidate text) touches the network at
most once for the whole run regardless of how many variants or rerank on/off passes reuse it.
Same eval.py contract, same RRF fusion / doc-id grouping steps as variants.make_variant and
local_variants.make_local_variant -- see whichever of those modules' docstrings for that shared
shape; this module only changes where the dense/keyword/lesson legs and the rerank scores come
from.

Every leg is fetched ONCE per query at MAX_PREFETCH (a limit big enough to cover every
variant's actual prefetch need at k=5, see run_live_ab.py) and then sliced locally to whatever
smaller window a given rerank on/off pass actually needs -- a top-N truncation of an
already-sorted list is exactly the same set the live box would have returned for that smaller
N, so this is not an approximation.
"""
from __future__ import annotations

from typing import Callable

from .. import core, recall_api
from ..core import query_terms
from . import legs as leg_fns
from .bm25_index import Bm25Corpus
from .live_cache import LiveCache
from .rrf import flatten_groups, group_by_doc, rrf_fuse

GROUP_DEPTH = recall_api.GROUP_DEPTH
LEG_NAMES = ("dense", "keyword", "lesson", "bm25")


def make_live_variant(cfg: dict, q: core.Qdrant, cache: LiveCache, *, legs: tuple[str, ...], name: str,
                      bm25: Bm25Corpus | None = None, max_prefetch: int = 100,
                      rerank_fn: Callable[[str, list[str]], list[float] | None] | None = None
                      ) -> Callable[..., dict]:
    bad = set(legs) - set(LEG_NAMES)
    if bad:
        raise ValueError(f"unknown leg(s): {bad}")
    if "bm25" in legs and bm25 is None:
        raise ValueError("legs includes 'bm25' but no Bm25Corpus was given")

    def search(query: str, limit: int = 5, prefer_lessons: bool = True, rerank: bool = True,
              per_doc: int = 1) -> dict:
        flt = recall_api.build_filter()
        terms = query_terms(query)
        need_vector = any(l in legs for l in ("dense", "keyword", "lesson"))
        vector = cache.cached_embed(cfg, query) if need_vector else None
        if need_vector and vector is None:
            return {"hits": [], "mode": "embed-failed"}

        will_rerank = bool(rerank) and rerank_fn is not None
        n_cand = recall_api.candidate_count(limit) if will_rerank else limit
        depth = max(GROUP_DEPTH, per_doc)
        pl = n_cand * depth
        if pl > max_prefetch:
            raise ValueError(f"pl={pl} exceeds max_prefetch={max_prefetch}; raise max_prefetch")

        named: dict[str, list[dict]] = {}
        if "dense" in legs:
            hits = cache.cached_leg(f"dense::{query}", lambda: leg_fns.dense_leg(q, vector, flt, max_prefetch))
            named["dense"] = hits[:pl]
        if "keyword" in legs and terms:
            tkey = ",".join(terms)
            hits = cache.cached_leg(f"keyword::{tkey}::{query}",
                                    lambda: leg_fns.keyword_dense_leg(q, vector, terms, flt, max_prefetch))
            named["keyword"] = hits[:pl]
        if "lesson" in legs and prefer_lessons:
            hits = cache.cached_leg(f"lesson::{query}", lambda: leg_fns.lesson_leg(q, vector, flt, max_prefetch))
            named["lesson"] = hits[:pl]
        if "bm25" in legs:
            named["bm25"] = bm25.search(query, pl)

        fused = rrf_fuse(named)
        groups = group_by_doc(fused, depth=depth)[:n_cand]
        flat = flatten_groups(groups, per_doc=per_doc)

        hits = [recall_api._hit(pt) for pt in flat]  # noqa: SLF001 - production's own hit shape
        mode = "+".join(legs) + "-live"
        if will_rerank and hits:
            texts = [h["text"] for h in hits]
            scores = rerank_fn(query, texts)
            if scores is not None and len(scores) == len(hits):
                for h, sc in zip(hits, scores):
                    h["fused_score"], h["rerank_score"] = h["score"], round(float(sc), 4)
                hits.sort(key=lambda h: -h["rerank_score"])
                for h in hits:
                    h["score"] = h["rerank_score"]
                mode += "+rerank"
            # scores is None -> at least one text's rerank permanently failed; fall back to the
            # fused order untouched, exactly like recall_api._apply_rerank's own fallback.
        return {"hits": hits[:limit], "mode": mode}

    search.__name__ = name  # type: ignore[attr-defined]
    return search
