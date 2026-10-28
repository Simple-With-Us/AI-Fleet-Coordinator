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
        }
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        if self.protocol:
            headers["MCP-Protocol-Version"] = self.protocol
        request = urllib.request.Request(
            ENDPOINT, data=json.dumps(message).encode(), headers=headers, method="POST"
        )
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
                    if self.session and session != self.session:
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
                    if not isinstance(protocol, str) or not re.fullmatch(
                        r"\d{4}-\d{2}-\d{2}", protocol
                    ):
                        return failure(message)
                    self.protocol = protocol
                    self.session = session
                return result
        except (OSError, ValueError, http.client.HTTPException):
            # Never reflect provider prose, headers, credentials or redirect locations.
            # An accepted write can lose its response even on a non-JSON 4xx/5xx.
            return failure(message)


def main():
    token = os.environ.pop("ASSIGNED_MCP_BEARER", "").strip()
    try:
        adapter = Adapter(token)
    except TransportFailure:
        print("assigned-hosted-stdio: assigned bearer is missing or malformed", file=sys.stderr)
        return 3
    while True:
        raw = sys.stdin.buffer.readline(MAX_BYTES + 1)
        if not raw:
            return 0
        if len(raw) > MAX_BYTES:
            print(
                "assigned-hosted-stdio: oversized input; stopped without sending", file=sys.stderr
            )
            return 2
        try:
            message = json.loads(raw)
            result = adapter.call(message)
        except (ValueError, TransportFailure):
            print("assigned-hosted-stdio: invalid input; stopped without sending", file=sys.stderr)
            return 2
        if result is not None:
            print(json.dumps(result), flush=True)


if __name__ == "__main__":
    sys.exit(main())
