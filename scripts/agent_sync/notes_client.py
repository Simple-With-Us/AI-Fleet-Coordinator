"""The Python side of the Operator notes API (AFC-486, docs/protocols/operator-notes.md).

`OperatorClient` signs and sends one call (notes_wire.py);  `operator_client` builds one for this host from the
fleet knob OPERATOR_API_URL and the principal's key in Infisical (`fleet-secrets get`, AI Fleet Coordinator, prod,
/operator), held in memory only.  `call`, `status` and `log` are what simple-notes and the stdio MCP run, the same
code on every host:  on a Mac they run apple-notes-coding.sh directly, anywhere else they send the request to the
Operator and wait for the Mac runner's result, so the answer has the same shape either way.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import notes as N
from . import notes_wire as W

KNOB = "OPERATOR_API_URL"
KNOB_PATH = (
    "/"  # fleet knobs live at the AI Fleet Coordinator project's root (fleet_knobs/source.py)
)
CALL_TIMEOUT_S = 20.0
POLL_FIRST_S, POLL_LATER_S = 1.0, 2.0
KEY_CACHE_S = 600.0
USER_AGENT = "agent-sync notes client"
# The log filters on the wire, in a fixed order:  (query name, notes.check_log_args key).
LOG_WIRE = (
    ("since_hours", "since_hours"),
    ("limit", "limit"),
    ("by_seat", "seat"),
    ("action", "action"),
    ("title", "title"),
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, *args: Any, **kwargs: Any
    ) -> None:  # a signed request never follows a redirect
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def fleet_secrets_get(
    name: str,
    path: str,
    env: Mapping[str, str],
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """One value from Infisical (AI Fleet Coordinator, prod) through `fleet-secrets get`, quotes dropped;  "" when
    fleet-secrets is missing, refuses or fails.  A lookup that hangs is NotesError not_configured, never a missing value."""
    binary = shutil.which("fleet-secrets", path=env.get("PATH")) or os.path.expanduser(
        "~/.local/bin/fleet-secrets"
    )
    if not os.access(binary, os.X_OK):
        return ""
    try:
        done = runner(
            [binary, "get", name, "--path", path, "--project", W.KEY_PROJECT],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            env=dict(env),
        )
    except subprocess.TimeoutExpired:
        # A hung lookup is not a missing value:  say so, so the caller does not go looking for a key that is there.
        # Nothing has been sent yet, so this is never an unknown outcome.
        raise N.NotesError(
            "not_configured",
            "fleet-secrets did not answer within 30 seconds reading %s, so nothing was sent;  try again"
            % name,
        ) from None
    except (OSError, subprocess.SubprocessError):
        return ""
    if done.returncode != 0:
        return ""
    return (done.stdout or "").strip().strip('"').strip("'")


def base_url(
    env: Mapping[str, str],
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str | None:
    """The Operator API's https origin:  OPERATOR_API_URL in the environment (tests, a one-off override), else the fleet
    knob of that name, else the same key read with `fleet-secrets get`.  The last step is for a host where fleet_knobs
    has no Infisical login of its own:  the server's agent shells carry no automation pair, so there the knob is only
    its last-known-good copy, which lapses after 48 hours (fleet_knobs/lkg.py), while fleet-secrets logs in for itself
    the way it does for the principal's key.  None when none of them names an https origin with no path."""
    raw = (env.get(KNOB) or "").strip()
    if not raw:
        try:
            from fleet_knobs import (
                knobs,
            )  # scripts/fleet_knobs:  absent on a host that does not ship it

            raw = str(knobs.resolve(KNOB, "", "str").value or "").strip()
        except Exception:  # noqa: BLE001 - an unreadable knob reads as not configured, never as a crash
            raw = ""
    if not raw:
        raw = fleet_secrets_get(KNOB, KNOB_PATH, env, runner).strip()
    if not raw:
        return None
    parts = urllib.parse.urlsplit(raw)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        return None
    if parts.username or parts.password:
        return None
    return "https://%s" % parts.netloc


class KeySource:
    """A principal's key:  the environment variable of its name (a container, a test), else `fleet-secrets get`.
    Kept in memory for KEY_CACHE_S, and dropped when the Operator answers 401 so a rotation is picked up."""

    def __init__(
        self,
        principal: str,
        env: Mapping[str, str],
        *,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.name = W.KEY_NAMES[principal]
        self.env = env
        self.runner = runner
        self.clock = clock
        self._value: str | None = None
        self._at = 0.0
        self._lock = threading.Lock()

    def get(self) -> str | None:
        with self._lock:
            if self._value and self.clock() - self._at < KEY_CACHE_S:
                return self._value
            value = (self.env.get(self.name) or "").strip().strip('"').strip("'")
            if not value:
                value = self._fetch()
            self._value = value if len(value) >= W.MIN_KEY_LEN else None
            self._at = self.clock()
            return self._value

    def forget(self) -> None:
        with self._lock:
            self._value = None

    def _fetch(self) -> str:
        return fleet_secrets_get(self.name, W.KEY_PATH, self.env, self.runner)


class OperatorClient:
    """One principal's signed calls to the Operator notes API."""

    def __init__(
        self,
        base: str,
        principal: str,
        key: KeySource | Callable[[], str | None],
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        opener: Callable[..., Any] = _OPENER.open,
    ) -> None:
        self.base = base.rstrip("/")
        self.principal = principal
        self.key = key
        self.clock = clock
        self.sleep = sleep
        self.opener = opener

    def _key(self) -> str | None:
        return self.key.get() if isinstance(self.key, KeySource) else self.key()

    def call(
        self,
        method: str,
        path: str,
        *,
        query: str = "",
        payload: Any = None,
        timeout: float = CALL_TIMEOUT_S,
    ) -> tuple[int, dict[str, Any] | None]:
        """(status, JSON body or None).  Raises NotesError for a missing key, a network failure or a body that is not
        JSON;  an HTTP error status is returned, not raised."""
        key = self._key()
        if not key:
            raise N.NotesError(
                "not_configured",
                "no %s key on this host (%s in Infisical AI Fleet Coordinator prod %s)"
                % (self.principal, W.KEY_NAMES[self.principal], W.KEY_PATH),
            )
        body = (
            b""
            if payload is None
            else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        url = self.base + path + ("?" + query if query else "")
        request = urllib.request.Request(
            url, data=body if method == "POST" else None, method=method
        )
        for name, value in W.headers(
            key, method, path, query, self.principal, body, self.clock()
        ).items():
            request.add_header(name, value)
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", USER_AGENT)
        try:
            with self.opener(request, timeout=timeout) as response:
                status, raw = response.status, response.read(W.MAX_BODY + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            try:
                raw = exc.read(64 * 1024)
            except OSError:
                raw = b""
            finally:
                exc.close()
            if status == 401 and isinstance(self.key, KeySource):
                self.key.forget()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            failure = N.NotesError(
                "unavailable",
                "the Operator notes API could not be reached (%s)" % type(exc).__name__,
            )
            # A refused connection or a name that does not resolve never sent a byte:  definite, so `call` keeps it a
            # plain unavailable.  Anything else (a reset, a timeout) may have reached the Operator.
            reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            failure.definite = isinstance(reason, (ConnectionRefusedError, socket.gaierror))
            raise failure from None
        if not raw:
            return status, None
        if len(raw) > W.MAX_BODY:  # refused before it is parsed
            raise N.NotesError(
                "unavailable",
                "the Operator answered HTTP %d with a body larger than 4 MiB" % status,
            )
        try:
            data = json.loads(raw.decode("utf-8"))
        except (
            UnicodeDecodeError,
            ValueError,
            RecursionError,
        ):  # RecursionError:  nesting too deep to parse
            if status >= 300:
                return status, None  # a proxy's page on an error:  the status says what happened
            raise N.NotesError(
                "unavailable", "the Operator answered HTTP %d with a body that is not JSON" % status
            ) from None
        return status, data if isinstance(data, dict) else None

    # ---- writers --------------------------------------------------------------------------------
    def enqueue(self, seat: str, request: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        """(the record, whether it was already there)."""
        status, data = self.call(
            "POST", "/notes/v1/requests", payload={"seat": seat, "request": dict(request)}
        )
        if status in (200, 202) and data is not None:
            return _own_record(data, seat, request["request_id"]), status == 200
        refusal = answer_error(status, data, self.principal)
        # A 429 (rate limited, before anything is queued) and the queue's own 503 (full, or not running) are definite
        # refusals:  nothing was queued, so `call` passes them on as unavailable instead of an unknown outcome, as
        # the Worker does.  A bare 5xx from a proxy stays unknown.
        refusal.definite = status == 429 or (
            status == 503 and (data or {}).get("error") == "unavailable"
        )
        raise refusal

    def record(self, seat: str, request_id: str) -> dict[str, Any]:
        status, data = self.call("GET", "/notes/v1/requests/%s/%s" % (seat, request_id))
        if status == 200 and data is not None:
            return _own_record(data, seat, request_id)
        raise answer_error(status, data, self.principal)

    def wait(self, seat: str, record: Mapping[str, Any], seconds: float) -> dict[str, Any]:
        """The record once it is final, or as it stands when `seconds` have passed.  A poll that fails for any reason
        (no answer, or a key lookup that fails after the enqueue) ends the wait with the record as last seen (queued or
        running, with its request_id), never an error:  the request is already queued, and an error would send the
        caller to retry under a new id and write twice.  The Worker's wait does the same."""
        current = dict(record)
        request_id = record.get("request_id")
        if not isinstance(request_id, str) or not N.REQUEST_ID_RE.fullmatch(request_id):
            # enqueue and record check every record against its own request, so this is a caller's mistake
            raise N.NotesError(
                "invalid_argument", "the record to wait on names no valid request_id"
            )
        waited, polls = 0.0, 0
        # Counted from the sleeps, not a clock:  a call's own time only makes the wait a little longer, never shorter.
        while (
            current.get("state") not in ("done", "refused", "unknown", "expired")
            and waited < seconds
        ):
            step = min(seconds - waited, POLL_FIRST_S if polls < 5 else POLL_LATER_S)
            self.sleep(step)
            waited += step
            polls += 1
            try:
                current = self.record(
                    seat, request_id
                )  # the enqueued id, never one a poll answered
            except N.NotesError:
                break
        return current

    def log(self, filters: Mapping[str, Any]) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            [
                (wire, str(filters[name]))
                for wire, name in LOG_WIRE
                if filters.get(name) not in (None, "")
            ]
        )
        status, data = self.call("GET", "/notes/v1/log", query=query)
        if status == 200 and data is not None:
            return [
                N.clean_log_entry(entry) or {}
                for entry in data.get("entries") or []
                if isinstance(entry, dict)
            ]
        raise answer_error(status, data, self.principal)

    def health(self) -> dict[str, Any]:
        status, data = self.call("GET", "/notes/v1/health")
        if status == 200 and data is not None:
            return data
        raise answer_error(status, data, self.principal)

    # ---- the runner ------------------------------------------------------------------------------
    def claim(self, wait: int) -> dict[str, Any] | None:
        status, data = self.call(
            "POST", "/notes/v1/runner/claim", payload={"wait": wait}, timeout=CALL_TIMEOUT_S + wait
        )
        if status == 204:
            return None
        if status == 200 and data is not None:
            return data
        raise answer_error(status, data, self.principal)

    def report(
        self, seat: str, request_id: str, claim: str, *, result: Any = None, error: Any = None
    ) -> int:
        payload: dict[str, Any] = {"seat": seat, "request_id": request_id, "claim": claim}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result
        # A report over the queue's body cap is answered 413 here, before it is sent, encoded as `call` encodes it:
        # the queue answers 413 from the length alone and closes, so a sender only ever sees a reset, and the runner
        # would retry it for ever.  A 413 makes the runner send a small error report instead and let the request go.
        try:
            size = len(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
        except UnicodeEncodeError:  # text UTF-8 cannot hold:  a refusal the queue would always give
            return 400
        if size > W.MAX_BODY:
            return 413
        status, data = self.call("POST", "/notes/v1/runner/result", payload=payload)
        body = data if isinstance(data, Mapping) else {}
        code, message = str(body.get("error") or ""), str(body.get("message") or "")
        # Final only when it is the queue's own answer:  the record of this request (200);  "no such request" (404);
        # another claim holds it (409);  or a refusal it will always give (400, 413).  Sending it again changes
        # nothing, so the runner lets it go.  Anything else (an empty body, a proxy's page, or the queue's 404 to a
        # principal it holds no key for) leaves the outcome with the runner for the next pass.
        if status == 200 and body.get("seat") == seat and body.get("request_id") == request_id:
            return status
        if (
            (status == 404 and code == "not_found" and message == "no such request")
            or (status == 409 and code == "conflict")
            or (status == 400 and code == "invalid_argument")
            or (status == 413 and code == "too_large")
        ):
            return status
        if status == 200:
            raise N.NotesError(
                "unavailable", "the Operator answered the report without this request's record"
            )
        raise answer_error(status, data, self.principal)

    def send_log(self, lines: list[dict[str, Any]]) -> int:
        status, data = self.call("POST", "/notes/v1/runner/log", payload={"lines": lines})
        if status == 200 and data is not None:
            return int(data.get("accepted") or 0)
        raise answer_error(status, data, self.principal)


def _own_record(data: Mapping[str, Any], seat: str, request_id: str) -> dict[str, Any]:
    """`data` when it is the record of this seat's request, as the Worker's readRecord checks;  else unavailable (an
    enqueue answered that way may still have queued the request, so `call` makes it outcome_unknown)."""
    if data.get("seat") != seat or data.get("request_id") != request_id:
        raise N.NotesError(
            "unavailable", "the Operator answered with the record of another request"
        )
    return dict(data)


def answer_error(
    status: int, data: Mapping[str, Any] | None, principal: str | None = None
) -> N.NotesError:
    """The NotesError for an HTTP answer that is not a success.  `principal` names the key in a 401 or 404 (the
    variable and where it lives, never any part of its value)."""
    code = str((data or {}).get("error") or "")
    message = str((data or {}).get("message") or "")[:400]
    if 300 <= status < 400:
        # The queue never redirects, so a redirect proves it never saw the request:  a configuration fault, as the
        # Worker reads it too.
        return N.NotesError(
            "not_configured",
            "the Operator answered with a redirect, which this client never follows (check the fleet knob %s)"
            % KNOB,
        )
    if status == 400:
        return N.NotesError(
            code if code in ("invalid_argument", "session_required") else "invalid_argument",
            message or "the Operator refused the request",
        )
    if status == 409:
        return N.NotesError(
            "invalid_argument",
            message or "this request_id was already used for a different request",
        )
    if status == 404 and code == "not_found" and message.startswith("no request"):
        return N.NotesError("not_found", message)
    if status in (401, 404):
        which = (
            "%s (Infisical AI Fleet Coordinator prod %s)" % (W.KEY_NAMES[principal], W.KEY_PATH)
            if principal in W.KEY_NAMES
            else "this host's notes key"
        )
        return N.NotesError(
            "not_configured",
            "the Operator does not accept %s (HTTP %d);  check that key and the Operator's [notes_queue]"
            % (which, status),
        )
    if status == 403:
        return N.NotesError("forbidden", message or "the Operator refused this seat")
    if status == 413:
        return N.NotesError("invalid_argument", message or "the request is too large")
    return N.NotesError(
        "unavailable",
        "the Operator notes API answered HTTP %d%s"
        % (status, (":  " + message) if message else ""),
    )


def operator_client(
    env: Mapping[str, str], principal: str = W.SERVER, **kwargs: Any
) -> OperatorClient:
    """The client for this host, or NotesError not_configured when the knob or the key is missing."""
    base = base_url(env)
    if base is None:
        raise N.NotesError(
            "not_configured",
            "this host cannot run apple-notes-coding.sh and the Operator API is not configured (fleet knob %s)"
            % KNOB,
        )
    return OperatorClient(base, principal, KeySource(principal, env), **kwargs)


# --------------------------------------------------------------------------------------------
# The one implementation simple-notes and the stdio MCP share
# --------------------------------------------------------------------------------------------


class Host:
    """Where this process stands:  the environment, the home folder, the seat's idempotency rows and, off a Mac, the
    Operator client (made on first use, so a Mac never reads the knob or a key)."""

    def __init__(
        self,
        env: Mapping[str, str],
        home: Path,
        store: Any,
        *,
        caller: str,
        client_factory: Callable[[], OperatorClient] | None = None,
        bearer_file: str | None = None,
        script_finder: Callable[[Mapping[str, str], Path], Path | None] | None = None,
    ) -> None:
        self._fleet_client = None
        self.bearer_file = bearer_file
        self.env = env
        self.home = home
        self.store = store
        self.caller = caller
        self._client_factory = client_factory or (lambda: operator_client(env))
        self._client: OperatorClient | None = None
        # Looked up when the host is made, not bound as a default argument at import:  notes.direct_available is the
        # one seam that says "this is a Mac", for the CLI, the stdio tools and their tests alike.
        self.script = (
            None if bearer_file is not None else (script_finder or N.direct_available)(env, home)
        )

    @property
    def client(self) -> OperatorClient:
        if self._client is None:
            self._client = self._client_factory()
        return self._client


_CODE_RE = re.compile(r"[a-z_]{1,40}")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f  ]+")


def _record_error(record: Mapping[str, Any]) -> dict[str, str]:
    """A record's error as {code, message}:  a code of the contract's shape (else empty), and the message as one line
    of at most 400 characters with no control character, since the CLI prints it to a terminal."""
    error = record.get("error") if isinstance(record.get("error"), Mapping) else {}
    code = str(error.get("code") or "")
    message = " ".join(_CONTROL_RE.sub(" ", str(error.get("message") or "")).split())
    return {"code": code if _CODE_RE.fullmatch(code) else "", "message": message[:400]}


def _shape(record: Mapping[str, Any], duplicate: bool) -> dict[str, Any]:
    """A tool's answer from an Operator record:  the same keys the direct path answers with."""
    state = str(record.get("state") or "")
    request_id = str(record.get("request_id") or "")
    action = record.get("action")
    error = _record_error(record)
    if state == "refused":
        raise N.NotesError(
            str(error.get("code") or "internal"),
            str(error.get("message") or "the request was refused"),
        )
    if state == "unknown":
        raise N.NotesError(
            "outcome_unknown",
            str(
                error.get("message")
                or "the outcome is unknown;  check the note, then send it again with a new request_id"
            ),
        )
    if state == "expired":
        # Final:  the queue answers this same record to the same request_id for 14 days, so the way on is a new one
        # (nothing was written).  The Worker's outcomeError says the same.
        raise N.NotesError(
            "unavailable",
            "nobody picked the request up within 24 hours (the Mac runner may be off), so nothing was written.  "
            "Send it again with a new request_id.",
        )
    if state not in ("queued", "running", "done"):
        # Not a state this client knows, so it cannot tell whether the note was written:  never a plain retry.
        raise N.NotesError(
            "outcome_unknown",
            "the Operator answered a record in a state this client does not know, so the outcome is unknown;  "
            "check the note before sending it again with a new request_id",
        )
    out: dict[str, Any] = {
        "request_id": request_id,
        "action": action,
        "status": state,
        "transport": "operator",
    }
    if record.get("title"):
        out["title"] = record["title"]
    result = record.get("result") if isinstance(record.get("result"), Mapping) else {}
    if state == "done":
        if action == "read":
            out["note"] = result.get("note")
        elif action == "list":
            out.update(
                notes=result.get("notes") or [],
                total=result.get("total", 0),
                truncated=bool(result.get("truncated")),
            )
        else:
            for key in ("note_id", "pin_skipped"):
                if result.get(key):
                    out[key] = result[key]
            out["duplicate"] = duplicate
        if record.get("result_expired"):
            out["result_expired"] = True
    return out


def call(
    host: Host,
    seat: str,
    request: Mapping[str, Any],
    *,
    wait: float,
    scan: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run one request for `seat`:  directly on a Mac, through the Operator elsewhere.  `scan` checks a write's text
    first (it raises to refuse);  without one, notes.scan_write does, so no write is ever sent unscanned.  It runs
    before request_problem, whose refusals can name a part of the request.  Raises NotesError."""
    action = request["action"]
    if action in N.WRITE_ACTIONS:
        (scan or N.scan_write)(request)
    problem = N.request_problem(request, seat, env=host.env)
    if problem:
        raise N.NotesError(
            "session_required" if "session id is required" in problem else "invalid_argument",
            problem,
        )
    base = {"request_id": request["request_id"], "action": action}
    if request.get("title"):
        base["title"] = request["title"]
    if host.bearer_file is not None:
        return _fleet_call(host, seat, request, wait)
    if host.script is not None:
        script = host.script

        def send() -> dict[str, Any]:
            done = N.run_direct(request, script, env=host.env, caller=host.caller)
            return {**base, "transport": "direct", **done, "status": "done"}

        if action in ("read", "list"):
            # A read changes nothing, so it takes no idempotency row;  a small status row (no note text) lets
            # notes_status answer for it here as it does through the Operator.
            done = send()
            N.remember_direct_read(host.store, request)
            return done
        return N.direct_once(host.store, request, send)
    try:
        record, duplicate = host.client.enqueue(seat, request)
    except N.NotesError as exc:
        if exc.code != "unavailable" or getattr(exc, "definite", False):
            raise
        # The request may have reached the Operator before the answer was lost (a timeout, a 5xx), so its outcome is
        # unknown.  Checking it, or sending it again under the same request_id, is safe:  the Operator answers the
        # record it has.  A new request_id could write the note twice.
        rid = request["request_id"]
        raise N.NotesError(
            "outcome_unknown",
            "%s, so the request may or may not be queued.  Check it with notes_status (simple-notes status %s), "
            "or send it again with the same request_id, %s, never a new one"
            % (exc.message.rstrip(". "), rid, rid),
        ) from None
    if wait > 0:
        record = host.client.wait(seat, record, wait)
    return _shape(record, duplicate)


def status(host: Host, seat: str, request_id: str) -> dict[str, Any]:
    """Where a request stands:  this Mac's own rows on a Mac, the Operator's record elsewhere."""
    N.check_request_id(request_id)
    if host.bearer_file is not None:
        return _fleet_tool(host, seat, "notes_status", {"request_id": request_id})
    if host.script is not None:
        found = N.direct_status(host.store, request_id)
        if found is None:
            raise N.NotesError(
                "not_found",
                "this Mac ran no notes request with that request_id in the last 24 hours",
            )
        return found
    record = host.client.record(seat, request_id)
    out: dict[str, Any] = {
        "request_id": request_id,
        "status": record.get("state"),
        "action": record.get("action"),
        "transport": "operator",
    }
    for key in ("title", "queued_at", "finished_at"):
        if record.get(key):
            out[key] = record[key]
    if record.get("error"):
        out["error"] = _record_error(record)
    result = record.get("result") if isinstance(record.get("result"), Mapping) else {}
    for key in ("note_id", "pin_skipped", "note", "notes"):
        if result.get(key):
            out[key] = result[key]
    return out


def log(host: Host, args: Mapping[str, Any]) -> dict[str, Any]:
    """The action log:  the file itself on a Mac, the Operator's copy elsewhere."""
    filters = N.check_log_args(args)
    if host.bearer_file is not None:
        return _fleet_tool(host, None, "notes_log", dict(args))
    if host.script is not None:
        entries = N.read_action_log(host.env, host.home, **filters)
        return {"entries": entries, "source": "mac", "count": len(entries)}
    entries = host.client.log(filters)
    return {"entries": entries, "source": "operator", "count": len(entries)}


def _fleet_tool(host, seat, tool, args, write=False):
    from .fleet_bearer import Client, FleetError

    try:
        if host._fleet_client is None:
            host._fleet_client = Client(host.bearer_file, seat, identity_env=host.env)
        if seat is not None and host._fleet_client.seat != seat:
            raise FleetError("not_authorized", "fleet bearer does not name the requested bot")
        result = host._fleet_client.call(tool, args, write)
        data = result.get("structuredContent")
        if not isinstance(data, dict):
            raise FleetError(
                "outcome_unknown" if write else "upstream_error", "gateway returned no notes result"
            )
        return data
    except FleetError as exc:
        recover = ""
        if write and args.get("request_id"):
            recover = (
                "; check simple-notes status "
                + args["request_id"]
                + " using the same fleet bearer file, never retry with a new request id"
            )
        raise N.NotesError(exc.code, str(exc) + recover) from None


def _fleet_call(host, seat, request, wait):
    action = request["action"]
    args = {
        key: value
        for key, value in request.items()
        if key in ("title", "markdown", "session", "format", "query", "limit", "request_id")
    }
    args.update(N.check_flags(request.get("flags")))
    args["wait_seconds"] = 0
    deadline = time.monotonic() + wait
    rid = request["request_id"]
    recover = (
        "; check simple-notes status "
        + rid
        + " using the same fleet bearer file, never repeat with a new request id"
    )

    def checked(out):
        if not isinstance(out, dict) or out.get("request_id") != rid or out.get("action") != action:
            raise N.NotesError(
                "outcome_unknown",
                "gateway notes identity/result did not match the original request" + recover,
            )
        state = out.get("status")
        if state == "refused":
            error = out.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if not isinstance(code, str) or not _CODE_RE.fullmatch(code):
                code = "internal"
            raise N.NotesError(code, "notes request was refused" + recover)
        if state == "expired":
            raise N.NotesError(
                "unavailable",
                "notes request expired before it was run; nothing was written" + recover,
            )
        if state not in ("queued", "running", "done") or out.get("error"):
            raise N.NotesError("outcome_unknown", "notes request outcome is unknown" + recover)
        if state == "done" and out.get("result_expired") is True:
            raise N.NotesError(
                "result_expired", "notes result expired; no note body is available" + recover
            )
        if (
            state == "done"
            and action == "read"
            and (
                not isinstance(out.get("note"), dict)
                or not isinstance(out["note"].get("body"), str)
            )
        ):
            raise N.NotesError("upstream_error", "completed notes read has no note" + recover)
        if (
            state == "done"
            and action == "list"
            and (
                not isinstance(out.get("notes"), list)
                or type(out.get("total")) is not int
                or out["total"] < len(out["notes"])
                or type(out.get("truncated")) is not bool
            )
        ):
            raise N.NotesError(
                "upstream_error", "completed notes list has no complete list metadata" + recover
            )
        return out

    try:
        out = _fleet_tool(host, seat, "notes_" + action, args, action in N.WRITE_ACTIONS)
        # Hosted read/list enqueue summaries omit action. A status fetch binds
        # both action and request id before accepting their result.
        if action in ("read", "list") and isinstance(out, dict) and "action" not in out:
            if out.get("request_id") != rid:
                raise N.NotesError("outcome_unknown", "notes request id did not match" + recover)
            out = _fleet_tool(host, seat, "notes_status", {"request_id": rid})
        out = checked(out)
        while out.get("status") in ("queued", "running") and time.monotonic() < deadline:
            time.sleep(min(2, max(0, deadline - time.monotonic())))
            try:
                candidate = _fleet_tool(host, seat, "notes_status", {"request_id": rid})
            except N.NotesError:
                out = {**out, "poll_warning": "status poll failed" + recover}
                break
            out = checked(candidate)
    except N.NotesError as exc:
        if recover in exc.message:
            raise
        raise N.NotesError(exc.code, exc.message + recover) from None
    if request.get("title"):
        out.setdefault("title", request["title"])
    return out
