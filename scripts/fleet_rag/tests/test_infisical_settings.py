"""infisical_settings (scripts/fleet_rag/infisical_settings.py) -- the Infisical
sole-source-of-truth contract for the fleet's Python services.

    cd scripts && python3 -m unittest fleet_rag.tests.test_infisical_settings -v

All network is mocked; no credentials, no Infisical account, no live HTTP.
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import threading
import time
import unittest
import urllib.error
from unittest import mock

from fleet_rag import infisical_settings as mod


PROJECT = "proj-test"
ENV = "dev"
MANAGED = ("API_TOKEN", "BACKEND_URL", "TIMEOUT_S", "SETTINGS_REFRESH_SECONDS")
REQUIRED = ("API_TOKEN",)
SECRETS = ("API_TOKEN",)


class _FakeHTTPResponse:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._payload).encode()


def _http_error(url, code):
    return urllib.error.HTTPError(url, code, "err", {}, io.BytesIO(b"{}"))


class _HttpWorld:
    """A programmable urlopen double.  Records (method, path, body, auth) of every call."""

    def __init__(self):
        self.calls: list[tuple] = []
        self.secrets: dict[str, str] = {}
        self.fail_login = False
        self.fail_fetch: list = []          # exceptions / statuses to raise/return in order
        self.fail_write = False
        self.patch_statuses: list[int] = []  # statuses to return for PATCH, in order

    def __call__(self, req, timeout=None):
        method = req.get_method()
        path = req.full_url.split("app.infisical.com", 1)[-1]
        body = json.loads(req.data.decode()) if req.data else None
        auth = req.get_header("Authorization")
        self.calls.append((method, path, body, auth))
        if path == "/api/v1/auth/universal-auth/login":
            if self.fail_login:
                raise urllib.error.URLError("login down")
            return _FakeHTTPResponse(200, {"accessToken": "tok-1"})
        if path.startswith("/api/v3/secrets/raw"):
            if method == "GET":
                if self.fail_fetch:
                    item = self.fail_fetch.pop(0)
                    if isinstance(item, Exception):
                        raise item
                    return _FakeHTTPResponse(item, {})
                return _FakeHTTPResponse(200, {
                    "secrets": [{"secretKey": k, "secretValue": v}
                                for k, v in self.secrets.items()]})
            # Writes go to PATCH|POST /api/v3/secrets/raw/<key> with secretValue in the body
            # (mirrors the fleet TypeScript pilot: PATCH first, POST on 404).
            if self.fail_write:
                raise urllib.error.URLError("write down")
            key = path.rsplit("/", 1)[-1]
            if self.patch_statuses and method == "PATCH":
                return _FakeHTTPResponse(self.patch_statuses.pop(0), {})
            if method == "PATCH" and key not in self.secrets:
                return _FakeHTTPResponse(404, {"message": "not found"})
            self.secrets[key] = body["secretValue"]
            return _FakeHTTPResponse(200, {})
        raise AssertionError("unexpected request " + method + " " + path)

    @property
    def network_calls(self):
        return len(self.calls)


class _SettingsCase(unittest.TestCase):
    def setUp(self):
        mod.reset_for_tests()
        self.world = _HttpWorld()
        self._urlopen = mock.patch("urllib.request.urlopen", self.world)
        self._urlopen.start()
        self._env = mock.patch.dict(os.environ, {
            "INFISICAL_AUTOMATION_CLIENT_ID": "cid",
            "INFISICAL_AUTOMATION_CLIENT_SECRET": "csec",
        }, clear=False)
        self._env.start()

    def tearDown(self):
        mod.stop_refresh()
        self._urlopen.stop()
        self._env.stop()
        mod.reset_for_tests()

    def make(self, **kw):
        params = dict(project_id=PROJECT, environment=ENV, managed_keys=MANAGED,
                      required_keys=REQUIRED, secret_keys=SECRETS)
        params.update(kw)
        return mod.InfisicalSettings(**params)


class InitTests(_SettingsCase):
    def test_init_populates_cache_from_infisical(self):
        self.world.secrets = {"API_TOKEN": "tok", "BACKEND_URL": "http://x", "TIMEOUT_S": "9"}
        s = self.make()
        s.init()
        self.assertEqual(s.get("API_TOKEN"), "tok")
        self.assertEqual(s.get("BACKEND_URL"), "http://x")
        self.assertIsNone(s.get("NOPE"))
        # Exactly two network calls: login + one secrets fetch.
        self.assertEqual(self.world.network_calls, 2)

    def test_runtime_reads_make_zero_network_calls_after_init(self):
        self.world.secrets = {"API_TOKEN": "tok"}
        s = self.make()
        s.init()
        before = self.world.network_calls
        for _ in range(50):
            s.get("API_TOKEN")
            s.present("API_TOKEN")
            s.admin_listing()
        self.assertEqual(self.world.network_calls, before)

    def test_missing_required_key_fails_fast_and_names_it(self):
        self.world.secrets = {"BACKEND_URL": "http://x"}   # API_TOKEN absent
        s = self.make()
        with self.assertRaises(mod.SettingsError) as ctx:
            s.init()
        self.assertIn("API_TOKEN", str(ctx.exception))
        self.assertIn("INFISICAL.md", str(ctx.exception))

    def test_no_identity_falls_back_to_env_without_network(self):
        with mock.patch.object(mod, "IDENTITY_FILE_DEFAULT",
                               pathlib.Path("/nonexistent-identity-file.json")), \
             mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INFISICAL_AUTOMATION_CLIENT_ID", None)
            os.environ.pop("INFISICAL_AUTOMATION_CLIENT_SECRET", None)
            os.environ.pop("INFISICAL_SHARED_CLIENT_ID", None)
            os.environ.pop("INFISICAL_SHARED_CLIENT_SECRET", None)
            os.environ.pop("INFISICAL_MACHINE_IDENTITY", None)
            self.world.secrets = {"API_TOKEN": "should-not-be-read"}
            s = self.make()
            with mock.patch.dict(os.environ, {"API_TOKEN": "env-tok", "TIMEOUT_S": "3"}):
                s.init()
            self.assertEqual(s.get("API_TOKEN"), "env-tok")
            self.assertEqual(s.get("TIMEOUT_S"), "3")
        self.assertEqual(self.world.network_calls, 0)

    def test_env_backfills_keys_missing_from_infisical(self):
        self.world.secrets = {"API_TOKEN": "tok"}   # TIMEOUT_S not in Infisical
        s = self.make()
        with mock.patch.dict(os.environ, {"TIMEOUT_S": "3"}):
            with self.assertLogs("infisical_settings", level="WARNING") as logs:
                s.init()
        self.assertEqual(s.get("API_TOKEN"), "tok")       # Infisical wins when present
        self.assertEqual(s.get("TIMEOUT_S"), "3")          # env bridges the gap
        self.assertTrue(any("TIMEOUT_S" in line for line in logs.output))

    def test_unmanaged_keys_are_ignored(self):
        self.world.secrets = {"API_TOKEN": "tok", "SOME_RANDOM_KEY": "x"}
        s = self.make()
        s.init()
        self.assertIsNone(s.get("SOME_RANDOM_KEY"))


class WriteThroughTests(_SettingsCase):
    def test_set_writes_infisical_before_cache(self):
        self.world.secrets = {"API_TOKEN": "tok"}
        s = self.make(required_keys=())
        s.init()
        order = []

        real = self.world

        def spy(req, timeout=None):
            # The Infisical write must land BEFORE the local cache changes.
            if req.get_method() in ("POST", "PATCH"):
                order.append(("write", s.get("BACKEND_URL")))
            return real(req, timeout=timeout)

        with mock.patch("urllib.request.urlopen", spy):
            s.set("BACKEND_URL", "http://new")   # new key: PATCH 404, then POST
        self.assertEqual(order, [("write", None), ("write", None)])  # cache empty during writes
        self.assertEqual(s.get("BACKEND_URL"), "http://new")   # cache updated after
        self.assertEqual(self.world.secrets["BACKEND_URL"], "http://new")

    def test_set_creates_new_key_via_post(self):
        self.world.secrets = {"API_TOKEN": "tok"}
        s = self.make(required_keys=())
        s.init()
        s.set("BACKEND_URL", "http://new")
        self.assertEqual(s.get("BACKEND_URL"), "http://new")
        self.assertEqual(self.world.secrets["BACKEND_URL"], "http://new")

    def test_set_updates_existing_key_via_patch(self):
        self.world.secrets = {"API_TOKEN": "tok", "TIMEOUT_S": "9"}
        s = self.make()
        s.init()
        s.set("TIMEOUT_S", "42")
        self.assertEqual(s.get("TIMEOUT_S"), "42")
        self.assertEqual(self.world.secrets["TIMEOUT_S"], "42")
        methods = [c[0] for c in self.world.calls if c[1].endswith("/TIMEOUT_S")]
        self.assertEqual(methods, ["PATCH"])   # no POST when the key exists

    def test_set_relogins_once_on_401_mid_write(self):
        self.world.secrets = {"API_TOKEN": "tok", "TIMEOUT_S": "9"}
        s = self.make()
        s.init()
        logins_before = sum(1 for c in self.world.calls if c[1].endswith("/login"))
        self.world.patch_statuses.append(401)
        s.set("TIMEOUT_S", "42")
        self.assertEqual(s.get("TIMEOUT_S"), "42")
        self.assertEqual(self.world.secrets["TIMEOUT_S"], "42")
        logins_after = sum(1 for c in self.world.calls if c[1].endswith("/login"))
        self.assertEqual(logins_after, logins_before + 1)

    def test_failed_write_rejects_and_leaves_cache(self):
        self.world.secrets = {"API_TOKEN": "tok", "TIMEOUT_S": "9"}
        self.world.fail_write = True
        s = self.make()
        s.init()
        with self.assertRaises(mod.SettingsError):
            s.set("TIMEOUT_S", "42")
        self.assertEqual(s.get("TIMEOUT_S"), "9")     # cache untouched

    def test_set_refuses_unmanaged_key(self):
        self.world.secrets = {"API_TOKEN": "tok"}
        s = self.make()
        s.init()
        with self.assertRaises(mod.SettingsError):
            s.set("NOT_MANAGED", "x")

    def test_set_requires_identity(self):
        s = self.make()
        with mock.patch.object(mod, "IDENTITY_FILE_DEFAULT",
                               pathlib.Path("/nonexistent-identity-file.json")), \
             mock.patch.dict(os.environ, {}, clear=False):
            for v in ("INFISICAL_AUTOMATION_CLIENT_ID", "INFISICAL_AUTOMATION_CLIENT_SECRET",
                      "INFISICAL_SHARED_CLIENT_ID", "INFISICAL_SHARED_CLIENT_SECRET",
                      "INFISICAL_MACHINE_IDENTITY"):
                os.environ.pop(v, None)
            with self.assertRaises(mod.SettingsError):
                s.set("TIMEOUT_S", "1")


class RefreshTests(_SettingsCase):
    def test_refresh_replaces_cache_on_success(self):
        self.world.secrets = {"API_TOKEN": "tok", "TIMEOUT_S": "9"}
        s = self.make()
        s.init()
        self.world.secrets["TIMEOUT_S"] = "11"
        self.assertTrue(s.refresh())
        self.assertEqual(s.get("TIMEOUT_S"), "11")

    def test_failed_refresh_keeps_last_known_good(self):
        self.world.secrets = {"API_TOKEN": "tok", "TIMEOUT_S": "9"}
        s = self.make()
        s.init()
        self.world.fail_fetch.append(urllib.error.URLError("boom"))
        with self.assertLogs("infisical_settings", level="ERROR"):
            self.assertFalse(s.refresh())
        self.assertEqual(s.get("TIMEOUT_S"), "9")
        self.assertEqual(s.get("API_TOKEN"), "tok")

    def test_refresh_relogins_once_on_401(self):
        self.world.secrets = {"API_TOKEN": "tok"}
        s = self.make()
        s.init()
        calls_before = self.world.network_calls
        self.world.fail_fetch.append(401)          # first GET -> 401
        self.assertTrue(s.refresh())
        # 401, re-login, retry: three more calls.
        self.assertEqual(self.world.network_calls, calls_before + 3)
        self.assertEqual(s.get("API_TOKEN"), "tok")

    def test_background_thread_refreshes_and_stops(self):
        self.world.secrets = {"API_TOKEN": "tok", "TIMEOUT_S": "9"}
        s = self.make()
        s.init()
        ticks = []
        s.add_refresh_listener(lambda: ticks.append(1))
        s.start_refresh(interval=0.05)
        deadline = time.time() + 5
        while not ticks and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(ticks, "refresh thread never fired")
        s.stop_refresh()
        n = len(ticks)
        time.sleep(0.15)
        self.assertEqual(len(ticks), n, "thread kept refreshing after stop")

    def test_refresh_interval_is_tunable_via_infisical(self):
        self.world.secrets = {"API_TOKEN": "tok", "SETTINGS_REFRESH_SECONDS": "60"}
        s = self.make()
        s.init()
        self.assertEqual(s.refresh_interval(), 60.0)
        # bad values fall back to the default, never to "no refresh"
        for bad in ("", "abc", "0", "-5"):
            self.world.secrets = {"API_TOKEN": "tok", "SETTINGS_REFRESH_SECONDS": bad}
            s.refresh()
            self.assertEqual(s.refresh_interval(), mod.DEFAULT_REFRESH_SECONDS, bad)


class AdminListingTests(_SettingsCase):
    def test_secrets_are_masked_nonsecrets_visible(self):
        self.world.secrets = {"API_TOKEN": "tok", "BACKEND_URL": "http://x"}
        s = self.make()
        s.init()
        listing = s.admin_listing()
        self.assertEqual(listing["API_TOKEN"], {"set": True, "value": "set"})
        self.assertEqual(listing["BACKEND_URL"], {"set": True, "value": "http://x"})
        self.assertEqual(listing["TIMEOUT_S"], {"set": False, "value": ""})
        for key in MANAGED:
            self.assertIn(key, listing)

    def test_export_env_mirrors_only_nonempty(self):
        self.world.secrets = {"API_TOKEN": "tok", "BACKEND_URL": "http://x", "TIMEOUT_S": ""}
        s = self.make()
        s.init()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("API_TOKEN", None)
            os.environ.pop("BACKEND_URL", None)
            exported = s.export_env(("API_TOKEN", "BACKEND_URL", "TIMEOUT_S"))
            self.assertEqual(sorted(exported), ["API_TOKEN", "BACKEND_URL"])
            self.assertEqual(os.environ["API_TOKEN"], "tok")
            self.assertNotIn("TIMEOUT_S", os.environ)


class ModuleDefaultTests(_SettingsCase):
    def test_get_returns_default_when_unconfigured(self):
        self.assertIsNone(mod.get("API_TOKEN"))
        self.assertEqual(mod.get("API_TOKEN", "d"), "d")
        self.assertFalse(mod.present("API_TOKEN"))
        self.assertEqual(mod.export_env(("API_TOKEN",)), [])

    def test_configure_init_set_roundtrip(self):
        self.world.secrets = {"API_TOKEN": "tok"}
        mod.configure(project_id=PROJECT, environment=ENV, managed_keys=MANAGED,
                      required_keys=REQUIRED, secret_keys=SECRETS)
        mod.init_settings()
        self.assertEqual(mod.get("API_TOKEN"), "tok")
        mod.set("TIMEOUT_S", "7")
        self.assertEqual(mod.get("TIMEOUT_S"), "7")


if __name__ == "__main__":
    unittest.main()
