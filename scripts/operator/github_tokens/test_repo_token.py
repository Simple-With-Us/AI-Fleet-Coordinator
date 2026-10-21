import copy
import json
import tempfile
import unittest
from pathlib import Path

from repo_token import RoutingError, TokenRouter, secret_names


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.mints = []
        self.wrong_app = False
        self.suspended = False
        self.wrong_scope = False
        self.router = TokenRouter(
            lambda slug: (100 if slug == "swu-echo" else 200, "NOT-A-KEY"),
            Path(self.temp.name) / "cache",
            api=self.api,
            signer=lambda *a: "MOCK",
            clock=lambda: 1800000000,
        )

    def api(self, method, path, bearer, body=None):
        app_id = getattr(self, "app_id", 100)
        if path == "/app":
            return {
                "id": app_id,
                "slug": "swu-wise"
                if self.wrong_app
                else ("swu-echo" if app_id == 100 else "swu-wise"),
            }
        if method == "GET":
            owner = path.split("/")[2]
            return {
                "id": 111 if owner == "Simple-With-Us" else 222,
                "app_id": app_id,
                "account": {"login": owner},
                "suspended_at": "yes" if self.suspended else None,
            }
        self.mints.append((path, copy.deepcopy(body)))
        owner = "Simple-With-Us" if "/111/" in path else "example-user"
        return {
            "token": "MOCK-NOT-A-REAL-TOKEN",
            "expires_at": "2027-01-15T09:00:00Z",
            "permissions": body["permissions"],
            "repositories": [
                {
                    "full_name": owner
                    + "/"
                    + ("WRONG" if self.wrong_scope else body["repositories"][0])
                }
            ],
        }

    def test_names(self):
        self.assertEqual(
            secret_names("swu-bf-builder"),
            ("GH_APP_SWU_BF_BUILDER_ID", "GH_APP_SWU_BF_BUILDER_PRIVATE_KEY"),
        )

    def test_bad_target(self):
        with self.assertRaises(RoutingError):
            self.router.token_for("swu-echo", "example-repo", {"contents": "read"})

    def test_bad_slug(self):
        with self.assertRaises(RoutingError):
            self.router.token_for("../echo", "a/b", {"contents": "read"})

    def test_wrong_app_fails_closed(self):
        self.wrong_app = True
        with self.assertRaises(RoutingError):
            self.router.token_for("swu-echo", "example-user/example-repo", {"contents": "read"})
        self.assertFalse(self.mints)

    def test_suspended_fails_closed(self):
        self.suspended = True
        with self.assertRaises(RoutingError):
            self.router.token_for("swu-echo", "example-user/example-repo", {"contents": "read"})

    def test_wrong_scope_not_cached(self):
        self.wrong_scope = True
        with self.assertRaises(RoutingError):
            self.router.token_for("swu-echo", "example-user/example-repo", {"contents": "read"})
        self.assertEqual(list(self.router.cache.glob("*.json")), [])

    def test_org_personal_split(self):
        self.router.token_for("swu-echo", "Simple-With-Us/Test", {"contents": "read"})
        self.router.token_for("swu-echo", "example-user/example-repo", {"contents": "read"})
        self.assertEqual(
            [x[0] for x in self.mints],
            ["/app/installations/111/access_tokens", "/app/installations/222/access_tokens"],
        )

    def test_cache_reuse(self):
        for _ in range(2):
            self.router.token_for("swu-echo", "example-user/example-repo", {"contents": "read"})
        self.assertEqual(len(self.mints), 1)
        f = next(self.router.cache.glob("*.json"))
        self.assertEqual(f.stat().st_mode & 0o777, 0o600)

    def test_permission_cache_split(self):
        for p in ["read", "write"]:
            self.router.token_for("swu-echo", "example-user/example-repo", {"contents": p})
        self.assertEqual(len(self.mints), 2)

    def test_app_cache_split(self):
        self.router.token_for("swu-echo", "example-user/example-repo", {"contents": "read"})
        self.app_id = 200
        self.router.token_for("swu-wise", "example-user/example-repo", {"contents": "read"})
        self.assertEqual(len(list(self.router.cache.glob("*.json"))), 2)

    def test_expired_refresh(self):
        self.router.token_for("swu-echo", "example-user/example-repo", {"contents": "read"})
        f = next(self.router.cache.glob("*.json"))
        d = json.loads(f.read_text())
        d["expires_at"] = "2020-01-01T00:00:00Z"
        f.write_text(json.dumps(d))
        self.router.token_for("swu-echo", "example-user/example-repo", {"contents": "read"})
        self.assertEqual(len(self.mints), 2)

    def test_no_permissions(self):
        with self.assertRaises(RoutingError):
            self.router.token_for("swu-echo", "a/b", {})


if __name__ == "__main__":
    unittest.main()


class ReviewFixTests(unittest.TestCase):
    """Regression tests for the second Kody review round."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.router = TokenRouter(
            lambda slug: (100, "NOT-A-KEY"),
            Path(self.temp.name) / "cache",
            api=lambda *a, **k: None,
            signer=lambda *a: "MOCK",
        )

    def test_openai_shaped_target_refused_with_stable_code(self):
        for target in ("Simple-With-Us/sk-abc123", "Simple-With-Us/disk-utils"):
            with self.assertRaises(RoutingError) as ctx:
                self.router.token_for("swu-echo", target, {"contents": "read"})
            self.assertEqual(str(ctx.exception), "secret_refused:target")
            self.assertNotIn("abc123", str(ctx.exception))

    def test_malformed_app_identity_fails_closed(self):
        def api(method, path, bearer, body=None):
            if path == "/app":
                return {"id": "100", "slug": "swu-echo"}
            return {}

        router = TokenRouter(
            lambda slug: (100, "NOT-A-KEY"),
            Path(self.temp.name) / "c2",
            api=api,
            signer=lambda *a: "MOCK",
        )
        with self.assertRaises(RoutingError) as ctx:
            router.token_for("swu-echo", "a/b", {"contents": "read"})
        self.assertIn("malformed", str(ctx.exception))

    def test_malformed_installation_account_fails_closed(self):
        def api(method, path, bearer, body=None):
            if path == "/app":
                return {"id": 100, "slug": "swu-echo"}
            return {"id": 111, "app_id": 100, "account": {"login": 7}, "suspended_at": None}

        router = TokenRouter(
            lambda slug: (100, "NOT-A-KEY"),
            Path(self.temp.name) / "c3",
            api=api,
            signer=lambda *a: "MOCK",
        )
        with self.assertRaises(RoutingError) as ctx:
            router.token_for("swu-echo", "a/b", {"contents": "read"})
        self.assertIn("identity", str(ctx.exception))
