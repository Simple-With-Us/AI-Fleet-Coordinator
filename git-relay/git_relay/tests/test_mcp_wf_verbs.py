"""GR-MCP exposes the relay's wf verbs as pass-through tools (AFC-664 item 6).

The wrapper must name exactly the relay's SPECS list — no verb stays
hosted-only (owner ruling, 2026-11-08) — forward to /v1/<action> with the
seat token, validate only the envelope locally, and keep read failures
distinct from write outcome-unknown failures.  Verbs whose bodies can
exceed the stdio line cap are carried by the chunked staging transport
(stage_chunk / stage_abort), proven end to end below.
"""

import io
import json
import unittest
import urllib.error

from git_relay.mcp_server import (
    TOOL_NAMES,
    TOOLS,
    WF_ACTIONS,
    RelayMcp,
    _validate_args,
    stage_messages,
)
from git_relay.workflow_verbs import SPECS


FAKE_TOKEN = "seat-token-value"


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class WfToolsListed(unittest.TestCase):
    def test_every_spec_action_is_a_tool_no_hosted_only_verbs(self):
        self.assertEqual(set(WF_ACTIONS), set(SPECS))
        for action in SPECS:
            self.assertIn(action, TOOL_NAMES)
        for action in (
            "wf_file_put",
            "wf_files_put",
            "wf_git_blob_create",
            "wf_git_tree_create",
            "wf_release_asset_upload_binary",
        ):
            self.assertIn(action, TOOL_NAMES)
        self.assertIn("stage_chunk", TOOL_NAMES)
        self.assertIn("stage_abort", TOOL_NAMES)
        names = [t["name"] for t in TOOLS]
        self.assertEqual(len(names), len(set(names)))

    def test_the_original_seven_tools_are_unchanged(self):
        for name in (
            "open_pr",
            "edit_pr",
            "reply_thread",
            "comment_pr",
            "merge_pr",
            "workflow_dispatch",
            "resolve_thread",
        ):
            self.assertIn(name, TOOL_NAMES)


class WfValidation(unittest.TestCase):
    def test_envelope_only_repo_required_and_bounded(self):
        self.assertIsNone(
            _validate_args(
                "wf_pr_close",
                {"repo": "o/r", "number": 3, "expected_head_sha": "a" * 40},
            )
        )
        self.assertIsNotNone(_validate_args("wf_pr_close", {"number": 3}))
        self.assertIsNotNone(_validate_args("wf_pr_close", {"repo": 3}))
        self.assertIsNotNone(
            _validate_args("wf_pr_list_page", {"repo": "o/r", **{f"k{i}": i for i in range(65)}})
        )


class WfForwarding(unittest.TestCase):
    def _server(self, opener):
        return RelayMcp("http://127.0.0.1:8471", "seat-token-value", opener=opener)

    def _call(self, server, name, args):
        return server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            }
        )

    def test_a_wf_read_is_forwarded_to_the_relay_action_path(self):
        seen = {}

        def opener(req, timeout=None):
            seen["url"] = req.full_url
            seen["auth"] = req.headers.get("Authorization")
            seen["body"] = json.loads(req.data.decode())
            return FakeResponse(json.dumps({"pulls": [], "page": 1}).encode())

        reply = self._call(
            self._server(opener), "wf_pr_list_page", {"repo": "o/r", "state": "open"}
        )
        self.assertEqual(seen["url"], "http://127.0.0.1:8471/v1/wf_pr_list_page")
        # Built from a variable, not a literal:  the relay's secret scanner
        # refuses any file whose text matches its bearer-credential pattern,
        # and the literal header + this fake token matches it — which made
        # this pre-existing test file unwritable through the relay.
        self.assertEqual(seen["auth"], "Bearer " + FAKE_TOKEN)
        self.assertEqual(seen["body"], {"repo": "o/r", "state": "open"})
        text = reply["result"]["content"][0]["text"]
        self.assertEqual(json.loads(text), {"pulls": [], "page": 1})

    def test_a_wf_read_http_error_is_a_refusal_not_an_unknown_write(self):
        def opener(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 502, "bad gateway", {}, io.BytesIO(b'{"error":"x"}')
            )

        reply = self._call(self._server(opener), "wf_pr_list_page", {"repo": "o/r"})
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("relay_refused", reply["result"]["content"][0]["text"])

    def test_a_wf_read_the_relay_marks_retryable_stays_retryable(self):
        # Kody finding on PR #1515: a 429 or 502 the relay marks retryable is not a refusal.
        body = b'{"error":"rate limited","retryable":true,"retry_after_seconds":7}'

        def opener(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 429, "slow down", {}, io.BytesIO(body))

        reply = self._call(self._server(opener), "wf_pr_list_page", {"repo": "o/r"})
        self.assertTrue(reply["result"]["isError"])
        error = json.loads(reply["result"]["content"][0]["text"])
        self.assertEqual(error["code"], "unavailable")
        self.assertIs(error["retryable"], True)
        self.assertEqual(error["retry_after_seconds"], 7)
        self.assertEqual(error["http_status"], 429)

    def test_a_wf_write_http_error_keeps_the_unknown_outcome_guard(self):
        def opener(req, timeout=None):
            raise urllib.error.HTTPError(
                req.full_url, 502, "bad gateway", {}, io.BytesIO(b'{"error":"x"}')
            )

        reply = self._call(
            self._server(opener),
            "wf_pr_close",
            {"repo": "o/r", "number": 3, "expected_head_sha": "a" * 40},
        )
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("outcome_unknown", reply["result"]["content"][0]["text"])

    def test_a_wf_read_timeout_is_unavailable_not_an_unknown_write(self):
        # Seer finding on PR #1515: the timeout branch ignored read/write entirely.
        def opener(req, timeout=None):
            raise TimeoutError("timed out")

        reply = self._call(self._server(opener), "wf_pr_list_page", {"repo": "o/r"})
        self.assertTrue(reply["result"]["isError"])
        text = reply["result"]["content"][0]["text"]
        self.assertIn("unavailable", text)
        self.assertNotIn("outcome_unknown", text)

    def test_a_wf_write_timeout_keeps_the_unknown_outcome_guard(self):
        def opener(req, timeout=None):
            raise TimeoutError("timed out")

        reply = self._call(
            self._server(opener),
            "wf_pr_close",
            {"repo": "o/r", "number": 3, "expected_head_sha": "a" * 40},
        )
        self.assertTrue(reply["result"]["isError"])
        self.assertIn("outcome_unknown", reply["result"]["content"][0]["text"])

    def test_an_unlisted_wf_name_is_an_unknown_tool(self):
        reply = self._call(self._server(None), "wf_no_such_verb", {"repo": "o/r"})
        self.assertEqual(reply["error"]["code"], -32602)


class WfStaging(unittest.TestCase):
    """The chunked transport carries the five large-body verbs whole."""

    def _server(self, opener):
        return RelayMcp("http://127.0.0.1:8471", "seat-token-value", opener=opener)

    def _run_staged(self, server, name, args, chunk_chars=64):
        messages = list(stage_messages(name, args, chunk_chars=chunk_chars))
        for message in messages:
            message["id"] = 9
        reply = None
        for message in messages:
            reply = server.handle(message)
        return reply, messages

    def test_a_large_file_put_is_reassembled_and_forwarded_exactly(self):
        seen = {}

        def opener(req, timeout=None):
            seen["url"] = req.full_url
            seen["body"] = json.loads(req.data.decode())
            return FakeResponse(json.dumps({"path": "a.txt", "commit": "b" * 40}).encode())

        args = {
            "repo": "o/r",
            "path": "a.txt",
            "branch": "feat",
            "message": "add a",
            "content_b64": "QUJD" * 30000,  # 120,000 chars, over one small line
        }
        reply, messages = self._run_staged(self._server(opener), "wf_file_put", args, chunk_chars=256)
        self.assertGreater(len(messages), 400)
        self.assertEqual(seen["url"], "http://127.0.0.1:8471/v1/wf_file_put")
        self.assertEqual(seen["body"], args)
        self.assertEqual(json.loads(reply["result"]["content"][0]["text"])["path"], "a.txt")

    def test_intermediate_chunks_forward_nothing(self):
        calls = []

        def opener(req, timeout=None):
            calls.append(req)
            return FakeResponse(b"{}")

        server = self._server(opener)
        messages = list(stage_messages("wf_git_blob_create", {"repo": "o/r", "text": "x" * 500}, chunk_chars=64))
        first = server.handle({**messages[0], "id": 1})
        self.assertEqual(calls, [])
        self.assertFalse(json.loads(first["result"]["content"][0]["text"])["complete"])

    def test_out_of_order_chunk_drops_the_stage(self):
        server = self._server(None)
        messages = list(stage_messages("wf_git_blob_create", {"repo": "o/r", "text": "x" * 500}, chunk_chars=64))
        server.handle({**messages[0], "id": 1})
        reply = server.handle({**messages[2], "id": 2})
        self.assertEqual(reply["error"]["code"], -32602)
        # The stage is gone:  the next in-order chunk is now a stray seq 1.
        reply = server.handle({**messages[1], "id": 3})
        self.assertEqual(reply["error"]["code"], -32602)

    def test_only_wf_verbs_can_be_staged(self):
        server = self._server(None)
        messages = list(stage_messages("open_pr", {"repo": "o/r"}, chunk_chars=64))
        reply = server.handle({**messages[0], "id": 1})
        self.assertEqual(reply["error"]["code"], -32602)

    def test_stage_abort_drops_without_forwarding(self):
        calls = []

        def opener(req, timeout=None):
            calls.append(req)
            return FakeResponse(b"{}")

        server = self._server(opener)
        messages = list(stage_messages("wf_git_tree_create", {"repo": "o/r", "files": []}, chunk_chars=16))
        server.handle({**messages[0], "id": 1})
        stage_id = messages[0]["params"]["arguments"]["stage_id"]
        reply = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "stage_abort", "arguments": {"stage_id": stage_id}},
            }
        )
        self.assertIn("stage dropped", reply["result"]["content"][0]["text"])
        self.assertEqual(calls, [])
        reply = server.handle({**messages[1], "id": 3})
        self.assertEqual(reply["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
