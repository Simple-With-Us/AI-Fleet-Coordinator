"""In-memory BM25 index over the cached fleet-agents corpus (chunk-level, like the dense legs)."""
from __future__ import annotations

from rank_bm25 import BM25Okapi

from .tokenizer import tokenize


class Bm25Corpus:
    """Wraps rank_bm25.BM25Okapi with the corpus rows so results come back Qdrant-shaped.

    `rows` are corpus_cache rows: {"id", "doc_id", "text", "source", "app", ...}.  Built
    once per process (tokenizing ~42k chunks takes a couple of seconds); `search()` is
    then just a get_scores() + argsort, called once per golden query.
    """

    def __init__(self, rows: list[dict]):
        self.rows = rows
        self._tokenized = [tokenize(r.get("text", "")) for r in rows]
        self.bm25 = BM25Okapi(self._tokenized)

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Top `limit` rows by BM25 score, Qdrant-search-result shaped: {id, score, payload}."""
        terms = tokenize(query)
        if not terms:
            return []
        scores = self.bm25.get_scores(terms)
        ranked = sorted(range(len(scores)), key=lambda i: -scores[i])[:limit]
        out = []
        for i in ranked:
            if scores[i] <= 0:
                break
            row = self.rows[i]
            payload = {k: v for k, v in row.items() if k != "id"}
            out.append({"id": row["id"], "score": float(scores[i]), "payload": payload})
        return out
