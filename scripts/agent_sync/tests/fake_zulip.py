"""A small fake Zulip server for the agent_sync tests.

ThreadingHTTPServer on 127.0.0.1 with a random port, run in a thread.  It implements only the
endpoints agent-sync uses, checks Basic auth, records every request, and can inject faults
(status codes, delays, dropped connections).  Event queues behave like Zulip's: a queue is
created by POST /register, GET /events long-polls (with a short heartbeat so tests stay fast),
events up to last_event_id are acknowledged, and an unknown queue answers BAD_EVENT_QUEUE_ID.
The narrow given to /register must be pair-shaped, as on the live server.

Several bots can hold keys at once (`add_bot`), each with its own queues, `mentioned` flags,
followed and muted topics.  A queue serves the event types it registered for:  `message`,
`update_message` (topic renames), `user_topic` (follow and mute), `realm_user` (user changes)
and heartbeats.  The `mentioned` flag follows the server's rules closely enough for routing
tests:  a mention inside a code span, a code block or a quote does not set it, a silent mention
(`@_**Name**`) does not set it, a group mention sets it for group members, and wildcard
mentions set the wildcard flags instead.
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
LISTENER_EVENT_TYPES = {"message", "update_message", "user_topic", "realm_user"}
HUMAN_CLIENT = "website"
BOT_CLIENT = "ZulipPython"
WILDCARDS_CHANNEL = ("all", "everyone", "channel", "stream")


@dataclass
class Recorded:
    method: str
    path: str
    query: dict[str, str]
    form: dict[str, str]
    headers: dict[str, str]
    user_id: int | None = None

    @property
    def params(self) -> dict[str, str]:
        return {**self.query, **self.form}


@dataclass
class Queue:
    queue_id: str
    narrow: list[list[str]] | None
    events: list[dict[str, Any]] = field(default_factory=list)
    next_event_id: int = 0
    user_id: int = 10
    event_types: tuple[str, ...] = ("message",)


FAKE_OWNER_ID = 12  # the realm owner (Jay); every fake bot is his, like the real fleet's


def user(user_id: int, email: str, full_name: str, is_bot: bool, role: int | None = None) -> dict[str, Any]:
    return {"user_id": user_id, "email": email, "delivery_email": email, "full_name": full_name,
            "is_bot": is_bot, "is_active": True, "role": role if role is not None else 400,
            "is_admin": role in (100, 200), "is_owner": role == 100,
            **({"bot_type": 1, "bot_owner_id": FAKE_OWNER_ID} if is_bot else {})}


_CODE_BLOCK_RE = re.compile(r"(?ms)^(```|~~~).*?(^\1\s*$|\Z)")
_CODE_SPAN_RE = re.compile(r"`[^`\n]*`")
_QUOTE_LINE_RE = re.compile(r"(?m)^\s*>.*$")


def visible_markup(content: str) -> str:
    """The content with code blocks, code spans and quoted lines removed: what the server looks at
    when it decides whether a mention notifies."""
    text = _CODE_BLOCK_RE.sub("", content)
    text = _CODE_SPAN_RE.sub("", text)
    return _QUOTE_LINE_RE.sub("", text)


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
            user(12, "jay@zulip.test", "Jay Wedgeworth", False, role=100),
            user(13, "cursor-bot@zulip.test", "Cursor", True),
            user(14, "grok-bot@zulip.test", "Grok", True),
            user(15, "minimax-bot@zulip.test", "MiniMax", True),
        ]
        self.keys: dict[str, dict[str, Any]] = {key: self.me}  # API key -> the bot it belongs to
        self.streams = {"agent-sync": 7, "other": 8, "sandbox": 9}
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
        self.longpoll_timeout = 90
        self.topic_policies: dict[int, dict[tuple[int, str], int]] = {}  # user id -> {(stream id, topic folded): policy}
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
        return self.auth_header_for(self.key)

    def auth_header_for(self, key: str) -> str:
        bot = self.keys[key]
        return "Basic " + base64.b64encode(("%s:%s" % (bot["email"], key)).encode()).decode()

    # ---- test-facing helpers --------------------------------------------------------------
    def user_named(self, full_name: str) -> dict[str, Any]:
        return next(u for u in self.users if u["full_name"] == full_name)

    def user_by_id(self, user_id: int) -> dict[str, Any]:
        return next(u for u in self.users if u["user_id"] == user_id)

    def add_user(self, email: str, full_name: str, *, is_bot: bool, role: int = 400,
                 user_id: int | None = None) -> dict[str, Any]:
        """Add a user and tell every realm_user queue about it."""
        with self.cond:
            new = user(user_id or (max(u["user_id"] for u in self.users) + 1), email, full_name, is_bot, role)
            self.users.append(new)
            self._push(lambda q: "realm_user" in q.event_types,
                       lambda q: {"type": "realm_user", "op": "add", "person": copy.deepcopy(new)})
            return new

    def add_bot(self, email: str, full_name: str, key: str, *, role: int = 400,
                user_id: int | None = None) -> dict[str, Any]:
        """Give a bot an API key, so tests can run several seats against one server."""
        with self.cond:
            existing = next((u for u in self.users if u["email"] == email), None)
            bot = existing or self.add_user(email, full_name, is_bot=True, role=role, user_id=user_id)
            bot["role"] = role
            bot["is_admin"] = role in (100, 200)
            bot["is_owner"] = role == 100
            self.keys[key] = bot
            return bot

    def set_role(self, user_id: int, role: int) -> None:
        """Change a user's role and tell every realm_user queue, the way Zulip does (op update with
        only the changed field)."""
        with self.cond:
            person = self.user_by_id(user_id)
            person["role"] = role
            person["is_admin"] = role in (100, 200)
            person["is_owner"] = role == 100
            self._push(lambda q: "realm_user" in q.event_types,
                       lambda q: {"type": "realm_user", "op": "update", "person": {"user_id": user_id, "role": role}})

    def add_message(self, sender: str | dict[str, Any], channel: str, topic: str, content: str, *,
                    timestamp: int | None = None, deliver: bool = True, client: str | None = None) -> int:
        """Store a message and (unless deliver is False) push it into the matching event queues."""
        with self.cond:
            who = sender if isinstance(sender, dict) else self.user_named(sender)
            message_id = self.next_id
            self.next_id += 1
            message = {
                "id": message_id, "type": "stream", "display_recipient": channel, "subject": topic,
                "stream_id": self.streams.setdefault(channel, 100 + len(self.streams)),
                "content": content, "sender_id": who["user_id"], "sender_email": who["email"],
                "sender_full_name": who["full_name"], "timestamp": timestamp if timestamp is not None else EPOCH + message_id,
                "client": client or (BOT_CLIENT if who.get("is_bot") else HUMAN_CLIENT),
            }
            self.messages.append(message)
            if deliver:
                self._deliver(message)
            return message_id

    def add_direct_message(self, sender: str | dict[str, Any], content: str, *,
                           recipients: list[dict[str, Any]] | None = None, deliver: bool = False,
                           client: str | None = None, timestamp: int | None = None) -> int:
        """Store a DM from `sender` to `recipients` (default: the main bot).  Delivered to the
        recipients' queues only when `deliver` is True (the CLI tests never expect DM events)."""
        with self.cond:
            who = sender if isinstance(sender, dict) else self.user_named(sender)
            members = [who] + [r for r in (recipients or [self.me]) if r["user_id"] != who["user_id"]]
            message_id = self.next_id
            self.next_id += 1
            message = {"id": message_id, "type": "private",
                       "display_recipient": [{"id": m["user_id"], "email": m["email"], "full_name": m["full_name"]}
                                             for m in sorted(members, key=lambda m: m["user_id"])],
                       "subject": "", "content": content, "sender_id": who["user_id"],
                       "sender_email": who["email"], "sender_full_name": who["full_name"],
                       "timestamp": timestamp if timestamp is not None else EPOCH + message_id,
                       "client": client or (BOT_CLIENT if who.get("is_bot") else HUMAN_CLIENT)}
            self.messages.append(message)
            if deliver:
                self._deliver(message)
            return message_id

    def rename_topic(self, channel: str, old: str, new: str) -> list[int]:
        """Rename a whole topic (propagate_mode change_all) and push update_message events."""
        with self.cond:
            moved = [m for m in self.messages if m["type"] == "stream" and m["display_recipient"] == channel
                     and m["subject"].casefold() == old.casefold()]
            for message in moved:
                message["subject"] = new
            if moved:
                ids = [m["id"] for m in moved]
                self._push(lambda q: "update_message" in q.event_types, lambda q: {
                    "type": "update_message", "message_id": ids[-1], "message_ids": ids,
                    "stream_id": self.streams[channel], "orig_subject": old, "subject": new,
                    "propagate_mode": "change_all", "rendering_only": False, "user_id": 12,
                    "edit_timestamp": int(time.time())})
            return [m["id"] for m in moved]

    def edit_content(self, message_id: int, content: str) -> None:
        """A content-only edit: an update_message event with no topic change."""
        with self.cond:
            found = self._find(message_id)
            found["content"] = content
            self._push(lambda q: "update_message" in q.event_types, lambda q: {
                "type": "update_message", "message_id": message_id, "message_ids": [message_id],
                "content": content, "rendering_only": False, "user_id": found["sender_id"]})

    def set_topic_policy(self, bot: dict[str, Any], channel: str, topic: str, policy: int) -> None:
        with self.cond:
            stream_id = self.streams[channel]
            self.topic_policies.setdefault(bot["user_id"], {})[(stream_id, topic.casefold())] = policy
            self._push(lambda q: q.user_id == bot["user_id"] and "user_topic" in q.event_types,
                       lambda q: {"type": "user_topic", "stream_id": stream_id, "topic_name": topic,
                                  "last_updated": int(time.time()), "visibility_policy": policy})

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
    def _push(self, wanted: Callable[[Queue], bool], make: Callable[[Queue], dict[str, Any]]) -> None:
        with self.cond:
            for queue in self.queues.values():
                if wanted(queue):
                    event = make(queue)
                    event["id"] = queue.next_event_id
                    queue.events.append(event)
                    queue.next_event_id += 1
            self.cond.notify_all()

    def _receives(self, user_id: int, message: dict[str, Any]) -> bool:
        if message["type"] == "private":
            return any(r["id"] == user_id for r in message["display_recipient"])
        return True  # every test bot is subscribed to every channel

    def _deliver(self, message: dict[str, Any]) -> None:
        for queue in self.queues.values():
            if "message" not in queue.event_types or not self._receives(queue.user_id, message):
                continue
            if self._narrow_matches(queue.narrow, message, queue.user_id):
                queue.events.append({"type": "message", "id": queue.next_event_id,
                                     "message": copy.deepcopy(message), "flags": self._flags(message, queue.user_id)})
                queue.next_event_id += 1
        self.cond.notify_all()

    def _flags(self, message: dict[str, Any], user_id: int | None = None) -> list[str]:
        who = self.user_by_id(user_id) if user_id is not None else self.me
        flags: list[str] = []
        if message["sender_id"] == who["user_id"]:
            flags.append("read")
        text = visible_markup(message["content"])
        name = re.escape(who["full_name"])
        if re.search(r"@\*\*%s(\|%d)?\*\*" % (name, who["user_id"]), text):
            flags.append("mentioned")
        for group in self.user_groups:
            if who["user_id"] in group.get("members", []) and "@*%s*" % group["name"] in text \
                    and "mentioned" not in flags:
                flags.append("mentioned")
        if any("@**%s**" % w in text for w in WILDCARDS_CHANNEL):
            flags.extend(["stream_wildcard_mentioned", "wildcard_mentioned"])
        if "@**topic**" in text:
            flags.append("topic_wildcard_mentioned")
        return flags

    def _narrow_matches(self, narrow: list[list[Any]] | None, message: dict[str, Any], user_id: int | None = None) -> bool:
        for operator, operand in narrow or []:
            if operator in ("channel", "stream") and (message["type"] != "stream" or
                                                      str(message["display_recipient"]).casefold() != str(operand).casefold()):
                return False
            if operator == "topic" and str(message["subject"]).casefold() != str(operand).casefold():
                return False
            if operator == "is" and operand == "mentioned":
                flags = self._flags(message, user_id)
                if not any(f in flags for f in ("mentioned", "wildcard_mentioned", "topic_wildcard_mentioned")):
                    return False
            if operator == "is" and operand in ("dm", "private") and message["type"] != "private":
                return False
            if operator == "sender" and str(message["sender_id"]) != str(operand) and message["sender_email"] != operand:
                return False
            if operator == "dm":
                ids = set(operand if isinstance(operand, list) else [operand])
                members = {r["id"] for r in message["display_recipient"]} if message["type"] == "private" else set()
                # Zulip matches the conversation with exactly these other people (the caller is always in it).
                if message["type"] != "private" or (members - {user_id}) != (ids - {user_id}):
                    return False
        return True

    def _dict_narrow_matches(self, narrow: list[dict[str, Any]], message: dict[str, Any], user_id: int | None) -> bool:
        pairs = [[item["operator"], item["operand"]] for item in narrow]
        return self._narrow_matches(pairs, message, user_id)

    def _user_for(self, header: str | None) -> dict[str, Any] | None:
        for key, bot in self.keys.items():
            if header == "Basic " + base64.b64encode(("%s:%s" % (bot["email"], key)).encode()).decode():
                return bot
        return None

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
                caller = fake._user_for(self.headers.get("Authorization"))
                with fake.cond:
                    fake.requests.append(Recorded(method, path, query, form, {k: v for k, v in self.headers.items()},
                                                  caller["user_id"] if caller else None))
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
                if caller is None:
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
                    status, body = fake._route(method, path, params, caller)
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
    def _route(self, method: str, path: str, params: dict[str, str], caller: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        ok: dict[str, Any] = {"result": "success", "msg": ""}
        key = (method, path)
        if key == ("GET", "users/me"):
            return 200, {**ok, **caller}
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
            return 200, {**ok, "subscribed": {caller["email"]: new} if new else {},
                         "already_subscribed": {caller["email"]: old} if old else {}}
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
            kind = params.get("type")
            if kind in ("direct", "private"):
                ids = json.loads(params["to"])
                recipients = [self.user_by_id(int(i)) for i in ids]
                return 200, {**ok, "id": self.add_direct_message(caller, params["content"], recipients=recipients,
                                                                 deliver=True, client=BOT_CLIENT)}
            if kind != "stream":
                raise _Reject(400, "only stream messages here", "BAD_REQUEST")
            if not params.get("topic"):
                raise _Reject(400, "Topic can't be empty", "BAD_REQUEST")
            return 200, {**ok, "id": self.add_message(caller, params["to"], params["topic"], params["content"])}
        if key == ("GET", "messages"):
            return 200, {**ok, **self._get_messages(params, caller["user_id"])}
        match = re.fullmatch(r"messages/(\d+)", path)
        if match and method == "GET":
            found = self._find(int(match.group(1)))
            return 200, {**ok, "message": self._public(found, caller["user_id"])}
        if match and method == "PATCH":
            found = self._find(int(match.group(1)))
            if params.get("propagate_mode") == "change_all" and "topic" in params:
                self.rename_topic(found["display_recipient"], found["subject"], params["topic"])
            return 200, ok
        match = re.fullmatch(r"messages/(\d+)/reactions", path)
        if match and method == "POST":
            self._find(int(match.group(1)))
            return 200, ok
        if key == ("GET", "user_groups"):
            return 200, {**ok, "user_groups": self.user_groups}
        if key == ("POST", "user_topics"):
            stream_id = int(params.get("stream_id", 0))
            channel = next((n for n, i in self.streams.items() if i == stream_id), "")
            self.set_topic_policy(caller, channel, params.get("topic", ""), int(params.get("visibility_policy", 0)))
            return 200, ok
        if key == ("POST", "register"):
            return 200, {**ok, **self._register(params, caller)}
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

    def _public(self, message: dict[str, Any], user_id: int | None = None) -> dict[str, Any]:
        shown = copy.deepcopy(message)
        shown["flags"] = self._flags(message, user_id)
        return shown

    def _get_messages(self, params: dict[str, str], user_id: int) -> dict[str, Any]:
        narrow = json.loads(params.get("narrow") or "[]")
        # As on the live server, is:mentioned also finds DMs that mention the caller.
        wants_dm = any(item["operator"] == "dm" or (item["operator"] == "is" and item["operand"] in ("dm", "private", "mentioned"))
                       for item in narrow)
        matching = sorted((m for m in self.messages
                           if (m["type"] == "stream" or (wants_dm and self._receives(user_id, m)))
                           and self._dict_narrow_matches(narrow, m, user_id)),
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
        return {"messages": [self._public(m, user_id) for m in chosen], "found_newest": True, "found_anchor": True}

    def _register(self, params: dict[str, str], caller: dict[str, Any]) -> dict[str, Any]:
        narrow = json.loads(params["narrow"]) if "narrow" in params else None
        if narrow is not None and not all(isinstance(item, list) and len(item) == 2 for item in narrow):
            raise _Reject(400, "Invalid narrow: expected a list of pairs", "BAD_REQUEST")
        event_types = json.loads(params.get("event_types", "[]"))
        if not event_types or not set(event_types) <= LISTENER_EVENT_TYPES:
            raise _Reject(400, "unexpected event_types", "BAD_REQUEST")
        with self.cond:
            self.queue_counter += 1
            queue = Queue("q%d" % self.queue_counter, narrow, user_id=caller["user_id"], event_types=tuple(event_types))
            self.queues[queue.queue_id] = queue
            self.registrations.append({"queue_id": queue.queue_id, "narrow": narrow, "params": dict(params),
                                       "user_id": caller["user_id"]})
            result: dict[str, Any] = {"queue_id": queue.queue_id, "last_event_id": -1,
                                      "event_queue_longpoll_timeout_seconds": self.longpoll_timeout}
            if "message" in event_types:
                result["max_message_id"] = max((m["id"] for m in self.messages), default=-1)
            if "realm_user" in event_types:
                result["realm_users"] = copy.deepcopy(self.users)
            if "user_topic" in event_types:
                streams = {i: n for n, i in self.streams.items()}
                result["user_topics"] = [
                    {"stream_id": sid, "topic_name": topic, "visibility_policy": policy, "last_updated": 0}
                    for (sid, topic), policy in self.topic_policies.get(caller["user_id"], {}).items() if sid in streams]
            self.cond.notify_all()
            return result

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
