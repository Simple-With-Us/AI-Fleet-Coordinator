"""Environment-only Linux authentication; no real credentials or network."""

import contextlib
import importlib.util
import io
import os
import shutil
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "linux_entrypoint", Path(__file__).parents[1] / "linux-entrypoint.py"
)
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)

PAIR = {
    "INFISICAL_AUTOMATION_CLIENT_ID": "synthetic-id",
    "INFISICAL_AUTOMATION_CLIENT_SECRET": "synthetic-secret",
}


class BootstrapTests(unittest.TestCase):
    def process(self, stdout="synthetic-token\n", returncode=0):
        proc = mock.Mock(returncode=returncode)
        proc.communicate.return_value = (stdout, "synthetic-private-output")
        proc.poll.return_value = returncode
        return proc

    def delegate(self, tmpdir="delegate"):
        """A real, executable stand-in for the plain CLI, so the delegate check passes and the
        test reaches the login it is actually about."""
        path = Path(tempfile.mkdtemp()) / tmpdir / "fleet-secrets"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\nexit 0\n")
        path.chmod(0o755)
        self.addCleanup(shutil.rmtree, path.parent, True)
        return path

    def test_environment_login_uses_resolved_cli_and_confines_credentials(self):
        proc = self.process()
        with (
            mock.patch.dict(os.environ, PAIR, clear=True),
            mock.patch.object(entry.core, "find_cli", return_value="/synthetic/infisical"),
            mock.patch.object(entry.subprocess, "Popen", return_value=proc) as run,
            mock.patch("builtins.open", side_effect=AssertionError("credential files forbidden")),
        ):
            env = entry.authenticated_env()
        self.assertEqual(env, {"INFISICAL_TOKEN": "synthetic-token"})
        argv = " ".join(run.call_args.args[0])
        self.assertNotIn("synthetic-secret", argv)
        self.assertNotIn("synthetic-id", argv)
        child = run.call_args.kwargs["env"]
        self.assertNotIn("INFISICAL_AUTOMATION_CLIENT_SECRET", child)
        self.assertNotIn("INFISICAL_AUTOMATION_CLIENT_ID", child)
        self.assertEqual(child["INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET"], "synthetic-secret")
        self.assertEqual(child["INFISICAL_DISABLE_UPDATE_CHECK"], "true")
        self.assertEqual(run.call_args.kwargs["cwd"], "/")

    def test_inherited_token_wins_and_strips_all_machine_credentials(self):
        with (
            mock.patch.dict(os.environ, dict(PAIR, INFISICAL_TOKEN="synthetic-token"), clear=True),
            mock.patch.object(entry, "load_machine_credentials") as load,
        ):
            self.assertEqual(entry.authenticated_env(), {"INFISICAL_TOKEN": "synthetic-token"})
        load.assert_not_called()

    def test_missing_environment_credentials_are_named_without_values(self):
        for missing in PAIR:
            env = {key: value for key, value in PAIR.items() if key != missing}
            with self.assertRaises(entry.EntryError) as caught:
                entry.load_machine_credentials(env)
            self.assertIn(missing, str(caught.exception))
            self.assertNotIn("synthetic-secret", str(caught.exception))

    def test_failed_login_suppresses_private_child_output(self):
        stderr = io.StringIO()
        with (
            mock.patch.dict(
                os.environ,
                dict(PAIR, FLEET_SECRETS_CLI=str(self.delegate())),
                clear=True,
            ),
            mock.patch.object(entry.core, "find_cli", return_value="/synthetic/infisical"),
            mock.patch.object(
                entry.subprocess, "Popen", return_value=self.process("PRIVATE_CHILD", 1)
            ),
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(entry.main(), 4)
        self.assertNotIn("PRIVATE_CHILD", stderr.getvalue())
        self.assertNotIn("synthetic-secret", stderr.getvalue())

    def test_timeout_reaps_the_child_and_restores_the_signal_handler(self):
        proc = self.process()
        proc.poll.return_value = None
        proc.communicate.side_effect = [subprocess.TimeoutExpired("login", 25), ("", "")]
        original = signal.getsignal(signal.SIGTERM)
        with mock.patch.object(entry.subprocess, "Popen", return_value=proc):
            with self.assertRaises(subprocess.TimeoutExpired):
                entry._login("/synthetic/infisical", {})
        proc.terminate.assert_called_once()
        self.assertIs(signal.getsignal(signal.SIGTERM), original)

    def test_interruption_reaps_the_child(self):
        proc = self.process()
        proc.poll.return_value = None
        proc.communicate.side_effect = [entry.Interrupted(signal.SIGTERM), ("", "")]
        with mock.patch.object(entry.subprocess, "Popen", return_value=proc):
            with self.assertRaises(entry.Interrupted):
                entry._login("/synthetic/infisical", {})
        proc.terminate.assert_called_once()

    def test_delegating_to_the_entry_point_itself_is_refused(self):
        # FLEET_SECRETS_BIN names the fleet-secrets command, which on the server is this
        # script:  exec'ing it would re-authenticate and exec again, forever.
        self_file = Path(entry.__file__)
        with tempfile.TemporaryDirectory() as home:
            install = Path(home) / "share"
            install.mkdir()
            (install / "fleet-secrets").symlink_to(self_file)
            for env in (
                {"FLEET_SECRETS_CLI": str(self_file), "HOME": "/nonexistent"},
                {"FLEET_SECRETS_HOME": str(install), "HOME": "/nonexistent"},
            ):
                with self.subTest(env=sorted(env)):
                    with (
                        mock.patch.dict(os.environ, dict(PAIR, **env), clear=True),
                        mock.patch.object(entry, "authenticated_env") as auth,
                        contextlib.redirect_stderr(io.StringIO()),
                    ):
                        self.assertEqual(entry.main(), 127)
                    auth.assert_not_called()

    def test_a_missing_cli_is_reported_before_the_login(self):
        # The delegate is checked first, so a mis-pathed CLI costs no login and is named.
        stderr = io.StringIO()
        with (
            mock.patch.dict(
                os.environ, dict(PAIR, FLEET_SECRETS_CLI="/nonexistent/fleet-secrets"), clear=True
            ),
            mock.patch.object(entry, "authenticated_env") as auth,
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(entry.main(), 127)
        auth.assert_not_called()
        self.assertIn("FLEET_SECRETS_CLI", stderr.getvalue())

    def test_missing_cli_has_a_distinct_exit_status(self):
        # The delegate is present, so this is the infisical CLI that is missing.
        with (
            mock.patch.dict(
                os.environ,
                dict(PAIR, FLEET_SECRETS_CLI=str(self.delegate())),
                clear=True,
            ),
            mock.patch.object(entry.core, "find_cli", return_value=None),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(entry.main(), 127)

    def test_inherited_token_is_validated_without_falling_back(self):
        with mock.patch.dict(os.environ, dict(PAIR, INFISICAL_TOKEN="invalid token"), clear=True):
            with self.assertRaises(entry.EntryError):
                entry.authenticated_env()


if __name__ == "__main__":
    unittest.main()
