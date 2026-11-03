"""Linux entrypoint accepts only a valid service-token bearer and delegates safely."""

from __future__ import annotations

import contextlib
import io
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_SPEC = importlib.util.spec_from_file_location(
    "fleet_secrets_linux_entrypoint", Path(__file__).parents[1] / "linux-entrypoint.py"
)
entry = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(entry)
TOKEN = "st.fixture-service-token-for-tests"


class ServiceTokenEntryTests(unittest.TestCase):
    def test_missing_and_malformed_tokens_fail_closed(self):
        for token in (None, "", "not-a-token", "st.short", "st.fixture has-space"):
            with self.subTest(token=token), mock.patch.dict(os.environ, {"INFISICAL_TOKEN": token or ""}, clear=True):
                with self.assertRaises(entry.EntryError) as caught:
                    entry.authenticated_env()
                self.assertRegex(str(caught.exception), "scoped service token|scoped service-token|scoped reader")

    def test_scoped_reader_and_writer_tokens_are_preserved_separately(self):
        with mock.patch.dict(
            os.environ,
            {"INFISICAL_TOKEN": TOKEN, "INFISICAL_WRITE_TOKEN": "st.write-token-fixture", "INFISICAL_AUTOMATION_CLIENT_SECRET": "legacy"},
            clear=True,
        ):
            env = entry.authenticated_env()
        self.assertEqual(
            env,
            {"INFISICAL_TOKEN": TOKEN, "INFISICAL_WRITE_TOKEN": "st.write-token-fixture"},
        )

    def test_malformed_optional_writer_does_not_block_valid_reader(self):
        with mock.patch.dict(
            os.environ,
            {"INFISICAL_TOKEN": TOKEN, "INFISICAL_WRITE_TOKEN": "not-a-token"},
            clear=True,
        ):
            self.assertEqual(
                entry.authenticated_env(),
                {"INFISICAL_TOKEN": TOKEN, "INFISICAL_WRITE_TOKEN": "not-a-token"},
            )

    def test_help_delegates_without_any_auth_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "fleet-secrets"
            target.write_text("#!/bin/sh\nexit 0\n")
            target.chmod(0o755)
            with (
                mock.patch.dict(os.environ, {"FLEET_SECRETS_CLI": str(target), "INFISICAL_TOKEN": TOKEN}, clear=True),
                mock.patch.object(entry.sys, "argv", ["fleet-secrets", "--help"]),
                mock.patch.object(entry.os, "execve") as execve,
            ):
                entry.main()
            self.assertFalse(set(execve.call_args.args[2]) & set(entry.core.AUTH_ENV_VARS))

    def test_secret_command_requires_token_then_execs_plain_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "fleet-secrets"
            target.write_text("#!/bin/sh\nexit 0\n")
            target.chmod(0o755)
            stderr = io.StringIO()
            with (
                mock.patch.dict(os.environ, {"FLEET_SECRETS_CLI": str(target)}, clear=True),
                mock.patch.object(entry.sys, "argv", ["fleet-secrets", "names"]),
                contextlib.redirect_stderr(stderr),
            ):
                self.assertEqual(entry.main(), 4)
            self.assertIn("scoped service token", stderr.getvalue())
            with (
                mock.patch.dict(os.environ, {"FLEET_SECRETS_CLI": str(target), "INFISICAL_TOKEN": TOKEN}, clear=True),
                mock.patch.object(entry.sys, "argv", ["fleet-secrets", "names"]),
                mock.patch.object(entry.os, "execve") as execve,
            ):
                entry.main()
            self.assertEqual(execve.call_args.args[2]["INFISICAL_TOKEN"], TOKEN)


if __name__ == "__main__":
    unittest.main()
