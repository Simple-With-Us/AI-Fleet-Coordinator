"""GR-MCP exposes the relay's wf verbs as pass-through tools (AFC-664 item 6).

The wrapper must name exactly the relay's SPECS list (minus the verbs whose
bodies cannot fit the stdio line cap), forward to /v1/<action> with the seat
token, validate only the envelope locally, and keep read failures distinct
from write outcome-unknown failures.
"""

import io
import json
import unittest
import urllib.error

from git_relay.mcp_server import (
    TOOL_NAMES,
    TOOLS,
    WF_ACTIONS,
    WF_EXCLUDED,
    RelayMcp,
    _validate_args,
)
from git_relay.workflow_verbs import SPECS


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class WfToolsListed(unittest.TestCase):
    def test_every_spec_action_but_the_line_cap_exclusions_is_a_tool(self):
        expected = set(SPECS) - set(WF_EXCLUDED)
        self.assertEqual(set(WF_ACTIONS), expected)
        for action in expected:
            self.assertIn(action, TOOL_NAMES)
        for action in WF_EXCLUDED:
            self.assertNotIn(action, TOOL_NAMES)
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
        self.assertEqual(seen["auth"], "Bearer seat-token-value")
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
        reply = self._call(self._server(None), "wf_files_put", {"repo": "o/r"})
        self.assertEqual(reply["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()
