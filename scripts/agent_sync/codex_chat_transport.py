"""Exact existing Codex thread queue transport.  Never resumes or starts a thread."""

from __future__ import annotations

import hashlib
import json
import os
import selectors
import socket
import subprocess
import time
import uuid
from collections.abc import Callable
from pathlib import Path

BINARY = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"
MAX_RESPONSE = 2 * 1024 * 1024
DESKTOP_EXECUTABLE = (
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex"
)
# Native thread queues reject thread/queue/add past this many submissions, so preflight
# must count the whole queue rather than a page of it.  A full queue drains on its own.
QUEUE_CAPACITY = 100
QUEUE_PAGE_SIZE = 10
FULL_QUEUE_TTL = 5.0
RECEIPT_TTL = 2.0
CACHE_LIMIT = 64
RECEIPT_CACHE_LIMIT = 8
MAX_CACHED_MESSAGE_HASHES = 1024
_LOADED_CACHE = (0.0, frozenset())


def loaded_rollouts(*, fresh=False):
    """One bounded lsof snapshot proves exact rollout ownership by bundled desktop Codex."""
    global _LOADED_CACHE
    now = time.monotonic()
    if not fresh and now - _LOADED_CACHE[0] < 5:
        return _LOADED_CACHE[1]
    paths = set()
    try:
        result = subprocess.run(
            ["lsof", "-n", "-Fpn", "-c", "codex"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
        if len(result.stdout) > MAX_RESPONSE:
            return frozenset()
        processes = {}
        pid = None
        for line in result.stdout.decode("utf-8", "replace").splitlines():
            if line.startswith("p"):
                pid = line[1:]
                processes.setdefault(pid, set())
            elif line.startswith("n") and pid:
                processes[pid].add(line[1:])
        for names in processes.values():
            if DESKTOP_EXECUTABLE in names:
                paths.update(
                    name
                    for name in names
                    if "/.codex/sessions/" in name and name.endswith(".jsonl")
                )
    except (OSError, subprocess.TimeoutExpired):
        pass
    _LOADED_CACHE = (now, frozenset(paths))
    return _LOADED_CACHE[1]


class InvalidThread(ValueError):
    """The selected thread identity or writable-history scope is permanently invalid."""


class ThreadUnavailable(ValueError):
    """Local history or an existing desktop owner may become available later."""


class RPCError(ValueError):
    """A correlated structured server rejection; never retain or display its message."""

    def __init__(self, error):
        super().__init__("Codex RPC request rejected")
        self.code = error.get("code") if isinstance(error, dict) else None
        self.rejected = (
            isinstance(error, dict)
            and type(self.code) is int
            and isinstance(error.get("message"), str)
        )
        self.queue_full = (
            isinstance(error, dict)
            and type(self.code) is int
            and self.code == -32600
            and error.get("message") == "queue cannot contain more than 100 submissions"
        )


class RPC:
    """Bounded JSON-line app-server client; stderr is discarded, never surfaced."""

    def __init__(self, binary=BINARY, *, env=None):
        self.proc = subprocess.Popen(
            [binary, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            bufsize=0,
        )
        os.set_blocking(self.proc.stdin.fileno(), False)
        self.buffer = b""
        self.serial = 0
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.proc.stdout, selectors.EVENT_READ)
        try:
            self.request(
                "initialize",
                {
                    "clientInfo": {"name": "agent-sync", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self.write({"method": "initialized"})
        except BaseException:
            # An interrupt during the handshake must not leak the spawned app-server.
            self.close()
            raise

    def write(self, row, *, deadline=None):
        data = json.dumps(row, separators=(",", ":")).encode() + b"\n"
        if len(data) > MAX_RESPONSE:
            raise ValueError("RPC input exceeds limit")
        deadline = time.monotonic() + 5 if deadline is None else deadline
        pending = memoryview(data)
        with selectors.DefaultSelector() as writable:
            writable.register(self.proc.stdin, selectors.EVENT_WRITE)
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not writable.select(remaining):
                    raise TimeoutError("Codex RPC write timeout")
                try:
                    written = os.write(self.proc.stdin.fileno(), pending)
                except BlockingIOError:
                    continue
                if written <= 0:
                    raise OSError("Codex RPC stdin closed")
                pending = pending[written:]

    def request(self, method, params, timeout=5):
        self.serial += 1
        expected = self.serial
        deadline = time.monotonic() + timeout
        self.write({"id": expected, "method": method, "params": params}, deadline=deadline)
        consumed = 0
        while time.monotonic() < deadline:
            if b"\n" not in self.buffer:
                if not self.selector.select(max(0, deadline - time.monotonic())):
                    raise TimeoutError("Codex RPC timeout")
                chunk = os.read(self.proc.stdout.fileno(), 65536)
                if not chunk:
                    raise ValueError("Codex RPC closed")
                consumed += len(chunk)
                if consumed > MAX_RESPONSE:
                    raise ValueError("Codex RPC response exceeds limit")
                self.buffer += chunk
                continue
            line, self.buffer = self.buffer.split(b"\n", 1)
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("Malformed Codex RPC response")
            if row.get("id") != expected:
                continue
            if "error" in row:
                raise RPCError(row["error"])
            if not isinstance(row.get("result"), dict):
                raise ValueError("Codex RPC request failed")
            return row["result"]
        raise TimeoutError("Codex RPC timeout")

    def close(self):
        # Cleanup is total: a raise here would escape the caller's finally and replace an
        # acknowledged outcome, so every teardown step is guarded.
        self.selector.close()
        try:
            # Unbuffered stdin has no pending Python buffer to flush while closing.
            self.proc.stdin.close()
            self.proc.wait(timeout=1)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.proc.terminate()
                self.proc.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=1)
                except (OSError, subprocess.TimeoutExpired):
                    pass
        finally:
            try:
                self.proc.stdout.close()
            except OSError:
                pass


def exact_text_input(value, text):
    """Queue records serialize UserInput in snake_case; requests use camelCase."""
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        return False
    item = value[0]
    return (
        item.get("type") == "text"
        and item.get("text") == text
        and set(item) <= {"type", "text", "text_elements", "textElements"}
        and item.get("text_elements", item.get("textElements", [])) == []
        and item.get("textElements", []) == []
    )


def message_id(binding, text):
    return str(
        uuid.UUID(hashlib.sha256((binding["session_id"] + "\0" + text).encode()).hexdigest()[:32])
    )


class CodexChatTransport:
    def __init__(self, *, home=None, binary=BINARY, rpc_factory=None, loaded=None):
        self.home = Path(home or Path.home())
        self.binary = binary
        self.rpc_factory = rpc_factory or (
            lambda: RPC(binary, env={**os.environ, "CODEX_HOME": str(self.home / ".codex")})
        )
        self.loaded = loaded
        self._full_queues = {}
        self._probe_failures = {}
        self._receipts = {}

    def _valid(self, binding):
        if binding.get("platform") != "codex" or binding.get("host") != socket.gethostname():
            return False
        try:
            if str(uuid.UUID(binding.get("session_id", ""))) != binding["session_id"]:
                return False
        except (ValueError, TypeError, AttributeError):
            return False
        cwd = binding.get("cwd")
        # A missing directory is recoverable (an unmounted volume), so only shape is checked here;
        # preflight reports the transient state as busy rather than a permanent hold.
        return isinstance(cwd, str) and os.path.isabs(cwd)

    def _thread(self, rpc, binding):
        result = rpc.request(
            "thread/read", {"threadId": binding["session_id"], "includeTurns": False}
        )
        thread = result.get("thread")
        if (
            not isinstance(thread, dict)
            or thread.get("id") != binding["session_id"]
            or thread.get("cwd") != binding["cwd"]
        ):
            raise InvalidThread("Thread identity mismatch")
        if thread.get("archived") is True or thread.get("readOnly") is True:
            raise InvalidThread("Thread is not writable")
        path = thread.get("path")
        if not isinstance(path, str):
            raise ThreadUnavailable("Missing local rollout")
        file = Path(path)
        base = self.home / ".codex" / "sessions"
        if file.is_symlink() or not file.resolve().is_relative_to(base.resolve()):
            raise InvalidThread("Thread rollout is outside active local history")
        if not file.is_file() or not os.access(file, os.R_OK):
            raise ThreadUnavailable("Thread rollout is not readable yet")
        if not self._is_loaded(thread):
            raise ThreadUnavailable("Thread is not loaded")
        return thread

    def _is_loaded(self, thread, *, fresh=False):
        if self.loaded is not None:
            return self.loaded(thread)
        return thread.get("path") in loaded_rollouts(fresh=fresh)

    @staticmethod
    def _key(binding):
        return tuple(binding.get(k) for k in ("platform", "host", "session_id", "cwd"))

    def _prerequisites(self):
        return os.access(self.binary, os.X_OK) and (self.home / ".codex/queue_1.sqlite").is_file()

    @staticmethod
    def _remember(cache, key, value, *, limit=CACHE_LIMIT):
        now = time.monotonic()
        for old_key, old_value in list(cache.items()):
            expires = old_value[0] if isinstance(old_value, tuple) else old_value
            if expires <= now:
                del cache[old_key]
        if len(cache) >= limit and key not in cache:
            del cache[next(iter(cache))]
        cache[key] = value

    def _full(self, binding):
        return time.monotonic() < self._full_queues.get(self._key(binding), 0)

    def _mark_full(self, binding):
        self._remember(self._full_queues, self._key(binding), time.monotonic() + FULL_QUEUE_TTL)

    @staticmethod
    def _queue(rpc, binding):
        """Small pages, at most one queue capacity; incomplete/cyclic listings fail closed."""
        items, cursor, seen = [], None, set()
        deadline = time.monotonic() + 5.0
        for _ in range(QUEUE_CAPACITY):
            params = {
                "threadId": binding["session_id"],
                "limit": min(QUEUE_PAGE_SIZE, QUEUE_CAPACITY - len(items)),
            }
            if cursor is not None:
                params["cursor"] = cursor
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Codex queue pagination deadline exceeded")
            result = rpc.request("thread/queue/list", params, timeout=remaining)
            page = result.get("data")
            if not isinstance(page, list) or len(page) > params["limit"]:
                raise ValueError("Invalid queue page")
            items.extend(page)
            cursor = result.get("nextCursor")
            if len(items) >= QUEUE_CAPACITY or cursor is None:
                return items
            if not isinstance(cursor, str) or not cursor or cursor in seen or not page:
                raise ValueError("Invalid queue cursor")
            seen.add(cursor)
        raise ValueError("Queue pagination exceeds bounded capacity window")

    def preflight(self, binding):
        if not self._valid(binding):
            return "invalid"
        if not self._prerequisites() or not os.path.isdir(binding["cwd"]):
            return "busy"
        if self._full(binding) or time.monotonic() < self._probe_failures.get(
            self._key(binding), 0
        ):
            return "busy"
        rpc = None
        try:
            rpc = self.rpc_factory()
            self._thread(rpc, binding)
            if len(self._queue(rpc, binding)) >= QUEUE_CAPACITY:
                self._mark_full(binding)
                return "busy"
            return "ready"
        except InvalidThread:
            return "invalid"
        except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
            self._remember(
                self._probe_failures, self._key(binding), time.monotonic() + FULL_QUEUE_TTL
            )
            return "busy"
        finally:
            if rpc:
                rpc.close()

    def _receipt_snapshot(self, binding):
        key = self._key(binding)
        cached = self._receipts.get(key)
        if cached and time.monotonic() < cached[0]:
            return cached[1], cached[2]
        rpc = self.rpc_factory()
        try:
            thread = self._thread(rpc, binding)
            items = set()
            for row in self._queue(rpc, binding):
                if not isinstance(row, dict):
                    continue
                value = row.get("input")
                if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
                    continue
                text = value[0].get("text")
                client_id = row.get("clientUserMessageId")
                if (
                    isinstance(text, str)
                    and exact_text_input(value, text)
                    and isinstance(client_id, str)
                    and len(client_id) <= 128
                ):
                    items.add((client_id, hashlib.sha256(text.encode()).digest()))
            messages = set()
            with open(thread["path"], "rb") as fh:
                fh.seek(0, os.SEEK_END)
                fh.seek(max(0, fh.tell() - MAX_RESPONSE))
                if fh.tell():
                    fh.readline()
                for raw in fh:
                    try:
                        event = json.loads(raw)
                    except (ValueError, UnicodeError):
                        continue
                    if not isinstance(event, dict) or not isinstance(event.get("payload"), dict):
                        continue
                    payload = event["payload"]
                    if event.get("type") == "event_msg" and payload.get("type") == "user_message":
                        if isinstance(payload.get("message"), str):
                            messages.add(hashlib.sha256(payload["message"].encode()).digest())
                    elif (
                        event.get("type") == "response_item"
                        and payload.get("type") == "message"
                        and payload.get("role") == "user"
                    ):
                        content = payload.get("content")
                        if isinstance(content, list):
                            texts = [
                                x.get("text")
                                for x in content
                                if isinstance(x, dict)
                                and x.get("type") == "input_text"
                                and isinstance(x.get("text"), str)
                            ]
                            if texts:
                                messages.add(hashlib.sha256("".join(texts).encode()).digest())
            # Keep only compact evidence hashes.  Oversized histories remain verifiable
            # on this call but never consume the bounded shared receipt cache.
            if len(messages) <= MAX_CACHED_MESSAGE_HASHES:
                self._remember(
                    self._receipts,
                    key,
                    (time.monotonic() + RECEIPT_TTL, items, messages),
                    limit=RECEIPT_CACHE_LIMIT,
                )
            return items, messages
        finally:
            rpc.close()

    def receipt(self, binding, text):
        if not self._valid(binding):
            return False
        try:
            items, messages = self._receipt_snapshot(binding)
            client_id = message_id(binding, text)
            digest = hashlib.sha256(text.encode()).digest()
            return digest in messages or (client_id, digest) in items
        except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
            return False

    def send(self, binding, text, *, permit: Callable[[], bool] | None = None):
        # A malformed or mismatched binding can never succeed, so it is held rather than left on
        # the retryable path: the daemon re-checks policy gates but not the physical probe here,
        # so a deferred verdict would requeue this wake forever.
        if not self._valid(binding):
            return "held"
        # Repeat the workspace availability check preflight made: it can disappear between the
        # two calls, and send() is reachable directly.  Deferred, not held, so the wake retries.
        if not os.path.isdir(binding["cwd"]) or not self._prerequisites() or self._full(binding):
            return "deferred"
        rpc = None
        sent = False
        try:
            rpc = self.rpc_factory()
            thread = self._thread(rpc, binding)
            # The native add atomically rejects full queues; avoid repeating preflight's listing.
            if permit is not None and not permit():
                return "deferred"
            # Positive discovery proof may already be stale.  Recheck ownership after the policy
            # permit and immediately before add, without reusing the discovery snapshot.
            if not self._is_loaded(thread, fresh=True):
                return "deferred"
            sent = True
            result = rpc.request(
                "thread/queue/add",
                {
                    "threadId": binding["session_id"],
                    "input": [{"type": "text", "text": text, "textElements": []}],
                    "clientUserMessageId": message_id(binding, text),
                },
            )
            # Exact correlated queue record acknowledges durable enqueue, not chat pickup.
            queued = result.get("queuedSubmission")
            if (
                not isinstance(queued, dict)
                or not isinstance(queued.get("id"), str)
                or not queued["id"]
                or queued.get("clientUserMessageId") != message_id(binding, text)
            ):
                return "unknown"
            self._receipts.pop(self._key(binding), None)
            return "accepted"
        except RPCError as exc:
            if sent and exc.queue_full:
                self._mark_full(binding)
                return "deferred"
            # A correlated well-formed rejection is held for explicit retry, not left
            # uncertain where it would block every other wake to this destination.
            return "failed" if sent and exc.rejected else "unknown" if sent else "deferred"
        except InvalidThread:
            # Identity, archive, read-only and history-scope failures are permanent, matching the
            # terminal verdict preflight gives them; deferring would retry this wake forever.
            return "held"
        except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
            # Nothing reached thread/queue/add, so the wake is still safe to retry: only a
            # failure after the add is uncertain.
            return "unknown" if sent else "deferred"
        finally:
            if rpc:
                rpc.close()
