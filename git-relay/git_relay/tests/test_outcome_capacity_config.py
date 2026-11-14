"""Capacity startup policy uses the existing source before touching durable state."""

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

from git_relay import app_tokens, outcomes, server


class OutcomeCapacityConfigurationTests(unittest.TestCase):
    def _factory(self, value="20000", *, error=None, enabled=True, **overrides):
        events = []
        source = mock.Mock()
        source.secret.side_effect = AssertionError("folder proof required")
        source.secrets.side_effect = AssertionError("App keys must remain lazy")

        def verified(name):
            events.append("capacity")
            if error is not None:
                raise error
            return value

        source.verified_secret.side_effect = verified
        env = {
            "GIT_RELAY_APP_MINT": "1" if enabled else "0",
            "GIT_RELAY_APPS_CREDENTIAL_FILE": "/synthetic/apps-token",
            "GIT_RELAY_SEATS_JSON": json.dumps({
                "swu-echo": {
                    "token": uuid.uuid4().hex,
                    "actions": ["open_pr"],
                    "repos": ["example/fixture-repo"],
                }
            }),
            **overrides,
        }
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
        stack.enter_context(mock.patch.object(server, "_build_shared_verifier", return_value=None))
        stack.enter_context(mock.patch.object(server, "_build_assertion_verifier", return_value=None))
        owner = stack.enter_context(mock.patch.object(
            server, "_owner_token_from_cli", return_value=uuid.uuid4().hex
        ))
        discovered = stack.enter_context(mock.patch.object(server.shutil, "which", return_value="synthetic-cli"))
        constructor = stack.enter_context(mock.patch.object(app_tokens, "FleetSecretsSource", return_value=source))
        minter = stack.enter_context(mock.patch.object(app_tokens, "AppTokenMinter", wraps=app_tokens.AppTokenMinter))

        def adopt(path):
            events.append("adopt")
            return path

        adoption = stack.enter_context(mock.patch.object(server, "adopt_legacy_state", side_effect=adopt))
        audit = stack.enter_context(mock.patch.object(server, "AuditLog"))

        def store(path, **kwargs):
            events.append("store")
            return outcomes.OutcomeStore(None, **kwargs)

        store_factory = stack.enter_context(mock.patch.object(server, "OutcomeStore", side_effect=store))
        return source, constructor, minter, adoption, audit, store_factory, owner, discovered, events

    def test_enabled_startup_reads_once_before_state_and_reuses_the_same_source(self):
        source, constructor, minter, adoption, audit, store, owner, discovered, events = self._factory(
            GIT_RELAY_OUTCOMES_MAX_ENTRIES="50000"
        )
        relay = server.from_env()
        source.verified_secret.assert_called_once_with(server.OUTCOMES_CAPACITY_KEY)
        constructor.assert_called_once_with(
            path="/github-apps", credential_file="/synthetic/apps-token"
        )
        self.assertIs(minter.call_args.args[0], source)
        self.assertIs(relay.app_minter._source, source)
        self.assertEqual(relay.outcomes._max_entries, 20000)
        store.assert_called_once_with(server.DEFAULT_OUTCOMES_PATH, max_entries=20000)
        self.assertEqual(events, ["capacity", "adopt", "adopt", "store"])
        source.secret.assert_not_called()
        source.secrets.assert_not_called()

    def test_configured_existing_folder_is_used_without_a_second_source(self):
        source, constructor, *_ = self._factory(GIT_RELAY_SECRETS_PATH="/moved-apps")
        server.from_env()
        constructor.assert_called_once_with(
            path="/moved-apps", credential_file="/synthetic/apps-token"
        )
        source.verified_secret.assert_called_once_with(server.OUTCOMES_CAPACITY_KEY)

    def test_invalid_setting_refuses_before_state_or_owner_credentials_and_never_echoes_it(self):
        for value in (None, True, 20000, "", " 20000", "20000 ", "020000", "+20000", "2e4", "20000.0", "4999", "50001", "9" * 100, "bad-value"):
            with self.subTest(value=value):
                fields = self._factory(value, GIT_RELAY_OUTCOMES_MAX_ENTRIES="20000")
                source, constructor, minter, adoption, audit, store, owner, discovered, events = fields
                with self.assertRaises(SystemExit) as caught:
                    server.from_env()
                self.assertIn(server.OUTCOMES_CAPACITY_KEY, str(caught.exception))
                if isinstance(value, str) and value:
                    self.assertNotIn(value, str(caught.exception))
                minter.assert_not_called()
                adoption.assert_not_called()
                audit.assert_not_called()
                store.assert_not_called()
                owner.assert_not_called()
                self.assertEqual(events, ["capacity"])
                self.doCleanups()

    def test_missing_or_unreadable_setting_does_not_fall_back_to_an_environment_value(self):
        source, constructor, minter, adoption, audit, store, owner, discovered, events = self._factory(
            error=app_tokens.AppTokenError("capacity setting unavailable"),
            GIT_RELAY_OUTCOMES_MAX_ENTRIES="20000",
        )
        with self.assertRaises(SystemExit):
            server.from_env()
        source.verified_secret.assert_called_once_with(server.OUTCOMES_CAPACITY_KEY)
        for boundary in (minter, adoption, audit, store, owner):
            boundary.assert_not_called()

    def test_app_disabled_keeps_the_default_without_a_source_or_new_authentication(self):
        source, constructor, minter, adoption, audit, store, owner, discovered, events = self._factory(
            enabled=False, GIT_RELAY_OUTCOMES_MAX_ENTRIES="not-a-setting-source"
        )
        relay = server.from_env()
        self.assertEqual(relay.outcomes._max_entries, 5000)
        self.assertIsNone(relay.app_minter)
        constructor.assert_not_called()
        source.verified_secret.assert_not_called()
        minter.assert_not_called()
        discovered.assert_not_called()
        self.assertEqual(events, ["adopt", "adopt", "store"])

    def test_production_validation_accepts_only_the_declared_bounds(self):
        for value in ("5000", "20000", "50000"):
            source = mock.Mock()
            source.verified_secret.return_value = value
            self.assertEqual(server._outcome_capacity_from_source(source), int(value))


class OutcomeCapacityRealSourceTests(unittest.TestCase):
    """The main-branch Source, real CLI and real verifier read only loopback fixtures."""

    def setUp(self):
        scripts = str(Path(__file__).resolve().parents[3] / "scripts")
        sys.path.insert(0, scripts)
        try:
            from fleet_knobs.tests._fake import ScopedReadAPI
            from fleet_secrets import cli
        finally:
            sys.path.remove(scripts)
        self.cli = cli
        self.api = ScopedReadAPI(
            {server.OUTCOMES_CAPACITY_KEY: "20000"}, project=str(uuid.uuid4()), path="/github-apps"
        )
        self.api.metadata_override = {"expiresAt": "2099-01-01T00:00:00Z"}
        self.api.__enter__()
        self.addCleanup(self.api.__exit__, None, None, None)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name).resolve()
        token = root / "token"
        token.write_text(self.api.token)
        token.chmod(0o600)
        self.calls = []

        def runner(argv, *, capture_output, timeout, env):
            self.calls.append((list(argv), dict(env), timeout))
            out, err = io.StringIO(), io.StringIO()
            # Source keeps production's existing child allowlist.  This test-only
            # runner routes the real CLI to its synthetic loopback provider.
            child = {**env, "FLEET_SECRETS_API": self.api.env["FLEET_SECRETS_API"]}
            code = self.cli.main(argv[1:], env=child, out=out, err=err)
            return subprocess.CompletedProcess(argv, code, out.getvalue().encode(), err.getvalue().encode())

        self.source = app_tokens.FleetSecretsSource(
            executable="synthetic-cli",
            runner=runner,
            path="/github-apps",
            credential_file=str(token),
            environ={
                "PATH": os.defpath,
                "INFISICAL_FLEET_OPS_PROJECT_ID": self.api.project,
                "INFISICAL_TOKEN": uuid.uuid4().hex,
                "HOME": str(root),
            },
        )

    def test_real_cli_reads_the_single_folder_proven_setting(self):
        self.assertEqual(server._outcome_capacity_from_source(self.source), 20000)
        self.assertEqual(len(self.calls), 1)
        argv, env, timeout = self.calls[0]
        self.assertEqual(argv, ["synthetic-cli", "get-verified", server.OUTCOMES_CAPACITY_KEY, "--path", "/github-apps"])
        self.assertEqual(timeout, 30)
        self.assertEqual(env, {
            "PATH": os.defpath,
            "INFISICAL_FLEET_OPS_PROJECT_ID": self.api.project,
            "INFISICAL_TOKEN": self.api.token,
            "INFISICAL_ENVIRONMENT": "prod",
        })
        self.assertEqual(len(self.api.requests), 2)
        self.assertEqual(self.api.requests[0], "/api/v2/service-token")
        self.assertIn("/raw/" + server.OUTCOMES_CAPACITY_KEY + "?", self.api.requests[1])

    def test_wrong_scope_or_provider_failure_refuses_without_a_value_read(self):
        for metadata, status in (
            ({"workspace": str(uuid.uuid4())}, 200),
            ({"scopes": [{"environment": "prod", "secretPath": "/other"}]}, 200),
            ({"scopes": [{"environment": "staging", "secretPath": "/github-apps"}]}, 200),
            ({}, 503),
        ):
            self.api.metadata_override = {"expiresAt": "2099-01-01T00:00:00Z", **metadata}
            self.api.status = status
            self.api.requests.clear()
            with self.subTest(metadata=metadata, status=status), self.assertRaises(app_tokens.AppTokenError):
                server._outcome_capacity_from_source(self.source)
            self.assertEqual(self.api.requests, ["/api/v2/service-token"])


if __name__ == "__main__":
    unittest.main()
