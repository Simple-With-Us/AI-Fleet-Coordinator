"""The additive external-file install transaction: gates, CAS, backup, recovery.

Every gate is exercised against a synthetic unit and seats file; systemctl and
the health check are stubbed, so nothing here touches a runtime. The receipt is
a real signed deploy_proof receipt bound to the exact before/after digests.
"""

import importlib.util
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from git_relay import deploy_proof
from git_relay.deploy_proof import ReplayStore, probe_path, sign_receipt

SPEC = importlib.util.spec_from_file_location(
    "deploy_seat_policy", Path(__file__).resolve().parents[2] / "deploy" / "seat_policy.py"
)
sp = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sp
SPEC.loader.exec_module(sp)

KEY = b"test-only install-seat proof key"
REPO = "Simple-With-Us/AI-Fleet-Coordinator"

OLD_ROW = {
    "actions": ["open_pr"],
    "app_slug": "swu-old",
    "auth": "shared",
    "identity": "OLD",
    "mint_permissions": {"pull_requests": "write"},
    "repos": [REPO],
}
NEW_ROW = {
    "actions": ["wf_repo_get", "wf_file_patch"],
    "app_slug": "swu-zc",
    "auth": "shared",
    "identity": "ZC",
    "mint_permissions": {"contents": "write"},
    "repos": [REPO],
}


class _FakeSubprocess:
    def __init__(self, rc=0):
        self.rc = rc
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append(argv)
        return SimpleNamespace(returncode=self.rc)


class InstallSeatTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        root = Path(self.dir.name)
        self.policy = {"swu-old": dict(OLD_ROW)}
        self.seats_path = root / "seats.json"
        self.seats_path.write_text(json.dumps(self.policy))
        self.config = root / "relay.env"
        self.config.write_text("GIT_RELAY_SEATS_FILE=%s\n" % self.seats_path)
        self.row_path = root / "row.json"
        self.row_path.write_text(json.dumps(NEW_ROW))
        self.key_path = root / "proof-key"
        self.key_path.write_bytes(KEY)
        self.relay_dir = root / "relay"
        self.relay_dir.mkdir()
        self.before = sp.canonical_digest(self.policy)
        self.after = sp.canonical_digest({**self.policy, "swu-zc": dict(NEW_ROW)})
        self.record = {
            "v": 2,
            "seat": "swu-zc",
            "proof_id": "proof-t1",
            "ts": int(time.time()),
            "url": deploy_proof.ExternalProof.URL,
            "repo": REPO,
            "probe_path": probe_path("proof-t1"),
            "policy_before": self.before,
            "policy_after": self.after,
            "outcome": deploy_proof.PROVEN,
            "refusal": "default branch is not writable",
        }
        self.receipt_path = root / "receipt.json"
        self.receipt_path.write_text(json.dumps(sign_receipt(KEY, self.record)))
        (root / "relay" / sp.DEFAULT_AUDIT).write_text(
            json.dumps(
                {
                    "reason": "default branch is not writable",
                    "branch": "main",
                    "path": probe_path("proof-t1"),
                    "seat": "swu-zc",
                    "action": "wf_file_patch",
                    "repo": REPO,
                    "result": "bad_request",
                    "ts": "2026-11-08T01:00:00Z",
                }
            )
            + "\n"
        )
        self.unit = sp.Unit(
            name="git-relay.service",
            relay=str(self.relay_dir),
            config=str(self.config),
            apps_token="x",
            environment={"GIT_RELAY_HOST": "127.0.0.1", "GIT_RELAY_PORT": "8471"},
        )
        self.env = mock.patch.dict(os.environ, {sp.PROOF_KEY_FILE_VAR: str(self.key_path)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def args2(self, **over):
        base = {
            "seat": "swu-zc",
            "row_json": str(self.row_path),
            "expect_before": self.before,
            "expect_after": self.after,
            "receipt": str(self.receipt_path),
            "apply": False,
            "wait": 1,
        }
        base.update(over)
        return SimpleNamespace(**base)

    def test_cas_refuses_a_live_policy_that_is_not_the_reviewed_one(self):
        rc = sp.install_seat(self.args2(expect_before="0" * 64), self.unit)
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(self.seats_path.read_text()), self.policy)

    def test_expect_after_mismatch_refuses(self):
        rc = sp.install_seat(self.args2(expect_after="0" * 64), self.unit)
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(self.seats_path.read_text()), self.policy)

    def test_proof_gate_stays_on_without_the_key_file(self):
        with mock.patch.dict(os.environ, {sp.PROOF_KEY_FILE_VAR: ""}):
            rc = sp.install_seat(self.args2(), self.unit)
        self.assertEqual(rc, 2)

    def test_dry_run_checks_every_gate_without_writing_or_consuming(self):
        rc = sp.install_seat(self.args2(), self.unit)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(self.seats_path.read_text()), self.policy)
        self.assertFalse(
            ReplayStore(str(self.relay_dir / sp.REPLAY_STORE)).seen("proof-t1")
        )

    def test_apply_installs_atomically_with_backup_and_consumes_the_receipt(self):
        fake = _FakeSubprocess()
        with mock.patch.object(sp, "subprocess", fake), mock.patch.object(
            sp, "_healthy", lambda *a: True
        ):
            rc = sp.install_seat(self.args2(apply=True), self.unit)
        self.assertEqual(rc, 0)
        installed = json.loads(self.seats_path.read_text())
        self.assertEqual(sp.canonical_digest(installed), self.after)
        self.assertEqual(list(installed), ["swu-old", "swu-zc"])
        backups = [p for p in self.seats_path.parent.glob("seats.json.backup-seat-add-*")]
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text()), self.policy)
        self.assertTrue(ReplayStore(str(self.relay_dir / sp.REPLAY_STORE)).seen("proof-t1"))
        self.assertIn(["systemctl", "restart", "git-relay.service"], fake.calls)

    def test_apply_with_an_unhealthy_restart_restores_the_previous_file(self):
        fake = _FakeSubprocess()
        with mock.patch.object(sp, "subprocess", fake), mock.patch.object(
            sp, "_healthy", lambda *a: False
        ):
            rc = sp.install_seat(self.args2(apply=True), self.unit)
        self.assertEqual(rc, 3)
        self.assertEqual(json.loads(self.seats_path.read_text()), self.policy)

    def test_a_replayed_receipt_is_refused_before_any_write(self):
        store = ReplayStore(str(self.relay_dir / sp.REPLAY_STORE))
        store.add(self.record)
        rc = sp.install_seat(self.args2(), self.unit)
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(self.seats_path.read_text()), self.policy)

    def test_a_receipt_for_another_transaction_is_refused(self):
        record = dict(self.record, policy_after="0" * 64)
        # Recompute the digests so the receipt is internally consistent but
        # names a DIFFERENT transaction than this one.
        other_after = sp.canonical_digest({"swu-old": dict(OLD_ROW), "swu-zc": dict(NEW_ROW, identity="OTHER")})
        record["policy_after"] = other_after
        self.receipt_path.write_text(json.dumps(sign_receipt(KEY, record)))
        rc = sp.install_seat(self.args2(), self.unit)
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
