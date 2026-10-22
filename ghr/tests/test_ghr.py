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
import time
import unittest
import unittest.mock
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from ghr.audit import AuditLog
from ghr.github_client import GitHubClient, RelayGitHubError
from ghr.policy import PolicyError, SeatPolicy
from ghr.server import MAX_REQUEST_BYTES, Relay, make_handler

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
        from ghr.mcp_server import RelayMcp

        if opener is None:
            opener = FakeOpener(payload or {"number": 9})
        return RelayMcp("http://relay.test", SEAT_TOKEN, opener=opener)

    def test_initialize_and_tools_list(self):
        server = self._server()
        reply = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        self.assertEqual(reply["result"]["serverInfo"]["name"], "swu-pr-relay")
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
                "create_release",
            ],
            "repos": ["Simple-With-Us/*"],
            "app_slug": "swu-echo",
            "mint_permissions": {
                "contents": "write",
                "pull_requests": "write",
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
            status, body = relay.handle(SEAT_TOKEN, "create_release", {"repo": "Simple-With-Us/x"})
            self.assertEqual(status, 400)
            self.assertIn("not implemented", body["error"])

    def test_mint_token_mints_within_ceiling_and_audits_no_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = self._relay(tmp)
            status, body = relay.handle(
                SEAT_TOKEN,
                "mint_token",
                {"repo": "Simple-With-Us/x", "permissions": {"contents": "read"}},
            )
            self.assertEqual(status, 200)
            self.assertEqual(body["app_slug"], "swu-echo")
            self.assertEqual(
                relay.app_minter.calls, [("swu-echo", "Simple-With-Us/x", {"contents": "read"})]
            )
            with open(os.path.join(tmp, "a.jsonl")) as fh:
                logged = fh.read()
            self.assertNotIn("MINTED-FAKE-TOKEN", logged)
            self.assertIn("token_sha256", logged)

    def test_mint_token_beyond_ceiling_is_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = self._relay(tmp)
            status, _ = relay.handle(
                SEAT_TOKEN,
                "mint_token",
                {"repo": "Simple-With-Us/x", "permissions": {"administration": "write"}},
            )
            self.assertEqual(status, 403)

    def test_app_actions_without_minter_get_503(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay = Relay(
                SeatPolicy(APP_SEATS_JSON), StubClient(), AuditLog(os.path.join(tmp, "a.jsonl"))
            )
            status, body = relay.handle(
                SEAT_TOKEN, "merge_pr", {"repo": "Simple-With-Us/x", "number": 1}
            )
            self.assertEqual(status, 503)
            self.assertIn("not configured", body["error"])


class TestMinter(unittest.TestCase):
    def test_mints_and_caches_and_refreshes(self):
        from ghr.app_tokens import AppTokenMinter

        class Source:
            def secret(self, name):
                return {"GH_APP_SWU_ECHO_ID": "100", "GH_APP_SWU_ECHO_PRIVATE_KEY": "PEM"}[name]

        api_calls = []

        def api(method, path, bearer, payload=None):
            api_calls.append((method, path))
            if path == "/app":
                return {"id": 100}
            if method == "GET":
                return {
                    "id": 55,
                    "app_id": 100,
                    "account": {"login": "Simple-With-Us"},
                    "suspended_at": None,
                }
            return {
                "token": "tok-" + str(len(api_calls)),
                "expires_at": "2027-01-01T00:00:00Z",
            }

        minter = AppTokenMinter(Source(), api, signer=lambda *a: "JWT", clock=lambda: 1_700_000_000)
        t1, expiry1 = minter.token_for("swu-echo", "Simple-With-Us/x", {"contents": "write"})
        t2, expiry2 = minter.token_for("swu-echo", "Simple-With-Us/x", {"contents": "write"})
        self.assertEqual(t1, t2)
        self.assertEqual(expiry1, expiry2)
        mints = [c for c in api_calls if c[0] == "POST"]
        self.assertEqual(len(mints), 1)

    def test_permission_outside_mint_ceiling_refused(self):
        from ghr.app_tokens import AppTokenError, AppTokenMinter

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
        from ghr.secret_scan import find_secret

        self.assertEqual(find_secret("token: ghp_" + "a" * 30), "github_token")
        self.assertEqual(find_secret("use github_pat_" + "b" * 40), "github_fine_grained_pat")
        self.assertEqual(find_secret("-----BEGIN " + "RSA PRIVATE KEY-----"), "private_key_block")
        self.assertEqual(find_secret("st." + "c" * 30), "infisical_service_token")

    def test_clean_text_passes(self):
        from ghr.secret_scan import find_secret

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
                    "repo": "Simple-With-Us/AI-Fleet-Coordinator",
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
                    "repo": "Simple-With-Us/AI-Fleet-Coordinator",
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
                    "repo": "Simple-With-Us/AI-Fleet-Coordinator",
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
        from ghr.audit import AuditLog

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


class TestInfisicalSourceOrdering(unittest.TestCase):
    def test_environment_wins_over_infisical(self):
        from ghr.app_tokens import InfisicalSource

        def boom(req, timeout=0):
            raise AssertionError("network must not be touched when env supplies the value")

        source = InfisicalSource("cid-1234", "secret", "ws", opener=boom)
        with unittest.mock.patch.dict(os.environ, {"GH_APP_X_ID": "42"}):
            self.assertEqual(source.secret("GH_APP_X_ID"), "42")

    def test_rejected_login_names_masked_identity_only(self):
        import urllib.error as ue

        from ghr.app_tokens import AppTokenError, InfisicalSource

        def unauthorized(req, timeout=0):
            raise ue.HTTPError(req.full_url, 401, "no", {}, None)

        source = InfisicalSource("abcd1234wxyz", "supersecretvalue", "ws", opener=unauthorized)
        with self.assertRaises(AppTokenError) as ctx:
            source.secret("GH_APP_X_ID")
        self.assertIn("client id ending wxyz", str(ctx.exception))
        self.assertNotIn("supersecretvalue", str(ctx.exception))


class TestGithubErrorSplit(unittest.TestCase):
    class ErrClient(StubClient):
        def __init__(self, status):
            super().__init__()
            self._status = status

        def open_pr(self, repo, base, head, title, body):
            raise RelayGitHubError(self._status, "boom")

    def _handle(self, status):
        tmp = tempfile.mkdtemp()
        relay = Relay(
            SeatPolicy(SEATS_JSON),
            self.ErrClient(status),
            AuditLog(os.path.join(tmp, "a.jsonl")),
        )
        code, payload = relay.handle(
            SEAT_TOKEN,
            "open_pr",
            {
                "repo": "Simple-With-Us/AI-Fleet-Coordinator",
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
        # idempotency key is released, and the seat may retry as-is.
        code, payload, outcome = self._handle(429)
        self.assertEqual(code, 429)
        self.assertTrue(payload["retryable"])
        self.assertEqual(outcome, "github_terminal")

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
                "repo": "Simple-With-Us/AI-Fleet-Coordinator",
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
                "repo": "Simple-With-Us/AI-Fleet-Coordinator",
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
                "repo": "Simple-With-Us/AI-Fleet-Coordinator",
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
        from ghr.outcomes import OutcomeStore, key_for

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


class TestAuditNestedSecrets(unittest.TestCase):
    def test_nested_credential_shapes_are_hashed(self):
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
                        "seat": {
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
                        "seat": {
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
                {"repo": "Simple-With-Us/x", "permissions": {"contents": "read"}},
            )
            self.assertEqual(status, 200)
            self.assertLessEqual(body["expires_in_seconds"], 600)
            self.assertGreater(body["expires_in_seconds"], 500)


class TestAppTokenErrorMapped(unittest.TestCase):
    def test_mint_failure_is_503_not_dropped(self):
        from ghr.app_tokens import AppTokenError

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
            status, body = relay.handle(
                SEAT_TOKEN, "merge_pr", {"repo": "Simple-With-Us/x", "number": 1}
            )
            self.assertEqual(status, 503)
            with open(os.path.join(tmp, "a.jsonl")) as fh:
                logged = fh.read()
            self.assertIn("unavailable", logged)


class TestMcpSchemaEnforcement(unittest.TestCase):
    def _server(self):
        from ghr.mcp_server import RelayMcp

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


class TestInfisicalRelogin(unittest.TestCase):
    def test_401_triggers_one_relogin_and_retry(self):
        from ghr.app_tokens import InfisicalSource

        calls = []

        logins = []

        def opener(req, timeout=30):
            calls.append(req.full_url)
            if "login" in req.full_url:
                logins.append(1)
                return _FakeResp({"accessToken": "tok-%d" % len(logins)})
            if req.headers.get("Authorization") == "Bearer tok-2":
                return _FakeResp({"secret": {"secretValue": "value"}})
            raise urllib.error.HTTPError(req.full_url, 401, "unauthorized", {}, None)

        source = InfisicalSource("cid-1234", "secret", "ws", opener=opener)
        self.assertEqual(source.secret("GH_APP_SWU_ECHO_ID"), "value")
        self.assertEqual(sum(1 for c in calls if "login" in c), 2)


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def read(self, _n=10_000_000):
        return json.dumps(self._data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False
