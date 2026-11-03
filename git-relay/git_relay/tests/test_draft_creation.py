"""Offline draft creation through the existing owner-token PR operation."""

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path

from git_relay.audit import AuditLog
from git_relay.github_client import GitHubClient
from git_relay.mcp_server import RelayMcp
from git_relay.mcp_server import _validate_args as validate_mcp
from git_relay.policy import SeatPolicy
from git_relay.server import Relay, _validate_args
from git_relay.tests.test_git_relay import FakeResponse

ARGS = {
    "repo": "example/fixture",
    "base": "main",
    "head": "feature",
    "title": "Title",
    "body": "Body",
}
TOKEN = "s" * 32
POLICY = json.dumps(
    {
        "swu-codex": {
            "token": TOKEN,
            "actions": ["open_pr"],
            "repos": [ARGS["repo"]],
            "app_slug": "swu-codex",
            "mint_permissions": {"pull_requests": "write"},
        }
    }
)
URL = "https://github.com/example/fixture/pull/1"
DRAFT_RECEIPT = {
    "number": 1,
    "html_url": URL,
    "draft": True,
    "base": {"repo": {"full_name": ARGS["repo"]}},
}


class DraftCreation(unittest.TestCase):
    def run_creation(self, extra, receipt, check_hold=False):
        calls = []

        def opener(request, timeout):
            calls.append(request)
            return FakeResponse(receipt)

        client = GitHubClient("synthetic-existing-owner-pr-token", opener=opener)
        with tempfile.TemporaryDirectory() as directory:
            relay = Relay(SeatPolicy(POLICY), client, AuditLog(str(Path(directory) / "audit")))
            response = relay.handle(TOKEN, "open_pr", {**ARGS, **extra})
            if check_hold:
                self.assertEqual(relay.handle(TOKEN, "open_pr", {**ARGS, **extra})[0], 409)
                self.assertEqual(len(calls), 1)
        return response, calls

    def test_true_reaches_existing_endpoint_and_credential_with_verified_receipt(self):
        response, calls = self.run_creation({"draft": True}, DRAFT_RECEIPT)
        self.assertEqual(response, (200, {"number": 1, "url": URL}))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].full_url, "https://api.github.com/repos/example/fixture/pulls")
        self.assertEqual(calls[0].get_method(), "POST")
        self.assertEqual(
            calls[0].get_header("Authorization"), "Bearer synthetic-existing-owner-pr-token"
        )
        self.assertIs(json.loads(calls[0].data)["draft"], True)

    def test_omitted_and_false_keep_existing_payload_and_receipt(self):
        self.assertEqual(
            _validate_args("open_pr", ARGS), _validate_args("open_pr", {**ARGS, "draft": False})
        )
        for extra in ({}, {"draft": False}):
            response, calls = self.run_creation(extra, {"number": 1, "html_url": "url"})
            self.assertEqual(response, (200, {"number": 1, "url": "url"}))
            self.assertEqual(
                json.loads(calls[0].data),
                {key: ARGS[key] for key in ("title", "head", "base", "body")},
            )

    def test_nonboolean_rejected_before_upstream_at_relay_mcp_and_client(self):
        for value in (None, "true", "false", 1, 0, [], {}):
            with self.subTest(value=value):
                response, calls = self.run_creation({"draft": value}, {})
                self.assertEqual(response[0], 400)
                self.assertEqual(calls, [])
                self.assertIsNotNone(validate_mcp("open_pr", {**ARGS, "draft": value}))
                client = GitHubClient("synthetic-existing-owner-pr-token")
                with self.assertRaises(ValueError):
                    client.open_pr(**ARGS, draft=value)
        for value in (False, True):
            self.assertIsNone(validate_mcp("open_pr", {**ARGS, "draft": value}))

    def test_missing_or_wrong_draft_receipt_is_unknown(self):
        for value in (None, False, "true", 1):
            response, calls = self.run_creation(
                {"draft": True}, {**DRAFT_RECEIPT, "draft": value}, check_hold=True
            )
            self.assertEqual(response[0], 502)
            self.assertEqual(response[1]["outcome"], "unknown")
            self.assertFalse(response[1]["retryable"])
            self.assertEqual(len(calls), 1)

    def test_malformed_identity_receipts_are_unknown_and_hold_replay(self):
        bad = [
            None,
            [],
            "scalar",
            1,
            {"draft": True},
            {**DRAFT_RECEIPT, "number": False},
            {**DRAFT_RECEIPT, "number": 0},
            {**DRAFT_RECEIPT, "number": 2**53},
            {**DRAFT_RECEIPT, "number": "1"},
            {**DRAFT_RECEIPT, "html_url": {}},
            {**DRAFT_RECEIPT, "html_url": "url"},
            {**DRAFT_RECEIPT, "html_url": URL + "?token=private"},
            {**DRAFT_RECEIPT, "html_url": URL.replace("/1", "/2")},
            {**DRAFT_RECEIPT, "html_url": URL.replace("example/fixture", "other/repo")},
            {**DRAFT_RECEIPT, "html_url": URL.replace("github.com", "github.com.invalid")},
            {**DRAFT_RECEIPT, "base": None},
            {**DRAFT_RECEIPT, "base": {"repo": {"full_name": "other/repo"}}},
        ]
        for receipt in bad:
            with self.subTest(receipt=receipt):
                response, calls = self.run_creation({"draft": True}, receipt, check_hold=True)
                self.assertEqual(response[0], 502)
                self.assertEqual(response[1]["outcome"], "unknown")
                self.assertFalse(response[1]["retryable"])
                self.assertEqual(len(calls), 1)

    def test_ready_cached_success_and_false_share_key_but_true_is_distinct(self):
        calls = []

        def opener(request, timeout):
            payload = json.loads(request.data)
            calls.append(payload)
            return FakeResponse(
                DRAFT_RECEIPT
                if payload.get("draft") is True
                else {"number": 2, "html_url": "legacy"}
            )

        with tempfile.TemporaryDirectory() as directory:
            relay = Relay(
                SeatPolicy(POLICY),
                GitHubClient("synthetic-owner", opener=opener),
                AuditLog(str(Path(directory) / "audit")),
            )
            first = relay.handle(TOKEN, "open_pr", ARGS)
            self.assertEqual(first[0], 200)
            self.assertEqual(relay.handle(TOKEN, "open_pr", {**ARGS, "draft": False}), first)
            self.assertEqual(len(calls), 1)
            drafted = relay.handle(TOKEN, "open_pr", {**ARGS, "draft": True})
            self.assertEqual(drafted, (200, {"number": 1, "url": URL}))
            self.assertEqual(relay.handle(TOKEN, "open_pr", {**ARGS, "draft": True}), drafted)
            self.assertEqual(len(calls), 2)

    def test_mcp_false_matches_old_strict_backend_wire_before_send(self):
        wire = []

        def opener(request, timeout):
            wire.append(request.data)
            if set(json.loads(request.data)) - set(ARGS):
                error = urllib.error.HTTPError(
                    request.full_url,
                    400,
                    "unexpected field",
                    {},
                    io.BytesIO(b'{"error":"unexpected field"}'),
                )
                self.addCleanup(error.close)
                raise error
            return FakeResponse({"number": 1, "url": "legacy"})

        wrapper = RelayMcp("https://relay.example.test", "synthetic-own-bearer", opener=opener)

        def call(extra):
            return wrapper.handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "open_pr", "arguments": {**ARGS, **extra}},
                }
            )

        for extra in ({}, {"draft": False}):
            self.assertNotIn("isError", call(extra)["result"])
        self.assertEqual(wire, [json.dumps(ARGS).encode()] * 2)
        self.assertTrue(call({"draft": True})["result"]["isError"])
        self.assertIs(json.loads(wire[2])["draft"], True)
        for value in (None, "false", 0, 1, [], {}):
            self.assertIn("error", call({"draft": value}))
            direct = wrapper._call_tool({"id": 1}, "open_pr", {**ARGS, "draft": value})
            self.assertIn("error", direct)
        self.assertEqual(len(wire), 3)
