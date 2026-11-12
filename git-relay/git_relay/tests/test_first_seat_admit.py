"""First-seat admission: provisional row, ExternalProof, rollback.

The receipt gate for a seat that can already prove itself is covered in
test_install_seat.py and is not relaxed here. Nothing in this file talks to
a live relay or holds a real bearer.
"""

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import pwd
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
    "deploy_seat_policy_admit", Path(__file__).resolve().parents[2] / "deploy" / "seat_policy.py"
)
sp = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = sp
SPEC.loader.exec_module(sp)

KEY = b"test-only install-seat proof key"
BEARER = "test-bearer-zc"
PROOF_ID = "proof-t1"
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
    def __init__(self):
        self.calls = []

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        return SimpleNamespace(returncode=0)


class FirstSeatAdmitTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.policy = {"swu-old": dict(OLD_ROW)}
        self.seats_path = self.root / "seats.json"
        self.seats_path.write_text(json.dumps(self.policy))
        self.config = self.root / "relay.env"
        self.config.write_text("GIT_RELAY_SEATS_FILE=%s\n" % self.seats_path)
        self.row_path = self.root / "row.json"
        self.row_path.write_text(json.dumps(NEW_ROW))
        self.key_path = self.root / "proof-key"
        self.key_path.write_bytes(KEY)
        os.chmod(self.key_path, 0o600)
        self.bearer_path = self.root / "bearer"
        self.bearer_path.write_text(BEARER + "\n")
        os.chmod(self.bearer_path, 0o600)
        self.relay_dir = self.root / "relay"
        self.relay_dir.mkdir()
        self.before = sp.canonical_digest(self.policy)
        self.after = sp.canonical_digest({**self.policy, "swu-zc": dict(NEW_ROW)})
        self.record = self.proven()
        self.receipt_path = self.root / "receipt.json"
        self.receipt_path.write_text(json.dumps(sign_receipt(KEY, self.record)))
        self.audit = self.relay_dir / sp.DEFAULT_AUDIT
        self.audit.write_text(json.dumps(self.audit_row()) + "\n")
        self.unit = sp.Unit(
            name="git-relay.service",
            relay=str(self.relay_dir),
            config=str(self.config),
            apps_token="x",
            environment={"GIT_RELAY_HOST": "127.0.0.1", "GIT_RELAY_PORT": "8471"},
        )
        self.env = mock.patch.dict(
            os.environ,
            {
                sp.PROOF_KEY_FILE_VAR: str(self.key_path),
                sp.FIRST_SEAT_BEARER_VAR: str(self.bearer_path),
            },
        )
        self.env.start()
        self.seen = {}

    def tearDown(self):
        self.env.stop()
        self.dir.cleanup()

    def proven(self, **over):
        record = {
            "v": deploy_proof.ExternalProof.VERSION,
            "seat": "swu-zc",
            "proof_id": PROOF_ID,
            "ts": int(time.time()),
            "url": deploy_proof.ExternalProof.URL,
            "repo": deploy_proof.ExternalProof.REPO,
            "probe_path": probe_path(PROOF_ID),
            "policy_before": self.before,
            "policy_after": self.after,
            "outcome": deploy_proof.PROVEN,
            "refusal": "default branch is not writable",
        }
        record.update(over)
        return record

    def audit_row(self):
        return {
            "reason": "default branch is not writable",
            "branch": "main",
            "path": probe_path(PROOF_ID),
            "seat": "swu-zc",
            "action": "wf_file_patch",
            "repo": deploy_proof.ExternalProof.REPO,
            "result": "bad_request",
            "ts": "2026-11-12T00:00:00Z",
        }

    def marker(self):
        return Path(str(self.seats_path) + sp.PROVISIONAL_SUFFIX)

    def live(self):
        return json.loads(self.seats_path.read_text())

    def store(self):
        return ReplayStore(str(self.relay_dir / sp.REPLAY_STORE))

    def first_args(self, **over):
        base = {
            "seat": "swu-zc",
            "row_json": str(self.row_path),
            "expect_before": self.before,
            "expect_after": self.after,
            "receipt": None,
            "bootstrap_receipt": None,
            "first_seat": True,
            "proof_id": PROOF_ID,
            "apply": True,
            "wait": 1,
        }
        base.update(over)
        return SimpleNamespace(**base)

    def receipt_args(self, **over):
        base = {
            "seat": "swu-zc",
            "row_json": str(self.row_path),
            "expect_before": self.before,
            "expect_after": self.after,
            "receipt": str(self.receipt_path),
            "bootstrap_receipt": None,
            "first_seat": False,
            "apply": False,
            "wait": 1,
        }
        base.update(over)
        return SimpleNamespace(**base)

    def install(self, args, proof):
        fake = _FakeSubprocess()
        out = io.StringIO()
        with (
            mock.patch.object(sp, "_external_proof", proof),
            mock.patch.object(sp, "subprocess", fake),
            mock.patch.object(sp, "_healthy", lambda *a: True),
            contextlib.redirect_stdout(out),
        ):
            rc = sp.install_seat(args, self.unit)
        return rc, out.getvalue(), fake

    def watching_proof(self, record):
        def proof(_deploy_proof, bearer, seat, proof_id, before, after):
            self.seen["bearer_matches"] = bearer == BEARER
            self.seen["seat"] = seat
            self.seen["digest"] = sp.canonical_digest(self.live())
            self.seen["marker_mode"] = self.marker().stat().st_mode & 0o777
            self.seen["marker_has_bearer"] = BEARER.encode() in self.marker().read_bytes()
            self.seen["called"] = True
            return record

        return proof

    def test_success_keeps_the_row_after_external_proof(self):
        rc, out, fake = self.install(self.first_args(), self.watching_proof(self.record))
        self.assertEqual(rc, 0)
        self.assertEqual(sp.canonical_digest(self.live()), self.after)
        self.assertEqual(list(self.live()), ["swu-old", "swu-zc"])
        self.assertEqual(self.seen["digest"], self.after)
        self.assertTrue(self.seen["bearer_matches"])
        self.assertFalse(self.seen["marker_has_bearer"])
        self.assertEqual(self.seen["marker_mode"], 0o600)
        self.assertFalse(self.marker().exists())
        self.assertTrue(self.store().seen(PROOF_ID))
        self.assertEqual(fake.calls, [["systemctl", "restart", "git-relay.service"]])
        self.assertIn("after ExternalProof", out)
        self.assertFalse(BEARER in out)

    def test_a_refused_proof_rolls_back_to_the_prior_digest(self):
        refused = self.proven(outcome=deploy_proof.REFUSED)
        rc, out, fake = self.install(self.first_args(), self.watching_proof(refused))
        self.assertEqual(rc, 3)
        self.assertEqual(self.live(), self.policy)
        self.assertEqual(self.seen["digest"], self.after)
        self.assertFalse(self.marker().exists())
        self.assertFalse(self.store().seen(PROOF_ID))
        self.assertEqual(len(fake.calls), 2)
        self.assertIn("proof refused", out)
        self.assertIn("rolled back to the prior digest", out)
        self.assertFalse(BEARER in out)

    def test_an_inconclusive_proof_rolls_back_to_the_prior_digest(self):
        inconclusive = self.proven(outcome=deploy_proof.INCONCLUSIVE)
        rc, out, fake = self.install(self.first_args(), self.watching_proof(inconclusive))
        self.assertEqual(rc, 3)
        self.assertEqual(self.live(), self.policy)
        self.assertEqual(self.seen["digest"], self.after)
        self.assertFalse(self.marker().exists())
        self.assertFalse(self.store().seen(PROOF_ID))
        self.assertEqual(len(fake.calls), 2)
        self.assertIn("inconclusive", out)
        self.assertIn("rolled back to the prior digest", out)

    def test_a_replayed_proof_id_is_refused_before_any_write(self):
        self.store().add(self.record)

        def proof(*_args):
            raise AssertionError("proof ran")

        rc, out, fake = self.install(self.first_args(), proof)
        self.assertEqual(rc, 2)
        self.assertEqual(self.live(), self.policy)
        self.assertFalse(self.marker().exists())
        self.assertEqual(fake.calls, [])
        self.assertIn("replayed", out)

    def test_a_wrong_seat_proof_rolls_back(self):
        wrong = self.proven(seat="swu-other")
        rc, out, fake = self.install(self.first_args(), self.watching_proof(wrong))
        self.assertEqual(rc, 3)
        self.assertEqual(self.live(), self.policy)
        self.assertEqual(self.seen["digest"], self.after)
        self.assertEqual(self.seen["seat"], "swu-zc")
        self.assertFalse(self.store().seen(PROOF_ID))
        self.assertFalse(self.marker().exists())
        self.assertEqual(len(fake.calls), 2)
        self.assertIn("another seat", out)
        self.assertIn("rolled back to the prior digest", out)

    def test_a_proven_record_with_no_audit_row_rolls_back(self):
        self.audit.unlink()
        rc, _out, fake = self.install(self.first_args(), self.watching_proof(self.record))
        self.assertEqual(rc, 3)
        self.assertEqual(self.live(), self.policy)
        self.assertFalse(self.store().seen(PROOF_ID))
        self.assertEqual(len(fake.calls), 2)

    def test_dry_run_writes_nothing_and_does_not_call_the_proof(self):
        def proof(*_args):
            raise AssertionError("proof ran")

        rc, out, fake = self.install(self.first_args(apply=False), proof)
        self.assertEqual(rc, 0)
        self.assertEqual(self.live(), self.policy)
        self.assertFalse(self.marker().exists())
        self.assertEqual(fake.calls, [])
        self.assertIn("writes nothing", out)
        self.assertFalse(BEARER in out)

    def test_a_seat_already_present_is_refused_before_the_proof(self):
        present = {**self.policy, "swu-zc": dict(NEW_ROW)}
        self.seats_path.write_text(json.dumps(present))
        digest = sp.canonical_digest(present)

        def proof(*_args):
            raise AssertionError("proof ran")

        rc, out, fake = self.install(
            self.first_args(expect_before=digest, expect_after="0" * 64), proof
        )
        self.assertEqual(rc, 2)
        self.assertEqual(self.live(), present)
        self.assertEqual(fake.calls, [])
        self.assertIn("seat already present", out)

    def test_the_receipt_gate_does_not_use_the_post_install_proof(self):
        def proof(*_args):
            raise AssertionError("post-install proof ran")

        rc, _out, fake = self.install(self.receipt_args(apply=True), proof)
        self.assertEqual(rc, 0)
        self.assertEqual(sp.canonical_digest(self.live()), self.after)
        self.assertTrue(self.store().seen(PROOF_ID))
        self.assertEqual(fake.calls, [["systemctl", "restart", "git-relay.service"]])
        self.assertFalse(self.marker().exists())

    def test_an_open_provisional_rolls_back_before_a_receipt_install(self):
        original = self.seats_path.read_text()
        installed = {**self.policy, "swu-zc": dict(NEW_ROW)}
        self.seats_path.write_text(json.dumps(installed))
        sp._write_provisional(
            str(self.seats_path),
            {
                "v": 1,
                "seat": "swu-zc",
                "proof_id": PROOF_ID,
                "before": self.before,
                "after": self.after,
                "policy_before": original,
            },
        )

        def proof(*_args):
            raise AssertionError("proof ran")

        rc, out, fake = self.install(self.receipt_args(apply=True), proof)
        self.assertEqual(rc, 3)
        self.assertEqual(self.live(), self.policy)
        self.assertFalse(self.marker().exists())
        self.assertFalse(self.store().seen(PROOF_ID))
        self.assertGreaterEqual(len(fake.calls), 1)
        self.assertIn("unproven", out)

    def test_a_committed_marker_is_cleared_and_the_row_stays(self):
        original = self.seats_path.read_text()
        installed = {**self.policy, "swu-zc": dict(NEW_ROW)}
        self.seats_path.write_text(json.dumps(installed))
        sp._write_provisional(
            str(self.seats_path),
            {
                "v": 1,
                "seat": "swu-zc",
                "proof_id": PROOF_ID,
                "before": self.before,
                "after": self.after,
                "policy_before": original,
            },
        )
        self.store().add(self.record)

        def proof(*_args):
            raise AssertionError("proof ran")

        rc, out, _fake = self.install(self.receipt_args(), proof)
        self.assertEqual(rc, 2)
        self.assertEqual(sp.canonical_digest(self.live()), self.after)
        self.assertFalse(self.marker().exists())
        self.assertIn("committed", out)

    def _root_marker_fstat(self):
        """Claim the opened marker is root-owned 0600.  This process cannot chown it."""
        real_fstat = os.fstat
        marker = os.path.abspath(self.marker())

        def fstat(fd):
            st = real_fstat(fd)
            try:
                name = os.path.abspath(os.readlink("/proc/self/fd/%d" % fd))
            except OSError:
                return st
            if name == marker:
                return SimpleNamespace(st_mode=0o100600, st_uid=0, st_size=st.st_size)
            return st

        return fstat

    def _plant_unproven(self):
        original = self.seats_path.read_text()
        installed = {**self.policy, "swu-zc": dict(NEW_ROW)}
        self.seats_path.write_text(json.dumps(installed))
        sp._write_provisional(
            str(self.seats_path),
            {
                "v": 1,
                "seat": "swu-zc",
                "proof_id": PROOF_ID,
                "before": self.before,
                "after": self.after,
                "policy_before": original,
            },
        )

    def test_crash_recovery_in_a_service_owned_seats_directory(self):
        """A service-owned seats directory must not block rollback after a crash.

        The live seats file may live in that directory.  The marker file stays
        root-owned 0600.  This process is not root, so geteuid is 0, the opened
        marker claims uid 0 and mode 0600, and chown is a no-op.
        """
        me = pwd.getpwuid(os.getuid())
        self.unit.user = me.pw_name
        directory = self.seats_path.parent
        self.assertNotEqual(directory.stat().st_uid, 0)
        self.assertEqual(self.seats_path.stat().st_uid, directory.stat().st_uid)
        self.assertEqual(directory.stat().st_mode & 0o022, 0)
        self._plant_unproven()

        def proof(*_args):
            raise AssertionError("proof ran")

        fake = _FakeSubprocess()
        out = io.StringIO()
        with (
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(os, "fstat", self._root_marker_fstat()),
            mock.patch.object(os, "chown"),
            mock.patch.object(sp, "_external_proof", proof),
            mock.patch.object(sp, "subprocess", fake),
            mock.patch.object(sp, "_healthy", lambda *a: True),
            contextlib.redirect_stdout(out),
        ):
            rc = sp.install_seat(self.receipt_args(apply=True), self.unit)
        text = out.getvalue()
        self.assertEqual(rc, 3)
        self.assertEqual(self.live(), self.policy)
        self.assertFalse(self.marker().exists())
        self.assertFalse(self.store().seen(PROOF_ID))
        self.assertIn("unproven", text)
        self.assertIn("rolled back", text)
        self.assertFalse(BEARER in text)

    def test_a_group_writable_seats_directory_still_refuses_recovery(self):
        me = pwd.getpwuid(os.getuid())
        self.unit.user = me.pw_name
        self._plant_unproven()
        os.chmod(self.seats_path.parent, 0o707)

        def proof(*_args):
            raise AssertionError("proof ran")

        out = io.StringIO()
        with (
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(os, "fstat", self._root_marker_fstat()),
            mock.patch.object(os, "chown"),
            mock.patch.object(sp, "_external_proof", proof),
            mock.patch.object(sp, "subprocess", _FakeSubprocess()),
            mock.patch.object(sp, "_healthy", lambda *a: True),
            contextlib.redirect_stdout(out),
        ):
            rc = sp.install_seat(self.receipt_args(apply=True), self.unit)
        self.assertEqual(rc, 2)
        self.assertEqual(sp.canonical_digest(self.live()), self.after)
        self.assertTrue(self.marker().exists())
        self.assertIn("group/world-writable", out.getvalue())

    def test_a_marker_whose_write_never_landed_is_cleared(self):
        original = self.seats_path.read_text()
        sp._write_provisional(
            str(self.seats_path),
            {
                "v": 1,
                "seat": "swu-zc",
                "proof_id": PROOF_ID,
                "before": self.before,
                "after": self.after,
                "policy_before": original,
            },
        )

        def proof(*_args):
            raise AssertionError("proof ran")

        rc, _out, fake = self.install(self.first_args(apply=False), proof)
        self.assertEqual(rc, 0)
        self.assertEqual(self.live(), self.policy)
        self.assertFalse(self.marker().exists())
        self.assertEqual(fake.calls, [])

    def test_main_rejects_a_bad_first_seat_invocation_before_reading_a_unit(self):
        with (
            mock.patch.object(sp, "read_unit", side_effect=AssertionError("unit read")),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            with self.assertRaises(SystemExit):
                sp.main(
                    [
                        "install-seat",
                        "--unit",
                        "git-relay.service",
                        "--seat",
                        "swu-zc",
                        "--row-json",
                        "row.json",
                        "--expect-before",
                        self.before,
                        "--expect-after",
                        self.after,
                        "--first-seat",
                    ]
                )
            with self.assertRaises(SystemExit):
                sp.main(
                    [
                        "install-seat",
                        "--unit",
                        "git-relay.service",
                        "--seat",
                        "swu-zc",
                        "--row-json",
                        "row.json",
                        "--expect-before",
                        self.before,
                        "--expect-after",
                        self.after,
                        "--receipt",
                        "receipt.json",
                        "--first-seat",
                        "--proof-id",
                        PROOF_ID,
                    ]
                )


class ProofKeyMintTests(unittest.TestCase):
    """proof-key mints a root-owned 0600 file and never prints the key.

    This environment is not root, so the success path mocks the fstat gate
    that production enforces (uid 0 and mode 0600).  A real non-root caller
    is refused before a file is created.  The owner of the file is root, the
    relay host operator.  The service user and the seat do not own it.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)

    def run_mint(self, path):
        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, {sp.PROOF_KEY_FILE_VAR: ""}),
            contextlib.redirect_stdout(out),
        ):
            rc = sp.proof_key(SimpleNamespace(key_file=str(path)))
        return rc, out.getvalue()

    def test_a_non_root_caller_is_refused_and_creates_nothing(self):
        path = self.root / "proof.key"
        with mock.patch.object(os, "geteuid", return_value=1000):
            rc, out = self.run_mint(path)
        self.assertEqual(rc, 2)
        self.assertFalse(path.exists())
        self.assertIn("root-owned 0600", out)

    def test_a_missing_path_is_refused(self):
        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, {sp.PROOF_KEY_FILE_VAR: ""}),
            contextlib.redirect_stdout(out),
        ):
            rc = sp.proof_key(SimpleNamespace(key_file=""))
        self.assertEqual(rc, 2)
        self.assertIn("names no proof-key file", out.getvalue())

    def test_root_refuses_a_directory_chain_someone_else_can_write(self):
        path = self.root / "proof.key"
        with mock.patch.object(os, "geteuid", return_value=0):
            rc, out = self.run_mint(path)
        self.assertEqual(rc, 2)
        self.assertFalse(path.exists())
        self.assertIn("directory chain", out)

    def test_an_existing_key_is_never_overwritten(self):
        path = self.root / "proof.key"
        path.write_bytes(b"already-there\n")
        os.chmod(path, 0o600)
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        with (
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(sp, "_proof_key_dir_trust"),
        ):
            rc, out = self.run_mint(path)
        self.assertEqual(rc, 2)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)
        self.assertIn("never overwrite", out)

    def test_a_file_that_is_not_root_owned_0600_is_removed(self):
        path = self.root / "proof.key"
        with (
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(sp, "_proof_key_dir_trust"),
            mock.patch.object(
                os, "fstat", return_value=SimpleNamespace(st_uid=1, st_mode=0o100600)
            ),
        ):
            rc, out = self.run_mint(path)
        self.assertEqual(rc, 2)
        self.assertFalse(path.exists())
        self.assertIn("root-owned 0600", out)

    def test_mint_writes_a_private_file_and_does_not_print_the_key(self):
        path = self.root / "proof.key"
        with (
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(sp, "_proof_key_dir_trust"),
            mock.patch.object(
                os, "fstat", return_value=SimpleNamespace(st_uid=0, st_mode=0o100600)
            ),
        ):
            rc, out = self.run_mint(path)
        self.assertEqual(rc, 0)
        info = path.stat()
        raw = path.read_bytes()
        self.assertEqual(info.st_mode & 0o777, 0o600)
        self.assertEqual(len(raw), 65)
        self.assertTrue(raw.endswith(b"\n"))
        self.assertFalse(raw.decode("ascii") in out)
        self.assertFalse(raw.decode("ascii").strip() in out)
        self.assertIn("root-owned 0600", out)
        self.assertIn("never printed", out)
        digest = hashlib.sha256(raw).hexdigest()
        with (
            mock.patch.object(os, "geteuid", return_value=0),
            mock.patch.object(sp, "_proof_key_dir_trust"),
        ):
            again, _out2 = self.run_mint(path)
        self.assertEqual(again, 2)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    unittest.main()
