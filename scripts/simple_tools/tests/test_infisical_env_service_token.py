from __future__ import annotations

import importlib.util
import unittest
import urllib.parse
from pathlib import Path
from unittest.mock import patch
from uuid import UUID


SPEC = importlib.util.spec_from_file_location(
    "infisical_env_service_token",
    Path(__file__).resolve().parents[1] / "server" / "infisical_env.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)

PROJECT = str(UUID(int=42))
TOKEN = "st.service-token-fixture-value"


def metadata(project=PROJECT, paths=("/zulip", "/operator"), permissions=("read",), expiry="2099-01-01T00:00:00Z"):
    return {
        "id": "synthetic-token-id",
        "name": "synthetic-server-reader",
        "projectId": project,
        "workspace": project,
        "scopes": [{"environment": "prod", "secretPath": path} for path in paths],
        "permissions": list(permissions),
        "expiresAt": expiry,
    }


class InfisicalEnvironmentServiceTokenTests(unittest.TestCase):
    def env(self):
        return {
            "INFISICAL_TOKEN": TOKEN,
            "INFISICAL_PROJECT_ID": PROJECT,
            "INFISICAL_ENVIRONMENT": "prod",
            "INFISICAL_SECRET_PATH": "/zulip",
        }

    def test_valid_multi_folder_primary_grant_is_checked_before_exact_raw_query(self):
        calls = []

        def fake_request(stage, url, **kwargs):
            calls.append((stage, url, kwargs))
            if url.endswith("/v2/service-token"):
                return metadata()
            return {
                "secrets": [{
                    "secretKey": "ZULIP_MA_EMAIL",
                    "secretValue": "fixture@example.test",
                    "type": "shared",
                }],
                "totalCount": 1,
            }

        with patch.object(MODULE, "_request", side_effect=fake_request):
            values = MODULE.fetch(self.env(), expected_paths=("/zulip", "/operator"))
        self.assertEqual(values, {"ZULIP_MA_EMAIL": "fixture@example.test"})
        self.assertEqual([call[0] for call in calls], ["service-token metadata", "read"])
        self.assertEqual(calls[0][2]["max_bytes"], MODULE.MAX_METADATA_BYTES)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(calls[1][1]).query)
        self.assertEqual(query["workspaceId"], [PROJECT])
        self.assertEqual(query["environment"], ["prod"])
        self.assertEqual(query["secretPath"], ["/zulip"])
        self.assertEqual(query["expandSecretReferences"], ["false"])
        self.assertEqual(query["recursive"], ["false"])
        self.assertEqual(query["include_imports"], ["false"])

    def test_wrong_project_path_permission_or_expiry_fails_before_raw_read(self):
        bad_grants = [
            metadata(project="different-project"),
            dict(metadata(), projectId="different-project"),
            dict(metadata(), projectId=None),
            metadata(paths=("/zulip", "/operator", "/extra")),
            metadata(permissions=("read", "write")),
            metadata(expiry="2000-01-01T00:00:00Z"),
            metadata(expiry="2099-01-01T00:00:00"),
            metadata(expiry="invalid"),
            metadata(expiry=123),
        ]
        for grant in bad_grants:
            with self.subTest(grant=grant):
                calls = []

                def fake_request(stage, url, **kwargs):
                    calls.append(url)
                    return grant

                with patch.object(MODULE, "_request", side_effect=fake_request):
                    with self.assertRaises(MODULE.LoadError):
                        MODULE.fetch(self.env(), expected_paths=("/zulip", "/operator"))
                self.assertEqual(len(calls), 1)
                self.assertTrue(calls[0].endswith("/v2/service-token"))

    def test_null_or_absent_expiry_accepts_canonical_workspace_metadata(self):
        for absent in (False, True):
            grant = metadata(expiry=None)
            grant.pop("projectId")
            if absent:
                grant.pop("expiresAt")
            with self.subTest(absent=absent), patch.object(
                MODULE, "_request", side_effect=[grant, {"secrets": []}]
            ) as request:
                self.assertEqual(
                    MODULE.fetch(self.env(), expected_paths=("/zulip", "/operator")), {}
                )
                self.assertEqual(request.call_count, 2)

    def test_returned_record_requires_exact_source_provenance(self):
        calls = []

        def fake_request(stage, url, **kwargs):
            calls.append(url)
            if url.endswith("/v2/service-token"):
                return metadata()
            return {"secrets": [{
                "secretKey": "ZULIP_MA_EMAIL",
                "secretValue": "fixture@example.test",
                "workspace": "other-project",
                "environment": "prod",
                "secretPath": "/zulip",
                "type": "shared",
            }]}

        with patch.object(MODULE, "_request", side_effect=fake_request):
            with self.assertRaisesRegex(MODULE.LoadError, "unverified listing"):
                MODULE.fetch(self.env(), expected_paths=("/zulip", "/operator"))
        self.assertEqual(len(calls), 2)

    def test_present_but_contradictory_record_provenance_is_rejected(self):
        calls = []

        def fake_request(stage, url, **kwargs):
            calls.append(url)
            if url.endswith("/v2/service-token"):
                return metadata()
            return {"secrets": [{
                "secretKey": "ZULIP_MA_EMAIL",
                "secretValue": "fixture@example.test",
                "workspace": PROJECT,
                "environment": "dev",
                "secretPath": "/zulip",
                "type": "shared",
            }]}

        with patch.object(MODULE, "_request", side_effect=fake_request):
            with self.assertRaisesRegex(MODULE.LoadError, "unverified listing"):
                MODULE.fetch(self.env(), expected_paths=("/zulip", "/operator"))
        self.assertEqual(len(calls), 2)

    def test_literal_reference_value_is_rejected_before_loader_receives_it(self):
        def fake_request(stage, url, **kwargs):
            if url.endswith("/v2/service-token"):
                return metadata()
            return {"secrets": [{
                "secretKey": "ZULIP_MA_EMAIL",
                "secretValue": "prefix-${prod.other.SECRET}",
                "type": "shared",
            }]}

        with patch.object(MODULE, "_request", side_effect=fake_request):
            with self.assertRaisesRegex(MODULE.LoadError, "unresolved reference"):
                MODULE.fetch(self.env(), expected_paths=("/zulip", "/operator"))

    def test_child_environment_drops_present_bootstrap_credentials(self):
        credentials = {
            "INFISICAL_LISTENER_READ_TOKEN": "st.synthetic-secondary-token",
            "INFISICAL_AI_FLEET_COORDINATOR_TOKEN": "st.synthetic-other-reader",
            "INFISICAL_WRITE_TOKEN": "st.synthetic-writer-token",
            "INFISICAL_CLIENT_ID": "legacy-id-sentinel",
            "INFISICAL_CLIENT_SECRET": "legacy-secret-sentinel",
            "INFISICAL_UNIVERSAL_AUTH_CLIENT_ID": "universal-id-sentinel",
            "INFISICAL_UNIVERSAL_AUTH_CLIENT_SECRET": "universal-secret-sentinel",
            "INFISICAL_AUTOMATION_CLIENT_ID": "automation-id-sentinel",
            "INFISICAL_AUTOMATION_CLIENT_SECRET": "automation-secret-sentinel",
            "INFISICAL_SHARED_CLIENT_ID": "shared-id-sentinel",
            "INFISICAL_SHARED_CLIENT_SECRET": "shared-secret-sentinel",
        }
        credentials[MODULE.WAKE_ROUTER_TOKEN] = "st.synthetic-wake-router-token"
        env = dict(self.env(), **credentials, KEEP_CONFIG="retained")

        def fetch(source, **kwargs):
            return {
                MODULE.OPERATOR_PATH: {"OPERATOR_ZULIP_USER_ID": "1", "OPERATOR_ZULIP_EMAIL": "op@example.test"},
                MODULE.WAKE_ROUTER_PATH: {"SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY": "r" * 40, "WAKE_ROUTER_KEY": "w" * 40},
            }.get(
                kwargs.get("path"),
                {
                    "ZULIP_TEST_EMAIL": "fixture@example.test",
                    "JET_ROUTINE_URL": "https://jet.example.test/wake/JET",
                    "JET_ROUTINE_KEY": "j" * 40,
                },
            )

        with patch.object(MODULE, "server_codes", return_value={"TEST"}), patch.object(
            MODULE, "fetch", side_effect=fetch
        ):
            child, _ = MODULE.build_env(env)
        self.assertEqual(child["ZULIP_TEST_EMAIL"], "fixture@example.test")
        self.assertEqual(child["KEEP_CONFIG"], "retained")
        self.assertEqual(child["INFISICAL_PROJECT_ID"], PROJECT)
        self.assertEqual(child["INFISICAL_ENVIRONMENT"], "prod")
        for name in {"INFISICAL_TOKEN", *credentials}:
            self.assertNotIn(name, child)

    def test_infisical_only_is_one_table_of_folders_the_loader_reads(self):
        read = {MODULE.ZULIP_PATH, MODULE.OPERATOR_PATH, MODULE.WAKE_ROUTER_PATH}
        exports = [export for export, _ in MODULE.INFISICAL_ONLY]
        self.assertEqual(len(exports), len(set(exports)))
        for export, sources in MODULE.INFISICAL_ONLY:
            with self.subTest(export=export):
                self.assertTrue(sources)
                for folder, name in sources:
                    self.assertIn(folder, read)
                    self.assertRegex(name, r"^[A-Z][A-Z0-9_]*$")
        self.assertEqual(
            dict(MODULE.INFISICAL_ONLY)["OPERATOR_REGISTRY_KEY"][0],
            (MODULE.WAKE_ROUTER_PATH, "SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY"),
        )
        self.assertTrue(MODULE.infisical_only_names().isdisjoint(MODULE.AUTH_ENV_VARS))

    def test_resolver_names_every_missing_name_and_folder_and_never_a_value(self):
        folders = {
            MODULE.ZULIP_PATH: {"JET_ROUTINE_URL": "https://jet.example.test/secret-path"},
            MODULE.OPERATOR_PATH: {"OPERATOR_ZULIP_USER_ID": "1", "OPERATOR_ZULIP_EMAIL": "op@example.test"},
            MODULE.WAKE_ROUTER_PATH: {"SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY": "r" * 40},
        }
        with self.assertRaises(MODULE.LoadError) as caught:
            MODULE.resolve_infisical_only(folders)
        message = str(caught.exception)
        self.assertIn("WAKE_ROUTER_KEY (/wake-router)", message)
        self.assertIn("JET_ROUTINE_KEY (/zulip)", message)
        self.assertNotIn("secret-path", message)
        self.assertNotIn("r" * 40, message)

    def test_resolver_refuses_a_folder_the_loader_did_not_read(self):
        with self.assertRaisesRegex(MODULE.LoadError, "which this loader does not read"):
            MODULE.resolve_infisical_only({MODULE.OPERATOR_PATH: {}, MODULE.WAKE_ROUTER_PATH: {}})

    def test_standalone_auth_inventory_matches_shared_helper(self):
        from fleet_secrets import core

        self.assertEqual(
            MODULE.AUTH_ENV_VARS,
            core.AUTH_ENV_VARS
            | {MODULE.LISTENER_READ_TOKEN, MODULE.OPERATOR_VOICE_TOKEN, MODULE.WAKE_ROUTER_TOKEN},
        )


if __name__ == "__main__":
    unittest.main()
