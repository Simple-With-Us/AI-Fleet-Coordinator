import subprocess
import unittest
from unittest.mock import patch

from agent_sync.operator_github_secrets import NamedSecrets, fleet_secrets_get
from agent_sync.github_token_router import RoutingError


class Tests(unittest.TestCase):
    def setUp(self):
        p = patch(
            "agent_sync.operator_github_secrets.fleet_secrets_bin",
            return_value="fleet-secrets",
        )
        p.start()
        self.addCleanup(p.stop)
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

    def test_default_transport_uses_fleet_secrets_get(self):
        class Done:
            returncode = 0
            stdout = b"v\n"

        with patch(
            "agent_sync.operator_github_secrets.subprocess.run", return_value=Done()
        ) as run:
            self.assertEqual(
                NamedSecrets(["CODEX"], []).caller_key("CODEX"), "v"
            )
            args = run.call_args[0][0]
            self.assertEqual(
                args, [
                    "fleet-secrets",
                    "get",
                    "OPERATOR_GITHUB_CALLER_CODEX_HMAC",
                    "--path",
                    "/operator-github/callers",
                    "--project",
                    "fleet-ops",
                ]
            )

    def test_default_transport_never_consults_environment(self):
        import os

        class Done:
            returncode = 0
            stdout = b"from-cli\n"

        with patch.dict(
            os.environ, {"OPERATOR_GITHUB_CALLER_CODEX_HMAC": "from-env"}
        ):
            with patch(
                "agent_sync.operator_github_secrets.subprocess.run", return_value=Done()
            ):
                self.assertEqual(
                    NamedSecrets(["CODEX"], []).caller_key("CODEX"), "from-cli"
                )

    def test_lookup_failures_name_variable_not_value(self):
        with patch(
            "agent_sync.operator_github_secrets.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["fleet-secrets"], 10),
        ):
            with self.assertRaises(RoutingError) as c:
                fleet_secrets_get("/operator-github/callers", "OPERATOR_GITHUB_CALLER_CODEX_HMAC")
        self.assertIn("OPERATOR_GITHUB_CALLER_CODEX_HMAC", str(c.exception))
        self.assertIn("timed out", str(c.exception))

        with patch(
            "agent_sync.operator_github_secrets.subprocess.run",
            side_effect=FileNotFoundError("fleet-secrets"),
        ):
            with self.assertRaises(RoutingError) as c:
                fleet_secrets_get("/operator-github/callers", "OPERATOR_GITHUB_CALLER_CODEX_HMAC")
        self.assertIn("not found", str(c.exception))


class LocalFailureOutcomeTests(unittest.TestCase):
    def test_local_lookup_failures_are_refused_so_the_nonce_is_released(self):
        with patch(
            "agent_sync.operator_github_secrets.fleet_secrets_bin",
            return_value="fleet-secrets",
        ):
            for effect, outcome in (
                (subprocess.TimeoutExpired(["fleet-secrets"], 10), "retryable"),
                (FileNotFoundError("fleet-secrets"), "refused"),
                (PermissionError("fleet-secrets"), "refused"),
            ):
                with patch(
                    "agent_sync.operator_github_secrets.subprocess.run", side_effect=effect
                ):
                    with self.assertRaises(RoutingError) as c:
                        fleet_secrets_get("/github-apps", "GH_APP_SWU_EXAMPLE_ID")
                self.assertEqual(c.exception.outcome, outcome)
