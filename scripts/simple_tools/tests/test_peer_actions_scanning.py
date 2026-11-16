"""Registry fields and stdin scanner refusals never echo or persist credential text."""

import contextlib
import io
import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest import mock

from simple_tools import peer_actions, peer_actions_cli, secretscan


class PeerActionScanTests(unittest.TestCase):
    def test_credential_shaped_compact_body_is_refused_at_build_time(self):
        # Assembled at runtime; never a credential.  A bare 32-character
        # mixed-case alphanumeric run is Zulip-shaped to the scanner, so
        # build_entry must refuse it before any row exists.
        shaped = "Ab1" * 10 + "Ab"
        with self.assertRaises(peer_actions.PeerActionsError) as caught:
            peer_actions.build_entry(**dict(self.fields, request_source=shaped))
        self.assertEqual(caught.exception.code, "refused_secret")
        self.assertNotIn(shaped, caught.exception.message)


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
        for value, expected in ((b"message 123", 0), (b"", 2), (b"\xff", 2), (b"x" * (256 * 1024 + 1), 2)):
            with self.subTest(expected=expected):
                out = io.StringIO()
                self.assertEqual(secretscan.main([], stdin=io.BytesIO(value), stdout=out), expected)
                self.assertLess(len(out.getvalue()), 200)

    def test_neutral_cli_does_not_reflect_an_untrusted_field_name(self):
        out = io.StringIO()
        self.assertEqual(secretscan.main(["--field", self.unsafe], stdin=io.BytesIO(), stdout=out), 2)
        self.assertNotIn(self.unsafe, out.getvalue())

    def test_transport_without_known_credentials_refuses_before_registry_work(self):
        from simple_tools.mcp.tools import Tools, ToolError

        context = SimpleNamespace(seat="CODEX")
        with mock.patch.object(peer_actions, "log_row") as append:
            with self.assertRaises(ToolError) as caught:
                Tools._tool_peer_actions_log(context, {})
        append.assert_not_called()
        self.assertEqual(caught.exception.code, "not_configured")
        self.assertIn("Tools.known", caught.exception.message)

    def test_compact_private_key_body_is_refused_for_local_and_fetched_credentials(self):
        # Deterministic synthetic PEM assembled at runtime; never a credential.
        # A freshly generated key made this test flaky:  the random base64 body
        # sometimes held a bare 32-character alphanumeric run matching the
        # scanner's Zulip-shaped API-key pattern, so build_entry refused before
        # the log_row rejection under test.  Fixed bytes keep both the fixture
        # and the covered rejection path deterministic.
        # Marker lines are assembled at runtime, like self.unsafe:  a literal
        # PEM block in the repo would trip the scanner on the fixture itself.
        dashes = "-" * 5
        begin = dashes + "BEGIN " + "PRIVATE KEY" + dashes
        end = dashes + "END " + "PRIVATE KEY" + dashes
        body = base64.b64encode(bytes((i * 7 + 13) % 256 for i in range(48))).decode("ascii")
        key = begin + "\n" + body + "\n" + end + "\n"
        compact = "".join(line for line in key.splitlines() if not line.startswith(dashes))
        # The refusal under test happens at log_row (the fetched SA JSON carries
        # the full PEM);  the compact body alone must stay below the scanner bar.
        self.assertIsNone(secretscan.scan(compact))
        raw = json.dumps({"private_key": key})
        entry = peer_actions.build_entry(**dict(self.fields, request_source=compact))
        for dry_run in (True, False):
            with self.subTest(dry_run=dry_run), mock.patch.object(peer_actions, "_http_opener") as network:
                with self.assertRaises(peer_actions.PeerActionsError) as caught:
                    peer_actions.log_row(entry, dry_run=dry_run, env={peer_actions.ENV_SA_JSON: raw})
                network.assert_not_called()
                self.assertEqual(caught.exception.code, "refused_secret")
                self.assertIn("request", caught.exception.message)
                self.assertNotIn(compact, caught.exception.message)
        env = {peer_actions.ENV_INFISICAL_TOKEN: "synthetic own reader", peer_actions.ENV_INFISICAL_PROJECT: "project-test"}
        with mock.patch.object(peer_actions, "_infisical_get_secret", return_value=raw) as read:
            with mock.patch.object(peer_actions, "_google_access_token") as oauth:
                with mock.patch.object(peer_actions, "SheetsClient") as sheets:
                    with self.assertRaises(peer_actions.PeerActionsError) as caught:
                        peer_actions.log_row(entry, env=env)
        read.assert_called_once()
        oauth.assert_not_called()
        sheets.assert_not_called()
        self.assertEqual(caught.exception.code, "refused_secret")
        self.assertNotIn(compact, caught.exception.message)

    def test_offline_dry_run_needs_no_alternative_auth_configuration(self):
        entry = peer_actions.build_entry(**self.fields)
        with mock.patch.object(peer_actions, "_http_opener") as network:
            result = peer_actions.log_row(entry, dry_run=True, env={})
        network.assert_not_called()
        self.assertTrue(result["dry_run"])

    def test_file_resolved_token_is_scanned_without_fetching_for_dry_run(self):
        known = "synthetic resolved bootstrap"
        entry = peer_actions.build_entry(**dict(self.fields, request_source=known))
        with tempfile.TemporaryDirectory() as directory:
            token_file = Path(directory) / "reader.token"
            token_file.write_text(known)
            token_file.chmod(0o600)
            with mock.patch.object(peer_actions, "_http_opener") as network:
                with self.assertRaises(peer_actions.PeerActionsError) as caught:
                    peer_actions.log_row(entry, dry_run=True, env={peer_actions.ENV_INFISICAL_TOKEN_FILE: str(token_file)})
        network.assert_not_called()
        self.assertEqual(caught.exception.code, "refused_secret")
        self.assertNotIn(known, caught.exception.message)

    def test_direct_service_account_sensitive_fields_are_scanned_but_metadata_is_not(self):
        known = "synthetic private value"
        env = {peer_actions.ENV_SA_JSON: json.dumps({"type": "service_account", "private_key": known})}
        entry = peer_actions.build_entry(**dict(self.fields, request_source=known))
        with self.assertRaises(peer_actions.PeerActionsError) as caught:
            peer_actions.log_row(entry, dry_run=True, env=env)
        self.assertEqual(caught.exception.code, "refused_secret")
        metadata = peer_actions.build_entry(**dict(self.fields, request_source="reviewed service_account metadata"))
        with mock.patch.object(peer_actions, "_http_opener") as network:
            self.assertTrue(peer_actions.log_row(metadata, dry_run=True, env=env)["dry_run"])
        network.assert_not_called()

    def test_fetched_service_account_refuses_before_oauth_or_append(self):
        known = "synthetic fetched private value"
        env = {peer_actions.ENV_INFISICAL_TOKEN: "synthetic operation bootstrap", peer_actions.ENV_INFISICAL_PROJECT: "project-test"}
        entry = peer_actions.build_entry(**dict(self.fields, request_source=known))
        with mock.patch.object(peer_actions, "_infisical_get_secret", return_value=json.dumps({"private_key": known})):
            with mock.patch.object(peer_actions, "_google_access_token") as oauth:
                with mock.patch.object(peer_actions, "SheetsClient") as sheets:
                    with self.assertRaises(peer_actions.PeerActionsError) as caught:
                        peer_actions.log_row(entry, env=env)
        oauth.assert_not_called()
        sheets.assert_not_called()
        self.assertEqual(caught.exception.code, "refused_secret")
        self.assertNotIn(known, caught.exception.message)

    def test_loader_snapshots_source_before_collecting_the_selected_binding(self):
        bootstrap = "synthetic original binding"
        source = {peer_actions.ENV_INFISICAL_TOKEN: bootstrap, peer_actions.ENV_INFISICAL_PROJECT: "project-test"}
        resolve = peer_actions.local_known_credentials

        def change_source_then_resolve(snapshot):
            source[peer_actions.ENV_SA_JSON] = json.dumps({"private_key": "synthetic other credential"})
            return resolve(snapshot)

        with mock.patch.object(peer_actions, "local_known_credentials", side_effect=change_source_then_resolve):
            with mock.patch.object(peer_actions, "_infisical_get_secret", return_value="{}") as fetch:
                self.assertEqual(peer_actions.load_service_account_json(source), {})
        self.assertEqual(fetch.call_args.args[0], bootstrap)

    def test_derived_oauth_credential_refuses_before_the_row_network_sink(self):
        known = "synthetic oauth credential"
        env = {peer_actions.ENV_SA_JSON: json.dumps({"private_key": "synthetic private value", "client_email": "example@example.test"})}
        entry = peer_actions.build_entry(**dict(self.fields, request_source=known))
        opener = mock.Mock()
        with mock.patch.object(peer_actions, "_google_access_token", return_value=known):
            with mock.patch.object(peer_actions, "_http_opener", return_value=opener):
                with self.assertRaises(peer_actions.PeerActionsError) as caught:
                    peer_actions.log_row(entry, env=env)
        opener.open.assert_not_called()
        self.assertEqual(caught.exception.code, "refused_secret")
        self.assertNotIn(known, caught.exception.message)

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
