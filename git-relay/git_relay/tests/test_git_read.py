"""The relay's read-only git route, over a real socket with a fake GitHub behind it."""

from __future__ import annotations

import base64
import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

from git_relay import git_read
from git_relay.server import make_handler

BEARER = "seat-bearer-value"
APP_TOKEN = "ghs_fake_installation_token_value"


class Audit:
    def __init__(self):
        self.rows = []

    def record(self, seat, action, repo, result, detail):
        self.rows.append((seat, action, repo, result, dict(detail)))


class UnauthAuditor:
    def __init__(self):
        self.count = 0

    def record(self, audit, action):
        self.count += 1
        return True


class Policy:
    def __init__(self, denial=None, ceiling=None):
        self.denial = denial
        self.ceiling = {"contents": "write"} if ceiling is None else ceiling

    def authenticate(self, token, proof=None):
        return "CLAUDE" if token == BEARER else None

    def authorize(self, seat, action, repo, now=None):
        return self.denial

    def app_identity(self, seat):
        return "swu-claude", dict(self.ceiling)


class Minter:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def token_for(self, slug, repo, permissions):
        self.calls.append((slug, repo, permissions))
        if self.error:
            raise self.error
        return APP_TOKEN, 0.0


class Relay:
    def __init__(self, policy=None, minter=None):
        self.policy = policy or Policy()
        self.app_minter = minter or Minter()
        self.audit = Audit()
        self._unauth_auditor = UnauthAuditor()


class FakeUpstreamResponse:
    def __init__(self, status, content_type, body):
        self.status = status
        self._headers = {"Content-Type": content_type}
        self._body = [body, b""]

    def getheader(self, name, default=None):
        return self._headers.get(name, default)

    def read(self, _size=-1):
        return self._body.pop(0)


class FakeUpstream:
    def __init__(self, status=200, content_type=git_read._ADVERTISE, body=b"pack-data"):
        self.requests = []
        self.response = FakeUpstreamResponse(status, content_type, body)

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body, dict(headers or {})))

    def getresponse(self):
        return self.response

    def close(self):
        pass


class GitReadTest(unittest.TestCase):
    def serve(self, relay, upstream=None):
        self.relay = relay
        self.upstream = upstream or FakeUpstream()
        real_handle = git_read.handle
        patch_handle = mock.patch.object(
            git_read, "handle", lambda r, h, m: real_handle(r, h, m, connect=lambda: self.upstream)
        )
        patch_handle.start()
        self.addCleanup(patch_handle.stop)
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(relay))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server.server_address[1]

    def call(self, port, method, path, body=None, headers=None, bearer=BEARER):
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        sent = dict(headers or {})
        if bearer:
            sent["Authorization"] = "Bearer " + bearer
        conn.request(method, path, body=body, headers=sent)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp, data

    ADV = "/git/Simple-With-Us/Fleet-OPS.git/info/refs?service=git-upload-pack"
    PACK = "/git/Simple-With-Us/Fleet-OPS.git/git-upload-pack"

    def test_advertisement_uses_a_read_token_the_caller_never_sees(self):
        port = self.serve(Relay())
        resp, data = self.call(port, "GET", self.ADV, headers={"Git-Protocol": "version=2"})
        self.assertEqual(resp.status, 200)
        self.assertEqual(data, b"pack-data")
        self.assertEqual(resp.getheader("Content-Type"), git_read._ADVERTISE)
        self.assertEqual(
            self.relay.app_minter.calls,
            [("swu-claude", "Simple-With-Us/Fleet-OPS", {"contents": "read"})],
        )
        method, path, _body, headers = self.upstream.requests[0]
        self.assertEqual(
            (method, path),
            ("GET", "/Simple-With-Us/Fleet-OPS.git/info/refs?service=git-upload-pack"),
        )
        expected = base64.b64encode(f"x-access-token:{APP_TOKEN}".encode()).decode()
        self.assertEqual(headers["Authorization"], "Basic " + expected)
        self.assertEqual(headers["Git-Protocol"], "version=2")
        self.assertNotIn(BEARER, json.dumps(headers))
        self.assertNotIn(APP_TOKEN.encode(), data)
        self.assertEqual(
            self.relay.audit.rows[0][:4], ("CLAUDE", "git_read", "Simple-With-Us/Fleet-OPS", "ok")
        )
        self.assertNotIn(APP_TOKEN, json.dumps(self.relay.audit.rows))

    def test_a_remote_written_without_dot_git_reaches_the_same_repo(self):
        port = self.serve(Relay())
        resp, _ = self.call(
            port, "GET", "/git/Simple-With-Us/Fleet-OPS/info/refs?service=git-upload-pack"
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(
            self.upstream.requests[0][1],
            "/Simple-With-Us/Fleet-OPS.git/info/refs?service=git-upload-pack",
        )

    def test_upload_pack_forwards_the_body(self):
        upstream = FakeUpstream(content_type=git_read._RESULT, body=b"PACK")
        port = self.serve(Relay(), upstream)
        resp, data = self.call(
            port, "POST", self.PACK, body=b"0000", headers={"Content-Type": git_read._REQUEST}
        )
        self.assertEqual((resp.status, data), (200, b"PACK"))
        self.assertEqual(
            upstream.requests[0][:3],
            ("POST", "/Simple-With-Us/Fleet-OPS.git/git-upload-pack", b"0000"),
        )

    def test_push_is_refused_on_every_shape(self):
        port = self.serve(Relay())
        for method, path in (
            ("GET", "/git/Simple-With-Us/Fleet-OPS.git/info/refs?service=git-receive-pack"),
            ("POST", "/git/Simple-With-Us/Fleet-OPS.git/git-receive-pack"),
        ):
            resp, _ = self.call(port, method, path, body=b"0000" if method == "POST" else None)
            self.assertEqual(resp.status, 403, path)
        self.assertEqual(self.upstream.requests, [])
        self.assertEqual(self.relay.app_minter.calls, [])

    def test_only_the_two_git_paths_reach_upstream(self):
        port = self.serve(Relay())
        for path in (
            "/git/Simple-With-Us/Fleet-OPS.git/HEAD",
            "/git/Simple-With-Us/Fleet-OPS.git/objects/info/packs",
            "/git/Simple-With-Us/../other.git/info/refs?service=git-upload-pack",
            "/git/a/b/c.git/info/refs?service=git-upload-pack",
            "/git/Simple-With-Us/.git/info/refs?service=git-upload-pack",
            "/git/@evil.example/x.git/info/refs?service=git-upload-pack",
        ):
            resp, _ = self.call(port, "GET", path)
            self.assertEqual(resp.status, 404, path)
        resp, _ = self.call(port, "GET", "/git/Simple-With-Us/Fleet-OPS.git/info/refs")
        self.assertEqual(resp.status, 400)
        self.assertEqual(self.upstream.requests, [])

    def test_bad_bearer_is_refused_before_any_mint(self):
        port = self.serve(Relay())
        for bearer in ("wrong", None):
            resp, _ = self.call(port, "GET", self.ADV, bearer=bearer)
            self.assertEqual(resp.status, 401)
        self.assertEqual(self.relay.app_minter.calls, [])
        self.assertEqual(self.relay._unauth_auditor.count, 2)

    def test_policy_denial_is_audited_and_mints_nothing(self):
        port = self.serve(Relay(policy=Policy(denial="repo not allowed for this seat")))
        resp, _ = self.call(port, "GET", self.ADV)
        self.assertEqual(resp.status, 403)
        self.assertEqual(self.relay.app_minter.calls, [])
        self.assertEqual(self.relay.audit.rows[0][3], "denied")

    def test_ceiling_without_contents_is_refused(self):
        port = self.serve(Relay(policy=Policy(ceiling={"issues": "write"})))
        resp, _ = self.call(port, "GET", self.ADV)
        self.assertEqual(resp.status, 403)
        self.assertEqual(self.relay.app_minter.calls, [])

    def test_mint_failure_names_only_the_type(self):
        port = self.serve(Relay(minter=Minter(error=RuntimeError("secret detail"))))
        resp, data = self.call(port, "GET", self.ADV)
        self.assertEqual(resp.status, 503)
        self.assertNotIn(b"secret detail", data)
        self.assertEqual(self.relay.audit.rows[0][4]["reason"], "RuntimeError")

    def test_upstream_error_text_is_not_relayed(self):
        upstream = FakeUpstream(status=404, content_type="text/plain", body=b"upstream words")
        port = self.serve(Relay(), upstream)
        resp, data = self.call(port, "GET", self.ADV)
        self.assertEqual(resp.status, 404)
        self.assertNotIn(b"upstream words", data)

    def test_a_html_answer_with_status_200_is_not_passed_on(self):
        upstream = FakeUpstream(content_type="text/html", body=b"<html>")
        port = self.serve(Relay(), upstream)
        resp, data = self.call(port, "GET", self.ADV)
        self.assertEqual(resp.status, 502)
        self.assertNotIn(b"<html>", data)

    def test_a_json_call_to_the_action_is_refused_before_any_dispatch(self):
        from git_relay.server import _validate_args

        with self.assertRaisesRegex(ValueError, "no JSON call"):
            _validate_args("git_read", {"repo": "Simple-With-Us/Fleet-OPS"})

    def test_upload_needs_its_content_type_and_a_length(self):
        port = self.serve(Relay())
        resp, _ = self.call(
            port, "POST", self.PACK, body=b"0000", headers={"Content-Type": "text/plain"}
        )
        self.assertEqual(resp.status, 415)
        resp, _ = self.call(
            port,
            "POST",
            self.PACK,
            body=b"",
            headers={"Content-Type": git_read._REQUEST, "Content-Length": "0"},
        )
        self.assertEqual(resp.status, 411)

    def test_a_forged_git_protocol_header_is_dropped(self):
        port = self.serve(Relay())
        self.call(port, "GET", self.ADV, headers={"Git-Protocol": "version=2 x\tinjected"})
        self.assertNotIn("Git-Protocol", self.upstream.requests[0][3])

    def test_transfers_past_the_cap_are_refused_not_queued(self):
        port = self.serve(Relay())
        acquired = [git_read._slots.acquire(blocking=False) for _ in range(git_read.MAX_CONCURRENT)]
        self.addCleanup(lambda: [git_read._slots.release() for ok in acquired if ok])
        resp, _ = self.call(port, "GET", self.ADV)
        self.assertEqual(resp.status, 503)
        self.assertEqual(self.relay.app_minter.calls, [])


if __name__ == "__main__":
    unittest.main()
