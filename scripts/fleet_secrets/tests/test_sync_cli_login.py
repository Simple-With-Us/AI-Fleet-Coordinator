"""Tests for the CLI login sync.

    cd scripts && python3 -m unittest fleet_secrets.tests.test_sync_cli_login -v

Everything here is a fake:  the Infisical API,  the fleet-secrets child,  and the
file the owner hands over.  No value is ever printed,  and that is what the tests
assert.
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from fleet_secrets import core
from fleet_secrets import sync_cli_login as sync

VAL = "SENTINEL_LOGIN_VALUE_4c7d"
TOKEN = "SENTINEL_TOKEN_9a2b"
PATH = "/cli-logins/OPENCODE"


class SyncTests(unittest.TestCase):
    """The sync writes the login,  proves it landed,  and says nothing about the value."""

    def setUp(self) -> None:
        self.env = dict(os.environ)
        self.env["GH_TOKEN"] = VAL
        # The project id is part of the environment the fleet's own tests set,  and
        # without it every request refuses before it is made.
        self.env[core.ENV_PROJECT] = "00000000-0000-4000-8000-000000003b6c"
        self.api: list = []
        self.written: dict = {}
        self.token_calls: list = []

    def fake_api(self, method, path, token, body, env):
        self.api.append((method, path, token, dict(body or {})))
        return 200

    def fake_api_json(self, method, path, token, body, env):
        self.api.append((method, path, token, dict(body or {})))
        return 200, {"message": "ok"}

    def _resolve_token(self, env, cli=None, mode=None):
        self.token_calls.append((cli, mode))
        return TOKEN

    def fake_fetch(self, path, env, project_name=core.DEFAULT_PROJECT, token=None):
        return dict(self.written)

    def run_sync(self, argv=None, env=None):
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(core, "_api", self.fake_api),
            mock.patch.object(core, "_api_json", self.fake_api_json),
            mock.patch.object(core, "fetch_all", self.fake_fetch),
            mock.patch.object(core, "resolve_token", self._resolve_token),
        ):
            code = sync.main(
                argv if argv is not None else ["--name", "GH_TOKEN", "--path", PATH],
                env=env if env is not None else self.env,
                out=out,
                err=err,
            )
        return code, out.getvalue(), err.getvalue()

    def test_the_value_comes_from_the_environment_and_never_lands_in_the_output(self):
        self.written = {"GH_TOKEN": VAL}
        code, out, err = self.run_sync()
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "synced GH_TOKEN at /cli-logins/OPENCODE\n")
        self.assertNotIn(VAL, out)
        self.assertNotIn(VAL, err)

    def test_the_policy_is_checked_before_the_value_is_read(self):
        """A name outside the allowlist must not reach a child or the network."""
        with mock.patch.object(sync, "read_value", side_effect=AssertionError("read anyway")):
            code, _out, err = self.run_sync(["--name", "SOMETHING_ELSE", "--path", PATH])
        self.assertEqual(code, 2)
        self.assertIn("name must be one of", err)
        self.assertEqual(self.api, [])

    def test_a_path_outside_the_allowlist_is_refused_before_the_value_is_read(self):
        with mock.patch.object(sync, "read_value", side_effect=AssertionError("read anyway")):
            code, _out, err = self.run_sync(["--name", "GH_TOKEN", "--path", "/linear"])
        self.assertEqual(code, 2)
        self.assertIn("write allowlist", err)
        self.assertEqual(self.api, [])

    def test_the_token_is_resolved_once_and_used_for_every_call(self):
        self.written = {"GH_TOKEN": VAL}
        code, _out, err = self.run_sync()
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.token_calls), 1)

    def test_a_read_back_that_does_not_match_is_a_failure(self):
        self.written = {"GH_TOKEN": "SOMETHING_ELSE"}
        code, _out, err = self.run_sync()
        self.assertEqual(code, 4)
        self.assertIn("does not match", err)

    def test_a_read_back_with_nothing_in_it_is_a_failure(self):
        self.written = {}
        code, _out, err = self.run_sync()
        self.assertEqual(code, 4)
        self.assertIn("read back found nothing", err)

    def test_the_fleet_secrets_source_reads_through_the_fleet_path(self):
        with mock.patch.object(sync, "_read_from_fleet_secrets", return_value=VAL) as reader:
            self.written = {"GH_TOKEN": VAL}
            code, _out, err = self.run_sync(
                ["--name", "GH_TOKEN", "--path", PATH, "--from", "fleet-secrets"]
            )
        self.assertEqual(code, 0, err)
        self.assertEqual(reader.call_args[0][0], "GH_TOKEN")
        self.assertEqual(reader.call_args[0][1], "/")

    def test_a_missing_binary_is_127_and_a_timeout_is_124(self):
        def missing(name, path, env):
            raise core.SecretsError("cli-missing:  fleet-secrets is not installed", 127)

        def timeout(name, path, env):
            raise core.SecretsError("the read timed out", 124)

        with mock.patch.object(sync, "_read_from_fleet_secrets", missing):
            code, _out, err = self.run_sync(
                ["--name", "GH_TOKEN", "--path", PATH, "--from", "fleet-secrets"]
            )
        self.assertEqual(code, 127)
        with mock.patch.object(sync, "_read_from_fleet_secrets", timeout):
            code, _out, err = self.run_sync(
                ["--name", "GH_TOKEN", "--path", PATH, "--from", "fleet-secrets"]
            )
        self.assertEqual(code, 124)

    def test_the_file_source_wants_an_owner_only_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            loose = Path(tmp) / "loose.txt"
            loose.write_text(VAL, encoding="utf-8")
            os.chmod(loose, 0o644)
            code, _out, err = self.run_sync(
                ["--name", "GH_TOKEN", "--path", PATH, "--from", "file", "--file", str(loose)]
            )
            self.assertEqual(code, 4)
            self.assertIn("mode 0600", err)

    def test_an_owner_only_file_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            owner_only = Path(tmp) / "login.txt"
            owner_only.write_text(VAL + "\n", encoding="utf-8")
            os.chmod(owner_only, 0o600)
            self.written = {"GH_TOKEN": VAL}
            code, out, err = self.run_sync(
                ["--name", "GH_TOKEN", "--path", PATH, "--from", "file", "--file", str(owner_only)]
            )
        self.assertEqual(code, 0, err)
        self.assertNotIn(VAL, out)


if __name__ == "__main__":
    unittest.main()
