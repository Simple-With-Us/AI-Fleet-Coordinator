"""Near-duplicate guard for recall_contribute.

Before a lesson is stored, embed the (scrubbed) candidate and look for an existing
agent-contribution whose dense cosine score is at or above THRESHOLD.  The CLI refuses with
exit 1 and the MCP server answers {"status": "duplicate", ...} unless the caller passes
force.  The guard never writes.

The only public entry point is ``near_duplicate(cfg, qdrant, text)``; recall_api can call it
through a single hook, and the CLI / MCP call it directly.  It uses the recall_api seams
(``embed``) lazily so the FLEET_RECALL_FAKE backend and the tests need no network.
"""
from __future__ import annotations

from typing import Any, Callable

from .core import FleetRagError
from .scrub import scrub

THRESHOLD = 0.92
LIMIT = 3
EXCERPT = 200
CONTRIB_SOURCE = "agent-contribution"


def _default_embed(cfg: dict[str, str], texts: list[str]) -> list[list[float]]:
    from . import recall_api  # lazy: recall_api may import this module
    return recall_api.embed(cfg, texts)


def contribution_filter() -> dict:
    """source=agent-contribution, including retracted rows so they still block a duplicate."""
    from . import recall_api  # lazy, see above
    return recall_api.build_filter(source=CONTRIB_SOURCE, include_retracted=True)


def _excerpt(text: str, n: int = EXCERPT) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 3].rstrip() + "..."


def near_duplicate(cfg: dict[str, str], qdrant: Any, text: str, threshold: float = THRESHOLD,
                   embed: Callable[[dict[str, str], list[str]], list[list[float]]] | None = None) -> dict:
    """{"duplicate": bool, "threshold": float, "existing": {...} | None, "candidates": [...]}

    ``existing`` (when duplicate) carries doc_id, id, score, seat, app, category, created_at,
    title and a 200-char excerpt of the stored text.  ``candidates`` lists the same fields for
    every hit returned (at most LIMIT), highest score first, so callers can show "close but
    not identical" matches too.  Raises FleetRagError only for an empty candidate text.
    """
    if not isinstance(text, str) or not text.strip():
        raise FleetRagError("near_duplicate needs a non-empty text")
    clean, _ = scrub(text.strip())
    vector = (embed or _default_embed)(cfg, [clean])[0]
    raw = qdrant.search_dense(vector, LIMIT, contribution_filter())
    cands = []
    for r in raw:
        p = r.get("payload") or {}
        cands.append({
            "id": str(r.get("id", "")),
            "score": round(float(r.get("score", 0.0)), 4),
            "doc_id": p.get("doc_id", ""),
            "seat": p.get("seat", ""),
            "app": p.get("app", ""),
            "category": p.get("category", ""),
            "created_at": int(p.get("created_at") or 0),
            "title": p.get("title", ""),
            "excerpt": _excerpt(p.get("text", "")),
        })
    cands.sort(key=lambda c: -c["score"])
    top = cands[0] if cands else None
    dup = bool(top and top["score"] >= threshold)
    return {"duplicate": dup, "threshold": threshold, "existing": top if dup else None,
            "candidates": cands}


def duplicate_message(existing: dict) -> str:
    """The one-line refusal the CLI prints (also reused by the MCP result)."""
    return (f"similar lesson already exists: {existing.get('doc_id') or existing.get('id')} "
            f"(score {existing.get('score', 0):.2f}) — use --force to add anyway")
