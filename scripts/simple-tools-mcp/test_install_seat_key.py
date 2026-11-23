import importlib.util
import os
import pathlib
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "install_seat_key", pathlib.Path(__file__).with_name("install_seat_key.py")
)
seat_key = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seat_key)


class ScopedSeatKeyTests(unittest.TestCase):
    def test_reads_only_after_exact_project_and_folder_metadata(self):
        token = "st." + "synthetic-seat-key-reader-token"
        calls = []

        def api(method, path, request_token, body, env, **kwargs):
            calls.append((path, request_token, dict(env)))
            if path == "/v2/service-token":
                return 200, {
                    "workspace": seat_key.INFISICAL_PROJECT,
                    "projectId": seat_key.INFISICAL_PROJECT,
                    "permissions": ["read"],
                    "scopes": [{"environment": "prod", "secretPath": "/zulip"}],
                    "expiresAt": "2099-01-01T00:00:00Z",
                }
            name = path.split("/v3/secrets/raw/", 1)[1].split("?", 1)[0]
            value = {"EMAIL": "seat@example.test", "KEY": "synthetic-zulip-key"}[name]
            return 200, {
                "secret": {
                    "secretKey": name,
                    "secretValue": value,
                    "type": "shared",
                    "workspace": seat_key.INFISICAL_PROJECT,
                    "environment": "prod",
                    "secretPath": "/zulip",
                }
            }

        with (
            mock.patch.dict(os.environ, {seat_key.AFC_TOKEN_ENV: token}),
            mock.patch.object(seat_key.scoped_secrets, "_api_json", api),
        ):
            self.assertEqual(
                seat_key.infisical_pair("EMAIL", "KEY"),
                ("seat@example.test", "synthetic-zulip-key"),
            )
        self.assertEqual(calls[0][0], "/v2/service-token")
        self.assertTrue(calls[1][0].startswith("/v3/secrets/raw/EMAIL?"))
        self.assertTrue(calls[2][0].startswith("/v3/secrets/raw/KEY?"))
        self.assertTrue(all(call[1] == token for call in calls))
        self.assertEqual(calls[1][2][seat_key.scoped_secrets.ENV_READ_PATHS], "/zulip")

    def test_wrong_metadata_stops_before_secret_read(self):
        calls = []

        def api(method, path, request_token, body, env, **kwargs):
            calls.append(path)
            return 200, {
                "workspace": "00000000-0000-4000-8000-000000000001",
                "projectId": "00000000-0000-4000-8000-000000000001",
                "permissions": ["read"],
                "scopes": [{"environment": "prod", "secretPath": "/zulip"}],
                "expiresAt": "2099-01-01T00:00:00Z",
            }

        token_name = seat_key.AFC_TOKEN_ENV
        with mock.patch.dict(os.environ, {token_name: "st." + "synthetic-seat-key-reader-token"}):
            with mock.patch.object(seat_key.scoped_secrets, "_api_json", api):
                with self.assertRaises(SystemExit):
                    seat_key.infisical_pair("EMAIL", "KEY")
        self.assertEqual(calls, ["/v2/service-token"])


class GenericHandoffReaderTests(unittest.TestCase):
    def test_generic_root_names_cross_the_real_worker_without_borrowing_bot_grants(self):
        from fleet_knobs.tests._fake import ScopedReadAPI

        names = ("CLOUDFLARE_JAY_ACCOUNT_EMAIL", "CLOUDFLARE_JAY_API_KEY", "ZULIP_OWNER_USER_ID")
        values = dict(zip(names, ("owner@example.test", "public fixture", "123")))
        client = seat_key._infisical_reader()
        original = client.bounded_fetch_project
        with ScopedReadAPI(
            values,
            project=str(uuid.uuid4()),
            path="/",
            token_name=seat_key.TOKEN_ACCOUNTS["/"],
        ) as api:

            def read(project, folder, env, **options):
                return original(
                    project,
                    folder,
                    {**env, "FLEET_SECRETS_API": api.env["FLEET_SECRETS_API"]},
                    **options,
                )

            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "INFISICAL_FLEET_OPS_PROJECT_ID": api.project,
                        "INFISICAL_TOKEN": api.token,
                        "FLEET_SECRETS_API": api.env["FLEET_SECRETS_API"],
                        # The store is the authority:  an empty home has no account token.
                        "HOME": str(Path(tempfile.mkdtemp(prefix="seat-key-store-"))),
                    },
                    clear=True,
                ),
                mock.patch.object(client, "bounded_fetch_project", side_effect=read),
            ):
                self.assertEqual([seat_key.handoff(n) for n in names], list(values.values()))
                api.metadata_override = {"expiresAt": None}
                self.assertEqual(seat_key.handoff(names[0]), values[names[0]])
                api.metadata_override = {
                    "scopes": [{"environment": "prod", "secretPath": "/zulip"}]
                }
                api.requests.clear()
                with self.assertRaises(SystemExit):
                    seat_key.handoff(names[1])
                self.assertEqual(api.requests, ["/api/v2/service-token"])
                os.environ.pop("INFISICAL_TOKEN")
                os.environ[seat_key.FLEET_OPS_TOKEN_ENV] = api.token
                api.requests.clear()
                with self.assertRaises(SystemExit):
                    seat_key.handoff(names[1])
                self.assertEqual(api.requests, [])

    def test_the_environment_is_not_a_source_and_unknown_names_cannot_expand_scope(self):
        # The environment is never a source for a value:  the name resolves from the consumer's
        # own scoped grant,  and an unknown name never reaches a read.
        with (
            mock.patch.dict(os.environ, {"CLOUDFLARE_JAY_API_KEY": "synced"}, clear=True),
            mock.patch.object(seat_key, "_infisical_reader", side_effect=AssertionError("reader")),
        ):
            with self.assertRaises(SystemExit):
                seat_key.handoff("CLOUDFLARE_JAY_API_KEY")
            with self.assertRaises(SystemExit):
                seat_key.handoff("OTHER")


if __name__ == "__main__":
    unittest.main()
