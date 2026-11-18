"""GR-MCP reaches a Cloudflare-fronted relay from a seat host (AFC-672).

Cloudflare refused urllib's default User-Agent (error 1010) and Cloudflare
Access then answered 403 because the wrapper sent no service token.  The
wrapper must send a named User-Agent on every call, attach the Access
service-token headers only to an https relay, refuse a half-configured pair at
startup, and never follow a redirect with the bearer or the Access secret.
"""

import http.server
import io
import json
import threading
import unittest

from git_relay import mcp_server
from git_relay.mcp_server import RelayMcp, access_headers_from_env

CALL = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "comment_pr", "arguments": {"repo": "o/r", "number": 1, "body": "x"}},
}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def capture(url, access_headers=None):
    seen = []

    def opener(req, timeout):
        seen.append(req)
        return FakeResponse(b'{"ok": true}')

    reply = RelayMcp(url, "seat-token-value", opener=opener, access_headers=access_headers).handle(
        CALL
    )
    return reply, seen[0]


ACCESS = {"CF-Access-Client-Id": "cid.access", "CF-Access-Client-Secret": "csecret-value"}


class UserAgent(unittest.TestCase):
    def test_every_call_names_itself_instead_of_python_urllib(self):
        for url in ("https://relay.example.test", "http://127.0.0.1:8471"):
            reply, req = capture(url)
            self.assertNotIn("error", reply)
            self.assertEqual(req.get_header("User-agent"), mcp_server.USER_AGENT)
            self.assertNotIn("urllib", mcp_server.USER_AGENT.lower())


class AccessHeaders(unittest.TestCase):
    def test_https_relay_gets_the_service_token_headers(self):
        _, req = capture("https://relay.example.test", ACCESS)
        self.assertEqual(req.get_header("Cf-access-client-id"), "cid.access")
        self.assertEqual(req.get_header("Cf-access-client-secret"), "csecret-value")
        self.assertEqual(req.get_header("Authorization"), "Bearer seat-token-value")

    def test_loopback_http_relay_never_gets_the_access_secret(self):
        _, req = capture("http://127.0.0.1:8471", ACCESS)
        self.assertIsNone(req.get_header("Cf-access-client-id"))
        self.assertIsNone(req.get_header("Cf-access-client-secret"))

    def test_access_headers_cannot_override_the_bearer(self):
        _, req = capture(
            "https://relay.example.test", {**ACCESS, "Authorization": "Bearer someone-else"}
        )
        self.assertEqual(req.get_header("Authorization"), "Bearer seat-token-value")

    def test_no_access_config_sends_no_access_headers(self):
        _, req = capture("https://relay.example.test")
        self.assertIsNone(req.get_header("Cf-access-client-id"))


class AccessEnv(unittest.TestCase):
    def test_relay_specific_pair_wins_over_fleet_pair(self):
        env = {
            "GIT_RELAY_CF_ACCESS_CLIENT_ID": "relay-id",
            "GIT_RELAY_CF_ACCESS_CLIENT_SECRET": "relay-secret",
            "CF_ACCESS_CLIENT_ID": "fleet-id",
            "CF_ACCESS_CLIENT_SECRET": "fleet-secret",
        }
        self.assertEqual(
            access_headers_from_env(env),
            {"CF-Access-Client-Id": "relay-id", "CF-Access-Client-Secret": "relay-secret"},
        )

    def test_fleet_pair_is_the_fallback(self):
        env = {"CF_ACCESS_CLIENT_ID": "fleet-id", "CF_ACCESS_CLIENT_SECRET": "fleet-secret"}
        self.assertEqual(access_headers_from_env(env)["CF-Access-Client-Id"], "fleet-id")

    def test_nothing_set_is_empty(self):
        self.assertEqual(access_headers_from_env({}), {})

    def test_half_pair_is_a_startup_error_without_echoing_the_value(self):
        for env in (
            {"GIT_RELAY_CF_ACCESS_CLIENT_ID": "relay-id"},
            {"CF_ACCESS_CLIENT_SECRET": "fleet-secret-value"},
        ):
            with self.assertRaises(ValueError) as ctx:
                access_headers_from_env(env)
            for value in env.values():
                self.assertNotIn(value, str(ctx.exception))


class NoRedirect(unittest.TestCase):
    """A real HTTP round trip:  a 302 (an Access login bounce, say) is not followed."""

    def setUp(self):
        self.hits = []
        hits = self.hits

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                hits.append((self.path, self.headers.get("Authorization")))
                if self.path.startswith("/v1/"):
                    self.send_response(302)
                    self.send_header("Location", "/elsewhere")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                body = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST

            def log_message(self, *args):
                pass

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_redirect_is_refused_and_the_bearer_goes_nowhere_else(self):
        url = f"http://127.0.0.1:{self.server.server_port}"
        reply = RelayMcp(url, "seat-token-value").handle(CALL)
        self.assertEqual([path for path, _ in self.hits], ["/v1/comment_pr"])
        self.assertTrue(reply["result"]["isError"])
        error = json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual(error["http_status"], 302)
        self.assertNotIn("seat-token-value", json.dumps(reply))


if __name__ == "__main__":
    unittest.main()
