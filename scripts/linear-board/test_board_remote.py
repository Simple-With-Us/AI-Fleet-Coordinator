#!/usr/bin/env python3
"""Tests for the opt-in remote read transport of scripts/linear-board/board (SIMPLE_BOARD_REMOTE=1).

Hermetic: the one network function is replaced in process, so no test reaches the hosted endpoint, Linear or any
credential store.  Run:  python3 scripts/linear-board/test_board_remote.py
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import unittest
import unittest.mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
_loader = importlib.machinery.SourceFileLoader("board_remote_under_test", str(HERE / "board"))
_spec = importlib.util.spec_from_loader(_loader.name, _loader)
board = importlib.util.module_from_spec(_spec)
_loader.exec_module(board)

BEARER = "sbt_codex_" + "A" * 43


def rpc(structured, *, is_error=False, sse=False, text=None):
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "structuredContent": structured,
            "isError": is_error,
            "content": [{"type": "text", "text": text}] if text else [],
        },
    }
    body = json.dumps(msg)
    if sse:
        return 200, "text/event-stream", ("event: message\ndata: " + body + "\n\n").encode()
    return 200, "application/json", body.encode()


class Remote(unittest.TestCase):
    def run_cli(self, argv, reply, env=None):
        calls = []

        def fake(url, headers, body, timeout):
            calls.append((url, headers, json.loads(body)))
            return reply

        out, err = io.StringIO(), io.StringIO()
        full = {"SIMPLE_BOARD_REMOTE": "1", "SIMPLE_TOOLS_BEARER": BEARER}
        full.update(env or {})
        code = 0
        with (
            unittest.mock.patch.dict(os.environ, full, clear=True),
            unittest.mock.patch.object(board, "_remote_post", fake),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            try:
                code = board.main(argv)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue(), calls

    def test_whoami_goes_to_the_fixed_endpoint_with_the_bearer_only_in_the_header(self):
        who = {
            "seat": "CODEX",
            "app": "Codex",
            "viewer_id": "v1",
            "workspace": "w",
            "transport": "hosted",
        }
        code, out, err, calls = self.run_cli(["whoami"], rpc(who))
        self.assertEqual(code, 0, err)
        url, headers, msg = calls[0]
        self.assertEqual(url, "https://agents.simplewithus.com/mcp")
        self.assertEqual(headers["Authorization"], "Bearer " + BEARER)
        self.assertEqual(msg["params"], {"name": "linear_whoami", "arguments": {}})
        self.assertIn("seat: CODEX", out)
        self.assertNotIn(BEARER, out + err + json.dumps(msg))

    LIST_TEXT = "agent-sync linear_list\nAFC-12 | Fix the thing | In Progress | P1\nAFC-13 | Other | Todo | P2"
    SHOW_TEXT = (
        "agent-sync linear_show: AFC-12\ndescription: the full body\nlabels: Kind/bug\n"
        "comment by CODEX: the comment body\nduplicate_of: AFC-9"
    )

    def test_list_prints_the_hosted_issue_rows_not_just_the_summary(self):
        # Real hosted shape: structuredContent is only {count, ids}; the rows are in the text content.
        code, out, err, calls = self.run_cli(
            ["list", "--team", "AFC", "--status", "open", "--limit", "5"],
            rpc({"count": 2, "ids": ["AFC-12", "AFC-13"]}, sse=True, text=self.LIST_TEXT),
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            calls[0][2]["params"],
            {"name": "linear_list", "arguments": {"team": "AFC", "status": "open", "limit": 5}},
        )
        self.assertIn("AFC-12 | Fix the thing", out)
        self.assertIn("AFC-13 | Other", out)

    def test_list_json_keeps_both_parts(self):
        code, out, _, _ = self.run_cli(
            ["list", "--json"], rpc({"count": 1, "ids": ["AFC-1"]}, text=self.LIST_TEXT)
        )
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data["structured"], {"count": 1, "ids": ["AFC-1"]})
        self.assertIn("Fix the thing", data["text"])

    def test_list_env_is_refused_because_the_hosted_tool_ignores_it(self):
        code, _, err, calls = self.run_cli(["list", "--env", "Mac"], rpc({"count": 0, "ids": []}))
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])

    def test_show_passes_id_and_comment_limit_and_keeps_the_text_content(self):
        code, out, err, calls = self.run_cli(
            ["show", "AFC-12", "--comment-limit", "3"],
            rpc(
                {
                    "identifier": "AFC-12",
                    "title": "t",
                    "state": "s",
                    "team": "AFC",
                    "description": "d",
                },
                text=self.SHOW_TEXT,
            ),
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(
            calls[0][2]["params"],
            {"name": "linear_show", "arguments": {"id": "AFC-12", "comment_limit": 3}},
        )
        for needle in ("the comment body", "Kind/bug", "duplicate_of: AFC-9", "the full body"):
            self.assertIn(needle, out)

    W = {"SIMPLE_BOARD_REMOTE_WRITES": "1"}

    def seq(self, *answers):
        it = iter(answers)
        return it

    def run_writes(self, argv, answers, env=None):
        calls, it = [], iter(answers)

        def fake(url, headers, body, timeout):
            calls.append(json.loads(body)["params"])
            return next(it)

        out, err = io.StringIO(), io.StringIO()
        full = {"SIMPLE_BOARD_REMOTE": "1", "SIMPLE_TOOLS_BEARER": BEARER, **self.W, **(env or {})}
        with (
            unittest.mock.patch.dict(os.environ, full, clear=True),
            unittest.mock.patch.object(board, "_remote_post", fake),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            try:
                code = board.main(argv)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue(), calls

    WHO = {"seat": "CODEX", "authenticated_seat": "CODEX", "app": "Codex"}

    def test_writes_need_their_own_switch(self):
        code, _, err, calls = self.run_cli(
            ["comment", "AFC-1", "--by", "CODEX", "--text", "x"], rpc({})
        )
        self.assertEqual((code, calls), (2, []))
        self.assertIn("SIMPLE_BOARD_REMOTE_WRITES", err)

    def test_comment_claim_status_file_map_to_hosted_tools_after_the_seat_check(self):
        cases = [
            (
                ["comment", "AFC-1", "--by", "codex", "--text", "hi"],
                "linear_comment",
                {"id": "AFC-1", "body": "hi"},
            ),
            (
                ["claim", "AFC-1", "--by", "CODEX", "--comment", "on it"],
                "linear_claim",
                {"id": "AFC-1", "comment": "on it"},
            ),
            (
                ["status", "AFC-1", "done", "--by", "CODEX"],
                "linear_status",
                {"id": "AFC-1", "status": "done"},
            ),
            (
                ["status", "AFC-1", "duplicate", "--of", "AFC-2"],
                "linear_status",
                {"id": "AFC-1", "status": "duplicate", "of": "AFC-2"},
            ),
            (
                [
                    "file",
                    "--title",
                    "T",
                    "--app",
                    "fleet-infra",
                    "--severity",
                    "P2",
                    "--desc",
                    "d",
                    "--label",
                    "needs-owner",
                ],
                "linear_file",
                {
                    "title": "T",
                    "app": "fleet-infra",
                    "kind": "agent-report",
                    "severity": "P2",
                    "description": "d",
                    "label": ["needs-owner"],
                },
            ),
        ]
        for argv, tool, args in cases:
            with self.subTest(argv=argv):
                answers = ([rpc(self.WHO)] if "--by" in argv else []) + [
                    rpc({"id": "1"}, text="done")
                ]
                code, out, err, calls = self.run_writes(argv, answers)
                self.assertEqual(code, 0, err)
                self.assertEqual(calls[-1], {"name": tool, "arguments": args})

    def test_by_that_is_not_the_bearers_seat_is_refused_before_any_write(self):
        code, _, err, calls = self.run_writes(
            ["comment", "AFC-1", "--by", "ECHO", "--text", "x"], [rpc(self.WHO)]
        )
        self.assertEqual(code, 2)
        self.assertEqual([c["name"] for c in calls], ["linear_whoami"])

    def test_by_mismatch_names_the_seat_the_server_reported(self):
        _, _, err, _ = self.run_writes(
            ["comment", "AFC-1", "--by", "ECHO", "--text", "x"], [rpc(self.WHO)]
        )
        self.assertIn("(CODEX)", err)

    def test_a_credential_shaped_comment_is_refused_before_any_write(self):
        code, _, err, calls = self.run_writes(
            ["comment", "AFC-1", "--by", "CODEX", "--text", "token ghp_" + "A1b2C3d4E5" * 4],
            [rpc(self.WHO)],
        )
        self.assertEqual([c["name"] for c in calls], ["linear_whoami"])
        self.assertIn("secret_scan:body", err)
        self.assertNotIn("ghp_", err)

    def test_show_with_no_text_prints_the_structured_result_and_text_is_escaped(self):
        code, out, _, _ = self.run_cli(["show", "AFC-1"], rpc({"id": "AFC-1"}))
        self.assertEqual(code, 0)
        self.assertIn('"id": "AFC-1"', out)
        code, out, _, _ = self.run_cli(["show", "AFC-1"], rpc({"id": "AFC-1"}, text="a\x1b[2Jb"))
        self.assertNotIn("\x1b", out)
        self.assertIn("\\x1b", out)

    def test_write_options_with_no_hosted_equivalent_are_refused(self):
        for argv in (
            ["claim", "AFC-1", "--by", "CODEX", "--steal", "--reason", "r"],
            ["comment", "AFC-1", "--by", "CODEX", "--text-file", "-"],
            ["status", "AFC-1", "done", "--resolution", "fixed"],
            ["file", "--title", "T", "--fix", "f"],
            ["file", "--title", "T", "--by", "X"],
        ):
            with self.subTest(argv=argv):
                code, _, _, calls = self.run_writes(argv, [rpc(self.WHO)] * 2)
                self.assertEqual(code, 2)
                self.assertNotIn("linear_comment", [c["name"] for c in calls])

    def test_remote_with_mac_collab_backend_fails_closed_without_exec_or_call(self):
        with unittest.mock.patch.object(board.os, "execv") as ex:
            code, _, err, calls = self.run_cli(
                ["whoami"], rpc({}), env={"BOARD_BACKEND": "mac-collab"}
            )
        ex.assert_not_called()
        self.assertEqual(code, 2)
        self.assertEqual(calls, [])

    def test_flags_with_no_hosted_equivalent_refuse(self):
        for argv in (
            ["list", "--search", "x"],
            ["list", "--mine", "CODEX"],
            ["list", "--app", "a"],
            ["list", "--label", "bug"],
        ):
            with self.subTest(argv=argv):
                code, _, err, calls = self.run_cli(argv, rpc({}))
                self.assertEqual(code, board.EXIT_USAGE)
                self.assertEqual(calls, [])

    def test_every_other_verb_is_refused_not_run_directly(self):
        for argv in (
            ["stats"],
            ["label", "add", "AFC-1", "x"],
            ["api", "query", "{ viewer { id } }"],
        ):
            with self.subTest(argv=argv):
                code, _, err, calls = self.run_cli(argv, rpc({}))
                self.assertEqual(code, board.EXIT_USAGE)
                self.assertIn("not available in remote mode", err)
                self.assertEqual(calls, [])

    def test_missing_or_malformed_bearer_dies_with_no_call_and_no_direct_fallback(self):
        for bearer in (None, "short", "has space " + "A" * 30):
            with self.subTest(bearer=bearer):
                env = {"SIMPLE_TOOLS_BEARER": bearer} if bearer else {}
                code, _, err, calls = self.run_cli(
                    ["whoami"], rpc({}), env=env if bearer else {"SIMPLE_TOOLS_BEARER": ""}
                )
                self.assertEqual(code, board.EXIT_NO_CREDS)
                self.assertEqual(calls, [])

    def test_refusal_and_server_errors_never_fall_back_or_echo_the_bearer(self):
        code, out, err, _ = self.run_cli(["whoami"], (401, "application/json", b""))
        self.assertEqual(code, board.EXIT_AUTH)
        self.assertNotIn(BEARER, out + err)
        code, _, err, _ = self.run_cli(["whoami"], rpc({"code": "not_configured"}, is_error=True))
        self.assertEqual(code, board.EXIT_ERROR)
        self.assertIn("not_configured", err)
        code, _, err, _ = self.run_cli(["whoami"], (200, "application/json", b"not json"))
        self.assertEqual(code, board.EXIT_ERROR)

    def test_the_endpoint_has_no_environment_override_and_is_https(self):
        self.assertTrue(board.REMOTE_MCP_URL.startswith("https://agents.simplewithus.com/"))
        for name in ("LINEAR_BOARD_MCP_URL", "SIMPLE_BOARD_REMOTE_URL", "SIMPLE_TOOLS_URL"):
            code, _, _, calls = self.run_cli(
                ["whoami"], rpc({"seat": "X"}), env={name: "http://127.0.0.1:1/mcp"}
            )
            self.assertEqual(calls[0][0], "https://agents.simplewithus.com/mcp")

    def test_without_the_switch_the_remote_path_is_not_taken(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(board.remote_selected())



class NativeEcho(unittest.TestCase):
    def test_native_transport_uses_fixed_custody_and_no_secret_environment(self):
        reply = b'{"jsonrpc":"2.0","id":0,"result":{"protocolVersion":"2025-06-18"}}\n{"jsonrpc":"2.0","id":1,"result":{"structuredContent":{"seat":"ECHO"},"content":[]}}\n'
        calls = []
        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return type("Reply", (), {"returncode":0, "stdout":reply})()
        with unittest.mock.patch.dict(os.environ, {"SIMPLE_BOARD_NATIVE_ECHO":"1", "SIMPLE_TOOLS_BEARER":"must-not-export"}, clear=True), unittest.mock.patch.object(board.os,"geteuid",return_value=0), unittest.mock.patch.object(board.subprocess,"run",run):
            result = board.remote_call("linear_whoami", {})
        self.assertEqual(result["structured"]["seat"],"ECHO")
        self.assertEqual(calls[0][0], ["/usr/bin/python3","-I","/usr/local/libexec/assigned-echo-mcp/linux-custody.py"])
        self.assertEqual(calls[0][1]["env"], {"PATH":"/usr/bin:/bin"})
        messages=[json.loads(x) for x in calls[0][1]["input"].splitlines()]
        self.assertEqual([x["method"] for x in messages], ["initialize","notifications/initialized","tools/call"])
        self.assertNotIn("must-not-export", str(calls))

    def test_native_failure_is_unknown_for_write_and_does_not_print_stderr(self):
        fake=type("Reply", (), {"returncode":3,"stdout":b"", "stderr":b"private-detail"})()
        out=io.StringIO()
        with unittest.mock.patch.object(board.os,"geteuid",return_value=0), unittest.mock.patch.object(board.subprocess,"run",return_value=fake), contextlib.redirect_stderr(out), self.assertRaises(SystemExit):
            board._native_echo_post(b'{}',True)
        self.assertIn("outcome unknown",out.getvalue())
        self.assertNotIn("private-detail",out.getvalue())

    def test_canonical_simple_board_cli_runs_native_whoami_and_write(self):
        import runpy
        import subprocess
        import sys
        def run(argv, **kwargs):
            call = json.loads(kwargs["input"].splitlines()[-1])
            name = call["params"]["name"]
            result = {"seat":"ECHO", "app":"FleetOPS"} if name == "linear_whoami" else {"ok":True}
            response = {"jsonrpc":"2.0", "id":1, "result":{"structuredContent":result,"content":[]}}
            raw = json.dumps({"jsonrpc":"2.0","id":0,"result":{"protocolVersion":"2025-06-18"}}).encode()+b"\n"+json.dumps(response).encode()+b"\n"
            return type("Reply", (), {"returncode":0,"stdout":raw})()
        entry = str(HERE.parent / "simple-board")
        for args in (["whoami"], ["comment","AFC-1","--text","Offline fixture", "--by","ECHO"]):
            output=io.StringIO()
            with unittest.mock.patch.dict(os.environ,{"SIMPLE_BOARD_NATIVE_ECHO":"1","SIMPLE_BOARD_REMOTE_WRITES":"1"},clear=True), unittest.mock.patch.object(os,"geteuid",return_value=0), unittest.mock.patch.object(subprocess,"run",run), unittest.mock.patch.object(sys,"argv",[entry,*args]), unittest.mock.patch.object(sys,"version_info",(3,11,0)), contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit:
                runpy.run_path(entry,run_name="__main__")
            self.assertEqual(exit.exception.code,0)
            self.assertTrue(output.getvalue())

    def test_native_nonroot_refuses_before_spawn(self):
        with unittest.mock.patch.object(board.os,"geteuid",return_value=501), unittest.mock.patch.object(board.subprocess,"run") as run, contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            board._native_echo_post(b'{}',False)
        run.assert_not_called()

if __name__ == "__main__":
    unittest.main()
