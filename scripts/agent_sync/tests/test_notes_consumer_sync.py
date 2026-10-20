"""scripts/agent-sync-mcp/sync_notes_consumer.py:  the NOTES_RELAY_CONSUMER knob goes from Infisical to the Worker.

Nothing here reaches Infisical, Cloudflare or wrangler:  the helpers of install_seat_key.py are replaced by fakes, and
the tests check that nothing is ever written to Infisical, that the value is validated against fleet-apps.json, and
that wrangler is called only with --apply and gets the value on standard input.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / "agent-sync-mcp" / "sync_notes_consumer.py"
spec = importlib.util.spec_from_file_location("sync_notes_consumer", SCRIPT)
SYNC = importlib.util.module_from_spec(spec)
spec.loader.exec_module(SYNC)


class FakeInstall:
    INFISICAL_API = "https://infisical.test"
    INFISICAL_PROJECT = "project-id"
    INFISICAL_ENV = "prod"

    def __init__(self, secrets, code=0):
        self.secrets = secrets
        self.requests = []
        self.wrangler_calls = []
        self.code = code

    def handoff(self, name):
        return "handoff-" + name.lower()

    def http_json(self, method, url, *, body=None, headers=None):
        self.requests.append((method, url))
        if url.endswith("/login"):
            return {"accessToken": "token"}
        return {"secrets": [{"secretKey": k, "secretValue": v} for k, v in self.secrets.items()]}

    def wrangler(self, args, stdin, scrub):
        self.wrangler_calls.append((args, stdin))
        return self.code


def run(install, *argv):
    out = io.StringIO()
    with mock.patch.object(SYNC, "_install", return_value=install), contextlib.redirect_stdout(out):
        code = SYNC.main(list(argv))
    return code, out.getvalue()


class SyncTests(unittest.TestCase):
    def test_check_mode_prints_the_value_and_writes_nothing(self):
        install = FakeInstall({"NOTES_RELAY_CONSUMER": "CLAUDE", "OTHER": "ignored"})
        code, out = run(install)
        self.assertEqual(code, 0)
        self.assertIn("NOTES_RELAY_CONSUMER=CLAUDE", out)
        self.assertEqual(install.wrangler_calls, [])
        self.assertEqual(
            {method for method, _ in install.requests},
            {"POST", "GET"},
            "login and one read, no write",
        )
        self.assertIn(
            "secretPath=%2F", install.requests[1][1], "the knob lives at the project root"
        )

    def test_apply_puts_the_value_on_stdin_to_the_binding_of_the_same_name(self):
        install = FakeInstall({"NOTES_RELAY_CONSUMER": " claude \n"})
        code, out = run(install, "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(
            install.wrangler_calls, [(["secret", "put", "NOTES_RELAY_CONSUMER"], "CLAUDE")]
        )
        self.assertIn("installed", out)

    def test_operator_is_explicit_service_not_roster(self):
        install = FakeInstall({"NOTES_RELAY_CONSUMER": "OPERATOR"})
        code, _ = run(install, "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(
            install.wrangler_calls, [(["secret", "put", "NOTES_RELAY_CONSUMER"], "OPERATOR")]
        )

    def test_a_wrangler_failure_is_the_exit_code(self):
        code, _ = run(FakeInstall({"NOTES_RELAY_CONSUMER": "CLAUDE"}, code=1), "--apply")
        self.assertEqual(code, 1)

    def test_a_missing_empty_or_bad_value_installs_nothing(self):
        for secrets, fragment in (
            ({}, "is not set in Infisical"),
            ({"NOTES_RELAY_CONSUMER": "  "}, "is not set"),
            ({"NOTES_RELAY_CONSUMER": "not a seat!"}, "not a seat tag"),
            ({"NOTES_RELAY_CONSUMER": "MONET"}, "not a live seat"),
            ({"NOTES_RELAY_CONSUMER": "NOBODY"}, "not a live seat"),
        ):
            with self.subTest(secrets=secrets):
                install = FakeInstall(secrets)
                with self.assertRaises(SystemExit) as caught:
                    run(install, "--apply")
                self.assertIn(fragment, str(caught.exception))
                self.assertEqual(install.wrangler_calls, [])

    def test_the_script_never_writes_to_infisical(self):
        source = SCRIPT.read_text()
        for verb in ('"PUT"', '"PATCH"', '"DELETE"'):
            self.assertNotIn(verb, source)
        self.assertEqual(source.count('"POST"'), 1, "the one POST is the login")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
