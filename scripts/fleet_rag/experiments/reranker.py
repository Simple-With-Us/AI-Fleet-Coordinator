"""Local cross-encoder rerankers for the BAAI-vs-ms-marco comparison (Part B).

Both models are run locally on this Mac via sentence_transformers.CrossEncoder so the
quality *and* latency comparison is apples-to-apples on identical hardware -- the
production ms-marco model is normally served remotely by TEI on the CPU-only Hetzner
box, which is different hardware again (see the results doc for that caveat). Loading
and predicting make no network call and no LLM call.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

MS_MARCO_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"   # production's TEI-served model
BGE_MODEL = "BAAI/bge-reranker-v2-m3"                     # candidate replacement

# Approximate parameter counts, for the latency-vs-size caveat in the report (not measured
# here -- these are the published model card figures).
MODEL_PARAMS = {MS_MARCO_MODEL: 22_700_000, BGE_MODEL: 568_000_000}


@dataclass
class TimedReranker:
    name: str
    model: object
    total_calls: int = 0
    total_seconds: float = 0.0

    def score(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        pairs = [(query, t) for t in texts]
        start = time.perf_counter()
        scores = self.model.predict(pairs)
        elapsed = time.perf_counter() - start
        self.total_calls += 1
        self.total_seconds += elapsed
        return [float(s) for s in scores]

    def as_rerank_fn(self) -> Callable[[str, list[str]], list[float]]:
        return self.score

    @property
    def avg_latency(self) -> float:
        return self.total_seconds / self.total_calls if self.total_calls else 0.0


def load(model_name: str) -> TimedReranker:
    from sentence_transformers import CrossEncoder

    model = CrossEncoder(model_name, max_length=512)
    return TimedReranker(name=model_name, model=model)
