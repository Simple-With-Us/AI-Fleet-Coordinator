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
    POST  /registry/v1/report-gone

Reads are GET, mutating ops are POST only:  a known op with the wrong method is 405, an
unknown route is 404.  Authentication reuses the listener's existing pattern:  a per-principal
HMAC-SHA256 signature (the principal is a seat, or OPERATOR), keyed with the principal's
existing per-seat key (the listener's routine `key_env` pattern;  no new secret architecture).
Three headers:

    X-Simple-Tools-Principal:  a seat code (CLAUDE, CODEX, MM, ...) or OPERATOR
    X-Simple-Tools-Timestamp:  unix seconds
    X-Simple-Tools-Signature:  lowercase hex HMAC-SHA256 over
        "simple-tools-registry/1\\n<METHOD>\\n<path>\\n<raw query>\\n<timestamp>\\n<principal>\\n<sha256 hex of the body>"

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
SessionRegistry.reopen (through operator_reopen below, so a pair letter stays a pair),
held -> SessionRegistry.letters_held, pending-outbox -> pending_outbox, mark-exported ->
mark_exported, report-gone -> SessionRegistry.close for the named rows.  `reopen` and `held`
have no CLI verb on this tree;  their answers follow the shapes the existing callers already
use (reopen_closed in mm_session_letter.py, and a plain letters list for held).

One writer (AFC-721).  The Eavesdropper is the only process that writes the registry, and
every write it makes goes through this module:  the API ops above, the Operator's own
location-answer flow (RegistryService.operator_register), and the one stale sweeper
(Sweeper, below), which the Eavesdropper runs on a timer for every platform.  A host never
sweeps or closes rows itself;  it names the chats it knows are gone with an OPERATOR-signed
report-gone call, and that call refuses to close more than a set share of the host's live
rows at once, so one broken host report cannot end every letter of a platform.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from . import operator_registry as OR

CONTRACT = "simple-tools-registry/1"
PREFIX = "/registry/v1/"
PRINCIPAL_HEADER = "X-Simple-Tools-Principal"
TIMESTAMP_HEADER = "X-Simple-Tools-Timestamp"
SIGNATURE_HEADER = "X-Simple-Tools-Signature"
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
    "report-gone",
)
OPS = READ_OPS + WRITE_OPS
_METHODS = {op: "GET" for op in READ_OPS} | {op: "POST" for op in WRITE_OPS}

Answer = tuple[int, dict[str, Any]]

#: The source a host's gone report writes into each close event.
SOURCE_HOST_REPORT = "host-report"
#: The most session ids one report-gone call may name.
MAX_GONE_IDS = 500
#: The default share of a host's live rows (for one platform) a single report may close.
DEFAULT_GONE_REPORT_MAX_SHARE = 0.5


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
        gone_report_max_share: float = DEFAULT_GONE_REPORT_MAX_SHARE,
    ) -> None:
        self.db_path = str(db_path)
        self.gone_report_max_share = float(gone_report_max_share)
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

    # ---- the Operator's own writes (in process, never over HTTP) --------------

    def operator_register(self, **fields: Any) -> dict[str, Any]:
        """Issue a letter as the Operator:  the one register path of this process.

        The `register` op and the Eavesdropper's own location-answer flow both come here, so
        every letter the Eavesdropper issues passes the same store check.  It never brings a
        registry into being:  a store is created as a local act on the host that holds it,
        so a host with no store raises RegistryUnavailable (503 over the API) rather than
        starting a second registry that would hand out colliding letters.  The issuer is
        always the Operator, whatever the caller passes."""
        self._require_store()
        fields = {name: value for name, value in fields.items() if name != "issuer"}
        return OR.register_session(self.db_path, issuer=OPERATOR, **fields)

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
        identity = _identity(payload)
        out = self.operator_register(
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
        # AFC-694:  `issues` replaces the session's Linear issues;  a request without it leaves them.
        fields["issues"] = payload.get("issues")
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
        registry = OR.SessionRegistry(self.db_path)
        try:
            letter, generation, previous = operator_reopen(
                registry,
                record_id,
                actor=principal,
                source=str(payload.get("source") or "registry-api"),
                pick=str(payload.get("letter_pick") or payload.get("pick") or "primary"),
                parent=str(payload.get("parent_session_id") or ""),
            )
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

    def _op_report_gone(self, principal: str, payload: Mapping[str, Any]) -> Answer:
        """Close the rows of chats a host reports gone (OPERATOR only).

        The body names one platform, the host (`verified_host`, or `location`), optionally one
        `profile_scope`, and `native_session_ids`:  the chats the host's letter hook knows have
        ended.  Only open rows of that platform and host whose session id is named are closed,
        each with source host-report.  The guard:  a report that would close more than
        `gone_report_max_share` of that host's live rows for the platform is refused whole,
        with nothing written, so one broken host report cannot end every letter at once.  A
        report that closes a single row is always allowed (a host with one live chat must be
        able to report it)."""
        refused = self._operator_only(principal)
        if refused:
            return refused
        self._require_store()
        platform = OR.normalize_platform(str(payload.get("platform") or ""))
        host = str(payload.get("verified_host") or payload.get("location") or "").strip()
        scope = str(payload.get("profile_scope") or payload.get("scope") or "").strip()
        ids = payload.get("native_session_ids")
        if not platform or not host:
            return _error(400, "invalid_argument", "platform and verified_host are required")
        if (
            not isinstance(ids, list)
            or not all(isinstance(item, str) and item.strip() for item in ids)
            or len(ids) > MAX_GONE_IDS
        ):
            return _error(
                400,
                "invalid_argument",
                "native_session_ids must be a list of at most %d session ids" % MAX_GONE_IDS,
            )
        named = {item.strip() for item in ids}
        reader = OR.SessionRegistry(self.db_path, read_only=True)
        try:
            live = [
                dict(row)
                for row in reader._conn.execute(
                    "SELECT record_id, profile_scope, native_session_id FROM sessions"
                    " WHERE platform = ? AND verified_host = ? AND closed_at IS NULL",
                    (platform, host),
                ).fetchall()
            ]
        finally:
            reader.close_db()
        doomed = [
            row
            for row in live
            if row["native_session_id"] in named and (not scope or row["profile_scope"] == scope)
        ]
        if len(doomed) > 1 and len(doomed) > self.gone_report_max_share * len(live):
            self.log(
                "registry-report-gone-refused",
                platform=platform,
                host=host,
                named=len(doomed),
                live=len(live),
            )
            return 409, {
                "error": "the report would close %d of %d live rows, more than the allowed share"
                % (len(doomed), len(live)),
                "code": "gone_share_exceeded",
                "named": len(doomed),
                "live": len(live),
            }
        closed: list[dict[str, Any]] = []
        registry = OR.SessionRegistry(self.db_path)
        try:
            for row in doomed:
                letter, generation = registry._active_alias(registry._conn, row["record_id"])
                if registry.close(
                    row["record_id"],
                    actor=principal,
                    source=SOURCE_HOST_REPORT,
                    reason="the host reported the chat gone",
                ):
                    closed.append(
                        {
                            "record_id": row["record_id"],
                            "label": letter,
                            "generation": generation,
                            "native_session_id": row["native_session_id"],
                        }
                    )
        finally:
            registry.close_db()
        return 200, {"closed": closed, "count": len(closed), "live": len(live)}


def operator_reopen(
    registry: Any,
    record_id: str,
    *,
    actor: str,
    source: str,
    pick: str,
    parent: str = "",
) -> tuple[str, int, str]:
    """Give a closed chat a letter again (SessionRegistry.reopen), in the space it held.

    Moved here from the client letter module (AFC-721), so the Eavesdropper's reopen never imports a
    client module.  The registry it calls is this tree's own, whose `reopen` takes the pick
    and the parent, so a chat that held a pair comes back with a pair and none of the client's
    probing for an older registry is needed."""
    return registry.reopen(
        record_id,
        actor=actor,
        source=source,
        pick=pick,
        parent_session_id=parent,
    )


class Sweeper:
    """The one stale sweeper, run by the Eavesdropper beside its registry service (AFC-721).

    Every `interval_minutes` it calls operator_registry.sweep_stale for each (platform, seat)
    that has open rows, with one stale window for every platform (`stale_after_hours`), and
    writes the last result to `status_path` (status.json beside the listener's own status).
    sweep_stale ends a letter only when its chat is stale AND replaced by a newer chat of the
    same seat, so a quiet chat nobody replaced keeps its letter.  No host runs a sweep:  the
    Mac's LaunchAgent and every per-platform sweep verb are retired by the same change set.

    `maybe_run(now)` is called from the daemon's tick, so the sweeper has no thread of its own
    and stops when the service it belongs to is dropped.  It never creates the store:  with no
    store it records that and sweeps nothing."""

    def __init__(
        self,
        service: RegistryService,
        *,
        interval_minutes: int,
        stale_after_hours: float,
        status_path: str,
        log: Callable[..., None] | None = None,
    ) -> None:
        self.service = service
        self.interval_s = max(1, int(interval_minutes)) * 60.0
        self.stale_after_minutes = max(1, round(float(stale_after_hours) * 60))
        self.status_path = str(status_path)
        self.log = log or (lambda *_args, **_kwargs: None)
        self.next_at = 0.0
        self.last: dict[str, Any] | None = None
        self._lock = threading.Lock()

    def maybe_run(self, now: float) -> dict[str, Any] | None:
        if now < self.next_at:
            return None
        self.next_at = now + self.interval_s
        return self.run(now)

    def _open_pairs(self) -> list[tuple[str, str]]:
        reader = OR.SessionRegistry(self.service.db_path, read_only=True)
        try:
            rows = reader._conn.execute(
                "SELECT DISTINCT platform, seat FROM sessions WHERE closed_at IS NULL"
                " ORDER BY platform, seat"
            ).fetchall()
        finally:
            reader.close_db()
        return [(str(row[0]), str(row[1])) for row in rows]

    def run(self, now: float) -> dict[str, Any]:
        with self._lock:
            result: dict[str, Any] = {
                "ran_at": now,
                "stale_after_minutes": self.stale_after_minutes,
                "platforms": {},
                "ended": 0,
                "ok": True,
            }
            if not os.path.isfile(self.service.db_path):
                result.update(ok=False, error="no registry store on this host")
            else:
                try:
                    for platform, seat in self._open_pairs():
                        ended = OR.sweep_stale(
                            self.service.db_path,
                            platform=platform,
                            seat=seat,
                            stale_after_minutes=self.stale_after_minutes,
                        )
                        if ended:
                            entry = result["platforms"].setdefault(platform, {})
                            entry[seat] = [
                                {"label": item.get("label"), "record_id": item.get("record_id")}
                                for item in ended
                            ]
                            result["ended"] += len(ended)
                except (OR.RegistryUnavailable, OSError, ValueError) as exc:
                    result.update(ok=False, error=type(exc).__name__ + ": " + str(exc)[:200])
            self.last = result
            self._write_status(result)
            self.log(
                "registry-sweep",
                ok=result["ok"],
                ended=result["ended"],
                error=result.get("error", ""),
            )
            return result

    def _write_status(self, result: Mapping[str, Any]) -> None:
        """Write the result to status_path atomically;  a failed write is logged, never raised."""
        try:
            folder = os.path.dirname(self.status_path) or "."
            os.makedirs(folder, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".registry-sweep.", dir=folder)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(result, handle, sort_keys=True)
                os.replace(tmp, self.status_path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            self.log("registry-sweep-status-error", error=type(exc).__name__)
