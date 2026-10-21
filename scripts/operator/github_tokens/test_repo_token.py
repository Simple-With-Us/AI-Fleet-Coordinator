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
        owner = "Simple-With-Us" if "/111/" in path else "jaywedgeworth22"
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
            self.router.token_for("swu-echo", "Fleet-OPS", {"contents": "read"})

    def test_bad_slug(self):
        with self.assertRaises(RoutingError):
            self.router.token_for("../echo", "a/b", {"contents": "read"})

    def test_wrong_app_fails_closed(self):
        self.wrong_app = True
        with self.assertRaises(RoutingError):
            self.router.token_for("swu-echo", "jaywedgeworth22/Fleet-OPS", {"contents": "read"})
        self.assertFalse(self.mints)

    def test_suspended_fails_closed(self):
        self.suspended = True
        with self.assertRaises(RoutingError):
            self.router.token_for("swu-echo", "jaywedgeworth22/Fleet-OPS", {"contents": "read"})

    def test_wrong_scope_not_cached(self):
        self.wrong_scope = True
        with self.assertRaises(RoutingError):
            self.router.token_for("swu-echo", "jaywedgeworth22/Fleet-OPS", {"contents": "read"})
        self.assertEqual(list(self.router.cache.glob("*.json")), [])

    def test_org_personal_split(self):
        self.router.token_for("swu-echo", "Simple-With-Us/Test", {"contents": "read"})
        self.router.token_for("swu-echo", "jaywedgeworth22/Fleet-OPS", {"contents": "read"})
        self.assertEqual(
            [x[0] for x in self.mints],
            ["/app/installations/111/access_tokens", "/app/installations/222/access_tokens"],
        )

    def test_cache_reuse(self):
        for _ in range(2):
            self.router.token_for("swu-echo", "jaywedgeworth22/Fleet-OPS", {"contents": "read"})
        self.assertEqual(len(self.mints), 1)
        f = next(self.router.cache.glob("*.json"))
        self.assertEqual(f.stat().st_mode & 0o777, 0o600)

    def test_permission_cache_split(self):
        for p in ["read", "write"]:
            self.router.token_for("swu-echo", "jaywedgeworth22/Fleet-OPS", {"contents": p})
        self.assertEqual(len(self.mints), 2)

    def test_app_cache_split(self):
        self.router.token_for("swu-echo", "jaywedgeworth22/Fleet-OPS", {"contents": "read"})
        self.app_id = 200
        self.router.token_for("swu-wise", "jaywedgeworth22/Fleet-OPS", {"contents": "read"})
        self.assertEqual(len(list(self.router.cache.glob("*.json"))), 2)

    def test_expired_refresh(self):
        self.router.token_for("swu-echo", "jaywedgeworth22/Fleet-OPS", {"contents": "read"})
        f = next(self.router.cache.glob("*.json"))
        d = json.loads(f.read_text())
        d["expires_at"] = "2020-01-01T00:00:00Z"
        f.write_text(json.dumps(d))
        self.router.token_for("swu-echo", "jaywedgeworth22/Fleet-OPS", {"contents": "read"})
        self.assertEqual(len(self.mints), 2)

    def test_no_permissions(self):
        with self.assertRaises(RoutingError):
            self.router.token_for("swu-echo", "a/b", {})


if __name__ == "__main__":
    unittest.main()
