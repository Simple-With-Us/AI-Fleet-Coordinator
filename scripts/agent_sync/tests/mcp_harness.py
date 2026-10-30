"""Harness for the `simple-chat mcp` tests:  the server driven over real pipes, in-process (with a fake
clock) or as a subprocess of scripts/simple-chat, against the fake Zulip server.

Every environment is an explicit dict:  HOME is a temp dir, the test bot's keys sit in the fake Infisical
/zulip (in-process:  the patched zulip fetch;  a subprocess:  FLEET_SECRETS_BIN points at
fixtures/fake-fleet-secrets, which reads a temp JSON file), so the server never reaches the real
fleet-secrets or any credential folder, and subprocesses never inherit os.environ.  Every line the server prints goes into the transcript that Harness.tearDown checks
for the key and its base64 form, and every stdout line must be one JSON-RPC 2.0 message.
"""

from __future__ import annotations

import io
import json
import os
import queue
import re
import secrets
import string
import subprocess
import sys
import uuid
from unittest import mock
import threading
from pathlib import Path
from typing import Any

from agent_sync import cli
from agent_sync import identity as I
from agent_sync import live as L
from agent_sync.mcp import identity_router as R
from agent_sync.mcp import stdio as S
from agent_sync.mcp import tools as T
from agent_sync.tests import bearer_stub
from agent_sync.tests.fake_zulip import BOT_EMAIL, EPOCH, FAKE_CHANNEL, FAKE_OWNER_ID


def seat_bearer_locator(seat: str) -> str:
    """The locator a seat's bearer lives at:  a FOLDER:KEY_NAME location and never a value.

    The key part allows only uppercase letters, digits and underscores, so a seat name with a hyphen
    is underscored there.
    """
    return "/fleet-seats:%s_SEAT_BEARER" % re.sub(r"[^A-Z0-9]", "_", seat.upper())
from agent_sync.tests.harness import SESSION, Harness, write_rc

SCRIPT = Path(__file__).resolve().parents[2] / "simple-chat"
BEARER_STUB_LAUNCHER = Path(__file__).resolve().parent / "fixtures" / "simple-chat-bearer-stub.py"
FAKE_FLEET_SECRETS = Path(__file__).resolve().parent / "fixtures" / "fake-fleet-secrets"
# the real binary resolves its Infisical project from this env or the home file and fails closed
# without one, so the fake is always driven with a project named.
FAKE_PROJECT_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, 'fixture-project-afc'))
MODERN = "2026-07-28"
AUTO = object()  # McpSession.call:  a write names the session's default identity
CLIENT_META = {
    S.META_VERSION: MODERN,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "test-client", "version": "1"},
}


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
        if (
            any(c.isupper() for c in key)
            and any(c.islower() for c in key)
            and any(c.isdigit() for c in key)
        ):
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

    def __init__(
        self,
        harness: McpHarness,
        env: dict[str, str],
        clock: FakeClock,
        argv: tuple[str, ...] = (),
        timeout: float = 5.0,
    ) -> None:
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
        self.lines: queue.Queue[bytes | None] = queue.Queue()
        self.next_id = 0
        # The identity a write names when the test does not name one (AFC-331:  a write must name its
        # identity):  the session's default, from the same precedence the server applies.
        flags = dict(zip(argv, argv[1:]))
        self.identity = I.resolve_seat(
            env, flag=flags.get("--as"), default=flags.get("--default-seat"), use_tag=False
        ).seat
        rt = cli.Runtime(
            env=env,
            stdin=server_in,
            stdout=server_out,
            stderr=self.err,
            home=harness.home,
            sleep=clock.sleep,
            timeout=timeout,
            events_timeout=3.0,
        )
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

    def request(
        self, method: str, params: dict[str, Any] | None = None, *, modern: bool = False
    ) -> dict[str, Any]:
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

    def call(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        modern: bool = False,
        identity: Any = AUTO,
    ) -> dict[str, Any]:
        """tools/call.  A write that names no identity gets the session's default one (`identity=AUTO`);
        pass identity=None to send none, or a name to send that one."""
        params: dict[str, Any] = {"name": name}
        if identity is AUTO:
            identity = (
                self.identity
                if name in T.WRITE_TOOLS and not (arguments and "identity" in arguments)
                else None
            )
        if identity is not None:
            arguments = {**(arguments or {}), "identity": identity}
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
        # An explicit rc file for the tests that pass --rc or ZULIP_RC;  nothing reads it by default (AFC-359).
        self.home_rc = write_rc(
            self.tmp / "rc" / "Claude-zuliprc", email=BOT_EMAIL, key=self.key, site=self.fake.url
        )
        self.add_infisical_bot("CLAUDE", email=BOT_EMAIL, key=self.key)
        self.fake_secrets_file = self.tmp / "fake-fleet-secrets.json"
        self.clock = FakeClock()
        self.spec = T.load_served_tools()  # tools.json, then eavesdropper-tools.json (AFC-330)
        # What tools/list serves a CLAUDE session whose Infisical holds only Claude's keys:  every tool with an
        # identity property whose enum is that one bot (AFC-331).
        self.served = R.decorate(self.spec, ["CLAUDE"], "CLAUDE")
        self.extra_secrets: list[str] = []
        self.sessions: list[McpSession] = []
        # The hosted gateway is stubbed at the bearer gate's transport only:  "ok" means it accepts the
        # seat's own bearer;  a test sets "revoked", "unavailable" or "other-seat" to see the gate fail closed.
        self.bearer_mode = "ok"
        patcher = mock.patch.object(S, "_bearer_verify", bearer_stub.stub_verify(lambda: self.bearer_mode))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        # unittest runs tearDown before cleanups:  close every server first, so its stderr and any
        # unread stdout are in the transcript the key check reads.
        for session in self.sessions:
            session.close()
        super().tearDown()
        everything = "\n".join(self.transcript)
        for value in self.extra_secrets:
            self.assertNotIn(value, everything, "another bot's key reached the output")

    def pin_owner(self, user_id: int = FAKE_OWNER_ID) -> None:
        """Pin the owner in the installed listener.toml, the way a host does.

        The owner-in-the-conversation guard (AFC-469) fails closed:  with no owner pinned a direct
        message is refused, so a test that sends one has to pin the owner first.

        The directory is the fleet's own private one, mode 700, and a symlinked state directory is
        refused rather than followed:  a test that writes through a link would write outside its
        own tree, and the file it pins would not be the one the listener reads.
        """
        if self.state_dir.is_symlink():
            raise AssertionError("the state directory is a symlink:  %s" % self.state_dir)
        L.private_dir(str(self.state_dir))
        (self.state_dir / "listener.toml").write_text("[daemon]\nowner_user_id = %d\n" % user_id)

    def pin_seat_bearers(self, locators: dict[str, str]) -> None:
        """Give seats their own bearer locators in the installed listener.toml, the way a host does.

        board proves its seat from the bearer at SIMPLE_BOARD_BEARER_LOCATOR and never reads AGENT_SEAT,
        so the locator a seat writes with comes from its own trusted daemon config.  A locator is a
        FOLDER:KEY_NAME location and never a value.  Only the seat sections are written, so the owner
        stays unpinned exactly as it was.
        """
        if self.state_dir.is_symlink():
            raise AssertionError("the state directory is a symlink:  %s" % self.state_dir)
        L.private_dir(str(self.state_dir))
        body: list[str] = []
        for seat, locator in sorted(locators.items()):
            body += [
                "[seat.%s]" % seat,
                'bot = "%s"' % seat.capitalize(),
                'bearer_locator = "%s"' % locator,
                "",
            ]
        (self.state_dir / "listener.toml").write_text("\n".join(body))

    def mcp_env(self, **overrides: str | None) -> dict[str, str]:
        base = {
            "HOME": str(self.home),
            "AGENT_SYNC_REALM": self.fake.url,
            # the fake realm's channel:  the suite pins it here so these tests keep
            # exercising their own channel name instead of the runtime default
            "AGENT_SYNC_CHANNEL": FAKE_CHANNEL,
            "AGENT_SYNC_STATE_DIR": str(self.state_dir),
            "AGENT_SEAT": "CLAUDE",
            "CLAUDE_CODE_SESSION_ID": SESSION,
            "FLEET_SECRETS_BIN": str(FAKE_FLEET_SECRETS),
            "FAKE_FLEET_SECRETS_FILE": str(self.fake_secrets_file),
            "INFISICAL_FLEET_OPS_PROJECT_ID": FAKE_PROJECT_ID,
            # The DM rule defaults ON (owner decision, Oct 22 2026).  The mechanical DM tool tests
            # are about tagging, grouping, the secret scan and idempotency, not the rule, so they run
            # with it off;  the rule's own tests are agent_sync/tests/test_dm_rule.py.
            "AGENT_SYNC_DM_RULE": "off",
            # board proves its seat from the bearer at SIMPLE_BOARD_BEARER_LOCATOR and never reads
            # AGENT_SEAT, so a Linear write through this server needs the seat's own locator.  It is a
            # FOLDER:KEY_NAME location and never a value.
            "SIMPLE_BOARD_BEARER_LOCATOR": seat_bearer_locator("CLAUDE"),
        }
        base.update({k: v for k, v in overrides.items() if v is not None})
        for name, value in overrides.items():
            if value is None:
                base.pop(name, None)
        self.write_fake_secrets()
        return base

    def write_fake_secrets(self) -> None:
        """The fake Infisical a subprocess reads (fixtures/fake-fleet-secrets):  the current infisical_values."""
        self.fake_secrets_file.write_text(
            json.dumps({"/zulip": self.infisical_values}), encoding="utf-8"
        )
        self.fake_secrets_file.chmod(0o600)

    def session(
        self,
        env: dict[str, str] | None = None,
        *,
        clock: FakeClock | None = None,
        argv: tuple[str, ...] = (),
        timeout: float = 5.0,
        initialize: bool = True,
    ) -> McpSession:
        session = McpSession(
            self, self.mcp_env() if env is None else env, clock or self.clock, argv, timeout
        )
        self.sessions.append(session)
        self.addCleanup(session.close)
        if initialize:
            response = session.request(
                "initialize",
                {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test-client", "version": "1"},
                },
            )
            self.assertIn("result", response, session.err.getvalue())
            session.notify("notifications/initialized")
        return session

    def spawn(
        self,
        env: dict[str, str],
        requests: list[Any],
        *,
        argv: tuple[str, ...] = (),
        timeout: float = 60.0,
    ) -> tuple[int, list[dict[str, Any]], str]:
        """Run scripts/simple-chat mcp as a real process, write every request, close stdin, and
        return (exit code, the parsed stdout lines, stderr)."""
        self.write_fake_secrets()
        data = b"".join(
            (json.dumps(r) + "\n").encode("utf-8") if not isinstance(r, bytes) else r
            for r in requests
        )
        proc = subprocess.run(
            [sys.executable, str(BEARER_STUB_LAUNCHER), "mcp", *argv],
            input=data,
            capture_output=True,
            env={**env, "BEARER_STUB_MODE": self.bearer_mode},
            timeout=timeout,
        )
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
        problem = T.schema_problem(
            tool["outputSchema"], result["structuredContent"], "structuredContent"
        )
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
                self.assertIsNone(
                    T._BROAD_MARKER_RE.search(line),
                    "marker text outside the fence lines: %r" % line,
                )
        items = [json.loads(line) for line in lines[begin + 1 : end]]
        for item in items:
            self.assertIsInstance(item, dict)
        return items
