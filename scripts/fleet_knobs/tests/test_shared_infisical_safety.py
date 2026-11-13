"""Shared-client review regressions with no live stores, subprocesses or network."""

import builtins
import json
import os
import secrets
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import fleet_infisical as client
from fleet_rag.infisical_settings import InfisicalSettings, SettingsError

from fleet_knobs import knobs, lkg, setter, source
from fleet_knobs.tests._fake import KnobTestCase


class RetiredSessionTests(unittest.TestCase):
    def test_even_an_explicit_tty_session_refuses_before_any_cli_or_network(self):
        with (
            mock.patch.object(client.sys.stdin, "isatty", return_value=True),
            mock.patch.object(
                client.subprocess, "run", side_effect=AssertionError("no consumer CLI session")
            ) as run,
            mock.patch.object(client, "_api_json") as api,
        ):
            with self.assertRaisesRegex(client.SecretsError, "service-token-required"):
                client.fetch_with_cli_session("synthetic-project", "/", {})
        run.assert_not_called()
        api.assert_not_called()

    def test_legacy_allow_session_argument_never_authorizes_a_tokenless_read(self):
        env = {client.ENV_PROJECT: "00000000-0000-4000-8000-000000000042"}
        with (
            mock.patch.object(client.sys.stdin, "isatty", return_value=True),
            mock.patch.object(
                client.subprocess, "run", side_effect=AssertionError("no session")
            ) as run,
            mock.patch.object(client, "_api_json") as api,
        ):
            with self.assertRaisesRegex(client.SecretsError, "service-token-required"):
                client.fetch_all("/", env, allow_session=True)
        run.assert_not_called()
        api.assert_not_called()


class ExternalShapeTests(unittest.TestCase):
    def response(self, raw):
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = raw
        with mock.patch.object(client, "_opener") as opener:
            opener.return_value.open.return_value = response
            return client._api_json("GET", "/synthetic", "test-only", None, {})

    def test_json_duplicate_nonfinite_deep_and_dense_responses_fail_closed(self):
        invalid = (
            b'{"same":1,"same":2}',
            b'{"value":NaN}',
            b'{"value":Infinity}',
            b'{"value":1e10000}',
            ("[" * 40 + "0" + "]" * 40).encode(),
            json.dumps([None] * 100_001).encode(),
            ("[" * 1500 + "0" + "]" * 1500).encode(),
        )
        for raw in invalid:
            with self.subTest(bytes=len(raw)):
                self.assertEqual(self.response(raw), (200, None))
        self.assertEqual(self.response(b'{"secrets":[]}'), (200, {"secrets": []}))

    def test_pairs_refuses_invalid_names_unsupported_values_and_duplicates(self):
        for payload in (
            {"BAD-NAME": "value"},
            {"KEY\n": "value"},
            {"KEY": None},
            {"KEY": {}},
            {"KEY": []},
            {"KEY": float("nan")},
            [{"key": "KEY", "value": "a"}, {"key": "KEY", "value": "b"}],
        ):
            with self.subTest(type=type(payload).__name__):
                with self.assertRaises(client.SecretsError):
                    client._pairs(payload)
        self.assertEqual(
            client._pairs({"A": True, "B": 3, "C": 1.5}), {"A": "true", "B": "3", "C": "1.5"}
        )

    def test_project_failure_names_only_the_unavailable_declared_sources(self):
        with tempfile.TemporaryDirectory() as home:
            with self.assertRaises(client.SecretsError) as caught:
                client.project_name_for_id("unmatched-project", {"HOME": home})
        message = caught.exception.message
        self.assertIn("project-token-binding-missing", message)
        for name, path in client.PROJECT_SOURCES.values():
            self.assertIn(name, message)
            self.assertIn(path, message)
        self.assertNotIn(home, message)
        self.assertNotIn("unmatched-project", message)


class ImportFailureTests(KnobTestCase):
    def test_broken_client_import_does_not_poison_a_healthy_sibling(self):
        original = builtins.__import__
        for failure in (SyntaxError("private-source-text"), ImportError("private-dependency")):
            self.new_process()

            def load(name, *args, **kwargs):
                if name == "fleet_infisical":
                    raise failure
                return original(name, *args, **kwargs)

            with (
                mock.patch.object(source, "HELPER_LOADER", source._load_helper),
                mock.patch("builtins.__import__", side_effect=load),
            ):
                knobs.resolve("FLEET_AUTOPUSH_MODE")
            self.assertEqual(knobs.report()["fetch_reason"], "scoped-reader-unavailable")
            self.assertFalse(Path(lkg.failure_path()).exists())
            self.fake.set(secrets={"FLEET_AUTOPUSH_MODE": "snapshot"})
            self.new_process(keep_failure_marker=True)
            self.assertEqual(knobs.get("FLEET_AUTOPUSH_MODE"), "snapshot")


class ExpiryDiagnosticTests(unittest.TestCase):
    def test_expiry_reason_survives_reader_writer_and_recall_adapters(self):
        refusal = client.SecretsError("token-expired-or-invalid")
        own = "st." + secrets.token_urlsafe(32)
        with (
            mock.patch.dict(os.environ, {source.ENV_KNOBS_TOKEN: own}, clear=True),
            mock.patch.object(source, "HELPER_LOADER", return_value=client),
            mock.patch.object(client, "fetch_project", side_effect=refusal),
        ):
            with self.assertRaises(source.FetchFailed) as caught:
                source.fetch_named(("FLEET_AUTOPUSH_MODE",), project_id=source.PROJECT_ID, path="/")
        self.assertEqual(caught.exception.reason, "token-expired-or-invalid")
        with mock.patch.object(client, "verify_service_token", side_effect=refusal):
            with self.assertRaisesRegex(setter.Refused, "expiry") as caught:
                setter.verify_writer(own, {})
        self.assertNotIn(own, str(caught.exception))
        settings = InfisicalSettings(
            project_id="synthetic-project", api_base="https://invalid.test"
        )
        with (
            mock.patch.object(settings, "_request", return_value=(200, {})),
            mock.patch.object(client, "validate_service_metadata", side_effect=refusal),
            mock.patch.dict(os.environ, {}, clear=True),
        ):
            with self.assertRaisesRegex(SettingsError, "expiry") as caught:
                settings._verify_service_scope(own)
        self.assertNotIn(own, str(caught.exception))
