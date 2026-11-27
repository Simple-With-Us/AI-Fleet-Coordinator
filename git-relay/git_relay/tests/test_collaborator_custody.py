"""Offline default-deny policy and own-App lifecycle preparation."""

import copy
import types
import unittest
from types import SimpleNamespace

from git_relay.app_tokens import AppTokenError, AppTokenMinter, mint_permission_allowed
from git_relay.collaborator_custody import (
    ACTIONS,
    CustodyCleanupPending,
    CustodyDenied,
    authorize,
    installation_lease,
    parse_grants,
)

SEAT = "swu-wise"
REPO = "Simple-With-Us/Example"
ACTION = "wf_collaborator_set"
SOURCE = [{"repo": REPO, "actions": [ACTION]}]


class CustodyTests(unittest.TestCase):
    def grants(self, value=SOURCE):
        return parse_grants(SEAT, value, [REPO])

    def minter(self, mutate=None, failed=None):
        calls = []
        installation = f"/repos/{REPO}/installation"
        rows = {
            "app": {"id": 1, "slug": SEAT},
            "installation": {
                "id": 2,
                "app_id": 1,
                "app_slug": SEAT,
                "account": {"login": "Simple-With-Us"},
                "permissions": {"administration": "write"},
                "suspended_at": None,
            },
            "mint": {
                "token": "issued",
                "expires_at": "1970-01-01T00:30:00Z",
                "permissions": {"metadata": "read", "administration": "write"},
                "repositories": [{"full_name": REPO}],
            },
        }
        if mutate:
            mutate(rows)

        def api(method, path, bearer, body=None):
            calls.append((method, path, bearer, body))
            if path == failed:
                raise RuntimeError("upstream failure")
            if method == "POST":
                # Custody's only POST is the mint, and the request reaches
                # here through the real AppTokenMinter helper bound below:
                # the owner module builds the endpoint, so this fixture never
                # spells or reconstructs it.
                return copy.deepcopy(rows["mint"])
            if path == "/app":
                return copy.deepcopy(rows["app"])
            if path == installation:
                return copy.deepcopy(rows["installation"])
            return None

        minter = SimpleNamespace(
            _api=api,
            _app_credentials=lambda seat: (1, None),
            _signer=lambda *args: "signed",
            _clock=lambda: 1000,
        )
        # The mock owns transport only; issuance is the owner module's real
        # code, bound here so the test exercises the actual routing custody
        # depends on instead of a fake that rebuilds it.
        minter._mint_installation_token = types.MethodType(
            AppTokenMinter._mint_installation_token, minter
        )
        return minter, calls

    def lease(self, minter, grants=None, **kw):
        return installation_lease(
            minter,
            self.grants() if grants is None else grants,
            kw.get("seat", SEAT),
            kw.get("app_slug", SEAT),
            kw.get("action", ACTION),
            kw.get("repo", REPO),
        )

    def test_optional_grants_default_deny_and_do_not_mutate(self):
        before = copy.deepcopy(SOURCE)
        self.assertEqual(parse_grants(SEAT, None, []), ())
        self.assertEqual(parse_grants(SEAT, [], []), ())
        grants = self.grants()
        self.assertEqual(SOURCE, before)
        SOURCE[0]["actions"].append("wf_collaborator_remove")
        try:
            self.assertEqual(grants[0].actions, frozenset({ACTION}))
        finally:
            SOURCE[0]["actions"].pop()
        for absent in [(), []]:
            minter, calls = self.minter()
            with self.assertRaises(CustodyDenied), self.lease(minter, grants=absent):
                self.fail("must not yield")
            self.assertEqual(calls, [])

    def test_malformed_extra_wildcard_and_duplicate_grants_refused(self):
        candidates = [
            True,
            {},
            [None],
            [{"repo": REPO, "actions": []}],
            [{"repo": REPO, "actions": [ACTION, ACTION]}],
            [{"repo": REPO, "actions": ["create_repo"]}],
            [{"repo": REPO, "actions": [None]}],
            [{"repo": REPO, "actions": [ACTION], "permissions": {}}],
            [{"repo": "Simple-With-Us/*", "actions": [ACTION]}],
            SOURCE * 2,
            SOURCE + [{"repo": REPO.lower(), "actions": [ACTION]}],
        ]
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(CustodyDenied):
                self.grants(candidate)
        with self.assertRaises(CustodyDenied):
            parse_grants(SEAT, SOURCE, ["Simple-With-Us/*"])
        with self.assertRaises(CustodyDenied):
            parse_grants(SEAT, SOURCE, ["Elsewhere/Example"])
        with self.assertRaises(CustodyDenied):
            parse_grants("Jay", SOURCE, [REPO])

    def test_exact_seat_app_repository_and_action_no_scope_inheritance(self):
        for change in [
            {"seat": "swu-echo"},
            {"app_slug": "swu-echo"},
            {"repo": "Simple-With-Us/Other"},
            {"repo": REPO + "/../Other"},
            {"action": "wf_collaborator_remove"},
            {"action": "create_repo"},
        ]:
            minter, calls = self.minter()
            with self.subTest(change=change), self.assertRaises(CustodyDenied):
                with self.lease(minter, **change):
                    self.fail("must not yield")
            self.assertEqual(calls, [])
        authorize(self.grants(), SEAT, SEAT, ACTION, REPO.upper())
        # The fleet-wide mint ceiling changed on main after this branch was
        # written (#2475/#2479/#2480 moved administration into the ceiling at
        # its requested level). Custody's guarantee here is the exact
        # seat/app/repository/action scope asserted above, not the global
        # ceiling, so only the read-level fact is asserted.
        self.assertTrue(mint_permission_allowed("administration", "read"))
        self.assertEqual(ACTIONS, {"wf_collaborator_set", "wf_collaborator_remove"})

    def test_least_single_repository_uncached_lease_releases_each_time(self):
        minter, calls = self.minter()
        for _ in range(2):
            with self.lease(minter) as issued:
                self.assertEqual(issued, "issued")
                self.assertEqual(calls[-1][0], "POST")
            self.assertEqual(calls[-1][:3], ("DELETE", "/installation/token", "issued"))
        self.assertEqual(sum(c[0] == "POST" for c in calls), 2)
        method, path, bearer, body = calls[2]
        self.assertEqual(method, "POST")
        # The owner module's helper built this path; assert its shape without
        # restating the endpoint spelling it owns.
        self.assertTrue(path.startswith("/app/installations/2/"))
        self.assertEqual(bearer, "signed")
        self.assertEqual(
            body,
            {
                "repositories": ["Example"],
                "permissions": {"metadata": "read", "administration": "write"},
            },
        )

    def test_handler_exception_still_attempts_cleanup(self):
        minter, calls = self.minter()
        with self.assertRaisesRegex(RuntimeError, "handler failed"), self.lease(minter):
            raise RuntimeError("handler failed")
        self.assertEqual(calls[-1][0], "DELETE")

    def test_identity_installation_and_scope_fail_closed(self):
        changes = [
            lambda r: r["app"].update(id=True),
            lambda r: r["app"].update(slug="swu-echo"),
            lambda r: r["installation"].update(id=True),
            lambda r: r["installation"].update(app_id=True),
            lambda r: r["installation"].update(app_id=3),
            lambda r: r["installation"].update(app_slug="swu-echo"),
            lambda r: r["installation"].update(suspended_at="2026-01-01T00:00:00Z"),
            lambda r: r["installation"].pop("suspended_at"),
            lambda r: r["installation"].update(account={"login": "Elsewhere"}),
            lambda r: r["installation"].update(permissions={"administration": "read"}),
            lambda r: r["mint"].update(repositories=[]),
            lambda r: r["mint"].update(repositories=[{"full_name": "Elsewhere/Example"}]),
            lambda r: r["mint"].update(permissions={"administration": "write"}),
            lambda r: r["mint"].update(expires_at="not-a-date"),
            lambda r: r["mint"].update(expires_at="1970-01-01T00:00:01Z"),
            lambda r: r["mint"].update(expires_at="2030-01-01T00:00:01Z"),
            lambda r: r["mint"].update(expires_at="1970-01-01T00:30:00"),
        ]
        for i, mutate in enumerate(changes):
            minter, calls = self.minter(mutate)
            with self.subTest(i=i), self.assertRaises(AppTokenError), self.lease(minter):
                self.fail("must not yield")
            if any(c[0] == "POST" for c in calls):
                self.assertEqual(calls[-1][0], "DELETE")
            else:
                self.assertNotIn("DELETE", [c[0] for c in calls])

    def test_expiry_compared_against_one_clock_read(self):
        minter, calls = self.minter()
        reads = iter([1000, 1000])
        minter._clock = lambda: next(reads)
        with self.lease(minter) as issued:
            self.assertEqual(issued, "issued")

    def test_preparation_is_not_registered_or_granted(self):
        from git_relay.policy import SeatPolicy
        from git_relay.workflow_verbs import SPECS

        self.assertFalse(ACTIONS.intersection(SPECS))
        self.assertFalse(ACTIONS.intersection(SeatPolicy.ALL_ACTIONS))

    def test_setup_failures_are_sanitized_without_retry(self):
        minter, calls = self.minter(failed="/app")
        with (
            self.assertRaisesRegex(AppTokenError, "custody upstream unavailable"),
            self.lease(minter),
        ):
            self.fail("must not yield")
        self.assertEqual(len(calls), 1)

    def test_cleanup_failure_is_explicit_terminal_no_replay(self):
        minter, calls = self.minter(failed="/installation/token")
        with self.assertRaisesRegex(CustodyCleanupPending, "do not replay"):
            with self.lease(minter):
                pass
        self.assertEqual(sum(c[0] == "POST" for c in calls), 1)
        self.assertEqual(sum(c[0] == "DELETE" for c in calls), 1)


if __name__ == "__main__":
    unittest.main()
