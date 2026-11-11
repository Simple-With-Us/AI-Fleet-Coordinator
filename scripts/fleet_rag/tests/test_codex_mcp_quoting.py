#!/usr/bin/env python3
"""Codex config.toml MCP quoting.

AG leftover chat 39d195c7: Codex refused ~/.codex/config.toml because a
github mcp-remote args line used JSON-style \\" inside a TOML basic string
("missing comma between array elements").  The fleet form is a launch
script so the header never sits in the TOML string.
"""
from __future__ import annotations

import tomllib
import unittest

LAUNCH = """
[mcp_servers.github]
command = "/Users/jay/apps/mcp-servers/github-mcp-launch.sh"
args = []
startup_timeout_sec = 120
enabled = false

[mcp_servers.render]
command = "/Users/jay/apps/mcp-servers/render-mcp-launch.sh"
args = []
startup_timeout_sec = 120
enabled = false
"""

# Exact AG error shape: \\\\ then " inside a basic string ends the string
# at --header, then Authorization is a bare token (missing comma).
BAD_JSON_STYLE = r"""
[mcp_servers.github]
command = "/bin/sh"
args = [ "-c", "npx -y mcp-remote https://api.githubcopilot.com/mcp/ --header \\"Authorization: Bearer $GITHUB_MCP_TOKEN\\"" ]
"""

VALID_ESCAPED_QUOTE = r"""
[mcp_servers.github]
command = "/bin/sh"
args = [ "-c", "npx -y mcp-remote https://api.githubcopilot.com/mcp/ --header \"Authorization: Bearer $GITHUB_MCP_TOKEN\"" ]
"""


class CodexMcpQuotingTests(unittest.TestCase):
    def test_launch_script_parses(self) -> None:
        data = tomllib.loads(LAUNCH)
        gh = data["mcp_servers"]["github"]
        self.assertEqual(
            gh["command"],
            "/Users/jay/apps/mcp-servers/github-mcp-launch.sh",
        )
        self.assertEqual(gh["args"], [])
        self.assertFalse(gh["enabled"])

    def test_json_style_backslash_quote_does_not_parse(self) -> None:
        with self.assertRaises(tomllib.TOMLDecodeError) as ctx:
            tomllib.loads(BAD_JSON_STYLE)
        msg = str(ctx.exception).lower()
        # Python tomllib: "unclosed array (at line 4, column 82)".
        # Codex: "missing comma between array elements" at the same column.
        self.assertTrue(
            "comma" in msg
            or "unclosed" in msg
            or "array" in msg
            or "quote" in msg
            or "unexpected" in msg,
            msg,
        )

    def test_toml_escaped_quote_parses_but_is_not_the_fleet_form(self) -> None:
        data = tomllib.loads(VALID_ESCAPED_QUOTE)
        args = data["mcp_servers"]["github"]["args"]
        self.assertEqual(len(args), 2)
        self.assertIn("Authorization: Bearer", args[1])


if __name__ == "__main__":
    unittest.main()
