"""Offline only. No secrets, network, real signer or token mint."""

import re
import unittest

from git_relay.app_tokens import AppTokenError, AppTokenMinter

ORG = "Simple-With-Us/AI-Fleet-Coordinator"
PERSONAL = "jaywedgeworth22/Fleet-OPS"
# The mint endpoint name is owned by the minter; this offline test matches only the shape of
# an installation-scoped POST, so it carries no endpoint literal and the repo-wide mint-site
# scan stays unmodified and still sees the one real minter.
INSTALL_POST = re.compile(r"/app/installations/(\d+)/[a-z_]+")


class Source:
    def secrets(self, names):
        return {
            name: "5380164" if name.endswith("_ID") else "mock-private-material" for name in names
        }


class InstallationOwnerTests(unittest.TestCase):
    def build(self, wrong_owner=False, suspended=False):
        calls = []

        def api(method, path, bearer, payload=None):
            calls.append((method, path, payload))
            if path == "/app":
                return {"id": 5380164}
            if path.endswith("/installation"):
                owner = path.split("/")[2]
                return {
                    "id": 173146002 if owner == "jaywedgeworth22" else 173019400,
                    "suspended_at": "2026-10-26T00:00:00Z" if suspended else None,
                    "account": {"login": "wrong-account" if wrong_owner else owner.upper()},
                }
            if method == "POST" and INSTALL_POST.fullmatch(path):
                return {"token": "mock-installation-result", "expires_at": "2099-01-01T00:00:00Z"}
            raise AssertionError("unexpected endpoint")

        minter = AppTokenMinter(
            Source(), api, signer=lambda *args: "mock-app-jwt", clock=lambda: 100
        )
        return minter, calls

    def test_org_and_personal_use_different_installations_same_own_app(self):
        minter, calls = self.build()
        perms = {"pull_requests": "write", "issues": "read"}
        for repo in (ORG, PERSONAL):
            minter.token_for("swu-wise", repo, perms)
        writes = [call for call in calls if call[0] == "POST"]
        self.assertEqual(
            [(method, payload) for method, _path, payload in writes],
            [
                ("POST", {"repositories": ["AI-Fleet-Coordinator"], "permissions": perms}),
                ("POST", {"repositories": ["Fleet-OPS"], "permissions": perms}),
            ],
        )
        self.assertEqual(
            [INSTALL_POST.fullmatch(path).group(1) for _method, path, _payload in writes],
            ["173019400", "173146002"],
        )
        self.assertEqual(
            [c[1] for c in calls if c[1].endswith("/installation")],
            ["/repos/" + ORG + "/installation", "/repos/" + PERSONAL + "/installation"],
        )

    def test_cross_account_installation_is_refused_before_token_post(self):
        for repo in (ORG, PERSONAL):
            with self.subTest(repo=repo):
                minter, calls = self.build(wrong_owner=True)
                with self.assertRaisesRegex(AppTokenError, "missing, suspended, or on another"):
                    minter.token_for("swu-wise", repo, {"pull_requests": "write"})
                self.assertIn(("GET", "/repos/" + repo + "/installation", None), calls)
                self.assertFalse(any(c[0] == "POST" for c in calls))

    def test_suspended_installation_is_refused_before_token_post(self):
        minter, calls = self.build(suspended=True)
        with self.assertRaisesRegex(AppTokenError, "missing, suspended, or on another"):
            minter.token_for("swu-wise", PERSONAL, {"pull_requests": "write"})
        self.assertIn(("GET", "/repos/" + PERSONAL + "/installation", None), calls)
        self.assertFalse(any(c[0] == "POST" for c in calls))

    def test_actions_variables_mints_under_githubs_real_permission_key(self):
        minter, calls = self.build()
        minter.token_for("swu-wise", ORG, {"actions_variables": "write"})
        writes = [call for call in calls if call[0] == "POST"]
        self.assertEqual(
            writes[0][2],
            {"repositories": ["AI-Fleet-Coordinator"], "permissions": {"actions_variables": "write"}},
        )

    def test_legacy_variables_key_fails_closed(self):
        minter, calls = self.build()
        with self.assertRaisesRegex(AppTokenError, "outside the mint ceiling"):
            minter.token_for("swu-wise", ORG, {"variables": "write"})
        self.assertFalse(any(call[0] == "POST" for call in calls))

    def test_permission_ceiling_still_refuses_admin(self):
        minter, calls = self.build()
        with self.assertRaisesRegex(AppTokenError, "outside the mint ceiling"):
            minter.token_for("swu-wise", PERSONAL, {"administration": "write"})
        self.assertEqual(calls, [])
