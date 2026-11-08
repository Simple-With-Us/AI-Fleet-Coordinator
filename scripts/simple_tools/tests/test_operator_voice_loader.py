"""The server's Operator voice (AFC-602):  the loader reads the Operator bot's own two names with their
own read-only token, and the credential reader takes them from the environment it is given.

The server container ships no fleet-secrets and its loader deliberately strips the machine identity, so
before this the Operator's credential read could only fail there (`fleet-secrets is not installed`).  The
regression test at the bottom is the one that matters:  whatever `build_env` produces on the server has
to be enough for `operator_credentials`, so a later change to the stripped names cannot silently cut the
Operator off again.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID

from simple_tools import service_identity as SI
from simple_tools import zulip as Z

SPEC = importlib.util.spec_from_file_location(
    "loader_voice", Path(__file__).resolve().parents[1] / "server" / "infisical_env.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)

PRIMARY_PROJECT = str(UUID(int=1))
FLEET_OPS_PROJECT = str(UUID(int=3))
PRIMARY_TOKEN = "st.primary-service-token-fixture-value"
VOICE_TOKEN = "st.operator-voice-service-token-fixture"
OPERATOR_ID = 4242
OPERATOR_EMAIL = "operator-bot@realm.example.test"
OPERATOR_KEY = "operator-key-" + "x" * 24


def voice_env(**overrides: str) -> dict[str, str]:
    values = {
        "INFISICAL_TOKEN": PRIMARY_TOKEN,
        "INFISICAL_PROJECT_ID": PRIMARY_PROJECT,
        MODULE.OPERATOR_VOICE_PROJECT: FLEET_OPS_PROJECT,
        MODULE.OPERATOR_VOICE_TOKEN: VOICE_TOKEN,
        "SIMPLE_TOOLS_INSTANCE": "server",
        "OPERATOR_ZULIP_USER_ID": str(OPERATOR_ID),
        "OPERATOR_ZULIP_EMAIL": OPERATOR_EMAIL,
        "ZULIP_SITE": "https://realm.example.test",
        "INFISICAL_AUTOMATION_CLIENT_ID": "machine-identity-id",
        "INFISICAL_AUTOMATION_CLIENT_SECRET": "machine-identity-secret",
    }
    values.update(overrides)
    return values


class OperatorVoiceKeysTests(unittest.TestCase):
    def read(self, secrets, env=None):
        calls = []

        def fetch(source, **kwargs):
            calls.append((dict(source), dict(kwargs)))
            if isinstance(secrets, Exception):
                raise secrets
            return secrets

        with patch.object(MODULE, "fetch", side_effect=fetch):
            keys, note = MODULE.operator_voice_keys(voice_env() if env is None else env)
        return keys, note, calls

    def test_it_reads_exactly_the_two_names_with_its_own_token_scope_and_project(self) -> None:
        keys, note, calls = self.read(
            {
                "ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL,
                "ZULIP_OPERATOR_API_KEY": OPERATOR_KEY,
                "SOMETHING_ELSE": "ignored",
            }
        )
        self.assertEqual(
            keys, {"ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL, "ZULIP_OPERATOR_API_KEY": OPERATOR_KEY}
        )
        self.assertNotIn(OPERATOR_KEY, note, "names and counts only")
        ((source, kwargs),) = calls
        self.assertEqual(kwargs["token"], VOICE_TOKEN, "never the primary token")
        self.assertEqual(kwargs["path"], "/zulip_operator")
        self.assertEqual(kwargs["expected_paths"], ("/zulip_operator",))
        self.assertEqual(source["INFISICAL_PROJECT_ID"], FLEET_OPS_PROJECT)
        self.assertEqual(source["INFISICAL_ENVIRONMENT"], "prod")

    def test_without_its_token_nothing_is_read_and_the_primary_token_is_never_reused(self) -> None:
        env = voice_env()
        env.pop(MODULE.OPERATOR_VOICE_TOKEN)
        keys, note, calls = self.read({}, env)
        self.assertEqual((keys, calls), ({}, []))
        self.assertIn("no dedicated token", note)

    def test_a_bad_project_or_token_never_fetches(self) -> None:
        for label, env, words in (
            ("project", voice_env(**{MODULE.OPERATOR_VOICE_PROJECT: "bad"}), "invalid project id"),
            ("token", voice_env(**{MODULE.OPERATOR_VOICE_TOKEN: "not-a-token"}), "token is invalid"),
        ):
            with self.subTest(label):
                keys, note, calls = self.read({}, env)
                self.assertEqual((keys, calls), ({}, []))
                self.assertIn(words, note)

    def test_a_failed_read_is_not_fatal_and_names_only_the_stage(self) -> None:
        keys, note, _ = self.read(MODULE.LoadError("read operator voice: HTTP 403"))
        self.assertEqual(keys, {})
        self.assertIn("not loaded (read operator voice: HTTP 403)", note)

    def test_half_a_pair_or_a_reference_is_not_loaded(self) -> None:
        for label, secrets in (
            ("one name", {"ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL}),
            ("reference", {"ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL, "ZULIP_OPERATOR_API_KEY": "${a.b}"}),
            ("empty", {"ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL, "ZULIP_OPERATOR_API_KEY": "  "}),
        ):
            with self.subTest(label):
                keys, note, _ = self.read(secrets)
                self.assertEqual(keys, {})
                self.assertIn("not loaded", note)


class BuildEnvVoiceTests(unittest.TestCase):
    def build(self, voice):
        def fetch(source, **kwargs):
            if kwargs.get("token") == VOICE_TOKEN:
                if isinstance(voice, Exception):
                    raise voice
                return voice
            return {}

        with (
            patch.object(MODULE, "server_codes", return_value={"MA"}),
            patch.object(MODULE, "fetch", side_effect=fetch),
        ):
            return MODULE.build_env(voice_env())

    def test_the_two_names_reach_the_daemon_and_every_bootstrap_credential_is_stripped(self) -> None:
        out, line = self.build(
            {"ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL, "ZULIP_OPERATOR_API_KEY": OPERATOR_KEY}
        )
        self.assertEqual(out["ZULIP_OPERATOR_API_KEY"], OPERATOR_KEY)
        for stripped in (
            MODULE.TOKEN,
            MODULE.OPERATOR_VOICE_TOKEN,
            "INFISICAL_AUTOMATION_CLIENT_ID",
            "INFISICAL_AUTOMATION_CLIENT_SECRET",
        ):
            self.assertNotIn(stripped, out)
        self.assertIn("operator voice keys from /zulip_operator: 2 of 2", line)
        self.assertNotIn(OPERATOR_KEY, line)

    def test_a_failed_voice_read_does_not_stop_the_listener_and_the_line_says_so(self) -> None:
        out, line = self.build(MODULE.LoadError("read operator voice: HTTP 401"))
        self.assertNotIn("ZULIP_OPERATOR_API_KEY", out)
        self.assertIn("operator voice keys not loaded", line)

    def test_what_the_server_loader_hands_over_is_enough_for_the_operator_credential(self) -> None:
        """The regression behind the 370 `operator-voice-unavailable` rows:  on the server instance
        the credential reader must be answered by the loader's own output, with no fleet-secrets,
        no machine identity and no fetch at all."""
        out, _ = self.build(
            {"ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL, "ZULIP_OPERATOR_API_KEY": OPERATOR_KEY}
        )

        def no_fetch(names):
            raise AssertionError("the reader went to the vault although the loader supplied the pair")

        creds = SI.operator_credentials(out, fetch=no_fetch)
        self.assertEqual((creds.email, creds.key), (OPERATOR_EMAIL, OPERATOR_KEY))
        self.assertIn("loader env", creds.source)
        self.assertNotIn(OPERATOR_KEY, creds.source)


class OperatorCredentialsFromEnvironmentTests(unittest.TestCase):
    def env(self, **overrides: str) -> dict[str, str]:
        base = {
            "OPERATOR_ZULIP_USER_ID": str(OPERATOR_ID),
            "OPERATOR_ZULIP_EMAIL": OPERATOR_EMAIL,
            "ZULIP_SITE": "https://realm.example.test",
            "ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL,
            "ZULIP_OPERATOR_API_KEY": OPERATOR_KEY,
        }
        base.update(overrides)
        return base

    def test_the_pair_in_the_environment_is_the_answer_without_a_project_or_a_fetch(self) -> None:
        creds = SI.operator_credentials(self.env())
        self.assertEqual((creds.email, creds.key), (OPERATOR_EMAIL, OPERATOR_KEY))

    def test_half_a_pair_is_refused_not_completed_from_the_vault(self) -> None:
        env = self.env()
        env.pop("ZULIP_OPERATOR_API_KEY")
        with self.assertRaises(Z.CredentialError) as caught:
            SI.operator_credentials(env, fetch=lambda names: self.fail("no vault read for a half pair"))
        self.assertIn("only one of the two names", str(caught.exception))

    def test_an_address_that_is_not_the_pinned_bot_is_refused(self) -> None:
        with self.assertRaises(Z.CredentialError) as caught:
            SI.operator_credentials(self.env(ZULIP_OPERATOR_EMAIL="seat-bot@realm.example.test"))
        self.assertIn("not the pinned service bot", str(caught.exception))

    def test_an_unresolved_reference_is_refused(self) -> None:
        with self.assertRaises(Z.CredentialError) as caught:
            SI.operator_credentials(self.env(ZULIP_OPERATOR_API_KEY="${folder.key}"))
        self.assertIn("unresolved reference", str(caught.exception))

    def test_a_partial_reference_is_refused_too(self) -> None:
        with self.assertRaises(Z.CredentialError) as caught:
            SI.operator_credentials(self.env(ZULIP_OPERATOR_API_KEY="abc${folder.key}def"))
        self.assertIn("unresolved reference", str(caught.exception))

    def test_with_no_pair_in_the_environment_the_reader_is_asked_as_before(self) -> None:
        env = self.env()
        env.pop("ZULIP_OPERATOR_EMAIL")
        env.pop("ZULIP_OPERATOR_API_KEY")
        env["INFISICAL_FLEET_OPS_PROJECT_ID"] = FLEET_OPS_PROJECT
        asked = []

        def fetch(names):
            asked.append(names)
            return {"ZULIP_OPERATOR_EMAIL": OPERATOR_EMAIL, "ZULIP_OPERATOR_API_KEY": OPERATOR_KEY}

        creds = SI.operator_credentials(env, fetch=fetch)
        self.assertEqual(creds.key, OPERATOR_KEY)
        self.assertEqual(len(asked), 1)
        self.assertIn("infisical Fleet-OPS prod /zulip_operator", creds.source)


if __name__ == "__main__":
    unittest.main()
