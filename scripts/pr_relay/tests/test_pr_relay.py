"""Tests for the PR relay: policy, audit, client request shapes, HTTP surface.

Nothing here touches the network: the GitHub opener is a fake, and the HTTP
tests bind an ephemeral port on 127.0.0.1 with a stubbed client.
"""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from pr_relay.audit import AuditLog
from pr_relay.github_client import GitHubClient, RelayGitHubError
from pr_relay.policy import PolicyError, SeatPolicy
from pr_relay.server import MAX_REQUEST_BYTES, Relay, make_handler

SEAT_TOKEN = "s" * 32
OTHER_TOKEN = "o" * 32

SEATS_JSON = json.dumps(
    {
        "swu-echo": {
            "token": SEAT_TOKEN,
            "actions": ["open_pr", "edit_pr", "reply_thread", "comment_pr"],
            "repos": ["Simple-With-Us/AI-Fleet-Coordinator"],
        },
        "swu-prime": {
            "token": OTHER_TOKEN,
            "actions": ["comment_pr"],
            "repos": ["Simple-With-Us/*"],
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
        self.assertIsNone(
            policy.authorize("swu-echo", "open_pr", "Simple-With-Us/AI-Fleet-Coordinator")
        )
        self.assertIsNotNone(policy.authorize("swu-prime", "open_pr", "Simple-With-Us/x"))
        self.assertIsNotNone(policy.authorize("swu-echo", "open_pr", "Simple-With-Us/SWU"))
        self.assertIsNone(policy.authorize("swu-prime", "comment_pr", "Simple-With-Us/SWU"))
        self.assertIsNotNone(policy.authorize("swu-prime", "comment_pr", "other-org/SWU"))
        self.assertIsNotNone(policy.authorize("ghost", "open_pr", "Simple-With-Us/x"))

    def test_rate_limit_denies_after_the_cap(self):
        policy = SeatPolicy(SEATS_JSON, rate_per_hour=2)
        repo = "Simple-With-Us/AI-Fleet-Coordinator"
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

    def edit_pr(self, repo, number, title=None, body=None):
        return {"number": number, "url": "u"}

    def reply_thread(self, repo, number, comment_id, body):
        return {"id": 2, "url": "u"}

    def comment_pr(self, repo, number, body):
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
                "repo": "Simple-With-Us/AI-Fleet-Coordinator",
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
            {"repo": "Simple-With-Us/SWU", "number": 1, "body": "x"},
            token="wrong" * 8,
        )
        self.assertEqual(status, 401)

    def test_policy_denial_gets_403(self):
        status, body = self._post(
            "/v1/open_pr",
            {"repo": "Simple-With-Us/SWU", "base": "main", "head": "f", "title": "t", "body": "b"},
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
        status, _ = self._post("/v1/open_pr", {"repo": "Simple-With-Us/AI-Fleet-Coordinator"})
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


class TestActionTiers(unittest.TestCase):
    def test_tier2_actions_are_refused_until_enabled_relay_wide(self):
        seats = json.dumps(
            {
                "swu-echo": {
                    "token": SEAT_TOKEN,
                    "actions": ["open_pr", "push"],
                    "repos": ["Simple-With-Us/*"],
                }
            }
        )
        policy = SeatPolicy(seats)
        denial = policy.authorize("swu-echo", "push", "Simple-With-Us/x")
        self.assertIn("not enabled relay-wide", denial)
        policy_on = SeatPolicy(seats, enabled_actions=["open_pr", "push"])
        self.assertIsNone(policy_on.authorize("swu-echo", "push", "Simple-With-Us/x"))

    def test_unregistered_action_is_rejected_at_config(self):
        with self.assertRaises(PolicyError):
            SeatPolicy(
                json.dumps(
                    {
                        "seat": {
                            "token": "x" * 16,
                            "actions": ["delete_repo"],
                            "repos": ["a/b"],
                        }
                    }
                )
            )


class TestMcpWrapper(unittest.TestCase):
    def _server(self, payload=None, opener=None):
        from pr_relay.mcp_server import RelayMcp

        if opener is None:
            opener = FakeOpener(payload or {"number": 9})
        return RelayMcp("http://relay.test", SEAT_TOKEN, opener=opener)

    def test_initialize_and_tools_list(self):
        server = self._server()
        reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(reply["result"]["serverInfo"]["name"], "swu-pr-relay")
        reply = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = [t["name"] for t in reply["result"]["tools"]]
        self.assertEqual(names, ["open_pr", "edit_pr", "reply_thread", "comment_pr"])

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
