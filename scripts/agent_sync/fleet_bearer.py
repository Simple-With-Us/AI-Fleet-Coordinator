"""Private-file per-bot fleet identity. No environment credentials or direct fallback.

Vendored byte-for-byte by FleetLink's CLI. Only the gateway receives the bearer;
provider credentials and account authorization stay on the servers.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import stat
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://agents.simplewithus.com/mcp"
MAX_BYTES = 4_000_000
TOKEN = re.compile(r"(?:sbt_([a-z][a-z0-9-]{1,19})|gbb_([a-z][a-z0-9]{1,19}))_([A-Za-z0-9_-]{43})")

# The per-seat bearer files the assigned stdio connectors and the fleet CLIs read (mcp/bearer_gate.py,
# fleet_rag/seat_auth.py):  a bearer comes from this one private place, never an environment
# value, a launcher claim or another seat's file.
BEARER_DIR = (".secrets", "fleet-tools")
SEAT_FILE = re.compile(r"[a-z][a-z0-9-]{1,19}")


def default_bearer_path(home, seat):
    """This seat's own fleet bearer file, or FleetError not_configured when `seat` cannot name one."""
    stem = str(seat or "").strip().lower()
    if not SEAT_FILE.fullmatch(stem):
        raise FleetError(
            "not_configured", "%r is not a seat a fleet bearer file can belong to" % (seat,)
        )
    return Path(home).joinpath(*BEARER_DIR, stem)



class FleetError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def private_bearer(path):
    """Open once, without symlink following; never disclose content in errors."""
    try:
        if not isinstance(path, (str, os.PathLike)) or not str(path).strip():
            raise ValueError
        path = Path(path).expanduser().absolute()
        # Walk with directory descriptors, not lstat-then-open: replacement of
        # an ancestor must not redirect the credential read through a symlink.
        directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for component in path.parts[1:-1]:
                child = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                )
                os.close(directory)
                directory = child
                info = os.fstat(directory)
                root_sticky = info.st_uid in (0, os.getuid()) and bool(info.st_mode & stat.S_ISVTX)
                if (info.st_mode & 0o022 and not root_sticky) or info.st_uid not in (
                    0,
                    os.getuid(),
                ):
                    raise ValueError
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)
                    or info.st_nlink != 1
                    or info.st_size > 256
                ):
                    raise ValueError
                raw = handle.read(257)
        finally:
            os.close(directory)
        value = raw.decode("ascii")
        if value[-1:] == "\n":
            value = value[:-1]
        match = TOKEN.fullmatch(value)
        if not match:
            raise ValueError
        seat = match[1].upper() if match[1] else "GB-" + match[2].upper()
        return seat, value
    except (OSError, ValueError, UnicodeError, TypeError):
        raise FleetError(
            "not_configured",
            "fleet bearer file is missing, unsafe or malformed; no fallback was used",
        ) from None


def bounded_json(raw):
    # Refuse deep nesting before the JSON parser can consume recursion or memory.
    depth = 0
    quoted = escaped = False
    for char in raw:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > 64:
                raise ValueError
        elif char in "]}":
            depth -= 1
    return json.loads(raw)


def contains_bearer(value, token):
    """Scan decoded strings and keys too, not only their JSON byte spelling."""
    stack = [value]
    visited = 0
    while stack:
        item = stack.pop()
        visited += 1
        if visited > 100_000:
            raise ValueError
        if isinstance(item, str) and token in item:
            return True
        if isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple)):
            stack.extend(item)
    return False


# Mirrored from identity.canonical_seat; parity tests cover published aliases.
SEAT_ALIASES = {
    "INSTINCT": "WISE",
    "WISE-INSTINCT": "WISE",
    "ECHO-INSTINCT": "WISE",
    "MUSE-ASSIST": "MA",
    "ROB": "MA",
    "ROB-MUSE": "MA",
}


def canonical_hint(raw):
    text = str(raw or "").strip()
    if re.fullmatch(r"grok[^a-z0-9]*build[^a-z0-9]*", text, re.IGNORECASE | re.ASCII):
        return "GROK"
    return SEAT_ALIASES.get(text.upper(), text.upper())


class GatewayTransport:
    """One verified TLS connection per client. No redirects, proxy or retries."""

    def __init__(self):
        self.connection = None

    def close(self):
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def open(self, request, timeout=40):
        if request.full_url != ENDPOINT or request.get_method() != "POST":
            raise ValueError("fixed gateway only")
        if self.connection is None:
            self.connection = http.client.HTTPSConnection(
                "agents.simplewithus.com", timeout=timeout
            )
        try:
            self.connection.request(
                "POST", "/mcp", body=request.data, headers=dict(request.header_items())
            )
            response = self.connection.getresponse()
            if response.status != 200:
                status = response.status
                self.close()
                raise urllib.error.HTTPError(ENDPOINT, status, "gateway refusal", {}, None)
            return response
        except Exception:
            self.close()
            raise


def gateway_transport():
    return GatewayTransport()


class Client:
    def __init__(self, path, expected_seat=None, identity_env=None):
        self._transport = gateway_transport()
        self.path = path
        self.seat, _ = private_bearer(path)
        identity_env = os.environ if identity_env is None else identity_env
        # Non-secret launcher hints can restrict, never select another bearer.
        for name in ("AGENT_LAUNCH_SEAT", "AGENT_SEAT", "AGENT_TAG"):
            value = canonical_hint(identity_env.get(name, ""))
            if value and value != self.seat:
                raise FleetError(
                    "not_authorized", "fleet bearer conflicts with the current bot identity"
                )
        if identity_env.get("AGENT_LAUNCHER") and not identity_env.get("AGENT_LAUNCH_SEAT"):
            raise FleetError("not_authorized", "launcher did not bind a bot identity")
        if expected_seat is not None and self.seat != canonical_hint(expected_seat):
            raise FleetError("not_authorized", "fleet bearer does not name the requested bot")
        self.verified = False
        self._verified_token = None

    def rpc(self, method, params, write=False):
        seat, token = private_bearer(self.path)
        if seat != self.seat:
            raise FleetError("not_authorized", "fleet bearer identity changed; nothing was sent")
        unknown = "; outcome unknown, check before repeating the write" if write else ""
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, allow_nan=False
        ).encode()
        if len(body) > MAX_BYTES or contains_bearer(params, token):
            raise FleetError(
                "invalid_argument",
                "request too large or contains the fleet bearer; nothing was sent",
            )
        request = urllib.request.Request(
            ENDPOINT,
            data=body,
            method="POST",
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-06-18",
                "User-Agent": "fleet-bearer-client/1",
            },
        )
        try:
            # No environment proxy, cookies, redirects or retry.
            with self._transport.open(request, timeout=40) as response:
                raw = response.read(MAX_BYTES + 1)
                ctype = response.headers.get("Content-Type", "").split(";", 1)[0].strip()
            if len(raw) > MAX_BYTES or token.encode() in raw:
                raise ValueError
            text = raw.decode("utf-8")
            if ctype == "text/event-stream":
                candidates = [
                    "\n".join(
                        line[5:].lstrip(" ")
                        for line in event.splitlines()
                        if line.startswith("data:")
                    )
                    for event in text.replace("\r\n", "\n").split("\n\n")
                ]
            elif ctype == "application/json":
                candidates = [text]
            else:
                raise ValueError
            replies = [bounded_json(part) for part in candidates if part.strip()]
            replies = [
                r
                for r in replies
                if isinstance(r, dict)
                and r.get("jsonrpc") == "2.0"
                and type(r.get("id")) is int
                and r["id"] == 1
            ]
            if len(replies) != 1:
                raise ValueError
            reply = replies[0]
            if contains_bearer(reply, token):
                raise ValueError
            if "error" in reply or not isinstance(reply.get("result"), dict):
                raise ValueError
            result = reply["result"]
            if not isinstance(result.get("isError", False), bool):
                raise TypeError
            if "content" in result and (
                not isinstance(result["content"], list)
                or any(not isinstance(p, dict) for p in result["content"])
            ):
                raise ValueError
            if result.get("isError"):
                sc = result.get("structuredContent")
                code = sc.get("code") if isinstance(sc, dict) else None
                if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,40}", code):
                    code = "upstream_error"
                raise FleetError(code, "gateway refused the call (" + code + ")" + unknown)
            return result
        except urllib.error.HTTPError as exc:
            uncertain = write and (exc.code in (408, 429) or exc.code >= 500)
            code = (
                "not_authorized"
                if exc.code in (401, 403)
                else ("outcome_unknown" if uncertain else "unavailable")
            )
            raise FleetError(
                code,
                "gateway HTTP " + str(exc.code) + (unknown if uncertain else ""),
            ) from None
        except FleetError:
            raise
        except (
            OSError,
            ValueError,
            TypeError,
            RecursionError,
            http.client.HTTPException,
            urllib.error.URLError,
        ):
            self._transport.close()
            raise FleetError(
                "outcome_unknown" if write else "unavailable",
                "gateway transport or response failed" + unknown,
            ) from None

    def call(self, name, arguments, write=False):
        _, token = private_bearer(self.path)
        if not self.verified or token != self._verified_token:
            who = self.rpc("tools/call", {"name": "whoami", "arguments": {}}).get(
                "structuredContent"
            )
            if (
                not isinstance(who, dict)
                or who.get("authenticated_seat") != self.seat
                or who.get("seat") != self.seat
            ):
                raise FleetError(
                    "not_authorized", "gateway identity did not match this bot; nothing was sent"
                )
            self.verified = True
            self._verified_token = token
        return self.rpc("tools/call", {"name": name, "arguments": arguments}, write)
