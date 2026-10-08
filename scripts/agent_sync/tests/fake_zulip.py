"""A small fake Zulip server for the agent_sync tests.

ThreadingHTTPServer on 127.0.0.1 with a random port, run in a thread.  It implements only the
endpoints agent-sync uses, checks Basic auth, records every request, and can inject faults
(status codes, delays, dropped connections).  Event queues behave like Zulip's: a queue is
created by POST /register, GET /events long-polls (with a short heartbeat so tests stay fast),
events up to last_event_id are acknowledged, and an unknown queue answers BAD_EVENT_QUEUE_ID.
The narrow given to /register must be pair-shaped, as on the live server.
"""
from __future__ import annotations

import base64
import copy
import json
import re
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

BOT_EMAIL = "claude-bot@zulip.test"
EPOCH = 1_790_000_000  # a fixed base for message timestamps


@dataclass
class Recorded:
    method: str
    path: str
    query: dict[str, str]
    form: dict[str, str]
    headers: dict[str, str]

    @property
    def params(self) -> dict[str, str]:
        return {**self.query, **self.form}


@dataclass
class Queue:
    queue_id: str
    narrow: list[list[str]] | None
    events: list[dict[str, Any]] = field(default_factory=list)
    next_event_id: int = 0


def user(user_id: int, email: str, full_name: str, is_bot: bool) -> dict[str, Any]:
    return {"user_id": user_id, "email": email, "delivery_email": email, "full_name": full_name,
            "is_bot": is_bot, "is_active": True}


class FakeZulip:
    def __init__(self, key: str, *, heartbeat: float = 0.3) -> None:
        self.key = key
        self.heartbeat = heartbeat
        self.cond = threading.Condition(threading.RLock())
        self.requests: list[Recorded] = []
        self.me = user(10, BOT_EMAIL, "Claude", True)
        self.users = [
            self.me,
            user(11, "codex-bot@zulip.test", "Codex", True),
            user(12, "jay@zulip.test", "Jay Wedgeworth", False),
            user(13, "cursor-bot@zulip.test", "Cursor", True),
            user(14, "grok-bot@zulip.test", "Grok", True),
            user(15, "minimax-bot@zulip.test", "MiniMax", True),
        ]
        self.streams = {"agent-sync": 7, "other": 8}
        self.subscribed = {"agent-sync", "other"}
        self.user_groups: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []
        self.next_id = 100
        self.queues: dict[str, Queue] = {}
        self.queue_counter = 0
        self.registrations: list[dict[str, Any]] = []
        self.deleted_queues: list[str] = []
        self.faults: list[dict[str, Any]] = []
        self.echo_auth = False
        handler = self._make_handler()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.server.handle_error = lambda request, client_address: None  # a client that timed out is not an error here
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    # ---- lifecycle ----------------------------------------------------------------------
    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    @property
    def auth_header(self) -> str:
        return "Basic " + base64.b64encode(("%s:%s" % (BOT_EMAIL, self.key)).encode()).decode()

    # ---- test-facing helpers --------------------------------------------------------------
    def user_named(self, full_name: str) -> dict[str, Any]:
        return next(u for u in self.users if u["full_name"] == full_name)

    def add_message(self, sender: str | dict[str, Any], channel: str, topic: str, content: str, *,
                    timestamp: int | None = None, deliver: bool = True) -> int:
        """Store a message and (unless deliver is False) push it into the matching event queues."""
        with self.cond:
            who = sender if isinstance(sender, dict) else self.user_named(sender)
            message_id = self.next_id
            self.next_id += 1
            message = {
                "id": message_id, "type": "stream", "display_recipient": channel, "subject": topic,
                "content": content, "sender_id": who["user_id"], "sender_email": who["email"],
                "sender_full_name": who["full_name"], "timestamp": timestamp if timestamp is not None else EPOCH + message_id,
            }
            self.messages.append(message)
            if deliver:
                for queue in self.queues.values():
                    if self._narrow_matches(queue.narrow, message):
                        flags = self._flags(message)
                        queue.events.append({"type": "message", "id": queue.next_event_id,
                                             "message": copy.deepcopy(message), "flags": flags})
                        queue.next_event_id += 1
                self.cond.notify_all()
            return message_id

    def add_direct_message(self, sender: str, content: str) -> int:
        with self.cond:
            who = self.user_named(sender)
            message_id = self.next_id
            self.next_id += 1
            self.messages.append({"id": message_id, "type": "private", "display_recipient": [who, self.me],
                                  "subject": "", "content": content, "sender_id": who["user_id"],
                                  "sender_email": who["email"], "sender_full_name": who["full_name"],
                                  "timestamp": EPOCH + message_id})
            return message_id

    def expire_queues(self) -> None:
        with self.cond:
            self.queues.clear()
            self.cond.notify_all()

    def inject(self, method: str, path: str, *, status: int | None = None, body: dict[str, Any] | None = None,
               headers: dict[str, str] | None = None, delay: float = 0.0, drop: bool = False, times: int = 1) -> None:
        self.faults.append({"method": method, "path": path, "status": status, "body": body, "headers": headers or {},
                            "delay": delay, "drop": drop, "times": times})

    def when(self, predicate: Callable[[], Any], action: Callable[[], None], timeout: float = 15.0) -> threading.Thread:
        """Run `action()` on a helper thread as soon as `predicate()` is true.  Tests use this to
        make something happen at a known point of the client's run (its queue exists, its long poll
        is open) instead of after a guessed number of milliseconds, which a busy machine breaks.
        `predicate` is evaluated with the server lock held; if it never holds, nothing runs."""
        def run() -> None:
            with self.cond:
                reached = self.cond.wait_for(predicate, timeout=timeout)
            if reached:
                action()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread

    def polling(self, count: int = 1) -> Callable[[], bool]:
        """A predicate for `when`: the client has opened its long poll at least `count` times."""
        return lambda: len(self.requests_to("GET", "events")) >= count

    def requests_to(self, method: str, path: str) -> list[Recorded]:
        return [r for r in self.requests if r.method == method and r.path == path]

    def request_log(self) -> list[tuple[str, str]]:
        return [(r.method, r.path) for r in self.requests]

    # ---- internals ---------------------------------------------------------------------------
    def _flags(self, message: dict[str, Any]) -> list[str]:
        flags: list[str] = []
        if message["sender_email"] == BOT_EMAIL:
            flags.append("read")
        if "@**%s**" % self.me["full_name"] in message["content"]:
            flags.append("mentioned")
        return flags

    def _narrow_matches(self, narrow: list[list[str]] | None, message: dict[str, Any]) -> bool:
        for operator, operand in narrow or []:
            if operator == "channel" and str(message["display_recipient"]).casefold() != operand.casefold():
                return False
            if operator == "topic" and str(message["subject"]).casefold() != operand.casefold():
                return False
            if operator == "is" and operand == "mentioned" and "mentioned" not in self._flags(message):
                return False
        return True

    def _dict_narrow_matches(self, narrow: list[dict[str, str]], message: dict[str, Any]) -> bool:
        pairs = [[item["operator"], item["operand"]] for item in narrow]
        return self._narrow_matches(pairs, message)

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:  # silence
                return

            def _send(self, status: int, body: dict[str, Any], headers: dict[str, str] | None = None) -> None:
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Connection", "close")
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(raw)

            def _handle(self, method: str) -> None:
                parts = urlsplit(self.path)
                path = parts.path[len("/api/v1/"):] if parts.path.startswith("/api/v1/") else parts.path
                query = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
                length = int(self.headers.get("Content-Length") or 0)
                form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True).items()} if length else {}
                with fake.cond:
                    fake.requests.append(Recorded(method, path, query, form, {k: v for k, v in self.headers.items()}))
                    fake.cond.notify_all()  # wakes `when` watchers
                    fault = next((f for f in fake.faults if f["times"] > 0 and f["method"] == method
                                  and f["path"] == path), None)
                    if fault is not None:
                        fault["times"] -= 1
                if fault is not None:
                    if fault["delay"]:
                        time.sleep(fault["delay"])
                    if fault["drop"]:
                        self.close_connection = True
                        return
                    if fault["status"] is not None:
                        self._send(fault["status"], fault["body"] or {}, fault["headers"])
                        return
                if self.headers.get("Authorization") != fake.auth_header:
                    msg = "Invalid API key"
                    if fake.echo_auth:  # echo what the CLIENT sent, as a careless server might
                        received = self.headers.get("Authorization") or ""
                        try:
                            sent_key = base64.b64decode(received.split(" ", 1)[1]).decode().split(":", 1)[1]
                        except (IndexError, ValueError):
                            sent_key = ""
                        msg += " (got %s; key %s)" % (received, sent_key)
                    self._send(401, {"result": "error", "msg": msg, "code": "UNAUTHORIZED"})
                    return
                params = {**query, **form}
                try:
                    status, body = fake._route(method, path, params)
                except _Reject as exc:
                    status, body = exc.status, {"result": "error", "msg": exc.msg, "code": exc.code}
                if fake.echo_auth and status >= 400:  # the client's own (valid) header and key, echoed back
                    body["msg"] = "%s [auth %s key %s]" % (body.get("msg"), self.headers.get("Authorization"), fake.key)
                self._send(status, body)

            def do_GET(self) -> None:
                self._handle("GET")

            def do_POST(self) -> None:
                self._handle("POST")

            def do_PATCH(self) -> None:
                self._handle("PATCH")

            def do_DELETE(self) -> None:
                self._handle("DELETE")

        return Handler

    # ---- routing -------------------------------------------------------------------------------
    def _route(self, method: str, path: str, params: dict[str, str]) -> tuple[int, dict[str, Any]]:
        ok: dict[str, Any] = {"result": "success", "msg": ""}
        key = (method, path)
        if key == ("GET", "users/me"):
            return 200, {**ok, **self.me}
        if key == ("GET", "users"):
            return 200, {**ok, "members": copy.deepcopy(self.users)}
        if key == ("GET", "users/me/subscriptions"):
            return 200, {**ok, "subscriptions": [
                {"name": n, "stream_id": self.streams[n], "description": "the %s channel" % n} for n in sorted(self.subscribed)]}
        if key == ("POST", "users/me/subscriptions"):
            wanted = [item["name"] for item in json.loads(params["subscriptions"])]
            new = [n for n in wanted if n not in self.subscribed]
            old = [n for n in wanted if n in self.subscribed]
            for name in new:
                self.streams.setdefault(name, 100 + len(self.streams))
                self.subscribed.add(name)
            return 200, {**ok, "subscribed": {BOT_EMAIL: new} if new else {},
                         "already_subscribed": {BOT_EMAIL: old} if old else {}}
        if key == ("GET", "get_stream_id"):
            name = params.get("stream", "")
            if name not in self.streams or (name not in self.subscribed and name.startswith("private")):
                raise _Reject(400, "Invalid channel name '%s'" % name, "BAD_REQUEST")
            return 200, {**ok, "stream_id": self.streams[name]}
        match = re.fullmatch(r"users/me/(\d+)/topics", path)
        if method == "GET" and match:
            stream_id = int(match.group(1))
            channel = next((n for n, i in self.streams.items() if i == stream_id), None)
            latest: dict[str, int] = {}
            for message in self.messages:
                if message["display_recipient"] == channel:
                    latest[message["subject"]] = max(latest.get(message["subject"], 0), message["id"])
            return 200, {**ok, "topics": [{"name": t, "max_id": i} for t, i in sorted(latest.items(), key=lambda x: -x[1])]}
        if key == ("POST", "messages"):
            if params.get("type") != "stream":
                raise _Reject(400, "only stream messages here", "BAD_REQUEST")
            if not params.get("topic"):
                raise _Reject(400, "Topic can't be empty", "BAD_REQUEST")
            return 200, {**ok, "id": self.add_message(self.me, params["to"], params["topic"], params["content"])}
        if key == ("GET", "messages"):
            return 200, {**ok, **self._get_messages(params)}
        match = re.fullmatch(r"messages/(\d+)", path)
        if match and method == "GET":
            found = self._find(int(match.group(1)))
            return 200, {**ok, "message": self._public(found)}
        if match and method == "PATCH":
            found = self._find(int(match.group(1)))
            if params.get("propagate_mode") == "change_all" and "topic" in params:
                for message in self.messages:
                    if message["display_recipient"] == found["display_recipient"] and message["subject"] == found["subject"]:
                        message["subject"] = params["topic"]
            return 200, ok
        match = re.fullmatch(r"messages/(\d+)/reactions", path)
        if match and method == "POST":
            self._find(int(match.group(1)))
            return 200, ok
        if key == ("GET", "user_groups"):
            return 200, {**ok, "user_groups": self.user_groups}
        if key == ("POST", "user_topics"):
            return 200, ok
        if key == ("POST", "register"):
            return 200, {**ok, **self._register(params)}
        if key == ("GET", "events"):
            return self._events(params)
        if key == ("DELETE", "events"):
            with self.cond:
                queue_id = params.get("queue_id", "")
                if self.queues.pop(queue_id, None) is None:
                    raise _Reject(400, "Bad event queue ID: %s" % queue_id, "BAD_EVENT_QUEUE_ID")
                self.deleted_queues.append(queue_id)
                self.cond.notify_all()
            return 200, ok
        raise _Reject(404, "no such endpoint %s %s" % key, "BAD_REQUEST")

    def _find(self, message_id: int) -> dict[str, Any]:
        for message in self.messages:
            if message["id"] == message_id:
                return message
        raise _Reject(400, "Invalid message(s)", "BAD_REQUEST")

    def _public(self, message: dict[str, Any]) -> dict[str, Any]:
        shown = copy.deepcopy(message)
        shown["flags"] = self._flags(message)
        return shown

    def _get_messages(self, params: dict[str, str]) -> dict[str, Any]:
        narrow = json.loads(params.get("narrow") or "[]")
        matching = sorted((m for m in self.messages if m["type"] == "stream" and self._dict_narrow_matches(narrow, m)),
                          key=lambda m: m["id"])
        before = int(params.get("num_before", 0))
        after = int(params.get("num_after", 0))
        include = params.get("include_anchor", "true") == "true"
        anchor = params["anchor"]
        if anchor == "newest":
            chosen = matching[-before:] if before else []
        else:
            anchor_id = int(anchor)
            lower = [m for m in matching if m["id"] < anchor_id or (include and m["id"] == anchor_id)]
            upper = [m for m in matching if m["id"] > anchor_id]
            chosen = (lower[-before:] if before else []) + upper[:after]
        return {"messages": [self._public(m) for m in chosen], "found_newest": True, "found_anchor": True}

    def _register(self, params: dict[str, str]) -> dict[str, Any]:
        narrow = json.loads(params["narrow"]) if "narrow" in params else None
        if narrow is not None and not all(isinstance(item, list) and len(item) == 2 for item in narrow):
            raise _Reject(400, "Invalid narrow: expected a list of pairs", "BAD_REQUEST")
        if json.loads(params.get("event_types", "[]")) != ["message"]:
            raise _Reject(400, "unexpected event_types", "BAD_REQUEST")
        with self.cond:
            self.queue_counter += 1
            queue = Queue("q%d" % self.queue_counter, narrow)
            self.queues[queue.queue_id] = queue
            self.registrations.append({"queue_id": queue.queue_id, "narrow": narrow, "params": dict(params)})
            self.cond.notify_all()
            return {"queue_id": queue.queue_id, "last_event_id": -1}

    def _events(self, params: dict[str, str]) -> tuple[int, dict[str, Any]]:
        queue_id = params.get("queue_id", "")
        last = int(params.get("last_event_id", -1))
        deadline = time.monotonic() + self.heartbeat
        with self.cond:
            while True:
                queue = self.queues.get(queue_id)
                if queue is None:
                    raise _Reject(400, "Bad event queue ID: %s" % queue_id, "BAD_EVENT_QUEUE_ID")
                queue.events = [e for e in queue.events if e["id"] > last]
                if queue.events:
                    return 200, {"result": "success", "msg": "", "events": copy.deepcopy(queue.events), "queue_id": queue_id}
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    beat = {"type": "heartbeat", "id": queue.next_event_id}
                    queue.next_event_id += 1
                    return 200, {"result": "success", "msg": "", "events": [beat], "queue_id": queue_id}
                self.cond.wait(timeout=remaining)


class _Reject(Exception):
    def __init__(self, status: int, msg: str, code: str) -> None:
        super().__init__(msg)
        self.status = status
        self.msg = msg
        self.code = code
