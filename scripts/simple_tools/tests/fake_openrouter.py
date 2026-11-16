"""A fake OpenRouter on 127.0.0.1 for the OpenRouter wake runtime's tests.

It implements only `POST /api/v1/chat/completions`, runs on a random loopback port in a thread, records
every request it receives (the headers and the parsed body), and answers from a script:  a valid
chat-completions answer by default, or whatever a test queues (an HTTP error, a body that is not JSON, a
provider error carried in a 200, a slow answer).  No test ever reaches openrouter.ai, and the key a test
plants is made at run time by the caller, so the source holds no key-shaped literal.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass
class Seen:
    """One request the fake received."""

    path: str
    headers: dict[str, str]
    raw: bytes
    body: Any = None

    @property
    def bearer(self) -> str:
        return self.headers.get("authorization", "")


@dataclass
class Reply:
    """What the fake answers to one request.  `body` may be a dict (sent as JSON), a str or bytes (sent
    as they are).  `delay` holds the response back;  `drip` sends the body one byte at a time with that
    pause between bytes, which a per-read timeout alone never catches."""

    status: int = 200
    body: Any = None
    delay: float = 0.0
    drip: float = 0.0
    headers: dict[str, str] = field(default_factory=dict)


def completion(
    content: Any,
    *,
    model: str = "meta/muse-spark-1.3-contributor-20260902",
    finish: str = "stop",
    usage: dict[str, Any] | None = None,
    **message: Any,
) -> dict[str, Any]:
    """A chat-completions body whose assistant message is `content` (a dict is sent as its JSON text;
    a list is sent as content parts, a string as it is)."""
    text = json.dumps(content) if isinstance(content, dict) else content
    msg: dict[str, Any] = {"role": "assistant", "content": text}
    msg.update(message)
    return {
        "id": "gen-test",
        "model": model,
        "provider": "Test",
        "choices": [{"index": 0, "finish_reason": finish, "message": msg}],
        "usage": usage
        if usage is not None
        else {
            "prompt_tokens": 1200,
            "completion_tokens": 80,
            "total_tokens": 1280,
            "completion_tokens_details": {"reasoning_tokens": 12},
            "cost": 0.0001,
        },
    }


class FakeOpenRouter:
    def __init__(self, key: str, answer: Any = None) -> None:
        self.key = key
        self.answer: Any = answer
        self.seen: list[Seen] = []
        self.script: list[Reply] = []
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    # ---- lifecycle ------------------------------------------------------------------------------
    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/api/v1" % self.server.server_address[1]

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # ---- scripting ------------------------------------------------------------------------------
    def reply(self, *replies: Reply) -> None:
        """Queue replies, used in order;  once the queue is empty the fake answers `answer` again."""
        with self.lock:
            self.script.extend(replies)

    def requests(self) -> list[Seen]:
        with self.lock:
            return list(self.seen)

    # ---- the handler ----------------------------------------------------------------------------
    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # silence
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                headers = {k.lower(): v for k, v in self.headers.items()}
                try:
                    body = json.loads(raw.decode("utf-8"))
                except ValueError:
                    body = None
                with fake.lock:
                    fake.seen.append(Seen(self.path, headers, raw, body))
                    scripted = fake.script.pop(0) if fake.script else None
                if self.path != "/api/v1/chat/completions":
                    self._send(Reply(status=404, body={"error": {"code": 404, "message": "no"}}))
                    return
                if headers.get("authorization") != "Bearer " + fake.key:
                    self._send(Reply(status=401, body={"error": {"code": 401, "message": "no"}}))
                    return
                self._send(scripted or Reply(body=fake.answer))

            def _send(self, reply: Reply) -> None:
                if reply.delay:
                    time.sleep(reply.delay)
                body = reply.body
                if isinstance(body, (dict, list)):
                    data = json.dumps(body).encode("utf-8")
                elif isinstance(body, str):
                    data = body.encode("utf-8")
                else:
                    data = body or b""
                self.send_response(reply.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for name, value in reply.headers.items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    if reply.drip:
                        for i in range(len(data)):
                            self.wfile.write(data[i : i + 1])
                            self.wfile.flush()
                            time.sleep(reply.drip)
                    else:
                        self.wfile.write(data)
                except OSError:
                    pass  # the client gave up (a timeout test)

        return Handler
