"""Bearer token for the local MCP HTTP listener.

Never print the token.  Compare with hmac.compare_digest.
"""

from __future__ import annotations

import hmac
import os
import subprocess

from .config import FLEET_SECRETS_BIN, TOKEN_FOLDER, TOKEN_NAME


def load_token() -> str:
    """SEAT_MCP_TOKEN from the environment, else Infisical Fleet-OPS /seat-mcp through `fleet-secrets`
    (AFC-359).  Empty or absent is a hard fail that names the folder and key, never a value."""
    token = os.environ.get(TOKEN_NAME, "").strip()
    if not token:
        binary = os.environ.get("FLEET_SECRETS_BIN") or str(FLEET_SECRETS_BIN)
        try:
            proc = subprocess.run(
                [binary, "get", TOKEN_NAME, "--path", TOKEN_FOLDER],
                capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL, timeout=60,
            )
            if proc.returncode == 0:
                token = proc.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            token = ""
    if not token:
        raise RuntimeError("%s missing in Infisical %s" % (TOKEN_NAME, TOKEN_FOLDER))
    return token


def bearer_ok(header: str, token: str) -> bool:
    """True when Authorization is Bearer <token>."""
    if not header or not token:
        return False
    parts = header.split(None, 1)
    if len(parts) != 2:
        return False
    scheme, got = parts
    if scheme.lower() != "bearer":
        return False
    got = got.strip()
    if len(got) != len(token):
        # Keep compare_digest from throwing on length mismatch.
        hmac.compare_digest(token, token)
        return False
    return hmac.compare_digest(got, token)


ACCESS_HEADERS = (
    "Cf-Access-Jwt-Assertion",
    "Cf-Access-Authenticated-User-Email",
)

ALLOWED_PUBLIC_ORIGINS = (
    "https://agents.jays.services",
)


def access_authenticated(headers) -> bool:
    """True when Cloudflare Access already gated this hop.

    Direct loopback clients do not send these.  Named-tunnel requests from
    Grok Bot / Cursor cloud do, after Access accepts the service token.
    """
    if headers is None:
        return False
    getter = headers.get if hasattr(headers, "get") else lambda k, d="": (headers or {}).get(k, d)
    for name in ACCESS_HEADERS:
        val = getter(name) or getter(name.lower())
        if val:
            return True
    return False


def origin_ok(origin: str, access_ok: bool = False) -> bool:
    """Allow missing Origin and local / editor origins.  Reject the rest.

    DNS-rebinding guard for streamable HTTP.  Loopback bind is the other half.
    Cloudflare Access-authenticated hops (Grok Bot via agents.jays.services)
    skip the browser-origin check — Access + Bearer still required.
    """
    if access_ok:
        return True
    if not origin:
        return True
    origin = origin.strip()
    if origin.lower() == "null":
        return True
    allowed_prefixes = (
        "http://127.0.0.1",
        "https://127.0.0.1",
        "http://localhost",
        "https://localhost",
        "app://",
        "vscode-file://",
        "vscode://",
        "cursor://",
    )
    if origin.startswith(allowed_prefixes):
        return True
    return origin.rstrip("/") in ALLOWED_PUBLIC_ORIGINS
