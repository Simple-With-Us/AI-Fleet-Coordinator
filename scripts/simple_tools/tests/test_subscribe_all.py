"""subscribe_all against the fake Zulip server:  public channels only, bots only, dry run by
default, names-only output, and never a key in what it prints."""
from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from simple_tools import subscribe_all as S
from simple_tools import zulip as Z
from simple_tools.tests.fake_zulip import FakeZulip


class SubscribeAllTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key = secrets.token_hex(16)
        self.fake = FakeZulip(self.key)
        self.fake.start()
        self.addCleanup(self.fake.stop)
        self.fake.streams["private-ops"] = 50
        self.human_key = secrets.token_hex(16)
        self.fake.keys[self.human_key] = self.fake.user_by_id(12)  # Jay, a human:  never touched
        self.admin_key = secrets.token_hex(16)
        self.boss = self.fake.add_bot("boss-bot@zulip.test", "Boss", self.admin_key, role=200)
        self.owner_key = secrets.token_hex(16)
        self.fake.add_bot("principal-bot@zulip.test", "Principal", self.owner_key, role=100)
        self.mixed_key = secrets.token_hex(16)
        self.mixed = self.fake.add_bot("mixed-bot@zulip.test", "Mixed", self.mixed_key, role=200)
        # An inconsistent record:  role says administrator but the owner flag is set.  The owner
        # gate reads the flag, not the role, so this bot is never touched.
        self.mixed["is_owner"] = True

    def creds(self, key: str, email: str) -> Z.Credentials:
        return Z.Credentials(email=email, key=key, site=self.fake.url, source="test")

    def all_creds(self) -> list[Z.Credentials]:
        return [self.creds(self.key, "claude-bot@zulip.test"), self.creds(self.human_key, "jay@zulip.test"),
                self.creds(self.admin_key, "boss-bot@zulip.test"), self.creds(self.owner_key, "principal-bot@zulip.test"),
                self.creds(self.mixed_key, "mixed-bot@zulip.test")]

    def test_dry_run_changes_nothing_and_names_the_missing_public_channel(self) -> None:
        out = io.StringIO()
        self.assertEqual(S.run(self.all_creds(), self.fake.url, apply=False, out=out), 0)
        text = out.getvalue()
        self.assertIn("would join #sandbox", text)
        self.assertNotIn("private-ops", text)
        self.assertEqual(self.fake.requests_to("POST", "users/me/subscriptions"), [])
        self.assertIn("2 bots checked", text,
                      "the human and both owner-flagged bots are skipped; a bot that is also an admin is checked")
        for secret in (self.key, self.human_key, self.admin_key, self.owner_key):
            self.assertNotIn(secret, text)
        self.assertNotIn("@zulip.test", text, "seat tags only, never an email")

    def test_apply_joins_public_channels_quietly_and_only_for_bots(self) -> None:
        out = io.StringIO()
        self.assertEqual(S.run(self.all_creds(), self.fake.url, apply=True, out=out), 0)
        self.assertIn("sandbox", self.fake.subscribed)
        self.assertNotIn("private-ops", self.fake.subscribed)
        self.assertIn("2 bots checked", out.getvalue(),
                      "the human and both owner-flagged bots are skipped; the admin bot is checked like any other bot")
        posts = self.fake.requests_to("POST", "users/me/subscriptions")
        # The fake keeps one global subscription set, so the first bot's join covers the channel
        # for the run; the assertion that matters here is the two bots checked above.
        self.assertEqual([r.user_id for r in posts], [10])
        self.assertEqual(json.loads(posts[0].params["subscriptions"]), [{"name": "sandbox"}])
        self.assertTrue(all(p["value"] is False for p in self.fake.subscription_properties))
        again = io.StringIO()
        S.run(self.all_creds(), self.fake.url, apply=True, out=again)
        self.assertIn("joined nothing", again.getvalue(), "a second run is a no-op")

    def test_infisical_uses_scoped_service_token_and_never_logs_in(self) -> None:
        from unittest import mock
        import os
        from fleet_secrets import core as scoped_secrets

        token_name = S.INFISICAL_TOKEN_ENV
        previous = os.environ.get(token_name)
        scoped_token = "st." + secrets.token_urlsafe(32)
        os.environ[token_name] = scoped_token
        calls = []
        def request(method, path, request_token, body, env, **kwargs):
            calls.append((method, path, request_token, env))
            if path == "/v2/service-token":
                return 200, {
                    "workspace": S.INFISICAL_PROJECT,
                    "projectId": S.INFISICAL_PROJECT,
                    "permissions": ["read"],
                    "scopes": [{"environment": "prod", "secretPath": "/zulip"}],
                    "expiresAt": None,
                }
            return 200, {"secrets": []}
        original = scoped_secrets._api_json
        scoped_secrets._api_json = request
        try:
            self.assertEqual(S.infisical_credentials(self.fake.url), [])
        finally:
            scoped_secrets._api_json = original
            if previous is None:
                os.environ.pop(token_name, None)
            else:
                os.environ[token_name] = previous
        self.assertEqual(calls[0][1], "/v2/service-token")
        self.assertTrue(calls[1][1].startswith("/v3/secrets/raw?"))
        self.assertTrue(all(call[2] == scoped_token for call in calls))
        self.assertEqual(calls[1][3][scoped_secrets.ENV_READ_PATHS], "/zulip")

    def test_infisical_wrong_scope_fails_before_secret_fetch(self) -> None:
        import os
        from unittest import mock
        from fleet_secrets import core as scoped_secrets

        scoped_token = "st." + secrets.token_urlsafe(32)
        calls = []
        def request(method, path, request_token, body, env, **kwargs):
            calls.append(path)
            return 200, {
                "workspace": S.INFISICAL_PROJECT,
                "projectId": S.INFISICAL_PROJECT,
                "permissions": ["read"],
                "scopes": [{"environment": "prod", "secretPath": "/other"}],
                "expiresAt": None,
            }
        with mock.patch.dict(os.environ, {S.INFISICAL_TOKEN_ENV: scoped_token}):
            with mock.patch.object(scoped_secrets, "_api_json", request):
                with self.assertRaises(S.Failure):
                    S.infisical_credentials(self.fake.url)
        self.assertEqual(calls, ["/v2/service-token"])

    def test_infisical_missing_or_malformed_token_fails_before_request(self) -> None:
        import os

        token_name = S.INFISICAL_TOKEN_ENV
        previous = os.environ.pop(token_name, None)
        original = S._http_json
        S._http_json = lambda *args, **kwargs: self.fail("unexpected network request")
        try:
            with self.assertRaisesRegex(S.Failure, token_name):
                S.infisical_credentials(self.fake.url)
            os.environ[token_name] = "automation.client-secret"
            with self.assertRaisesRegex(S.Failure, token_name):
                S.infisical_credentials(self.fake.url)
        finally:
            S._http_json = original
            os.environ.pop(token_name, None)
            if previous is not None:
                os.environ[token_name] = previous

    def test_listener_daemon_imports_without_the_fleet_secrets_package(self) -> None:
        repo = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as empty_image:
            scripts = Path(empty_image) / "scripts"
            scripts.mkdir()
            shutil.copytree(
                repo / "scripts" / "simple_tools",
                scripts / "simple_tools",
                ignore=shutil.ignore_patterns("tests", "__pycache__"),
            )
            shutil.copy2(repo / "scripts" / "simple-chat", scripts / "simple-chat")
            env = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": str(scripts),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            result = subprocess.run(
                [sys.executable, "-c", "from simple_tools.daemon import Daemon"],
                cwd=empty_image,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_infisical_cli_reports_missing_host_helper_without_traceback(self) -> None:
        repo = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as empty_image:
            scripts = Path(empty_image) / "scripts"
            scripts.mkdir()
            shutil.copytree(
                repo / "scripts" / "simple_tools",
                scripts / "simple_tools",
                ignore=shutil.ignore_patterns("tests", "__pycache__"),
            )
            env = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": str(scripts),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            result = subprocess.run(
                [sys.executable, "-m", "simple_tools.subscribe_all", "--source", "infisical"],
                cwd=empty_image,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("Infisical source unavailable", result.stderr)
        self.assertNotIn("ModuleNotFoundError", result.stderr)


if __name__ == "__main__":
    unittest.main()
