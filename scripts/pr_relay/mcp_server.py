"""MCP wrapper for the PR relay:  seats call tools, never see the GitHub token.

A minimal stdio JSON-RPC server implementing the MCP handshake subset seats
need:  initialize, notifications/initialized, tools/list and tools/call.  The
wrapper holds only the seat's own bearer token;  every call is forwarded to
the relay over HTTP, where policy, rate caps and the audit log apply.

Config:  PR_RELAY_URL (default http://127.0.0.1:8471), PR_RELAY_SEAT_TOKEN.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2025-06-18"
MAX_LINE = 1024 * 1024
TIMEOUT_SECONDS = 30

TOOLS = [
    {
        "name": "open_pr",
        "description": "Open a pull request as the relay's GitHub identity",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "owner/name"},
                "base": {"type": "string"},
                "head": {"type": "string"},
                "title": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["repo", "base", "head", "title", "body"],
        },
    },
    {
        "name": "edit_pr",
        "description": "Edit a pull request's title and/or body",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "number": {"type": "integer"},
                "title": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["repo", "number"],
        },
    },
    {
        "name": "reply_thread",
        "description": "Reply inline to a pull request review thread",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "number": {"type": "integer"},
                "comment_id": {"type": "integer"},
                "body": {"type": "string"},
            },
            "required": ["repo", "number", "comment_id", "body"],
        },
    },
    {
        "name": "comment_pr",
        "description": "Post a top-level comment on a pull request",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "number": {"type": "integer"},
                "body": {"type": "string"},
            },
            "required": ["repo", "number", "body"],
        },
    },
]

TOOL_NAMES = {t["name"] for t in TOOLS}


class RelayMcp:
    """Transport-free core, so tests drive it without stdio."""

    def __init__(self, relay_url: str, seat_token: str, opener=None):
        if not seat_token:
            raise ValueError("PR_RELAY_SEAT_TOKEN is required")
        self._url = relay_url.rstrip("/")
        self._token = seat_token
        self._opener = opener or urllib.request.urlopen

    def handle(self, message: dict) -> dict | None:
        method = message.get("method")
        if method == "notifications/initialized":
            return None
        if method == "initialize":
            return _result(
                message,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "swu-pr-relay", "version": "1"},
                },
            )
        if method == "tools/list":
            return _result(message, {"tools": TOOLS})
        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            if name not in TOOL_NAMES:
                return _error(message, -32602, f"unknown tool {name!r}")
            args = params.get("arguments")
            if not isinstance(args, dict):
                return _error(message, -32602, "arguments must be an object")
            return self._call_tool(message, name, args)
        if method == "ping":
            return _result(message, {})
        return _error(message, -32601, f"unknown method {method!r}")

    def _call_tool(self, message: dict, name: str, args: dict) -> dict:
        req = urllib.request.Request(
            f"{self._url}/v1/{name}",
            method="POST",
            data=json.dumps(args).encode(),
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener(req, timeout=TIMEOUT_SECONDS) as resp:
                payload = json.loads(resp.read(MAX_LINE))
        except urllib.error.HTTPError as exc:
            preview = exc.read(512).decode("utf-8", "replace")
            return _result(
                message,
                {
                    "content": [{"type": "text", "text": f"relay refused ({exc.code}): {preview}"}],
                    "isError": True,
                },
            )
        except (OSError, ValueError):
            return _result(
                message,
                {
                    "content": [{"type": "text", "text": "relay unreachable or reply invalid"}],
                    "isError": True,
                },
            )
        return _result(message, {"content": [{"type": "text", "text": json.dumps(payload)}]})


def _result(message: dict, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": message.get("id"), "result": result}


def _error(message: dict, code: int, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": code, "message": text}}


def main() -> None:
    import sys

    server = RelayMcp(
        os.environ.get("PR_RELAY_URL", "http://127.0.0.1:8471"),
        os.environ.get("PR_RELAY_SEAT_TOKEN", ""),
    )
    for line in sys.stdin:
        if len(line) > MAX_LINE:
            continue
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(message, dict):
            continue
        reply = server.handle(message)
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
