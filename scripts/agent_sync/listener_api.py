"""The server listener's read API (AFC-330 part B):  signed GET endpoints for one seat's records, so the
hosted MCP Worker can answer eavesdropper_status, eavesdropper_inbox, eavesdropper_owner_queue, eavesdropper_wakes and
eavesdropper_held_back for a hosted seat.

    GET /listener/v1/<SEAT>/<record>?<query>      record:  status, inbox, owner-queue, wakes, held-back

Authentication is per seat, with the HMAC key the listener already shares with the Worker for that
seat's wakes (the listener's routine key_env, default <CODE>_ROUTINE_KEY;  the Worker's
WAKE_HMAC_KEY_<SEAT>).  Two headers:

    X-Agent-Sync-Timestamp:  unix seconds
    X-Agent-Sync-Signature:  lowercase hex HMAC-SHA256 over
                             "agent-sync-read/1\\nGET\\n<path>\\n<raw query>\\n<timestamp>"

so a signature names one seat, one record, one exact query and one moment;  it is good for five
minutes either side of the listener's clock.  A seat with no key (or a key under 32 characters) is
404, like an unknown seat or path;  a bad or stale signature is 401;  anything but GET is 405.  The
answer is the record's view (listener_records.py) as JSON, with no-store caching.  The API is
read-only on purpose:  nothing a request carries can move a cursor or clear an entry.

Bodies in the views are Zulip-authored:  the Worker fences them as untrusted before any model sees
them.  Nothing here logs a key, a signature or a body.

The same server carries the Operator's notes queue (AFC-486, notes_queue.py) under /notes/v1/ when the daemon
gives it one:  GET and POST there, with its own principals and signature (notes_wire.py).  It likewise carries
the Linear project webhook (AFC-565, linear_projects_hook.py) under /linear/v1/ when given one:  POST only,
verified by the Linear-Signature.  Everything else stays GET only.  Python 3.11+, standard library only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import threading
import time
from collections.abc import Callable, Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from . import listener_records as LR
from . import notes_wire as NW
from .zulip import UsageError

CONTRACT = "agent-sync-read/1"
SIGNATURE_HEADER = "X-Agent-Sync-Signature"
TIMESTAMP_HEADER = "X-Agent-Sync-Timestamp"
MAX_SKEW_S = 300
MIN_KEY_LEN = 32
MAX_QUERY = 512
REQUEST_TIMEOUT_SECONDS = 5.0
DEFAULT_PORT = 8788
_PATH_RE = re.compile(
    r"^/listener/v1/([A-Z0-9][A-Z0-9_-]{0,31})/(status|inbox|owner-queue|wakes|held-back)$"
)
# The query parameters each record takes, and how each is checked.
_INT = re.compile(r"^\d{1,9}$")
_STAMP = re.compile(r"^\d{1,12}$")
_STATE = re.compile(r"^[a-z_]{1,32}$")
PARAMS: dict[str, dict[str, Callable[[str], Any]]] = {
    "status": {},
    "inbox": {"since_seq": lambda v: _num(v, 0, 10**9), "limit": lambda v: _num(v, 1, 50)},
    "owner-queue": {"all": lambda v: _flag(v), "limit": lambda v: _num(v, 1, 50)},
    "wakes": {
        "since_hours": lambda v: _num(v, 1, 720),
        "state": lambda v: _match(_STATE, v),
        "limit": lambda v: _num(v, 1, 100),
    },
    "held-back": {"limit": lambda v: _num(v, 1, 100)},
}


def _num(value: str, low: int, high: int) -> int:
    if not _INT.fullmatch(value) or not low <= int(value) <= high:
        raise ValueError("out of range")
    return int(value)


def _flag(value: str) -> bool:
    if value not in ("true", "false"):
        raise ValueError("not a flag")
    return value == "true"


def _match(pattern: re.Pattern[str], value: str) -> str:
    if not pattern.fullmatch(value):
        raise ValueError("bad value")
    return value


def signing_string(path: str, query: str, timestamp: str) -> bytes:
    return ("%s\nGET\n%s\n%s\n%s" % (CONTRACT, path, query, timestamp)).encode("utf-8")


def sign(key: str, path: str, query: str, timestamp: str) -> str:
    return hmac.new(
        key.encode("utf-8"), signing_string(path, query, timestamp), hashlib.sha256
    ).hexdigest()


def parse_params(record: str, query: str) -> dict[str, Any]:
    allowed = PARAMS[record]
    params: dict[str, Any] = {}
    for name, value in parse_qsl(query, keep_blank_values=True, strict_parsing=bool(query)):
        if name not in allowed or name in params:
            raise ValueError("unknown or repeated parameter")
        params[name] = allowed[name](value)
    return params


class ListenerReadApi:
    """A small threaded HTTP server over one state root.  `keys` maps seat -> shared HMAC key."""

    def __init__(
        self,
        bind: str,
        port: int,
        root: str,
        keys: Mapping[str, str],
        *,
        clock: Callable[[], float] = time.time,
        log: Callable[..., None] | None = None,
        notes: Any = None,
        linear_projects: Any = None,
    ) -> None:
        self.root = root
        self.notes = notes  # a notes_queue.NotesQueue, or None:  then /notes/v1/ is 404 like any unknown path
        self.linear_projects = linear_projects  # a linear_projects_hook.LinearProjectsHook, or None
        self.keys = {
            seat.upper(): key
            for seat, key in keys.items()
            if LR.SEAT_RE.fullmatch(seat.upper())
            and isinstance(key, str)
            and len(key) >= MIN_KEY_LEN
        }
        self.clock = clock
        self.log = log or (lambda *_args, **_kwargs: None)
        owner = self

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True
            request_queue_size = 16
            _slots = threading.BoundedSemaphore(8)

            def get_request(self):
                request, address = super().get_request()
                request.settimeout(REQUEST_TIMEOUT_SECONDS)
                return request, address

            def process_request(self, request, client_address):
                if not self._slots.acquire(blocking=False):
                    request.close()
                    return
                super().process_request(request, client_address)

            def process_request_thread(self, request, client_address):
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    self._slots.release()

            def handle_error(self, _request, _client_address):
                return

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, _format: str, *_args: Any) -> None:
                return

            def _respond(self, status: int, body: Mapping[str, Any]) -> None:
                data = json.dumps(
                    body, ensure_ascii=False, separators=(",", ":"), default=str
                ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except OSError:
                    pass

            def _refuse_method(self) -> None:
                self.close_connection = True
                self._respond(405, {"error": "method not allowed"})

            do_PUT = do_PATCH = do_DELETE = _refuse_method

            def _notes(self, method: str) -> bool:
                """Serve a /notes/v1/ request when the daemon gave this server a notes queue.  False otherwise."""
                if owner.notes is None or not self.path.startswith(NW.PREFIX):
                    return False
                body = b""
                if method == "POST":
                    raw_length = self.headers.get("Content-Length")
                    if raw_length is None or not raw_length.strip().isdigit():
                        self._respond(
                            411,
                            {"error": "length_required", "message": "Content-Length is required"},
                        )
                        return True
                    length = int(raw_length.strip())
                    if length > NW.MAX_BODY:
                        self._respond(
                            413, {"error": "too_large", "message": "the request body is too large"}
                        )
                        return True
                    try:
                        body = self.rfile.read(length)
                    except OSError:
                        return True
                    if len(body) != length:
                        self._respond(
                            400, {"error": "invalid_argument", "message": "the body ended early"}
                        )
                        return True
                try:
                    status, answer = owner.notes.handle(method, self.path, self.headers, body)
                except Exception as exc:  # noqa: BLE001 - one request must never take the server down
                    owner.log("notes-api-error", error=type(exc).__name__)
                    status, answer = 500, {"error": "internal", "message": "the notes queue failed"}
                if answer is None:
                    self.send_response(status)
                    self.send_header("Content-Length", "0")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.end_headers()
                else:
                    self._respond(status, answer)
                return True

            def _linear(self, method: str) -> bool:
                """Serve a /linear/v1/ request when the daemon gave this server a projects hook."""
                if owner.linear_projects is None or not self.path.startswith("/linear/v1/"):
                    return False
                body = b""
                if method == "POST":
                    raw_length = self.headers.get("Content-Length")
                    if raw_length is None or not raw_length.strip().isdigit():
                        self._respond(
                            411,
                            {"error": "length_required", "message": "Content-Length is required"},
                        )
                        return True
                    length = int(raw_length.strip())
                    if length > 256 * 1024:  # linear_projects_hook.MAX_BODY
                        self._respond(
                            413, {"error": "too_large", "message": "the request body is too large"}
                        )
                        return True
                    try:
                        body = self.rfile.read(length)
                    except OSError:
                        return True
                    if len(body) != length:
                        self._respond(
                            400, {"error": "invalid_argument", "message": "the body ended early"}
                        )
                        return True
                try:
                    status, answer = owner.linear_projects.handle(
                        method, self.path, self.headers, body
                    )
                except Exception as exc:  # noqa: BLE001 - one request must never take the server down
                    owner.log("linear-projects-api-error", error=type(exc).__name__)
                    status, answer = 500, {"error": "internal", "message": "the hook failed"}
                if answer is None:
                    self.send_response(status)
                    self.send_header("Content-Length", "0")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Connection", "close")
                    self.end_headers()
                else:
                    self._respond(status, answer)
                return True

            def do_POST(self) -> None:
                self.close_connection = True
                if self._notes("POST"):
                    return
                if self._linear("POST"):
                    return
                self._respond(405, {"error": "method not allowed"})

            def do_GET(self) -> None:
                self.close_connection = True
                if self._notes("GET"):
                    return
                status, body = owner.handle(
                    self.path,
                    self.headers.get(TIMESTAMP_HEADER),
                    self.headers.get(SIGNATURE_HEADER),
                )
                self._respond(status, body)

        self.server = Server((bind, port), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="listener-read-api",
            daemon=True,
        )

    def handle(
        self, raw_path: str, timestamp: str | None, signature: str | None
    ) -> tuple[int, dict[str, Any]]:
        """(status, body) for one GET.  Pure, so tests call it without a socket."""
        parts = urlsplit(raw_path)
        match = _PATH_RE.fullmatch(parts.path)
        if not match or parts.fragment or len(parts.query) > MAX_QUERY:
            return 404, {"error": "not found"}
        seat, record = match.groups()
        key = self.keys.get(seat)
        if key is None:
            return 404, {"error": "not found"}
        now = self.clock()
        if (
            not timestamp
            or not _STAMP.fullmatch(timestamp.strip())
            or abs(now - int(timestamp)) > MAX_SKEW_S
        ):
            self.log("listener-api-refused", seat=seat, reason="timestamp")
            return 401, {"error": "unauthorized"}
        expected = sign(key, parts.path, parts.query, timestamp.strip())
        if not signature or not hmac.compare_digest(expected, signature.strip().lower()):
            self.log("listener-api-refused", seat=seat, reason="signature")
            return 401, {"error": "unauthorized"}
        try:
            params = parse_params(record, parts.query)
        except ValueError:
            return 400, {"error": "bad parameters"}
        try:
            view = LR.view(self.root, seat, record, params, now=now)
        except UsageError:
            return 409, {"error": "records unavailable"}
        except OSError:
            return 503, {"error": "temporarily unavailable"}
        self.log("listener-api-read", seat=seat, record=record)
        return 200, {"contract": CONTRACT, "seat": seat, "record": record, "view": view}

    @property
    def address(self) -> tuple[str, int]:
        host, port = self.server.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        if self.thread.is_alive():
            self.thread.join(timeout=2.0)
