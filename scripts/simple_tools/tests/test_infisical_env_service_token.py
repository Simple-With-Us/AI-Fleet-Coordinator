from __future__ import annotations

import importlib.util
import re
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
        self.assertEqual(
            [call[0] for call in calls], ["service-token metadata (INFISICAL_TOKEN)", "read"]
        )
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
                {"ZULIP_TEST_EMAIL": "fixture@example.test"},
            )

        with patch.object(MODULE, "server_codes", return_value={"TEST"}), patch.object(
            MODULE, "fetch", side_effect=fetch
        ):
            child, _ = MODULE.build_env(env)
        self.assertEqual(child["ZULIP_TEST_EMAIL"], "fixture@example.test")
        self.assertEqual(child["KEEP_CONFIG"], "retained")
        self.assertEqual(child["INFISICAL_PROJECT_ID"], PROJECT)
        for name in {"INFISICAL_TOKEN", *credentials}:
            self.assertNotIn(name, child)

    def test_infisical_only_is_one_table_with_one_source_a_row_and_no_seat(self):
        read = {MODULE.ZULIP_PATH, MODULE.OPERATOR_PATH, MODULE.WAKE_ROUTER_PATH}
        exports = [row[0] for row in MODULE.INFISICAL_ONLY]
        self.assertEqual(len(exports), len(set(exports)))
        for export, folder, name, pattern in MODULE.INFISICAL_ONLY:
            with self.subTest(export=export):
                self.assertIn(folder, read)
                self.assertRegex(name, r"^[A-Z][A-Z0-9_]*$")
                self.assertFalse(MODULE.is_seat_name(export), "a per-seat name never stops every seat")
                self.assertFalse(MODULE.is_seat_name(name))
                re.compile(pattern)
        rows = {row[0]: row[1:] for row in MODULE.INFISICAL_ONLY}
        self.assertEqual(
            rows["OPERATOR_REGISTRY_KEY"][:2],
            (MODULE.WAKE_ROUTER_PATH, "SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY"),
        )
        self.assertEqual(rows["WAKE_ROUTER_KEY"][:2], ("/wake-router", "WAKE_ROUTER_KEY"), "one bearer, one home, no seat")
        self.assertTrue(MODULE.infisical_only_names().isdisjoint(MODULE.AUTH_ENV_VARS))

    def test_the_loader_sources_own_disjoint_names(self):
        exports = {row[0] for row in MODULE.INFISICAL_ONLY}
        groups = [exports, set(MODULE.OPERATOR_NAMES), set(MODULE.OPERATOR_VOICE_NAMES),
                  MODULE.allowed_names(MODULE.server_codes(MODULE.PARTITION_DEFAULT))]
        for i, a in enumerate(groups):
            for b in groups[i + 1:]:
                self.assertTrue(a.isdisjoint(b), sorted(a & b))

    def test_the_registry_key_pattern_is_the_knob_registry_pattern(self):
        from fleet_knobs import registry

        rows = {row[0]: row for row in MODULE.INFISICAL_ONLY}
        spec = registry.BY_NAME["SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY"]
        self.assertEqual("^" + rows["OPERATOR_REGISTRY_KEY"][3] + "$", spec.pattern)

    def test_resolver_names_every_missing_or_unusable_name_and_folder_and_never_a_value(self):
        folders = {
            MODULE.ZULIP_PATH: {},
            MODULE.OPERATOR_PATH: {"OPERATOR_ZULIP_USER_ID": "not-a-number", "OPERATOR_ZULIP_EMAIL": "op@example.test"},
            MODULE.WAKE_ROUTER_PATH: {"SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY": "short-secret-sentinel"},
        }
        with self.assertRaises(MODULE.LoadError) as caught:
            MODULE.resolve_infisical_only(folders)
        message = str(caught.exception)
        self.assertIn(
            "the Infisical value for OPERATOR_ZULIP_USER_ID (/operator), "
            "OPERATOR_REGISTRY_KEY (/wake-router SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY) is not usable",
            message,
        )
        self.assertNotIn("short-secret-sentinel", message)
        self.assertNotIn("not-a-number", message)
        folders[MODULE.OPERATOR_PATH] = {}
        with self.assertRaisesRegex(
            MODULE.LoadError, r"Infisical is missing OPERATOR_ZULIP_USER_ID \(/operator\), OPERATOR_ZULIP_EMAIL"
        ):
            MODULE.resolve_infisical_only(folders)

    def test_resolver_refuses_a_folder_the_loader_did_not_read(self):
        with self.assertRaisesRegex(MODULE.LoadError, "which this loader does not read"):
            MODULE.resolve_infisical_only({MODULE.OPERATOR_PATH: {}})

    def test_every_metadata_error_names_the_token_variable(self):
        def refuse(stage, url, **kwargs):
            raise MODULE.LoadError("%s: HTTP 401" % stage)

        for name in (MODULE.TOKEN, MODULE.WAKE_ROUTER_TOKEN, MODULE.LISTENER_READ_TOKEN, MODULE.OPERATOR_VOICE_TOKEN):
            with self.subTest(name=name), patch.object(MODULE, "_request", side_effect=refuse):
                with self.assertRaisesRegex(MODULE.LoadError, r"^service-token metadata \(%s\): HTTP 401$" % name):
                    MODULE.fetch(self.env(), token=TOKEN, token_name=name, expected_paths=("/zulip", "/operator"))
        with patch.object(MODULE, "_request", side_effect=[metadata(project="different-project")]):
            with self.assertRaisesRegex(
                MODULE.LoadError, r"service-token metadata \(EAVESDROPPER_WAKE_ROUTER_INFISICAL_TOKEN\): project"
            ):
                MODULE.fetch(
                    self.env(), token=TOKEN, token_name=MODULE.WAKE_ROUTER_TOKEN, expected_paths=("/zulip", "/operator")
                )

    def test_standalone_auth_inventory_matches_shared_helper(self):
        from fleet_secrets import core

        self.assertEqual(
            MODULE.AUTH_ENV_VARS,
            core.AUTH_ENV_VARS
            | {MODULE.LISTENER_READ_TOKEN, MODULE.OPERATOR_VOICE_TOKEN, MODULE.WAKE_ROUTER_TOKEN},
        )


if __name__ == "__main__":
    unittest.main()
