"""Hosted-OAuth identity assertions for git-relay (Worker-minted, callback-verified).

An OAuth connection to ST-MCP carries no admin-minted bearer, so there is nothing
the relay's shared-bearer verifier could resolve.  Instead the Worker, after its
own grant checks (seat, epoch, phase, grant age, pause) pass, mints a SHORT-LIVED,
PER-CALL HMAC assertion and sends it as the call's bearer.  The signing key lives
in the dedicated Fleet-OPS /gr-assert-signing runtime folder:  this process holds NO mint-capable
material at all, so the relay verifies by asking the Worker back over the same
signed internal channel as the shared-bearer route (GR_BEARER_VERIFY_KEY, the
X-Agent-Sync-Signature wake scheme - git_relay/shared_bearer.py).

    token   gra2_<base64url JSON claims>_<64 hex HMAC-SHA256>
    claims  {v:"gr-assert/2", aud:"git-relay", seat, iat, exp, jti, action,
             body: sha256-hex of the exact request body, scopes, epoch,
             approved_at, phase, grant_ref}

What each side enforces:

  - here (local, before any network call):  shape, contract version, audience,
    the TTL clamp (exp-iat <= MAX_TTL_S, iat not in the future beyond
    FUTURE_SKEW_S, not expired), and that the action and body SHA-256 the
    Worker signed match THIS request - a captured assertion cannot be re-aimed
    at another action or payload.
  - the Worker (authoritative, per call):  the HMAC itself, the same window
    again, the action/body match again, the seat's live gate check (epoch,
    phase, pause) so a revocation or pause refuses immediately, and a durable
    one-use claim of the jti so a replayed assertion refuses.
  - policy.py:  the seat must be flagged oauth_assert with a configured
    identity matching the verified seat, and a write action needs the
    "zulip:write" scope inside the signed claims - a read-scope grant's
    assertion can never authorize a write.

Positive answers are NEVER cached:  each assertion is one-use, so every
presentation must reach the Worker's jti claim.  Refusals cache briefly so a
guessing loop costs little Worker traffic.

Failure shape:  fail closed.  Malformed, stale, mis-bound, replayed, revoked
or unverifiable tokens resolve to None (the caller answers 401).  The token
and the key are never logged or included in an error string.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import threading
import time
import urllib.error
import urllib.request

CONTRACT = "gr-assert/2"
VERIFY_CONTRACT = "gr-assert-verify/1"
AUDIENCE = "git-relay"
MAX_TTL_S = 120
FUTURE_SKEW_S = 60
MIN_KEY_LEN = 32

SIGNATURE_HEADER = "X-Agent-Sync-Signature"
MAX_RESPONSE_BYTES = 4096
REQUEST_TIMEOUT_S = 10
NEGATIVE_CACHE_S = 30  # a refused or replayed assertion must not hammer the Worker
CACHE_MAX_ENTRIES = 1024

TOKEN_RE = re.compile(r"^gra2_([A-Za-z0-9_-]+)_([0-9a-f]{64})$")
SEAT_RE = re.compile(r"^[A-Z][A-Z0-9-]{0,31}$")
JTI_RE = re.compile(r"^[A-Za-z0-9_-]{16}$")
ACTION_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
GRANT_REF_RE = re.compile(r"^[0-9a-f]{12}$")
SCOPE_RE = re.compile(r"^[a-z][a-z0-9:_-]{0,63}$")
KNOWN_SCOPES = frozenset({"zulip:read", "zulip:write"})
MAX_CLAIMS_BYTES = 2048


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _b64url_decode(text: str) -> bytes | None:
    try:
        pad = "=" * (-len(text) % 4)
        return base64.urlsafe_b64decode(text + pad)
    except Exception:
        return None


def parse_claims(token: str) -> dict | None:
    """The claims of an assertion-shaped token, or None.  Structural and local
    window checks only - the HMAC is verified by the Worker, never here."""
    if not isinstance(token, str):
        return None
    m = TOKEN_RE.match(token.strip())
    if not m:
        return None
    raw = _b64url_decode(m.group(1))
    if raw is None or len(raw) > MAX_CLAIMS_BYTES:
        return None
    try:
        claims = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(claims, dict):
        return None
    # The exact key set the Worker mints:  an extra or missing key means a
    # token this Worker never produced (mirrors the Worker's own strict
    # buildClaims round-trip).
    if set(claims) != {
        "v",
        "aud",
        "seat",
        "iat",
        "exp",
        "jti",
        "action",
        "body",
        "scopes",
        "epoch",
        "approved_at",
        "phase",
        "grant_ref",
    }:
        return None
    if claims.get("v") != CONTRACT or claims.get("aud") != AUDIENCE:
        return None
    seat = claims.get("seat")
    if not isinstance(seat, str) or not SEAT_RE.match(seat):
        return None
    iat, exp = claims.get("iat"), claims.get("exp")
    # bool is an int subclass:  JSON true/false is not a timestamp.
    if (
        not isinstance(iat, int)
        or isinstance(iat, bool)
        or not isinstance(exp, int)
        or isinstance(exp, bool)
        or exp <= iat
        or exp - iat > MAX_TTL_S
    ):
        return None
    if not isinstance(claims.get("jti"), str) or not JTI_RE.match(claims["jti"]):
        return None
    if not isinstance(claims.get("action"), str) or not ACTION_RE.match(claims["action"]):
        return None
    if not isinstance(claims.get("body"), str) or not HEX64_RE.match(claims["body"]):
        return None
    scopes = claims.get("scopes")
    if (
        not isinstance(scopes, list)
        or len(scopes) > 8
        or any(
            not isinstance(s, str) or not SCOPE_RE.match(s) or s not in KNOWN_SCOPES for s in scopes
        )
    ):
        return None
    epoch, approved_at, phase = claims.get("epoch"), claims.get("approved_at"), claims.get("phase")
    if (
        not isinstance(epoch, int)
        or isinstance(epoch, bool)
        or not isinstance(phase, int)
        or isinstance(phase, bool)
    ):
        return None
    if not isinstance(approved_at, (int, float)) or isinstance(approved_at, bool):
        return None
    if not isinstance(claims.get("grant_ref"), str) or not GRANT_REF_RE.match(claims["grant_ref"]):
        return None
    now = time.time()
    if now >= exp:
        return None
    if iat - now > FUTURE_SKEW_S:
        return None
    return claims


class GrAssertionVerifier:
    """Verifies Worker-minted per-call assertions via the Worker's verify route.

    `key_source` is anything with .secret(name) -> str (FleetSecretsSource in
    production, a fake in tests);  the key is the SAME GR_BEARER_VERIFY_KEY the
    shared-bearer route uses - the relay needs no mint-capable secret, only the
    callback channel key.  `transport(method, url, headers, body, timeout)` is
    injectable for tests;  it must return (status, response_bytes).
    """

    def __init__(self, url: str, key_name: str, key_source, transport=None):
        if not url.startswith("https://"):
            raise ValueError("git-relay config: assertion verify URL must be https")
        self._url = url
        self._key = key_source.secret(key_name)  # raises at startup when unreadable
        if len(self._key) < MIN_KEY_LEN:
            raise ValueError("git-relay config: bearer verify key is shorter than 32 characters")
        self._transport = transport or self._urllib_transport
        self._lock = threading.Lock()
        # token hash -> refused-until;  refusals only, positives never cache
        # (a cached positive would skip the Worker's one-use jti claim).
        self._cache: dict[str, tuple[float, float]] = {}

    @staticmethod
    def _urllib_transport(method, url, headers, body, timeout):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.build_opener(_NoRedirect()).open(req, timeout=timeout) as resp:
                return resp.status, resp.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            return exc.code, b""

    @staticmethod
    def parse_identity(token: str) -> str | None:
        """The seat an assertion-shaped token names (unverified), or None.

        Used by policy to refuse assertion-shaped tokens locally without any
        other path when no verifier is configured, or when verification fails.
        """
        claims = parse_claims(token)
        return claims["seat"] if claims is not None else None

    def resolve(self, token: str, proof: dict | None) -> tuple[str, dict] | None:
        """(seat, claims) for a presented assertion under this request's proof,
        or None.  Never raises.  `proof` carries {"action", "body_sha256"} of
        the exact request the token rides on;  a mismatch is a refusal."""
        try:
            claims = parse_claims(token)
            if claims is None:
                return None
            if not isinstance(proof, dict):
                return None
            if proof.get("action") != claims["action"]:
                return None
            if proof.get("body_sha256") != claims["body"]:
                return None
            digest = hashlib.sha256(token.strip().encode()).hexdigest()
            now = time.time()
            with self._lock:
                refused = self._cache.get(digest)
            if refused and refused[0] > now:
                return None
            if not self._verify_remote(token, claims, now):
                with self._lock:
                    if len(self._cache) >= CACHE_MAX_ENTRIES:
                        oldest = min(self._cache, key=lambda k: self._cache[k][1])
                        del self._cache[oldest]
                    self._cache[digest] = (now + NEGATIVE_CACHE_S, now)
                return None
            return claims["seat"], claims
        except Exception:
            return None  # fail closed, always

    def _verify_remote(self, token: str, claims: dict, now: float) -> bool:
        body = json.dumps(
            {
                "contract": VERIFY_CONTRACT,
                "token": token.strip(),
                "action": claims["action"],
                "body_sha256": claims["body"],
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
        if status != 200 or not payload or len(payload) > MAX_RESPONSE_BYTES:
            return False
        try:
            answer = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            return False
        if not isinstance(answer, dict) or answer.get("ok") is not True:
            return False
        # The answer must name the seat the signed claims name - a route that
        # answers for another seat is a bug or an attack, not auth.
        return answer.get("seat") == claims["seat"]
