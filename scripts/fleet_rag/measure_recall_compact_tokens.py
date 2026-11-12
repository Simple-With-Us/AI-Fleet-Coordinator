#!/usr/bin/env python3
"""Approximate token counts for recall_search payloads (compact vs full).

Runs 10 golden queries against synthetic long hits (no live Qdrant) and prints JSON with
before/after token estimates.  Used in AFC-742 evidence.
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from fleet_rag import recall_presentation as presentation

GOLDEN = pathlib.Path(__file__).resolve().parent / "golden.jsonl"
SAMPLE_COUNT = 10


def _synthetic_hits(query: str, n: int = 5) -> list[dict]:
    body = (query + " ") * 120
    hits = []
    for i in range(n):
        doc = f"doc/sample-{i % 3}"
        hits.append(
            {
                "score": round(0.95 - i * 0.05, 4),
                "text": body + f" chunk={i}",
                "title": f"{query[:48]}",
                "doc_id": doc,
                "url": f"https://example.test/{doc}",
                "source": "doc",
                "app": "fleet",
                "category": "lesson",
                "chunk_index": i % 2,
            }
        )
    return hits


def main() -> int:
    queries: list[str] = []
    for line in GOLDEN.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        queries.append(str(row["query"]))
        if len(queries) >= SAMPLE_COUNT:
            break
    rows = []
    verbose_total = 0
    compact_total = 0
    for query in queries:
        raw = {"mode": "hybrid", "hits": _synthetic_hits(query)}
        verbose = presentation.present_search_result(
            raw, presentation.SearchPresentation(full=True)
        )
        compact = presentation.present_search_result(raw, presentation.SearchPresentation())
        verbose_t = presentation.approximate_tokens(verbose)
        compact_t = presentation.approximate_tokens(compact)
        verbose_total += verbose_t
        compact_total += compact_t
        rows.append(
            {
                "query": query[:80],
                "tokens_full": verbose_t,
                "tokens_compact": compact_t,
                "saved_pct": round(100 * (1 - compact_t / verbose_t), 1),
            }
        )
    summary = {
        "queries": len(rows),
        "tokens_full_total": verbose_total,
        "tokens_compact_total": compact_total,
        "saved_pct_total": round(100 * (1 - compact_total / verbose_total), 1),
        "rows": rows,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
