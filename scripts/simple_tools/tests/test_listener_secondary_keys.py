from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import UUID


SPEC = importlib.util.spec_from_file_location(
    "loader", Path(__file__).resolve().parents[1] / "server" / "infisical_env.py"
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)

PRIMARY_PROJECT = str(UUID(int=1))
SECONDARY_PROJECT = str(UUID(int=2))
PRIMARY_TOKEN = "st.primary-service-token-fixture-value"
SECONDARY_TOKEN = "st.secondary-service-token-fixture-value"


class ListenerSecondaryKeyTests(unittest.TestCase):
    def env(self, **overrides):
        values = {
            "INFISICAL_TOKEN": PRIMARY_TOKEN,
            "INFISICAL_PROJECT_ID": PRIMARY_PROJECT,
            MODULE.LISTENER_READ_PROJECT: SECONDARY_PROJECT,
            MODULE.LISTENER_READ_TOKEN: SECONDARY_TOKEN,
            "MA_ROUTINE_KEY": "stale" * 12,
        }
        values.update(overrides)
        return values

    def run_case(self, secondary):
        calls = []

        def fetch(env, **kwargs):
            calls.append((env.copy(), kwargs.copy()))
            if kwargs.get("token") == SECONDARY_TOKEN:
                if isinstance(secondary, Exception):
                    raise secondary
                return secondary
            if kwargs.get("path") == MODULE.OPERATOR_PATH:
                return {}
            return {
                "ZULIP_MA_EMAIL": "own@example.test",
                "MA_ROUTINE_KEY": "primary" * 12,
                "JET_ROUTINE_KEY": "jet" * 15,
                "PATH": "untrusted",
            }

        with (
            patch.object(MODULE, "server_codes", return_value={"MA", "GB_FIXER", "JET"}),
            patch.object(MODULE, "fetch", side_effect=fetch),
        ):
            out, line = MODULE.build_env(self.env())
        return out, line, calls

    def test_secondary_uses_its_own_token_and_never_overrides_other_names(self):
        out, line, calls = self.run_case(
            {
                "MA_ROUTINE_KEY": "own" * 15,
                "GB_FIXER_ROUTINE_KEY": "fixer" * 10,
                "JET_ROUTINE_KEY": "foreign" * 10,
                "PATH": "bad",
                "ZULIP_MA_EMAIL": "foreign@example.test",
                "GB_TRADER_ROUTINE_KEY": "notheld" * 10,
            }
        )
        self.assertEqual(out["MA_ROUTINE_KEY"], "own" * 15)
        self.assertEqual(out["JET_ROUTINE_KEY"], "jet" * 15)
        self.assertEqual(out["ZULIP_MA_EMAIL"], "own@example.test")
        self.assertNotIn("GB_TRADER_ROUTINE_KEY", out)
        self.assertNotIn("PATH", out)
        self.assertNotIn(MODULE.TOKEN, out)
        self.assertNotIn(MODULE.LISTENER_READ_TOKEN, out)
        self.assertNotIn("own" * 15, line)

        primary_calls = [call for call in calls if call[1].get("token") == PRIMARY_TOKEN]
        secondary_calls = [call for call in calls if call[1].get("token") == SECONDARY_TOKEN]
        self.assertEqual(len(primary_calls), 2)
        self.assertEqual(len(secondary_calls), 1)
        secondary_env, secondary_kwargs = secondary_calls[0]
        self.assertEqual(secondary_env["INFISICAL_PROJECT_ID"], SECONDARY_PROJECT)
        self.assertEqual(secondary_env["INFISICAL_ENVIRONMENT"], "prod")
        self.assertEqual(secondary_kwargs["path"], "/zulip")
        self.assertEqual(secondary_kwargs["expected_paths"], ("/zulip",))
        self.assertEqual(primary_calls[0][1]["expected_paths"], ("/zulip", MODULE.OPERATOR_PATH))

    def test_failure_does_not_fall_back_to_primary_project_or_stale_value(self):
        out, line, calls = self.run_case(MODULE.LoadError("read listener keys: HTTP 403"))
        self.assertNotIn("MA_ROUTINE_KEY", out)
        self.assertEqual(out["JET_ROUTINE_KEY"], "jet" * 15)
        self.assertIn("not loaded", line)
        self.assertTrue(any(call[1].get("token") == SECONDARY_TOKEN for call in calls))

    def test_missing_secondary_token_never_reuses_primary_token(self):
        env = self.env()
        env.pop(MODULE.LISTENER_READ_TOKEN)
        with patch.object(MODULE, "fetch") as fetch:
            keys, note = MODULE.listener_read_keys(env, {"MA"})
        self.assertEqual(keys, {})
        self.assertIn("dedicated service token", note)
        fetch.assert_not_called()

    def test_missing_short_reference_keys_are_not_loaded(self):
        out, _, _ = self.run_case(
            {"MA_ROUTINE_KEY": "${foreign.secret}" * 4, "GB_FIXER_ROUTINE_KEY": "short"}
        )
        self.assertNotIn("MA_ROUTINE_KEY", out)
        self.assertNotIn("GB_FIXER_ROUTINE_KEY", out)

    def test_invalid_project_never_fetches(self):
        with patch.object(MODULE, "fetch") as fetch:
            keys, line = MODULE.listener_read_keys(
                {MODULE.LISTENER_READ_PROJECT: "bad", MODULE.LISTENER_READ_TOKEN: SECONDARY_TOKEN},
                {"MA"},
            )
        self.assertEqual(keys, {})
        self.assertIn("invalid project id", line)
        fetch.assert_not_called()

    def test_optout_unchanged(self):
        with patch.object(MODULE, "fetch") as fetch:
            self.assertEqual(MODULE.listener_read_keys({}, {"MA"}), ({}, ""))
        fetch.assert_not_called()

    def test_strict_secondary_listing_rejects_bad_rows_and_truncation(self):
        malformed = [
            {"secrets": [{"secretKey": "MA_ROUTINE_KEY", "secretValue": "a" * 40}] * 2},
            {"secrets": [], "totalCount": 1},
            {"secrets": [{"secretKey": "MA_ROUTINE_KEY", "secretValue": 2}]},
        ]
        valid_metadata = {
            "id": "synthetic-id",
            "name": "synthetic-reader",
            "projectId": SECONDARY_PROJECT,
            "workspace": SECONDARY_PROJECT,
            "scopes": [{"environment": "prod", "secretPath": "/zulip"}],
            "permissions": ["read"],
            "expiresAt": "2099-01-01T00:00:00Z",
        }
        for response in malformed:
            with self.subTest(response=response):
                def fake_request(stage, url, **kwargs):
                    if url.endswith("/v2/service-token"):
                        return valid_metadata
                    return response

                with patch.object(MODULE, "_request", side_effect=fake_request):
                    with self.assertRaises(MODULE.LoadError):
                        MODULE.fetch(
                            self.env(INFISICAL_PROJECT_ID=SECONDARY_PROJECT),
                            path="/zulip",
                            token=SECONDARY_TOKEN,
                            expected_paths=("/zulip",),
                            strict=True,
                        )

    def test_bad_secondary_grant_is_rejected_before_raw_read(self):
        env = self.env(INFISICAL_PROJECT_ID=SECONDARY_PROJECT)
        requests = []

        def fake_request(stage, url, **kwargs):
            requests.append(url)
            if url.endswith("/v2/service-token"):
                return {
                    "id": "synthetic-id",
                    "name": "synthetic-reader",
                    "projectId": SECONDARY_PROJECT,
                    "workspace": SECONDARY_PROJECT,
                    "scopes": [{"environment": "prod", "secretPath": "/"}],
                    "permissions": ["read", "write"],
                    "expiresAt": "2099-01-01T00:00:00Z",
                }
            return {"secrets": []}

        with patch.object(MODULE, "_request", side_effect=fake_request):
            with self.assertRaisesRegex(MODULE.LoadError, "folder scope did not match"):
                MODULE.fetch(
                    env, path="/zulip", token=SECONDARY_TOKEN, expected_paths=("/zulip",)
                )
        self.assertEqual(len(requests), 1)

    def test_no_eligible_server_seats_never_fetches_secondary(self):
        for codes in [set(), {"JET", "ECHO", "BF-ENGINEER"}]:
            with self.subTest(codes=codes), patch.object(MODULE, "fetch") as fetch:
                keys, line = MODULE.listener_read_keys(self.env(), codes)
                self.assertEqual(keys, {})
                self.assertIn("no eligible server seats", line)
                fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
