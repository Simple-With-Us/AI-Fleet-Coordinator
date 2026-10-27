import json
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from whatsapp_bridge import outbound
from whatsapp_bridge.bridge import State
from whatsapp_bridge.outbound import Outbox, body_hash

KEY = "k" * 64
WORKER = "https://worker.example"


class FakeWorker:
    """The Worker's outbox: one reply at a time, and every result it gets."""

    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.results = []
        self.requests = []

    def __call__(self, url, headers, body=None, **_options):
        self.requests.append((url, headers, body))
        decoded = json.loads(body)
        if url.endswith("/internal/wa-outbox/JET"):
            return {"ok": True, "reply": self.replies.pop(0) if self.replies else None}
        self.results.append(decoded)
        return {"ok": True, "state": decoded.get("outcome")}


def reply(intent_id="wa_reply_" + "a" * 32, text="hello Jay", **over):
    row = {
        "intent_id": intent_id,
        "text": text,
        "reply_to_seq": None,
        "reply_to_id_hash": None,
        "queued_at": 1792700000,
        "lease_until": 1792700300,
        "token": "11111111-2222-3333-4444-555555555555",
        "attempts": 1,
    }
    row.update(over)
    return row


class OutboundTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.state = State(self.directory.name)
        self.state.record_recipient("user:owner", 1792700000)
        self.slept = []

    def tearDown(self):
        self.state.close()
        self.directory.cleanup()

    def outbox(self, worker, enabled=True):
        return Outbox(
            self.state,
            "synthetic-key",
            KEY,
            WORKER,
            request=worker,
            clock=lambda: 1792700000,
            sleeper=self.slept.append,
            enabled=(lambda: enabled),
        )

    def test_a_reply_is_sent_once_and_the_result_carries_the_message_id(self):
        worker = FakeWorker([reply()])
        sent = []

        def fake_send(api_key, recipient, text, context_id=None, timeout=outbound.SEND_TIMEOUT):
            sent.append((api_key, recipient, text, context_id))
            return {"outcome": "sent", "message_id": "wamid.sent"}

        with patch.object(outbound, "send", fake_send):
            answer = self.outbox(worker).step()
        self.assertEqual(answer["state"], "sent")
        self.assertEqual(sent, [("synthetic-key", "user:owner", "hello Jay", None)])
        self.assertEqual(len(worker.results), 1)
        self.assertEqual(worker.results[0]["outcome"], "sent")
        self.assertEqual(worker.results[0]["message_id"], "wamid.sent")
        self.assertEqual(worker.results[0]["contract"], "wa-outbox-result/1")
        row = self.state.db.execute("SELECT state, message_id FROM send_ledger").fetchone()
        self.assertEqual(row[0], "sent")
        self.assertEqual(row[1], "wamid.sent")

    def test_the_result_body_is_signed_and_the_claim_is_signed_too(self):
        worker = FakeWorker([])
        self.outbox(worker).step()
        url, headers, body = worker.requests[0]
        self.assertTrue(url.endswith("/internal/wa-outbox/JET"))
        self.assertEqual(
            headers["X-WA-Bridge-Signature"],
            __import__("hmac").new(KEY.encode(), body, __import__("hashlib").sha256).hexdigest(),
        )

    def test_an_unknown_outcome_is_never_sent_again(self):
        worker = FakeWorker([reply()])
        calls = []

        def fake_send(*_args, **_options):
            calls.append(1)
            return {"outcome": "unknown", "status": 500, "code": 1}

        with patch.object(outbound, "send", fake_send):
            answer = self.outbox(worker).step()
        self.assertEqual(answer["state"], "outcome_unknown")
        self.assertEqual(len(calls), 1, "an unknown answer is never retried")
        self.assertEqual(worker.results[0]["outcome"], "outcome_unknown")
        # The next step finds nothing to send: the Worker holds no queue.
        with patch.object(outbound, "send", fake_send):
            self.outbox(worker).step()
        self.assertEqual(len(calls), 1, "the unknown reply is never sent again")
        row = self.state.db.execute("SELECT state FROM send_ledger").fetchone()
        self.assertEqual(row[0], "outcome_unknown")

    def test_a_rate_limit_is_retried_within_the_cap_and_refused_after_it(self):
        worker = FakeWorker([reply()])
        calls = []

        def fake_send(*_args, **_options):
            calls.append(1)
            return {"outcome": "retry", "status": 429, "code": 4, "retry_after_s": 1}

        with patch.object(outbound, "send", fake_send):
            answer = self.outbox(worker).step()
        self.assertEqual(answer["state"], "refused")
        self.assertEqual(len(calls), outbound.MAX_ATTEMPTS)
        self.assertEqual(worker.results[0]["outcome"], "refused")
        self.assertEqual(worker.results[0]["error"], "retry_cap_reached")

    def test_a_service_error_with_the_documented_code_is_retried(self):
        worker = FakeWorker([reply()])
        calls = []

        def fake_send(*_args, **_options):
            calls.append(1)
            if len(calls) == 1:
                return {
                    "outcome": "retry",
                    "status": 503,
                    "code": outbound.RETRYABLE_CODE,
                    "retry_after_s": 1,
                }
            return {"outcome": "sent", "message_id": "wamid.sent"}

        with patch.object(outbound, "send", fake_send):
            answer = self.outbox(worker).step()
        self.assertEqual(answer["state"], "sent")
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.slept, [1])

    def test_a_definite_refusal_keeps_its_code_and_is_never_retried(self):
        worker = FakeWorker([reply()])
        calls = []

        def fake_send(*_args, **_options):
            calls.append(1)
            return {"outcome": "refused", "status": 403, "code": 131005}

        with patch.object(outbound, "send", fake_send):
            answer = self.outbox(worker).step()
        self.assertEqual(answer["state"], "refused")
        self.assertEqual(len(calls), 1)
        self.assertEqual(worker.results[0]["status"], 403)
        self.assertEqual(worker.results[0]["code"], 131005)

    def test_a_connection_that_never_opened_goes_back_to_the_queue(self):
        worker = FakeWorker([reply()])
        with patch.object(outbound, "send", lambda *a, **k: {"outcome": "requeue"}):
            answer = self.outbox(worker).step()
        self.assertEqual(answer["state"], "requeue")
        self.assertEqual(worker.results[0]["outcome"], "requeue")
        self.assertEqual(self.state.db.execute("SELECT COUNT(*) FROM send_ledger").fetchone()[0], 0)

    def test_the_switch_off_claims_nothing_and_sends_nothing(self):
        worker = FakeWorker([reply()])
        calls = []
        with patch.object(
            outbound,
            "send",
            lambda *a, **k: calls.append(1) or {"outcome": "sent", "message_id": "x"},
        ):
            answer = self.outbox(worker, enabled=False).step()
        self.assertEqual(answer["state"], "send_disabled")
        self.assertEqual(len(calls), 0)
        self.assertEqual(len(worker.requests), 0, "nothing is claimed while the switch is off")

    def test_the_switch_flipping_before_the_post_is_a_decline_and_never_an_unknown(self):
        worker = FakeWorker([reply()])
        switches = [True, False]

        def enabled():
            return switches.pop(0) if switches else False

        box = Outbox(
            self.state,
            "synthetic-key",
            KEY,
            WORKER,
            request=worker,
            clock=lambda: 1792700000,
            sleeper=self.slept.append,
            enabled=enabled,
        )
        calls = []
        with patch.object(
            outbound,
            "send",
            lambda *a, **k: calls.append(1) or {"outcome": "sent", "message_id": "x"},
        ):
            answer = box.step()
        self.assertEqual(answer["state"], "declined")
        self.assertEqual(len(calls), 0, "the POST never started")
        self.assertEqual(worker.results[0]["outcome"], "declined")

    def test_a_restart_finds_a_claimed_reply_queued_again_and_a_sending_reply_unknown(self):
        worker = FakeWorker([])
        self.state.ledger_put(
            "wa_reply_" + "a" * 32, "claimed", body_hash("hello"), None, 1792700000
        )
        self.state.ledger_put(
            "wa_reply_" + "b" * 32, "sending", body_hash("world"), None, 1792700000
        )
        self.outbox(worker).step()
        outcomes = {row["intent_id"]: row["outcome"] for row in worker.results}
        self.assertEqual(outcomes["wa_reply_" + "a" * 32], "requeue")
        self.assertEqual(outcomes["wa_reply_" + "b" * 32], "outcome_unknown")

    def test_without_a_bound_address_nothing_is_sent(self):
        directory = tempfile.TemporaryDirectory()
        state = State(directory.name)
        worker = FakeWorker([reply()])
        try:
            box = Outbox(
                state,
                "synthetic-key",
                KEY,
                WORKER,
                request=worker,
                clock=lambda: 1792700000,
                sleeper=self.slept.append,
                enabled=lambda: True,
            )
            calls = []
            with patch.object(
                outbound,
                "send",
                lambda *a, **k: calls.append(1) or {"outcome": "sent", "message_id": "x"},
            ):
                answer = box.step()
            self.assertEqual(answer["state"], "no_recipient")
            self.assertEqual(len(calls), 0)
            self.assertEqual(len(worker.requests), 0)
        finally:
            state.close()
            directory.cleanup()

    def test_a_reply_in_context_quotes_the_message_the_bridge_still_holds(self):
        worker = FakeWorker([reply(reply_to_id_hash="b" * 64)])
        self.state.remember_target("b" * 64, "wamid.1234")
        sent = []

        def fake_send(api_key, recipient, text, context_id=None, timeout=outbound.SEND_TIMEOUT):
            sent.append(context_id)
            return {"outcome": "sent", "message_id": "wamid.sent"}

        with patch.object(outbound, "send", fake_send):
            self.outbox(worker).step()
        self.assertEqual(sent, ["wamid.1234"])

    def test_a_reply_whose_target_is_gone_goes_out_without_a_quote(self):
        worker = FakeWorker([reply(reply_to_id_hash="b" * 64)])
        sent = []

        def fake_send(api_key, recipient, text, context_id=None, timeout=outbound.SEND_TIMEOUT):
            sent.append(context_id)
            return {"outcome": "sent", "message_id": "wamid.sent"}

        with patch.object(outbound, "send", fake_send):
            self.outbox(worker).step()
        self.assertEqual(sent, [None])
        self.assertEqual(worker.results[0]["context_missing"], True)

    def test_the_provider_answer_is_classified_by_its_status_and_code(self):
        class FakeError(urllib.error.HTTPError):
            def __init__(self, status, code):
                super().__init__(
                    "https://api.whatsapp.com", status, "Bad", {"Retry-After": "7"}, None
                )
                self._body = json.dumps(
                    {"error": {"code": code, "message": "secret detail"}}
                ).encode()

            def read(self, *_args):
                return self._body

        with patch.object(outbound.urllib.request, "build_opener") as opener:
            opener.return_value.open.side_effect = FakeError(429, 4)
            self.assertEqual(outbound.send("k", "user:owner", "hi")["outcome"], "retry")
            opener.return_value.open.side_effect = FakeError(503, 131016)
            self.assertEqual(outbound.send("k", "user:owner", "hi")["outcome"], "retry")
            opener.return_value.open.side_effect = FakeError(503, 1)
            self.assertEqual(outbound.send("k", "user:owner", "hi")["outcome"], "unknown")
            opener.return_value.open.side_effect = FakeError(400, 100)
            self.assertEqual(outbound.send("k", "user:owner", "hi")["outcome"], "refused")
            opener.return_value.open.side_effect = FakeError(403, 131005)
            self.assertEqual(outbound.send("k", "user:owner", "hi")["outcome"], "refused")

    def test_at_most_ten_sends_a_minute(self):
        worker = FakeWorker([reply()])
        box = self.outbox(worker)
        box._sent_at = [1792700000 - index for index in range(outbound.SENDS_PER_MINUTE)]
        calls = []
        with patch.object(
            outbound,
            "send",
            lambda *a, **k: calls.append(1) or {"outcome": "sent", "message_id": "x"},
        ):
            answer = box.step()
        self.assertEqual(answer["state"], "rate_limited")
        self.assertEqual(len(calls), 0, "nothing is sent this minute")
        self.assertEqual(worker.results[0]["outcome"], "requeue")

    def test_the_local_ledger_holds_no_body(self):
        worker = FakeWorker([reply(text="a private reply")])
        with patch.object(outbound, "send", lambda *a, **k: {"outcome": "sent", "message_id": "x"}):
            self.outbox(worker).step()
        rows = self.state.db.execute("SELECT * FROM send_ledger").fetchall()
        self.assertNotIn("a private reply", json.dumps([list(row) for row in rows]))


    def test_failed_requeue_report_retains_ledger_and_claim_token(self):
        worker = FakeWorker([reply()])
        box = self.outbox(worker)
        box._sent_at = [1792700000] * 10
        def failing(url, *args, **kwargs):
            if url.endswith("/result"):
                raise outbound.BridgeError("not acknowledged")
            return worker(url, *args, **kwargs)
        box.request = failing
        with self.assertRaises(outbound.BridgeError): box.step()
        self.assertEqual(self.state.ledger_pending()[0][1], "claimed")
        self.assertEqual(self.state.ledger_token(reply()["intent_id"]), reply()["token"])
        box.request = worker
        box.recover()
        self.assertEqual(worker.results[-1]["token"], reply()["token"])
        self.assertEqual(self.state.ledger_pending(), [])

    def test_failed_restart_report_does_not_erase_claimed_ledger(self):
        self.state.ledger_put(reply()["intent_id"], "claimed", body_hash("hi"), None, 1792700000)
        box = self.outbox(lambda *a, **k: (_ for _ in ()).throw(outbound.BridgeError("offline")))
        with self.assertRaises(outbound.BridgeError): box.recover()
        self.assertEqual(len(self.state.ledger_pending()), 1)


    def test_long_retry_after_requeues_without_parking_inbound_loop(self):
        worker = FakeWorker([reply()])
        with patch.object(outbound, "send", lambda *a, **k: {"outcome": "retry", "status": 429, "retry_after_s": 3600}):
            result = self.outbox(worker).step()
        self.assertEqual(result["state"], "requeue")
        self.assertEqual(self.slept, [])
        self.assertEqual(worker.results[0]["retry_after_s"], 3600)


if __name__ == "__main__":
    unittest.main()
