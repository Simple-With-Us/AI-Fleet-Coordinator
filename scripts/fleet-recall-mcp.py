#!/usr/bin/env python3
"""fleet-recall -- dependency-free MCP server (stdio) over the fleet-agents corpus.

JSON-RPC 2.0, one message per line on stdin/stdout.  Logging goes to stderr only.  Exposes
exactly the recall tool contract (recall_search / recall_stats / recall_contribute) from
scripts/fleet_rag/recall_api.py, so the CLI, this server, and the seat-mcp tools agree.

Credentials load lazily on the first tools/call, so initialize and tools/list work on a
machine with no keys.  Set FLEET_RECALL_FAKE=1 to serve an in-process fake corpus (tests).

Register (Claude Code): claude mcp add fleet-recall -- python3 /path/to/fleet-recall-mcp.py
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

from fleet_rag import __version__  # noqa: E402
from fleet_rag import contribute_guard, public_fallback, recall_api, search_log  # noqa: E402
from fleet_rag.core import FleetRagError  # noqa: E402

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "fleet-recall", "version": __version__}

TOOLS = [
    {
        "name": "recall_search",
        "description": (
            "Search the fleet's shared knowledge corpus (lessons, owner preferences, infrastructure "
            "facts, decisions, runbooks, board findings, effort logs, Apple Notes, fleet docs).  Use it "
            "BEFORE re-deriving a lesson, guessing an owner preference, or debugging infrastructure "
            "another seat has already documented.  Hybrid dense + keyword search, one hit per document "
            "(per_doc keeps the best 1..3 chunks of each), agent lessons boosted into the fusion "
            "(prefer_lessons) and cross-encoder reranked when the endpoint is configured (rerank; the "
            "returned mode says whether it engaged).  Filter by category, app, source, seat, or recency."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language question or keywords."},
                "limit": {"type": "integer", "minimum": 1, "maximum": recall_api.MAX_LIMIT, "default": 5},
                "category": {"type": "string", "enum": list(recall_api.CATEGORIES),
                             "description": "Restrict to one category."},
                "app": {"type": "string", "description": "Lowercase app slug, e.g. fleet, socratic-trade."},
                "source": {"type": "string", "enum": list(recall_api.SOURCES)},
                "seat": {"type": "string", "description": "Uppercase seat tag, e.g. CLAUDE, GROK."},
                "since_days": {"type": "integer", "minimum": 1,
                               "description": "Only content created in the last N days."},
                "per_doc": {"type": "integer", "minimum": 1, "maximum": recall_api.PER_DOC_MAX, "default": 1,
                            "description": "Best N chunks to return per document (default 1)."},
                "rerank": {"type": "boolean", "default": True,
                           "description": "Rerank the candidates with the cross-encoder when it is "
                                          "configured; false keeps the fused order."},
                "prefer_lessons": {"type": "boolean", "default": True,
                                   "description": "Boost agent lessons into the fusion; false for raw "
                                                  "document research."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "recall_stats",
        "description": (
            "Collection health and point counts by source and app.  Use it to check the corpus is "
            "reachable before relying on recall_search, or to see which sources have been ingested."),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "recall_contribute",
        "description": (
            "Store one reusable piece of knowledge for every other seat: a lesson learned, an owner "
            "preference, an infrastructure fact, a decision, or a runbook step.  Use it AFTER you learn "
            "something reusable that is not already in the corpus (search first).  40..4000 chars, "
            "secrets are scrubbed and the scrub kinds returned; text that still looks like a secret is "
            "refused (the gitleaks gate fails closed; 'gitleaks-unavailable' in the returned list means "
            "only the regex scrub ran).  A near-duplicate of an existing agent contribution (cosine "
            ">= 0.92) is not stored: the result is {\"status\": \"duplicate\", \"existing\": {...}} "
            "unless force is true.  Requires the write key and a seat (argument or AGENT_SEAT)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "minLength": recall_api.CONTRIB_MIN,
                         "maxLength": recall_api.CONTRIB_MAX},
                "category": {"type": "string", "enum": list(recall_api.CONTRIB_CATEGORIES)},
                "app": {"type": "string", "default": "fleet"},
                "seat": {"type": "string", "description": "Uppercase seat tag; defaults to $AGENT_SEAT."},
                "title": {"type": "string"},
                "url": {"type": "string", "description": "Source link (PR, board item, doc), optional."},
                "force": {"type": "boolean", "default": False,
                          "description": "Store even when a near-duplicate contribution already exists."},
            },
            "required": ["text", "category"],
        },
    },
]

_ALLOWED_ARGS = {t["name"]: set(t["inputSchema"]["properties"]) for t in TOOLS}


def log(msg: str) -> None:
    sys.stderr.write(f"fleet-recall: {msg}\n")
    sys.stderr.flush()


def _int(args: dict, key: str) -> None:
    if key in args and args[key] is not None:
        try:
            args[key] = int(args[key])
        except (TypeError, ValueError):
            raise FleetRagError(f"{key} must be an integer") from None


def _bool(args: dict, key: str) -> None:
    if key in args and args[key] is not None and not isinstance(args[key], bool):
        raise FleetRagError(f"{key} must be a boolean")


def call_tool(name: str, args: dict) -> dict:
    if name not in _ALLOWED_ARGS:
        raise KeyError(name)
    unknown = set(args) - _ALLOWED_ARGS[name]
    if unknown:
        raise FleetRagError("unknown argument(s): " + ", ".join(sorted(unknown)))
    if name == "recall_search":
        _int(args, "limit")
        _int(args, "since_days")
        _int(args, "per_doc")
        _bool(args, "rerank")
        _bool(args, "prefer_lessons")
        caller_seat = args.get("seat") or os.environ.get("AGENT_SEAT") or os.environ.get("AGENT_TAG")
        # A null for an optional flag means "default", not False.
        for key in ("per_doc", "rerank", "prefer_lessons"):
            if key in args and args[key] is None:
                del args[key]
        t0 = time.time()
        res = public_fallback.call_with_fallback(
            "recall_search", args, lambda: recall_api.recall_search(**args))
        latency_ms = int((time.time() - t0) * 1000)
        search_log.log_search(query=args.get("query", ""), seat=caller_seat,
                              app=args.get("app"), hits_count=len(res.get("hits", [])),
                              latency_ms=latency_ms, mode=res.get("mode", "mcp"))
        return res
    if name == "recall_stats":
        return public_fallback.call_with_fallback("recall_stats", {}, recall_api.recall_stats)
    return public_fallback.call_with_fallback(
        "recall_contribute", args, lambda: contribute_with_guard(dict(args)))


def contribute_with_guard(args: dict) -> dict:
    """recall_contribute behind the near-duplicate guard; force=true skips the guard.

    The guard itself talks to Qdrant directly (not through recall_api.recall_contribute), so a
    Tailscale-down connection error here is caught and the guard is skipped rather than raised
    -- the caller (public_fallback.call_with_fallback) still gets a chance to fall back the
    actual contribute call to the public service.  A believed-down Tailscale skips the guard
    call entirely for the same reason: no point waiting out a local retry storm for a dedup
    check the public service does not perform anyway.
    """
    force = args.pop("force", False)
    if not isinstance(force, bool):
        raise FleetRagError("force must be a boolean")
    text = args.get("text")
    guard = None
    in_range = isinstance(text, str) and recall_api.CONTRIB_MIN <= len(text.strip()) <= recall_api.CONTRIB_MAX
    if not force and in_range and public_fallback.guard_may_run():
        try:
            cfg = recall_api.get_config(need_write=False)
            guard = contribute_guard.near_duplicate(cfg, recall_api.Qdrant(cfg), text)
        except FleetRagError as e:
            if not public_fallback.is_connection_error(e):
                raise
            guard = None
        if guard is not None and guard["duplicate"]:
            return {"status": "duplicate", "existing": guard["existing"],
                    "threshold": guard["threshold"],
                    "message": contribute_guard.duplicate_message(guard["existing"])
                    + " (pass force=true)"}
    res = recall_api.recall_contribute(**args)
    if guard and guard["candidates"]:
        res["nearest"] = guard["candidates"][0]
    return res


def handle(msg: dict) -> dict | None:
    """Return a JSON-RPC response for a request, or None for a notification."""
    rid = msg.get("id")
    method = msg.get("method")
    is_notification = "id" not in msg

    def ok(result: object) -> dict:
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    def err(code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

    # params must be an object (or absent) before anything reads from it.
    params = msg.get("params")
    if params is None:
        params = {}
    elif not isinstance(params, dict):
        if is_notification:
            log(f"ignoring notification {method!r} with non-object params")
            return None
        return err(-32602, f"params must be an object, not {type(params).__name__}")

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if isinstance(requested, str) and requested else PROTOCOL_VERSION
        return ok({"protocolVersion": version, "capabilities": {"tools": {}},
                   "serverInfo": SERVER_INFO,
                   "instructions": "Search before re-deriving a lesson; contribute after you learn "
                                   "something reusable."})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return err(-32602, "arguments must be an object")
        try:
            result = call_tool(str(name), dict(args))
        except KeyError:
            return err(-32602, f"unknown tool: {name}")
        except FleetRagError as e:
            return ok({"content": [{"type": "text", "text": str(e)}], "isError": True})
        except Exception as e:  # noqa: BLE001 - keep the server alive, report the class only
            log(f"{name} failed: {type(e).__name__}")
            return ok({"content": [{"type": "text", "text": f"{name} failed: {type(e).__name__}"}],
                       "isError": True})
        return ok({"content": [{"type": "text", "text": json.dumps(result)}], "isError": False})
    if is_notification:
        return None
    return err(-32601, f"method not found: {method}")


def serve(stdin=None, stdout=None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
            stdout.write(json.dumps(resp) + "\n")
            stdout.flush()
            continue
        batch = msg if isinstance(msg, list) else [msg]
        out = []
        for m in batch:
            if not isinstance(m, dict):
                out.append({"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32600, "message": "invalid request"}})
                continue
            try:
                resp = handle(m)
            except Exception as e:  # noqa: BLE001 - one bad message must never end the loop
                log(f"internal error handling {m.get('method')!r}: {type(e).__name__}")
                if "id" not in m:
                    continue
                resp = {"jsonrpc": "2.0", "id": m.get("id"),
                        "error": {"code": -32603, "message": f"internal error: {type(e).__name__}"}}
            if resp is not None:
                out.append(resp)
        if not out:
            continue
        stdout.write(json.dumps(out if isinstance(msg, list) else out[0]) + "\n")
        stdout.flush()


def main() -> int:
    if os.environ.get("FLEET_RECALL_FAKE") == "1":
        recall_api.install_fake_backend()
        log("FAKE backend (FLEET_RECALL_FAKE=1)")
    try:
        serve()
    except KeyboardInterrupt:
        pass
    except BrokenPipeError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
