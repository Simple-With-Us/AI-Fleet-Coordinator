#!/usr/bin/env python3
"""stdio MCP proxy to local seat-mcp (127.0.0.1:8793).

Reads SEAT_MCP_TOKEN from the environment, else Infisical Fleet-OPS /seat-mcp through
`fleet-secrets get` (AFC-359).  Never prints it.  Never puts the token on argv.  Loopback only.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

FLEET_SECRETS = os.environ.get("FLEET_SECRETS_BIN") or str(
    Path.home() / ".local" / "bin" / "fleet-secrets"
)
BASE = "http://127.0.0.1:8793/mcp"


def load_token() -> str:
    token = os.environ.get("SEAT_MCP_TOKEN", "").strip()
    if not token:
        try:
            proc = subprocess.run(
                [FLEET_SECRETS, "get", "SEAT_MCP_TOKEN", "--path", "/seat-mcp"],
                capture_output=True,
                text=True,
                check=False,
                stdin=subprocess.DEVNULL,
                timeout=60,
            )
            if proc.returncode == 0:
                token = proc.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            token = ""
    if not token:
        sys.stderr.write("SEAT_MCP_TOKEN missing in Infisical /seat-mcp\n")
        sys.exit(1)
    return token


def post(token: str, body: bytes, timeout: float) -> bytes:
    req = urllib.request.Request(
        BASE,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        return (
            exc.read()
            or json.dumps(
                {"jsonrpc": "2.0", "error": {"code": -32000, "message": "http %s" % exc.code}}
            ).encode()
        )


def main() -> None:
    token = load_token()
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        timeout = 30.0
        try:
            msg = json.loads(line)
            method = str(msg.get("method") or "")
            if method in {"tools/call", "seat_launch", "grok_session_prompt"}:
                timeout = 240.0
        except json.JSONDecodeError:
            timeout = 30.0
        out = post(token, line.encode("utf-8"), timeout)
        text = out.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        # Streamable HTTP may wrap as SSE.  Unwrap one data: payload.
        if text.startswith("event:") or text.startswith("data:"):
            for chunk in text.splitlines():
                if chunk.startswith("data:"):
                    text = chunk[5:].strip()
                    break
        sys.stdout.write(text + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
