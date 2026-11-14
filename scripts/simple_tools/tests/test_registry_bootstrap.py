"""The interactive registry reader cannot inherit a different consumer's grant."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from simple_tools import registry_api as API
from fleet_knobs.tests._fake import ScopedReadAPI
from fleet_knobs.source import PROJECT_ID as SHARED_PROJECT_ID


class RegistryProjectContractTests(unittest.TestCase):
    def test_registry_project_metadata_cannot_drift_from_the_shared_catalog(self):
        # The minimal listener payload excludes knobs; enforce metadata parity in CI.
        self.assertEqual(API.REGISTRY_PROJECT_ID, SHARED_PROJECT_ID)


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
        provider = types.SimpleNamespace(ENV_READ_PATHS="INFISICAL_READ_PATHS",
                                         __file__=str(Path(API.__file__).resolve().parents[1] / "fleet_infisical.py"),
                                         bounded_fetch_project=self.fetch)
        self.modules = mock.patch.dict(sys.modules, {"fleet_infisical": provider,
                                                     "fleet_knobs": None, "fleet_knobs.source": None})
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
        self.assertEqual(args[:2], (API.REGISTRY_PROJECT_ID, "/operator-registry"))
        self.assertEqual(args[2]["INFISICAL_READ_PATHS"], "/operator-registry")
        self.assertEqual(kwargs["token"], os.environ[API.TOKEN_ENV])
        self.assertEqual(set(kwargs["names"]), {API.URL_ENV, API.KEY_VAR})
        self.assertEqual(kwargs["timeout"], 3.0)
        self.assertEqual(kwargs["max_output_bytes"], 8192)

    def test_foreign_preloaded_reader_receives_neither_binding_nor_values(self):
        foreign = types.SimpleNamespace(__file__="/foreign/fleet_infisical.py",
                                        bounded_fetch_project=self.fetch)
        with mock.patch.dict(sys.modules, {"fleet_infisical": foreign}):
            self.assertEqual(API.base_url(), "")
        self.fetch.assert_not_called()

    def test_missing_binding_cannot_fall_back_to_knobs_or_cached_key(self):
        os.environ[API.TOKEN_ENV] = ""
        result = API.call(None, "assign", {})
        self.assertEqual(result["code"], "not_configured")
        self.assertIn(API.TOKEN_ENV, result["error"])
        self.fetch.assert_not_called()

    def test_listener_payload_reads_registry_without_the_knob_package(self):
        source = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "simple_tools"
            package.mkdir()
            (package / "__init__.py").write_text("")
            shutil.copyfile(source / "simple_tools/registry_api.py", package / "registry_api.py")
            shutil.copyfile(source / "fleet_infisical.py", root / "fleet_infisical.py")
            program = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
sys.modules['fleet_knobs'] = None
sys.modules['fleet_knobs.source'] = None
from simple_tools import registry_api as API
config = json.load(sys.stdin)
env = {API.TOKEN_ENV: config['token'], 'FLEET_SECRETS_API': config['api']}
answer = API._direct_values(env)
assert answer[API.URL_ENV] == 'https://registry.example.test'
assert answer[API.KEY_VAR] == 'synthetic-key-' * 4
assert API._direct_values(env) == answer
print('listener-reader-ok')
'''
            with ScopedReadAPI({API.URL_ENV: "https://registry.example.test", API.KEY_VAR: "synthetic-key-" * 4},
                               project=API.REGISTRY_PROJECT_ID, path=API.REGISTRY_PATH) as api:
                result = subprocess.run([sys.executable, "-I", "-B", "-c", program, str(root)],
                                        input=json.dumps({"token": api.token, "api": api.env["FLEET_SECRETS_API"]}),
                                        capture_output=True, text=True, timeout=10, env={"PATH": os.defpath})
                self.assertEqual(len(api.requests), 3, result.stderr)
                self.assertNotIn(api.token, result.stdout + result.stderr)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "listener-reader-ok"), result.stderr)

    def test_provider_failure_is_cached_and_payload_is_not_reported(self):
        self.fetch.side_effect = RuntimeError("provider-secret-must-not-appear")
        for _ in range(2):
            result = API.call(None, "assign", {})
            self.assertEqual(result["code"], "not_configured")
            self.assertNotIn("provider-secret", result["error"])
        self.fetch.assert_called_once()

    def test_missing_required_registry_name_refuses_before_signed_transport(self):
        complete = dict(self.fetch.return_value)
        for name in (API.URL_ENV, API.KEY_VAR):
            with self.subTest(name=name):
                API.reset_knob_cache()
                self.fetch.return_value = {key: value for key, value in complete.items() if key != name}
                with mock.patch.object(API, "_open") as opened:
                    answer = API.call(None, "assign", {})
                self.assertEqual(answer["code"], "not_configured")
                self.assertIn(name, answer["error"])
                opened.assert_not_called()

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
