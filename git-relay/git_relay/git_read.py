"""Read-only git over HTTP through the relay:  fetch and clone as the seat's own GitHub App.

    GET  /git/<owner>/<repo>[.git]/info/refs?service=git-upload-pack
    POST /git/<owner>/<repo>[.git]/git-upload-pack

The seat presents its own bearer in the Authorization header, and git needs nothing else.  The relay checks the
seat's allowlist, mints a contents:read installation token for that one repo, and forwards the smart HTTP exchange to
GitHub.  The token stays in this process:  the caller never sees it, unlike `mint_token`.  Pushing is refused, because
git-receive-pack has no route here, and no other GitHub path can be reached.

Every request writes one audit row and draws on the seat's hourly quota.  At most MAX_CONCURRENT exchanges run at
once and at most MAX_PER_SEAT for one seat, so a slow clone cannot take every request slot from the other relay actions
or from other seats.  Upstream errors are reported by status alone:  no upstream text is relayed, and only exception
type names reach the audit log.  A request refused before it can name a seat goes to the bounded unauthenticated audit.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import re
import threading
from collections.abc import Callable

from .secret_scan import find_secret

ACTION = "git_read"
READ_PERMISSIONS = {"contents": "read"}
UPSTREAM_HOST = "github.com"
UPSTREAM_TIMEOUT_S = 300
MAX_CONCURRENT = 4
MAX_PER_SEAT = 2
# Fetch negotiation sends wants and haves.  A longer list is a client that needs http.postBuffer set, not a bigger cap.
MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 4 * 1024 * 1024 * 1024
CHUNK_BYTES = 256 * 1024

_PATH = re.compile(
    r"/git/([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]{1,104})/(info/refs|git-upload-pack)"
)
_GIT_PROTOCOL = re.compile(r"version=[0-9]{1,2}(?::[a-z0-9=-]{1,40}){0,4}")
_ADVERTISE = "application/x-git-upload-pack-advertisement"
_RESULT = "application/x-git-upload-pack-result"
_REQUEST = "application/x-git-upload-pack-request"
_slots = threading.BoundedSemaphore(MAX_CONCURRENT)
_seat_slots: dict[str, threading.BoundedSemaphore] = {}
_seat_slots_lock = threading.Lock()


def is_git_path(path: str) -> bool:
    return path == "/git" or path.startswith("/git/") or path.startswith("/git?")


def _seat_slot(seat: str) -> threading.BoundedSemaphore:
    with _seat_slots_lock:
        return _seat_slots.setdefault(seat, threading.BoundedSemaphore(MAX_PER_SEAT))


def _audit_repo(repo: str) -> str:
    """The repo as the audit row may hold it:  a credential-shaped path segment is never written down."""
    return "" if find_secret(repo) else repo


def _default_connect() -> http.client.HTTPConnection:
    return http.client.HTTPSConnection(UPSTREAM_HOST, timeout=UPSTREAM_TIMEOUT_S)


def _send_json(handler, status: int, obj: dict) -> None:
    raw = json.dumps(obj).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def _parse(method: str, target: str) -> tuple[str, str, str] | tuple[None, int, str]:
    """(repo, upstream path, service kind), or (None, status, reason)."""
    path, _, query = target.partition("?")
    match = _PATH.fullmatch(path)
    if not match:
        if "receive-pack" in target:
            return None, 403, "this route is read-only"
        return None, 404, "not found"
    owner, name, tail = match.groups()
    name = name.removesuffix(".git")  # a remote may be written with or without it
    if ".." in owner or ".." in name or not name or owner.startswith(".") or name.startswith("."):
        return None, 404, "not found"
    repo = f"{owner}/{name}"
    if tail == "info/refs":
        if method != "GET":
            return None, 405, "use GET"
        if query != "service=git-upload-pack":
            if "receive-pack" in query:
                return None, 403, "this route is read-only"
            return None, 400, "only service=git-upload-pack is served"
        return repo, f"/{repo}.git/info/refs?service=git-upload-pack", "advertise"
    if method != "POST":
        return None, 405, "use POST"
    if query:
        return None, 400, "no query is accepted"
    return repo, f"/{repo}.git/git-upload-pack", "upload"


def handle(
    relay,
    handler,
    method: str,
    connect: Callable[[], http.client.HTTPConnection] = _default_connect,
) -> None:
    """Serve one git request.  Every outcome, allowed or denied, past authentication writes exactly one audit row."""
    handler.close_connection = True
    repo, upstream_path, kind = _parse(method, handler.path)
    if repo is None:
        relay._unauth_auditor.record(relay.audit, ACTION)
        _send_json(handler, upstream_path, {"error": kind})
        return
    body = b""
    if kind == "upload":
        if handler.headers.get("Content-Type") != _REQUEST:
            relay._unauth_auditor.record(relay.audit, ACTION)
            _send_json(handler, 415, {"error": "unexpected content type"})
            return
        try:
            length = int(handler.headers.get("Content-Length") or "0")
        except ValueError:
            length = 0
        if length < 1 or length > MAX_REQUEST_BYTES:
            # git sends a chunked body past http.postBuffer, so a client with a long want list sets that higher.
            relay._unauth_auditor.record(relay.audit, ACTION)
            _send_json(
                handler, 411, {"error": "a Content-Length within the byte bound is required"}
            )
            return
        body = handler.rfile.read(length)
    auth = handler.headers.get("Authorization") or ""
    presented = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
    proof = {"action": ACTION, "body_sha256": hashlib.sha256(body).hexdigest()}
    seat = relay.policy.authenticate(presented, proof)
    if seat is None:
        relay._unauth_auditor.record(relay.audit, ACTION)
        _send_json(handler, 401, {"error": "unauthenticated"})
        return
    # The default is "error":  an exception of a type the except clause does not name still records its real
    # class, never a silent "ok" (the finally below records before the exception propagates).
    outcome, detail = "error", {"kind": kind, "bytes": 0}
    try:
        outcome = _serve(relay, handler, seat, repo, upstream_path, kind, body, detail, connect)
    except (OSError, http.client.HTTPException) as exc:
        # The client went away, or the upstream broke mid-stream.  Headers may be sent, so closing is the only signal.
        outcome = "unavailable"
        detail["reason"] = type(exc).__name__
    finally:
        relay.audit.record(seat, ACTION, _audit_repo(repo), outcome, detail)


def _serve(relay, handler, seat, repo, upstream_path, kind, body, detail, connect) -> str:
    # Imported here:  server imports this module.
    from .server import _perm_within_ceiling

    # The path charset admits a token pasted as a repo name:  refuse a credential-shaped one before it
    # is authorized against or sent to the minter, the same rule every other relay argument already follows.
    if find_secret(repo):
        detail["reason"] = "credential-shaped repo name"
        _send_json(
            handler, 400, {"error": "refused: the repo name looks like it contains a credential"}
        )
        return "refused"
    denial = relay.policy.authorize(seat, ACTION, repo)
    if denial:
        detail["reason"] = denial
        _send_json(handler, 403, {"error": denial})
        return "denied"
    if relay.app_minter is None:
        _send_json(handler, 503, {"error": "app-token minting is not configured on this relay"})
        return "unavailable"
    app_slug, ceiling = relay.policy.app_identity(seat)
    missing = _perm_within_ceiling(READ_PERMISSIONS, ceiling)
    if missing:
        detail["reason"] = f"ceiling lacks {missing}"
        _send_json(handler, 403, {"error": f"seat ceiling does not cover permission {missing!r}"})
        return "denied"
    seat_slot = _seat_slot(seat)
    if not seat_slot.acquire(blocking=False):
        detail["reason"] = "busy"
        _send_json(
            handler,
            503,
            {"error": "too many git transfers are running;  retry shortly", "retryable": True},
        )
        return "unavailable"
    if not _slots.acquire(blocking=False):
        seat_slot.release()
        detail["reason"] = "busy"
        _send_json(
            handler,
            503,
            {"error": "too many git transfers are running;  retry shortly", "retryable": True},
        )
        return "unavailable"
    conn = None
    try:
        try:
            token, _expiry = relay.app_minter.token_for(app_slug, repo, dict(READ_PERMISSIONS))
        except Exception as exc:  # noqa: BLE001 - a mint failure of any type is one refusal, named by type only
            detail["reason"] = type(exc).__name__
            _send_json(
                handler,
                503,
                {"error": "could not obtain read access for this repo", "retryable": True},
            )
            return "unavailable"
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        del token
        headers = {
            "Authorization": "Basic " + basic,
            "User-Agent": "swu-git-relay",
            "Accept": "*/*",
        }
        del basic
        protocol = handler.headers.get("Git-Protocol") or ""
        if _GIT_PROTOCOL.fullmatch(protocol):
            headers["Git-Protocol"] = protocol
        if kind == "upload":
            headers["Content-Type"] = _REQUEST
            if handler.headers.get("Content-Encoding") == "gzip":
                headers["Content-Encoding"] = "gzip"
        conn = connect()
        conn.request(
            "POST" if kind == "upload" else "GET", upstream_path, body=body or None, headers=headers
        )
        del headers
        resp = conn.getresponse()
        wanted = _RESULT if kind == "upload" else _ADVERTISE
        if resp.status != 200 or resp.getheader("Content-Type") != wanted:
            detail["upstream_status"] = resp.status
            status = resp.status if resp.status in (401, 403, 404) else 502
            _send_json(handler, status, {"error": f"GitHub answered {resp.status}"})
            return "github_terminal" if status != 502 else "github_unknown"
        handler.send_response(200)
        handler.send_header("Content-Type", wanted)
        handler.send_header("Cache-Control", "no-cache")
        encoding = resp.getheader("Content-Encoding")
        if encoding == "gzip":
            handler.send_header("Content-Encoding", "gzip")
        handler.end_headers()
        while True:
            chunk = resp.read(CHUNK_BYTES)
            if not chunk:
                return "ok"
            detail["bytes"] += len(chunk)
            if detail["bytes"] > MAX_RESPONSE_BYTES:
                detail["reason"] = "response over the byte bound"
                return "unavailable"  # the closed connection fails the client's transfer
            handler.wfile.write(chunk)
    finally:
        _slots.release()
        seat_slot.release()
        if conn is not None:
            conn.close()
