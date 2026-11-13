"""The interactive registry reader cannot inherit a different consumer's grant."""

import os
import sys
import types
import unittest
from unittest import mock

from simple_tools import registry_api as API


class DirectRegistryTests(unittest.TestCase):
    def setUp(self):
        API.reset_knob_cache()
        self.addCleanup(API.reset_knob_cache)
        self.env = mock.patch.dict(os.environ, {
            API.TOKEN_ENV: "st." + "synthetic-registry-" * 3,
            "INFISICAL_READ_PATHS": "/wrong-consumer",
            "FLEET_KNOBS_INFISICAL_TOKEN": "never-borrow-this",
            API.KEY_VAR: "never-use-cached-key",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.fetch = mock.Mock(return_value={API.URL_ENV: "https://registry.example.test",
                                            API.KEY_VAR: "synthetic-signing-key-" * 3})
        provider = types.SimpleNamespace(ENV_READ_PATHS="INFISICAL_READ_PATHS", fetch_project=self.fetch)
        source = types.SimpleNamespace(PROJECT_ID="synthetic-project")
        self.modules = mock.patch.dict(sys.modules, {"fleet_infisical": provider,
                                                     "fleet_knobs.source": source})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.knob = mock.patch.object(API, "_knob_value", side_effect=AssertionError("wrong consumer"))
        self.knob.start()
        self.addCleanup(self.knob.stop)

    def test_exact_two_named_reads_share_short_deadline_and_process_cache(self):
        self.assertEqual(API.base_url(), "https://registry.example.test")
        self.assertTrue(API.signing_key().startswith("synthetic-signing-key"))
        self.fetch.assert_called_once()
        args, kwargs = self.fetch.call_args
        self.assertEqual(args[:2], ("synthetic-project", "/operator-registry"))
        self.assertEqual(args[2]["INFISICAL_READ_PATHS"], "/operator-registry")
        self.assertEqual(kwargs["token"], os.environ[API.TOKEN_ENV])
        self.assertEqual(set(kwargs["names"]), {API.URL_ENV, API.KEY_VAR})
        self.assertEqual(kwargs["timeout"], 3.0)

    def test_missing_binding_cannot_fall_back_to_knobs_or_cached_key(self):
        os.environ[API.TOKEN_ENV] = ""
        result = API.call(None, "assign", {})
        self.assertEqual(result["code"], "not_configured")
        self.assertIn(API.TOKEN_ENV, result["error"])
        self.fetch.assert_not_called()

    def test_provider_failure_is_cached_and_payload_is_not_reported(self):
        self.fetch.side_effect = RuntimeError("provider-secret-must-not-appear")
        for _ in range(2):
            result = API.call(None, "assign", {})
            self.assertEqual(result["code"], "not_configured")
            self.assertNotIn("provider-secret", result["error"])
        self.fetch.assert_called_once()

    def test_failed_dedicated_route_never_uses_inherited_url_and_signing_key(self):
        os.environ[API.URL_ENV] = "https://inherited.example.test"
        os.environ[API.KEY_VAR] = "inherited-signing-key-" * 3
        self.fetch.side_effect = RuntimeError("provider-secret-must-not-appear")
        for token in ("", "st." + "synthetic-registry-" * 3):
            with self.subTest(token_present=bool(token)):
                API.reset_knob_cache()
                os.environ[API.TOKEN_ENV] = token
                with mock.patch.object(API, "_open") as opened:
                    result = API.call(None, "assign", {})
                self.assertEqual(result["code"], "not_configured")
                self.assertIn(API.TOKEN_ENV, result["error"])
                self.assertNotIn("provider-secret", result["error"])
                self.assertEqual(API.base_url(), "")
                self.assertEqual(API.signing_key(), "")
                opened.assert_not_called()

    def test_rotation_and_api_origin_change_invalidate_process_cache(self):
        API.base_url()
        os.environ[API.TOKEN_ENV] += "rotated"
        API.base_url()
        os.environ["FLEET_SECRETS_API"] = "https://other.example.test/api"
        API.base_url()
        self.assertEqual(self.fetch.call_count, 3)

    def test_explicit_mapping_never_uses_live_reader(self):
        self.assertEqual(API.base_url({API.URL_ENV: "http://localhost:1234"}), "http://localhost:1234")
        self.fetch.assert_not_called()

    def test_malformed_endpoint_or_key_is_refused_before_signing(self):
        for url, key in (("https://user:password@example.test", "k" * 32),
                         ("https://example.test/other", "k" * 32),
                         ("http://public.example.test", "k" * 32),
                         ("https://example.test", "short")):
            API.reset_knob_cache()
            self.fetch.return_value = {API.URL_ENV: url, API.KEY_VAR: key}
            result = API.call(None, "assign", {})
            self.assertEqual(result["code"], "not_configured")
