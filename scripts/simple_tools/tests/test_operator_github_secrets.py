"""The named-secret adapter, over the consumer's own scoped service token.

    cd scripts/simple_tools/tests && python3 -m unittest test_operator_github_secrets -v

No network and no token store:  the shared client's scoped read is faked.
"""

import os
import unittest
from unittest.mock import patch

import fleet_infisical
from simple_tools.operator_github_secrets import NamedSecrets, scoped_secret_get
from simple_tools.github_token_router import RoutingError


def fake_read(value="mock", status="ok"):
    """A fake scoped read:  one name, one value, one status."""

    def read(name, folder, account, env=None):
        read.calls.append((name, folder, account))
        return (value, status)

    read.calls = []
    return read


class Tests(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def read(path, name):
            self.calls.append((path, name))
            return "mock"

        self.loader = NamedSecrets(["CODEX"], ["swu-example-app"], transport=read)

    def test_names_only(self):
        self.loader.caller_key("CODEX")
        self.loader.app_credentials("swu-example-app")
        self.assertEqual(
            self.calls,
            [
                ("/operator-github/callers", "OPERATOR_GITHUB_CALLER_CODEX_HMAC"),
                ("/github-apps", "GH_APP_SWU_EXAMPLE_APP_ID"),
                ("/github-apps", "GH_APP_SWU_EXAMPLE_APP_PRIVATE_KEY"),
            ],
        )

    def test_unknown_seat_no_read(self):
        self.assertIsNone(self.loader.caller_key("CLAUDE"))
        self.assertEqual(self.calls, [])

    def test_unknown_app_no_read(self):
        with self.assertRaises(RoutingError):
            self.loader.app_credentials("swu-echo")
        self.assertEqual(self.calls, [])

    def test_the_default_transport_reads_through_the_scoped_token(self):
        read = fake_read("v")
        with patch.object(fleet_infisical, "fetch_scoped", read):
            self.assertEqual(NamedSecrets(["CODEX"], []).caller_key("CODEX"), "v")
        self.assertEqual(
            read.calls,
            [
                (
                    "OPERATOR_GITHUB_CALLER_CODEX_HMAC",
                    "/operator-github/callers",
                    "operator-github-callers-ro",
                )
            ],
        )

    def test_the_default_transport_never_consults_the_environment(self):
        read = fake_read("from-the-store")
        with patch.dict(os.environ, {"OPERATOR_GITHUB_CALLER_CODEX_HMAC": "from-env"}):
            with patch.object(fleet_infisical, "fetch_scoped", read):
                self.assertEqual(
                    NamedSecrets(["CODEX"], []).caller_key("CODEX"), "from-the-store"
                )

    def test_a_refused_read_names_the_variable_not_the_value(self):
        for status in ("no-token", "keychain-locked", "token-unreadable", "forbidden", "failed"):
            with patch.object(fleet_infisical, "fetch_scoped", fake_read("", status)):
                with self.assertRaises(RoutingError) as caught:
                    scoped_secret_get("/operator-github/callers", "OPERATOR_GITHUB_CALLER_CODEX_HMAC")
            self.assertIn("OPERATOR_GITHUB_CALLER_CODEX_HMAC", str(caught.exception))
            self.assertIn(status, str(caught.exception))

    def test_a_folder_without_a_token_is_refused(self):
        with self.assertRaises(RoutingError) as caught:
            scoped_secret_get("/nowhere", "SOME_NAME")
        self.assertIn("SOME_NAME", str(caught.exception))


class TimeoutOutcomeTests(unittest.TestCase):
    """A hung lookup is retryable;  a refused credential is not."""

    def test_a_timeout_is_retryable_and_not_refused(self) -> None:
        def timeout(name, folder, account, env=None):
            raise TimeoutError("timed out")

        with patch.object(fleet_infisical, "fetch_scoped", timeout):
            with self.assertRaises(RoutingError) as caught:
                scoped_secret_get("/operator-github/callers", "OPERATOR_GITHUB_CALLER_CODEX_HMAC")
        self.assertEqual(caught.exception.outcome, "retryable")
        self.assertIn("OPERATOR_GITHUB_CALLER_CODEX_HMAC", str(caught.exception))


class LocalFailureOutcomeTests(unittest.TestCase):
    def test_local_lookup_failures_are_refused_so_the_nonce_is_released(self):
        for status in ("no-token", "keychain-locked", "token-unreadable", "forbidden", "failed"):
            with patch.object(fleet_infisical, "fetch_scoped", fake_read("", status)):
                with self.assertRaises(RoutingError) as caught:
                    scoped_secret_get("/github-apps", "GH_APP_SWU_EXAMPLE_ID")
            self.assertEqual(caught.exception.outcome, "refused")


if __name__ == "__main__":
    unittest.main()
