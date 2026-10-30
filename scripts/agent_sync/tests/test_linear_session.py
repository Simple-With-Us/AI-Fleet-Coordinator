"""AFC-361:  the stdio Linear writes need a session id from every identity but the single-thread ones, take it from
the call's `session` argument or from the server's own environment, and never lend one identity's session to another."""

from __future__ import annotations

import unittest

from agent_sync.mcp import linear_tools as LT
from agent_sync.tests.mcp_harness import (
    SESSION as SERVER_SESSION,
)  # the harness's CLAUDE_CODE_SESSION_ID
from agent_sync.tests.mcp_harness import seat_bearer_locator
from agent_sync.tests.test_mcp_extras import FakePrograms
from agent_sync.tests.test_mcp_identity import ENABLED, PERSONA


class ChildEnvTests(unittest.TestCase):
    def test_only_the_session_vars_it_is_handed_reach_board(self):
        server = {
            "PATH": "/usr/bin",
            "CLAUDE_CODE_SESSION_ID": "cc-sess-0001",
            "ZULIP_API_KEY": "must-not-pass",
        }
        out = LT.child_env(server, "CLAUDE", {}, session_env=server)
        self.assertEqual(out["CLAUDE_CODE_SESSION_ID"], "cc-sess-0001")
        self.assertNotIn("ZULIP_API_KEY", out)
        # board proves its seat from the bearer and never reads AGENT_SEAT, so the seat variables are
        # stripped from the child and no caller can put one back.
        self.assertNotIn("AGENT_SEAT", out)
        self.assertNotIn("AGENT_LAUNCH_SEAT", out)

    def test_no_session_var_is_forwarded_unless_the_caller_hands_it_over(self):
        server = {
            "PATH": "/usr/bin",
            "CLAUDE_CODE_SESSION_ID": "cc-sess-0001",
            "AGENT_SESSION": "generic-0002",
        }
        out = LT.child_env(server, "CURSOR", {})
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", out)
        self.assertNotIn("AGENT_SESSION", out)

    def test_the_bearer_locator_is_the_only_identity_the_child_gets(self):
        out = LT.child_env({"PATH": "/usr/bin"}, "CLAUDE", {}, locator=seat_bearer_locator("CLAUDE"))
        self.assertEqual(out["SIMPLE_BOARD_BEARER_LOCATOR"], seat_bearer_locator("CLAUDE"))
        self.assertNotIn("AGENT_SEAT", out)

    def test_an_explicit_session_argument_wins(self):
        out = LT.child_env(
            {},
            "CODEX",
            {"AGENT_SESSION": "from-arg-0002"},
            session_env={"AGENT_SESSION": "env-0001"},
        )
        self.assertEqual(out["AGENT_SESSION"], "from-arg-0002")


class LinearSessionToolTests(FakePrograms):
    WRITES = (
        ("linear_claim", {"id": "AFC-1"}),
        ("linear_comment", {"id": "AFC-1", "body": "hello"}),
        ("linear_status", {"id": "AFC-1", "status": "addressed"}),
        ("linear_file", {"title": "a title"}),
    )

    def test_linear_status_and_every_core_write_take_a_session_argument(self):
        session = self.session(self.env())
        names = {
            tool["name"]: tool for tool in session.request("tools/list", {})["result"]["tools"]
        }
        for name, _ in self.WRITES:
            self.assertIn("session", names[name]["inputSchema"]["properties"], name)

    def test_the_servers_own_identity_uses_its_environment_session(self):
        session = self.session(self.env())
        for name, args in self.WRITES:
            self.assertNotIn("isError", session.call(name, args), name)
        for call in self.calls(self.board_out):
            self.assertEqual(call["argv"][call["argv"].index("--session") + 1], SERVER_SESSION)

    def test_another_identity_does_not_borrow_the_servers_session(self):
        # A server authenticated as CURSOR holds CLAUDE's platform session variable, which is not CURSOR's.
        session = self.session(self.env(AGENT_SEAT="CURSOR"))
        for name, args in self.WRITES:
            error = self.assert_tool_error(session.call(name, args), "session_required")
            self.assertIn("Linear board", error["message"])
            self.assertIn("`session` argument", error["message"])
        self.assertEqual(
            self.calls(self.board_out), [], "board must not run when the session is missing"
        )

    def test_another_identity_passes_its_own_session(self):
        # a server authenticated as CURSOR does not hold CLAUDE's platform variable
        session = self.session(self.env(AGENT_SEAT="CURSOR", CLAUDE_CODE_SESSION_ID=None))
        for name, args in self.WRITES:
            result = session.call(name, dict(args, session="cursor-chat-0009"))
            self.assertNotIn("isError", result, name)
        for call in self.calls(self.board_out):
            self.assertEqual(call["argv"][call["argv"].index("--session") + 1], "cursor-chat-0009")
            self.assertNotIn(
                "CLAUDE_CODE_SESSION_ID",
                call["env"],
                "the server's session reached another identity",
            )

    @unittest.skipUnless(PERSONA in ENABLED, "the persona is not enabled in this build")
    def test_a_single_thread_identity_needs_none(self):
        session = self.session(self.env(AGENT_SEAT=PERSONA, CLAUDE_CODE_SESSION_ID=None))
        for name, args in self.WRITES:
            self.assertNotIn("isError", session.call(name, args), name)
        for call in self.calls(self.board_out):
            self.assertNotIn("--session", call["argv"])

    def test_a_bad_session_is_invalid_argument(self):
        session = self.session(self.env())
        self.assert_tool_error(
            session.call("linear_comment", {"id": "AFC-1", "body": "x", "session": "no good!"}),
            "invalid_argument",
        )

    def test_reads_do_not_need_one(self):
        session = self.session(self.env(AGENT_SEAT="CURSOR"))
        self.assertNotIn("isError", session.call("linear_list", {"limit": 1}))
        # a CLAUDE-authenticated server does not read as CURSOR
        self.assert_tool_error(
            self.session(self.env()).call("linear_list", {"limit": 1}, identity="CURSOR"),
            "identity_not_allowed",
        )


class BearerLocatorTests(FakePrograms):
    """A stdio Linear write is signed with the acting seat's own bearer locator.

    board proves its seat from the bearer at SIMPLE_BOARD_BEARER_LOCATOR and never reads AGENT_SEAT, so
    the locator comes from the seat's own trusted daemon config, and a write with no locator is refused
    before board runs.
    """

    def test_the_seats_own_locator_reaches_board(self):
        session = self.session(self.env())
        self.assertNotIn("isError", session.call("linear_claim", {"id": "AFC-1"}))
        for call in self.calls(self.board_out):
            self.assertEqual(call["bearer"], seat_bearer_locator("CLAUDE"))
            self.assertNotIn("AGENT_SEAT", call["env"])
            self.assertNotIn("AGENT_LAUNCH_SEAT", call["env"])

    def test_a_write_with_no_locator_is_refused_before_board_runs(self):
        self.pin_seat_bearers({})
        session = self.session(self.env(SIMPLE_BOARD_BEARER_LOCATOR=None))
        error = self.assert_tool_error(session.call("linear_claim", {"id": "AFC-1"}), "not_authorized")
        self.assertIn("CLAUDE", error["message"])
        self.assertIn("bearer_locator", error["message"])
        self.assertEqual(self.calls(self.board_out), [], "board must not run when there is no locator")

    def test_a_read_with_no_locator_still_works(self):
        self.pin_seat_bearers({})
        session = self.session(self.env(SIMPLE_BOARD_BEARER_LOCATOR=None))
        self.assertNotIn("isError", session.call("linear_list", {"limit": 1}))

    def test_the_locator_from_the_listeners_own_config_is_used(self):
        self.pin_seat_bearers({"CLAUDE": seat_bearer_locator("CLAUDE")})
        session = self.session(self.env(SIMPLE_BOARD_BEARER_LOCATOR=None))
        self.assertNotIn("isError", session.call("linear_claim", {"id": "AFC-1"}))
        for call in self.calls(self.board_out):
            self.assertEqual(call["bearer"], seat_bearer_locator("CLAUDE"))


if __name__ == "__main__":
    unittest.main()
