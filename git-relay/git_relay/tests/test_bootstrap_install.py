"""install-seat --bootstrap-receipt: the full gate chain with a real signed
bootstrap receipt, a real replay store and a real audit log file.

Mirrors test_install_seat.py's synthetic unit; systemctl and the health check
are stubbed, nothing touches a runtime. The receipt is a real production
sign_receipt envelope over a BootstrapProof record bound to the exact
before/after digests and to the seat row's configured identity.
"""

import importlib.util
import json
import os
import sys
import tempfile
import threading
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
# Synthetic fixture repository only: no real organization names in fixtures.
REPO = "example-org/example-repo"

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


class BootstrapInstallTests(unittest.TestCase):
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
        os.chmod(self.key_path, 0o600)
        self.relay_dir = root / "relay"
        self.relay_dir.mkdir()
        self.before = sp.canonical_digest(self.policy)
        self.after = sp.canonical_digest({**self.policy, "swu-zc": dict(NEW_ROW)})
        self.record = {
            "v": deploy_proof.BootstrapProof.VERSION,
            "seat": "swu-zc",
            "identity": "ZC",
            "proof_id": "proof-b1",
            "ts": int(time.time()),
            "url": deploy_proof.BootstrapProof.URL,
            "repo": REPO,
            "probe_path": probe_path("proof-b1"),
            "policy_before": self.before,
            "policy_after": self.after,
            "outcome": deploy_proof.PROVEN,
            "refusal": deploy_proof.BOOTSTRAP_CAUSE,
        }
        self.receipt_path = root / "receipt.json"
        self.receipt_path.write_text(json.dumps(sign_receipt(KEY, self.record)))
        self.audit_path = root / "relay" / sp.DEFAULT_AUDIT
        self._write_audit_row(identity="ZC")
        self.unit = sp.Unit(
            name="git-relay.service",
            relay=str(self.relay_dir),
            config=str(self.config),
            apps_token="x",
            environment={"GIT_RELAY_HOST": "127.0.0.1", "GIT_RELAY_PORT": "8471"},
        )
        self.env = mock.patch.dict(os.environ, {sp.PROOF_KEY_FILE_VAR: str(self.key_path)})
        self.env.start()
        # Patch once for the whole test: per-call patching races when two
        # installer threads nest their patches and leaks the fixture repo.
        self.repo_patch = mock.patch.object(deploy_proof.BootstrapProof, "REPO", REPO)
        self.repo_patch.start()
        self.addCleanup(self.repo_patch.stop)

    def _write_audit_row(self, identity="ZC"):
        self.audit_path.write_text(
            json.dumps(
                {
                    "cause": deploy_proof.BOOTSTRAP_CAUSE,
                    "identity": identity,
                    "action": "wf_file_patch",
                    "repo": REPO,
                    "result": "unauthenticated",
                    "path": probe_path("proof-b1"),
                    "seat": "unknown",
                    "ts": "2026-11-10T01:00:00Z",
                }
            )
            + "\n"
        )

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def install(self, args, unit):
        return sp.install_seat(args, unit)

    def args2(self, **over):
        base = {
            "seat": "swu-zc",
            "row_json": str(self.row_path),
            "expect_before": self.before,
            "expect_after": self.after,
            "receipt": None,
            "bootstrap_receipt": str(self.receipt_path),
            "apply": False,
            "wait": 1,
        }
        base.update(over)
        return SimpleNamespace(**base)

    def test_dry_run_checks_every_gate_without_writing_or_consuming(self):
        rc = self.install(self.args2(), self.unit)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(self.seats_path.read_text()), self.policy)
        self.assertFalse(ReplayStore(str(self.relay_dir / sp.REPLAY_STORE)).seen("proof-b1"))

    def test_apply_installs_and_consumes_the_bootstrap_receipt(self):
        fake = _FakeSubprocess()
        with (
            mock.patch.object(sp, "subprocess", fake),
            mock.patch.object(sp, "_healthy", lambda *a: True),
        ):
            rc = self.install(self.args2(apply=True), self.unit)
        self.assertEqual(rc, 0)
        installed = json.loads(self.seats_path.read_text())
        self.assertEqual(sp.canonical_digest(installed), self.after)
        self.assertEqual(list(installed), ["swu-old", "swu-zc"])
        self.assertTrue(ReplayStore(str(self.relay_dir / sp.REPLAY_STORE)).seen("proof-b1"))
        self.assertIn(["systemctl", "restart", "git-relay.service"], fake.calls)

    def test_a_replayed_bootstrap_receipt_is_refused_before_any_write(self):
        store = ReplayStore(str(self.relay_dir / sp.REPLAY_STORE))
        store.add(self.record)
        rc = self.install(self.args2(), self.unit)
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(self.seats_path.read_text()), self.policy)

    def test_a_row_without_the_proven_identity_is_refused(self):
        other_row = dict(NEW_ROW, identity="OTHER")
        self.row_path.write_text(json.dumps(other_row))
        after = sp.canonical_digest({"swu-old": dict(OLD_ROW), "swu-zc": other_row})
        rc = self.install(self.args2(expect_after=after), self.unit)
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(self.seats_path.read_text()), self.policy)

    def test_an_audit_row_for_another_confirmed_identity_is_refused(self):
        self._write_audit_row(identity="MALLORY")
        rc = self.install(self.args2(), self.unit)
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(self.seats_path.read_text()), self.policy)

    def test_a_missing_audit_row_is_refused(self):
        self.audit_path.write_text("")
        rc = self.install(self.args2(), self.unit)
        self.assertEqual(rc, 2)

    def test_a_row_without_identity_is_refused_before_verification(self):
        row = {k: v for k, v in NEW_ROW.items() if k != "identity"}
        self.row_path.write_text(json.dumps(row))
        after = sp.canonical_digest({"swu-old": dict(OLD_ROW), "swu-zc": row})
        rc = self.install(self.args2(expect_after=after), self.unit)
        self.assertEqual(rc, 2)
        self.assertFalse(ReplayStore(str(self.relay_dir / sp.REPLAY_STORE)).seen("proof-b1"))

    def test_concurrent_installers_cannot_replay_the_same_receipt(self):
        # Replay safety lives in the install-seat transaction lock plus the
        # durable store: two concurrent installers serialize on the lock and
        # the second one either waits out the winner or hits the consumed
        # receipt. Exactly one may succeed.
        fake = _FakeSubprocess()
        results = []
        barrier = threading.Barrier(2)

        def run():
            barrier.wait(timeout=10)
            results.append(self.install(self.args2(apply=True), self.unit))

        with (
            mock.patch.object(sp, "subprocess", fake),
            mock.patch.object(sp, "_healthy", lambda *a: True),
            mock.patch.object(sp, "TRANSACTION_LOCK_WAIT_S", 5),
        ):
            threads = [threading.Thread(target=run) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
        self.assertEqual(sorted(results), [0, 2])
        self.assertTrue(ReplayStore(str(self.relay_dir / sp.REPLAY_STORE)).seen("proof-b1"))

    def test_external_receipt_and_bootstrap_receipt_are_not_interchangeable(self):
        record = dict(self.record, v=2)
        del record["identity"]
        self.receipt_path.write_text(json.dumps(sign_receipt(KEY, record)))
        rc = self.install(self.args2(), self.unit)
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(self.seats_path.read_text()), self.policy)


class ProofKeyCustodyTests(unittest.TestCase):
    """The proof-key loader's custody refusals;  fixtures only, no real keys."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.key = self.root / "k"
        self.key.write_bytes(b"k" * 32)
        os.chmod(self.key, 0o600)

    def test_happy_path_reads_the_key(self):
        self.assertEqual(sp._load_proof_key(str(self.key)), b"k" * 32)

    def test_symlink_key_refused(self):
        link = self.root / "link"
        os.symlink(self.key, link)
        with self.assertRaisesRegex(ValueError, "symlink"):
            sp._load_proof_key(str(link))

    def test_group_or_world_writable_directory_refused(self):
        os.chmod(self.root, 0o777)
        try:
            with self.assertRaisesRegex(ValueError, "group/world-writable"):
                sp._load_proof_key(str(self.key))
        finally:
            os.chmod(self.root, 0o700)

    def test_empty_key_refused(self):
        self.key.write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "empty"):
            sp._load_proof_key(str(self.key))

    def test_as_root_the_full_ancestor_chain_is_checked(self):
        # Fixture trees live under /var (a symlink on macOS) or a sticky
        # world-writable /tmp:  as root the require_root_owned-equivalent
        # chain walk must refuse either, proving the walk actually runs.
        with mock.patch.object(os, "geteuid", lambda: 0):
            with self.assertRaisesRegex(ValueError, "directory chain"):
                sp._load_proof_key(str(self.key))


if __name__ == "__main__":
    unittest.main()
