"""AFC-486:  the Operator's Apple Notes queue (notes_queue.py), its routes on the listener API (listener_api.py) and
the daemon's wiring of both (daemon._configure_listener_api).

NotesQueue.handle is driven directly with a temporary state root and a fake clock, so no socket is involved;  one test
runs the queue behind a real ListenerReadApi on an ephemeral port.  Seats are checked against the repo's own
fleet-apps.json (identity.seat_registry_path with no FLEET_APPS_JSON override).  Every key is generated per run, so the
source holds no key-looking literal.

Run:  cd scripts && python3 -m unittest agent_sync.tests.test_notes_queue
"""

from __future__ import annotations

import json
import secrets
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from agent_sync import daemon as D
from agent_sync import listener_api as API
from agent_sync import notes as N
from agent_sync import notes_queue as NQ
from agent_sync import notes_wire as W

SID = "thread-id-0001"
WORKER_SEATS = ("CLAUDE", "JET", "CURSOR")
SERVER_SEATS = ("CODEX",)


class FakeClock:
    def __init__(self) -> None:
        self.now = float(int(time.time()))

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def request(action: str = "create", identity: str = "CLAUDE", rid: str = "request-0001", **fields: Any) -> dict:
    """A request object built by the shared builder, so the test sends exactly what a caller would."""
    if action in N.BODY_ACTIONS:
        fields.setdefault("markdown", "the body text")
        if identity == "CLAUDE":
            fields.setdefault("session", SID)
    if action != "list":
        fields.setdefault("title", "[AFC, %s] Queue Test" % ("Claude" if identity == "CLAUDE" else identity))
    if "format" in fields:
        fields["fmt"] = fields.pop("format")
    return N.make_request(action, identity, request_id=rid, **fields)


class QueueHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="notes-queue-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = str(self.tmp / "state")
        self.clock = FakeClock()
        self.keys = {name: secrets.token_hex(24) for name in W.PRINCIPALS}
        self.events: list[tuple[str, dict]] = []
        self.queue = self.make_queue()

    def make_queue(self, **overrides: Any) -> NQ.NotesQueue:
        principals = {
            W.WORKER: (self.keys[W.WORKER], WORKER_SEATS),
            W.SERVER: (self.keys[W.SERVER], SERVER_SEATS),
            W.RUNNER: (self.keys[W.RUNNER], ()),
        }
        principals.update(overrides)
        return NQ.NotesQueue(
            self.root, principals, env={}, clock=self.clock, log=lambda event, **f: self.events.append((event, f))
        )

    def call(
        self,
        method: str,
        path: str,
        payload: Any = None,
        *,
        principal: str = W.WORKER,
        key: str | None = None,
        stamp: float | None = None,
        queue: NQ.NotesQueue | None = None,
    ) -> NQ.Answer:
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        bare, _, query = path.partition("?")
        headers = W.headers(
            key if key is not None else self.keys.get(principal, "x" * 40),
            method,
            bare,
            query,
            principal,
            body,
            self.clock() if stamp is None else stamp,
        )
        return (queue or self.queue).handle(method, path, headers, body)

    def enqueue(self, req: dict, seat: str | None = None, **kwargs: Any) -> NQ.Answer:
        return self.call("POST", "/notes/v1/requests", {"seat": seat or req["identity"], "request": req}, **kwargs)

    def status(self, seat: str, rid: str, **kwargs: Any) -> NQ.Answer:
        return self.call("GET", "/notes/v1/requests/%s/%s" % (seat, rid), **kwargs)

    def claim(self, wait: float = 0, **kwargs: Any) -> NQ.Answer:
        return self.call("POST", "/notes/v1/runner/claim", {"wait": wait}, principal=W.RUNNER, **kwargs)

    def report(self, claimed: dict, **outcome: Any) -> NQ.Answer:
        payload = {"seat": claimed["seat"], "request_id": claimed["request_id"], "claim": claimed["claim"]}
        payload.update(outcome)
        return self.call("POST", "/notes/v1/runner/result", payload, principal=W.RUNNER)

    def assert_answer(self, answer: NQ.Answer, status: int, code: str | None = None) -> dict:
        self.assertEqual(answer.status, status, answer.body)
        if code is not None:
            self.assertEqual((answer.body or {}).get("error"), code, answer.body)
        return answer.body or {}


class SignatureTests(QueueHarness):
    def test_a_good_signature_is_accepted(self) -> None:
        body = self.assert_answer(self.enqueue(request()), 202)
        self.assertEqual((body["contract"], body["state"], body["seat"]), (W.CONTRACT, "queued", "CLAUDE"))

    def test_a_bad_signature_is_401(self) -> None:
        self.assert_answer(self.enqueue(request(), key=secrets.token_hex(24)), 401, "unauthorized")
        headers = W.headers(self.keys[W.WORKER], "GET", "/notes/v1/log", "", W.WORKER, b"", self.clock())
        headers[W.SIGNATURE_HEADER] = "00" * 32
        self.assert_answer(self.queue.handle("GET", "/notes/v1/log", headers, b""), 401, "unauthorized")
        # The signature covers the exact query:  one signed for limit=1 does not authorize limit=50.
        signed = W.headers(self.keys[W.WORKER], "GET", "/notes/v1/log", "limit=1", W.WORKER, b"", self.clock())
        self.assert_answer(self.queue.handle("GET", "/notes/v1/log?limit=50", signed, b""), 401)
        # and the body:  a signature over one body does not carry another.
        good = W.headers(self.keys[W.WORKER], "POST", "/notes/v1/requests", "", W.WORKER, b"{}", self.clock())
        other = json.dumps({"seat": "CLAUDE", "request": request()}).encode()
        self.assert_answer(self.queue.handle("POST", "/notes/v1/requests", good, other), 401)
        self.assertIn(("notes-api-refused", {"principal": "WORKER", "reason": "signature"}), self.events)

    def test_a_stale_or_future_timestamp_is_401(self) -> None:
        for skew in (-(W.MAX_SKEW_S + 1), W.MAX_SKEW_S + 1):
            self.assert_answer(self.enqueue(request(), stamp=self.clock() + skew), 401, "unauthorized")
        self.assert_answer(self.enqueue(request(), stamp=self.clock() - W.MAX_SKEW_S), 202)

    def test_an_unknown_principal_or_one_without_a_usable_key_is_404(self) -> None:
        self.assert_answer(self.enqueue(request(), principal="ADMIN", key=self.keys[W.WORKER]), 404, "not_found")
        short = self.make_queue(**{W.WORKER: ("k" * (W.MIN_KEY_LEN - 1), WORKER_SEATS)})
        self.assertNotIn(W.WORKER, short.principals)
        self.assert_answer(
            self.enqueue(request(), key="k" * (W.MIN_KEY_LEN - 1), queue=short), 404, "not_found"
        )
        self.assert_answer(self.call("GET", "/notes/v1/nothing-here"), 404, "not_found")

    def test_a_principal_on_a_path_it_may_not_use_is_403(self) -> None:
        self.assert_answer(self.enqueue(request(), principal=W.RUNNER), 403, "forbidden")
        self.assert_answer(self.call("POST", "/notes/v1/runner/claim", {"wait": 0}), 403, "forbidden")
        self.assert_answer(self.call("GET", "/notes/v1/log", principal=W.RUNNER), 403, "forbidden")


class EnqueueTests(QueueHarness):
    def test_enqueue_is_202_the_same_request_again_is_200_and_a_different_one_is_409(self) -> None:
        first = self.assert_answer(self.enqueue(request()), 202)
        again = self.assert_answer(self.enqueue(request()), 200)
        self.assertEqual(again, first)
        self.assert_answer(self.enqueue(request(markdown="a different body")), 409, "conflict")
        # The record holds the request's shape, never its body.
        self.assertNotIn("markdown", json.dumps(first))
        self.assertEqual(self.assert_answer(self.status("CLAUDE", "request-0001"), 200)["state"], "queued")
        self.assertIn(("notes-queue-queued", {"seat": "CLAUDE", "action": "create", "principal": "WORKER"}), self.events)

    def test_a_principal_may_act_only_for_its_own_seats(self) -> None:
        self.assert_answer(self.enqueue(request(identity="CODEX", title="[AFC, Codex] t", session=SID)), 403, "forbidden")
        self.assert_answer(
            self.enqueue(request(identity="CODEX", title="[AFC, Codex] t", session=SID), principal=W.SERVER), 202
        )
        self.assert_answer(self.enqueue(request(), principal=W.SERVER), 403, "forbidden")
        self.assert_answer(self.status("CLAUDE", "request-0001", principal=W.SERVER), 403, "forbidden")
        # A seat the principal lists but that is not a live agent identity is still refused.
        odd = self.make_queue(**{W.WORKER: (self.keys[W.WORKER], ("NOPE",))})
        self.assert_answer(
            self.enqueue(request(identity="NOPE", title="[AFC, NOPE] t", session=SID), queue=odd), 403, "forbidden"
        )

    def test_managed_config_allows_hosted_codex_without_other_seat_widening(self) -> None:
        import tomllib
        config = tomllib.loads((Path(__file__).resolve().parents[1] / "server" / "listener.toml").read_text())
        seats = config["notes_queue"]["worker_seats"]
        self.assertEqual(seats, ["JET", "GROK-WEB", "ECHO", "WISE", "CURSOR", "CLAUDE", "CODEX"])
        self.assertEqual(config["notes_queue"]["server_seats"], ["CLAUDE", "CODEX"])
        queue = self.make_queue(**{W.WORKER: (self.keys[W.WORKER], seats)})
        own = request(identity="CODEX", title="[AFC, Codex] t", session=SID)
        self.assert_answer(self.enqueue(own, queue=queue), 202)
        self.assert_answer(self.status("CODEX", own["request_id"], queue=queue), 200)
        self.assert_answer(self.enqueue(own, seat="CLAUDE", queue=queue), 400)
        self.assert_answer(self.call("POST", "/notes/v1/runner/claim", {"wait": 0}, queue=queue), 403, "forbidden")

    def test_the_seat_must_be_the_requests_own_identity(self) -> None:
        self.assert_answer(self.enqueue(request(identity="JET", title="[AFC, JET] t"), seat="CLAUDE"), 400)

    def test_a_create_whose_title_names_another_agent_is_400(self) -> None:
        body = self.assert_answer(self.enqueue(request(title="[AFC, Codex] Not Mine")), 400, "invalid_argument")
        self.assertIn("CLAUDE", body["message"])
        # An update of another agent's note is allowed (it runs --existing-only on the Mac).
        self.assert_answer(self.enqueue(request("update", title="[AFC, Codex] Not Mine", rid="request-0002")), 202)

    def test_a_create_without_a_session_needs_an_exempt_seat(self) -> None:
        bare = N.make_request("create", "CLAUDE", request_id="request-0003", title="[AFC, Claude] t", markdown="x")
        body = self.assert_answer(self.enqueue(bare), 400, "session_required")
        self.assertIn("session", body["message"])
        update = N.make_request("update", "CURSOR", request_id="request-0004", title="[AFC, Cursor] t", markdown="x")
        self.assert_answer(self.enqueue(update), 400, "session_required")
        exempt = N.make_request("create", "JET", request_id="request-0005", title="[AFC, JET] t", markdown="x")
        self.assert_answer(self.enqueue(exempt), 202)
        pin = N.make_request("pin", "CLAUDE", request_id="request-0006", title="[AFC, Claude] t")
        self.assert_answer(self.enqueue(pin), 202)

    def test_write_text_gets_the_secret_scan(self) -> None:
        token = "ghp_" + "a" * 36  # built at run time from one repeated letter:  plainly synthetic
        body = self.assert_answer(self.enqueue(request(markdown="see %s here" % token)), 400, "invalid_argument")
        self.assertIn("GitHub token", body["message"])
        self.assertNotIn(token, json.dumps(body))
        flagged = request(rid="request-0007", flags={"summary": "key %s" % token})
        self.assert_answer(self.enqueue(flagged), 400, "invalid_argument")
        self.assertEqual(self.queue.active, {})

    def test_a_malformed_body_or_request_is_400(self) -> None:
        self.assert_answer(self.call("POST", "/notes/v1/requests", ["not", "an", "object"]), 400)
        self.assert_answer(self.call("POST", "/notes/v1/requests", {"seat": "CLAUDE"}), 400)
        self.assert_answer(self.call("POST", "/notes/v1/requests", {"seat": "CLAUDE", "request": request(), "x": 1}), 400)
        self.assert_answer(self.call("POST", "/notes/v1/requests", {"seat": "CLAUDE", "request": dict(request(), v=1)}), 400)

    def test_the_queue_cap_answers_503(self) -> None:
        with mock.patch.object(NQ, "QUEUE_MAX", 2):
            self.assert_answer(self.enqueue(request(rid="cap-request-1")), 202)
            self.assert_answer(self.enqueue(request(rid="cap-request-2")), 202)
            self.assert_answer(self.enqueue(request(rid="cap-request-3")), 503, "unavailable")
            # The same request again still answers its record:  the cap stops new work only.
            self.assert_answer(self.enqueue(request(rid="cap-request-1")), 200)
        with mock.patch.object(NQ, "SEAT_QUEUE_MAX", 2):
            self.assert_answer(self.enqueue(request(rid="cap-request-3")), 503, "unavailable")
            self.assert_answer(self.enqueue(request("create", "JET", "cap-request-4", title="[AFC, JET] t")), 202)


class RunnerTests(QueueHarness):
    def test_claim_is_fifo_and_an_empty_queue_answers_204_at_once(self) -> None:
        self.assert_answer(self.enqueue(request(rid="fifo-request-1")), 202)
        self.clock.advance(1)
        self.assert_answer(self.enqueue(request("create", "JET", "fifo-request-2", title="[AFC, JET] t")), 202)
        first = self.assert_answer(self.claim(), 200)
        second = self.assert_answer(self.claim(), 200)
        self.assertEqual((first["request_id"], second["request_id"]), ("fifo-request-1", "fifo-request-2"))
        self.assertEqual(first["request"], request(rid="fifo-request-1"))
        self.assertEqual((first["seat"], first["principal"], first["lease_seconds"]), ("CLAUDE", "WORKER", int(NQ.LEASE_S)))
        started = time.monotonic()
        self.assertEqual(self.claim(), NQ.Answer(204, None))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(self.assert_answer(self.status("CLAUDE", "fifo-request-1"), 200)["state"], "running")
        self.assert_answer(self.claim(wait=W.CLAIM_WAIT_MAX + 1), 400, "invalid_argument")

    def test_a_waiting_claim_wakes_when_a_request_arrives(self) -> None:
        got: list[NQ.Answer] = []
        waiter = threading.Thread(target=lambda: got.append(self.claim(wait=5)))
        waiter.start()
        time.sleep(0.2)
        self.assert_answer(self.enqueue(request()), 202)
        waiter.join(timeout=5)
        self.assertEqual(got[0].status, 200)
        self.assertEqual(got[0].body["request_id"], "request-0001")

    def test_results_done_refused_and_outcome_unknown(self) -> None:
        for rid in ("result-request-1", "result-request-2", "result-request-3"):
            self.assert_answer(self.enqueue(request(rid=rid)), 202)
            self.clock.advance(1)
        done, refused, unknown = (self.assert_answer(self.claim(), 200) for _ in range(3))
        body = self.assert_answer(self.report(done, result={"status": "done", "note_id": "x-coredata://n1"}), 200)
        self.assertEqual(body["state"], "done")
        stored = self.assert_answer(self.status("CLAUDE", "result-request-1"), 200)
        self.assertEqual(stored["result"], {"status": "done", "note_id": "x-coredata://n1"})
        self.assertIsNotNone(body["finished_at"])
        body = self.assert_answer(self.report(refused, error={"code": "not_found", "message": "no note"}), 200)
        self.assertEqual((body["state"], body["error"]["code"]), ("refused", "not_found"))
        body = self.assert_answer(self.report(unknown, error={"code": "outcome_unknown", "message": "timed out"}), 200)
        self.assertEqual((body["state"], body["error"]["code"]), ("unknown", "outcome_unknown"))
        # A finished write keeps no body on disk.
        for path in (Path(self.root) / NQ.QUEUE_DIR / NQ.REQUESTS_DIR / "CLAUDE").glob("*.json"):
            self.assertNotIn("the body text", path.read_text())
        # An error code the contract does not know is internal, which proves nothing:  for a create it is unknown (the
        # note may have been written), and for a pin a refusal.
        self.assert_answer(self.enqueue(request(rid="result-request-4")), 202)
        odd = self.assert_answer(self.claim(), 200)
        body = self.assert_answer(self.report(odd, error={"code": "made_up", "message": "x"}), 200)
        self.assertEqual((body["state"], body["error"]["code"]), ("unknown", "outcome_unknown"))
        self.assert_answer(self.enqueue(request("pin", rid="result-request-5")), 202)
        pin = self.assert_answer(self.claim(), 200)
        body = self.assert_answer(self.report(pin, error={"code": "internal", "message": "x"}), 200)
        self.assertEqual((body["state"], body["error"]["code"]), ("refused", "internal"))
        # An error whose code or message is not text is refused whole, before anything is stored.
        self.assert_answer(self.enqueue(request("pin", rid="result-request-6")), 202)
        bad = self.assert_answer(self.claim(), 200)
        self.assert_answer(self.report(bad, error={"code": "internal", "message": {"deep": [1]}}), 400, "invalid_argument")
        self.assertEqual(self.assert_answer(self.status("CLAUDE", "result-request-6"), 200)["state"], "running")

    def test_a_read_and_a_list_return_their_result(self) -> None:
        self.assert_answer(self.enqueue(request("read", rid="read-request-1", format="text")), 202)
        self.assert_answer(self.enqueue(request("list", rid="list-request-1", query="Queue")), 202)
        read, listed = self.assert_answer(self.claim(), 200), self.assert_answer(self.claim(), 200)
        note = {"title": "[AFC, Claude] Queue Test", "id": "n1", "format": "text", "body": "hello", "bytes": 5}
        body = self.assert_answer(self.report(read, result={"note": note}), 200)
        # The answer to a report never echoes the result (the runner already holds it);  the record does.
        self.assertNotIn("result", body)
        self.assertEqual((body["seat"], body["request_id"], body["state"]), ("CLAUDE", "read-request-1", "done"))
        stored = self.assert_answer(self.status("CLAUDE", "read-request-1"), 200)
        self.assertEqual(stored["result"]["note"]["body"], "hello")
        self.assert_answer(self.report(listed, result={"status": "done"}), 400, "invalid_argument")  # no notes list
        body = self.assert_answer(self.report(listed, result={"notes": [], "total": 0}), 200)
        self.assertEqual(body["state"], "done")

    def test_a_stored_read_withholds_secret_shaped_lines_even_from_an_older_runner(self) -> None:
        self.assert_answer(self.enqueue(request("read", rid="read-secret-01")), 202)
        claimed = self.assert_answer(self.claim(), 200)
        token = "ghp_" + "a" * 36  # built at run time from one repeated letter
        body = "<div>Before.</div>\n<div>key %s</div>" % token
        result = {"note": {"title": "k %s" % token, "body": body, "withheld": 0}}  # a title an older runner sent as is
        self.assert_answer(self.report(claimed, result=result), 200)
        note = self.assert_answer(self.status("CLAUDE", "read-secret-01"), 200)["result"]["note"]
        self.assertNotIn(token, json.dumps(note))
        self.assertEqual((note["body"], note["withheld"]), ("<div>Before.</div>\n<div>%s</div>" % N.WITHHELD, 1))
        self.assertEqual(note["title"], N.WITHHELD)
        self.assertNotIn(token, "".join(p.read_text() for p in (Path(self.root) / NQ.QUEUE_DIR).rglob("*.json")))
        # Each known field is kept only in its own type, and nothing else is kept.
        self.assert_answer(self.enqueue(request("read", rid="read-shape-001")), 202)
        claimed = self.assert_answer(self.claim(), 200)
        odd = {"title": ["x"], "id": 5, "format": "raw", "body": "b", "bytes": "10", "truncated": "yes", "withheld": -1, "extra": "x"}
        self.assert_answer(self.report(claimed, result={"note": odd}), 200)
        note = self.assert_answer(self.status("CLAUDE", "read-shape-001"), 200)["result"]["note"]
        # The size and the marker count are the stored body's own, never the runner's word.
        self.assertEqual(note, {"body": "b", "bytes": 1, "withheld": 0, "format": "raw"})
        # A lone surrogate is not text:  the whole report is refused before anything is stored.
        self.assert_answer(self.enqueue(request("read", rid="read-surrogate-1")), 202)
        claimed = self.assert_answer(self.claim(), 200)
        self.assert_answer(self.report(claimed, result={"note": {"title": "t", "body": "a\ud800b"}}), 400, "invalid_argument")
        self.assertEqual(self.assert_answer(self.status("CLAUDE", "read-surrogate-1"), 200)["state"], "running")

    def test_a_repeated_result_is_200_and_a_wrong_claim_is_409(self) -> None:
        self.assert_answer(self.enqueue(request()), 202)
        claimed = self.assert_answer(self.claim(), 200)
        wrong = dict(claimed, claim="w" * 24)
        self.assert_answer(self.report(wrong, result={"status": "done"}), 409, "conflict")
        first = self.assert_answer(self.report(claimed, result={"status": "done", "note_id": "n1"}), 200)
        again = self.assert_answer(self.report(claimed, result={"status": "done", "note_id": "n1"}), 200)
        self.assertEqual(again, first)
        self.assert_answer(self.report(wrong, result={"status": "done"}), 409, "conflict")
        self.assert_answer(self.report(dict(claimed, claim="short"), result={}), 400, "invalid_argument")
        self.assert_answer(self.report(claimed, result={"status": "done"}, error={"code": "internal"}), 400)
        self.assert_answer(self.report(dict(claimed, request_id="no-such-request"), result={}), 404, "not_found")
        # seat and request_id name a file under requests/:  a malformed one is refused before any lookup.
        for bad in (dict(claimed, seat="../etc"), dict(claimed, request_id="../../escape"), dict(claimed, seat="a/b")):
            with self.subTest(bad=(bad["seat"], bad["request_id"])):
                self.assert_answer(self.report(bad, result={"status": "done"}), 400, "invalid_argument")

    def test_an_expired_lease_makes_a_write_unknown_and_puts_a_read_back(self) -> None:
        self.assert_answer(self.enqueue(request(rid="lease-write-1")), 202)
        self.clock.advance(1)
        self.assert_answer(self.enqueue(request("read", rid="lease-read-1")), 202)
        write, read = self.assert_answer(self.claim(), 200), self.assert_answer(self.claim(), 200)
        self.assertEqual((write["request_id"], read["request_id"]), ("lease-write-1", "lease-read-1"))
        self.clock.advance(NQ.LEASE_S + 1)
        body = self.assert_answer(self.status("CLAUDE", "lease-write-1"), 200)
        self.assertEqual((body["state"], body["error"]["code"]), ("unknown", "outcome_unknown"))
        self.assertEqual(self.assert_answer(self.status("CLAUDE", "lease-read-1"), 200)["state"], "queued")
        # The old claim no longer holds the read, and the read is handed out again ...
        self.assert_answer(self.report(read, result={"note": {"body": "x"}}), 409, "conflict")
        for attempt in (2, 3):
            again = self.assert_answer(self.claim(), 200)
            self.assertEqual(again["request_id"], "lease-read-1")
            self.assertNotEqual(again["claim"], read["claim"])
            self.clock.advance(NQ.LEASE_S + 1)
        # ... at most READ_ATTEMPTS times, then it is refused as unavailable.
        body = self.assert_answer(self.status("CLAUDE", "lease-read-1"), 200)
        self.assertEqual((body["state"], body["error"]["code"]), ("refused", "unavailable"))
        self.assertEqual(self.claim(), NQ.Answer(204, None))

    def test_a_read_the_runner_reports_unavailable_is_tried_again(self) -> None:
        # Its script timed out, say:  a read changes nothing, so it goes back to the queue, and the third try's answer
        # is final.  A write reported unavailable is final at once (nothing ran).
        self.assert_answer(self.enqueue(request("read", rid="slow-read-01")), 202)
        timed_out = {"code": "unavailable", "message": "the notes script did not answer within 180 seconds"}
        for attempt in (1, 2):
            claimed = self.assert_answer(self.claim(), 200)
            body = self.assert_answer(self.report(claimed, error=timed_out), 200)
            self.assertEqual((body["state"], body["request_id"]), ("queued", "slow-read-01"), attempt)
        claimed = self.assert_answer(self.claim(), 200)
        self.assert_answer(self.report(claimed, error=timed_out), 200)
        body = self.assert_answer(self.status("CLAUDE", "slow-read-01"), 200)
        self.assertEqual((body["state"], body["error"]["code"]), ("refused", "unavailable"))
        self.assert_answer(self.enqueue(request("pin", rid="busy-pin-01")), 202)
        claimed = self.assert_answer(self.claim(), 200)
        self.assert_answer(self.report(claimed, error={"code": "unavailable", "message": "busy"}), 200)
        self.assertEqual(self.assert_answer(self.status("CLAUDE", "busy-pin-01"), 200)["state"], "refused")

    def test_a_late_definite_report_settles_a_write_the_lease_called_unknown(self) -> None:
        # The Mac slept past the lease:  the write reads unknown, then the runner reports what really happened.
        self.assert_answer(self.enqueue(request(rid="late-write-1")), 202)
        write = self.assert_answer(self.claim(), 200)
        self.clock.advance(NQ.LEASE_S + 1)
        self.assertEqual(self.assert_answer(self.status("CLAUDE", "late-write-1"), 200)["state"], "unknown")
        self.assert_answer(self.report(write, result={"status": "done", "note_id": "x-coredata://late"}), 200)
        body = self.assert_answer(self.status("CLAUDE", "late-write-1"), 200)
        self.assertEqual((body["state"], body["result"]["note_id"]), ("done", "x-coredata://late"))
        self.assertNotIn("error", body)
        # Once settled it stays settled:  the same report again is a no-op, and another claim is still refused.
        self.assert_answer(self.report(write, result={"status": "done"}), 200)
        self.assert_answer(self.report(dict(write, claim="c" * 24), result={"status": "done"}), 409, "conflict")
        # A late report that is itself unknown changes nothing.
        self.assert_answer(self.enqueue(request(rid="late-write-2")), 202)
        second = self.assert_answer(self.claim(), 200)
        self.clock.advance(NQ.LEASE_S + 1)
        self.assertEqual(self.assert_answer(self.status("CLAUDE", "late-write-2"), 200)["state"], "unknown")
        self.assert_answer(self.report(second, error={"code": "outcome_unknown", "message": "m"}), 200)
        self.assertEqual(self.assert_answer(self.status("CLAUDE", "late-write-2"), 200)["state"], "unknown")
        # Nor does a late internal, or an error with no code:  they prove nothing, so the write stays unknown.
        for rid, error in (("late-write-3", {"code": "internal", "message": "exit 9"}), ("late-write-4", {})):
            with self.subTest(rid):
                self.assert_answer(self.enqueue(request(rid=rid)), 202)
                late = self.assert_answer(self.claim(), 200)
                self.clock.advance(NQ.LEASE_S + 1)
                self.assert_answer(self.report(late, error=error), 200)
                body = self.assert_answer(self.status("CLAUDE", rid), 200)
                self.assertEqual((body["state"], body["error"]["code"]), ("unknown", "outcome_unknown"))

    def test_a_request_nobody_claims_expires_after_24_hours(self) -> None:
        self.assert_answer(self.enqueue(request()), 202)
        self.clock.advance(NQ.QUEUED_TTL_S - 1)
        self.assertEqual(self.assert_answer(self.status("CLAUDE", "request-0001"), 200)["state"], "queued")
        self.clock.advance(2)
        body = self.assert_answer(self.status("CLAUDE", "request-0001"), 200)
        self.assertEqual((body["state"], body["error"]["code"]), ("expired", "unavailable"))
        self.assertEqual(self.claim(), NQ.Answer(204, None))
        # The same request_id is a finished record now:  sending the request again answers it, never re-queues it.
        self.assertEqual(self.assert_answer(self.enqueue(request()), 200)["state"], "expired")

    def test_a_restart_keeps_queued_and_running_records(self) -> None:
        self.assert_answer(self.enqueue(request(rid="restart-request-1")), 202)
        self.clock.advance(1)
        self.assert_answer(self.enqueue(request(rid="restart-request-2")), 202)
        self.clock.advance(1)
        self.assert_answer(self.enqueue(request(rid="restart-request-3")), 202)
        running = self.assert_answer(self.claim(), 200)
        self.assertEqual(running["request_id"], "restart-request-1")
        done = self.assert_answer(self.claim(), 200)
        self.assert_answer(self.report(done, result={"status": "done"}), 200)
        restarted = self.make_queue()
        self.assertEqual(
            sorted(rid for _, rid in restarted.active), ["restart-request-1", "restart-request-3"]
        )
        status = lambda rid: self.assert_answer(self.status("CLAUDE", rid, queue=restarted), 200)["state"]  # noqa: E731
        self.assertEqual(
            [status(rid) for rid in ("restart-request-1", "restart-request-2", "restart-request-3")],
            ["running", "done", "queued"],
        )
        claimed = self.assert_answer(self.call("POST", "/notes/v1/runner/claim", {"wait": 0}, principal=W.RUNNER,
                                               queue=restarted), 200)
        self.assertEqual(claimed["request_id"], "restart-request-3")
        # The claim survives the restart too:  the runner's report for the request it held still lands.
        payload = {"seat": "CLAUDE", "request_id": "restart-request-1", "claim": running["claim"], "result": {"status": "done"}}
        body = self.assert_answer(
            self.call("POST", "/notes/v1/runner/result", payload, principal=W.RUNNER, queue=restarted), 200
        )
        self.assertEqual(body["state"], "done")


class ActionLogTests(QueueHarness):
    def entry(self, entry_id: str, **fields: Any) -> dict:
        row = {"id": entry_id, "ts": N.iso_now(), "mode": "create", "seat": "CLAUDE", "title": "[AFC, Claude] t",
               "outcome": "ok", "exit": 0}
        row.update(fields)
        return row

    def append(self, lines: list[Any]) -> dict:
        return self.assert_answer(
            self.call("POST", "/notes/v1/runner/log", {"lines": lines}, principal=W.RUNNER), 200
        )

    def test_the_log_append_drops_duplicate_ids_and_lines_that_are_not_log_lines(self) -> None:
        body = self.append([self.entry("log-1"), self.entry("log-2"), self.entry("log-1"), {"no": "id"}, "text"])
        self.assertEqual((body["accepted"], body["duplicates"]), (2, 1))
        body = self.append([self.entry("log-2"), self.entry("log-3")])
        self.assertEqual((body["accepted"], body["duplicates"]), (1, 1))
        # A restart remembers the ids already kept.
        restarted = self.make_queue()
        body = self.assert_answer(
            self.call("POST", "/notes/v1/runner/log", {"lines": [self.entry("log-3")]}, principal=W.RUNNER,
                      queue=restarted),
            200,
        )
        self.assertEqual((body["accepted"], body["duplicates"]), (0, 1))
        # A rotation moves the kept lines along (.1, .2, ...):  a restart still knows their ids, from every copy, so a
        # resend is not kept twice.
        log = Path(self.root) / NQ.QUEUE_DIR / NQ.ACTION_LOG
        log.rename(log.with_name("%s.%d" % (NQ.ACTION_LOG, NQ.LOG_ROTATIONS)))
        rotated = self.make_queue()
        body = self.assert_answer(
            self.call("POST", "/notes/v1/runner/log", {"lines": [self.entry("log-1"), self.entry("log-5")]},
                      principal=W.RUNNER, queue=rotated),
            200,
        )
        self.assertEqual((body["accepted"], body["duplicates"]), (1, 1))
        self.assert_answer(
            self.call("POST", "/notes/v1/runner/log", {"lines": [self.entry("x")] * (NQ.LOG_BATCH_MAX + 1)},
                      principal=W.RUNNER),
            400,
        )
        # Only known fields are kept, so nothing the runner sends can add a note's text to the log.
        self.append([self.entry("log-4", body="SECRET NOTE TEXT")])
        self.assertNotIn("SECRET NOTE TEXT", (Path(self.root) / NQ.QUEUE_DIR / NQ.ACTION_LOG).read_text())

    def test_start_up_reads_the_newest_files_only_until_it_has_enough_ids(self) -> None:
        log = Path(self.root) / NQ.QUEUE_DIR / NQ.ACTION_LOG
        self.append([self.entry("old-1"), self.entry("old-2")])
        log.rename(log.with_name("%s.1" % NQ.ACTION_LOG))
        self.append([self.entry("new-1"), self.entry("new-2")])
        with mock.patch.object(NQ, "LOG_IDS_KEEP", 2), mock.patch.object(NQ.L, "read_jsonl", wraps=NQ.L.read_jsonl) as read:
            restarted = self.make_queue()
        self.assertEqual(list(restarted.log_ids), ["new-1", "new-2"])
        self.assertEqual([Path(c.args[0]).name for c in read.call_args_list if NQ.ACTION_LOG in c.args[0]], [NQ.ACTION_LOG])

    def test_the_id_check_remembers_more_ids_than_the_mac_keeps_lines(self) -> None:
        # A runner that has lost its place sends every line the Mac keeps again:  six files of 5 MiB, each line at
        # least as long as the shortest one apple-notes-coding.sh writes (every field there, each as short as it gets).
        flags = {"notify": False, "needs_owner": False, "html": False, "existing_only": False, "summary": False, "pr": None}
        shortest = {"v": 1, "id": "0" * 32, "ts": "2026-10-23T18:00:00.000+00:00", "host": None, "mode": None, "title": None,
                    "note_id": None, "exit": 0, "outcome": "", "duration_ms": None, "seat": None, "session": None,
                    "caller": "script", "request_id": None, "requester": None, "input_bytes": None, "body_bytes": None,
                    "body_sha256": None, "reformatted": None, "pin": None, "flags": flags}
        size = len(json.dumps(shortest, separators=(",", ":"))) + 1
        most = (N.ACTION_LOG_ROTATIONS + 1) * (5 * 1024 * 1024 + size) // size  # a file can pass 5 MiB by one line
        self.assertGreater(NQ.LOG_IDS_KEEP, most)

    def test_an_odd_line_never_costs_the_batch_and_a_failed_append_keeps_its_ids_unmarked(self) -> None:
        # JSON's Infinity parses to a float with no int:  that one field is dropped, and the rest of the batch is kept.
        body = self.append([self.entry("odd-1", exit=float("inf"), duration_ms=float("nan")), self.entry("odd-2")])
        self.assertEqual((body["accepted"], body["duplicates"]), (2, 0))
        kept = [json.loads(line) for line in (Path(self.root) / NQ.QUEUE_DIR / NQ.ACTION_LOG).read_text().splitlines()]
        self.assertEqual([(e["id"], e.get("exit")) for e in kept], [("odd-1", None), ("odd-2", 0)])
        # A failed append marks no id, so the runner's resend of the same batch is kept, not dropped as duplicates.
        with mock.patch.object(NQ.L, "append_jsonl", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.call("POST", "/notes/v1/runner/log", {"lines": [self.entry("late-1")]}, principal=W.RUNNER)
        body = self.append([self.entry("late-1"), self.entry("late-1")])
        self.assertEqual((body["accepted"], body["duplicates"]), (1, 1), "a repeat inside one batch is still one line")

    def test_the_log_read_filters_newest_first(self) -> None:
        older = N.iso_now(lambda: time.time() - 7200)
        self.append([
            self.entry("log-a", ts=older),
            self.entry("log-b", mode="update", title="[AFC, Claude] Other"),
            self.entry("log-c", seat="JET", title="[AFC, JET] t"),
            self.entry("log-d"),
        ])
        ids = lambda query: [e["id"] for e in self.assert_answer(  # noqa: E731
            self.call("GET", "/notes/v1/log" + ("?" + query if query else "")), 200)["entries"]]
        self.assertEqual(set(ids("")), {"log-a", "log-b", "log-c", "log-d"})
        self.assertEqual(ids("since_hours=1&by_seat=CLAUDE&action=create"), ["log-d"])
        self.assertEqual(ids("action=update"), ["log-b"])
        self.assertEqual(ids("by_seat=jet"), ["log-c"])
        self.assertEqual(ids("title=other"), ["log-b"])
        self.assertEqual(len(ids("limit=2")), 2)
        body = self.assert_answer(self.call("GET", "/notes/v1/log?by_seat=CLAUDE", principal=W.SERVER), 200)
        self.assertEqual(body["count"], len(body["entries"]))
        for bad in ("since_hours=abc", "limit=0", "colour=red", "action=delete"):
            self.assert_answer(self.call("GET", "/notes/v1/log?" + bad), 400, "invalid_argument")


# --------------------------------------------------------------------------------------------
# Over a real socket:  the queue behind the listener API, with the read API beside it
# --------------------------------------------------------------------------------------------


class ListenerApiSocketTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="notes-queue-api-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = str(self.tmp / "state")
        self.read_key = secrets.token_hex(24)
        self.worker_key = secrets.token_hex(24)
        self.queue = NQ.NotesQueue(self.root, {W.WORKER: (self.worker_key, ("CLAUDE",))}, env={})
        self.api = API.ListenerReadApi("127.0.0.1", 0, self.root, {"CLAUDE": self.read_key}, notes=self.queue)
        self.api.start()
        self.addCleanup(self.api.close)
        host, port = self.api.address
        self.base = "http://%s:%d" % (host, port)

    def send(self, method: str, path: str, payload: Any = None, key: str | None = None) -> tuple[int, Any]:
        body = b"" if payload is None else json.dumps(payload).encode("utf-8")
        headers = W.headers(key or self.worker_key, method, path, "", W.WORKER, body, time.time())
        req = urllib.request.Request(self.base + path, data=body if method == "POST" else None, method=method,
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                raw = response.read()
                return response.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            with exc:
                raw = exc.read()
            return exc.code, json.loads(raw) if raw else None

    def test_a_request_goes_in_and_its_record_comes_back(self) -> None:
        req = request(rid="socket-request-1")
        status, body = self.send("POST", "/notes/v1/requests", {"seat": "CLAUDE", "request": req})
        self.assertEqual((status, body["state"]), (202, "queued"))
        status, record = self.send("GET", "/notes/v1/requests/CLAUDE/socket-request-1")
        self.assertEqual(status, 200)
        self.assertEqual((record["request_id"], record["action"], record["principal"]), ("socket-request-1", "create", "WORKER"))
        self.assertEqual(self.send("POST", "/notes/v1/requests", {"seat": "CLAUDE", "request": req})[0], 200)
        self.assertEqual(self.send("GET", "/notes/v1/requests/CLAUDE/socket-request-1", key=secrets.token_hex(24))[0], 401)
        self.assertEqual(self.send("GET", "/notes/v1/requests/CLAUDE/no-such-request")[0], 404)

    def test_the_read_api_answers_as_before(self) -> None:
        path = "/listener/v1/CLAUDE/status"
        stamp = str(int(time.time()))
        req = urllib.request.Request(self.base + path, headers={
            API.TIMESTAMP_HEADER: stamp, API.SIGNATURE_HEADER: API.sign(self.read_key, path, "", stamp)})
        with urllib.request.urlopen(req, timeout=5) as response:
            body = json.loads(response.read())
            self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual((body["contract"], body["seat"], body["record"]), (API.CONTRACT, "CLAUDE", "status"))
        self.assertEqual(body["view"]["inbox_pending"], 0)
        # POST outside /notes/v1/ is still refused, and an unsigned read is still 401.
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(urllib.request.Request(self.base + path, data=b"{}", method="POST"), timeout=5)
        caught.exception.close()
        self.assertEqual(caught.exception.code, 405)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(self.base + path, timeout=5)
        caught.exception.close()
        self.assertEqual(caught.exception.code, 401)


# --------------------------------------------------------------------------------------------
# The daemon's wiring (daemon._configure_listener_api)
# --------------------------------------------------------------------------------------------


class DaemonWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="notes-queue-daemon-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.events: list[tuple[str, dict]] = []
        self.keys = {name: secrets.token_hex(24) for name in W.PRINCIPALS}

    def daemon(self, *, env: dict[str, str] | None = None, enabled: bool = True, api: bool = True,
               worker: tuple[str, ...] = ("CLAUDE", "JET"), seat_keys: bool = False) -> D.Daemon:
        d = D.Daemon.__new__(D.Daemon)
        d.hidden, d.hidden_lock = [], threading.Lock()
        d.root = str(self.tmp / "state")
        d.clock = D.Clock()
        d.log = SimpleNamespace(write=lambda event, **fields: self.events.append((event, fields)))
        d.listener_api_server = None
        d.notes_queue = None
        d._notes_queue_spec = None
        d.seats = {"JET": object()} if seat_keys else {}
        d.env = dict(env if env is not None else {W.KEY_NAMES[name]: self.keys[name] for name in W.PRINCIPALS})
        if seat_keys:
            d.env["JET_ROUTINE_KEY"] = "j" * 40
        d.config = SimpleNamespace(
            listener_api_enabled=api,
            listener_api_bind="127.0.0.1",
            listener_api_port=0,
            notes_queue_enabled=enabled,
            notes_queue_worker_seats=frozenset(worker),
            notes_queue_server_seats=frozenset({"CODEX"}),
            seats={"JET": SimpleNamespace(routine=None)} if seat_keys else {},
        )
        self.addCleanup(self.close, d)
        return d

    @staticmethod
    def close(d: D.Daemon) -> None:
        if d.listener_api_server is not None:
            d.listener_api_server.close()

    def names(self) -> list[str]:
        return [event for event, _ in self.events]

    def fields(self, name: str) -> dict:
        return [f for event, f in self.events if event == name][-1]

    def test_the_queue_starts_with_its_principals_and_every_key_is_hidden(self) -> None:
        d = self.daemon()
        d._configure_listener_api()
        self.assertIsNotNone(d.listener_api_server)
        self.assertIs(d.listener_api_server.notes, d.notes_queue)
        self.assertEqual(sorted(d.notes_queue.principals), ["RUNNER", "SERVER", "WORKER"])
        self.assertEqual(d.notes_queue.principals["WORKER"].seats, frozenset({"CLAUDE", "JET"}))
        self.assertEqual(d.notes_queue.principals["RUNNER"].seats, frozenset())
        self.assertEqual(self.fields("notes-queue-started"), {"principals": ["RUNNER", "SERVER", "WORKER"]})
        self.assertIn("listener-api-started", self.names())
        for key in self.keys.values():
            self.assertIn(key, d.hidden)
            self.assertNotIn(key, json.dumps(self.events))
            self.assertEqual(d.scrub("x %s y" % key), "x [redacted] y")

    def test_quoted_keys_are_stripped_and_a_missing_runner_key_is_a_warning(self) -> None:
        env = {W.KEY_NAMES["WORKER"]: '"%s"' % self.keys["WORKER"]}
        d = self.daemon(env=env)
        d._configure_listener_api()
        self.assertEqual(d.notes_queue.principals["WORKER"].key, self.keys["WORKER"])
        started = self.fields("notes-queue-started")
        self.assertEqual(started["principals"], ["WORKER"])
        self.assertIn("nothing can claim a request", started["warning"])

    def test_no_key_means_no_queue_and_with_no_seat_key_no_server(self) -> None:
        d = self.daemon(env={})
        d._configure_listener_api()
        self.assertIsNone(d.notes_queue)
        self.assertIsNone(d.listener_api_server)
        self.assertIn("no key is set", self.fields("notes-queue-off")["reason"])
        self.assertIn("not started", self.fields("listener-api-deny-all")["reason"])
        short = self.daemon(env={W.KEY_NAMES["WORKER"]: "k" * 20})
        short._configure_listener_api()
        self.assertIsNone(short.notes_queue)
        self.assertIn("shorter than 32", self.fields("notes-queue-off")["reason"])
        self.assertIn("k" * 20, short.hidden)

    def test_seat_keys_alone_still_start_the_read_api_without_a_queue(self) -> None:
        d = self.daemon(enabled=False, seat_keys=True)
        d._configure_listener_api()
        self.assertIsNotNone(d.listener_api_server)
        self.assertIsNone(d.listener_api_server.notes)
        self.assertIsNone(d.notes_queue)
        self.assertEqual(self.fields("listener-api-started")["seats"], ["JET"])
        self.assertNotIn("notes-queue-started", self.names())
        self.assertNotIn("notes-queue-off", self.names())

    def test_the_queue_needs_the_listener_api(self) -> None:
        d = self.daemon(api=False)
        d._configure_listener_api()
        self.assertIsNone(d.listener_api_server)
        self.assertIsNone(d.notes_queue)
        self.assertIn("[listener_api] is off", self.fields("notes-queue-off")["reason"])

    def test_a_reload_rebuilds_the_server_and_keeps_an_unchanged_queue(self) -> None:
        d = self.daemon()
        d._configure_listener_api()
        first_server, first_queue = d.listener_api_server, d.notes_queue
        d._configure_listener_api()
        self.assertIsNot(d.listener_api_server, first_server)
        self.assertIs(d.notes_queue, first_queue, "same principals:  the same queue object")
        self.assertIs(d.listener_api_server.notes, first_queue)
        d.config.notes_queue_worker_seats = frozenset({"CLAUDE"})
        d._configure_listener_api()
        self.assertIsNot(d.notes_queue, first_queue)
        self.assertEqual(d.notes_queue.principals["WORKER"].seats, frozenset({"CLAUDE"}))
        d.config.notes_queue_enabled = False
        d._configure_listener_api()
        self.assertIsNone(d.notes_queue)
        self.assertIsNone(d.listener_api_server, "no seat key and no queue:  nothing to serve")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
