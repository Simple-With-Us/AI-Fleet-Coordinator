"""Actual isolated reader, provider trickle and bounded private IPC regressions."""

import io
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from urllib.parse import unquote

import fleet_infisical as core


class BoundedReadTests(unittest.TestCase):
    def setUp(self):
        self.project = str(uuid.uuid4())
        self.sample = "st." + secrets.token_urlsafe(32) + ".part.two"
        self.mode = "normal"
        self.calls = []
        self.children = []
        self.mixed = threading.Barrier(2)
        case = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                case.calls.append(self.path)
                if self.headers.get("Authorization") != "Bearer " + case.sample:
                    self.send_error(403)
                    return
                if case.mode == "slow-headers":
                    time.sleep(1.2)
                metadata = self.path.endswith("/service-token")
                if metadata:
                    payload = {
                        "workspace": case.project,
                        "permissions": ["read"],
                        "scopes": [{"environment": "prod", "secretPath": "/operator-registry"}],
                        "expiresAt": "2099-01-01T00:00:00Z",
                    }
                    if case.mode == "wrong-scope":
                        payload["scopes"] = [{"environment": "prod", "secretPath": "/"}]
                else:
                    name = unquote(self.path.split("/raw/", 1)[1].split("?", 1)[0])
                    if case.mode == "mixed":
                        case.mixed.wait(timeout=1)
                        if name == "B_KEY":
                            time.sleep(1.2)
                    payload = {
                        "secret": {
                            "secretKey": name,
                            "secretValue": "value-" + name,
                            "type": "shared",
                            "workspace": case.project,
                            "environment": "prod",
                            "secretPath": "/operator-registry",
                        }
                    }
                raw = json.dumps(payload).encode()
                try:
                    failed = case.mode == "slow-error" or (
                        case.mode == "mixed" and not metadata and name == "A_KEY"
                    )
                    self.send_response(503 if failed else 200)
                    if case.mode == "slow-chunk":
                        self.send_header("Transfer-Encoding", "chunked")
                    else:
                        self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    if case.mode == "slow-chunk":
                        for value in b"0000000000000000000001\r\n":
                            self.wfile.write(bytes([value]))
                            self.wfile.flush()
                            time.sleep(0.04)
                    elif case.mode in {"slow-body", "slow-error"}:
                        for value in raw[:24]:
                            self.wfile.write(bytes([value]))
                            self.wfile.flush()
                            time.sleep(0.05)
                        self.wfile.write(raw[24:])
                    else:
                        self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(
            target=lambda: self.server.serve_forever(poll_interval=0.01), daemon=True
        )
        self.thread.start()
        self.addCleanup(self.stop)
        self.env = {
            core.ENV_API: "http://127.0.0.1:%d/api" % self.server.server_address[1],
            core.ENV_READ_PATHS: "/operator-registry",
            "INFISICAL_ENVIRONMENT": "prod",
            "INFISICAL_TOKEN": "another-consumer",
            "PYTHONPATH": "/untrusted",
        }
        real = core.subprocess.Popen

        def tracked(*args, **kwargs):
            self.assertFalse(kwargs.get("shell", False))
            self.assertEqual(kwargs["env"], {"PATH": os.defpath})
            self.assertNotIn(self.sample, repr(args))
            child = real(*args, **kwargs)
            self.children.append(child)
            return child

        patch = mock.patch.object(core.subprocess, "Popen", side_effect=tracked)
        patch.start()
        self.addCleanup(patch.stop)

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
        for child in self.children:
            self.assertIsNotNone(child.poll(), "the owned read child survived cleanup")

    def fetch(self, timeout=1.0, **kwargs):
        return core.bounded_fetch_project(
            self.project,
            "/operator-registry",
            self.env,
            token=self.sample,
            names=("A_KEY", "B_KEY"),
            timeout=timeout,
            **kwargs,
        )

    def test_real_worker_verifies_grant_and_exact_named_values(self):
        self.assertEqual(self.fetch(), {"A_KEY": "value-A_KEY", "B_KEY": "value-B_KEY"})
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(self.children), 1)

    def test_wrong_grant_stops_inside_worker_before_any_value_read(self):
        self.mode = "wrong-scope"
        with self.assertRaises(core.SecretsError) as caught:
            self.fetch()
        self.assertEqual(caught.exception.failure_kind, "refused")
        self.assertEqual(len(self.calls), 1)

    def test_trickle_success_error_headers_and_chunk_framing_share_one_budget(self):
        for mode in ("slow-body", "slow-error", "slow-headers", "slow-chunk", "mixed"):
            with self.subTest(mode=mode):
                self.mode = mode
                started = time.monotonic()
                with self.assertRaises(core.SecretsError):
                    self.fetch(timeout=0.2)
                self.assertLess(time.monotonic() - started, 0.65)
                self.assertIsNotNone(self.children[-1].poll())

    def scripted(self, program):
        return mock.patch.object(
            core, "_bounded_read_command", return_value=[sys.executable, "-I", "-B", "-c", program]
        )

    def test_blocked_resolver_or_executor_worker_is_killed_and_reaped(self):
        programs = [
            "import concurrent.futures,time; pool=concurrent.futures.ThreadPoolExecutor(); pool.submit(time.sleep,30); pool.shutdown(wait=True)",
            "import socket,time;socket.getaddrinfo=lambda *a,**k:time.sleep(30);socket.getaddrinfo('fixture.invalid',443)",
        ]
        for program in programs:
            with self.subTest(program=program[:20]), self.scripted(program):
                started = time.monotonic()
                with self.assertRaisesRegex(core.SecretsError, "bounded-read-timeout"):
                    self.fetch(timeout=0.2)
                self.assertLess(time.monotonic() - started, 0.65)
            self.assertIsNotNone(self.children[-1].poll())

    def test_partial_or_late_success_never_returns_values(self):
        program = "import json,os,sys,time;sys.stdout.write(json.dumps(dict(schema=1,ok=True,values=dict(A_KEY='late'))));sys.stdout.flush();os.close(1);time.sleep(30)"
        with self.scripted(program), self.assertRaises(core.SecretsError):
            self.fetch(timeout=0.2)

    def test_malformed_oversized_or_unrequested_output_fails_without_reflection(self):
        programs = [
            "import sys;sys.stdout.write('private-provider-text')",
            "import sys,time;sys.stdout.buffer.write(b'x'*100000);sys.stdout.flush();time.sleep(30)",
            "import json;print(json.dumps(dict(schema=2,ok=True,values={})))",
            "import json;print(json.dumps(dict(schema=1,ok=True,values=dict(OTHER='private-provider-text'))))",
        ]
        for program in programs:
            with self.subTest(program=program[:20]), self.scripted(program):
                with self.assertRaises(core.SecretsError) as caught:
                    self.fetch(max_output_bytes=1024)
                self.assertNotIn("private-provider-text", str(caught.exception))

    def test_interruption_kills_only_the_owned_worker_before_propagating(self):
        with mock.patch.object(core, "_read_worker_pipes", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.fetch()
        self.assertIsNotNone(self.children[-1].poll())

    def test_cleanup_stall_is_exposed_as_fixed_unavailable_failure(self):
        child = mock.Mock(stdin=io.BytesIO(), stdout=io.BytesIO())
        child.poll.return_value = None
        child.wait.side_effect = subprocess.TimeoutExpired("owned child", 0)
        with (
            mock.patch.object(core.subprocess, "Popen", return_value=child),
            mock.patch.object(
                core, "_read_worker_pipes", side_effect=subprocess.TimeoutExpired("owned child", 0)
            ),
            self.assertRaisesRegex(core.SecretsError, "bounded-read-cleanup-failed"),
        ):
            self.fetch(timeout=0.2)
        child.kill.assert_called()

    def test_interruption_keeps_its_type_and_surfaces_combined_cleanup_failure(self):
        for interrupt in (KeyboardInterrupt(), SystemExit(7)):
            for failed in ("kill", "wait"):
                with self.subTest(interrupt=type(interrupt).__name__, failed=failed):
                    child = mock.Mock(stdin=io.BytesIO(), stdout=io.BytesIO())
                    child.poll.return_value = None
                    if failed == "kill":
                        child.kill.side_effect = OSError("private-os-detail")
                    else:
                        child.wait.side_effect = subprocess.TimeoutExpired("owned child", 0)
                    with (
                        mock.patch.object(core.subprocess, "Popen", return_value=child),
                        mock.patch.object(core, "_read_worker_pipes", side_effect=interrupt),
                        self.assertRaises(type(interrupt)) as caught,
                    ):
                        self.fetch(timeout=0.2)
                    self.assertIs(caught.exception, interrupt)
                    cause = caught.exception.__cause__
                    self.assertIsInstance(cause, core.SecretsError)
                    self.assertEqual(str(cause), "bounded-read-cleanup-failed")
                    self.assertEqual(cause.failure_kind, "unavailable")
                    self.assertNotIn("private-os-detail", str(cause))

    def test_local_result_validation_cannot_return_success_after_deadline(self):
        with (
            mock.patch.object(core, "_bounded_read_request", return_value={"A_KEY": "fixture"}),
            mock.patch.object(core, "reject_unresolved", side_effect=lambda *_: time.sleep(0.08)),
            self.assertRaisesRegex(core.SecretsError, "bounded-read-timeout"),
        ):
            self.fetch(timeout=0.05)

    def test_invalid_input_is_refused_before_any_worker_or_provider(self):
        for timeout in (0, -1, float("inf"), True):
            with self.subTest(timeout=timeout), self.assertRaises(core.UsageError):
                self.fetch(timeout=timeout)
        self.assertEqual(self.children, [])
        self.assertEqual(self.calls, [])

    def test_private_worker_has_no_selectable_write_operation(self):
        request = dict(
            schema=1,
            operation="write",
            project=self.project,
            path="/operator-registry",
            env={core.ENV_API: self.env[core.ENV_API]},
            token=self.sample,
            names=["A_KEY"],
            writer=False,
            timeout=1.0,
            max_output_bytes=4096,
        )
        with self.assertRaises(core.SecretsError):
            core._bounded_read_request(request, time.monotonic() + 1)
        self.assertEqual(self.calls, [])

    def test_request_cap_is_checked_before_launch(self):
        self.env[core.ENV_API] += "x" * (128 * 1024)
        with self.assertRaises(core.UsageError):
            self.fetch()
        self.assertEqual(self.children, [])
