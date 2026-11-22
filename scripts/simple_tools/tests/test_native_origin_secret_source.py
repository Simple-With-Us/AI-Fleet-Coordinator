import importlib.util
import io
import json
import secrets
import tempfile
import unittest
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

spec = importlib.util.spec_from_file_location("origin_source_loader", Path(__file__).resolve().parents[1] / "server" / "infisical_env.py")
loader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loader)


class TestNativeOriginSecretSource(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        partition = Path(self.temp.name) / "partition.toml"
        partition.write_text('[seats]\nCODEX = "server"\n')
        self.primary, self.secondary = str(uuid.uuid4()), str(uuid.uuid4())
        self.primary_token, self.secondary_token, self.router_token = ["st." + secrets.token_urlsafe(24) for _ in range(3)]
        self.origin_key = secrets.token_hex(32)
        self.routine_key = secrets.token_hex(32)
        self.registry_key = secrets.token_hex(32)
        self.env = {
            "INFISICAL_TOKEN": self.primary_token,
            "INFISICAL_PROJECT_ID": self.primary,
            "INFISICAL_LISTENER_READ_TOKEN": self.secondary_token,
            "INFISICAL_LISTENER_READ_PROJECT_ID": self.secondary,
            "EAVESDROPPER_WAKE_ROUTER_INFISICAL_TOKEN": self.router_token,
            "SIMPLE_TOOLS_PARTITION": str(partition),
            loader.REVIEW_ORIGIN_READ_KEY: secrets.token_hex(32),
        }
        self.grants = {
            self.primary_token: (self.primary, ["/zulip", "/operator"]),
            self.secondary_token: (self.secondary, ["/zulip"]),
            self.router_token: (self.primary, ["/wake-router"]),
        }
        self.rows = {
            (self.primary, "/zulip"): {"CODEX_ROUTINE_URL": "https://listener.example.test/wake"},
            (self.primary, "/operator"): {"OPERATOR_ZULIP_USER_ID": "123", "OPERATOR_ZULIP_EMAIL": "operator@example.test"},
            (self.primary, "/wake-router"): {"SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY": self.registry_key, "WAKE_ROUTER_KEY": secrets.token_hex(32)},
            (self.secondary, "/zulip"): {"CODEX_ROUTINE_KEY": self.routine_key, loader.REVIEW_ORIGIN_READ_KEY: self.origin_key},
        }
        self.calls = []
        self.row_overrides = {}
        self.metadata_overrides = {}

    def opener(self, request, timeout):
        self.assertEqual(request.get_method(), "GET")
        reader_identity = request.get_header("Authorization").removeprefix("Bearer ")
        self.assertTrue(reader_identity in self.grants, "only existing fixture readers may be used")
        project, paths = self.grants[reader_identity]
        url = urlsplit(request.full_url)
        self.assertEqual(url.scheme, "https")
        self.assertEqual(url.netloc, "app.infisical.com")
        if url.path == "/api/v2/service-token":
            self.calls.append((project, "metadata"))
            payload = {"id": "fixture-reader", "name": "fixture-reader", "workspace": project,
                       "permissions": ["read"], "scopes": [{"environment": "prod", "secretPath": path} for path in paths],
                       "expiresAt": None}
            payload.update(self.metadata_overrides.get(reader_identity, {}))
        else:
            self.assertEqual(url.path, "/api/v3/secrets/raw")
            query = parse_qs(url.query)
            self.assertEqual(query["workspaceId"], [project])
            self.assertEqual(query["environment"], ["prod"])
            self.assertEqual(query["expandSecretReferences"], ["false"])
            path = query["secretPath"][0]
            self.assertTrue(path in paths, "folder must already be granted")
            self.calls.append((project, path))
            values = self.rows[(project, path)]
            rows = []
            for name, value in values.items():
                row = {"secretKey": name, "secretValue": value, "type": "shared", "workspace": project,
                       "environment": "prod", "secretPath": path}
                row.update(self.row_overrides.get((project, path, name), {}))
                rows.append(row)
            payload = {"secrets": rows, "totalCount": len(rows)}
        return io.BytesIO(json.dumps(payload).encode())

    def load(self):
        return loader.build_env(self.env, opener=self.opener, sleep=lambda _: None)

    def assert_held(self):
        output, line = self.load()
        self.assertNotIn(loader.REVIEW_ORIGIN_READ_KEY, output)
        self.assertEqual(output["CODEX_ROUTINE_KEY"], self.routine_key)
        self.assertEqual(output["OPERATOR_REGISTRY_KEY"], self.registry_key)
        self.assertIn("optional capability held", line)
        self.assertNotIn(self.origin_key, line)

    def test_secondary_canonical_source_uses_existing_read_once_and_strips_container_copy(self):
        output, line = self.load()
        self.assertEqual(output[loader.REVIEW_ORIGIN_READ_KEY], self.origin_key)
        self.assertEqual(self.calls.count((self.secondary, "/zulip")), 1)
        self.assertEqual(set(path for _, path in self.calls if path != "metadata"), {"/zulip", "/operator", "/wake-router"})
        for token_name in loader.AUTH_ENV_VARS:
            self.assertNotIn(token_name, output)
        self.assertIn("loaded from secondary /zulip", line)
        self.assertNotIn(self.origin_key, line)

    def test_missing_secondary_key_holds_only_optional_capability(self):
        self.rows[(self.secondary, "/zulip")].pop(loader.REVIEW_ORIGIN_READ_KEY)
        self.assert_held()

    def test_primary_operator_and_primary_zulip_are_not_fallback_sources(self):
        for path in ("/operator", "/zulip"):
            with self.subTest(path=path):
                self.rows[(self.secondary, "/zulip")].pop(loader.REVIEW_ORIGIN_READ_KEY, None)
                self.rows[(self.primary, path)][loader.REVIEW_ORIGIN_READ_KEY] = self.origin_key
                self.assert_held()
                self.rows[(self.primary, path)].pop(loader.REVIEW_ORIGIN_READ_KEY)

    def test_duplicate_primary_record_holds_even_when_different_or_unusable(self):
        for value in (self.origin_key, secrets.token_hex(32), "${unresolved}", None):
            with self.subTest(value_type=type(value).__name__):
                self.rows[(self.primary, "/operator")][loader.REVIEW_ORIGIN_READ_KEY] = value
                self.assert_held()

    def test_hidden_unresolved_malformed_and_reused_canonical_keys_hold(self):
        for value in (None, "short", "${unresolved}", " " * 64, self.registry_key, self.routine_key):
            with self.subTest(value_type=type(value).__name__, length=len(value) if isinstance(value, str) else 0):
                self.rows[(self.secondary, "/zulip")][loader.REVIEW_ORIGIN_READ_KEY] = value
                self.assert_held()
        self.rows[(self.secondary, "/zulip")][loader.REVIEW_ORIGIN_READ_KEY] = self.origin_key
        self.row_overrides[(self.secondary, "/zulip", loader.REVIEW_ORIGIN_READ_KEY)] = {"secretValueHidden": True}
        self.assert_held()

    def test_secondary_scope_mismatch_still_stops_startup_before_values(self):
        self.metadata_overrides[self.secondary_token] = {"scopes": [{"environment": "prod", "secretPath": "/"}]}
        with self.assertRaises(loader.LoadError):
            self.load()
        self.assertNotIn((self.secondary, "/zulip"), self.calls)

    def test_missing_optional_source_never_uses_primary_or_container_copy(self):
        self.env.pop("INFISICAL_LISTENER_READ_PROJECT_ID")
        output, line = self.load()
        self.assertNotIn(loader.REVIEW_ORIGIN_READ_KEY, output)
        self.assertEqual(output["OPERATOR_REGISTRY_KEY"], self.registry_key)
        self.assertIn("optional capability held", line)


if __name__ == "__main__":
    unittest.main()
