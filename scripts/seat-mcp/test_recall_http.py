"""REST /recall/* aliases.  No live Qdrant."""

from __future__ import annotations

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from seat_mcp import server


class FakeHandler(server.SeatHandler):
    token = "test-token"


def _start():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
    httpd.allow_reuse_address = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


class RecallHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = _start()
        host, port = cls.httpd.server_address
        cls.base = "http://%s:%s" % (host, port)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()

    def setUp(self) -> None:
        server.recall_stats = lambda _args: {"collection": "fleet-agents", "points": 3}  # type: ignore[method-assign]
        server.recall_search = lambda args: {"hits": [{"q": args.get("query")}], "mode": "hybrid"}  # type: ignore[method-assign]
        server.recall_contribute = lambda args: {"id": "x", "seat": args.get("seat")}  # type: ignore[method-assign]
        server.recall_correct = lambda args: {"id": "x", "revision": 2}  # type: ignore[method-assign]
        server.recall_retract = lambda args: {"id": "x", "retracted": True}  # type: ignore[method-assign]

    def _json(self, method: str, path: str, body=None, token: str = "test-token"):
        data = None if body is None else json.dumps(body).encode()
        headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
        req = Request(self.base + path, data=data, method=method, headers=headers)
        with urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())

    def test_health_lists_recall_paths(self) -> None:
        req = Request(self.base + "/health")
        with urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode())
        self.assertEqual(
            payload.get("recall"),
            [
                "/recall/stats",
                "/recall/search",
                "/recall/contribute",
                "/recall/correct",
                "/recall/retract",
            ],
        )

    def test_stats_requires_bearer(self) -> None:
        req = Request(self.base + "/recall/stats")
        with self.assertRaises(HTTPError) as ctx:
            urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 401)

    def test_mutation_aliases(self) -> None:
        code, payload = self._json(
            "POST",
            "/recall/correct",
            {"doc_id": "d", "text": "x" * 60, "reason": "current", "seat": "GROK"},
        )
        self.assertEqual((code, payload["revision"]), (200, 2))
        code, payload = self._json(
            "POST", "/recall/retract", {"doc_id": "d", "reason": "obsolete", "seat": "GROK"}
        )
        self.assertEqual((code, payload["retracted"]), (200, True))

    def test_stats_and_search(self) -> None:
        code, payload = self._json("GET", "/recall/stats")
        self.assertEqual(code, 200)
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload.get("points"), 3)
        code, payload = self._json("POST", "/recall/search", {"query": "pm2 orphan"})
        self.assertEqual(code, 200)
        self.assertEqual(payload["hits"][0]["q"], "pm2 orphan")


if __name__ == "__main__":
    unittest.main()
