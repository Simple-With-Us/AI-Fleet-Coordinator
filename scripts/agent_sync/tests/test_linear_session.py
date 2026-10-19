"""AFC-361:  the stdio Linear tools hand the board child the session variables, so `board` accepts the write."""
from __future__ import annotations

import unittest

from agent_sync.mcp import linear_tools as LT


class ChildEnvSessionTests(unittest.TestCase):
    def test_the_servers_session_variables_reach_board(self):
        env = {"PATH": "/usr/bin", "CLAUDE_CODE_SESSION_ID": "cc-sess-0001", "CODEX_THREAD_ID": "cx-0002",
               "AGENT_SESSION": "generic-0003", "ZULIP_API_KEY": "must-not-pass"}
        out = LT.child_env(env, "CLAUDE", {})
        for name in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "AGENT_SESSION"):
            self.assertEqual(out[name], env[name])
        self.assertNotIn("ZULIP_API_KEY", out)
        self.assertEqual(out["AGENT_SEAT"], "CLAUDE")

    def test_an_explicit_session_argument_wins_over_the_servers_environment(self):
        out = LT.child_env({"AGENT_SESSION": "from-env-0001"}, "CODEX", {"AGENT_SESSION": "from-arg-0002"})
        self.assertEqual(out["AGENT_SESSION"], "from-arg-0002")

    def test_no_session_leaves_the_environment_without_one(self):
        out = LT.child_env({"PATH": "/usr/bin"}, "CLAUDE", {})
        self.assertNotIn("AGENT_SESSION", out)
        self.assertNotIn("CLAUDE_CODE_SESSION_ID", out)


if __name__ == "__main__":
    unittest.main()
