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
            "INFISICAL_CLIENT_ID": "legacy-id-sentinel",
            "INFISICAL_CLIENT_SECRET": "legacy-secret-sentinel",
        }
        env = dict(self.env(), **credentials, KEEP_CONFIG="retained")
        with patch.object(MODULE, "server_codes", return_value={"TEST"}), patch.object(
            MODULE, "fetch", return_value={"ZULIP_TEST_EMAIL": "fixture@example.test"}
        ):
            child, _ = MODULE.build_env(env)
        self.assertEqual(child["ZULIP_TEST_EMAIL"], "fixture@example.test")
        self.assertEqual(child["KEEP_CONFIG"], "retained")
        self.assertEqual(child["INFISICAL_PROJECT_ID"], PROJECT)
        for name in {"INFISICAL_TOKEN", *credentials}:
            self.assertNotIn(name, child)


if __name__ == "__main__":
    unittest.main()
