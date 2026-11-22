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
    """SEAT_MCP_TOKEN, or a fail-closed refusal that names the unavailable source.

    A host that bound the token in the environment keeps it.  Otherwise the read runs
    under this consumer's own scoped service token, and every failure says which
    source was unavailable:  the shared client,  the token store,  or the read.  No
    value ever appears in the refusal.
    """
    token = os.environ.get("SEAT_MCP_TOKEN", "").strip()
    if token:
        return token
    try:
        import fleet_infisical as FI
    except ImportError:
        sys.stderr.write(
            "seat-mcp: the shared Infisical client (fleet_infisical.py) is not importable, "
            "so the %s scoped service token cannot be read\n" % TOKEN_ACCOUNT
        )
        sys.exit(1)
    env = dict(os.environ)
    # The approved origin is pinned in code:  an inherited FLEET_SECRETS_API never
    # redirects the read,  so the bearer never travels to another origin.
    env[FI.ENV_API] = FI.API_BASE
    env[FI.ENV_READ_PATHS] = SECRET_FOLDER
    try:
        value, status = FI.fetch_scoped("SEAT_MCP_TOKEN", SECRET_FOLDER, TOKEN_ACCOUNT, env)
    except TimeoutError:
        sys.stderr.write(
            "seat-mcp: the Infisical read for the %s scoped service token timed out\n"
            % TOKEN_ACCOUNT
        )
        sys.exit(1)
    except Exception as exc:
        sys.stderr.write(
            "seat-mcp: the Infisical read for the %s scoped service token could not run (%s)\n"
            % (TOKEN_ACCOUNT, type(exc).__name__)
        )
        sys.exit(1)
    if status != "ok" or not value:
        sys.stderr.write(
            "seat-mcp: no usable token:  the %s scoped service token store is unavailable or "
            "refused (%s),  and SEAT_MCP_TOKEN binds none\n" % (TOKEN_ACCOUNT, status)
        )
        sys.exit(1)
    return value


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
