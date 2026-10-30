"""Tests for the opt-in remote read transport of `simple-chat` (SIMPLE_CHAT_REMOTE=1).

Hermetic: the one network function is replaced in process, HOME is a temp dir, and no Zulip key, rc file or
network is read.  Run:  cd scripts && python3 -m unittest agent_sync.tests.test_remote_read
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_sync import cli

BEARER = "sbt_codex_" + "C" * 43


def reply(structured, text="ok text"):
    msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "structuredContent": structured,
            "isError": False,
            "content": [{"type": "text", "text": text}],
        },
    }
    return 200, "application/json", json.dumps(msg).encode()


class RemoteRead(unittest.TestCase):
    def run_cli(self, argv, answer, env=None, prog="simple-chat"):
        calls = []

        def fake(url, headers, body, timeout):
            calls.append((url, headers, json.loads(body)))
            return answer(calls[-1]) if callable(answer) else answer

        full = {"SIMPLE_CHAT_REMOTE": "1", "SIMPLE_TOOLS_BEARER": BEARER}
        full.update(env or {})
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as home, mock.patch.object(cli, "_remote_post", fake):
            code = cli.main(
                argv,
                prog=prog,
                env=full,
                stdout=out,
                stderr=err,
                stdin=io.StringIO(),
                home=Path(home),
            )
        return code, out.getvalue(), err.getvalue(), calls

    def test_reads_map_to_hosted_tools_over_the_fixed_endpoint(self):
        cases = [
            (["whoami"], "whoami", {}),
            (["channels", "--all"], "zulip_channels", {"all": True}),
            (
                ["topics", "--channel", "work-in-progress", "--limit", "5"],
                "zulip_topics",
                {"channel": "work-in-progress", "limit": 5},
            ),
            (
                ["read", "--topic", "t1", "--channel", "sandbox", "--since", "9", "--limit", "4"],
                "zulip_read_topic",
                {"channel": "sandbox", "topic": "t1", "limit": 4, "since_id": 9},
            ),
            (["inbox", "--peek", "--limit", "3"], "zulip_inbox", {"limit": 3}),
        ]
        for argv, tool, args in cases:
            with self.subTest(argv=argv):
                code, out, err, calls = self.run_cli(argv, reply({"seat": "CODEX"}))
                self.assertEqual(code, 0, err)
                url, headers, msg = calls[0]
                self.assertEqual(url, "https://agents.simplewithus.com/mcp")
                self.assertEqual(headers["Authorization"], "Bearer " + BEARER)
                self.assertEqual(msg["params"], {"name": tool, "arguments": args})
                self.assertIn("ok text", out)
                self.assertNotIn(BEARER, out + err)

    def test_identity_flags_and_local_only_options_are_refused_without_a_call(self):
        for argv in (
            ["--as", "ECHO", "whoami"],
            ["whoami", "--as", "ECHO"],
            ["whoami", "--default-seat", "ECHO"],
            ["read", "--topic", "t", "--new"],
            ["inbox"],
            ["inbox", "--peek", "--local"],
        ):
            with self.subTest(argv=argv):
                code, _, err, calls = self.run_cli(argv, reply({}))
                self.assertEqual(code, 2)
                self.assertEqual(calls, [])

    def test_writes_and_other_verbs_are_refused_not_run_directly(self):
        for argv in (
            ["listen"],
            ["wait", "--topic", "t"],
            ["listener", "status"],
            ["verify-owner-msg", "1"],
        ):
            with self.subTest(argv=argv):
                code, _, err, calls = self.run_cli(argv, reply({}))
                self.assertEqual(calls, [])
                self.assertNotEqual(code, 0)

    W = {"SIMPLE_CHAT_REMOTE_WRITES": "1"}

    def test_writes_need_their_own_switch_and_never_run_directly(self):
        for argv in (
            ["post", "--topic", "t", "hello"],
            ["reply", "--id", "5", "hi"],
            ["react", "--id", "5", "eyes"],
            ["dm", "--to", "7", "hi"],
        ):
            with self.subTest(argv=argv):
                code, _, err, calls = self.run_cli(argv, reply({}))
                self.assertEqual((code, calls), (2, []))
                self.assertIn("SIMPLE_CHAT_REMOTE_WRITES", err)

    @staticmethod
    def full(default, allowed=None, **over):
        """The shape delegated-identities.js `identities` returns."""
        rows = [
            {"identity": n, "zulip": "configured", "linear": "missing", "recall": "configured"}
            for n in (allowed or [default])
        ]
        return {
            "default_identity": default,
            "authenticated_seat": default,
            "transport": "hosted",
            "identities": rows,
            **over,
        }

    @staticmethod
    def by_tool(structured, default="CODEX", identities=None):
        def answer(call):
            if call[2]["params"]["name"] == "identities":
                if identities is not None:
                    return identities
                return reply(RemoteRead.full(default))
            return reply(structured)

        return answer

    def test_a_write_names_only_the_identity_the_server_reports_as_the_bearers_default(self):
        code, _, err, calls = self.run_cli(
            ["post", "--topic", "t", "hi"], self.by_tool({"ok": True}, default="MC"), env=self.W
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(calls[1][2]["params"]["arguments"]["identity"], "MC")
        # reads never ask for it and never carry one
        code, _, _, calls = self.run_cli(["channels"], self.by_tool({"ok": True}))
        self.assertEqual([c[2]["params"]["name"] for c in calls], ["zulip_channels"])
        self.assertNotIn("identity", calls[0][2]["params"]["arguments"])

    def test_a_write_is_not_sent_unless_the_answer_matches_the_bearers_own_seat_and_allowed_set(
        self,
    ):
        full = self.full
        bad = [
            {},
            {"default_identity": None},
            full("../x"),
            full(5),
            full("CODEX", authenticated_seat="CLAUDE"),  # default is not the authenticated seat
            full("CODEX", authenticated_seat=None),
            full("CODEX", allowed=["CLAUDE"]),  # default missing from the allowed set
            full("CODEX", allowed=["CODEX", "CODEX"]),  # not exactly once
            full("CODEX", allowed=["codex"]),  # no case or alias normalization
            full("CODEX", identities="CODEX"),  # wrong type
            full("CODEX", identities=[{"identity": 7}]),
            full("CODEX", identities=["CODEX"]),
            {**full("CODEX"), "identities": [{"identity": "CODEX"}, "junk"]},
            {k: v for k, v in full("CODEX").items() if k != "identities"},
        ]
        for answer in bad:
            with self.subTest(answer=answer):
                code, _, err, calls = self.run_cli(
                    ["post", "--topic", "t", "hi"],
                    self.by_tool({"ok": True}, identities=reply(answer)),
                    env=self.W,
                )
                self.assertEqual(
                    (code, [c[2]["params"]["name"] for c in calls]), (2, ["identities"])
                )
                self.assertIn("did not match this bearer", err)

    def test_a_multi_identity_connection_writes_as_its_own_default_only(self):
        ans = reply(self.full("CODEX", allowed=["CODEX", "GB-FIXER"]))
        code, _, err, calls = self.run_cli(
            ["post", "--topic", "t", "hi"], self.by_tool({"ok": True}, identities=ans), env=self.W
        )
        self.assertEqual(code, 0, err)
        self.assertEqual(calls[1][2]["params"]["arguments"]["identity"], "CODEX")

    def test_any_failure_of_the_identities_call_stops_the_write_with_no_fallback(self):
        def rpc_error(code):
            body = {"jsonrpc": "2.0", "id": 1, "error": {"code": code, "message": "x"}}
            return (200, "application/json", json.dumps(body).encode())

        def tool_error(code):
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "isError": True,
                    "content": [{"type": "text", "text": json.dumps({"code": code})}],
                },
            }
            return (200, "application/json", json.dumps(body).encode())

        cases = [rpc_error(c) for c in (-32601, -32602, -32000, -32603, -32700)]
        cases += [
            tool_error(c) for c in ("unknown_tool", "paused", "rate_limited", "not_authorized")
        ]
        cases += [(502, "application/json", b"")]
        for ans in cases:
            with self.subTest(answer=ans):
                code, _, err, calls = self.run_cli(
                    ["post", "--topic", "t", "hi"],
                    self.by_tool({"ok": True}, identities=ans),
                    env=self.W,
                )
                self.assertEqual(
                    (code, [c[2]["params"]["name"] for c in calls]), (2, ["identities"])
                )

    def test_writes_map_to_the_hosted_zulip_tools(self):
        cases = [
            (
                [
                    "post",
                    "--topic",
                    "t1",
                    "--channel",
                    "sandbox",
                    "--to",
                    "CODEX",
                    "hello",
                    "there",
                ],
                "zulip_post",
                {"channel": "sandbox", "topic": "t1", "text": "hello there", "to": ["CODEX"]},
            ),
            (["reply", "--id", "42", "ok"], "zulip_reply", {"message_id": 42, "text": "ok"}),
            (["react", "--id", "42", "eyes"], "zulip_react", {"message_id": 42, "emoji": "eyes"}),
            (
                ["dm", "--to", "7", "--to", "a@b.co", "hi"],
                "zulip_dm_send",
                {"text": "hi", "user_ids": [7], "emails": ["a@b.co"]},
            ),
        ]
        for argv, tool, args in cases:
            with self.subTest(argv=argv):
                code, out, err, calls = self.run_cli(argv, self.by_tool({"ok": True}), env=self.W)
                self.assertEqual(code, 0, err)
                self.assertEqual([c[2]["params"]["name"] for c in calls], ["identities", tool])
                self.assertEqual(
                    calls[1][2]["params"],
                    {"name": tool, "arguments": {**args, "identity": "CODEX"}},
                )

    def test_a_non_ascii_digit_recipient_is_a_clean_refusal_not_a_traceback(self):
        code, _, err, calls = self.run_cli(["dm", "--to", "\u0663", "hi"], reply({}), env=self.W)
        self.assertEqual((code, calls), (2, []))
        self.assertNotIn("Traceback", err)

    def test_hosted_text_cannot_drive_the_terminal(self):
        code, out, err, _ = self.run_cli(["whoami"], reply({}, text="a\x1b]0;owned\x07b\x9bc"))
        self.assertEqual(code, 0, err)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertIn("\\x1b", out)

    def test_write_options_with_no_hosted_equivalent_are_refused(self):
        for argv in (
            ["post", "--topic", "t", "--fleet", "x"],
            ["post", "--topic", "t", "--quiet", "x"],
            ["post", "--topic", "t", "--no-tag", "x"],
            ["post", "--topic", "t", "--to-owner", "x"],
            ["post", "--topic", "t", "--body-file", "/x"],
            ["dm", "hi"],
            ["dm", "--owner", "hi"],
            ["dm", "--to", "CODEX", "hi"],
            ["post", "--topic", "t"],
        ):
            with self.subTest(argv=argv):
                code, _, _, calls = self.run_cli(argv, reply({}), env=self.W)
                self.assertEqual((code, calls), (2, []))

    def test_listener_hook_verbs_do_not_run_locally_in_remote_mode(self):
        with mock.patch("agent_sync.attach.main") as hook:
            code, _, err, calls = self.run_cli(["attach"], reply({}))
        hook.assert_not_called()
        self.assertEqual((code, calls), (2, []))

    def test_bearer_problems_and_refusals_never_fall_back(self):
        for env in ({"SIMPLE_TOOLS_BEARER": ""}, {"SIMPLE_TOOLS_BEARER": "short"}):
            code, _, err, calls = self.run_cli(["whoami"], reply({}), env=env)
            self.assertEqual(code, 2)
            self.assertEqual(calls, [])
        code, out, err, _ = self.run_cli(["whoami"], (401, "", b""))
        self.assertEqual(code, 2)
        self.assertNotIn(BEARER, out + err)

    def test_no_endpoint_override_and_switch_off_by_default(self):
        _, _, _, calls = self.run_cli(
            ["whoami"], reply({}), env={"SIMPLE_CHAT_URL": "http://127.0.0.1:1/mcp"}
        )
        self.assertEqual(calls[0][0], "https://agents.simplewithus.com/mcp")
        out, err = io.StringIO(), io.StringIO()
        with tempfile.TemporaryDirectory() as home, mock.patch.object(cli, "_remote_post") as post:
            cli.main(
                ["identities"], env={}, stdout=out, stderr=err, stdin=io.StringIO(), home=Path(home)
            )
        post.assert_not_called()

    def test_simple_notes_is_never_switched_to_remote(self):
        code, _, _, calls = self.run_cli(["whoami"], reply({}), prog="simple-notes")
        self.assertEqual(calls, [])


class TransportErrors(unittest.TestCase):
    def test_a_truncated_or_malformed_http_answer_is_a_clean_error(self):
        import http.client

        class Op:
            def open(self, *a, **k):
                raise http.client.IncompleteRead(b"x")

        with mock.patch.object(cli.urllib.request, "build_opener", return_value=Op()):
            with self.assertRaises(cli._TransportError) as ctx:
                cli._remote_post("https://agents.simplewithus.com/mcp", {}, b"{}", 1)
        self.assertTrue(ctx.exception.sent)


class EntrypointTrap(unittest.TestCase):
    """The real scripts/simple-chat file in a subprocess:  its own attach/detach fast path must not run."""

    SCRIPT = Path(__file__).resolve().parents[2] / "simple-chat"

    def run_entry(self, verb, remote):
        import os
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as home:
            env = {"PATH": os.environ.get("PATH", ""), "HOME": home}
            if remote:
                env["SIMPLE_CHAT_REMOTE"] = "1"
            # sitecustomize-free trap:  a sentinel file is written if agent_sync.attach is imported
            sentinel = Path(home) / "attach-imported"
            code = (
                "import sys, runpy, pathlib, importlib.abc\n"
                "class T(importlib.abc.MetaPathFinder):\n"
                "    def find_spec(self, name, path, target=None):\n"
                "        if name == 'agent_sync.attach':\n"
                f"            pathlib.Path({str(sentinel)!r}).write_text('x')\n"
                "            raise ImportError('trap')\n"
                "sys.meta_path.insert(0, T())\n"
                f"sys.argv = [{str(self.SCRIPT)!r}, {verb!r}]\n"
                f"runpy.run_path({str(self.SCRIPT)!r}, run_name='__main__')\n"
            )
            proc = subprocess.run(
                [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60
            )
            return proc.returncode, proc.stderr, sentinel.exists()

    def test_remote_mode_refuses_attach_and_detach_at_the_entrypoint_without_importing_the_hook(
        self,
    ):
        for verb in ("attach", "detach"):
            code, err, imported = self.run_entry(verb, remote=True)
            self.assertEqual(code, 2, err)
            self.assertFalse(imported)
            self.assertIn("remote mode", err)

    def test_without_remote_mode_the_fast_path_is_untouched(self):
        _, _, imported = self.run_entry("attach", remote=False)
        self.assertTrue(imported)


if __name__ == "__main__":
    unittest.main()
