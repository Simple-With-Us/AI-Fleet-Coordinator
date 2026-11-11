"""First-seat bootstrap proof: denial binding, receipt gates, probe bounds."""

import hashlib
import hmac
import json
import os
import tempfile
import threading
import time
import unittest

from git_relay import audit as audit_mod
from git_relay import deploy_proof
from git_relay import policy as policy_mod
from git_relay.policy import SeatPolicy

KEY = b"k" * 32
NOW = int(time.time())


def _sign(record, key=KEY):
    mac = hmac.new(
        key, json.dumps(record, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256
    ).hexdigest()
    return {"record": record, "mac": mac}


def _record(**over):
    rec = {
        "v": deploy_proof.BootstrapProof.VERSION,
        "seat": "SWU-ZC",
        "identity": "zcode",
        "proof_id": "b" * 32,
        "ts": NOW,
        "url": deploy_proof.BootstrapProof.URL,
        "repo": deploy_proof.BootstrapProof.REPO,
        "outcome": deploy_proof.PROVEN,
        "policy_before": "1" * 64,
        "policy_after": "2" * 64,
    }
    rec["probe_path"] = deploy_proof.probe_path(rec["proof_id"])
    rec.update(over)
    return rec


def _boot_row(record, identity="zcode", **over):
    row = {
        "cause": deploy_proof.BOOTSTRAP_CAUSE,
        "identity": identity,
        "action": "wf_file_patch",
        "repo": record["repo"],
        "result": "unauthenticated",
        "path": record["probe_path"],
        "seat": "unknown",
        "ts": "2026-11-10T00:00:00Z",
    }
    row.update(over)
    return row


class BootstrapReceiptTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = deploy_proof.ReplayStore(os.path.join(self.dir.name, "store.json"))
        self.rec = _record()
        self.rows = [_boot_row(self.rec)]

    def verify(self, receipt=None, **kw):
        args = dict(
            seat="SWU-ZC",
            identity="zcode",
            now=NOW,
            store=self.store,
            policy_before="1" * 64,
            policy_after="2" * 64,
            audit_rows=self.rows,
        )
        args.update(kw)
        return deploy_proof.verify_bootstrap_receipt(KEY, receipt or _sign(self.rec), **args)

    def test_happy_path_consumes_replay(self):
        record = self.verify()
        self.assertEqual(record["proof_id"], "b" * 32)
        self.assertTrue(self.store.seen("b" * 32))
        with self.assertRaisesRegex(ValueError, "replayed"):
            self.verify()

    def test_dry_run_does_not_consume(self):
        self.verify(store_id=False)
        self.verify(store_id=False)

    def test_forged_mac_refused(self):
        with self.assertRaisesRegex(ValueError, "forged"):
            self.verify(receipt=_sign(self.rec, key=b"x" * 32))
        tampered = _sign(self.rec)
        tampered["record"] = dict(self.rec, seat="OTHER")
        with self.assertRaisesRegex(ValueError, "forged"):
            self.verify(receipt=tampered)

    def test_outcome_and_target(self):
        with self.assertRaisesRegex(ValueError, "inconclusive"):
            self.verify(receipt=_sign(_record(outcome=deploy_proof.INCONCLUSIVE)))
        with self.assertRaisesRegex(ValueError, "proof target"):
            self.verify(receipt=_sign(_record(v=2)))
        with self.assertRaisesRegex(ValueError, "proof target"):
            self.verify(receipt=_sign(_record(url="https://elsewhere.example/v1/")))

    def test_freshness(self):
        with self.assertRaisesRegex(ValueError, "stale"):
            self.verify(receipt=_sign(_record(ts=NOW - 601)))
        with self.assertRaisesRegex(ValueError, "stale"):
            self.verify(receipt=_sign(_record(ts=NOW + 1)))

    def test_seat_identity_and_transaction_binding(self):
        with self.assertRaisesRegex(ValueError, "another seat"):
            self.verify(seat="OTHER")
        with self.assertRaisesRegex(ValueError, "another identity"):
            self.verify(identity="mallory")
        with self.assertRaisesRegex(ValueError, "another transaction"):
            self.verify(policy_after="3" * 64)

    def test_audit_binding_required_and_unique(self):
        with self.assertRaisesRegex(ValueError, "not bound"):
            self.verify(audit_rows=[])
        with self.assertRaisesRegex(ValueError, "not bound"):
            self.verify(audit_rows=self.rows + [_boot_row(self.rec)])
        with self.assertRaisesRegex(ValueError, "not bound"):
            self.verify(audit_rows=[_boot_row(self.rec, identity="mallory")])
        # A hashed identity stand-in binds exactly like the plain value.
        hashed = _boot_row(self.rec)
        del hashed["identity"]
        hashed["identity_sha256"] = hashlib.sha256("zcode".encode()).hexdigest()
        self.assertEqual(self.verify(audit_rows=[hashed])["seat"], "SWU-ZC")


class BootstrapProofRunTests(unittest.TestCase):
    def _proof(self, answers):
        proof = deploy_proof.BootstrapProof("bearer-tok")
        calls = list(answers)

        def call(action, args):
            answer = calls.pop(0)
            if isinstance(answer, type) and issubclass(answer, OSError):
                raise answer()
            return answer

        proof.call = call
        return proof

    def test_proven_requires_two_exact_refusals(self):
        denied = (401, {"error": deploy_proof.BOOTSTRAP_CAUSE})
        out = self._proof([denied, denied])("SWU-ZC", "zcode", "b" * 32, "1" * 64, "2" * 64)
        self.assertEqual(out["outcome"], deploy_proof.PROVEN)
        self.assertEqual(out["refusal"], deploy_proof.BOOTSTRAP_CAUSE)

    def test_a_successful_read_is_not_the_pre_activation_state(self):
        out = self._proof([(200, {"sha": "abc"})])("SWU-ZC", "zcode", "b" * 32, "1" * 64, "2" * 64)
        self.assertEqual(out["outcome"], deploy_proof.REFUSED)
        self.assertEqual(out["stage"], "read")

    def test_any_other_refusal_is_not_a_proof(self):
        denied = (401, {"error": deploy_proof.BOOTSTRAP_CAUSE})
        other = (401, {"error": "bearer_rejected"})
        out = self._proof([denied, other])("SWU-ZC", "zcode", "b" * 32, "1" * 64, "2" * 64)
        self.assertEqual(out["outcome"], deploy_proof.REFUSED)
        self.assertEqual(out["stage"], "probe")

    def test_network_loss_is_inconclusive(self):
        out = self._proof([OSError])("SWU-ZC", "zcode", "b" * 32, "1" * 64, "2" * 64)
        self.assertEqual(out["outcome"], deploy_proof.INCONCLUSIVE)


class _FakeVerifier:
    def __init__(self, identity="zcode"):
        self.identity = identity
        self.calls = []

    def parse_identity(self, token):
        return token.get("who")

    def resolve(self, token):
        self.calls.append(token["who"])
        return self.identity


def _policy(verifier):
    pol = SeatPolicy.__new__(SeatPolicy)
    pol._verifier = verifier
    pol._lock = threading.Lock()
    pol._bootstrap_calls = []
    pol._bootstrap_idents = {}
    return pol


class BootstrapResolveBoundTests(unittest.TestCase):
    def test_per_identity_bound(self):
        verifier = _FakeVerifier()
        pol = _policy(verifier)
        tok = {"who": "zcode"}
        self.assertEqual(pol.bootstrap_resolve(tok), "zcode")
        self.assertEqual(pol.bootstrap_resolve(tok), "zcode")
        self.assertIsNone(pol.bootstrap_resolve(tok))
        self.assertEqual(len(verifier.calls), 2)

    def test_global_bound_caps_distinct_identities(self):
        verifier = _FakeVerifier()
        pol = _policy(verifier)
        for i in range(6):
            pol.bootstrap_resolve({"who": f"id{i}"})
        self.assertIsNone(pol.bootstrap_resolve({"who": "id6"}))
        self.assertEqual(len(verifier.calls), 6)

    def test_identity_cardinality_bound(self):
        verifier = _FakeVerifier()
        pol = _policy(verifier)
        # The default cardinality (8) exceeds the global window budget (6),
        # so exercise cardinality with a lowered cap:  rotating names must
        # not multiply the budget, while a tracked identity keeps its own.
        original = policy_mod.BOOTSTRAP_VERIFY_IDENTITIES
        policy_mod.BOOTSTRAP_VERIFY_IDENTITIES = 3
        try:
            for i in range(3):
                self.assertIsNotNone(pol.bootstrap_resolve({"who": f"id{i}"}))
            self.assertIsNone(pol.bootstrap_resolve({"who": "id3"}))
            self.assertIsNotNone(pol.bootstrap_resolve({"who": "id2"}))
        finally:
            policy_mod.BOOTSTRAP_VERIFY_IDENTITIES = original
        self.assertEqual(len(verifier.calls), 4)

    def test_failed_verify_still_reserves(self):
        verifier = _FakeVerifier(identity=None)
        pol = _policy(verifier)
        tok = {"who": "zcode"}
        self.assertIsNone(pol.bootstrap_resolve(tok))
        self.assertIsNone(pol.bootstrap_resolve(tok))
        self.assertIsNone(pol.bootstrap_resolve(tok))
        self.assertEqual(len(verifier.calls), 2)

    def test_unparseable_bearer_never_reaches_verifier(self):
        verifier = _FakeVerifier()
        pol = _policy(verifier)
        self.assertIsNone(pol.bootstrap_resolve({"nope": 1}))
        self.assertEqual(verifier.calls, [])


class BootstrapDenialAuditorTests(unittest.TestCase):
    def test_row_shape_and_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = audit_mod.AuditLog(os.path.join(tmp, "audit.log"))
            auditor = audit_mod.BootstrapDenialAuditor(limit=2)
            ok = auditor.record(
                log, "wf_file_patch", "Simple-With-Us/AI-Fleet-Coordinator", "zcode",
                "deploy-proof/" + "b" * 32 + ".probe",
            )
            self.assertTrue(ok)
            self.assertTrue(auditor.record(log, "wf_repo_get", "Simple-With-Us/AI-Fleet-Coordinator", "zcode", None))
            self.assertFalse(auditor.record(log, "wf_repo_get", "x", "zcode", None))
            with open(log._path, encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh]
            self.assertEqual(len(rows), 2)
            row = rows[0]
            self.assertEqual(row["cause"], "seat_not_in_policy")
            self.assertEqual(row["identity"], "zcode")
            self.assertEqual(row["action"], "wf_file_patch")
            self.assertEqual(row["result"], "unauthenticated")
            self.assertEqual(row["seat"], "unknown")
            self.assertTrue(row["path"].startswith("deploy-proof/"))
            self.assertNotIn("path", rows[1])


if __name__ == "__main__":
    unittest.main()
