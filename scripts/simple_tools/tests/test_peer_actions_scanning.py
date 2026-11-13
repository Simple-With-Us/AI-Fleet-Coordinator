"""Registry fields and stdin scanner refusals never echo or persist credential text."""

import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

from simple_tools import peer_actions, peer_actions_cli, secretscan


class PeerActionScanTests(unittest.TestCase):
    def setUp(self):
        # Synthetic scanner fixture assembled at runtime; never a credential.
        self.unsafe = "gh" + "p_" + "A" * 40
        self.fields = dict(acting_seat="CODEX", requesting_seat="CLAUDE",
                           request_source="message 123", action_taken="reviewed code", result="complete")

    def test_each_peer_text_field_is_refused_before_building_a_row(self):
        for field, label in (("request_source", "request"), ("action_taken", "action_taken"),
                             ("result", "result")):
            with self.subTest(field=field):
                fields = dict(self.fields, **{field: self.unsafe})
                with self.assertRaises(peer_actions.PeerActionsError) as caught:
                    peer_actions.build_entry(**fields)
                self.assertEqual(caught.exception.code, "refused_secret")
                self.assertIn(label, caught.exception.message)
                self.assertNotIn(self.unsafe, caught.exception.message)
                self.assertNotIn("gh" + "p_", caught.exception.message)

    def test_loaded_credentials_are_refused_in_every_builder_field(self):
        known = "synthetic loaded credential"
        for field in ("request_source", "action_taken", "result"):
            with self.subTest(field=field):
                fields = dict(self.fields, **{field: known})
                with self.assertRaises(peer_actions.PeerActionsError) as caught:
                    peer_actions.build_entry(**fields, known=[known])
                self.assertEqual(caught.exception.code, "refused_secret")
                self.assertNotIn(known, caught.exception.message)

    def test_stdio_passes_its_loaded_credentials_before_any_registry_call(self):
        from simple_tools.mcp.tools import Tools

        known = "synthetic loaded credential"
        context = SimpleNamespace(seat="CODEX", known=[known])
        args = dict(requesting_seat="CLAUDE", request=known, action_taken="reviewed code", result="complete")
        with mock.patch.object(peer_actions, "log_row") as append:
            with self.assertRaises(peer_actions.PeerActionsError) as caught:
                Tools._tool_peer_actions_log(context, args)
        append.assert_not_called()
        self.assertEqual(caught.exception.code, "refused_secret")
        self.assertNotIn(known, caught.exception.message)

    def test_cli_refuses_its_loaded_binding_before_dry_run_output(self):
        known = "synthetic loaded credential"
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {peer_actions.ENV_INFISICAL_TOKEN: known}):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                result = peer_actions_cli.main([
                    "log", "--acting", "CODEX", "--requesting", "CLAUDE", "--request", known,
                    "--action", "reviewed code", "--result", "complete", "--dry-run", "--json",
                ])
        self.assertEqual(result, 5)
        self.assertNotIn(known, out.getvalue() + err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["error"], "refused_secret")

    def test_missing_and_closed_stdin_return_safe_invalid_input(self):
        closed = io.BytesIO()
        closed.close()
        for stream in (None, closed):
            with self.subTest(stream=stream):
                out = io.StringIO()
                with mock.patch.object(sys, "stdin", stream):
                    result = secretscan.main([], stdout=out)
                self.assertEqual(result, 2)
                self.assertEqual(json.loads(out.getvalue())["code"], "invalid_input")

    def test_cli_dry_run_cannot_print_an_unscanned_row(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(peer_actions, "log_row") as append:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                result = peer_actions_cli.main([
                    "log", "--acting", "CODEX", "--requesting", "CLAUDE", "--request", self.unsafe,
                    "--action", "reviewed code", "--result", "complete", "--dry-run", "--json",
                ])
        append.assert_not_called()
        self.assertEqual(result, 5)
        self.assertNotIn(self.unsafe, out.getvalue() + err.getvalue())
        self.assertEqual(json.loads(out.getvalue())["error"], "refused_secret")

    def test_neutral_cli_reports_only_identifier_and_kind(self):
        out = io.StringIO()
        result = secretscan.main(["--field", "request"], stdin=io.BytesIO(self.unsafe.encode()), stdout=out)
        self.assertEqual(result, 1)
        answer = json.loads(out.getvalue())
        self.assertEqual((answer["field"], answer["code"]), ("request", "refused_secret"))
        self.assertNotIn(self.unsafe, out.getvalue())

    def test_shipped_module_is_an_isolated_stdin_command_without_package_setup(self):
        entry = Path(__file__).resolve().parents[1] / "secretscan.py"
        result = subprocess.run(
            [sys.executable, "-I", str(entry), "--field", "request"],
            input=self.unsafe.encode(), capture_output=True, timeout=5,
            env={"PATH": os.defpath},
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["code"], "refused_secret")
        self.assertNotIn(self.unsafe.encode(), result.stdout + result.stderr)

    def test_neutral_cli_safe_text_and_invalid_input_are_bounded(self):
        for value, expected in ((b"message 123", 0), (b"\xff", 2), (b"x" * (256 * 1024 + 1), 2)):
            with self.subTest(expected=expected):
                out = io.StringIO()
                self.assertEqual(secretscan.main([], stdin=io.BytesIO(value), stdout=out), expected)
                self.assertLess(len(out.getvalue()), 200)

    def test_neutral_cli_does_not_reflect_an_untrusted_field_name(self):
        out = io.StringIO()
        self.assertEqual(secretscan.main(["--field", self.unsafe], stdin=io.BytesIO(), stdout=out), 2)
        self.assertNotIn(self.unsafe, out.getvalue())

    def test_neutral_cli_argument_errors_do_not_echo_credentials(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(secretscan.main([self.unsafe], stdin=io.BytesIO(), stdout=out), 2)
        self.assertEqual(json.loads(out.getvalue())["code"], "invalid_arguments")
        self.assertNotIn(self.unsafe, out.getvalue() + err.getvalue())

    def test_neutral_cli_input_failure_does_not_echo_exception_text(self):
        stream = mock.Mock()
        stream.read.side_effect = OSError(self.unsafe)
        out = io.StringIO()
        self.assertEqual(secretscan.main([], stdin=stream, stdout=out), 2)
        self.assertEqual(json.loads(out.getvalue())["code"], "invalid_input")
        self.assertNotIn(self.unsafe, out.getvalue())


if __name__ == "__main__":
    unittest.main()
