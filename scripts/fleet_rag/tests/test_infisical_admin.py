"""Admin settings surface of fleet-recall-service (server.py).

    cd scripts && python3 -m unittest fleet_rag.tests.test_infisical_admin -v

Covers the canonical contract's admin gating + write-through at the HTTP layer:
GET /admin/settings, POST /admin/settings, POST /admin/reload-settings.  The
Infisical settings module itself is mocked -- its own contract is proven in
test_infisical_settings.py.  No network, no credentials.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import pathlib
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fleet_rag import infisical_settings as real_settings

SERVICE_DIR = pathlib.Path(__file__).resolve().parents[2] / "fleet-recall-service"
SERVER_PY = SERVICE_DIR / "server.py"
SEAT_TOKEN = "seat-token"
ADMIN_TOKEN = "admin-token"


def _load_server():
    loader = importlib.machinery.SourceFileLoader("fleet_recall_admin_server", str(SERVER_PY))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


server = _load_server()


class _FakeSettings:
    """Stands in for server.infisical_settings: {key: value} cache, write-through log."""

    def __init__(self, values, fail_write=False):
        self.values = dict(values)
        self.fail_write = fail_write
        self.writes: list[tuple[str, str]] = []

    def get(self, key, default=None):
        return self.values.get(key, default)

    def present(self, key):
        return bool(self.values.get(key))

    def set(self, key, value):
        if self.fail_write:
            raise real_settings.SettingsError("write down")
        self.writes.append((key, value))
        self.values[key] = value

    def refresh_now(self):
        return True

    def admin_listing(self):
        out = {}
        for key in server.MANAGED_KEYS:
            val = self.values.get(key, "")
            if key in server.SECRET_KEYS:
                out[key] = {"set": bool(val), "value": "set" if val else "empty"}
            else:
                out[key] = {"set": bool(val), "value": val}
        return out


class _AdminCase(unittest.TestCase):
    def _start(self, values, fail_write=False):
        self.fake = _FakeSettings(values, fail_write)
        self._patch_settings = mock.patch.object(server, "infisical_settings", self.fake)
        self._patch_log = mock.patch.object(server, "log", lambda msg: None)
        self._patch_settings.start()
        self._patch_log.start()
        self.httpd = server.make_server("127.0.0.1", 0, SEAT_TOKEN, timeout=5)
        host, port = self.httpd.server_address[:2]
        self.base = f"http://{host}:{port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self._patch_settings.stop()
        self._patch_log.stop()

    def request(self, method, path, body=None, token=None):
        data = None if body is None else json.dumps(body).encode()
        hdrs = {"Content-Type": "application/json"}
        if token is not None:
            hdrs["Authorization"] = "Bearer " + token
        req = Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            with urlopen(req, timeout=10) as resp:
                payload = resp.read()
                return resp.status, (json.loads(payload) if payload else None)
        except HTTPError as e:
            with e:
                payload = e.read()
            return e.code, (json.loads(payload) if payload else None)


class AdminGatingTests(_AdminCase):
    def setUp(self):
        self._start({"RECALL_API_TOKEN": SEAT_TOKEN, "RECALL_ADMIN_TOKEN": ADMIN_TOKEN,
                     "RECALL_SOCKET_TIMEOUT": "15"})

    def test_admin_listing_requires_admin_token(self):
        status, body = self.request("GET", "/admin/settings", token=None)
        self.assertEqual(status, 401)
        status, body = self.request("GET", "/admin/settings", token="wrong")
        self.assertEqual(status, 401)
        # The seat bearer is NOT the admin bearer.
        status, body = self.request("GET", "/admin/settings", token=SEAT_TOKEN)
        self.assertEqual(status, 401)

    def test_admin_listing_returns_inventory_with_secrets_masked(self):
        status, body = self.request("GET", "/admin/settings", token=ADMIN_TOKEN)
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        listing = body["settings"]
        self.assertEqual(listing["RECALL_ADMIN_TOKEN"], {"set": True, "value": "set"})
        self.assertEqual(listing["RECALL_SOCKET_TIMEOUT"], {"set": True, "value": "15"})
        self.assertNotIn("admin-token", json.dumps(listing))

    def test_admin_disabled_when_token_not_configured(self):
        self.fake.values.pop("RECALL_ADMIN_TOKEN")
        status, body = self.request("GET", "/admin/settings", token=ADMIN_TOKEN)
        self.assertEqual(status, 403)
        self.assertIn("not configured", body["error"])

    def test_admin_write_through(self):
        status, body = self.request("POST", "/admin/settings",
                                    {"key": "RECALL_SOCKET_TIMEOUT", "value": "30"},
                                    token=ADMIN_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "key": "RECALL_SOCKET_TIMEOUT"})
        self.assertEqual(self.fake.writes, [("RECALL_SOCKET_TIMEOUT", "30")])
        self.assertEqual(self.fake.values["RECALL_SOCKET_TIMEOUT"], "30")

    def test_admin_write_rejects_unknown_key(self):
        status, body = self.request("POST", "/admin/settings",
                                    {"key": "NOT_A_SETTING", "value": "x"},
                                    token=ADMIN_TOKEN)
        self.assertEqual(status, 400)
        self.assertEqual(self.fake.writes, [])

    def test_admin_write_rejects_non_string_value(self):
        status, body = self.request("POST", "/admin/settings",
                                    {"key": "RECALL_SOCKET_TIMEOUT", "value": 42},
                                    token=ADMIN_TOKEN)
        self.assertEqual(status, 400)

    def test_admin_write_failure_rejects_save(self):
        self.fake.fail_write = True
        status, body = self.request("POST", "/admin/settings",
                                    {"key": "RECALL_SOCKET_TIMEOUT", "value": "30"},
                                    token=ADMIN_TOKEN)
        self.assertEqual(status, 502)
        self.assertFalse(body["ok"])
        # Cache was NOT updated on a failed write-through.
        self.assertEqual(self.fake.values["RECALL_SOCKET_TIMEOUT"], "15")

    def test_admin_write_requires_admin_auth(self):
        status, _ = self.request("POST", "/admin/settings",
                                 {"key": "RECALL_SOCKET_TIMEOUT", "value": "30"},
                                 token=SEAT_TOKEN)
        self.assertEqual(status, 401)
        self.assertEqual(self.fake.writes, [])

    def test_admin_reload(self):
        status, body = self.request("POST", "/admin/reload-settings", token=ADMIN_TOKEN)
        self.assertEqual(status, 200)
        self.assertEqual(body, {"ok": True, "refreshed": True})
        status, _ = self.request("POST", "/admin/reload-settings", token=SEAT_TOKEN)
        self.assertEqual(status, 401)


class MainEnvironmentTests(unittest.TestCase):
    """server.main() reads INFISICAL_ENVIRONMENT:  unset or empty means prod, any other slug
    is refused with exit 2 and a log line, before the server binds."""

    def _run(self, env):
        seen = {}
        logs = []
        real = real_settings

        def fake_configure(**kwargs):
            seen.update(kwargs)
            return real.configure(**kwargs)

        def fake_init():
            raise real.SettingsError("stop here")

        fake = mock.Mock(wraps=real)
        fake.PROD_ENVIRONMENT = real.PROD_ENVIRONMENT
        fake.configure = fake_configure
        fake.init_settings = fake_init
        with mock.patch.dict("os.environ", env, clear=False), \
                mock.patch.object(server, "infisical_settings", fake), \
                mock.patch.object(server, "log", logs.append):
            if "INFISICAL_ENVIRONMENT" not in env:
                import os
                os.environ.pop("INFISICAL_ENVIRONMENT", None)
            code = server.main([])
        real.reset_for_tests()
        return code, seen, logs

    def test_default_is_prod(self):
        code, seen, logs = self._run({})
        self.assertEqual(code, 2)                       # the fake init stops startup
        self.assertEqual(seen["environment"], "prod")
        self.assertIn("settings init failed: stop here", logs[0])

    def test_empty_value_is_prod(self):
        code, seen, _logs = self._run({"INFISICAL_ENVIRONMENT": " "})
        self.assertEqual(seen["environment"], "prod")

    def test_dev_and_staging_are_refused(self):
        for slug in ("dev", "staging"):
            code, _seen, logs = self._run({"INFISICAL_ENVIRONMENT": slug})
            self.assertEqual(code, 2)
            self.assertTrue(any("refusing Infisical environment" in line and repr(slug) in line
                                for line in logs), logs)

    def test_source_has_no_dev_default(self):
        text = SERVER_PY.read_text()
        self.assertNotIn('"INFISICAL_ENVIRONMENT", "dev"', text)


if __name__ == "__main__":
    unittest.main()
