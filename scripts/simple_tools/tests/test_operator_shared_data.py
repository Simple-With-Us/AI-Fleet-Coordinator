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


if __name__ == "__main__":
    unittest.main()
