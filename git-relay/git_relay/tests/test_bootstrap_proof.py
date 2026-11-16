"""First-seat bootstrap proof: denial binding, receipt gates, probe bounds.

The receipt tests sign with the PRODUCTION signer (deploy_proof.sign_receipt)
and the chain tests run the real Relay.handle -> SeatPolicy.bootstrap_resolve
-> BootstrapDenialAuditor -> on-disk AuditLog -> read_audit_rows path, so the
bound row is the one the server actually writes, never a stand-in.
"""

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

KEY = b"test-only bootstrap proof key"
# Synthetic fixture repository only: no real organization names in fixtures.
REPO = "example-org/example-repo"
NOW = int(time.time())

OLD_ROW = {
    "actions": ["open_pr"],
    "app_slug": "swu-old",
    "auth": "shared",
    "identity": "OLD",
    "mint_permissions": {"pull_requests": "write"},
    "repos": [REPO],
}


def _hmac(record, key=KEY):
    return hmac.new(key, deploy_proof._canonical(record), hashlib.sha256).hexdigest()


def _record(**over):
    rec = {
        "v": deploy_proof.BootstrapProof.VERSION,
        "seat": "swu-zc",
        "identity": "ZC",
        "proof_id": "b" * 32,
        "ts": NOW,
        "url": deploy_proof.BootstrapProof.URL,
        "repo": REPO,
        "outcome": deploy_proof.PROVEN,
        "refusal": deploy_proof.BOOTSTRAP_CAUSE,
        "policy_before": "1" * 64,
        "policy_after": "2" * 64,
    }
    rec.update(over)
    if "probe_path" not in over:
        rec["probe_path"] = deploy_proof.probe_path(rec["proof_id"])
    return rec


def _boot_row(record, identity="ZC", **over):
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
            seat="swu-zc",
            identity="ZC",
            now=NOW,
            store=self.store,
            policy_before="1" * 64,
            policy_after="2" * 64,
            audit_rows=self.rows,
            repo=REPO,
        )
        args.update(kw)
        if receipt is None:
            receipt = deploy_proof.sign_receipt(KEY, self.rec)
        return deploy_proof.verify_bootstrap_receipt(KEY, receipt, **args)

    def test_production_signed_receipt_verifies_and_consumes_replay(self):
        record = self.verify()
        self.assertEqual(record["proof_id"], "b" * 32)
        self.assertTrue(self.store.seen("b" * 32))
        with self.assertRaisesRegex(ValueError, "replayed"):
            self.verify()

    def test_dry_run_does_not_consume(self):
        self.verify(store_id=False)
        self.verify(store_id=False)

    def test_envelope_is_exactly_the_production_signer_shape(self):
        with self.assertRaisesRegex(ValueError, "receipt shape"):
            self.verify(receipt={"record": self.rec, "mac": _hmac(self.rec)})
        with self.assertRaisesRegex(ValueError, "receipt shape"):
            self.verify(receipt={"record": self.rec, "hmac": _hmac(self.rec), "extra": 1})

    def test_forged_or_tampered_hmac_refused(self):
        with self.assertRaisesRegex(ValueError, "forged"):
            self.verify(receipt={"record": self.rec, "hmac": _hmac(self.rec, key=b"x" * 32)})
        tampered = deploy_proof.sign_receipt(KEY, self.rec)
        tampered["record"] = dict(self.rec, seat="OTHER")
        with self.assertRaisesRegex(ValueError, "forged"):
            self.verify(receipt=tampered)

    def test_outcome_and_target(self):
        rec = _record(outcome=deploy_proof.INCONCLUSIVE)
        with self.assertRaisesRegex(ValueError, "not a proof"):
            self.verify(receipt={"record": rec, "hmac": _hmac(rec)})
        with self.assertRaisesRegex(ValueError, "another proof target"):
            self.verify(receipt=deploy_proof.sign_receipt(KEY, _record(v=2)))
        with self.assertRaisesRegex(ValueError, "another proof target"):
            self.verify(
                receipt=deploy_proof.sign_receipt(KEY, _record(url="https://x.example/v1/"))
            )

    def test_a_misdescribed_refusal_is_not_a_proof(self):
        # A signed PROVEN record that misdescribes the pre-activation denial
        # must not bind:  the refusal cause is part of what the proof proves.
        rec = _record(refusal="bearer_rejected")
        with self.assertRaisesRegex(ValueError, "not a proof"):
            self.verify(receipt={"record": rec, "hmac": _hmac(rec)})

    def test_freshness(self):
        with self.assertRaisesRegex(ValueError, "stale"):
            self.verify(receipt=deploy_proof.sign_receipt(KEY, _record(ts=NOW - 601)))
        with self.assertRaisesRegex(ValueError, "stale"):
            self.verify(receipt=deploy_proof.sign_receipt(KEY, _record(ts=NOW + 1)))

    def test_seat_identity_and_transaction_binding(self):
        with self.assertRaisesRegex(ValueError, "another seat"):
            self.verify(seat="OTHER")
        with self.assertRaisesRegex(ValueError, "another identity"):
            self.verify(identity="MALLORY")
        with self.assertRaisesRegex(ValueError, "another transaction"):
            self.verify(policy_after="3" * 64)

    def test_audit_binding_required_and_unique(self):
        with self.assertRaisesRegex(ValueError, "not bound"):
            self.verify(audit_rows=[])
        with self.assertRaisesRegex(ValueError, "not bound"):
            self.verify(audit_rows=self.rows + [_boot_row(self.rec)])
        with self.assertRaisesRegex(ValueError, "not bound"):
            self.verify(audit_rows=[_boot_row(self.rec, identity="MALLORY")])
        # A hashed identity stand-in binds exactly like the plain value.
        hashed = _boot_row(self.rec)
        del hashed["identity"]
        hashed["identity_sha256"] = hashlib.sha256(b"ZC").hexdigest()
        self.assertEqual(self.verify(audit_rows=[hashed])["seat"], "swu-zc")


class BootstrapProofRunTests(unittest.TestCase):
    def _proof(self, answers):
        proof = deploy_proof.BootstrapProof("bearer-tok")
        calls = list(answers)
        proof.seen_args = []

        def call(action, args):
            proof.seen_args.append((action, args))
            answer = calls.pop(0)
            if isinstance(answer, type) and issubclass(answer, OSError):
                raise answer()
            return answer

        proof.call = call
        return proof

    def test_proven_requires_two_exact_refusals(self):
        denied = (401, {"error": deploy_proof.BOOTSTRAP_CAUSE, "bootstrap_admission": "operator"})
        out = self._proof([denied, denied])("swu-zc", "ZC", "b" * 32, "1" * 64, "2" * 64)
        self.assertEqual(out["outcome"], deploy_proof.PROVEN)
        self.assertEqual(out["refusal"], deploy_proof.BOOTSTRAP_CAUSE)

    def test_probe_request_is_byte_identical_to_external_proof(self):
        denied = (401, {"error": deploy_proof.BOOTSTRAP_CAUSE, "bootstrap_admission": "operator"})
        boot = self._proof([denied, denied])
        boot("swu-zc", "ZC", "b" * 32, "1" * 64, "2" * 64)
        ext = deploy_proof.ExternalProof("bearer-tok")
        captured = []

        def ext_call(action, args):
            captured.append((action, args))
            if action == "wf_repo_get":
                return 200, {"repository": {"full_name": ext.REPO, "default_branch": "main"}}
            return 400, {"error": "default branch is not writable"}

        ext.call = ext_call
        out = ext("swu-zc", "b" * 32, "1" * 64, "2" * 64)
        self.assertEqual(out["outcome"], deploy_proof.PROVEN)
        # Same proof id: the two negative probes must be the same request.
        ext_probe = dict(captured[1][1])
        boot_probe = dict(boot.seen_args[1][1])
        self.assertEqual(captured[1][0], "wf_file_patch")
        self.assertEqual(boot.seen_args[1][0], "wf_file_patch")
        self.assertEqual(boot_probe, ext_probe)
        self.assertEqual(boot_probe["expected_head_sha"], deploy_proof.NULL_OID)
        self.assertEqual(boot_probe["base_blob_sha"], deploy_proof.NULL_OID)
        self.assertEqual(boot_probe["branch"], "main")
        self.assertEqual(boot_probe["path"], deploy_proof.probe_path("b" * 32))

    def test_a_successful_read_is_not_the_pre_activation_state(self):
        out = self._proof([(200, {"sha": "abc"})])("swu-zc", "ZC", "b" * 32, "1" * 64, "2" * 64)
        self.assertEqual(out["outcome"], deploy_proof.REFUSED)
        self.assertEqual(out["stage"], "read")

    def test_any_other_refusal_is_not_a_proof(self):
        denied = (401, {"error": deploy_proof.BOOTSTRAP_CAUSE, "bootstrap_admission": "operator"})
        other = (401, {"error": "bearer_rejected"})
        out = self._proof([denied, other])("swu-zc", "ZC", "b" * 32, "1" * 64, "2" * 64)
        self.assertEqual(out["outcome"], deploy_proof.REFUSED)
        self.assertEqual(out["stage"], "probe")

    def test_network_loss_is_inconclusive(self):
        out = self._proof([OSError])("swu-zc", "ZC", "b" * 32, "1" * 64, "2" * 64)
        self.assertEqual(out["outcome"], deploy_proof.INCONCLUSIVE)


class _FakeVerifier:
    def __init__(self, identity="ZC"):
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
        self.assertEqual(pol.bootstrap_resolve(tok), "ZC")
        self.assertEqual(pol.bootstrap_resolve(tok), "ZC")
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
        # so exercise cardinality with a lowered cap: rotating names must
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
                log,
                "wf_file_patch",
                REPO,
                "ZC",
                "deploy-proof/" + "b" * 32 + ".probe",
            )
            self.assertTrue(ok)
            self.assertTrue(auditor.record(log, "wf_repo_get", REPO, "ZC", None))
            self.assertFalse(auditor.record(log, "wf_repo_get", "x", "ZC", None))
            with open(log._path, encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh]
            self.assertEqual(len(rows), 2)
            row = rows[0]
            self.assertEqual(row["cause"], "seat_not_in_policy")
            self.assertEqual(row["identity"], "ZC")
            self.assertEqual(row["action"], "wf_file_patch")
            self.assertEqual(row["result"], "unauthenticated")
            self.assertEqual(row["seat"], "unknown")
            self.assertTrue(row["path"].startswith("deploy-proof/"))
            self.assertNotIn("path", rows[1])


class BootstrapRelayChainTests(unittest.TestCase):
    """The live refusal chain, end to end: Relay.handle authenticates with the
    real SeatPolicy, resolves the identity under the bootstrap bounds, audits
    the denial through the real BootstrapDenialAuditor onto a real on-disk
    AuditLog, and the rows read back from that file bind a production-signed
    bootstrap receipt."""

    def _server(self, tmp, identity="ZC"):
        from git_relay.audit import AuditLog
        from git_relay.server import Relay

        class Verifier:
            def parse_identity(self, token):
                return identity if token == "bearer-zc" else None

            def resolve(self, token):
                return identity

        policy = SeatPolicy(json.dumps({"swu-old": dict(OLD_ROW)}), verifier=Verifier())
        log = AuditLog(os.path.join(tmp, "audit.jsonl"))
        return Relay(policy, object(), log), log

    def _probe(self, proof_id):
        return {
            "repo": REPO,
            "branch": "main",
            "path": deploy_proof.probe_path(proof_id),
            "message": "Bounded external negative probe %s: default branch must refuse" % proof_id,
            "expected_head_sha": deploy_proof.NULL_OID,
            "base_blob_sha": deploy_proof.NULL_OID,
            "edits": [{"start": 0, "end": 0, "text": ""}],
        }

    def test_handle_denial_binds_a_production_signed_bootstrap_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            server, log = self._server(tmp)
            proof_id = "c" * 32
            status, body = server.handle(
                "bearer-zc", "wf_repo_get", {"repo": REPO}, operator_bootstrap=True
            )
            self.assertEqual(
                (status, body),
                (401, {"error": "seat_not_in_policy", "bootstrap_admission": "operator"}),
            )
            status, body = server.handle(
                "bearer-zc", "wf_file_patch", self._probe(proof_id), operator_bootstrap=True
            )
            self.assertEqual(
                (status, body),
                (401, {"error": "seat_not_in_policy", "bootstrap_admission": "operator"}),
            )

            rows = deploy_proof.read_audit_rows(log._path)
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]["cause"], "seat_not_in_policy")
            self.assertEqual(rows[1]["identity"], "ZC")
            self.assertEqual(rows[1]["result"], "unauthenticated")
            self.assertEqual(rows[1]["path"], deploy_proof.probe_path(proof_id))
            self.assertNotIn("path", rows[0])

            receipt = deploy_proof.sign_receipt(KEY, _record(proof_id=proof_id))
            store = deploy_proof.ReplayStore(os.path.join(tmp, "store.json"))
            record = deploy_proof.verify_bootstrap_receipt(
                KEY,
                receipt,
                seat="swu-zc",
                identity="ZC",
                now=NOW,
                store=store,
                policy_before="1" * 64,
                policy_after="2" * 64,
                audit_rows=rows,
                repo=REPO,
            )
            self.assertEqual(record["proof_id"], proof_id)

    def test_non_bindable_actions_never_spend_bootstrap_budgets(self):
        from git_relay.typed_gateway import ACTIONS

        # Only wf_repo_get / wf_file_patch denials can bind a receipt, so only
        # they may spend the verifier resolve and bootstrap-row budgets;  a
        # spray of other actions with an unclaimed bearer gets the generic
        # bounded spray row and leaves enrollment capacity intact.
        for action in ("open_pr", "merge_pr", "comment_pr"):
            self.assertNotIn(action, ACTIONS)
        with tempfile.TemporaryDirectory() as tmp:
            server, log = self._server(tmp)
            for action in ("open_pr", "merge_pr", "comment_pr"):
                status, body = server.handle(
                    "bearer-zc", action, {"repo": REPO}, operator_bootstrap=True
                )
                self.assertEqual((status, body), (401, {"error": "seat_not_in_policy"}))
            rows = deploy_proof.read_audit_rows(log._path)
            self.assertEqual(len(rows), 3)
            for row in rows:
                self.assertEqual(row["cause"], "seat_not_in_policy")
                self.assertNotIn("identity", row)
            status, _body = server.handle(
                "bearer-zc", "wf_repo_get", {"repo": REPO}, operator_bootstrap=True
            )
            self.assertEqual(status, 401)
            rows = deploy_proof.read_audit_rows(log._path)
            self.assertEqual(rows[-1]["identity"], "ZC")

    def test_a_row_for_another_confirmed_identity_does_not_bind(self):
        with tempfile.TemporaryDirectory() as tmp:
            server, log = self._server(tmp, identity="MALLORY")
            proof_id = "d" * 32
            status, _body = server.handle(
                "bearer-zc", "wf_file_patch", self._probe(proof_id), operator_bootstrap=True
            )
            self.assertEqual(status, 401)
            rows = deploy_proof.read_audit_rows(log._path)
            receipt = deploy_proof.sign_receipt(KEY, _record(proof_id=proof_id))
            with self.assertRaisesRegex(ValueError, "not bound"):
                deploy_proof.verify_bootstrap_receipt(
                    KEY,
                    receipt,
                    seat="swu-zc",
                    identity="ZC",
                    now=NOW,
                    store=deploy_proof.ReplayStore(os.path.join(tmp, "store.json")),
                    policy_before="1" * 64,
                    policy_after="2" * 64,
                    audit_rows=rows,
                    repo=REPO,
                )

    def test_an_unverifiable_bearer_gets_no_bootstrap_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            server, log = self._server(tmp)
            status, body = server.handle(
                "bearer-nope", "wf_repo_get", {"repo": REPO}, operator_bootstrap=True
            )
            self.assertEqual(status, 401)
            rows = deploy_proof.read_audit_rows(log._path)
            self.assertTrue(
                all(r.get("cause") != "seat_not_in_policy" or "identity" not in r for r in rows)
            )

    def test_over_audit_budget_probes_leave_no_bindable_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            server, log = self._server(tmp)
            server._bootstrap_auditor = audit_mod.BootstrapDenialAuditor(limit=1)
            first, second = "e" * 32, "f" * 32
            status, _ = server.handle(
                "bearer-zc", "wf_file_patch", self._probe(first), operator_bootstrap=True
            )
            self.assertEqual(status, 401)
            status, _ = server.handle(
                "bearer-zc", "wf_file_patch", self._probe(second), operator_bootstrap=True
            )
            self.assertEqual(status, 401)
            rows = deploy_proof.read_audit_rows(log._path)
            self.assertEqual(len(rows), 1)
            with self.assertRaisesRegex(ValueError, "not bound"):
                deploy_proof.verify_bootstrap_receipt(
                    KEY,
                    deploy_proof.sign_receipt(KEY, _record(proof_id=second)),
                    seat="swu-zc",
                    identity="ZC",
                    now=NOW,
                    store=deploy_proof.ReplayStore(os.path.join(tmp, "store.json")),
                    policy_before="1" * 64,
                    policy_after="2" * 64,
                    audit_rows=rows,
                    repo=REPO,
                )

    def test_resolve_over_budget_writes_the_ordinary_bounded_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            server, log = self._server(tmp)
            for _ in range(3):
                status, _ = server.handle(
                    "bearer-zc", "wf_repo_get", {"repo": REPO}, operator_bootstrap=True
                )
                self.assertEqual(status, 401)
            rows = deploy_proof.read_audit_rows(log._path)
            self.assertEqual(len(rows), 3)
            self.assertIn("identity", rows[0])
            # The third call is over the per-identity bound: the ordinary
            # bounded spray row, with the cause but NO confirmed identity.
            self.assertEqual(rows[2]["cause"], "seat_not_in_policy")
            self.assertNotIn("identity", rows[2])


if __name__ == "__main__":
    unittest.main()


class ListenerCapabilitySeparationTests(unittest.TestCase):
    """Bootstrap admission is a property of the LISTENER the request arrives
    on, never of the peer address:  the tunnel dials the public listener
    from 127.0.0.1 exactly like an operator would, so remote_addr cannot be
    the operator boundary.  The public listener never reaches the verifier
    or spends the bootstrap budgets;  the operator listener does."""

    def _relay(self, tmp):
        from git_relay.audit import AuditLog
        from git_relay.server import Relay

        calls = []

        class Verifier:
            def parse_identity(self, token):
                return "ZC" if token == "bearer-zc" else None

            def resolve(self, token):
                calls.append(token)
                return "ZC"

        policy = SeatPolicy(json.dumps({"swu-old": dict(OLD_ROW)}), verifier=Verifier())
        log = AuditLog(os.path.join(tmp, "audit.jsonl"))
        return Relay(policy, object(), log), log, calls

    def test_public_listener_never_reaches_the_verifier(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay, log, calls = self._relay(tmp)
            status, _body = relay.handle(
                "bearer-zc", "wf_repo_get", {"repo": REPO}, operator_bootstrap=False
            )
            self.assertEqual(status, 401)
            self.assertEqual(calls, [])
            with open(log._path, encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh]
            # The generic bounded unauth row only:  no bootstrap identity row,
            # no auditor spend, and nothing named after the bearer's claim.
            self.assertFalse(any(r.get("identity") == "ZC" for r in rows))
            self.assertFalse(any(r.get("cause") == "bootstrap_denied" for r in rows))

    def test_operator_listener_resolves_and_audits(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay, log, calls = self._relay(tmp)
            status, _body = relay.handle(
                "bearer-zc", "wf_repo_get", {"repo": REPO}, operator_bootstrap=True
            )
            self.assertEqual(status, 401)
            self.assertEqual(calls, ["bearer-zc"])
            with open(log._path, encoding="utf-8") as fh:
                rows = [json.loads(line) for line in fh]
            self.assertTrue(
                any(
                    r.get("identity") == "ZC" and r.get("cause") == "seat_not_in_policy"
                    for r in rows
                )
            )

    def test_over_budget_operator_denial_carries_no_marker(self):
        # The marker asserts installable audit evidence exists:  once the
        # per-identity budget is spent, the denial is still refused but no
        # longer claims admission, so the proof cannot go PROVEN on an
        # uninstallable run.
        with tempfile.TemporaryDirectory() as tmp:
            relay, log, calls = self._relay(tmp)
            for _ in range(2):
                status, body = relay.handle(
                    "bearer-zc", "wf_repo_get", {"repo": REPO}, operator_bootstrap=True
                )
                self.assertEqual(status, 401)
                self.assertEqual(body.get("bootstrap_admission"), "operator")
            status, body = relay.handle(
                "bearer-zc", "wf_repo_get", {"repo": REPO}, operator_bootstrap=True
            )
            self.assertEqual(status, 401)
            self.assertNotIn("bootstrap_admission", body)

    def test_audit_io_failure_carries_no_marker(self):
        # An unwritable audit log must not crash the handler nor emit the
        # admission marker:  the refusal stands, the proof will REFUSE.
        from git_relay.server import Relay

        class Verifier:
            def parse_identity(self, token):
                return "ZC" if token == "bearer-zc" else None

            def resolve(self, token):
                return "ZC"

        policy = SeatPolicy(json.dumps({"swu-old": dict(OLD_ROW)}), verifier=Verifier())
        log = audit_mod.AuditLog("/nonexistent-dir-9x/audit.jsonl")
        relay = Relay(policy, object(), log)
        status, body = relay.handle(
            "bearer-zc", "wf_repo_get", {"repo": REPO}, operator_bootstrap=True
        )
        self.assertEqual(status, 401)
        self.assertNotIn("bootstrap_admission", body)

    def test_http_public_listener_cannot_reach_bootstrap_even_from_loopback(self):
        # cloudflared dials the public listener from 127.0.0.1:  the peer
        # address is identical to an operator's, so only the listener the
        # request ARRIVED on may decide bootstrap admission.
        import http.client
        import threading

        from git_relay.server import BoundedThreadingHTTPServer, make_handler

        with tempfile.TemporaryDirectory() as tmp:
            relay, log, calls = self._relay(tmp)
            public = BoundedThreadingHTTPServer(("127.0.0.1", 0), make_handler(relay))
            operator = BoundedThreadingHTTPServer(
                ("127.0.0.1", 0), make_handler(relay, operator_bootstrap=True)
            )
            for srv in (public, operator):
                threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                for srv, expected_calls in ((public, 0), (operator, 1)):
                    conn = http.client.HTTPConnection(
                        "127.0.0.1", srv.server_address[1], timeout=10
                    )
                    conn.request(
                        "POST",
                        "/v1/wf_repo_get",
                        body=json.dumps({"repo": REPO}),
                        headers={
                            "Authorization": "Bearer bearer-zc",
                            "Content-Type": "application/json",
                        },
                    )
                    resp = conn.getresponse()
                    resp.read()
                    conn.close()
                    self.assertEqual(resp.status, 401)
                    self.assertEqual(len(calls), expected_calls)
            finally:
                for srv in (public, operator):
                    srv.shutdown()
                    srv.server_close()


class OperatorBootstrapEndToEndTests(unittest.TestCase):
    """The real HTTP flow:  the public listener refuses the proof run (no
    operator-admission marker), the operator listener proves it, and the
    signed receipt verifies against the run's own audit rows with the exact
    URL binding both edges share."""

    def _relay(self, tmp):
        from git_relay.audit import AuditLog
        from git_relay.server import Relay

        class Verifier:
            def parse_identity(self, token):
                return "ZC" if token == "bearer-zc" else None

            def resolve(self, token):
                return "ZC"

        policy = SeatPolicy(json.dumps({"swu-old": dict(OLD_ROW)}), verifier=Verifier())
        log = AuditLog(os.path.join(tmp, "audit.jsonl"))
        return Relay(policy, object(), log), log

    def _serve(self, relay, operator):
        import threading

        from git_relay.server import BoundedThreadingHTTPServer, make_handler

        srv = BoundedThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(relay, operator_bootstrap=operator)
        )
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv, "http://127.0.0.1:%d/v1/" % srv.server_address[1]

    def test_public_listener_proof_run_refuses_without_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay, log = self._relay(tmp)
            _srv, url = self._serve(relay, operator=False)
            proof = deploy_proof.BootstrapProof("bearer-zc", url=url)
            record = proof("swu-zc", "ZC", "d" * 32, "1" * 64, "2" * 64)
            self.assertEqual(record["outcome"], deploy_proof.REFUSED)
            self.assertEqual(record["stage"], "read")
            self.assertEqual(record.get("reason"), "no_operator_admission")

    def test_operator_listener_proof_binds_receipt_and_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            relay, log = self._relay(tmp)
            _srv, url = self._serve(relay, operator=True)
            proof = deploy_proof.BootstrapProof("bearer-zc", url=url)
            proof_id = "e" * 32
            record = proof("swu-zc", "ZC", proof_id, "1" * 64, "2" * 64)
            self.assertEqual(record["outcome"], deploy_proof.PROVEN)
            self.assertEqual(record["url"], url)

            rows = deploy_proof.read_audit_rows(log._path)
            receipt = deploy_proof.sign_receipt(KEY, record)
            store = deploy_proof.ReplayStore(os.path.join(tmp, "store.json"))
            verified = deploy_proof.verify_bootstrap_receipt(
                KEY,
                receipt,
                seat="swu-zc",
                identity="ZC",
                now=int(time.time()),
                store=store,
                policy_before="1" * 64,
                policy_after="2" * 64,
                audit_rows=rows,
                url=url,
            )
            self.assertEqual(verified["proof_id"], proof_id)

            # The same receipt against any other URL string refuses:  proof
            # and installer bind the exact same validated endpoint.
            other = deploy_proof.ReplayStore(os.path.join(tmp, "other.json"))
            with self.assertRaises(ValueError):
                deploy_proof.verify_bootstrap_receipt(
                    KEY,
                    receipt,
                    seat="swu-zc",
                    identity="ZC",
                    now=int(time.time()),
                    store=other,
                    policy_before="1" * 64,
                    policy_after="2" * 64,
                    audit_rows=rows,
                    url="http://127.0.0.1:1/v1/",
                )

    def test_git_paths_refused_on_operator_listener(self):
        import http.client

        with tempfile.TemporaryDirectory() as tmp:
            relay, log = self._relay(tmp)
            srv, _url = self._serve(relay, operator=True)
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            conn.request("GET", "/example.git/info/refs?service=git-upload-pack")
            resp = conn.getresponse()
            resp.read()
            conn.close()
            self.assertEqual(resp.status, 404)

    def test_operator_url_validation(self):
        good = ("http://127.0.0.1:8472/v1/", "https://localhost:8472/v1/", "http://[::1]:8472/v1/")
        for url in good:
            self.assertEqual(deploy_proof.validate_operator_url(url), url)
        bad = (
            "ftp://127.0.0.1:8472/v1/",
            "http://example.com:8472/v1/",
            "http://user@127.0.0.1:8472/v1/",
            "http://127.0.0.1:8472/v1/?x=1",
            "http://127.0.0.1:8472/v1/#frag",
            "http://127.0.0.1/v1/",
            "http://127.0.0.1:8472/v1",
            "http://127.0.0.1:8472/api/v1/",
            "http://127.0.0.1:99999/v1/",
            "http://127.0.0.1:0/v1/",
            "http://127.0.0.1:8472/v1/?",
            "http://127.0.0.1:8472/v1/#",
            "http://127.0.0.1:8472/v" + chr(10) + "1/",
            "http://127.0.0.1:8472/" + chr(9) + "v1/",
            "",
            None,
        )
        for url in bad:
            with self.assertRaises(ValueError, msg=repr(url)):
                deploy_proof.validate_operator_url(url)
        with self.assertRaises(ValueError):
            deploy_proof.BootstrapProof("bearer-zc", url="http://example.com:8472/v1/")
