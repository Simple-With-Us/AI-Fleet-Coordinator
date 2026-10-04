"""fleet-recall-service (scripts/fleet-recall-service/server.py) against the in-process fake corpus.

    cd scripts && python3 -m unittest fleet_rag.tests.test_service -v

The server binds 127.0.0.1:0 in a thread; recall_api.install_fake_backend() replaces every
network seam, so no credentials, no Qdrant, no TEI, no gitleaks.
"""
from __future__ import annotations

import functools
import http.server
import importlib.machinery
import importlib.util
import io
import json
import os
import pathlib
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fleet_rag import recall_api
from fleet_rag.recall_api import FakeQdrant

SERVICE_DIR = pathlib.Path(__file__).resolve().parents[2] / "fleet-recall-service"
SERVER_PY = SERVICE_DIR / "server.py"
BOOTSTRAP_SH = SERVICE_DIR / "bootstrap.sh"
TOKEN = "test-token-0123456789"
GOOD_TEXT = ("pm2 start does not re-read env from the ecosystem file; restart with --update-env "
             "so the cached PATH is replaced.")


def _load_server():
    loader = importlib.machinery.SourceFileLoader("fleet_recall_service_server", str(SERVER_PY))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


server = _load_server()


class _ServiceCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        recall_api.install_fake_backend()
        cls._log = mock.patch.object(server, "log", lambda msg: None)
        cls._log.start()
        cls.httpd = server.make_server("127.0.0.1", 0, TOKEN)
        host, port = cls.httpd.server_address[:2]
        cls.base = f"http://{host}:{port}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls._log.stop()

    def setUp(self) -> None:
        recall_api.install_fake_backend()
        server.reset_health_cache()
        os.environ.pop("AGENT_SEAT", None)

    def request(self, method: str, path: str, body=None, token: str | None = TOKEN,
                headers: dict | None = None, raw: bytes | None = None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        hdrs = {"Content-Type": "application/json", **(headers or {})}
        if token is not None:
            hdrs["Authorization"] = "Bearer " + token
        req = Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            with urlopen(req, timeout=10) as resp:
                payload = resp.read()
                return resp.status, dict(resp.headers), (json.loads(payload) if payload else None)
        except HTTPError as e:
            with e:
                payload = e.read()
            return e.code, dict(e.headers), (json.loads(payload) if payload else None)

    def rpc(self, method: str, params=None, rid: int | None = 1, headers: dict | None = None, token=TOKEN):
        msg: dict = {"jsonrpc": "2.0", "method": method}
        if rid is not None:
            msg["id"] = rid
        if params is not None:
            msg["params"] = params
        return self.request("POST", "/mcp", msg, token=token, headers=headers)


class HealthTests(_ServiceCase):
    def test_health_is_public_and_reports_backend(self):
        status, headers, body = self.request("GET", "/health", token=None)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["name"], "fleet-recall-service")
        self.assertEqual(body["version"], server.SERVICE_VERSION)
        self.assertEqual(body["collection"], "fleet-agents-fake")
        self.assertEqual(body["points"], 3)
        self.assertTrue(body["backend_ok"])
        self.assertEqual(body["recall"], list(server.RECALL_PATHS))
        self.assertEqual(body["mcp"], "/mcp")
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_root_and_head_health(self):
        status, _, body = self.request("GET", "/", token=None)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        req = Request(self.base + "/health", method="HEAD")
        with urlopen(req, timeout=10) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"")

    def test_health_survives_backend_failure(self):
        class Boom(FakeQdrant):
            def info(self):
                raise ConnectionError("down")
        with mock.patch.object(recall_api, "Qdrant", Boom):
            status, _, body = self.request("GET", "/health", token=None)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertFalse(body["backend_ok"])
        self.assertIsNone(body["points"])
        self.assertEqual(body["error"], "ConnectionError")
        self.assertNotIn("down", json.dumps(body))

    def test_health_count_is_cached(self):
        self.request("GET", "/health", token=None)
        FakeQdrant.points.append({"id": "extra", "payload": {"source": "doc", "text": "x"}})
        _, _, body = self.request("GET", "/health", token=None)
        self.assertEqual(body["points"], 3)          # cached snapshot
        server.reset_health_cache()
        _, _, body = self.request("GET", "/health", token=None)
        self.assertEqual(body["points"], 4)


class AuthTests(_ServiceCase):
    def test_missing_token_is_401_everywhere_but_health(self):
        for method, path in (("GET", "/recall/stats"), ("POST", "/recall/search"),
                             ("POST", "/recall/contribute"), ("POST", "/mcp"), ("GET", "/mcp"),
                             ("DELETE", "/mcp")):
            status, headers, body = self.request(method, path, {"query": "x"}, token=None)
            self.assertEqual(status, 401, (method, path))
            self.assertEqual(headers.get("WWW-Authenticate"), "Bearer")
            self.assertEqual(body, {"ok": False, "error": "unauthorized"})

    def test_wrong_token_is_401(self):
        for bad in ("nope", TOKEN[:-1], TOKEN + "x", TOKEN.upper()):
            status, _, _ = self.request("GET", "/recall/stats", token=bad)
            self.assertEqual(status, 401, bad)
        status, _, _ = self.request("GET", "/recall/stats", token=None, headers={"Authorization": "Basic " + TOKEN})
        self.assertEqual(status, 401)
        status, _, _ = self.request("GET", "/recall/stats", token=None, headers={"Authorization": TOKEN})
        self.assertEqual(status, 401)

    def test_right_token_is_200(self):
        status, _, body = self.request("GET", "/recall/stats")
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        status, _, _ = self.request("GET", "/recall/stats", token=None, headers={"Authorization": "BEARER " + TOKEN})
        self.assertEqual(status, 200)                # scheme is case-insensitive

    def test_non_ascii_bearer_is_401_not_a_traceback(self):
        # http.server decodes header values as latin-1; hmac.compare_digest on str raises
        # TypeError for non-ASCII, which used to kill the handler thread instead of answering.
        bad = "\u00e9" * len(TOKEN)
        self.assertFalse(server.bearer_ok("Bearer " + bad, TOKEN))
        self.assertFalse(server.bearer_ok("Bearer " + bad[:-1] + "x", TOKEN))
        with mock.patch.object(self.httpd, "handle_error") as handle_error:
            status, headers, body = self.request("GET", "/recall/stats", token=bad)
            status2, _, _ = self.request("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, token=bad)
        self.assertEqual(status, 401)
        self.assertEqual(status2, 401)
        self.assertEqual(body, {"ok": False, "error": "unauthorized"})
        self.assertEqual(headers.get("WWW-Authenticate"), "Bearer")
        handle_error.assert_not_called()

    def test_bearer_ok_helper(self):
        self.assertTrue(server.bearer_ok("Bearer " + TOKEN, TOKEN))
        self.assertFalse(server.bearer_ok("Bearer " + TOKEN, ""))
        self.assertFalse(server.bearer_ok("", TOKEN))
        self.assertFalse(server.bearer_ok(None, TOKEN))
        self.assertFalse(server.bearer_ok("Bearer", TOKEN))
        self.assertFalse(server.bearer_ok("Bearer " + TOKEN[:-1], TOKEN))


def _read_pipelined(sock: socket.socket, timeout: float = 5.0) -> tuple[list[tuple[int, dict, bytes]], bool]:
    """Read until the server closes (or the timeout): ([(status, headers, body), ...], closed)."""
    sock.settimeout(timeout)
    buf = b""
    closed = False
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                closed = True
                break
            buf += chunk
    except socket.timeout:
        pass
    responses = []
    while buf.startswith(b"HTTP/"):
        head, sep, rest = buf.partition(b"\r\n\r\n")
        if not sep:
            break
        lines = head.split(b"\r\n")
        status = int(lines[0].split()[1])
        headers = {k.strip().lower(): v.strip() for k, v in (ln.decode().split(":", 1) for ln in lines[1:])}
        length = int(headers.get("content-length", "0"))
        responses.append((status, headers, rest[:length]))
        buf = rest[length:]
    return responses, closed


class KeepAliveTests(_ServiceCase):
    """Replies sent before the body is read must not let that body become the next request.

    Traefik pools HTTP/1.1 connections to the container; before the fix, an unauthenticated
    POST's JSON body was parsed as a new request line (400 / 404 for the *next* caller).
    """

    def _pipeline(self, first: bytes, second: bytes):
        host, port = self.httpd.server_address[:2]
        with socket.create_connection((host, port), timeout=5) as sock:
            sock.sendall(first + second)
            return _read_pipelined(sock)

    @staticmethod
    def _req(method: str, path: str, headers: dict, body: bytes = b"") -> bytes:
        lines = [f"{method} {path} HTTP/1.1", "Host: t"] + [f"{k}: {v}" for k, v in headers.items()]
        if body:
            lines.append(f"Content-Length: {len(body)}")
        return ("\r\n".join(lines) + "\r\n\r\n").encode() + body

    def _assert_clean(self, responses, closed, first_status: int):
        self.assertTrue(responses, "no response at all")
        self.assertEqual(responses[0][0], first_status)
        self.assertEqual(responses[0][1].get("connection"), "close")
        statuses = [r[0] for r in responses]
        self.assertNotIn(400, statuses, "leftover body was parsed as a request")
        self.assertNotIn(404, statuses, "leftover body was parsed as a request")
        self.assertNotIn(501, statuses, "leftover body was parsed as a request")
        if len(responses) > 1:
            # Answered normally on the same socket (not the case with close, but allowed).
            self.assertEqual(responses[1][0], 200)
            self.assertEqual(json.loads(responses[1][2])["points"], 3)
        else:
            self.assertTrue(closed, "single response but the connection stayed open")

    def test_unauthenticated_post_with_body_then_authenticated_get(self):
        body = json.dumps({"query": "handoff grep trap", "limit": 2}).encode()
        first = self._req("POST", "/recall/search", {"Content-Type": "application/json"}, body)
        second = self._req("GET", "/recall/stats", {"Authorization": "Bearer " + TOKEN})
        responses, closed = self._pipeline(first, second)
        self._assert_clean(responses, closed, 401)

    def test_unauthenticated_mcp_post_then_authenticated_get(self):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
        first = self._req("POST", "/mcp", {"Authorization": "Bearer nope"}, body)
        second = self._req("GET", "/recall/stats", {"Authorization": "Bearer " + TOKEN})
        responses, closed = self._pipeline(first, second)
        self._assert_clean(responses, closed, 401)

    def test_oversized_post_then_authenticated_get(self):
        # The 413 path cannot drain MAX_BODY+1 bytes, so it must close instead.
        first = self._req("POST", "/recall/search", {"Authorization": "Bearer " + TOKEN,
                                                      "Content-Length": str(server.MAX_BODY + 1)})
        second = self._req("GET", "/recall/stats", {"Authorization": "Bearer " + TOKEN})
        responses, closed = self._pipeline(first, second)
        self._assert_clean(responses, closed, 413)

    def test_post_to_unknown_path_with_body_then_get(self):
        body = json.dumps({"query": "x"}).encode()
        first = self._req("POST", "/recall/nope", {"Authorization": "Bearer " + TOKEN}, body)
        second = self._req("GET", "/recall/stats", {"Authorization": "Bearer " + TOKEN})
        responses, closed = self._pipeline(first, second)
        self.assertEqual(responses[0][0], 404)
        self.assertEqual(responses[0][1].get("connection"), "close")
        self.assertEqual(len(responses), 1)
        self.assertTrue(closed)

    def test_chunked_body_is_411_and_closes(self):
        first = self._req("POST", "/recall/search", {"Authorization": "Bearer " + TOKEN,
                                                      "Transfer-Encoding": "chunked"})
        second = b"5\r\n{\"q\":\r\n0\r\n\r\n"
        responses, closed = self._pipeline(first, second)
        self.assertEqual(responses[0][0], 411)
        self.assertEqual(responses[0][1].get("connection"), "close")
        self.assertTrue(closed)

    # -- GET / HEAD never read a body, so a stray one must be drained up front (2026-09-02).
    def test_get_health_with_body_then_authenticated_get(self):
        # Before the fix the JSON body stayed on the socket and the next request line was
        # `{"query": ...}` -> 501 for the pipelined caller.
        body = json.dumps({"query": "handoff grep trap", "limit": 2}).encode()
        first = self._req("GET", "/health", {"Content-Type": "application/json"}, body)
        second = self._req("GET", "/recall/stats", {"Authorization": "Bearer " + TOKEN})
        responses, closed = self._pipeline(first, second)
        self._assert_clean(responses, closed, 200)
        self.assertTrue(json.loads(responses[0][2])["ok"])

    def test_get_body_that_is_a_full_request_is_not_executed(self):
        # The nastier variant: a body that is itself a well-formed request used to be *executed*
        # as the next request on the connection.  Now it is drained and the socket closes.
        smuggled = self._req("GET", "/recall/stats", {"Authorization": "Bearer " + TOKEN})
        first = self._req("GET", "/health", {}, smuggled)
        responses, closed = self._pipeline(first, b"")
        self.assertEqual([r[0] for r in responses], [200])
        self.assertEqual(responses[0][1].get("connection"), "close")
        self.assertTrue(closed)

    def test_get_health_with_chunked_body_closes(self):
        first = self._req("GET", "/health", {"Transfer-Encoding": "chunked"})
        chunks = b"5\r\n{\"q\":\r\n0\r\n\r\n"
        second = self._req("GET", "/recall/stats", {"Authorization": "Bearer " + TOKEN})
        responses, closed = self._pipeline(first, chunks + second)
        self.assertEqual(responses[0][0], 200)
        self.assertEqual(responses[0][1].get("connection"), "close")
        self.assertEqual(len(responses), 1)
        self.assertTrue(closed)

    def test_head_with_body_then_get(self):
        body = json.dumps({"query": "x"}).encode()
        first = self._req("HEAD", "/health", {"Content-Type": "application/json"}, body)
        second = self._req("GET", "/recall/stats", {"Authorization": "Bearer " + TOKEN})
        responses, closed = self._pipeline(first, second)
        self._assert_clean(responses, closed, 200)
        self.assertEqual(responses[0][2], b"", "HEAD must not carry a body")
        self.assertTrue(closed)

    def test_unauthenticated_get_with_body_is_401_and_closes(self):
        # do_GET drains up front and _unauthorized() drains again: the second call must be a
        # no-op, not a blocking read of a body that is already gone.
        body = json.dumps({"query": "x"}).encode()
        first = self._req("GET", "/recall/stats", {"Content-Type": "application/json"}, body)
        second = self._req("GET", "/recall/stats", {"Authorization": "Bearer " + TOKEN})
        responses, closed = self._pipeline(first, second)
        self._assert_clean(responses, closed, 401)
        self.assertEqual(responses[0][1].get("www-authenticate"), "Bearer")

    def test_authenticated_get_with_body_answers_and_closes(self):
        body = json.dumps({"query": "x"}).encode()
        first = self._req("GET", "/recall/stats", {"Authorization": "Bearer " + TOKEN}, body)
        second = self._req("GET", "/health", {})
        responses, closed = self._pipeline(first, second)
        self._assert_clean(responses, closed, 200)
        self.assertEqual(json.loads(responses[0][2])["points"], 3)

    def test_get_without_body_still_keeps_alive(self):
        # The unconditional drain must not touch a body-less GET: the socket stays pooled.
        first = self._req("GET", "/health", {})
        second = self._req("GET", "/recall/stats", {"Authorization": "Bearer " + TOKEN,
                                                    "Connection": "close"})
        responses, closed = self._pipeline(first, second)
        self.assertEqual([r[0] for r in responses], [200, 200])
        self.assertNotIn("connection", responses[0][1])
        self.assertTrue(closed)

    def test_authenticated_keep_alive_still_works(self):
        # Two well-formed authenticated requests on one socket are both answered.
        body = json.dumps({"query": "handoff grep trap", "limit": 1}).encode()
        first = self._req("POST", "/recall/search", {"Authorization": "Bearer " + TOKEN,
                                                      "Content-Type": "application/json"}, body)
        second = self._req("GET", "/recall/stats", {"Authorization": "Bearer " + TOKEN,
                                                    "Connection": "close"})
        responses, closed = self._pipeline(first, second)
        self.assertEqual([r[0] for r in responses], [200, 200])
        self.assertNotIn("connection", responses[0][1])
        self.assertEqual(json.loads(responses[1][2])["points"], 3)
        self.assertTrue(closed)


class SocketTimeoutTests(_ServiceCase):
    """A client that declares a body and stalls must not park a handler thread forever.

    RecallHandler.timeout was None: an unauthenticated POST with Content-Length 500000 and ten
    bytes sent blocked in _discard_unread_body() until the peer went away.  Now the socket
    carries RECALL_SOCKET_TIMEOUT (15 s); the tests shrink it to 0.5 s.
    """

    SHORT = 0.5

    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        cls.httpd.RequestHandlerClass.timeout = cls.SHORT

    def _stall(self, first: bytes, partial: bytes, deadline: float = 4.0):
        host, port = self.httpd.server_address[:2]
        started = time.monotonic()
        with mock.patch.object(self.httpd, "handle_error") as handle_error, \
                socket.create_connection((host, port), timeout=deadline) as sock:
            sock.sendall(first + partial)         # ... and never send the rest
            responses, closed = _read_pipelined(sock, timeout=deadline)
        elapsed = time.monotonic() - started
        handle_error.assert_not_called()
        self.assertTrue(responses, "no response before the deadline")
        self.assertLess(elapsed, deadline, "reply did not arrive within the socket timeout")
        self.assertGreaterEqual(elapsed, self.SHORT * 0.5, "reply came before the drain could time out")
        self.assertEqual(responses[0][1].get("connection"), "close")
        self.assertEqual(len(responses), 1)
        self.assertTrue(closed)
        return responses[0]

    @staticmethod
    def _stalled_post(path: str, headers: dict) -> bytes:
        lines = [f"POST {path} HTTP/1.1", "Host: t", "Content-Length: 500000",
                 "Content-Type: application/json"] + [f"{k}: {v}" for k, v in headers.items()]
        return ("\r\n".join(lines) + "\r\n\r\n").encode()

    def test_unauthenticated_stalled_body_is_401_within_timeout(self):
        status, _, body = self._stall(self._stalled_post("/recall/search", {}), b'{"query":')
        self.assertEqual(status, 401)
        self.assertEqual(json.loads(body), {"ok": False, "error": "unauthorized"})

    def test_authenticated_stalled_body_is_408_within_timeout(self):
        first = self._stalled_post("/recall/search", {"Authorization": "Bearer " + TOKEN})
        status, _, body = self._stall(first, b'{"query":')
        self.assertEqual(status, 408)
        self.assertEqual(json.loads(body), {"ok": False, "error": "request body timed out"})

    def test_stalled_mcp_post_is_408_within_timeout(self):
        first = self._stalled_post("/mcp", {"Authorization": "Bearer " + TOKEN})
        status, _, _ = self._stall(first, b'{"jsonrpc":')
        self.assertEqual(status, 408)

    def test_stalled_get_body_still_answers_within_timeout(self):
        first = ("GET /health HTTP/1.1\r\nHost: t\r\nContent-Length: 500000\r\n\r\n").encode()
        status, _, body = self._stall(first, b"0123456789")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["ok"])

    def test_peer_closing_mid_body_is_400_not_a_traceback(self):
        host, port = self.httpd.server_address[:2]
        with mock.patch.object(self.httpd, "handle_error") as handle_error:
            with socket.create_connection((host, port), timeout=4) as sock:
                sock.sendall(self._stalled_post("/recall/search", {"Authorization": "Bearer " + TOKEN})
                             + b'{"query":')
                sock.shutdown(socket.SHUT_WR)
                responses, closed = _read_pipelined(sock, timeout=4)
        handle_error.assert_not_called()
        self.assertEqual(responses[0][0], 400)
        self.assertEqual(responses[0][1].get("connection"), "close")
        self.assertTrue(closed)

    def test_socket_timeout_helper_and_defaults(self):
        self.assertEqual(server.DEFAULT_SOCKET_TIMEOUT, 15.0)
        self.assertEqual(server.socket_timeout({}), 15.0)
        self.assertEqual(server.socket_timeout({"RECALL_SOCKET_TIMEOUT": "2.5"}), 2.5)
        self.assertEqual(server.socket_timeout({"RECALL_SOCKET_TIMEOUT": "30"}), 30.0)
        for bad in ("", "abc", "0", "-1", "nan"):
            self.assertEqual(server.socket_timeout({"RECALL_SOCKET_TIMEOUT": bad}), 15.0, bad)
        with mock.patch.dict(os.environ, {"RECALL_SOCKET_TIMEOUT": "7"}):
            self.assertEqual(server.socket_timeout(), 7.0)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("RECALL_SOCKET_TIMEOUT", None)
            self.assertEqual(server.socket_timeout(), 15.0)
        # The class attribute is never None (None means "block forever"), and make_server can
        # override it per instance without touching the base class.
        self.assertIsNotNone(server.RecallHandler.timeout)
        self.assertGreater(server.RecallHandler.timeout, 0)
        httpd = server.make_server("127.0.0.1", 0, TOKEN, timeout=0.25)
        try:
            self.assertEqual(httpd.RequestHandlerClass.timeout, 0.25)
            self.assertEqual(server.RecallHandler.timeout, server.socket_timeout())
        finally:
            httpd.server_close()


class RestTests(_ServiceCase):
    def test_stats(self):
        status, _, body = self.request("GET", "/recall/stats")
        self.assertEqual(status, 200)
        self.assertEqual(body["collection"], "fleet-agents-fake")
        self.assertEqual(body["points"], 3)
        self.assertEqual(body["by_source"]["doc"], 3)
        self.assertTrue(body["embedder_healthy"])
        # POST works too (seat-mcp parity).
        status, _, body2 = self.request("POST", "/recall/stats", {})
        self.assertEqual(status, 200)
        self.assertEqual(body2["points"], 3)

    def test_search(self):
        status, _, body = self.request("POST", "/recall/search",
                                       {"query": "leaking credentials handoff", "limit": "2", "app": "fleet"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["mode"], "hybrid")
        self.assertEqual(len(body["hits"]), 2)
        self.assertIn("global-api-keys", body["hits"][0]["text"])
        # recall_api may over-fetch for grouping / rerank; the contract is the returned hit count
        # (asserted above) and the filter that reached Qdrant.
        _, terms, limit, flt = FakeQdrant.calls[-1]
        self.assertGreaterEqual(limit, 2)
        self.assertIn({"key": "source", "match": {"value": "meta"}}, flt["must_not"])
        self.assertIn({"key": "app", "match": {"value": "fleet"}}, flt["must"])

    def test_search_per_doc_rerank_prefer_lessons_wired(self):
        # rerank=False, prefer_lessons=False must reach recall_search unchanged (mode stays
        # plain "hybrid", never "+rerank"), proving these are not silently dropped server-side.
        status, _, body = self.request("POST", "/recall/search",
                                       {"query": "leaking credentials handoff", "limit": 2,
                                        "per_doc": 2, "rerank": False, "prefer_lessons": False})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["mode"], "hybrid")

    def test_search_per_doc_rerank_prefer_lessons_validation(self):
        status, _, body = self.request("POST", "/recall/search", {"query": "x", "per_doc": "lots"})
        self.assertEqual(status, 400)
        self.assertIn("per_doc", body["error"])
        status, _, body = self.request("POST", "/recall/search", {"query": "x", "rerank": "yes"})
        self.assertEqual(status, 400)
        self.assertIn("rerank", body["error"])
        status, _, body = self.request("POST", "/recall/search", {"query": "x", "prefer_lessons": "no"})
        self.assertEqual(status, 400)
        self.assertIn("prefer_lessons", body["error"])

    def test_search_errors(self):
        status, _, body = self.request("POST", "/recall/search", {})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        self.assertIn("query", body["error"])
        status, _, body = self.request("POST", "/recall/search", {"query": "x", "limit": "lots"})
        self.assertEqual(status, 400)
        self.assertIn("limit", body["error"])
        status, _, body = self.request("POST", "/recall/search", {"query": "x", "bogus": 1})
        self.assertEqual(status, 400)
        self.assertIn("bogus", body["error"])
        status, _, body = self.request("POST", "/recall/search", {"query": "x", "category": "nope"})
        self.assertEqual(status, 400)
        self.assertIn("category", body["error"])
        status, _, body = self.request("POST", "/recall/search", raw=b"not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "parse error")
        status, _, body = self.request("POST", "/recall/search", raw=b"[1,2]")
        self.assertEqual(status, 400)
        self.assertIn("object", body["error"])
        status, _, _ = self.request("GET", "/recall/search")
        self.assertEqual(status, 405)

    def test_contribute_requires_seat_and_ignores_process_agent_seat(self):
        os.environ["AGENT_SEAT"] = "SERVERSEAT"
        try:
            status, _, body = self.request("POST", "/recall/contribute", {"text": GOOD_TEXT, "category": "lesson"})
        finally:
            os.environ.pop("AGENT_SEAT", None)
        self.assertEqual(status, 400)
        self.assertIn("seat is required", body["error"])
        self.assertEqual(FakeQdrant.upserts, [])

    def test_contribute_round_trip(self):
        status, _, body = self.request("POST", "/recall/contribute",
                                       {"text": GOOD_TEXT, "category": "lesson", "seat": "cursor",
                                        "app": "fleet", "title": "pm2 env", "url": "https://example.com/pr/1"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertTrue(body["doc_id"].startswith("contrib/CURSOR/"))
        self.assertEqual(body["status"], "completed")
        self.assertEqual(len(FakeQdrant.upserts), 1)
        stored = FakeQdrant.upserts[0][0]["payload"]
        self.assertEqual(stored["seat"], "CURSOR")
        self.assertEqual(stored["source"], "agent-contribution")
        self.assertEqual(stored["url"], "https://example.com/pr/1")
        status, _, again = self.request("POST", "/recall/search", {"query": "update-env", "source": "agent-contribution"})
        self.assertEqual(status, 200)
        self.assertEqual(len(again["hits"]), 1)
        self.assertEqual(again["hits"][0]["seat"], "CURSOR")

    def test_contribute_validation(self):
        status, _, body = self.request("POST", "/recall/contribute", {"text": "too short", "category": "lesson", "seat": "X"})
        self.assertEqual(status, 400)
        self.assertIn("too short", body["error"])
        status, _, body = self.request("POST", "/recall/contribute", {"text": GOOD_TEXT, "category": "doc", "seat": "X"})
        self.assertEqual(status, 400)
        self.assertIn("category", body["error"])

    def test_backend_failure_is_502_class_only(self):
        class Boom(FakeQdrant):
            def query_hybrid(self, *a, **k):
                raise RuntimeError("secret-ish detail http://10.0.0.1")
        with mock.patch.object(recall_api, "Qdrant", Boom):
            status, _, body = self.request("POST", "/recall/search", {"query": "anything at all"})
        self.assertEqual(status, 502)
        self.assertEqual(body["error"], "recall_search failed: RuntimeError")

    def test_unknown_paths_and_payload_limit(self):
        status, _, body = self.request("GET", "/nope")
        self.assertEqual(status, 404)
        status, _, _ = self.request("POST", "/recall/nope", {})
        self.assertEqual(status, 404)
        status, _, body = self.request("POST", "/recall/search", token=TOKEN,
                                       headers={"Content-Length": str(server.MAX_BODY + 1)}, raw=b"")
        self.assertEqual(status, 413)


class McpTests(_ServiceCase):
    def test_initialize_sets_session_id_and_echoes_known_version(self):
        status, headers, body = self.rpc("initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                                        "clientInfo": {"name": "t", "version": "0"}})
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], 1)
        res = body["result"]
        self.assertEqual(res["protocolVersion"], "2025-03-26")
        self.assertEqual(res["serverInfo"]["name"], "fleet-recall-service")
        self.assertEqual(res["capabilities"], {"tools": {"listChanged": False}})
        self.assertIn("recall_search", res["instructions"])
        sid = headers.get("MCP-Session-Id")
        self.assertTrue(sid and len(sid) == 32)
        # Unknown / missing protocol version falls back to the default.
        _, _, body = self.rpc("initialize", {"protocolVersion": "1999-01-01"})
        self.assertEqual(body["result"]["protocolVersion"], server.DEFAULT_PROTOCOL)
        _, _, body = self.rpc("initialize", {})
        self.assertEqual(body["result"]["protocolVersion"], server.DEFAULT_PROTOCOL)

    def test_notification_is_202_with_no_body(self):
        status, headers, body = self.rpc("notifications/initialized", rid=None)
        self.assertEqual(status, 202)
        self.assertIsNone(body)
        self.assertNotIn("MCP-Session-Id", headers)

    def test_id_null_is_a_notification(self):
        # JSON-RPC 2.0: null is what a Response uses for an unknown id, so a Request carrying
        # id: null is treated as a notification (no response) rather than answered with id null.
        for method in ("ping", "tools/list", "resources/list", "notifications/initialized"):
            status, headers, body = self.request("POST", "/mcp", {"jsonrpc": "2.0", "id": None, "method": method})
            self.assertEqual(status, 202, method)
            self.assertIsNone(body, method)
            self.assertNotIn("MCP-Session-Id", headers)
        self.assertIsNone(server.handle_rpc({"jsonrpc": "2.0", "id": None, "method": "ping"}))
        self.assertIsNone(server.handle_rpc({"jsonrpc": "2.0", "id": None, "method": "tools/list", "params": [1]}))
        self.assertIsNone(server.handle_rpc({"jsonrpc": "2.0", "id": None}))
        # id 0 and id "" are real ids.
        self.assertEqual(server.handle_rpc({"jsonrpc": "2.0", "id": 0, "method": "ping"})["id"], 0)
        self.assertEqual(server.handle_rpc({"jsonrpc": "2.0", "id": "", "method": "ping"})["id"], "")

    def test_ping_list_discover(self):
        _, _, body = self.rpc("ping", rid=7)
        self.assertEqual(body, {"jsonrpc": "2.0", "id": 7, "result": {}})
        _, _, body = self.rpc("tools/list", rid=8)
        tools = body["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], ["recall_search", "recall_stats", "recall_contribute"])
        for t in tools:
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertTrue(t["description"])
        self.assertEqual(tools[0]["inputSchema"]["required"], ["query"])
        self.assertEqual(tools[2]["inputSchema"]["required"], ["text", "category", "seat"])
        _, _, body = self.rpc("server/discover", rid=9)
        self.assertEqual(body["result"]["protocolVersions"], list(server.PROTOCOL_VERSIONS))
        self.assertEqual(len(body["result"]["tools"]), 3)

    def test_tools_call_stats_search_contribute(self):
        _, _, body = self.rpc("tools/call", {"name": "recall_stats"}, rid=2)
        res = body["result"]
        self.assertFalse(res["isError"])
        self.assertEqual(res["structuredContent"]["points"], 3)
        self.assertEqual(json.loads(res["content"][0]["text"])["points"], 3)

        _, _, body = self.rpc("tools/call", {"name": "recall_search",
                                             "arguments": {"query": "leaking credentials handoff", "limit": "2"}}, rid=3)
        res = body["result"]
        self.assertFalse(res["isError"])
        self.assertEqual(res["structuredContent"]["mode"], "hybrid")
        self.assertEqual(len(res["structuredContent"]["hits"]), 2)

        _, _, body = self.rpc("tools/call", {"name": "recall_contribute",
                                             "arguments": {"text": GOOD_TEXT, "category": "lesson", "seat": "GROK"}}, rid=4)
        res = body["result"]
        self.assertFalse(res["isError"])
        self.assertTrue(res["structuredContent"]["doc_id"].startswith("contrib/GROK/"))

        _, _, body = self.rpc("tools/call", {"name": "recall_search",
                                             "arguments": {"query": "update-env", "source": "agent-contribution"}}, rid=5)
        self.assertEqual(body["result"]["structuredContent"]["hits"][0]["seat"], "GROK")

    def test_tools_call_errors(self):
        _, _, body = self.rpc("tools/call", {"name": "recall_contribute",
                                             "arguments": {"text": GOOD_TEXT, "category": "lesson"}}, rid=2)
        self.assertTrue(body["result"]["isError"])
        self.assertIn("seat is required", body["result"]["content"][0]["text"])
        _, _, body = self.rpc("tools/call", {"name": "recall_contribute",
                                             "arguments": {"text": "too short", "category": "lesson", "seat": "X"}}, rid=3)
        self.assertTrue(body["result"]["isError"])
        self.assertIn("too short", body["result"]["content"][0]["text"])
        _, _, body = self.rpc("tools/call", {"name": "nope", "arguments": {}}, rid=4)
        self.assertEqual(body["error"]["code"], -32602)
        _, _, body = self.rpc("tools/call", {"name": "recall_search", "arguments": [1]}, rid=5)
        self.assertEqual(body["error"]["code"], -32602)
        _, _, body = self.rpc("tools/call", {"name": "recall_search", "arguments": {"query": "x", "bogus": 1}}, rid=6)
        self.assertTrue(body["result"]["isError"])
        self.assertIn("bogus", body["result"]["content"][0]["text"])
        _, _, body = self.rpc("resources/list", rid=7)
        self.assertEqual(body["error"]["code"], -32601)
        _, _, body = self.rpc("tools/list", params=[1, 2], rid=8)
        self.assertEqual(body["error"]["code"], -32602)

    def test_framing_errors(self):
        status, _, body = self.request("POST", "/mcp", raw=b"not json")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], -32700)
        status, _, body = self.request("POST", "/mcp", raw=b"[1]")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], -32600)
        status, _, body = self.rpc("ping", headers={"Mcp-Method": "tools/list"})
        self.assertEqual(status, 400)
        self.assertIn("Mcp-Method", body["error"]["message"])
        status, _, body = self.rpc("ping", headers={"MCP-Protocol-Version": "1999-01-01"})
        self.assertEqual(status, 400)
        self.assertIn("MCP-Protocol-Version", body["error"]["message"])
        status, _, body = self.rpc("ping", headers={"MCP-Protocol-Version": "2025-11-25"})
        self.assertEqual(status, 200)
        status, _, body = self.request("GET", "/mcp")
        self.assertEqual(status, 405)
        status, _, body = self.request("DELETE", "/mcp")
        self.assertEqual(status, 405)

    def test_backend_failure_in_tool_reports_class_only(self):
        class Boom(FakeQdrant):
            def info(self):
                raise RuntimeError("api-key=abc")
        with mock.patch.object(recall_api, "Qdrant", Boom):
            _, _, body = self.rpc("tools/call", {"name": "recall_stats"}, rid=2)
        self.assertTrue(body["result"]["isError"])
        self.assertEqual(body["result"]["content"][0]["text"], "recall_stats failed: RuntimeError")


def _tgz(path: pathlib.Path, files: dict[str, bytes], executable: tuple[str, ...] = ()) -> None:
    with tarfile.open(path, "w:gz") as t:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name in executable else 0o644
            t.addfile(info, io.BytesIO(data))


class _QuietFiles(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):  # noqa: D401
        pass


class BootstrapTests(unittest.TestCase):
    """bootstrap.sh under RECALL_BOOTSTRAP_ONLY=1: source tarball + gitleaks release from a local
    http.server; the binary is a tiny shell script that prints the pinned version."""

    FAKE_GITLEAKS = b"#!/bin/sh\necho v8.30.1\n"

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="recall-bootstrap-")
        cls.www = pathlib.Path(cls.tmp.name) / "www"
        cls.www.mkdir()
        _tgz(cls.www / "src.tgz", {
            "AI-Fleet-Coordinator-main/scripts/fleet_rag/__init__.py": b"__version__ = 'test'\n",
            "AI-Fleet-Coordinator-main/scripts/fleet-recall-service/server.py": b"# stub\n",
            "AI-Fleet-Coordinator-main/README.md": b"not extracted\n",
        })
        _tgz(cls.www / "gitleaks_8.30.1_linux_arm64.tar.gz",
             {"gitleaks": cls.FAKE_GITLEAKS, "LICENSE": b"MIT\n"}, executable=("gitleaks",))
        _tgz(cls.www / "gitleaks-wrong-version.tgz",
             {"gitleaks": b"#!/bin/sh\necho 8.29.0\n"}, executable=("gitleaks",))
        _tgz(cls.www / "gitleaks-no-binary.tgz", {"README.md": b"nope\n"})
        (cls.www / "not-a-tarball.tgz").write_bytes(b"this is not gzip data at all, just bytes")
        handler = functools.partial(_QuietFiles, directory=str(cls.www))
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.httpd.daemon_threads = True
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        cls.thread.start()
        host, port = cls.httpd.server_address[:2]
        cls.base = f"http://{host}:{port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.tmp.cleanup()

    def run_bootstrap(self, gitleaks_url: str | None, **extra_env: str):
        work = pathlib.Path(tempfile.mkdtemp(prefix="run-", dir=self.tmp.name))
        env = {k: v for k, v in os.environ.items() if not k.startswith(("RECALL_", "GITLEAKS_"))}
        env.update({
            "RECALL_BOOTSTRAP_ONLY": "1",
            "RECALL_APP_DIR": str(work / "app"),
            "RECALL_TARBALL": str(work / "src.tgz"),
            "RECALL_TARBALL_URL": self.base + "/src.tgz",
            "RECALL_GITLEAKS_DIR": str(work / "bin"),
            "RECALL_GITLEAKS_MIN_BYTES": "1",
            **extra_env,
        })
        if gitleaks_url is not None:
            env["RECALL_GITLEAKS_URL"] = gitleaks_url
        proc = subprocess.run(["bash", str(BOOTSTRAP_SH)], env=env, capture_output=True, text=True, timeout=60)
        return proc, work

    def assert_no_binary(self, work: pathlib.Path) -> None:
        self.assertFalse((work / "bin" / "gitleaks").exists())
        self.assertFalse((work / "bin" / "gitleaks.staging").exists())

    def test_installs_gitleaks_from_release_tarball(self):
        proc, work = self.run_bootstrap(self.base + "/gitleaks_8.30.1_linux_arm64.tar.gz")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("bootstrap: extracted 2 files", proc.stdout)
        self.assertTrue((work / "app" / "fleet-recall-service" / "server.py").is_file())
        self.assertTrue((work / "app" / "fleet_rag" / "__init__.py").is_file())
        self.assertFalse((work / "app" / "README.md").exists())
        exe = work / "bin" / "gitleaks"
        self.assertTrue(exe.is_file())
        self.assertTrue(exe.stat().st_mode & stat.S_IXUSR)
        self.assertEqual(subprocess.run([str(exe), "version"], capture_output=True, text=True).stdout.strip(), "v8.30.1")
        self.assertIn("bootstrap: gitleaks: installed 8.30.1 at", proc.stdout)
        self.assertIn("RECALL_BOOTSTRAP_ONLY=1, not starting the server", proc.stdout)
        self.assertFalse((work / "bin" / "gitleaks.staging").exists())
        # Second run: reuses the tarball, sees the binary already at the pinned version.
        proc2 = subprocess.run(["bash", str(BOOTSTRAP_SH)], capture_output=True, text=True, timeout=60, env={
            **{k: v for k, v in os.environ.items() if not k.startswith(("RECALL_", "GITLEAKS_"))},
            "RECALL_BOOTSTRAP_ONLY": "1", "RECALL_APP_DIR": str(work / "app"),
            "RECALL_TARBALL": str(work / "src.tgz"), "RECALL_TARBALL_URL": self.base + "/missing.tgz",
            "RECALL_GITLEAKS_DIR": str(work / "bin"), "RECALL_GITLEAKS_URL": self.base + "/missing.tgz"})
        self.assertEqual(proc2.returncode, 0, proc2.stdout + proc2.stderr)
        self.assertIn("bootstrap: reusing", proc2.stdout)
        self.assertIn("already runs 8.30.1; nothing to do", proc2.stdout)

    def test_download_failure_is_logged_and_the_bootstrap_continues(self):
        proc, work = self.run_bootstrap(self.base + "/missing.tgz")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("bootstrap: gitleaks: download failed (HTTPError", proc.stdout)
        self.assertIn("continuing WITHOUT gitleaks; recall_contribute will report gitleaks-unavailable", proc.stdout)
        self.assertIn("RECALL_BOOTSTRAP_ONLY=1, not starting the server", proc.stdout)
        self.assert_no_binary(work)
        # A dead host is the same story (URLError), not a hang.
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead = s.getsockname()[1]
        proc, work = self.run_bootstrap(f"http://127.0.0.1:{dead}/gitleaks.tgz")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("download failed (URLError", proc.stdout)
        self.assert_no_binary(work)

    def test_bad_tarballs_are_rejected_and_the_bootstrap_continues(self):
        proc, work = self.run_bootstrap(self.base + "/gitleaks-wrong-version.tgz")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("does not run or does not report 8.30.1", proc.stdout)
        self.assert_no_binary(work)
        proc, work = self.run_bootstrap(self.base + "/gitleaks-no-binary.tgz")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("tarball contains no gitleaks binary", proc.stdout)
        self.assert_no_binary(work)
        proc, work = self.run_bootstrap(self.base + "/not-a-tarball.tgz")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("install failed (ReadError", proc.stdout)
        self.assert_no_binary(work)
        proc, work = self.run_bootstrap(self.base + "/gitleaks_8.30.1_linux_arm64.tar.gz",
                                        RECALL_GITLEAKS_MIN_BYTES="1000000")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("implausible tarball size", proc.stdout)
        self.assert_no_binary(work)
        proc, work = self.run_bootstrap(self.base + "/gitleaks_8.30.1_linux_arm64.tar.gz",
                                        RECALL_GITLEAKS_SHA256="0" * 64)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("sha256 mismatch", proc.stdout)
        self.assert_no_binary(work)

    def test_required_mode_aborts_on_failure(self):
        proc, work = self.run_bootstrap(self.base + "/missing.tgz", RECALL_GITLEAKS_REQUIRED="1")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("RECALL_GITLEAKS_REQUIRED=1, aborting", proc.stdout)
        self.assert_no_binary(work)

    def test_gitleaks_only_mode_skips_the_source_fetch(self):
        # The Dockerfile RUN step: no repo tarball, no server, just the binary.
        proc, work = self.run_bootstrap(self.base + "/gitleaks_8.30.1_linux_arm64.tar.gz",
                                        RECALL_GITLEAKS_ONLY="1", RECALL_TARBALL_URL=self.base + "/missing.tgz")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertNotIn("bootstrap: downloading", proc.stdout)
        self.assertFalse((work / "app").exists())
        self.assertTrue((work / "bin" / "gitleaks").is_file())
        self.assertIn("installed 8.30.1", proc.stdout)

    def test_skip_mode_and_default_url_shape(self):
        proc, work = self.run_bootstrap(self.base + "/gitleaks_8.30.1_linux_arm64.tar.gz", RECALL_GITLEAKS_SKIP="1")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("RECALL_GITLEAKS_SKIP=1, not installed", proc.stdout)
        self.assert_no_binary(work)
        # Without an override the URL is the pinned GitHub release for this machine's arch.
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            dead = s.getsockname()[1]
        proc, work = self.run_bootstrap(None, GITLEAKS_VERSION="v8.30.1",
                                        RECALL_TARBALL_URL=self.base + "/src.tgz",
                                        https_proxy=f"http://127.0.0.1:{dead}", HTTPS_PROXY=f"http://127.0.0.1:{dead}")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        arch = {"x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}[os.uname().machine]
        self.assertIn("bootstrap: gitleaks: downloading https://github.com/gitleaks/gitleaks/releases/download/"
                      f"v8.30.1/gitleaks_8.30.1_linux_{arch}.tar.gz", proc.stdout)
        self.assertIn("download failed", proc.stdout)
        self.assert_no_binary(work)


class ProcessTests(unittest.TestCase):
    """The real entry point: refuses to start without a token, serves the fake corpus with one."""

    def test_refuses_without_token(self):
        env = {k: v for k, v in os.environ.items() if k != "RECALL_API_TOKEN"}
        proc = subprocess.run([sys.executable, str(SERVER_PY)], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 2)
        # Fail-fast comes from the Infisical settings init: the required key is named,
        # and INFISICAL.md is pointed at.  The exit code (2) is unchanged.
        self.assertIn("RECALL_API_TOKEN", proc.stdout)
        self.assertIn("INFISICAL.md", proc.stdout)

    def test_fake_mode_serves_health_and_logs_no_token(self):
        import socket
        import time
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        env = {**os.environ, "RECALL_API_TOKEN": TOKEN, "RECALL_FAKE": "1", "HOST": "127.0.0.1", "PORT": str(port)}
        proc = subprocess.Popen([sys.executable, str(SERVER_PY)], env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        try:
            deadline = time.time() + 15
            body = None
            while time.time() < deadline:
                try:
                    with urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
                        body = json.loads(r.read())
                        break
                except OSError:
                    time.sleep(0.2)
            self.assertIsNotNone(body, "server did not come up")
            self.assertEqual(body["points"], 3)
            req = Request(f"http://127.0.0.1:{port}/recall/stats", headers={"Authorization": "Bearer " + TOKEN})
            with urlopen(req, timeout=5) as r:
                self.assertEqual(json.loads(r.read())["points"], 3)
        finally:
            proc.terminate()
            out, _ = proc.communicate(timeout=10)
        self.assertIn("FAKE", out)
        self.assertIn("listening on 127.0.0.1", out)
        self.assertNotIn(TOKEN, out)
        self.assertNotIn("Bearer", out)


if __name__ == "__main__":
    unittest.main()
