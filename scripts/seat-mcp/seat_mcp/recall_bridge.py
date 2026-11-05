"""Bridge the fleet-recall tools into seat-mcp.

seat-mcp is the HTTP MCP behind https://agents.jays.services/mcp, so cloud seats (Grok Bot,
Cursor cloud, Claude Code Cloud) reach the fleet-agents corpus through these five tools.  The
logic lives in the installed fleet_rag package (/Users/jay/apps/fleet-rag, put there by
scripts/install-fleet-rag.sh); this module only validates arguments, imports
fleet_rag.recall_api by name, and maps its errors onto SeatError.  It never reimplements the
search / stats / contribute semantics.

Contract (shared with scripts/recall and fleet-recall-mcp.py):
  recall_search(query, limit=5, category?, app?, source?, seat?, since_days?) -> {hits, mode}
  recall_stats() -> {collection, status, points, embedder_healthy, by_source, by_app}
  recall_contribute(text, category, app="fleet", seat, title?, url?) -> {id, doc_id, scrubbed, status}
  recall_correct(doc_id, text, reason, seat, title?, url?, expected_revision?)
  recall_retract(doc_id, reason, seat, expected_revision?)

On this surface `seat` is REQUIRED for the three mutations: a cloud caller has no AGENT_SEAT in
its environment, and the seat-mcp process's own AGENT_SEAT must never be attributed to it.  A seat
may correct or retract its own contributions;  OWNER may mutate any contribution.  Retracted
contributions are excluded from normal search and visible only with include_retracted=true.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

from .seats import SeatError

JsonDict = dict[str, Any]

DEFAULT_FLEET_RAG_HOME = Path("/Users/jay/apps/fleet-rag")
INSTALL_HINT = (
    "fleet_rag package not installed at %s.  "
    "Run `bash scripts/install-fleet-rag.sh` from an AI-Fleet-Coordinator checkout "
    "(or set FLEET_RAG_HOME) and restart seat-mcp."
)

CONTRIBUTE_CATEGORIES = ("lesson", "preference", "infrastructure", "decision", "runbook")
MAX_LIMIT = 50

_api: ModuleType | None = None


def fleet_rag_home() -> Path:
    """Installed location of the fleet_rag package (parent dir).  Env override for tests."""
    raw = os.environ.get("FLEET_RAG_HOME", "").strip()
    return Path(raw) if raw else DEFAULT_FLEET_RAG_HOME


def _load_api() -> ModuleType:
    """Import fleet_rag.recall_api from the installed location, once.

    The sys.path insert is guarded so a seat-mcp process that never calls a recall tool never
    touches sys.path, and so an already-importable fleet_rag (tests, dev checkout) wins.
    """
    global _api
    if _api is not None:
        return _api
    home = fleet_rag_home()
    pkg = home / "fleet_rag" / "recall_api.py"
    if not pkg.is_file():
        raise SeatError(INSTALL_HINT % home)
    home_s = str(home)
    if home_s not in sys.path:
        sys.path.insert(0, home_s)
    try:
        import importlib

        mod = importlib.import_module("fleet_rag.recall_api")
    except ImportError as exc:
        raise SeatError("fleet_rag.recall_api failed to import from %s:  %s" % (home, exc)) from exc
    for name in (
        "recall_search",
        "recall_stats",
        "recall_contribute",
        "recall_correct",
        "recall_retract",
    ):
        if not callable(getattr(mod, name, None)):
            raise SeatError(
                "fleet_rag.recall_api at %s lacks %s; reinstall with scripts/install-fleet-rag.sh"
                % (home, name)
            )
    try:
        fallback = importlib.import_module("fleet_rag.public_fallback")
    except ImportError as exc:
        raise SeatError(
            "fleet_rag.public_fallback failed to import from %s:  %s" % (home, exc)
        ) from exc
    if not callable(getattr(fallback, "call_mutation", None)):
        raise SeatError(
            "fleet_rag.public_fallback at %s lacks call_mutation; reinstall with scripts/install-fleet-rag.sh"
            % home
        )
    _api = mod
    return mod


def _reset_api_cache() -> None:
    """Test hook.  Drops the cached module so the next call re-imports."""
    global _api
    _api = None


def _user_errors() -> tuple[type[BaseException], ...]:
    """Exception types from fleet_rag that are the caller's fault (or a service outage)."""
    kinds: list[type[BaseException]] = [ValueError]
    try:
        from fleet_rag.core import FleetRagError  # type: ignore[import-not-found]

        kinds.append(FleetRagError)
    except ImportError:
        pass
    return tuple(kinds)


def _call(fn_name: str, **kwargs: Any) -> JsonDict:
    api = _load_api()
    fn = getattr(api, fn_name)
    try:
        if fn_name in ("recall_correct", "recall_retract"):
            import importlib

            fallback = importlib.import_module("fleet_rag.public_fallback")
            out = fallback.call_mutation(fn_name, kwargs, lambda: fn(**kwargs))
        else:
            out = fn(**kwargs)
    except SeatError:
        raise
    except _user_errors() as exc:
        raise SeatError("%s:  %s" % (fn_name, exc)) from exc
    if not isinstance(out, dict):
        raise SeatError("%s returned %s, expected object" % (fn_name, type(out).__name__))
    return out


# --------------------------------------------------------------------------- argument helpers


def _opt_str(arguments: JsonDict, *names: str) -> str | None:
    for name in names:
        val = arguments.get(name)
        if val is None:
            continue
        if not isinstance(val, str):
            raise SeatError("%s must be a string" % name)
        val = val.strip()
        if val:
            return val
    return None


def _opt_int(arguments: JsonDict, name: str, lo: int, hi: int) -> int | None:
    val = arguments.get(name)
    if val is None or val == "":
        return None
    if isinstance(val, bool) or not isinstance(val, (int, float, str)):
        raise SeatError("%s must be an integer" % name)
    try:
        num = int(val)
    except (TypeError, ValueError):
        raise SeatError("%s must be an integer" % name) from None
    if num < lo or num > hi:
        raise SeatError("%s must be between %d and %d" % (name, lo, hi))
    return num


# --------------------------------------------------------------------------- tools


def recall_search(arguments: JsonDict) -> JsonDict:
    query = _opt_str(arguments, "query", "q")
    if not query:
        raise SeatError("query is required")
    limit = _opt_int(arguments, "limit", 1, MAX_LIMIT) or 5
    kwargs: JsonDict = {"query": query, "limit": limit}
    for key in ("category", "app", "source"):
        val = _opt_str(arguments, key)
        if val:
            kwargs[key] = val.lower()
    seat = _opt_str(arguments, "seat")
    if seat:
        kwargs["seat"] = seat.upper()
    since = _opt_int(arguments, "since_days", 1, 3650)
    if since is None:
        since = _opt_int(arguments, "sinceDays", 1, 3650)
    if since is not None:
        kwargs["since_days"] = since
    if arguments.get("include_retracted") is True or arguments.get("includeRetracted") is True:
        kwargs["include_retracted"] = True
    return _call("recall_search", **kwargs)


def recall_stats(arguments: JsonDict) -> JsonDict:
    return _call("recall_stats")


def recall_contribute(arguments: JsonDict) -> JsonDict:
    text = arguments.get("text")
    if not isinstance(text, str) or not text.strip():
        raise SeatError("text is required")
    category = _opt_str(arguments, "category")
    if not category:
        raise SeatError("category is required (one of %s)" % "|".join(CONTRIBUTE_CATEGORIES))
    category = category.lower()
    if category not in CONTRIBUTE_CATEGORIES:
        raise SeatError("category must be one of %s" % "|".join(CONTRIBUTE_CATEGORIES))
    seat = _opt_str(arguments, "seat")
    if not seat:
        raise SeatError(
            "seat is required on seat-mcp (cloud callers have no AGENT_SEAT).  "
            "Pass your uppercase seat tag, e.g. GROK, CURSOR, CLAUDE."
        )
    kwargs: JsonDict = {
        "text": text,
        "category": category,
        "app": (_opt_str(arguments, "app") or "fleet").lower(),
        "seat": seat.upper(),
    }
    title = _opt_str(arguments, "title")
    if title:
        kwargs["title"] = title
    url = _opt_str(arguments, "url")
    if url:
        kwargs["url"] = url
    return _call("recall_contribute", **kwargs)


def _required_seat(arguments: JsonDict, operation: str) -> str:
    seat = _opt_str(arguments, "seat")
    if not seat:
        raise SeatError(
            "seat is required on seat-mcp for %s (cloud callers have no AGENT_SEAT).  "
            "Pass your uppercase seat tag, e.g. GROK, CURSOR, CLAUDE." % operation
        )
    seat = seat.upper()
    if seat == "OWNER":
        raise SeatError(
            "OWNER recall mutations are refused on seat-mcp:  this shared bearer does not "
            "prove the owner identity.  Use the recall service's owner-token route."
        )
    return seat


def _expected_revision(arguments: JsonDict) -> int | None:
    val = arguments.get("expected_revision")
    if val is None:
        val = arguments.get("expectedRevision")
    if val is None or val == "":
        return None
    if isinstance(val, bool) or not isinstance(val, int) or not 1 <= val <= 1000000:
        raise SeatError("expected_revision must be an integer between 1 and 1000000")
    return val


def _mutation_base(arguments: JsonDict, operation: str) -> JsonDict:
    doc_id = _opt_str(arguments, "doc_id") or _opt_str(arguments, "docId")
    if not doc_id:
        raise SeatError("doc_id is required")
    reason = _opt_str(arguments, "reason")
    if not reason:
        raise SeatError("reason is required")
    kwargs: JsonDict = {
        "doc_id": doc_id,
        "reason": reason,
        "seat": _required_seat(arguments, operation),
    }
    expected = _expected_revision(arguments)
    if expected is not None:
        kwargs["expected_revision"] = expected
    return kwargs


def recall_correct(arguments: JsonDict) -> JsonDict:
    text = arguments.get("text")
    if not isinstance(text, str) or not text.strip():
        raise SeatError("replacement text is required")
    kwargs = _mutation_base(arguments, "recall_correct")
    kwargs["text"] = text
    title = _opt_str(arguments, "title")
    if title:
        kwargs["title"] = title
    url = _opt_str(arguments, "url")
    if url:
        kwargs["url"] = url
    return _call("recall_correct", **kwargs)


def recall_retract(arguments: JsonDict) -> JsonDict:
    return _call("recall_retract", **_mutation_base(arguments, "recall_retract"))


RECALL_TOOL_IMPL = {
    "recall_search": recall_search,
    "recall_stats": recall_stats,
    "recall_contribute": recall_contribute,
    "recall_correct": recall_correct,
    "recall_retract": recall_retract,
}


def recall_tool_schemas() -> list[JsonDict]:
    filter_props = {
        "category": {
            "type": "string",
            "description": "lesson|preference|infrastructure|decision|runbook|finding|note|doc",
        },
        "app": {
            "type": "string",
            "description": "Lowercase app slug:  fleet, socratic-trade, congress-trade, usage-monitor, dealdex, botfleet, ...",
        },
        "source": {
            "type": "string",
            "description": "board|effort-log|apple-note|doc|skill|memory|chat-log|agent-contribution",
        },
        "seat": {
            "type": "string",
            "description": "Uppercase seat tag:  CLAUDE, MONET, GROK, CODEX, AG, CURSOR, OWNER.",
        },
        "since_days": {
            "type": "integer",
            "minimum": 1,
            "maximum": 3650,
            "description": "Only points created in the last N days.",
        },
    }
    return [
        {
            "name": "recall_search",
            "description": (
                "Hybrid (dense + keyword) search over the fleet-agents knowledge corpus:  "
                "board findings, effort logs, Apple Notes, fleet docs, skills, memory, chat "
                "logs, and agent contributions.  Returns scored chunks with source, app, category, seat, "
                "doc_id, heading, title, url, path, created_at.  Read-only."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language question or keywords.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_LIMIT,
                        "description": "Default 5.",
                    },
                    **filter_props,
                    "include_retracted": {
                        "type": "boolean",
                        "default": False,
                        "description": "Include retracted contributions for audit; normal search hides them.",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "recall_stats",
            "description": (
                "Corpus health:  collection, status, point count, embedder health, "
                "and counts by source and by app.  Read-only."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "recall_contribute",
            "description": (
                "Add one lesson / preference / infrastructure note / decision / runbook to the "
                "fleet-agents corpus.  Text 40..4000 chars.  Secrets are scrubbed and a gitleaks "
                "gate refuses anything that still looks like a credential.  seat is REQUIRED "
                "here (cloud callers have no AGENT_SEAT).  Stored as source=agent-contribution."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The knowledge to store (40..4000 chars).",
                    },
                    "category": {"type": "string", "enum": list(CONTRIBUTE_CATEGORIES)},
                    "app": {"type": "string", "description": "Lowercase app slug.  Default fleet."},
                    "seat": {
                        "type": "string",
                        "description": "Your uppercase seat tag (required).",
                    },
                    "title": {"type": "string", "description": "Optional short title."},
                    "url": {
                        "type": "string",
                        "description": "Optional link (PR, issue, board item).",
                    },
                },
                "required": ["text", "category", "seat"],
            },
        },
        {
            "name": "recall_correct",
            "description": (
                "Correct one contribution by exact doc_id.  The same point and doc_id stay in use, the old "
                "text remains in the revision audit, and search sees only the replacement.  A seat may "
                "correct its own contributions;  OWNER may correct any.  A retracted contribution is not "
                "resurrected.  Text and metadata are secret-scrubbed and gitleaks-gated."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "doc_id": {"type": "string", "description": "Exact contrib/... lesson id."},
                    "text": {
                        "type": "string",
                        "description": "Replacement lesson text (40..4000 chars).",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why the correction is being made (3..500 chars).",
                    },
                    "seat": {
                        "type": "string",
                        "description": "Your uppercase seat tag (required).",
                    },
                    "title": {"type": "string", "description": "Optional replacement title."},
                    "url": {"type": "string", "description": "Optional replacement link."},
                    "expected_revision": {"type": "integer", "minimum": 1},
                },
                "required": ["doc_id", "text", "reason", "seat"],
            },
        },
        {
            "name": "recall_retract",
            "description": (
                "Retract (take back) one contribution by exact doc_id.  The point stays as audit metadata "
                "with actor, time and reason, but normal search excludes it.  A seat may retract its own "
                "contributions;  OWNER may retract any.  Repeating a retraction is idempotent."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "doc_id": {"type": "string", "description": "Exact contrib/... lesson id."},
                    "reason": {
                        "type": "string",
                        "description": "Why the contribution is being retracted (3..500 chars).",
                    },
                    "seat": {
                        "type": "string",
                        "description": "Your uppercase seat tag (required).",
                    },
                    "expected_revision": {"type": "integer", "minimum": 1},
                },
                "required": ["doc_id", "reason", "seat"],
            },
        },
    ]
