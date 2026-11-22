"""Actual isolated reader, provider trickle and bounded private IPC regressions."""

import contextlib
import datetime
import errno
import importlib.util
import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
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
        self.omitted_fields = ()
        self.record_changes = {}
        self.expiry = "2099-01-01T00:00:00Z"
        self.permissions = ["read"]
        self.metadata_changes = {}
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
                if metadata and case.mode == "redirect":
                    self.send_response(302)
                    self.send_header("Location", case.env[core.ENV_API] + "/redirect-target")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if metadata:
                    payload = {
                        "workspace": case.project,
                        "permissions": case.permissions,
                        "scopes": [{"environment": "prod", "secretPath": "/operator-registry"}],
                        "expiresAt": case.expiry,
                    }
                    if case.mode == "wrong-scope":
                        payload["scopes"] = [{"environment": "prod", "secretPath": "/"}]
                    payload.update(case.metadata_changes)
                elif "/raw?" in self.path:
                    payload = {
                        "secrets": [
                            {
                                "secretKey": "A_KEY",
                                "type": "shared",
                                "workspace": case.project,
                                "environment": "prod",
                                "secretPath": "/operator-registry",
                            }
                        ]
                    }
                    for field in case.omitted_fields:
                        payload["secrets"][0].pop(field, None)
                    payload["secrets"][0].update(case.record_changes)
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
                            "secretValueHidden": False,
                        }
                    }
                    for field in case.omitted_fields:
                        payload["secret"].pop(field, None)
                    payload["secret"].update(case.record_changes)
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
            "INFISICAL_TOKEN": "test-token",
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

    def test_expected_identity_is_checked_inside_the_real_worker_metadata_request(self):
        expected = {"id": "fixture-record", "name": "consumer-ro", "expiresAt": self.expiry}
        self.metadata_changes = dict(expected)
        self.assertEqual(
            self.fetch(expected_identity=expected, require_finite_expiry=True),
            {"A_KEY": "value-A_KEY", "B_KEY": "value-B_KEY"},
        )
        self.assertEqual(sum(path.endswith("/service-token") for path in self.calls), 1)
        for changes in (
            {"id": "other"},
            {"name": "other"},
            {"_id": "other"},
            {"id": None},
            {"name": None},
            {"expiresAt": None},
            {"expiresAt": "2098-01-01T00:00:00Z"},
            {"expiresAt": "2000-01-01T00:00:00Z"},
            {"scopes": [{"environment": "prod", "secretPath": "/"}]},
            {"permissions": ["read", "write"]},
            {"workspace": str(uuid.uuid4())},
        ):
            with self.subTest(changes=changes):
                self.calls.clear()
                self.metadata_changes = dict(expected, **changes)
                with self.assertRaises(core.SecretsError):
                    self.fetch(expected_identity=expected, require_finite_expiry=True)
                self.assertEqual(len(self.calls), 1, "grant refusal must precede values")

    def test_an_explicit_api_base_reaches_the_worker_and_overrides_the_environment(self):
        # The provider can be named in code.  The explicit argument wins over a base
        # the caller's own environment carried,  so a route can pin its own origin.
        env = {**self.env, core.ENV_API: "https://ignored.example.invalid/api"}
        self.assertEqual(
            core.bounded_fetch_project(
                self.project,
                "/operator-registry",
                env,
                token=self.sample,
                names=("A_KEY",),
                timeout=1.0,
                api_base=self.env[core.ENV_API],
            ),
            {"A_KEY": "value-A_KEY"},
        )
        self.assertEqual(len(self.calls), 2)

    def test_an_invalid_explicit_api_base_is_refused_before_any_worker(self):
        for bad in ("", "not a url", "https://user@example.invalid/api", "ftp://example.invalid"):
            with self.subTest(bad=bad):
                with self.assertRaises(core.UsageError):
                    core.bounded_fetch_project(
                        self.project,
                        "/operator-registry",
                        self.env,
                        token=self.sample,
                        names=("A_KEY",),
                        timeout=1.0,
                        api_base=bad,
                    )
        self.assertEqual(self.calls, [])
        self.assertEqual(self.children, [])

    def test_expected_expiry_accepts_only_the_same_instant(self):
        expected = {"id": "fixture-record", "name": "consumer-ro", "expiresAt": self.expiry}
        self.metadata_changes = dict(expected, expiresAt="2098-12-31T18:00:00-06:00")
        self.assertEqual(len(self.fetch(expected_identity=expected)), 2)

    def test_real_worker_verifies_grant_and_exact_named_values(self):
        self.assertEqual(self.fetch(), {"A_KEY": "value-A_KEY", "B_KEY": "value-B_KEY"})
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(self.children), 1)

    def test_real_worker_never_follows_redirect_from_validated_loopback_provider(self):
        self.mode = "redirect"
        with self.assertRaises(core.SecretsError) as caught:
            self.fetch()
        self.assertEqual(caught.exception.failure_kind, "refused")
        self.assertEqual(self.calls, ["/api/v2/service-token"])
        self.assertEqual(len(self.children), 1)

    def test_real_worker_rejects_malformed_loopback_base_before_http(self):
        self.env[core.ENV_API] += "?untrusted-marker"
        with self.assertRaises(core.SecretsError) as caught:
            self.fetch()
        self.assertEqual(caught.exception.failure_kind, "refused")
        self.assertEqual(str(caught.exception), "bounded-read-failed")
        self.assertEqual(self.calls, [])
        self.assertEqual(len(self.children), 1)

    def test_real_worker_finite_expiry_policy_refuses_null_before_values(self):
        self.expiry = None
        with self.assertRaisesRegex(core.SecretsError, "token-expired-or-invalid"):
            self.fetch(require_finite_expiry=True)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0], "/api/v2/service-token")
        # Opting in changes only this caller; legacy read-only grants retain null.
        self.calls.clear()
        self.assertEqual(self.fetch(), {"A_KEY": "value-A_KEY", "B_KEY": "value-B_KEY"})
        self.assertEqual(len(self.calls), 3)

    def test_real_worker_finite_expiry_accepts_future_and_refuses_expired(self):
        self.assertEqual(len(self.fetch(require_finite_expiry=True)), 2)
        self.calls.clear()
        self.expiry = "2000-01-01T00:00:00Z"
        with self.assertRaisesRegex(core.SecretsError, "token-expired-or-invalid"):
            self.fetch(require_finite_expiry=True)
        self.assertEqual(len(self.calls), 1)

    def test_finite_expiry_option_is_boolean_before_worker_creation(self):
        for value in (None, 0, 1, "true"):
            with self.subTest(value=value), self.assertRaises(core.UsageError):
                self.fetch(require_finite_expiry=value)
        self.assertEqual(self.children, [])
        self.assertEqual(self.calls, [])

    def test_large_legal_request_survives_eagain_after_a_real_partial_pipe_write(self):
        program = """
import json,sys,time
raw = bytearray()
while True:
    chunk = sys.stdin.buffer.read(1024)
    if not chunk:
        break
    raw.extend(chunk)
    time.sleep(0.0001)
request = json.loads(raw)
assert 65536 < len(raw) <= 128 * 1024
assert request['operation'] == 'named-fetch'
assert len(request['names']) == 1024 and request['strict_records'] is True
print(json.dumps(dict(schema=1,ok=True,values=dict(A_KEY='fixture'))))
"""
        names = ["A_KEY"] + ["N_%04d_" % n + "x" * 64 for n in range(1023)]
        real_write = os.write
        attempted, accepted = [], []

        def partial_then_blocked(fd, data):
            attempted.append(len(data))
            if len(attempted) == 2:
                raise BlockingIOError(errno.EAGAIN, "synthetic transient pipe pressure")
            count = real_write(fd, data[:3072])
            accepted.append(count)
            return count

        with (
            self.scripted(program),
            mock.patch.object(core.os, "write", side_effect=partial_then_blocked),
        ):
            result = core.bounded_fetch_project(
                self.project,
                "/operator-registry",
                self.env,
                token=self.sample,
                names=names,
                timeout=2.0,
                strict_records=True,
            )
        self.assertEqual(result, {"A_KEY": "fixture"})
        self.assertGreater(sum(accepted), 65536)
        self.assertGreater(len(accepted), 2)
        self.assertGreater(attempted[0], accepted[0])
        self.assertEqual(self.calls, [])

    def test_repeated_eagain_keeps_original_deadline_and_reaps_the_real_child(self):
        with (
            self.scripted("import sys;sys.stdin.buffer.read()"),
            mock.patch.object(
                core.os, "write", side_effect=BlockingIOError(errno.EAGAIN, "fixture")
            ),
            self.assertRaises(core.SecretsError) as caught,
        ):
            started = time.monotonic()
            self.fetch(timeout=0.15)
        self.assertLess(time.monotonic() - started, 0.65)
        # The read gives up on its own deadline:  the timeout,  or the cleanup that
        # follows it when the killed child is not reaped inside the cleanup's window.
        self.assertIn(
            str(caught.exception), ("bounded-read-timeout", "bounded-read-cleanup-failed")
        )
        child = self.children[-1]
        give_up = time.monotonic() + 1.0
        while child.poll() is None and time.monotonic() < give_up:
            time.sleep(0.01)
        self.assertIsNotNone(child.poll())

    def test_real_worker_preserves_only_closed_unresolved_reason_and_code_five(self):
        self.record_changes = {"secretValue": "${PUBLIC_FIXTURE_REFERENCE}"}
        observed = {}
        real_read = core._read_worker_pipes

        def capture_reply(child, request, limit, deadline):
            raw = real_read(child, request, limit, deadline)
            observed.update(reply=json.loads(raw), returncode=child.returncode)
            return raw

        with (
            mock.patch.object(core, "_read_worker_pipes", side_effect=capture_reply),
            self.assertRaises(core.SecretsError) as caught,
        ):
            self.fetch(strict_records=True)
        self.assertEqual(caught.exception.code, 5)
        self.assertEqual(str(caught.exception), "unresolved-reference")
        self.assertEqual(observed["returncode"], 5)
        self.assertEqual(
            observed["reply"],
            {
                "schema": 1,
                "ok": False,
                "reason": "unresolved-reference",
                "code": 5,
                "failure_kind": None,
            },
        )
        self.assertEqual(len(self.calls), 3)

    def test_unresolved_wire_reason_requires_exact_typed_code_and_child_exit(self):
        for reason, code, status, extra in (
            ("unresolved-reference", True, 5, {}),
            ("unresolved-reference", "5", 5, {}),
            ("unresolved-reference", 5.0, 5, {}),
            ("unresolved-reference", 4, 5, {}),
            ("unresolved-reference", 5, 4, {}),
            ("private-provider-text", 5, 5, {}),
            ("unresolved-reference", 5, 5, {"ok": True}),
            ("unresolved-reference", 5, 5, {"provider": "private-provider-text"}),
        ):
            reply = dict(schema=1, ok=False, reason=reason, code=code, failure_kind=None)
            reply.update(extra)
            program = "import json,sys;sys.stdin.buffer.read();print(%r);sys.exit(%d)" % (
                json.dumps(reply),
                status,
            )
            with self.subTest(code=code, status=status), self.scripted(program):
                with self.assertRaises(core.SecretsError) as caught:
                    self.fetch()
                self.assertEqual(caught.exception.code, 4)
                self.assertEqual(str(caught.exception), "bounded-read-failed")

    def test_wrong_grant_stops_inside_worker_before_any_value_read(self):
        self.mode = "wrong-scope"
        with self.assertRaises(core.SecretsError) as caught:
            self.fetch()
        self.assertEqual(caught.exception.failure_kind, "refused")
        self.assertEqual(len(self.calls), 1)

    def test_strict_records_survive_real_worker_and_require_explicit_provenance(self):
        self.assertEqual(
            self.fetch(strict_records=True),
            {"A_KEY": "value-A_KEY", "B_KEY": "value-B_KEY"},
        )
        for field in ("workspace", "environment", "secretPath", "secretValueHidden"):
            with self.subTest(missing=field):
                self.omitted_fields = (field,)
                with self.assertRaises(core.SecretsError):
                    self.fetch(strict_records=True)
        self.omitted_fields = ()
        for changes in (
            {"workspace": str(uuid.uuid4())},
            {"environment": "staging"},
            {"secretPath": "/"},
            {"secretValueHidden": True},
            {"secretValueHidden": 0},
            {"secretValue": ""},
            {"secretValue": None},
            {"type": "personal"},
        ):
            with self.subTest(changed=tuple(changes)):
                self.record_changes = changes
                with self.assertRaises(core.SecretsError):
                    self.fetch(strict_records=True)

    def test_legacy_single_scope_record_contract_is_preserved(self):
        self.omitted_fields = ("workspace", "environment", "secretPath", "secretValueHidden")
        self.assertEqual(self.fetch(), {"A_KEY": "value-A_KEY", "B_KEY": "value-B_KEY"})

    def test_strict_record_option_is_a_boolean_before_any_child_or_provider(self):
        for value in (None, 0, 1, "true"):
            with self.subTest(value=value), self.assertRaises(core.UsageError):
                self.fetch(strict_records=value)
        self.assertEqual(self.children, [])
        self.assertEqual(self.calls, [])

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
            strict_records=False,
            timeout=1.0,
            max_output_bytes=4096,
        )
        with self.assertRaises(core.SecretsError):
            core._bounded_read_request(request, time.monotonic() + 1)
        self.assertEqual(self.calls, [])

    def test_private_worker_rejects_malformed_project_before_provider(self):
        for project in (None, 7, [self.project], "", "x" * 257):
            request = dict(
                schema=1,
                operation="named-fetch",
                project=project,
                path="/operator-registry",
                env={core.ENV_API: self.env[core.ENV_API]},
                token=self.sample,
                names=["A_KEY"],
                writer=False,
                strict_records=False,
                timeout=1.0,
                max_output_bytes=4096,
            )
            with self.subTest(project_type=type(project).__name__):
                result = subprocess.run(
                    core._bounded_read_command(),
                    input=json.dumps(request).encode(),
                    capture_output=True,
                    env={"PATH": os.defpath},
                    timeout=3,
                )
                self.assertEqual(result.returncode, 4)
                self.assertEqual(result.stderr, b"")
                self.assertEqual(
                    json.loads(result.stdout),
                    {
                        "schema": 1,
                        "ok": False,
                        "reason": "bounded-read-invalid",
                        "failure_kind": "refused",
                    },
                )
                self.assertEqual(self.calls, [])

    def test_request_cap_is_checked_before_launch(self):
        self.env[core.ENV_API] += "x" * (128 * 1024)
        with self.assertRaises(core.UsageError):
            self.fetch()
        self.assertEqual(self.children, [])


class FileNamedReadTests(unittest.TestCase):
    stop = BoundedReadTests.stop

    def setUp(self):
        BoundedReadTests.setUp(self)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.binding = self.home / "own-reader"
        self.binding.write_text(self.sample)
        self.binding.chmod(0o600)

    def request(self):
        return dict(
            schema=1,
            operation="file-named-fetch",
            project_slug="fleet-ops",
            path="/operator-registry",
            token_file=str(self.binding),
            names=["A_KEY"],
            timeout=1.0,
            max_output_bytes=4096,
            env={
                "HOME": str(self.home),
                core.ENV_PROJECT: self.project,
                core.ENV_API: self.env[core.ENV_API],
                "INFISICAL_ENVIRONMENT": "prod",
            },
        )

    def test_legacy_four_positional_receiver_wrapper_preserves_real_grant_validation(self):
        original = core.validate_service_metadata
        calls = []

        def finite_grant(metadata, project, paths, permissions):
            # Same four-positional call contract as the settings bootstrap receiver.
            calls.append((project, paths, permissions))
            original(metadata, project, paths, permissions)
            expiry = datetime.datetime.fromisoformat(metadata["expiresAt"].replace("Z", "+00:00"))
            if expiry > datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=90):
                raise core.SecretsError("receiver-expiry-refused")

        self.permissions = ["read", "write"]
        self.expiry = (
            datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=30)
        ).isoformat()
        with mock.patch.object(core, "validate_service_metadata", new=finite_grant):
            self.assertEqual(
                core.verify_service_token(
                    self.sample,
                    self.project,
                    "/operator-registry",
                    self.env,
                    writer=True,
                ),
                ("/operator-registry",),
            )
            self.expiry = "2099-01-01T00:00:00Z"
            with self.assertRaisesRegex(core.SecretsError, "receiver-expiry-refused"):
                core.verify_service_token(
                    self.sample, self.project, "/operator-registry", self.env, writer=True
                )
            self.mode = "wrong-scope"
            with self.assertRaisesRegex(core.SecretsError, "token-scope-mismatch"):
                core.verify_service_token(
                    self.sample, self.project, "/operator-registry", self.env, writer=True
                )
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            len(self.calls), 3, "every attempt uses the actual provider metadata verifier"
        )

    def test_fetch_preserves_legacy_verifier_call_shape_and_real_named_read(self):
        original = core.verify_service_token
        calls = []

        def legacy_verifier(token, project, path, env, *, writer=False, deadline=None):
            calls.append((project, path, writer))
            return original(token, project, path, env, writer=writer, deadline=deadline)

        with mock.patch.object(core, "verify_service_token", new=legacy_verifier):
            self.assertEqual(
                core.fetch_project(
                    self.project,
                    "/operator-registry",
                    self.env,
                    token=self.sample,
                    names=["A_KEY"],
                    strict_records=True,
                    require_finite_expiry=False,
                ),
                {"A_KEY": "value-A_KEY"},
            )
        self.assertEqual(calls, [(self.project, "/operator-registry", False)])
        self.assertEqual(len(self.calls), 2)

    def run_worker(self, request=None, command=None):
        child = core.subprocess.Popen(
            command or core._bounded_read_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={"PATH": os.defpath},
            shell=False,
            close_fds=True,
        )
        try:
            output = core._read_worker_pipes(
                child,
                json.dumps(request or self.request()).encode(),
                65536,
                time.monotonic() + 2,
            )
            self.assertNotIn(self.sample, output.decode())
            self.assertNotIn(str(self.binding), output.decode())
            return json.loads(output), child.returncode
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=1)
            for stream in (child.stdin, child.stdout):
                if not stream.closed:
                    stream.close()

    def test_actual_file_worker_reads_one_value_without_an_app_or_grandchild(self):
        reply, code = self.run_worker()
        self.assertEqual(
            (reply, code), ({"schema": 1, "ok": True, "values": {"A_KEY": "value-A_KEY"}}, 0)
        )
        self.assertEqual(len(self.children), 1)
        self.assertEqual(len(self.calls), 2)
        # A copied canonical file has no package, CLI, bootstrap-module or RAG dependency.
        copied = self.home / "copied-client.py"
        shutil.copyfile(core.__file__, copied)
        self.assertEqual(
            self.run_worker(
                command=[sys.executable, "-I", "-B", str(copied), "--bounded-read-worker"]
            )[1],
            0,
        )

    def test_file_worker_requires_finite_expiry_but_raw_reader_retains_null_compatibility(self):
        self.expiry = None
        reply, code = self.run_worker()
        self.assertEqual((reply["reason"], code), ("token-expired-or-invalid", 4))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(
            core.bounded_fetch_project(
                self.project,
                "/operator-registry",
                self.env,
                token=self.sample,
                names=["A_KEY"],
                timeout=1,
                strict_records=True,
            ),
            {"A_KEY": "value-A_KEY"},
        )

    def test_file_worker_rejects_expired_wrong_scope_and_hidden_rows(self):
        for mode, expiry, changes in (
            ("wrong-scope", "2099-01-01T00:00:00Z", {}),
            ("normal", "2000-01-01T00:00:00Z", {}),
            ("normal", "2099-01-01T00:00:00Z", {"secretValueHidden": True}),
            ("normal", "2099-01-01T00:00:00Z", {"secretPath": "/foreign"}),
        ):
            with self.subTest(mode=mode, expiry=expiry, changes=changes):
                self.mode, self.expiry, self.record_changes = mode, expiry, changes
                reply, code = self.run_worker()
                self.assertEqual(code, 4)
                self.assertFalse(reply["ok"])

    def test_metadata_env_precedes_file_and_existing_first_line_format_is_preserved(self):
        directory = self.home / ".simple-tools"
        directory.mkdir(mode=0o700)
        metadata = directory / "fleet-ops-project-id"
        metadata.write_text("\n  " + self.project + "  \nignored second line\n")
        request = self.request()
        del request["env"][core.ENV_PROJECT]
        self.assertTrue(self.run_worker(request)[0]["ok"])
        metadata.unlink()
        metadata.symlink_to(self.binding)
        # A configured ID wins without inspecting the unsafe metadata file.
        self.assertTrue(self.run_worker()[0]["ok"])
        before = len(self.calls)
        reply, code = self.run_worker(request)
        self.assertEqual((reply["reason"], code), ("project-binding-required", 4))
        self.assertEqual(len(self.calls), before)
        request["env"][core.ENV_PROJECT] = "malformed-explicit-id"
        self.assertEqual(self.run_worker(request)[0]["reason"], "project-binding-required")
        self.assertEqual(len(self.calls), before)

    def test_metadata_fifo_oversize_and_bad_uuid_refuse_before_bootstrap_content(self):
        directory = self.home / ".simple-tools"
        directory.mkdir(mode=0o700)
        metadata = directory / "fleet-ops-project-id"
        request = self.request()
        del request["env"][core.ENV_PROJECT]
        for kind in ("fifo", "oversize", "malformed"):
            with self.subTest(kind=kind):
                if kind == "fifo":
                    os.mkfifo(metadata, 0o600)
                else:
                    metadata.write_text("x" * (4097 if kind == "oversize" else 20))
                with mock.patch.object(core, "_protected_read", wraps=core._protected_read) as read:
                    with self.assertRaisesRegex(core.SecretsError, "project-binding-required"):
                        core._file_named_fetch(request, time.monotonic())
                    self.assertEqual(len(read.call_args_list), 1)
                    self.assertFalse(read.call_args.kwargs["private"])
                metadata.unlink()
        self.assertEqual(self.calls, [])

    def test_schema_and_ambient_auth_refuse_before_file_or_provider(self):
        requests = []
        for key, value in (
            ("token", self.sample),
            ("writer", True),
            ("strict_records", False),
            ("require_finite_expiry", False),
        ):
            requests.append(dict(self.request(), **{key: value}))
        request = self.request()
        request["env"]["INFISICAL_TOKEN"] = self.sample
        requests.append(request)
        for value in (True, "29", float("inf")):
            requests.append(dict(self.request(), timeout=value))
        for request in requests:
            with (
                self.subTest(keys=list(request)),
                mock.patch.object(core, "_protected_read") as read,
            ):
                with self.assertRaises(core.UsageError):
                    core._file_named_fetch(request, time.monotonic())
                read.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_unsafe_bootstraps_refuse_without_content_read(self):
        unsafe = self.home / "unsafe"
        cases = (
            "symlink",
            "fifo",
            "hardlink",
            "mode",
            "oversize",
            "shared-parent",
            "sticky-parent",
            "retired",
        )
        for kind in cases:
            with self.subTest(kind=kind):
                selected = unsafe
                if kind == "symlink":
                    unsafe.symlink_to(self.binding)
                elif kind == "fifo":
                    os.mkfifo(unsafe, 0o600)
                elif kind == "hardlink":
                    os.link(self.binding, unsafe)
                elif kind in ("shared-parent", "sticky-parent", "retired"):
                    parent = self.home / (".secrets" if kind == "retired" else kind)
                    parent.mkdir()
                    selected = parent / "token"
                    selected.write_text(self.sample)
                    selected.chmod(0o600)
                    parent.chmod(
                        0o700
                        if kind == "retired"
                        else (0o1777 if kind == "sticky-parent" else 0o777)
                    )
                else:
                    unsafe.write_text("x" * 8193 if kind == "oversize" else self.sample)
                    unsafe.chmod(0o644 if kind == "mode" else 0o600)
                with (
                    mock.patch.object(
                        core.os, "read", side_effect=AssertionError("content must not be read")
                    ),
                    self.assertRaisesRegex(core.SecretsError, "bootstrap-file-refused"),
                ):
                    core._protected_read(str(selected), 8192, private=True)
                if unsafe.exists() or unsafe.is_symlink():
                    unsafe.unlink()
        self.assertEqual(self.calls, [])

    def test_read_only_file_and_retired_parent_alias(self):
        self.binding.chmod(0o400)
        self.assertTrue(self.run_worker()[0]["ok"])
        retired = self.home / ".secrets"
        retired.mkdir(mode=0o700)
        alias = self.home / "alias"
        alias.symlink_to(retired, target_is_directory=True)
        token = retired / "own-token"
        token.write_text(self.sample)
        token.chmod(0o600)
        with mock.patch.object(core.os, "read", side_effect=AssertionError("no retired content")):
            with self.assertRaisesRegex(core.SecretsError, "bootstrap-file-refused"):
                core._protected_read(str(alias / token.name), 8192, private=True)

    def test_changed_file_and_replaced_name_refuse_after_real_read(self):
        for replacement in (False, True):
            self.binding.write_text(self.sample)
            original = os.read
            changed = False

            def mutate(fd, size):
                nonlocal changed
                value = original(fd, size)
                if value and not changed:
                    changed = True
                    if replacement:
                        self.binding.unlink()
                    self.binding.write_text(self.sample + "changed")
                    self.binding.chmod(0o600)
                return value

            with mock.patch.object(core.os, "read", side_effect=mutate):
                with self.assertRaisesRegex(core.SecretsError, "bootstrap-file-refused"):
                    core._protected_read(str(self.binding), 8192, private=True)

    def test_raw_token_format_has_no_assignment_or_other_source_fallback(self):
        for text in (
            "export INFISICAL_TOKEN=" + self.sample,
            "'" + self.sample + "'",
            self.sample + "\nextra",
        ):
            self.binding.write_text(text)
            reply, code = self.run_worker()
            self.assertEqual((reply["reason"], code), ("bootstrap-file-refused", 4))
        self.assertEqual(self.calls, [])

    def test_file_operation_keeps_parent_deadline_and_reaps_trickling_worker(self):
        self.mode = "slow-body"
        request = self.request()
        request["timeout"] = 0.2
        started = time.monotonic()
        with self.assertRaises(core.SecretsError):
            core._bounded_read_request(request, started + 0.2)
        self.assertLess(time.monotonic() - started, 0.8)
        self.assertIsNotNone(self.children[-1].poll())


class DeclaredConsumerReaderTests(unittest.TestCase):
    """Real resolver, protected fixture store, canonical worker and loopback provider."""

    stop = BoundedReadTests.stop

    def setUp(self):
        BoundedReadTests.setUp(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.home.chmod(0o700)
        self.declaration = self.home / "consumer.json"
        self.store = self.home / "Library" / "Keychains" / "login.keychain-db"
        self.store.parent.mkdir(parents=True, mode=0o700)
        self.store.write_text(self.sample)
        self.store.chmod(0o600)
        self.alternate_keychain = self.home / "alternate.keychain-db"
        self.alternate_keychain.write_text(self.sample)
        self.alternate_keychain.chmod(0o600)
        self.selection = {
            "schemaVersion": 1,
            "consumer": "fixture-reader",
            "project": "fleet-ops",
            "projectId": self.project,
            "environment": "prod",
            "secretPath": "/operator-registry",
            "permissions": ["read"],
            "expiresAt": self.expiry,
            "tokenName": "consumer-ro",
            "tokenId": "fixture-record",
            "storage": {
                "kind": "mac-login-keychain",
                "service": "infisical-service-token",
                "account": "consumer-ro",
            },
        }
        self.write_declaration()
        metadata_dir = self.home / ".simple-tools"
        metadata_dir.mkdir(mode=0o700)
        self.project_file = metadata_dir / "fleet-ops-project-id"
        self.project_file.write_text(self.project)
        self.metadata_changes = {"id": "fixture-record", "name": "consumer-ro"}
        scripts = Path(core.__file__).parent
        spec = importlib.util.spec_from_file_location(
            "declared_scoped_reader", scripts / "infisical-scoped.py"
        )
        self.reader = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.reader)
        # Change only the provider origin in a private source fixture so the isolated
        # worker executes the canonical implementation without accepting an override.
        worker = self.home / "fleet_infisical.py"
        source = Path(core.__file__).read_text()
        origin_needle = 'API_BASE = "https://app.infisical.com/api"'
        self.assertEqual(
            source.count(origin_needle), 1, "loopback fixture must replace exactly one origin"
        )
        worker.write_text(
            source.replace(origin_needle, "API_BASE = " + repr(self.env[core.ENV_API]))
        )
        security = self.home / "security"
        security.write_text(
            "#!" + sys.executable + "\nimport sys\nfrom pathlib import Path\n"
            "args = sys.argv[1:]\n"
            "assert args[:6] == ['find-generic-password','-s','infisical-service-token','-a','consumer-ro','-w']\n"
            "assert len(args) in (6, 7)\n"
            "selected = Path(args[6]) if len(args) == 7 else Path(__file__).with_name('alternate.keychain-db')\n"
            "try:\n    sys.stdout.write(selected.read_text())\n"
            "except FileNotFoundError:\n    sys.exit(44)\n"
        )
        security.chmod(0o700)
        for patch in (
            mock.patch.dict(os.environ, {"HOME": str(self.home)}, clear=True),
            mock.patch.object(core, "_is_macos", return_value=True),
            mock.patch.object(core, "SECURITY_BIN", str(security)),
            mock.patch.object(core, "API_BASE", self.env[core.ENV_API]),
            mock.patch.object(
                core,
                "_bounded_read_command",
                return_value=[sys.executable, "-I", "-B", str(worker), "--bounded-read-worker"],
            ),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def write_declaration(self):
        self.declaration.write_text(json.dumps(self.selection))
        self.declaration.chmod(0o600)

    def run_reader(self, verb="get", extra=()):
        argv = ["infisical-scoped.py", verb]
        if verb == "get":
            argv.append("A_KEY")
        argv += [
            "--folder",
            "/operator-registry",
            "--consumer-binding",
            str(self.declaration),
            "--consumer",
            "fixture-reader",
            *extra,
        ]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.reader.main(argv)
        self.assertNotIn(self.sample, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def test_real_named_read_and_stateful_store_rotation_have_no_cached_fallback(self):
        code, out, err = self.run_reader()
        self.assertEqual((code, out, err), (0, "value-A_KEY\n", ""))
        self.assertEqual(len(self.calls), 2)
        self.store.write_text("st." + secrets.token_urlsafe(40))
        self.calls.clear()
        code, out, err = self.run_reader()
        self.assertEqual((code, out), (4, ""))
        self.assertEqual(len(self.calls), 1)

    def test_login_keychain_is_pinned_and_alternate_search_item_cannot_satisfy_it(self):
        prefix = [
            str(self.home / "security"),
            "find-generic-password",
            "-s",
            "infisical-service-token",
            "-a",
            "consumer-ro",
            "-w",
        ]
        # The synthetic search list really has a usable alternate item.  An
        # unpinned call would succeed, which makes the refusal below meaningful.
        unpinned = subprocess.run(
            prefix,
            capture_output=True,
            text=True,
            check=True,
            env={"PATH": os.defpath},
        )
        self.assertEqual(unpinned.stdout, self.sample)
        with mock.patch.object(core.subprocess, "Popen", wraps=core.subprocess.Popen) as calls:
            self.assertEqual(self.run_reader("names")[:2], (0, "A_KEY\n"))
        self.assertEqual(calls.call_args.args[0], [*prefix, str(self.store)])

        self.calls.clear()
        self.store.unlink()
        with mock.patch.object(core.subprocess, "Popen", wraps=core.subprocess.Popen) as calls:
            self.assertEqual(self.run_reader()[:2], (4, ""))
        self.assertEqual(calls.call_count, 1, "missing login item must not try another store")
        self.assertEqual(calls.call_args.args[0], [*prefix, str(self.store)])
        self.assertEqual(self.calls, [], "missing login item must precede provider access")

        self.store.write_text("st." + secrets.token_urlsafe(40))
        self.store.chmod(0o600)
        self.assertEqual(self.run_reader()[:2], (4, ""))
        self.assertEqual(len(self.calls), 1, "different login item must fail at token metadata")

    def test_names_and_doctor_are_strict_metadata_only(self):
        for verb in ("names", "doctor"):
            self.calls.clear()
            code, out, err = self.run_reader(verb)
            self.assertEqual((code, err), (0, ""))
            if verb == "names":
                self.assertEqual(out, "A_KEY\n")
            else:
                self.assertTrue(json.loads(out)["ok"])
                self.assertEqual(json.loads(out)["names_count"], 1)
            self.assertEqual(len(self.calls), 2)
            self.assertTrue(self.calls[-1].startswith("/api/v3/secrets/raw?"))
            self.assertIn("viewSecretValue=false", self.calls[-1])
            for field in ("workspace", "environment", "secretPath", "type"):
                self.omitted_fields = (field,)
                code, out, err = self.run_reader(verb)
                if verb == "doctor":
                    self.assertFalse(json.loads(out)["ok"])
                else:
                    self.assertEqual((code, out), (4, ""))
            self.omitted_fields = ()

    def test_live_metadata_identity_scope_expiry_refuse_before_values(self):
        for verb in ("get", "names", "doctor"):
            for changes in (
                {"id": "different"},
                {"name": "different"},
                {"_id": "different"},
                {"expiresAt": None},
                {"expiresAt": "2098-01-01T00:00:00Z"},
                {"workspace": str(uuid.uuid4())},
                {"permissions": ["read", "write"]},
            ):
                with self.subTest(verb=verb, changes=changes):
                    self.calls.clear()
                    self.metadata_changes = dict(id="fixture-record", name="consumer-ro")
                    self.metadata_changes.update(changes)
                    code, out, err = self.run_reader(verb)
                    if verb == "doctor":
                        self.assertFalse(json.loads(out)["ok"])
                    else:
                        self.assertEqual((code, out), (4, ""))
                    self.assertEqual(len(self.calls), 1)

    def test_declaration_and_resolver_mismatches_precede_store_and_provider(self):
        for field, value in (
            ("consumer", "another-reader"),
            ("project", "ai-fleet-coordinator"),
            ("projectId", str(uuid.uuid4())),
            ("environment", "staging"),
            ("secretPath", "/"),
            ("permissions", ["read", "write"]),
            ("expiresAt", None),
            ("expiresAt", "2000-01-01T00:00:00Z"),
            ("tokenName", "../consumer-ro"),
            ("tokenId", "bad\nidentifier"),
            ("tokenName", self.sample),
            ("expiresAt", "2099-01-01\n00:00:00Z"),
            ("schemaVersion", True),
            ("secretValue", "synthetic-value"),
            (
                "storage",
                {
                    "kind": "mac-login-keychain",
                    "service": "infisical-service-token",
                    "account": "other",
                },
            ),
        ):
            with self.subTest(field=field):
                original = dict(self.selection)
                self.selection[field] = value
                self.write_declaration()
                with mock.patch.object(
                    core, "token_from_store", side_effect=AssertionError("no store")
                ):
                    self.assertEqual(self.run_reader()[0], 4)
                self.selection = original
        self.write_declaration()
        self.project_file.write_text(str(uuid.uuid4()))
        with mock.patch.object(core, "token_from_store", side_effect=AssertionError("no store")):
            self.assertEqual(self.run_reader()[0], 4)
        self.assertEqual(self.calls, [])

    def test_conflicts_including_empty_markers_precede_custody(self):
        for key in (*core.AUTH_ENV_VARS, core.ENV_API, "INFISICAL_API_URL", core.ENV_READ_PATHS):
            with self.subTest(key=key), mock.patch.dict(os.environ, {key: ""}):
                with mock.patch.object(
                    core, "_protected_read", side_effect=AssertionError("no file")
                ):
                    self.assertEqual(self.run_reader()[0], 4)
        self.assertEqual(self.run_reader(extra=("--account", ""))[0], 4)
        self.assertEqual(self.calls, [])

    def test_unsafe_custody_duplicate_keys_and_unknown_storage_refuse_without_store(self):
        original = self.declaration.read_text()
        for kind in (
            "mode",
            "parent-mode",
            "hardlink",
            "oversize",
            "duplicate",
            "unknown-storage",
            "symlink",
            "symlink-parent",
            "fifo",
        ):
            with self.subTest(kind=kind):
                self.declaration.unlink()
                self.declaration.write_text(original)
                self.declaration.chmod(0o600)
                selected = self.declaration
                alias = self.home / "alias"
                if kind == "mode":
                    self.declaration.chmod(0o400)
                elif kind == "parent-mode":
                    self.home.chmod(0o755)
                elif kind == "hardlink":
                    os.link(self.declaration, alias)
                elif kind == "oversize":
                    self.declaration.write_text(" " * (16384 + 1))
                elif kind == "duplicate":
                    self.declaration.write_text(original[:-1] + ',"consumer":"fixture-reader"}')
                elif kind == "unknown-storage":
                    doc = json.loads(original)
                    doc["storage"]["value"] = "synthetic-value"
                    self.declaration.write_text(json.dumps(doc))
                elif kind == "symlink":
                    os.rename(self.declaration, alias)
                    self.declaration.symlink_to(alias)
                elif kind == "symlink-parent":
                    alias.symlink_to(self.home, target_is_directory=True)
                    selected = alias / self.declaration.name
                elif kind == "fifo":
                    self.declaration.unlink()
                    os.mkfifo(self.declaration, 0o600)
                with mock.patch.object(
                    core, "token_from_store", side_effect=AssertionError("no store")
                ):
                    saved = self.declaration
                    self.declaration = selected
                    self.assertEqual(self.run_reader()[0], 4)
                    self.declaration = saved
                if alias.exists() or alias.is_symlink():
                    alias.unlink()
                self.home.chmod(0o700)
        self.assertEqual(self.calls, [])

    def test_linux_declared_mac_store_has_no_file_or_ambient_fallback(self):
        with mock.patch.object(core, "_is_macos", return_value=False):
            code, out, err = self.run_reader()
        self.assertEqual((code, out), (4, ""))
        self.assertEqual(self.calls, [])

    def test_declared_named_read_refuses_wrong_row_before_output(self):
        for changes in (
            {"workspace": str(uuid.uuid4())},
            {"environment": "staging"},
            {"secretPath": "/"},
            {"type": "personal"},
            {"secretValueHidden": True},
            {"secretValue": ""},
        ):
            with self.subTest(changes=changes):
                self.record_changes = changes
                self.assertEqual(self.run_reader()[:2], (4, ""))
        self.record_changes = {}
        for field in ("workspace", "environment", "secretPath", "secretValueHidden"):
            self.omitted_fields = (field,)
            self.assertEqual(self.run_reader()[:2], (4, ""))

    def test_bounded_store_output_overflow_and_diagnostics_never_escape(self):
        security = self.home / "security"
        for body in (
            "sys.stdout.write('x' * 9000)",
            "sys.stderr.write('private-diagnostic');sys.exit(1)",
            "sys.stdout.write('private-diagnostic');sys.exit(1)",
        ):
            security.write_text("#!" + sys.executable + "\nimport sys\n" + body + "\n")
            code, out, err = self.run_reader()
            self.assertEqual((code, out), (4, ""))
            self.assertNotIn("private-diagnostic", err)
        self.assertEqual(self.calls, [])

    def test_declared_named_response_over_eight_kib_is_refused_without_output(self):
        self.record_changes = {"secretValue": "fixture-value-" * 1024}
        code, out, err = self.run_reader()
        self.assertEqual((code, out), (4, ""))
        self.assertNotIn("fixture-value-", err)
        self.assertEqual(len(self.calls), 2)

    def test_real_hung_store_is_bounded_and_reaped(self):
        security = self.home / "security"
        security.write_text("#!" + sys.executable + "\nimport time\ntime.sleep(30)\n")
        started = time.monotonic()
        code, out, err = self.run_reader()
        self.assertEqual((code, out), (124, ""))
        self.assertLess(time.monotonic() - started, 5.5)
        self.assertIsNotNone(self.children[-1].poll())
        self.assertEqual(self.calls, [])
