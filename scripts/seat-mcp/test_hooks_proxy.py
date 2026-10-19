"""/hooks/<endpoint>/<secret> proxy to the local webhook ingress, and its log redaction (AFC-359).

No live ingress:  urllib.request.urlopen is replaced inside the server thread, so nothing leaves the process."""

from __future__ import annotations

import email.message
import io
import json
import socket
import threading
import time
import unittest
import urllib.error
from http.server import ThreadingHTTPServer
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from seat_mcp import server

# Made-up capability path:  the shape of a real one, never a real value.
ENDPOINT = "wh_testEndpoint0000000"
SECRET = "whsec_testSecretValueNotReal"
HOOK_PATH = "/hooks/%s/%s" % (ENDPOINT, SECRET)


class FakeHandler(server.SeatHandler):
    token = "test-token"


class FakeResponse:
    def __init__(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.status = status
        self._body = body
        self.headers = email.message.Message()
        self.headers["Content-Type"] = content_type

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeIngress:
    """Records what the proxy forwarded and answers like the BotFleet ingress."""

    def __init__(
        self, status: int = 202, body: dict | None = None, fail: Exception | None = None
    ) -> None:
        self.status = status
        self.body = json.dumps(body if body is not None else {"accepted": True}).encode()
        self.fail = fail
        self.calls: list[dict] = []

    def __call__(self, req, timeout=None):  # urllib.request.urlopen signature
        self.calls.append(
            {
                "url": req.full_url,
                "method": req.get_method(),
                "data": req.data,
                "headers": {k.lower(): v for k, v in req.header_items()},
                "timeout": timeout,
            }
        )
        if self.fail is not None:
            raise self.fail
        if self.status >= 400:
            hdrs = email.message.Message()
            hdrs["Content-Type"] = "application/json"
            raise urllib.error.HTTPError(
                req.full_url, self.status, "error", hdrs, io.BytesIO(self.body)
            )
        return FakeResponse(self.status, self.body)


class HooksProxyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        host, port = cls.httpd.server_address
        cls.base = "http://%s:%s" % (host, port)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _post(self, path: str, body: bytes, headers: dict | None = None):
        req = Request(
            self.base + path,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode())
        except HTTPError as exc:
            with exc:
                return exc.code, json.loads(exc.read().decode())

    def test_post_is_forwarded_to_the_local_ingress(self) -> None:
        fake = FakeIngress(202, {"accepted": True, "status": "queued"})
        with mock.patch("urllib.request.urlopen", fake):
            code, payload = self._post(
                HOOK_PATH, b'{"action":"opened"}', {"X-GitHub-Event": "pull_request"}
            )
        self.assertEqual(code, 202)
        self.assertEqual(payload, {"accepted": True, "status": "queued"})
        self.assertEqual(len(fake.calls), 1)
        call = fake.calls[0]
        self.assertEqual(call["url"], "http://127.0.0.1:8800" + HOOK_PATH)
        self.assertEqual(call["method"], "POST")
        self.assertEqual(call["data"], b'{"action":"opened"}')
        self.assertEqual(call["headers"].get("x-github-event"), "pull_request")
        self.assertNotIn("host", call["headers"])

    def test_ingress_rejection_is_relayed(self) -> None:
        fake = FakeIngress(401, {"error": "Invalid webhook URL or secret"})
        with mock.patch("urllib.request.urlopen", fake):
            code, payload = self._post(HOOK_PATH, b"{}")
        self.assertEqual(code, 401)
        self.assertEqual(payload, {"error": "Invalid webhook URL or secret"})

    def test_ingress_down_is_a_502(self) -> None:
        fake = FakeIngress(fail=urllib.error.URLError("connection refused"))
        with mock.patch("urllib.request.urlopen", fake):
            code, payload = self._post(HOOK_PATH, b"{}")
        self.assertEqual(code, 502)
        self.assertFalse(payload["ok"])
        self.assertNotIn(SECRET, json.dumps(payload))

    def test_hook_path_is_never_logged(self) -> None:
        log = io.StringIO()
        fake = FakeIngress(202)
        with mock.patch("urllib.request.urlopen", fake), mock.patch("sys.stderr", log):
            self._post(HOOK_PATH, b"{}")
        text = log.getvalue()
        self.assertIn("/hooks/<redacted>", text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn(ENDPOINT, text)

    def test_other_paths_are_still_logged(self) -> None:
        log = io.StringIO()
        with mock.patch("sys.stderr", log):
            with urlopen(Request(self.base + "/health"), timeout=5) as resp:
                resp.read()
        self.assertIn("/health", log.getvalue())
        self.assertNotIn("<redacted>", log.getvalue())

    def _raw(self, request: bytes, then: bytes = b"", pause: float = 0.0) -> tuple[int, dict]:
        """Send a hand-built request on a raw socket;  return (status, json body)."""
        host, port = self.httpd.server_address
        with socket.create_connection((host, port), timeout=10) as sock:
            sock.sendall(request)
            if pause:
                time.sleep(pause)
            if then:
                try:
                    sock.sendall(then)
                except OSError:
                    pass
            chunks = []
            while True:
                try:
                    data = sock.recv(65536)
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)
        raw = b"".join(chunks)
        head, _, body = raw.partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1])
        return status, json.loads(body.decode() or "{}")

    def _head(self, extra: str) -> bytes:
        return (
            "POST %s HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n%s\r\n"
            % (HOOK_PATH, extra)
        ).encode()

    def test_oversized_body_is_413_without_reading_or_forwarding(self) -> None:
        fake = FakeIngress(202)
        with mock.patch("urllib.request.urlopen", fake):
            code, payload = self._raw(
                self._head("Content-Length: %d\r\n" % (server.MAX_BODY_BYTES + 1))
            )
        self.assertEqual(code, 413)
        self.assertFalse(payload["ok"])
        self.assertEqual(fake.calls, [])

    def test_missing_content_length_is_411(self) -> None:
        fake = FakeIngress(202)
        with mock.patch("urllib.request.urlopen", fake):
            code, _ = self._raw(self._head(""))
        self.assertEqual(code, 411)
        self.assertEqual(fake.calls, [])

    def test_chunked_body_is_411(self) -> None:
        fake = FakeIngress(202)
        with mock.patch("urllib.request.urlopen", fake):
            code, _ = self._raw(
                self._head("Transfer-Encoding: chunked\r\n"), b"2\r\n{}\r\n0\r\n\r\n"
            )
        self.assertEqual(code, 411)
        self.assertEqual(fake.calls, [])

    def test_invalid_or_negative_content_length_is_400(self) -> None:
        fake = FakeIngress(202)
        with mock.patch("urllib.request.urlopen", fake):
            for value in ("-5", "abc"):
                code, _ = self._raw(self._head("Content-Length: %s\r\n" % value))
                self.assertEqual(code, 400, value)
        self.assertEqual(fake.calls, [])

    def test_stalled_body_is_408(self) -> None:
        fake = FakeIngress(202)
        with (
            mock.patch("urllib.request.urlopen", fake),
            mock.patch.object(server, "HOOK_READ_TIMEOUT_SECONDS", 0.3),
        ):
            started = time.monotonic()
            code, payload = self._raw(self._head("Content-Length: 100\r\n") + b'{"a"')
        self.assertEqual(code, 408)
        self.assertFalse(payload["ok"])
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(fake.calls, [])

    def test_get_without_a_body_is_still_forwarded(self) -> None:
        fake = FakeIngress(405, {"error": "Webhooks accept POST requests"})
        with mock.patch("urllib.request.urlopen", fake):
            try:
                urlopen(Request(self.base + HOOK_PATH), timeout=5)
                code = 200
            except HTTPError as exc:
                with exc:
                    code = exc.code
        self.assertEqual(code, 405)
        self.assertEqual(fake.calls[0]["method"], "GET")
        self.assertIsNone(fake.calls[0]["data"])


if __name__ == "__main__":
    unittest.main()
