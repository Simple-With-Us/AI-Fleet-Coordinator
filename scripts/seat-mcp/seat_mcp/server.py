"""Streamable HTTP MCP server on 127.0.0.1 only.

Supports initialize / tools/* (2025-03-26 and 2025-11-25) and server/discover
(2026-07-28).  No public hostname.  No tunnel.
"""

from __future__ import annotations

import json
import sys
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from . import __version__
from .auth import access_authenticated, bearer_ok, load_token, origin_ok
from .config import BIND_HOST, BIND_PORT, MCP_PATH
from .recall_bridge import recall_contribute, recall_search, recall_stats
from .seats import SeatError
from .tools import TOOL_IMPL, tool_schemas

RECALL_PATHS = ("/recall/stats", "/recall/search", "/recall/contribute")

JsonDict = dict[str, Any]
PROTOCOL_VERSIONS = ("2025-03-26", "2025-11-25", "2026-07-28")
SERVER_NAME = "seat-mcp"


def _json_bytes(payload: JsonDict) -> bytes:
    return (json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8")


def handle_rpc(msg: JsonDict) -> JsonDict | None:
    """Return a JSON-RPC response, or None for notifications."""
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if req_id is None and method:
        # Notification.  No response body.
        return None
    if not method:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32600, "message": "invalid request"},
        }
    try:
        result = _dispatch(str(method), params)
    except FileNotFoundError as exc:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32004, "message": str(exc)}}
    except SeatError as exc:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32001, "message": str(exc)}}
    except Exception as exc:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32000, "message": str(exc), "data": traceback.format_exc()[-800:]},
        }
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _dispatch(method: str, params: JsonDict) -> JsonDict:
    if method == "initialize":
        requested = str(params.get("protocolVersion") or "2025-11-25")
        version = requested if requested in PROTOCOL_VERSIONS else "2025-11-25"
        return {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
            "instructions": (
                "Async Mac seat jobs.  Call seat_launch, then seat_status, "
                "then seat_reply or seat_result.  DeepSeek is read-only.  "
                "For live Grok TUI chats:  grok_sessions_list, then "
                "grok_session_prompt (seat grok-tui) or seat_launch with "
                "seat=grok-tui and opts.sessionId.  Fleet RAG: MCP tools "
                "recall_search / recall_stats / recall_contribute on this "
                "same /mcp, or REST GET /recall/stats, POST /recall/search, "
                "POST /recall/contribute.  Public hop is "
                "https://agents.jays.services (Access + Bearer)."
            ),
        }
    if method == "server/discover":
        return {
            "protocolVersions": list(PROTOCOL_VERSIONS),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
            "tools": tool_schemas(),
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": tool_schemas()}
    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        impl = TOOL_IMPL.get(name)
        if impl is None:
            raise SeatError("unknown tool:  %s" % name)
        data = impl(arguments)
        return {
            "content": [{"type": "text", "text": json.dumps(data, ensure_ascii=True)}],
            "structuredContent": data,
            "isError": False,
        }
    if method.startswith("notifications/"):
        return {}
    raise SeatError("unknown method:  %s" % method)


class SeatHandler(BaseHTTPRequestHandler):
    server_version = "seat-mcp/1.0"
    token = ""

    def log_message(self, fmt: str, *args: Any) -> None:
        # Do not log Authorization or query strings that might carry a token.
        raw_path = urlparse(self.path).path
        # A /hooks/<id>/<secret> path IS the credential:  never log it (AFC-359).
        path = "/hooks/<redacted>" if raw_path.startswith("/hooks/") else raw_path
        try:
            msg = fmt % args
            if path != raw_path:
                msg = msg.replace(self.path, path).replace(raw_path, path)
        except Exception:
            msg = "log"
        if "Bearer" in msg or "SEAT_MCP" in msg:
            msg = "redacted"
        try:
            sys.stderr.write("%s - %s %s %s\n" % (self.address_string(), self.command, path, msg))
        except Exception:
            pass

    def _send(
        self, code: int, body: bytes, content_type: str, extra: list[tuple[str, str]] | None = None
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, val in extra or []:
            self.send_header(key, val)
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def _forbidden_origin(self) -> bool:
        origin = self.headers.get("Origin", "")
        if origin_ok(origin, access_ok=access_authenticated(self.headers)):
            return False
        payload = _json_bytes(
            {"jsonrpc": "2.0", "error": {"code": -32002, "message": "invalid origin"}}
        )
        self._send(403, payload, "application/json")
        return True

    def _unauthorized(self) -> None:
        payload = _json_bytes(
            {"jsonrpc": "2.0", "error": {"code": -32001, "message": "unauthorized"}}
        )
        self._send(401, payload, "application/json", [("WWW-Authenticate", "Bearer")])

    def _auth_ok(self) -> bool:
        header = self.headers.get("Authorization", "")
        return bearer_ok(header, self.token)

    def do_GET(self) -> None:
        if self._forbidden_origin():
            return
        path = urlparse(self.path).path
        if path in {"/health", "/"}:
            body = _json_bytes(
                {
                    "ok": True,
                    "name": SERVER_NAME,
                    "listen": "%s:%s" % (BIND_HOST, BIND_PORT),
                    "mcp": MCP_PATH,
                    "recall": list(RECALL_PATHS),
                }
            )
            self._send(200, body, "application/json")
            return
        if path == "/recall/stats":
            if not self._auth_ok():
                self._unauthorized()
                return
            self._recall_rest("stats", {})
            return
        if path == MCP_PATH:
            if not self._auth_ok():
                self._unauthorized()
                return
            # No standalone SSE in v1.  Spec allows 405.
            self._send(405, b"sse not offered\n", "text/plain; charset=utf-8")
            return
        if path.startswith("/hooks/"):
            self._proxy_to_webhook_ingress()
            return
        self._send(404, b"not found\n", "text/plain; charset=utf-8")

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_DELETE(self) -> None:
        if self._forbidden_origin():
            return
        path = urlparse(self.path).path
        if path != MCP_PATH:
            self._send(404, b"not found\n", "text/plain; charset=utf-8")
            return
        if not self._auth_ok():
            self._unauthorized()
            return
        self._send(405, b"sessions are not server-owned\n", "text/plain; charset=utf-8")

    def _read_json_object(self) -> JsonDict | None:
        length_s = self.headers.get("Content-Length", "0")
        try:
            length = int(length_s)
        except ValueError:
            length = 0
        if length < 0 or length > 2_000_000:
            self._send(413, b"payload too large\n", "text/plain; charset=utf-8")
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            msg = json.loads(raw.decode("utf-8") or "{}")
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, _json_bytes({"ok": False, "error": "parse error"}), "application/json")
            return None
        if not isinstance(msg, dict):
            self._send(
                400, _json_bytes({"ok": False, "error": "invalid request"}), "application/json"
            )
            return None
        return msg

    def _recall_rest(self, kind: str, arguments: JsonDict) -> None:
        try:
            if kind == "stats":
                data = recall_stats(arguments)
            elif kind == "search":
                data = recall_search(arguments)
            else:
                data = recall_contribute(arguments)
        except SeatError as exc:
            self._send(400, _json_bytes({"ok": False, "error": str(exc)}), "application/json")
            return
        except Exception as exc:
            self._send(
                500,
                _json_bytes({"ok": False, "error": str(exc)}),
                "application/json",
            )
            return
        self._send(200, _json_bytes({"ok": True, **data}), "application/json")

    def _proxy_to_webhook_ingress(self) -> None:
        import urllib.error
        import urllib.request

        length_s = self.headers.get("Content-Length", "0")
        try:
            length = int(length_s)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else None
        target_url = f"http://127.0.0.1:8800{self.path}"
        headers = {}
        for key, val in self.headers.items():
            if key.lower() not in {"host", "connection"}:
                headers[key] = val
        req = urllib.request.Request(target_url, data=body, headers=headers, method=self.command)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp_body = resp.read()
                content_type = resp.headers.get("Content-Type", "application/json")
                self._send(resp.status, resp_body, content_type)
        except urllib.error.HTTPError as exc:
            err_body = exc.read()
            content_type = exc.headers.get("Content-Type", "application/json")
            self._send(exc.code, err_body, content_type)
        except Exception as exc:
            self._send(
                502,
                _json_bytes({"ok": False, "error": f"Webhook proxy failed: {exc}"}),
                "application/json",
            )

    def do_POST(self) -> None:
        if self._forbidden_origin():
            return
        path = urlparse(self.path).path
        if path.startswith("/hooks/"):
            self._proxy_to_webhook_ingress()
            return
        if path in {"/recall/search", "/recall/contribute", "/recall/stats"}:
            if not self._auth_ok():
                self._unauthorized()
                return
            msg = self._read_json_object()
            if msg is None:
                return
            kind = path.rsplit("/", 1)[-1]
            self._recall_rest(kind, msg)
            return
        if path != MCP_PATH:
            self._send(404, b"not found\n", "text/plain; charset=utf-8")
            return
        if not self._auth_ok():
            self._unauthorized()
            return
        length_s = self.headers.get("Content-Length", "0")
        try:
            length = int(length_s)
        except ValueError:
            length = 0
        if length < 0 or length > 2_000_000:
            self._send(413, b"payload too large\n", "text/plain; charset=utf-8")
            return
        raw = self.rfile.read(length) if length else b""
        try:
            msg = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(
                400,
                _json_bytes(
                    {"jsonrpc": "2.0", "error": {"code": -32700, "message": "parse error"}}
                ),
                "application/json",
            )
            return
        if not isinstance(msg, dict):
            self._send(
                400,
                _json_bytes(
                    {"jsonrpc": "2.0", "error": {"code": -32600, "message": "invalid request"}}
                ),
                "application/json",
            )
            return
        header_method = self.headers.get("Mcp-Method") or self.headers.get("MCP-Method")
        if header_method and msg.get("method") and header_method != msg.get("method"):
            self._send(
                400,
                _json_bytes(
                    {"jsonrpc": "2.0", "error": {"code": -32600, "message": "Mcp-Method mismatch"}}
                ),
                "application/json",
            )
            return
        proto = self.headers.get("MCP-Protocol-Version") or self.headers.get("Mcp-Protocol-Version")
        if proto and proto not in PROTOCOL_VERSIONS:
            self._send(
                400,
                _json_bytes(
                    {
                        "jsonrpc": "2.0",
                        "error": {"code": -32600, "message": "unsupported MCP-Protocol-Version"},
                    }
                ),
                "application/json",
            )
            return
        result = handle_rpc(msg)
        extra: list[tuple[str, str]] = []
        if msg.get("method") == "initialize" and result and "result" in result:
            extra.append(("MCP-Session-Id", uuid.uuid4().hex))
        if result is None:
            self._send(202, b"", "application/json", extra)
            return
        self._send(200, _json_bytes(result), "application/json", extra)


def serve(host: str = BIND_HOST, port: int = BIND_PORT) -> None:
    token = load_token()
    SeatHandler.token = token
    httpd = ThreadingHTTPServer((host, port), SeatHandler)
    httpd.allow_reuse_address = True
    print("seat-mcp listen %s:%s%s" % (host, port, MCP_PATH), flush=True)
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
