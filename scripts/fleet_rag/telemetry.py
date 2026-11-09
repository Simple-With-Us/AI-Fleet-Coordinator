"""Optional Sentry instrumentation for the fleet-agents RAG pipeline.

Off unless SENTRY_DSN is set in the environment; sentry_sdk is not a hard dependency (fleet_rag
is stdlib-only everywhere else), so this module is a silent no-op both when the package is not
installed and when SENTRY_DSN is unset -- exactly today's behavior for anyone who has not opted
in.  Every function here swallows its own errors: telemetry must never break a caller, matching
the "any failure falls back silently" rule the rest of this package follows for reranking and
the public fallback.

Never sends payload text, queries, doc_id contents, or credentials -- only exception types,
counts, and known-safe tags (component/operation/source/app), matching "values are never
printed or logged" elsewhere in fleet_rag.

Why this exists: recall_search's cross-encoder rerank silently falls back to the fused order on
any failure (core.rerank, recall_api._apply_rerank) -- by design, so a flaky reranker never
breaks a query.  But that means a reranker outage degrades retrieval quality (measured
2026-09-02: Recall@5 0.92 with rerank vs 0.84 without, docs/RAG-FLEET-INFRA.md) with zero signal
until the next `recall eval` -- weekly, per the Oracle routine.  This module gives that failure
mode, and ingest source/sentinel failures that already log locally, a Sentry trail so they show
up the same way every other fleet outage does.
"""
from __future__ import annotations

import os
from typing import Any

_sdk: Any = None
_init_tried = False


def _client() -> Any:
    """Lazily import and initialize sentry_sdk once.  None when unavailable or unconfigured."""
    global _sdk, _init_tried
    if _init_tried:
        return _sdk
    _init_tried = True
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return None
    try:
        import sentry_sdk  # optional dependency, imported lazily so it stays optional
        sentry_sdk.init(dsn=dsn, environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
                        traces_sample_rate=0.0)
        _sdk = sentry_sdk
    except Exception:  # noqa: BLE001 - telemetry must never break the caller
        _sdk = None
    return _sdk


def reset() -> None:
    """Test hook: forget the cached client so the next call re-reads SENTRY_DSN."""
    global _sdk, _init_tried
    _sdk, _init_tried = None, False


def capture_message(message: str, level: str = "warning", **tags: Any) -> None:
    sdk = _client()
    if sdk is None:
        return
    try:
        with sdk.push_scope() as scope:
            scope.set_tag("component", "fleet-rag")
            for k, v in tags.items():
                scope.set_tag(k, str(v))
            sdk.capture_message(message, level=level)
    except Exception:  # noqa: BLE001 - telemetry must never break the caller
        pass


def capture_exception(exc: BaseException, **tags: Any) -> None:
    sdk = _client()
    if sdk is None:
        return
    try:
        with sdk.push_scope() as scope:
            scope.set_tag("component", "fleet-rag")
            for k, v in tags.items():
                scope.set_tag(k, str(v))
            sdk.capture_exception(exc)
    except Exception:  # noqa: BLE001 - telemetry must never break the caller
        pass
