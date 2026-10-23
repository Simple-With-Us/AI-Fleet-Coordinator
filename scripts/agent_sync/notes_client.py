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
import shutil
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


def base_url(env: Mapping[str, str]) -> str | None:
    """The Operator API's https origin:  OPERATOR_API_URL in the environment (tests, a one-off override), else the fleet
    knob of that name.  None when neither names an https origin with no path."""
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
        binary = shutil.which("fleet-secrets", path=self.env.get("PATH")) or os.path.expanduser(
            "~/.local/bin/fleet-secrets"
        )
        if not os.access(binary, os.X_OK):
            return ""
        try:
            done = self.runner(
                [binary, "get", self.name, "--path", W.KEY_PATH, "--project", W.KEY_PROJECT],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env=dict(self.env),
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        if done.returncode != 0:
            return ""
        return (done.stdout or "").strip().strip('"').strip("'")


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
            raise N.NotesError(
                "unavailable",
                "the Operator notes API could not be reached (%s)" % type(exc).__name__,
            ) from None
        if not raw:
            return status, None
        try:
            data = json.loads(
                raw.decode("utf-8")
            )  # the body is already capped at MAX_BODY bytes above
        except (
            UnicodeDecodeError,
            ValueError,
            RecursionError,
        ):  # RecursionError:  nesting too deep to parse
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
            return data, status == 200
        raise answer_error(status, data)

    def record(self, seat: str, request_id: str) -> dict[str, Any]:
        status, data = self.call("GET", "/notes/v1/requests/%s/%s" % (seat, request_id))
        if status == 200 and data is not None:
            return data
        raise answer_error(status, data)

    def wait(self, seat: str, record: Mapping[str, Any], seconds: float) -> dict[str, Any]:
        """The record once it is final, or as it stands when `seconds` have passed.  A poll that cannot reach the
        Operator ends the wait with the record as last seen (queued or running, with its request_id), never an error:
        the request is already queued, and an error would send the caller to retry under a new id and write twice."""
        current = dict(record)
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
                current = self.record(seat, str(current.get("request_id")))
            except N.NotesError as exc:
                if exc.code != "unavailable":
                    raise
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
        raise answer_error(status, data)

    def health(self) -> dict[str, Any]:
        status, data = self.call("GET", "/notes/v1/health")
        if status == 200 and data is not None:
            return data
        raise answer_error(status, data)

    # ---- the runner ------------------------------------------------------------------------------
    def claim(self, wait: int) -> dict[str, Any] | None:
        status, data = self.call(
            "POST", "/notes/v1/runner/claim", payload={"wait": wait}, timeout=CALL_TIMEOUT_S + wait
        )
        if status == 204:
            return None
        if status == 200 and data is not None:
            return data
        raise answer_error(status, data)

    def report(
        self, seat: str, request_id: str, claim: str, *, result: Any = None, error: Any = None
    ) -> int:
        payload: dict[str, Any] = {"seat": seat, "request_id": request_id, "claim": claim}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result
        status, data = self.call("POST", "/notes/v1/runner/result", payload=payload)
        if status in (200, 400, 404, 409, 413):
            # Recorded;  or the Operator no longer holds this claim;  or it refuses this report and always will (400,
            # 413).  Sending it again changes nothing, so the runner never holds on to it.
            return status
        raise answer_error(status, data)

    def send_log(self, lines: list[dict[str, Any]]) -> int:
        status, data = self.call("POST", "/notes/v1/runner/log", payload={"lines": lines})
        if status == 200 and data is not None:
            return int(data.get("accepted") or 0)
        raise answer_error(status, data)


def answer_error(status: int, data: Mapping[str, Any] | None) -> N.NotesError:
    """The NotesError for an HTTP answer that is not a success."""
    code = str((data or {}).get("error") or "")
    message = str((data or {}).get("message") or "")[:400]
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
        return N.NotesError(
            "not_configured",
            "the Operator does not accept this host's notes key (HTTP %d);  check the key in Infisical and the "
            "Operator's [notes_queue]" % status,
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
        script_finder: Callable[[Mapping[str, str], Path], Path | None] | None = None,
    ) -> None:
        self.env = env
        self.home = home
        self.store = store
        self.caller = caller
        self._client_factory = client_factory or (lambda: operator_client(env))
        self._client: OperatorClient | None = None
        # Looked up when the host is made, not bound as a default argument at import:  notes.direct_available is the
        # one seam that says "this is a Mac", for the CLI, the stdio tools and their tests alike.
        self.script = (script_finder or N.direct_available)(env, home)

    @property
    def client(self) -> OperatorClient:
        if self._client is None:
            self._client = self._client_factory()
        return self._client


def _shape(record: Mapping[str, Any], duplicate: bool) -> dict[str, Any]:
    """A tool's answer from an Operator record:  the same keys the direct path answers with."""
    state = str(record.get("state") or "")
    request_id = str(record.get("request_id") or "")
    action = record.get("action")
    error = record.get("error") if isinstance(record.get("error"), Mapping) else {}
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
        raise N.NotesError(
            "unavailable",
            str(error.get("message") or "nobody picked the request up within 24 hours"),
        )
    if state not in ("queued", "running", "done"):
        raise N.NotesError(
            "unavailable", "the Operator answered a record in a state this client does not know"
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
    first (it raises to refuse).  Raises NotesError."""
    problem = N.request_problem(request, seat, env=host.env)
    if problem:
        raise N.NotesError(
            "session_required" if "session id is required" in problem else "invalid_argument",
            problem,
        )
    action = request["action"]
    if action in N.WRITE_ACTIONS and scan is not None:
        scan(request)
    base = {"request_id": request["request_id"], "action": action}
    if request.get("title"):
        base["title"] = request["title"]
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
        if exc.code != "unavailable":
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
    for key in ("title", "queued_at", "finished_at", "error"):
        if record.get(key):
            out[key] = record[key]
    result = record.get("result") if isinstance(record.get("result"), Mapping) else {}
    for key in ("note_id", "pin_skipped", "note", "notes"):
        if result.get(key):
            out[key] = result[key]
    return out


def log(host: Host, args: Mapping[str, Any]) -> dict[str, Any]:
    """The action log:  the file itself on a Mac, the Operator's copy elsewhere."""
    filters = N.check_log_args(args)
    if host.script is not None:
        entries = N.read_action_log(host.env, host.home, **filters)
        return {"entries": entries, "source": "mac", "count": len(entries)}
    entries = host.client.log(filters)
    return {"entries": entries, "source": "operator", "count": len(entries)}
