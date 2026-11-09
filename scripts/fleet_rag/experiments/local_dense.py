"""Local BAAI/bge-m3 dense embeddings + brute-force cosine search, for when the live TEI/Qdrant
path on the shared Hetzner box is unreachable (see the results doc's Operational note).

This is a RELATIVE, same-hardware proxy for the production dense leg, not a reproduction of it:
`sentence_transformers.SentenceTransformer("BAAI/bge-m3")` loads the same published weights TEI
serves, but truncation length, pooling/normalisation details and fp16-vs-fp32 inference can all
differ slightly from the TEI ONNX backend, so scores (and LESSON_SCORE_THRESHOLD, calibrated
against the live TEI vectors) are not guaranteed to land in exactly the same range. Every number
produced with this module is labelled "local re-embedding" in the results doc for that reason.

Both the corpus embedding matrix and the golden-query embedding matrix are cached to .cache/ (row
order aligned to corpus_cache.load_cache()'s row order / golden.jsonl's row order respectively) so
repeated experiment runs pay the encode cost once. No network call other than the one-time model
download from HuggingFace (unrelated to the Hetzner box) and no write to any shared resource.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import time
from typing import Any

import numpy as np

from ..core import LESSON_CATEGORIES, LESSON_SCORE_THRESHOLD, LESSON_SOURCE

MODEL_NAME = "BAAI/bge-m3"
CACHE_DIR = pathlib.Path(__file__).with_name(".cache")
CORPUS_EMB_FILE = CACHE_DIR / "local-bge-m3-corpus.npy"
CORPUS_META_FILE = CACHE_DIR / "local-bge-m3-corpus.meta.json"
QUERY_EMB_FILE = CACHE_DIR / "local-bge-m3-queries.npy"
QUERY_META_FILE = CACHE_DIR / "local-bge-m3-queries.meta.json"

ENCODE_BATCH = 16


def get_device() -> str:
    import torch

    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


class LocalEmbedder:
    """Thin wrapper around SentenceTransformer("BAAI/bge-m3"), timed."""

    def __init__(self, device: str | None = None):
        from sentence_transformers import SentenceTransformer

        self.device = device or get_device()
        t0 = time.perf_counter()
        self.model = SentenceTransformer(MODEL_NAME, device=self.device)
        self.load_seconds = time.perf_counter() - t0

    def encode(self, texts: list[str], batch_size: int = ENCODE_BATCH,
               show_progress_bar: bool = False) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.model.get_sentence_embedding_dimension()), dtype=np.float32)
        vecs = self.model.encode(texts, batch_size=batch_size, normalize_embeddings=True,
                                 show_progress_bar=show_progress_bar, convert_to_numpy=True)
        return np.asarray(vecs, dtype=np.float32)


def _corpus_fingerprint(rows: list[dict]) -> str:
    h = hashlib.sha256()
    h.update(str(len(rows)).encode())
    for r in rows[:5] + rows[-5:]:
        h.update(str(r.get("id", "")).encode())
    return h.hexdigest()[:16]


def embed_corpus(embedder: LocalEmbedder, rows: list[dict], force: bool = False) -> np.ndarray:
    """Corpus embedding matrix, row-aligned to `rows`, cached to disk."""
    fp = _corpus_fingerprint(rows)
    if not force and CORPUS_EMB_FILE.exists() and CORPUS_META_FILE.exists():
        meta = json.loads(CORPUS_META_FILE.read_text())
        if meta.get("fingerprint") == fp and meta.get("n") == len(rows):
            return np.load(CORPUS_EMB_FILE)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    texts = [r.get("text", "") for r in rows]
    t0 = time.perf_counter()
    emb = embedder.encode(texts, show_progress_bar=True)
    elapsed = time.perf_counter() - t0
    np.save(CORPUS_EMB_FILE, emb)
    CORPUS_META_FILE.write_text(json.dumps({"fingerprint": fp, "n": len(rows), "dim": int(emb.shape[1]),
                                            "model": MODEL_NAME, "encode_seconds": elapsed}))
    return emb


def embed_queries(embedder: LocalEmbedder, queries: list[str], force: bool = False) -> np.ndarray:
    fp = hashlib.sha256("\n".join(queries).encode()).hexdigest()[:16]
    if not force and QUERY_EMB_FILE.exists() and QUERY_META_FILE.exists():
        meta = json.loads(QUERY_META_FILE.read_text())
        if meta.get("fingerprint") == fp and meta.get("n") == len(queries):
            return np.load(QUERY_EMB_FILE)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    emb = embedder.encode(queries, show_progress_bar=False)
    np.save(QUERY_EMB_FILE, emb)
    QUERY_META_FILE.write_text(json.dumps({"fingerprint": fp, "n": len(queries), "model": MODEL_NAME}))
    return emb


class LocalIndex:
    """Brute-force cosine index over the cached corpus, standing in for core.Qdrant's three
    prefetch legs (legs.py) when the live box is unreachable. `embeddings` must be L2-normalised
    (LocalEmbedder.encode already does this) so dot product == cosine similarity."""

    def __init__(self, rows: list[dict], embeddings: np.ndarray):
        if len(rows) != embeddings.shape[0]:
            raise ValueError(f"rows/embeddings length mismatch: {len(rows)} vs {embeddings.shape[0]}")
        self.rows = rows
        self.embeddings = embeddings
        self.lesson_mask = np.array(
            [r.get("source") == LESSON_SOURCE and r.get("category") in LESSON_CATEGORIES for r in rows],
            dtype=bool)

    def _payload_of(self, i: int) -> dict:
        row = self.rows[i]
        return {k: v for k, v in row.items() if k != "id"}

    def _topk(self, mask: np.ndarray | None, vector: np.ndarray, limit: int,
              score_threshold: float | None = None) -> list[dict[str, Any]]:
        if mask is None:
            idx = np.arange(len(self.rows))
            sims = self.embeddings @ vector
        else:
            idx = np.nonzero(mask)[0]
            if idx.size == 0:
                return []
            sims = self.embeddings[idx] @ vector
        if score_threshold is not None:
            keep = sims >= score_threshold
            idx, sims = idx[keep], sims[keep]
            if idx.size == 0:
                return []
        order = np.argsort(-sims)[:limit]
        out = []
        for pos in order:
            i = int(idx[pos])
            out.append({"id": self.rows[i]["id"], "score": float(sims[pos]), "payload": self._payload_of(i)})
        return out

    def dense_leg(self, vector: np.ndarray, limit: int) -> list[dict]:
        """Local equivalent of legs.dense_leg: plain cosine search, no restriction."""
        return self._topk(None, vector, limit)

    def keyword_leg(self, vector: np.ndarray, terms: list[str], limit: int) -> list[dict]:
        """Local equivalent of legs.keyword_dense_leg: restricted to rows whose text contains
        ANY term (case-insensitive substring -- the same semantics recall_api's FakeQdrant test
        double uses for the {"match": {"text": ...}} filter shape), still ranked by cosine."""
        if not terms:
            return []
        mask = np.zeros(len(self.rows), dtype=bool)
        for i, r in enumerate(self.rows):
            low = str(r.get("text", "")).lower()
            if any(t in low for t in terms):
                mask[i] = True
        return self._topk(mask, vector, limit)

    def lesson_leg(self, vector: np.ndarray, limit: int,
                  threshold: float = LESSON_SCORE_THRESHOLD) -> list[dict]:
        """Local equivalent of legs.lesson_leg: agent-contribution lessons only, with the same
        dense-cosine score floor production uses (see the results doc for whether this threshold,
        calibrated against TEI vectors, still holds for locally re-embedded ones)."""
        return self._topk(self.lesson_mask, vector, limit, score_threshold=threshold)
