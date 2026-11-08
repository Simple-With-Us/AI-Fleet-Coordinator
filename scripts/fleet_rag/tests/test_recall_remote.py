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
            return answer(calls[-1]) if callable(answer) else answer

        def boom(*a, **k):
            raise AssertionError("the direct or public route was reached in remote mode")

        full = {"SIMPLE_RECALL_REMOTE": "1", "SIMPLE_TOOLS_BEARER": BEARER}
        full.update(env or {})
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, full, clear=True), mock.patch.object(recall, "_remote_post", fake), \
                mock.patch.object(recall.public_fallback, "call_with_fallback", boom), \
                mock.patch.object(recall.public_fallback, "call_mutation", boom), \
                mock.patch.object(recall.recall_api, "recall_search", boom), \
                mock.patch.object(recall.recall_api, "recall_contribute", boom), \
                mock.patch.object(recall.recall_api, "recall_correct", boom), \
                mock.patch.object(recall.recall_api, "recall_retract", boom), \
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
        code, _, _, calls = self.run_cli(["pm2 dump", "--include-retracted"], reply({"count": 1}))
        self.assertEqual(code, 0)
        self.assertTrue(calls[0][2]["params"]["arguments"]["include_retracted"])
        self.assertIn("BEGIN_UNTRUSTED_RECALL", out)
        self.assertNotIn(BEARER, out + err)

    def test_hosted_text_cannot_drive_the_terminal(self):
        code, out, err, _ = self.run_cli(["stats"], reply({"points": 5}, text="a\x1b]0;owned\x07b"))
        self.assertEqual(code, 0, err)
        self.assertNotIn("\x1b", out)
        self.assertIn("\\x1b", out)

    def test_a_credential_shaped_query_is_refused_before_any_send(self):
        code, out, err, calls = self.run_cli(["ghp_" + "A1b2C3d4E5" * 4], reply({}))
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertIn("secret_scan:query", err)
        self.assertNotIn("ghp_", err + out)

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

    IDENTITIES = {"default_identity": "CODEX", "authenticated_seat": "CODEX", "transport": "hosted",
                  "identities": [{"identity": "CODEX", "zulip": "configured", "linear": "configured",
                                  "recall": "configured"}]}

    def seat_then(self, structured, contribute_answer=None):
        """Answers identities with `structured`, then recall_contribute with the stored answer."""
        def answer(call):
            if call[2]["params"]["name"] == "identities":
                return reply(structured)
            return contribute_answer or reply({"id": "1", "doc_id": "d", "seat": "CODEX", "duplicate": False}, "stored")
        return answer

    def test_contribute_names_the_connections_verified_seat(self):
        code, out, err, calls = self.run_cli(
            ["contribute", self.LESSON, "--category", "lesson", "--app", "fleet", "--title", "pm2", "--url", "https://x.example/1"],
            self.seat_then(self.IDENTITIES), env=self.W)
        self.assertEqual(code, 0, err)
        self.assertEqual([c[2]["params"]["name"] for c in calls], ["identities", "recall_contribute"])
        self.assertEqual(calls[0][2]["params"]["arguments"], {})
        args = calls[1][2]["params"]["arguments"]
        self.assertEqual(args, {"text": self.LESSON, "category": "lesson", "app": "fleet",
                                "title": "pm2", "url": "https://x.example/1", "identity": "CODEX"})
        self.assertNotIn("idempotency_key", args)
        self.assertNotIn("seat", args)

    def test_contribute_without_a_proven_seat_sends_no_write(self):
        for structured in (
            {"default_identity": "CODEX", "authenticated_seat": "ECHO",  # disagrees with default
             "identities": [{"identity": "CODEX"}, {"identity": "ECHO"}]},
            {"default_identity": "CODEX", "authenticated_seat": "", "identities": [{"identity": "CODEX"}]},
            {"default_identity": "CODEX", "authenticated_seat": "CODEX",
             "identities": [{"identity": "ECHO"}]},  # own seat not listed as callable
            {"default_identity": "CODEX", "authenticated_seat": "CODEX", "identities": "nope"},
            {"authenticated_seat": "CODEX", "identities": [{"identity": "CODEX"}]},  # no default to agree with
        ):
            with self.subTest(structured=structured):
                code, _, err, calls = self.run_cli(
                    ["contribute", self.LESSON, "--category", "lesson"],
                    self.seat_then(structured), env=self.W)
                self.assertEqual(code, 2)
                self.assertEqual([c[2]["params"]["name"] for c in calls], ["identities"])
                self.assertIn("write was not sent", err)

    def test_contribute_when_the_identity_check_fails_no_write_is_sent(self):
        def answer(call):
            return reply({"code": "not_authorized"}, is_error=True)
        code, _, err, calls = self.run_cli(["contribute", self.LESSON, "--category", "lesson"], answer, env=self.W)
        self.assertEqual(code, 2)
        self.assertEqual([c[2]["params"]["name"] for c in calls], ["identities"])
        self.assertIn("not_authorized", err)

    def test_contribute_refuses_seat_and_force(self):
        for extra in (["--seat", "ECHO"], ["--force"]):
            code, _, _, calls = self.run_cli(["contribute", self.LESSON, "--category", "lesson"] + extra,
                                             reply({}), env=self.W)
            self.assertEqual((code, calls), (2, []))

    def test_correct_and_retract_need_the_write_switch(self):
        doc = "contrib/CODEX/2026-11-05/472fc36e"
        for argv in (["correct", doc, self.LESSON, "--reason", "make it current"],
                     ["retract", doc, "--reason", "obsolete now"]):
            code, _, err, calls = self.run_cli(argv, reply({}))
            self.assertEqual((code, calls), (2, []))
            self.assertIn("SIMPLE_RECALL_REMOTE_WRITES", err)

    def test_correct_and_retract_name_the_verified_connection_seat(self):
        doc = "contrib/CODEX/2026-11-05/472fc36e"
        operations = (
            (["correct", doc, self.LESSON, "--reason", "make it current", "--expected-revision", "2"],
             "recall_correct", {"doc_id": doc, "text": self.LESSON, "reason": "make it current",
                                "expected_revision": 2, "identity": "CODEX"}),
            (["retract", doc, "--reason", "obsolete now"],
             "recall_retract", {"doc_id": doc, "reason": "obsolete now", "identity": "CODEX"}),
        )
        for argv, tool, arguments in operations:
            with self.subTest(tool=tool):
                answer = self.seat_then(self.IDENTITIES, reply({"id": "1", "doc_id": doc}))
                code, _, err, calls = self.run_cli(argv, answer, env=self.W)
                self.assertEqual(code, 0, err)
                self.assertEqual([c[2]["params"]["name"] for c in calls], ["identities", tool])
                self.assertEqual(calls[0][2]["params"]["arguments"], {})
                self.assertEqual(calls[1][2]["params"], {"name": tool, "arguments": arguments})
                self.assertNotIn("seat", calls[1][2]["params"]["arguments"])

    def test_correct_and_retract_without_a_verified_seat_send_no_write(self):
        doc = "contrib/CODEX/2026-11-05/472fc36e"
        for argv in (["correct", doc, self.LESSON, "--reason", "current"],
                     ["retract", doc, "--reason", "obsolete"]):
            with self.subTest(operation=argv[0]):
                code, _, err, calls = self.run_cli(
                    argv, self.seat_then({"default_identity": "CODEX", "authenticated_seat": "ECHO",
                                          "identities": [{"identity": "CODEX"}]}), env=self.W)
                self.assertEqual(code, 2)
                self.assertEqual([c[2]["params"]["name"] for c in calls], ["identities"])
                self.assertIn("write was not sent", err)

    def test_mutations_refuse_a_caller_seat(self):
        doc = "contrib/CODEX/2026-11-05/472fc36e"
        for argv in (["correct", doc, self.LESSON, "--reason", "make it current", "--seat", "ECHO"],
                     ["retract", doc, "--reason", "obsolete now", "--seat", "ECHO"]):
            code, _, err, calls = self.run_cli(argv, reply({}), env=self.W)
            self.assertEqual((code, calls), (2, []))
            self.assertIn("no meaning in remote mode", err)

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
            with self.assertRaises(recall._TransportError) as ctx:
                recall._remote_post("https://agents.simplewithus.com/mcp", {}, b"{}", 1)
        self.assertTrue(ctx.exception.sent)


if __name__ == "__main__":
    unittest.main()
