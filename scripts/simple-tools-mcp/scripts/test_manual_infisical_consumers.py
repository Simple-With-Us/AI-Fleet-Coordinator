"""Exercise the production Cloudflare loader and shared CLI against loopback only."""

import http.server
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import tempfile
import threading
import unittest
import urllib.parse
from unittest import mock

SCRIPTS = pathlib.Path(__file__).resolve().parents[2]
SOURCE = SCRIPTS / "simple-tools-mcp" / "infra_phase0.py"
PROJECT_ID = "00000000-0000-4000-8000-000000000001"
WRONG_PROJECT_ID = "00000000-0000-4000-8000-000000000002"
FIXTURE_CREDENTIAL = "st.test-only-grok-key"
TEST_VALUE = "fixture-only-cloudflare-api-key"
TEST_NAME = "CLOUDFLARE_JAY_API_KEY"
FIXED_ERROR = (
    "error: CLOUDFLARE_JAY_API_KEY is not in the environment or Infisical "
    "(name only; no value printed)"
)

spec = importlib.util.spec_from_file_location("infra_phase0_manual_test", SOURCE)
infra_phase0 = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(infra_phase0)


class FixtureInfisical(http.server.BaseHTTPRequestHandler):
    requests = []

    def do_GET(self):
        type(self).requests.append((self.path, self.headers.get("Authorization"), self.command))
        if self.path == "/api/v2/service-token":
            payload = {
                "id": "fixture-only",
                "workspace": PROJECT_ID,
                "projectId": PROJECT_ID,
                "permissions": ["read"],
                "scopes": [{"environment": "prod", "secretPath": "/"}],
                "expiresAt": "2099-01-01T00:00:00Z",
            }
        elif self.path.startswith(f"/api/v3/secrets/raw/{TEST_NAME}?"):
            payload = {
                "secret": {
                    "secretKey": TEST_NAME,
                    "secretPath": "/",
                    "workspace": PROJECT_ID,
                    "environment": "prod",
                    "type": "shared",
                    "secretValueHidden": False,
                    "secretValue": TEST_VALUE,
                }
            }
        else:
            self.send_error(404)
            return
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


class ManualInfisicalConsumerTests(unittest.TestCase):
    def call_production_loader(self, bindings):
        FixtureInfisical.requests = []
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FixtureInfisical)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = {
            "PATH": os.defpath,
            "HOME": tempfile.gettempdir(),
            "FLEET_SECRETS_API": f"http://127.0.0.1:{server.server_port}/api",
            "INFISICAL_READ_PATHS": "/",
            **bindings,
        }
        try:
            with mock.patch.dict(os.environ, env, clear=True):
                try:
                    return infra_phase0._load(TEST_NAME), None
                except SystemExit as error:
                    return None, str(error)
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_production_loader_uses_its_own_project_root_and_named_key(self):
        value, error = self.call_production_loader(
            {
                "INFISICAL_TOKEN": FIXTURE_CREDENTIAL,
                "INFISICAL_FLEET_OPS_PROJECT_ID": PROJECT_ID,
            }
        )
        self.assertIsNone(error)
        self.assertEqual(value, TEST_VALUE)
        self.assertEqual(len(FixtureInfisical.requests), 2)
        metadata, secret = FixtureInfisical.requests
        self.assertEqual(metadata, ("/api/v2/service-token", f"Bearer {FIXTURE_CREDENTIAL}", "GET"))
        parsed = urllib.parse.urlsplit(secret[0])
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(parsed.path, f"/api/v3/secrets/raw/{TEST_NAME}")
        self.assertEqual(secret[1:], (f"Bearer {FIXTURE_CREDENTIAL}", "GET"))
        self.assertEqual(query["workspaceId"], [PROJECT_ID])
        self.assertEqual(query["environment"], ["prod"])
        self.assertEqual(query["secretPath"], ["/"])
        self.assertEqual(query["viewSecretValue"], ["true"])

    def test_missing_or_other_project_token_refuses_before_provider_access(self):
        bindings = {
            "INFISICAL_AI_FLEET_COORDINATOR_TOKEN": FIXTURE_CREDENTIAL,
            "INFISICAL_AI_FLEET_COORDINATOR_PROJECT_ID": PROJECT_ID,
            "INFISICAL_FLEET_OPS_PROJECT_ID": PROJECT_ID,
        }
        value, error = self.call_production_loader(bindings)
        self.assertIsNone(value)
        self.assertEqual(error, FIXED_ERROR)
        self.assertEqual(FixtureInfisical.requests, [])
        self.assertNotIn(TEST_VALUE, error)

    def test_wrong_own_project_id_refuses_before_named_value_read(self):
        value, error = self.call_production_loader(
            {
                "INFISICAL_TOKEN": FIXTURE_CREDENTIAL,
                "INFISICAL_FLEET_OPS_PROJECT_ID": WRONG_PROJECT_ID,
            }
        )
        self.assertIsNone(value)
        self.assertEqual(error, FIXED_ERROR)
        self.assertEqual(len(FixtureInfisical.requests), 1)
        self.assertEqual(
            FixtureInfisical.requests[0],
            ("/api/v2/service-token", f"Bearer {FIXTURE_CREDENTIAL}", "GET"),
        )
        self.assertNotIn(TEST_VALUE, error)


class HetznerReaderDeliveryTests(unittest.TestCase):
    def test_incomplete_reader_pair_refuses_before_reader_or_provider_child(self):
        source = (SCRIPTS / "check-hetzner-cx43.sh").read_text()
        log_lines = re.findall(r'^LOG="[^"\n]*"$', source, flags=re.MULTILINE)
        self.assertEqual(len(log_lines), 1)
        log_line = log_lines[0]
        for delivered in ((), ("fleet_infisical_cli.py",), ("fleet_infisical.py",)):
            with self.subTest(delivered=delivered), tempfile.TemporaryDirectory() as temp:
                root = pathlib.Path(temp)
                log = root / "watch.log"
                marker = root / "child-called"
                launcher = root / "check-hetzner-cx43.sh"
                launcher.write_text(source.replace(log_line, f'LOG="{log}"'))
                stub = (
                    "from pathlib import Path\n"
                    f"Path({str(marker)!r}).write_text('reader')\n"
                    "print('fixture-only-not-a-live-value')\n"
                )
                for name in delivered:
                    (root / name).write_text(stub)
                binaries = root / "bin"
                binaries.mkdir()
                curl = binaries / "curl"
                curl.write_text(
                    "#!/bin/sh\n"
                    f"printf provider > '{marker}'\n"
                    "printf fixture-only-not-a-live-value\n"
                    "exit 99\n"
                )
                curl.chmod(0o700)
                result = subprocess.run(
                    ["/bin/bash", str(launcher)],
                    cwd=root,
                    env={"PATH": f"{binaries}:{os.defpath}", "HOME": str(root)},
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertEqual(result.stderr, "")
                self.assertFalse(marker.exists())
                diagnostic = log.read_text()
                self.assertRegex(
                    diagnostic,
                    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}  ERROR: shared Infisical reader pair missing\n$",
                )
                self.assertNotIn("fixture-only-not-a-live-value", diagnostic)
                self.assertNotIn("HCLOUD_TOKEN missing/empty", diagnostic)


if __name__ == "__main__":
    unittest.main()
