"""Migration of the seat policy from the file input to the systemd credential input.

Everything runs against a synthetic unit in a temp directory.  systemctl, the health check
and systemd's credential copy are simulated by FakeHost, so nothing here touches a runtime.
The crash tests stop the transaction after every journaled phase, then recover in a fresh
Host from the journal alone, which is what a crash or reboot leaves behind.
"""

import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, DEPLOY / file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


sp = _load("deploy_seat_policy", "seat_policy.py")
mg = _load("deploy_seat_policy_migrate", "seat_policy_migrate.py")

UNIT = "git-relay.service"
OTHER_LINE = "GIT_RELAY_ENABLE_WORKFLOW_VERBS=1"
ROW = {
    "actions": ["open_pr"],
    "app_slug": "swu-old",
    "auth": "shared",
    "identity": "OLD",
    "mint_permissions": {"pull_requests": "write"},
    "repos": ["example-org/example-repo"],
}


class FakeHost(mg.Host):
    """systemd, as far as the migration depends on it."""

    def __init__(self, root: Path, healthy=True, relay="/nonexistent-relay"):
        super().__init__(wait=1, credentials_root=str(root / "run-credentials"), dropin_root=str(root / "systemd"))
        self.root = root
        self.healthy_after_restart = healthy
        self.relay = relay
        self.calls = []
        self.serve_corrupt = False
        self.fail_restart_until_restore = False

    def _dropin(self):
        return Path(self.dropin_root) / f"{UNIT}.d" / mg.DROPIN_NAME

    def systemctl(self, *args):
        self.calls.append(args)
        if args[0] != "restart":
            return 0
        config = (self.root / "config.env").read_text()
        uses_credential = f"{sp.SEATS_CREDENTIAL_VAR}={sp.SEATS_CREDENTIAL}" in config
        if uses_credential:
            # An absolute LoadCredential whose file is missing is a start failure.
            backing = self._backing()
            if not backing or not os.path.exists(backing):
                return 1
            served = Path(self.credentials_root) / UNIT / sp.SEATS_CREDENTIAL
            served.parent.mkdir(parents=True, exist_ok=True)
            data = Path(backing).read_bytes()
            served.write_bytes(b"{}" if self.serve_corrupt else data)
        return 0

    def _backing(self):
        d = self._dropin()
        if not d.exists():
            return ""
        for line in d.read_text().splitlines():
            if line.startswith("LoadCredential="):
                return line.split(":", 1)[1]
        return ""

    def cat(self, unit):
        text = "[Service]\nDynamicUser=yes\n"
        if self._dropin().exists():
            text += self._dropin().read_text()
        return text

    def read_unit(self, unit):
        return sp.Unit(
            name=UNIT,
            relay=self.relay,
            config=str(self.root / "config.env"),
            apps_token="/etc/git-relay/apps.infisical-token",
            environment={"GIT_RELAY_HOST": "127.0.0.1", "GIT_RELAY_PORT": "8471"},
            user="git-relay",
            dynamic_user=True,
            seat_credential=self._backing(),
        )

    def healthy(self, unit):
        if self.fail_restart_until_restore:
            config = (self.root / "config.env").read_text()
            return f"{sp.SEATS_CREDENTIAL_VAR}=" not in config
        return self.healthy_after_restart


class MigrateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.state.mkdir()
        self.policy = {"swu-old": dict(ROW)}
        self.seats = self.state / "seats.json"
        self.policy_bytes = (json.dumps(self.policy, sort_keys=True) + "\n").encode()
        self.seats.write_bytes(self.policy_bytes)
        os.chmod(self.seats, 0o600)
        self.config = self.root / "config.env"
        self.config_bytes = (
            f"# relay config\n{OTHER_LINE}\nGIT_RELAY_SEATS_FILE={self.seats}\nZZ_LAST=keep\n"
        ).encode()
        self.config.write_bytes(self.config_bytes)
        os.chmod(self.config, 0o600)
        self.etc = self.root / "etc"
        self.etc.mkdir(mode=0o700)
        os.chmod(self.etc, 0o700)
        self.journal = self.root / "journal"
        self.journal.mkdir()
        os.chmod(self.journal, 0o700)
        self.backing = str(self.etc / "seat-policy.json")
        self.digest = sp.canonical_digest(self.policy)
        self.host = FakeHost(self.root)
        self.args = SimpleNamespace(
            unit=UNIT,
            backing=self.backing,
            journal_dir=str(self.journal),
            expect_digest=self.digest,
            allow_non_root=True,
        )
        patcher = mock.patch.object(sp, "validate_with", lambda relay, seats: None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cmd(self, fn, *a, **kw):
        out = StringIO()
        with redirect_stdout(out):
            try:
                rc = fn(self.args, self.host, *a, **kw)
            except RuntimeError as exc:
                rc = exc
        return rc, out.getvalue()

    def assert_unchanged(self):
        self.assertEqual(self.config.read_bytes(), self.config_bytes)
        self.assertFalse(os.path.lexists(self.backing))
        self.assertFalse(self.host._dropin().exists())
        self.assertEqual(self.seats.read_bytes(), self.policy_bytes)

    # plan and success
    def test_plan_writes_nothing_and_lists_the_effects(self):
        rc, out = self.run_cmd(mg.plan)
        self.assertEqual(rc, 0)
        self.assertIn("plan only", out)
        self.assertIn("GIT_RELAY_SEATS_FILE", out)
        self.assertIn("LoadCredential=seat-policy:", out)
        self.assert_unchanged()
        self.assertFalse((self.journal / mg.JOURNAL).exists())
        self.assertEqual(self.host.calls, [])

    def test_apply_switches_only_the_one_line_and_adds_the_credential(self):
        rc, out = self.run_cmd(mg.apply)
        self.assertEqual(rc, 0, out)
        lines = self.config.read_bytes().decode().split("\n")
        self.assertEqual(lines[0], "# relay config")
        self.assertEqual(lines[1], OTHER_LINE)
        self.assertEqual(lines[2], "GIT_RELAY_SEATS_CREDENTIAL=seat-policy")
        self.assertEqual(lines[3:], ["ZZ_LAST=keep", ""])
        self.assertEqual(os.stat(self.config).st_mode & 0o777, 0o600)
        self.assertEqual(Path(self.backing).read_bytes(), self.policy_bytes)
        self.assertEqual(os.stat(self.backing).st_mode & 0o777, 0o600)
        self.assertEqual(
            self.host._dropin().read_text(), f"[Service]\nLoadCredential=seat-policy:{self.backing}\n"
        )
        self.assertEqual(self.seats.read_bytes(), self.policy_bytes)  # old file untouched
        self.assertEqual([c for c in self.host.calls if c[0] == "restart"], [("restart", UNIT)])
        journal = json.loads((self.journal / mg.JOURNAL).read_text())
        self.assertEqual(journal["phase"], "healthy")
        self.assertEqual(bytes.fromhex(journal["config"]["bytes_hex"]), self.config_bytes)
        self.assertEqual(bytes.fromhex(journal["policy"]["bytes_hex"]), self.policy_bytes)

    def test_installer_accepts_the_migrated_unit(self):
        self.assertEqual(self.run_cmd(mg.apply)[0], 0)
        unit = self.host.read_unit(UNIT)
        path, credential = sp._seat_policy_source(unit)
        self.assertEqual((path, credential), (self.backing, True))
        self.assertIsNone(sp._credential_backing_refusal(self.backing))

    # refusals leave everything as it was
    def _refused(self, fn=None):
        rc, out = self.run_cmd(fn or mg.apply)
        self.assertEqual(rc, 2, out)
        self.assertIn("refusing", out)
        return out

    def test_digest_mismatch_refuses(self):
        self.args.expect_digest = "0" * 64
        self._refused()
        self.assert_unchanged()
        self.assertFalse((self.journal / mg.JOURNAL).exists())

    def test_existing_backing_or_dropin_is_never_overwritten(self):
        Path(self.backing).write_text("x")
        self._refused()
        os.unlink(self.backing)
        self.host._dropin().parent.mkdir(parents=True)
        self.host._dropin().write_text("x")
        self._refused()
        self.assertEqual(self.config.read_bytes(), self.config_bytes)

    def test_unsafe_directories_refuse(self):
        os.chmod(self.etc, 0o755)
        self._refused()
        os.chmod(self.etc, 0o700)
        os.chmod(self.journal, 0o755)
        self._refused()
        os.chmod(self.journal, 0o700)
        self.assert_unchanged()

    def test_backing_beside_the_current_policy_refuses(self):
        os.chmod(self.state, 0o700)
        self.args.backing = str(self.state / "seat-policy.json")
        self._refused()
        self.assert_unchanged()

    def test_config_shapes_refuse(self):
        for text in (
            f"{OTHER_LINE}\n",  # no FILE line
            f"GIT_RELAY_SEATS_FILE={self.seats}\nGIT_RELAY_SEATS_FILE={self.seats}\n",  # two
            f"GIT_RELAY_SEATS_FILE={self.seats}\nGIT_RELAY_SEATS_CREDENTIAL=seat-policy\n",  # both
            f"GIT_RELAY_SEATS_FILE={self.seats}\nGIT_RELAY_SEATS_JSON={{}}\n",  # inline too
            "GIT_RELAY_SEATS_FILE=relative/seats.json\n",
        ):
            self.config.write_text(text)
            before = self.config.read_bytes()
            self._refused()
            self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(os.path.lexists(self.backing))

    def test_static_token_seat_refuses(self):
        policy = {"swu-old": {**ROW, "token": "x"}}
        self.seats.write_text(json.dumps(policy))
        self.args.expect_digest = sp.canonical_digest(policy)
        self._refused()
        self.assertFalse(os.path.lexists(self.backing))

    def test_already_on_credential_refuses(self):
        self.host._dropin().parent.mkdir(parents=True)
        self.host._dropin().write_text(f"[Service]\nLoadCredential=seat-policy:{self.backing}\n")
        self._refused()

    def test_existing_journal_refuses_until_recovered(self):
        self.assertEqual(self.run_cmd(mg.apply)[0], 0)
        self._refused()

    def test_requires_root_without_the_test_flag(self):
        self.args.allow_non_root = False
        if os.geteuid() != 0:
            self._refused()
            self.assert_unchanged()

    # failure and recovery
    def test_unhealthy_restart_restores_every_before_image(self):
        self.host.healthy_after_restart = False
        self.host.fail_restart_until_restore = True
        rc, out = self.run_cmd(mg.apply)
        self.assertEqual(rc, 3, out)
        self.assertEqual(self.config.read_bytes(), self.config_bytes)
        self.assertFalse(self.host._dropin().exists())
        self.assertEqual(self.seats.read_bytes(), self.policy_bytes)
        self.assertEqual(json.loads((self.journal / mg.JOURNAL).read_text())["phase"], "restored")
        self.assertEqual(os.stat(self.config).st_mode & 0o777, 0o600)

    def test_missing_backing_is_a_start_failure_and_is_restored(self):
        real = mg.atomic_write

        def skip_backing(path, data, mode, uid=None, gid=None):
            if path == self.backing:
                return
            return real(path, data, mode, uid, gid)

        with mock.patch.object(mg, "atomic_write", skip_backing):
            self.host.fail_restart_until_restore = True
            rc, out = self.run_cmd(mg.apply)
        self.assertEqual(rc, 3, out)
        self.assertEqual(self.config.read_bytes(), self.config_bytes)
        self.assertFalse(self.host._dropin().exists())

    def test_served_copy_mismatch_restores(self):
        self.host.serve_corrupt = True
        rc, out = self.run_cmd(mg.apply)
        self.assertEqual(rc, 3, out)
        self.assertIn("does not serve", out)
        self.assertEqual(self.config.read_bytes(), self.config_bytes)
        self.assertFalse(self.host._dropin().exists())

    def test_crash_after_each_phase_restores_in_process(self):
        for phase in ("backing-written", "config-edited", "dropin-written", "restarted"):
            with self.subTest(phase=phase):
                self.setUp()
                rc, out = self.run_cmd(mg.apply, crash_after=phase)
                self.assertIsInstance(rc, RuntimeError)
                self.assertEqual(self.config.read_bytes(), self.config_bytes)
                self.assertFalse(self.host._dropin().exists())
                self.assertEqual(self.seats.read_bytes(), self.policy_bytes)
                self.assertEqual(json.loads((self.journal / mg.JOURNAL).read_text())["phase"], "restored")

    def test_hard_crash_then_fresh_recover_from_the_journal_alone(self):
        """The process dies with no cleanup (power loss, kill -9, reboot):  the restore path
        itself never ran.  A new process with no memory recovers from the journal."""
        for phase in ("backing-written", "config-edited", "dropin-written", "restarted"):
            with self.subTest(phase=phase):
                self.setUp()
                with mock.patch.object(mg, "_restore_from", side_effect=RuntimeError("power loss")):
                    rc, _ = self.run_cmd(mg.apply, crash_after=phase)
                self.assertIsInstance(rc, RuntimeError)
                journal = json.loads((self.journal / mg.JOURNAL).read_text())
                self.assertEqual(journal["phase"], phase)  # last journaled phase survives
                fresh = FakeHost(self.root)
                fresh.healthy_after_restart = True
                self.host = fresh
                self.args = SimpleNamespace(unit=UNIT, journal_dir=str(self.journal))
                rc, out = self.run_cmd(mg.recover)
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.config.read_bytes(), self.config_bytes)
                self.assertFalse(fresh._dropin().exists())
                self.assertEqual(self.seats.read_bytes(), self.policy_bytes)
                # safe to run again
                rc, out = self.run_cmd(mg.recover)
                self.assertEqual(rc, 0, out)
                self.assertEqual(self.config.read_bytes(), self.config_bytes)

    def test_recover_works_with_the_service_stopped_and_after_a_completed_migration(self):
        self.assertEqual(self.run_cmd(mg.apply)[0], 0)
        self.host = FakeHost(self.root)  # new process after a reboot:  nothing in memory
        self.args = SimpleNamespace(unit=UNIT, journal_dir=str(self.journal))
        rc, out = self.run_cmd(mg.recover)
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.config.read_bytes(), self.config_bytes)
        self.assertFalse(self.host._dropin().exists())
        self.assertEqual([c[0] for c in self.host.calls][:2], ["daemon-reload", "restart"])

    def test_recover_refuses_a_tampered_journal_wrong_unit_or_no_journal(self):
        self.args = SimpleNamespace(unit=UNIT, journal_dir=str(self.journal))
        self.assertEqual(self.run_cmd(mg.recover)[0], 2)  # no journal
        self.args = SimpleNamespace(
            unit=UNIT, backing=self.backing, journal_dir=str(self.journal),
            expect_digest=self.digest, allow_non_root=True,
        )
        self.assertEqual(self.run_cmd(mg.apply)[0], 0)
        path = self.journal / mg.JOURNAL
        journal = json.loads(path.read_text())
        self.args = SimpleNamespace(unit="other.service", journal_dir=str(self.journal))
        self.assertEqual(self.run_cmd(mg.recover)[0], 2)
        journal["config"]["bytes_hex"] = b"tampered\n".hex()
        path.write_text(json.dumps(journal))
        self.args = SimpleNamespace(unit=UNIT, journal_dir=str(self.journal))
        rc, out = self.run_cmd(mg.recover)
        self.assertEqual(rc, 2)
        self.assertIn("fails its own digest", out)
        self.assertNotEqual(self.config.read_bytes(), b"tampered\n")

    def test_journal_is_private_and_never_deleted(self):
        self.assertEqual(self.run_cmd(mg.apply)[0], 0)
        self.assertEqual(os.stat(self.journal / mg.JOURNAL).st_mode & 0o777, 0o600)
        self.assertTrue(
            hashlib.sha256(self.config_bytes).hexdigest()
            == json.loads((self.journal / mg.JOURNAL).read_text())["config"]["sha256"]
        )


if __name__ == "__main__":
    unittest.main()
