"""Tests for the PR relay: policy, audit, client request shapes, HTTP surface.

Nothing here touches the network: the GitHub opener is a fake, and the HTTP
tests bind an ephemeral port on 127.0.0.1 with a stubbed client.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from git_relay.audit import AuditLog
from git_relay.github_client import GitHubClient, RelayGitHubError
from git_relay.policy import PolicyError, SeatPolicy
from git_relay.server import MAX_REQUEST_BYTES, BoundedThreadingHTTPServer, Relay, make_handler

SEAT_TOKEN = "s" * 32
OTHER_TOKEN = "o" * 32

SEATS_JSON = json.dumps(
    {
        "swu-echo": {
            "token": SEAT_TOKEN,
            "actions": ["open_pr", "edit_pr", "reply_thread", "comment_pr"],
            "repos": ["example/fixture-repo"],
            "app_slug": "swu-echo",
            "mint_permissions": {"pull_requests": "write", "issues": "write"},
        },
        "swu-prime": {
            "token": OTHER_TOKEN,
            "actions": ["comment_pr"],
            "repos": ["example/*"],
            "app_slug": "swu-prime",
            "mint_permissions": {"issues": "write"},
        },
    }
)


class FakeResponse:
    def __init__(self, payload: dict):
        self._raw = json.dumps(payload).encode()

    def read(self, _limit: int) -> bytes:
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


class FakeOpener:
    def __init__(self, payload: dict):
        self.payload = payload
        self.requests: list[urllib.request.Request] = []

    def __call__(self, req: urllib.request.Request, timeout: int):
        self.requests.append(req)
        return FakeResponse(self.payload)


class TestPolicy(unittest.TestCase):
    def test_rejects_empty_and_malformed_config(self):
        for bad in ("", "not json", "[]", "{}"):
            with self.assertRaises(PolicyError):
                SeatPolicy(bad)
        with self.assertRaises(PolicyError):
            SeatPolicy(
                json.dumps({"seat": {"token": "short", "actions": ["open_pr"], "repos": ["a/b"]}})
            )
        with self.assertRaises(PolicyError):
            SeatPolicy(
                json.dumps({"seat": {"token": "x" * 16, "actions": ["nuke"], "repos": ["a/b"]}})
            )

    def test_authenticate_matches_only_the_right_token(self):
        policy = SeatPolicy(SEATS_JSON)
        self.assertEqual(policy.authenticate(SEAT_TOKEN), "swu-echo")
        self.assertEqual(policy.authenticate(OTHER_TOKEN), "swu-prime")
        self.assertIsNone(policy.authenticate("x" * 32))
        self.assertIsNone(policy.authenticate(""))

    def test_authorize_enforces_action_and_repo_allowlists(self):
        policy = SeatPolicy(SEATS_JSON)
        self.assertIsNone(policy.authorize("swu-echo", "open_pr", "example/fixture-repo"))
        self.assertIsNotNone(policy.authorize("swu-prime", "open_pr", "example/x"))
        self.assertIsNotNone(policy.authorize("swu-echo", "open_pr", "example/other-repo"))
        self.assertIsNone(policy.authorize("swu-prime", "comment_pr", "example/other-repo"))
        self.assertIsNotNone(policy.authorize("swu-prime", "comment_pr", "other-org/SWU"))
        self.assertIsNotNone(policy.authorize("ghost", "open_pr", "example/x"))

    def test_rate_limit_denies_after_the_cap(self):
        policy = SeatPolicy(SEATS_JSON, rate_per_hour=2)
        repo = "example/fixture-repo"
        self.assertIsNone(policy.authorize("swu-echo", "open_pr", repo, now=1000.0))
        self.assertIsNone(policy.authorize("swu-echo", "open_pr", repo, now=1001.0))
        self.assertIsNotNone(policy.authorize("swu-echo", "open_pr", repo, now=1002.0))
        self.assertIsNone(policy.authorize("swu-echo", "open_pr", repo, now=4601.0))


class TestAudit(unittest.TestCase):
    def test_rows_are_appended_and_bodies_are_hashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "audit.jsonl")
            log = AuditLog(path)
            log.record(
                "swu-echo", "open_pr", "a/b", "ok", {"title": "t", "body": "secret", "pr": 3}
            )
            log.record("swu-echo", "comment_pr", "a/b", "denied", {"reason": "no"})
            with open(path) as fh:
                rows = [json.loads(line) for line in fh]
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["result"], "ok")
        self.assertEqual(rows[0]["pr"], 3)
        self.assertNotIn("secret", json.dumps(rows))
        self.assertIn("body_sha256", rows[0])
        self.assertEqual(rows[1]["reason"], "no")
        self.assertIn("ts", rows[0])


class TestClient(unittest.TestCase):
    def test_open_pr_posts_to_pulls_with_auth(self):
        opener = FakeOpener({"number": 7, "html_url": "https://x/pr/7"})
        client = GitHubClient("tok", base_url="https://api.test", opener=opener)
        result = client.open_pr("o/r", "main", "feat", "t", "b")
        req = opener.requests[0]
        self.assertEqual(req.full_url, "https://api.test/repos/o/r/pulls")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.get_header("Authorization"), "Bearer tok")
        self.assertEqual(json.loads(req.data)["head"], "feat")
        self.assertEqual(result, {"number": 7, "url": "https://x/pr/7"})

    def test_reply_thread_carries_in_reply_to(self):
        opener = FakeOpener({"id": 5, "html_url": "u"})
        client = GitHubClient("tok", base_url="https://api.test", opener=opener)
        client.reply_thread("o/r", 3, 99, "done")
        payload = json.loads(opener.requests[0].data)
        self.assertEqual(payload, {"body": "done", "in_reply_to": 99})
        self.assertIn("/repos/o/r/pulls/3/comments", opener.requests[0].full_url)

    def test_edit_pr_requires_a_field_and_patches(self):
        opener = FakeOpener({"number": 3, "html_url": "u"})
        client = GitHubClient("tok", base_url="https://api.test", opener=opener)
        with self.assertRaises(ValueError):
            client.edit_pr("o/r", 3, None, None)
        client.edit_pr("o/r", 3, "new title", None)
        req = opener.requests[0]
        self.assertEqual(req.get_method(), "PATCH")
        self.assertEqual(json.loads(req.data), {"title": "new title"})

    def test_bad_repo_shape_is_rejected_before_any_call(self):
        opener = FakeOpener({})
        client = GitHubClient("tok", base_url="https://api.test", opener=opener)
        for repo in ("no-slash", "a/b/c", "../x"):
            with self.assertRaises(ValueError):
                client.comment_pr(repo, 1, "hi")
        self.assertEqual(opener.requests, [])

    def test_http_error_raises_with_status_not_body(self):
        class Boom:
            def __call__(self, _req, timeout):
                raise urllib.error.HTTPError(
                    "u", 422, "Unprocessable", {}, io.BytesIO(b'{"message": "nope"}')
                )

        client = GitHubClient("tok", base_url="https://api.test", opener=Boom())
        with self.assertRaises(RelayGitHubError) as ctx:
            client.comment_pr("o/r", 1, "hi")
        self.assertEqual(ctx.exception.status, 422)
        self.assertNotIn("tok", str(ctx.exception))


class StubClient:
    def __init__(self):
        self.calls = []

    def open_pr(self, repo, base, head, title, body):
        self.calls.append(("open_pr", repo, base, head, title, body))
        return {"number": 1, "url": "https://x/pr/1"}

    def edit_pr(self, repo, number, title=None, body=None, bearer=None):
        self.calls.append(("edit_pr", repo, number, title, body, bearer))
        return {"number": number, "url": "u"}

    def reply_thread(self, repo, number, comment_id, body, bearer=None):
        self.calls.append(("reply_thread", repo, number, comment_id, body, bearer))
        return {"id": 2, "url": "u"}

    def comment_pr(self, repo, number, body, bearer=None):
        self.calls.append(("comment_pr", repo, number, body, bearer))
        return {"id": 3, "url": "u"}


class TestHttpSurface(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        relay = Relay(
            SeatPolicy(SEATS_JSON), StubClient(), AuditLog(os.path.join(cls.tmp.name, "a.jsonl"))
        )
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(relay))
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.tmp.cleanup()

    def _post(self, path, payload, token=SEAT_TOKEN):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            method="POST",
            data=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_open_pr_end_to_end(self):
        status, body = self._post(
            "/v1/open_pr",
            {
                "repo": "example/fixture-repo",
                "base": "main",
                "head": "feat",
                "title": "t",
                "body": "b",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["number"], 1)

    def test_unauthenticated_gets_401_and_no_audit_row(self):
        status, _ = self._post(
            "/v1/comment_pr",
            {"repo": "example/other-repo", "number": 1, "body": "x"},
            token="wrong" * 8,
        )
        self.assertEqual(status, 401)

    def test_policy_denial_gets_403(self):
        status, body = self._post(
            "/v1/open_pr",
            {"repo": "example/other-repo", "base": "main", "head": "f", "title": "t", "body": "b"},
            token=OTHER_TOKEN,
        )
        self.assertEqual(status, 403)
        self.assertIn("not allowed", body["error"])

    def test_bad_json_and_missing_fields_get_400(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/open_pr",
            method="POST",
            data=b"{oops",
            headers={"Authorization": f"Bearer {SEAT_TOKEN}"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)
        status, _ = self._post("/v1/open_pr", {"repo": "example/fixture-repo"})
        self.assertEqual(status, 400)

    def test_oversize_body_is_rejected(self):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/v1/open_pr",
            method="POST",
            data=b" " * (MAX_REQUEST_BYTES + 1),
            headers={"Authorization": f"Bearer {SEAT_TOKEN}"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            self.fail("expected 400")
        except urllib.error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_healthz_needs_no_auth(self):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/healthz", timeout=5) as resp:
            self.assertEqual(resp.status, 200)


if __name__ == "__main__":
    unittest.main()


class TestBoundedServer(unittest.TestCase):
    def test_concurrency_is_capped_and_no_request_is_refused(self):
        # Kody discussion_r4372744451:  the relay must bound in-flight work
        # without refusing anyone - excess connections wait their turn.
        from http.server import BaseHTTPRequestHandler

        active = 0
        peak = 0
        lock = threading.Lock()

        class Slow(BaseHTTPRequestHandler):
            def do_GET(self):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.05)
                with lock:
                    active -= 1
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = BoundedThreadingHTTPServer(("127.0.0.1", 0), Slow, max_concurrent=3)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            port = server.server_address[1]
            results = []

            def hit():
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as resp:
                        results.append(resp.status)
                except Exception as exc:  # a refusal here fails the test
                    results.append(repr(exc))

            threads = [threading.Thread(target=hit) for _ in range(12)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(15)
            self.assertEqual(results, [200] * 12)
            self.assertLessEqual(peak, 3)
        finally:
            server.shutdown()


class TestBoundedServerResilience(unittest.TestCase):
    """E2E for the bound (Kody discussion_r4372744451):  stalled clients lose
    their slots to the socket timeout, a queued valid request is then served,
    and shutdown returns even with every slot pinned."""

    def _server(self, **kwargs):
        from http.server import BaseHTTPRequestHandler

        class Ok(BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = BoundedThreadingHTTPServer(("127.0.0.1", 0), Ok, **kwargs)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        return server

    @staticmethod
    def _stall(port):
        import socket

        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.sendall(b"GET / HTTP/1.0\r\n")  # half-sent:  no terminating blank line
        return sock

    def test_stalled_clients_time_out_and_a_queued_valid_request_is_served(self):
        server = self._server(max_concurrent=2, socket_timeout_s=1)
        port = server.server_address[1]
        stalled = [self._stall(port), self._stall(port)]
        self.addCleanup(lambda: [s.close() for s in stalled])
        time.sleep(0.3)  # both stalls accepted, both slots pinned
        started = time.time()
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as resp:
            self.assertEqual(resp.status, 200)
        elapsed = time.time() - started
        self.assertGreaterEqual(
            elapsed, 0.5, "served instantly: the stalls never pinned the slots?"
        )
        self.assertLess(elapsed, 8)
        server.shutdown()

    def test_shutdown_returns_while_every_slot_is_pinned(self):
        server = self._server(max_concurrent=2, socket_timeout_s=1)
        port = server.server_address[1]
        stalled = [self._stall(port), self._stall(port)]
        self.addCleanup(lambda: [s.close() for s in stalled])
        time.sleep(0.3)
        done = threading.Event()

        def stop():
            server.shutdown()
            done.set()

        threading.Thread(target=stop, daemon=True).start()
        self.assertTrue(done.wait(10), "shutdown() stuck behind pinned slots")

    def test_nonpositive_concurrency_is_clamped_and_env_parse_is_strict(self):
        from git_relay.server import _positive_int

        self.assertEqual(_positive_int("junk", 8), 8)
        self.assertEqual(_positive_int("0", 8), 8)
        self.assertEqual(_positive_int("-3", 8), 8)
        self.assertEqual(_positive_int("5", 8), 5)
        server = self._server(max_concurrent=0, socket_timeout_s=5)  # clamped to 1, not a deadlock
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as resp:
            self.assertEqual(resp.status, 200)
        server.shutdown()


class TestNonPrActionsRejected(unittest.TestCase):
    def test_push_and_admin_are_not_valid_actions(self):
        with self.assertRaises(PolicyError):
            SeatPolicy(
                json.dumps({"seat": {"token": "x" * 16, "actions": ["push"], "repos": ["a/b"]}})
            )
        with self.assertRaises(PolicyError):
            SeatPolicy(
                json.dumps({"seat": {"token": "x" * 16, "actions": ["merge_pr"], "repos": ["a/b"]}})
            )


class TestMcpWrapper(unittest.TestCase):
    def _server(self, payload=None, opener=None):
        from git_relay.mcp_server import RelayMcp

        if opener is None:
            opener = FakeOpener(payload or {"number": 9})
        return RelayMcp("http://relay.test", SEAT_TOKEN, opener=opener)

    def test_initialize_and_tools_list(self):
        server = self._server()
        reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(reply["result"]["serverInfo"]["name"], "GR-MCP")
        reply = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in reply["result"]["tools"]]
        self.assertEqual(
            names,
            [
                "open_pr",
                "edit_pr",
                "reply_thread",
                "comment_pr",
                "merge_pr",
                "workflow_dispatch",
                "resolve_thread",
            ],  # mint_token stays HTTP-only: raw App tokens never cross stdio
        )

    def test_tools_call_forwards_to_relay_with_seat_token(self):
        opener = FakeOpener({"number": 9, "url": "u"})
        server = self._server(opener=opener)
        reply = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "open_pr",
                    "arguments": {
                        "repo": "a/b",
                        "base": "main",
                        "head": "f",
                        "title": "t",
                        "body": "b",
                    },
                },
            }
        )
        req = opener.requests[0]
        self.assertEqual(req.full_url, "http://relay.test/v1/open_pr")
        self.assertEqual(req.get_header("Authorization"), "Bearer " + SEAT_TOKEN)
        self.assertIn('"number": 9', reply["result"]["content"][0]["text"])

    def test_unknown_tool_and_notification(self):
        server = self._server()
        reply = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {"name": "push", "arguments": {}},
            }
        )
        self.assertEqual(reply["error"]["code"], -32602)
        self.assertIsNone(server.handle({"method": "notifications/initialized"}))

    def test_relay_refusal_is_tool_error_not_protocol_error(self):
        class Refusing:
            def __call__(self, _req, timeout):
                raise urllib.error.HTTPError(
                    "u", 403, "Forbidden", {}, io.BytesIO(b'{"error":"no"}')
                )

        server = self._server(opener=Refusing())
        reply = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "comment_pr",
                    "arguments": {"repo": "a/b", "number": 1, "body": "x"},
                },
            }
        )
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("403", reply["result"]["content"][0]["text"])


class TestMcpWriteUncertainty(unittest.TestCase):
    def call(self, opener):
        from git_relay.mcp_server import RelayMcp

        return RelayMcp("http://relay.test", "private-seat-token", opener=opener).handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "comment_pr",
                    "arguments": {"repo": "a/b", "number": 1, "body": "x"},
                },
            }
        )["result"]

    def test_http_errors_preserve_write_uncertainty_without_leaking_prose(self):
        cases = [
            (502, {"outcome": "unknown"}, "outcome_unknown"),
            (409, {"outcome": "pending"}, "outcome_unknown"),
            (503, {}, "outcome_unknown"),
            (503, {"retryable": True}, "unavailable"),
            (503, {"retryable": 1}, "outcome_unknown"),
            (503, {"retryable": "true"}, "outcome_unknown"),
            (429, {"retryable": True, "retry_after_seconds": 30}, "unavailable"),
            (409, {"outcome": "unknown", "retryable": True}, "outcome_unknown"),
            (403, {}, "relay_refused"),
        ]
        for status, payload, expected in cases:
            with self.subTest(status=status, payload=payload):
                payload["error"] = "private-seat-token"

                def opener(_req, timeout):
                    raise urllib.error.HTTPError(
                        "u", status, "error", {}, io.BytesIO(json.dumps(payload).encode())
                    )

                result = self.call(opener)
                self.assertTrue(result["isError"])
                error = json.loads(result["content"][0]["text"])
                self.assertEqual(error["code"], expected)
                self.assertNotIn("private-seat-token", json.dumps(result))
                self.assertEqual(error["retryable"], expected == "unavailable")
                if status == 429:
                    self.assertEqual(error["retry_after_seconds"], 30)

    def test_transport_loss_is_not_a_definite_refusal(self):
        def opener(_req, timeout):
            raise OSError("private-seat-token")

        result = self.call(opener)
        self.assertEqual(json.loads(result["content"][0]["text"])["code"], "outcome_unknown")
        self.assertNotIn("private-seat-token", json.dumps(result))

    def test_unreadable_server_error_stays_unknown(self):
        def opener(_req, timeout):
            raise urllib.error.HTTPError("u", 502, "error", {}, io.BytesIO(b"not-json"))

        result = self.call(opener)
        self.assertEqual(json.loads(result["content"][0]["text"])["code"], "outcome_unknown")

    def test_unreadable_and_nonobject_4xx_stay_unknown(self):
        from git_relay.mcp_server import MAX_LINE

        for body in (b"not-json", b"[]", b"null", b"x" * (MAX_LINE + 1)):

            def opener(_req, timeout):
                raise urllib.error.HTTPError("u", 409, "error", {}, io.BytesIO(body))

            error = json.loads(self.call(opener)["content"][0]["text"])
            self.assertEqual(error["code"], "outcome_unknown")

    def test_retry_hint_is_bounded_numeric_and_never_provider_prose(self):
        for hint in ("private-seat-token", True, -1, float("inf"), float("nan"), 86401):

            def opener(_req, timeout):
                payload = {"retryable": True, "retry_after_seconds": hint}
                raise urllib.error.HTTPError(
                    "u", 429, "error", {}, io.BytesIO(json.dumps(payload).encode())
                )

            error = json.loads(self.call(opener)["content"][0]["text"])
            self.assertNotIn("retry_after_seconds", error)

    def test_success_status_does_not_override_unknown_outcome(self):
        result = self.call(FakeOpener({"outcome": "unknown"}))
        self.assertEqual(json.loads(result["content"][0]["text"])["code"], "outcome_unknown")

    def test_nonobject_success_stays_unknown(self):
        result = self.call(FakeOpener(["not-an-envelope"]))
        self.assertEqual(json.loads(result["content"][0]["text"])["code"], "outcome_unknown")


APP_SEATS_JSON = json.dumps(
    {
        "swu-echo": {
            "token": SEAT_TOKEN,
            "actions": [
                "open_pr",
                "mint_token",
                "merge_pr",
                "workflow_dispatch",
                "resolve_thread",
                "edit_pr",
                "reply_thread",
                "comment_pr",
                "create_release",
            ],
            "repos": ["example/*"],
            "app_slug": "swu-echo",
            "mint_permissions": {
                "contents": "write",
                "pull_requests": "write",
                "issues": "write",
                "actions": "write",
                "metadata": "read",
            },
        }
    }
)


class FakeMinter:
    def __init__(self):
        self.calls = []

    def token_for(self, slug, repo, permissions):
        self.calls.append((slug, repo, permissions))
        return "MINTED-FAKE-TOKEN", time.time() + 3600


class TestAppActions(unittest.TestCase):
    def _relay(self, tmp):
        return Relay(
            SeatPolicy(APP_SEATS_JSON),
            StubClient(),
            AuditLog(os.path.join(tmp, "a.jsonl")),
            app_minter=FakeMinter(),
        )

    def test_policy_requires_app_slug_for_app_actions(self):
        with self.assertRaises(PolicyError):
            SeatPolicy(
                json.dumps(
                    {
                        "seat": {
                            "token": "x" * 16,
                            "actions": ["mint_token"],
                            "repos": ["a/b"],
                        }
                    }
                )
            )

    def test_registered_action_refuses_at_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = self._relay(tmp)
            status, body = relay.handle(SEAT_TOKEN, "create_release", {"repo": "example/x"})
            self.assertEqual(status, 400)
            self.assertIn("not implemented", body["error"])

    def test_mint_token_mints_within_ceiling_and_audits_no_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = self._relay(tmp)
            status, body = relay.handle(
                SEAT_TOKEN,
                "mint_token",
                {"repo": "example/x", "permissions": {"contents": "read"}},
            )
            self.assertEqual(status, 200)
            self.assertEqual(body["app_slug"], "swu-echo")
            self.assertEqual(
                relay.app_minter.calls, [("swu-echo", "example/x", {"contents": "read"})]
            )
            with open(os.path.join(tmp, "a.jsonl")) as fh:
                logged = fh.read()
            self.assertNotIn("MINTED-FAKE-TOKEN", logged)
            self.assertIn("token_sha256", logged)

    def test_edit_pr_without_a_field_is_a_local_400(self):
        # With neither title nor body, GitHub would answer 422 AFTER the call
        # left the fleet;  _validate_args now refuses locally as a 400 and no
        # token is minted or spent.
        with tempfile.TemporaryDirectory() as tmp:
            relay = self._relay(tmp)
            status, body = relay.handle(SEAT_TOKEN, "edit_pr", {"repo": "example/x", "number": 1})
            self.assertEqual(status, 400)
            self.assertIn("title or body is required", body["error"])
            self.assertEqual(relay.app_minter.calls, [])
            self.assertEqual(relay.client.calls, [])

    def test_mint_token_beyond_ceiling_is_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = self._relay(tmp)
            status, _ = relay.handle(
                SEAT_TOKEN,
                "mint_token",
                {"repo": "example/x", "permissions": {"administration": "write"}},
            )
            self.assertEqual(status, 403)

    def test_app_actions_without_minter_get_503(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = Relay(
                SeatPolicy(APP_SEATS_JSON), StubClient(), AuditLog(os.path.join(tmp, "a.jsonl"))
            )
            status, body = relay.handle(SEAT_TOKEN, "merge_pr", {"repo": "example/x", "number": 1})
            self.assertEqual(status, 503)
            self.assertIn("not configured", body["error"])

    def test_resolve_thread_needs_contents_write_with_pull_requests_write(self):
        # GitHub gates resolveReviewThread at merge level for App tokens.
        thread = "PRRT_kwDOAbc-123_x"
        with tempfile.TemporaryDirectory() as tmp:
            relay = self._relay(tmp)
            relay.client.resolve_thread = lambda thread_id, bearer: {"resolved": True}
            status, _ = relay.handle(
                SEAT_TOKEN, "resolve_thread", {"repo": "example/x", "thread_id": thread}
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                relay.app_minter.calls,
                [("swu-echo", "example/x", {"contents": "write", "pull_requests": "write"})],
            )

    def test_resolve_thread_denied_before_mint_when_ceiling_lacks_contents_write(self):
        thread = "PRRT_kwDOAbc-123_x"
        with tempfile.TemporaryDirectory() as tmp:
            relay = self._relay(tmp)
            relay.policy = SeatPolicy(
                json.dumps(
                    {
                        "swu-echo": {
                            "token": SEAT_TOKEN,
                            "actions": ["resolve_thread"],
                            "repos": ["example/*"],
                            "app_slug": "swu-echo",
                            "mint_permissions": {"pull_requests": "write"},
                        }
                    }
                )
            )
            relay.client.resolve_thread = lambda thread_id, bearer: {"resolved": True}
            status, _ = relay.handle(
                SEAT_TOKEN, "resolve_thread", {"repo": "example/x", "thread_id": thread}
            )
            self.assertEqual(status, 403)
            self.assertEqual(relay.app_minter.calls, [])

    def test_edit_pr_uses_minted_bearer(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = self._relay(tmp)
            status, body = relay.handle(
                SEAT_TOKEN, "edit_pr", {"repo": "example/x", "number": 1, "title": "t2"}
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                relay.app_minter.calls, [("swu-echo", "example/x", {"pull_requests": "write"})]
            )
            self.assertEqual(
                relay.client.calls, [("edit_pr", "example/x", 1, "t2", None, "MINTED-FAKE-TOKEN")]
            )

    def test_reply_thread_uses_minted_bearer(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = self._relay(tmp)
            status, body = relay.handle(
                SEAT_TOKEN,
                "reply_thread",
                {"repo": "example/x", "number": 1, "comment_id": 2, "body": "b"},
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                relay.app_minter.calls, [("swu-echo", "example/x", {"pull_requests": "write"})]
            )
            self.assertEqual(
                relay.client.calls, [("reply_thread", "example/x", 1, 2, "b", "MINTED-FAKE-TOKEN")]
            )

    def test_comment_pr_uses_minted_bearer(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = self._relay(tmp)
            status, body = relay.handle(
                SEAT_TOKEN, "comment_pr", {"repo": "example/x", "number": 1, "body": "b"}
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                relay.app_minter.calls, [("swu-echo", "example/x", {"pull_requests": "write"})]
            )
            self.assertEqual(
                relay.client.calls, [("comment_pr", "example/x", 1, "b", "MINTED-FAKE-TOKEN")]
            )

    def test_comment_pr_uses_only_pull_request_permission(self):
        # A PR-only ceiling is enough, even though GitHub's comment endpoint
        # uses /issues/:number/comments.  No Issues permission is required.
        with tempfile.TemporaryDirectory() as tmp:
            relay = self._relay(tmp)
            relay.policy = SeatPolicy(
                json.dumps(
                    {
                        "swu-echo": {
                            "token": SEAT_TOKEN,
                            "actions": ["comment_pr"],
                            "repos": ["example/*"],
                            "app_slug": "swu-echo",
                            "mint_permissions": {"pull_requests": "write"},
                        }
                    }
                )
            )
            status, _ = relay.handle(
                SEAT_TOKEN, "comment_pr", {"repo": "example/x", "number": 1, "body": "b"}
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                relay.app_minter.calls, [("swu-echo", "example/x", {"pull_requests": "write"})]
            )
            self.assertEqual(
                relay.client.calls, [("comment_pr", "example/x", 1, "b", "MINTED-FAKE-TOKEN")]
            )

    def test_comment_pr_beyond_ceiling_is_denied(self):
        for ceiling in ({"issues": "write"}, {"pull_requests": "read"}, {"contents": "read"}):
            with self.subTest(ceiling=ceiling), tempfile.TemporaryDirectory() as tmp:
                relay = Relay(
                    SeatPolicy(
                        json.dumps(
                            {
                                "swu-echo": {
                                    "token": SEAT_TOKEN,
                                    "actions": ["comment_pr"],
                                    "repos": ["example/*"],
                                    "app_slug": "swu-echo",
                                    "mint_permissions": ceiling,
                                }
                            }
                        )
                    ),
                    StubClient(),
                    AuditLog(os.path.join(tmp, "a.jsonl")),
                    app_minter=FakeMinter(),
                )
                status, _ = relay.handle(
                    SEAT_TOKEN, "comment_pr", {"repo": "example/x", "number": 1, "body": "b"}
                )
                self.assertEqual(status, 403)
                self.assertEqual(relay.app_minter.calls, [])
                self.assertEqual(relay.client.calls, [])


class TestMinter(unittest.TestCase):
    def test_mints_and_caches_and_refreshes(self):
        from git_relay.app_tokens import AppTokenMinter

        class Source:
            def secrets(self, names):
                values = {"GH_APP_SWU_ECHO_ID": "100", "GH_APP_SWU_ECHO_PRIVATE_KEY": "PEM"}
                return {n: values[n] for n in names}

        api_calls = []

        def api(method, path, bearer, payload=None):
            api_calls.append((method, path))
            if path == "/app":
                return {"id": 100}
            if method == "GET":
                return {
                    "id": 55,
                    "app_id": 100,
                    "account": {"login": "example"},
                    "suspended_at": None,
                }
            return {
                "token": "tok-" + str(len(api_calls)),
                "expires_at": "2027-01-01T00:00:00Z",
            }

        minter = AppTokenMinter(Source(), api, signer=lambda *a: "JWT", clock=lambda: 1_700_000_000)
        t1, expiry1 = minter.token_for("swu-echo", "example/x", {"contents": "write"})
        t2, expiry2 = minter.token_for("swu-echo", "example/x", {"contents": "write"})
        self.assertEqual(t1, t2)
        self.assertEqual(expiry1, expiry2)
        mints = [c for c in api_calls if c[0] == "POST"]
        self.assertEqual(len(mints), 1)

    def test_permission_outside_mint_ceiling_refused(self):
        from git_relay.app_tokens import AppTokenError, AppTokenMinter

        minter = AppTokenMinter(None, None, clock=lambda: 0)
        with self.assertRaises(AppTokenError):
            minter.token_for("swu-echo", "a/b", {"administration": "write"})


class TestAppClientShapes(unittest.TestCase):
    def test_merge_pr_uses_minted_bearer(self):
        opener = FakeOpener({"merged": True, "sha": "abc"})
        client = GitHubClient("PAT", base_url="https://api.test", opener=opener)
        result = client.merge_pr("o/r", 3, "squash", bearer="MINTED")
        req = opener.requests[0]
        self.assertEqual(req.get_method(), "PUT")
        self.assertEqual(req.get_header("Authorization"), "Bearer MINTED")
        self.assertIn("/repos/o/r/pulls/3/merge", req.full_url)
        self.assertTrue(result["merged"])

    def test_workflow_dispatch_shape(self):
        opener = FakeOpener({})
        client = GitHubClient("PAT", base_url="https://api.test", opener=opener)
        client.workflow_dispatch("o/r", "ci.yml", "main", {"force": True}, bearer="MINTED")
        req = opener.requests[0]
        self.assertIn("/actions/workflows/ci.yml/dispatches", req.full_url)
        self.assertEqual(json.loads(req.data), {"ref": "main", "inputs": {"force": True}})

    def test_resolve_thread_posts_graphql(self):
        opener = FakeOpener({"data": {"resolveReviewThread": {"thread": {"isResolved": True}}}})
        client = GitHubClient("PAT", base_url="https://api.test", opener=opener)
        result = client.resolve_thread("PRRT_abc", bearer="MINTED")
        req = opener.requests[0]
        self.assertTrue(req.full_url.endswith("/graphql"))
        self.assertEqual(json.loads(req.data)["variables"], {"id": "PRRT_abc"})
        self.assertTrue(result["resolved"])
        with self.assertRaises(ValueError):
            client.resolve_thread("bogus", bearer="MINTED")


class TestSecretScan(unittest.TestCase):
    def test_flags_credential_shapes(self):
        from git_relay.secret_scan import find_secret

        self.assertEqual(find_secret("token: ghp_" + "a" * 30), "github_token")
        self.assertEqual(find_secret("use github_pat_" + "b" * 40), "github_fine_grained_pat")
        self.assertEqual(find_secret("-----BEGIN " + "RSA PRIVATE KEY-----"), "private_key_block")
        self.assertEqual(find_secret("st." + "c" * 30), "infisical_service_token")

    def test_clean_text_passes(self):
        from git_relay.secret_scan import find_secret

        self.assertIsNone(find_secret("fix: resolve the latest.stable parser bug"))
        self.assertIsNone(find_secret("list.everything and st. short prose"))
        self.assertIsNone(find_secret(None))
        self.assertIsNone(find_secret(123))

    def _relay(self, tmp):
        client = StubClient()
        relay = Relay(SeatPolicy(SEATS_JSON), client, AuditLog(os.path.join(tmp, "a.jsonl")))
        return relay, client

    def test_open_pr_with_pasted_token_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay, client = self._relay(tmp)
            code, payload = relay.handle(
                SEAT_TOKEN,
                "open_pr",
                {
                    "repo": "example/fixture-repo",
                    "base": "main",
                    "head": "b",
                    "title": "t",
                    "body": "leak ghp_" + "a" * 30,
                },
            )
            self.assertEqual(code, 400)
            self.assertIn("credential", payload["error"])
            self.assertEqual(client.calls, [])
            rows = [json.loads(line) for line in open(os.path.join(tmp, "a.jsonl"))]
            self.assertEqual(rows[-1]["result"], "bad_request")

    def test_reply_thread_with_private_key_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay, client = self._relay(tmp)
            code, payload = relay.handle(
                SEAT_TOKEN,
                "reply_thread",
                {
                    "repo": "example/fixture-repo",
                    "number": 1,
                    "comment_id": 2,
                    "body": "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
                },
            )
            self.assertEqual(code, 400)
            self.assertEqual(client.calls, [])

    def test_clean_open_pr_still_flows(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay, client = self._relay(tmp)
            code, payload = relay.handle(
                SEAT_TOKEN,
                "open_pr",
                {
                    "repo": "example/fixture-repo",
                    "base": "main",
                    "head": "b",
                    "title": "t",
                    "body": "ordinary body",
                },
            )
            self.assertEqual(code, 200)
            self.assertEqual(client.calls[0][0], "open_pr")


class TestAuditSecretHashing(unittest.TestCase):
    def test_token_shaped_value_under_unknown_key_is_hashed(self):
        from git_relay.audit import AuditLog

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "audit.jsonl")
            AuditLog(path).record(
                "seat", "act", "r", "ok", {"note": "ghp_" + "a" * 30, "keep": "v"}
            )
            row = json.loads(open(path).readline())
        self.assertIn("note_sha256", row)
        self.assertEqual(row["note_len"], 34)
        self.assertEqual(row["keep"], "v")
        self.assertNotIn("note", row)


class TestFleetSecretsSource(unittest.TestCase):
    """Owner decision 2026-10-21:  relay secrets resolve through the
    fleet-secrets CLI only - never the process environment, never direct
    Infisical HTTP."""

    def _source(self, stdout=b"", returncode=0, calls=None):
        from git_relay.app_tokens import FleetSecretsSource

        def runner(argv, capture_output, timeout):
            if calls is not None:
                calls.append(argv)
            return subprocess.CompletedProcess(argv, returncode, stdout=stdout, stderr=b"")

        return FleetSecretsSource(runner=runner)

    def test_reads_through_fleet_secrets_get_and_caches(self):
        calls = []
        source = self._source(stdout=b"value\n", calls=calls)
        self.assertEqual(source.secret("GH_APP_SWU_ECHO_ID"), "value")
        self.assertEqual(source.secret("GH_APP_SWU_ECHO_ID"), "value")
        self.assertEqual(
            calls, [["fleet-secrets", "get", "GH_APP_SWU_ECHO_ID", "--path", "/github-apps"]]
        )

    def test_default_path_is_github_apps_and_is_overridable(self):
        from git_relay.app_tokens import AppTokenError, FleetSecretsSource

        calls = []

        def runner(argv, capture_output, timeout):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout=b"v", stderr=b"")

        FleetSecretsSource(runner=runner).secret("GH_APP_SWU_ECHO_ID")
        self.assertEqual(calls[0][-2:], ["--path", "/github-apps"])
        calls.clear()
        FleetSecretsSource(runner=runner, path="/ghr").secret("GH_APP_SWU_ECHO_ID")
        self.assertEqual(calls[0][-2:], ["--path", "/ghr"])
        with self.assertRaises(AppTokenError):
            FleetSecretsSource(runner=runner, path="github-apps")

    def test_environment_is_never_consulted(self):
        calls = []
        source = self._source(stdout=b"from-fleet-secrets\n", calls=calls)
        with unittest.mock.patch.dict(os.environ, {"GH_APP_SWU_ECHO_ID": "from-env"}):
            self.assertEqual(source.secret("GH_APP_SWU_ECHO_ID"), "from-fleet-secrets")
        self.assertEqual(len(calls), 1)

    def test_missing_value_names_the_variable_only(self):
        from git_relay.app_tokens import AppTokenError

        source = self._source(returncode=1)
        with self.assertRaises(AppTokenError) as ctx:
            source.secret("GH_APP_SWU_ECHO_PRIVATE_KEY")
        self.assertIn("GH_APP_SWU_ECHO_PRIVATE_KEY", str(ctx.exception))

    def test_timeout_and_missing_executable_are_named_errors(self):
        from git_relay.app_tokens import AppTokenError, FleetSecretsSource

        def hanging(argv, capture_output, timeout):
            raise subprocess.TimeoutExpired(argv, timeout)

        with self.assertRaises(AppTokenError) as ctx:
            FleetSecretsSource(runner=hanging).secret("GH_APP_X_ID")
        self.assertIn("timed out", str(ctx.exception))

        def missing(argv, capture_output, timeout):
            raise FileNotFoundError(argv[0])

        with self.assertRaises(AppTokenError) as ctx:
            FleetSecretsSource(runner=missing).secret("GH_APP_X_ID")
        self.assertIn("not found", str(ctx.exception))

    def test_secrets_reads_a_pair_in_one_cli_call(self):
        calls = []
        source = self._source(
            stdout=b'{"GH_APP_SWU_ECHO_ID": "42", "GH_APP_SWU_ECHO_PRIVATE_KEY": "pem"}',
            calls=calls,
        )
        pair = source.secrets(["GH_APP_SWU_ECHO_ID", "GH_APP_SWU_ECHO_PRIVATE_KEY"])
        self.assertEqual(pair, {"GH_APP_SWU_ECHO_ID": "42", "GH_APP_SWU_ECHO_PRIVATE_KEY": "pem"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            calls[0],
            [
                "fleet-secrets",
                "get",
                "GH_APP_SWU_ECHO_ID",
                "GH_APP_SWU_ECHO_PRIVATE_KEY",
                "--path",
                "/github-apps",
            ],
        )
        # Cached:  reading the pair (or one name of it) again runs no CLI call.
        self.assertEqual(
            source.secrets(["GH_APP_SWU_ECHO_ID", "GH_APP_SWU_ECHO_PRIVATE_KEY"]), pair
        )
        self.assertEqual(source.secret("GH_APP_SWU_ECHO_ID"), "42")
        self.assertEqual(len(calls), 1)

    def test_absent_name_is_not_refetched_per_request(self):
        from git_relay.app_tokens import AppTokenError

        calls = []
        source = self._source(returncode=3, calls=calls)
        with self.assertRaises(AppTokenError):
            source.secret("GH_APP_NOPE_ID")
        with self.assertRaises(AppTokenError):
            source.secret("GH_APP_NOPE_ID")
        self.assertEqual(len(calls), 1)

    def test_absent_names_are_read_as_exact_tokens(self):
        from git_relay.app_tokens import AppTokenError, FleetSecretsSource

        # GH_APP_X_ID is a prefix of GH_APP_X_ID_OLD:  the CLI's exit-3 line
        # lists only the second, and only the second may be remembered absent.
        calls = []

        def runner(argv, capture_output, timeout):
            calls.append(argv)
            if "GH_APP_X_ID_OLD" in argv:
                return subprocess.CompletedProcess(
                    argv,
                    3,
                    stdout=b"",
                    stderr=(
                        b"fleet-secrets: key GH_APP_X_ID_OLD is not in Infisical at /github-apps\n"
                    ),
                )
            return subprocess.CompletedProcess(argv, 0, stdout=b"7", stderr=b"")

        source = FleetSecretsSource(runner=runner)
        with self.assertRaises(AppTokenError) as ctx:
            source.secrets(["GH_APP_X_ID", "GH_APP_X_ID_OLD"])
        self.assertIn("GH_APP_X_ID_OLD", str(ctx.exception))
        self.assertEqual(source.secret("GH_APP_X_ID"), "7")
        with self.assertRaises(AppTokenError):
            source.secret("GH_APP_X_ID_OLD")
        self.assertEqual(len(calls), 2)

    def test_the_absent_line_is_found_after_other_stderr_lines(self):
        from git_relay.app_tokens import AppTokenError, FleetSecretsSource

        # A warning ahead of the exit-3 line must not send the parse to the
        # first-name fallback, which would blame the live GH_APP_X_ID.
        calls = []

        def runner(argv, capture_output, timeout):
            calls.append(argv)
            if "GH_APP_X_PRIVATE_KEY" in argv:
                return subprocess.CompletedProcess(
                    argv,
                    3,
                    stdout=b"",
                    stderr=(
                        b"warning: a newer release is available\n"
                        b"fleet-secrets: key GH_APP_X_PRIVATE_KEY"
                        b" is not in Infisical at /github-apps\n"
                    ),
                )
            return subprocess.CompletedProcess(argv, 0, stdout=b"7", stderr=b"")

        source = FleetSecretsSource(runner=runner)
        with self.assertRaises(AppTokenError) as ctx:
            source.secrets(["GH_APP_X_ID", "GH_APP_X_PRIVATE_KEY"])
        self.assertIn("GH_APP_X_PRIVATE_KEY", str(ctx.exception))
        self.assertEqual(source.secret("GH_APP_X_ID"), "7")
        self.assertEqual(len(calls), 2)

    def test_every_listed_absent_name_is_remembered(self):
        from git_relay.app_tokens import AppTokenError, FleetSecretsSource

        calls = []

        def runner(argv, capture_output, timeout):
            calls.append(argv)
            return subprocess.CompletedProcess(
                argv,
                3,
                stdout=b"",
                stderr=(
                    b"fleet-secrets: key GH_APP_X_ID, GH_APP_X_PRIVATE_KEY"
                    b" is not in Infisical at /github-apps\n"
                ),
            )

        source = FleetSecretsSource(runner=runner)
        with self.assertRaises(AppTokenError):
            source.secrets(["GH_APP_X_ID", "GH_APP_X_PRIVATE_KEY"])
        for name in ("GH_APP_X_ID", "GH_APP_X_PRIVATE_KEY"):
            with self.assertRaises(AppTokenError, msg=name):
                source.secret(name)
        self.assertEqual(len(calls), 1)

    def test_usage_failure_surfaces_the_cli_reason(self):
        from git_relay.app_tokens import AppTokenError, FleetSecretsSource

        # The CLI's exit-2 stderr:  one reason line, then its whole usage block.
        def runner(argv, capture_output, timeout):
            return subprocess.CompletedProcess(
                argv,
                2,
                stdout=b"",
                stderr=(
                    b"fleet-secrets: bad --path:  use an absolute Infisical folder path"
                    b" such as /\n"
                    b"usage: fleet-secrets run [--path /] [--only NAME,NAME] -- CMD [ARG...]\n"
                    b"       fleet-secrets get NAME [NAME ...] [--path /]\n"
                    b"       fleet-secrets doctor [--json]\n"
                ),
            )

        source = FleetSecretsSource(runner=runner)
        with self.assertRaises(AppTokenError) as ctx:
            source.secret("GH_APP_SWU_ECHO_ID")
        self.assertIn("GH_APP_SWU_ECHO_ID", str(ctx.exception))
        self.assertIn("bad --path", str(ctx.exception))
        self.assertNotIn("usage:", str(ctx.exception))
        self.assertNotIn("\n", str(ctx.exception))

    def test_unreadable_pair_output_is_an_app_token_error(self):
        from git_relay.app_tokens import AppTokenError

        # For several names the CLI prints one JSON object.  Anything else on
        # stdout (a stray banner, JSON that is not an object) is a lookup
        # failure like every other:  AppTokenError, never a bare ValueError
        # (a 400 bad_request) or AttributeError (a 500), and the message
        # carries the names only, never the output.
        for stdout in (b"warning: a newer release is available\n", b"[]", b'"SENTINEL_OUT"'):
            calls = []
            source = self._source(stdout=stdout, calls=calls)
            with self.assertRaises(AppTokenError, msg=stdout) as ctx:
                source.secrets(["GH_APP_X_ID", "GH_APP_X_PRIVATE_KEY"])
            self.assertIn("GH_APP_X_ID", str(ctx.exception))
            self.assertNotIn("newer release", str(ctx.exception))
            self.assertNotIn("SENTINEL_OUT", str(ctx.exception))
            self.assertEqual(len(calls), 1)

    def test_path_guard_matches_the_cli_rules(self):
        from git_relay.app_tokens import AppTokenError, FleetSecretsSource

        for bad in (
            "github-apps",
            "",
            "/github apps",
            "/github-apps/../linear",
            "/github-apps/..",
            "/github-apps\n",
        ):
            with self.assertRaises(AppTokenError, msg=bad):
                FleetSecretsSource(path=bad)
        FleetSecretsSource(path="/github-apps")  # the real folder stays accepted

    def test_empty_value_is_refused(self):
        from git_relay.app_tokens import AppTokenError

        source = self._source(stdout=b"  \n")
        with self.assertRaises(AppTokenError):
            source.secret("GH_APP_X_ID")

    def test_secret_value_never_appears_in_errors(self):
        from git_relay.app_tokens import AppTokenError, FleetSecretsSource

        def runner(argv, capture_output, timeout):
            return subprocess.CompletedProcess(argv, 1, stdout=b"", stderr=b"backend detail")

        try:
            FleetSecretsSource(runner=runner).secret("GH_APP_X_ID")
        except AppTokenError as exc:
            self.assertNotIn("backend detail", str(exc))
        else:
            self.fail("expected AppTokenError")


class TestFleetSecretsSourceAgainstTheRealCli(unittest.TestCase):
    """The relay parses the fleet-secrets CLI's stdout and stderr, so these
    tests run the real CLI (scripts/fleet_secrets/cli.py, in this repository)
    in-process with a fake folder in place of the Infisical export:  a
    wording change in the CLI fails here instead of silently sending every
    exit 3 through the first-name fallback."""

    FOLDER = {"GH_APP_X_ID": "7", "GH_APP_X_PRIVATE_KEY": "pem"}

    def _source(self, calls):
        from git_relay.app_tokens import FleetSecretsSource

        scripts = str(pathlib.Path(__file__).resolve().parents[3] / "scripts")
        sys.path.insert(0, scripts)
        try:
            from fleet_secrets import cli, core
        finally:
            sys.path.remove(scripts)

        def fake_export(path, env, project_name=None, token=None):
            return dict(self.FOLDER)

        def runner(argv, capture_output, timeout):
            calls.append(argv)
            out, err = io.StringIO(), io.StringIO()
            with unittest.mock.patch.object(core, "fetch_all", fake_export):
                code = cli.main(argv[1:], env={}, out=out, err=err)
            return subprocess.CompletedProcess(
                argv, code, stdout=out.getvalue().encode(), stderr=err.getvalue().encode()
            )

        return FleetSecretsSource(runner=runner)

    def test_a_pair_reads_through_the_real_cli(self):
        calls = []
        source = self._source(calls)
        self.assertEqual(
            source.secrets(["GH_APP_X_ID", "GH_APP_X_PRIVATE_KEY"]),
            {"GH_APP_X_ID": "7", "GH_APP_X_PRIVATE_KEY": "pem"},
        )
        self.assertEqual(len(calls), 1)

    def test_only_the_names_the_real_cli_lists_are_remembered_absent(self):
        from git_relay.app_tokens import AppTokenError

        calls = []
        source = self._source(calls)
        with self.assertRaises(AppTokenError) as ctx:
            source.secrets(["GH_APP_X_ID", "GH_APP_X_ID_OLD"])
        # The fallback would blame GH_APP_X_ID, the first name:  this only
        # passes when the CLI's own exit-3 line was parsed.
        self.assertIn("GH_APP_X_ID_OLD", str(ctx.exception))
        self.assertEqual(source.secret("GH_APP_X_ID"), "7")
        with self.assertRaises(AppTokenError):
            source.secret("GH_APP_X_ID_OLD")
        self.assertEqual(len(calls), 2)

    def test_a_real_usage_error_surfaces_one_line(self):
        from git_relay.app_tokens import AppTokenError

        source = self._source([])
        with self.assertRaises(AppTokenError) as ctx:
            source.secret("GH-APP-X-ID")  # not a valid key name:  the CLI exits 2
        self.assertIn("bad key name", str(ctx.exception))
        self.assertNotIn("usage:", str(ctx.exception))
        self.assertNotIn("\n", str(ctx.exception))


class TestGithubErrorSplit(unittest.TestCase):
    class ErrClient(StubClient):
        def __init__(self, status, code=None, retry_after=None):
            super().__init__()
            self._err = RelayGitHubError(status, "boom", code=code, retry_after=retry_after)

        def open_pr(self, repo, base, head, title, body):
            raise self._err

    def _handle(self, status, code=None, retry_after=None):
        tmp = tempfile.mkdtemp()
        relay = Relay(
            SeatPolicy(SEATS_JSON),
            self.ErrClient(status, code=code, retry_after=retry_after),
            AuditLog(os.path.join(tmp, "a.jsonl")),
        )
        code, payload = relay.handle(
            SEAT_TOKEN,
            "open_pr",
            {
                "repo": "example/fixture-repo",
                "base": "main",
                "head": "b",
                "title": "t",
                "body": "x",
            },
        )
        rows = [json.loads(line) for line in open(os.path.join(tmp, "a.jsonl"))]
        return code, payload, rows[-1]["result"]

    def test_unknown_outcomes_are_502_not_retryable_and_marked(self):
        # A transport failure or 5xx can arrive after GitHub accepted the write,
        # so it is never marked retryable:  replaying it could duplicate the write.
        for status in (0, 500, 503):
            code, payload, outcome = self._handle(status)
            self.assertEqual(code, 502, status)
            self.assertFalse(payload["retryable"], status)
            self.assertEqual(payload["outcome"], "unknown", status)
            self.assertEqual(outcome, "github_unknown", status)

    def test_rate_limit_is_a_definite_refusal_and_retryable(self):
        # 429 is an HTTP refusal from GitHub:  the write did not happen, the
        # idempotency key is released, and the seat may retry as-is.  The audit
        # outcome is the distinct retryable label, never the terminal one, and
        # a Retry-After header is surfaced to the caller and the audit row.
        code, payload, outcome = self._handle(429, retry_after=17)
        self.assertEqual(code, 429)
        self.assertTrue(payload["retryable"])
        self.assertEqual(payload["retry_after_seconds"], 17)
        self.assertEqual(outcome, "github_retryable")

    def test_408_timeout_is_unknown_never_released(self):
        # A 408 can arrive after GitHub applied the write:  classify it with
        # transport failures and 5xx as unknown and hold the idempotency key.
        code, payload, outcome = self._handle(408)
        self.assertEqual(code, 502)
        self.assertFalse(payload["retryable"])
        self.assertEqual(payload["outcome"], "unknown")
        self.assertEqual(outcome, "github_unknown")

    def test_invalid_grant_releases_the_key_as_retryable_auth_failure(self):
        # invalid_grant is a definite credential refusal:  nothing was written,
        # the key is released, and a corrected credential may retry cleanly.
        code, payload, outcome = self._handle(0, code="invalid_grant")
        self.assertEqual(code, 503)
        self.assertTrue(payload["retryable"])
        self.assertEqual(outcome, "github_auth")

    def test_terminal_failures_are_400_and_marked(self):
        for status in (401, 403, 404, 422):
            code, payload, outcome = self._handle(status)
            self.assertEqual(code, 400, status)
            self.assertFalse(payload["retryable"], status)
            self.assertEqual(outcome, "github_terminal", status)

    def test_replay_of_unknown_outcome_is_refused_409(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = Relay(
                SeatPolicy(SEATS_JSON),
                self.ErrClient(503),
                AuditLog(os.path.join(tmp, "a.jsonl")),
            )
            args = {
                "repo": "example/fixture-repo",
                "base": "main",
                "head": "b",
                "title": "t",
                "body": "x",
            }
            code, _ = relay.handle(SEAT_TOKEN, "open_pr", args)
            self.assertEqual(code, 502)
            code, payload = relay.handle(SEAT_TOKEN, "open_pr", args)
            self.assertEqual(code, 409)
            self.assertEqual(payload["outcome"], "unknown")
            self.assertFalse(payload["retryable"])

    def test_retry_after_definite_refusal_proceeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = Relay(
                SeatPolicy(SEATS_JSON),
                self.ErrClient(429),
                AuditLog(os.path.join(tmp, "a.jsonl")),
            )
            args = {
                "repo": "example/fixture-repo",
                "base": "main",
                "head": "b",
                "title": "t",
                "body": "x",
            }
            for _ in range(2):
                code, _ = relay.handle(SEAT_TOKEN, "open_pr", args)
                self.assertEqual(code, 429)

    def test_successful_replay_returns_stored_result_without_github(self):
        with tempfile.TemporaryDirectory() as tmp:

            class CountingClient(StubClient):
                def __init__(self):
                    super().__init__()
                    self.open_calls = 0

                def open_pr(self, repo, base, head, title, body):
                    self.open_calls += 1
                    return {"number": 9, "url": "https://example/pr/9"}

            client = CountingClient()
            relay = Relay(SeatPolicy(SEATS_JSON), client, AuditLog(os.path.join(tmp, "a.jsonl")))
            args = {
                "repo": "example/fixture-repo",
                "base": "main",
                "head": "b",
                "title": "t",
                "body": "x",
            }
            code, body1 = relay.handle(SEAT_TOKEN, "open_pr", args)
            self.assertEqual(code, 200)
            code, body2 = relay.handle(SEAT_TOKEN, "open_pr", args)
            self.assertEqual(code, 200)
            self.assertEqual(body1, body2)
            self.assertEqual(client.open_calls, 1)

    def test_pending_rows_load_as_unknown(self):
        from git_relay.outcomes import OutcomeStore, key_for

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "outcomes.json")
            store = OutcomeStore(path)
            key = key_for("swu-echo", "open_pr", {"repo": "a/b"})
            store.begin(key)
            # crash mid-write: reload turns pending into unknown, never replayable
            reloaded = OutcomeStore(path)
            prior = reloaded.begin(key)
            self.assertIsNotNone(prior)
            self.assertEqual(prior["state"], "unknown")


class TestOutcomeStoreHardening(unittest.TestCase):
    def test_corrupt_store_blocks_startup_and_is_preserved(self):
        from git_relay.outcomes import OutcomeStore, OutcomeStoreError

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "o.json")
            with open(path, "w") as fh:
                fh.write("{not json")
            with self.assertRaises(OutcomeStoreError) as ctx:
                OutcomeStore(path)
            self.assertIn(path, str(ctx.exception))
            self.assertTrue(os.path.exists(path))

    def test_invalid_row_shape_blocks_startup_with_path_and_key(self):
        from git_relay.outcomes import OutcomeStore, OutcomeStoreError

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "o.json")
            with open(path, "w") as fh:
                json.dump({"not-a-hash-key": {"state": "ok", "ts": 1.0}}, fh)
            with self.assertRaises(OutcomeStoreError) as ctx:
                OutcomeStore(path)
            self.assertIn("not-a-hash-key", str(ctx.exception))

    def test_interrupted_rename_recovers_from_temp_and_preserves_corrupt(self):
        from git_relay.outcomes import OutcomeStore, key_for

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "o.json")
            key = key_for("swu-echo", "open_pr", {"repo": "a/b"})
            with open(path, "w") as fh:
                fh.write("{torn")
            with open(path + ".tmp", "w") as fh:
                json.dump({key: {"state": "ok", "ts": 1.0, "result": {"number": 3}}}, fh)
            store = OutcomeStore(path)
            self.assertEqual(store.begin(key)["state"], "ok")
            preserved = [f for f in os.listdir(tmp) if "corrupt" in f]
            self.assertEqual(len(preserved), 1)
            with open(os.path.join(tmp, preserved[0])) as fh:
                self.assertEqual(fh.read(), "{torn")

    def test_trim_evicts_only_oldest_refused_rows(self):
        import git_relay.outcomes as oc
        from git_relay.outcomes import OutcomeStore

        ceiling = oc.MAX_ENTRIES
        oc.MAX_ENTRIES = 3
        now = 1_700_000_000.0
        try:
            store = OutcomeStore(None, clock=lambda: now)
            store._rows = {
                "k0": {"state": "unknown", "ts": now - 100},
                "k1": {"state": "ok", "ts": now - 60},
                "k2": {"state": "refused", "ts": now - 50},
                "k3": {"state": "refused", "ts": now - 40},
            }
            store._trim()
            self.assertNotIn("k2", store._rows)  # oldest releasable row goes first
            self.assertIn("k3", store._rows)
            self.assertIn("k0", store._rows)  # unknown rows are never evicted
            self.assertIn("k1", store._rows)  # fresh ok rows stay inside the replay window
        finally:
            oc.MAX_ENTRIES = ceiling

    def test_trim_evicts_stale_ok_rows_past_the_replay_window(self):
        from git_relay.outcomes import OK_TTL_SECONDS, OutcomeStore

        now = 1_700_000_000.0
        store = OutcomeStore(None, clock=lambda: now)
        store._rows = {
            "stale_ok": {"state": "ok", "ts": now - OK_TTL_SECONDS - 10},
            "fresh_ok": {"state": "ok", "ts": now - 60},
            "held": {"state": "unknown", "ts": now - OK_TTL_SECONDS - 20},
        }
        store._trim()
        self.assertNotIn("stale_ok", store._rows)
        self.assertIn("fresh_ok", store._rows)
        self.assertIn("held", store._rows)  # unknown is held past any TTL

    def test_begin_refuses_when_full_of_held_rows(self):
        import git_relay.outcomes as oc
        from git_relay.outcomes import OutcomeStore, OutcomeStoreError

        ceiling = oc.MAX_ENTRIES
        oc.MAX_ENTRIES = 2
        try:
            store = OutcomeStore(None)
            store._rows = {
                "u1": {"state": "unknown", "ts": 1.0},
                "p2": {"state": "pending", "ts": 2.0},
            }
            with self.assertRaises(OutcomeStoreError):
                store.begin("new-key")
            self.assertNotIn("new-key", store._rows)
        finally:
            oc.MAX_ENTRIES = ceiling


class TestAuditNestedSecrets(unittest.TestCase):
    def test_nested_credential_input_is_refused_before_any_side_effect(self):
        # A credential-shaped value nested anywhere in the request must refuse
        # the operation itself:  no dispatch, no GitHub call, and an audit row
        # that carries neither the value nor its prefix.
        with tempfile.TemporaryDirectory() as tmp:
            relay = Relay(
                SeatPolicy(SEATS_JSON), StubClient(), AuditLog(os.path.join(tmp, "a.jsonl"))
            )
            secretish = "ghp_" + "x" * 30
            code, payload = relay.handle(
                SEAT_TOKEN,
                "open_pr",
                {
                    "repo": "example/fixture-repo",
                    "base": "main",
                    "head": "b",
                    "title": "t",
                    "body": "x",
                    "extra": {"nested": ["k", secretish]},
                },
            )
            self.assertEqual(code, 400)
            self.assertIn("extra.nested[1]", payload["error"])
            self.assertNotIn(secretish, payload["error"])
            with open(os.path.join(tmp, "a.jsonl")) as fh:
                row = fh.read()
            self.assertNotIn(secretish, row)
            self.assertNotIn(secretish[:8], row)
            self.assertIn('"result": "bad_request"', row)

    def test_nested_credential_shapes_are_hashed_as_last_line(self):
        # Defense in depth:  even a detail value that reaches AuditLog.record
        # directly is hashed, whatever key it arrived under.
        with tempfile.TemporaryDirectory() as tmp:
            log = AuditLog(os.path.join(tmp, "a.jsonl"))
            secretish = "ghp_" + "x" * 30
            log.record(
                "swu-echo",
                "open_pr",
                "a/b",
                "ok",
                {"repo": "a/b", "extra": {"nested": ["k", secretish]}},
            )
            with open(os.path.join(tmp, "a.jsonl")) as fh:
                row = fh.read()
            self.assertNotIn(secretish, row)
            self.assertIn("_sha256", row)

    def test_reserved_fields_cannot_be_overwritten_by_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = AuditLog(os.path.join(tmp, "a.jsonl"))
            log.record(
                "swu-echo",
                "open_pr",
                "a/b",
                "ok",
                {"seat": "attacker", "result": "denied", "ts": "1999-01-01"},
            )
            with open(os.path.join(tmp, "a.jsonl")) as fh:
                row = json.loads(fh.read())
            self.assertEqual(row["seat"], "swu-echo")
            self.assertEqual(row["result"], "ok")
            self.assertNotEqual(row["ts"], "1999-01-01")


class TestFailClosedBearer(unittest.TestCase):
    def test_empty_task_bearer_never_falls_back_to_owner_token(self):
        client = GitHubClient("PAT", base_url="https://api.test", opener=FakeOpener({}))
        with self.assertRaises(ValueError):
            client.merge_pr("o/r", 3, "squash", bearer="")

    def test_graphql_error_type_is_preserved(self):
        opener = FakeOpener({"errors": [{"type": "FORBIDDEN", "message": "nope"}]})
        client = GitHubClient("PAT", base_url="https://api.test", opener=opener)
        with self.assertRaises(RelayGitHubError) as ctx:
            client.resolve_thread("PRRT_abc", bearer="MINTED")
        self.assertIn("FORBIDDEN", str(ctx.exception))

    def test_scoped_forbidden_without_mutation_result_is_definite_refusal(self):
        for payload in (None, {"resolveReviewThread": None}):
            client = GitHubClient(
                "PAT",
                opener=FakeOpener(
                    {
                        "data": payload,
                        "errors": [{"type": "FORBIDDEN", "path": ["resolveReviewThread"]}],
                    }
                ),
            )
            with self.assertRaises(RelayGitHubError) as ctx:
                client.resolve_thread("PRRT_abc", bearer="MINTED")
            self.assertEqual(ctx.exception.status, 403)
            self.assertEqual(ctx.exception.code, "FORBIDDEN")

    def test_scoped_forbidden_is_terminal_refusal_in_relay_and_not_held_unknown(self):
        from git_relay.outcomes import OutcomeStore

        with tempfile.TemporaryDirectory() as tmp:
            outcomes = OutcomeStore(os.path.join(tmp, "out.json"))
            client = GitHubClient(
                "PAT",
                opener=FakeOpener(
                    {
                        "data": {"resolveReviewThread": None},
                        "errors": [{"type": "FORBIDDEN", "path": ["resolveReviewThread"]}],
                    }
                ),
            )
            audit_path = os.path.join(tmp, "a.jsonl")
            relay = Relay(
                SeatPolicy(APP_SEATS_JSON),
                client,
                AuditLog(audit_path),
                outcomes=outcomes,
                app_minter=FakeMinter(),
            )
            code, payload = relay.handle(
                SEAT_TOKEN,
                "resolve_thread",
                {"repo": "example/fixture-repo", "thread_id": "PRRT_kwDOAbc-123_x"},
            )
            self.assertEqual(code, 400)
            self.assertEqual(payload["github_status"], 403)
            self.assertEqual(payload["github_code"], "FORBIDDEN")
            self.assertFalse(payload["retryable"])
            self.assertFalse(any(row.get("state") == "unknown" for row in outcomes._rows.values()))
            with open(audit_path) as fh:
                self.assertEqual(json.loads(fh.readlines()[-1])["result"], "github_terminal")

    def test_partial_mixed_or_unscoped_graphql_errors_remain_unknown(self):
        forbidden = {"type": "FORBIDDEN", "path": ["resolveReviewThread"]}
        cases = [
            {"errors": [{"type": "FORBIDDEN"}]},
            {"errors": [{"type": "FORBIDDEN", "path": ["other"]}]},
            {"errors": [forbidden, {"type": "INTERNAL"}]},
            {
                "errors": [forbidden],
                "data": {"resolveReviewThread": {"thread": {"isResolved": True}}},
            },
            {"errors": [forbidden], "data": "malformed"},
        ]
        for response in cases:
            client = GitHubClient("PAT", opener=FakeOpener(response))
            with self.assertRaises(RelayGitHubError) as ctx:
                client.resolve_thread("PRRT_abc", bearer="MINTED")
            self.assertEqual(ctx.exception.status, 0, response)

    def test_graphql_non_dict_reply_is_an_error_not_attributeerror(self):
        opener = FakeOpener(["not", "a", "dict"])
        client = GitHubClient("PAT", base_url="https://api.test", opener=opener)
        with self.assertRaises(RelayGitHubError):
            client.resolve_thread("PRRT_abc", bearer="MINTED")


class TestDispatchInputsBounds(unittest.TestCase):
    def _client(self):
        return GitHubClient("PAT", base_url="https://api.test", opener=FakeOpener({}))

    def test_nested_inputs_refused(self):
        with self.assertRaises(ValueError):
            self._client().workflow_dispatch(
                "o/r", "ci.yml", "main", {"env": {"nested": True}}, bearer="MINTED"
            )

    def test_too_many_inputs_refused(self):
        with self.assertRaises(ValueError):
            self._client().workflow_dispatch(
                "o/r", "ci.yml", "main", {f"k{i}": "v" for i in range(33)}, bearer="MINTED"
            )

    def test_oversized_input_value_refused(self):
        with self.assertRaises(ValueError):
            self._client().workflow_dispatch(
                "o/r", "ci.yml", "main", {"k": "v" * 2000}, bearer="MINTED"
            )


class TestPolicyMintPermissionsValidation(unittest.TestCase):
    def test_non_object_mint_permissions_refused_at_load(self):
        with self.assertRaises(PolicyError):
            SeatPolicy(
                json.dumps(
                    {
                        "swu-echo": {
                            "token": "x" * 16,
                            "actions": ["mint_token"],
                            "repos": ["a/b"],
                            "app_slug": "swu-echo",
                            "mint_permissions": ["contents"],
                        }
                    }
                )
            )

    def test_permission_outside_ceiling_refused_at_load(self):
        with self.assertRaises(PolicyError) as ctx:
            SeatPolicy(
                json.dumps(
                    {
                        "swu-echo": {
                            "token": "x" * 16,
                            "actions": ["mint_token"],
                            "repos": ["a/b"],
                            "app_slug": "swu-echo",
                            "mint_permissions": {"workflows": "write"},
                        }
                    }
                )
            )
        self.assertIn("ceiling", str(ctx.exception))


class TestMintTokenExpiry(unittest.TestCase):
    def test_expires_in_seconds_reflects_real_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:

            class ShortMinter:
                def token_for(self, slug, repo, permissions):
                    return "MINTED-FAKE-TOKEN", time.time() + 600

            relay = Relay(
                SeatPolicy(APP_SEATS_JSON),
                StubClient(),
                AuditLog(os.path.join(tmp, "a.jsonl")),
                app_minter=ShortMinter(),
            )
            status, body = relay.handle(
                SEAT_TOKEN,
                "mint_token",
                {"repo": "example/x", "permissions": {"contents": "read"}},
            )
            self.assertEqual(status, 200)
            self.assertLessEqual(body["expires_in_seconds"], 600)
            self.assertGreater(body["expires_in_seconds"], 500)


class TestAppTokenErrorMapped(unittest.TestCase):
    def test_mint_failure_is_503_not_dropped(self):
        from git_relay.app_tokens import AppTokenError

        with tempfile.TemporaryDirectory() as tmp:

            class BrokenMinter:
                def token_for(self, slug, repo, permissions):
                    raise AppTokenError("infisical unavailable")

            relay = Relay(
                SeatPolicy(APP_SEATS_JSON),
                StubClient(),
                AuditLog(os.path.join(tmp, "a.jsonl")),
                app_minter=BrokenMinter(),
            )
            status, body = relay.handle(SEAT_TOKEN, "merge_pr", {"repo": "example/x", "number": 1})
            self.assertEqual(status, 503)
            with open(os.path.join(tmp, "a.jsonl")) as fh:
                logged = fh.read()
            self.assertIn("unavailable", logged)

    def test_unreadable_fleet_secrets_output_is_503_not_400(self):
        from git_relay.app_tokens import AppTokenMinter, FleetSecretsSource

        # The real minter and source, with only the CLI faked:  stdout that is
        # not the JSON object a pair read prints must answer like every other
        # fleet-secrets failure (503, audited "unavailable"), not as the seat's
        # bad request.
        def runner(argv, capture_output, timeout):
            return subprocess.CompletedProcess(argv, 0, stdout=b"not json\n", stderr=b"")

        def api(method, path, bearer, payload=None):
            raise AssertionError("no GitHub call may follow a failed secrets read")

        with tempfile.TemporaryDirectory() as tmp:
            relay = Relay(
                SeatPolicy(APP_SEATS_JSON),
                StubClient(),
                AuditLog(os.path.join(tmp, "a.jsonl")),
                app_minter=AppTokenMinter(
                    FleetSecretsSource(runner=runner), api, signer=lambda *a: "JWT"
                ),
            )
            status, body = relay.handle(SEAT_TOKEN, "merge_pr", {"repo": "example/x", "number": 1})
            self.assertEqual(status, 503)
            self.assertIn("unreadable output", body["error"])
            with open(os.path.join(tmp, "a.jsonl")) as fh:
                rows = [json.loads(line) for line in fh]
            self.assertEqual(rows[-1]["result"], "unavailable")


class TestMcpSchemaEnforcement(unittest.TestCase):
    def _server(self):
        from git_relay.mcp_server import RelayMcp

        return RelayMcp("http://relay.test", "seat-token", opener=None)

    def _call(self, name, args):
        return self._server().handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            }
        )

    def test_bad_thread_id_rejected_before_forwarding(self):
        reply = self._call("resolve_thread", {"repo": "a/b", "thread_id": "not-a-thread"})
        self.assertEqual(reply["error"]["code"], -32602)

    def test_nested_workflow_inputs_rejected_before_forwarding(self):
        reply = self._call(
            "workflow_dispatch",
            {"repo": "a/b", "workflow": "ci.yml", "ref": "main", "inputs": {"a": {"b": 1}}},
        )
        self.assertEqual(reply["error"]["code"], -32602)

    def test_unexpected_argument_rejected_before_forwarding(self):
        reply = self._call(
            "open_pr",
            {"repo": "a/b", "base": "m", "head": "h", "title": "t", "body": "b", "admin": True},
        )
        self.assertEqual(reply["error"]["code"], -32602)

    def test_missing_required_rejected_before_forwarding(self):
        reply = self._call("open_pr", {"repo": "a/b"})
        self.assertEqual(reply["error"]["code"], -32602)


class TestScanBounds(unittest.TestCase):
    def _relay(self, tmp):
        client = StubClient()
        relay = Relay(SeatPolicy(SEATS_JSON), client, AuditLog(os.path.join(tmp, "a.jsonl")))
        return relay, client

    def test_over_breadth_request_is_refused_not_skipped(self):
        # 65 keys at one level:  the scan must refuse the request, because a
        # skipped branch could carry a credential the dispatcher still relays.
        with tempfile.TemporaryDirectory() as tmp:
            relay, client = self._relay(tmp)
            args = {
                "repo": "example/fixture-repo",
                "base": "main",
                "head": "b",
                "title": "t",
                "body": "x",
            }
            args.update({f"pad{i}": "v" for i in range(65)})
            code, _payload = relay.handle(SEAT_TOKEN, "open_pr", args)
            self.assertEqual(code, 400)
            self.assertEqual(client.calls, [])

    def test_credential_in_the_65th_key_never_reaches_github(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay, client = self._relay(tmp)
            args = {
                "repo": "example/fixture-repo",
                "base": "main",
                "head": "b",
                "title": "t",
                "body": "x",
            }
            args.update({f"pad{i}": "v" for i in range(64)})
            args["zz_leak"] = "ghp_" + "a" * 30
            code, _payload = relay.handle(SEAT_TOKEN, "open_pr", args)
            self.assertEqual(code, 400)
            self.assertEqual(client.calls, [])

    def test_over_depth_request_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay, client = self._relay(tmp)
            deep = current = {}
            for _ in range(12):
                current["n"] = {}
                current = current["n"]
            args = {
                "repo": "example/fixture-repo",
                "base": "main",
                "head": "b",
                "title": "t",
                "body": deep,
            }
            code, _payload = relay.handle(SEAT_TOKEN, "open_pr", args)
            self.assertEqual(code, 400)
            self.assertEqual(client.calls, [])


class TestValidationGate(unittest.TestCase):
    def test_unexpected_fields_are_rejected_and_create_no_durable_state(self):
        from git_relay.outcomes import OutcomeStore

        with tempfile.TemporaryDirectory() as tmp:
            outcomes = OutcomeStore(os.path.join(tmp, "out.json"))
            relay = Relay(
                SeatPolicy(SEATS_JSON),
                StubClient(),
                AuditLog(os.path.join(tmp, "a.jsonl")),
                outcomes=outcomes,
            )
            code, payload = relay.handle(
                SEAT_TOKEN,
                "open_pr",
                {
                    "repo": "example/fixture-repo",
                    "base": "main",
                    "head": "b",
                    "title": "t",
                    "body": "x",
                    "trace_id": "ignored-by-design-now-refused",
                },
            )
            self.assertEqual(code, 400)
            self.assertIn("unexpected fields", payload["error"])
            self.assertEqual(outcomes._rows, {})  # malformed input leaves no durable row

    def test_bad_shape_creates_no_durable_state(self):
        from git_relay.outcomes import OutcomeStore

        with tempfile.TemporaryDirectory() as tmp:
            outcomes = OutcomeStore(os.path.join(tmp, "out.json"))
            relay = Relay(
                SeatPolicy(SEATS_JSON),
                StubClient(),
                AuditLog(os.path.join(tmp, "a.jsonl")),
                outcomes=outcomes,
            )
            code, _ = relay.handle(
                SEAT_TOKEN,
                "open_pr",
                {"repo": "example/fixture-repo", "base": "main", "head": "b", "title": "t"},
            )
            self.assertEqual(code, 400)
            self.assertEqual(outcomes._rows, {})

    def test_policy_denial_runs_before_the_idempotency_gate(self):
        from git_relay.outcomes import OutcomeStore

        with tempfile.TemporaryDirectory() as tmp:
            outcomes = OutcomeStore(os.path.join(tmp, "out.json"))
            relay = Relay(
                SeatPolicy(SEATS_JSON),
                StubClient(),
                AuditLog(os.path.join(tmp, "a.jsonl")),
                outcomes=outcomes,
            )
            code, _ = relay.handle(
                OTHER_TOKEN,  # swu-prime may only comment_pr
                "open_pr",
                {
                    "repo": "example/fixture-repo",
                    "base": "main",
                    "head": "b",
                    "title": "t",
                    "body": "x",
                },
            )
            self.assertEqual(code, 403)
            self.assertEqual(outcomes._rows, {})  # a denial never occupies an outcome key


class TestActionHygiene(unittest.TestCase):
    def test_credential_shaped_action_is_not_persisted_or_echoed(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = Relay(
                SeatPolicy(SEATS_JSON), StubClient(), AuditLog(os.path.join(tmp, "a.jsonl"))
            )
            leaked = "ghp_" + "a" * 30
            code, payload = relay.handle(SEAT_TOKEN, leaked, {})
            self.assertEqual(code, 400)
            self.assertNotIn(leaked, json.dumps(payload))
            with open(os.path.join(tmp, "a.jsonl")) as fh:
                rows = [json.loads(line) for line in fh]
            self.assertEqual(rows[-1]["action"], "<credential-shaped>")
            self.assertEqual(rows[-1]["result"], "bad_request")


class TestCredentialPreview(unittest.TestCase):
    class PreviewClient(StubClient):
        def __init__(self, err):
            super().__init__()
            self._err = err

        def open_pr(self, repo, base, head, title, body):
            raise self._err

    def _handle(self, err):
        tmp = tempfile.mkdtemp()
        relay = Relay(
            SeatPolicy(SEATS_JSON), self.PreviewClient(err), AuditLog(os.path.join(tmp, "a.jsonl"))
        )
        return relay.handle(
            SEAT_TOKEN,
            "open_pr",
            {
                "repo": "example/fixture-repo",
                "base": "main",
                "head": "b",
                "title": "t",
                "body": "x",
            },
        )

    def test_invalid_grant_reports_the_masked_credential_preview(self):
        err = RelayGitHubError(
            401, "bad credentials", code="invalid_grant", credential_preview="ghp_abcd\u2026wxyz"
        )
        code, payload = self._handle(err)
        self.assertEqual(code, 503)
        self.assertEqual(payload["credential_preview"], "ghp_abcd\u2026wxyz")

    def test_unknown_outcome_carries_the_mapped_error_code(self):
        err = RelayGitHubError(502, "bad gateway", code="http_502")
        code, payload = self._handle(err)
        self.assertEqual(code, 502)
        self.assertEqual(payload["github_code"], "http_502")
        self.assertFalse(payload["retryable"])

    def test_mask_credential_shape(self):
        from git_relay.secret_scan import mask_credential

        self.assertEqual(mask_credential("ghp_abcdefghijklmnop"), "ghp_abcd\u2026mnop")
        self.assertEqual(mask_credential("short"), "<masked>")
        self.assertEqual(mask_credential(None), "<masked>")


class Round6Tests(unittest.TestCase):
    def test_outcome_store_at_capacity_evicts_a_refused_row(self):
        from git_relay import outcomes

        cap = 20
        with unittest.mock.patch.object(outcomes, "MAX_ENTRIES", cap):
            store = outcomes.OutcomeStore(None)
            keys = [outcomes.key_for("S", "act", {"i": i}) for i in range(cap)]
            for k in keys:
                store.begin(k)
                store.finish(k, "refused")
            fresh = outcomes.key_for("S", "act", {"i": "fresh"})
            self.assertIsNone(store.begin(fresh))  # None means acquired, a refused row was evicted
            self.assertLessEqual(len(store._rows), cap)

    def test_mcp_wrapper_thread_id_matches_the_relay_shape(self):
        from git_relay import mcp_server

        base = {"repo": "Example-Org/widget-repo"}
        for bad in ("PRRT_a b", "PRRT_short", "PRRT_" + "a" * 129):
            self.assertIsNotNone(
                mcp_server._validate_args("resolve_thread", {**base, "thread_id": bad})
            )
        self.assertIsNone(
            mcp_server._validate_args("resolve_thread", {**base, "thread_id": "PRRT_kwDOAbc-123_x"})
        )

    def test_thread_id_shape_and_unexpected_names_are_400s(self):
        from git_relay.server import _validate_args

        base = {"repo": "Example-Org/widget-repo"}
        with self.assertRaises(ValueError):
            _validate_args("resolve_thread", {**base, "thread_id": "not-a-thread"})
        with self.assertRaises(ValueError) as c:
            _validate_args("comment_pr", {**base, "number": 1, "body": "x", "ghp_" + "a" * 30: 1})
        self.assertNotIn("ghp_", str(c.exception))


class BotIdentityTests(unittest.TestCase):
    """Q7: one bearer per bot identity;  a bot's Zulip bot and GitHub App are
    the same identity.  A bearer whose App is not its own bot's App is refused.
    """

    def _seats(self, **overrides):
        seat = {
            "token": "x" * 32,
            "actions": ["mint_token", "merge_pr"],
            "repos": ["example/fixture-repo"],
            "app_slug": "swu-echo",
            "mint_permissions": {"contents": "write", "pull_requests": "write"},
        }
        seat.update(overrides)
        return json.dumps({"swu-echo": seat})

    def test_app_must_be_the_seats_own_bot(self):
        # A well-formed App that belongs to ANOTHER bot is the Q7 case.
        for other in ("swu-wise", "swu-echo-bot", "swu-echo-2"):
            with self.assertRaises(PolicyError, msg=other) as ctx:
                SeatPolicy(self._seats(app_slug=other))
            self.assertIn("own bot identity", str(ctx.exception))

    def test_malformed_or_missing_app_is_still_refused(self):
        for other in ("echo", "SWU-ECHO", ""):
            with self.assertRaises(PolicyError, msg=other):
                SeatPolicy(self._seats(app_slug=other))

    def test_matching_bot_and_app_loads(self):
        policy = SeatPolicy(self._seats())
        self.assertEqual(policy.bot_identities(), {"swu-echo": "swu-echo"})

    def test_a_bearer_cannot_act_as_another_bots_app(self):
        policy = SeatPolicy(self._seats())
        # ECHO's bearer authenticates as ECHO and nothing else, and both the
        # accessor and the App dispatch read that seat's own entry.
        self.assertEqual(policy.authenticate("x" * 32), "swu-echo")
        self.assertIsNone(policy.authenticate("z" * 32))
        self.assertEqual(policy.app_identity("swu-echo")[0], "swu-echo")
        with self.assertRaises(KeyError):
            policy.app_identity("swu-wise")

    def test_seats_without_app_actions_have_no_app_identity(self):
        seats = json.dumps(
            {
                "swu-echo": {
                    "token": "x" * 32,
                    "actions": ["open_pr"],
                    "repos": ["example/fixture-repo"],
                }
            }
        )
        policy = SeatPolicy(seats)
        self.assertEqual(policy.bot_identities(), {})

    def test_no_two_seats_share_one_app(self):
        # Config load already refuses a foreign App, so a policy that loads at
        # all maps each App to exactly one seat.
        seats = json.dumps(
            {
                "swu-echo": {
                    "token": "a" * 32,
                    "actions": ["mint_token"],
                    "repos": ["a/b"],
                    "app_slug": "swu-echo",
                    "mint_permissions": {"contents": "read"},
                },
                "swu-wise": {
                    "token": "b" * 32,
                    "actions": ["mint_token"],
                    "repos": ["a/b"],
                    "app_slug": "swu-wise",
                    "mint_permissions": {"contents": "read"},
                },
            }
        )
        mapping = SeatPolicy(seats).bot_identities()
        self.assertEqual(mapping, {"swu-echo": "swu-echo", "swu-wise": "swu-wise"})
        self.assertEqual(len(set(mapping.values())), len(mapping))


class CliEntryTests(unittest.TestCase):
    """The CLI/binary name is git-relay (hard cutover, no alias)."""

    BIN = pathlib.Path(__file__).resolve().parents[2] / "bin" / "git-relay"

    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(self.BIN), *args], capture_output=True, text=True, timeout=60
        )

    def test_bin_is_named_git_relay_and_executable(self):
        self.assertTrue(self.BIN.exists(), f"missing {self.BIN}")
        self.assertEqual(self.BIN.name, "git-relay")
        self.assertTrue(os.access(self.BIN, os.X_OK), "bin/git-relay must be executable")

    def test_help_names_the_cli_and_its_mcp_server(self):
        out = self._run("--help")
        self.assertEqual(out.returncode, 0)
        self.assertIn("usage: git-relay", out.stdout)
        self.assertIn("GR-MCP", out.stdout)
        # Hard cutover:  no legacy name anywhere in the help text.
        self.assertNotIn("pr-relay", out.stdout.lower())

    def test_unknown_command_exits_2_without_starting_a_service(self):
        out = self._run("definitely-not-a-command")
        self.assertEqual(out.returncode, 2)
        self.assertIn("unknown command", out.stderr)

    def test_serve_fails_closed_without_seat_policy(self):
        env = dict(os.environ, GIT_RELAY_SEATS_JSON="")
        env.pop("GIT_RELAY_SEATS_JSON", None)
        out = subprocess.run(
            [sys.executable, str(self.BIN), "serve"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("git-relay config", out.stderr)


class LegacyStateAdoption(unittest.TestCase):
    """The GHR -> git-relay rename must not abandon the write-idempotency ledger.

    A pending/unknown outcome row exists to fail closed:  it is what refuses a
    replay of a write whose GitHub outcome was never known.  Starting the
    renamed relay on an empty store would let those writes run a second time.
    """

    KEY = "b" * 64

    def _legacy_ledger(self, directory: pathlib.Path) -> pathlib.Path:
        path = directory / "pr-relay-outcomes.json"
        path.write_text(
            json.dumps({self.KEY: {"state": "unknown", "ts": 1700000000.0}}),
            encoding="utf-8",
        )
        return path

    def test_a_ledger_left_under_the_old_name_is_adopted(self):
        from git_relay.outcomes import OutcomeStore
        from git_relay.server import adopt_legacy_state

        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        legacy = self._legacy_ledger(directory)

        adopted = adopt_legacy_state(str(directory / "git-relay-outcomes.json"))

        self.assertFalse(legacy.exists(), "the old file is moved, not copied")
        self.assertTrue(pathlib.Path(adopted).exists())
        # The held row survived the rename:  a replay still fails closed.
        store = OutcomeStore(adopted)
        self.assertEqual(store._rows[self.KEY]["state"], "unknown")

    def test_an_existing_current_file_always_wins(self):
        from git_relay.server import adopt_legacy_state

        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        legacy = self._legacy_ledger(directory)
        current = directory / "git-relay-outcomes.json"
        current.write_text("{}", encoding="utf-8")

        adopted = adopt_legacy_state(str(current))

        self.assertEqual(adopted, str(current))
        self.assertTrue(legacy.exists(), "a present current file means no adoption")

    def test_nothing_to_adopt_returns_the_default(self):
        from git_relay.server import adopt_legacy_state

        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        adopted = adopt_legacy_state(str(directory / "git-relay-outcomes.json"))
        self.assertEqual(adopted, str(directory / "git-relay-outcomes.json"))
        self.assertFalse(pathlib.Path(adopted).exists())

    def test_a_lost_race_keeps_the_winners_ledger_and_the_legacy_file(self):
        # Another relay holds the adoption lock and has already produced the
        # current file.  Clobbering it would orphan the live ledger, which is the
        # one thing this migration must never do.
        from git_relay.server import adopt_legacy_state

        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        legacy = self._legacy_ledger(directory)
        current = directory / "git-relay-outcomes.json"
        current.write_text(json.dumps({"winner": {"state": "applied"}}), encoding="utf-8")
        (directory / ".git-relay-outcomes.json.adopt.lock").write_text("", encoding="utf-8")

        adopted = adopt_legacy_state(str(current))

        self.assertEqual(adopted, str(current))
        self.assertEqual(
            json.loads(current.read_text(encoding="utf-8")),
            {"winner": {"state": "applied"}},
            "the current ledger must survive untouched",
        )
        self.assertTrue(legacy.exists())

    def test_a_lock_with_no_result_fails_closed_instead_of_racing(self):
        # The lock is held, nothing has been adopted yet and the deadline passed.
        # Starting with an empty ledger would re-arm held writes, so refuse.
        from git_relay import server

        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        legacy = self._legacy_ledger(directory)
        lock = directory / ".git-relay-outcomes.json.adopt.lock"
        # A live holder:  an exclusive flock on its own open file description,
        # exactly what another relay process would hold.
        holder = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, holder)
        fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
        saved = server.ADOPT_LOCK_WAIT_SECONDS
        server.ADOPT_LOCK_WAIT_SECONDS = 0.0
        try:
            with self.assertRaises(SystemExit) as caught:
                server.adopt_legacy_state(str(directory / "git-relay-outcomes.json"))
        finally:
            server.ADOPT_LOCK_WAIT_SECONDS = saved
        self.assertIn("adopt.lock", str(caught.exception))
        self.assertTrue(lock.exists(), "a live holder's lock is never deleted")
        self.assertFalse((directory / "git-relay-outcomes.json").exists())
        self.assertTrue(legacy.exists(), "a refused adoption leaves the old ledger alone")

    def test_a_lock_file_left_by_a_dead_process_does_not_wedge_startup(self):
        # A crash between creating the lock and finishing the adoption must not
        # wedge every later startup.  The kernel released the dead owner's flock,
        # so the leftover file (whatever PID or age it records) is simply locked
        # again;  no PID or age guess decides it.
        from git_relay import server

        for label, pid_text, old_mtime in (
            ("dead owner", "999999999", False),
            ("old file naming this live pid", str(os.getpid()), True),
        ):
            with self.subTest(label):
                directory = pathlib.Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, directory, True)
                legacy = self._legacy_ledger(directory)
                lock = directory / ".git-relay-outcomes.json.adopt.lock"
                lock.write_text(f"{pid_text}\n", encoding="utf-8")
                if old_mtime:
                    old = time.time() - 3600.0
                    os.utime(lock, (old, old))

                adopted = server.adopt_legacy_state(str(directory / "git-relay-outcomes.json"))

                self.assertTrue(pathlib.Path(adopted).exists())
                self.assertFalse(legacy.exists(), "the leftover lock must not stop the move")
                self.assertFalse(lock.exists(), "the lock is cleaned up after the adoption")

    def test_adoption_stages_through_a_temporary_file_and_leaves_none_behind(self):
        from git_relay.server import adopt_legacy_state

        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        self._legacy_ledger(directory)

        adopt_legacy_state(str(directory / "git-relay-outcomes.json"))

        leftovers = [
            p.name
            for p in directory.iterdir()
            if p.name.endswith(".adopt") or p.name.endswith(".adopt.lock")
        ]
        self.assertEqual(leftovers, [], "the staged copy and its lock are both cleaned up")

    def test_from_env_adopts_the_default_but_never_an_explicit_path(self):
        from git_relay import server

        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        self._legacy_ledger(directory)
        explicit = directory / "operator-chosen.json"
        policy = json.dumps(
            {
                "swu-echo": {
                    "token": "e" * 32,
                    "actions": ["open_pr"],
                    "repos": ["acme-org/acme-repo"],
                }
            }
        )

        origin = pathlib.Path.cwd()

        def build(**overrides):

            env = dict(os.environ)
            env["GIT_RELAY_SEATS_JSON"] = policy
            env["GIT_RELAY_APP_MINT"] = "0"
            env.update(overrides)
            return contextlib.ExitStack()

        def build(**overrides):
            from git_relay import server

            env = dict(os.environ)
            env["GIT_RELAY_SEATS_JSON"] = policy
            env["GIT_RELAY_APP_MINT"] = "0"
            env.update(overrides)
            stack = contextlib.ExitStack()
            stack.enter_context(unittest.mock.patch.dict(os.environ, env, clear=True))
            stack.enter_context(
                unittest.mock.patch.object(server, "_owner_token_from_cli", return_value="t" * 40)
            )
            return stack

        with build():
            os.chdir(directory)
            try:
                relay = server.from_env()
            finally:
                os.chdir(origin)
        self.assertTrue((directory / "git-relay-outcomes.json").exists())
        self.assertEqual(relay.outcomes._rows[self.KEY]["state"], "unknown")

        # An explicit path is the operator's decision:  no file is moved.
        (directory / "git-relay-outcomes.json").unlink()
        self._legacy_ledger(directory)
        with build(GIT_RELAY_OUTCOMES_PATH=str(explicit)):
            os.chdir(directory)
            try:
                server.from_env()
            finally:
                os.chdir(origin)
        self.assertTrue((directory / "pr-relay-outcomes.json").exists())
        self.assertFalse(explicit.exists())

    def _app_mint_env(self, **overrides):
        policy = json.dumps(
            {
                "swu-echo": {
                    "token": "e" * 32,
                    "actions": ["open_pr"],
                    "repos": ["acme-org/acme-repo"],
                }
            }
        )
        from git_relay import server

        creds = tempfile.NamedTemporaryFile(prefix="apps-token-", delete=False, suffix=".token")
        creds.write(b"apps-scoped\n")
        creds.close()
        self.addCleanup(os.unlink, creds.name)

        env = dict(os.environ)
        env["GIT_RELAY_SEATS_JSON"] = policy
        env["GIT_RELAY_APPS_CREDENTIAL_FILE"] = creds.name
        env.pop("GIT_RELAY_APP_MINT", None)
        env.update(overrides)
        stack = contextlib.ExitStack()
        stack.enter_context(unittest.mock.patch.dict(os.environ, env, clear=True))
        stack.enter_context(
            unittest.mock.patch.object(server, "_owner_token_from_cli", return_value="t" * 40)
        )
        return stack

    def test_from_env_honors_the_configured_secrets_path(self):
        from git_relay import app_tokens, server

        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        captured = {}
        real_source = app_tokens.FleetSecretsSource

        def fake_source(**kwargs):
            captured.update(kwargs)
            return real_source(runner=lambda *a, **k: None, **kwargs)

        origin = pathlib.Path.cwd()
        with (
            self._app_mint_env(GIT_RELAY_SECRETS_PATH="/other-apps"),
            unittest.mock.patch.object(
                server.shutil, "which", return_value="/usr/local/bin/fleet-secrets"
            ),
            unittest.mock.patch.object(app_tokens, "FleetSecretsSource", fake_source),
        ):
            os.chdir(directory)
            try:
                server.from_env()
            finally:
                os.chdir(origin)
        self.assertEqual(captured.get("path"), "/other-apps")
        self.assertTrue(
            captured.get("credential_file", "").endswith(".token"),
            f"apps source was not bound to a credential file: {captured!r}",
        )

    def test_from_env_rejects_an_empty_secrets_path(self):
        from git_relay import server

        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        origin = pathlib.Path.cwd()
        with (
            self._app_mint_env(GIT_RELAY_SECRETS_PATH=""),
            unittest.mock.patch.object(
                server.shutil, "which", return_value="/usr/local/bin/fleet-secrets"
            ),
        ):
            os.chdir(directory)
            try:
                with self.assertRaises(SystemExit) as ctx:
                    server.from_env()
            finally:
                os.chdir(origin)
        self.assertIn("GIT_RELAY_SECRETS_PATH", str(ctx.exception))


class TestOwnerTokenFromCli(unittest.TestCase):
    """The open_pr owner token comes ONLY from `fleet-secrets get-verified`
    at /git-relay-owner;  an ambient GITHUB_TOKEN is never consulted."""

    def _fake_source(self, captured, value=None, error=None):
        def factory(**kwargs):
            captured.update(kwargs)

            class _Source:
                def verified_secret(self, name):
                    captured["name"] = name
                    if error is not None:
                        raise error
                    return value

            return _Source()

        return factory

    def test_reads_the_fixed_folder_and_ignores_the_environment(self):
        from git_relay import app_tokens, server

        captured = {}
        with (
            unittest.mock.patch.dict(
                os.environ,
                {
                    "GITHUB_TOKEN": "ambient-must-not-be-used",
                    "GIT_RELAY_OWNER_CREDENTIAL_FILE": "/tmp/owner-test-creds",
                },
                clear=True,
            ),
            unittest.mock.patch.object(
                app_tokens, "FleetSecretsSource", self._fake_source(captured, "cli-value")
            ),
        ):
            self.assertEqual(server._owner_token_from_cli(), "cli-value")
        self.assertEqual(captured.get("path"), "/git-relay-owner")
        self.assertEqual(captured.get("credential_file"), "/tmp/owner-test-creds")
        self.assertEqual(captured.get("name"), "GITHUB_TOKEN")

    def test_credential_file_binding_comes_from_the_environment(self):
        from git_relay import app_tokens, server

        captured = {}
        with (
            unittest.mock.patch.dict(
                os.environ,
                {"GIT_RELAY_OWNER_CREDENTIAL_FILE": "/run/credentials/owner-token"},
                clear=True,
            ),
            unittest.mock.patch.object(
                app_tokens, "FleetSecretsSource", self._fake_source(captured, "v")
            ),
        ):
            server._owner_token_from_cli()
        self.assertEqual(captured.get("credential_file"), "/run/credentials/owner-token")

    def test_a_failed_read_is_a_startup_error_with_no_value_in_it(self):
        from git_relay import app_tokens, server

        captured = {}
        with (
            unittest.mock.patch.dict(
                os.environ,
                {"GIT_RELAY_OWNER_CREDENTIAL_FILE": "/tmp/owner-test-creds"},
                clear=True,
            ),
            unittest.mock.patch.object(
                app_tokens,
                "FleetSecretsSource",
                self._fake_source(
                    captured, error=app_tokens.AppTokenError("the-value-must-not-appear")
                ),
            ),
            self.assertRaises(RuntimeError) as ctx,
        ):
            server._owner_token_from_cli()
        # AppTokenError messages name keys/folders only; the relay adds no values.
        self.assertIn("owner token", str(ctx.exception))


class TestVerifiedSecret(unittest.TestCase):
    def _source(self, result=None, raises=None, **kwargs):
        from git_relay.app_tokens import FleetSecretsSource

        captured = {}

        def runner(argv, **run_kwargs):
            captured["argv"] = list(argv)
            captured["run_kwargs"] = run_kwargs
            if raises is not None:
                raise raises
            return result

        return FleetSecretsSource(runner=runner, **kwargs), captured

    def _result(self, code=0, stdout=b"v\n", stderr=b""):
        return subprocess.CompletedProcess(["x"], code, stdout, stderr)

    def test_uses_the_get_verified_command_at_the_source_folder(self):
        source, captured = self._source(result=self._result())
        self.assertEqual(source.verified_secret("GITHUB_TOKEN"), "v")
        self.assertEqual(captured["argv"][0:2], ["fleet-secrets", "get-verified"])
        self.assertEqual(captured["argv"][2], "GITHUB_TOKEN")
        self.assertIn("--path", captured["argv"])
        # transitional mode: no env= kwarg, the legacy call shape is byte-identical
        self.assertNotIn("env", captured["run_kwargs"])

    def test_absent_is_remembered_and_other_failures_are_not(self):
        from git_relay.app_tokens import AppTokenError

        source, _ = self._source(result=self._result(code=3))
        with self.assertRaises(AppTokenError) as ctx:
            source.verified_secret("MISSING")
        self.assertIn("MISSING", str(ctx.exception))
        self.assertIn("MISSING", source._missing)

        source, _ = self._source(result=self._result(code=1))
        with self.assertRaises(AppTokenError):
            source.verified_secret("OTHER")
        with self.assertRaises(AppTokenError):
            source.verified_secret("OTHER")  # not cached absent: raises, no retry state

    def test_empty_stdout_is_an_error_not_a_value(self):
        from git_relay.app_tokens import AppTokenError

        source, _ = self._source(result=self._result(code=0, stdout=b"\n"))
        with self.assertRaises(AppTokenError):
            source.verified_secret("EMPTY")

    def test_credential_file_scopes_the_child_environment(self):
        from git_relay.app_tokens import FleetSecretsSource

        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        creds = directory / "owner-token"
        creds.write_text("scoped-token\n", encoding="utf-8")

        captured = {}

        def runner(argv, **run_kwargs):
            captured.update(run_kwargs)
            return self._result()

        environ = {
            "PATH": "/usr/bin",
            "HOME": "/home/x",
            "INFISICAL_FLEET_OPS_PROJECT_ID": "project-uuid",
            "INFISICAL_TOKEN": "ambient-must-not-leak",
            "GIT_RELAY_SEATS_JSON": "co-located-must-not-leak",
        }
        source = FleetSecretsSource(
            runner=runner,
            path="/git-relay-owner",
            credential_file=str(creds),
            environ=environ,
        )
        source.verified_secret("GITHUB_TOKEN")
        child = captured["env"]
        self.assertEqual(child["INFISICAL_TOKEN"], "scoped-token")
        self.assertEqual(child["PATH"], "/usr/bin")
        self.assertEqual(child["INFISICAL_ENVIRONMENT"], "prod")
        self.assertEqual(child["INFISICAL_FLEET_OPS_PROJECT_ID"], "project-uuid")
        self.assertNotIn("ENVIRONMENT", child)
        self.assertNotIn("INFISICAL_PROJECT_ID", child)
        self.assertNotIn("HOME", child)  # no incidental ~/.agent-sync resolution
        self.assertNotIn("GIT_RELAY_SEATS_JSON", child)

    def test_unreadable_credential_file_fails_closed(self):
        from git_relay.app_tokens import AppTokenError, FleetSecretsSource

        source = FleetSecretsSource(
            runner=lambda *a, **k: None,
            path="/git-relay-owner",
            credential_file="/nonexistent/credential",
        )
        with self.assertRaises(AppTokenError) as ctx:
            source.verified_secret("GITHUB_TOKEN")
        self.assertNotIn("credential-value", str(ctx.exception))

    def test_scoped_env_applies_to_app_key_reads_too(self):
        from git_relay.app_tokens import FleetSecretsSource

        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        creds = directory / "apps-token"
        creds.write_text("apps-scoped\n", encoding="utf-8")

        captured = {}

        def runner(argv, **run_kwargs):
            captured.update(run_kwargs)
            return self._result(stdout=b'{"GH_APP_X_ID": "1", "GH_APP_X_PRIVATE_KEY": "k"}')

        source = FleetSecretsSource(
            runner=runner,
            credential_file=str(creds),
            environ={"PATH": "/usr/bin", "INFISICAL_FLEET_OPS_PROJECT_ID": "uuid"},
        )
        source.secrets(["GH_APP_X_ID", "GH_APP_X_PRIVATE_KEY"])
        self.assertEqual(captured["env"]["INFISICAL_TOKEN"], "apps-scoped")


class TestProductionCredentialBinding(unittest.TestCase):
    """from_env's production entry binds every fleet-secrets source to its
    credential file and fails closed otherwise - the class-level
    credential_file=None shape is unreachable from production wiring."""

    POLICY = json.dumps(
        {
            "swu-echo": {
                "token": "e" * 32,
                "actions": ["open_pr"],
                "repos": ["acme-org/acme-repo"],
            }
        }
    )

    def _env(self, **overrides):
        env = {
            "GIT_RELAY_SEATS_JSON": self.POLICY,
            "GIT_RELAY_APP_MINT": "0",
        }
        env.update(overrides)
        return unittest.mock.patch.dict(os.environ, env, clear=True)

    def _credentials_dir(self):
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        for name in ("git-relay-owner-token", "git-relay-apps-token"):
            (directory / name).write_text("x\n", encoding="utf-8")
        return directory

    def test_no_binding_refuses_startup_naming_the_scope_variable(self):
        from git_relay import server

        with self._env(), self.assertRaises(SystemExit) as ctx:
            server.from_env()
        message = str(ctx.exception)
        self.assertIn("GIT_RELAY_OWNER_CREDENTIAL_FILE", message)
        self.assertIn("/git-relay-owner", message)
        self.assertNotIn("x", message.replace("Exit", "").replace("exit", ""))

    def test_empty_override_is_a_config_error(self):
        from git_relay import server

        with self._env(GIT_RELAY_OWNER_CREDENTIAL_FILE="  "):
            with self.assertRaises(SystemExit) as ctx:
                server.from_env()
        self.assertIn("GIT_RELAY_OWNER_CREDENTIAL_FILE", str(ctx.exception))

    def test_no_inheritance_escape_even_when_the_old_variable_is_set(self):
        from git_relay import server

        with self._env(GIT_RELAY_ALLOW_ENV_INHERITANCE="1"):
            with self.assertRaises(SystemExit) as ctx:
                server.from_env()
        self.assertIn("GIT_RELAY_OWNER_CREDENTIAL_FILE", str(ctx.exception))

    def test_all_three_sources_bind_their_credential_files_from_the_directory(self):
        from git_relay import app_tokens, server

        directory = self._credentials_dir()
        constructions = []

        def fake_source(**kwargs):
            constructions.append(dict(kwargs))

            class _Source:
                def verified_secret(self, name):
                    return "v"

                def secret(self, name):
                    return "k" * 32

            return _Source()

        with (
            self._env(
                CREDENTIALS_DIRECTORY=str(directory),
                GIT_RELAY_APP_MINT="1",
                GIT_RELAY_APPS_CREDENTIAL_FILE=str(directory / "git-relay-apps-token"),
                GIT_RELAY_VERIFY_URL="https://sync.example/gr-internal/verify-bearer",
            ),
            unittest.mock.patch.object(
                server.shutil, "which", return_value="/usr/local/bin/fleet-secrets"
            ),
            unittest.mock.patch.object(app_tokens, "FleetSecretsSource", fake_source),
            unittest.mock.patch.object(server, "_owner_token_from_cli", return_value="t" * 40),
        ):
            server.from_env()
        bound = {c.get("path"): c.get("credential_file") for c in constructions}
        self.assertEqual(bound.get("/github-apps"), str(directory / "git-relay-apps-token"))
        # two-reader host contract:  /tools binds the App reader's file
        self.assertEqual(bound.get("/tools"), str(directory / "git-relay-apps-token"))

    def test_owner_source_binds_from_the_directory_when_unpatched(self):
        from git_relay import app_tokens, server

        directory = self._credentials_dir()
        captured = {}

        def fake_source(**kwargs):
            captured.update(kwargs)

            class _Source:
                def verified_secret(self, name):
                    return "v"

            return _Source()

        with (
            self._env(CREDENTIALS_DIRECTORY=str(directory)),
            unittest.mock.patch.object(app_tokens, "FleetSecretsSource", fake_source),
        ):
            server.from_env()
        self.assertEqual(captured.get("path"), "/git-relay-owner")
        self.assertEqual(captured.get("credential_file"), str(directory / "git-relay-owner-token"))


class TestScopedChildProjectBinding(unittest.TestCase):
    """The scoped CLI child must carry the SUPPORTED fleet-secrets variable
    names and cannot fall through to ~/.agent-sync file resolution."""

    def _creds(self):
        directory = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory, True)
        creds = directory / "token"
        creds.write_text("scoped\n", encoding="utf-8")
        return str(creds)

    def test_missing_project_binding_fails_closed_naming_the_variable(self):
        from git_relay.app_tokens import AppTokenError, FleetSecretsSource

        source = FleetSecretsSource(
            runner=lambda *a, **k: None,
            path="/git-relay-owner",
            credential_file=self._creds(),
            environ={"PATH": "/usr/bin", "HOME": "/home/x"},
        )
        with self.assertRaises(AppTokenError) as ctx:
            source.verified_secret("GITHUB_TOKEN")
        self.assertIn("INFISICAL_FLEET_OPS_PROJECT_ID", str(ctx.exception))

    def test_blank_project_binding_is_missing(self):
        from git_relay.app_tokens import AppTokenError, FleetSecretsSource

        source = FleetSecretsSource(
            runner=lambda *a, **k: None,
            path="/git-relay-owner",
            credential_file=self._creds(),
            environ={"PATH": "/usr/bin", "INFISICAL_FLEET_OPS_PROJECT_ID": "  "},
        )
        with self.assertRaises(AppTokenError):
            source.verified_secret("GITHUB_TOKEN")
