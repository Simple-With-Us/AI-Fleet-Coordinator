import importlib.util
import os
import pathlib
import unittest
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
                    "expiresAt": None,
                }
            name = path.split("/v3/secrets/raw/", 1)[1].split("?", 1)[0]
            value = {"EMAIL": "seat@example.test", "KEY": "synthetic-zulip-key"}[name]
            return 200, {"secret": {
                "secretKey": name, "secretValue": value, "type": "shared",
                "workspace": seat_key.INFISICAL_PROJECT, "environment": "prod", "secretPath": "/zulip",
            }}

        with mock.patch.dict(os.environ, {seat_key.INFISICAL_TOKEN_ENVS[seat_key.INFISICAL_PROJECT]: token}):
            with mock.patch.object(seat_key.scoped_secrets, "_api_json", api):
                self.assertEqual(seat_key.infisical_pair("EMAIL", "KEY"),
                                 ("seat@example.test", "synthetic-zulip-key"))
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
                "workspace": seat_key.FLEET_OPS_PROJECT,
                "projectId": seat_key.FLEET_OPS_PROJECT,
                "permissions": ["read"],
                "scopes": [{"environment": "prod", "secretPath": "/zulip"}],
                "expiresAt": None,
            }

        token_name = seat_key.INFISICAL_TOKEN_ENVS[seat_key.INFISICAL_PROJECT]
        with mock.patch.dict(os.environ, {token_name: "st." + "synthetic-seat-key-reader-token"}):
            with mock.patch.object(seat_key.scoped_secrets, "_api_json", api):
                with self.assertRaises(SystemExit):
                    seat_key.infisical_pair("EMAIL", "KEY")
        self.assertEqual(calls, ["/v2/service-token"])


if __name__ == "__main__":
    unittest.main()
