import importlib.util
import json
import os
import tempfile
import textwrap
import unittest
import urllib.parse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "wise_relay", Path(__file__).with_name("wise-relay-exec.py")
)
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


class AdapterTests(unittest.TestCase):
    def config(self):
        return {
            "WISE_GIT_RELAY_BEARER": "sbt_fake",
            "WISE_GIT_RELAY_URL": "https://relay.example.test",
            "WISE_GIT_RELAY_REPOSITORY": w.REPO,
            "WISE_GIT_RELAY_EXPECTED_ACTOR": w.ACTOR,
            "WISE_GIT_RELAY_PERMISSIONS": json.dumps(w.PERMISSIONS),
        }

    def test_config_map(self):
        self.assertEqual(w.configuration(self.config()), self.config())

    def test_config_list(self):
        self.assertEqual(
            w.configuration([{"key": k, "value": v} for k, v in self.config().items()]),
            self.config(),
        )

    def test_duplicate(self):
        with self.assertRaises(w.Refused):
            w.configuration([{"key": "x", "value": "a"}, {"key": "x", "value": "b"}])

    def test_config_requires_exact_keys(self):
        for extra in [True, False]:
            c = self.config()
            if extra:
                c["X"] = "x"
            else:
                c.pop("WISE_GIT_RELAY_BEARER")
            with self.assertRaises(w.Refused):
                w.configuration(c)

    def test_repo_actor_perms(self):
        for k, v in [
            ("WISE_GIT_RELAY_REPOSITORY", "other/Repo"),
            ("WISE_GIT_RELAY_EXPECTED_ACTOR", "jaywedgeworth22"),
            ("WISE_GIT_RELAY_PERMISSIONS", "{}"),
        ]:
            c = self.config()
            c[k] = v
            with self.assertRaises(w.Refused):
                w.configuration(c)

    def test_relay_origin(self):
        for u in [
            "http://relay.test",
            "https://x@y.test",
            "https://y.test/other",
            "https://y.test/?token=secret",
        ]:
            c = self.config()
            c["WISE_GIT_RELAY_URL"] = u
            with self.assertRaises(w.Refused):
                w.configuration(c)

    def run_mint(self, login=w.ACTOR, ttl=3590, slug="swu-wise", token="ghs_fake"):
        calls = []

        def request(url, headers, body):
            calls.append((url, headers, body))
            if len(calls) == 1:
                return {"app_slug": slug, "token": token, "expires_in_seconds": ttl}
            return {"data": {"viewer": {"login": login}}}

        return w.mint_verified(self.config(), request), calls

    def test_mint_verified(self):
        token, calls = self.run_mint()
        self.assertEqual(token, "ghs_fake")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][2], {"repo": w.REPO, "permissions": w.PERMISSIONS})
        self.assertEqual(calls[1][0], "https://api.github.com/graphql")
        self.assertNotIn("sbt_fake", str(calls[1]))

    def test_no_personal_or_echo_principal(self):
        for name in ["jaywedgeworth22", "swu-echo[bot]", "github-actions[bot]", None]:
            with self.assertRaises(w.Refused):
                self.run_mint(login=name)

    def test_token_bounds(self):
        for kwargs in [
            {"ttl": 1},
            {"ttl": 3601},
            {"ttl": True},
            {"slug": "swu-echo"},
            {"token": "ghp_fake"},
        ]:
            with self.assertRaises(w.Refused):
                self.run_mint(**kwargs)

    def test_null_and_malformed_viewer_refused(self):
        for response in [
            None,
            {},
            {"data": None},
            {"data": []},
            {"data": {"viewer": None}},
            {"data": {"viewer": []}},
            {"errors": ["denied"]},
        ]:
            calls = []

            def request(url, headers, body):
                calls.append(url)
                if len(calls) == 1:
                    return {"app_slug": "swu-wise", "token": "ghs_fake", "expires_in_seconds": 3590}
                return response

            with self.assertRaises(w.Refused):
                w.mint_verified(self.config(), request)

    def test_source_keeps_secret_in_memory(self):
        source = Path(w.__file__).read_text()
        self.assertNotIn("GITHUB_ENV", source)
        self.assertNotIn("--github-env", source)
        self.assertNotIn("--projectId", source)
        self.assertIn("fetch_project", source)

    def test_live_workflow_uses_the_same_scoped_token_contract(self):
        workflow = (
            Path(__file__).resolve().parent.parent / ".github/workflows/auto-merge-reusable.yml"
        )
        source = workflow.read_text()
        self.assertIn(
            "WISE_RELAY_INFISICAL_TOKEN: ${{ secrets.WISE_RELAY_INFISICAL_TOKEN || secrets.wise_infisical_token }}",
            source,
        )
        self.assertIn("def fetch_infisical_configuration(token, request=request_json):", source)
        self.assertIn('"/v2/service-token"', source)
        self.assertIn('"include_imports": "false"', source)
        self.assertNotIn("from fleet_secrets", source)
        self.assertNotIn('"infisical",\n                          "export"', source)

    def test_caller_merge_principal_mismatch_is_refused_before_mint(self):
        guard = 'if os.environ.get("EXPECTED_MERGE_ACTOR", ACTOR) != ACTOR:'
        wf = Path(__file__).resolve().parent.parent / ".github/workflows/auto-merge-reusable.yml"
        for source in (Path(w.__file__).read_text(), wf.read_text()):
            self.assertEqual(source.count(guard), 1)
            self.assertLess(source.index(guard), source.index("token = mint_verified(config)"))
            self.assertIn("caller merge principal does not match the relay principal", source)

    def test_exec_token_is_masked_in_actions(self):
        source = Path(w.__file__).read_text()
        self.assertIn('print("::add-mask::" + token, flush=True)', source)
        self.assertIn('os.environ.get("GITHUB_ACTIONS") == "true"', source)

    def test_dedicated_infisical_token_is_promoted_and_legacy_credentials_are_stripped(self):
        env = w.infisical_environment(
            {
                w.INFISICAL_TOKEN_ENV: "st.wise-purpose-token-fixture",
                "INFISICAL_TOKEN": "st.ambient-generic-token-fixture",
                "INFISICAL_AUTOMATION_CLIENT_ID": "legacy-id-fixture",
                "INFISICAL_AUTOMATION_CLIENT_SECRET": "legacy-secret-fixture",
            }
        )
        self.assertEqual(env["INFISICAL_TOKEN"], "st.wise-purpose-token-fixture")
        self.assertNotIn(w.INFISICAL_TOKEN_ENV, env)
        self.assertNotIn("INFISICAL_AUTOMATION_CLIENT_ID", env)
        self.assertNotIn("INFISICAL_AUTOMATION_CLIENT_SECRET", env)

    def test_missing_malformed_and_short_service_tokens_fail_closed(self):
        for value in (None, "", "automation.id-secret", "st.short"):
            with self.subTest(value=value):
                env = {} if value is None else {w.INFISICAL_TOKEN_ENV: value}
                with self.assertRaisesRegex(w.Refused, w.INFISICAL_TOKEN_ENV):
                    w.infisical_environment(env)

    def test_configuration_fetch_verifies_exact_project_folder_before_read(self):
        token = "st." + "x" * 32
        calls = []

        def api(method, path, request_token, body, env, **kwargs):
            calls.append((path, request_token, dict(env)))
            if path == "/v2/service-token":
                return 200, {
                    "workspace": w.FLEETOPS_PROJECT,
                    "projectId": w.FLEETOPS_PROJECT,
                    "permissions": ["read"],
                    "scopes": [{"environment": "prod", "secretPath": w.FOLDER}],
                    "expiresAt": None,
                }
            return 200, {
                "secrets": [
                    {
                        "secretKey": key,
                        "secretValue": value,
                        "type": "shared",
                        "workspace": w.FLEETOPS_PROJECT,
                        "environment": "prod",
                        "secretPath": w.FOLDER,
                    }
                    for key, value in self.config().items()
                ]
            }

        with mock.patch.object(w.scoped_secrets, "_api_json", api):
            got = w.fetch_configuration({w.INFISICAL_TOKEN_ENV: token})
        self.assertEqual(w.configuration(got), self.config())
        self.assertEqual(calls[0][0], "/v2/service-token")
        self.assertTrue(calls[1][0].startswith("/v3/secrets/raw?"))
        self.assertTrue(all(call[1] == token for call in calls))
        self.assertEqual(calls[1][2][w.scoped_secrets.ENV_READ_PATHS], w.FOLDER)

    def test_wrong_scope_stops_before_fetching_configuration(self):
        calls = []

        def api(method, path, request_token, body, env, **kwargs):
            calls.append(path)
            return 200, {
                "workspace": w.FLEETOPS_PROJECT,
                "projectId": w.FLEETOPS_PROJECT,
                "permissions": ["read"],
                "scopes": [{"environment": "prod", "secretPath": "/wrong"}],
                "expiresAt": None,
            }

        with mock.patch.object(w.scoped_secrets, "_api_json", api):
            with self.assertRaisesRegex(w.Refused, "scoped-folder"):
                w.fetch_configuration({w.INFISICAL_TOKEN_ENV: "st." + "x" * 32})
        self.assertEqual(calls, ["/v2/service-token"])

    def test_child_exec_strips_every_shared_infisical_auth_alias(self):
        captured = {}

        def fake_exec(path, args, env):
            captured.update(env)

        source_env = {name: "synthetic-auth-value" for name in w.AUTH_ENV_VARS}
        source_env[w.INFISICAL_TOKEN_ENV] = "st." + "x" * 32
        source_env["GH_TOKEN"] = "synthetic-old-github-token"
        with mock.patch.dict(os.environ, source_env, clear=True):
            with mock.patch.object(w, "fetch_configuration", return_value=self.config()):
                with mock.patch.object(w, "mint_verified", return_value="ghs_synthetic"):
                    with mock.patch.object(
                        w.sys, "argv", ["wise-relay-exec.py", "--", "/bin/true"]
                    ):
                        with mock.patch.object(w.os, "execvpe", fake_exec):
                            w.main()

        for name in w.AUTH_ENV_VARS:
            self.assertNotIn(name, captured)
        self.assertEqual(captured["GH_TOKEN"], "ghs_synthetic")


class InlineWorkflowTests(unittest.TestCase):
    def setUp(self):
        source = (
            Path(__file__).resolve().parent.parent / ".github/workflows/auto-merge-reusable.yml"
        ).read_text()
        marker = "<<'WISE_RELAY_PY'\n"
        start = source.index(marker) + len(marker)
        end = source.index("\n          WISE_RELAY_PY", start)
        python_source = textwrap.dedent(source[start:end])
        self.namespace = {"__name__": "inline_wise_test"}
        exec(
            compile(python_source, "auto-merge-reusable.yml:WISE_RELAY_PY", "exec"), self.namespace
        )
        self.configuration = {
            "WISE_GIT_RELAY_BEARER": "synthetic-relay-bearer",
            "WISE_GIT_RELAY_URL": "https://relay.example.test",
            "WISE_GIT_RELAY_REPOSITORY": w.REPO,
            "WISE_GIT_RELAY_EXPECTED_ACTOR": w.ACTOR,
            "WISE_GIT_RELAY_PERMISSIONS": json.dumps(w.PERMISSIONS),
        }

    def metadata(self, **overrides):
        result = {
            "workspace": w.FLEETOPS_PROJECT,
            "projectId": w.FLEETOPS_PROJECT,
            "permissions": ["read"],
            "scopes": [{"environment": "prod", "secretPath": w.FOLDER}],
            "expiresAt": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }
        result.update(overrides)
        return result

    def records(self, **overrides):
        data = dict(self.configuration)
        data.update(overrides)
        return [
            {
                "secretKey": key,
                "secretValue": value,
                "type": "shared",
                "workspace": w.FLEETOPS_PROJECT,
                "environment": "prod",
                "secretPath": w.FOLDER,
            }
            for key, value in data.items()
        ]

    def test_embedded_fetcher_runs_from_empty_cwd_and_verifies_metadata(self):
        calls = []

        def fake_http(url, headers, body=None):
            calls.append((url, headers, body))
            if url.endswith("/v2/service-token"):
                return self.metadata()
            return {"secrets": self.records()}

        with tempfile.TemporaryDirectory() as empty_cwd:
            old_cwd = os.getcwd()
            try:
                os.chdir(empty_cwd)
                found = self.namespace["fetch_infisical_configuration"](
                    "st." + "x" * 32, request=fake_http
                )
            finally:
                os.chdir(old_cwd)
        self.assertEqual(found, self.configuration)
        self.assertEqual(calls[0][0], "https://app.infisical.com/api/v2/service-token")
        self.assertEqual(calls[0][1]["Authorization"], "Bearer st." + "x" * 32)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(calls[1][0]).query)
        self.assertEqual(query["workspaceId"], [w.FLEETOPS_PROJECT])
        self.assertEqual(query["environment"], ["prod"])
        self.assertEqual(query["secretPath"], [w.FOLDER])
        self.assertEqual(query["expandSecretReferences"], ["false"])
        self.assertEqual(query["include_imports"], ["false"])

    def test_bad_metadata_fails_before_secret_list_request(self):
        invalid = (
            self.metadata(workspace="wrong-project"),
            self.metadata(permissions=["read", "write"]),
            self.metadata(scopes=[{"environment": "prod", "secretPath": "/wrong"}]),
            self.metadata(expiresAt="2000-01-01T00:00:00+00:00"),
        )
        for answer in invalid:
            with self.subTest(answer=answer):
                calls = []

                def fake_http(url, headers, body=None):
                    calls.append(url)
                    return answer

                with self.assertRaises(self.namespace["Refused"]):
                    self.namespace["fetch_infisical_configuration"](
                        "st." + "x" * 32, request=fake_http
                    )
                self.assertEqual(len(calls), 1)

    def test_bad_secret_provenance_import_and_reference_are_rejected(self):
        invalid_records = (
            self.records()[0] | {"workspace": "wrong-project"},
            self.records()[0] | {"type": "imported"},
            self.records()[0] | {"secretValue": "${OTHER_SECRET}"},
        )
        for record in invalid_records:
            with self.subTest(record=record):

                def fake_http(url, headers, body=None):
                    if url.endswith("/v2/service-token"):
                        return self.metadata()
                    return {"secrets": [record]}

                with self.assertRaises(self.namespace["Refused"]):
                    self.namespace["fetch_infisical_configuration"](
                        "st." + "x" * 32, request=fake_http
                    )

    def test_all_infisical_bindings_are_removed_before_consumer_exec(self):
        namespace = self.namespace
        namespace["fetch_infisical_configuration"] = lambda token: self.configuration
        namespace["mint_verified"] = lambda config, request=None: "ghs_synthetic"
        captured = {}

        def fake_exec(path, args, env):
            captured.update(env)

        source_env = {name: "synthetic-auth-value" for name in namespace["AUTH_ENV_VARS"]}
        source_env["WISE_RELAY_INFISICAL_TOKEN"] = "st." + "x" * 32
        source_env["GH_TOKEN"] = "synthetic-old-github-token"
        with mock.patch.dict(os.environ, source_env, clear=True):
            with mock.patch.object(namespace["sys"], "argv", ["inline", "--", "/bin/true"]):
                with mock.patch.object(namespace["os"], "execvpe", fake_exec):
                    namespace["main"]()
        for name in namespace["AUTH_ENV_VARS"]:
            self.assertNotIn(name, captured)
        self.assertEqual(captured["GH_TOKEN"], "ghs_synthetic")


if __name__ == "__main__":
    unittest.main()
