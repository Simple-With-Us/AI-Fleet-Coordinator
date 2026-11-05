"""The server-instance Infisical API route:  the one host with no fleet-secrets to run.

`zulip._fetch_from_fleet_secrets` answers through `fleet-secrets run --only` wherever the reader is
installed.  The server container's image ships neither the reader nor the `infisical` CLI it shells
out to, so on that instance (AGENT_SYNC_INSTANCE=server) a fetch with no binary installed reads
Infisical directly, with the fleet automation identity in the daemon's environment.  These tests
fake the Infisical API at the opener, so no network is touched, and pin the three properties the
route was added under:  it resolves, it fails loudly, and it never engages where the CLI route
exists (or off the server instance).
"""

import io
import json
import os
import unittest
import urllib.error
import urllib.parse
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from agent_sync import zulip as Z
from agent_sync.service_identity import operator_credentials

FAKE_PROJECT_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "fixture-project-fleet-ops"))
OPERATOR_BOT_ID = "43210"
OPERATOR_EMAIL = "operator@example.test"
OPERATOR_KEY = "fixture-operator-key"
CLIENT_ID = "fixture-automation-client-id"
CLIENT_SECRET = "fixture-automation-client-secret"
FAKE_FLEET_SECRETS = Path(__file__).resolve().parent / "fixtures" / "fake-fleet-secrets"


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeInfisical:
    """An _API_OPENER stand-in:  a universal-auth login and one raw folder read, both recorded."""

    def __init__(self, folders, *, login_status=200, error_body=b""):
        self.folders = folders  # {secretPath: {name: value}}
        self.login_status = login_status
        self.error_body = error_body
        self.requests = []  # (method, url, body, authorization header)

    def open(self, req, timeout=None):
        body = json.loads(req.data.decode("utf-8")) if req.data else None
        self.requests.append(
            (req.get_method(), req.full_url, body, req.get_header("Authorization"))
        )
        if req.full_url.endswith("/v1/auth/universal-auth/login"):
            if self.login_status != 200:
                raise urllib.error.HTTPError(
                    req.full_url, self.login_status, "refused", {}, io.BytesIO(self.error_body)
                )
            return FakeResponse({"accessToken": "fixture-access-token"})
        if "/v3/secrets/raw" in req.full_url:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)
            folder = self.folders.get(query["secretPath"][0], {})
            return FakeResponse(
                {"secrets": [{"secretKey": k, "secretValue": v} for k, v in folder.items()]}
            )
        raise AssertionError("unexpected request: " + req.full_url)


class ApiRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.home.mkdir()
        self.empty_path = root / "empty-bin"
        self.empty_path.mkdir()
        for cache in (Z._value_cache, Z._names_cache, Z._failures):
            cache.clear()
            self.addCleanup(cache.clear)

    def server_env(self, **extra):
        """An env with no fleet-secrets anywhere:  a home with no ~/.local/bin and a PATH with
        nothing on it, on the server instance, with the automation identity present."""
        env = {
            "HOME": str(self.home),
            "PATH": str(self.empty_path),
            Z.ENV_INFISICAL_PROJECT: FAKE_PROJECT_ID,
            "AGENT_SYNC_INSTANCE": "server",
            Z.ENV_AUTOMATION_CLIENT_ID: CLIENT_ID,
            Z.ENV_AUTOMATION_CLIENT_SECRET: CLIENT_SECRET,
        }
        env.update(extra)
        return env

    def test_operator_credentials_resolve_through_the_api_route(self):
        api = FakeInfisical(
            {
                "/zulip_operator": {
                    "ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL,
                    "ZULIP_OPERATOR_API_KEY": OPERATOR_KEY,
                }
            }
        )
        env = self.server_env(
            OPERATOR_ZULIP_USER_ID=OPERATOR_BOT_ID,
            OPERATOR_ZULIP_EMAIL=OPERATOR_EMAIL,
            ZULIP_SITE="https://realm.example.test",
        )
        # tests/__init__ replaces the module's fetch with a guard;  restore the real dispatcher.
        with mock.patch.object(Z, "_default_infisical_fetch", Z._fetch_from_fleet_secrets), (
            mock.patch.object(Z, "_API_OPENER", api)
        ):
            creds = operator_credentials(env, self.home)
        self.assertEqual(creds.email, OPERATOR_EMAIL)
        self.assertEqual(creds.key, OPERATOR_KEY)
        # One login, with the automation identity, then one read of the Operator's own folder in
        # the pinned Fleet-OPS project, carrying the login's token.
        self.assertEqual([r[0] for r in api.requests], ["POST", "GET"])
        self.assertEqual(
            api.requests[0][2], {"clientId": CLIENT_ID, "clientSecret": CLIENT_SECRET}
        )
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(api.requests[1][1]).query)
        self.assertEqual(query["workspaceId"], [FAKE_PROJECT_ID])
        self.assertEqual(query["secretPath"], ["/zulip_operator"])
        self.assertEqual(api.requests[1][3], "Bearer fixture-access-token")

    def test_the_api_route_answers_from_cache_within_the_ttl(self):
        api = FakeInfisical({"/zulip": {"A": "1"}})
        env = self.server_env()
        with mock.patch.object(Z, "_API_OPENER", api):
            self.assertEqual(Z._fetch_from_fleet_secrets(("A",), env, self.home), {"A": "1"})
            self.assertEqual(Z._fetch_from_fleet_secrets(("A",), env, self.home), {"A": "1"})
        self.assertEqual(len(api.requests), 2)  # one login and one read served both fetches

    def test_a_name_the_folder_does_not_hold_fails_loudly(self):
        api = FakeInfisical({"/zulip_operator": {"ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL}})
        env = self.server_env()
        with mock.patch.object(Z, "_API_OPENER", api):
            with self.assertRaises(Z.CredentialError) as caught:
                Z._fetch_from_fleet_secrets(
                    ("ZULIP_OPERATOR_EMAIL", "ZULIP_OPERATOR_API_KEY"),
                    env,
                    self.home,
                    "/zulip_operator",
                )
        self.assertIn("key-absent", str(caught.exception))
        self.assertIn("ZULIP_OPERATOR_API_KEY", str(caught.exception))

    def test_a_refused_login_fails_loudly_and_leaks_nothing(self):
        api = FakeInfisical({}, login_status=401, error_body=b"DO-NOT-LOG-MARKER")
        env = self.server_env()
        with mock.patch.object(Z, "_API_OPENER", api):
            with self.assertRaises(Z.CredentialError) as caught:
                Z._fetch_from_fleet_secrets(("A",), env, self.home)
        message = str(caught.exception)
        self.assertIn("infisical-api login", message)
        self.assertIn("HTTP 401", message)
        self.assertNotIn("DO-NOT-LOG-MARKER", message)
        self.assertNotIn(CLIENT_SECRET, message)
        self.assertEqual(len(api.requests), 1)  # a 401 is not retried

    def test_no_binary_off_the_server_instance_keeps_the_installed_reader_error(self):
        api = FakeInfisical({"/zulip": {"A": "1"}})
        env = self.server_env()
        del env["AGENT_SYNC_INSTANCE"]
        with mock.patch.object(Z, "_API_OPENER", api):
            with self.assertRaises(Z.CredentialError) as caught:
                Z._fetch_from_fleet_secrets(("A",), env, self.home)
        self.assertIn("fleet-secrets is not installed", str(caught.exception))
        self.assertEqual(api.requests, [])

    def test_the_server_instance_without_the_identity_keeps_the_installed_reader_error(self):
        api = FakeInfisical({"/zulip": {"A": "1"}})
        env = self.server_env()
        del env[Z.ENV_AUTOMATION_CLIENT_ID]
        del env[Z.ENV_AUTOMATION_CLIENT_SECRET]
        with mock.patch.object(Z, "_API_OPENER", api):
            with self.assertRaises(Z.CredentialError) as caught:
                Z._fetch_from_fleet_secrets(("A",), env, self.home)
        self.assertIn("fleet-secrets is not installed", str(caught.exception))
        self.assertEqual(api.requests, [])

    def test_a_half_set_automation_pair_is_refused_before_any_request(self):
        api = FakeInfisical({"/zulip": {"A": "1"}})
        env = self.server_env()
        del env[Z.ENV_AUTOMATION_CLIENT_SECRET]
        with mock.patch.object(Z, "_API_OPENER", api):
            with self.assertRaises(Z.CredentialError) as caught:
                Z._fetch_from_fleet_secrets(("A",), env, self.home)
        self.assertIn("automation-identity-incomplete", str(caught.exception))
        self.assertEqual(api.requests, [])

    def test_a_non_prod_environment_slug_is_refused(self):
        api = FakeInfisical({"/zulip": {"A": "1"}})
        env = self.server_env(INFISICAL_ENVIRONMENT="dev")
        with mock.patch.object(Z, "_API_OPENER", api):
            with self.assertRaises(Z.CredentialError) as caught:
                Z._fetch_from_fleet_secrets(("A",), env, self.home)
        self.assertIn("non-prod-environment", str(caught.exception))
        self.assertEqual(api.requests, [])

    def test_a_warm_cache_does_not_serve_a_non_prod_fetch(self):
        api = FakeInfisical({"/zulip": {"A": "1"}})
        env = self.server_env()
        with mock.patch.object(Z, "_API_OPENER", api):
            self.assertEqual(Z._fetch_from_fleet_secrets(("A",), env, self.home), {"A": "1"})
            env["INFISICAL_ENVIRONMENT"] = "dev"
            with self.assertRaises(Z.CredentialError) as caught:
                Z._fetch_from_fleet_secrets(("A",), env, self.home)
        self.assertIn("non-prod-environment", str(caught.exception))
        self.assertEqual(len(api.requests), 2)  # refused before any new request was made

    def test_the_server_instance_gate_casefolds_like_the_other_instance_checks(self):
        api = FakeInfisical({"/zulip": {"A": "1"}})
        env = self.server_env(AGENT_SYNC_INSTANCE="Server")
        with mock.patch.object(Z, "_API_OPENER", api):
            self.assertEqual(Z._fetch_from_fleet_secrets(("A",), env, self.home), {"A": "1"})

    def test_an_installed_binary_still_wins_over_the_api_route(self):
        data = Path(self.tmp.name) / "fake-fleet-secrets.json"
        data.write_text(json.dumps({"/zulip": {"A": "from-cli"}}), encoding="utf-8")

        class Bomb:
            def open(self, *args, **kwargs):
                raise AssertionError("the API route ran although fleet-secrets is installed")

        env = self.server_env(
            PATH=os.environ.get("PATH", ""),  # the fake's shebang needs a real PATH
            FLEET_SECRETS_BIN=str(FAKE_FLEET_SECRETS),
            FAKE_FLEET_SECRETS_FILE=str(data),
        )
        with mock.patch.object(Z, "_API_OPENER", Bomb()):
            self.assertEqual(
                Z._fetch_from_fleet_secrets(("A",), env, self.home), {"A": "from-cli"}
            )


if __name__ == "__main__":
    unittest.main()
