import unittest

from git_relay.app_tokens import AppTokenError, AppTokenMinter


class Source:
    def secrets(self, names):
        return {n: ("12" if n.endswith("_ID") else "private-test-fixture") for n in names}


class Metadata(unittest.TestCase):
    def fixture(self, change=None):
        install = {
            "id": 34,
            "app_id": 12,
            "app_slug": "swu-fixture",
            "account": {"id": 56, "login": "Example-Org"},
            "permissions": {"contents": "write", "workflows": "write"},
            "suspended_at": None,
            "token": "must-not-return",
        }
        if change:
            install.update(change)
        calls = []

        def api(method, path, bearer):
            calls.append((method, path))
            return {"id": 12, "slug": "swu-fixture"} if path == "/app" else install

        return AppTokenMinter(Source(), api, signer=lambda *a: "fixture-jwt"), calls

    def test_get_only_safe_projection(self):
        m, c = self.fixture()
        x = m.installation_metadata("swu-fixture", "Example-Org/Fixture-Repo")
        self.assertEqual(len(c), 2)
        self.assertTrue(all(v[0] == "GET" for v in c))
        self.assertNotIn("token", str(x))
        self.assertEqual(x["installation"]["accepted_permissions"]["workflows"], "write")
        self.assertFalse(m._tokens)

    def test_wrong_identity_account_or_permissions_fails(self):
        for change in [
            {"app_id": 99},
            {"app_slug": "swu-other-fixture"},
            {"account": {"id": 56, "login": "Other"}},
            {"permissions": {"bad": "admin"}},
            {"id": True},
            {"suspended_at": 123},
        ]:
            m, _ = self.fixture(change)
            with self.assertRaises(AppTokenError):
                m.installation_metadata("swu-fixture", "Example-Org/Fixture-Repo")

    def test_suspended_is_reported_not_minted(self):
        m, _ = self.fixture({"suspended_at": "2026-10-31T00:00:00Z"})
        self.assertTrue(
            m.installation_metadata("swu-fixture", "Example-Org/Fixture-Repo")["installation"][
                "suspended"
            ]
        )

    def test_selectors_rejected_by_closed_shape(self):
        from git_relay.workflow_verbs import Refused, WorkflowVerbs

        for key in ["seat", "app_slug", "installation_id", "token", "url"]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(
                    "wf_installation_metadata",
                    {"repo": "Example-Org/Fixture-Repo", key: "other"},
                    "Example-Org/Fixture-Repo",
                )


if __name__ == "__main__":
    unittest.main()


class Dispatch(unittest.TestCase):
    def test_own_policy_identity_gets_metadata_without_token_or_outcome(self):
        from unittest.mock import MagicMock

        from git_relay.server import Relay

        from .test_workflow_verbs import REPO

        relay = Relay.__new__(Relay)
        relay.policy = MagicMock()
        relay.policy.app_identity.return_value = ("swu-fixture", {"metadata": "read"})
        relay.app_minter = MagicMock()
        relay.app_minter.installation_metadata.return_value = {"installation": {"safe": True}}
        out = relay._dispatch_workflow(
            "swu-fixture", "wf_installation_metadata", {"repo": REPO}, REPO
        )
        self.assertEqual(out, {"installation": {"safe": True}})
        relay.app_minter.token_for.assert_not_called()
        relay.app_minter.installation_metadata.assert_called_once_with("swu-fixture", REPO)

    def test_other_policy_identity_is_denied(self):
        from unittest.mock import MagicMock

        from git_relay.server import PolicyDenied, Relay

        relay = Relay.__new__(Relay)
        relay.policy = MagicMock()
        relay.policy.app_identity.return_value = ("swu-other-fixture", {})
        relay.app_minter = MagicMock()
        with self.assertRaises(PolicyDenied):
            relay._dispatch_workflow(
                "swu-fixture", "wf_installation_metadata", {"repo": "a/b"}, "a/b"
            )
        relay.app_minter.installation_metadata.assert_not_called()

    def test_upstream_errors_are_fixed_not_reflected(self):
        from unittest.mock import MagicMock

        from git_relay.server import Relay, ServerUnavailable

        relay = Relay.__new__(Relay)
        relay.policy = MagicMock()
        relay.policy.app_identity.return_value = ("swu-fixture", {"metadata": "read"})
        relay.app_minter = MagicMock()
        relay.app_minter.installation_metadata.side_effect = AppTokenError(
            "hostile secret shaped prose"
        )
        with self.assertRaises(ServerUnavailable) as caught:
            relay._dispatch_workflow(
                "swu-fixture", "wf_installation_metadata", {"repo": "a/b"}, "a/b"
            )
        self.assertNotIn("hostile", str(caught.exception))


class Boundaries(unittest.TestCase):
    def test_bool_app_binding_fails_even_for_id_one(self):
        m, _ = Metadata().fixture({"app_id": True})
        with self.assertRaises(AppTokenError):
            m.installation_metadata("swu-fixture", "Example-Org/Fixture-Repo")

    def test_safe_error_status_and_retry_without_prose(self):
        from git_relay.app_tokens import InstallationReadError
        from git_relay.github_client import RelayGitHubError

        m, _ = Metadata().fixture()

        def api(*args):
            raise RelayGitHubError(429, "never reflect hostile prose", retry_after=30)

        m._api = api
        with self.assertRaises(InstallationReadError) as caught:
            m.installation_metadata("swu-fixture", "Example-Org/Fixture-Repo")
        self.assertEqual(caught.exception.status, 429)
        self.assertEqual(caught.exception.retry_after, 30)
        self.assertNotIn("hostile", str(caught.exception))

    def test_missing_metadata_ceiling_refuses_before_custodian(self):
        from unittest.mock import MagicMock

        from git_relay.server import PolicyDenied, Relay

        relay = Relay.__new__(Relay)
        relay.policy = MagicMock()
        relay.policy.app_identity.return_value = ("swu-fixture", {})
        relay.app_minter = MagicMock()
        with self.assertRaises(PolicyDenied):
            relay._dispatch_workflow(
                "swu-fixture",
                "wf_installation_metadata",
                {"repo": "Example-Org/Fixture-Repo"},
                "Example-Org/Fixture-Repo",
            )
        relay.app_minter.installation_metadata.assert_not_called()
