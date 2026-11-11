"""Reciprocal rank fusion and doc-id grouping for the ablation, mirroring production shape.

Production fuses its prefetches *inside Qdrant* (POST /points/query, {"fusion": "rrf"}) and
then groups the fused, chunk-level list by doc_id in-process (recall_api.fused_groups /
group_hits).  A locally computed BM25 ranked list cannot be handed to Qdrant's server-side
fusion, so this module reimplements the same two steps (RRF over named ranked lists, then
first-seen-order doc_id grouping) in Python so every variant -- the production legs, BM25
added as a leg, BM25 replacing a leg -- goes through an identical, controllable fusion and
grouping step before being scored by the *unmodified* eval.py harness.
"""
from __future__ import annotations

from typing import Any

RRF_K = 60  # standard Cormack/Clarke/Buettcher constant; also what recall_api implicitly
            # inherits from Qdrant's native "rrf" fusion (Qdrant does not expose its constant
            # as configurable, so this is the same well-known default, not a guess pulled to
            # flatter one variant -- see the results doc's fusion-parity check).


def rrf_fuse(named_lists: dict[str, list[dict]], k: int = RRF_K,
             weights: dict[str, float] | None = None) -> list[dict]:
    """Fuse several ranked lists of Qdrant-shaped points ({"id", "payload", "score"}) by id.

    Returns points in fused order, each carrying `rrf_score` and `rrf_legs` (which named
    lists it appeared in, for diagnostics).  First-seen payload wins when the same id
    appears in more than one list (they're the same point, so the payload is identical).
    """
    scores: dict[Any, float] = {}
    legs: dict[Any, list[str]] = {}
    payload_of: dict[Any, dict] = {}
    for name, lst in (named_lists or {}).items():
        w = (weights or {}).get(name, 1.0)
        for rank, pt in enumerate(lst, start=1):
            pid = pt["id"]
            scores[pid] = scores.get(pid, 0.0) + w / (k + rank)
            legs.setdefault(pid, []).append(name)
            payload_of.setdefault(pid, pt)
    ordered = sorted(scores, key=lambda i: -scores[i])
    out = []
    for pid in ordered:
        pt = dict(payload_of[pid])
        pt["rrf_score"] = scores[pid]
        pt["rrf_legs"] = legs[pid]
        out.append(pt)
    return out


def group_by_doc(points: list[dict], depth: int = 5) -> list[dict]:
    """First-seen-order doc_id grouping, at most `depth` chunks kept per doc.

    This is recall_api.group_hits with the same semantics (fused order preserved, ties
    broken by first appearance), reimplemented here so the experiment package has no
    import-time dependency on recall_api internals beyond build_filter.
    """
    groups: dict[Any, list[dict]] = {}
    order: list[Any] = []
    for pt in points:
        key = (pt.get("payload") or {}).get("doc_id")
        if key not in groups:
            groups[key] = []
            order.append(key)
        bucket = groups[key]
        if len(bucket) < max(1, depth):
            bucket.append(pt)
    return [{"id": k, "hits": groups[k]} for k in order]


def flatten_groups(groups: list[dict], per_doc: int = 1) -> list[dict]:
    """Best `per_doc` chunks of each group -> a flat hit list, in group (fused) order."""
    out: list[dict] = []
    for g in groups:
        for raw in (g.get("hits") or [])[:per_doc]:
            out.append(raw)
    return out
