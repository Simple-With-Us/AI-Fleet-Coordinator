"""Shared per-identity bearer verification against ST-MCP (owner direction 2026-10-23:
one credential per identity, shared between ST-MCP and GR-MCP, minted in the admin panel).

A seat presents its admin-minted bearer (today the gbb_<role>_<secret> family).  This
module resolves the bearer to its identity by asking the Worker's HMAC-authed internal
verify route.  The raw bearer NEVER leaves this process:  only its SHA-256 hex travels,
inside a body signed with the same X-Simple-Tools-Signature scheme as the wake listener
(HMAC-SHA256 over the raw bytes, freshness window on sent_at).  The verify key itself
is read through the fleet-secrets CLI only, like every other credential this service
touches - never from the process environment.

Failure shape:  fail closed.  A malformed token, an unreachable Worker, a bad answer,
an expired or paused identity - all resolve to None (the caller answers 401).  Neither
the bearer nor its hash is ever logged or included in an error string.

A live positive answer is reused for up to an hour by default (owner ruling 2026-11-06:
a checked token may work for an hour, because a seat is never cut off through the
tools), never past the bearer's own expiry.  So a revocation or pause can reach a
seat's Git calls, writes included, up to an hour late in one relay process.  The hour
runs from the Worker's own live check (its answer's verified_at), so a Worker answer
served from the Worker's cache does not start a second hour.
GIT_RELAY_VERIFY_CACHE_S sets a shorter window, and 0 turns the reuse off.  Only a
refusal is remembered as one:  a Worker that is down or over its daily limit says
nothing about the bearer, so that call fails and the next one asks again.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
import urllib.error
import urllib.request

VERIFY_CONTRACT = "gr-bearer-verify/1"
SIGNATURE_HEADER = "X-Simple-Tools-Signature"

# The bearer families this resolver understands, both admin-minted:
#   gbb_<role>_<43>      a Grok bot (identity GB-<ROLE>)
#   sbt_<seat>_<43>      an OAuth-capable seat (identity is its KNOWN_SEATS name)
# Further families join here when the admin panel mints them.
TOKEN_RES = (
    re.compile(r"^gbb_([a-z][a-z0-9]{1,19})_([A-Za-z0-9_-]{43})$"),
    re.compile(r"^sbt_([a-z][a-z0-9-]{1,19})_([A-Za-z0-9_-]{43})$"),
)

MAX_RESPONSE_BYTES = 4096
REQUEST_TIMEOUT_S = 10
# Positive answers are reused for an hour by default (owner ruling 2026-11-06), so one
# relay process asks the Worker about a bearer at most once an hour instead of on every
# Git call (each ask is a Worker request and a SeatGate RPC against daily caps).  The
# trade:  a revoked or paused identity can keep GR access, writes included, for up to
# the window.  GIT_RELAY_VERIFY_CACHE_S sets the window in seconds, capped at
# POSITIVE_CACHE_CAP_S;  0 turns it off.  An entry never outlives the bearer's expiry.
DEFAULT_POSITIVE_CACHE_S = "3600"
POSITIVE_CACHE_CAP_S = 3600
NEGATIVE_CACHE_S = 30  # a wrong guess must not hammer the Worker
# The verify route answers a bearer's verdict as a 200, so the only non-200s that can be
# remembered as refusals are its own 400 (an identity it does not know) and 401, and a
# 403.  Anything else says nothing about the bearer:  a 404 is the route's not_configured,
# which is how a missing or unreadable verify key looks, and 5xx, 429 and redirects are
# about the Worker.  Error 1027, the daily limit, is sent as a 429 or names itself, in
# prose ("Error 1027", "error code: 1027") or JSON ("error":1027, "error_code":1027).
REFUSAL_STATUSES = frozenset({400, 401, 403})
WORKER_LIMIT_RE = re.compile(
    rb"""(?:error[\s_-]*(?:code)?|\bcode)["']?\s*:?\s*["']?1027\b""", re.IGNORECASE
)
# The cache key is attacker-controlled (any string hashes), so the map is
# bounded:  oldest entries are evicted past the cap.
CACHE_MAX_ENTRIES = 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _VerifyUnavailable(Exception):
    """No verdict about the bearer.  resolve() fails closed and remembers nothing."""


class SharedBearerVerifier:
    """Resolves a presented bearer to its identity via the ST-MCP verify route.

    `key_source` is anything with .secret(name) -> str (FleetSecretsSource in
    production, a fake in tests).  `transport(method, url, headers, body, timeout)`
    is injectable for tests;  it must return (status, response_bytes).
    """

    def __init__(self, url: str, key_name: str, key_source, transport=None):
        if not url.startswith("https://"):
            raise ValueError("git-relay config: verify URL must be https")
        self._url = url
        self._key_name = key_name
        self._key = key_source.secret(key_name)  # raises at startup when unreadable
        if len(self._key) < 32:
            raise ValueError("git-relay config: bearer verify key is shorter than 32 characters")
        self._transport = transport or self._urllib_transport
        # Guards every cache read/evict/write: the server is threaded, and a
        # non-atomic min()+del under concurrency raised RuntimeError/KeyError,
        # which the fail-closed handler turned into a false refusal of a valid
        # bearer.  The remote verify stays outside the lock (slow IO).
        self._lock = threading.Lock()
        import os

        try:
            self._positive_ttl = min(
                float(os.environ.get("GIT_RELAY_VERIFY_CACHE_S", DEFAULT_POSITIVE_CACHE_S)),
                POSITIVE_CACHE_CAP_S,
            )
        except ValueError:
            self._positive_ttl = 0.0
        # token hash -> (identity|None, valid_until, inserted_at);  bounded.
        self._cache: dict[str, tuple[str | None, float, float]] = {}
        self._opener = urllib.request.build_opener(_NoRedirect())

    @staticmethod
    def _urllib_transport(method, url, headers, body, timeout):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout) as resp:
                return resp.status, resp.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            # Kept only to recognize the Worker-limit marker;  never logged.
            return exc.code, exc.read(MAX_RESPONSE_BYTES + 1)

    @staticmethod
    def parse_identity(token: str) -> str | None:
        """The identity a bearer names, or None when malformed.

        gbb_ names a bot (GB-<ROLE>);  sbt_ names a seat exactly as its
        KNOWN_SEATS entry (JET-CHAT included, hyphen preserved).
        """
        if not isinstance(token, str):
            return None
        token = token.strip()
        m = TOKEN_RES[0].match(token)
        if m:
            return f"GB-{m.group(1).upper()}"
        m = TOKEN_RES[1].match(token)
        if m:
            return m.group(1).upper()
        return None

    def resolve(self, token: str) -> str | None:
        """The verified identity for a presented bearer, or None.  Never raises."""
        try:
            identity = self.parse_identity(token)
            if identity is None:
                return None
            digest = hashlib.sha256(token.strip().encode()).hexdigest()
            with self._lock:
                cached = self._cache.get(digest)
            now = time.time()
            if cached and cached[1] > now:
                return cached[0]
            resolved = self._verify_remote(identity, digest, now)
            # Positive answers cache for the TTL above, never past the bearer's
            # own expiry, and never more than the TTL after the Worker's own
            # live check (verified_at):  the Worker may answer from its cache,
            # and the two layers together must stay within one window.
            # Refusals always cache briefly, or a guessing loop costs one
            # Worker call per request.
            with self._lock:
                if len(self._cache) >= CACHE_MAX_ENTRIES:
                    oldest = min(self._cache, key=lambda k: self._cache[k][2])
                    del self._cache[oldest]
                if resolved is not None:
                    identity_name, expires_at_s, verified_at_s = resolved
                    ttl = min(
                        self._positive_ttl,
                        max(0.0, expires_at_s - now),
                        max(0.0, verified_at_s + self._positive_ttl - now),
                    )
                    # A verdict stored by a call that started after this one
                    # (a refusal, above all) is newer than this answer:  an
                    # older positive answer never writes over it.
                    newer = self._cache.get(digest)
                    if ttl > 0 and not (newer is not None and newer[2] > now):
                        self._cache[digest] = (identity_name, now + ttl, now)
                else:
                    self._cache[digest] = (None, now + NEGATIVE_CACHE_S, now)
            if resolved is not None:
                return identity_name
            return None
        except _VerifyUnavailable:
            return None  # fail closed, and let the next call ask again
        except Exception:
            return None  # fail closed, always

    def _verify_remote(self, identity: str, digest: str, now: float):
        body = json.dumps(
            {
                "contract": VERIFY_CONTRACT,
                "identity": identity,
                "hash": digest,
                "sent_at": int(now),
            },
            separators=(",", ":"),
        ).encode()
        signature = hmac.new(self._key.encode(), body, hashlib.sha256).hexdigest()
        status, payload = self._transport(
            "POST",
            self._url,
            {
                "Content-Type": "application/json",
                "User-Agent": "git-relay/1.0 (+ops)",
                SIGNATURE_HEADER: signature,
            },
            body,
            REQUEST_TIMEOUT_S,
        )
        if status != 200:
            if status not in REFUSAL_STATUSES or WORKER_LIMIT_RE.search(payload):
                raise _VerifyUnavailable
            return None
        if not payload or len(payload) > MAX_RESPONSE_BYTES:
            raise _VerifyUnavailable
        try:
            answer = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            raise _VerifyUnavailable from None
        if not isinstance(answer, dict):
            raise _VerifyUnavailable
        if answer.get("ok") is not True:
            return None
        # The answer must name the identity we asked about - a route that
        # answers for a different identity is a bug or an attack, not auth.
        if answer.get("identity") != identity:
            return None
        expires_at = answer.get("expires_at")
        if not isinstance(expires_at, (int, float)) or expires_at <= now * 1000:
            return None  # expires_at is epoch milliseconds;  already-expired refuses
        # verified_at (epoch ms) is when the Worker last checked the bearer
        # live;  an older Worker omits it, and then the check counts as now.
        # A value from the future counts as now too, so it can never stretch
        # the window.
        verified_at = answer.get("verified_at")
        if isinstance(verified_at, bool) or not isinstance(verified_at, (int, float)):
            verified_at_s = now
        else:
            verified_at_s = min(now, verified_at / 1000.0)
        return identity, expires_at / 1000.0, verified_at_s
