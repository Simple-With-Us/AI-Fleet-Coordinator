#!/usr/bin/env python3
"""stdio MCP proxy to local seat-mcp (127.0.0.1:8793).

Reads SEAT_MCP_TOKEN from the environment, else Infisical Fleet-OPS /seat-mcp through this
proxy's own scoped read-only service token (AFC-359).  Never prints it.  Never puts the token
on argv.  Loopback only.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

# This proxy's own scoped read-only service token:  Fleet-OPS prod /seat-mcp, the one folder
# it reads.  The token lives in the consumer's own store (the owner's login keychain on the Mac,
# a private file on a server);  the shared Infisical client checks the token's own grant and the
# answer's own provenance before the value is used.
TOKEN_ACCOUNT = "seat-mcp-ro"
SECRET_FOLDER = "/seat-mcp"
BASE = "http://127.0.0.1:8793/mcp"


def _shared_client_path() -> None:
    """Put the shared client on the path, from wherever this host keeps it.

    In the repository the client sits beside this file's parent;  on the Mac it is
    installed beside the scoped reader, which is the one consumer-facing entry point.
    """
    here = Path(__file__).resolve()
    for candidate in (here.parent.parent, Path.home() / "apps" / "infisical-scoped"):
        if (candidate / "fleet_infisical.py").is_file():
            sys.path.insert(0, str(candidate))
            return


_shared_client_path()


def load_token() -> str:
    token = os.environ.get("SEAT_MCP_TOKEN", "").strip()
    if not token:
        try:
            import fleet_infisical as FI

            env = dict(os.environ)
            env[FI.ENV_READ_PATHS] = SECRET_FOLDER
            value, status = FI.fetch_scoped("SEAT_MCP_TOKEN", SECRET_FOLDER, TOKEN_ACCOUNT, env)
            token = value if status == "ok" else ""
        except Exception:
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
