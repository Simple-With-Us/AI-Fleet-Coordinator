"""External own-seat proof: refusal set, audit binding, receipt gates, replay store.

Forged, stale, replayed, wrong-seat and other-transaction receipts are refused;
a receipt is accepted only with exactly one matching row in the relay's actual
audit log; a timeout is an inconclusive record that is never signed and whose
retry carries a fresh id. The replay store survives a process restart.
"""

import json
import os
import tempfile
import time
import unittest

from git_relay.deploy_proof import (
    INCONCLUSIVE,
    NULL_OID,
    PROVEN,
    REFUSALS,
    REFUSED,
    ExternalProof,
    ReplayStore,
    probe_path,
    sign_receipt,
    verify_receipt,
)

REPO = "Simple-With-Us/AI-Fleet-Coordinator"
KEY = b"test-only proof key"
BEFORE = "a" * 64
AFTER = "b" * 64


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


def audit_row(record, **over):
    """The row the relay's audit log actually writes for the denied probe call."""
    row = {
        "reason": record.get("refusal", "default branch is not writable"),
        "branch": "main",
        "path": record["probe_path"],
        "expected_head_sha": NULL_OID,
        "ts": "2026-11-08T01:00:00Z",
        "seat": record["seat"],
        "action": "wf_file_patch",
        "repo": record["repo"],
        "result": "bad_request",
    }
    row.update(over)
    return row


class ExternalProofTests(unittest.TestCase):
    def test_proven_record_carries_audit_correlation_fields(self):
        relay = FakeRelay()
        record = proof(relay)("swu-zc", "proof-0001", BEFORE, AFTER)
        self.assertEqual(record["outcome"], PROVEN)
        self.assertEqual(record["refusal"], "default branch is not writable")
        for field in (
            "v",
            "seat",
            "proof_id",
            "ts",
            "url",
            "repo",
            "probe_path",
            "policy_before",
            "policy_after",
        ):
            self.assertIn(field, record)
        # The proof id rides the negative probe's PATH: the field the relay's
        # audit row carries for a denied workflow call (never the message).
        probe = next(body for url, body in relay.requests if "wf_file_patch" in url)
        self.assertEqual(probe["path"], probe_path("proof-0001"))
        self.assertIn("proof-0001", probe["path"])
        self.assertEqual(record["probe_path"], probe["path"])
        self.assertEqual((record["policy_before"], record["policy_after"]), (BEFORE, AFTER))

    def test_probe_is_mutation_free_by_construction(self):
        relay = FakeRelay()
        proof(relay)("swu-zc", "proof-0005", BEFORE, AFTER)
        probe = next(body for url, body in relay.requests if "wf_file_patch" in url)
        # The git null object id never names a ref head, commit or blob, so no
        # provider write can be satisfied from this request whatever the
        # service allows (the relay's comparisons cannot match it, and the
        # provider enforces expectedHeadOid server-side). Empty edits on top.
        self.assertEqual(NULL_OID, "0" * 40)
        self.assertEqual(probe["expected_head_sha"], NULL_OID)
        self.assertEqual(probe["base_blob_sha"], NULL_OID)
        self.assertEqual(probe["edits"], [{"start": 0, "end": 0, "text": ""}])
        self.assertEqual(probe["branch"], "main")

    def test_every_documented_refusal_proves_without_a_manifest_digest(self):
        for refusal in sorted(REFUSALS):
            record = proof(FakeRelay(refusal=refusal))("swu-zc", "proof-0002", BEFORE, AFTER)
            self.assertEqual(record["outcome"], PROVEN, refusal)
            self.assertEqual(record["refusal"], refusal)

    def test_a_wrong_answer_or_failed_read_is_refused_not_proven(self):
        record = proof(FakeRelay(refusal="something else entirely"))("swu-zc", "p1", BEFORE, AFTER)
        self.assertEqual(record["outcome"], REFUSED)

    def test_an_unpathable_proof_id_is_refused_before_any_call(self):
        relay = FakeRelay()
        with self.assertRaises(ValueError):
            proof(relay)("swu-zc", "bad/id with spaces", BEFORE, AFTER)
        self.assertEqual(relay.requests, [])

    def test_timeout_is_inconclusive_and_never_signed(self):
        relay = FakeRelay(fail_probe=TimeoutError("timed out"))
        record = proof(relay)("swu-zc", "proof-0003", BEFORE, AFTER)
        self.assertEqual(record["outcome"], INCONCLUSIVE)
        self.assertEqual(record["stage"], "probe")
        with self.assertRaises(ValueError):
            sign_receipt(KEY, record)
        # The recovery contract: a retry runs under a FRESH proof id, so the old
        # id's unknown relay state can never be replayed as a success.
        relay.fail_probe = None
        retry = proof(relay)("swu-zc", "proof-0004", BEFORE, AFTER)
        self.assertEqual(retry["outcome"], PROVEN)
        self.assertNotEqual(retry["proof_id"], record["proof_id"])

    def test_read_timeout_is_inconclusive(self):
        record = proof(FakeRelay(fail_read=TimeoutError("timed out")))("swu-zc", "p2", BEFORE, AFTER)
        self.assertEqual((record["outcome"], record["stage"]), (INCONCLUSIVE, "read"))


class ReceiptGateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.store = ReplayStore(os.path.join(self.dir.name, "proofs.json"))
        self.record = proof(FakeRelay())("swu-zc", "proof-1001", BEFORE, AFTER)
        self.receipt = sign_receipt(KEY, self.record)
        self.now = int(time.time())

    def tearDown(self):
        self.dir.cleanup()

    def verify(self, receipt, **over):
        kwargs = {
            "seat": "swu-zc",
            "now": self.now,
            "store": self.store,
            "policy_before": BEFORE,
            "policy_after": AFTER,
            "audit_rows": [audit_row(self.record)],
        }
        kwargs.update(over)
        return verify_receipt(KEY, receipt, **kwargs)

    def test_signed_receipt_verifies_once(self):
        record = self.verify(self.receipt)
        self.assertEqual(record["proof_id"], "proof-1001")
        with self.assertRaisesRegex(ValueError, "replayed"):
            self.verify(self.receipt)

    def test_replay_refusal_survives_a_process_restart(self):
        self.verify(self.receipt)
        # A new process reads the same store file: the accepted id persists.
        fresh = ReplayStore(os.path.join(self.dir.name, "proofs.json"))
        with self.assertRaisesRegex(ValueError, "replayed"):
            self.verify(self.receipt, store=fresh)

    def test_a_malformed_store_refuses_everything(self):
        with open(os.path.join(self.dir.name, "proofs.json"), "w") as fh:
            fh.write("not json")
        with self.assertRaisesRegex(ValueError, "malformed"):
            ReplayStore(os.path.join(self.dir.name, "proofs.json"))

    def test_forged_receipts_are_refused(self):
        with self.assertRaisesRegex(ValueError, "forged"):
            verify_receipt(
                b"another key",
                self.receipt,
                seat="swu-zc",
                now=self.now,
                store=self.store,
                policy_before=BEFORE,
                policy_after=AFTER,
                audit_rows=[audit_row(self.record)],
            )
        tampered = {
            "record": dict(self.receipt["record"], refusal="default branch is not writable!"),
            "hmac": self.receipt["hmac"],
        }
        with self.assertRaisesRegex(ValueError, "forged"):
            self.verify(tampered)

    def test_stale_receipt_is_refused(self):
        with self.assertRaisesRegex(ValueError, "stale"):
            self.verify(self.receipt, now=self.now + 601)

    def test_wrong_seat_receipt_is_refused(self):
        with self.assertRaisesRegex(ValueError, "another seat"):
            self.verify(self.receipt, seat="swu-echo")

    def test_receipt_of_another_transaction_is_refused(self):
        with self.assertRaisesRegex(ValueError, "another transaction"):
            self.verify(self.receipt, policy_before="c" * 64)
        with self.assertRaisesRegex(ValueError, "another transaction"):
            self.verify(self.receipt, policy_after="d" * 64)

    def test_receipt_of_another_outcome_is_refused(self):
        record = dict(self.record, outcome=INCONCLUSIVE)
        with self.assertRaises(ValueError):
            sign_receipt(KEY, record)

    def test_audit_binding_is_required_and_authoritative(self):
        # No matching audit row: the operator's signed claim alone is not proof.
        with self.assertRaisesRegex(ValueError, "relay audit"):
            self.verify(self.receipt, audit_rows=[])
        # A row for another probe path (another proof id) does not bind this one.
        with self.assertRaisesRegex(ValueError, "relay audit"):
            self.verify(self.receipt, audit_rows=[audit_row(self.record, path="deploy-proof/other.probe")])
        # A row whose reason differs from the recorded refusal does not bind.
        with self.assertRaisesRegex(ValueError, "relay audit"):
            self.verify(self.receipt, audit_rows=[audit_row(self.record, reason="seat policy")])
        # Two matching rows are ambiguous: refuse, and nothing is stored.
        with self.assertRaisesRegex(ValueError, "relay audit"):
            self.verify(self.receipt, audit_rows=[audit_row(self.record), audit_row(self.record)])
        self.assertFalse(self.store.seen("proof-1001"))

    def test_dry_run_verification_consumes_nothing(self):
        # The guarded transaction's dry run checks every gate without storing;
        # the apply then verifies the same receipt again and records it.
        self.verify(self.receipt, store_id=False)
        self.assertFalse(self.store.seen("proof-1001"))
        self.assertEqual(self.verify(self.receipt)["proof_id"], "proof-1001")

    def test_a_refused_receipt_is_never_recorded(self):
        with self.assertRaisesRegex(ValueError, "stale"):
            self.verify(self.receipt, now=self.now + 601)
        self.assertFalse(self.store.seen("proof-1001"))
        # The same receipt inside its window then verifies: only acceptance stores.
        self.assertEqual(self.verify(self.receipt)["proof_id"], "proof-1001")


if __name__ == "__main__":
    unittest.main()
