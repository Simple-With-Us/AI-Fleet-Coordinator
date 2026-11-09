"""The fully-offline half of the measurement: BM25 alone, and rerankers A/B on BM25 candidates.

Needs no live Qdrant, no live TEI, and no local dense embedder at all -- only the cached corpus
(corpus_cache), rank_bm25, and the two locally-cached cross-encoder rerankers. Safe to run
regardless of Hetzner connectivity or whether the local bge-m3 download succeeds; see
run_local_ab.py for the parts that need a dense leg.

    python3 -m fleet_rag.experiments.run_offline_bm25 [--k 5] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from .. import core, eval as ev
from . import variants as V
from .bm25_index import Bm25Corpus
from .corpus_cache import load_cache


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
    a = ap.parse_args(argv)
    k = a.k

    print("== loading config (Infisical only) + corpus cache ==", file=sys.stderr)
    cfg = core.load_config(need_write=False)
    rows = load_cache()
    print(f"corpus: {len(rows)} points cached", file=sys.stderr)
    t0 = time.perf_counter()
    bm25 = Bm25Corpus(rows)
    print(f"BM25 index built in {time.perf_counter() - t0:.1f}s", file=sys.stderr)
    golden_rows = ev.load_golden()

    out: dict = {"k": k, "corpus_points": len(rows)}

    print("== BM25 alone, no rerank ==", file=sys.stderr)
    fn_bm25 = V.make_variant(cfg, None, legs=("bm25",), name="bm25-alone", bm25=bm25)
    res_bm25 = ev.run_eval(k, search=fn_bm25, search_kwargs={"rerank": False})
    out["bm25_alone"] = _summarize(res_bm25)
    print(f"  R@1={res_bm25['recall_at_1']:.2f} R@{k}={res_bm25['recall_at_k']:.2f} MRR={res_bm25['mrr']:.2f}",
         file=sys.stderr)
    _dump(out, a.json)

    print("== reranker A/B on BM25 candidates (local) ==", file=sys.stderr)
    from . import reranker as RR
    t_ms_marco = RR.load(RR.MS_MARCO_MODEL)
    t_bge = RR.load(RR.BGE_MODEL)
    fn_bm25_ms = V.make_variant(cfg, None, legs=("bm25",), name="bm25+ms-marco", bm25=bm25,
                                rerank_fn=t_ms_marco.as_rerank_fn())
    fn_bm25_bge = V.make_variant(cfg, None, legs=("bm25",), name="bm25+bge-v2-m3", bm25=bm25,
                                 rerank_fn=t_bge.as_rerank_fn())

    t0 = time.perf_counter()
    res_ms = ev.run_eval(k, search=fn_bm25_ms)
    wall_ms = time.perf_counter() - t0
    t0 = time.perf_counter()
    res_bge = ev.run_eval(k, search=fn_bm25_bge)
    wall_bge = time.perf_counter() - t0

    out["bm25_reranked"] = {
        "ms_marco": {**_summarize(res_ms), "params": RR.MODEL_PARAMS[RR.MS_MARCO_MODEL],
                    "avg_predict_seconds": round(t_ms_marco.avg_latency, 4),
                    "n_predict_calls": t_ms_marco.total_calls, "wall_seconds": round(wall_ms, 2)},
        "bge_reranker_v2_m3": {**_summarize(res_bge), "params": RR.MODEL_PARAMS[RR.BGE_MODEL],
                               "avg_predict_seconds": round(t_bge.avg_latency, 4),
                               "n_predict_calls": t_bge.total_calls, "wall_seconds": round(wall_bge, 2)},
        "rank_flips": _rank_diffs(golden_rows, res_ms["ranks"], res_bge["ranks"]),
        "flips_vs_bm25_alone": {
            "ms_marco": _rank_diffs(golden_rows, res_bm25["ranks"], res_ms["ranks"]),
            "bge_reranker_v2_m3": _rank_diffs(golden_rows, res_bm25["ranks"], res_bge["ranks"]),
        },
    }
    print(f"  bm25+ms-marco: R@1={res_ms['recall_at_1']:.2f} R@{k}={res_ms['recall_at_k']:.2f} "
         f"MRR={res_ms['mrr']:.2f} avg_predict={t_ms_marco.avg_latency:.3f}s", file=sys.stderr)
    print(f"  bm25+bge-v2-m3: R@1={res_bge['recall_at_1']:.2f} R@{k}={res_bge['recall_at_k']:.2f} "
         f"MRR={res_bge['mrr']:.2f} avg_predict={t_bge.avg_latency:.3f}s", file=sys.stderr)
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
