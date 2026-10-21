"""AFC-361:  the session id a notes write carries (session_ids.py and every place that enforces it).

Covers the rule itself, the envelope field, what run_direct hands the script (`--session` and AGENT_SEAT), the CLI,
the stdio tools (refuse, exempt, auto-detect, spoof) and the Mac listener's refusal.  The notes script is never run
for real:  the subprocess runner is the same fake the other notes tests use.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from agent_sync import notes as N
from agent_sync import notes_relay as NR
from agent_sync import session_ids as S
from agent_sync.tests import test_mcp_identity as TI
from agent_sync.tests import test_notes_relay as TR
from agent_sync.tests.harness import SESSION, Harness
from agent_sync.tests.test_notes import FakeRun

EXEMPT = ("GROK-BOT", "GB-FIXER", "MA", "JET", "WISE", "ECHO")
SID = "thread-id-0001"


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


class EnvelopeTests(unittest.TestCase):
    def test_a_session_rides_the_envelope_round_trips_and_is_checked(self):
        env = N.make_envelope(
            "create", "[AFC, Claude] t", "body", {}, "requestid-0001", "CLAUDE", SID
        )
        self.assertEqual(env["session"], SID)
        self.assertEqual(N.parse_message(N.render_message(env)), env)
        self.assertNotIn(
            "session",
            N.make_envelope("create", "[AFC, Claude] t", "body", {}, "requestid-0001", "CLAUDE"),
        )
        with self.assertRaises(N.NotesError) as bad:
            N.make_envelope(
                "create", "[AFC, Claude] t", "body", {}, "requestid-0001", "CLAUDE", "no"
            )
        self.assertEqual(bad.exception.code, "invalid_argument")
        with self.assertRaises(N.EnvelopeError):
            N.parse_message(N.render_message(dict(env, session="bad id")))
        with self.assertRaises(N.EnvelopeError):
            N.parse_message(N.render_message(dict(env, session=12345)))
        old = {k: v for k, v in env.items() if k != "session"}
        self.assertEqual(
            N.parse_message(N.render_message(old)),
            old,
            "an envelope from before AFC-361 still parses",
        )


class DirectRunnerTests(unittest.TestCase):
    SCRIPT = Path("/fake/apple-notes-coding.sh")

    def run_direct(self, envelope):
        run = FakeRun()
        N.run_direct(
            envelope,
            self.SCRIPT,
            env={"HOME": "/h", "PATH": "/usr/bin", "AGENT_SEAT": "NOT-ME", "ZULIP_API_KEY": "x"},
            runner=run,
        )
        return run.calls[0]

    def test_the_script_gets_the_session_on_argv_and_the_verified_identity_in_its_environment(self):
        call = self.run_direct(
            N.make_envelope(
                "create", "[AFC, Claude] t", "body", {}, "requestid-0001", "CLAUDE", SID
            )
        )
        self.assertEqual(call["argv"][1:], ["--session", SID, "--title", "[AFC, Claude] t"])
        self.assertEqual(
            call["env"]["AGENT_SEAT"],
            "CLAUDE",
            "the envelope's identity, not the caller's own AGENT_SEAT",
        )
        self.assertNotIn("ZULIP_API_KEY", call["env"])
        update = self.run_direct(
            N.make_envelope(
                "update", "[AFC, Claude] t", "body", {}, "requestid-0002", "CLAUDE", SID
            )
        )
        self.assertEqual(
            update["argv"][1:], ["--update", "--session", SID, "--title", "[AFC, Claude] t"]
        )

    def test_no_session_means_no_flag_and_pin_and_unpin_never_carry_one(self):
        call = self.run_direct(
            N.make_envelope("create", "[AFC, JET] t", "body", {}, "requestid-0003", "JET")
        )
        self.assertNotIn("--session", call["argv"])
        self.assertEqual(call["env"]["AGENT_SEAT"], "JET")
        pin = self.run_direct(
            N.make_envelope("pin", "[AFC, Claude] t", "", {}, "requestid-0004", "CLAUDE", SID)
        )
        self.assertNotIn("--session", pin["argv"])
        self.assertEqual(pin["env"]["AGENT_SEAT"], "CLAUDE")


class ToolTests(TI.IdentityHarness):
    """The stdio tools, over a relay-capable fake Zulip.  CLAUDE has CLAUDE_CODE_SESSION_ID in the harness environment."""

    def setUp(self):
        super().setUp()
        self.fake.streams[N.RELAY_CHANNEL] = 140
        self.fake.subscribed.add(N.RELAY_CHANNEL)

    def posted(self):
        return [
            N.parse_message(m["content"])
            for m in self.fake.messages
            if m["display_recipient"] == N.RELAY_CHANNEL
        ]

    def write(self, tool="notes_create", env=None, identity="CLAUDE", **args):
        args.setdefault("title", "[AFC, %s] t" % identity.title())
        args.setdefault("markdown", "the body text")
        args.setdefault("transport", "relay")
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
        first, second, third = self.posted()
        self.assertEqual(
            (first["session"], second["session"], third["session"]),
            (SESSION, "explicit-id-01", "generic-id-0001"),
        )

    def test_a_non_exempt_identity_without_a_session_is_refused_and_nothing_is_posted_or_run(self):
        env = self.mcp_env(CLAUDE_CODE_SESSION_ID=None)
        for tool in ("notes_create", "notes_update"):
            error = self.assert_tool_error(
                self.write(tool, env=env, markdown="private body words"), "session_required"
            )
            self.assertIn("`session` argument", error["message"])
            self.assertIn("CLAUDE_CODE_SESSION_ID", error["message"])
            self.assertIn("AGENT_SESSION", error["message"])
            self.assertNotIn("private body words", error["message"])
        run = FakeRun()
        with (
            mock.patch.object(
                N, "direct_available", return_value=Path("/fake/apple-notes-coding.sh")
            ),
            mock.patch.object(N.subprocess, "run", run),
        ):
            self.assert_tool_error(self.write(env=env, transport="auto"), "session_required")
        self.assertEqual((self.posted(), run.calls), ([], []))

    def test_pin_and_unpin_need_no_session(self):
        env = self.mcp_env(CLAUDE_CODE_SESSION_ID=None)
        for tool in ("notes_pin", "notes_unpin"):
            self.assert_structured(
                tool,
                self.session(env).call(
                    tool, {"title": "[AFC, Claude] t", "transport": "relay"}, identity="CLAUDE"
                ),
            )
        self.assertEqual([e["action"] for e in self.posted()], ["pin", "unpin"])
        self.assertTrue(all("session" not in e for e in self.posted()))

    def test_another_platforms_variable_does_not_satisfy_a_seat_that_has_its_own(self):
        env = self.mcp_env(CLAUDE_CODE_SESSION_ID=None, CODEX_THREAD_ID="019a-thread-1")
        self.assert_tool_error(self.write(env=env), "session_required")
        # CURSOR acts through this CLAUDE-launched server:  CLAUDE_CODE_SESSION_ID is not CURSOR's variable
        error = self.assert_tool_error(
            self.write(identity="CURSOR", title="[AFC, Cursor] t"), "session_required"
        )
        self.assertIn("CURSOR_CONVERSATION_ID", error["message"])
        # The server's environment is CLAUDE's, so it is not lent to CURSOR even when it holds CURSOR's variable.
        self.assert_tool_error(
            self.write(
                identity="CURSOR",
                title="[AFC, Cursor] t",
                env=self.mcp_env(CURSOR_CONVERSATION_ID="cursor-conv-0001"),
            ),
            "session_required",
        )
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
        self.assertEqual(self.posted()[-1]["session"], "cursor-conv-0001")

    def test_an_invalid_session_is_refused_not_ignored(self):
        for bad in ("abc", "bad id", "x" * 129):
            result = self.write(session=bad)
            self.assertTrue(result.get("isError"), bad)
        self.assertEqual(self.posted(), [])
        error = self.assert_tool_error(
            self.write(env=self.mcp_env(CLAUDE_CODE_SESSION_ID="no")), "invalid_argument"
        )
        self.assertIn("CLAUDE_CODE_SESSION_ID", error["message"])

    def test_an_exempt_identity_writes_without_a_session_and_no_argument_exempts_anyone_else(self):
        persona = TI.PERSONA
        env = self.mcp_env(CLAUDE_CODE_SESSION_ID=None)
        result = self.write(
            env=env, identity=persona, title="[AFC, %s] t" % persona, request_id="persona-request-1"
        )
        self.assert_structured("notes_create", result)
        self.assertNotIn("session", self.posted()[-1])
        self.assertEqual(self.posted()[-1]["identity"], persona)
        # the schema has no way to claim an exemption, and a made-up argument is refused outright
        for extra in ({"exempt": True}, {"single_thread": True}, {"seat": "MA"}):
            self.assertTrue(self.write(env=env, **extra).get("isError"), extra)
        self.assertEqual(len(self.posted()), 1)
        listed = {t["name"]: t for t in self.session().request("tools/list")["result"]["tools"]}
        for tool in ("notes_create", "notes_update"):
            self.assertFalse(listed[tool]["inputSchema"]["additionalProperties"])
            self.assertIn("session", listed[tool]["inputSchema"]["properties"])
            self.assertNotIn("session", listed[tool]["inputSchema"].get("required", []))

    def test_the_direct_path_hands_the_script_the_session_and_the_identity(self):
        run = FakeRun()
        with (
            mock.patch.object(
                N, "direct_available", return_value=Path("/fake/apple-notes-coding.sh")
            ),
            mock.patch.object(N.subprocess, "run", run),
        ):
            self.assert_structured(
                "notes_create", self.write(transport="auto", request_id="direct-request-0001")
            )
        call = run.calls[0]
        self.assertEqual(call["argv"][-4:], ["--session", SESSION, "--title", "[AFC, Claude] t"])
        self.assertEqual(call["env"]["AGENT_SEAT"], "CLAUDE")


class CliTests(Harness):
    def setUp(self):
        super().setUp()
        self.script = Path("/fake/apple-notes-coding.sh")

    def run_notes(self, *argv, without_session=False):
        run = FakeRun()
        environ = self.env(CLAUDE_CODE_SESSION_ID=None) if without_session else None
        with (
            mock.patch.object(N, "direct_available", return_value=self.script),
            mock.patch.object(N.subprocess, "run", run),
        ):
            return run, self.run_cli(
                "notes", "create", "[AFC, Claude] cli t", *argv, stdin="body\n", env=environ
            )

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


class RelayTests(TR.RelayHarness):
    def setUp(self):
        super().setUp()
        self.senders = {"JET": 21, "WISE": 22, "ECHO": 23, "MA": 24, "GROK-BOT": 25}
        for name, email in (
            ("JET", "openai-dot-bot@zulip.test"),
            ("WISE", "instinct-owl-bot@zulip.test"),
            ("ECHO", "instinct-bat-bot@zulip.test"),
            ("MA", "muse-assist-bot@zulip.test"),
            ("GROK-BOT", "grok-bot-bot@zulip.test"),
        ):
            user_id = self.senders[name]
            TR.USERS[user_id] = TR.person(user_id, email)
            self.runner.users[user_id] = TR.USERS[user_id]
            self.addCleanup(TR.USERS.pop, user_id, None)

    def send(self, identity, title, session=None, action="create", rid="session-request-1"):
        envelope = N.make_envelope(action, title, "hello body", {}, rid, identity, session)
        user_id = self.senders.get(identity) or TR.SENDERS[identity]
        return self.message(user_id, "[%s·ab12cd34] %s" % (identity, N.render_message(envelope)))

    def test_a_create_or_update_without_a_session_from_a_non_exempt_sender_is_refused_with_how_to_pass_one(
        self,
    ):
        for action in ("create", "update"):
            message = self.send(
                "CURSOR", "[AFC, Cursor] t", action=action, rid="no-session-" + action
            )
            self.assert_refused(message, "session id is required")
            self.assertIn("`session` argument", self.client.replies[-1])
            self.assertIn("CURSOR_CONVERSATION_ID", self.client.replies[-1])
            self.assertNotIn("hello body", self.client.replies[-1])

    def test_the_session_runs_and_reaches_the_script(self):
        message = self.send("CURSOR", "[AFC, Cursor] t", SID)
        self.assertEqual(self.process(message), "done")
        self.assertEqual(
            self.calls[0]["argv"][-4:], ["--session", SID, "--title", "[AFC, Cursor] t"]
        )
        self.assertEqual(self.calls[0]["env"]["AGENT_SEAT"], "CURSOR")

    def test_exempt_senders_need_none_and_pin_never_does(self):
        titles = {
            "GB-FIXER": "[AFC, GB-FIXER] t",
            "JET": "[AFC, JET] t",
            "WISE": "[AFC, WISE] t",
            "ECHO": "[AFC, ECHO] t",
            "MA": "[AFC, Muse Assist] t",
            "GROK-BOT": "[AFC, GROK-BOT] t",
        }
        for identity, title in titles.items():
            with self.subTest(identity):
                self.assertEqual(
                    self.process(
                        self.send(identity, title, rid="exempt-" + identity.lower() + "-1")
                    ),
                    "done",
                )
                self.assertNotIn("--session", self.calls[-1]["argv"])
                self.assertEqual(self.calls[-1]["env"]["AGENT_SEAT"], identity)
        before = len(self.calls)
        self.assertEqual(
            self.process(
                self.send("CURSOR", "[AFC, Cursor] t", action="pin", rid="pin-no-session-1")
            ),
            "done",
        )
        self.assertEqual(len(self.calls), before + 1)

    def test_the_exemption_is_the_senders_bot_never_a_claim_in_the_envelope(self):
        # CURSOR's bot claims to be GB-FIXER (exempt):  refused for the identity, so the exemption never applies
        envelope = N.make_envelope(
            "create", "[AFC, GB-FIXER] t", "x", {}, "spoof-request-01", "GB-FIXER"
        )
        message = self.message(TR.SENDERS["CURSOR"], N.render_message(envelope))
        self.assert_refused(message, "is not the sender")

    def test_validate_names_the_session_refusal_code(self):
        message = self.send("GROK-WEB", "[AFC, Grok Web] t")
        with self.assertRaises(NR.Refusal) as caught:
            NR.validate(message, TR.USERS[12])
        self.assertEqual(caught.exception.code, "session")
        self.assertEqual(caught.exception.request_id, "session-request-1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
