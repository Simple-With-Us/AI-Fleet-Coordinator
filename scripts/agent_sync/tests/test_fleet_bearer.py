"""No real credentials or live services: shared identity client contract."""
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from agent_sync import fleet_bearer as F
from agent_sync import notes_client as NC


class FleetBearerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.path = self.home / "bearer"
        self.token = "sbt_codex_" + "x" * 43
        self.path.write_text(self.token)
        self.path.chmod(0o600)

    def tearDown(self):
        self.tmp.cleanup()

    def test_single_line_private_file_and_explicit_seat_binding(self):
        self.assertEqual(F.private_bearer(self.path), ("CODEX", self.token))
        self.path.write_text(self.token + "\n")
        self.assertEqual(F.Client(self.path).seat, "CODEX")
        with self.assertRaises(F.FleetError):
            F.Client(self.path, "CLUTCH")

    def test_rejects_unsafe_modes_links_and_malformed_content(self):
        for mode in (0o644, 0o660):
            self.path.chmod(mode)
            with self.assertRaises(F.FleetError):
                F.private_bearer(self.path)
        self.path.chmod(0o600)
        link = self.home / "link"
        link.symlink_to(self.path)
        with self.assertRaises(F.FleetError):
            F.private_bearer(link)
        link.unlink()
        os.link(self.path, link)
        with self.assertRaises(F.FleetError):
            F.private_bearer(self.path)
        link.unlink()
        for value in (self.token + "\n\n", "TOKEN=" + self.token, "{}", "fla_" + "x" * 64):
            self.path.write_text(value)
            with self.assertRaises(F.FleetError) as exc:
                F.private_bearer(self.path)
            self.assertNotIn(value, str(exc.exception))

    def test_symlink_and_writable_ancestor_refused(self):
        directory = self.home / "dir"
        directory.mkdir()
        file = directory / "token"
        file.write_text(self.token)
        file.chmod(0o600)
        directory.chmod(0o777)
        with self.assertRaises(F.FleetError):
            F.private_bearer(file)
        directory.chmod(0o700)
        link = self.home / "alias"
        link.symlink_to(directory, target_is_directory=True)
        with self.assertRaises(F.FleetError):
            F.private_bearer(link / "token")

    def test_gateway_whoami_is_authoritative_before_mutation(self):
        client = F.Client(self.path)
        with patch.object(client, "rpc", return_value={"structuredContent": {"seat": "CLUTCH", "authenticated_seat": "CLUTCH"}}) as rpc:
            with self.assertRaises(F.FleetError):
                client.call("notes_create", {}, True)
            self.assertEqual(rpc.call_count, 1)
        with patch.object(client, "rpc", side_effect=[{"structuredContent": {"seat": "CODEX", "authenticated_seat": "CODEX"}}, {"structuredContent": {"ok": True}}]) as rpc:
            self.assertTrue(client.call("notes_create", {}, True)["structuredContent"]["ok"])
            self.assertEqual(rpc.call_count, 2)

    def test_request_containing_token_refused_before_network(self):
        client = F.Client(self.path)
        with patch.object(F.urllib.request, "build_opener") as opener:
            with self.assertRaises(F.FleetError):
                client.rpc("tools/call", {"title": self.token}, True)
            opener.assert_not_called()

    def test_nonsecret_launcher_identity_can_only_restrict(self):
        for env in ({"AGENT_LAUNCHER": "botfleet"}, {"AGENT_LAUNCH_SEAT": "CLUTCH"}, {"AGENT_SEAT": "CLUTCH"}):
            with self.assertRaises(F.FleetError):
                F.Client(self.path, identity_env=env)
        self.assertEqual(F.Client(self.path, identity_env={"AGENT_LAUNCHER": "botfleet", "AGENT_LAUNCH_SEAT": "CODEX"}).seat, "CODEX")

    def test_deep_json_refused(self):
        with self.assertRaises(ValueError):
            F.bounded_json("[" * 100 + "0" + "]" * 100)

    def test_notes_poll_preserves_request_and_does_not_enqueue_twice(self):
        host = NC.Host({}, self.home, None, caller="cli", bearer_file=str(self.path))
        request = {"action": "pin", "request_id": "r1", "identity": "CODEX", "title": "title"}
        with patch.object(NC, "_fleet_tool", side_effect=[{"status": "queued", "request_id": "r1", "action": "pin"}, {"status": "done", "request_id": "r1", "action": "pin"}]) as tool, patch.object(NC.time, "sleep"):
            result = NC._fleet_call(host, "CODEX", request, 300)
            self.assertEqual(result["status"], "done")
            self.assertEqual(tool.call_args_list[0].args[2], "notes_pin")
            self.assertEqual(tool.call_args_list[1].args[2], "notes_status")
            self.assertEqual(tool.call_args_list[1].args[3], {"request_id": "r1"})

    def test_notes_mismatched_missing_and_final_states_keep_exact_recovery_id(self):
        from agent_sync import notes as N
        host = NC.Host({}, self.home, None, caller="cli", bearer_file=str(self.path))
        request = {"action": "pin", "request_id": "original-id", "identity": "CODEX", "title": "title"}
        for out in ({"status": "done", "action": "unpin", "request_id": "original-id"}, {"status": "done", "action": "pin", "request_id": "other"}, {"status": "done", "request_id": "original-id"}, {"status": "refused", "request_id": "original-id", "action": "pin", "error": {"code": "forbidden"}}, {"status": "expired", "request_id": "original-id", "action": "pin"}):
            with patch.object(NC, "_fleet_tool", return_value=out):
                with self.assertRaises(N.NotesError) as exc:
                    NC._fleet_call(host, "CODEX", request, 0)
                self.assertIn("original-id", str(exc.exception))
                if out["status"] == "refused":
                    self.assertEqual(exc.exception.code, "forbidden")
                if out["status"] == "expired":
                    self.assertEqual(exc.exception.code, "unavailable")
        with patch.object(NC, "_fleet_tool", side_effect=[{"status": "queued", "request_id": "original-id", "action": "pin"}, N.NotesError("unavailable", "synthetic")]), patch.object(NC.time, "sleep"):
            out = NC._fleet_call(host, "CODEX", request, 300)
            self.assertEqual(out["request_id"], "original-id")
            self.assertEqual(out["status"], "queued")
            self.assertIn("original-id", out["poll_warning"])


    def test_empty_bearer_path_notes_prefix_and_verb_refuse_before_legacy(self):
        from agent_sync import cli, notes_cli, zulip
        from types import SimpleNamespace
        for argv in (["--fleet-bearer-file", "", "list"], ["list", "--fleet-bearer-file", ""]):
            args = cli.build_parser_for("simple-notes").parse_args(argv)
            self.assertEqual(args.fleet_bearer_file, "")
            with patch.object(NC, "operator_client") as direct, patch.object(NC.N, "direct_available") as script:
                with self.assertRaises(zulip.CredentialError):
                    notes_cli._cmd_notes(SimpleNamespace(env={}, home=self.home), args, {})
                direct.assert_not_called()
                script.assert_not_called()
        with self.assertRaises(F.FleetError):
            F.Client("")

    def test_notes_read_list_completeness_and_expiry(self):
        from agent_sync import notes as N
        host = NC.Host({}, self.home, None, caller="cli", bearer_file=str(self.path))
        for action, data in (("read", {}), ("read", {"result_expired": True}), ("list", {"notes": []}), ("list", {"notes": [], "total": 0}), ("list", {"total": 0, "truncated": False})):
            out = {"request_id": "original-id", "action": action, "status": "done", **data}
            request = {"action": action, "request_id": "original-id", "identity": "CODEX"}
            with patch.object(NC, "_fleet_tool", return_value=out):
                with self.assertRaises(N.NotesError) as exc:
                    NC._fleet_call(host, "CODEX", request, 0)
                self.assertIn("original-id", str(exc.exception))
                if data.get("result_expired"):
                    self.assertEqual(exc.exception.code, "result_expired")
        request = {"action": "list", "request_id": "original-id", "identity": "CODEX"}
        full = {"request_id": "original-id", "action": "list", "status": "done", "notes": [], "total": 17, "truncated": True}
        with patch.object(NC, "_fleet_tool", side_effect=[{"request_id": "original-id", "status": "done"}, full]):
            self.assertEqual(NC._fleet_call(host, "CODEX", request, 0), full)


    def test_explicit_fleet_mode_cannot_start_backend_runner(self):
        from agent_sync import cli, notes_cli, notes_runner, zulip
        from types import SimpleNamespace
        for value in (str(self.path), "", "malformed"):
            args = cli.build_parser_for("simple-notes").parse_args(["--fleet-bearer-file", value, "runner", "--once"])
            with patch.object(notes_runner, "runner_for") as runner, patch.object(notes_cli, "_cmd_runner") as dispatch:
                with self.assertRaises(zulip.UsageError):
                    notes_cli.cmd_notes(SimpleNamespace(env={}, home=self.home), args)
                dispatch.assert_not_called()
                runner.assert_not_called()
        args = cli.build_parser_for("simple-notes").parse_args(["runner", "--once"])
        with patch.object(notes_cli, "_cmd_runner", return_value=0) as dispatch:
            self.assertEqual(notes_cli.cmd_notes(SimpleNamespace(env={}, home=self.home), args), 0)
            dispatch.assert_called_once()


    def test_all_notes_verbs_preserve_explicit_mode_in_both_positions(self):
        from agent_sync import cli, notes_cli, zulip
        from types import SimpleNamespace
        verbs = {"create": ["title"], "update": ["title"], "pin": ["title"], "unpin": ["title"], "read": ["title"], "list": [], "log": [], "status": ["original-id"]}
        for verb, rest in verbs.items():
            for value in (str(self.path), ""):
                for argv in (["--fleet-bearer-file", value, verb, *rest], [verb, *rest, "--fleet-bearer-file", value]):
                    args = cli.build_parser_for("simple-notes").parse_args(argv)
                    self.assertEqual(args.fleet_bearer_file, value, argv)
                    if value == "":
                        with patch.object(NC, "operator_client") as legacy, patch.object(NC.N, "direct_available") as script:
                            with self.assertRaises(zulip.CredentialError):
                                notes_cli.cmd_notes(SimpleNamespace(env={}, home=self.home), args)
                            legacy.assert_not_called()
                            script.assert_not_called()



class TransportTests(FleetBearerTests):
    def answer(self, data, content_type="application/json"):
        response = io.BytesIO(data)
        response.headers = {"Content-Type": content_type}
        return response

    def test_json_and_sse_and_header_only_bearer(self):
        reply = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": {"ok": True}}})
        for raw, content_type in ((reply.encode(), "application/json"), (('data: ' + reply + '\n\n').encode(), "text/event-stream")):
            with patch.object(F.urllib.request, "build_opener") as opener:
                opener.return_value.open.return_value = self.answer(raw, content_type)
                self.assertTrue(F.Client(self.path).rpc("tools/call", {"name": "notes_list", "arguments": {}})["structuredContent"]["ok"])
                request = opener.return_value.open.call_args.args[0]
                self.assertEqual(request.full_url, F.ENDPOINT)
                self.assertEqual(request.get_header("Authorization"), "Bearer " + self.token)
                self.assertNotIn(self.token.encode(), request.data)
                self.assertTrue(any(isinstance(handler, F.NoRedirect) for handler in opener.call_args.args))
                self.assertTrue(any(isinstance(handler, F.urllib.request.ProxyHandler) and handler.proxies == {} for handler in opener.call_args.args))

    def test_bad_oversize_and_reflected_token_answers_are_redacted_unknown_no_retry(self):
        for raw in (b"not JSON", b"x" * (F.MAX_BYTES + 1), json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": [{"text": self.token}]}}).encode(), b'{"jsonrpc":"2.0","id":1,"result":{"isError":"false"}}'):
            with patch.object(F.urllib.request, "build_opener") as opener:
                opener.return_value.open.return_value = self.answer(raw)
                with self.assertRaises(F.FleetError) as exc:
                    F.Client(self.path).rpc("tools/call", {}, True)
                self.assertEqual(opener.return_value.open.call_count, 1)
                self.assertNotIn(self.token, str(exc.exception))
                self.assertIn("outcome unknown", str(exc.exception))

    def test_auth_refusal_not_retried_and_http_exception_unknown(self):
        import http.client
        import urllib.error
        for error in (urllib.error.HTTPError(F.ENDPOINT, 401, "no", {}, None), http.client.IncompleteRead(b"partial")):
            with patch.object(F.urllib.request, "build_opener") as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(F.FleetError) as exc:
                    F.Client(self.path).rpc("tools/call", {}, True)
                self.assertEqual(opener.return_value.open.call_count, 1)
                self.assertNotIn("partial", str(exc.exception))
                if isinstance(error, urllib.error.HTTPError):
                    self.assertEqual(exc.exception.code, "not_authorized")
                else:
                    self.assertEqual(exc.exception.code, "outcome_unknown")

    def test_unicode_escaped_bearer_reflection_and_decoded_request_are_refused(self):
        escaped = "\\u0073" + self.token[1:]
        raw = ('{"jsonrpc":"2.0","id":1,"result":{"structuredContent":{"nested":["' + escaped + '"]}}}').encode()
        with patch.object(F.urllib.request, "build_opener") as opener:
            opener.return_value.open.return_value = self.answer(raw)
            with self.assertRaises(F.FleetError):
                F.Client(self.path).rpc("tools/call", {}, True)
        params = json.loads('{"nested":["' + escaped + '"]}')
        with patch.object(F.urllib.request, "build_opener") as opener:
            with self.assertRaises(F.FleetError):
                F.Client(self.path).rpc("tools/call", params, True)
            opener.assert_not_called()
