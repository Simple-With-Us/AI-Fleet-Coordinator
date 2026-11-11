"""Offline measurement experiments for the fleet RAG (BM25 / reranker A-B).

Everything under this package is read-only against the live `fleet-agents` collection
(scroll + dense search, never upsert/delete) and makes zero writes and zero LLM calls.
See docs/reviews/2026-09-14-fleet-rag-bm25-reranker-ab.md for the write-up and results.
"""
