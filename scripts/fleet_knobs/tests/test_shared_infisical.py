"""The shared client works without the legacy application and preserves the verified scope boundary."""

import json
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import fleet_infisical

from fleet_knobs.tests._fake import ScopedReadAPI


class SharedClientTests(unittest.TestCase):
    def test_missing_shared_client_keeps_import_safe_and_returns_process_local_reason(self):
        scripts = str(Path(__file__).resolve().parents[2])
        boot = """
import sys
sys.path.insert(0, sys.argv[1])
sys.modules['fleet_infisical'] = None
from fleet_knobs import source
try:
    source.fetch({'OPERATOR_API_URL'})
except source.FetchFailed as exc:
    print(exc.reason)
else:
    raise AssertionError('missing client allowed a read')
"""
        with tempfile.TemporaryDirectory() as home:
            proc = subprocess.run(
                [sys.executable, "-I", "-B", "-c", boot, scripts],
                env={"HOME": home, "PATH": "/unavailable"},
                cwd=home,
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "scoped-reader-unavailable")

    def test_listener_image_includes_shared_client_in_payload_and_build_context(self):
        server = Path(__file__).resolve().parents[2] / "simple_tools" / "server"
        self.assertIn(
            "COPY scripts/fleet_infisical.py /app/scripts/",
            (server / "Dockerfile").read_text().splitlines(),
        )
        self.assertIn(
            "!scripts/fleet_infisical.py",
            (server / "Dockerfile.dockerignore").read_text().splitlines(),
        )

    def test_legacy_import_is_an_alias_to_one_client(self):
        from fleet_secrets import core

        self.assertIs(core, fleet_infisical)
        self.assertIs(core.SecretsError, fleet_infisical.SecretsError)

    def test_real_scoped_read_runs_without_legacy_application_or_executable(self):
        scripts = str(Path(__file__).resolve().parents[2])
        boot = """
import json,sys
sys.path.insert(0, sys.argv[1])
sys.modules['fleet_secrets'] = None
sys.modules['fleet_secrets.core'] = None
from fleet_knobs import source
import fleet_infisical
assert source._load_helper() is fleet_infisical
result = source.fetch({'OPERATOR_API_URL'})
assert result == {'OPERATOR_API_URL': 'https://operator.invalid'}
print(json.dumps(sorted(result)))
"""
        with (
            tempfile.TemporaryDirectory() as home,
            ScopedReadAPI({"OPERATOR_API_URL": "https://operator.invalid"}) as api,
        ):
            env = {"HOME": home, "PATH": "/unavailable", **api.env}
            for wrong_scope in (False, True):
                with self.subTest(wrong_scope=wrong_scope):
                    api.requests.clear()
                    if wrong_scope:
                        api.metadata_override["permissions"] = ["read", "write"]
                    proc = subprocess.run(
                        [sys.executable, "-I", "-B", "-c", boot, scripts],
                        env=env,
                        cwd=home,
                        capture_output=True,
                        text=True,
                        timeout=10,
                    )
                    if wrong_scope:
                        self.assertNotEqual(proc.returncode, 0)
                        self.assertEqual(api.requests, ["/api/v2/service-token"])
                    else:
                        self.assertEqual(proc.returncode, 0, proc.stderr)
                        self.assertEqual(
                            json.loads(proc.stdout),
                            ["OPERATOR_API_URL"],
                        )
                        self.assertEqual(len(api.requests), 2)
                    self.assertNotIn(api.token, proc.stdout + proc.stderr)


class NamesOnlyTests(unittest.TestCase):
    def setUp(self):
        self.project, self.path = "fixture-project", "/zulip"
        self.token = "st." + "fixture" * 8
        self.env = {}
        self.metadata = {
            "workspace": self.project,
            "scopes": [{"environment": "prod", "secretPath": self.path}],
            "permissions": ["read"],
            "expiresAt": "2099-01-01T00:00:00Z",
        }
        self.records = [self.record("B_KEY"), self.record("A_KEY")]

    def record(self, name, **extra):
        return {
            "secretKey": name,
            "secretValueHidden": True,
            "secretValue": fleet_infisical.HIDDEN_VALUE_MASK,
            "workspace": self.project,
            "environment": "prod",
            "secretPath": self.path,
            "type": "shared",
            **extra,
        }

    def listing(self, responses=None):
        responses = responses or [(200, self.metadata), (200, {"secrets": self.records})]
        with mock.patch.object(fleet_infisical, "_api_json", side_effect=responses) as api:
            result = fleet_infisical.list_project_names(
                self.project, self.path, self.env, token=self.token, timeout=2.0
            )
        return result, api.call_args_list

    def test_names_only_query_shares_transport_provenance_and_one_deadline(self):
        result, calls = self.listing()
        self.assertEqual(result, ("A_KEY", "B_KEY"))
        self.assertEqual(calls[0].args[:2], ("GET", "/v2/service-token"))
        parsed = urllib.parse.urlsplit(calls[1].args[1])
        self.assertEqual(parsed.path, "/v3/secrets/raw")
        self.assertEqual(
            urllib.parse.parse_qs(parsed.query),
            {
                "workspaceId": [self.project],
                "environment": ["prod"],
                "secretPath": [self.path],
                "viewSecretValue": ["false"],
                "expandSecretReferences": ["false"],
                "recursive": ["false"],
                "include_imports": ["false"],
            },
        )
        self.assertEqual(calls[0].kwargs["deadline"], calls[1].kwargs["deadline"])
        self.assertNotIn("secretValue", repr(result))

    def test_values_duplicates_and_foreign_record_provenance_fail_closed(self):
        for change in (
            {"secretValue": "unexpected-value"},
            {"secretValueHidden": False},
            {"secretValueHidden": 1},
            {"secretValueHidden": "true"},
            {"secretValueHidden": None},
            {"workspace": "foreign-project"},
            {"environment": "dev"},
            {"secretPath": "/other"},
            {"type": "personal"},
            {"secretKey": "bad name"},
            {"secretKey": "A_KEY"},
        ):
            with self.subTest(change=change):
                self.records[0] = self.record("B_KEY", **change)
                with self.assertRaises(fleet_infisical.SecretsError) as caught:
                    self.listing()
                self.assertNotIn("unexpected-value", str(caught.exception))

    def test_absent_or_empty_values_need_no_mask(self):
        for value in (None, ""):
            with self.subTest(value=value):
                self.records[0] = self.record("B_KEY", secretValue=value)
                self.assertEqual(self.listing()[0], ("A_KEY", "B_KEY"))

    def test_ambient_multifolder_and_writer_grants_never_reach_values(self):
        for wider in ("folder", "writer"):
            with self.subTest(wider=wider):
                metadata = dict(self.metadata)
                self.env[fleet_infisical.ENV_READ_PATHS] = "/zulip,/other"
                if wider == "folder":
                    metadata["scopes"] = self.metadata["scopes"] + [
                        {"environment": "prod", "secretPath": "/other"}
                    ]
                else:
                    metadata["permissions"] = ["read", "write"]
                with (
                    mock.patch.object(
                        fleet_infisical, "_api_json", return_value=(200, metadata)
                    ) as api,
                    self.assertRaises(fleet_infisical.SecretsError),
                ):
                    fleet_infisical.list_project_names(
                        self.project, self.path, self.env, token=self.token
                    )
                self.assertEqual(api.call_count, 1)

    def test_bad_binding_and_nonprod_fail_before_network(self):
        for token, env in (("invalid", {}), (self.token, {"INFISICAL_ENVIRONMENT": "dev"})):
            with self.subTest(binding=token == self.token):
                with mock.patch.object(fleet_infisical, "_api_json") as api:
                    with self.assertRaises(fleet_infisical.SecretsError):
                        fleet_infisical.list_project_names(
                            self.project, self.path, env, token=token
                        )
                api.assert_not_called()

    def test_every_route_requires_future_timezone_aware_expiry_before_values(self):
        invalid = (
            None,
            "",
            False,
            1,
            "invalid-expiry",
            "Infinity",
            "2000-01-01T00:00:00Z",
            "2099-01-01T00:00:00",
        )
        for route in ("names", "reader", "writer"):
            for expiry in (*invalid, "missing"):
                with self.subTest(route=route, expiry=expiry):
                    metadata = dict(self.metadata, expiresAt=expiry)
                    if expiry == "missing":
                        metadata.pop("expiresAt")
                    if route == "writer":
                        metadata["permissions"] = ["read", "write"]
                    with (
                        mock.patch.object(
                            fleet_infisical, "_api_json", return_value=(200, metadata)
                        ) as api,
                        self.assertRaisesRegex(
                            fleet_infisical.SecretsError, "token-expired-or-invalid"
                        ),
                    ):
                        if route == "names":
                            fleet_infisical.list_project_names(
                                self.project, self.path, self.env, token=self.token
                            )
                        else:
                            fleet_infisical.fetch_project(
                                self.project,
                                self.path,
                                self.env,
                                token=self.token,
                                writer=route == "writer",
                                names=("A_KEY",),
                            )
                    self.assertEqual(api.call_count, 1)


if __name__ == "__main__":
    unittest.main()
