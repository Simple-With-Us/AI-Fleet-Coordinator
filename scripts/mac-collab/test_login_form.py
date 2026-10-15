"""/login: an HTML form that mints the same 30-day session cookie as the Basic path.

Exists for browsers whose password manager cannot fill the native Basic dialog
(browser-only seats such as Instinct).  Never asserts on token values in any
response body -- those must stay secret.
"""
import http.client
import importlib.util
import os
import tempfile
import threading
import unittest
from http.cookies import SimpleCookie
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("mac_collab_server_login", HERE / "mac-collab-server.py")
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)

ROOT_TOKEN = "root-token-aaa-0123456789"
SEAT_TOKEN = "seat-token-bbb-0123456789"


class TestLoginForm(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.secrets = Path(cls.tmp.name) / "mac-collab.env"
        cls.secrets.write_text(
            'MAC_COLLAB_TOKEN="%s"\nMAC_COLLAB_TOKEN_WISE="%s"\n' % (ROOT_TOKEN, SEAT_TOKEN),
            encoding="utf-8",
        )
        cls._orig = (server.SECRETS, server.AUDIT_LOG, os.environ.get("MAC_COLLAB_TOKEN"))
        server.SECRETS = cls.secrets
        server.AUDIT_LOG = Path(cls.tmp.name) / "audit.log"
        server._LOGGED_FILE_WINS = False
        os.environ.pop("MAC_COLLAB_TOKEN", None)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        server.SECRETS, server.AUDIT_LOG, env_was = cls._orig
        server._LOGGED_FILE_WINS = False
        if env_was is not None:
            os.environ["MAC_COLLAB_TOKEN"] = env_was
        cls.tmp.cleanup()

    def setUp(self):
        server._AUTH_FAILS.clear()

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            data = resp.read().decode("utf-8", "replace")
            return resp.status, dict(resp.getheaders()), data
        finally:
            conn.close()

    def post_login(self, token):
        body = urlencode({"username": "board", "token": token})
        return self.request(
            "POST", "/login", body=body,
            headers={"Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(body))},
        )

    def test_get_login_page_is_a_fillable_form(self):
        status, headers, body = self.request("GET", "/login")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn('method="post" action="/login"', body)
        self.assertIn('name="token"', body)
        self.assertIn('type="password"', body)
        self.assertIn('autocomplete="current-password"', body)
        self.assertIn('autocomplete="username"', body)
        self.assertNotIn(ROOT_TOKEN, body)
        self.assertNotIn(SEAT_TOKEN, body)

    def test_wrong_token_is_401_without_cookie_or_echo(self):
        status, headers, body = self.post_login("not-a-real-token-xyz")
        self.assertEqual(status, 401)
        self.assertNotIn("Set-Cookie", headers)
        self.assertIn("Token not recognized", body)
        self.assertNotIn("not-a-real-token-xyz", body)
        self.assertNotIn(ROOT_TOKEN, body)

    def test_seat_token_mints_seat_cookie_and_redirects(self):
        status, headers, body = self.post_login(SEAT_TOKEN)
        self.assertEqual(status, 303)
        self.assertEqual(headers.get("Location"), "/board")
        jar = SimpleCookie()
        jar.load(headers["Set-Cookie"])
        morsel = jar[server.SESSION_COOKIE]
        self.assertEqual(server.parse_session_value(morsel.value), "INSTINCT")
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertNotIn(SEAT_TOKEN, headers["Set-Cookie"])
        # the cookie alone opens the board page
        status2, headers2, body2 = self.request(
            "GET", "/board", headers={"Cookie": "%s=%s" % (server.SESSION_COOKIE, morsel.value)}
        )
        self.assertEqual(status2, 200)
        self.assertIn("Fleet Findings Board", body2)

    def test_root_token_is_owner_identity(self):
        status, headers, _ = self.post_login(ROOT_TOKEN)
        self.assertEqual(status, 303)
        jar = SimpleCookie()
        jar.load(headers["Set-Cookie"])
        self.assertEqual(server.parse_session_value(jar[server.SESSION_COOKIE].value), "OWNER")

    def test_wrong_content_type_is_415(self):
        status, _, _ = self.request(
            "POST", "/login", body='{"token":"x"}',
            headers={"Content-Type": "application/json", "Content-Length": "13"},
        )
        self.assertEqual(status, 415)

    def test_failures_rate_limit_to_429(self):
        for _ in range(server.AUTH_FAIL_MAX):
            status, _, _ = self.post_login("wrong-" + "x" * 20)
            self.assertEqual(status, 401)
        status, _, body = self.post_login(SEAT_TOKEN)
        self.assertEqual(status, 429)
        self.assertIn("Too many failed attempts", body)

    def test_board_401_keeps_basic_and_links_the_form(self):
        status, headers, body = self.request("GET", "/board")
        self.assertEqual(status, 401)
        self.assertIn("Basic", headers.get("WWW-Authenticate", ""))
        self.assertIn('href="/login"', body)

    def test_audit_line_never_holds_the_token(self):
        self.post_login(SEAT_TOKEN)
        self.post_login("wrong-" + "y" * 20)
        log = server.AUDIT_LOG.read_text(encoding="utf-8")
        self.assertIn("action=login name=INSTINCT ok=1", log)
        self.assertIn("action=login name= ok=0", log)
        self.assertNotIn(SEAT_TOKEN, log)
        self.assertNotIn("wrong-yyyy", log)


if __name__ == "__main__":
    unittest.main()
