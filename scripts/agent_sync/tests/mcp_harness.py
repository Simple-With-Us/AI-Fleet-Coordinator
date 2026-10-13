"""Harness for the `agent-sync mcp` tests:  the server driven over real pipes, in-process (with a fake
clock) or as a subprocess of scripts/agent-sync, against the fake Zulip server.

Every environment is an explicit dict:  HOME is a temp dir whose .secrets/Zulip holds the test
bot's rc file, so the server never opens the real ~/.secrets, and subprocesses never inherit
os.environ.  Every line the server prints goes into the transcript that Harness.tearDown checks
for the key and its base64 form, and every stdout line must be one JSON-RPC 2.0 message.
"""
from __future__ import annotations

import io
import json
import os
import queue
import secrets
import string
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from agent_sync import cli
from agent_sync.mcp import stdio as S
from agent_sync.mcp import tools as T
from agent_sync.tests.fake_zulip import BOT_EMAIL, EPOCH
from agent_sync.tests.harness import SESSION, Harness, write_rc

SCRIPT = Path(__file__).resolve().parents[2] / "agent-sync"
MODERN = "2026-07-28"
CLIENT_META = {S.META_VERSION: MODERN, "io.modelcontextprotocol/clientCapabilities": {},
               "io.modelcontextprotocol/clientInfo": {"name": "test-client", "version": "1"}}


class FakeClock:
    """Wall-clock stand-in:  `sleep` records the wait and moves the clock forward."""

    def __init__(self, start: float = EPOCH + 50) -> None:
        self.now = float(start)
        self.sleeps: list[float] = []
        self.lock = threading.Lock()

    def time(self) -> float:
        with self.lock:
            return self.now

    def sleep(self, seconds: float) -> None:
        with self.lock:
            self.sleeps.append(seconds)
            self.now += max(0.0, seconds)


def zulip_shaped() -> str:
    """A fresh 32-character key with upper, lower and digits (never the bot's)."""
    alphabet = string.ascii_letters + string.digits
    while True:
        key = "".join(secrets.choice(alphabet) for _ in range(32))
        if any(c.isupper() for c in key) and any(c.islower() for c in key) and any(c.isdigit() for c in key):
            return key


def check_jsonrpc_line(test: Any, line: bytes) -> dict[str, Any]:
    """Every stdout line is one ASCII JSON-RPC 2.0 response."""
    text = line.decode("ascii")
    test.assertTrue(text.endswith("\n"))
    message = json.loads(text)
    test.assertIsInstance(message, dict)
    test.assertEqual(message.get("jsonrpc"), "2.0")
    test.assertIn("id", message)
    test.assertTrue(("result" in message) != ("error" in message), message)
    return message


class McpSession:
    """One in-process server on a thread, talking over two os.pipe() pairs."""

    def __init__(self, harness: "McpHarness", env: dict[str, str], clock: FakeClock, argv: tuple[str, ...] = (),
                 timeout: float = 5.0) -> None:
        self.harness = harness
        self.clock = clock
        server_in_r, client_w = os.pipe()
        client_r, server_out_w = os.pipe()
        self.to_server = os.fdopen(client_w, "wb", buffering=0)
        self.from_server = os.fdopen(client_r, "rb")
        server_in = os.fdopen(server_in_r, "rb")
        server_out = os.fdopen(server_out_w, "wb", buffering=0)
        self.err = io.StringIO()
        self.code: int | None = None
        self.lines: "queue.Queue[bytes | None]" = queue.Queue()
        self.next_id = 0
        rt = cli.Runtime(env=env, stdin=server_in, stdout=server_out, stderr=self.err, home=harness.home,
                         sleep=clock.sleep, timeout=timeout, events_timeout=3.0)
        args = cli.build_parser().parse_args(["mcp", *argv])

        def serve() -> None:
            try:
                self.code = S.run(rt, args, clock=clock.time)
            finally:
                server_out.close()
                server_in.close()

        def pump() -> None:
            for line in iter(self.from_server.readline, b""):
                self.lines.put(line)
            self.lines.put(None)

        self.thread = threading.Thread(target=serve, daemon=True)
        self.reader = threading.Thread(target=pump, daemon=True)
        self.thread.start()
        self.reader.start()
        self.closed = False

    def send_raw(self, data: bytes) -> None:
        self.to_server.write(data)

    def send(self, message: Any) -> None:
        self.send_raw((json.dumps(message) + "\n").encode("utf-8"))

    def receive(self, timeout: float = 20.0) -> dict[str, Any]:
        line = self.lines.get(timeout=timeout)
        if line is None:
            raise AssertionError("the server closed stdout; stderr: %s" % self.err.getvalue())
        with self.harness._transcript_lock:
            self.harness.transcript.append(line.decode("ascii", "replace"))
        return check_jsonrpc_line(self.harness, line)

    def request(self, method: str, params: dict[str, Any] | None = None, *, modern: bool = False) -> dict[str, Any]:
        self.next_id += 1
        params = dict(params or {})
        if modern:
            params["_meta"] = dict(CLIENT_META)
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params:
            message["params"] = params
        self.send(message)
        response = self.receive()
        self.harness.assertEqual(response["id"], self.next_id)
        return response

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)

    def call(self, name: str, arguments: dict[str, Any] | None = None, *, modern: bool = False) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        response = self.request("tools/call", params, modern=modern)
        self.harness.assertIn("result", response, response)
        return response["result"]

    def close(self) -> int | None:
        """EOF to the server, then everything it printed and nobody read goes into the transcript."""
        if not self.closed:
            self.closed = True
            self.to_server.close()
            self.thread.join(timeout=20)
            self.reader.join(timeout=5)
            self.from_server.close()
            unread: list[str] = []
            while True:
                try:
                    line = self.lines.get_nowait()
                except queue.Empty:
                    break
                if line is not None:
                    unread.append(line.decode("ascii", "replace"))
            with self.harness._transcript_lock:
                self.harness.transcript.extend(unread + [self.err.getvalue()])
        return self.code


class McpHarness(Harness):
    """Harness plus the strict rc file under HOME and helpers to start servers."""

    def setUp(self) -> None:
        super().setUp()
        self.home_rc = write_rc(self.home / ".secrets" / "Zulip" / "Claude-zuliprc", email=BOT_EMAIL, key=self.key,
                                site=self.fake.url)
        self.clock = FakeClock()
        self.spec = T.load_tools()
        self.extra_secrets: list[str] = []
        self.sessions: list[McpSession] = []

    def tearDown(self) -> None:
        # unittest runs tearDown before cleanups:  close every server first, so its stderr and any
        # unread stdout are in the transcript the key check reads.
        for session in self.sessions:
            session.close()
        super().tearDown()
        everything = "\n".join(self.transcript)
        for value in self.extra_secrets:
            self.assertNotIn(value, everything, "another bot's key reached the output")

    def mcp_env(self, **overrides: str | None) -> dict[str, str]:
        base = {"HOME": str(self.home), "AGENT_SYNC_REALM": self.fake.url, "AGENT_SYNC_STATE_DIR": str(self.state_dir),
                "AGENT_SEAT": "CLAUDE", "CLAUDE_CODE_SESSION_ID": SESSION}
        base.update({k: v for k, v in overrides.items() if v is not None})
        for name, value in overrides.items():
            if value is None:
                base.pop(name, None)
        return base

    def session(self, env: dict[str, str] | None = None, *, clock: FakeClock | None = None,
                argv: tuple[str, ...] = (), timeout: float = 5.0, initialize: bool = True) -> McpSession:
        session = McpSession(self, self.mcp_env() if env is None else env, clock or self.clock, argv, timeout)
        self.sessions.append(session)
        self.addCleanup(session.close)
        if initialize:
            response = session.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                                      "clientInfo": {"name": "test-client", "version": "1"}})
            self.assertIn("result", response, session.err.getvalue())
            session.notify("notifications/initialized")
        return session

    def spawn(self, env: dict[str, str], requests: list[Any], *, argv: tuple[str, ...] = (),
              timeout: float = 60.0) -> tuple[int, list[dict[str, Any]], str]:
        """Run scripts/agent-sync mcp as a real process, write every request, close stdin, and
        return (exit code, the parsed stdout lines, stderr)."""
        data = b"".join((json.dumps(r) + "\n").encode("utf-8") if not isinstance(r, bytes) else r for r in requests)
        proc = subprocess.run([sys.executable, str(SCRIPT), "mcp", *argv], input=data, capture_output=True, env=env,
                              timeout=timeout)
        stderr = proc.stderr.decode("utf-8", "replace")
        with self._transcript_lock:
            self.transcript.extend([proc.stdout.decode("utf-8", "replace"), stderr])
        lines = proc.stdout.splitlines(keepends=True)
        return proc.returncode, [check_jsonrpc_line(self, line) for line in lines], stderr

    # ---- assertions -------------------------------------------------------------------------
    def assert_tool_error(self, result: dict[str, Any], code: str) -> dict[str, Any]:
        self.assertIs(result.get("isError"), True, result)
        self.assertNotIn("structuredContent", result)
        obj = json.loads(result["content"][0]["text"])
        self.assertEqual(obj["code"], code, obj)
        self.assertEqual(result["_meta"][T.ERROR_META_KEY], obj)
        self.assertIsInstance(obj["retryable"], bool)
        return obj

    def assert_structured(self, name: str, result: dict[str, Any]) -> dict[str, Any]:
        self.assertNotIn("isError", result)
        tool = next(t for t in self.spec["tools"] if t["name"] == name)
        problem = T.schema_problem(tool["outputSchema"], result["structuredContent"], "structuredContent")
        self.assertIsNone(problem, problem)
        return result["structuredContent"]

    def assert_fence(self, text: str) -> list[dict[str, Any]]:
        """Exactly one BEGIN and one END line with the same nonce; every line between them one JSON
        object; no marker text anywhere else.  Returns the items."""
        lines = text.splitlines()
        self.assertEqual(lines, text.split("\n"), "a line break other than \\n reached the text")
        begins = [i for i, line in enumerate(lines) if line.startswith("BEGIN_UNTRUSTED_ZULIP")]
        ends = [i for i, line in enumerate(lines) if line.startswith("END_UNTRUSTED_ZULIP")]
        self.assertEqual(len(begins), 1, text)
        self.assertEqual(len(ends), 1, text)
        begin, end = begins[0], ends[0]
        self.assertLess(begin, end)
        self.assertEqual(end, len(lines) - 1)
        nonce = lines[begin].split("nonce=", 1)[1]
        self.assertRegex(nonce, r"^[0-9a-f]{16}$")
        self.assertEqual(lines[end], "END_UNTRUSTED_ZULIP nonce=" + nonce)
        for index, line in enumerate(lines):
            if index not in (begin, end):
                self.assertIsNone(T._BROAD_MARKER_RE.search(line), "marker text outside the fence lines: %r" % line)
        items = [json.loads(line) for line in lines[begin + 1:end]]
        for item in items:
            self.assertIsInstance(item, dict)
        return items
