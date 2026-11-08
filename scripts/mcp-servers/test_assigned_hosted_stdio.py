"""Offline synthetic tests. No credentials, network or live writes; files only in temp folders."""

import base64
import http.client
import importlib.util
import io
import json
import os
import stat
import subprocess
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

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
        if session is not None:
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

    def test_notifications_stay_local_with_no_stdio_response(self):
        def never(*args, **kw):
            self.fail("a notification reached the network")

        adapter = mod.Adapter(TOKEN, never)
        for method in (
            "notifications/initialized",
            "notifications/cancelled",
            "notifications/roots/list_changed",
        ):
            self.assertIsNone(adapter.call({"jsonrpc": "2.0", "method": method}))

    def test_bare_tool_call_sends_a_protocol_version_without_initialize(self):
        seen = []

        def opener(request, timeout):
            seen.append(request)
            return Response(json.dumps(self.reply()).encode())

        mod.Adapter(TOKEN, opener).call(self.message())
        self.assertEqual(seen[0].headers["Mcp-protocol-version"], mod.DEFAULT_PROTOCOL)
        self.assertNotIn("Mcp-session-id", seen[0].headers)

    def test_first_initialize_names_no_protocol_version(self):
        seen = []

        def opener(request, timeout):
            seen.append(request)
            return Response(json.dumps(self.reply({"protocolVersion": "2025-06-18"})).encode())

        mod.Adapter(TOKEN, opener).call(self.message("initialize"))
        self.assertNotIn("Mcp-protocol-version", seen[0].headers)

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

    def test_identities_discovery_receives_no_injected_property(self):
        def opener(request, timeout):
            self.assertEqual(json.loads(request.data)["params"]["arguments"], {})
            return Response(json.dumps(self.reply()).encode())

        message = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": "identities", "arguments": {}},
        }
        self.assertEqual(mod.Adapter(TOKEN, opener).call(message), self.reply())

    def test_incomplete_chunked_response_returns_unknown_without_crashing(self):
        class Truncated(Response):
            def read(self, size=-1):
                raise http.client.IncompleteRead(b"private-partial-response")

        result = mod.Adapter(TOKEN, lambda *a, **kw: Truncated(b"")).call(self.message())
        error = json.loads(result["result"]["content"][0]["text"])
        self.assertEqual(error["code"], "outcome_unknown")
        self.assertNotIn("private-partial-response", json.dumps(result))

    def test_explicit_reinitialize_adopts_rotated_session_and_safe_protocol(self):
        replies = iter(
            [
                Response(
                    json.dumps(self.reply({"protocolVersion": "future-v2"})).encode(),
                    session="first",
                ),
                Response(
                    json.dumps(self.reply({"protocolVersion": "future-v2"})).encode(),
                    session="second",
                ),
            ]
        )
        adapter = mod.Adapter(TOKEN, lambda *a, **kw: next(replies))
        adapter.call(self.message("initialize"))
        adapter.call(self.message("initialize"))
        self.assertEqual(adapter.session, "second")
        self.assertEqual(adapter.protocol, "future-v2")

    def test_invalid_and_oversized_frames_are_replied_and_next_frame_survives(self):
        import sys
        from unittest import mock

        payload = (
            b"invalid\n"
            + b"x" * (mod.MAX_BYTES + 10)
            + b"\n"
            + b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        )
        fake_stdin = type("Input", (), {"buffer": io.BytesIO(payload)})()
        out = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"ASSIGNED_MCP_BEARER": TOKEN}),
            mock.patch.object(sys, "stdin", fake_stdin),
            mock.patch.object(sys, "stdout", out),
            mock.patch.object(mod.Adapter, "call", return_value=None) as call,
        ):
            self.assertEqual(mod.main(), 0)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(len(out.getvalue().splitlines()), 2)

    def test_no_redirect_handler(self):
        self.assertIsNone(
            mod.NoRedirect().redirect_request(
                None, None, 302, "redirect", {}, "https://untrusted.invalid"
            )
        )


INIT_RESULT = {
    "protocolVersion": "2025-06-18",
    "capabilities": {"tools": {}, "events": {}},
    "serverInfo": {"name": "ST-MCP", "version": "2"},
    "instructions": "You act as ECHO, with this seat's own instructions.",
}
TOOLS_RESULT = {"tools": [{"name": "whoami", "inputSchema": {"type": "object"}}]}


class Upstream:
    """A fake hosted server that counts every POST by its JSON-RPC method."""

    def __init__(self, session=None):
        self.methods = []
        self.requests = []
        self.session = session
        self.tools = TOOLS_RESULT

    def __call__(self, request, timeout):
        body = json.loads(request.data)
        self.methods.append(body["method"])
        self.requests.append(request)
        result = {"initialize": INIT_RESULT, "tools/list": self.tools}.get(
            body["method"], {"content": []}
        )
        reply = {"jsonrpc": "2.0", "id": body["id"], "result": result}
        return Response(json.dumps(reply).encode(), session=self.session)


def frame(method, frame_id=1, **params):
    return {"jsonrpc": "2.0", "id": frame_id, "method": method, "params": params}


INITIALIZE = frame("initialize", protocolVersion="2025-06-18", capabilities={})
TOOL_CALL = frame("tools/call", name="whoami", arguments={})
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}


class DiskCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = os.path.join(temp.name, "cache")
        self.upstream = Upstream()

    def adapter(self, token=TOKEN, upstream=None):
        return mod.Adapter(token, upstream or self.upstream, cache_dir=self.dir)

    def test_warm_one_shot_session_costs_one_request(self):
        self.adapter().call(INITIALIZE)
        self.upstream.methods.clear()
        adapter = self.adapter()
        reply = adapter.call(frame("initialize", 41, protocolVersion="2025-06-18"))
        self.assertEqual(reply["id"], 41)
        self.assertIsNone(adapter.call(INITIALIZED))
        self.assertFalse(adapter.call(TOOL_CALL)["result"].get("isError"))
        self.assertEqual(self.upstream.methods, ["tools/call"])

    def test_cached_initialize_keeps_seat_instructions_capabilities_and_protocol(self):
        first = self.adapter().call(INITIALIZE)
        self.assertEqual(first["result"], INIT_RESULT)
        adapter = self.adapter()
        second = adapter.call(frame("initialize", "abc", protocolVersion="2025-06-18"))
        self.assertEqual(second, {"jsonrpc": "2.0", "id": "abc", "result": INIT_RESULT})
        adapter.call(TOOL_CALL)
        self.assertEqual(self.upstream.requests[-1].headers["Mcp-protocol-version"], "2025-06-18")
        self.assertEqual(self.upstream.methods, ["initialize", "tools/call"])

    def test_each_requested_protocol_version_negotiates_for_itself(self):
        self.adapter().call(INITIALIZE)
        self.adapter().call(frame("initialize", protocolVersion="2025-03-26"))
        self.assertEqual(self.upstream.methods, ["initialize", "initialize"])

    def test_answers_are_never_shared_between_bearers(self):
        self.adapter().call(INITIALIZE)
        self.adapter().call(frame("tools/list"))
        other = "sbt_other_" + "B" * 43
        self.upstream.methods.clear()
        adapter = self.adapter(other)
        adapter.call(INITIALIZE)
        adapter.call(frame("tools/list"))
        self.assertEqual(self.upstream.methods, ["initialize", "tools/list"])

    def test_tools_list_is_served_from_disk_then_refreshes_after_the_ttl(self):
        self.assertEqual(self.adapter().call(frame("tools/list"))["result"], TOOLS_RESULT)
        self.assertEqual(self.adapter().call(frame("tools/list", 2))["result"], TOOLS_RESULT)
        self.assertEqual(self.upstream.methods, ["tools/list"])
        newer = {"tools": TOOLS_RESULT["tools"] + [{"name": "new_tool"}]}
        self.upstream.tools = newer
        later = time.time() + mod.CACHE_TTL_S + 1
        with mock.patch.object(mod.time, "time", return_value=later):
            self.assertEqual(self.adapter().call(frame("tools/list", 3))["result"], newer)
            self.assertEqual(self.adapter().call(frame("tools/list", 4))["result"], newer)
        self.assertEqual(self.upstream.methods, ["tools/list", "tools/list"])

    def test_initialize_refreshes_after_the_ttl_and_a_future_timestamp_is_not_trusted(self):
        self.adapter().call(INITIALIZE)
        for offset in (mod.CACHE_TTL_S + 1, -3600):
            with mock.patch.object(mod.time, "time", return_value=time.time() + offset):
                cache = mod.DiskCache(self.dir, TOKEN)
                self.assertIsNone(cache.load(mod.initialize_key(INITIALIZE)))

    def test_paged_lists_are_not_cached(self):
        self.upstream.tools = {"tools": [], "nextCursor": "page-2"}
        self.adapter().call(frame("tools/list"))
        self.adapter().call(frame("tools/list", 2))
        self.adapter().call(frame("tools/list", 3, cursor="page-2"))
        self.assertEqual(len(self.upstream.methods), 3)

    def test_errors_and_tool_results_are_not_cached(self):
        def error(request, timeout):
            body = json.loads(request.data)
            reply = {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -1, "message": "no"}}
            return Response(json.dumps(reply).encode())

        for message in (INITIALIZE, frame("tools/list")):
            self.adapter(upstream=error).call(message)
        self.adapter().call(TOOL_CALL)
        self.adapter().call(TOOL_CALL)
        self.assertFalse(os.path.exists(self.dir) and os.listdir(self.dir))
        self.assertEqual(self.upstream.methods, ["tools/call", "tools/call"])

    def test_a_server_that_issues_sessions_keeps_getting_real_initializes(self):
        upstream = Upstream(session="stateful")
        self.adapter(upstream=upstream).call(INITIALIZE)
        self.adapter(upstream=upstream).call(INITIALIZE)
        self.assertEqual(upstream.methods, ["initialize", "initialize"])

    def test_a_session_issuing_server_never_has_its_catalog_cached(self):
        upstream = Upstream(session="stateful")
        for frame_id in (1, 2):
            self.adapter(upstream=upstream).call(frame("tools/list", frame_id))
        self.assertEqual(upstream.methods, ["tools/list", "tools/list"])
        self.assertFalse(os.path.exists(self.dir) and os.listdir(self.dir))

    def test_a_server_that_starts_issuing_sessions_drops_what_was_cached(self):
        self.adapter().call(frame("tools/list"))
        self.assertEqual(len(os.listdir(self.dir)), 1)
        upstream = Upstream(session="stateful")
        adapter = self.adapter(upstream=upstream)
        adapter.call(INITIALIZE)
        self.assertEqual(os.listdir(self.dir), [])
        adapter.call(frame("tools/list", 2))
        self.assertEqual(upstream.methods, ["initialize", "tools/list"])

    def test_a_live_session_is_never_answered_from_disk(self):
        upstream = Upstream(session="stateful")
        adapter = self.adapter(upstream=upstream)
        adapter.call(INITIALIZE)
        cache = mod.DiskCache(self.dir, TOKEN)  # Another process's entry, written after the clear.
        cache.store(mod.tools_key("2025-06-18"), TOOLS_RESULT)
        cache.store(mod.initialize_key(INITIALIZE), INIT_RESULT)
        adapter.call(frame("tools/list", 2))
        adapter.call(frame("initialize", 3, protocolVersion="2025-06-18"))
        self.assertEqual(upstream.methods, ["initialize", "tools/list", "initialize"])

    def test_a_catalog_is_only_replayed_to_the_version_that_received_it(self):
        modern = frame("tools/list", 2, _meta={mod.META_VERSION: "2026-07-28"})
        self.adapter().call(frame("tools/list"))
        self.upstream.tools = {**TOOLS_RESULT, "resultType": "complete"}
        self.adapter().call(modern)  # A legacy answer on disk must not satisfy this.
        self.assertEqual(self.upstream.methods, ["tools/list", "tools/list"])
        replay = self.adapter().call(modern)
        self.assertEqual(replay["result"]["resultType"], "complete")
        self.assertNotIn("resultType", self.adapter().call(frame("tools/list", 3))["result"])
        self.assertEqual(len(self.upstream.methods), 2)
        negotiated = self.adapter()
        negotiated.protocol = "2025-03-26"
        negotiated.call(frame("tools/list", 5))
        self.assertEqual(len(self.upstream.methods), 3)

    def test_a_malformed_envelope_is_left_for_the_server_to_refuse(self):
        for bad in (7, "", "two words", None):
            malformed = frame("tools/list", _meta={mod.META_VERSION: bad})
            self.adapter().call(frame("tools/list"))
            self.upstream.methods.clear()
            self.upstream.requests.clear()
            self.adapter().call(malformed)
            self.assertEqual(self.upstream.methods, ["tools/list"])
            self.assertNotIn("Mcp-protocol-version", self.upstream.requests[0].headers)
        self.assertEqual(len(os.listdir(self.dir)), 1)

    def test_an_initialize_in_the_modern_form_is_not_answered_from_disk(self):
        self.adapter().call(INITIALIZE)
        modern = frame(
            "initialize", 2, protocolVersion="2025-06-18", _meta={mod.META_VERSION: "2026-07-28"}
        )
        self.upstream.methods.clear()
        self.adapter().call(modern)
        self.assertEqual(self.upstream.methods, ["initialize"])

    def test_an_initialize_in_the_modern_form_never_fills_the_legacy_slot(self):
        modern_result = {**INIT_RESULT, "protocolVersion": "2026-07-28", "resultType": "complete"}

        def modern_server(request, timeout):
            body = json.loads(request.data)
            self.upstream.methods.append(body["method"])
            reply = {"jsonrpc": "2.0", "id": body["id"], "result": modern_result}
            return Response(json.dumps(reply).encode())

        modern = frame(
            "initialize", 2, protocolVersion="2025-06-18", _meta={mod.META_VERSION: "2026-07-28"}
        )
        self.adapter(upstream=modern_server).call(modern)
        self.assertFalse(os.path.exists(self.dir) and os.listdir(self.dir))
        legacy = self.adapter()
        self.assertEqual(legacy.call(INITIALIZE)["result"], INIT_RESULT)
        self.assertEqual(legacy.protocol, INIT_RESULT["protocolVersion"])
        self.assertEqual(self.upstream.methods, ["initialize", "initialize"])
        self.adapter(upstream=modern_server).call(modern)  # A legacy answer on disk survives it.
        self.assertEqual(self.adapter().call(INITIALIZE)["result"], INIT_RESULT)
        self.assertEqual(self.upstream.methods, ["initialize", "initialize", "initialize"])

    def test_an_empty_session_id_counts_as_no_session(self):
        upstream = Upstream(session="")
        adapter = self.adapter(upstream=upstream)
        adapter.call(INITIALIZE)
        adapter.call(frame("tools/list", 2))  # A later answer on that same connection.
        self.assertEqual(len(os.listdir(self.dir)), 2)
        self.assertNotIn("Mcp-session-id", upstream.requests[-1].headers)
        warm = self.adapter(upstream=upstream)
        warm.call(frame("initialize", 3, protocolVersion="2025-06-18"))
        warm.call(frame("tools/list", 4))
        self.assertEqual(upstream.methods, ["initialize", "tools/list"])

    def test_only_a_request_with_no_cursor_is_a_first_page(self):
        self.adapter().call(frame("tools/list"))
        self.upstream.methods.clear()
        for bad in ("", 0, None, False, []):
            self.assertIn("result", self.adapter().call(frame("tools/list", 2, cursor=bad)))
        self.adapter().call({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": []})
        self.assertEqual(self.upstream.methods, ["tools/list"] * 6)
        no_params = {"jsonrpc": "2.0", "id": 4, "method": "tools/list"}
        self.assertIn("result", self.adapter().call(no_params))
        self.assertEqual(len(self.upstream.methods), 6)  # No params at all is the first page.

    def test_a_cursor_answer_never_becomes_the_first_page(self):
        self.upstream.tools = {"tools": [{"name": "second_page"}]}
        self.adapter().call(frame("tools/list", cursor="page-2"))
        self.assertFalse(os.path.exists(self.dir) and os.listdir(self.dir))

    def test_a_real_initialize_after_a_cached_one_names_no_protocol_version(self):
        self.adapter().call(INITIALIZE)
        adapter = self.adapter()
        adapter.call(frame("initialize", 2, protocolVersion="2025-06-18"))  # From disk.
        self.assertEqual(adapter.protocol, "2025-06-18")
        self.upstream.requests.clear()
        adapter.call(frame("initialize", 3, protocolVersion="2025-03-26"))  # A miss.
        self.assertEqual(self.upstream.methods[-1], "initialize")
        self.assertNotIn("Mcp-protocol-version", self.upstream.requests[0].headers)

    def test_a_repeat_real_initialize_names_no_protocol_version(self):
        adapter = mod.Adapter(TOKEN, self.upstream)  # Uncached, so both reach the server.
        adapter.call(INITIALIZE)
        adapter.call(frame("initialize", 2, protocolVersion="2025-03-26"))
        self.assertEqual(self.upstream.methods, ["initialize", "initialize"])
        for request in self.upstream.requests:
            self.assertNotIn("Mcp-protocol-version", request.headers)

    def test_a_requests_own_envelope_names_its_version(self):
        modern = frame("tools/call", 2, name="w", _meta={mod.META_VERSION: "2026-07-28"})
        adapter = self.adapter()
        adapter.call(modern)  # Nothing negotiated: the default must not contradict it.
        self.assertEqual(self.upstream.requests[-1].headers["Mcp-protocol-version"], "2026-07-28")
        adapter.call(INITIALIZE)
        adapter.call(modern)  # Negotiated legacy: still the request's own version.
        self.assertEqual(self.upstream.requests[-1].headers["Mcp-protocol-version"], "2026-07-28")
        adapter.call(TOOL_CALL)  # A legacy request keeps the negotiated one.
        self.assertEqual(self.upstream.requests[-1].headers["Mcp-protocol-version"], "2025-06-18")

    def test_cache_is_private_and_holds_no_bearer(self):
        self.adapter().call(INITIALIZE)
        self.adapter().call(frame("tools/list"))
        self.assertEqual(stat.S_IMODE(os.stat(self.dir).st_mode), 0o700)
        names = os.listdir(self.dir)
        self.assertEqual(len(names), 2)
        for name in names:
            path = os.path.join(self.dir, name)
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            self.assertNotIn(TOKEN, name)
            self.assertNotIn(TOKEN, Path(path).read_text())

    def test_a_widened_planted_or_corrupt_file_is_ignored(self):
        self.adapter().call(frame("tools/list"))
        name = os.listdir(self.dir)[0]
        path = os.path.join(self.dir, name)
        good = Path(path).read_text()
        os.chmod(path, 0o644)
        self.adapter().call(frame("tools/list", 2))
        self.assertEqual(len(self.upstream.methods), 2)
        os.unlink(path)
        decoy = os.path.join(os.path.dirname(self.dir), "decoy")
        Path(decoy).write_text(good)
        os.chmod(decoy, 0o600)
        os.symlink(decoy, path)
        self.adapter().call(frame("tools/list", 3))
        self.assertEqual(len(self.upstream.methods), 3)
        for corrupt in (
            b"not json",
            b"[]",
            b'{"at":"x","result":{}}',
            b'{"at":1}',
            b'{"at":1' + b"0" * 400 + b',"result":{}}',  # Too large to subtract from a float.
        ):
            os.unlink(path)
            Path(path).write_bytes(corrupt)
            os.chmod(path, 0o600)
            count = len(self.upstream.methods)
            self.adapter().call(frame("tools/list", 4))
            self.assertEqual(len(self.upstream.methods), count + 1)

    def test_a_cached_initialize_without_seat_shape_is_not_answered_locally(self):
        os.makedirs(self.dir, mode=0o700)
        cache = mod.DiskCache(self.dir, TOKEN)
        malformed = {"protocolVersion": "bad version", "capabilities": {}}
        cache.store(mod.initialize_key(INITIALIZE), malformed)
        self.adapter().call(INITIALIZE)
        self.assertEqual(self.upstream.methods, ["initialize"])

    def test_an_unusable_cache_folder_changes_nothing_for_the_client(self):
        parent = os.path.dirname(self.dir)
        Path(self.dir).write_text("a file where the folder should be")
        adapter = self.adapter()
        self.assertEqual(adapter.call(INITIALIZE)["result"], INIT_RESULT)
        self.assertEqual(adapter.call(frame("tools/list"))["result"], TOOLS_RESULT)
        self.assertEqual(self.upstream.methods, ["initialize", "tools/list"])
        self.assertEqual(os.listdir(parent), ["cache"])

    def test_cache_folder_comes_from_the_environment_or_stays_off(self):
        self.assertEqual(mod.default_cache_dir({"HOME": "/h"}), "/h/.cache/assigned-hosted-stdio")
        self.assertEqual(
            mod.default_cache_dir({"HOME": "/h", "XDG_CACHE_HOME": "/x"}),
            "/x/assigned-hosted-stdio",
        )
        self.assertIsNone(mod.default_cache_dir({"PATH": "/usr/bin:/bin"}))
        self.assertIsNone(mod.default_cache_dir({"HOME": "relative"}))


class BearerRefusalTests(unittest.TestCase):
    REFUSED = json.dumps(
        {"error": "invalid_token", "error_description": "This token is not valid."}
    ).encode()

    def refusing(self, body=REFUSED, code=401):
        def opener(request, timeout):
            raise urllib.error.HTTPError("u", code, "refused", {}, io.BytesIO(body))

        return opener

    def test_tool_call_says_the_bearer_was_refused_and_clears_this_seats_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            mod.Adapter(TOKEN, Upstream(), cache_dir=temp).call(INITIALIZE)
            other = mod.DiskCache(temp, "sbt_other_" + "B" * 43)
            other.store("tools", TOOLS_RESULT)
            adapter = mod.Adapter(TOKEN, self.refusing(), cache_dir=temp)
            self.assertEqual(adapter.call(INITIALIZE)["result"], INIT_RESULT)  # Connect succeeds.
            reply = adapter.call(TOOL_CALL)
            self.assertTrue(reply["result"]["isError"])
            error = json.loads(reply["result"]["content"][0]["text"])
            self.assertEqual(error["code"], "bearer_refused")
            self.assertFalse(error["retryable"])
            self.assertIn("did not run", error["message"])
            self.assertNotIn(TOKEN, json.dumps(reply))
            self.assertIsNone(mod.DiskCache(temp, TOKEN).load(mod.initialize_key(INITIALIZE)))
            self.assertEqual(other.load("tools"), TOOLS_RESULT)

    def test_clearing_continues_past_a_file_another_adapter_already_removed(self):
        with tempfile.TemporaryDirectory() as temp:
            cache = mod.DiskCache(temp, TOKEN)
            cache.store("tools", TOOLS_RESULT)
            cache.store("init-a", INIT_RESULT)
            real_unlink = os.unlink
            calls = []

            def racing_unlink(path):
                calls.append(path)
                if len(calls) == 1:
                    real_unlink(path)
                    raise FileNotFoundError(path)
                real_unlink(path)

            with mock.patch.object(mod.os, "unlink", racing_unlink):
                cache.clear()
            self.assertEqual(os.listdir(temp), [])
            self.assertEqual(len(calls), 2)

    def test_a_real_initialize_also_names_the_refusal(self):
        reply = mod.Adapter(TOKEN, self.refusing()).call(INITIALIZE)
        self.assertEqual(reply["error"]["code"], -32000)
        self.assertIn("refused this connection's assigned bearer", reply["error"]["message"])

    def test_any_other_answer_stays_outcome_unknown(self):
        for body, code in (
            (b"not-json", 401),
            (b"[]", 401),
            (b'{"error":"other"}', 401),
            (self.REFUSED, 403),
            (self.REFUSED, 500),
        ):
            reply = mod.Adapter(TOKEN, self.refusing(body, code)).call(TOOL_CALL)
            error = json.loads(reply["result"]["content"][0]["text"])
            self.assertEqual(error["code"], "outcome_unknown")


class MainLoopTests(unittest.TestCase):
    """The stdio loop as launchers run it: the cache folder comes from HOME."""

    def run_main(self, home, frames):
        upstream = Upstream()
        raw = b"".join(json.dumps(f).encode() + b"\n" for f in frames)
        fake_stdin = type("Input", (), {"buffer": io.BytesIO(raw)})()
        out = io.StringIO()
        env = {k: v for k, v in os.environ.items() if k != "XDG_CACHE_HOME"}
        env.update({"HOME": home, "ASSIGNED_MCP_BEARER": TOKEN})
        with (
            mock.patch.dict(os.environ, env, clear=True),
            mock.patch.object(mod.sys, "stdin", fake_stdin),
            mock.patch.object(mod.sys, "stdout", out),
            mock.patch.object(
                mod.urllib.request, "build_opener", return_value=mock.Mock(open=upstream)
            ),
        ):
            self.assertEqual(mod.main(), 0)
        return upstream.methods, [json.loads(line) for line in out.getvalue().splitlines()]

    def test_second_process_reaches_the_server_only_for_the_tool_call(self):
        frames = [INITIALIZE, INITIALIZED, frame("tools/list", 2), frame("tools/call", 3, name="w")]
        with tempfile.TemporaryDirectory() as home:
            methods, replies = self.run_main(home, frames)
            self.assertEqual(methods, ["initialize", "tools/list", "tools/call"])
            self.assertEqual([r["id"] for r in replies], [1, 2, 3])  # No reply to the notification.
            self.assertEqual(len(os.listdir(os.path.join(home, ".cache/assigned-hosted-stdio"))), 2)
            methods, second = self.run_main(home, frames)
            self.assertEqual(methods, ["tools/call"])
            self.assertEqual(second, replies)


class FileFrameTests(unittest.TestCase):
    def test_larger_frames_are_file_only_and_bounded(self):
        message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "gr_wf_files_put",
                "arguments": {"content_b64": "A" * (5 * 1024 * 1024)},
            },
        }
        size = len(json.dumps(message).encode())
        self.assertTrue(mod.file_frame_allowed(message, size))
        message["params"]["name"] = "gr_wf_runs"
        self.assertFalse(mod.file_frame_allowed(message, size))
        message["params"]["name"] = "gr_wf_files_put"
        self.assertFalse(mod.file_frame_allowed(message, mod.MAX_FILE_FRAME_BYTES + 1))
        self.assertFalse(mod.file_frame_allowed([message], size))
        self.assertEqual(mod.MAX_BYTES, 4 * 1024 * 1024)


class FileWireEncodingTests(unittest.TestCase):
    def test_non_ascii_metadata_uses_wire_encoder_for_argument_bytes(self):
        message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "gr_wf_files_put",
                "arguments": {
                    "content_b64": "A" * (5 * 1024 * 1024),
                    "metadata": "😀" * 10000,
                },
            },
        }
        wire_size = len(json.dumps(message).encode())
        compact_args = len(
            json.dumps(
                message["params"]["arguments"], separators=(",", ":"), ensure_ascii=False
            ).encode()
        )
        self.assertGreater(wire_size - compact_args, 65536)
        self.assertTrue(mod.file_frame_allowed(message, wire_size))
        message["params"]["arguments"]["metadata"] = "😀" * 90000
        wire = len(json.dumps(message).encode())
        args_size = len(json.dumps(message["params"]["arguments"]).encode())
        self.assertGreater(args_size, mod.MAX_FILE_ARGS_BYTES)
        self.assertLessEqual(wire, mod.MAX_FILE_FRAME_BYTES)
        self.assertFalse(mod.file_frame_allowed(message, wire))


class ModernHeaderTests(unittest.TestCase):
    """A request that names a modern version in its envelope carries the standard headers the
    hosted server requires (SEP-2243), exactly as the SDK's own client sends them.  The Worker's
    test/stdio-adapter-headers.test.mjs sends what this adapter builds through the real SDK path."""

    VERSION = "2026-07-28"

    def sent(self, message):
        upstream = Upstream()
        mod.Adapter(TOKEN, upstream).call(message)
        return {key.lower(): value for key, value in upstream.requests[-1].header_items()}

    def modern(self, method, frame_id=1, **params):
        return frame(method, frame_id, _meta={mod.META_VERSION: self.VERSION}, **params)

    def test_a_modern_tool_call_names_its_version_method_and_tool(self):
        headers = self.sent(self.modern("tools/call", name="whoami", arguments={}))
        self.assertEqual(headers["mcp-protocol-version"], self.VERSION)
        self.assertEqual(headers["mcp-method"], "tools/call")
        self.assertEqual(headers["mcp-name"], "whoami")

    def test_a_method_with_no_name_field_sends_the_method_alone(self):
        for method in ("tools/list", "server/discover", "ping", "resources/list"):
            headers = self.sent(self.modern(method))
            self.assertEqual(headers["mcp-protocol-version"], self.VERSION, method)
            self.assertEqual(headers["mcp-method"], method)
            self.assertNotIn("mcp-name", headers, method)

    def test_every_named_method_mirrors_its_own_body_field(self):
        cases = [
            ("tools/call", {"name": "gr_wf_runs", "arguments": {}}, "gr_wf_runs"),
            ("prompts/get", {"name": "daily"}, "daily"),
            ("resources/read", {"uri": "file:///notes/a b.md"}, "file:///notes/a b.md"),
            ("tasks/get", {"taskId": "task-1"}, "task-1"),
            ("tasks/update", {"taskId": "task-2"}, "task-2"),
            ("tasks/cancel", {"taskId": "task-3"}, "task-3"),
        ]
        for method, params, expected in cases:
            self.assertEqual(self.sent(self.modern(method, **params))["mcp-name"], expected, method)
        # The field that names it must be a string, or no header says so and the server judges the body.
        for params in ({"name": 5}, {"name": None}, {}, {"uri": "x"}):
            self.assertNotIn("mcp-name", self.sent(self.modern("tools/call", **params)))

    def test_a_name_that_is_not_plain_ascii_goes_out_in_the_base64_form(self):
        self.assertEqual(mod.encode_header_value("whoami"), "whoami")
        self.assertEqual(mod.encode_header_value("a\tb"), "a\tb")
        self.assertEqual(mod.encode_header_value("naïve"), "=?base64?bmHDr3Zl?=")
        self.assertEqual(mod.encode_header_value(""), "=?base64??=")
        self.assertEqual(mod.encode_header_value(" lead"), "=?base64?IGxlYWQ=?=")
        self.assertEqual(mod.encode_header_value("\ud83d"), "=?base64?77+9?=")  # a lone surrogate is U+FFFD
        for value in ("trail ", "x\ty\t", "line\nbreak", "😀", "=?base64?abc?=", "\x7f"):
            encoded = mod.encode_header_value(value)
            self.assertTrue(encoded.startswith("=?base64?") and encoded.endswith("?="), value)
        # What the server decodes is what the body says.
        for value in ("naïve", "", " lead", "trail ", "😀", "=?base64?abc?=", "日本語"):
            encoded = mod.encode_header_value(value)
            self.assertEqual(base64.b64decode(encoded[len("=?base64?") : -len("?=")]).decode("utf-8"), value)
        sent = self.sent(self.modern("tools/call", name="naïve", arguments={}))
        self.assertEqual(sent["mcp-name"], "=?base64?bmHDr3Zl?=")

    def test_a_modern_initialize_names_its_version_and_method_and_no_tool(self):
        headers = self.sent(self.modern("initialize", protocolVersion=self.VERSION, capabilities={}))
        self.assertEqual(headers["mcp-protocol-version"], self.VERSION)
        self.assertEqual(headers["mcp-method"], "initialize")
        self.assertNotIn("mcp-name", headers)

    def test_legacy_requests_send_no_method_or_name_header(self):
        for message in (INITIALIZE, TOOL_CALL, frame("tools/list"), frame("ping")):
            headers = self.sent(message)
            self.assertNotIn("mcp-method", headers, message["method"])
            self.assertNotIn("mcp-name", headers, message["method"])
        self.assertNotIn("mcp-protocol-version", self.sent(INITIALIZE))
        self.assertEqual(self.sent(TOOL_CALL)["mcp-protocol-version"], mod.DEFAULT_PROTOCOL)

    def test_a_malformed_envelope_is_left_for_the_server_to_refuse_with_none_of_the_three(self):
        for claimed in ("two words", "", 20260728, None, ["2026-07-28"], "é"):
            message = frame("tools/call", 1, name="whoami", arguments={}, _meta={mod.META_VERSION: claimed})
            headers = self.sent(message)
            for name in ("mcp-protocol-version", "mcp-method", "mcp-name"):
                self.assertNotIn(name, headers, f"{claimed!r} {name}")

    def test_a_method_that_cannot_be_a_header_value_sends_no_method_or_name(self):
        for method in ("tools/call\nX-Evil: 1", "tools/ call", "", "é"):
            message = {"jsonrpc": "2.0", "id": 4, "method": method, "params": {"name": "x", "_meta": {mod.META_VERSION: self.VERSION}}}
            headers = self.sent(message)
            self.assertNotIn("mcp-method", headers, repr(method))
            self.assertNotIn("mcp-name", headers, repr(method))
            self.assertNotIn("x-evil", headers)

    def test_the_headers_leave_the_body_and_the_other_headers_as_they_were(self):
        upstream = Upstream()
        message = self.modern("tools/call", name="whoami", arguments={"a": 1})
        mod.Adapter(TOKEN, upstream).call(message)
        request = upstream.requests[-1]
        sent_headers = {key.lower(): value for key, value in request.header_items()}
        self.assertEqual(sent_headers["authorization"], "Bearer " + TOKEN)
        self.assertEqual(sent_headers["content-type"], "application/json")
        self.assertEqual(sent_headers["user-agent"], "assigned-hosted-stdio/1.0")
        body = json.loads(request.data)
        self.assertEqual(body["params"]["name"], "whoami")
        self.assertEqual(body["params"]["_meta"], {mod.META_VERSION: self.VERSION})
        self.assertEqual(body["params"]["arguments"], {"a": 1, "identity": "ECHO"})


if __name__ == "__main__":
    unittest.main()
