"""service_identity.operator_credentials -> the scoped-service-token reader:  the handoff."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from simple_tools import operator_identity_reader as OIR
from simple_tools import service_identity as SI
from simple_tools import zulip as Z
from simple_tools.tests.test_operator_identity_reader import (
    CONFIG, _meta, _pin, _secret,
)

ENV = {
    "OPERATOR_ZULIP_USER_ID": "1217148",
    "OPERATOR_ZULIP_EMAIL": "operator-bot@zulip.test",
    "INFISICAL_FLEET_OPS_PROJECT_ID": CONFIG["project_id"],
}


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        (self.home / ".config" / "operator-identity").mkdir(parents=True)
        os.chmod(self.home, 0o700)
        os.chmod(self.home / ".config", 0o700)
        os.chmod(self.home / ".config" / "operator-identity", 0o700)
        self.path = self.home / ".config" / "operator-identity" / "reader.json"
        self.old_pin = OIR.DEPLOYMENT_SHA256
        OIR.DEPLOYMENT_SHA256 = _pin(CONFIG)
        self.addCleanup(setattr, OIR, "DEPLOYMENT_SHA256", self.old_pin)
        self.addCleanup(OIR.evict_reader, self.home)
        self._write(CONFIG)

    def _write(self, config, mode=0o600):
        self.path.write_text(json.dumps(config))
        self.path.chmod(mode)

    def _install_fake_api(self, secrets_map=None, meta=None):
        secrets_map = secrets_map or {n: _secret(CONFIG, n, "v-" + n) for n in OIR.NAMES}
        secrets_map["ZULIP_OPERATOR_EMAIL"]["secret"]["secretValue"] = "operator-bot@zulip.test"
        meta = _meta(CONFIG) if meta is None else meta
        calls = []

        def fake_request(path):
            calls.append(path)
            if path == "/v2/service-token":
                return meta
            for name in OIR.NAMES:
                if path.startswith("/v3/secrets/raw/" + name):
                    return secrets_map[name]
            raise AssertionError("unexpected path " + path)

        # Route the fake through the memoized reader the factory will load.
        reader = OIR.reader_for(self.home)
        reader._request = fake_request
        return calls

    def test_the_factory_reads_through_the_service_token_reader(self):
        calls = self._install_fake_api()
        creds = SI.operator_credentials(dict(ENV), self.home)
        self.assertEqual(creds.email, "operator-bot@zulip.test")
        self.assertEqual(creds.key, "v-ZULIP_OPERATOR_API_KEY")
        self.assertIn("scoped service token", creds.source)
        self.assertTrue(any(p == "/v2/service-token" for p in calls))

    def test_a_second_call_reuses_the_reader_and_its_cache(self):
        calls = self._install_fake_api()
        SI.operator_credentials(dict(ENV), self.home)
        first = len(calls)
        SI.operator_credentials(dict(ENV), self.home)
        self.assertEqual(len(calls), first, "the memoized reader's 600 s cache serves the second call")

    def test_a_wrong_email_is_refused(self):
        self._install_fake_api()
        env = dict(ENV, OPERATOR_ZULIP_EMAIL="other-bot@zulip.test")
        with self.assertRaises(Z.CredentialError) as ctx:
            SI.operator_credentials(env, self.home)
        self.assertIn("not the pinned service bot", str(ctx.exception))

    def test_an_unsafe_config_never_falls_back(self):
        self.path.chmod(0o644)
        with self.assertRaises(Z.CredentialError) as ctx:
            SI.operator_credentials(dict(ENV), self.home)
        self.assertIn("reader-custody-unsafe", str(ctx.exception))

    def test_a_project_contradiction_is_refused(self):
        self._install_fake_api()
        env = dict(ENV, INFISICAL_FLEET_OPS_PROJECT_ID="9bf7417a-fbbb-42ca-870c-2b45207233f5")
        with self.assertRaises(Z.CredentialError) as ctx:
            SI.operator_credentials(env, self.home)
        self.assertIn("another project", str(ctx.exception))

    def test_no_reader_config_keeps_the_existing_reader_path(self):
        self.path.unlink()
        OIR.evict_reader(self.home)
        with self.assertRaises(Z.CredentialError) as ctx:
            SI.operator_credentials(dict(ENV), self.home)
        self.assertNotIn("service-token reader", str(ctx.exception),
                         "absent config falls through to the existing fleet-secrets path")


if __name__ == "__main__":
    unittest.main()
