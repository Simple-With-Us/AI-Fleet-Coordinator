"""Part A (BM25 fused as a 4th leg / BM25 replacing the keyword leg) and Part C (best
combination) against the LIVE Hetzner Qdrant/TEI stack, with every network call routed through
live_cache.LiveCache so a query is only ever sent to the box once for the life of the cache
directory. See the results doc's Operational note for why this exists: the box has been slow
(a `recall stats` call took over two minutes but did succeed) and SSH/Tailscale has been
unreliable, so this script uses short-ish per-call timeouts with a few bounded retries and
treats a permanently-failing query as a recorded miss, never an aborted run.

Zero writes to Qdrant (every leg fetch is a /points/search read), zero writes to the corpus,
zero LLM calls (rerank is TEI's cross-encoder /rerank endpoint, not an LLM).

    python3 -m fleet_rag.experiments.run_live_ab [--k 5] [--json out.json]

Part B (bge-reranker-v2-m3) is opportunistic: if a complete local copy is on disk (offline
check only -- this script never triggers or waits on that download itself; see
docs/reviews/2026-09-14-fleet-rag-bm25-reranker-ab.md's Operational note for that separate,
bounded attempt), it is loaded and compared against the live ms-marco rerank on the same
(cached) candidate lists, and Part C uses whichever reranker wins. Otherwise Part B is marked
blocked and Part C uses the live ms-marco rerank (production's actual reranker).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

# Tune the shared HTTP plumbing's per-call budget. Reassigning core.DEFAULT_TIMEOUT / core.RETRIES
# does NOT work -- every caller (core.embed, Qdrant._call, core.rerank) already bound those names
# as parameter DEFAULTS at core.py's own import time (a plain Python late-binding gotcha: default
# argument values are evaluated once, when the `def` executes, not read again per call), so
# reassigning the module attribute afterward is silently a no-op for anyone relying on the
# default. What every caller DOES look up dynamically, on every call, is the plain name
# `http_json` in core.py's module namespace -- so replacing that name is what actually takes
# effect everywhere. 30s per attempt, 2 retries (3 attempts total) matches "generous per-call
# timeouts (30-60s) and bounded retries (3)"; a fully-failing call now costs at most about 95s
# (3 x 30s + ~5s of backoff) instead of the ~615s the unpatched defaults (120s x 5 attempts) cost
# the run this replaces (see the results doc's Operational note for that measured cost).
from .. import core

LIVE_TIMEOUT = 30
LIVE_RETRIES = 2
_real_http_json = core.http_json


def _bounded_http_json(url, body=None, headers=None, method=None, timeout=LIVE_TIMEOUT, retries=LIVE_RETRIES):
    return _real_http_json(url, body, headers, method, timeout=LIVE_TIMEOUT, retries=LIVE_RETRIES)


core.http_json = _bounded_http_json

from .. import eval as ev  # noqa: E402
from . import live_variants as LVV  # noqa: E402
from .bm25_index import Bm25Corpus  # noqa: E402
from .corpus_cache import load_cache  # noqa: E402
from .live_cache import LiveCache  # noqa: E402

MAX_PREFETCH = 100  # covers n_cand(=candidate_count(5)=20) * depth(=GROUP_DEPTH=5) = 100
LIVE_ANCHOR = {"recall_at_1": 0.72, "recall_at_k": 0.93, "mrr": 0.81}
BM25_ALONE = {"n": 75, "recall_at_1": 0.6267, "recall_at_k": 0.8533, "mrr": 0.7171}  # already measured offline


def _summarize(res: dict) -> dict:
    return {"n": res["n"], "recall_at_1": round(res["recall_at_1"], 4),
            "recall_at_k": round(res["recall_at_k"], 4), "mrr": round(res["mrr"], 4),
            "modes": res["modes"]}


def _rank_diffs(rows: list[dict], base: list, other: list) -> list[dict]:
    out = []
    for row, b, o in zip(rows, base, other):
        if b != o:
            out.append({"query": row["query"], "note": row.get("note", ""), "base_rank": b, "other_rank": o})
    return out


def _try_load_local_bge_reranker():
    """Offline-only check: is a COMPLETE local BAAI/bge-reranker-v2-m3 on disk right now? Never
    triggers a download (HF_HUB_OFFLINE=1 for the duration of this one call only)."""
    old = os.environ.get("HF_HUB_OFFLINE")
    os.environ["HF_HUB_OFFLINE"] = "1"
    try:
        from . import reranker as RR
        return RR.load(RR.BGE_MODEL)
    except Exception as e:  # noqa: BLE001 - any failure means "not available", not a crash
        print(f"  local bge-reranker-v2-m3 not available: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    finally:
        if old is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = old


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--json", default=None)
    a = ap.parse_args(argv)
    k = a.k

    print("== loading config + corpus cache ==", file=sys.stderr)
    cfg = core.load_config(need_write=False)
    rows = load_cache()
    print(f"corpus: {len(rows)} points cached", file=sys.stderr)
    t0 = time.perf_counter()
    bm25 = Bm25Corpus(rows)
    print(f"BM25 index built in {time.perf_counter() - t0:.1f}s", file=sys.stderr)
    golden_rows = ev.load_golden()

    q = core.Qdrant(cfg)
    cache = LiveCache("run_live_ab")
    print(f"live cache dir: {cache.dir}", file=sys.stderr)

    ms_marco_rerank_fn = lambda query, texts: cache.cached_rerank(cfg, query, texts)  # noqa: E731

    out: dict = {"k": k, "corpus_points": len(rows), "live_anchor": LIVE_ANCHOR, "bm25_alone": BM25_ALONE}

    leg_sets = {
        "prod-3leg (dense+keyword+lesson)": ("dense", "keyword", "lesson"),
        "bm25-added (dense+keyword+lesson+bm25)": ("dense", "keyword", "lesson", "bm25"),
        "bm25-replaces-keyword (dense+bm25+lesson)": ("dense", "bm25", "lesson"),
    }
    part_a: dict[str, dict] = {}
    for name, legs in leg_sets.items():
        fn = LVV.make_live_variant(cfg, q, cache, legs=legs, name=name, bm25=bm25,
                                   max_prefetch=MAX_PREFETCH, rerank_fn=ms_marco_rerank_fn)
        print(f"  -- {name}: no-rerank -- (failures so far: {cache.failure_count()})", file=sys.stderr)
        no_rr = ev.run_eval(k, search=fn, search_kwargs={"rerank": False})
        print(f"  -- {name}: +live-ms-marco-rerank -- (failures so far: {cache.failure_count()})", file=sys.stderr)
        with_rr = ev.run_eval(k, search=fn, search_kwargs={"rerank": True})
        part_a[name] = {"no_rerank": _summarize(no_rr), "with_rerank": _summarize(with_rr),
                        "_ranks_no_rerank": no_rr["ranks"], "_ranks_with_rerank": with_rr["ranks"]}
        out["part_a_retrieval"] = {n: {kk: vv for kk, vv in d.items() if not kk.startswith("_")}
                                   for n, d in part_a.items()}
        out["live_call_failures"] = cache.failure_count()
        _dump(out, a.json)
        print(f"     R@1={part_a[name]['with_rerank']['recall_at_1']:.2f} "
             f"R@{k}={part_a[name]['with_rerank']['recall_at_k']:.2f} "
             f"MRR={part_a[name]['with_rerank']['mrr']:.2f}", file=sys.stderr)

    base_name = "prod-3leg (dense+keyword+lesson)"
    out["part_a_flips"] = {}
    for name in leg_sets:
        if name == base_name:
            continue
        out["part_a_flips"][name] = {
            "no_rerank": _rank_diffs(golden_rows, part_a[base_name]["_ranks_no_rerank"], part_a[name]["_ranks_no_rerank"]),
            "with_rerank": _rank_diffs(golden_rows, part_a[base_name]["_ranks_with_rerank"], part_a[name]["_ranks_with_rerank"]),
        }
    _dump(out, a.json)

    # ---- opportunistic Part B / C: is a complete local bge-reranker-v2-m3 available? ----
    print("== checking for a complete local bge-reranker-v2-m3 (offline check only) ==", file=sys.stderr)
    t_bge = _try_load_local_bge_reranker()
    best_legs_name = max(part_a, key=lambda n: part_a[n]["with_rerank"]["recall_at_k"])
    best_legs = leg_sets[best_legs_name]
    print(f"  best Part-A retrieval variant: {best_legs_name}", file=sys.stderr)

    if t_bge is not None:
        print("== Part B: live ms-marco vs local bge-reranker-v2-m3, same (cached) candidates ==", file=sys.stderr)
        fn_ms = LVV.make_live_variant(cfg, q, cache, legs=best_legs, name="ms-marco-live", bm25=bm25,
                                      max_prefetch=MAX_PREFETCH, rerank_fn=ms_marco_rerank_fn)
        fn_bge = LVV.make_live_variant(cfg, q, cache, legs=best_legs, name="bge-reranker-v2-m3", bm25=bm25,
                                       max_prefetch=MAX_PREFETCH, rerank_fn=t_bge.as_rerank_fn())
        t0 = time.perf_counter()
        res_ms = ev.run_eval(k, search=fn_ms)
        wall_ms = time.perf_counter() - t0
        t0 = time.perf_counter()
        res_bge = ev.run_eval(k, search=fn_bge)
        wall_bge = time.perf_counter() - t0
        from . import reranker as RR
        out["part_b_reranker"] = {
            "candidate_source": best_legs_name,
            "ms_marco": {**_summarize(res_ms), "params": RR.MODEL_PARAMS[RR.MS_MARCO_MODEL],
                        "wall_seconds": round(wall_ms, 2), "note": "served live via TEI, cached per (query,text)"},
            "bge_reranker_v2_m3": {**_summarize(res_bge), "params": RR.MODEL_PARAMS[RR.BGE_MODEL],
                                   "avg_predict_seconds": round(t_bge.avg_latency, 4),
                                   "n_predict_calls": t_bge.total_calls, "wall_seconds": round(wall_bge, 2),
                                   "note": "run locally on this Mac (not TEI/not the Hetzner box)"},
            "rank_flips": _rank_diffs(golden_rows, res_ms["ranks"], res_bge["ranks"]),
        }
        best_reranker = "bge_reranker_v2_m3" if res_bge["recall_at_k"] >= res_ms["recall_at_k"] else "ms_marco"
        best_rerank_fn = t_bge.as_rerank_fn() if best_reranker == "bge_reranker_v2_m3" else ms_marco_rerank_fn
    else:
        out["part_b_reranker"] = {"status": "blocked: Hugging Face CDN unreachable from this Mac "
                                             "(see Operational note) -- bge-reranker-v2-m3 weights "
                                             "never completed downloading"}
        best_reranker, best_rerank_fn = "ms_marco (live)", ms_marco_rerank_fn
    _dump(out, a.json)

    print(f"== Part C: {best_legs_name} + {best_reranker} ==", file=sys.stderr)
    fn_combo = LVV.make_live_variant(cfg, q, cache, legs=best_legs, name="combo", bm25=bm25,
                                     max_prefetch=MAX_PREFETCH, rerank_fn=best_rerank_fn)
    res_combo = ev.run_eval(k, search=fn_combo)
    out["part_c_combination"] = {"legs": best_legs_name, "reranker": best_reranker, **_summarize(res_combo)}
    out["live_call_failures"] = cache.failure_count()
    print(f"  combo: R@1={res_combo['recall_at_1']:.2f} R@{k}={res_combo['recall_at_k']:.2f} "
         f"MRR={res_combo['mrr']:.2f}  (total live-call failures this run: {cache.failure_count()})",
         file=sys.stderr)
    _dump(out, a.json)
    return 0


def _dump(out: dict, path: str | None) -> None:
    text = json.dumps(out, indent=2)
    if path:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {path}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
