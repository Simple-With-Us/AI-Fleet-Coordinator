"""GR-MCP:  the MCP wrapper for git-relay.  Seats call tools, never see the token.

A minimal stdio JSON-RPC server implementing the MCP handshake subset seats
need:  initialize, notifications/initialized, tools/list and tools/call.  The
wrapper holds only the seat's own bearer token;  every call is forwarded to
git-relay over HTTP, where policy, rate caps and the audit log apply.

Config:  GIT_RELAY_URL (default http://127.0.0.1:8471), GIT_RELAY_SEAT_TOKEN.
"""

from __future__ import annotations

import json
import os
import re
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

# mint_token is deliberately NOT exposed over MCP:  it returns a raw App
# installation token, which would land in the MCP client's stdio transcript and
# any logging around it.  Seats that need a token for git-over-HTTPS call the
# relay's HTTP API directly, where the response goes only to that caller.
TOOLS += [
    {
        "name": "merge_pr",
        "description": "Merge a pull request as the caller's own GitHub App",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "number": {"type": "integer"},
                "method": {"type": "string", "enum": ["squash", "merge", "rebase"]},
            },
            "required": ["repo", "number"],
        },
    },
    {
        "name": "workflow_dispatch",
        "description": "Dispatch a workflow run as the caller's own GitHub App",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "workflow": {"type": "string", "description": "workflow file name or numeric id"},
                "ref": {"type": "string"},
                "inputs": {"type": "object"},
            },
            "required": ["repo", "workflow", "ref"],
        },
    },
    {
        "name": "resolve_thread",
        "description": "Resolve a pull request review thread as the caller's own GitHub App",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "thread_id": {"type": "string", "description": "PRRT_ review-thread id"},
            },
            "required": ["repo", "thread_id"],
        },
    },
]


TOOL_NAMES = {t["name"] for t in TOOLS}

MAX_DISPATCH_INPUTS = 32
MAX_DISPATCH_INPUT_VALUE = 1024


def _validate_args(name: str, args: dict) -> str | None:
    """Enforce the advertised tool schema server-side before anything is forwarded."""
    schema = next(t["inputSchema"] for t in TOOLS if t["name"] == name)
    props = schema.get("properties", {})
    for required in schema.get("required", []):
        if required not in args:
            return f"missing argument {required!r}"
    for key, value in args.items():
        spec = props.get(key)
        if spec is None:
            return f"unexpected argument {key!r}"
        kind = spec.get("type")
        if kind == "string" and not isinstance(value, str):
            return f"argument {key!r} must be a string"
        if kind == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
            return f"argument {key!r} must be an integer"
        if kind == "object" and not isinstance(value, dict):
            return f"argument {key!r} must be an object"
    thread_id = args.get("thread_id")
    if name == "resolve_thread" and not (
        isinstance(thread_id, str) and re.fullmatch(r"PRRT_[A-Za-z0-9_-]{6,128}", thread_id)
    ):
        return "thread_id must be a PRRT_ review-thread id"
    if name == "workflow_dispatch":
        inputs = args.get("inputs")
        if inputs is not None:
            if len(inputs) > MAX_DISPATCH_INPUTS:
                return f"inputs is capped at {MAX_DISPATCH_INPUTS} keys"
            for key, value in inputs.items():
                if not isinstance(key, str) or value is None or isinstance(value, (dict, list)):
                    return "inputs must be a flat object of scalar values"
                if isinstance(value, str) and len(value) > MAX_DISPATCH_INPUT_VALUE:
                    return (
                        f"inputs string values are capped at {MAX_DISPATCH_INPUT_VALUE} characters"
                    )
    return None


class RelayMcp:
    """Transport-free core, so tests drive it without stdio."""

    def __init__(self, relay_url: str, seat_token: str, opener=None):
        if not seat_token:
            raise ValueError("GIT_RELAY_SEAT_TOKEN is required")
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
                    "serverInfo": {"name": "GR-MCP", "version": "1"},
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
            invalid = _validate_args(name, args)
            if invalid:
                return _error(message, -32602, invalid)
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
                raw = resp.read(MAX_LINE + 1)
            if len(raw) > MAX_LINE:
                return _tool_failure(message, "outcome_unknown")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                return _tool_failure(message, "outcome_unknown")
            if payload.get("outcome") in ("unknown", "pending"):
                return _tool_failure(message, "outcome_unknown")
        except urllib.error.HTTPError as exc:
            # Every exposed tool writes.  A server error or replay hold can
            # follow an accepted write, so never call it a definite refusal.
            try:
                raw = exc.read(MAX_LINE + 1)
                error = json.loads(raw) if len(raw) <= MAX_LINE else None
            except (OSError, ValueError):
                error = None
            if isinstance(error, dict) and error.get("outcome") in ("unknown", "pending"):
                return _tool_failure(message, "outcome_unknown", exc.code)
            if isinstance(error, dict) and error.get("retryable") is True:
                return _tool_failure(
                    message,
                    "unavailable",
                    exc.code,
                    retryable=True,
                    retry_after=error.get("retry_after_seconds"),
                )
            if not isinstance(error, dict) or exc.code >= 500:
                return _tool_failure(message, "outcome_unknown", exc.code)
            return _tool_failure(message, "relay_refused", exc.code)
        except (OSError, ValueError):
            return _tool_failure(message, "outcome_unknown")
        return _result(message, {"content": [{"type": "text", "text": json.dumps(payload)}]})


def _tool_failure(message, code, status=None, retryable=False, retry_after=None):
    # Provider prose can contain private details.  Return only stable local
    # guidance and the HTTP status, never the response body or credentials.
    if code == "outcome_unknown":
        detail = (
            "The write outcome is unknown.  Do not retry blindly; check the target state first."
        )
    elif code == "unavailable":
        detail = "Relay reports a retryable pre-write failure.  Nothing was written."
    else:
        detail = "Relay refused the request."
    error = {"code": code, "message": detail, "retryable": retryable}
    if (
        retryable
        and isinstance(retry_after, (int, float))
        and not isinstance(retry_after, bool)
        and 0 <= retry_after <= 86400
    ):
        error["retry_after_seconds"] = retry_after
    if status is not None:
        error["http_status"] = status
    return _result(
        message,
        {"isError": True, "content": [{"type": "text", "text": json.dumps(error)}]},
    )


def _result(message: dict, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": message.get("id"), "result": result}


def _error(message: dict, code: int, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": message.get("id"), "error": {"code": code, "message": text}}


def main() -> None:
    import sys

    server = RelayMcp(
        os.environ.get("GIT_RELAY_URL", "http://127.0.0.1:8471"),
        os.environ.get("GIT_RELAY_SEAT_TOKEN", ""),
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
