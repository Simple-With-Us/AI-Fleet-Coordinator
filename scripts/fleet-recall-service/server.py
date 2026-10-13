#!/usr/bin/env python3
"""fleet-recall-service -- Hetzner-side HTTP front for the fleet-agents corpus.

Stdlib only.  One ThreadingHTTPServer that imports fleet_rag.recall_api and exposes:

  GET  /health               no auth   {ok, version, collection, points, backend_ok}
  GET  /recall/stats         bearer    recall_stats()
  POST /recall/search        bearer    recall_search(**body)
  POST /recall/contribute    bearer    recall_contribute(**body)   (seat REQUIRED)
  POST /mcp                  bearer    streamable-HTTP JSON-RPC: initialize,
                                       notifications/*, ping, tools/list, tools/call,
                                       server/discover (same framing as seat-mcp)

Auth is `Authorization: Bearer <RECALL_API_TOKEN>` on everything except /health, compared in
constant time over bytes (a non-ASCII bearer is just wrong, never a traceback).  Configuration
Logs go
to stdout and never include bodies, tokens, or query strings.

Configuration is Infisical-first: on startup the service loads its managed settings
(QDRANT_*, TEI_*, RECALL_*) from the "AI Fleet Coordinator" Infisical project into an
in-memory cache (see INFISICAL.md at the repo root), exports the backend keys into the
process environment for the fleet_rag library, and refreshes the cache in the background
(daemon thread, SIGHUP, or POST /admin/reload-settings).  All runtime reads come from the
cache -- no per-request Infisical fetch exists anywhere in the request path.  When no
Infisical machine identity is configured (local dev), the managed keys are read from the
process environment exactly as before.

Keep-alive discipline: the server speaks HTTP/1.1 and sits behind Traefik, which pools
connections.  Any reply sent BEFORE the request body was consumed (401, 413, 404/405 on a POST,
and every GET/HEAD, which never reads a body) therefore drains a small body and closes the
connection (`Connection: close`), so the unread body can never be parsed as the next request on
the pooled socket.  The drain runs once per request (a second call is a no-op), so a 200 on
GET /health with a stray body and a 401 on GET /recall/stats with one both close cleanly.

Socket timeout: every connection carries RecallHandler.timeout (RECALL_SOCKET_TIMEOUT seconds,
default 15).  A client that declares a body and then stalls -- Content-Length 500000 with ten
bytes sent -- no longer parks a handler thread forever: the drain gives up on the timeout and
the pending reply (401/404/405) still goes out with `Connection: close`; a stalled body on an
authenticated POST is answered 408 with close.  Neither path reaches handle_error, so there is
no traceback.  An idle keep-alive connection is closed by http.server after the same timeout.

JSON-RPC: a message without an `id`, or with `id: null`, is a notification (202, no body).

The service exists so cloud seats and phones reach recall even while the Mac is asleep: it runs
on the Coolify box next to Qdrant and TEI (both reachable over the host's Tailscale address).
Set RECALL_FAKE=1 to serve recall_api's in-process fake corpus (tests, smoke checks).
"""
from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

# The service dir sits next to the fleet_rag package (scripts/ in the repo, /app in the
# container), so the parent directory is the import root.
_HERE = os.path.dirname(os.path.realpath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fleet_rag import __version__ as _RAG_VERSION  # noqa: E402
from fleet_rag import recall_api  # noqa: E402
from fleet_rag import infisical_settings  # noqa: E402
from fleet_rag.infisical_settings import SettingsError as _SettingsError  # noqa: E402
from fleet_rag.core import FleetRagError  # noqa: E402

SERVICE_VERSION = "1.0.0"
SERVER_NAME = "fleet-recall-service"
MCP_PATH = "/mcp"
RECALL_PATHS = ("/recall/stats", "/recall/search", "/recall/contribute")
ADMIN_SETTINGS_PATH = "/admin/settings"
ADMIN_RELOAD_PATH = "/admin/reload-settings"
PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25", "2026-07-28")
DEFAULT_PROTOCOL = "2025-11-25"
MAX_BODY = 2_000_000
HEALTH_CACHE_S = 30.0
DEFAULT_SOCKET_TIMEOUT = 15.0

REQUIRED_ENV = ("QDRANT_URL", "QDRANT_API_KEY", "QDRANT_FLEET_COLLECTION", "TEI_URL", "TEI_API_KEY")
OPTIONAL_ENV = ("QDRANT_READONLY_API_KEY", "TEI_EMBED_MODEL")

# Every key the service owns.  Values live in Infisical (project below); the process
# environment is only the bootstrap fallback when no Infisical identity is configured.
MANAGED_KEYS = REQUIRED_ENV + OPTIONAL_ENV + (
    "RECALL_API_TOKEN", "RECALL_ADMIN_TOKEN", "RECALL_SOCKET_TIMEOUT",
    "SETTINGS_REFRESH_SECONDS", "HOST", "PORT",
)
# Secret keys are never returned by value from the admin surface (INFISICAL.md).
SECRET_KEYS = frozenset({
    "QDRANT_API_KEY", "QDRANT_READONLY_API_KEY", "TEI_API_KEY",
    "RECALL_API_TOKEN", "RECALL_ADMIN_TOKEN",
})
# Backend keys the fleet_rag library reads from the environment; server.py mirrors the
# Infisical cache into os.environ for them at startup and on every refresh.
BACKEND_ENV_KEYS = REQUIRED_ENV + OPTIONAL_ENV
# The "AI Fleet Coordinator" Infisical project.  FLEET_RAG_INFISICAL_PROJECT overrides it
# (fleet convention, see fleet_rag/core.py).  The environment is prod, the only one Infisical
# keeps; INFISICAL_ENVIRONMENT may name it but any other slug is refused (INFISICAL.md).
AFC_INFISICAL_PROJECT = "9bf7417a-fbbb-42ca-870c-2b45207233f5"

JsonDict = dict[str, Any]

# --------------------------------------------------------------------------- tool contract

TOOLS: list[JsonDict] = [
    {
        "name": "recall_search",
        "description": (
            "Search the fleet's shared knowledge corpus (lessons, owner preferences, infrastructure "
            "facts, decisions, runbooks, board findings, effort logs, Apple Notes, fleet docs).  Use it "
            "BEFORE re-deriving a lesson, guessing an owner preference, or debugging infrastructure "
            "another seat has already documented.  Hybrid dense + keyword search; filter by category, "
            "app, source, seat, or recency.  Read-only."),
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
                "per_doc": {"type": "integer", "minimum": 1, "maximum": recall_api.PER_DOC_MAX,
                            "default": 1, "description": "Chunks to keep per matching document."},
                "rerank": {"type": "boolean", "default": True,
                           "description": "Cross-encoder rerank of the fused candidates."},
                "prefer_lessons": {"type": "boolean", "default": True,
                                   "description": "Boost a matching agent-contribution lesson into "
                                                  "the fused results."},
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
            "refused.  seat is REQUIRED on this surface (a cloud caller has no AGENT_SEAT)."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "minLength": recall_api.CONTRIB_MIN,
                         "maxLength": recall_api.CONTRIB_MAX},
                "category": {"type": "string", "enum": list(recall_api.CONTRIB_CATEGORIES)},
                "app": {"type": "string", "default": "fleet"},
                "seat": {"type": "string", "description": "Your uppercase seat tag (required)."},
                "title": {"type": "string"},
                "url": {"type": "string", "description": "Source link (PR, board item, doc), optional."},
            },
            "required": ["text", "category", "seat"],
        },
    },
]

_ALLOWED_ARGS = {t["name"]: set(t["inputSchema"]["properties"]) for t in TOOLS}
_REQUIRED_ARGS = {t["name"]: list(t["inputSchema"].get("required", [])) for t in TOOLS}


def _int(args: JsonDict, key: str) -> None:
    val = args.get(key)
    if key not in args or val is None or val == "":
        args.pop(key, None)
        return
    if isinstance(val, bool):
        raise FleetRagError(f"{key} must be an integer")
    try:
        args[key] = int(val)
    except (TypeError, ValueError):
        raise FleetRagError(f"{key} must be an integer") from None


def _bool(args: JsonDict, key: str) -> None:
    """In place: drop an absent/None/"" value; otherwise require an actual JSON boolean -- a
    JSON API should never guess whether the string "false" means false."""
    val = args.get(key)
    if key not in args or val is None or val == "":
        args.pop(key, None)
        return
    if not isinstance(val, bool):
        raise FleetRagError(f"{key} must be a boolean")


def call_tool(name: str, args: JsonDict) -> JsonDict:
    """Validate the argument set and dispatch to recall_api.  KeyError for an unknown tool."""
    if name not in _ALLOWED_ARGS:
        raise KeyError(name)
    if not isinstance(args, dict):
        raise FleetRagError("arguments must be an object")
    args = dict(args)
    unknown = set(args) - _ALLOWED_ARGS[name]
    if unknown:
        raise FleetRagError("unknown argument(s): " + ", ".join(sorted(unknown)))
    missing = [k for k in _REQUIRED_ARGS[name] if args.get(k) is None and k != "seat"]
    if missing:
        raise FleetRagError("missing required argument(s): " + ", ".join(missing))
    if name == "recall_search":
        _int(args, "limit")
        _int(args, "since_days")
        _int(args, "per_doc")
        _bool(args, "rerank")
        _bool(args, "prefer_lessons")
        return recall_api.recall_search(**args)
    if name == "recall_stats":
        return recall_api.recall_stats()
    seat = args.get("seat")
    if not isinstance(seat, str) or not seat.strip():
        # Never fall back to the service process's own AGENT_SEAT for a remote caller.
        raise FleetRagError("seat is required on this surface (cloud callers have no AGENT_SEAT); "
                            "pass your uppercase seat tag, e.g. GROK, CURSOR, CLAUDE")
    return recall_api.recall_contribute(**args)


# --------------------------------------------------------------------------- health

_health_lock = threading.Lock()
_health_cache: dict[str, Any] = {"at": 0.0, "value": None}


def _backend_snapshot() -> JsonDict:
    """Collection name and live point count, cached for HEALTH_CACHE_S.  Never raises."""
    now = time.monotonic()
    with _health_lock:
        if _health_cache["value"] is not None and now - _health_cache["at"] < HEALTH_CACHE_S:
            return dict(_health_cache["value"])
    snap: JsonDict = {"collection": infisical_settings.get("QDRANT_FLEET_COLLECTION") or None,
                      "points": None, "backend_ok": False}
    try:
        cfg = recall_api.get_config(need_write=False)
        q = recall_api.Qdrant(cfg)
        info = q.info()
        snap = {"collection": q.collection, "points": int(info.get("points_count", 0)), "backend_ok": True}
    except Exception as e:  # noqa: BLE001 - class only, never the message (it may name hosts)
        snap["error"] = type(e).__name__
    with _health_lock:
        _health_cache.update(at=now, value=dict(snap))
    return snap


def reset_health_cache() -> None:
    with _health_lock:
        _health_cache.update(at=0.0, value=None)


def health_payload() -> JsonDict:
    return {"ok": True, "name": SERVER_NAME, "version": SERVICE_VERSION, "fleet_rag": _RAG_VERSION,
            **_backend_snapshot(), "mcp": MCP_PATH, "recall": list(RECALL_PATHS)}


# --------------------------------------------------------------------------- MCP

def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8")


def _rpc_error(rid: Any, code: int, message: str) -> JsonDict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def handle_rpc(msg: JsonDict) -> JsonDict | None:
    """One JSON-RPC message -> response dict, or None for a notification.

    A message whose `id` is absent or null is a notification: the method still runs (per the
    spec) but nothing is returned, so the HTTP layer answers 202 with no body.
    """
    rid = msg.get("id")
    response = _dispatch_rpc(msg, rid)
    return None if rid is None else response


def _dispatch_rpc(msg: JsonDict, rid: Any) -> JsonDict | None:
    method = msg.get("method")
    params = msg.get("params")
    if params is None:
        params = {}
    elif not isinstance(params, dict):
        return _rpc_error(rid, -32602, "params must be an object")
    if not isinstance(method, str) or not method:
        return _rpc_error(rid, -32600, "invalid request")

    def ok(result: Any) -> JsonDict:
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if isinstance(requested, str) and requested in PROTOCOL_VERSIONS else DEFAULT_PROTOCOL
        return ok({
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVICE_VERSION},
            "instructions": (
                "Fleet RAG over the fleet-agents corpus.  Search before re-deriving a lesson; "
                "contribute after you learn something reusable.  Tools: recall_search, "
                "recall_stats, recall_contribute (seat required).  REST twins: GET /recall/stats, "
                "POST /recall/search, POST /recall/contribute with the same bearer."),
        })
    if method == "server/discover":
        return ok({"protocolVersions": list(PROTOCOL_VERSIONS),
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": SERVER_NAME, "version": SERVICE_VERSION},
                   "tools": TOOLS})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return _rpc_error(rid, -32602, "arguments must be an object")
        try:
            data = call_tool(str(name), args)
        except KeyError:
            return _rpc_error(rid, -32602, f"unknown tool: {name}")
        except FleetRagError as e:
            return ok({"content": [{"type": "text", "text": str(e)}], "isError": True})
        except Exception as e:  # noqa: BLE001 - keep serving; report the class only
            log(f"tools/call {name} failed: {type(e).__name__}")
            return ok({"content": [{"type": "text", "text": f"{name} failed: {type(e).__name__}"}],
                       "isError": True})
        return ok({"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=True)}],
                   "structuredContent": data, "isError": False})
    if method.startswith("notifications/"):
        return ok({})                      # a notification with an id gets an empty result
    return _rpc_error(rid, -32601, f"method not found: {method}")


# --------------------------------------------------------------------------- auth / logging

def bearer_ok(header: str | None, token: str) -> bool:
    """Constant-time check of `Authorization: Bearer <token>`.

    Compared as bytes: http.server decodes header values as latin-1, and hmac.compare_digest on
    str raises TypeError for anything non-ASCII, which would turn a garbage bearer into a
    traceback and a dropped connection instead of a 401.
    """
    if not header or not token:
        return False
    parts = header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False
    got = parts[1].strip().encode("latin-1", "replace")
    want = token.encode("utf-8")
    if len(got) != len(want):
        hmac.compare_digest(want, want)
        return False
    return hmac.compare_digest(got, want)


def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    sys.stdout.write(f"{ts}Z {SERVER_NAME}: {msg}\n")
    sys.stdout.flush()


def socket_timeout(env: dict[str, str] | None = None) -> float:
    """Per-connection socket timeout in seconds: RECALL_SOCKET_TIMEOUT, else DEFAULT_SOCKET_TIMEOUT.

    Reads the Infisical-backed settings cache first (which already includes the process
    environment when no Infisical identity is configured), then the environment directly
    when settings are not initialized (tests).  An explicit `env` dict keeps its old
    meaning for callers that pin a mapping.  A missing, empty, unparsable, or
    non-positive value falls back to the default so a typo can never disable the
    timeout (None would mean "block forever").
    """
    if env is not None:
        raw = env.get("RECALL_SOCKET_TIMEOUT", "")
    else:
        raw = infisical_settings.get("RECALL_SOCKET_TIMEOUT") or os.environ.get("RECALL_SOCKET_TIMEOUT", "")
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_SOCKET_TIMEOUT
    return val if val > 0 else DEFAULT_SOCKET_TIMEOUT


# --------------------------------------------------------------------------- handler

class RecallHandler(BaseHTTPRequestHandler):
    server_version = f"{SERVER_NAME}/{SERVICE_VERSION}"
    sys_version = ""
    token = ""
    protocol_version = "HTTP/1.1"
    # StreamRequestHandler.setup() applies this to the socket, so a stalled body (or an idle
    # keep-alive connection) can never park a handler thread forever.
    timeout: float | None = socket_timeout()
    _body_drained = False

    # -- plumbing
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: D401 - BaseHTTPRequestHandler hook
        # Path only (no query string), no headers, no bodies.
        pass

    def handle_one_request(self) -> None:
        # One handler instance serves every request on a keep-alive connection, so the
        # "body already drained" mark must be reset per request, not per connection.
        self._body_drained = False
        super().handle_one_request()

    def _send(self, code: int, body: bytes, ctype: str = "application/json",
              extra: list[tuple[str, str]] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in extra or []:
            self.send_header(k, v)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)
        log(f"{self.command} {urlparse(self.path).path} {code}")

    def _content_length(self) -> int | None:
        """Declared Content-Length, or None when absent / unparsable."""
        raw = self.headers.get("Content-Length")
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    def _discard_unread_body(self) -> None:
        """Called before any reply that is sent without consuming the request body.

        Drains a body of up to MAX_BODY bytes so the reply is delivered cleanly, and marks the
        connection for close whenever a body (or a chunked one) may still be on the wire, so a
        pooled Traefik connection can never hand the leftover bytes to us as the next request.
        Idempotent per request: do_GET drains unconditionally, and the 401/404/405 helpers it
        then calls must not block on a second read of a body that is already gone.  A drain
        that hits the socket timeout gives up quietly; the connection is closing anyway.
        """
        if self._body_drained:
            return
        self._body_drained = True
        chunked = bool(self.headers.get("Transfer-Encoding"))
        if self.headers.get("Content-Length") is None and not chunked:
            return                                  # no body on the wire
        length = self._content_length()             # None here means unparsable: close
        if length == 0 and not chunked:
            return
        self.close_connection = True
        if length is not None and 0 < length <= MAX_BODY:
            try:
                remaining = length
                while remaining > 0:
                    chunk = self.rfile.read(min(remaining, 65536))
                    if not chunk:
                        break
                    remaining -= len(chunk)
            except TimeoutError:
                log(f"{self.command} {urlparse(self.path).path} body drain timed out; closing")
            except OSError:
                pass

    def _unauthorized(self) -> None:
        self._discard_unread_body()
        self.close_connection = True       # never keep an unauthenticated connection open
        self._send(401, _json_bytes({"ok": False, "error": "unauthorized"}),
                   extra=[("WWW-Authenticate", "Bearer")])

    def _auth_ok(self) -> bool:
        return bearer_ok(self.headers.get("Authorization"), self.token)

    # -- admin (Infisical write-through surface; admin token only, never the seat token)
    def _admin_auth_ok(self) -> bool:
        admin = (infisical_settings.get("RECALL_ADMIN_TOKEN") or "").strip()
        return bool(admin) and bearer_ok(self.headers.get("Authorization"), admin)

    def _admin_auth(self) -> bool:
        """False when the request is already answered (403 unconfigured / 401)."""
        if not infisical_settings.present("RECALL_ADMIN_TOKEN"):
            self._discard_unread_body()
            self.close_connection = True
            self._send(403, _json_bytes({"ok": False,
                                         "error": "admin token not configured; set RECALL_ADMIN_TOKEN "
                                                  "in Infisical (see INFISICAL.md)"}))
            return False
        if not self._admin_auth_ok():
            self._unauthorized()
            return False
        return True

    def _admin_settings(self) -> None:
        if not self._admin_auth():
            return
        self._send(200, _json_bytes({"ok": True, "settings": infisical_settings.admin_listing()}))

    def _admin_set_setting(self) -> None:
        if not self._admin_auth():
            return
        msg = self._read_json_object()
        if msg is None:
            return
        key, value = msg.get("key"), msg.get("value")
        if not isinstance(key, str) or key not in MANAGED_KEYS:
            self._send(400, _json_bytes({"ok": False, "error": "unknown or unmanaged setting"}))
            return
        if not isinstance(value, str):
            self._send(400, _json_bytes({"ok": False, "error": "value must be a string"}))
            return
        try:
            infisical_settings.set(key, value)
        except _SettingsError as e:
            # Write-through failed: the Infisical write did not land, so the save fails
            # here and the cache was NOT updated.
            self._send(502, _json_bytes({"ok": False, "error": str(e)}))
            return
        self._send(200, _json_bytes({"ok": True, "key": key}))

    def _admin_reload(self) -> None:
        if not self._admin_auth():
            return
        self._discard_unread_body()
        refreshed = infisical_settings.refresh_now()
        self._send(200, _json_bytes({"ok": True, "refreshed": refreshed}))

    def _read_body(self) -> bytes | None:
        if self.headers.get("Transfer-Encoding"):
            # Chunked bodies are not framed here; refusing without reading means close.
            self.close_connection = True
            self._send(411, _json_bytes({"ok": False, "error": "Content-Length required"}))
            return None
        length = self._content_length()
        if length is None:
            length = 0
        if length < 0 or length > MAX_BODY:
            # The body is not read, so the connection must not be reused.
            self.close_connection = True
            self._send(413, _json_bytes({"ok": False, "error": "payload too large"}))
            return None
        self._body_drained = True                   # consumed (or given up on) below
        if not length:
            return b""
        try:
            raw = self.rfile.read(length)
        except TimeoutError:
            # The client declared a body and stalled; the rest may never come.  Answer and
            # close instead of parking the thread or raising into handle_error.
            self.close_connection = True
            self._send(408, _json_bytes({"ok": False, "error": "request body timed out"}))
            return None
        if len(raw) < length:
            # Peer closed mid-body: nothing sensible to parse, and nothing to keep open.
            self.close_connection = True
            self._send(400, _json_bytes({"ok": False, "error": "incomplete body"}))
            return None
        return raw

    def _read_json_object(self) -> JsonDict | None:
        raw = self._read_body()
        if raw is None:
            return None
        try:
            msg = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, _json_bytes({"ok": False, "error": "parse error"}))
            return None
        if not isinstance(msg, dict):
            self._send(400, _json_bytes({"ok": False, "error": "body must be a JSON object"}))
            return None
        return msg

    def _not_found(self) -> None:
        self._discard_unread_body()
        self._send(404, _json_bytes({"ok": False, "error": "not found"}))

    def _method_not_allowed(self, error: str, allow: str | None = "POST") -> None:
        self._discard_unread_body()
        extra = [("Allow", allow)] if allow else None
        self._send(405, _json_bytes({"ok": False, "error": error}), extra=extra)

    # -- REST
    def _recall_rest(self, name: str, args: JsonDict) -> None:
        try:
            data = call_tool(name, args)
        except FleetRagError as e:
            self._send(400, _json_bytes({"ok": False, "error": str(e)}))
            return
        except Exception as e:  # noqa: BLE001
            log(f"{name} failed: {type(e).__name__}")
            self._send(502, _json_bytes({"ok": False, "error": f"{name} failed: {type(e).__name__}"}))
            return
        self._send(200, _json_bytes({"ok": True, **data}))

    # -- verbs
    def do_GET(self) -> None:
        # No GET/HEAD route reads a body, so drain (and close) up front: a GET /health that
        # carries Content-Length or Transfer-Encoding would otherwise leave its bytes on the
        # pooled socket to be parsed as the next request (501 -- or executed, if the body is
        # itself a well-formed request).
        self._discard_unread_body()
        path = urlparse(self.path).path
        if path in ("/health", "/"):
            self._send(200, _json_bytes(health_payload()))
            return
        if path == ADMIN_SETTINGS_PATH:
            self._admin_settings()
            return
        if path == "/recall/stats":
            if not self._auth_ok():
                self._unauthorized()
                return
            self._recall_rest("recall_stats", {})
            return
        if path == MCP_PATH:
            if not self._auth_ok():
                self._unauthorized()
                return
            # No standalone SSE stream in v1; the spec allows 405.
            self._method_not_allowed("sse not offered")
            return
        if path in RECALL_PATHS:
            self._method_not_allowed("use POST")
            return
        self._not_found()

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_DELETE(self) -> None:
        path = urlparse(self.path).path
        if path != MCP_PATH:
            self._not_found()
            return
        if not self._auth_ok():
            self._unauthorized()
            return
        self._method_not_allowed("sessions are not server-owned", allow=None)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == ADMIN_SETTINGS_PATH:
            self._admin_set_setting()
            return
        if path == ADMIN_RELOAD_PATH:
            self._admin_reload()
            return
        if path in RECALL_PATHS:
            if not self._auth_ok():
                self._unauthorized()
                return
            msg = self._read_json_object()
            if msg is None:
                return
            self._recall_rest("recall_" + path.rsplit("/", 1)[-1], msg)
            return
        if path != MCP_PATH:
            self._not_found()
            return
        if not self._auth_ok():
            self._unauthorized()
            return
        raw = self._read_body()
        if raw is None:
            return
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, _json_bytes(_rpc_error(None, -32700, "parse error")))
            return
        if not isinstance(msg, dict):
            self._send(400, _json_bytes(_rpc_error(None, -32600, "invalid request")))
            return
        header_method = self.headers.get("Mcp-Method") or self.headers.get("MCP-Method")
        if header_method and msg.get("method") and header_method != msg.get("method"):
            self._send(400, _json_bytes(_rpc_error(msg.get("id"), -32600, "Mcp-Method mismatch")))
            return
        proto = self.headers.get("MCP-Protocol-Version") or self.headers.get("Mcp-Protocol-Version")
        if proto and proto not in PROTOCOL_VERSIONS:
            self._send(400, _json_bytes(_rpc_error(msg.get("id"), -32600, "unsupported MCP-Protocol-Version")))
            return
        result = handle_rpc(msg)
        extra: list[tuple[str, str]] = []
        if msg.get("method") == "initialize" and result and "result" in result:
            extra.append(("MCP-Session-Id", uuid.uuid4().hex))
        if result is None:
            self._send(202, b"", extra=extra)
            return
        self._send(200, _json_bytes(result), extra=extra)


# --------------------------------------------------------------------------- lifecycle

def make_server(host: str, port: int, token: str,
                timeout: float | None = None) -> ThreadingHTTPServer:
    """Build (but do not run) the server.  Tests bind 127.0.0.1:0 and serve in a thread.

    `timeout` overrides the per-connection socket timeout (RECALL_SOCKET_TIMEOUT / 15 s).
    """
    attrs: JsonDict = {"token": token}
    if timeout is not None:
        attrs["timeout"] = timeout
    handler = type("BoundRecallHandler", (RecallHandler,), attrs)
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.allow_reuse_address = True
    httpd.daemon_threads = True
    return httpd


def env_report() -> tuple[list[str], list[str]]:
    """(present, missing) required setting NAMES, read from the Infisical-backed cache.
    Values are never returned."""
    present = [k for k in REQUIRED_ENV if infisical_settings.get(k)]
    missing = [k for k in REQUIRED_ENV if not infisical_settings.get(k)]
    return present, missing


def main(argv: list[str] | None = None) -> int:
    # Infisical is the source of truth for every managed key (INFISICAL.md).  The settings
    # module needs the project + environment before anything else; the machine identity
    # (INFISICAL_AUTOMATION_CLIENT_ID/SECRET in the environment, or the handoff file)
    # authorizes the fetch.  With no identity, managed keys fall back to the process
    # environment and the old env-only behavior is preserved.
    # The environment is prod.  An unset or empty INFISICAL_ENVIRONMENT means prod and any
    # other slug is refused, so a stale dev/staging override exits 2 with a clear log line.
    environment = ((os.environ.get("INFISICAL_ENVIRONMENT") or "").strip()
                   or infisical_settings.PROD_ENVIRONMENT)
    try:
        infisical_settings.configure(
            project_id=os.environ.get("FLEET_RAG_INFISICAL_PROJECT") or AFC_INFISICAL_PROJECT,
            environment=environment,
            managed_keys=MANAGED_KEYS, required_keys=("RECALL_API_TOKEN",),
            secret_keys=tuple(SECRET_KEYS))
        infisical_settings.init_settings()
    except _SettingsError as e:
        log(f"settings init failed: {e}")
        return 2
    # The fleet_rag backend reads its QDRANT_*/TEI_* config from the environment; mirror
    # the cache into os.environ now and again on every refresh so the two never diverge.
    infisical_settings.export_env(BACKEND_ENV_KEYS)
    infisical_settings.add_refresh_listener(
        lambda: infisical_settings.export_env(BACKEND_ENV_KEYS))
    infisical_settings.start_refresh()
    try:
        import signal as _signal
        _signal.signal(_signal.SIGHUP,
                       lambda _s, _f: (log("SIGHUP: reloading settings from Infisical"),
                                      infisical_settings.refresh_now()))
    except (AttributeError, OSError, ValueError):
        pass                                       # no SIGHUP on this platform; admin reload only

    token = (infisical_settings.get("RECALL_API_TOKEN") or "").strip()
    if not token:
        log("RECALL_API_TOKEN is not set; refusing to start")
        return 2
    if os.environ.get("RECALL_FAKE") == "1":
        recall_api.install_fake_backend()
        log("serving the FAKE in-process corpus (RECALL_FAKE=1)")
    else:
        present, missing = env_report()
        optional = [k for k in OPTIONAL_ENV if infisical_settings.get(k)]
        log("settings present: " + ", ".join(present + optional))
        if missing:
            log("settings MISSING: " + ", ".join(missing) + " (tools will fail until set)")
    host = infisical_settings.get("HOST") or os.environ.get("HOST", "0.0.0.0")
    try:
        port = int(infisical_settings.get("PORT") or os.environ.get("PORT", "8080"))
    except ValueError:
        port = 8080
    httpd = make_server(host, port, token, timeout=socket_timeout())
    log(f"listening on {host}:{port}  mcp={MCP_PATH}  recall={','.join(RECALL_PATHS)}  "
        f"service={SERVICE_VERSION} fleet_rag={_RAG_VERSION} socket_timeout={httpd.RequestHandlerClass.timeout}s")
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        infisical_settings.stop_refresh()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
