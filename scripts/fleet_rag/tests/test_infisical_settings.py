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
ENV = "prod"
MANAGED = ("API_TOKEN", "BACKEND_URL", "TIMEOUT_S", "SETTINGS_REFRESH_SECONDS")
REQUIRED = ("API_TOKEN",)
SECRETS = ("API_TOKEN",)
# A well-shaped Infisical service token:  the st. prefix, no whitespace, 20 to 2048
# characters.  It is a fixture, not a credential.
SERVICE_TOKEN = "st." + "s3rv1c3t0k3nf1gurat10n"


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
        self.login_delay = 0.0             # seconds to hold a login open
        self.login_tokens: list[str] = []  # one access token per login, in order
        self.fail_fetch: list = []          # exceptions / statuses to raise/return in order
        self.fail_write = False
        self.patch_statuses: list[int] = []  # statuses to return for PATCH, in order
        self.post_statuses: list[int] = []   # statuses to return for POST, in order

    def __call__(self, req, timeout=None):
        method = req.get_method()
        path = req.full_url.split("app.infisical.com", 1)[-1]
        body = json.loads(req.data.decode()) if req.data else None
        auth = req.get_header("Authorization")
        self.calls.append((method, path, body, auth))
        if path == "/api/v1/auth/universal-auth/login":
            if self.login_delay:
                # Hold the login open so concurrent callers really overlap.
                time.sleep(self.login_delay)
            if self.fail_login:
                raise urllib.error.URLError("login down")
            if self.login_tokens:
                return _FakeHTTPResponse(200, {"accessToken": self.login_tokens.pop(0)})
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
            if self.post_statuses and method == "POST":
                return _FakeHTTPResponse(self.post_statuses.pop(0), {})
            if method == "PATCH" and key not in self.secrets:
                return _FakeHTTPResponse(404, {"message": "not found"})
            self.secrets[key] = body["secretValue"]
            return _FakeHTTPResponse(200, {})
        raise AssertionError("unexpected request " + method + " " + path)

    @property
    def network_calls(self):
        return len(self.calls)

    @property
    def login_calls(self):
        return sum(1 for c in self.calls if c[1].endswith("/login"))


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
        # The ambient environment must not carry a service token in, or every case
        # below would run on the token path instead of the one it means to test.
        os.environ.pop("INFISICAL_TOKEN", None)

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


class ServiceTokenTests(_SettingsCase):
    """INFISICAL_TOKEN is the first-match-wins credential.

    The service token is the bearer directly, so no universal-auth login happens on
    that path, and a 401 there cannot be answered by a re-login."""

    def _with_token(self, token):
        """The service token in the environment, with the automation pair still set."""
        return mock.patch.dict(os.environ, {"INFISICAL_TOKEN": token}, clear=False)

    def _logins(self):
        return [c for c in self.world.calls if c[1].endswith("/login")]

    def test_env_token_wins_over_the_automation_pair(self):
        self.world.secrets = {"API_TOKEN": "tok"}
        s = self.make()
        with self._with_token(SERVICE_TOKEN):
            s.init()
        self.assertEqual(s.get("API_TOKEN"), "tok")
        # One call, and it is the secrets fetch:  the pair was never logged in with.
        self.assertEqual(self.world.network_calls, 1)
        self.assertEqual(self.world.calls[0][0], "GET")
        self.assertEqual(self.world.calls[0][3], "Bearer " + SERVICE_TOKEN)

    def test_no_universal_auth_login_happens_on_the_token_path(self):
        self.world.secrets = {"API_TOKEN": "tok"}
        s = self.make()
        with self._with_token(SERVICE_TOKEN):
            s.init()
        self.assertEqual(self._logins(), [])
        for method, _path, _body, auth in self.world.calls:
            self.assertEqual(auth, "Bearer " + SERVICE_TOKEN)

    def test_401_on_the_token_path_raises_and_does_not_retry(self):
        self.world.secrets = {"API_TOKEN": "tok"}
        self.world.fail_fetch.append(401)
        s = self.make()
        with self._with_token(SERVICE_TOKEN):
            with self.assertRaises(mod.SettingsError) as ctx:
                s.init()
        self.assertIn("INFISICAL_TOKEN", str(ctx.exception))
        # One call:  the 401 was not retried and no login was attempted.
        self.assertEqual(self.world.network_calls, 1)
        self.assertEqual(self._logins(), [])

    def test_401_on_the_token_path_names_what_to_check(self):
        self.world.secrets = {"API_TOKEN": "tok"}
        self.world.fail_fetch.append(401)
        s = self.make()
        with self._with_token(SERVICE_TOKEN):
            with self.assertRaises(mod.SettingsError) as ctx:
                s.init()
        message = str(ctx.exception)
        self.assertIn("check that the token is current", message)
        self.assertIn("INFISICAL.md", message)

    def test_401_on_the_write_path_does_not_relogin(self):
        self.world.secrets = {"API_TOKEN": "tok", "TIMEOUT_S": "9"}
        s = self.make(required_keys=())
        with self._with_token(SERVICE_TOKEN):
            s.init()
            self.world.patch_statuses.append(401)
            with self.assertRaises(mod.SettingsError) as ctx:
                s.set("TIMEOUT_S", "42")
        self.assertIn("INFISICAL_TOKEN", str(ctx.exception))
        self.assertEqual(self._logins(), [])
        # The cache is untouched:  the save was rejected.
        self.assertEqual(s.get("TIMEOUT_S"), "9")

    def test_the_token_is_the_bearer_on_the_write_path(self):
        self.world.secrets = {"API_TOKEN": "tok", "TIMEOUT_S": "9"}
        s = self.make(required_keys=())
        with self._with_token(SERVICE_TOKEN):
            s.init()
            s.set("TIMEOUT_S", "42")
        self.assertEqual(s.get("TIMEOUT_S"), "42")
        writes = [c for c in self.world.calls if c[0] in ("PATCH", "POST")]
        self.assertTrue(writes)
        for _method, _path, _body, auth in writes:
            self.assertEqual(auth, "Bearer " + SERVICE_TOKEN)

    def test_the_token_never_appears_in_an_exception_message(self):
        s = self.make()
        failures = []
        # A malformed token.
        with self._with_token("not-a-service-token"):
            try:
                s.init()
            except mod.SettingsError as e:
                failures.append(str(e))
        # A 401 on the token path.
        self.world.fail_fetch.append(401)
        with self._with_token(SERVICE_TOKEN):
            try:
                s.init()
            except mod.SettingsError as e:
                failures.append(str(e))
        # A fetch that fails with another status.
        self.world.fail_fetch.append(500)
        with self._with_token(SERVICE_TOKEN):
            try:
                s.init()
            except mod.SettingsError as e:
                failures.append(str(e))
        self.assertEqual(len(failures), 3, failures)
        for message in failures:
            self.assertNotIn(SERVICE_TOKEN, message)
            self.assertNotIn("not-a-service-token", message)

    def test_the_token_never_appears_in_the_admin_listing(self):
        self.world.secrets = {"API_TOKEN": "tok", "INFISICAL_TOKEN": SERVICE_TOKEN}
        s = self.make(managed_keys=MANAGED + ("INFISICAL_TOKEN",))
        with self._with_token(SERVICE_TOKEN):
            s.init()
        listing = s.admin_listing()
        self.assertNotIn(SERVICE_TOKEN, json.dumps(listing))
        self.assertEqual(listing["INFISICAL_TOKEN"], {"set": True, "value": "set"})

    def test_the_token_is_never_exported_to_the_environment(self):
        self.world.secrets = {"API_TOKEN": "tok", "INFISICAL_TOKEN": SERVICE_TOKEN}
        s = self.make(managed_keys=MANAGED + ("INFISICAL_TOKEN",))
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INFISICAL_TOKEN", None)
            with self._with_token(SERVICE_TOKEN):
                s.init()
            os.environ.pop("INFISICAL_TOKEN", None)
            exported = s.export_env(("API_TOKEN", "INFISICAL_TOKEN"))
            self.assertEqual(exported, ["API_TOKEN"])
            self.assertNotIn("INFISICAL_TOKEN", os.environ)

    def test_no_credential_at_all_fails_closed(self):
        s = self.make()
        with mock.patch.object(mod, "IDENTITY_FILE_DEFAULT",
                               pathlib.Path("/nonexistent-identity-file.json")), \
             mock.patch.dict(os.environ, {}, clear=False):
            for var in ("INFISICAL_TOKEN", "INFISICAL_AUTOMATION_CLIENT_ID",
                        "INFISICAL_AUTOMATION_CLIENT_SECRET", "INFISICAL_SHARED_CLIENT_ID",
                        "INFISICAL_SHARED_CLIENT_SECRET", "INFISICAL_MACHINE_IDENTITY"):
                os.environ.pop(var, None)
            with mock.patch.dict(os.environ, {"API_TOKEN": "env-tok"}):
                s.init()
            self.assertEqual(s.get("API_TOKEN"), "env-tok")
            # No credential:  the write-through refuses instead of guessing.
            with self.assertRaises(mod.SettingsError):
                s.set("TIMEOUT_S", "1")
        self.assertEqual(self.world.network_calls, 0)

    def test_a_malformed_token_is_refused_before_any_api_call(self):
        bad = (
            "not-a-service-token",
            "st.short",
            "st." + "x" * 16,
            "st." + "x" * 2049,
            "st." + "has a space" + "x" * 20,
        )
        for token in bad:
            self.world.calls.clear()
            s = self.make()
            with self._with_token(token):
                with self.assertRaises(mod.SettingsError) as ctx:
                    s.init()
            self.assertIn("INFISICAL_TOKEN", str(ctx.exception), token)
            self.assertNotIn(token, str(ctx.exception), token)
            self.assertEqual(self.world.network_calls, 0, token)

    def test_refresh_on_the_token_path_uses_the_token_and_never_logs_in(self):
        self.world.secrets = {"API_TOKEN": "tok", "TIMEOUT_S": "9"}
        s = self.make()
        with self._with_token(SERVICE_TOKEN):
            s.init()
            self.world.secrets["TIMEOUT_S"] = "11"
            self.assertTrue(s.refresh())
            self.assertEqual(s.get("TIMEOUT_S"), "11")
        self.assertEqual(self._logins(), [])
        fetch = [c for c in self.world.calls if c[0] == "GET"][-1]
        self.assertEqual(fetch[3], "Bear" + "er " + SERVICE_TOKEN)

    def test_refresh_keeps_the_last_known_good_cache_when_the_token_is_refused(self):
        self.world.secrets = {"API_TOKEN": "tok", "TIMEOUT_S": "9"}
        s = self.make()
        with self._with_token(SERVICE_TOKEN):
            s.init()
            self.world.fail_fetch.append(401)
            with self.assertLogs("infisical_settings", level="ERROR") as logs:
                self.assertFalse(s.refresh())
        self.assertEqual(s.get("TIMEOUT_S"), "9")
        self.assertTrue(any("INFISICAL_TOKEN" in line for line in logs.output), logs.output)
        self.assertEqual(self._logins(), [])

    def test_a_service_token_never_lands_in_the_machine_identity_cache(self):
        # The session-token cache belongs to the machine identity alone.  A
        # service token in there can be paired with the machine source, and
        # then a reader reuses a credential the source does not own and goes
        # down a re-login path that cannot refresh it.
        self.world.secrets = {"API_TOKEN": "tok"}
        s = self.make()
        with self._with_token(SERVICE_TOKEN):
            s.init()
        self.assertIsNone(s._access_token)
        self.assertIsNone(s._credential_source)

    def test_the_machine_identity_caches_its_own_session_token(self):
        self.world.secrets = {"API_TOKEN": "tok"}
        s = self.make()
        s.init()
        self.assertEqual(s._access_token, "tok-1")
        self.assertEqual(s._credential_source, mod.SOURCE_MACHINE_IDENTITY)

    def test_a_reader_never_sees_a_token_paired_with_the_wrong_source(self):
        # The token and the source are one fact.  A reader that sees the
        # source must see the token that goes with it, even while a fetch
        # is running.
        self.world.secrets = {"API_TOKEN": "tok"}
        s = self.make()
        s.init()
        seen = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                seen.append(s._credential())

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        try:
            for _ in range(50):
                with self._with_token(SERVICE_TOKEN):
                    s.refresh()
                s.refresh()
        finally:
            stop.set()
            t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertTrue(seen)
        for token, source in seen:
            if source == mod.SOURCE_MACHINE_IDENTITY:
                self.assertEqual(token, "tok-1")
            else:
                self.assertEqual(source, mod.SOURCE_SERVICE_TOKEN)
                self.assertEqual(token, SERVICE_TOKEN)

    def test_a_401_on_the_post_path_raises_the_service_token_message(self):
        # PATCH answers 404 for a key that is not there yet, so the write
        # goes on to POST.  A read-scoped token is refused there, and the
        # message has to name the credential and the fix.
        self.world.secrets = {"API_TOKEN": "tok"}
        s = self.make(required_keys=())
        with self._with_token(SERVICE_TOKEN):
            s.init()
            self.world.post_statuses.append(401)
            with self.assertRaises(mod.SettingsError) as ctx:
                s.set("TIMEOUT_S", "42")
        message = str(ctx.exception)
        self.assertIn("INFISICAL_TOKEN", message)
        self.assertIn("check that the token is current", message)
        self.assertEqual(self._logins(), [])

    def test_the_machine_identity_is_still_the_second_leg(self):
        self.world.secrets = {"API_TOKEN": "tok"}
        s = self.make()
        s.init()
        # No service token in the environment:  the pair is logged in with, as before.
        self.assertEqual(len(self._logins()), 1)
        fetch = [c for c in self.world.calls if c[0] == "GET"][0]
        self.assertEqual(fetch[3], "Bear" + "er tok-1")


class ConcurrentLoginTests(_SettingsCase):
    """Concurrent callers share one universal-auth login.

    A cold start with N callers used to fire N logins:  the cache check and
    the login were not atomic, so every thread that missed the cache logged
    in on its own.  That is latency and a rate-limit risk for no benefit,
    because every one of them ends up holding the same token.
    """

    def _run_concurrently(self, fn, count=8):
        """Run fn in count threads released together; return (results, errors)."""
        results = []
        errors = []
        barrier = threading.Barrier(count)

        def worker():
            try:
                barrier.wait(timeout=10)
                results.append(fn())
            except Exception as exc:  # noqa: BLE001 - the test wants the failure
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(count)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertFalse([t for t in threads if t.is_alive()], "a worker thread hung")
        return results, errors

    def test_concurrent_cold_start_callers_share_one_login(self):
        self.world.login_delay = 0.05
        self.world.login_tokens = ["tok-a"]
        s = self.make()
        results, errors = self._run_concurrently(lambda: s._credential())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(self.world.login_calls, 1)
        for token, source in results:
            self.assertEqual(token, "tok-a")
            self.assertEqual(source, mod.SOURCE_MACHINE_IDENTITY)

    def test_concurrent_callers_that_share_a_stale_token_share_one_refresh(self):
        # Every caller holds the same token and every one of them has been told
        # it is stale.  They must still cost one login between them.
        self.world.login_delay = 0.05
        self.world.login_tokens = ["tok-a", "tok-b"]
        s = self.make()
        s._credential()
        self.assertEqual(self.world.login_calls, 1)
        results, errors = self._run_concurrently(
            lambda: s._machine_session_token(stale_token="tok-a")
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(self.world.login_calls, 2)
        for token in results:
            self.assertEqual(token, "tok-b")

    def test_a_stale_token_is_never_served_to_a_caller_that_asked_for_a_new_one(self):
        self.world.login_tokens = ["tok-a", "tok-b"]
        s = self.make()
        s._credential()
        self.assertEqual(s._access_token, "tok-a")
        fresh = s._machine_session_token(stale_token="tok-a")
        self.assertEqual(fresh, "tok-b")
        self.assertEqual(s._access_token, "tok-b")

    def test_a_slow_login_does_not_hold_the_cache_lock(self):
        # The login runs under its own lock, so a reader that only wants the
        # settings cache is never stuck behind a slow network call.
        self.world.login_delay = 0.4
        self.world.login_tokens = ["tok-a", "tok-b"]
        self.world.secrets = {"API_TOKEN": "tok"}
        s = self.make()
        s.init()
        started = threading.Event()

        def slow_login():
            started.set()
            s._machine_session_token(stale_token="tok-a")

        t = threading.Thread(target=slow_login)
        t.start()
        try:
            self.assertTrue(started.wait(timeout=5))
            self.assertEqual(s.get("API_TOKEN"), "tok")
        finally:
            t.join(timeout=30)
        self.assertFalse(t.is_alive())


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


class ProdOnlyTests(_SettingsCase):
    """Infisical's only environment is prod:  the default is prod and every other slug is
    refused before any network call, while the last-known-good cache is kept."""

    def test_default_environment_is_prod(self):
        s = mod.InfisicalSettings(project_id=PROJECT, managed_keys=MANAGED, required_keys=REQUIRED)
        self.assertEqual(s.environment, "prod")
        self.assertEqual(mod.PROD_ENVIRONMENT, "prod")
        self.world.secrets = {"API_TOKEN": "tok"}
        s.init()
        fetch = [c for c in self.world.calls if c[0] == "GET"][0]
        self.assertIn("environment=prod", fetch[1])
        self.assertNotIn("environment=dev", fetch[1])

    def test_blank_and_mixed_case_prod_are_prod(self):
        for given in ("", "  ", "prod", "PROD", " Prod "):
            self.assertEqual(self.make(environment=given).environment, "prod", given)

    def test_other_slugs_are_refused_before_any_network_call(self):
        self.world.secrets = {"API_TOKEN": "tok"}
        for slug in ("dev", "staging", "development", "production", "preview"):
            s = self.make(environment=slug)
            with self.assertRaises(mod.SettingsError) as ctx:
                s.init()
            self.assertIn(repr(slug), str(ctx.exception))
            self.assertIn("prod is the only environment", str(ctx.exception))
        self.assertEqual(self.world.network_calls, 0)

    def test_refused_even_with_no_identity(self):
        with mock.patch.dict(os.environ, {"INFISICAL_AUTOMATION_CLIENT_ID": "",
                                          "INFISICAL_AUTOMATION_CLIENT_SECRET": ""}):
            with mock.patch.object(mod.InfisicalSettings, "_identity_from_file",
                                   return_value=(None, None)):
                with self.assertRaises(mod.SettingsError):
                    self.make(environment="dev").init()
        self.assertEqual(self.world.network_calls, 0)

    def test_set_refuses_non_prod_and_writes_nothing(self):
        s = self.make(environment="staging")
        with self.assertRaises(mod.SettingsError):
            s.set("TIMEOUT_S", "7")
        self.assertIsNone(s.get("TIMEOUT_S"))
        self.assertEqual(self.world.network_calls, 0)

    def test_refresh_refuses_non_prod_and_keeps_last_known_good(self):
        self.world.secrets = {"API_TOKEN": "tok", "BACKEND_URL": "http://x"}
        s = self.make()
        s.init()
        calls_after_init = self.world.network_calls
        s.environment = "dev"                 # drifted after startup
        self.assertFalse(s.refresh())
        self.assertEqual(s.get("API_TOKEN"), "tok")
        self.assertEqual(s.get("BACKEND_URL"), "http://x")
        self.assertEqual(self.world.network_calls, calls_after_init)

    def test_configure_refuses_non_prod_and_keeps_previous_settings(self):
        self.world.secrets = {"API_TOKEN": "tok"}
        first = mod.configure(project_id=PROJECT, environment="prod", managed_keys=MANAGED,
                              required_keys=REQUIRED, secret_keys=SECRETS)
        mod.init_settings()
        with self.assertRaises(mod.SettingsError):
            mod.configure(project_id=PROJECT, environment="dev", managed_keys=MANAGED,
                          required_keys=REQUIRED, secret_keys=SECRETS)
        self.assertIs(mod._default, first)
        self.assertEqual(mod.get("API_TOKEN"), "tok")


if __name__ == "__main__":
    unittest.main()
