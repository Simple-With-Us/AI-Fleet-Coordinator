"""Exercise sheet/health/Cursor secret loaders through the shared isolated CLI."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cursor_chat_surfaces as cursor

WORKER_SPEC = importlib.util.spec_from_file_location(
    "fleet_worker_health_test", ROOT / "fleet-worker-health.py"
)
worker = importlib.util.module_from_spec(WORKER_SPEC)
assert WORKER_SPEC and WORKER_SPEC.loader
WORKER_SPEC.loader.exec_module(worker)

FIXTURE_CREDENTIAL = "st.test-only-grok-key"
BAD_INPUT = "st.invalid-test-binding"
PROJECT = str(uuid4())
VALUES = {
    "HEALTH_HMAC_KEY": "synthetic-health-key",
    "CURSOR_API_KEY": "synthetic-cursor-key",
    "CURSOR_SYNC_API_KEY": "synthetic-cursor-sync-key",
    "GOOGLE_SHEETS_SA": '{"client_email":"synthetic@example.invalid"}',
}


class Provider(BaseHTTPRequestHandler):
    allowed_paths = {"/"}
    requests: list[str] = []

    def log_message(self, *_args):
        return

    def _send(self, status: int, data: dict) -> None:
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        auth = self.headers.get("Authorization", "")
        if self.path == "/api/v2/service-token":
            if auth != "Bearer " + FIXTURE_CREDENTIAL:
                self._send(401, {})
                return
            self._send(
                200,
                {
                    "workspace": PROJECT,
                    "permissions": ["read"],
                    "scopes": [
                        {"environment": "prod", "secretPath": path}
                        for path in sorted(self.allowed_paths)
                    ],
                    "expiresAt": "2099-01-01T00:00:00Z",
                },
            )
            return
        parsed = urlsplit(self.path)
        if not parsed.path.startswith("/api/v3/secrets/raw/"):
            self._send(404, {})
            return
        name = parsed.path.rsplit("/", 1)[-1]
        path = parse_qs(parsed.query).get("secretPath", [""])[0]
        self.requests.append(f"{name}:{path}")
        if (
            auth != "Bearer " + FIXTURE_CREDENTIAL
            or path not in self.allowed_paths
            or name not in VALUES
        ):
            self._send(404, {})
            return
        self._send(
            200,
            {
                "secret": {
                    "secretKey": name,
                    "secretValue": VALUES[name],
                    "secretValueHidden": False,
                    "type": "shared",
                    "workspace": PROJECT,
                    "environment": "prod",
                    "secretPath": path,
                }
            },
        )


class IsolatedInfisicalLoaders(unittest.TestCase):
    def setUp(self):
        self.cwd_before = os.getcwd()
        self.server = HTTPServer(("127.0.0.1", 0), Provider)
        Provider.requests = []
        Provider.allowed_paths = {"/"}
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.env_before = dict(os.environ)
        for name in (
            "HEALTH_HMAC_KEY",
            "CURSOR_API_KEY",
            "CURSOR_SYNC_API_KEY",
            "GOOGLE_SHEETS_SA",
        ):
            os.environ.pop(name, None)
        os.environ.update(
            {
                "INFISICAL_TOKEN": FIXTURE_CREDENTIAL,
                "INFISICAL_FLEET_OPS_PROJECT_ID": PROJECT,
                "INFISICAL_ENVIRONMENT": "prod",
                "FLEET_SECRETS_API": f"http://127.0.0.1:{self.server.server_port}/api",
            }
        )

    def tearDown(self):
        os.chdir(self.cwd_before)
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        os.environ.clear()
        os.environ.update(self.env_before)

    def test_worker_and_staged_cursor_loader_use_the_cli_from_hostile_cwd(self):
        with tempfile.TemporaryDirectory() as temp:
            hostile = Path(temp)
            (hostile / "fleet_infisical.py").write_text("raise AssertionError('wrong client')\n")
            (hostile / "fleet_infisical_cli.py").write_text("raise AssertionError('wrong CLI')\n")
            os.environ["PYTHONPATH"] = str(hostile)
            os.chdir(hostile)

            self.assertEqual(worker.read_key(), VALUES["HEALTH_HMAC_KEY"])

            old_live = cursor.LIVE_DIR
            cursor.LIVE_DIR = hostile / "cursor-chat-surfaces"
            try:
                cursor.copy_live_files()
                staged_spec = importlib.util.spec_from_file_location(
                    "staged_cursor_chat_surfaces", cursor.LIVE_DIR / "cursor_chat_surfaces.py"
                )
                staged = importlib.util.module_from_spec(staged_spec)
                assert staged_spec and staged_spec.loader
                staged_spec.loader.exec_module(staged)
                self.assertEqual(staged._infisical_api_key(), VALUES["CURSOR_API_KEY"])
                self.assertEqual(
                    (cursor.LIVE_DIR / "fleet_infisical_cli.py").read_bytes(),
                    (ROOT / "fleet_infisical_cli.py").read_bytes(),
                )
                self.assertEqual(
                    (cursor.LIVE_DIR / "fleet_infisical.py").read_bytes(),
                    (ROOT / "fleet_infisical.py").read_bytes(),
                )
            finally:
                cursor.LIVE_DIR = old_live
                os.chdir(self.cwd_before)

        self.assertIn("HEALTH_HMAC_KEY:/", Provider.requests)
        self.assertIn("CURSOR_API_KEY:/", Provider.requests)

    def test_missing_and_wrong_binding_return_no_value(self):
        old_token = os.environ.pop("INFISICAL_TOKEN")
        try:
            self.assertEqual(worker.read_key(), "")
            self.assertIsNone(cursor._infisical_api_key())
        finally:
            os.environ["INFISICAL_TOKEN"] = old_token

        os.environ["INFISICAL_TOKEN"] = "st.invalid-test-binding"
        self.assertEqual(worker.read_key(), "")
        self.assertIsNone(cursor._infisical_api_key())
        self.assertNotIn("st.invalid-test-binding", " ".join(Provider.requests))

    def test_wrong_scope_fails_before_secret_fetch(self):
        Provider.allowed_paths = {"/unrelated"}
        self.assertEqual(worker.read_key(), "")
        self.assertEqual(Provider.requests, [])

    def test_canonical_cli_isolated_entry_returns_no_value_on_bad_binding(self):
        with tempfile.TemporaryDirectory() as temp:
            hostile = Path(temp)
            (hostile / "fleet_infisical.py").write_text("raise AssertionError('wrong client')\n")
            (hostile / "fleet_infisical_cli.py").write_text("raise AssertionError('wrong CLI')\n")
            bad_env = dict(os.environ, INFISICAL_TOKEN=BAD_INPUT, PYTHONPATH=temp)
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(ROOT / "fleet_infisical_cli.py"),
                    "get",
                    "HEALTH_HMAC_KEY",
                    "--path",
                    "/",
                    "--project",
                    "fleet-ops",
                ],
                cwd=hostile,
                env=bad_env,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")
            self.assertNotIn("st.invalid-test-binding", result.stderr)


if __name__ == "__main__":
    unittest.main()
