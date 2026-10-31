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
            "app_slug": "swu-wise",
            "account": {"id": 56, "login": "Simple-With-Us"},
            "permissions": {"contents": "write", "workflows": "write"},
            "suspended_at": None,
            "token": "must-not-return",
        }
        if change:
            install.update(change)
        calls = []

        def api(method, path, bearer):
            calls.append((method, path))
            return {"id": 12, "slug": "swu-wise"} if path == "/app" else install

        return AppTokenMinter(Source(), api, signer=lambda *a: "fixture-jwt"), calls

    def test_get_only_safe_projection(self):
        m, c = self.fixture()
        x = m.installation_metadata("swu-wise", "Simple-With-Us/AI-Fleet-Coordinator")
        self.assertEqual(len(c), 2)
        self.assertTrue(all(v[0] == "GET" for v in c))
        self.assertNotIn("token", str(x))
        self.assertEqual(x["installation"]["accepted_permissions"]["workflows"], "write")
        self.assertFalse(m._tokens)

    def test_wrong_identity_account_or_permissions_fails(self):
        for change in [
            {"app_id": 99},
            {"app_slug": "swu-echo"},
            {"account": {"id": 56, "login": "Other"}},
            {"permissions": {"bad": "admin"}},
            {"id": True},
            {"suspended_at": 123},
        ]:
            m, _ = self.fixture(change)
            with self.assertRaises(AppTokenError):
                m.installation_metadata("swu-wise", "Simple-With-Us/AI-Fleet-Coordinator")

    def test_suspended_is_reported_not_minted(self):
        m, _ = self.fixture({"suspended_at": "2026-10-31T00:00:00Z"})
        self.assertTrue(
            m.installation_metadata("swu-wise", "Simple-With-Us/AI-Fleet-Coordinator")[
                "installation"
            ]["suspended"]
        )

    def test_selectors_rejected_by_closed_shape(self):
        from git_relay.workflow_verbs import Refused, WorkflowVerbs

        for key in ["seat", "app_slug", "installation_id", "token", "url"]:
            with self.assertRaises(Refused):
                WorkflowVerbs.check_shape(
                    "wf_installation_metadata",
                    {"repo": "Simple-With-Us/AI-Fleet-Coordinator", key: "other"},
                    "Simple-With-Us/AI-Fleet-Coordinator",
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
        relay.policy.app_identity.return_value = ("swu-wise", {})
        relay.app_minter = MagicMock()
        relay.app_minter.installation_metadata.return_value = {"installation": {"safe": True}}
        out = relay._dispatch_workflow("swu-wise", "wf_installation_metadata", {"repo": REPO}, REPO)
        self.assertEqual(out, {"installation": {"safe": True}})
        relay.app_minter.token_for.assert_not_called()
        relay.app_minter.installation_metadata.assert_called_once_with("swu-wise", REPO)

    def test_other_policy_identity_is_denied(self):
        from unittest.mock import MagicMock

        from git_relay.server import PolicyDenied, Relay

        relay = Relay.__new__(Relay)
        relay.policy = MagicMock()
        relay.policy.app_identity.return_value = ("swu-echo", {})
        relay.app_minter = MagicMock()
        with self.assertRaises(PolicyDenied):
            relay._dispatch_workflow("swu-wise", "wf_installation_metadata", {"repo": "a/b"}, "a/b")
        relay.app_minter.installation_metadata.assert_not_called()

    def test_upstream_errors_are_fixed_not_reflected(self):
        from unittest.mock import MagicMock

        from git_relay.server import Relay, ServerUnavailable

        relay = Relay.__new__(Relay)
        relay.policy = MagicMock()
        relay.policy.app_identity.return_value = ("swu-wise", {})
        relay.app_minter = MagicMock()
        relay.app_minter.installation_metadata.side_effect = AppTokenError(
            "hostile secret shaped prose"
        )
        with self.assertRaises(ServerUnavailable) as caught:
            relay._dispatch_workflow("swu-wise", "wf_installation_metadata", {"repo": "a/b"}, "a/b")
        self.assertNotIn("hostile", str(caught.exception))
