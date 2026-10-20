"""Environment-only Linux authentication; no real credentials or network."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import signal
import subprocess
import unittest
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
            mock.patch.dict(os.environ, PAIR, clear=True),
            mock.patch.object(entry.core, "find_cli", return_value="/synthetic/infisical"),
            mock.patch.object(entry.subprocess, "Popen", return_value=self.process("PRIVATE_CHILD", 1)),
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

    def test_missing_cli_has_a_distinct_exit_status(self):
        with (
            mock.patch.dict(os.environ, PAIR, clear=True),
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
