"""fleet_rag — shared library for the fleet-agents knowledge corpus.

Modules:
  core    credentials, HTTP with retries, embeddings (TEI), Qdrant client, point ids
  scrub   secret redaction for anything that goes into the corpus
  chunk   markdown-aware chunker sized for the bge-m3 4096-token window

Canonical doc: docs/RAG-FLEET-INFRA.md.
"""

__version__ = "0.2.0"
