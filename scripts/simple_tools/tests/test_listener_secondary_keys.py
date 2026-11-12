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
WAKE_TOKEN = "st.wake-router-token-fixture-value"
OPERATOR_VALUES = {"OPERATOR_ZULIP_USER_ID": "4242", "OPERATOR_ZULIP_EMAIL": "operator@example.test"}
WAKE_VALUES = {"SIMPLE_TOOLS_OPERATOR_REGISTRY_KEY": "r" * 40, "WAKE_ROUTER_KEY": "w" * 40}
JET_PAIR = {"JET_ROUTINE_URL": "https://jet.example.test/wake/JET", "JET_ROUTINE_KEY": "jet" * 15}


class ListenerSecondaryKeyTests(unittest.TestCase):
    def env(self, **overrides):
        values = {
            "INFISICAL_TOKEN": PRIMARY_TOKEN,
            "INFISICAL_PROJECT_ID": PRIMARY_PROJECT,
            MODULE.LISTENER_READ_PROJECT: SECONDARY_PROJECT,
            MODULE.LISTENER_READ_TOKEN: SECONDARY_TOKEN,
            MODULE.WAKE_ROUTER_TOKEN: WAKE_TOKEN,
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
            if kwargs.get("path") == MODULE.WAKE_ROUTER_PATH:
                return dict(WAKE_VALUES)
            if kwargs.get("path") == MODULE.OPERATOR_PATH:
                return dict(OPERATOR_VALUES)
            return {
                "ZULIP_MA_EMAIL": "own@example.test",
                "MA_ROUTINE_KEY": "primary" * 12,
                **JET_PAIR,
                "PATH": "untrusted",
            }

        with (
            patch.object(MODULE, "server_codes", return_value={"MA", "GB_FIXER", "JET"}),
            patch.object(MODULE, "fetch", side_effect=fetch),
        ):
            out, line = MODULE.build_env(self.env())
        return out, line, calls

    def test_secondary_uses_its_own_token_and_supplies_only_routine_keys(self):
        out, line, calls = self.run_case(
            {
                "GB_FIXER_ROUTINE_KEY": "fixer" * 10,
                "PATH": "bad",
                "ZULIP_MA_EMAIL": "foreign@example.test",
                "GB_TRADER_ROUTINE_KEY": "notheld" * 10,
            }
        )
        self.assertEqual(out["GB_FIXER_ROUTINE_KEY"], "fixer" * 10)
        self.assertEqual(out["MA_ROUTINE_KEY"], "primary" * 12, "the container's stale copy is never used")
        self.assertEqual(out["JET_ROUTINE_KEY"], "jet" * 15)
        self.assertEqual(out["ZULIP_MA_EMAIL"], "own@example.test")
        self.assertNotIn("GB_TRADER_ROUTINE_KEY", out)
        self.assertNotIn("PATH", out)
        self.assertNotIn(MODULE.TOKEN, out)
        self.assertNotIn(MODULE.LISTENER_READ_TOKEN, out)
        self.assertNotIn("fixer" * 10, line)

        primary_calls = [call for call in calls if call[1].get("token") == PRIMARY_TOKEN]
        secondary_calls = [call for call in calls if call[1].get("token") == SECONDARY_TOKEN]
        self.assertEqual(len(primary_calls), 2)
        self.assertEqual(len(secondary_calls), 1)
        secondary_env, secondary_kwargs = secondary_calls[0]
        self.assertEqual(secondary_env["INFISICAL_PROJECT_ID"], SECONDARY_PROJECT)
        self.assertEqual(secondary_kwargs["path"], "/zulip")
        self.assertEqual(secondary_kwargs["expected_paths"], ("/zulip",))
        self.assertEqual(secondary_kwargs["token_name"], MODULE.LISTENER_READ_TOKEN)
        self.assertEqual(primary_calls[0][1]["expected_paths"], ("/zulip", MODULE.OPERATOR_PATH))

    def test_the_same_rule_holds_for_every_seat_with_no_seat_list(self):
        self.assertFalse(hasattr(MODULE, "LISTENER_READ_CODES"))
        self.assertEqual(
            MODULE._secondary_names({"JET", "CLAUDE", "GB_FIXER"}),
            {"JET_ROUTINE_KEY", "CLAUDE_ROUTINE_KEY", "GB_FIXER_ROUTINE_KEY"},
        )

    def test_a_key_held_by_both_projects_is_loaded_from_neither_and_named(self):
        out, line, _ = self.run_case({"MA_ROUTINE_KEY": "own" * 15, "JET_ROUTINE_KEY": "foreign" * 10})
        self.assertNotIn("MA_ROUTINE_KEY", out)
        self.assertNotIn("JET_ROUTINE_KEY", out)
        self.assertIn("held by both projects, so not loaded: JET_ROUTINE_KEY, MA_ROUTINE_KEY", line)
        for value in ("own" * 15, "foreign" * 10, "primary" * 12, "jet" * 15):
            self.assertNotIn(value, line)

    def test_zc_key_comes_from_the_secondary_project_and_its_url_from_the_primary(self):
        zc_url = "https://example.test:8791/wake/ZC"
        calls = []

        def fetch(env, **kwargs):
            calls.append(kwargs.copy())
            if kwargs.get("token") == SECONDARY_TOKEN:
                return {
                    "ZC_ROUTINE_KEY": "fleet-ops-zc" * 4,
                    "ZC_ROUTINE_URL": "https://elsewhere.invalid/wake/ZC",
                    "ZULIP_ZC_EMAIL": "foreign@example.test",
                }
            if kwargs.get("path") == MODULE.WAKE_ROUTER_PATH:
                return dict(WAKE_VALUES)
            if kwargs.get("path") == MODULE.OPERATOR_PATH:
                return dict(OPERATOR_VALUES)
            return {
                **JET_PAIR,
                "ZULIP_ZC_EMAIL": "zc@example.test",
                "ZC_ROUTINE_URL": zc_url,
            }

        with (
            patch.object(MODULE, "server_codes", return_value={"ZC"}),
            patch.object(MODULE, "fetch", side_effect=fetch),
        ):
            out, line = MODULE.build_env(self.env())
        # The key is the secondary project's (its only home);  the URL is the primary's (the secondary read
        # carries _ROUTINE_KEY names only);  a foreign Zulip name is ignored.
        self.assertEqual(out["ZC_ROUTINE_KEY"], "fleet-ops-zc" * 4)
        self.assertEqual(out["ZC_ROUTINE_URL"], zc_url)
        self.assertEqual(out["ZULIP_ZC_EMAIL"], "zc@example.test")
        self.assertNotIn("fleet-ops-zc", line)
        self.assertEqual(sum(1 for call in calls if call.get("token") == SECONDARY_TOKEN), 1)

    def test_a_failed_secondary_read_stops_startup(self):
        with self.assertRaisesRegex(MODULE.LoadError, "read listener keys: HTTP 403"):
            self.run_case(MODULE.LoadError("read listener keys: HTTP 403"))

    def test_missing_secondary_token_never_reuses_primary_token_and_stops_startup(self):
        env = self.env()
        env.pop(MODULE.LISTENER_READ_TOKEN)
        with patch.object(MODULE, "fetch") as fetch:
            with self.assertRaisesRegex(MODULE.LoadError, "INFISICAL_LISTENER_READ_TOKEN is missing or invalid"):
                MODULE.listener_read_keys(env, {"MA"})
        fetch.assert_not_called()

    def test_short_keys_are_not_loaded_and_are_named(self):
        out, line, _ = self.run_case({"GB_FIXER_ROUTINE_KEY": "tiny"})
        self.assertNotIn("GB_FIXER_ROUTINE_KEY", out)
        self.assertIn("not usable, so not loaded: GB_FIXER_ROUTINE_KEY", line)
        self.assertNotIn("tiny", line)

    def test_invalid_project_never_fetches_and_stops_startup(self):
        with patch.object(MODULE, "fetch") as fetch:
            with self.assertRaisesRegex(MODULE.LoadError, "INFISICAL_LISTENER_READ_PROJECT_ID is invalid"):
                MODULE.listener_read_keys(
                    {MODULE.LISTENER_READ_PROJECT: "bad", MODULE.LISTENER_READ_TOKEN: SECONDARY_TOKEN},
                    {"MA"},
                )
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

if __name__ == "__main__":
    unittest.main()
