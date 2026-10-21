"""Disabled-by-default Operator GitHub broker, hosted by the existing listener.

No server starts on import. A reviewed caller must explicitly construct/start
BrokerHttp with its own seat policy, key loader and independent mint-auth keys.
Read-API/routine keys are NOT accepted automatically. Never log request bodies,
signatures, private keys, or token response bodies.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .github_token_router import RoutingError, permission_set, target_parts
from .operator_github_state import LedgerError

CONTRACT = "operator-github-mint/1"
PATH = re.compile(r"^/operator/v1/github/([A-Z][A-Z0-9_-]{0,31})/token$")
NONCE = re.compile(r"^[a-f0-9]{64}$")
MAX_BODY = 4096
MAX_SKEW = 60
MAX_NONCES = 10000
MAX_REQUESTS_PER_MINUTE = 60


class Refused(Exception):
    def __init__(self, status: int, reason: str):
        self.status, self.reason = status, reason
        super().__init__(reason)


@dataclass(frozen=True)
class CallerPolicy:
    app_slug: str
    # Exact owner/repository names only, no wildcard expansion.
    repositories: Mapping[str, Mapping[str, str]]


def signature(key: str, path: str, stamp: str, nonce: str, body: bytes) -> str:
    message = "\n".join(
        (CONTRACT, "POST", path, stamp, nonce, hashlib.sha256(body).hexdigest())
    ).encode()
    return hmac.new(key.encode(), message, hashlib.sha256).hexdigest()


class Broker:
    """Every request authenticates and applies current policy BEFORE cache access.

    `key_for` and `policy_for` are trusted server-side lookups, not client input.
    The runtime must reload revocation/policy changes on these lookups. `router`
    loads only the bound App's key through Infisical. No seat/env fallback.
    """

    def __init__(
        self,
        key_for: Callable,
        policy_for: Callable,
        router,
        bot_identity: Callable,
        *,
        clock=time.time,
        audit=None,
        enabled=False,
        nonce_ledger=None,
    ):
        self.enabled = enabled
        self.key_for, self.policy_for = key_for, policy_for
        self.router, self.bot_identity = router, bot_identity
        self.clock, self.audit = clock, audit or (lambda **kw: None)
        self.lock = threading.Lock()
        self.nonce_ledger = nonce_ledger
        self.rates = {}

    def _mint(self, policy, repo, requested):
        identity = self.bot_identity(policy.app_slug)
        if (
            identity["login"] != policy.app_slug + "[bot]"
            or not isinstance(identity["id"], int)
            or identity["id"] <= 0
        ):
            raise ValueError()
        token = self.router.token_for(policy.app_slug, repo, requested)
        return {
            "token": token,
            "repo": repo,
            "permissions": requested,
            "app_slug": policy.app_slug,
            "git_author_name": identity["login"],
            "git_author_email": str(identity["id"])
            + "+"
            + identity["login"]
            + "@users.noreply.github.com",
        }

    def issue(self, path: str, stamp: str, nonce: str, sig: str, body: bytes):
        # Exactly one audit row per call, emitted from the finally below so no
        # return path or exception - validation, authentication, refusal, or
        # issuance - leaves the trail without a record.
        seat = None
        outcome = "refused"
        extra = {}
        try:
            if not self.enabled:
                raise Refused(404, "disabled")
            match = PATH.fullmatch(path)
            if not match or len(body) > MAX_BODY:
                raise Refused(400, "invalid request")
            seat = match[1]
            if not isinstance(stamp, str) or not re.fullmatch(r"[0-9]{1,12}", stamp):
                raise Refused(401, "authentication failed")
            now = self.clock()
            if (
                abs(now - int(stamp)) > MAX_SKEW
                or not isinstance(nonce, str)
                or not NONCE.fullmatch(nonce)
            ):
                raise Refused(401, "authentication failed")
            with self.lock:
                bucket = [t for t in self.rates.get(seat, []) if t > now - 60]
                if len(bucket) >= MAX_REQUESTS_PER_MINUTE:
                    raise Refused(429, "rate limited")
                self.rates[seat] = bucket + [now]
            key = self.key_for(seat)
            if not isinstance(key, str) or len(key) < 32:
                raise Refused(401, "authentication failed")
            if (
                not isinstance(sig, str)
                or not re.fullmatch(r"[a-f0-9]{64}", sig)
                or not hmac.compare_digest(signature(key, path, stamp, nonce, body), sig)
            ):
                raise Refused(401, "authentication failed")
            if self.nonce_ledger is None:
                raise Refused(503, "replay storage unavailable")
            # The ledger serializes itself with BEGIN IMMEDIATE; it does not
            # need the broker's global lock.
            try:
                state = self.nonce_ledger.begin(
                    seat, nonce, int(stamp) + MAX_SKEW + 1, now, MAX_NONCES
                )
            except LedgerError:
                raise Refused(503, "replay storage unavailable") from None
            if state == "capacity":
                raise Refused(503, "request capacity exceeded")
            if state != "fresh":
                _, prior_outcome, prior_response = state
                if prior_outcome != "ok" or not prior_response:
                    # pending or outcome_unknown: the first attempt's result is
                    # not known, so refuse rather than risk a duplicate mint.
                    raise Refused(409, "prior outcome unknown; retry with a fresh nonce")
                # Idempotent retry: the first response was lost.  The ledger
                # stores only non-secret metadata, never the token, so the
                # recorded request is re-validated against the CURRENT policy
                # and a fresh token is minted.  Replay stays bounded by the
                # HMAC and the timestamp-skew window.
                try:
                    data = json.loads(prior_response)
                    if not isinstance(data, dict):
                        raise ValueError()
                    target_parts(data["repo"])
                    requested = permission_set(data["permissions"])
                except (ValueError, TypeError, KeyError, RoutingError, RecursionError):
                    raise Refused(
                        409, "recorded request is not replayable; retry with a fresh nonce"
                    ) from None
                policy = self.policy_for(seat)
                allowed = (
                    policy.repositories.get(data["repo"].casefold())
                    if isinstance(policy, CallerPolicy)
                    else None
                )
                ranks = {"read": 1, "write": 2}
                if not isinstance(allowed, Mapping) or any(
                    ranks.get(allowed.get(k), 0) < ranks[v] for k, v in requested.items()
                ):
                    raise Refused(403, "caller not authorized")
                try:
                    response = self._mint(policy, data["repo"], requested)
                except Exception as exc:
                    if not _classifiable(exc):
                        raise
                    status = getattr(exc, "status", None)
                    if status == 408 or (isinstance(status, int) and status >= 500):
                        raise Refused(503, "token issuance uncertain; retry later") from None
                    raise Refused(502, "token issuance failed") from None
                outcome = "served-retry"
                extra = {
                    "repo": data["repo"],
                    "permissions": requested,
                    "app": policy.app_slug,
                }
                return response
            # The nonce is now consumed (pending).  Definite refusals release
            # it; uncertain failures settle it as outcome_unknown.
            try:
                data = json.loads(body, object_pairs_hook=_unique)
                _bounded(data)
                if not isinstance(data, dict) or set(data) != {"repo", "permissions"}:
                    raise ValueError()
                target_parts(data["repo"])
                requested = permission_set(data["permissions"])
            except (ValueError, TypeError, KeyError, RoutingError, RecursionError):
                self.nonce_ledger.release(seat, nonce)
                raise Refused(400, "invalid request") from None
            policy = self.policy_for(seat)
            if not isinstance(policy, CallerPolicy):
                self.nonce_ledger.release(seat, nonce)
                raise Refused(403, "caller not authorized")
            allowed = policy.repositories.get(data["repo"].casefold())
            if not isinstance(allowed, Mapping):
                self.nonce_ledger.release(seat, nonce)
                raise Refused(403, "repository not authorized")
            ranks = {"read": 1, "write": 2}
            if any(ranks.get(allowed.get(k), 0) < ranks[v] for k, v in requested.items()):
                self.nonce_ledger.release(seat, nonce)
                raise Refused(403, "permission not authorized")
            try:
                response = self._mint(policy, data["repo"], requested)
            except Exception as exc:
                if not _classifiable(exc):
                    self.nonce_ledger.settle(seat, nonce, "outcome_unknown")
                    raise
                status = getattr(exc, "status", None)
                if status in (401, 403, 429):
                    # Definite provider rejection: the mint never happened, so
                    # a corrected request may reuse the nonce.
                    self.nonce_ledger.release(seat, nonce)
                    raise Refused(502, "token issuance refused") from None
                self.nonce_ledger.settle(seat, nonce, "outcome_unknown")
                if status == 408 or (isinstance(status, int) and status >= 500):
                    raise Refused(
                        503, "token issuance uncertain; retry with a fresh nonce"
                    ) from None
                raise Refused(502, "token issuance failed") from None
            # Only non-secret metadata is persisted:  never the token.
            stored = {k: v for k, v in response.items() if k != "token"}
            self.nonce_ledger.settle(seat, nonce, "ok", json.dumps(stored))
            outcome = "issued"
            extra = {
                "repo": data["repo"],
                "permissions": requested,
                "app": policy.app_slug,
            }
            return response
        finally:
            # Auditing must never mask the request's own outcome.
            try:
                self.audit(seat=seat, outcome=outcome, at=self.clock(), **extra)
            except Exception:
                pass


def _classifiable(exc):
    """The router boundary is duck-typed; both RoutingError trees are accepted."""
    return (
        isinstance(exc, (ValueError, TypeError, KeyError)) or type(exc).__name__ == "RoutingError"
    )


def _bounded(value, _depth=0, _budget=None):
    """Reject hostile JSON shapes before any field is trusted.

    Body bytes are already capped by MAX_BODY; this caps what those bytes can
    parse into - at most 32 levels deep and 512 nodes total.
    """
    if _budget is None:
        _budget = [512]
    _budget[0] -= 1
    if _depth > 32 or _budget[0] < 0:
        raise ValueError("invalid request")
    if isinstance(value, dict):
        for k, v in value.items():
            _bounded(k, _depth + 1, _budget)
            _bounded(v, _depth + 1, _budget)
    elif isinstance(value, list):
        for item in value:
            _bounded(item, _depth + 1, _budget)


def _unique(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError("duplicate field")
        out[k] = v
    return out


class BrokerHttp:
    """HTTP transport for same-daemon lifecycle, not a new process/service.

    Bind loopback by default. Remote production needs a separately verified TLS
    reverse proxy routing ONLY this endpoint. Never deploy plain public HTTP.
    """

    def __init__(self, broker: Broker, bind="127.0.0.1", port=0):
        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True
            slots = threading.BoundedSemaphore(8)

            def get_request(self):
                s, a = super().get_request()
                s.settimeout(10)
                return s, a

            def process_request(self, s, a):
                if not self.slots.acquire(blocking=False):
                    s.close()
                    return
                try:
                    super().process_request(s, a)
                except BaseException:
                    self.slots.release()
                    raise

            def process_request_thread(self, s, a):
                try:
                    super().process_request_thread(s, a)
                finally:
                    self.slots.release()

            def handle_error(self, *args):
                return

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                return

            def respond(self, status, data):
                payload = json.dumps(data, separators=(",", ":")).encode()
                self.send_response(status)
                for k, v in [
                    ("Content-Type", "application/json"),
                    ("Content-Length", str(len(payload))),
                    ("Cache-Control", "no-store"),
                    ("Connection", "close"),
                ]:
                    self.send_header(k, v)
                self.end_headers()
                self.close_connection = True
                try:
                    self.wfile.write(payload)
                except OSError:
                    pass

            def do_POST(self):
                try:
                    # Reject ambiguous framing and duplicate security headers.
                    for name in [
                        "Content-Length",
                        "X-Operator-Timestamp",
                        "X-Operator-Nonce",
                        "X-Operator-Signature",
                    ]:
                        if len(self.headers.get_all(name, [])) != 1:
                            raise Refused(400, "invalid framing")
                    if (
                        self.headers.get("Transfer-Encoding")
                        or self.headers.get("Content-Type") != "application/json"
                    ):
                        raise Refused(400, "invalid framing")
                    length = self.headers["Content-Length"]
                    if not re.fullmatch(r"[0-9]{1,5}", length) or not 1 <= int(length) <= MAX_BODY:
                        raise Refused(400, "invalid length")
                    body = self.rfile.read(int(length))
                    if len(body) != int(length):
                        raise Refused(400, "incomplete request")
                    result = broker.issue(
                        self.path,
                        self.headers["X-Operator-Timestamp"],
                        self.headers["X-Operator-Nonce"],
                        self.headers["X-Operator-Signature"],
                        body,
                    )
                    self.respond(200, result)
                except Refused as e:
                    self.respond(e.status, {"error": e.reason})
                except Exception:
                    self.respond(503, {"error": "broker unavailable"})

            def do_GET(self):
                self.respond(405, {"error": "method not allowed"})

            do_PUT = do_DELETE = do_PATCH = do_GET

        self.server = Server((bind, port), Handler)
        self.thread = None

    @property
    def address(self):
        return self.server.server_address

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        if self.thread:
            self.server.shutdown()
            self.thread.join(timeout=5)
        self.server.server_close()
