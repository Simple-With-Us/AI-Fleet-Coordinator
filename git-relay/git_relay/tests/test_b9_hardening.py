"""B9 hardening: 401 audit, credential-shaped audit detail, scan coverage, MCP edges."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest

from git_relay.audit import AuditLog
from git_relay.policy import SeatPolicy
from git_relay.secret_scan import find_secret
from git_relay.server import Relay

SEAT_TOKEN = "s" * 32
SEATS_JSON = json.dumps(
    {
        "swu-echo": {
            "token": SEAT_TOKEN,
            "actions": ["open_pr", "comment_pr", "workflow_dispatch"],
            "repos": ["example/fixture-repo"],
            "app_slug": "swu-echo",
            "mint_permissions": {"pull_requests": "write", "actions": "write"},
        }
    }
)


class StubClient:
    def __init__(self):
        self.calls = []


def _rows(path):
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [json.loads(line) for line in fh if line.strip()]


class TestUnauthenticatedAudit(unittest.TestCase):
    def test_401_writes_one_unknown_seat_row_without_token_material(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.jsonl")
            relay = Relay(SeatPolicy(SEATS_JSON), StubClient(), AuditLog(path))
            wrong = "wrong-token-material-1234567890"
            code, _ = relay.handle(wrong, "comment_pr", {"repo": "example/fixture-repo"})
            self.assertEqual(code, 401)
            rows = _rows(path)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["seat"], "unknown")
            self.assertEqual(rows[0]["result"], "unauthenticated")
            self.assertEqual(rows[0]["action"], "comment_pr")
            with open(path) as fh:
                self.assertNotIn(wrong, fh.read())

    def test_401_credential_shaped_action_is_never_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.jsonl")
            relay = Relay(SeatPolicy(SEATS_JSON), StubClient(), AuditLog(path))
            sprayed = "gh" + "p_" + "A" * 36
            code, _ = relay.handle("wrong-token", sprayed, {})
            self.assertEqual(code, 401)
            with open(path) as fh:
                raw = fh.read()
            self.assertNotIn(sprayed, raw)
            rows = _rows(path)
            self.assertEqual(rows[0]["action"], "<credential-shaped>")

    def test_401_audit_is_rate_limited(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.jsonl")
            relay = Relay(SeatPolicy(SEATS_JSON), StubClient(), AuditLog(path))
            for i in range(40):
                code, _ = relay.handle(f"wrong-{i}-" + "x" * 24, "comment_pr", {})
                self.assertEqual(code, 401)
            rows = _rows(path)
            self.assertGreaterEqual(len(rows), 1)
            self.assertLess(len(rows), 40)


class TestAuditKeyMeaning(unittest.TestCase):
    def _record(self, detail):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "a.jsonl")
        AuditLog(path).record("seat", "act", "a/b", "ok", detail)
        with open(path) as fh:
            raw = fh.read()
        return json.loads(raw), raw

    def test_credential_meaning_keys_are_hashed_at_any_nesting(self):
        secrets = {
            "client_secret": "client-secret-value-001",
            "X-Api-Key": "api-key-value-00000002",
            "signing_key": "signing-key-value-0003",
            "Authorization": "auth-header-value-00004",
            "cookie": "cookie-value-000000005",
            "session": "session-value-00000006",
            "passwd": "passwd-value-000000007",
            "credential": "credential-value-00008",
        }
        detail = dict(secrets)
        detail["nested"] = {"refresh_token": "refresh-token-value-009", "keep": "visible"}
        detail["inputs"] = {"harmless_name": "dispatch-input-value-10", "count": 3}
        detail["keep"] = "ordinary"
        row, raw = self._record(detail)
        for value in secrets.values():
            self.assertNotIn(value, raw)
        self.assertNotIn("refresh-token-value-009", raw)
        self.assertNotIn("dispatch-input-value-10", raw)
        self.assertEqual(row["keep"], "ordinary")
        self.assertEqual(row["nested"]["keep"], "visible")
        expect = hashlib.sha256(secrets["client_secret"].encode()).hexdigest()
        self.assertEqual(row["client_secret_sha256"], expect)

    def test_plausibly_credential_value_under_benign_key_is_hashed(self):
        value = "AKIA" + "A1B2C3D4E5F6G7H8"
        row, raw = self._record({"note": value, "branch": "feature/x"})
        self.assertNotIn(value, raw)
        self.assertEqual(row["branch"], "feature/x")

    def test_long_token_like_value_hashes_but_identifiers_stay(self):
        token_like = "T0k_" + "aB3-" * 9 + "Zz9"
        row, raw = self._record(
            {"note": token_like, "sha": "a" * 40, "clientSecret": "camel-secret-1"}
        )
        self.assertNotIn(token_like, raw)
        self.assertNotIn("camel-secret-1", raw)
        self.assertEqual(row["sha"], "a" * 40)

    def test_masked_credential_preview_stays_readable(self):
        row, _ = self._record({"credential_preview": "ghp_abcd\u2026wxyz"})
        self.assertEqual(row["credential_preview"], "ghp_abcd\u2026wxyz")


class TestScanPatterns(unittest.TestCase):
    def test_new_credential_shapes_are_flagged(self):
        self.assertIsNotNone(find_secret("key AKIA" + "A1B2C3D4E5F6G7H8" + " end"))
        self.assertIsNotNone(find_secret("xox" + "b-" + "0" * 10 + "-" + "A" * 16))
        jwt = "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20
        self.assertIsNotNone(find_secret(jwt))
        self.assertIsNotNone(find_secret("Authorization: Bearer " + "t" * 24))

    def test_prose_bearer_word_is_not_flagged(self):
        self.assertIsNone(find_secret("the bearer token is provisioned out of band"))


class TestMcpEdges(unittest.TestCase):
    def _server(self):
        from git_relay.mcp_server import RelayMcp

        return RelayMcp("http://127.0.0.1:8471", "seat-token-value", opener=None)

    def test_plaintext_non_loopback_relay_url_is_refused(self):
        from git_relay.mcp_server import RelayMcp

        with self.assertRaises(ValueError):
            RelayMcp("http://relay.example.com:8471", "seat-token-value", opener=None)
        RelayMcp("http://127.0.0.1:8471", "seat-token-value", opener=None)
        RelayMcp("http://localhost:8471", "seat-token-value", opener=None)
        RelayMcp("https://relay.example.com", "seat-token-value", opener=None)

    def test_malformed_line_with_recoverable_id_gets_parse_error(self):
        from git_relay.mcp_server import process_line

        logs = []
        reply = process_line('{"jsonrpc":"2.0","id":7,"method":', self._server(), logs.append)
        self.assertIsNotNone(reply)
        self.assertEqual(reply["id"], 7)
        self.assertEqual(reply["error"]["code"], -32700)

    def test_malformed_line_without_id_logs_bounded_stderr_only(self):
        from git_relay.mcp_server import process_line

        logs = []
        reply = process_line("{oops not json", self._server(), logs.append)
        self.assertIsNone(reply)
        self.assertEqual(len(logs), 1)
        self.assertLess(len(logs[0]), 200)
        self.assertNotIn("oops", logs[0])

    def test_oversized_line_with_recoverable_id_gets_invalid_request(self):
        from git_relay.mcp_server import MAX_LINE, process_line

        logs = []
        line = '{"jsonrpc":"2.0","id":"big-1","method":"ping","pad":"' + "x" * MAX_LINE + '"}'
        reply = process_line(line, self._server(), logs.append)
        self.assertIsNotNone(reply)
        self.assertEqual(reply["id"], "big-1")
        self.assertEqual(reply["error"]["code"], -32600)

    def test_non_object_line_logs_instead_of_silent_drop(self):
        from git_relay.mcp_server import process_line

        logs = []
        reply = process_line("[1,2,3]", self._server(), logs.append)
        self.assertIsNone(reply)
        self.assertEqual(len(logs), 1)

    def test_valid_line_still_works(self):
        from git_relay.mcp_server import process_line

        logs = []
        reply = process_line(
            '{"jsonrpc":"2.0","id":1,"method":"tools/list"}', self._server(), logs.append
        )
        self.assertIn("tools", reply["result"])
        self.assertEqual(logs, [])


if __name__ == "__main__":
    unittest.main()
