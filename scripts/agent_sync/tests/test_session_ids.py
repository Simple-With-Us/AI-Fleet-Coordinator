"""AFC-361:  the session id a notes write carries (session_ids.py and every place that enforces it).

Covers the rule itself, the request object's field (notes.make_request and check_request), what run_direct hands the
script (`--session` and AGENT_SEAT), the CLI and the stdio tools on the Mac's direct path (refuse, exempt,
auto-detect, spoof).  The Operator's queue applies the same rule (notes.request_problem);  test_notes_queue.py covers
it there.  The #notes-relay transport and the Mac listener's refusal are retired (AFC-486).  The notes script is never
run for real:  the subprocess runner is faked.
"""

from __future__ import annotations

import os
import stat
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from agent_sync import notes as N
from agent_sync import session_ids as S
from agent_sync.tests import test_mcp_identity as TI
from agent_sync.tests.harness import SESSION, Harness

EXEMPT = ("GROK-BOT", "GB-FIXER", "MA", "JET", "WISE", "ECHO")
SID = "thread-id-0001"
SCRIPT = Path("/fake/apple-notes-coding.sh")


class FakeRun:
    """Stands in for the script's process:  records argv, stdin and env, and answers a created note."""

    def __init__(self, code: int = 0, stdout: str = "created note id=x-coredata://abc in folder Coding\n"):
        self.calls: list[dict] = []
        self.result = (code, stdout)
        self.html_mode = None

    def __call__(self, argv, **kwargs):
        if "--html" in argv:
            self.html_mode = stat.S_IMODE(os.stat(argv[argv.index("--html") + 1]).st_mode)
        self.calls.append({"argv": list(argv), "input": kwargs.get("input"), "env": kwargs.get("env")})
        code, out = self.result
        return subprocess.CompletedProcess(argv, code, out.encode(), b"")


class RuleTests(unittest.TestCase):
    def test_exempt_seats_are_the_single_thread_identities_after_canonical_names(self):
        for seat in EXEMPT + ("gb-oracle", "ROB", "INSTINCT", "Muse-Assist"):
            self.assertTrue(S.is_exempt(seat), seat)
        for seat in (
            "CLAUDE",
            "CODEX",
            "GROK",
            "GROK-WEB",
            "BF-DEPLOYER",
            "FX",
            "GB",
            "XGB-FIXER",
            "",
            None,
        ):
            self.assertFalse(S.is_exempt(seat), seat)

    def test_an_explicit_value_beats_the_environment_and_a_bad_one_is_an_error(self):
        env = {"AGENT_SESSION": "generic-id-0001", "CLAUDE_CODE_SESSION_ID": "claude-id-0001"}
        self.assertEqual(S.detect("CLAUDE", "explicit-id-01", env), "explicit-id-01")
        self.assertEqual(S.detect("CLAUDE", "  padded-id-01 ", {}), "padded-id-01")
        for bad in ("abc", "-dash", "has space", "x" * 129, "bad/char", "tab\there"):
            with self.assertRaises(ValueError, msg=bad):
                S.detect("CLAUDE", bad, env)
        with self.assertRaises(ValueError):
            S.require("CLAUDE", "abc", env)

    def test_agent_session_then_the_seats_own_variable_and_no_cross_platform_fallback_for_a_seat_with_an_entry(
        self,
    ):
        self.assertEqual(
            S.detect(
                "CLAUDE",
                None,
                {"AGENT_SESSION": "generic-id-0001", "CLAUDE_CODE_SESSION_ID": "claude-id-0001"},
            ),
            "generic-id-0001",
        )
        self.assertEqual(
            S.detect("CLAUDE", None, {"CLAUDE_CODE_SESSION_ID": "claude-id-0001"}), "claude-id-0001"
        )
        self.assertEqual(
            S.detect("CODEX", None, {"CODEX_THREAD_ID": "019a-thread-1"}), "019a-thread-1"
        )
        self.assertEqual(
            S.detect("CURSOR", None, {"CURSOR_AGENT_CHAT_ID": "chat-id-0002"}), "chat-id-0002"
        )
        self.assertEqual(
            S.detect(
                "CURSOR",
                None,
                {"CURSOR_CONVERSATION_ID": "conv-id-0001", "CURSOR_AGENT_CHAT_ID": "chat-id-0002"},
            ),
            "conv-id-0001",
        )
        # a CLAUDE seat with only CODEX_THREAD_ID is NOT given it:  CLAUDE has an entry, so there is no fallback
        self.assertIsNone(S.detect("CLAUDE", None, {"CODEX_THREAD_ID": "019a-thread-1"}))
        with self.assertRaises(S.SessionRequired):
            S.require("CLAUDE", None, {"CODEX_THREAD_ID": "019a-thread-1"})
        # a seat with no entry tries every variable, in order
        self.assertEqual(
            S.detect(
                "FX",
                None,
                {"CODEX_THREAD_ID": "019a-thread-1", "OPENCODE_SESSION_ID": "open-id-0001"},
            ),
            "019a-thread-1",
        )
        self.assertIsNone(S.detect("FX", None, {}))
        with self.assertRaises(ValueError):
            S.detect("CLAUDE", None, {"CLAUDE_CODE_SESSION_ID": "no"})
        # the aliases resolve first:  ROB is MA, which has no entry and is exempt
        self.assertIsNone(S.require("ROB", None, {}))

    def test_the_refusal_names_the_argument_the_variable_and_this_surface(self):
        with self.assertRaises(S.SessionRequired) as tool:
            S.require("CLAUDE", None, {}, "tool")
        self.assertIn("`session` argument", str(tool.exception))
        self.assertIn("AGENT_SESSION", str(tool.exception))
        self.assertIn("CLAUDE_CODE_SESSION_ID", str(tool.exception))
        with self.assertRaises(S.SessionRequired) as cli:
            S.require("CODEX", None, {}, "cli")
        self.assertIn("--session ID", str(cli.exception))
        self.assertIn("CODEX_THREAD_ID", str(cli.exception))
        with self.assertRaises(S.SessionRequired) as relay:
            S.require("CURSOR", None, {}, "relay")
        self.assertIn("`session` argument", str(relay.exception))
        self.assertIn("CURSOR_CONVERSATION_ID", str(relay.exception))
        with self.assertRaises(S.SessionRequired) as bare:
            S.require("FX", None, {}, "tool")
        self.assertNotIn("own variable", str(bare.exception))
        with self.assertRaises(ValueError):
            S.require("CLAUDE", None, {}, "nowhere")

    def test_exempt_seats_need_none_record_one_when_they_have_it_and_still_reject_a_bad_explicit_value(
        self,
    ):
        for seat in EXEMPT:
            with self.subTest(seat):
                self.assertIsNone(S.require(seat, None, {}))
                self.assertEqual(S.require(seat, "optional-id-01", {}), "optional-id-01")
                self.assertEqual(
                    S.require(seat, None, {"AGENT_SESSION": "env-id-0001"}), "env-id-0001"
                )
                self.assertIsNone(
                    S.require(seat, None, {"CLAUDE_CODE_SESSION_ID": "no"}),
                    "junk in the environment is passed over",
                )
                with self.assertRaises(ValueError):
                    S.require(seat, "no", {})


class RequestTests(unittest.TestCase):
    def test_a_session_rides_the_request_object_and_is_checked(self):
        req = N.make_request("create", "CLAUDE", request_id="requestid-0001", title="[AFC, Claude] t",
                             markdown="body", session=SID)
        self.assertEqual(req["session"], SID)
        self.assertEqual(N.check_request(req), req, "a request survives the trip through JSON")
        self.assertNotIn(
            "session",
            N.make_request("create", "JET", request_id="requestid-0001", title="[AFC, JET] t", markdown="body"),
        )
        with self.assertRaises(N.NotesError) as bad:
            N.make_request("create", "CLAUDE", request_id="requestid-0001", title="[AFC, Claude] t",
                           markdown="body", session="no")
        self.assertEqual(bad.exception.code, "invalid_argument")
        for session in ("bad id", 12345):
            with self.assertRaises(N.NotesError):
                N.check_request(dict(req, session=session))
        # Only create and update carry a session:  a pin that names one is refused on arrival.
        pin = N.make_request("pin", "CLAUDE", request_id="requestid-0002", title="[AFC, Claude] t", session=SID)
        self.assertNotIn("session", pin)
        with self.assertRaises(N.NotesError):
            N.check_request(dict(pin, session=SID))

    def test_the_rule_applies_to_every_request_that_reaches_the_queue_or_the_runner(self):
        bare = N.make_request("create", "CLAUDE", request_id="requestid-0003", title="[AFC, Claude] t", markdown="x")
        problem = N.request_problem(bare, "CLAUDE")
        self.assertIn("session id is required", problem)
        self.assertIn("`session` argument", problem)
        self.assertIn("CLAUDE_CODE_SESSION_ID", problem)
        self.assertIsNone(N.request_problem(dict(bare, session=SID), "CLAUDE"))
        for seat in EXEMPT:
            with self.subTest(seat):
                req = N.make_request("update", seat, request_id="requestid-0004", title="[AFC, %s] t" % seat,
                                     markdown="x")
                self.assertIsNone(N.request_problem(req, seat))
        pin = N.make_request("pin", "CURSOR", request_id="requestid-0005", title="[AFC, Cursor] t")
        self.assertIsNone(N.request_problem(pin, "CURSOR"))
        # The exemption is the verified identity's, never the request's own claim.
        exempt_claim = N.make_request("create", "JET", request_id="requestid-0006", title="[AFC, JET] t", markdown="x")
        self.assertIn("not CURSOR", N.request_problem(exempt_claim, "CURSOR"))


class DirectRunnerTests(unittest.TestCase):
    def run_direct(self, request):
        run = FakeRun()
        N.run_direct(
            request,
            SCRIPT,
            env={"HOME": "/h", "PATH": "/usr/bin", "AGENT_SEAT": "NOT-ME", "ZULIP_API_KEY": "x"},
            runner=run,
        )
        return run.calls[0]

    def test_the_script_gets_the_session_on_argv_and_the_verified_identity_in_its_environment(self):
        call = self.run_direct(
            N.make_request("create", "CLAUDE", request_id="requestid-0001", title="[AFC, Claude] t",
                           markdown="body", session=SID)
        )
        self.assertEqual(call["argv"][1:], ["--session", SID, "--title", "[AFC, Claude] t"])
        self.assertEqual(
            call["env"]["AGENT_SEAT"],
            "CLAUDE",
            "the request's identity, not the caller's own AGENT_SEAT",
        )
        self.assertNotIn("ZULIP_API_KEY", call["env"])
        update = self.run_direct(
            N.make_request("update", "CLAUDE", request_id="requestid-0002", title="[AFC, Claude] t",
                           markdown="body", session=SID)
        )
        self.assertEqual(
            update["argv"][1:], ["--update", "--session", SID, "--title", "[AFC, Claude] t"]
        )

    def test_no_session_means_no_flag_and_pin_and_unpin_never_carry_one(self):
        call = self.run_direct(
            N.make_request("create", "JET", request_id="requestid-0003", title="[AFC, JET] t", markdown="body")
        )
        self.assertNotIn("--session", call["argv"])
        self.assertEqual(call["env"]["AGENT_SEAT"], "JET")
        for action in ("pin", "unpin"):
            pin = self.run_direct(
                N.make_request(action, "CLAUDE", request_id="requestid-0004", title="[AFC, Claude] t", session=SID)
            )
            self.assertNotIn("--session", pin["argv"])
            self.assertEqual(pin["env"]["AGENT_SEAT"], "CLAUDE")


class ToolTests(TI.IdentityHarness):
    """The stdio tools on the Mac's direct path:  the script is found and its runs are faked, so every write that the
    session rule lets through reaches self.run.  CLAUDE has CLAUDE_CODE_SESSION_ID in the harness environment."""

    def setUp(self):
        super().setUp()
        self.run = FakeRun()
        for patch in (
            mock.patch.object(N, "direct_available", return_value=SCRIPT),
            mock.patch.object(N, "_run_helper", self.run),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def script_sessions(self):
        """The session id each script run was handed (None for a run without one)."""
        return [
            call["argv"][call["argv"].index("--session") + 1] if "--session" in call["argv"] else None
            for call in self.run.calls
        ]

    def write(self, tool="notes_create", env=None, identity="CLAUDE", **args):
        args.setdefault("title", "[AFC, %s] t" % identity.title())
        args.setdefault("markdown", "the body text")
        return self.session(env).call(tool, args, identity=identity)

    def test_the_environment_supplies_the_session_and_an_explicit_argument_beats_it(self):
        self.assert_structured("notes_create", self.write(request_id="env-request-0001"))
        self.assert_structured(
            "notes_update",
            self.write("notes_update", request_id="env-request-0002", session="explicit-id-01"),
        )
        self.assert_structured(
            "notes_create",
            self.write(
                env=self.mcp_env(AGENT_SESSION="generic-id-0001"), request_id="env-request-0003"
            ),
        )
        self.assertEqual(self.script_sessions(), [SESSION, "explicit-id-01", "generic-id-0001"])

    def test_a_non_exempt_identity_without_a_session_is_refused_and_nothing_is_run(self):
        env = self.mcp_env(CLAUDE_CODE_SESSION_ID=None)
        for tool in ("notes_create", "notes_update"):
            error = self.assert_tool_error(
                self.write(tool, env=env, markdown="private body words"), "session_required"
            )
            self.assertIn("`session` argument", error["message"])
            self.assertIn("CLAUDE_CODE_SESSION_ID", error["message"])
            self.assertIn("AGENT_SESSION", error["message"])
            self.assertNotIn("private body words", error["message"])
        self.assertEqual(self.run.calls, [])

    def test_pin_and_unpin_need_no_session(self):
        env = self.mcp_env(CLAUDE_CODE_SESSION_ID=None)
        for tool in ("notes_pin", "notes_unpin"):
            self.assert_structured(
                tool, self.session(env).call(tool, {"title": "[AFC, Claude] t"}, identity="CLAUDE")
            )
        self.assertEqual([call["argv"][1] for call in self.run.calls], ["--pin-only", "--unpin-only"])
        self.assertEqual(self.script_sessions(), [None, None])

    def test_another_platforms_variable_does_not_satisfy_a_seat_that_has_its_own(self):
        env = self.mcp_env(CLAUDE_CODE_SESSION_ID=None, CODEX_THREAD_ID="019a-thread-1")
        self.assert_tool_error(self.write(env=env), "session_required")
        # A server authenticated as CURSOR:  CLAUDE_CODE_SESSION_ID is not CURSOR's variable
        error = self.assert_tool_error(
            self.write(
                identity="CURSOR",
                title="[AFC, Cursor] t",
                env=self.mcp_env(AGENT_SEAT="CURSOR"),
            ),
            "session_required",
        )
        self.assertIn("CURSOR_CONVERSATION_ID", error["message"])
        # A CLAUDE-authenticated server never acts as CURSOR, even when its environment holds CURSOR's variable.
        self.assert_tool_error(
            self.write(
                identity="CURSOR",
                title="[AFC, Cursor] t",
                env=self.mcp_env(CURSOR_CONVERSATION_ID="cursor-conv-0001"),
            ),
            "identity_not_allowed",
        )
        self.assertEqual(self.run.calls, [])
        # A server started for CURSOR (AGENT_SEAT=CURSOR) uses its own variable.
        self.assert_structured(
            "notes_create",
            self.write(
                identity="CURSOR",
                title="[AFC, Cursor] t",
                env=self.mcp_env(
                    AGENT_SEAT="CURSOR",
                    CLAUDE_CODE_SESSION_ID=None,
                    CURSOR_CONVERSATION_ID="cursor-conv-0001",
                ),
            ),
        )
        self.assertEqual(self.script_sessions(), ["cursor-conv-0001"])
        self.assertEqual(self.run.calls[-1]["env"]["AGENT_SEAT"], "CURSOR")

    def test_an_invalid_session_is_refused_not_ignored(self):
        for bad in ("abc", "bad id", "x" * 129):
            result = self.write(session=bad)
            self.assertTrue(result.get("isError"), bad)
        error = self.assert_tool_error(
            self.write(env=self.mcp_env(CLAUDE_CODE_SESSION_ID="no")), "invalid_argument"
        )
        self.assertIn("CLAUDE_CODE_SESSION_ID", error["message"])
        self.assertEqual(self.run.calls, [])

    def test_an_exempt_identity_writes_without_a_session_and_no_argument_exempts_anyone_else(self):
        persona = TI.PERSONA
        env = self.mcp_env(CLAUDE_CODE_SESSION_ID=None)
        persona_env = self.mcp_env(AGENT_SEAT=persona, CLAUDE_CODE_SESSION_ID=None)  # authenticated as the persona
        result = self.write(
            env=persona_env, identity=persona, title="[AFC, %s] t" % persona, request_id="persona-request-1"
        )
        self.assert_structured("notes_create", result)
        self.assertEqual(self.script_sessions(), [None])
        self.assertEqual(self.run.calls[-1]["env"]["AGENT_SEAT"], persona)
        # the schema has no way to claim an exemption, and a made-up argument is refused outright
        for extra in ({"exempt": True}, {"single_thread": True}, {"seat": "MA"}):
            self.assertTrue(self.write(env=env, **extra).get("isError"), extra)
        self.assertEqual(len(self.run.calls), 1)
        listed = {t["name"]: t for t in self.session().request("tools/list")["result"]["tools"]}
        for tool in ("notes_create", "notes_update"):
            self.assertFalse(listed[tool]["inputSchema"]["additionalProperties"])
            self.assertIn("session", listed[tool]["inputSchema"]["properties"])
            self.assertNotIn("session", listed[tool]["inputSchema"].get("required", []))

    def test_the_direct_path_hands_the_script_the_session_and_the_identity(self):
        self.assert_structured("notes_create", self.write(request_id="direct-request-0001"))
        call = self.run.calls[0]
        self.assertEqual(call["argv"][-4:], ["--session", SESSION, "--title", "[AFC, Claude] t"])
        self.assertEqual(call["env"]["AGENT_SEAT"], "CLAUDE")


class CliTests(Harness):
    def run_notes(self, *argv, without_session=False):
        run = FakeRun()
        environ = self.env(CLAUDE_CODE_SESSION_ID=None) if without_session else None
        with (
            mock.patch.object(N, "direct_available", return_value=SCRIPT),
            mock.patch.object(N, "_run_helper", run),
        ):
            return run, self.run_cli("create", "[AFC, Claude] cli t", *argv, stdin="body\n", env=environ, prog="simple-notes")

    def test_the_cli_reads_the_environment_and_a_flag_beats_it(self):
        run, result = self.run_notes()
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(run.calls[0]["argv"][-4:-2], ["--session", SESSION])
        run, result = self.run_notes("--session", "flag-id-0001")
        self.assertEqual(result.code, 0, result.err)
        self.assertEqual(run.calls[0]["argv"][-4:-2], ["--session", "flag-id-0001"])

    def test_the_cli_refuses_a_non_exempt_writer_with_no_session_and_writes_nothing(self):
        run, result = self.run_notes(without_session=True)
        self.assertEqual(result.code, 2)
        self.assertIn("--session ID", result.err)
        self.assertIn("CLAUDE_CODE_SESSION_ID", result.err)
        self.assertEqual(run.calls, [])
        run, bad = self.run_notes("--session", "no")
        self.assertEqual((bad.code, run.calls), (2, []))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


