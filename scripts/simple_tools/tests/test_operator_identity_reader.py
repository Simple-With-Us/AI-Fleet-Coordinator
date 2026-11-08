"""operator_identity_reader: the server-native scoped-service-token reader, with a fake API."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from simple_tools import operator_identity_reader as OIR

CONFIG = {
    "schema": 1,
    "service_token": "st.aaaa.bbbb.cccc",
    "api_url": "https://app.infisical.com",
    # Runtime-derived fixture id:  deterministic, no committed inventory-shaped literal.
    "project_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "simple-tools/tests/operator-identity-reader/project")),
    "token_id": "11111111-2222-4333-8444-555555555555",
    "token_name": "operator-identity-read",
}


def _pin(config):
    pins = {k: config[k] for k in ("project_id", "token_id", "token_name", "api_url")}
    return hashlib.sha256(json.dumps(pins, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _meta(config, days=30):
    return {
        "_id": config["token_id"],
        "id": config["token_id"],
        "name": config["token_name"],
        "workspace": config["project_id"],
        "scopes": [{"environment": "prod", "secretPath": "/zulip_operator"}],
        "permissions": ["read"],
        "expiresAt": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)).isoformat(),
    }


def _secret(config, name, value):
    return {"secret": {
        "workspace": config["project_id"], "environment": "prod",
        "secretPath": "/zulip_operator", "secretKey": name,
        "type": "shared", "secretValueHidden": False, "secretValue": value,
    }}


class ReaderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        (self.home / ".config" / "operator-identity").mkdir(parents=True)
        os.chmod(self.home, 0o700)
        os.chmod(self.home / ".config", 0o700)
        os.chmod(self.home / ".config" / "operator-identity", 0o700)
        self.path = self.home / ".config" / "operator-identity" / "reader.json"
        self._write(CONFIG)
        self.old_pin = OIR.DEPLOYMENT_SHA256
        OIR.DEPLOYMENT_SHA256 = _pin(CONFIG)
        self.addCleanup(setattr, OIR, "DEPLOYMENT_SHA256", self.old_pin)

    def _write(self, config, mode=0o600):
        self.path.write_text(json.dumps(config))
        self.path.chmod(mode)

    def _reader(self, meta=None, secrets=None):
        config = dict(CONFIG)
        meta = _meta(config) if meta is None else meta
        secrets = secrets or {n: _secret(config, n, "v-" + n) for n in OIR.NAMES}

        def fake_request(path):
            if path == "/v2/service-token":
                return meta
            for name in OIR.NAMES:
                if path.startswith("/v3/secrets/raw/" + name):
                    return secrets[name]
            raise AssertionError("unexpected path " + path)

        reader = OIR.load(self.home)
        reader._request = fake_request
        return reader

    def test_absent_config_means_no_reader(self):
        self.path.unlink()
        self.assertIsNone(OIR.load(self.home))

    def test_symlinked_config_directory_with_no_reader_means_absent(self):
        directory = self.home / ".config"
        moved = self.home / "dotfiles"
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        self.path.unlink()
        self.assertIsNone(OIR.load(self.home))

    def test_symlinked_config_directory_with_reader_still_refuses(self):
        directory = self.home / ".config"
        moved = self.home / "dotfiles"
        directory.rename(moved)
        directory.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(OIR.ReaderError):
            OIR.load(self.home)

    def test_dangling_reader_symlink_is_not_absence(self):
        self.path.unlink()
        self.path.symlink_to(self.home / "missing-reader")
        with self.assertRaises(OIR.ReaderError):
            OIR.load(self.home)

    def test_inaccessible_optional_config_is_not_absence(self):
        with mock.patch.object(OIR.os, "lstat", side_effect=PermissionError):
            with self.assertRaises(OIR.ReaderError):
                OIR.load(self.home)

    def test_reader_for_does_not_cache_absence(self):
        self.path.unlink()
        self.assertIsNone(OIR.reader_for(self.home))
        self._write(CONFIG)  # installed later:  the same process must pick it up
        reader = OIR.reader_for(self.home)
        self.assertIsNotNone(reader)
        OIR.evict_reader(self.home)

    def test_an_unpinned_deployment_refuses_every_config(self):
        OIR.DEPLOYMENT_SHA256 = ""
        with self.assertRaises(OIR.ReaderError) as ctx:
            OIR.load(self.home)
        self.assertEqual(str(ctx.exception), "reader-deployment-unqualified")

    def test_a_world_readable_config_refuses(self):
        self._write(CONFIG, mode=0o644)
        with self.assertRaises(OIR.ReaderError) as ctx:
            OIR.load(self.home)
        self.assertEqual(str(ctx.exception), "reader-custody-unsafe")

    def test_a_malformed_token_refuses(self):
        bad = dict(CONFIG, service_token="bearer xyz")
        self._write(bad)
        OIR.DEPLOYMENT_SHA256 = _pin(bad)
        with self.assertRaises(OIR.ReaderError) as ctx:
            OIR.load(self.home)
        self.assertEqual(str(ctx.exception), "reader-config-invalid")

    def test_happy_path_returns_the_three_names(self):
        reader = self._reader()
        values = reader.credentials()
        self.assertEqual(sorted(values), sorted(OIR.NAMES))
        self.assertEqual(values["ZULIP_OPERATOR_EMAIL"], "v-ZULIP_OPERATOR_EMAIL")

    def test_wrong_scope_in_metadata_refuses(self):
        meta = _meta(CONFIG)
        meta["scopes"] = [{"environment": "prod", "secretPath": "/zulip"}]
        reader = self._reader(meta=meta)
        with self.assertRaises(OIR.ReaderError) as ctx:
            reader.credentials()
        self.assertEqual(str(ctx.exception), "reader-metadata-invalid")

    def test_a_secret_row_from_the_wrong_path_refuses(self):
        config = dict(CONFIG)
        secrets = {n: _secret(config, n, "v") for n in OIR.NAMES}
        secrets["ZULIP_OPERATOR_API_KEY"]["secret"]["secretPath"] = "/zulip"
        reader = self._reader(secrets=secrets)
        with self.assertRaises(OIR.ReaderError) as ctx:
            reader.credentials()
        self.assertEqual(str(ctx.exception), "reader-key-unverified")


if __name__ == "__main__":
    unittest.main()
