"""Tests for the OPS-966 routing prototype.

Owner decision 2026-10-22 (Q5):  git-relay is the ONE path that mints per-bot
GitHub App tokens, so `TokenRouter.token_for` now fails closed and names
git-relay.  The input-validation helpers the router used are still directly
testable, so those contracts keep their coverage here;  the mint/cache
behaviour they used to assert is unreachable by design and is covered by
`MintingOwnershipTests` instead.
"""

import tempfile
import unittest
from pathlib import Path

from repo_token import (
    MINTING_OWNER,
    RoutingError,
    TokenRouter,
    permission_set,
    secret_names,
    target_parts,
)


class ValidationTests(unittest.TestCase):
    """The reviewed input contract, still reachable without minting."""

    def test_names(self):
        self.assertEqual(
            secret_names("swu-bf-builder"),
            ("GH_APP_SWU_BF_BUILDER_ID", "GH_APP_SWU_BF_BUILDER_PRIVATE_KEY"),
        )

    def test_names_reject_bad_slug(self):
        for bad in ("../echo", "echo", "SWU-ECHO", ""):
            with self.assertRaises(RoutingError, msg=bad):
                secret_names(bad)

    def test_bad_target(self):
        for bad in ("example-repo", "", "../a/b", "a/b/c", "a/..", "a/b?x=1"):
            with self.assertRaises(RoutingError, msg=bad):
                target_parts(bad)

    def test_credential_shaped_target_is_refused_with_a_stable_code(self):
        target = "example-org/sk-" + "a" * 20
        with self.assertRaises(RoutingError) as ctx:
            target_parts(target)
        self.assertEqual(str(ctx.exception), "secret_refused:target")
        self.assertNotIn("a" * 20, str(ctx.exception))

    def test_legitimate_names_with_sk_substring_are_not_refused(self):
        # "sk-" as a bare substring must not refuse flask-app, task-manager
        # or disk-usage.
        for target in ("example-org/disk-usage", "example-org/flask-app"):
            self.assertEqual(target_parts(target), ("example-org", target.split("/")[1]))

    def test_permissions_must_be_explicit_read_or_write(self):
        for bad in ({}, {"contents": "admin"}, {"Contents": "read"}, {"contents": "write "}):
            with self.assertRaises(RoutingError, msg=str(bad)):
                permission_set(bad)

    def test_metadata_permission_is_forced_to_read(self):
        self.assertEqual(permission_set({"contents": "write"})["metadata"], "read")
        with self.assertRaises(RoutingError):
            permission_set({"metadata": "write"})


class MintingOwnershipTests(unittest.TestCase):
    """The Operator broker is not a minting path (Q5).

    Every assertion here is about the refusal:  the guard must fire before any
    App credential is read, before any cache file is written and before any
    GitHub call is made, and it must name git-relay as the path that owns
    minting.
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.loader_calls = []
        self.api_calls = []
        self.router = TokenRouter(
            self.loader,
            Path(self.temp.name) / "cache",
            api=self.api,
            signer=lambda *a: "MOCK",
            clock=lambda: 1800000000,
        )

    def loader(self, slug):
        self.loader_calls.append(slug)
        return 100, "NOT-A-KEY"

    def api(self, method, path, bearer, body=None):
        self.api_calls.append((method, path))
        raise AssertionError("the guard must refuse before any GitHub call")

    def _refused(self, slug="swu-echo", target="example-user/example-repo", perms=None):
        with self.assertRaises(RoutingError) as ctx:
            self.router.token_for(slug, target, {"contents": "read"} if perms is None else perms)
        return ctx.exception

    def test_token_for_refuses_and_names_git_relay(self):
        exc = self._refused()
        self.assertEqual(MINTING_OWNER, "git-relay")
        self.assertIn(MINTING_OWNER, str(exc))
        self.assertIn("will not mint", str(exc))

    def test_refusal_is_a_definite_refusal_not_an_unknown_outcome(self):
        # "outcome_unknown" would tell a caller to reconcile a credential that
        # may exist;  nothing was requested, so this is a plain refusal.
        self.assertEqual(self._refused().outcome, "refused")
        self.assertIsNone(self._refused().status)

    def test_refusal_happens_before_any_credential_read(self):
        self._refused()
        self.assertEqual(self.loader_calls, [])

    def test_refusal_happens_before_any_github_call(self):
        self._refused()
        self.assertEqual(self.api_calls, [])

    def test_refusal_writes_no_token_cache(self):
        self._refused()
        self.assertEqual(list(self.router.cache.glob("*.json")), [])

    def test_refusal_never_carries_credential_material(self):
        exc = self._refused()
        message = str(exc)
        self.assertIsNone(getattr(exc, "credential_preview", None))
        self.assertNotIn("NOT-A-KEY", message)
        self.assertNotIn("MOCK", message)
        self.assertNotIn("PRIVATE KEY", message)

    def test_refusal_holds_for_any_slug_target_and_permissions(self):
        for slug, target, perms in (
            ("swu-echo", "example-user/example-repo", {"contents": "read"}),
            ("swu-wise", "acme-org/acme-repo", {"contents": "write"}),
            ("swu-echo", "a/b", {"metadata": "read"}),
        ):
            exc = self._refused(slug, target, perms)
            self.assertIn(MINTING_OWNER, str(exc))
        self.assertEqual(self.loader_calls, [])
        self.assertEqual(self.api_calls, [])


class CacheShapeTests(unittest.TestCase):
    """The private-cache guard still holds without reaching a mint."""

    def test_cache_dir_is_private_and_never_a_symlink(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        router = TokenRouter(lambda slug: (1, "K"), Path(temp.name) / "cache")
        path = router._cache_file(("scope",))
        self.assertEqual(path.parent.stat().st_mode & 0o077, 0)
        # A symlinked cache directory is refused rather than followed.
        link = Path(temp.name) / "link"
        link.symlink_to(path.parent)
        with self.assertRaises(RoutingError):
            TokenRouter(lambda slug: (1, "K"), link)._cache_file(("scope",))


if __name__ == "__main__":
    unittest.main()
