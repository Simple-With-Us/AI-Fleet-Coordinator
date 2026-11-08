"""Private-file per-bot fleet identity. No environment credentials or direct fallback.

Vendored byte-for-byte by FleetLink's CLI. Only the gateway receives the bearer;
provider credentials and account authorization stay on the servers.

A successful whoami verification is remembered on disk for
VERIFY_CACHE_TTL_SECONDS (see below), so a burst of one-shot CLI processes
from one seat verifies once an hour instead of once per process (owner
ruling 2026-11-06:  a checked token may work for an hour).  The cache holds
only the SHA-256 of the bearer, never the bearer;  it is a local pre-flight
shortcut only - the gateway still authenticates every call, and only this
client's own "identity confirmed" step can be up to an hour stale.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import stat
import time
import urllib.error
import urllib.request
from pathlib import Path

ENDPOINT = "https://agents.simplewithus.com/mcp"
MAX_BYTES = 4_000_000
TOKEN = re.compile(r"(?:sbt_([a-z][a-z0-9-]{1,19})|gbb_([a-z][a-z0-9]{1,19}))_([A-Za-z0-9_-]{43})")


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


# The whoami verification cache:  one small JSON row beside the seat's
# idempotency state (<state>/<SEAT>/mcp-idem.json), in the state root
# simple_tools.state resolves ($SIMPLE_TOOLS_STATE_DIR, else ~/.simple-tools).
# The rule is mirrored here, not imported, so this file stays self-contained
# for its vendored copies.  Every cache anomaly - missing, malformed, loose
# permissions, wrong owner, a different bearer's hash, a stale or future
# timestamp - is a miss, never an error:  the client falls back to the live
# whoami it has always made, so the cache can only ever save a call, never
# change an outcome.
VERIFY_CACHE_TTL_SECONDS = 3600.0
VERIFY_CACHE_FILE = "fleet-bearer-verify.json"
STATE_DIR_ENV = "SIMPLE_TOOLS_STATE_DIR"


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
        self._env = identity_env
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

    def _verify_cache_path(self):
        """The cache file for this seat, or None when no state root exists.

        The state root itself is never created here:  a seat with no state
        directory yet simply verifies live every time, as it did before the
        cache existed.  The seat directory below an existing root is made
        on first write, with the same privacy as the state it sits beside.
        """
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9-]{0,39}", self.seat):
            return None
        override = self._env.get(STATE_DIR_ENV, "")
        root = Path(override).expanduser() if override else Path.home() / ".simple-tools"
        if not root.is_dir():
            return None
        seat_dir = root / self.seat
        # A planted symlink for the seat directory would let the mkdir and
        # the cache write escape the state root:  refuse it, the same
        # discipline private_bearer keeps for the bearer file itself.  The
        # cache simply stays off and verification runs live.
        if seat_dir.is_symlink():
            return None
        return seat_dir / VERIFY_CACHE_FILE

    def _bearer_path_digest(self):
        return hashlib.sha256(
            str(Path(self.path).expanduser().absolute()).encode("utf-8")
        ).hexdigest()

    def _cached_verification(self, token):
        """True only for a fresh entry naming this bearer file and its hash.

        The entry binds the file's path as well as the bearer's hash, so a
        verification proven through one bearer file can never be spent by a
        client reading a different one that happens to share the state root.
        """
        try:
            path = self._verify_cache_path()
            if path is None:
                return False
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) not in (0o400, 0o600)
                    or info.st_nlink != 1
                    or info.st_size > 1024
                ):
                    return False
                entry = bounded_json(handle.read().decode("ascii"))
            if not isinstance(entry, dict) or entry.get("seat") != self.seat:
                return False
            digest = hashlib.sha256(token.encode("ascii")).hexdigest()
            if entry.get("token_sha256") != digest:
                return False
            if entry.get("bearer_path_sha256") != self._bearer_path_digest():
                return False
            verified_at = entry.get("verified_at")
            if not isinstance(verified_at, (int, float)) or isinstance(verified_at, bool):
                return False
            age = time.time() - verified_at
            return -5.0 <= age < VERIFY_CACHE_TTL_SECONDS
        except Exception:
            return False

    def _remember_verification(self, token):
        """Best-effort:  record a just-proven verification for the next process.

        Any failure is silent - the verification itself already happened
        live, and a later process simply verifies live again.
        """
        try:
            path = self._verify_cache_path()
            if path is None:
                return
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            entry = {
                "seat": self.seat,
                "token_sha256": hashlib.sha256(token.encode("ascii")).hexdigest(),
                "bearer_path_sha256": self._bearer_path_digest(),
                "verified_at": time.time(),
            }
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(json.dumps(entry))
        except Exception:
            pass

    def call(self, name, arguments, write=False):
        _, token = private_bearer(self.path)
        if not self.verified or token != self._verified_token:
            if not self._cached_verification(token):
                who = self.rpc("tools/call", {"name": "whoami", "arguments": {}}).get(
                    "structuredContent"
                )
                if (
                    not isinstance(who, dict)
                    or who.get("authenticated_seat") != self.seat
                    or who.get("seat") != self.seat
                ):
                    raise FleetError(
                        "not_authorized",
                        "gateway identity did not match this bot; nothing was sent",
                    )
                self._remember_verification(token)
            self.verified = True
            self._verified_token = token
        return self.rpc("tools/call", {"name": name, "arguments": arguments}, write)
