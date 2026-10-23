"""Full A/B (Parts A/B/C) run against the LOCAL bge-m3 dense fallback, for when the live TEI /
Qdrant path on the shared Hetzner box is unreachable (see the results doc's Operational note --
this session confirmed the outage via core.embedder_healthy()==False, core.Qdrant.healthz()==
False, a core.embed() call exceeding 90s, and the ssh hop to the Coolify host timing out during
the banner exchange,
before falling back to this path). Zero writes, zero LLM calls, and the only network call this
script itself makes is Infisical credential fetch (to build `cfg`, unused beyond that) -- no
live Qdrant or TEI call is ever attempted here.

Mirrors run_ab.py's Parts A/B/C exactly (same eval.py harness, same RRF fusion / doc-id grouping
/ rerank steps -- see local_variants.make_local_variant), except:
  - retrieval legs come from local_dense.LocalIndex (brute-force cosine over locally re-embedded
    BAAI/bge-m3 vectors) instead of core.Qdrant + live TEI embed;
  - Part A's "production rerank" step uses the local cross-encoder/ms-marco-MiniLM-L-6-v2 (same
    weights production's TEI deployment serves, run locally instead of over the network) as a
    stand-in for the live TEI /rerank call, since that endpoint is on the same unreachable box;
  - Part A gets a 4th leg-set, "bm25-alone" (bm25 only, no dense leg at all -- listed in the
    original brief's Variant A but missing from run_ab.py's leg_sets), included here because it
    needs no dense leg at all and so is unaffected by the local-vs-live question.
  - there is no "re-confirm the live production baseline" step (impossible while the box is
    down); the anchor is the already-captured-and-confirmed-twice 0.72 / 0.93 / 0.81 R@1/R@5/MRR
    quoted in the task brief and results doc, and this script's own "local dense 3-leg" number is
    reported next to it so the gap is visible.

    python3 -m fleet_rag.experiments.run_local_ab [--k 5] [--json out.json] [--rebuild-cache]
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from .. import core, eval as ev
from . import local_variants as LV
from . import variants as V
from .bm25_index import Bm25Corpus
from .corpus_cache import load_cache
from .local_dense import LocalEmbedder, LocalIndex, embed_corpus

LIVE_ANCHOR = {"recall_at_1": 0.72, "recall_at_k": 0.93, "mrr": 0.81}


def _summarize(res: dict) -> dict:
    return {"n": res["n"], "recall_at_1": round(res["recall_at_1"], 4),
            "recall_at_k": round(res["recall_at_k"], 4), "mrr": round(res["mrr"], 4),
            "modes": res["modes"]}


def _rank_diffs(rows: list[dict], base: list[int | None], other: list[int | None]) -> list[dict]:
    out = []
    for row, b, o in zip(rows, base, other):
        if b != o:
            out.append({"query": row["query"], "note": row.get("note", ""), "base_rank": b, "other_rank": o})
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--json", default=None)
    ap.add_argument("--rebuild-cache", action="store_true")
    ap.add_argument("--rebuild-embeddings", action="store_true", help="ignore the cached local corpus embeddings")
    a = ap.parse_args(argv)
    k = a.k

    print("== loading config (Infisical only -- no live Qdrant/TEI call) + corpus cache ==", file=sys.stderr)
    cfg = core.load_config(need_write=False)
    rows = load_cache(force_rebuild=a.rebuild_cache)
    print(f"corpus: {len(rows)} points cached", file=sys.stderr)
    t0 = time.perf_counter()
    bm25 = Bm25Corpus(rows)
    print(f"BM25 index built in {time.perf_counter() - t0:.1f}s", file=sys.stderr)
    golden_rows = ev.load_golden()

    print("== loading local BAAI/bge-m3 embedder ==", file=sys.stderr)
    embedder = LocalEmbedder()
    print(f"  device={embedder.device}  load={embedder.load_seconds:.1f}s", file=sys.stderr)
    t0 = time.perf_counter()
    corpus_emb = embed_corpus(embedder, rows, force=a.rebuild_embeddings)
    print(f"corpus embeddings ready in {time.perf_counter() - t0:.1f}s  shape={corpus_emb.shape}", file=sys.stderr)
    idx = LocalIndex(rows, corpus_emb)
    qcache: dict = {}

    out: dict = {"k": k, "corpus_points": len(rows), "live_anchor_R1_Rk_MRR": LIVE_ANCHOR,
                "embedder_device": embedder.device}

    # ---- local ms-marco stand-in for the live TEI rerank (same weights, run locally) ----
    print("== loading local reranker stand-in for production TEI rerank ==", file=sys.stderr)
    from . import reranker as RR
    t_prod_rerank = RR.load(RR.MS_MARCO_MODEL)
    prod_rerank_local = t_prod_rerank.as_rerank_fn()

    # ---- Part A: retrieval-leg ablation, local dense, both without and with the rerank stand-in ----
    print("== Part A: retrieval variants (local dense; BM25 add / replace / alone) ==", file=sys.stderr)
    leg_sets = {
        "prod-3leg-local (dense+keyword+lesson)": ("dense", "keyword", "lesson"),
        "bm25-added-local (dense+keyword+lesson+bm25)": ("dense", "keyword", "lesson", "bm25"),
        "bm25-replaces-keyword-local (dense+bm25+lesson)": ("dense", "bm25", "lesson"),
        "bm25-alone (bm25 only, no dense leg)": ("bm25",),
    }
    part_a: dict[str, dict] = {}
    for name, legs in leg_sets.items():
        fn = LV.make_local_variant(idx, embedder, legs=legs, name=name, bm25=bm25,
                                   rerank_fn=prod_rerank_local, qcache=qcache)
        print(f"  -- {name}: no-rerank --", file=sys.stderr)
        no_rr = ev.run_eval(k, search=fn, search_kwargs={"rerank": False})
        print(f"  -- {name}: +rerank-stand-in --", file=sys.stderr)
        with_rr = ev.run_eval(k, search=fn, search_kwargs={"rerank": True})
        part_a[name] = {"no_rerank": _summarize(no_rr), "with_rerank_standin": _summarize(with_rr),
                        "_ranks_no_rerank": no_rr["ranks"], "_ranks_with_rerank": with_rr["ranks"]}
        out["part_a_retrieval"] = {n: {kk: vv for kk, vv in d.items() if not kk.startswith("_")}
                                   for n, d in part_a.items()}
        _dump(out, a.json)

    base_name = "prod-3leg-local (dense+keyword+lesson)"
    out["part_a_flips"] = {}
    for name in leg_sets:
        if name == base_name:
            continue
        out["part_a_flips"][name] = {
            "no_rerank": _rank_diffs(golden_rows, part_a[base_name]["_ranks_no_rerank"], part_a[name]["_ranks_no_rerank"]),
            "with_rerank_standin": _rank_diffs(golden_rows, part_a[base_name]["_ranks_with_rerank"], part_a[name]["_ranks_with_rerank"]),
        }
    print(f"local dense 3-leg baseline: R@1={part_a[base_name]['no_rerank']['recall_at_1']:.2f} "
         f"R@{k}={part_a[base_name]['no_rerank']['recall_at_k']:.2f} "
         f"MRR={part_a[base_name]['no_rerank']['mrr']:.2f}  (live anchor: {LIVE_ANCHOR})", file=sys.stderr)
    _dump(out, a.json)

    # ---- Part B: reranker model comparison, same (local) candidate lists ----
    print("== Part B: ms-marco-MiniLM-L-6-v2 vs BAAI/bge-reranker-v2-m3 (local, local-dense candidates) ==",
         file=sys.stderr)
    best_legs_name = max(part_a, key=lambda n: part_a[n]["with_rerank_standin"]["recall_at_k"])
    best_legs = leg_sets[best_legs_name]
    print(f"  using the best Part-A retrieval variant's candidates: {best_legs_name}", file=sys.stderr)

    t_bge = RR.load(RR.BGE_MODEL)
    fn_ms_marco = LV.make_local_variant(idx, embedder, legs=best_legs, name="ms-marco-local", bm25=bm25,
                                        rerank_fn=t_prod_rerank.as_rerank_fn(), qcache=qcache)
    fn_bge = LV.make_local_variant(idx, embedder, legs=best_legs, name="bge-reranker-v2-m3", bm25=bm25,
                                   rerank_fn=t_bge.as_rerank_fn(), qcache=qcache)

    t0 = time.perf_counter()
    res_ms_marco = ev.run_eval(k, search=fn_ms_marco)
    wall_ms_marco = time.perf_counter() - t0
    t0 = time.perf_counter()
    res_bge = ev.run_eval(k, search=fn_bge)
    wall_bge = time.perf_counter() - t0

    out["part_b_reranker"] = {
        "candidate_source": best_legs_name,
        "ms_marco": {**_summarize(res_ms_marco), "params": RR.MODEL_PARAMS[RR.MS_MARCO_MODEL],
                    "avg_predict_seconds": round(t_prod_rerank.avg_latency, 4),
                    "n_predict_calls": t_prod_rerank.total_calls, "wall_seconds": round(wall_ms_marco, 2)},
        "bge_reranker_v2_m3": {**_summarize(res_bge), "params": RR.MODEL_PARAMS[RR.BGE_MODEL],
                               "avg_predict_seconds": round(t_bge.avg_latency, 4),
                               "n_predict_calls": t_bge.total_calls, "wall_seconds": round(wall_bge, 2)},
        "rank_flips": _rank_diffs(golden_rows, res_ms_marco["ranks"], res_bge["ranks"]),
    }
    print(f"  ms-marco: R@{k}={res_ms_marco['recall_at_k']:.2f} avg_predict={t_prod_rerank.avg_latency:.3f}s",
         file=sys.stderr)
    print(f"  bge-v2-m3: R@{k}={res_bge['recall_at_k']:.2f} avg_predict={t_bge.avg_latency:.3f}s", file=sys.stderr)
    _dump(out, a.json)

    # ---- Part C: best retrieval + best reranker together ----
    best_reranker = "bge_reranker_v2_m3" if res_bge["recall_at_k"] >= res_ms_marco["recall_at_k"] else "ms_marco"
    print(f"== Part C: {best_legs_name} + {best_reranker} ==", file=sys.stderr)
    best_rerank_fn = (t_bge if best_reranker == "bge_reranker_v2_m3" else t_prod_rerank).as_rerank_fn()
    fn_combo = LV.make_local_variant(idx, embedder, legs=best_legs, name="combo", bm25=bm25,
                                     rerank_fn=best_rerank_fn, qcache=qcache)
    res_combo = ev.run_eval(k, search=fn_combo)
    out["part_c_combination"] = {"legs": best_legs_name, "reranker": best_reranker, **_summarize(res_combo)}
    print(f"  combo: R@1={res_combo['recall_at_1']:.2f} R@{k}={res_combo['recall_at_k']:.2f} "
         f"MRR={res_combo['mrr']:.2f}", file=sys.stderr)

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
