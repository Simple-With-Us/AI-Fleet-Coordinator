"""Driver for the BM25 / bge-reranker-v2-m3 A-B measurement (see the results doc for the
write-up).  Zero writes to Qdrant, zero LLM calls.  Uses the *unmodified* eval.py harness to
score every variant, so the numbers are directly comparable to `python3 -m fleet_rag.eval`.

    python3 -m fleet_rag.experiments.run_ab [--k 5] [--json out.json] [--skip-rerank-bench]

Part A: production 3-leg retrieval vs +BM25 (4th leg) vs BM25-replaces-keyword-leg, each
        with rerank off (retrieval quality alone) and with rerank on (production's real,
        currently-deployed TEI ms-marco reranker, so the retrieval delta is measured against
        what actually ships today).
Part B: on the winning retrieval variant's candidate pool, current ms-marco-MiniLM-L-6-v2 vs
        BAAI/bge-reranker-v2-m3, both run locally so latency is apples-to-apples (see the
        results doc for why that still is not the CPU-only Hetzner box).
Part C: winning retrieval + winning reranker together, to see whether the gains stack.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import time

from .. import core, eval as ev, recall_api
from . import variants as V
from .bm25_index import Bm25Corpus
from .corpus_cache import load_cache

# The box this hits (docs/RAG-FLEET-INFRA.md) warns "Tailscale can flap"; during this
# measurement session it flapped hard enough to break the documented SSH-tunnel workaround
# too (see the results doc's Operational note).  A phase here is 75-300 live queries -- losing
# one to a mid-run drop is a real cost, so retry the whole phase a few times, reopening the
# tunnel between attempts, rather than letting one FleetRagError blow up an hour of progress.
RETRY_ATTEMPTS = 6
RETRY_PAUSE_SECONDS = 45


def _reopen_tunnel() -> None:
    try:
        subprocess.run(["bash", "scripts/recall-tunnel", "up"], cwd=str(pathlib.Path(__file__).resolve().parents[3]),
                       capture_output=True, timeout=30, check=False)
    except Exception:  # noqa: BLE001 - best-effort; the retried call will just fail again if this didn't help
        pass


def resilient(label: str, fn, *args, **kwargs):
    """Run fn(*args, **kwargs), retrying on FleetRagError (the shared box's Tailscale flapping)."""
    last: core.FleetRagError | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return fn(*args, **kwargs)
        except core.FleetRagError as e:
            last = e
            print(f"  [{label}] attempt {attempt}/{RETRY_ATTEMPTS} failed: {e}", file=sys.stderr)
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_PAUSE_SECONDS)
                _reopen_tunnel()
    raise last  # type: ignore[misc]


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
    ap.add_argument("--json", default=None, help="write the full results object here")
    ap.add_argument("--rebuild-cache", action="store_true")
    ap.add_argument("--skip-rerank-bench", action="store_true", help="skip Part B/C (retrieval only)")
    a = ap.parse_args(argv)
    k = a.k

    print("== loading config + corpus cache ==", file=sys.stderr)
    cfg = core.load_config(need_write=False)
    rows = load_cache(force_rebuild=a.rebuild_cache)
    print(f"corpus: {len(rows)} points cached", file=sys.stderr)
    bm25 = Bm25Corpus(rows)
    q = core.Qdrant(cfg)
    golden_rows = ev.load_golden()

    out: dict = {"k": k, "corpus_points": len(rows)}

    # ---- 0. re-confirm the real production baseline (the actually-deployed pipeline) ----
    # Note: ev.run_compare() runs all 4 COMPARE_CONFIGS (300 queries); its "off/off" leg
    # (prefer_lessons=False) reproducibly drives Qdrant into a slow-query path on this box
    # that trips Qdrant's own request timeout (HTTP 408) even on a fully healthy connection --
    # not the Tailscale flapping this script otherwise retries around (see the results doc's
    # Operational note).  Only "both" (production's real default) and "+lessons" (needed for
    # the parity check below) are actually used, so fetch just those two directly instead.
    print("== re-running the real production baseline (recall_search) ==", file=sys.stderr)
    both_res = resilient("baseline/both", ev.run_eval, k)
    lessons_res = resilient("baseline/+lessons", ev.run_eval, k, search_kwargs={"rerank": False})
    out["production_baseline"] = {"both": _summarize(both_res), "+lessons": _summarize(lessons_res)}
    print(ev.render(both_res), file=sys.stderr)
    _dump(out, a.json)

    # ---- Part A: retrieval-leg ablation, both without and with the real production rerank ----
    print("== Part A: retrieval variants (BM25 add / replace) ==", file=sys.stderr)
    prod_rerank = V.production_rerank_fn(cfg)
    leg_sets = {
        "prod-3leg (dense+keyword+lesson)": ("dense", "keyword", "lesson"),
        "bm25-added (dense+keyword+lesson+bm25)": ("dense", "keyword", "lesson", "bm25"),
        "bm25-replaces-keyword (dense+bm25+lesson)": ("dense", "bm25", "lesson"),
    }
    part_a: dict[str, dict] = {}
    variant_fns: dict[str, object] = {}
    for name, legs in leg_sets.items():
        fn = V.make_variant(cfg, q, legs=legs, name=name, bm25=bm25, rerank_fn=prod_rerank)
        variant_fns[name] = fn
        print(f"  -- {name}: no-rerank --", file=sys.stderr)
        no_rr = resilient(f"{name}/no-rerank", ev.run_eval, k, search=fn, search_kwargs={"rerank": False})
        print(f"  -- {name}: +prod-rerank --", file=sys.stderr)
        with_rr = resilient(f"{name}/+rerank", ev.run_eval, k, search=fn, search_kwargs={"rerank": True})
        part_a[name] = {"no_rerank": _summarize(no_rr), "with_prod_rerank": _summarize(with_rr),
                        "_ranks_no_rerank": no_rr["ranks"], "_ranks_with_rerank": with_rr["ranks"]}
        out["part_a_retrieval"] = {n: {kk: vv for kk, vv in d.items() if not kk.startswith("_")}
                                   for n, d in part_a.items()}
        _dump(out, a.json)

    # parity check: our python-fused 3-leg (no rerank) vs the real +lessons config
    base_name = "prod-3leg (dense+keyword+lesson)"
    real_lessons_ranks = lessons_res["ranks"]
    parity_diffs = _rank_diffs(golden_rows, real_lessons_ranks, part_a[base_name]["_ranks_no_rerank"])
    out["parity_check_vs_real_plus_lessons"] = {
        "real_recall_at_k": lessons_res["recall_at_k"],
        "repro_recall_at_k": part_a[base_name]["no_rerank"]["recall_at_k"],
        "n_rank_diffs": len(parity_diffs), "diffs": parity_diffs}
    print(f"parity check (repro 3-leg vs real +lessons): "
         f"{len(parity_diffs)} rank diffs out of {len(golden_rows)}", file=sys.stderr)

    # rank-level flips for each BM25 variant vs the reproduced baseline, both without and with rerank
    out["part_a_flips"] = {}
    for name in leg_sets:
        if name == base_name:
            continue
        out["part_a_flips"][name] = {
            "no_rerank": _rank_diffs(golden_rows, part_a[base_name]["_ranks_no_rerank"], part_a[name]["_ranks_no_rerank"]),
            "with_prod_rerank": _rank_diffs(golden_rows, part_a[base_name]["_ranks_with_rerank"], part_a[name]["_ranks_with_rerank"]),
        }

    if a.skip_rerank_bench:
        _dump(out, a.json)
        return 0

    # ---- Part B: reranker model comparison, same candidate lists, both local ----
    print("== Part B: ms-marco-MiniLM-L-6-v2 vs BAAI/bge-reranker-v2-m3 (local) ==", file=sys.stderr)
    from . import reranker as RR
    best_legs_name = max(part_a, key=lambda n: part_a[n]["with_prod_rerank"]["recall_at_k"])
    best_legs = leg_sets[best_legs_name]
    print(f"  using the best Part-A retrieval variant's candidates: {best_legs_name}", file=sys.stderr)

    t_ms_marco = RR.load(RR.MS_MARCO_MODEL)
    t_bge = RR.load(RR.BGE_MODEL)
    fn_ms_marco = V.make_variant(cfg, q, legs=best_legs, name="ms-marco-local", bm25=bm25,
                                 rerank_fn=t_ms_marco.as_rerank_fn())
    fn_bge = V.make_variant(cfg, q, legs=best_legs, name="bge-reranker-v2-m3", bm25=bm25,
                            rerank_fn=t_bge.as_rerank_fn())

    t0 = time.perf_counter()
    res_ms_marco = resilient("part-b/ms-marco", ev.run_eval, k, search=fn_ms_marco)
    wall_ms_marco = time.perf_counter() - t0
    t0 = time.perf_counter()
    res_bge = resilient("part-b/bge-v2-m3", ev.run_eval, k, search=fn_bge)
    wall_bge = time.perf_counter() - t0

    out["part_b_reranker"] = {
        "candidate_source": best_legs_name,
        "ms_marco": {**_summarize(res_ms_marco), "params": RR.MODEL_PARAMS[RR.MS_MARCO_MODEL],
                    "avg_predict_seconds": round(t_ms_marco.avg_latency, 4),
                    "n_predict_calls": t_ms_marco.total_calls, "wall_seconds": round(wall_ms_marco, 2)},
        "bge_reranker_v2_m3": {**_summarize(res_bge), "params": RR.MODEL_PARAMS[RR.BGE_MODEL],
                               "avg_predict_seconds": round(t_bge.avg_latency, 4),
                               "n_predict_calls": t_bge.total_calls, "wall_seconds": round(wall_bge, 2)},
        "rank_flips": _rank_diffs(golden_rows, res_ms_marco["ranks"], res_bge["ranks"]),
    }
    print(f"  ms-marco: R@{k}={res_ms_marco['recall_at_k']:.2f} avg_predict={t_ms_marco.avg_latency:.3f}s", file=sys.stderr)
    print(f"  bge-v2-m3: R@{k}={res_bge['recall_at_k']:.2f} avg_predict={t_bge.avg_latency:.3f}s", file=sys.stderr)
    _dump(out, a.json)

    # ---- Part C: best retrieval + best reranker together ----
    best_reranker = "bge_reranker_v2_m3" if res_bge["recall_at_k"] >= res_ms_marco["recall_at_k"] else "ms_marco"
    print(f"== Part C: {best_legs_name} + {best_reranker} ==", file=sys.stderr)
    best_rerank_fn = (t_bge if best_reranker == "bge_reranker_v2_m3" else t_ms_marco).as_rerank_fn()
    fn_combo = V.make_variant(cfg, q, legs=best_legs, name="combo", bm25=bm25, rerank_fn=best_rerank_fn)
    res_combo = resilient("part-c/combo", ev.run_eval, k, search=fn_combo)
    out["part_c_combination"] = {"legs": best_legs_name, "reranker": best_reranker,
                                 **_summarize(res_combo)}
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
    else:
        print(text)


if __name__ == "__main__":
    sys.exit(main())
