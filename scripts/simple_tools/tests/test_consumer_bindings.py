"""The one rule every consumer's own grant follows (the owner's ruling of November 22, 2026).

The store is the authority:  the consumer's own account token is read from the host's approved
store first,  and it is the credential the read runs under.  An environment binding is a second
source only when the token's own grant metadata proves it is this consumer's narrow grant:  the
workspace is the project,  the permissions are exactly read,  the granted folder is exactly the one
the consumer reads,  and the token's own name is this consumer's account.  A general binding scoped
to "/" or to several folders fails that check,  so it can no longer serve a consumer.

The shared client is `scripts/fleet_infisical.py`;  `consumer_binding` is the one way a consumer
resolves its own grant.
"""

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import fleet_infisical as FI
from fleet_knobs.tests._fake import ScopedReadAPI, store_token

ACCOUNT = "zulip-ro"
FOLDER = "/zulip"
PROJECT = str(uuid.uuid4())


class ConsumerBindingTests(unittest.TestCase):
    """The shared client's binding:  the store first,  a bound copy only when it proves itself."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="binding-home-"))
        self.addCleanup(self._clean)
        self.env = {"HOME": str(self.home), "INFISICAL_ENVIRONMENT": "prod"}

    def _clean(self):
        import shutil

        shutil.rmtree(self.home, ignore_errors=True)

    def _api(self, **kwargs):
        """A fake provider that serves this consumer's own narrow grant."""
        api = ScopedReadAPI(
            {"CODEX_ZULIP_KEY": "public fixture"},
            project=PROJECT,
            path=FOLDER,
            token_name=ACCOUNT,
            **kwargs,
        )
        api.__enter__()
        self.addCleanup(api.__exit__)
        self.env["FLEET_SECRETS_API"] = api.env["FLEET_SECRETS_API"]
        return api

    def test_the_store_is_read_first_and_the_environment_is_never_consulted(self):
        # The consumer's own account token is in the store:  it is the credential,  and the
        # environment is not looked at at all.
        api = self._api()
        store_token(self.home, ACCOUNT, api.token)
        self.env["INFISICAL_TOKEN"] = "st." + "general-binding-" * 3
        token, source = FI.consumer_binding(
            ACCOUNT, FOLDER, self.env, PROJECT, home=self.home
        )
        self.assertEqual((token, source), (api.token, "store"))
        # The provider was never asked:  the store answered.
        self.assertEqual(api.requests, [])

    def test_a_bound_copy_of_the_consumers_own_grant_is_accepted(self):
        # No store,  but the bound copy proves itself:  the workspace is the project,  the
        # permissions are exactly read,  the folder is exactly this one,  and the token's own name
        # is this account.
        api = self._api()
        self.env["INFISICAL_TOKEN"] = api.token
        token, source = FI.consumer_binding(
            ACCOUNT, FOLDER, self.env, PROJECT, home=self.home
        )
        self.assertEqual((token, source), (api.token, "environment"))

    def test_a_general_or_wider_scoped_token_is_refused_and_names_the_account(self):
        cases = (
            ({"scopes": [{"environment": "prod", "secretPath": "/"}]}, "token-scope-mismatch"),
            (
                {
                    "scopes": [
                        {"environment": "prod", "secretPath": FOLDER},
                        {"environment": "prod", "secretPath": "/"},
                    ]
                },
                "token-scope-mismatch",
            ),
            ({"permissions": ["read", "write"]}, "token-scope-mismatch"),
            ({"workspace": str(uuid.uuid4())}, "token-scope-mismatch"),
            ({"expiresAt": "2000-01-01T00:00:00Z"}, "token-expired-or-invalid"),
            ({"name": "someone-else-ro"}, "token-name-mismatch"),
        )
        for metadata, refusal in cases:
            with self.subTest(metadata=metadata):
                api = self._api()
                api.metadata_override = metadata
                self.env["INFISICAL_TOKEN"] = api.token
                with self.assertRaises(FI.SecretsError) as caught:
                    FI.consumer_binding(ACCOUNT, FOLDER, self.env, PROJECT, home=self.home)
                message = str(caught.exception)
                self.assertIn(ACCOUNT, message)
                self.assertIn(refusal, message)
                # The refusal never carries a token value.
                self.assertNotIn(api.token, message)

    def test_nothing_is_written_to_a_log_or_a_message(self):
        api = self._api()
        api.metadata_override = {"scopes": [{"environment": "prod", "secretPath": "/"}]}
        self.env["INFISICAL_TOKEN"] = api.token
        with self.assertNoLogs(level="DEBUG"):
            with self.assertRaises(FI.SecretsError) as caught:
                FI.consumer_binding(ACCOUNT, FOLDER, self.env, PROJECT, home=self.home)
        # The refusal names the account and the reason,  never a token value.
        self.assertNotIn(api.token, str(caught.exception))
        self.assertIn(ACCOUNT, str(caught.exception))

    def test_a_missing_store_and_a_missing_binding_name_the_account(self):
        with self.assertRaises(FI.SecretsError) as caught:
            FI.consumer_binding(ACCOUNT, FOLDER, self.env, PROJECT, home=self.home)
        self.assertIn(ACCOUNT, str(caught.exception))
        self.assertIn("no-token", str(caught.exception))

    def test_an_account_this_consumer_does_not_name_is_refused(self):
        with self.assertRaises(FI.SecretsError) as caught:
            FI.consumer_binding("../etc/passwd", FOLDER, self.env, PROJECT, home=self.home)
        self.assertIn("service-token-required", str(caught.exception))


class SiteAccountTests(unittest.TestCase):
    """Every consumer names the account it reads,  and the account is the token's own name."""

    def test_the_accounts_are_named_in_code(self):
        from simple_tools import notes_client as NC
        from simple_tools import zulip as Z

        self.assertEqual(Z.INFISICAL_TOKEN_ACCOUNTS["/zulip"], "zulip-ro")
        self.assertEqual(Z.INFISICAL_TOKEN_ACCOUNTS["/zulip_operator"], "zulip-operator-ro")
        self.assertEqual(Z.INFISICAL_TOKEN_ACCOUNTS["/wake"], "wake-ro")
        self.assertEqual(
            NC.TOKEN_ACCOUNTS[("ai-fleet-coordinator", "/operator")], "notes-operator-ro"
        )


if __name__ == "__main__":
    unittest.main()
