"""Optional native data loaded by the shared Operator startup source table.

The loopback Infisical verifies the shared token metadata and every returned row's project, folder, environment and
source type.  All bearer and provider values here are plainly nonsecret fixtures.
"""

from __future__ import annotations

import http.server
import importlib.util
import json
import re
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
PROJECT_ID = "00000000-0000-4000-8000-000000000099"
SHARED_BINDING = "nonsecret-shared-token-fixture"

spec = importlib.util.spec_from_file_location("operator_shared_data_loader", SERVER_DIR / "infisical_env.py")
assert spec and spec.loader
IE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(IE)


class FakeInfisical:
    def __init__(self, cursor_value: str | None = "native-api-fixture") -> None:
        self.grant = (IE.OPERATOR_VOICE_PATH,)
        self.requests: list[tuple[str, str]] = []
        self.secrets = {
            "/zulip_operator": {
                "ZULIP_OPERATOR_EMAIL": "operator-fixture@example.test",
                "ZULIP_OPERATOR_API_KEY": "operator-voice-fixture",
            }
        }
        if cursor_value is not None:
            self.secrets["/zulip_operator"][IE.OPERATOR_CURSOR_NAME] = cursor_value
        self.bad_provenance = False
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def _send(self, code: int, payload: dict) -> None:
                data = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:
                fake.requests.append((self.command, self.path))
                if self.headers.get("Authorization") != "Bearer " + SHARED_BINDING:
                    return self._send(401, {"message": "fixture did not match"})
                if self.path == "/api/v2/service-token":
                    return self._send(
                        200,
                        {
                            "id": "fixture-token-id",
                            "name": "fixture-shared-operator-reader",
                            "projectId": PROJECT_ID,
                            "workspace": PROJECT_ID,
                            "scopes": [
                                {"environment": "prod", "secretPath": path} for path in fake.grant
                            ],
                            "permissions": ["read"],
                            "expiresAt": "2099-01-01T00:00:00Z",
                        },
                    )
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                path = query.get("secretPath", [""])[0]
                if path not in fake.grant or query.get("environment") != ["prod"]:
                    return self._send(404, {"message": "fixture folder not granted"})
                rows = []
                for name, value in fake.secrets.get(path, {}).items():
                    provenance_path = "/wrong-folder" if fake.bad_provenance else path
                    rows.append(
                        {
                            "secretKey": name,
                            "secretValue": value,
                            "workspace": PROJECT_ID,
                            "environment": "prod",
                            "secretPath": provenance_path,
                            "type": "shared",
                        }
                    )
                return self._send(200, {"secrets": rows})

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d/api" % self.server.server_address[1]

    def __enter__(self) -> "FakeInfisical":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.server.server_close()


class SharedOperatorDataTests(unittest.TestCase):
    def build_env(self, fake: FakeInfisical) -> tuple[dict[str, str], str]:
        env = {
            IE.TOKEN: "primary-test",
            "INFISICAL_PROJECT_ID": PROJECT_ID,
            IE.WAKE_ROUTER_TOKEN: "nonsecret-router-fixture",
            IE.OPERATOR_VOICE_TOKEN: SHARED_BINDING,
            IE.OPERATOR_VOICE_PROJECT: PROJECT_ID,
            "INFISICAL_API_URL": fake.url,
            "CURSOR_SYNC_API_KEY": "container-copy-fixture",
            "ZULIP_SITE": "https://zulip.example.test",
        }
        original_fetch = IE.fetch

        def fetch(env, *, path=IE.ZULIP_PATH, token_name=IE.TOKEN, **kwargs):
            if token_name == IE.OPERATOR_VOICE_TOKEN:
                return original_fetch(env, path=path, token_name=token_name, **kwargs)
            if token_name == IE.WAKE_ROUTER_TOKEN:
                return {
                    "SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY": "registry-fixture-value-please-do-not-use",
                    "WAKE_ROUTER_KEY": "wake-router-fixture-value-please-do-not-use",
                }
            if path == IE.ZULIP_PATH:
                return {
                    "ZULIP_JET_EMAIL": "jet-fixture@example.test",
                    "ZULIP_JET_API_KEY": "jet-api-fixture",
                }
            if path == IE.OPERATOR_PATH:
                return {
                    "OPERATOR_ZULIP_USER_ID": "4242",
                    "OPERATOR_ZULIP_EMAIL": "operator-fixture@example.test",
                }
            raise AssertionError("unexpected startup source")

        with (
            mock.patch.object(IE, "server_codes", return_value={"JET"}),
            mock.patch.object(IE, "fetch", side_effect=fetch),
            mock.patch.object(IE, "SERVICE_TOKEN_RE", re.compile(r".+")),
        ):
            return IE.build_env(env)

    def test_unbound_optional_source_names_the_shared_binding_without_failing_startup(self) -> None:
        data, note = IE.operator_optional_data({})
        self.assertEqual(data, {})
        self.assertIn(IE.OPERATOR_VOICE_TOKEN, note)

    def test_missing_optional_key_keeps_voice_and_disables_cursor_data(self) -> None:
        with FakeInfisical(cursor_value=None) as fake:
            out, line = self.build_env(fake)
        self.assertEqual(out["ZULIP_OPERATOR_API_KEY"], "operator-voice-fixture")
        self.assertNotIn("CURSOR_SYNC_API_KEY", out)
        self.assertIn("cursor-cloud data unavailable (native key missing)", line)
        raw_paths = [
            urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["secretPath"][0]
            for _, path in fake.requests
            if "/api/v3/secrets/raw?" in path
        ]
        self.assertEqual(len(raw_paths), 1)
        self.assertEqual(raw_paths, [IE.OPERATOR_VOICE_PATH])
        self.assertNotIn("container-copy-fixture", line)

    def test_one_folder_shared_fetch_loads_optional_native_data(self) -> None:
        with FakeInfisical() as fake:
            out, line = self.build_env(fake)
        self.assertEqual(out["CURSOR_SYNC_API_KEY"], "native-api-fixture")
        self.assertIn("cursor-cloud data loaded from /zulip_operator", line)
        self.assertNotIn("native-api-fixture", line)
        self.assertNotIn("container-copy-fixture", line)
        raw_paths = [
            urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)["secretPath"][0]
            for _, path in fake.requests
            if "/api/v3/secrets/raw?" in path
        ]
        self.assertEqual(raw_paths, [IE.OPERATOR_VOICE_PATH])

    def test_empty_or_unresolved_optional_key_keeps_voice_but_disables_feed(self) -> None:
        for value in ("", "${UNRESOLVED_FIXTURE}"):
            with self.subTest(value="empty" if not value else "unresolved"), FakeInfisical(value) as fake:
                out, line = self.build_env(fake)
            self.assertEqual(out["ZULIP_OPERATOR_API_KEY"], "operator-voice-fixture")
            self.assertIn("cursor-cloud data unavailable (native key missing)", line)
            self.assertNotIn("UNRESOLVED_FIXTURE", line)
            self.assertNotIn("CURSOR_SYNC_API_KEY", out)

    def test_invalid_source_row_provenance_fails_closed_for_entire_response(self) -> None:
        with FakeInfisical() as fake:
            fake.bad_provenance = True
            with self.assertRaises(IE.LoadError):
                self.build_env(fake)



# The server's Operator voice (AFC-602):  the loader reads the Operator bot's own two names with their
# own read-only token, and the credential reader takes them from the environment it is given.
# 
# The server container ships no fleet-secrets and its loader deliberately strips the machine identity, so
# before this the Operator's credential read could only fail there (`fleet-secrets is not installed`).  The
# regression test at the bottom is the one that matters:  whatever `build_env` produces on the server has
# to be enough for `operator_credentials`, so a later change to the stripped names cannot silently cut the
# Operator off again.



import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from simple_tools import service_identity as SI
from simple_tools import zulip as Z

SPEC = importlib.util.spec_from_file_location(
    "loader_voice", Path(__file__).resolve().parents[1] / "server" / "infisical_env.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)

PRIMARY_PROJECT = str(UUID(int=1))
FLEET_OPS_PROJECT = str(UUID(int=3))
PRIMARY_TOKEN = "st.primary-service-token-fixture-value"
VOICE_TOKEN = "st.operator-voice-service-token-fixture"
OPERATOR_ID = 4242
OPERATOR_EMAIL = "operator-bot@realm.example.test"
OPERATOR_KEY = "operator-key-" + "x" * 24
WAKE_TOKEN = "st.wake-router-token-fixture-value"


def voice_env(**overrides: str) -> dict[str, str]:
    values = {
        "INFISICAL_TOKEN": PRIMARY_TOKEN,
        "INFISICAL_PROJECT_ID": PRIMARY_PROJECT,
        MODULE.OPERATOR_VOICE_PROJECT: FLEET_OPS_PROJECT,
        MODULE.OPERATOR_VOICE_TOKEN: VOICE_TOKEN,
        MODULE.WAKE_ROUTER_TOKEN: WAKE_TOKEN,
        "SIMPLE_TOOLS_INSTANCE": "server",
        "OPERATOR_ZULIP_USER_ID": str(OPERATOR_ID),
        "OPERATOR_ZULIP_EMAIL": OPERATOR_EMAIL,
        "ZULIP_SITE": "https://realm.example.test",
        "INFISICAL_AUTOMATION_CLIENT_ID": "machine-identity-id",
        "INFISICAL_AUTOMATION_CLIENT_SECRET": "machine-identity-secret",
    }
    values.update(overrides)
    return values


class OperatorVoiceKeysTests(unittest.TestCase):
    def read(self, secrets, env=None):
        calls = []

        def fetch(source, **kwargs):
            calls.append((dict(source), dict(kwargs)))
            if isinstance(secrets, Exception):
                raise secrets
            return secrets

        with patch.object(MODULE, "fetch", side_effect=fetch):
            keys, note = MODULE.operator_voice_keys(voice_env() if env is None else env)
        return keys, note, calls

    def test_it_reads_exactly_the_two_names_with_its_own_token_scope_and_project(self) -> None:
        keys, note, calls = self.read(
            {
                "ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL,
                "ZULIP_OPERATOR_API_KEY": OPERATOR_KEY,
                "SOMETHING_ELSE": "ignored",
            }
        )
        self.assertEqual(
            keys, {"ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL, "ZULIP_OPERATOR_API_KEY": OPERATOR_KEY}
        )
        self.assertNotIn(OPERATOR_KEY, note, "names and counts only")
        ((source, kwargs),) = calls
        self.assertEqual(kwargs["token"], VOICE_TOKEN, "never the primary token")
        self.assertEqual(kwargs["path"], "/zulip_operator")
        self.assertEqual(kwargs["expected_paths"], ("/zulip_operator",))
        self.assertEqual(source["INFISICAL_PROJECT_ID"], FLEET_OPS_PROJECT)
        self.assertEqual(kwargs["token_name"], MODULE.OPERATOR_VOICE_TOKEN)

    def test_without_its_token_nothing_is_read_and_the_primary_token_is_never_reused(self) -> None:
        env = voice_env()
        env.pop(MODULE.OPERATOR_VOICE_TOKEN)
        keys, note, calls = self.read({}, env)
        self.assertEqual((keys, calls), ({}, []))
        self.assertIn("no dedicated token", note)

    def test_a_bad_project_or_token_never_fetches_and_stops_startup(self) -> None:
        for label, env, words in (
            ("project", voice_env(**{MODULE.OPERATOR_VOICE_PROJECT: "bad"}), "INFISICAL_FLEET_OPS_PROJECT_ID is invalid"),
            ("token", voice_env(**{MODULE.OPERATOR_VOICE_TOKEN: "not-a-token"}), "INFISICAL_OPERATOR_VOICE_TOKEN is invalid"),
        ):
            with self.subTest(label):
                with self.assertRaisesRegex(MODULE.LoadError, words):
                    self.read({}, env)

    def test_a_failed_read_of_a_bound_token_stops_startup_and_names_only_the_stage(self) -> None:
        with self.assertRaisesRegex(MODULE.LoadError, r"^read operator voice: HTTP 403$"):
            self.read(MODULE.LoadError("read operator voice: HTTP 403"))

    def test_half_a_pair_or_a_reference_stops_startup_and_names_the_missing_name(self) -> None:
        for label, secrets in (
            ("one name", {"ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL}),
            ("reference", {"ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL, "ZULIP_OPERATOR_API_KEY": "${a.b}"}),
            ("empty", {"ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL, "ZULIP_OPERATOR_API_KEY": "  "}),
        ):
            with self.subTest(label):
                with self.assertRaisesRegex(
                    MODULE.LoadError, r"Infisical is missing ZULIP_OPERATOR_API_KEY \(/zulip_operator\)"
                ) as caught:
                    self.read(secrets)
                self.assertNotIn(OPERATOR_EMAIL, str(caught.exception))


class BuildEnvVoiceTests(unittest.TestCase):
    def build(self, voice):
        def fetch(source, **kwargs):
            if kwargs.get("token") == VOICE_TOKEN:
                if isinstance(voice, Exception):
                    raise voice
                return voice
            if kwargs.get("path") == MODULE.OPERATOR_PATH:
                # The Operator's identity comes from /operator, never from the container copy.
                return {"OPERATOR_ZULIP_USER_ID": str(OPERATOR_ID), "OPERATOR_ZULIP_EMAIL": OPERATOR_EMAIL}
            if kwargs.get("path") == MODULE.WAKE_ROUTER_PATH:
                return {"SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY": "r" * 40, "WAKE_ROUTER_KEY": "w" * 40}
            return {}

        with (
            patch.object(MODULE, "server_codes", return_value={"MA"}),
            patch.object(MODULE, "fetch", side_effect=fetch),
        ):
            return MODULE.build_env(voice_env())

    def test_the_two_names_reach_the_daemon_and_every_bootstrap_credential_is_stripped(self) -> None:
        out, line = self.build(
            {"ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL, "ZULIP_OPERATOR_API_KEY": OPERATOR_KEY}
        )
        self.assertEqual(out["ZULIP_OPERATOR_API_KEY"], OPERATOR_KEY)
        for stripped in (
            MODULE.TOKEN,
            MODULE.OPERATOR_VOICE_TOKEN,
            "INFISICAL_AUTOMATION_CLIENT_ID",
            "INFISICAL_AUTOMATION_CLIENT_SECRET",
        ):
            self.assertNotIn(stripped, out)
        self.assertIn("operator voice keys from /zulip_operator: 2 of 2", line)
        self.assertNotIn(OPERATOR_KEY, line)

    def test_a_failed_voice_read_stops_startup(self) -> None:
        with self.assertRaisesRegex(MODULE.LoadError, "read operator voice: HTTP 401"):
            self.build(MODULE.LoadError("read operator voice: HTTP 401"))

    def test_what_the_server_loader_hands_over_is_enough_for_the_operator_credential(self) -> None:
        """The regression behind the 370 `operator-voice-unavailable` rows:  on the server instance
        the credential reader must be answered by the loader's own output, with no fleet-secrets,
        no machine identity and no fetch at all."""
        out, _ = self.build(
            {"ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL, "ZULIP_OPERATOR_API_KEY": OPERATOR_KEY}
        )

        def no_fetch(names):
            raise AssertionError("the reader went to the vault although the loader supplied the pair")

        creds = SI.operator_credentials(out, fetch=no_fetch)
        self.assertEqual((creds.email, creds.key), (OPERATOR_EMAIL, OPERATOR_KEY))
        self.assertIn("loader env", creds.source)
        self.assertNotIn(OPERATOR_KEY, creds.source)


class OperatorCredentialsFromEnvironmentTests(unittest.TestCase):
    def env(self, **overrides: str) -> dict[str, str]:
        base = {
            "OPERATOR_ZULIP_USER_ID": str(OPERATOR_ID),
            "OPERATOR_ZULIP_EMAIL": OPERATOR_EMAIL,
            "ZULIP_SITE": "https://realm.example.test",
            "ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL,
            "ZULIP_OPERATOR_API_KEY": OPERATOR_KEY,
        }
        base.update(overrides)
        return base

    def test_the_pair_in_the_environment_is_the_answer_without_a_project_or_a_fetch(self) -> None:
        creds = SI.operator_credentials(self.env())
        self.assertEqual((creds.email, creds.key), (OPERATOR_EMAIL, OPERATOR_KEY))

    def test_half_a_pair_is_refused_not_completed_from_the_vault(self) -> None:
        env = self.env()
        env.pop("ZULIP_OPERATOR_API_KEY")
        with self.assertRaises(Z.CredentialError) as caught:
            SI.operator_credentials(env, fetch=lambda names: self.fail("no vault read for a half pair"))
        self.assertIn("only one of the two names", str(caught.exception))

    def test_an_address_that_is_not_the_pinned_bot_is_refused(self) -> None:
        with self.assertRaises(Z.CredentialError) as caught:
            SI.operator_credentials(self.env(ZULIP_OPERATOR_EMAIL="seat-bot@realm.example.test"))
        self.assertIn("not the pinned service bot", str(caught.exception))

    def test_an_unresolved_reference_is_refused(self) -> None:
        with self.assertRaises(Z.CredentialError) as caught:
            SI.operator_credentials(self.env(ZULIP_OPERATOR_API_KEY="${folder.key}"))
        self.assertIn("unresolved reference", str(caught.exception))

    def test_a_partial_reference_is_refused_too(self) -> None:
        with self.assertRaises(Z.CredentialError) as caught:
            SI.operator_credentials(self.env(ZULIP_OPERATOR_API_KEY="abc${folder.key}def"))
        self.assertIn("unresolved reference", str(caught.exception))

    def test_with_no_pair_in_the_environment_the_reader_is_asked_as_before(self) -> None:
        env = self.env()
        env.pop("ZULIP_OPERATOR_EMAIL")
        env.pop("ZULIP_OPERATOR_API_KEY")
        env["INFISICAL_FLEET_OPS_PROJECT_ID"] = FLEET_OPS_PROJECT
        asked = []

        def fetch(names):
            asked.append(names)
            return {"ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL, "ZULIP_OPERATOR_API_KEY": OPERATOR_KEY}

        creds = SI.operator_credentials(env, fetch=fetch)
        self.assertEqual(creds.key, OPERATOR_KEY)
        self.assertEqual(len(asked), 1)
        self.assertIn("infisical Fleet-OPS prod /zulip_operator", creds.source)



if __name__ == "__main__":
    unittest.main()
