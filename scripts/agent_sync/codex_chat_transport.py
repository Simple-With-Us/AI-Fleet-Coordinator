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
_LOADED_CACHE = (0.0, frozenset())


def loaded_rollouts():
    """One bounded lsof snapshot proves exact rollout ownership by bundled desktop Codex."""
    global _LOADED_CACHE
    now = time.monotonic()
    if now - _LOADED_CACHE[0] < 5:
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
            if "error" in row or not isinstance(row.get("result"), dict):
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
        self.loaded = loaded or (lambda thread: thread.get("path") in loaded_rollouts())

    def _valid(self, binding):
        if binding.get("platform") != "codex" or binding.get("host") != socket.gethostname():
            return False
        try:
            if str(uuid.UUID(binding.get("session_id", ""))) != binding["session_id"]:
                return False
        except (ValueError, TypeError, AttributeError):
            return False
        cwd = binding.get("cwd")
        return isinstance(cwd, str) and os.path.isabs(cwd) and os.path.isdir(cwd)

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
            raise ValueError("Thread identity mismatch")
        if thread.get("archived") is True or thread.get("readOnly") is True:
            raise ValueError("Thread is not writable")
        path = thread.get("path")
        if not isinstance(path, str):
            raise ValueError("Missing local rollout")
        file = Path(path)
        base = self.home / ".codex" / "sessions"
        if (
            not file.is_file()
            or not os.access(file, os.R_OK)
            or file.is_symlink()
            or not file.resolve().is_relative_to(base.resolve())
        ):
            raise ValueError("Thread rollout is not active local history")
        if not self.loaded(thread):
            raise ValueError("Thread is not loaded")
        return thread

    def preflight(self, binding):
        if not self._valid(binding):
            return "invalid"
        rpc = None
        try:
            # A missing binary or queue database is a recoverable local state (app not
            # installed yet, updated, or restarted), so it stays queued as "busy".  Only
            # an unsupported host or platform is permanently unavailable, and _valid
            # already rejects those as "invalid" before this point.
            if (
                not os.access(self.binary, os.X_OK)
                or not (self.home / ".codex/queue_1.sqlite").is_file()
            ):
                return "busy"
            rpc = self.rpc_factory()
            self._thread(rpc, binding)
            rows = rpc.request(
                "thread/queue/list",
                {"threadId": binding["session_id"], "limit": QUEUE_CAPACITY},
            )
            data = rows.get("data")
            if not isinstance(data, list):
                return "busy"
            # A full queue rejects the next add outright; treat it as busy so the nudge
            # is retried after the queue drains rather than stranded as uncertain.
            return "busy" if len(data) >= QUEUE_CAPACITY else "ready"
        except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
            return "busy"
        finally:
            if rpc:
                rpc.close()

    def receipt(self, binding, text):
        if not self._valid(binding):
            return False
        rpc = None
        try:
            rpc = self.rpc_factory()
            thread = self._thread(rpc, binding)
            result = rpc.request(
                "thread/queue/list", {"threadId": binding["session_id"], "limit": 100}
            )
            client_id = message_id(binding, text)
            for item in result.get("data", []):
                if (
                    isinstance(item, dict)
                    and item.get("clientUserMessageId") == client_id
                    and exact_text_input(item.get("input"), text)
                ):
                    return True
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
                    if (
                        event.get("type") == "event_msg"
                        and payload.get("type") == "user_message"
                        and payload.get("message") == text
                    ):
                        return True
                    if (
                        event.get("type") == "response_item"
                        and payload.get("type") == "message"
                        and payload.get("role") == "user"
                    ):
                        content = payload.get("content", [])
                        if (
                            "".join(
                                x.get("text", "")
                                for x in content
                                if isinstance(x, dict) and x.get("type") == "input_text"
                            )
                            == text
                        ):
                            return True
        except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
            return False
        finally:
            if rpc:
                rpc.close()
        return False

    def send(self, binding, text, *, permit: Callable[[], bool] | None = None):
        if self.preflight(binding) != "ready":
            return "deferred"
        rpc = None
        sent = False
        try:
            rpc = self.rpc_factory()
            self._thread(rpc, binding)
            if permit is not None and not permit():
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
            return "accepted"
        except (OSError, ValueError, TimeoutError, subprocess.SubprocessError):
            # Nothing reached thread/queue/add, so the wake is still safe to retry: only a
            # failure after the add is uncertain.
            return "unknown" if sent else "deferred"
        finally:
            if rpc:
                rpc.close()
