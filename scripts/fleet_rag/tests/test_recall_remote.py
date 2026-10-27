"""Tests for the opt-in remote read transport of scripts/recall (SIMPLE_RECALL_REMOTE=1).

Hermetic: the one network function is replaced in process; the direct and public routes are patched to fail the
test if reached.  Run:  cd scripts && python3 -m unittest fleet_rag.tests.test_recall_remote
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[2] / "recall"
_loader = importlib.machinery.SourceFileLoader("recall_remote_under_test", str(SCRIPT))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
recall = importlib.util.module_from_spec(_spec)
_loader.exec_module(recall)

BEARER = "sbt_codex_" + "B" * 43
FENCED = "BEGIN_UNTRUSTED_RECALL n\nhit one\nEND_UNTRUSTED_RECALL n"


def reply(structured, text=FENCED, *, is_error=False):
    msg = {"jsonrpc": "2.0", "id": 1, "result": {"structuredContent": structured, "isError": is_error,
                                                 "content": [{"type": "text", "text": text}]}}
    return 200, "application/json", json.dumps(msg).encode()


class RemoteRecall(unittest.TestCase):
    def run_cli(self, argv, answer, env=None):
        calls = []

        def fake(url, headers, body, timeout):
            calls.append((url, headers, json.loads(body)))
            return answer

        def boom(*a, **k):
            raise AssertionError("the direct or public route was reached in remote mode")

        full = {"SIMPLE_RECALL_REMOTE": "1", "SIMPLE_TOOLS_BEARER": BEARER}
        full.update(env or {})
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, full, clear=True), mock.patch.object(recall, "_remote_post", fake), \
                mock.patch.object(recall.public_fallback, "call_with_fallback", boom), \
                mock.patch.object(recall.recall_api, "recall_search", boom), \
                mock.patch.object(recall.recall_api, "recall_contribute", boom), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = recall.main(argv)
        return code, out.getvalue(), err.getvalue(), calls

    def test_search_maps_only_supported_arguments_to_the_fixed_endpoint(self):
        code, out, err, calls = self.run_cli(["pm2 dump", "--limit", "3", "--app", "fleet", "--category", "lesson"],
                                             reply({"count": 1, "mode": "hybrid", "doc_ids": ["d1"]}))
        self.assertEqual(code, 0, err)
        url, headers, msg = calls[0]
        self.assertEqual(url, "https://agents.simplewithus.com/mcp")
        self.assertEqual(headers["Authorization"], "Bearer " + BEARER)
        self.assertEqual(msg["params"], {"name": "recall_search",
                         "arguments": {"query": "pm2 dump", "limit": 3, "app": "fleet", "category": "lesson"}})
        self.assertIn("BEGIN_UNTRUSTED_RECALL", out)
        self.assertNotIn(BEARER, out + err)

    def test_stats(self):
        code, out, _, calls = self.run_cli(["stats", "--json"], reply({"points": 5}))
        self.assertEqual(code, 0)
        self.assertEqual(calls[0][2]["params"]["name"], "recall_stats")
        self.assertEqual(json.loads(out)["structured"], {"points": 5})

    def test_unsupported_search_flags_refuse_without_a_call(self):
        for argv in (["q", "--source", "board"], ["q", "--seat", "X"], ["q", "--since-days", "3"],
                     ["q", "--per-doc", "2"], ["q", "--no-rerank"], ["q", "--no-lessons"], ["q", "--full"]):
            with self.subTest(argv=argv):
                code, _, err, calls = self.run_cli(argv, reply({}))
                self.assertEqual(code, 2)
                self.assertIn("no hosted equivalent", err)
                self.assertEqual(calls, [])

    def test_other_verbs_refuse_and_are_not_run_directly(self):
        for argv in (["digest"], ["doctor"], ["ingest"]):
            with self.subTest(argv=argv):
                code, _, err, calls = self.run_cli(argv, reply({}))
                self.assertEqual(code, 2)
                self.assertIn("not available in remote mode", err)
                self.assertEqual(calls, [])

    LESSON = "pm2 start does not re-read env from the ecosystem file; use --update-env on restart."
    W = {"SIMPLE_RECALL_REMOTE_WRITES": "1"}

    def test_contribute_needs_its_own_switch(self):
        code, _, err, calls = self.run_cli(["contribute", self.LESSON, "--category", "lesson"], reply({}))
        self.assertEqual((code, calls), (2, []))
        self.assertIn("SIMPLE_RECALL_REMOTE_WRITES", err)

    def test_contribute_maps_to_recall_contribute_without_a_caller_seat(self):
        code, out, err, calls = self.run_cli(
            ["contribute", self.LESSON, "--category", "lesson", "--app", "fleet", "--title", "pm2", "--url", "https://x.example/1"],
            reply({"id": "1", "doc_id": "d", "seat": "CODEX", "duplicate": False}, "stored"), env=self.W)
        self.assertEqual(code, 0, err)
        self.assertEqual(calls[0][2]["params"], {"name": "recall_contribute", "arguments": {
            "text": self.LESSON, "category": "lesson", "app": "fleet", "title": "pm2", "url": "https://x.example/1"}})
        self.assertNotIn("seat", calls[0][2]["params"]["arguments"])

    def test_contribute_refuses_seat_and_force(self):
        for extra in (["--seat", "ECHO"], ["--force"]):
            code, _, _, calls = self.run_cli(["contribute", self.LESSON, "--category", "lesson"] + extra,
                                             reply({}), env=self.W)
            self.assertEqual((code, calls), (2, []))

    def test_bearer_problems_and_refusals_do_not_fall_back(self):
        for env in ({"SIMPLE_TOOLS_BEARER": ""}, {"SIMPLE_TOOLS_BEARER": "short"}):
            code, _, err, calls = self.run_cli(["stats"], reply({}), env=env)
            self.assertEqual(code, 2)
            self.assertEqual(calls, [])
        code, out, err, _ = self.run_cli(["stats"], (401, "", b""))
        self.assertEqual(code, 2)
        self.assertNotIn(BEARER, out + err)
        code, _, err, _ = self.run_cli(["stats"], reply({"code": "not_configured"}, is_error=True))
        self.assertEqual(code, 2)
        self.assertIn("not_configured", err)

    def test_no_endpoint_override(self):
        _, _, _, calls = self.run_cli(["stats"], reply({}), env={"RECALL_MCP_URL": "http://127.0.0.1:1/mcp"})
        self.assertEqual(calls[0][0], "https://agents.simplewithus.com/mcp")

    def test_switch_off_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(recall.remote_selected())


class TransportErrors(unittest.TestCase):
    def test_a_truncated_or_malformed_http_answer_is_a_clean_error(self):
        import http.client

        class Op:
            def open(self, *a, **k):
                raise http.client.IncompleteRead(b"x")

        with mock.patch.object(recall.urllib.request, "build_opener", return_value=Op()):
            with self.assertRaises(recall.FleetRagError):
                recall._remote_post("https://agents.simplewithus.com/mcp", {}, b"{}", 1)


if __name__ == "__main__":
    unittest.main()
