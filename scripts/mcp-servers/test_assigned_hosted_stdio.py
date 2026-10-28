"""Offline synthetic tests. No credentials, network, files or live writes."""

import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "adapter", Path(__file__).with_name("assigned-hosted-stdio.py")
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
TOKEN = "sbt_echo_" + "A" * 43


class Response(io.BytesIO):
    def __init__(self, data, status=200, kind="application/json", session=None):
        super().__init__(data)
        self.status = status
        self.headers = {"Content-Type": kind}
        if session:
            self.headers["Mcp-Session-Id"] = session


class AdapterTests(unittest.TestCase):
    def message(self, method="tools/call", identity="ECHO"):
        return {
            "jsonrpc": "2.0",
            "id": 7,
            "method": method,
            "params": {"name": "gr_merge_pr", "arguments": {"identity": identity}},
        }

    def reply(self, result=None):
        return {"jsonrpc": "2.0", "id": 7, "result": result or {"content": []}}

    def test_only_assigned_bearer_formats_no_generic_oauth(self):
        for invalid in ("", "oauth-token", TOKEN + "\nprivate", "gbb_role_short"):
            with self.assertRaises(mod.TransportFailure):
                mod.Adapter(invalid)
        self.assertEqual(mod.Adapter(TOKEN).identity, "ECHO")
        self.assertEqual(mod.Adapter("gbb_fixer_" + "B" * 43).identity, "GB-FIXER")

    def test_cross_seat_refused_before_transport(self):
        def never(*args, **kw):
            self.fail("cross-seat reached network")

        result = mod.Adapter(TOKEN, never).call(self.message(identity="WISE"))
        self.assertEqual(
            json.loads(result["result"]["content"][0]["text"])["code"], "identity_not_allowed"
        )

    def test_fixed_https_endpoint_bearer_only_and_transparent_tool_payload(self):
        expected = self.reply(
            {"isError": True, "structuredContent": {"outcome": "unknown"}, "content": []}
        )
        seen = []

        def opener(request, timeout):
            seen.append(request)
            self.assertEqual(timeout, 120)
            self.assertEqual(request.full_url, mod.ENDPOINT)
            self.assertEqual(request.headers["Authorization"], "Bearer " + TOKEN)
            self.assertFalse(any("cf-access" in k.lower() for k in request.headers))
            return Response(json.dumps(expected).encode())

        self.assertEqual(mod.Adapter(TOKEN, opener).call(self.message()), expected)
        self.assertEqual(len(seen), 1)

    def test_session_and_protocol_follow_initialize(self):
        calls = []

        def opener(request, timeout):
            calls.append(request)
            if len(calls) == 1:
                return Response(
                    json.dumps(self.reply({"protocolVersion": "2025-03-26"})).encode(),
                    session="session-1",
                )
            self.assertEqual(request.headers["Mcp-session-id"], "session-1")
            self.assertEqual(request.headers["Mcp-protocol-version"], "2025-03-26")
            return Response(json.dumps(self.reply()).encode())

        adapter = mod.Adapter(TOKEN, opener)
        adapter.call(self.message("initialize"))
        adapter.call(self.message("tools/list"))

    def test_sse_multiline_data_ignores_heartbeat_and_matches_id(self):
        raw = b': heartbeat\n\nevent: message\ndata: {"jsonrpc":"2.0",\ndata: "id":7,"result":{"tools":[{"name":"gr_merge_pr"}]}}\n\n'
        result = mod.Adapter(TOKEN, lambda *a, **kw: Response(raw, kind="text/event-stream")).call(
            self.message("tools/list")
        )
        self.assertEqual(result["result"]["tools"][0]["name"], "gr_merge_pr")

    def test_notifications_have_no_stdio_response(self):
        adapter = mod.Adapter(TOKEN, lambda *a, **kw: Response(b"", status=202))
        self.assertIsNone(adapter.call({"jsonrpc": "2.0", "method": "notifications/initialized"}))

    def test_http_transport_parse_loss_are_unknown_no_prose_no_retry(self):
        cases = [
            OSError(TOKEN),
            urllib.error.HTTPError("u", 403, TOKEN, {}, io.BytesIO(TOKEN.encode())),
            urllib.error.HTTPError("u", 409, "pending", {}, io.BytesIO(b"not-json")),
            urllib.error.HTTPError("u", 302, "redirect", {}, io.BytesIO(b"provider-prose")),
        ]
        for exc in cases:
            calls = []

            def opener(*a, calls=calls, exc=exc, **kw):
                calls.append(1)
                raise exc

            result = mod.Adapter(TOKEN, opener).call(self.message())
            self.assertEqual(
                json.loads(result["result"]["content"][0]["text"])["code"], "outcome_unknown"
            )
            self.assertNotIn(TOKEN, json.dumps(result))
            self.assertEqual(len(calls), 1)

    def test_malformed_oversized_secret_wrongid_or_duplicate_response_unknown(self):
        cases = [
            b"not-json",
            b"[]",
            b"x" * (mod.MAX_BYTES + 1),
            json.dumps(self.reply({"private": TOKEN})).encode(),
            b'{"jsonrpc":"2.0","id":8,"result":{}}',
        ]
        for raw in cases:
            result = mod.Adapter(TOKEN, lambda *a, raw=raw, **kw: Response(raw)).call(
                self.message()
            )
            self.assertTrue(result["result"]["isError"])
            self.assertNotIn(TOKEN, json.dumps(result))
        raw = b'data: {"jsonrpc":"2.0","id":7,"result":{}}\n\n' * 2
        result = mod.Adapter(TOKEN, lambda *a, **kw: Response(raw, kind="text/event-stream")).call(
            self.message()
        )
        self.assertTrue(result["result"]["isError"])

    def test_all_family_calls_receive_only_credential_assigned_identity(self):
        names = [
            "whoami",
            "zulip_inbox",
            "linear_whoami",
            "notes_list",
            "recall_stats",
            "fleetlink_whoami",
            "eavesdropper_status",
            "gr_open_pr",
            "gr_edit_pr",
            "gr_reply_thread",
            "gr_comment_pr",
            "gr_merge_pr",
            "gr_workflow_dispatch",
            "gr_resolve_thread",
        ]

        def opener(request, timeout):
            payload = json.loads(request.data)
            self.assertEqual(payload["params"]["arguments"]["identity"], "ECHO")
            return Response(json.dumps(self.reply()).encode())

        adapter = mod.Adapter(TOKEN, opener)
        for name in names:
            result = adapter.call(
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {"name": name, "arguments": {}},
                }
            )
            self.assertEqual(result, self.reply())

    def test_tool_catalog_preserved_with_all_seven_gr_tools(self):
        names = [
            "gr_open_pr",
            "gr_edit_pr",
            "gr_reply_thread",
            "gr_comment_pr",
            "gr_merge_pr",
            "gr_workflow_dispatch",
            "gr_resolve_thread",
        ]
        expected = self.reply(
            {"tools": [{"name": name, "inputSchema": {"type": "object"}} for name in names]}
        )
        result = mod.Adapter(TOKEN, lambda *a, **kw: Response(json.dumps(expected).encode())).call(
            self.message("tools/list")
        )
        self.assertEqual(result, expected)

    def test_installer_code_only_and_missing_credential_fails_without_network(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("ASSIGNED_MCP_", "INFISICAL_", "FLEET_SECRETS_"))
            }
            env["HOME"] = tmp
            installer = Path(__file__).resolve().parents[1] / "install-assigned-hosted-stdio.sh"
            for _ in range(2):
                subprocess.run(["bash", str(installer)], env=env, capture_output=True, check=True)
            command = Path(tmp) / ".local/bin/assigned-hosted-stdio"
            self.assertTrue(command.is_symlink())
            result = subprocess.run(
                [str(command)], env=env, capture_output=True, timeout=5, check=False
            )
            self.assertEqual(result.returncode, 3)
            self.assertIn(b"missing or malformed", result.stderr)
            self.assertFalse((Path(tmp) / ".config").exists())

    def test_no_redirect_handler(self):
        self.assertIsNone(
            mod.NoRedirect().redirect_request(
                None, None, 302, "redirect", {}, "https://untrusted.invalid"
            )
        )


if __name__ == "__main__":
    unittest.main()
