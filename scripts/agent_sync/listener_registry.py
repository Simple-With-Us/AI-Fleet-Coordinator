"""The Operator's session registry over the listener's HTTP API (Path B, operator.simplewithus.com).

A thin adapter over `operator_registry.py`'s existing op functions, served by the existing
listener (listener_api.py) under /registry/v1/, with the same JSON answer contract as the
registry CLI / local path.  The Operator is the fleet's deterministic coordination bot;  its
Zulip listener is eavesdropper, and this module is part of the Operator, never branded by a
model or a seat.

    GET   /registry/v1/resolve?platform=P&letter=L[&generation=N]
    GET   /registry/v1/history?platform=P[&limit=N]
    GET   /registry/v1/held?record_id=R   (or the session identity)
    GET   /registry/v1/pending-outbox[?limit=N]
    POST  /registry/v1/register        POST  /registry/v1/relabel
    POST  /registry/v1/update          POST  /registry/v1/close
    POST  /registry/v1/touch           POST  /registry/v1/reopen
    POST  /registry/v1/sweep           POST  /registry/v1/mark-exported

Reads are GET, mutating ops are POST only:  a known op with the wrong method is 405, an
unknown route is 404.  Authentication reuses the listener's existing pattern:  a per-principal
HMAC-SHA256 signature (the principal is a seat, or OPERATOR), keyed with the principal's
existing per-seat key (the listener's routine `key_env` pattern;  no new secret architecture).
Three headers:

    X-Agent-Sync-Principal:  a seat code (CLAUDE, CODEX, MM, ...) or OPERATOR
    X-Agent-Sync-Timestamp:  unix seconds
    X-Agent-Sync-Signature:  lowercase hex HMAC-SHA256 over
        "agent-sync-registry/1\\n<METHOD>\\n<path>\\n<raw query>\\n<timestamp>\\n<principal>\\n<sha256 hex of the body>"

good for five minutes either side of the listener's clock.  A principal with no key (or a key
under 32 characters) is 404, like an unknown one;  a bad or stale signature is 401.

Authority, enforced here and in `operator_registry` itself:

  * Only the Operator issues a letter (owner ruling, Oct 21 2026):  `register` and `relabel`
    require the OPERATOR principal, and the issuer handed to the registry is the authenticated
    principal, never a value the request body carries.  Any other issuer is refused (403).
  * A seat acts only on its own sessions:  `update`, `close`, `touch`, `reopen` and `held`
    check the stored row's seat against the principal, `sweep` may name only the principal's
    own seat, `resolve` answers only for the principal's own session, and `history` is
    filtered to the principal's own rows.  The OPERATOR principal acts for any seat.
  * The export outbox is the Operator's own:  `pending-outbox` and `mark-exported` require
    the OPERATOR principal.
  * No op brings a store into being:  every op, `register` and `relabel` included, answers
    503 until the host's store exists.  A store is created as a local act on the host that
    holds it, never through this API, so a misdirected call cannot start a second registry.

Op mapping (the registry's 12 ops):  register -> register_session, relabel -> relabel_session,
resolve -> resolve_label, update -> update_session, close -> close_session, sweep ->
sweep_stale, history -> letter_history, touch -> touch_session, reopen ->
SessionRegistry.reopen (through platform_letter.operator_reopen, so a pair letter stays a
pair), held -> SessionRegistry.letters_held, pending-outbox -> pending_outbox,
mark-exported -> mark_exported.  `reopen` and `held` have no CLI verb on this tree;  their
answers follow the shapes the existing callers already use (reopen_closed in
mm_session_letter.py, and a plain letters list for held).

Python 3.11+, standard library only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from . import operator_registry as OR

CONTRACT = "agent-sync-registry/1"
PREFIX = "/registry/v1/"
PRINCIPAL_HEADER = "X-Agent-Sync-Principal"
TIMESTAMP_HEADER = "X-Agent-Sync-Timestamp"
SIGNATURE_HEADER = "X-Agent-Sync-Signature"
MAX_SKEW_S = 300
MIN_KEY_LEN = 32
MAX_BODY = 64 * 1024
MAX_QUERY = 1024
#: The principal that may issue a letter:  the Operator itself (operator_registry.ISSUER_OPERATOR).
OPERATOR = OR.ISSUER_OPERATOR
#: Where the OPERATOR principal's key lives in the listener's environment:  a named env var
#: holding the key, the same `key_env` pattern as a seat's routine key.  The value is a key,
#: never logged.
OPERATOR_KEY_ENV = "OPERATOR_REGISTRY_KEY"

_PRINCIPAL_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,31}$")
_STAMP_RE = re.compile(r"^\d{1,12}$")

READ_OPS = ("resolve", "history", "held", "pending-outbox")
WRITE_OPS = (
    "register",
    "relabel",
    "update",
    "close",
    "sweep",
    "touch",
    "reopen",
    "mark-exported",
)
OPS = READ_OPS + WRITE_OPS
_METHODS = {op: "GET" for op in READ_OPS} | {op: "POST" for op in WRITE_OPS}

Answer = tuple[int, dict[str, Any]]


def body_hash(body: bytes) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def signing_string(
    method: str, path: str, query: str, timestamp: str, principal: str, body: bytes
) -> bytes:
    return (
        "%s\n%s\n%s\n%s\n%s\n%s\n%s"
        % (CONTRACT, method.upper(), path, query, timestamp, principal, body_hash(body))
    ).encode("utf-8")


def sign(
    key: str, method: str, path: str, query: str, timestamp: str, principal: str, body: bytes
) -> str:
    return hmac.new(
        key.encode("utf-8"),
        signing_string(method, path, query, timestamp, principal, body),
        hashlib.sha256,
    ).hexdigest()


def headers(
    key: str, method: str, path: str, query: str, principal: str, body: bytes, now: float
) -> dict[str, str]:
    """The three signed headers (and the content type) for one request, for clients."""
    timestamp = str(int(now))
    out = {
        PRINCIPAL_HEADER: principal,
        TIMESTAMP_HEADER: timestamp,
        SIGNATURE_HEADER: sign(key, method, path, query, timestamp, principal, body),
    }
    if body:
        out["Content-Type"] = "application/json"
    return out


def _error(status: int, code: str, message: str) -> Answer:
    return status, {"error": code, "message": message}


def _header(headers: Mapping[str, str], name: str) -> str:
    value = headers.get(name)
    if isinstance(value, str):
        return value
    lowered = name.lower()
    for key, value in headers.items():
        if isinstance(key, str) and key.lower() == lowered and isinstance(value, str):
            return value
    return ""


def _identity(payload: Mapping[str, Any]) -> dict[str, str]:
    """The session identity in a request, with the CLI's spellings folded in:
    --location is verified_host, --scope is profile_scope, --session-id is native_session_id."""

    def pick(*names: str) -> str:
        for name in names:
            value = payload.get(name)
            if isinstance(value, str) and value:
                return value
        return ""

    return {
        # The identity is folded to the form the registry stores, here at the one
        # seam every op passes through:  register_session stores the platform
        # folded by normalize_platform and the session id trimmed by
        # session_ids.check, and the mutation functions fold the same way before
        # their lookups.  The ownership pre-check (_own_row) compares raw stored
        # values, so an unfolded identity (platform "MiniMax", a padded session
        # id) would miss the stored row, read as "no row, no refusal", and the
        # mutation would then find and change another seat's session.
        "platform": OR.normalize_platform(pick("platform")),
        "verified_host": pick("verified_host", "location"),
        "profile_scope": pick("profile_scope", "scope"),
        "native_session_id": pick("native_session_id", "session_id").strip(),
    }


class RegistryService:
    """The registry adapter over one registry file.  `keys` maps principal (a seat code, or
    OPERATOR) -> shared HMAC key;  a principal whose key is missing or shorter than 32
    characters is not accepted at all (404, like an unknown one).  Construction touches no
    file:  the store is opened per call, by the registry's own functions."""

    def __init__(
        self,
        db_path: str,
        keys: Mapping[str, str],
        *,
        clock: Callable[[], float] = time.time,
        log: Callable[..., None] | None = None,
    ) -> None:
        self.db_path = str(db_path)
        self.keys = {
            principal.upper(): key
            for principal, key in keys.items()
            if isinstance(principal, str)
            and _PRINCIPAL_RE.fullmatch(principal.upper())
            and isinstance(key, str)
            and len(key) >= MIN_KEY_LEN
        }
        self.clock = clock
        self.log = log or (lambda *_args, **_kwargs: None)

    # ---- authentication -------------------------------------------------

    def authenticate(
        self, method: str, path: str, query: str, headers: Mapping[str, str], body: bytes
    ) -> str | Answer:
        name = _header(headers, PRINCIPAL_HEADER).strip().upper()
        key = self.keys.get(name)
        if key is None:
            return _error(404, "not_found", "not found")
        timestamp = _header(headers, TIMESTAMP_HEADER).strip()
        if not _STAMP_RE.fullmatch(timestamp) or abs(self.clock() - int(timestamp)) > MAX_SKEW_S:
            self.log("registry-api-refused", principal=name, reason="timestamp")
            return _error(401, "unauthorized", "unauthorized")
        expected = sign(key, method, path, query, timestamp, name, body)
        signature = _header(headers, SIGNATURE_HEADER).strip().lower()
        if not signature or not hmac.compare_digest(expected, signature):
            self.log("registry-api-refused", principal=name, reason="signature")
            return _error(401, "unauthorized", "unauthorized")
        return name

    # ---- the router ------------------------------------------------------

    def handle(self, method: str, raw_path: str, headers: Mapping[str, str], body: bytes) -> Answer:
        """(status, JSON body) for one request under /registry/v1/.  Pure apart from the
        registry file, so tests call it without a socket."""
        parts = urlsplit(raw_path)
        path, query = parts.path, parts.query
        if not path.startswith(PREFIX) or parts.fragment or len(query) > MAX_QUERY:
            return _error(404, "not_found", "not found")
        op = path[len(PREFIX) :]
        if op not in _METHODS or "/" in op:
            return _error(404, "not_found", "not found")
        method = method.upper()
        if method != _METHODS[op]:
            return _error(405, "method_not_allowed", "method not allowed")
        if len(body) > MAX_BODY:
            return _error(413, "too_large", "the request body is too large")
        principal = self.authenticate(method, path, query, headers, body)
        if not isinstance(principal, str):
            return principal
        payload: dict[str, Any] = {}
        if method == "GET":
            for name, value in parse_qsl(query, keep_blank_values=True):
                if name in payload:
                    return _error(400, "invalid_argument", "a repeated query parameter")
                payload[name] = value
        elif body:
            try:
                parsed = json.loads(body.decode("utf-8"))
                json.dumps(parsed, ensure_ascii=False).encode("utf-8")
            except (UnicodeError, ValueError, RecursionError):
                return _error(400, "invalid_argument", "the body is not JSON")
            if not isinstance(parsed, dict):
                return _error(400, "invalid_argument", "the body must be a JSON object")
            payload = parsed
        try:
            status, answer = getattr(self, "_op_" + op.replace("-", "_"))(principal, payload)
        except OR.LabelExhausted as exc:
            return 409, {"error": str(exc), "code": "labels_exhausted"}
        except OR.SessionClosed as exc:
            return 409, {"error": str(exc), "code": "closed_session"}
        except OR.SessionUnknown as exc:
            return 404, {"error": str(exc), "code": "not_registered"}
        except OR.RegistryUnavailable as exc:
            return _error(503, "unavailable", str(exc)[:300])
        except (OR.UnknownRecord, KeyError):
            return 404, {"error": "no session with this record_id", "code": "not_registered"}
        except ValueError as exc:
            return 400, {"error": str(exc)[:400]}
        self.log("registry-api-op", principal=principal, op=op, status=status)
        return status, answer

    # ---- authority helpers ------------------------------------------------

    @staticmethod
    def _operator_only(principal: str) -> Answer | None:
        if principal != OPERATOR:
            return _error(403, "issuer_refused", "only the Operator may issue a letter")
        return None

    def _row_for(self, identity: Mapping[str, str]) -> Any:
        """The stored row for one identity, or None.  Read-only:  never creates the file."""
        if not os.path.isfile(self.db_path):
            raise OR.RegistryUnavailable("the registry %s is not there" % self.db_path)
        registry = OR.SessionRegistry(self.db_path, read_only=True)
        try:
            return registry.session_by_identity(
                platform=identity["platform"],
                verified_host=identity["verified_host"],
                profile_scope=identity["profile_scope"],
                native_session_id=identity["native_session_id"],
            )
        finally:
            registry.close_db()

    def _own_row(self, principal: str, identity: Mapping[str, str]) -> tuple[Any, Answer | None]:
        """(row, refusal):  the identity's row when the principal may act on it.  A missing
        row is no refusal:  the op itself answers for it, in the registry's own words."""
        row = self._row_for(identity)
        if row is None or principal == OPERATOR:
            return row, None
        if str(row["seat"]).strip().upper() != principal:
            return None, _error(403, "forbidden", "a seat may act only on its own sessions")
        return row, None

    def _require_store(self) -> None:
        if not os.path.isfile(self.db_path):
            raise OR.RegistryUnavailable("the registry %s is not there" % self.db_path)

    # ---- reads (GET) --------------------------------------------------------

    def _op_resolve(self, principal: str, payload: Mapping[str, Any]) -> Answer:
        self._require_store()
        generation = payload.get("generation")
        found = OR.resolve_label(
            self.db_path,
            str(payload.get("platform") or ""),
            str(payload.get("letter") or ""),
            int(generation) if generation not in (None, "") else None,
        )
        if found is None:
            return 200, {"label": str(payload.get("letter") or ""), "found": False}
        if principal != OPERATOR and str(found.get("seat") or "").strip().upper() != principal:
            return _error(403, "forbidden", "a seat may act only on its own sessions")
        return 200, found

    def _op_history(self, principal: str, payload: Mapping[str, Any]) -> Answer:
        self._require_store()
        limit = payload.get("limit", 50)
        rows = OR.letter_history(self.db_path, str(payload.get("platform") or ""), int(limit or 50))
        if principal != OPERATOR:
            rows = [row for row in rows if str(row.get("seat") or "").upper() == principal]
        return 200, {"items": rows, "count": len(rows)}

    def _op_held(self, principal: str, payload: Mapping[str, Any]) -> Answer:
        self._require_store()
        record_id = str(payload.get("record_id") or "")
        if not record_id:
            row, refusal = self._own_row(principal, _identity(payload))
            if refusal:
                return refusal
            if row is None:
                return 404, {"error": "no session with this identity", "code": "not_registered"}
            record_id = str(row["record_id"])
        elif principal != OPERATOR:
            registry = OR.SessionRegistry(self.db_path, read_only=True)
            try:
                row = registry._get(record_id)
            finally:
                registry.close_db()
            if str(row["seat"]).strip().upper() != principal:
                return _error(403, "forbidden", "a seat may act only on its own sessions")
        registry = OR.SessionRegistry(self.db_path, read_only=True)
        try:
            letters = registry.letters_held(record_id)
        finally:
            registry.close_db()
        return 200, {
            "record_id": record_id,
            "letters": list(letters),
            "label": letters[0] if letters else "",
        }

    def _op_pending_outbox(self, principal: str, payload: Mapping[str, Any]) -> Answer:
        refused = self._operator_only(principal)
        if refused:
            return refused
        self._require_store()
        limit = payload.get("limit", 50)
        rows = OR.pending_outbox(self.db_path, int(limit or 50))
        return 200, {"items": rows, "count": len(rows)}

    # ---- writes (POST only) ---------------------------------------------------

    def _op_register(self, principal: str, payload: Mapping[str, Any]) -> Answer:
        refused = self._operator_only(principal)
        if refused:
            return refused
        # Never bring a registry into being:  a store is created as a local act on
        # the host that holds it, so a host with no store answers 503 here as on
        # every other op, rather than starting a second registry that would hand
        # out colliding letters (the registry CLI's own guard, applied to the API).
        self._require_store()
        identity = _identity(payload)
        out = OR.register_session(
            self.db_path,
            platform=identity["platform"],
            verified_host=identity["verified_host"],
            profile_scope=identity["profile_scope"],
            native_session_id=identity["native_session_id"],
            seat=str(payload.get("seat") or ""),
            source=str(payload.get("source") or ""),
            title=str(payload.get("title") or ""),
            project=str(payload.get("project") or ""),
            thread_link=str(payload.get("thread_link") or ""),
            apple_note_link=str(payload.get("apple_note_link") or ""),
            wake_capability=str(payload.get("wake_capability") or "unknown"),
            issuer=OPERATOR,
            letter_space=payload.get("letter_space") or None,
            letter_pick=payload.get("letter_pick") or None,
            parent_session_id=str(payload.get("parent_session_id") or ""),
            parent_letter=str(payload.get("parent_letter") or ""),
        )
        return 200, out

    def _op_relabel(self, principal: str, payload: Mapping[str, Any]) -> Answer:
        refused = self._operator_only(principal)
        if refused:
            return refused
        self._require_store()
        identity = _identity(payload)
        out = OR.relabel_session(
            self.db_path,
            platform=identity["platform"],
            verified_host=identity["verified_host"],
            profile_scope=identity["profile_scope"],
            native_session_id=identity["native_session_id"],
            seat=str(payload.get("seat") or ""),
            source=str(payload.get("source") or ""),
            letter_space=str(payload.get("letter_space") or "double"),
            letter_pick=str(payload.get("letter_pick") or "primary"),
            parent_session_id=str(payload.get("parent_session_id") or ""),
            parent_letter=str(payload.get("parent_letter") or ""),
            issuer=OPERATOR,
            force=bool(payload.get("force")),
        )
        return 200, out

    def _op_update(self, principal: str, payload: Mapping[str, Any]) -> Answer:
        identity = _identity(payload)
        _row, refusal = self._own_row(principal, identity)
        if refusal:
            return refusal
        fields = {
            name: payload.get(name)
            for name in ("title", "project", "thread_link", "apple_note_link", "wake_capability")
        }
        out = OR.update_session(
            self.db_path,
            platform=identity["platform"],
            verified_host=identity["verified_host"],
            profile_scope=identity["profile_scope"],
            native_session_id=identity["native_session_id"],
            **fields,
        )
        return 200, out

    def _op_close(self, principal: str, payload: Mapping[str, Any]) -> Answer:
        identity = _identity(payload)
        _row, refusal = self._own_row(principal, identity)
        if refusal:
            return refusal
        out = OR.close_session(
            self.db_path,
            platform=identity["platform"],
            verified_host=identity["verified_host"],
            profile_scope=identity["profile_scope"],
            native_session_id=identity["native_session_id"],
            reason=str(payload.get("reason") or ""),
            actor=principal,
            source=str(payload.get("source") or "registry-api"),
        )
        return 200, out

    def _op_touch(self, principal: str, payload: Mapping[str, Any]) -> Answer:
        identity = _identity(payload)
        _row, refusal = self._own_row(principal, identity)
        if refusal:
            return refusal
        out = OR.touch_session(
            self.db_path,
            platform=identity["platform"],
            verified_host=identity["verified_host"],
            profile_scope=identity["profile_scope"],
            native_session_id=identity["native_session_id"],
        )
        return 200, out

    def _op_sweep(self, principal: str, payload: Mapping[str, Any]) -> Answer:
        seat = str(payload.get("seat") or "")
        if principal != OPERATOR and seat.strip().upper() != principal:
            return _error(403, "forbidden", "a seat may sweep only its own sessions")
        self._require_store()
        ended = OR.sweep_stale(
            self.db_path,
            platform=str(payload.get("platform") or ""),
            seat=seat,
            stale_after_minutes=int(payload.get("stale_after_minutes") or 120),
        )
        return 200, {"ended": ended, "count": len(ended)}

    def _op_reopen(self, principal: str, payload: Mapping[str, Any]) -> Answer:
        self._require_store()
        record_id = str(payload.get("record_id") or "")
        if not record_id:
            row, refusal = self._own_row(principal, _identity(payload))
            if refusal:
                return refusal
            if row is None:
                return 404, {"error": "no session with this identity", "code": "not_registered"}
            record_id = str(row["record_id"])
        elif principal != OPERATOR:
            registry = OR.SessionRegistry(self.db_path, read_only=True)
            try:
                row = registry._get(record_id)
            finally:
                registry.close_db()
            if str(row["seat"]).strip().upper() != principal:
                return _error(403, "forbidden", "a seat may act only on its own sessions")
        from . import platform_letter as PL

        registry = OR.SessionRegistry(self.db_path)
        try:
            letter, generation, previous = PL.operator_reopen(
                OR,
                self.db_path,
                registry,
                record_id,
                actor=principal,
                source=str(payload.get("source") or "registry-api"),
                pick=str(payload.get("letter_pick") or payload.get("pick") or "primary"),
                parent=str(payload.get("parent_session_id") or ""),
            )
        except PL.SpaceLost as exc:
            return 409, {"error": str(exc), "code": "space_lost"}
        finally:
            registry.close_db()
        return 200, {
            "record_id": record_id,
            "label": letter,
            "generation": generation,
            "previous": previous,
            "outcome": "reopened",
        }

    def _op_mark_exported(self, principal: str, payload: Mapping[str, Any]) -> Answer:
        refused = self._operator_only(principal)
        if refused:
            return refused
        self._require_store()
        ids = payload.get("outbox_ids")
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids):
            return _error(400, "invalid_argument", "outbox_ids must be a list of ids")
        exported_at = payload.get("exported_at")
        marked = OR.mark_exported(
            self.db_path, list(ids), str(exported_at) if exported_at else None
        )
        return 200, {"marked": marked}
