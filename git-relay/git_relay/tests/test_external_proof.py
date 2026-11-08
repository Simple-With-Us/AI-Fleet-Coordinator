"""External own-seat proof: refusal set, audit-correlation fields, receipt gates.

Forged, stale, replayed and wrong-seat receipts are refused; a timeout is an
inconclusive record that is never signed and whose retry carries a fresh id.
"""

import json
import time
import unittest

from git_relay.deploy_proof import (
    INCONCLUSIVE,
    PROVEN,
    REFUSALS,
    REFUSED,
    ExternalProof,
    sign_receipt,
    verify_receipt,
)

REPO = "Simple-With-Us/AI-Fleet-Coordinator"
KEY = b"test-only proof key"


class _Response:
    def __init__(self, status, payload):
        self.code = self.status = status
        self._raw = json.dumps(payload).encode()

    def read(self, _limit):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeRelay:
    """Answers as the current API: repo read plus the documented probe refusal."""

    def __init__(self, refusal="default branch is not writable", fail_read=None, fail_probe=None):
        self.refusal = refusal
        self.fail_read = fail_read
        self.fail_probe = fail_probe
        self.requests = []

    def open(self, request, timeout):
        body = json.loads(request.data.decode())
        self.requests.append((request.full_url, body))
        if self.fail_read and "wf_repo_get" in request.full_url:
            raise self.fail_read
        if self.fail_probe and "wf_file_patch" in request.full_url:
            raise self.fail_probe
        if "wf_repo_get" in request.full_url:
            return _Response(200, {"repository": {"full_name": REPO, "default_branch": "main"}})
        return _Response(400, {"error": self.refusal})


def proof(relay):
    return ExternalProof("test-bearer", opener=relay)


class ExternalProofTests(unittest.TestCase):
    def test_proven_record_carries_audit_correlation_fields(self):
        relay = FakeRelay()
        record = proof(relay)("swu-zc", "proof-0001")
        self.assertEqual(record["outcome"], PROVEN)
        self.assertEqual(record["refusal"], "default branch is not writable")
        for field in ("v", "seat", "proof_id", "ts", "url", "repo"):
            self.assertIn(field, record)
        # The proof id rides the negative probe's message, so the relay's audit
        # detail (canonical arguments of the denied call) names exactly this run.
        probe = next(body for url, body in relay.requests if "wf_file_patch" in url)
        self.assertIn("proof-0001", probe["message"])
        self.assertEqual(probe["expected_head_sha"], "0" * 40)

    def test_every_documented_refusal_proves_without_a_manifest_digest(self):
        for refusal in sorted(REFUSALS):
            record = proof(FakeRelay(refusal=refusal))("swu-zc", "proof-0002")
            self.assertEqual(record["outcome"], PROVEN, refusal)
            self.assertEqual(record["refusal"], refusal)

    def test_a_wrong_answer_or_failed_read_is_refused_not_proven(self):
        record = proof(FakeRelay(refusal="something else entirely"))("swu-zc", "p1")
        self.assertEqual(record["outcome"], REFUSED)

    def test_timeout_is_inconclusive_and_never_signed(self):
        relay = FakeRelay(fail_probe=TimeoutError("timed out"))
        record = proof(relay)("swu-zc", "proof-0003")
        self.assertEqual(record["outcome"], INCONCLUSIVE)
        self.assertEqual(record["stage"], "probe")
        with self.assertRaises(ValueError):
            sign_receipt(KEY, record)
        # The recovery contract: a retry runs under a FRESH proof id, so the old
        # id's unknown relay state can never be replayed as a success.
        relay.fail_probe = None
        retry = proof(relay)("swu-zc", "proof-0004")
        self.assertEqual(retry["outcome"], PROVEN)
        self.assertNotEqual(retry["proof_id"], record["proof_id"])

    def test_read_timeout_is_inconclusive(self):
        record = proof(FakeRelay(fail_read=TimeoutError("timed out")))("swu-zc", "p2")
        self.assertEqual((record["outcome"], record["stage"]), (INCONCLUSIVE, "read"))


class ReceiptGateTests(unittest.TestCase):
    def setUp(self):
        self.record = proof(FakeRelay())("swu-zc", "proof-1001")
        self.receipt = sign_receipt(KEY, self.record)
        self.now = int(time.time())

    def test_signed_receipt_verifies_once(self):
        seen = set()
        record = verify_receipt(KEY, self.receipt, seat="swu-zc", now=self.now, seen_proof_ids=seen)
        self.assertEqual(record["proof_id"], "proof-1001")
        seen.add(record["proof_id"])
        with self.assertRaisesRegex(ValueError, "replayed"):
            verify_receipt(KEY, self.receipt, seat="swu-zc", now=self.now, seen_proof_ids=seen)

    def test_forged_receipts_are_refused(self):
        with self.assertRaisesRegex(ValueError, "forged"):
            verify_receipt(
                b"another key", self.receipt, seat="swu-zc", now=self.now, seen_proof_ids=set()
            )
        tampered = {
            "record": dict(self.receipt["record"], refusal="default branch is not writable!"),
            "hmac": self.receipt["hmac"],
        }
        with self.assertRaisesRegex(ValueError, "forged"):
            verify_receipt(KEY, tampered, seat="swu-zc", now=self.now, seen_proof_ids=set())

    def test_stale_receipt_is_refused(self):
        later = self.now + 601
        with self.assertRaisesRegex(ValueError, "stale"):
            verify_receipt(KEY, self.receipt, seat="swu-zc", now=later, seen_proof_ids=set())

    def test_wrong_seat_receipt_is_refused(self):
        with self.assertRaisesRegex(ValueError, "another seat"):
            verify_receipt(KEY, self.receipt, seat="swu-echo", now=self.now, seen_proof_ids=set())

    def test_receipt_of_another_outcome_is_refused(self):
        record = dict(self.record, outcome=INCONCLUSIVE)
        with self.assertRaises(ValueError):
            sign_receipt(KEY, record)


if __name__ == "__main__":
    unittest.main()
