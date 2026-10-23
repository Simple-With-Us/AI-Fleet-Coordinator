"""The Operator's Apple Notes queue (AFC-486, docs/protocols/operator-notes.md).

Hosts that cannot run apple-notes-coding.sh (the Hetzner server's CLIs and stdio MCP, the hosted ST-MCP Worker)
send a request object here over signed HTTPS (notes_wire.py), and the seatless Mac runner (notes_runner.py) claims
it, runs the script and reports the result.  No Zulip message is involved.  The routes, served by the listener API
server (listener_api.py) under /notes/v1/:

    POST /notes/v1/requests                      WORKER, SERVER   enqueue one request for a seat
    GET  /notes/v1/requests/<SEAT>/<request_id>  WORKER, SERVER   the request's record
    GET  /notes/v1/log?<filters>                 WORKER, SERVER   the script's action log, as the runner sent it
    GET  /notes/v1/health                        WORKER, SERVER   queue counts and the runner's last contact
    POST /notes/v1/runner/claim                  RUNNER           the oldest queued request (long-poll up to 25 s)
    POST /notes/v1/runner/result                 RUNNER           the outcome of a claimed request
    POST /notes/v1/runner/log                    RUNNER           new action-log lines

A request runs only when ALL of these hold, checked here and again by the runner:  the principal signed it, the
principal may act for the seat (`[notes_queue] worker_seats` or `server_seats`), the seat is a live agent identity,
the request object is well formed (notes.check_request), it passes notes.request_problem for that seat (a create's
title carries the seat's own agent name, a create or update carries a session id unless the seat is a single-thread
identity), and its text passes the secret scan.  A request_id names one request per seat:  the same request sent
again answers its record, a different one under the same id is a conflict.

Every record is a mode-600 file, <state>/notes-queue/requests/<SEAT>/<request_id>.json, written durably before an
answer leaves, so a restart loses nothing.  A claimed write that does not report within LEASE_S becomes `unknown`
(never run twice:  it may have written);  a claimed read or list goes back to the queue, because reading twice
changes nothing.  A write's body is dropped from its record when it finishes, a read's or list's result after
RESULT_TTL_S, and finished records after KEEP_S.  Nothing here logs a key, a signature, a title or a body.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, NamedTuple
from urllib.parse import parse_qsl, urlsplit

from . import identities as X
from . import identity as I
from . import live as L
from . import notes as N
from . import notes_wire as W
from . import secretscan

QUEUE_DIR = "notes-queue"
REQUESTS_DIR = "requests"
ACTION_LOG = "notes-actions.jsonl"
LEASE_S = 1500.0  # above the script's 1,200-second cap (notes.SCRIPT_TIMEOUT_S)
QUEUED_TTL_S = 24 * 3600.0
RESULT_TTL_S = 3600.0
KEEP_S = 14 * 24 * 3600.0
QUEUE_MAX = 1000
SEAT_QUEUE_MAX = 200
READ_ATTEMPTS = 3
LOG_ROTATE_BYTES = 5 * 1024 * 1024
LOG_ROTATIONS = 10
LOG_BATCH_MAX = 200
LOG_IDS_KEEP = 20_000
PRUNE_EVERY_S = 3600.0
FINAL = ("done", "refused", "unknown", "expired")
STATES = ("queued", "running") + FINAL
_REQUEST_PATH_RE = re.compile(
    r"^/notes/v1/requests/([A-Z0-9][A-Z0-9_-]{0,31})/([A-Za-z0-9][A-Za-z0-9_-]{7,63})$"
)
_STAMP_RE = re.compile(r"^\d{1,12}$")
_CLAIM_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_LOG_PARAMS = {"since_hours", "limit", "by_seat", "action", "title"}
_ERROR_CODES = frozenset(
    {
        "invalid_argument",
        "not_found",
        "unavailable",
        "outcome_unknown",
        "internal",
        "session_required",
    }
)


class Principal(NamedTuple):
    name: str
    key: str
    seats: frozenset[str]


class Answer(NamedTuple):
    status: int
    body: dict[str, Any] | None


def _error(status: int, code: str, message: str) -> Answer:
    return Answer(status, {"error": code, "message": message})


def _iso(moment: float | None) -> str | None:
    return None if moment is None else N.iso_now(lambda: moment)


class NotesQueue:
    """One queue over one state root.  `principals` maps WORKER, SERVER and RUNNER to (key, seats);  a principal
    whose key is missing or shorter than 32 characters is not accepted at all (404, like an unknown one)."""

    def __init__(
        self,
        root: str,
        principals: Mapping[str, tuple[str, Iterable[str]]],
        *,
        env: Mapping[str, str] | None = None,
        clock: Callable[[], float] = time.time,
        log: Callable[..., None] | None = None,
    ) -> None:
        self.dir = Path(root) / QUEUE_DIR
        self.requests_dir = self.dir / REQUESTS_DIR
        self.action_log = self.dir / ACTION_LOG
        self.principals: dict[str, Principal] = {}
        for name, (key, seats) in principals.items():
            if name in W.PRINCIPALS and isinstance(key, str) and len(key) >= W.MIN_KEY_LEN:
                self.principals[name] = Principal(
                    name,
                    key,
                    frozenset(str(seat).upper() for seat in seats if isinstance(seat, str)),
                )
        self.env = dict(env or {})
        self.clock = clock
        self.log = log or (lambda *_args, **_kwargs: None)
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.active: dict[
            tuple[str, str], dict[str, Any]
        ] = {}  # queued and running records, by (seat, request_id)
        self.log_ids: OrderedDict[str, None] = OrderedDict()
        self._last_prune = 0.0
        # When the runner last asked for work, last reported and last sent log lines (health, for the doctor and
        # Muster).  In memory:  a restart reads them as unknown until the runner's next poll, at most 25 s later.
        self.runner_polled_at: float | None = None
        self.runner_reported_at: float | None = None
        self.runner_logged_at: float | None = None
        self._load()

    # ---- storage ---------------------------------------------------------------------------------
    def _path(self, seat: str, request_id: str) -> Path:
        return self.requests_dir / seat / ("%s.json" % request_id)

    def _save(self, record: Mapping[str, Any]) -> None:
        L.write_json(
            str(self._path(record["seat"], record["request_id"])), dict(record), durable=True
        )

    def _read(self, seat: str, request_id: str) -> dict[str, Any] | None:
        record = L.read_json(str(self._path(seat, request_id)))
        return record if self._valid(record) else None

    @staticmethod
    def _valid(record: Any) -> bool:
        return (
            isinstance(record, dict)
            and isinstance(record.get("seat"), str)
            and isinstance(record.get("request_id"), str)
            and record.get("state") in STATES
            and isinstance(record.get("queued_at"), (int, float))
        )

    def _load(self) -> None:
        L.private_dir(str(self.requests_dir))
        for seat_dir in sorted(self.requests_dir.iterdir()):
            if not seat_dir.is_dir():
                continue
            for path in seat_dir.glob("*.json"):
                record = L.read_json(str(path))
                if self._valid(record) and record["state"] in ("queued", "running"):
                    self.active[(record["seat"], record["request_id"])] = record
        for entry in L.read_jsonl(str(self.action_log))[-LOG_IDS_KEEP:]:
            if isinstance(entry.get("id"), str):
                self.log_ids[entry["id"]] = None

    # ---- the record's public view --------------------------------------------------------------
    @staticmethod
    def view(record: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {
            "contract": W.CONTRACT,
            "seat": record["seat"],
            "request_id": record["request_id"],
            "action": record.get("action"),
            "title": record.get("title"),
            "principal": record.get("principal"),
            "state": record["state"],
            "queued_at": _iso(record.get("queued_at")),
            "claimed_at": _iso(record.get("claimed_at")),
            "finished_at": _iso(record.get("finished_at")),
        }
        if "result" in record:
            out["result"] = record["result"]
        if "error" in record:
            out["error"] = record["error"]
        if record.get("result_expired"):
            out["result_expired"] = True
        return out

    # ---- time-based changes ----------------------------------------------------------------------
    def _sweep(self, now: float) -> None:
        """Expire leases and stale queued requests, drop old results, and (hourly) delete old records.  Under the lock."""
        for key, record in list(self.active.items()):
            if record["state"] == "running" and now > float(record.get("lease_until") or 0):
                if record.get("action") in N.WRITE_ACTIONS:
                    record["lease_expired"] = (
                        True  # a late, definite report from the same claim may still settle it
                    )
                    self._finish(
                        record,
                        now,
                        state="unknown",
                        error={
                            "code": "outcome_unknown",
                            "message": "the Mac runner did not report within %d seconds, so the note may or may not "
                            "have been written.  Check the Coding folder, then send it again with a new request_id."
                            % int(LEASE_S),
                        },
                    )
                elif int(record.get("attempts") or 0) >= READ_ATTEMPTS:
                    self._finish(
                        record,
                        now,
                        state="refused",
                        error={
                            "code": "unavailable",
                            "message": "the Mac runner took this read %d times and never answered"
                            % READ_ATTEMPTS,
                        },
                    )
                else:
                    record.update(state="queued", claim=None, lease_until=None)
                    self._save(record)
            elif record["state"] == "queued" and now - float(record["queued_at"]) > QUEUED_TTL_S:
                self._finish(
                    record,
                    now,
                    state="expired",
                    error={
                        "code": "unavailable",
                        "message": "the Mac runner did not pick this up within 24 hours;  nothing was written",
                    },
                )
        if now - self._last_prune >= PRUNE_EVERY_S:
            self._last_prune = now
            self._prune(now)

    def _prune(self, now: float) -> None:
        for seat_dir in self.requests_dir.iterdir():
            if not seat_dir.is_dir():
                continue
            for path in seat_dir.glob("*.json"):
                record = L.read_json(str(path))
                if not self._valid(record) or record["state"] not in FINAL:
                    continue
                finished = float(record.get("finished_at") or record["queued_at"])
                if now - finished > KEEP_S:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                elif record.get("result_expires_at") and now > float(record["result_expires_at"]):
                    record.pop("result", None)
                    record.pop("result_expires_at", None)
                    record["result_expired"] = True
                    L.write_json(str(path), record, durable=True)

    def _finish(
        self,
        record: dict[str, Any],
        now: float,
        *,
        state: str,
        result: Mapping[str, Any] | None = None,
        error: Mapping[str, Any] | None = None,
    ) -> None:
        record["state"] = state
        record["finished_at"] = now
        record.pop(
            "request", None
        )  # the body (and a read's format) is not kept past the request's run
        record.pop("lease_until", None)
        if result is not None:
            record["result"] = dict(result)
            if record.get("action") in ("read", "list"):
                record["result_expires_at"] = now + RESULT_TTL_S
        if error is not None:
            record["error"] = {
                "code": str(error.get("code"))[:40],
                "message": str(error.get("message") or "")[:400],
            }
        self.active.pop((record["seat"], record["request_id"]), None)
        self._save(record)
        self.log(
            "notes-queue-finished", seat=record["seat"], action=record.get("action"), state=state
        )

    # ---- authentication --------------------------------------------------------------------------
    def authenticate(
        self, method: str, path: str, query: str, headers: Mapping[str, str], body: bytes
    ) -> Principal | Answer:
        name = (headers.get(W.PRINCIPAL_HEADER) or "").strip().upper()
        principal = self.principals.get(name)
        if principal is None:
            return _error(404, "not_found", "not found")
        timestamp = (headers.get(W.TIMESTAMP_HEADER) or "").strip()
        if not _STAMP_RE.fullmatch(timestamp) or abs(self.clock() - int(timestamp)) > W.MAX_SKEW_S:
            self.log("notes-api-refused", principal=name, reason="timestamp")
            return _error(401, "unauthorized", "unauthorized")
        expected = W.sign(principal.key, method, path, query, timestamp, name, body)
        signature = (headers.get(W.SIGNATURE_HEADER) or "").strip().lower()
        if not signature or not hmac.compare_digest(expected, signature):
            self.log("notes-api-refused", principal=name, reason="signature")
            return _error(401, "unauthorized", "unauthorized")
        return principal

    # ---- the router --------------------------------------------------------------------------------
    def handle(self, method: str, raw_path: str, headers: Mapping[str, str], body: bytes) -> Answer:
        """(status, JSON body or None) for one request under /notes/v1/.  Pure apart from the state root, so tests
        call it without a socket."""
        parts = urlsplit(raw_path)
        path, query = parts.path, parts.query
        if not path.startswith(W.PREFIX) or parts.fragment or len(query) > 1024:
            return _error(404, "not_found", "not found")
        if len(body) > W.MAX_BODY:
            return _error(413, "too_large", "the request body is larger than %d bytes" % W.MAX_BODY)
        method = method.upper()
        principal = self.authenticate(method, path, query, headers, body)
        if isinstance(principal, Answer):
            return principal
        payload: Any = None
        if method == "POST":
            try:
                payload = json.loads(body.decode("utf-8")) if body else {}
            except (UnicodeDecodeError, ValueError, RecursionError):
                return _error(400, "invalid_argument", "the body is not JSON")
            if not isinstance(payload, dict):
                return _error(400, "invalid_argument", "the body must be a JSON object")
        writer = principal.name in (W.WORKER, W.SERVER)
        if method == "POST" and path == "/notes/v1/requests" and writer and not query:
            return self.enqueue(principal, payload)
        if method == "GET" and writer:
            match = _REQUEST_PATH_RE.fullmatch(path)
            if match and not query:
                return self.status(principal, match.group(1), match.group(2))
            if path == "/notes/v1/log":
                return self.read_log(query)
            if path == "/notes/v1/health" and not query:
                return self.health()
        if method == "POST" and principal.name == W.RUNNER and not query:
            if path == "/notes/v1/runner/claim":
                return self.claim(payload)
            if path == "/notes/v1/runner/result":
                return self.result(payload)
            if path == "/notes/v1/runner/log":
                return self.append_log(payload)
        if method not in ("GET", "POST"):
            return _error(405, "method_not_allowed", "method not allowed")
        if path.startswith(("/notes/v1/requests", "/notes/v1/log", "/notes/v1/runner/")):
            return _error(403, "forbidden", "this principal may not use this path and method")
        return _error(404, "not_found", "not found")

    # ---- writers -----------------------------------------------------------------------------------
    def _seat_problem(self, principal: Principal, seat: Any) -> Answer | None:
        if not isinstance(seat, str) or not N.SEAT_RE.fullmatch(seat):
            return _error(400, "invalid_argument", "the body names no valid seat")
        if seat not in principal.seats:
            return _error(403, "forbidden", "%s may not act for %s" % (principal.name, seat))
        try:
            problem = X.agent_identity_problem(seat, registry=I.load_seat_registry(env=self.env))
        except I.SeatRegistryError:
            problem = (
                "the seat registry cannot be read, so %s cannot be checked as an agent identity"
                % seat
            )
        if problem:
            return _error(403, "forbidden", problem[:300])
        return None

    def enqueue(self, principal: Principal, payload: Mapping[str, Any]) -> Answer:
        seat = payload.get("seat")
        refused = self._seat_problem(principal, seat)
        if refused:
            return refused
        if set(payload) - {"seat", "request"}:
            return _error(
                400, "invalid_argument", "the body has a field this version does not know"
            )
        try:
            request = N.check_request(payload.get("request"))
        except N.NotesError as exc:
            return _error(400, exc.code, exc.message)
        problem = N.request_problem(request, seat, env=self.env)
        if problem:
            code = "session_required" if "session id is required" in problem else "invalid_argument"
            return _error(400, code, problem[:600])
        if request["action"] in N.WRITE_ACTIONS:
            for field, text in N.text_fields(request).items():
                kind = secretscan.scan(text)
                if kind:
                    return _error(
                        400,
                        "invalid_argument",
                        "refusing to write:  the %s looks like it contains %s" % (field, kind),
                    )
        digest = N.request_digest(request)
        now = self.clock()
        with self.lock:
            self._sweep(now)
            existing = self.active.get((seat, request["request_id"])) or self._read(
                seat, request["request_id"]
            )
            if existing is not None:
                if existing.get("digest") != digest:
                    return _error(
                        409, "conflict", "this request_id was already used for a different request"
                    )
                return Answer(200, self.view(existing))
            waiting = [r for r in self.active.values() if r["state"] == "queued"]
            if (
                len(waiting) >= QUEUE_MAX
                or sum(1 for r in waiting if r["seat"] == seat) >= SEAT_QUEUE_MAX
            ):
                return _error(503, "unavailable", "the notes queue is full;  try again later")
            record = {
                "seat": seat,
                "request_id": request["request_id"],
                "action": request["action"],
                "title": request.get("title"),
                "principal": principal.name,
                "state": "queued",
                "digest": digest,
                "request": request,
                "queued_at": now,
                "attempts": 0,
            }
            self._save(record)
            self.active[(seat, request["request_id"])] = record
            self.changed.notify_all()
        self.log(
            "notes-queue-queued", seat=seat, action=request["action"], principal=principal.name
        )
        return Answer(202, self.view(record))

    def status(self, principal: Principal, seat: str, request_id: str) -> Answer:
        if seat not in principal.seats:
            return _error(403, "forbidden", "%s may not act for %s" % (principal.name, seat))
        with self.lock:
            self._sweep(self.clock())
            record = self.active.get((seat, request_id)) or self._read(seat, request_id)
            if record is None:
                return _error(404, "not_found", "no request with that request_id for %s" % seat)
            return Answer(200, self.view(record))

    def read_log(self, query: str) -> Answer:
        try:
            raw = dict(parse_qsl(query, keep_blank_values=False, strict_parsing=bool(query)))
        except ValueError:
            return _error(400, "invalid_argument", "bad query")
        if set(raw) - _LOG_PARAMS:
            return _error(400, "invalid_argument", "unknown log filter")
        args: dict[str, Any] = dict(raw)
        for name in ("since_hours", "limit"):
            if name in args:
                if not re.fullmatch(r"\d{1,6}", args[name]):
                    return _error(
                        400, "invalid_argument", "argument '%s' must be an integer" % name
                    )
                args[name] = int(args[name])
        try:
            filters = N.check_log_args(args)
        except N.NotesError as exc:
            return _error(400, exc.code, exc.message)
        entries = N.log_filter(
            N.iter_log_files(self.action_log, rotations=LOG_ROTATIONS), **filters
        )
        return Answer(200, {"contract": W.CONTRACT, "entries": entries, "count": len(entries)})

    def health(self) -> Answer:
        """Counts and the runner's last contact, for the doctor (and so Muster):  no title, seat list or body."""
        now = self.clock()
        with self.lock:
            self._sweep(now)
            queued = [r for r in self.active.values() if r["state"] == "queued"]
            running = [r for r in self.active.values() if r["state"] == "running"]
            oldest = min((float(r["queued_at"]) for r in queued), default=None)
            return Answer(
                200,
                {
                    "contract": W.CONTRACT,
                    "queued": len(queued),
                    "running": len(running),
                    "oldest_queued_seconds": None if oldest is None else int(now - oldest),
                    "runner_polled_at": _iso(self.runner_polled_at),
                    "runner_reported_at": _iso(self.runner_reported_at),
                    "runner_logged_at": _iso(self.runner_logged_at),
                    "principals": sorted(self.principals),
                    "now": _iso(now),
                },
            )

    # ---- the runner --------------------------------------------------------------------------------
    def claim(self, payload: Mapping[str, Any]) -> Answer:
        wait = payload.get("wait", 0)
        if (
            isinstance(wait, bool)
            or not isinstance(wait, (int, float))
            or not 0 <= wait <= W.CLAIM_WAIT_MAX
        ):
            return _error(
                400, "invalid_argument", "wait must be 0 to %d seconds" % W.CLAIM_WAIT_MAX
            )
        deadline = time.monotonic() + float(wait)
        with self.lock:
            while True:
                now = self.clock()
                self.runner_polled_at = now
                self._sweep(now)
                waiting = sorted(
                    (r for r in self.active.values() if r["state"] == "queued"),
                    key=lambda r: float(r["queued_at"]),
                )
                if waiting:
                    record = waiting[0]
                    record.update(
                        state="running",
                        claim=secrets.token_urlsafe(18),
                        claimed_at=now,
                        lease_until=now + LEASE_S,
                        attempts=int(record.get("attempts") or 0) + 1,
                    )
                    self._save(record)
                    self.log(
                        "notes-queue-claimed", seat=record["seat"], action=record.get("action")
                    )
                    return Answer(
                        200,
                        {
                            "contract": W.CONTRACT,
                            "claim": record["claim"],
                            "seat": record["seat"],
                            "request_id": record["request_id"],
                            "principal": record.get("principal"),
                            "request": record["request"],
                            "lease_seconds": int(LEASE_S),
                        },
                    )
                left = deadline - time.monotonic()
                if left <= 0:
                    return Answer(204, None)
                self.changed.wait(timeout=min(left, 5.0))

    def result(self, payload: Mapping[str, Any]) -> Answer:
        seat, request_id, claim = (
            payload.get("seat"),
            payload.get("request_id"),
            payload.get("claim"),
        )
        if (
            not isinstance(seat, str)
            or not isinstance(request_id, str)
            or not isinstance(claim, str)
            or not _CLAIM_RE.fullmatch(claim)
        ):
            return _error(400, "invalid_argument", "seat, request_id and claim are required")
        if ("result" in payload) == ("error" in payload) or set(payload) - {
            "seat",
            "request_id",
            "claim",
            "result",
            "error",
        }:
            return _error(400, "invalid_argument", "send exactly one of result and error")
        now = self.clock()
        with self.lock:
            self.runner_reported_at = now
            record = self.active.get((seat, request_id)) or self._read(seat, request_id)
            if record is None:
                return _error(404, "not_found", "no such request")
            if record["state"] in FINAL:
                if record.get("claim") != claim:
                    return _error(409, "conflict", "this request is already finished")
                definite = "result" in payload or (
                    isinstance(payload.get("error"), Mapping)
                    and payload["error"].get("code") != "outcome_unknown"
                )
                if not (record.get("lease_expired") and definite):
                    return Answer(
                        200, self.view(record)
                    )  # the same report again:  already recorded
                # The lease ran out (the Mac slept or lost the network) and the write was called unknown, but the
                # runner now knows what happened:  record that, so notes_status tells the truth instead of leaving
                # the caller to send a note that was written again under a new request_id.
                record.pop("error", None)
                record.pop("lease_expired", None)
                record["late_report"] = True
            elif record["state"] != "running" or record.get("claim") != claim:
                return _error(409, "conflict", "this claim does not hold the request")
            if "error" in payload:
                error = payload["error"] if isinstance(payload["error"], Mapping) else {}
                code = str(error.get("code") or "internal")
                code = code if code in _ERROR_CODES else "internal"
                state = "unknown" if code == "outcome_unknown" else "refused"
                self._finish(
                    record, now, state=state, error={"code": code, "message": error.get("message")}
                )
            else:
                cleaned = self._clean_result(record.get("action"), payload["result"])
                if cleaned is None:
                    return _error(
                        400, "invalid_argument", "the result does not fit the request's action"
                    )
                self._finish(record, now, state="done", result=cleaned)
            return Answer(200, self.view(record))

    @staticmethod
    def _clean_result(action: Any, raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, Mapping):
            return None
        if action in N.WRITE_ACTIONS:
            out: dict[str, Any] = {"status": "done"}
            for key in ("note_id", "pin_skipped"):
                if isinstance(raw.get(key), str) and raw[key]:
                    out[key] = raw[key][:200]
            return out
        if action == "read":
            note = raw.get("note")
            if not isinstance(note, Mapping) or not isinstance(note.get("body"), str):
                return None
            keep = {
                key: note.get(key)
                for key in (
                    "title",
                    "id",
                    "created",
                    "modified",
                    "format",
                    "body",
                    "bytes",
                    "truncated",
                )
            }
            if len(str(keep.get("body") or "").encode("utf-8")) > N.READ_MAX:
                return None
            return {"note": keep}
        if action == "list":
            if not isinstance(raw.get("notes"), list):
                return None
            return N.list_result(raw)
        return None

    def append_log(self, payload: Mapping[str, Any]) -> Answer:
        lines = payload.get("lines")
        if not isinstance(lines, list) or len(lines) > LOG_BATCH_MAX:
            return _error(
                400,
                "invalid_argument",
                "lines must be a list of at most %d entries" % LOG_BATCH_MAX,
            )
        now = self.clock()
        accepted: list[dict[str, Any]] = []
        duplicates = 0
        with self.lock:
            self.runner_logged_at = now
            for raw in lines:
                entry = N.clean_log_entry(raw)
                if entry is None:
                    continue
                if entry["id"] in self.log_ids:
                    duplicates += 1
                    continue
                entry["received_at"] = N.iso_now(lambda: now)
                accepted.append(entry)
                self.log_ids[entry["id"]] = None
            while len(self.log_ids) > LOG_IDS_KEEP:
                self.log_ids.popitem(last=False)
            if accepted:
                self._rotate_log()
                L.append_jsonl(str(self.action_log), accepted)
        return Answer(
            200, {"contract": W.CONTRACT, "accepted": len(accepted), "duplicates": duplicates}
        )

    def _rotate_log(self) -> None:
        try:
            if os.path.getsize(self.action_log) < LOG_ROTATE_BYTES:
                return
        except OSError:
            return
        for index in range(LOG_ROTATIONS - 1, 0, -1):
            older = self.action_log.with_name("%s.%d" % (ACTION_LOG, index))
            if older.exists():
                os.replace(older, self.action_log.with_name("%s.%d" % (ACTION_LOG, index + 1)))
        os.replace(self.action_log, self.action_log.with_name("%s.1" % ACTION_LOG))
