#!/usr/bin/env python3
"""Tests for the board CLI's ST-MCP parity verbs:  commands, run, api and mcp.

Run:  python3 scripts/linear-board/test_board_parity.py   (stdlib only, under a minute)

The catalogs in catalog/*.json and the fixtures in testdata/agent-sync-fixtures.json are exported from
the hosted server's JavaScript (scripts/agent-sync-mcp/scripts/export-linear-catalog.mjs), and the node
test linear-catalog-parity pins them to a fresh export.  These tests close the loop on the Python side:
the CLI serves exactly the catalog's commands, builds byte-identical documents for every named command,
and its ports of the GraphQL guards, the public-input allowlist and the argument schema check give the
server's verdicts and messages.  The CLI runs as a subprocess against test_board.py's mock of Linear,
extended with the parity operations and a fake of Linear's official MCP server (JSON and SSE replies),
so no test reaches Linear or reads a real credential.
"""

from __future__ import annotations

import io
import json
import re
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from test_board import BoardTest, MockLinear, load_cli, make_handler

CATALOG = HERE / "catalog"
FIXTURES = json.loads((HERE / "testdata" / "agent-sync-fixtures.json").read_text())
CLI = load_cli()


def catalog(name):
    return json.loads((CATALOG / name).read_text())


# ----------------------------------------------------------------- pure ports against the server's fixtures


class CatalogTests(unittest.TestCase):
    def test_catalogs_load_with_unique_names_and_kinds(self):
        named = CLI.named_commands()
        self.assertEqual(len(named), catalog("named-commands.json")["count"])
        self.assertGreaterEqual(len(named), 175)
        self.assertEqual(len({c["name"] for c in named}), len(named))
        self.assertTrue(all(c["kind"] in ("read", "write") for c in named))
        tools = CLI.official_catalog()["tools"]
        self.assertEqual(len(tools), 11)
        self.assertTrue(all(t["name"] == "linear_" + t["upstream_name"] for t in tools))
        self.assertEqual(CLI.official_catalog()["url"], CLI.LINEAR_MCP_URL)

    def test_names_resolve_with_or_without_the_prefix(self):
        self.assertEqual(CLI.named_command("get_issue")["name"], "linear_get_issue")
        self.assertEqual(CLI.named_command("linear_get_issue")["name"], "linear_get_issue")
        self.assertEqual(CLI.official_tool("get_diff")["name"], "linear_get_diff")
        with self.assertRaises(SystemExit) as cm:
            CLI.named_command("no_such_command")
        self.assertEqual(cm.exception.code, 2)

    def test_every_named_command_matches_the_servers_document(self):
        """Python builds the same AgentSyncNamed document and variables as buildNamedOperation, and
        the guards give the same verdict, for every command (required args, all args, save create)."""
        by = {c["name"]: c for c in CLI.named_commands()}
        self.assertEqual({r["name"] for r in FIXTURES["named"]}, set(by))
        for row in FIXTURES["named"]:
            with self.subTest(command=row["name"], case=row["case"]):
                entry = by[row["name"]]
                problem = CLI.schema_problem(entry["input_schema"], row["args"])
                try:
                    if problem:
                        raise CLI.ParityError(problem)
                    doc, variables = CLI.build_named_operation(
                        entry, CLI.with_defaults(entry["input_schema"], row["args"])
                    )
                except CLI.ParityError as e:
                    self.assertFalse(row["ok"])
                    self.assertEqual(str(e), row["error"])
                    continue
                self.assertTrue(row["ok"], row.get("error"))
                self.assertEqual(doc, row["document"])
                self.assertEqual(variables, row["variables"])
                kind = "mutation" if entry["kind"] == "write" else "query"
                try:
                    prepared = CLI.prepare_graphql(doc, variables, kind)
                except CLI.ParityError as e:
                    self.assertEqual((False, str(e)), (row["guard_ok"], row.get("guard_error")))
                    continue
                self.assertTrue(row["guard_ok"])
                self.assertEqual(len(prepared["roots"]), 1)
                self.assertEqual(
                    prepared["roots"][0]["name"],
                    (entry.get("variants") or {}).get(
                        "update" if "id" in row["args"] else "create", entry
                    )["root"],
                )

    def test_guards_match_the_servers_verdicts_and_messages(self):
        for row in FIXTURES["guards"]:
            with self.subTest(case=row["name"]):
                try:
                    p = CLI.prepare_graphql(row["document"], row["variables"], row["expected"])
                except CLI.ParityError as e:
                    self.assertFalse(row["ok"], f"Python refused what the server accepts: {e}")
                    self.assertEqual(str(e), row["error"])
                    continue
                self.assertTrue(
                    row["ok"], f"Python accepted what the server refuses: {row.get('error')}"
                )
                self.assertEqual([r["name"] for r in p["roots"]], row["roots"])
                self.assertEqual(p["name"], row["operation"])
                if row["expected"] == "mutation":
                    self.assertEqual(p["args"], row["args"])

    def test_public_inputs_match_the_server(self):
        by = {c["name"]: c for c in CLI.named_commands()}
        for row in FIXTURES["public_inputs"]:
            with self.subTest(case=row["name"]):
                try:
                    CLI.assert_public_inputs(by[row["command"]], row["values"])
                    got = (True, None)
                except CLI.ParityError as e:
                    got = (False, str(e))
                self.assertEqual(got, (row["ok"], row.get("error")))

    def test_argument_schema_check_matches_the_server(self):
        tools = {c["name"]: c for c in CLI.named_commands()}
        tools.update({t["name"]: t for t in CLI.official_catalog()["tools"]})
        for row in FIXTURES["schema"]:
            with self.subTest(tool=row["name"], args=row["args"]):
                self.assertEqual(
                    CLI.schema_problem(tools[row["name"]]["input_schema"], row["args"]),
                    row["problem"],
                )
                self.assertEqual(
                    CLI.with_defaults(tools[row["name"]]["input_schema"], row["args"]),
                    row["defaults"],
                )

    def test_guards_reject_blocked_roots_protected_inputs_and_multiple_operations(self):
        bad = [
            (
                "mutation { apiKeyCreate(input: {}) { success } }",
                "This Linear mutation root is protected.",
            ),
            (
                "mutation { oauthClientCreate(input: {}) { success } }",
                "This Linear mutation root is protected.",
            ),
            (
                "mutation { issueBatchCreate(input: {}) { success } }",
                "Batch and bulk mutation roots are not allowed.",
            ),
            (
                "mutation { a { b } } mutation { c { d } }",
                "GraphQL document must contain exactly one operation.",
            ),
            (
                'mutation { issueArchive(id: "1") { success } issueDelete(id: "1") { success } }',
                "GraphQL mutations must contain exactly one root field.",
            ),
        ]
        for doc, msg in bad:
            with self.subTest(doc=doc), self.assertRaises(CLI.ParityError) as cm:
                CLI.prepare_graphql(doc, {}, "mutation")
            self.assertEqual(str(cm.exception), msg)

    def test_a_selection_override_cannot_escape_its_braces(self):
        entry = CLI.named_command("get_issue")
        for sel in (
            'id } } mutation X { issueDelete(id: "1") { success',
            "id } fragment F on Issue { id",
            "id }",
            "{",
            "...",
            "id(",
            "",
        ):
            with self.subTest(selection=sel), self.assertRaises(CLI.ParityError):
                CLI.build_named_operation(
                    entry,
                    {"id": "AFC-1", "selection": sel} if sel else {"id": "AFC-1", "selection": "}"},
                )
        doc, _ = CLI.build_named_operation(
            entry, {"id": "AFC-1", "selection": "id title labels(first: 5) { nodes { name } }"}
        )
        self.assertIn("issue(id: $id) { id title labels(first: 5) { nodes { name } } }", doc)

    def test_save_commands_pick_create_or_update_by_id(self):
        entry = CLI.named_command("save_issue")
        create, _ = CLI.build_named_operation(entry, {"input": {"title": "t"}})
        update, v = CLI.build_named_operation(entry, {"id": "AFC-1", "input": {"title": "t"}})
        self.assertIn("issueCreate(input: $input)", create)
        self.assertIn("issueUpdate(id: $id, input: $input)", update)
        self.assertEqual(v, {"id": "AFC-1", "input": {"title": "t"}})
        with self.assertRaises(CLI.ParityError):
            CLI.build_named_operation(
                CLI.named_command("save_project"), {"id": "p", "input": {}, "slackChannelName": "x"}
            )

    def test_tag_inputs_tags_bodies_in_variables_and_literals(self):
        tag = lambda t: f"[CLAUDE] {t}"
        p = CLI.prepare_graphql(
            "mutation M($input: CommentCreateInput!) { commentCreate(input: $input) { success } }",
            {"input": {"issueId": "i", "body": "hi", "nested": [{"description": "d"}]}},
            "mutation",
        )
        doc = CLI.tag_inputs(p, tag)
        self.assertEqual(
            p["variables"]["input"],
            {"issueId": "i", "body": "[CLAUDE] hi", "nested": [{"description": "[CLAUDE] d"}]},
        )
        self.assertEqual(doc, p["document"])
        src = 'mutation { issueCreate(input: { title: "t", description: """Line\n  "two"\n""", teamId: $t }) { success } }'
        p = CLI.prepare_graphql(src.replace("$t", '"x"'), {}, "mutation")
        doc = CLI.tag_inputs(p, tag)
        again = CLI.prepare_graphql(doc, {}, "mutation")
        self.assertEqual(again["args"]["input"]["description"], '[CLAUDE] Line\n"two"')
        self.assertEqual(again["args"]["input"]["title"], "t")
        p = CLI.prepare_graphql(
            'mutation M($b: String!) { commentCreate(input: { issueId: "i", body: $b }) { success } }',
            {"b": "é ✓"},
            "mutation",
        )
        CLI.tag_inputs(p, tag)
        self.assertEqual(p["variables"]["b"], "[CLAUDE] é ✓")
        # Only the root's `input` argument is tagged, as on the server.
        p = CLI.prepare_graphql(
            'mutation { issueUpdate(id: "x", body: "b", input: {}) { success } }', {}, "mutation"
        )
        self.assertEqual(CLI.tag_inputs(p, tag), p["document"])

    def test_protect_labels_refuses_claim_labels_and_authors(self):
        class FakeL:
            def __init__(self, labels, issue_labels=()):
                self.labels, self.issue_labels, self.calls = labels, issue_labels, []

            def call(self, q, v):
                self.calls.append(v)
                if "AgentSyncLabel" in q:
                    return {
                        "issueLabel": {"id": v["id"], "name": self.labels[v["id"]]}
                        if v["id"] in self.labels
                        else None
                    }
                return {"issue": {"labels": {"nodes": [{"name": n} for n in self.issue_labels]}}}

        def check(doc, variables, L):
            CLI.protect_labels(L, CLI.prepare_graphql(doc, variables, "mutation"))

        L = FakeL({"a": "needs-owner", "c": "claimed:CODEX", "s": "seat:CODEX", "x": "Stale-Claim"})
        check(
            'mutation M($i: IssueUpdateInput!) { issueUpdate(id: "1", input: $i) { success } }',
            {"i": {"addedLabelIds": ["a"]}},
            L,
        )
        for lid in ("c", "s", "x"):
            with self.subTest(label=lid), self.assertRaisesRegex(CLI.ParityError, "board claim"):
                check(
                    'mutation M($i: IssueUpdateInput!) { issueUpdate(id: "1", input: $i) { success } }',
                    {"i": {"removedLabelIds": [lid]}},
                    L,
                )
        with self.assertRaisesRegex(CLI.ParityError, "does not exist"):
            check('mutation { issueAddLabel(id: "1", labelId: "nope") { success } }', {}, L)
        with self.assertRaisesRegex(CLI.ParityError, "author identity"):
            check(
                'mutation { commentCreate(input: { issueId: "1", body: "b", createAsUser: "Jay" }) { success } }',
                {},
                L,
            )
        with self.assertRaisesRegex(CLI.ParityError, "managed by"):
            check(
                'mutation { issueLabelCreate(input: { name: "claimed:CLAUDE" }) { success } }',
                {},
                L,
            )
        with self.assertRaisesRegex(CLI.ParityError, "board claim"):
            check('mutation { issueLabelUpdate(id: "c", input: { name: "x" }) { success } }', {}, L)
        with self.assertRaisesRegex(CLI.ParityError, "dedicated fleet-aware"):
            check('mutation { issueLabelMerge(id: "a") { success } }', {}, L)
        with self.assertRaisesRegex(CLI.ParityError, "fleet claim"):
            check(
                'mutation { issueUpdate(id: "1", input: { labelIds: ["a"] }) { success } }',
                {},
                FakeL({"a": "x"}, ["claimed:CODEX"]),
            )
        check(
            'mutation { issueUpdate(id: "1", input: { labelIds: ["a"] }) { success } }',
            {},
            FakeL({"a": "x"}, ["ordinary"]),
        )


# ----------------------------------------------------------------- the CLI against the mock


class ParityMock(MockLinear):
    """test_board's mock plus the parity operations and a fake official MCP server."""

    def __init__(self):
        super().__init__()
        self.api_requests: list = []  # (operation, query, variables) of parity operations
        self.api_data = None  # canned data for AgentSyncNamed / ad-hoc operations
        self.http500_ops: dict = {}  # operation -> times to answer HTTP 500
        self.mcp_requests: list = []  # (headers, body)
        self.mcp_mode = "sse"
        self.mcp_result = {
            "content": [{"type": "text", "text": "Cycles group issues into time boxes."}]
        }

    def handle(self, op, q, v):
        if op == "AgentSyncLabel":
            lb = self.labels.get(v["id"])
            return {"issueLabel": {"id": lb["id"], "name": lb["name"]} if lb else None}, None
        if op == "AgentSyncSchema":
            self.api_requests.append((op, q, v))
            if v["name"] != "Issue":
                return {"__type": None}, None
            ref = {"kind": "SCALAR", "name": "String", "ofType": None}
            return {
                "__type": {
                    "name": "Issue",
                    "kind": "OBJECT",
                    "description": "An issue.",
                    "fields": [
                        {
                            "name": "id",
                            "args": [],
                            "type": {
                                "kind": "NON_NULL",
                                "name": None,
                                "ofType": {"kind": "SCALAR", "name": "ID", "ofType": None},
                            },
                        },
                        {"name": "title", "args": [], "type": ref},
                        {
                            "name": "comments",
                            "args": [
                                {
                                    "name": "first",
                                    "type": {"kind": "SCALAR", "name": "Int", "ofType": None},
                                }
                            ],
                            "type": {"kind": "OBJECT", "name": "CommentConnection", "ofType": None},
                        },
                        {"name": "accessToken", "args": [], "type": ref, "isDeprecated": True},
                    ],
                    "inputFields": None,
                    "enumValues": None,
                    "interfaces": [],
                    "possibleTypes": None,
                }
            }, None
        if op.startswith("Board"):
            return super().handle(op, q, v)
        self.api_requests.append((op, q, v))
        if self.api_data is not None:
            return self.api_data, None
        root = re.search(r"\{\s*(\w+)", q).group(1)
        return {root: {"success": True, "id": "made-1", "secretValue": "should-not-print"}}, None


def parity_handler(mock: ParityMock):
    base = make_handler(mock)

    class H(base):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.path == "/mcp":
                return self.mcp(body)
            m = re.search(rb"\b(?:query|mutation)\s+(\w+)", body)
            op = m.group(1).decode() if m else "?"
            with mock.lock:
                if mock.http500_ops.get(op, 0) > 0:
                    mock.http500_ops[op] -= 1
                    mock.api_requests.append((op, "HTTP 500", None))
                    return self._send(500, {"errors": [{"message": "boom"}]})
            self.rfile = io.BytesIO(body)
            return super().do_POST()

        def mcp(self, raw):
            msg = json.loads(raw or b"{}")
            with mock.lock:
                mock.mcp_requests.append(({k.lower(): v for k, v in self.headers.items()}, msg))
                auth = self.headers.get("Authorization") or ""
                if auth[7:] not in mock.tokens:
                    return self._send(401, {"error": "unauthorized"})
            if msg.get("method") == "notifications/initialized":
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return None
            if msg.get("method") == "initialize":
                reply = {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "fake-linear", "version": "1"},
                    },
                }
                return self._rpc(reply, {"Mcp-Session-Id": "session-abc"})
            if msg.get("method") == "tools/call":
                if self.headers.get("Mcp-Session-Id") != "session-abc":
                    return self._send(400, {"error": "missing session"})
                return self._rpc({"jsonrpc": "2.0", "id": msg["id"], "result": mock.mcp_result})
            return self._send(400, {})

        def _rpc(self, reply, headers=None):
            if mock.mcp_mode == "json":
                return self._send(200, reply, headers)
            # An SSE stream with a progress notification and a comment before the answer.
            raw = (
                ': keep-alive\n\nevent: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
                f"event: message\nid: 1\ndata: {json.dumps(reply)}\n\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            for k, val in (headers or {}).items():
                self.send_header(k, val)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return None

    return H


class ParityCliTest(BoardTest):
    def setUp(self):
        super().setUp()
        self.server.shutdown()
        self.server.server_close()
        self.mock = ParityMock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), parity_handler(self.mock))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        port = self.server.server_address[1]
        self.env.update(
            {
                "LINEAR_BOARD_API_URL": f"http://127.0.0.1:{port}/graphql",
                "LINEAR_BOARD_TOKEN_URL": f"http://127.0.0.1:{port}/oauth/token",
                "LINEAR_BOARD_MCP_URL": f"http://127.0.0.1:{port}/mcp",
            }
        )

    def parity_ops(self):
        return [r[0] for r in self.mock.api_requests]


class CommandsCliTests(ParityCliTest):
    def test_command_list_equals_the_exported_catalog(self):
        """The Python half of the JS<->Python pin:  board serves exactly the exported names."""
        p = self.board("commands", "--json", drop=("AGENT_SEAT",))
        self.assertEqual(
            [c["name"] for c in json.loads(p.stdout)],
            [c["name"] for c in catalog("named-commands.json")["commands"]],
        )
        p = self.board("mcp", "list", "--json", drop=("AGENT_SEAT",))
        self.assertEqual(
            [t["name"] for t in json.loads(p.stdout)],
            [t["name"] for t in catalog("official-mcp-tools.json")["tools"]],
        )
        self.assertEqual(self.mock.ops, [])
        self.assertEqual(self.mock.mints, 0)

    def test_commands_filter_and_show(self):
        p = self.board("commands", "--kind", "write", "--search", "label", "--json")
        rows = json.loads(p.stdout)
        self.assertTrue(rows and all(r["kind"] == "write" for r in rows))
        self.assertIn("linear_retire_issue_label", [r["name"] for r in rows])
        p = self.board("commands", "show", "list_issues")
        self.assertIn("first:  Int, optional, default 20", p.stdout)
        self.assertIn("filter:  IssueFilter, optional", p.stdout)
        self.assertIn("default selection:  nodes { id identifier", p.stdout)
        p = self.board("mcp", "show", "linear_update_diff")
        self.assertIn("urlOrId:  string, required", p.stdout)


class RunCliTests(ParityCliTest):
    def test_run_read_sends_the_named_document(self):
        p = self.board(
            "run", "get_issue", "--arg", "id=123", drop=("AGENT_SEAT",)
        )
        ((op, q, v),) = self.mock.api_requests
        self.assertEqual(op, "AgentSyncNamed")
        doc, variables = CLI.build_named_operation(CLI.named_command("get_issue"), {"id": "123"})
        self.assertEqual((q, v), (doc, variables))
        self.assertEqual(v, {"id": "123"})  # an id stays a string
        out = json.loads(p.stdout)
        self.assertEqual(out["issue"]["secretValue"], "[redacted secret]")

    def test_run_read_applies_schema_defaults_and_json_args(self):
        self.board(
            "run",
            "linear_list_issues",
            "--args-json",
            '{"filter": {"team": {"key": {"eq": "AFC"}}}}',
            "--arg",
            "first=3",
            "--selection",
            "nodes { id }",
        )
        ((_, q, v),) = self.mock.api_requests
        self.assertEqual(
            v, {"first": 3, "filter": {"team": {"key": {"eq": "AFC"}}}, "includeArchived": False}
        )
        self.assertIn(
            "issues(first: $first, filter: $filter, includeArchived: $includeArchived) { nodes { id } }",
            q,
        )

    def test_run_refuses_bad_input_before_any_request(self):
        for args, msg in (
            (["get_issue"], "argument 'id' is required"),
            (["get_issue", "--arg", "id=1", "--arg", "bogus=1"], "unknown argument 'bogus'"),
            (["list_issues", "--arg", "first=500"], "argument 'first' must be at most 100"),
            (
                ["list_issues", "--args-json", '{"filter": {"ageTime": {"eq": 1}}}'],
                "restricted field (IssueFilter.ageTime)",
            ),
            (
                ["get_issue", "--arg", "id=1", "--selection", "id } } mutation { x"],
                "selection must contain",
            ),
            (["get_issue", "--arg", "id=1", "--idempotency-key", "k"], "only for writes"),
            (["nope"], "no named Linear command"),
        ):
            with self.subTest(args=args):
                p = self.board("run", *args, code=2)
                self.assertIn(msg, p.stderr)
        self.assertEqual(self.mock.ops, [])
        self.assertEqual(self.mock.mints, 0)

    def test_run_write_gate_seat_and_key(self):
        args = (
            "run",
            "create_comment",
            "--args-json",
            '{"input": {"issueId": "i", "body": "hello"}}',
            "--idempotency-key",
            "k1",
        )
        p = self.board(*args, drop=("AGENT_SEAT",), code=6)
        self.assertIn("`board run create_comment` is a write", p.stderr)
        p = self.board(*args, drop=("AGENT_SEAT",), code=6)
        self.assertIn("no verified seat", p.stderr)
        p = self.board(*args[:4], code=2)
        self.assertIn("--idempotency-key", p.stderr)
        self.assertEqual(self.mock.ops, [])

    def test_run_write_tags_the_body_and_never_sends_a_key_twice(self):
        args = (
            "run",
            "create_comment",
            "--args-json",
            '{"input": {"issueId": "i", "body": "hello"}}',
            "--idempotency-key",
            "k1",
            "--session",
            "6db9ee77-aaaa",
        )
        first = self.board(*args)
        ((op, q, v),) = self.mock.api_requests
        self.assertTrue(q.startswith("mutation AgentSyncNamed($input: CommentCreateInput!)"))
        self.assertEqual(v["input"]["body"], "[CLAUDE·6db9ee77] hello")
        second = self.board(*args)
        self.assertEqual(
            len(self.mock.api_requests), 1, "a retry with the same key sent the write again"
        )
        self.assertEqual(json.loads(second.stdout), json.loads(first.stdout))
        self.assertIn("not sending it again", second.stderr)
        p = self.board(
            "run",
            "create_comment",
            "--args-json",
            '{"input": {"issueId": "i", "body": "other"}}',
            "--idempotency-key",
            "k1",
            code=2,
        )
        self.assertIn("belongs to a different request", p.stderr)
        self.assertEqual(len(self.mock.api_requests), 1)
        record = self.tmp / "cache" / "Fleet.linear-api.json"
        self.assertEqual(oct(record.stat().st_mode & 0o777), "0o600")

    def test_an_unknown_outcome_is_never_replayed(self):
        self.mock.http500_ops["AgentSyncNamed"] = 1
        args = ("run", "archive_issue", "--arg", "id=OPS-10", "--idempotency-key", "arch-1")
        p = self.board(*args, code=1)
        self.assertIn("may have reached Linear", p.stderr)
        self.assertEqual(self.parity_ops(), ["AgentSyncNamed"])  # one attempt, never retried
        p = self.board(*args, code=1)
        self.assertIn("unknown outcome", p.stderr)
        self.assertEqual(self.parity_ops(), ["AgentSyncNamed"])
        self.board("run", "archive_issue", "--arg", "id=OPS-10", "--idempotency-key", "arch-2")
        self.assertEqual(self.parity_ops(), ["AgentSyncNamed", "AgentSyncNamed"])

    def test_a_definitive_refusal_frees_the_key(self):
        self.mock.limit_ops["AgentSyncNamed"] = 1  # 400 RATELIMITED:  certainly not applied
        args = ("run", "archive_issue", "--arg", "id=OPS-10", "--idempotency-key", "arch-1")
        p = self.board(*args, code=5)
        self.assertIn("Not applied", p.stderr)
        self.board(*args)
        self.assertEqual(self.parity_ops(), ["AgentSyncNamed"])

    def test_claim_labels_are_protected(self):
        claimed = self.mock.lid("claimed:CODEX")
        p = self.board(
            "run",
            "update_issue",
            "--args-json",
            json.dumps({"id": "OPS-11", "input": {"removedLabelIds": [claimed]}}),
            "--idempotency-key",
            "k",
            code=2,
        )
        self.assertIn("board claim", p.stderr)
        self.assertNotIn("AgentSyncNamed", self.parity_ops())
        ordinary = self.mock.lid("imported")
        self.board(
            "run",
            "update_issue",
            "--args-json",
            json.dumps({"id": "OPS-11", "input": {"addedLabelIds": [ordinary]}}),
            "--idempotency-key",
            "k2",
        )
        self.assertEqual(self.parity_ops(), ["AgentSyncNamed"])

    def test_writes_check_the_seat_apps_identity(self):
        cid, secret = "claude-app-client-id", "claude-app-client-secret-value"
        self.mock.add_client(cid, secret, "Codex")  # swapped credentials
        self.ops_put("CLAUDE_LINEAR_CLIENT_ID", cid)
        self.ops_put("CLAUDE_LINEAR_CLIENT_SECRET", secret)
        out = self.board(
            "run", "archive_issue", "--arg", "id=OPS-10", "--idempotency-key", "k", code=4
        )
        self.assertIn("refusing to write as CLAUDE", out.stderr)
        self.assertEqual(self.mock.api_requests, [])
        self.board("run", "get_issue", "--arg", "id=OPS-10")  # reads still work


class ApiCliTests(ParityCliTest):
    def test_api_query_and_vars(self):
        self.mock.api_data = {"viewer": {"id": "v1", "apiKey": "x"}}
        p = self.board(
            "api",
            "query",
            "query Q($n: Int) { viewer { id } }",
            "--vars",
            '{"n": 2}',
            drop=("AGENT_SEAT",),
        )
        self.assertEqual(
            json.loads(p.stdout), {"viewer": {"id": "v1", "apiKey": "[redacted secret]"}}
        )
        self.assertEqual(self.mock.api_requests[0][2], {"n": 2})
        doc = self.private("q.graphql", "query Q { viewer { id } }", mode=0o644)
        self.board("api", "query", f"@{doc}", "--json")
        self.assertEqual(len(self.mock.api_requests), 2)

    def test_api_refusals_send_nothing(self):
        cases = (
            (["query", "query A { a } query B { b }"], "exactly one operation", 2),
            (["query", 'mutation { issueArchive(id: "1") { success } }'], "must be a query", 2),
            (
                [
                    "mutation",
                    "mutation { apiKeyCreate(input: {}) { success } }",
                    "--idempotency-key",
                    "k",
                ],
                "protected",
                2,
            ),
            (
                [
                    "mutation",
                    "mutation { organizationDelete { success } }",
                    "--idempotency-key",
                    "k",
                ],
                "protected",
                2,
            ),
            (
                ["mutation", "mutation { issueBulkUpdate { success } }", "--idempotency-key", "k"],
                "bulk",
                2,
            ),
            (["query", "{ " + "a { " * 17 + "id" + " }" * 17 + " }"], "depth exceeds 16", 2),
            (["query", "{ viewer { id }"], "is invalid", 2),
            (["query", "{ viewer { id } }", "--vars", "[1]"], "must be a JSON object", 2),
            (
                ["mutation", 'mutation { issueArchive(id: "1") { success } }'],
                "--idempotency-key",
                2,
            ),
        )
        for args, msg, code in cases:
            with self.subTest(args=args):
                p = self.board("api", *args, code=code)
                self.assertIn(msg, p.stderr)
        self.assertEqual(self.mock.ops, [])

    def test_api_mutation_is_gated_and_idempotent(self):
        doc = "mutation M($input: CommentCreateInput!) { commentCreate(input: $input) { success } }"
        args = (
            "api",
            "mutation",
            doc,
            "--vars",
            '{"input": {"issueId": "i", "body": "hey"}}',
            "--idempotency-key",
            "m1",
        )
        self.board(*args, drop=("AGENT_SEAT",), code=6)
        self.board(*args)
        self.board(*args)
        self.assertEqual(self.parity_ops(), ["M"])
        self.assertEqual(self.mock.api_requests[0][2]["input"]["body"], "[CLAUDE·5e55105a] hey")
        literal = 'mutation { commentCreate(input: { issueId: "i", body: "inline \\"quoted\\"" }) { success } }'
        self.board("api", "mutation", literal, "--idempotency-key", "m2")
        sent = self.mock.api_requests[-1][1]
        self.assertIn('body: "[CLAUDE\\u00b75e55105a] inline \\"quoted\\""', sent)

    def test_api_schema(self):
        p = self.board("api", "schema", "Issue", drop=("AGENT_SEAT",))
        self.assertIn("Issue  (OBJECT)  4 fields", p.stdout)
        self.assertIn("  id: ID!", p.stdout)
        self.assertIn("  comments(first: Int): CommentConnection", p.stdout)
        p = self.board("api", "schema", "Issue", "--field", "tit", "--json")
        self.assertEqual([f["name"] for f in json.loads(p.stdout)["fields"]], ["title"])
        p = self.board("api", "schema", "Nope", code=2)
        self.assertIn("no type named", p.stderr)


class McpCliTests(ParityCliTest):
    def calls(self):
        return [m.get("method") for _, m in self.mock.mcp_requests]

    def test_read_tool_over_sse(self):
        p = self.board(
            "mcp",
            "call",
            "search_documentation",
            "--args-json",
            '{"query": "cycles"}',
            drop=("AGENT_SEAT",),
        )
        self.assertEqual(p.stdout.strip(), "Cycles group issues into time boxes.")
        self.assertEqual(self.calls(), ["initialize", "notifications/initialized", "tools/call"])
        init_h, init = self.mock.mcp_requests[0]
        self.assertEqual(init_h["accept"], "application/json, text/event-stream")
        self.assertIn(
            init_h["authorization"][7:], self.mock.tokens
        )  # the same app token as GraphQL
        self.assertEqual(init["params"]["protocolVersion"], "2025-03-26")
        call_h, call = self.mock.mcp_requests[2]
        self.assertEqual(call_h["mcp-session-id"], "session-abc")
        self.assertEqual(call_h["mcp-protocol-version"], "2025-03-26")
        self.assertEqual(
            call["params"],
            {"name": "search_documentation", "arguments": {"query": "cycles", "page": 0}},
        )

    def test_read_tool_over_json(self):
        self.mock.mcp_mode = "json"
        self.mock.mcp_result = {
            "content": [
                {"type": "text", "text": "diff"},
                {"type": "image", "mimeType": "image/png", "data": "aGVsbG8="},
            ]
        }
        p = self.board("mcp", "call", "linear_get_diff", "--args-json", '{"urlOrId": "AFC-1"}')
        self.assertIn("diff", p.stdout)
        self.assertIn("[image image/png", p.stdout)
        p = self.board(
            "mcp", "call", "linear_get_diff", "--args-json", '{"urlOrId": "AFC-1"}', "--json"
        )
        self.assertEqual(json.loads(p.stdout)["content"][1]["data"], "aGVsbG8=")

    def test_max_chars_counts_like_the_hosted_server(self):
        # JSON.stringify(texts).length:  compact, and non-ASCII is one character, not \\uXXXX.
        self.mock.mcp_mode = "json"
        text = "é" * 1200  # --max-chars is at least 1000
        self.mock.mcp_result = {"content": [{"type": "text", "text": text}]}
        hosted_length = len(json.dumps([text], ensure_ascii=False, separators=(",", ":")))
        p = self.board(
            "mcp",
            "call",
            "linear_get_diff",
            "--args-json",
            '{"urlOrId": "AFC-1"}',
            "--max-chars",
            str(hosted_length),
        )
        self.assertEqual(p.stdout.strip(), text)
        p = self.board(
            "mcp",
            "call",
            "linear_get_diff",
            "--args-json",
            '{"urlOrId": "AFC-1"}',
            "--max-chars",
            str(hosted_length - 1),
            code=1,
        )
        self.assertIn("exceeds --max-chars", p.stderr)

    def test_tool_errors(self):
        self.mock.mcp_result = {
            "isError": True,
            "content": [{"type": "text", "text": "Diff not found"}],
        }
        p = self.board("mcp", "call", "get_diff", "--args-json", '{"urlOrId": "AFC-1"}', code=1)
        self.assertIn("Diff not found", p.stderr)

    def test_arguments_are_checked_against_the_catalog_first(self):
        for args, msg in (
            (["get_diff", "--args-json", "{}"], "argument 'urlOrId' is required"),
            (
                ["get_diff", "--args-json", '{"urlOrId": "x", "extra": 1}'],
                "unknown argument 'extra'",
            ),
            (["list_diffs", "--args-json", '{"orderBy": "sideways"}'], "must be one of"),
            (
                ["update_diff", "--args-json", '{"urlOrId": "x"}', "--idempotency-key", "k"],
                "at least one diff update",
            ),
            (
                [
                    "delete_diff_comment",
                    "--args-json",
                    '{"commentId": "c", "draftId": "d"}',
                    "--idempotency-key",
                    "k",
                ],
                "exactly one commentId or draftId",
            ),
            (
                ["save_diff_comment", "--args-json", '{"urlOrId": "x", "body": "b"}'],
                "--idempotency-key",
            ),
            (["nope"], "no official Linear MCP tool"),
        ):
            with self.subTest(args=args):
                p = self.board("mcp", "call", *args, code=2)
                self.assertIn(msg, p.stderr)
        self.assertEqual(self.mock.mcp_requests, [])

    def test_write_tool_is_gated_tagged_and_idempotent(self):
        body = json.dumps({"urlOrId": "AFC-1", "decision": "commented", "body": "Looks good."})
        args = ("mcp", "call", "submit_diff_review", "--args-json", body, "--idempotency-key", "r1")
        p = self.board(*args, drop=("AGENT_SEAT",), code=6)
        self.assertIn("`board mcp call submit_diff_review` is a write", p.stderr)
        self.mock.mcp_result = {"content": [{"type": "text", "text": "Review submitted."}]}
        p = self.board(*args)
        self.assertIn("Review submitted.", p.stdout)
        sent = self.mock.mcp_requests[2][1]["params"]["arguments"]
        self.assertEqual(
            sent,
            {"urlOrId": "AFC-1", "decision": "commented", "body": "[CLAUDE·5e55105a] Looks good."},
        )
        p = self.board(*args)
        self.assertIn("Review submitted.", p.stdout)
        self.assertEqual(
            self.calls().count("tools/call"), 1, "a retry with the same key called the tool again"
        )

    def test_mcp_url_is_loopback_or_linear_only(self):
        p = self.board(
            "mcp",
            "call",
            "get_diff",
            "--args-json",
            '{"urlOrId": "x"}',
            extra={"LINEAR_BOARD_MCP_URL": "https://evil.example/mcp"},
            code=2,
        )
        self.assertIn("refusing", p.stderr)


class ConvenienceTests(BoardTest):
    """The hosted linear_list team and env filters, and linear_claim's optional comment."""

    def test_list_by_team_and_env(self):
        rows = json.loads(self.board("list", "--team", "st", "--json").stdout)
        self.assertEqual([r["identifier"] for r in rows], ["ST-1"])
        rows = json.loads(self.board("list", "--env", "mac", "--json").stdout)
        self.assertEqual([r["identifier"] for r in rows], ["OPS-11"])
        rows = json.loads(self.board("list", "--team", "OPS", "--env", "cloud", "--json").stdout)
        self.assertEqual(rows, [])
        p = self.board("list", "--team", "a b", code=2)
        self.assertIn("not a team key", p.stderr)

    def test_claim_with_a_comment(self):
        self.board("claim", "OPS-10", "--by", "CLAUDE", "--comment", "Taking the digest fix.")
        body = self.comments_of(self.issue_by_ident("OPS-10"))[-1]
        self.assertTrue(
            body.startswith("[CLAUDE·5e55105a] CLAIMED\n\nTaking the digest fix."), body
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
