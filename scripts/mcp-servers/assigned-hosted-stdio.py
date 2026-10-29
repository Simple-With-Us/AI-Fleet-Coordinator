#!/usr/bin/env -S python3 -I
"""Assigned-bearer stdio transport. No secret reader, token minting or retries."""

import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.request

ENDPOINT = "https://agents.simplewithus.com/mcp"
MAX_BYTES = 4 * 1024 * 1024
MAX_FILE_ARGS_BYTES = 6 * 1024 * 1024
MAX_FILE_FRAME_BYTES = MAX_FILE_ARGS_BYTES + 65536
TOKEN_RE = re.compile(
    r"^(?:sbt_([a-z][a-z0-9-]{1,19})|gbb_([a-z][a-z0-9]{1,19}))_[A-Za-z0-9_-]{43}$"
)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TransportFailure(Exception):
    pass


def assigned_identity(token):
    match = TOKEN_RE.fullmatch(token)
    if not match:
        raise TransportFailure("assigned bearer is missing or malformed")
    return match[1].upper() if match[1] else "GB-" + match[2].upper()


def failure(message, code="outcome_unknown"):
    if "id" not in message:
        return None
    if message.get("method") == "tools/call":
        return {
            "jsonrpc": "2.0",
            "id": message["id"],
            "result": {
                "isError": True,
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "code": code,
                                "retryable": False,
                                "message": "The write outcome is unknown. Do not retry blindly; check target state first."
                                if code == "outcome_unknown"
                                else "That identity is not assigned to this connection.",
                            }
                        ),
                    }
                ],
            },
        }
    return {
        "jsonrpc": "2.0",
        "id": message["id"],
        "error": {
            "code": -32000,
            "message": "Assigned hosted transport unavailable; no automatic retry.",
        },
    }


class Adapter:
    def __init__(self, token, opener=None):
        self.identity = assigned_identity(token)  # A local guard, not server authentication.
        self.token = token
        self.opener = opener or urllib.request.build_opener(NoRedirect()).open
        self.session = None
        self.protocol = None

    def call(self, message):
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            raise TransportFailure("invalid JSON-RPC message")
        params = message.get("params")
        if message.get("method") == "tools/call" and isinstance(params, dict):
            args = params.get("arguments")
            # No alias selection: the hosted server independently enforces its binding.
            if isinstance(args, dict) and "identity" in args and args["identity"] != self.identity:
                return failure(message, "identity_not_allowed")
        if message.get("method") == "tools/call" and isinstance(params, dict):
            args = params.get("arguments", {})
            if isinstance(args, dict) and params.get("name") != "identities":
                # Assigned by the credential, never a configurable seat selector.
                message = {
                    **message,
                    "params": {**params, "arguments": {**args, "identity": self.identity}},
                }
        headers = {
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "assigned-hosted-stdio/1.0",
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        encoded = json.dumps(message).encode()
        if not file_frame_allowed(message, len(encoded)):
            raise TransportFailure("Input frame too large.")
        request = urllib.request.Request(ENDPOINT, data=encoded, headers=headers, method="POST")
        try:
            with self.opener(request, timeout=120) as response:
                status = response.status
                if status == 202 and "id" not in message:
                    return None
                if status != 200:
                    return failure(message)
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    if len(session) > 256 or not re.fullmatch(r"[!-~]+", session):
                        return failure(message)
                    if (
                        self.session
                        and session != self.session
                        and message.get("method") != "initialize"
                    ):
                        return failure(message)
                raw = response.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES or self.token.encode() in raw:
                    return failure(message)
                content_type = response.headers.get("Content-Type", "").split(";")[0].strip()
                if content_type == "text/event-stream":
                    payloads = []
                    data = []
                    for line in raw.decode("utf-8").replace("\r\n", "\n").split("\n") + [""]:
                        if not line:
                            if data:
                                payloads.append(json.loads("\n".join(data)))
                                data = []
                        elif line.startswith("data:"):
                            data.append(line[5:].lstrip(" "))
                elif content_type == "application/json":
                    payloads = [json.loads(raw)]
                else:
                    return failure(message)
                matching = [
                    p
                    for p in payloads
                    if isinstance(p, dict)
                    and p.get("jsonrpc") == "2.0"
                    and "id" in p
                    and p["id"] == message.get("id")
                    and (("result" in p) != ("error" in p))
                ]
                if "id" not in message:
                    return None
                if len(matching) != 1:
                    return failure(message)
                result = matching[0]
                if message.get("method") == "initialize" and isinstance(result.get("result"), dict):
                    protocol = result["result"].get("protocolVersion")
                    if not isinstance(protocol, str) or not re.fullmatch(r"[!-~]{1,128}", protocol):
                        return failure(message)
                    self.protocol = protocol
                    self.session = session
                return result
        except (OSError, ValueError, http.client.HTTPException):
            # Never reflect provider prose, headers, credentials or redirect locations.
            # An accepted write can lose its response even on a non-JSON 4xx/5xx.
            return failure(message)


def file_frame_allowed(message, frame_bytes):
    if frame_bytes <= MAX_BYTES:
        return True
    if frame_bytes > MAX_FILE_FRAME_BYTES or not isinstance(message, dict):
        return False
    params = message.get("params")
    if (
        message.get("jsonrpc") != "2.0"
        or message.get("method") != "tools/call"
        or not isinstance(params, dict)
        or params.get("name") not in {"gr_wf_file_put", "gr_wf_files_put"}
        or set(message) - {"jsonrpc", "id", "method", "params"}
        or set(params) - {"name", "arguments", "_meta"}
        or not isinstance(params.get("arguments"), dict)
    ):
        return False
    size = len(json.dumps(params["arguments"]).encode())
    return size <= MAX_FILE_ARGS_BYTES and frame_bytes - size <= 65536


def main():
    token = os.environ.pop("ASSIGNED_MCP_BEARER", "").strip()
    try:
        adapter = Adapter(token)
    except TransportFailure:
        print("assigned-hosted-stdio: assigned bearer is missing or malformed", file=sys.stderr)
        return 3
    while True:
        raw = sys.stdin.buffer.readline(MAX_FILE_FRAME_BYTES + 1)
        if not raw:
            return 0
        if len(raw) > MAX_FILE_FRAME_BYTES:
            # Drain the rejected frame; its tail must not become another request.
            while raw and not raw.endswith(b"\n"):
                raw = sys.stdin.buffer.readline(MAX_FILE_FRAME_BYTES + 1)
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32600, "message": "Input frame too large."},
                    }
                ),
                flush=True,
            )
            continue
        try:
            message = json.loads(raw)
            if not file_frame_allowed(message, len(raw)):
                raise TransportFailure("Input frame too large.")
            result = adapter.call(message)
        except (ValueError, TransportFailure):
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "Invalid JSON-RPC input."},
                    }
                ),
                flush=True,
            )
            continue
        if result is not None:
            print(json.dumps(result), flush=True)


if __name__ == "__main__":
    sys.exit(main())
