"""Retrieval eval over golden.jsonl: Recall@1, Recall@k, MRR, per-source recall, A/B compare.

Each golden row is {"query": ..., "note": ..., plus at least one of
"expect_doc_id_prefix" (hit.doc_id startswith) and "expect_text_contains" (a string or a
list of strings; any one appearing case-insensitively in hit.text counts)}, and optionally
"expect_source" (hit.source must equal it).  A row is a hit when ANY returned result satisfies
EVERY expectation the row carries: when several keys are present the same hit must match the
doc-id prefix AND contain one of the needles AND come from that source.

Per-source recall buckets each row by its expected source: "expect_source" when given, else
the source implied by the doc-id prefix (doc/, board/, note/ -> apple-note, contrib/ ->
agent-contribution, ...), else "any".

    python3 -m fleet_rag.eval [--k 5] [--threshold 0.6] [--golden path] [--json]
                              [--no-lessons] [--no-rerank] [--compare]

--compare runs every query four times (prefer_lessons and rerank each on/off) and prints the
delta of each configuration against both-off, plus the queries whose rank changed.

Exit 1 when Recall@k is below the threshold, 2 on a credential/usage error.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections import Counter
from typing import Callable

from .core import FleetRagError

GOLDEN = pathlib.Path(__file__).with_name("golden.jsonl")
THRESHOLD = 0.6

# doc_id prefix segment -> payload source, for per-source bucketing of prefix-only rows.
PREFIX_SOURCES = {"doc": "doc", "skill": "skill", "board": "board", "effort-log": "effort-log",
                  "memory": "memory", "note": "apple-note", "contrib": "agent-contribution"}

COMPARE_CONFIGS = (
    ("off/off", {"prefer_lessons": False, "rerank": False}),
    ("+lessons", {"prefer_lessons": True, "rerank": False}),
    ("+rerank", {"prefer_lessons": False, "rerank": True}),
    ("both", {"prefer_lessons": True, "rerank": True}),
)


def load_golden(path: pathlib.Path | str = GOLDEN) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                raise FleetRagError(f"{path}:{n}: not valid JSON ({e.msg})") from None
            if not row.get("query"):
                raise FleetRagError(f"{path}:{n}: missing 'query'")
            if not (row.get("expect_doc_id_prefix") or row.get("expect_text_contains")):
                raise FleetRagError(f"{path}:{n}: needs expect_doc_id_prefix or expect_text_contains")
            if row.get("expect_source") is not None and not isinstance(row["expect_source"], str):
                raise FleetRagError(f"{path}:{n}: expect_source must be a string")
            rows.append(row)
    return rows


def expected_source(row: dict) -> str:
    """Bucket for per-source recall: explicit expect_source, else implied by the prefix, else any."""
    if row.get("expect_source"):
        return row["expect_source"]
    prefix = str(row.get("expect_doc_id_prefix") or "")
    head = prefix.split("/", 1)[0]
    return PREFIX_SOURCES.get(head, "any")


def matches(row: dict, hit: dict) -> bool:
    """True when this one hit satisfies every expectation on the golden row.

    A doc-id prefix, a text expectation and an expected source must ALL hold when present; a
    row with neither prefix nor text never matches (load_golden rejects such rows anyway).
    """
    prefix = row.get("expect_doc_id_prefix")
    needles = row.get("expect_text_contains") or []
    if isinstance(needles, str):
        needles = [needles]
    needles = [n for n in needles if n]
    source = row.get("expect_source")
    if prefix and not str(hit.get("doc_id", "")).startswith(prefix):
        return False
    if needles:
        text = str(hit.get("text", "")).lower()
        if not any(n.lower() in text for n in needles):
            return False
    if source and str(hit.get("source", "")) != source:
        return False
    return bool(prefix or needles)


def _score(ranks: list[int | None]) -> dict:
    n = len(ranks)
    return {
        "n": n,
        "recall_at_1": (sum(1 for r in ranks if r == 1) / n) if n else 0.0,
        "recall_at_k": (sum(1 for r in ranks if r is not None) / n) if n else 0.0,
        "mrr": (sum(1.0 / r for r in ranks if r) / n) if n else 0.0,
    }


def _search(search: Callable[..., dict], query: str, k: int, kwargs: dict | None) -> dict:
    return search(query, limit=k, **kwargs) if kwargs else search(query, limit=k)


def run_eval(k: int = 5, golden: pathlib.Path | str = GOLDEN,
             search: Callable[..., dict] | None = None,
             search_kwargs: dict | None = None) -> dict:
    """Run every golden query through recall_search(limit=k, **search_kwargs) and score it.

    Returns the overall scores, per-source scores ("by_source"), the modes the search reported
    ("modes", so a run shows whether the rerank actually engaged), the rank of every row
    ("ranks", None for a miss) and the misses with their top-3.
    """
    if search is None:
        from .recall_api import recall_search
        search = recall_search
    rows = load_golden(golden)
    ranks: list[int | None] = []
    misses: list[dict] = []
    modes: Counter[str] = Counter()
    by_source_ranks: dict[str, list[int | None]] = {}
    for row in rows:
        res = _search(search, row["query"], k, search_kwargs)
        hits = res.get("hits", [])
        modes[str(res.get("mode", "?"))] += 1
        rank = next((i + 1 for i, h in enumerate(hits) if matches(row, h)), None)
        ranks.append(rank)
        by_source_ranks.setdefault(expected_source(row), []).append(rank)
        if rank is None:
            misses.append({"query": row["query"], "note": row.get("note", ""),
                           "top": [(h.get("doc_id", ""), h.get("text", "")[:80].replace("\n", " "))
                                   for h in hits[:3]]})
    out = {"k": k, **_score(ranks), "misses": misses, "ranks": ranks,
           "modes": dict(sorted(modes.items())),
           "by_source": {src: _score(r) for src, r in sorted(by_source_ranks.items())}}
    return out


def run_compare(k: int = 5, golden: pathlib.Path | str = GOLDEN,
                search: Callable[..., dict] | None = None) -> dict:
    """Score every COMPARE_CONFIGS variant and the per-query rank changes vs both-off."""
    rows = load_golden(golden)
    runs = {name: run_eval(k, golden, search, kwargs) for name, kwargs in COMPARE_CONFIGS}
    base = runs["off/off"]
    changes = []
    for i, row in enumerate(rows):
        per = {name: runs[name]["ranks"][i] for name, _ in COMPARE_CONFIGS}
        if len(set(per.values())) > 1:
            changes.append({"query": row["query"], "ranks": per})
    deltas = {}
    for name, _ in COMPARE_CONFIGS:
        r = runs[name]
        deltas[name] = {m: r[m] - base[m] for m in ("recall_at_1", "recall_at_k", "mrr")}
    return {"k": k, "n": len(rows), "runs": runs, "deltas": deltas, "changes": changes}


def render(res: dict) -> str:
    lines = [f"golden: {res['n']} queries   k={res['k']}",
             f"Recall@1 {res['recall_at_1']:.2f}   Recall@{res['k']} {res['recall_at_k']:.2f}   "
             f"MRR {res['mrr']:.2f}"]
    if res.get("modes"):
        lines.append("modes: " + ", ".join(f"{m}={n}" for m, n in res["modes"].items()))
    for src, sc in (res.get("by_source") or {}).items():
        lines.append(f"  {src:<20} n={sc['n']:<3} R@1 {sc['recall_at_1']:.2f}  "
                     f"R@{res['k']} {sc['recall_at_k']:.2f}  MRR {sc['mrr']:.2f}")
    for m in res["misses"]:
        lines.append(f"MISS  {m['query']}" + (f"   ({m['note']})" if m.get("note") else ""))
        for doc_id, snippet in m["top"]:
            lines.append(f"      top: {doc_id or '-'}  {snippet}")
    return "\n".join(lines)


def render_compare(res: dict) -> str:
    k = res["k"]
    lines = [f"golden: {res['n']} queries   k={k}   (delta vs off/off)",
             f"{'config':<10} {'R@1':>6} {'R@' + str(k):>6} {'MRR':>6}   {'dR@1':>6} {'dR@k':>6} {'dMRR':>6}   modes"]
    for name, _ in COMPARE_CONFIGS:
        r = res["runs"][name]
        d = res["deltas"][name]
        modes = ", ".join(f"{m}={n}" for m, n in r["modes"].items())
        lines.append(f"{name:<10} {r['recall_at_1']:>6.2f} {r['recall_at_k']:>6.2f} {r['mrr']:>6.2f}   "
                     f"{d['recall_at_1']:>+6.2f} {d['recall_at_k']:>+6.2f} {d['mrr']:>+6.2f}   {modes}")
    if res["changes"]:
        lines.append("rank changes (off/off -> +lessons / +rerank / both; - = miss):")
        for c in res["changes"]:
            r = c["ranks"]
            fmt = lambda v: "-" if v is None else str(v)  # noqa: E731
            lines.append(f"  {fmt(r['off/off']):>2} -> {fmt(r['+lessons']):>2} / {fmt(r['+rerank']):>2} / "
                         f"{fmt(r['both']):>2}   {c['query']}")
    else:
        lines.append("no per-query rank changes")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fleet_rag.eval", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--threshold", type=float, default=THRESHOLD)
    ap.add_argument("--golden", default=str(GOLDEN))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--no-lessons", action="store_true", help="prefer_lessons=False")
    ap.add_argument("--no-rerank", action="store_true", help="rerank=False")
    ap.add_argument("--compare", action="store_true",
                    help="run prefer_lessons / rerank on and off and print the deltas")
    a = ap.parse_args(argv)
    try:
        if a.compare:
            res = run_compare(a.k, a.golden)
            print(json.dumps(res, indent=2) if a.json else render_compare(res))
            return 0 if res["runs"]["both"]["recall_at_k"] >= a.threshold else 1
        kwargs = {}
        if a.no_lessons:
            kwargs["prefer_lessons"] = False
        if a.no_rerank:
            kwargs["rerank"] = False
        res = run_eval(a.k, a.golden, search_kwargs=kwargs or None)
    except FleetRagError as e:
        print(f"eval: {e}", file=sys.stderr)
        return 2
    print(json.dumps(res, indent=2) if a.json else render(res))
    return 0 if res["recall_at_k"] >= a.threshold else 1


if __name__ == "__main__":
    sys.exit(main())
