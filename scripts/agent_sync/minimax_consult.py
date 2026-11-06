"""Tool-less MiniMax bridge consult with conservative, durable spend reservations.

No agent runtime, redirects, tools, automatic retries, token estimates, or spend refunds.
Rates/limits are a pinned qualification, not a claim about future provider prices.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

MODEL = "MiniMax-M3.1-Flash"
URL = "https://api.minimax.io/v1/responses"
CONTEXT_TOKENS = 1_000_000
OUTPUT_TOKENS = 4096
MAX_PROMPT_BYTES = 150_000
MAX_RESPONSE_BYTES = 1_000_000
# Integer nano-dollars avoid floating-point under-reservations.
RESERVE_NANO_USD = CONTEXT_TOKENS * 150 + OUTPUT_TOKENS * 600
DAILY_NANO_USD = 2_000_000_000
ZONE = ZoneInfo("America/Chicago")


class ConsultUnavailable(ValueError):
    """Keep the inbox item pending; do not persist a terminal delivery."""


class ConsultCharged(ConsultUnavailable):
    """The attempt reserved spend; the outcome is ambiguous and is never retried."""


def strict_json(text):
    def pairs(rows):
        out = {}
        for k, v in rows:
            if k in out:
                raise ConsultUnavailable("duplicate JSON key")
            out[k] = v
        return out
    def constant(value):
        raise ConsultUnavailable("non-finite JSON number")
    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, TypeError) as exc:
        raise ConsultUnavailable("invalid JSON") from exc


class SpendLedger:
    """All bridge processes share this private SQLite ledger, outside disposable wake dirs.

    Full reservations are permanent. A request ID is single-use even across restarts.
    Both Chicago calendar-day and rolling-24h limits bind, including ambiguous outcomes.
    """
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        if self.path.is_symlink() or self.path.stat().st_mode & 0o077:
            raise ConsultUnavailable("unsafe spend ledger")
        self.db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS spends (request TEXT PRIMARY KEY, at REAL NOT NULL, day TEXT NOT NULL, nano INTEGER NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS clock (id INTEGER PRIMARY KEY CHECK(id=1), last REAL NOT NULL)")

    def reserve(self, request, now):
        import math
        if type(now) not in (int, float) or not math.isfinite(now) or now < 0:
            raise ConsultUnavailable("invalid clock")
        if not isinstance(request, str) or len(request) != 64 or any(c not in "0123456789abcdef" for c in request):
            raise ConsultUnavailable("invalid reservation key")
        day = datetime.fromtimestamp(now, ZONE).date().isoformat()
        try:
            self.db.execute("BEGIN IMMEDIATE")
            last = self.db.execute("SELECT last FROM clock WHERE id=1").fetchone()
            if last and now < last[0]:
                raise ConsultUnavailable("clock moved backward")
            if self.db.execute("SELECT 1 FROM spends WHERE request=?", (request,)).fetchone():
                raise ConsultCharged("attempt already reserved; outcome retained")
            daily, rolling = self.db.execute("SELECT COALESCE(SUM(CASE WHEN day=? THEN nano ELSE 0 END),0), COALESCE(SUM(CASE WHEN at>? THEN nano ELSE 0 END),0) FROM spends", (day, now - 86400)).fetchone()
            if max(daily, rolling) + RESERVE_NANO_USD > DAILY_NANO_USD:
                raise ConsultUnavailable("daily spend limit")
            self.db.execute("INSERT INTO spends VALUES(?,?,?,?)", (request, now, day, RESERVE_NANO_USD))
            self.db.execute("INSERT OR REPLACE INTO clock VALUES(1,?)", (now,))
            self.db.execute("COMMIT")
        except Exception:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ConsultUnavailable("redirect refused")


def parse_response(data):
    obj = strict_json(data)
    if not isinstance(obj, dict) or obj.get("model") != MODEL or obj.get("status") != "completed" or obj.get("error") is not None:
        raise ConsultUnavailable("unexpected model or response status")
    if obj.get("tools") not in (None, []) or obj.get("service_tier") not in (None, "standard"):
        raise ConsultUnavailable("unexpected tools or service tier")
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        raise ConsultUnavailable("missing usage")
    counts = [usage.get(k) for k in ("input_tokens", "output_tokens", "total_tokens")]
    if any(type(v) is not int or v < 0 for v in counts):
        raise ConsultUnavailable("invalid usage")
    inputs, outputs, total = counts
    if inputs > CONTEXT_TOKENS or outputs > OUTPUT_TOKENS or total != inputs + outputs:
        raise ConsultUnavailable("usage exceeds qualified envelope")
    rows = obj.get("output")
    if not isinstance(rows, list):
        raise ConsultUnavailable("invalid output")
    messages = []
    for row in rows:
        if not isinstance(row, dict) or row.get("type") not in ("reasoning", "message"):
            raise ConsultUnavailable("unexpected output item")
        if row.get("status") != "completed":
            raise ConsultUnavailable("incomplete output item")
        if row["type"] == "reasoning":
            continue
        if row.get("role") != "assistant":
            raise ConsultUnavailable("unexpected output role")
        content = row.get("content")
        if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], dict) or content[0].get("type") != "output_text" or not isinstance(content[0].get("text"), str):
            raise ConsultUnavailable("invalid message content")
        messages.append(content[0]["text"])
    if len(messages) != 1:
        raise ConsultUnavailable("expected one JSON decision")
    return strict_json(messages[0])


class MiniMaxConsult:
    def __init__(self, ledger, key_reader, *, opener=None, clock=time.time):
        self.ledger, self.key_reader, self.clock = ledger, key_reader, clock
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def run(self, prompt, request_key):
        if not isinstance(prompt, str) or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
            raise ConsultUnavailable("prompt exceeds byte limit")
        key = self.key_reader()
        if not isinstance(key, str) or not key or any(c.isspace() for c in key):
            raise ConsultUnavailable("credential unavailable")
        body = json.dumps({"model": MODEL, "input": prompt, "max_output_tokens": OUTPUT_TOKENS, "reasoning": {"effort": "low"}, "stream": False}, ensure_ascii=True).encode()
        request = urllib.request.Request(URL, body, {"Authorization": "Bearer " + key, "Content-Type": "application/json"}, method="POST")
        # Durable before HTTP. No ambiguous reservation can be replayed or refunded.
        self.ledger.reserve(request_key, self.clock())
        try:
            with self.opener.open(request, timeout=60) as response:
                if response.geturl() != URL or response.status != 200:
                    raise ConsultUnavailable("unexpected HTTP response")
                data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise ConsultUnavailable("response too large")
            return parse_response(data)
        except Exception as exc:
            # Never put remote error bodies or credentials into logs.  The reservation already
            # landed, so this attempt was charged:  the caller settles the row for review and
            # never retries it.
            raise ConsultCharged("consult unavailable; reservation retained") from exc


class ScopedKeyReader:
    """One named key through the caller's private read-only Infisical service token.

    No root listing, universal-auth identity, wrapper, environment API-key fallback,
    secret writes, or local API-key copy. Returned value stays in process memory.
    """
    ORIGIN = "https://app.infisical.com"
    KEY = "MINIMAX_API_KEY"

    def __init__(self, token_file, project_id, *, secret_path="/", opener=None, clock=time.time):
        # secret_path is the exact scope the token must prove, nothing broader:  the metadata
        # check below refuses a token whose grant does not match it exactly.
        if not isinstance(secret_path, str) or not secret_path.startswith("/"):
            raise ConsultUnavailable("invalid scoped secret path")
        self.secret_path = secret_path
        self.token_file, self.project_id = token_file, project_id
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        self.clock = clock
        self._value = ""
        self._refresh_at = 0.0

    def _token(self):
        import stat
        if not self.token_file or not self.project_id:
            raise ConsultUnavailable("scoped credential binding missing")
        try:
            fd = os.open(self.token_file, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as f:
                st = os.fstat(f.fileno())
                if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
                    raise ConsultUnavailable("unsafe token file")
                raw = f.read(4097)
            if len(raw) > 4096:
                raise ConsultUnavailable("oversized token file")
            token = raw.decode().strip()
        except (OSError, UnicodeError) as exc:
            raise ConsultUnavailable("scoped credential binding unavailable") from exc
        if not token.startswith("st.") or not 8 <= len(token) <= 512 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            raise ConsultUnavailable("invalid scoped token")
        return token

    def _get(self, path, token):
        request = urllib.request.Request(self.ORIGIN + path, headers={"Authorization": "Bearer " + token, "Accept": "application/json"}, method="GET")
        with self.opener.open(request, timeout=10) as response:
            if response.geturl() != self.ORIGIN + path or response.status != 200:
                raise ConsultUnavailable("credential redirect/status refused")
            data = response.read(65537)
        if len(data) > 65536:
            raise ConsultUnavailable("credential response too large")
        return strict_json(data)

    def binding_ready(self):
        """True when the local binding (token file + project id) is present and well-formed.

        Presence only - no network, no Infisical call, no ledger write.  A consult whose
        network read still fails before charging is unwound by the caller's pending path."""
        try:
            self._token()
        except ConsultUnavailable:
            return False
        return bool(self.project_id)

    def __call__(self):
        from urllib.parse import urlencode
        now = self.clock()
        if now < self._refresh_at:
            if not self._value:
                raise ConsultUnavailable("credential read backed off")
            return self._value
        self._value = ""
        self._refresh_at = now + 120
        try:
            token = self._token()
            meta = self._get("/api/v2/service-token", token)
            if not isinstance(meta, dict) or meta.get("workspace") != self.project_id or meta.get("projectId", self.project_id) != self.project_id or meta.get("scopes") != [{"environment": "prod", "secretPath": self.secret_path}] or meta.get("permissions") != ["read"]:
                raise ConsultUnavailable("scoped token grant mismatch")
            expiry = meta.get("expiresAt")
            if expiry is not None:
                dt = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
                if dt.tzinfo is None or dt.timestamp() <= now:
                    raise ConsultUnavailable("scoped token expired")
            query = urlencode({"workspaceId": self.project_id, "environment": "prod", "secretPath": self.secret_path})
            data = self._get("/api/v3/secrets/raw/" + self.KEY + "?" + query, token)
            row = data.get("secret") if isinstance(data, dict) else None
            value = row.get("secretValue") if isinstance(row, dict) and row.get("secretKey") == self.KEY else None
            if not isinstance(value, str) or not 8 <= len(value) <= 512 or any(ord(c) < 33 or ord(c) > 126 for c in value):
                raise ConsultUnavailable("invalid named API key")
            self._value = value
            self._refresh_at = min(now + 600, dt.timestamp()) if expiry is not None else now + 600
            return value
        except Exception as exc:
            raise ConsultUnavailable("scoped named-key read unavailable") from exc
