"""The Python side of the Operator notes API (AFC-486, docs/protocols/operator-notes.md):  the wire signature, the
https origin, the principal's key, the signed client against a fake Operator, the wait, and the one `call` that
simple-notes and the stdio tools share, on a Mac and off one.

Nothing here reaches a network, Infisical or the real fleet-secrets:  the opener, the key runner and the clock are
fakes, and the one test that needs real HTTP (a redirect is never followed) talks to a server on 127.0.0.1.

Run:  cd scripts && python3 -m unittest agent_sync.tests.test_notes_client
"""

from __future__ import annotations

import hashlib
import hmac
import http.server
import io
import json
import socket
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any
from unittest import mock

from agent_sync import notes as N
from agent_sync import notes_client as NC
from agent_sync import notes_wire as W
from agent_sync.idem import IdemStore

# Built from plain parts, so no key-looking literal sits in the source (the gitleaks job runs on this repo).
TEST_KEY = "notes-wire-test-vector-" + "x" * 16
NOW = 1761170400.0
SESSION = "11112222-3333-4444-5555-666677778888"
TITLE = "[AFC, Claude] Client Check"
MASTER = "⭐️ Background Jobs Master List"


def hdr(request: Any, name: str) -> str | None:
    """urllib stores header names capitalized ("X-agent-sync-principal")."""
    return request.get_header(name.capitalize())


def record(state: str, rid: str = "requestid-0001", *, action: str = "create", seat: str = "CLAUDE", title: str | None = TITLE, **fields: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "contract": W.CONTRACT,
        "seat": seat,
        "request_id": rid,
        "action": action,
        "principal": "SERVER",
        "state": state,
        "queued_at": "2026-10-22T22:00:00Z",
    }
    if title is not None:
        out["title"] = title
    out.update(fields)
    return out


class FakeResponse:
    def __init__(self, status: int, data: Any) -> None:
        self.status = status
        self.body = b"" if data is None else (data if isinstance(data, bytes) else json.dumps(data).encode())

    def read(self, limit: int = -1) -> bytes:
        return self.body if limit < 0 else self.body[:limit]

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeOperator:
    """The Operator notes API as an opener:  every request's signature is recomputed from the documented string and
    checked, then answered.  `records[(seat, rid)]` is the list of records a status read answers in turn (the last one
    repeats);  `answers[(method, path)]` overrides any route with (status, body);  `fail` is raised instead."""

    def __init__(self, key: str = TEST_KEY, principal: str = W.SERVER, clock: Any = None) -> None:
        self.key = key
        self.principal = principal
        self.clock = clock or (lambda: NOW)
        self.calls: list[dict[str, Any]] = []
        self.records: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.queued: dict[tuple[str, str], dict[str, Any]] = {}
        self.answers: dict[tuple[str, str], tuple[int, Any]] = {}
        self.log_entries: list[dict[str, Any]] = []
        self.fail: BaseException | None = None
        self.claims: list[dict[str, Any] | None] = []
        self.reports: list[dict[str, Any]] = []
        self.timeouts: list[float] = []

    def __call__(self, request: Any, timeout: float | None = None) -> FakeResponse:
        method = request.get_method()
        parts = urllib.parse.urlsplit(request.full_url)
        body = request.data or b""
        timestamp = hdr(request, W.TIMESTAMP_HEADER)
        principal = hdr(request, W.PRINCIPAL_HEADER)
        expected = hmac.new(
            self.key.encode(),
            ("agent-sync-notes/1\n%s\n%s\n%s\n%s\n%s\n%s" % (method, parts.path, parts.query, timestamp, principal, hashlib.sha256(body).hexdigest())).encode(),
            hashlib.sha256,
        ).hexdigest()
        assert hdr(request, W.SIGNATURE_HEADER) == expected, "the signature does not match the documented string"
        assert principal == self.principal, principal
        assert timestamp == str(int(self.clock())), timestamp
        assert (hdr(request, "Content-Type") == "application/json") == bool(body)
        assert method != "GET" or not body
        payload = json.loads(body) if body else None
        self.calls.append({"method": method, "path": parts.path, "query": parts.query, "payload": payload})
        self.timeouts.append(timeout)
        if self.fail is not None:
            raise self.fail
        status, data = self.route(method, parts.path, parts.query, payload)
        if status >= 400:
            raw = data if isinstance(data, bytes) else (b"" if data is None else json.dumps(data).encode())
            raise urllib.error.HTTPError(request.full_url, status, "error", {}, io.BytesIO(raw))
        return FakeResponse(status, data)

    def route(self, method: str, path: str, query: str, payload: Any) -> tuple[int, Any]:
        if (method, path) in self.answers:
            return self.answers[(method, path)]
        if method == "POST" and path == "/notes/v1/requests":
            seat, req = payload["seat"], N.check_request(payload["request"])
            key = (seat, req["request_id"])
            if key in self.queued:
                current = self.records.get(key, [self.queued[key]])[0]
                return 200, current
            self.queued[key] = record("queued", req["request_id"], action=req["action"], seat=seat, title=req.get("title"))
            return 202, self.queued[key]
        if method == "GET" and path.startswith("/notes/v1/requests/"):
            seat, rid = path[len("/notes/v1/requests/") :].split("/")
            script = self.records.get((seat, rid))
            if not script:
                if (seat, rid) in self.queued:
                    return 200, self.queued[(seat, rid)]
                return 404, {"error": "not_found", "message": "no request %s for %s" % (rid, seat)}
            return 200, (script.pop(0) if len(script) > 1 else script[0])
        if method == "GET" and path == "/notes/v1/log":
            return 200, {"entries": self.log_entries}
        if method == "POST" and path == "/notes/v1/runner/claim":
            claimed = self.claims.pop(0) if self.claims else None
            return (204, None) if claimed is None else (200, claimed)
        if method == "POST" and path == "/notes/v1/runner/result":
            self.reports.append(payload)
            return 200, {"state": "done", "seat": payload["seat"], "request_id": payload["request_id"]}
        if method == "POST" and path == "/notes/v1/runner/log":
            return 200, {"accepted": len(payload["lines"]), "duplicates": 0}
        return 404, {"error": "not_found", "message": "no such path"}

    def client(self, sleeps: list[float] | None = None, principal: str | None = None) -> NC.OperatorClient:
        sleeps = sleeps if sleeps is not None else []
        return NC.OperatorClient(
            "https://ops.example.test",
            principal or self.principal,
            lambda: self.key,
            clock=self.clock,
            sleep=sleeps.append,
            opener=self,
        )


class WireTests(unittest.TestCase):
    """The signing string the Operator, the Python clients and the hosted Worker (src/notes.js) all compute."""

    BODY = b'{"seat":"CLAUDE","request":{"v":2}}'
    # Vectors for the JS side to copy:  key TEST_KEY, timestamp 1761170400.
    POST_VECTOR = "eee8cb8381398685f7a72fa15d5b4e07fd954dbcc80e0a2b4a007764d71dafb6"
    GET_VECTOR = "b5e4bc6d3b769594f251fd7c674a2f7cd49aed7b7ba4ca02aa9ac68a1a342236"

    def documented(self, method, path, query, timestamp, principal, body):
        text = "agent-sync-notes/1\n%s\n%s\n%s\n%s\n%s\n%s" % (method, path, query, timestamp, principal, hashlib.sha256(body).hexdigest())
        return hmac.new(TEST_KEY.encode(), text.encode(), hashlib.sha256).hexdigest()

    def test_the_signature_is_the_documented_string_signed(self):
        self.assertEqual(hashlib.sha256(self.BODY).hexdigest(), "a2e108707e07bbed3acef3e759eee92bf6604498d7f26fe9f3757e3ce5c2016f")
        post = W.sign(TEST_KEY, "POST", "/notes/v1/requests", "", "1761170400", "SERVER", self.BODY)
        self.assertEqual(post, self.documented("POST", "/notes/v1/requests", "", "1761170400", "SERVER", self.BODY))
        self.assertEqual(post, self.POST_VECTOR)
        get = W.sign(TEST_KEY, "GET", "/notes/v1/log", "since_hours=24&limit=50", "1761170400", "WORKER", b"")
        self.assertEqual(get, self.documented("GET", "/notes/v1/log", "since_hours=24&limit=50", "1761170400", "WORKER", b""))
        self.assertEqual(get, self.GET_VECTOR)
        self.assertEqual(W.sign(TEST_KEY, "post", "/notes/v1/requests", "", "1761170400", "SERVER", self.BODY), post, "the method is upper-cased")
        self.assertEqual(W.signing_string("GET", "/p", "q=1", "5", "RUNNER", b""), ("agent-sync-notes/1\nGET\n/p\nq=1\n5\nRUNNER\n" + W.EMPTY_SHA256).encode())

    def test_every_part_is_signed(self):
        base = ("POST", "/notes/v1/requests", "", "1761170400", "SERVER", self.BODY)
        signed = W.sign(TEST_KEY, *base)
        for index, other in enumerate(("GET", "/notes/v1/requestz", "a=1", "1761170401", "WORKER", self.BODY + b" ")):
            changed = list(base)
            changed[index] = other
            with self.subTest(part=index):
                self.assertNotEqual(W.sign(TEST_KEY, *changed), signed)
        self.assertNotEqual(W.sign(TEST_KEY + "y", *base), signed)

    def test_the_headers(self):
        out = W.headers(TEST_KEY, "POST", "/notes/v1/requests", "", "SERVER", self.BODY, NOW + 0.9)
        self.assertEqual(
            out,
            {
                "X-Agent-Sync-Principal": "SERVER",
                "X-Agent-Sync-Timestamp": "1761170400",
                "X-Agent-Sync-Signature": self.POST_VECTOR,
                "Content-Type": "application/json",
            },
        )
        self.assertNotIn("Content-Type", W.headers(TEST_KEY, "GET", "/notes/v1/log", "", "WORKER", b"", NOW))
        self.assertEqual(W.KEY_NAMES, {"WORKER": "OPERATOR_NOTES_WORKER_KEY", "SERVER": "OPERATOR_NOTES_SERVER_KEY", "RUNNER": "OPERATOR_NOTES_RUNNER_KEY"})
        self.assertEqual((W.MAX_SKEW_S, W.MIN_KEY_LEN, W.MAX_BODY, W.CLAIM_WAIT_MAX), (300, 32, 4 * 1024 * 1024, 25))


class BaseUrlTests(unittest.TestCase):
    def test_the_environment_names_an_https_origin(self):
        for raw, expected in (
            ("https://ops.example.test", "https://ops.example.test"),
            ("  https://ops.example.test/  ", "https://ops.example.test"),
            ("https://ops.example.test:8443", "https://ops.example.test:8443"),
            ("http://ops.example.test", None),
            ("https://ops.example.test/notes/v1", None),
            ("https://ops.example.test?x=1", None),
            ("https://ops.example.test#frag", None),
            ("https://user:pw@ops.example.test", None),
            ("https://", None),
            ("ops.example.test", None),
        ):
            with self.subTest(raw):
                with mock.patch("fleet_knobs.knobs.resolve", side_effect=AssertionError("the knob is not read")):
                    self.assertEqual(NC.base_url({"OPERATOR_API_URL": raw}), expected)

    def test_without_the_variable_the_fleet_knob_is_read_and_a_failure_is_not_configured(self):
        from fleet_knobs import knobs

        with mock.patch("fleet_knobs.knobs.resolve", return_value=knobs.Knob("OPERATOR_API_URL", "https://knob.example.test", "infisical")) as resolve:
            self.assertEqual(NC.base_url({}), "https://knob.example.test")
        self.assertEqual(resolve.call_args.args, ("OPERATOR_API_URL", "", "str"))
        with mock.patch("fleet_knobs.knobs.resolve", return_value=knobs.Knob("OPERATOR_API_URL", "", "default")):
            self.assertIsNone(NC.base_url({}))
        with mock.patch("fleet_knobs.knobs.resolve", side_effect=RuntimeError("no Infisical")):
            self.assertIsNone(NC.base_url({}))
        with mock.patch.dict("sys.modules", {"fleet_knobs": None}):
            self.assertIsNone(NC.base_url({}))

    def test_operator_client_is_not_configured_without_an_origin(self):
        with mock.patch("fleet_knobs.knobs.resolve", side_effect=RuntimeError("no Infisical")):
            with self.assertRaises(N.NotesError) as caught:
                NC.operator_client({})
        self.assertEqual(caught.exception.code, "not_configured")
        self.assertIn("OPERATOR_API_URL", caught.exception.message)
        client = NC.operator_client({"OPERATOR_API_URL": "https://ops.example.test"}, W.RUNNER)
        self.assertEqual((client.base, client.principal, client.key.name), ("https://ops.example.test", "RUNNER", "OPERATOR_NOTES_RUNNER_KEY"))
        self.assertEqual(NC.operator_client({"OPERATOR_API_URL": "https://ops.example.test"}).principal, "SERVER")


class KeySourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.bin = Path(self.tmp.name) / "bin"
        self.bin.mkdir()
        self.now = [1000.0]
        self.runs: list[list[str]] = []
        self.answer = subprocess.CompletedProcess([], 0, '"%s"\n' % TEST_KEY, "")

    def fake_secrets(self):
        path = self.bin / "fleet-secrets"
        path.write_text("#!/bin/sh\nexit 99\n")  # never run:  the runner below stands in for it
        path.chmod(0o700)
        return path

    def runner(self, argv, **kwargs):
        self.runs.append(list(argv))
        self.kwargs = kwargs
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer

    def source(self, env):
        return NC.KeySource(W.SERVER, env, runner=self.runner, clock=lambda: self.now[0])

    def test_the_environment_comes_first_and_quotes_are_dropped(self):
        source = self.source({"OPERATOR_NOTES_SERVER_KEY": " '%s' " % TEST_KEY, "PATH": str(self.bin)})
        self.assertEqual(source.get(), TEST_KEY)
        self.assertEqual(self.runs, [])
        self.assertIsNone(self.source({"OPERATOR_NOTES_SERVER_KEY": "short", "PATH": str(self.bin)}).get(), "under 32 characters is no key")

    def test_else_fleet_secrets_is_asked_once_and_the_answer_is_cached(self):
        binary = self.fake_secrets()
        env = {"PATH": str(self.bin), "HOME": self.tmp.name}
        source = self.source(env)
        self.assertEqual(source.get(), TEST_KEY)
        self.assertEqual(self.runs, [[str(binary), "get", "OPERATOR_NOTES_SERVER_KEY", "--path", "/operator", "--project", "ai-fleet-coordinator"]])
        self.assertEqual((self.kwargs["capture_output"], self.kwargs["text"], self.kwargs["check"]), (True, True, False))
        self.assertEqual(self.kwargs["env"], env)
        self.now[0] += NC.KEY_CACHE_S - 1
        self.assertEqual(source.get(), TEST_KEY)
        self.assertEqual(len(self.runs), 1, "cached")
        self.now[0] += 2
        source.get()
        self.assertEqual(len(self.runs), 2, "read again after the cache time")
        source.forget()
        source.get()
        self.assertEqual(len(self.runs), 3, "forget drops it at once")

    def test_a_failing_or_missing_fleet_secrets_is_no_key(self):
        self.fake_secrets()
        env = {"PATH": str(self.bin)}
        self.answer = subprocess.CompletedProcess([], 3, "", "denied")
        self.assertIsNone(self.source(env).get())
        self.answer = OSError("cannot run")
        self.assertIsNone(self.source(env).get())
        # A hung lookup is not a missing key:  it says so, and that nothing was sent.
        self.answer = subprocess.TimeoutExpired("fleet-secrets", 30)
        with self.assertRaises(N.NotesError) as caught:
            self.source(env).get()
        self.assertEqual(caught.exception.code, "not_configured")
        self.assertIn("did not answer within 30 seconds", caught.exception.message)
        self.assertIn("nothing was sent", caught.exception.message)
        self.runs.clear()
        empty = Path(self.tmp.name) / "empty"
        empty.mkdir()
        with mock.patch.object(NC.os.path, "expanduser", return_value=str(empty / "nope")):
            self.assertIsNone(self.source({"PATH": str(empty)}).get())
        self.assertEqual(self.runs, [], "nothing to run")


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.op = FakeOperator()
        self.sleeps: list[float] = []
        self.client = self.op.client(self.sleeps)
        self.req = N.make_request("create", "CLAUDE", request_id="requestid-0001", title=TITLE, markdown="body", session=SESSION)

    def test_enqueue_signs_the_request_and_tells_a_repeat_apart(self):
        first, again = self.client.enqueue("CLAUDE", self.req), self.client.enqueue("CLAUDE", self.req)
        self.assertEqual((first[0]["state"], first[1]), ("queued", False))
        self.assertEqual(again[1], True, "200 is the record of the same request sent before")
        call = self.op.calls[0]
        self.assertEqual((call["method"], call["path"], call["query"]), ("POST", "/notes/v1/requests", ""))
        self.assertEqual(call["payload"], {"seat": "CLAUDE", "request": self.req})
        self.assertEqual(self.op.timeouts[0], NC.CALL_TIMEOUT_S)

    def test_record_log_claim_report_and_send_log(self):
        self.op.records[("CLAUDE", "requestid-0001")] = [record("done", result={"note_id": "x-coredata://n1"})]
        self.assertEqual(self.client.record("CLAUDE", "requestid-0001")["state"], "done")
        self.assertEqual(self.op.calls[-1]["path"], "/notes/v1/requests/CLAUDE/requestid-0001")
        self.op.log_entries = [{"id": "e1", "ts": "2026-10-22T22:00:00Z", "mode": "create", "body": "dropped"}, "not an entry"]
        entries = self.client.log({"since_hours": 24, "limit": 50, "seat": "CODEX", "action": None, "title": "Plan Notes"})
        self.assertEqual(entries, [{"id": "e1", "ts": "2026-10-22T22:00:00Z", "mode": "create"}])
        self.assertEqual(self.op.calls[-1]["query"], "since_hours=24&limit=50&by_seat=CODEX&title=Plan+Notes")
        runner = FakeOperator(principal=W.RUNNER)
        client = runner.client(principal=W.RUNNER)
        self.assertIsNone(client.claim(25))
        self.assertEqual(runner.timeouts[-1], NC.CALL_TIMEOUT_S + 25)
        runner.claims = [{"claim": "c1", "seat": "CLAUDE", "request_id": "requestid-0001", "request": self.req}]
        self.assertEqual(client.claim(0)["claim"], "c1")
        self.assertEqual(client.report("CLAUDE", "requestid-0001", "c1", result={"status": "done"}), 200)
        self.assertEqual(client.report("CLAUDE", "requestid-0002", "c2", error={"code": "not_found", "message": "m"}), 200)
        self.assertEqual(
            runner.reports,
            [
                {"seat": "CLAUDE", "request_id": "requestid-0001", "claim": "c1", "result": {"status": "done"}},
                {"seat": "CLAUDE", "request_id": "requestid-0002", "claim": "c2", "error": {"code": "not_found", "message": "m"}},
            ],
        )
        # Final only when it is the queue's own answer:  nothing to resend, so the runner lets it go.
        for status, body in (
            (404, {"error": "not_found", "message": "no such request"}),
            (409, {"error": "conflict", "message": "this claim does not hold the request"}),
            (400, {"error": "invalid_argument", "message": "the result does not fit the request's action"}),
            (413, {"error": "too_large", "message": "too large"}),
        ):
            with self.subTest(final=status):
                runner.answers[("POST", "/notes/v1/runner/result")] = (status, body)
                self.assertEqual(client.report("CLAUDE", "r", "c", result={}), status)
        # A report over the queue's body cap is answered 413 before it is sent (the queue would close on it, so a
        # sender only ever sees a reset):  the runner then sends a small error report and lets the request go.
        sent_before = len(runner.calls)
        huge = {"note": {"body": "\x01" * (W.MAX_BODY // 5)}}  # each control character is six bytes once escaped
        self.assertEqual(client.report("CLAUDE", "r", "c", result=huge), 413)
        self.assertEqual(len(runner.calls), sent_before, "nothing was sent")
        # Anything else leaves the outcome with the runner:  an empty 200, a proxy's 404 page, the queue's 404 to a
        # principal it holds no key for, a 503.
        for status, body in (
            (200, None),
            (200, {"state": "done", "seat": "CLAUDE", "request_id": "someone-else"}),
            (404, b"404 page not found"),
            (404, {"error": "not_found", "message": "not found"}),
            (503, {"error": "unavailable"}),
        ):
            with self.subTest(kept=status, body=str(body)[:30]):
                runner.answers[("POST", "/notes/v1/runner/result")] = (status, body)
                with self.assertRaises(N.NotesError):
                    client.report("CLAUDE", "r", "c", result={})
        self.assertEqual(client.send_log([{"id": "e1"}, {"id": "e2"}]), 2)

    def test_http_answers_map_to_stable_codes(self):
        for status, data, code, words in (
            (400, {"error": "invalid_argument", "message": "the title is bad"}, "invalid_argument", "the title is bad"),
            (400, {"error": "session_required", "message": "pass a session"}, "session_required", "pass a session"),
            (400, {"error": "weird"}, "invalid_argument", "the Operator refused the request"),
            # A rejected key is named by its variable and where it lives, never by any part of its value.
            (401, {"error": "unauthorized"}, "not_configured", "does not accept OPERATOR_NOTES_SERVER_KEY (Infisical AI Fleet Coordinator prod /operator) (HTTP 401)"),
            (403, {"error": "forbidden", "message": "seat CLAUDE is not a server seat"}, "forbidden", "not a server seat"),
            (404, None, "not_configured", "HTTP 404"),
            (404, {"error": "not_found", "message": "no request r1 for CLAUDE"}, "not_found", "no request r1"),
            (409, {"error": "conflict"}, "invalid_argument", "already used for a different request"),
            (413, None, "invalid_argument", "too large"),
            (503, {"error": "unavailable", "message": "the queue is full"}, "unavailable", "HTTP 503:  the queue is full"),
            (500, None, "unavailable", "HTTP 500"),
        ):
            with self.subTest(status=status, code=code):
                self.op.answers[("POST", "/notes/v1/requests")] = (status, data)
                with self.assertRaises(N.NotesError) as caught:
                    self.client.enqueue("CLAUDE", self.req)
                self.assertEqual(caught.exception.code, code)
                self.assertIn(words, caught.exception.message)
                self.assertNotIn(TEST_KEY[:8], caught.exception.message)
                direct = NC.answer_error(status, data, W.SERVER)
                self.assertEqual((direct.code, direct.message), (caught.exception.code, caught.exception.message))
        # Without a principal it says "this host's notes key".
        self.assertIn("this host's notes key", NC.answer_error(401, None).message)

    def test_a_401_drops_a_cached_key_so_a_rotation_is_picked_up(self):
        source = NC.KeySource(W.SERVER, {"OPERATOR_NOTES_SERVER_KEY": TEST_KEY})
        client = NC.OperatorClient("https://ops.example.test", W.SERVER, source, clock=lambda: NOW, opener=self.op)
        self.op.answers[("GET", "/notes/v1/requests/CLAUDE/requestid-0001")] = (401, {"error": "unauthorized"})
        source.get()
        self.assertIsNotNone(source._value)
        with self.assertRaises(N.NotesError) as caught:
            client.record("CLAUDE", "requestid-0001")
        self.assertEqual(caught.exception.code, "not_configured")
        self.assertIsNone(source._value)

    def test_no_key_network_failures_and_bodies_that_are_not_json(self):
        client = NC.OperatorClient("https://ops.example.test", W.SERVER, lambda: None, opener=self.op)
        with self.assertRaises(N.NotesError) as caught:
            client.enqueue("CLAUDE", self.req)
        self.assertEqual(caught.exception.code, "not_configured")
        self.assertIn("OPERATOR_NOTES_SERVER_KEY", caught.exception.message)
        self.assertEqual(self.op.calls, [], "nothing is sent unsigned")
        for failure in (urllib.error.URLError("refused"), TimeoutError("slow"), ConnectionResetError("reset")):
            with self.subTest(type(failure).__name__):
                self.op.fail = failure
                with self.assertRaises(N.NotesError) as caught:
                    self.client.enqueue("CLAUDE", self.req)
                self.assertEqual(caught.exception.code, "unavailable")
                self.assertIn("could not be reached", caught.exception.message)
        self.op.fail = None
        self.op.answers[("GET", "/notes/v1/log")] = (200, b"<html>proxy page</html>")
        with self.assertRaises(N.NotesError) as caught:
            self.client.log({})
        self.assertIn("not JSON", caught.exception.message)
        self.op.answers[("GET", "/notes/v1/log")] = (502, b"<html>bad gateway</html>")
        with self.assertRaises(N.NotesError) as caught:
            self.client.log({})
        self.assertEqual(caught.exception.code, "unavailable")
        # A nesting too deep to parse:  forced, since how deep json.loads goes before RecursionError depends on the
        # Python version (3.15 parses what 3.14 refuses).
        # A body over the cap is refused before it is parsed.
        self.op.answers[("GET", "/notes/v1/log")] = (200, b"[" + b"0," * (W.MAX_BODY // 2) + b"0]")
        with self.assertRaises(N.NotesError) as caught:
            self.client.log({})
        self.assertIn("larger than 4 MiB", caught.exception.message)
        self.op.answers[("GET", "/notes/v1/log")] = (200, b"[[[]]]")
        with mock.patch.object(NC.json, "loads", side_effect=RecursionError("maximum recursion depth exceeded")):
            with self.assertRaises(N.NotesError) as caught:
                self.client.log({})
        self.assertEqual(caught.exception.code, "unavailable")
        self.assertIn("not JSON", caught.exception.message)

    def test_a_redirect_is_never_followed(self):
        hits: list[str] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                hits.append(self.path)
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                self.send_response(302)
                self.send_header("Location", "/notes/v1/elsewhere")
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST  # noqa: N815

            def log_message(self, *args):
                return None

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client = NC.OperatorClient("http://127.0.0.1:%d" % server.server_address[1], W.SERVER, lambda: TEST_KEY)
        with self.assertRaises(N.NotesError) as caught:
            client.enqueue("CLAUDE", self.req)
        # The queue never redirects, so a redirect is a configuration fault, as the Worker reads it:  never an unknown
        # outcome (call passes not_configured through as it is).
        self.assertEqual(caught.exception.code, "not_configured")
        self.assertIn("redirect", caught.exception.message)
        self.assertIn("OPERATOR_API_URL", caught.exception.message)
        self.assertEqual(hits, ["/notes/v1/requests"], "the redirect target was never asked")
        self.assertTrue(any(isinstance(h, NC._NoRedirect) for h in NC._OPENER.handlers))


class WaitTests(unittest.TestCase):
    def setUp(self):
        self.op = FakeOperator()
        self.sleeps: list[float] = []
        self.client = self.op.client(self.sleeps)
        self.queued = record("queued")

    def test_it_stops_at_a_final_state(self):
        for final in ("done", "refused", "unknown", "expired"):
            with self.subTest(final):
                self.sleeps.clear()
                self.op.records[("CLAUDE", "requestid-0001")] = [record("queued"), record("running"), record(final)]
                self.assertEqual(self.client.wait("CLAUDE", self.queued, 300)["state"], final)
                self.assertEqual(self.sleeps, [1.0, 1.0, 1.0])

    def test_it_stops_when_the_wait_runs_out_and_polls_more_slowly_later(self):
        self.op.records[("CLAUDE", "requestid-0001")] = [record("running")]
        self.assertEqual(self.client.wait("CLAUDE", self.queued, 9.5)["state"], "running")
        self.assertEqual(self.sleeps, [1.0, 1.0, 1.0, 1.0, 1.0, 2.0, 2.0, 0.5])
        self.assertAlmostEqual(sum(self.sleeps), 9.5)

    def test_a_poll_that_cannot_reach_the_operator_ends_the_wait_with_the_record_it_had(self):
        # PR #876 review:  the request is already queued, so an error here would send the caller to retry under a new
        # request_id and write the note twice.  The wait ends, answering queued with the id.
        self.op.fail = urllib.error.URLError("connection reset")
        got = self.client.wait("CLAUDE", self.queued, 300)
        self.assertEqual((got["state"], got["request_id"]), ("queued", "requestid-0001"))
        self.assertEqual(self.sleeps, [1.0])
        self.op.fail = None
        # Any other failed poll (a 403, or a key lookup that fails after the enqueue) ends the wait the same way,
        # as the Worker's wait does:  the request is queued whatever the poll answered.
        self.op.answers[("GET", "/notes/v1/requests/CLAUDE/requestid-0001")] = (403, {"error": "forbidden", "message": "m"})
        got = self.client.wait("CLAUDE", self.queued, 300)
        self.assertEqual((got["state"], got["request_id"]), ("queued", "requestid-0001"))

    def test_wait_zero_and_an_already_final_record_answer_at_once(self):
        self.assertEqual(self.client.wait("CLAUDE", self.queued, 0), self.queued)
        # A record with no valid request_id is never polled by a made-up one ("None", ""):  refused outright.
        for bad in (None, "", "short"):
            with self.assertRaises(N.NotesError) as caught:
                self.client.wait("CLAUDE", dict(self.queued, request_id=bad), 300)
            self.assertEqual(caught.exception.code, "invalid_argument")
        self.assertEqual((self.sleeps, self.op.calls), ([], []))
        done = record("done")
        self.assertEqual(self.client.wait("CLAUDE", done, 300), done)
        self.assertEqual((self.sleeps, self.op.calls), ([], []))


class FakeClient:
    """An OperatorClient stand-in for `call`, `status` and `log` off a Mac."""

    def __init__(self, enqueued: dict[str, Any], *, duplicate: bool = False, waited: dict[str, Any] | None = None) -> None:
        self.enqueued, self.duplicate, self.waited = enqueued, duplicate, waited
        self.calls: list[tuple[Any, ...]] = []
        self.entries: list[dict[str, Any]] = []

    def enqueue(self, seat, request):
        self.calls.append(("enqueue", seat, dict(request)))
        return dict(self.enqueued), self.duplicate

    def wait(self, seat, current, seconds):
        self.calls.append(("wait", seat, current["request_id"], seconds))
        return dict(self.waited or current)

    def record(self, seat, request_id):
        self.calls.append(("record", seat, request_id))
        return dict(self.waited or self.enqueued)

    def log(self, filters):
        self.calls.append(("log", dict(filters)))
        return list(self.entries)


class HostTests(unittest.TestCase):
    SCRIPT = Path("/fake/apple-notes-coding.sh")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.store = IdemStore(str(self.home / "state" / "CLAUDE" / "mcp-idem.json"))
        self.env = {"HOME": str(self.home), "PATH": "/usr/bin"}
        self.runs: list[dict[str, Any]] = []
        self.answer: Any = {"status": "done", "note_id": "x-coredata://n1"}
        self.scans: list[dict[str, Any]] = []

    def fake_run(self, request, script, **kwargs):
        self.runs.append({"request": dict(request), "script": script, **kwargs})
        if isinstance(self.answer, BaseException):
            raise self.answer
        return dict(self.answer)

    def mac(self):
        patcher = mock.patch.object(N, "run_direct", self.fake_run)
        patcher.start()
        self.addCleanup(patcher.stop)
        return NC.Host(self.env, self.home, self.store, caller="mcp", script_finder=lambda env, home: self.SCRIPT)

    def elsewhere(self, client):
        made = []

        def factory():
            made.append(client)
            return client

        host = NC.Host(self.env, self.home, self.store, caller="cli", client_factory=factory, script_finder=lambda env, home: None)
        return host, made

    def req(self, action="create", rid="requestid-0001", **kwargs):
        defaults = {"title": TITLE if action == "create" else MASTER, "session": SESSION if action in N.BODY_ACTIONS else None}
        defaults.update(kwargs)
        if action == "list":
            defaults.pop("title")
        return N.make_request(action, "CLAUDE", request_id=rid, **defaults)

    def scan(self, request):
        self.scans.append(dict(request))

    def test_the_queues_own_503_is_a_definite_refusal_and_a_bare_5xx_is_unknown(self):
        op = FakeOperator()
        host, _ = self.elsewhere(op.client([]))
        op.answers[("POST", "/notes/v1/requests")] = (503, {"error": "unavailable", "message": "the notes queue is full;  try again later"})
        with self.assertRaises(N.NotesError) as caught:
            NC.call(host, "CLAUDE", self.req(rid="requestid-0043"), wait=0, scan=self.scan)
        self.assertEqual(caught.exception.code, "unavailable")  # nothing was queued
        self.assertIn("the notes queue is full", caught.exception.message)
        # So is a 429:  a rate limit turns the request away before anything is queued, as the Worker reads it too.
        op.answers[("POST", "/notes/v1/requests")] = (429, b"Too Many Requests")
        with self.assertRaises(N.NotesError) as caught:
            NC.call(host, "CLAUDE", self.req(rid="requestid-0048"), wait=0, scan=self.scan)
        self.assertEqual(caught.exception.code, "unavailable")
        # A refused connection or a name that does not resolve sent nothing:  a plain, retryable unavailable.
        for reason in (ConnectionRefusedError(111, "refused"), socket.gaierror(-2, "Name or service not known")):
            with self.subTest(type(reason).__name__):
                op.fail = urllib.error.URLError(reason)
                with self.assertRaises(N.NotesError) as caught:
                    NC.call(host, "CLAUDE", self.req(rid="requestid-0046"), wait=0, scan=self.scan)
                self.assertEqual(caught.exception.code, "unavailable")
        op.fail = urllib.error.URLError(ConnectionResetError(104, "reset"))  # may have been sent
        with self.assertRaises(N.NotesError) as caught:
            NC.call(host, "CLAUDE", self.req(rid="requestid-0047"), wait=0, scan=self.scan)
        self.assertEqual(caught.exception.code, "outcome_unknown")
        op.fail = None
        # An enqueue answered with another request's record may still have queued this one:  outcome_unknown.
        op.answers[("POST", "/notes/v1/requests")] = (202, record("queued", "someone-else-01"))
        with self.assertRaises(N.NotesError) as caught:
            NC.call(host, "CLAUDE", self.req(rid="requestid-0045"), wait=0, scan=self.scan)
        self.assertEqual(caught.exception.code, "outcome_unknown")
        self.assertIn("another request", caught.exception.message)
        for answer in ((503, b"<html>no available server</html>"), (502, None)):
            with self.subTest(answer[0]):
                op.answers[("POST", "/notes/v1/requests")] = answer
                with self.assertRaises(N.NotesError) as caught:
                    NC.call(host, "CLAUDE", self.req(rid="requestid-0044"), wait=0, scan=self.scan)
                self.assertEqual(caught.exception.code, "outcome_unknown")  # a proxy's 5xx may have queued it

    def test_a_lost_answer_to_the_post_tells_the_caller_to_resend_with_the_same_request_id(self):
        class Unreachable(FakeClient):
            def enqueue(self, seat, request):
                raise N.NotesError("unavailable", "the Operator notes API could not be reached (TimeoutError)")

        host, _ = self.elsewhere(Unreachable(record("queued")))
        with self.assertRaises(N.NotesError) as caught:
            NC.call(host, "CLAUDE", self.req(rid="requestid-0042"), wait=30, scan=self.scan)
        # The request may have reached the queue before the answer was lost:  its outcome is unknown, not retryable.
        self.assertEqual(caught.exception.code, "outcome_unknown")
        self.assertIn("may or may not be queued", caught.exception.message)
        self.assertIn("simple-notes status requestid-0042", caught.exception.message)
        self.assertIn("same request_id, requestid-0042, never a new one", caught.exception.message)

    def test_the_mac_seam_is_notes_direct_available_looked_up_when_the_host_is_made(self):
        with mock.patch.object(N, "direct_available", return_value=self.SCRIPT) as found:
            host = NC.Host(self.env, self.home, self.store, caller="cli")
        self.assertEqual(host.script, self.SCRIPT)
        self.assertEqual(found.call_args.args, (self.env, self.home))
        with mock.patch.object(N, "direct_available", return_value=None):
            self.assertIsNone(NC.Host(self.env, self.home, self.store, caller="cli").script)

    def test_a_mac_write_runs_directly_once_per_request_id(self):
        host = self.mac()
        first = NC.call(host, "CLAUDE", self.req(), wait=90, scan=self.scan)
        self.assertEqual(
            first,
            {
                "request_id": "requestid-0001",
                "action": "create",
                "title": TITLE,
                "transport": "direct",
                "status": "done",
                "note_id": "x-coredata://n1",
                "duplicate": False,
            },
        )
        again = NC.call(host, "CLAUDE", self.req(), wait=90, scan=self.scan)
        self.assertTrue(again["duplicate"])
        self.assertEqual(len(self.runs), 1)
        self.assertEqual((self.runs[0]["script"], self.runs[0]["caller"]), (self.SCRIPT, "mcp"))
        self.assertEqual(len(self.scans), 2, "every write is scanned, a repeat too")
        self.assertEqual(self.store.row("k:notes-direct:requestid-0001")["state"], "sent")
        status = NC.status(host, "CLAUDE", "requestid-0001")
        self.assertEqual((status["status"], status["transport"], status["note_id"]), ("done", "direct", "x-coredata://n1"))

    def test_a_mac_read_or_list_skips_the_idempotency_rows_and_the_scan(self):
        host = self.mac()
        self.answer = {"note": {"title": MASTER, "body": "<div>x</div>"}}
        for _ in range(2):
            out = NC.call(host, "CLAUDE", self.req("read", rid="readrequest-01"), wait=90, scan=self.scan)
        self.assertEqual((out["status"], out["transport"], out["note"]["body"]), ("done", "direct", "<div>x</div>"))
        self.assertNotIn("duplicate", out)
        # Its status row costs one write of the idempotency file, not two.
        with mock.patch.object(self.store, "mutate", wraps=self.store.mutate) as writes:
            NC.call(host, "CLAUDE", self.req("read", rid="readrequest-02"), wait=90, scan=self.scan)
        self.assertEqual(writes.call_count, 1)
        self.answer = {"notes": [], "total": 0, "truncated": False}
        listed = NC.call(host, "CLAUDE", self.req("list", rid="listrequest-01"), wait=90, scan=self.scan)
        self.assertEqual((listed["status"], listed["notes"], listed["total"]), ("done", [], 0))
        self.assertEqual(len(self.runs), 4, "a read runs every time")
        self.assertEqual(self.scans, [])
        self.assertIsNone(self.store.row("k:notes-direct:readrequest-01"))
        # A small status row (no note text) lets notes_status answer for a read this Mac ran.
        self.assertEqual(self.store.row("k:notes-read:readrequest-01")["result"], {"request_id": "readrequest-01", "action": "read"})
        self.assertNotIn("<div>x</div>", json.dumps(self.store.row("k:notes-read:readrequest-01")))
        status = NC.status(host, "CLAUDE", "listrequest-01")
        self.assertEqual((status["status"], status["transport"], status["action"]), ("done", "direct", "list"))

    def test_status_on_a_mac_reads_the_idempotency_rows(self):
        host = self.mac()
        with self.assertRaises(N.NotesError) as caught:
            NC.status(host, "CLAUDE", "nobody-asked-01")
        self.assertEqual(caught.exception.code, "not_found")
        self.store.begin("k:notes-direct:unknown-req-01", 60, {"tool": "notes_direct"})
        self.store.set("k:notes-direct:unknown-req-01", state="unknown")
        self.assertEqual(NC.status(host, "CLAUDE", "unknown-req-01"), {"request_id": "unknown-req-01", "status": "unknown", "transport": "direct"})
        self.store.begin("k:notes-direct:pending-req-01", 60, {"tool": "notes_direct"})
        self.assertEqual(NC.status(host, "CLAUDE", "pending-req-01")["status"], "running")
        with self.assertRaises(N.NotesError):
            NC.status(host, "CLAUDE", "bad id")

    def test_a_mac_write_that_timed_out_is_refused_on_retry(self):
        host = self.mac()
        self.answer = N.NotesError("outcome_unknown", "the notes script did not finish")
        with self.assertRaises(N.NotesError) as caught:
            NC.call(host, "CLAUDE", self.req(), wait=90)
        self.assertEqual(caught.exception.code, "outcome_unknown")
        self.answer = {"status": "done"}
        with self.assertRaises(N.NotesError) as caught:
            NC.call(host, "CLAUDE", self.req(), wait=90)
        self.assertIn("may have been interrupted", caught.exception.message)
        self.assertEqual(len(self.runs), 1)

    def test_who_may_send_is_checked_before_anything_runs_or_is_sent(self):
        host = self.mac()
        client = FakeClient(record("queued"))
        off, made = self.elsewhere(client)
        for target in (host, off):
            with self.subTest(mac=target is host):
                with self.assertRaises(N.NotesError) as caught:
                    NC.call(target, "CLAUDE", self.req(title="[AFC, Codex] Not Mine"), wait=90, scan=self.scan)
                self.assertEqual(caught.exception.code, "invalid_argument")
                with self.assertRaises(N.NotesError) as caught:
                    NC.call(target, "CLAUDE", self.req(session=None), wait=90, scan=self.scan)
                self.assertEqual(caught.exception.code, "session_required")
                with self.assertRaises(N.NotesError):
                    NC.call(target, "CODEX", self.req(), wait=90, scan=self.scan)
        # The scan runs first (it sends nothing, and it keeps a refusal below from naming a secret);  nothing ran.
        self.assertEqual((self.runs, client.calls, made), ([], [], []))

    def test_a_write_is_scanned_even_when_the_caller_brings_no_scan(self):
        host = self.mac()
        leak = "token " + "ghp_" + "EXAMPLE0" * 5
        with self.assertRaises(N.SecretRefused):
            NC.call(host, "CLAUDE", self.req(markdown=leak), wait=0)
        self.assertEqual(self.runs, [])
        # The scan comes before the seat checks, whose refusals name part of the request (another seat's agent here).
        with self.assertRaises(N.SecretRefused):
            NC.call(host, "CLAUDE", self.req(title="[AFC, Codex] Not Mine", markdown=leak), wait=0)
        self.assertEqual(self.runs, [])

    def test_a_scan_refusal_stops_the_write(self):
        host = self.mac()

        def refuse(request):
            raise N.NotesError("invalid_argument", "refusing to write:  the markdown looks like it contains a token")

        with self.assertRaises(N.NotesError):
            NC.call(host, "CLAUDE", self.req(), wait=90, scan=refuse)
        self.assertEqual(self.runs, [])
        self.assertIsNone(self.store.row("k:notes-direct:requestid-0001"))

    def test_off_a_mac_the_operator_answers_in_the_same_shape(self):
        done = record("done", result={"note_id": "x-coredata://n2", "pin_skipped": "the Shortcut failed"})
        client = FakeClient(record("queued"), waited=done)
        host, made = self.elsewhere(client)
        out = NC.call(host, "CLAUDE", self.req(), wait=300, scan=self.scan)
        self.assertEqual(
            out,
            {
                "request_id": "requestid-0001",
                "action": "create",
                "status": "done",
                "transport": "operator",
                "title": TITLE,
                "note_id": "x-coredata://n2",
                "pin_skipped": "the Shortcut failed",
                "duplicate": False,
            },
        )
        self.assertEqual([c[0] for c in client.calls], ["enqueue", "wait"])
        self.assertEqual(client.calls[1][3], 300)
        self.assertEqual(len(made), 1)
        self.assertEqual(len(self.scans), 1)
        self.assertIsNone(self.store.row("k:notes-direct:requestid-0001"), "the Operator keeps its own rows")

    def test_the_operator_states_map_to_answers_and_errors(self):
        for state, extra, expected in (
            ("queued", {}, ("queued", None)),
            ("running", {}, ("running", None)),
            ("refused", {"error": {"code": "not_found", "message": "no note with that title"}}, (None, "not_found")),
            ("refused", {}, (None, "internal")),
            ("unknown", {"error": {"code": "outcome_unknown", "message": "the lease ran out"}}, (None, "outcome_unknown")),
            ("expired", {}, (None, "unavailable")),
            ("lost", {}, (None, "outcome_unknown")),  # a state this client does not know is never read as done
        ):
            with self.subTest(state=state, extra=bool(extra)):
                host, _ = self.elsewhere(FakeClient(record(state, **extra)))
                if expected[1] is None:
                    out = NC.call(host, "CLAUDE", self.req(), wait=0)
                    self.assertEqual((out["status"], out["transport"]), (expected[0], "operator"))
                    self.assertNotIn("duplicate", out)
                else:
                    with self.assertRaises(N.NotesError) as caught:
                        NC.call(host, "CLAUDE", self.req(), wait=0)
                    self.assertEqual(caught.exception.code, expected[1])
                    if extra:
                        self.assertEqual(caught.exception.message, extra["error"]["message"])

    def test_the_duplicate_flag_and_a_wait_of_zero(self):
        client = FakeClient(record("done", result={"note_id": "x-coredata://n3"}), duplicate=True)
        host, _ = self.elsewhere(client)
        out = NC.call(host, "CLAUDE", self.req(), wait=0)
        self.assertEqual((out["status"], out["duplicate"], out["note_id"]), ("done", True, "x-coredata://n3"))
        self.assertEqual([c[0] for c in client.calls], ["enqueue"], "wait 0 polls nothing")

    def test_off_a_mac_a_read_and_a_list_answer_their_results(self):
        note = {"title": MASTER, "body": "<div>x</div>", "format": "html"}
        host, _ = self.elsewhere(FakeClient(record("done", action="read", title=MASTER, result={"note": note}, result_expired=True)))
        out = NC.call(host, "CLAUDE", self.req("read"), wait=0, scan=self.scan)
        self.assertEqual((out["note"], out["result_expired"], out["action"]), (note, True, "read"))
        rows = [{"title": TITLE, "id": "n1", "created": "", "modified": ""}]
        host, _ = self.elsewhere(FakeClient(record("done", action="list", title=None, result={"notes": rows, "total": 9, "truncated": True})))
        out = NC.call(host, "CLAUDE", self.req("list"), wait=0, scan=self.scan)
        self.assertEqual((out["notes"], out["total"], out["truncated"]), (rows, 9, True))
        self.assertNotIn("title", out)
        self.assertEqual(self.scans, [], "a read is never scanned")

    def test_status_and_log_off_a_mac_read_the_operator(self):
        finished = record(
            "refused",
            finished_at="2026-10-22T22:05:00Z",
            error={"code": "not_found", "message": "no note"},
        )
        client = FakeClient(finished)
        host, _ = self.elsewhere(client)
        self.assertEqual(
            NC.status(host, "CLAUDE", "requestid-0001"),
            {
                "request_id": "requestid-0001",
                "status": "refused",
                "action": "create",
                "transport": "operator",
                "title": TITLE,
                "queued_at": "2026-10-22T22:00:00Z",
                "finished_at": "2026-10-22T22:05:00Z",
                "error": {"code": "not_found", "message": "no note"},
            },
        )
        done = FakeClient(record("done", result={"note_id": "x-coredata://n4", "pin_skipped": "busy"}))
        host, _ = self.elsewhere(done)
        info = NC.status(host, "CLAUDE", "requestid-0001")
        self.assertEqual((info["note_id"], info["pin_skipped"]), ("x-coredata://n4", "busy"))
        # The CLI prints a record's error to a terminal:  one line, no control character, a code of the contract's shape.
        odd = record("refused", error={"code": "Not A Code", "message": "line one\n\x1b[31mred\x1b[0m\x00 " + "x" * 500})
        host, _ = self.elsewhere(FakeClient(odd))
        error = NC.status(host, "CLAUDE", "requestid-0001")["error"]
        self.assertEqual(error["code"], "")
        self.assertTrue(error["message"].startswith("line one [31mred [0m x"), error["message"][:40])
        self.assertEqual(len(error["message"]), 400)
        with self.assertRaises(N.NotesError) as caught:
            NC.call(host, "CLAUDE", self.req(rid="requestid-0001"), wait=0)
        self.assertEqual(caught.exception.code, "internal")
        self.assertNotIn("\x1b", caught.exception.message)
        client.entries = [{"id": "e1", "ts": "2026-10-22T22:00:00Z"}]
        host, _ = self.elsewhere(client)
        self.assertEqual(
            NC.log(host, {"by_seat": "codex"}),
            {"entries": [{"id": "e1", "ts": "2026-10-22T22:00:00Z"}], "source": "operator", "count": 1},
        )
        self.assertEqual(client.calls[-1], ("log", {"since_hours": 24, "limit": 50, "seat": "CODEX", "action": None, "title": None}))
        with self.assertRaises(N.NotesError):
            NC.log(host, {"since_hours": 0})

    def test_log_on_a_mac_reads_the_file(self):
        log = self.home / "actions.jsonl"
        from datetime import datetime, timezone

        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        log.write_text(json.dumps({"id": "e1", "ts": ts, "mode": "update", "seat": "CLAUDE"}) + "\n")
        self.env["APPLE_NOTES_ACTION_LOG"] = str(log)
        host = self.mac()
        self.assertEqual(NC.log(host, {}), {"entries": [{"id": "e1", "ts": ts, "mode": "update", "seat": "CLAUDE"}], "source": "mac", "count": 1})
        self.assertEqual(NC.log(host, {"action": "create"})["count"], 0)

    def test_a_mac_never_makes_an_operator_client(self):
        made = []
        host = NC.Host(self.env, self.home, self.store, caller="cli", client_factory=lambda: made.append(1), script_finder=lambda env, home: self.SCRIPT)
        with mock.patch.object(N, "run_direct", self.fake_run):
            NC.call(host, "CLAUDE", self.req(), wait=90)
            NC.status(host, "CLAUDE", "requestid-0001")
        self.assertEqual(made, [])

    def test_the_real_client_end_to_end_off_a_mac(self):
        op = FakeOperator()
        sleeps: list[float] = []
        op.records[("CLAUDE", "requestid-0001")] = [record("running"), record("done", result={"note_id": "x-coredata://n5"})]
        host, _ = self.elsewhere(op.client(sleeps))
        out = NC.call(host, "CLAUDE", self.req(), wait=300)
        self.assertEqual((out["status"], out["note_id"], out["duplicate"]), ("done", "x-coredata://n5", False))
        self.assertEqual(sleeps, [1.0, 1.0])
        self.assertEqual([c["method"] for c in op.calls], ["POST", "GET", "GET"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
