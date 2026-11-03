"""Offline routing tests; all network, subprocesses and credential stores are faked."""

import io
import os
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from fleet_knobs import source as knobs
from fleet_rag import core as rag
from fleet_rag import infisical_settings as settings

from fleet_secrets import cli, core

PROJECT = "00000000-0000-4000-8000-000000000001"
OTHER = "00000000-0000-4000-8000-000000000002"
READ = "st.synthetic-read-secret-token"
COORDINATOR_READ = "st.synthetic-coordinator-read-token"
WRITE = "st.synthetic-write-secret-token"
VALUE = "synthetic-provider-secret-value"


def grant(project=PROJECT, paths=("/",), permissions=("read",)):
    return {
        "workspace": project,
        "permissions": list(permissions),
        "scopes": [{"environment": "prod", "secretPath": path} for path in paths],
        "expiresAt": None,
    }


class ScopedTokens(unittest.TestCase):
    def setUp(self):
        self.env = {
            core.ENV_TOKEN: READ,
            core.ENV_AI_FLEET_COORDINATOR_TOKEN: COORDINATOR_READ,
            core.ENV_WRITE_TOKEN: WRITE,
            core.ENV_PROJECT: PROJECT,
            core.PROJECT_SOURCES["ai-fleet-coordinator"][0]: OTHER,
        }
        self.metadata = {
            READ: grant(),
            COORDINATOR_READ: grant(OTHER),
            WRITE: grant(permissions=("read", "write")),
        }
        self.calls = []
        self.records = [
            {
                "secretKey": "ALPHA",
                "secretValue": VALUE,
                "type": "shared",
                "workspace": PROJECT,
                "environment": "prod",
                "secretPath": "/",
            }
        ]
        self.api = mock.patch.object(core, "_api_json", self.respond)
        self.api.start()
        self.addCleanup(self.api.stop)
        for target in ("subprocess.run", "os.open"):
            patch = mock.patch(
                "fleet_secrets.core." + target,
                side_effect=AssertionError("credential store or login used"),
            )
            patch.start()
            self.addCleanup(patch.stop)

    def respond(self, method, path, token, body, env, **kwargs):
        self.calls.append((method, path, token, body, kwargs))
        if path == "/v2/service-token":
            return 200, self.metadata[token]
        if method in ("PATCH", "POST"):
            return 200, {"secret": dict(self.records[0], secretValue=body["secretValue"])}
        if "/raw/" in path:
            name = urlparse(path).path.rsplit("/", 1)[-1]
            matching = next((row for row in self.records if row["secretKey"] == name), None)
            return (200, {"secret": matching}) if matching else (404, {})
        return 200, {"secrets": self.records}

    def test_reader_metadata_precedes_bounded_v3_request_without_expansion(self):
        self.assertEqual(core.fetch_all("/", self.env), {"ALPHA": VALUE})
        self.assertEqual(
            [call[1].split("?")[0] for call in self.calls], ["/v2/service-token", "/v3/secrets/raw"]
        )
        query = parse_qs(urlparse(self.calls[1][1]).query)
        self.assertEqual(query["workspaceId"], [PROJECT])
        for name in ("expandSecretReferences", "recursive", "include_imports"):
            self.assertEqual(query[name], ["false"])
        self.assertTrue(all(call[2] == READ and call[4]["max_bytes"] > 0 for call in self.calls))

    def test_wrong_project_never_reads_or_falls_back(self):
        with self.assertRaisesRegex(core.SecretsError, "scope-mismatch"):
            core.fetch_all("/fleetlink", self.env, "ai-fleet-coordinator")
        self.assertEqual(len(self.calls), 1)

    def test_project_selector_uses_its_bound_token(self):
        self.metadata[COORDINATOR_READ] = grant(OTHER, paths=("/fleetlink",))
        self.records = [dict(self.records[0], workspace=OTHER, secretPath="/fleetlink")]
        result = core.fetch_all("/fleetlink", self.env, "ai-fleet-coordinator")
        self.assertEqual(result, {"ALPHA": VALUE})
        self.assertEqual(self.calls[0][2], COORDINATOR_READ)
        self.assertEqual(self.calls[1][2], COORDINATOR_READ)

    def test_broader_permissions_scopes_wildcards_expiry_and_duplicates_fail_closed(self):
        bad = [
            grant(permissions=("read", "write")),
            grant(paths=("/", "/extra")),
            grant(paths=("/**",)),
            grant(paths=("/", "/")),
            dict(grant(), expiresAt="2020-01-01T00:00:00Z"),
            dict(grant(), expiresAt="invalid"),
            dict(grant(), permissions=["read", "read"]),
        ]
        changed_environment = grant()
        changed_environment["scopes"][0]["environment"] = "dev"
        bad.append(changed_environment)
        bad.append(dict(grant(), projectId=OTHER))
        for metadata in bad:
            with self.subTest(metadata=metadata):
                self.metadata[READ] = metadata
                self.calls.clear()
                with self.assertRaises(core.SecretsError):
                    core.fetch_all("/", self.env)
                self.assertEqual(len(self.calls), 1)

    def test_multiple_exact_folders_require_explicit_declaration(self):
        self.metadata[READ] = grant(paths=("/", "/zulip"))
        with self.assertRaises(core.SecretsError):
            core.fetch_all("/", self.env)
        self.env[core.ENV_READ_PATHS] = "/,/zulip"
        self.assertEqual(core.fetch_all("/", self.env), {"ALPHA": VALUE})
        for path in ("/zulip/child", "/**", "/./zulip"):
            with self.assertRaises(core.SecretsError):
                core.fetch_project(PROJECT, path, self.env)

    def test_writer_is_distinct_and_allowlist_precedes_metadata_or_values(self):
        self.metadata[WRITE] = grant(paths=("/linear",), permissions=("read", "write"))
        self.assertEqual(core.put_secret("CACHE_CODEX", "/linear", VALUE, self.env), "updated")
        self.assertTrue(all(call[2] == WRITE for call in self.calls))
        self.calls.clear()
        with self.assertRaises(core.SecretsError):
            core.put_secret("ARBITRARY_SECRET", "/linear", VALUE, self.env)
        self.assertEqual(self.calls, [])

    def test_writer_never_borrows_reader_and_readonly_writer_is_rejected(self):
        self.env.pop(core.ENV_WRITE_TOKEN)
        with self.assertRaisesRegex(core.SecretsError, "INFISICAL_WRITE_TOKEN"):
            core.put_secret("CACHE_CODEX", "/linear", VALUE, self.env)
        self.assertEqual(self.calls, [])
        self.env[core.ENV_WRITE_TOKEN] = READ
        self.metadata[READ] = grant(paths=("/linear",))
        with self.assertRaises(core.SecretsError):
            core.put_secret("CACHE_CODEX", "/linear", VALUE, self.env)
        self.assertEqual(len(self.calls), 1)

    def test_bad_optional_writer_binding_does_not_disable_valid_reader(self):
        self.env[core.ENV_WRITE_TOKEN] = "st.malformed-writer-binding"
        self.assertEqual(core.fetch_all("/", self.env), {"ALPHA": VALUE})
        self.assertTrue(self.calls)
        self.assertTrue(all(call[2] == READ for call in self.calls))

    def test_mi_and_session_credentials_are_ignored_without_token(self):
        env = {
            core.ENV_PROJECT: PROJECT,
            core.ENV_AUTOMATION_ID: VALUE,
            core.ENV_AUTOMATION_SECRET: VALUE,
            "INFISICAL_SHARED_CLIENT_SECRET": VALUE,
        }
        with self.assertRaisesRegex(core.SecretsError, "service-token-required"):
            core.fetch_all("/", env)
        self.assertEqual(self.calls, [])

    def test_personal_wrong_folder_duplicate_hidden_and_references_are_rejected(self):
        for patch in (
            {"type": "personal"},
            {"secretPath": "/other"},
            {"secretValueHidden": True},
            {"secretValue": "prefix ${prod.other.SECRET}"},
        ):
            self.records = [dict(self.records[0], **patch)]
            with self.assertRaises(core.SecretsError):
                core.fetch_all("/", self.env)
            self.records = [{"secretKey": "ALPHA", "secretValue": VALUE, "type": "shared"}]
        self.records.append(dict(self.records[0]))
        with self.assertRaises(core.SecretsError):
            core.fetch_all("/", self.env)

    def test_errors_and_cli_metadata_never_include_tokens_or_secret_values(self):
        self.metadata[READ] = {"message": VALUE, "secretValue": READ}
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["get", "ALPHA"], env=self.env, out=out, err=err)
        self.assertEqual(code, 4)
        for secret in (READ, WRITE, VALUE):
            self.assertNotIn(secret, out.getvalue() + err.getvalue())

    def test_run_removes_both_bootstrap_tokens_and_rejects_reserved_injection(self):
        child = []
        with mock.patch.object(cli.os, "execvpe", side_effect=lambda *args: child.append(args[2])):
            cli.main(["run", "--only", "ALPHA", "--", "synthetic-command"], env=self.env)
        self.assertEqual(child[0]["ALPHA"], VALUE)
        self.assertNotIn(core.ENV_TOKEN, child[0])
        self.assertNotIn(core.ENV_WRITE_TOKEN, child[0])

    def test_knobs_named_read_preserves_project_folder_names_and_budget(self):
        env = {**self.env, knobs.ENV_KNOBS_TOKEN: COORDINATOR_READ}
        env.pop(core.ENV_TOKEN, None)
        env.pop(core.ENV_WRITE_TOKEN, None)
        self.records = [dict(self.records[0], workspace=OTHER)]
        with (
            mock.patch.object(knobs, "HELPER_LOADER", return_value=core),
            mock.patch.dict(os.environ, env, clear=True),
        ):
            self.assertEqual(
                knobs.fetch_named(["ALPHA"], project_id=OTHER, path="/"), {"ALPHA": VALUE}
            )
        self.assertIn("/raw/ALPHA?", self.calls[-1][1])
        self.assertIsNotNone(self.calls[-1][4]["deadline"])

    def test_knobs_never_borrows_generic_or_writer_token(self):
        env = {core.ENV_TOKEN: READ, core.ENV_WRITE_TOKEN: WRITE}
        with (
            mock.patch.object(knobs, "HELPER_LOADER", return_value=core),
            mock.patch.dict(os.environ, env, clear=True),
            self.assertRaises(knobs.FetchFailed),
        ):
            knobs.fetch_named(["ALPHA"], project_id=PROJECT, path="/")
        self.assertEqual(self.calls, [])

    def test_rag_load_rejects_foreign_binding_instead_of_fleet_secrets_fallback(self):
        env = dict(self.env, FLEET_RAG_INFISICAL_PROJECT=OTHER, FLEET_RECALL_INFISICAL_TOKEN=READ)
        env.pop(core.ENV_TOKEN)
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(
                rag, "fleet_secrets_many", side_effect=AssertionError("foreign fallback")
            ),
            self.assertRaises(rag.FleetRagError),
        ):
            rag.load_config()
        self.assertEqual(len(self.calls), 1)

    def test_settings_default_never_reads_shared_identity_file(self):
        env = {
            "INFISICAL_AUTOMATION_CLIENT_ID": VALUE,
            "INFISICAL_AUTOMATION_CLIENT_SECRET": VALUE,
            "INFISICAL_MACHINE_IDENTITY": "/synthetic/never-read",
        }
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(Path, "read_text", side_effect=AssertionError("credential read")),
        ):
            service = settings.InfisicalSettings(project_id=PROJECT)
            self.assertEqual(service._credential(), (None, ""))
            configured = settings.InfisicalSettings(
                project_id=PROJECT, identity_prefixes=("INFISICAL_AUTOMATION",)
            )
            self.assertEqual(configured._credential(), (None, ""))

    def test_settings_read_token_without_writer_cannot_fall_back_to_explicit_mi(self):
        env = {"INFISICAL_TOKEN": READ, "OWN_APP_CLIENT_ID": "id", "OWN_APP_CLIENT_SECRET": VALUE}
        with mock.patch.dict(os.environ, env, clear=True):
            service = settings.InfisicalSettings(project_id=PROJECT, identity_prefixes=("OWN_APP",))
            self.assertEqual(service._credential(writer=True), (None, ""))


if __name__ == "__main__":
    unittest.main()
