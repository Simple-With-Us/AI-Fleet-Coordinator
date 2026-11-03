"""Offline test of scripts/telemetry/configure-agents.sh (mock Infisical, temp HOME)."""
from __future__ import annotations

import json
import http.server
import os
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from relay_common import CONFIGURE, PRODUCERS

scripts_path = Path(__file__).resolve().parents[2] / "scripts"
sys.path[:0] = [str(scripts_path), str(scripts_path / "telemetry")]
from configure_agents import PROJECT_ID, fetch_export
from fleet_secrets import core


def _token() -> str:
    return "relay-" + secrets.token_hex(16)


class ConfigureAgentsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="telemetry-relay-cfg-"))
        self.home = self.tmp / "home"
        self.out = self.tmp / "out"
        self.bin = self.tmp / "bin"
        self.home.mkdir()
        self.bin.mkdir()
        self.tokens = {
            "RELAY_BASE_URL": "https://relay.example.test",
            "RELAY_BEARER_CLAUDE_CODE": _token(),
            "RELAY_BEARER_CODEX": _token(),
            "RELAY_BEARER_GROK": _token(),
            "RELAY_BEARER_SHIM": _token(),
        }
        self.project = PROJECT_ID
        self.service_token = "st." + secrets.token_urlsafe(32)
        self.metadata = {
            "projectId": self.project,
            "workspace": self.project,
            "permissions": ["read"],
            "scopes": [{"environment": "prod", "secretPath": "/"}],
            "expiresAt": None,
        }
        self.requests = []
        self.request_lock = threading.Lock()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self._stop_server)
        self.env = {
            **os.environ,
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.home),
            "INFISICAL_PROJECT_ID": self.project,
            "INFISICAL_TELEMETRY_CONFIG_READ_TOKEN": self.service_token,
            "FLEET_SECRETS_API": "http://127.0.0.1:%d/api" % self.server.server_address[1],
            "OUT_DIR": str(self.out),
            "RELAY_URL": "",
        }

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)

    def _handler(self):
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                with owner.request_lock:
                    owner.requests.append((self.path, self.headers.get("Authorization")))
                parsed = urllib.parse.urlsplit(self.path)
                if self.headers.get("Authorization") != "Bearer " + owner.service_token:
                    self.send_response(401)
                    body = b"{}"
                elif parsed.path == "/api/v2/service-token":
                    self.send_response(200)
                    body = json.dumps(owner.metadata).encode()
                elif parsed.path == "/api/v3/secrets/raw":
                    params = urllib.parse.parse_qs(parsed.query)
                    owner.query = params
                    self.send_response(200)
                    body = json.dumps({"secrets": [
                        {"secretKey": key, "secretValue": value, "type": "shared",
                         "workspace": owner.project, "environment": "prod", "secretPath": "/"}
                        for key, value in owner.tokens.items()
                    ]}).encode()
                else:
                    self.send_response(404)
                    body = b"{}"
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def tearDown(self) -> None:
        # Temp tree only; never touch the real HOME.
        for child in sorted(self.tmp.rglob("*"), reverse=True):
            if child.is_file() or child.is_symlink():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        self.tmp.rmdir()

    def _run(self, *args: str, extra: dict | None = None) -> subprocess.CompletedProcess:
        env = dict(self.env)
        if extra:
            env.update(extra)
        return subprocess.run(
            ["bash", str(CONFIGURE), *args],
            cwd=str(self.tmp),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_writes_snippets_and_prints_no_secrets(self) -> None:
        proc = self._run("--relay-url", "https://relay.example.test")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        combined = proc.stdout + proc.stderr
        for name, value in self.tokens.items():
            if name == "RELAY_BASE_URL":
                continue
            self.assertNotIn(value, combined, name)
            self.assertIn("%s=set" % name, proc.stdout)
        self.assertTrue((self.out / "claude-settings.json").is_file())
        self.assertTrue((self.out / "codex-config-otel.toml").is_file())
        self.assertTrue((self.out / "grok-telemetry.json").is_file())
        self.assertTrue((self.out / "README.txt").is_file())
        for path in self.out.iterdir():
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600, path.name)
        # Nothing landed in the fake HOME (the script must not target a Mac).
        leftover = [p for p in self.home.rglob("*") if p != self.home]
        self.assertEqual(leftover, [])

    def test_snippets_point_at_the_relay_ports_and_keep_prompts(self) -> None:
        proc = self._run("--relay-url", "https://relay.example.test")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        claude = json.loads((self.out / "claude-settings.json").read_text(encoding="utf-8"))
        env = claude["env"]
        self.assertEqual(env["OTEL_EXPORTER_OTLP_ENDPOINT"], "https://relay.example.test:4318")
        self.assertEqual(env["OTEL_LOG_USER_PROMPTS"], "1")
        self.assertEqual(env["OTEL_LOG_TOOL_DETAILS"], "1")
        self.assertEqual(env["CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"], "1")
        self.assertIn("signal=agent", env["OTEL_RESOURCE_ATTRIBUTES"])
        self.assertIn(self.tokens["RELAY_BEARER_CLAUDE_CODE"], env["OTEL_EXPORTER_OTLP_HEADERS"])
        codex = (self.out / "codex-config-otel.toml").read_text(encoding="utf-8")
        self.assertIn("https://relay.example.test:4319", codex)
        self.assertIn("log_user_prompt = true", codex)
        grok = json.loads((self.out / "grok-telemetry.json").read_text(encoding="utf-8"))
        self.assertEqual(grok["telemetry"]["otel_endpoint"], "https://relay.example.test:4320")
        self.assertTrue(grok["telemetry"]["otel_log_user_prompts"])

    def test_infisical_call_is_prod_only(self) -> None:
        proc = self._run("--relay-url", "https://relay.example.test")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.query["workspaceId"], [self.project])
        self.assertEqual(self.query["environment"], ["prod"])
        self.assertEqual(self.query["secretPath"], ["/"])
        self.assertEqual([path.split("?", 1)[0] for path, _ in self.requests],
                         ["/api/v2/service-token", "/api/v3/secrets/raw"])
        self.assertTrue(all(auth == "Bearer " + self.service_token for _, auth in self.requests))

    def test_missing_dedicated_token_rejects_legacy_credentials_without_api_call(self) -> None:
        proc = self._run(extra={
            "INFISICAL_TELEMETRY_CONFIG_READ_TOKEN": "",
            "INFISICAL_TOKEN": "st.generic-unrelated-token-fixture",
            "INFISICAL_AUTOMATION_CLIENT_ID": "legacy-id-fixture",
            "INFISICAL_AUTOMATION_CLIENT_SECRET": "legacy-secret-fixture",
        })
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("INFISICAL_TELEMETRY_CONFIG_READ_TOKEN", proc.stderr)
        self.assertEqual(self.requests, [])
        self.assertFalse(self.out.exists())

    def test_inexact_metadata_fails_before_secret_read(self) -> None:
        self.metadata["scopes"] = [{"environment": "prod", "secretPath": "/"},
                                   {"environment": "prod", "secretPath": "/other"}]
        proc = self._run("--relay-url", "https://relay.example.test")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual([path.split("?", 1)[0] for path, _ in self.requests],
                         ["/api/v2/service-token"])
        self.assertFalse(self.out.exists())

    def test_empty_bearer_fails_closed(self) -> None:
        self.tokens["RELAY_BEARER_CODEX"] = ""
        proc = self._run("--relay-url", "https://relay.example.test")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("RELAY_BEARER_CODEX=empty", proc.stdout + proc.stderr)
        self.assertFalse((self.out / "claude-settings.json").exists())

    def test_script_has_no_jay_mac_path_and_no_home_writes(self) -> None:
        src = CONFIGURE.read_text(encoding="utf-8")
        self.assertNotIn("/Users/jay", src)
        self.assertIn("SHIPPED ONLY", src)
        # The only writes are OUT_DIR and a temp report; $HOME is never a destination.
        self.assertNotIn('>"$HOME', src.replace(" ", ""))
        self.assertNotIn(">>$HOME", src.replace(" ", ""))
        self.assertNotRegex(src, r"(?m)^\s*(cp|mv|install)\s+.*~")

    def test_producer_ports_match_the_relay_contract(self) -> None:
        src = CONFIGURE.read_text(encoding="utf-8")
        self.assertIn(":4318", src)
        self.assertIn(":4319", src)
        self.assertIn(":4320", src)
        self.assertIn("4321", src)
        self.assertEqual(PRODUCERS["claude"]["port"], 4318)

    def test_helper_uses_dedicated_token_and_exact_scope_metadata(self) -> None:
        calls = []

        def api(method, path, token, body, env, **_kwargs):
            calls.append((method, path, token))
            if path == "/v2/service-token":
                return 200, self.metadata
            return 200, {"secrets": [
                {"secretKey": key, "secretValue": value, "type": "shared",
                 "workspace": self.project, "environment": "prod", "secretPath": "/"}
                for key, value in self.tokens.items()
            ]}

        env = {
            **self.env,
            "INFISICAL_TOKEN": "st.generic-unrelated-token-fixture",
            "INFISICAL_AUTOMATION_CLIENT_ID": "legacy-id-fixture",
            "INFISICAL_AUTOMATION_CLIENT_SECRET": "legacy-secret-fixture",
        }
        with mock.patch.object(core, "_api_json", side_effect=api):
            result = fetch_export(env)
        self.assertEqual(result, self.tokens)
        self.assertEqual([path.split("?", 1)[0] for _, path, _ in calls],
                         ["/v2/service-token", "/v3/secrets/raw"])
        self.assertTrue(all(token == self.service_token for _, _, token in calls))

    def test_helper_rejects_invalid_metadata_before_secret_read(self) -> None:
        bad_metadata = dict(self.metadata, projectId="00000000-0000-4000-8000-000000000099")
        calls = []

        def api(method, path, token, body, env, **_kwargs):
            calls.append(path)
            return (200, bad_metadata) if path == "/v2/service-token" else (200, {"secrets": []})

        with mock.patch.object(core, "_api_json", side_effect=api):
            with self.assertRaises(core.SecretsError):
                fetch_export(self.env)
        self.assertEqual(calls, ["/v2/service-token"])

    def test_helper_rejects_another_configured_project(self) -> None:
        with self.assertRaisesRegex(core.SecretsError, "project-id-mismatch"):
            fetch_export({**self.env, "INFISICAL_PROJECT_ID": "00000000-0000-4000-8000-000000000099"})
        self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
