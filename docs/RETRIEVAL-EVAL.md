# Retrieval-Quality Eval

This is the document-retrieval check for Fleet RAG.  The weekly health check on `scripts/fleet_rag/golden.jsonl` stays as described in `docs/RAG-FLEET-INFRA.md`.

A second golden set scores document retrieval, separate from the weekly `golden.jsonl` health check.  `scripts/fleet_rag/retrieval_golden.json` holds 36 queries drawn from this repo's docs, runbooks and policies.  Each query lists the doc id prefixes that count as relevant.  Those ids are the ingest ids, `doc/AI-Fleet-Coordinator/<path>`.  A query is recalled at k when any hit's doc id starts with one of its prefixes.  The report is recall@1, recall@5, recall@10 and MRR, as a table and as JSON.

```bash
recall eval --retrieval
recall eval --offline --threshold 0.9
recall eval --offline --json --out /tmp/retrieval-before.json
recall eval --offline --chunk-target 800 --json --out /tmp/retrieval-after.json
recall eval --compare /tmp/retrieval-before.json /tmp/retrieval-after.json
```

`--retrieval` searches the live fleet collection (`QDRANT_FLEET_COLLECTION`) and needs the direct Qdrant path, the same rule as the weekly eval.  `--offline` chunks the files the golden set names, ranks them with an in-memory BM25 index, and reads no credentials.  CI runs that offline command.  `--source`, `--app` and `--category` are exact filters on both modes.  `--no-rerank`, `--no-lessons` and `--per-doc` change the live ranking.  Chunk target, overlap and hard max apply only with `--offline`, which is how a chunking change is measured without a re-ingest.

`--compare` with no paths is still the weekly lessons and rerank matrix.  `--compare` with two saved JSON reports prints the metric delta and every query whose rank moved.

