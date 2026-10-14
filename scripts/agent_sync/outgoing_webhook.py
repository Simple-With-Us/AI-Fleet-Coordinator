"""Bounded Zulip outgoing-webhook HTTP ingress and durable accepted-message spool.

Only authenticated metadata is persisted.  The daemon later fetches the message through the
configured seat's Zulip API client before routing it, so webhook fields never become router input.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from . import live as L

MAX_BODY_BYTES = 128 * 1024
REQUEST_TIMEOUT_SECONDS = 3.0
MAX_PENDING_EVENTS = 10000
_SEAT_RE = re.compile(r"^[A-Z0-9][A-Z0-9_-]{0,31}$")
_FILE_RE = re.compile(r"^([A-Z0-9][A-Z0-9_-]{0,31})-(\d{1,20})\.json$")


class DurableSpool:
    """One fsynced, private file per accepted (seat, Zulip message id), replayable after restart."""

    def __init__(self, directory: str) -> None:
        self.directory = directory
        L.private_dir(directory)
        os.chmod(directory, 0o700)
        self.lock = threading.Lock()

    @staticmethod
    def _name(seat: str, message_id: int) -> str:
        normalized = seat.upper()
        if (not _SEAT_RE.fullmatch(normalized) or isinstance(message_id, bool) or
                not isinstance(message_id, int) or message_id <= 0):
            raise ValueError("invalid spool identity")
        return "%s-%d.json" % (normalized, message_id)

    def add(self, seat: str, message_id: int) -> bool:
        """Persist identity only; return False when this accepted event already has a spool file."""
        name = self._name(seat, message_id)
        payload = json.dumps({"seat": seat.upper(), "message_id": message_id},
                             sort_keys=True, separators=(",", ":")).encode("utf-8")
        path = os.path.join(self.directory, name)
        with self.lock:
            pending_count = sum(1 for item in os.listdir(self.directory) if _FILE_RE.fullmatch(item))
            if pending_count >= MAX_PENDING_EVENTS:
                raise OverflowError("webhook spool is full")
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                return False
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(fd, view)
                    view = view[written:]
                os.fsync(fd)
            except BaseException:
                os.close(fd)
                try:
                    os.unlink(path)
                except OSError:
                    pass
                raise
            else:
                os.close(fd)
            self._fsync_directory()
        return True

    def pending(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            names = os.listdir(self.directory)
        except FileNotFoundError:
            return rows
        for name in names:
            match = _FILE_RE.fullmatch(name)
            if not match:
                continue
            seat, raw_id = match.groups()
            try:
                with open(os.path.join(self.directory, name), "rb") as fh:
                    row = json.loads(fh.read(2048).decode("utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            message_id = int(raw_id)
            if (self._name(seat, message_id) == name and isinstance(row, dict) and
                    row == {"seat": seat, "message_id": message_id}):
                rows.append(row)
        return sorted(rows, key=lambda row: (row["message_id"], row["seat"]))

    def remove(self, seat: str, message_id: int) -> None:
        path = os.path.join(self.directory, self._name(seat, message_id))
        with self.lock:
            try:
                os.unlink(path)
            except FileNotFoundError:
                return
            self._fsync_directory()

    def _fsync_directory(self) -> None:
        fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


class OutgoingWebhookServer:
    """A small threaded HTTP receiver.  Tokens are never retained on disk or included in logs."""

    def __init__(self, bind: str, port: int, tokens: Mapping[str, str],
                 accept: Callable[[str, str, int], str],
                 log: Callable[..., None] | None = None) -> None:
        self.tokens = {seat.upper(): token for seat, token in tokens.items()
                       if _SEAT_RE.fullmatch(seat.upper()) and isinstance(token, str) and token}
        self.accept = accept
        self.log = log or (lambda *_args, **_kwargs: None)
        owner = self

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True
            request_queue_size = 32
            _slots = threading.BoundedSemaphore(16)

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
                data = json.dumps(body, separators=(",", ":")).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    self.wfile.write(data)
                except OSError:
                    pass

            def do_POST(self) -> None:  # noqa: N802
                self.close_connection = True
                parsed_path = urlsplit(self.path)
                path = parsed_path.path
                prefix = "/zulip/outgoing/"
                if (parsed_path.query or parsed_path.fragment or not path.startswith(prefix) or
                        not _SEAT_RE.fullmatch(path[len(prefix):])):
                    self._respond(404, {"error": "not found"})
                    return
                seat = path[len(prefix):].upper()
                expected = owner.tokens.get(seat)
                if expected is None:
                    self._respond(404, {"error": "not found"})
                    return
                if self.headers.get("Transfer-Encoding"):
                    self._respond(400, {"error": "invalid request"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    length = 0
                if length <= 0:
                    self._respond(400, {"error": "invalid request"})
                    return
                if length > MAX_BODY_BYTES:
                    self._respond(413, {"error": "request too large"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._respond(400, {"error": "invalid request"})
                    return
                self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)
                try:
                    raw = self.rfile.read(length)
                    body = json.loads(raw.decode("utf-8"))
                except (OSError, UnicodeDecodeError, ValueError):
                    self._respond(400, {"error": "invalid request"})
                    return
                if not isinstance(body, dict):
                    self._respond(400, {"error": "invalid request"})
                    return
                token = body.get("token")
                if (not isinstance(token, str) or
                        not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))):
                    owner.log("outgoing-webhook-rejected", seat=seat, reason="authentication")
                    self._respond(401, {"error": "unauthorized"})
                    return
                bot_email = body.get("bot_email")
                trigger = body.get("trigger")
                message = body.get("message")
                message_id = message.get("id") if isinstance(message, dict) else None
                if (not isinstance(bot_email, str) or len(bot_email) > 320 or
                        not isinstance(trigger, str) or len(trigger) > 80 or
                        isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0):
                    self._respond(400, {"error": "invalid request"})
                    return
                try:
                    status = owner.accept(seat, bot_email, message_id)
                except Exception:
                    status = "unavailable"
                if status in ("accepted", "duplicate"):
                    owner.log("outgoing-webhook-accepted", seat=seat, id=message_id,
                              duplicate=status == "duplicate")
                    self._respond(200, {"response_not_required": True})
                elif status == "unavailable":
                    owner.log("outgoing-webhook-deferred", seat=seat)
                    self._respond(503, {"error": "temporarily unavailable"})
                else:
                    owner.log("outgoing-webhook-rejected", seat=seat, reason="identity")
                    self._respond(401, {"error": "unauthorized"})

        self.server = Server((bind, port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.2},
                                       name="zulip-outgoing-webhook", daemon=True)

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
