"""Bearer-pattern false positive: a constant NAME is not a credential value.

Regression coverage for the 2026-11-27 refusal of the Socratic Trade
securities-import route wiring: its pre-existing auth comment names the
environment variable the bearer token is read from, in ALL-CAPS_SNAKE
shape, and find_secret reported it as bearer_credential. The carve-out
suppresses only a token that is entirely an identifier shape; every
mixed-class value shape must still refuse.
"""

import unittest

from git_relay.secret_scan import find_secret

ENV_NAME = "PROBE_" + "IMPORT_TOKEN_X"
DUMMY = "abcdef" + "ghijklmnop"


class BearerConstantNameTests(unittest.TestCase):
    def test_env_var_name_in_comment_passes(self):
        text = "// Auth: bearer " + ENV_NAME + ", constant-time. DEFAULT-CLOSED."
        self.assertIsNone(find_secret(text))

    def test_env_var_name_at_sentence_end_passes(self):
        text = "text Bearer " + "PROBE_" + "TOKEN_NAME" + ". more prose"
        self.assertIsNone(find_secret(text))

    def test_dummy_literal_still_refused(self):
        text = 'h = "Authorization: Bearer ' + DUMMY + '";'
        self.assertEqual(find_secret(text), "bearer_credential")

    def test_lowercase_token_still_refused(self):
        self.assertEqual(find_secret("bearer " + DUMMY), "bearer_credential")

    def test_all_caps_without_underscore_still_refused(self):
        self.assertEqual(
            find_secret("Bearer " + "ABCDEFGH" + "IJKLMNOP"), "bearer_credential"
        )

    def test_mixed_case_with_underscore_still_refused(self):
        self.assertEqual(
            find_secret("Bearer " + "AbC_dEfG" + "hIjKlMnOpQ"), "bearer_credential"
        )

    def test_base64_value_still_refused(self):
        self.assertEqual(
            find_secret("Bearer " + "QUJDREVGR0hJSktMTU5PUA=="), "bearer_credential"
        )

    def test_dotted_chain_still_refused(self):
        self.assertEqual(
            find_secret("Bearer " + "FOO_BAR_BAZ_QUX" + ".env"), "bearer_credential"
        )

    def test_plain_prose_passes(self):
        self.assertIsNone(
            find_secret("The client sends a bearer token with each request.")
        )


if __name__ == "__main__":
    unittest.main()
