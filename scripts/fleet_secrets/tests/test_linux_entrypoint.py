"""Bootstrap file security checks; no real credentials or network."""

import contextlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "linux_entrypoint", Path(__file__).parents[1] / "linux-entrypoint.py"
)
entry = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entry)


class BootstrapTests(unittest.TestCase):
    def test_only_owned_private_regular_file_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bootstrap.json"
            path.write_text(
                json.dumps(
                    {
                        "INFISICAL_AUTOMATION_CLIENT_ID": "fake-id",
                        "INFISICAL_AUTOMATION_CLIENT_SECRET": "fake-secret",
                    }
                )
            )
            path.chmod(0o600)
            self.assertEqual(entry.load_bootstrap(path), ["fake-id", "fake-secret"])
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                entry.load_bootstrap(path)
            path.chmod(0o600)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                entry.load_bootstrap(link)

    def test_missing_pair_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bootstrap.json"
            path.write_text("{}")
            path.chmod(0o600)
            with self.assertRaises(ValueError):
                entry.load_bootstrap(path)

    def test_failed_login_never_prints_child_output(self):
        result = mock.Mock(
            returncode=1, stdout="FAKE_SECRET_FROM_CHILD", stderr="FAKE_SECRET_FROM_CHILD"
        )
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(entry, "load_bootstrap", return_value=["fake-id", "fake-secret"]),
            mock.patch.object(entry.subprocess, "run", return_value=result) as run,
            contextlib.redirect_stderr(stderr),
        ):
            self.assertEqual(entry.main(), 4)
        self.assertNotIn("FAKE_SECRET_FROM_CHILD", stderr.getvalue())
        self.assertNotIn("fake-secret", run.call_args.args[0])

    def test_direct_consumers_receive_token_without_bootstrap_credentials(self):
        result = mock.Mock(returncode=0, stdout="fake-token\n")
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(entry, "load_bootstrap", return_value=["fake-id", "fake-secret"]),
            mock.patch.object(entry.subprocess, "run", return_value=result) as run,
        ):
            env = entry.authenticated_env()
        self.assertEqual(env, {"INFISICAL_TOKEN": "fake-token"})
        self.assertNotIn("fake-secret", run.call_args.args[0])

    def test_inherited_token_does_not_reauthenticate(self):
        with (
            mock.patch.dict(os.environ, {"INFISICAL_TOKEN": "fake-token"}, clear=True),
            mock.patch.object(entry, "load_bootstrap") as load,
        ):
            self.assertEqual(entry.authenticated_env(), {"INFISICAL_TOKEN": "fake-token"})
        load.assert_not_called()

    def test_main_hands_the_minted_token_to_the_execed_cli(self):
        result = mock.Mock(returncode=0, stdout="fake-token\n")
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.object(entry, "load_bootstrap", return_value=["fake-id", "fake-secret"]),
            mock.patch.object(entry.subprocess, "run", return_value=result),
            mock.patch.object(entry.os, "execve") as execve,
        ):
            self.assertIsNone(entry.main())
        env = execve.call_args.args[2]
        self.assertEqual(env["INFISICAL_TOKEN"], "fake-token")
        self.assertNotIn("fake-id", env.values())
        self.assertNotIn("fake-secret", env.values())

    def test_main_reauthenticates_rather_than_trusting_an_inherited_token(self):
        result = mock.Mock(returncode=0, stdout="fresh-token\n")
        with (
            mock.patch.dict(os.environ, {"INFISICAL_TOKEN": "expired-token"}, clear=True),
            mock.patch.object(entry, "load_bootstrap", return_value=["fake-id", "fake-secret"]),
            mock.patch.object(entry.subprocess, "run", return_value=result) as run,
            mock.patch.object(entry.os, "execve") as execve,
        ):
            entry.main()
        run.assert_called_once()
        self.assertEqual(execve.call_args.args[2]["INFISICAL_TOKEN"], "fresh-token")

    def test_blank_inherited_token_is_not_taken_as_authenticated(self):
        with (
            mock.patch.dict(os.environ, {"INFISICAL_TOKEN": "   "}, clear=True),
            mock.patch.object(entry, "load_bootstrap", return_value=["fake-id", "fake-secret"]),
            mock.patch.object(
                entry.subprocess, "run", return_value=mock.Mock(returncode=0, stdout="fake-token\n")
            ),
        ):
            env = entry.authenticated_env()
        self.assertEqual(env["INFISICAL_TOKEN"], "fake-token")


if __name__ == "__main__":
    unittest.main()
