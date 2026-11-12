"""A fake Grok Bot routine webhook for the listener's `http` wake tests.

ThreadingHTTPServer on 127.0.0.1 with a random port.  It records every request (method, path,
query, headers and the raw body bytes) under a Condition, so tests wait on recorded state, never
on a sleep.  `script` lists what to answer, one entry per request:  a status code, "drop" (close
the connection without an answer), "redirect" (a 302 to another host), or (status, dict) to answer
that JSON object (the hosted MCP Worker's answers, which also echo the auth headers), or a callable
that takes the Received request and returns one of those (the wake router's test answer, which
repeats the request's wake_id).  An empty script answers 200.  Every answer echoes the request's
auth headers back in its body, the way a careless server might, so the tests can prove the daemon
never logs a response body.  A GET is answered from `health` (path to (status, dict), or to
("redirect", URL) for a 302 there, else 404, the way the wake bridges answer `/health/<SEAT>`) and kept in `health_requests`, not `received`.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit


@dataclass
class Received:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes

    def header(self, name: str) -> str | None:
        for key, value in self.headers.items():
            if key.casefold() == name.casefold():
                return value
        return None

    def json(self) -> dict[str, Any]:
        return json.loads(self.body.decode("utf-8"))


class FakeRoutine:
    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.received: list[Received] = []
        self.script: list[Any] = []
        self.health: dict[str, tuple[int | str, Any]] = {}
        self.health_requests: list[Received] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                return

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                parts = urlsplit(self.path)
                with fake.cond:
                    fake.received.append(Received(self.command, parts.path,
                                                  {k: v[0] for k, v in parse_qs(parts.query).items()},
                                                  {k: v for k, v in self.headers.items()}, body))
                    action = fake.script.pop(0) if fake.script else 200
                    if callable(action):  # an answer built from the request it answers
                        action = action(fake.received[-1])
                    fake.cond.notify_all()
                if action == "drop":
                    self.close_connection = True
                    return
                echo = {k: v for k, v in self.headers.items() if k.casefold() not in ("content-length", "host")}
                answer: dict[str, Any] = {"ok": True}
                if isinstance(action, tuple):
                    action, extra = action
                    answer.update(extra)
                raw = json.dumps(dict(answer, echo=echo)).encode()
                status = 302 if action == "redirect" else int(action)
                self.send_response(status)
                if action == "redirect":
                    self.send_header("Location", "http://127.0.0.2:9/stolen")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(raw)

            do_POST = do_PUT = do_PATCH = _handle

            def do_GET(self) -> None:
                # A GET is answered from `health` (path -> (status, dict)), or 404.  It is kept
                # in `health_requests`, never in `received`, so a POST count stays exact.
                path = urlsplit(self.path).path
                with fake.cond:
                    fake.health_requests.append(
                        Received("GET", path, {}, {k: v for k, v in self.headers.items()}, b"")
                    )
                    status, answer = fake.health.get(path, (404, {"error": "not found"}))
                location = answer if status == "redirect" else None
                if location is not None:
                    status, answer = 302, {}
                raw = json.dumps(answer).encode()
                self.send_response(status)
                if location is not None:
                    self.send_header("Location", location)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server.handle_error = lambda request, client_address: None
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def wait_for(self, count: int, timeout: float = 30.0) -> bool:
        with self.cond:
            return self.cond.wait_for(lambda: len(self.received) >= count, timeout=timeout)
