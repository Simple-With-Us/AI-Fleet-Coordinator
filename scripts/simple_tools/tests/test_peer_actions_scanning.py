"""Registry fields and stdin scanner refusals never echo or persist credential text."""

import contextlib
import io
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from simple_tools import peer_actions, peer_actions_cli, secretscan


class PeerActionScanTests(unittest.TestCase):
    def setUp(self):
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
