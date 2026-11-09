#!/usr/bin/env python3
"""Unit tests for grok-acp MCP name -> ACP object expansion.  No live serve."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ACP = ROOT / "scripts" / "grok-acp-runtime"
sys.path.insert(0, str(ACP))

from mcp_catalog import CatalogError, expand_names, parse_mcp_servers, to_acp  # noqa: E402

SAMPLE = """
[cli]
use_leader = true

[mcp_servers.github]
command = "sh"
args = [
    "/Users/jay/apps/mcp-servers/github-mcp-launch.sh",
]
enabled = true
startup_timeout_sec = 60

[mcp_servers.XcodeBuildMCP]
command = "/opt/homebrew/bin/npx"
args = ["-y", "xcodebuildmcp@latest", "mcp"]
enabled = true

[mcp_servers.XcodeBuildMCP.env]
XCODEBUILDMCP_SENTRY_DISABLED = "true"

[mcp_servers.sequential-thinking]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-sequential-thinking"]
enabled = false

[mcp_servers.remote]
type = "http"
url = "https://example.invalid/mcp"
enabled = true

[mcp_servers.remote.headers]
Authorization = "Bearer test-token"
"""


class ParseTests(unittest.TestCase):
    def test_parses_stdio_and_http(self) -> None:
        cat = parse_mcp_servers(SAMPLE)
        self.assertEqual(cat["github"]["command"], "sh")
        self.assertEqual(
            cat["github"]["args"],
            ["/Users/jay/apps/mcp-servers/github-mcp-launch.sh"],
        )
        self.assertTrue(cat["github"]["enabled"])
        self.assertEqual(
            cat["XcodeBuildMCP"]["env"]["XCODEBUILDMCP_SENTRY_DISABLED"],
            "true",
        )
        self.assertFalse(cat["sequential-thinking"]["enabled"])
        self.assertEqual(cat["remote"]["url"], "https://example.invalid/mcp")
        self.assertEqual(cat["remote"]["headers"]["Authorization"], "Bearer test-token")

    def test_expand_empty(self) -> None:
        self.assertEqual(expand_names([], parse_mcp_servers(SAMPLE)), [])

    def test_expand_github(self) -> None:
        out = expand_names(["github"], parse_mcp_servers(SAMPLE))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["name"], "github")
        self.assertEqual(out[0]["command"], "sh")
        self.assertEqual(out[0]["args"][0].endswith("github-mcp-launch.sh"), True)
        self.assertEqual(out[0]["env"], [])

    def test_expand_env_not_logged_shape(self) -> None:
        out = expand_names(["XcodeBuildMCP"], parse_mcp_servers(SAMPLE))
        env = out[0]["env"]
        self.assertEqual(env[0]["name"], "XCODEBUILDMCP_SENTRY_DISABLED")
        self.assertEqual(env[0]["value"], "true")

    def test_http_shape(self) -> None:
        out = expand_names(["remote"], parse_mcp_servers(SAMPLE))
        self.assertEqual(out[0]["type"], "http")
        self.assertEqual(out[0]["url"], "https://example.invalid/mcp")
        self.assertEqual(out[0]["headers"][0]["name"], "Authorization")

    def test_unknown_name(self) -> None:
        with self.assertRaises(CatalogError) as ctx:
            expand_names(["not-a-server"], parse_mcp_servers(SAMPLE))
        self.assertIn("unknown MCP server", str(ctx.exception))
        self.assertIn("github", str(ctx.exception))

    def test_disabled_name(self) -> None:
        with self.assertRaises(CatalogError) as ctx:
            expand_names(["sequential-thinking"], parse_mcp_servers(SAMPLE))
        self.assertIn("disabled", str(ctx.exception))

    def test_dedupe(self) -> None:
        out = expand_names(["github", "github"], parse_mcp_servers(SAMPLE))
        self.assertEqual(len(out), 1)

    def test_to_acp_requires_command_or_url(self) -> None:
        with self.assertRaises(CatalogError):
            to_acp("empty", {"enabled": True})

    def test_acp_home_disables_claude_plugin_mcps(self) -> None:
        cfg = (ROOT / "scripts" / "grok-acp-runtime" / "acp-home-config.toml").read_text(
            encoding="utf-8"
        )
        for name in (
            "cloudflare",
            "vercel",
            "chrome-devtools-mcp",
            "chrome-devtools",
            "cloudflare-api",
        ):
            self.assertIn(name, cfg)
        self.assertIn("disabled_mcp_servers", cfg)
        self.assertNotIn("github", cfg.split("disabled_mcp_servers", 1)[1])

    def test_load_from_path(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
            fh.write(SAMPLE)
            path = Path(fh.name)
        cat = parse_mcp_servers(path.read_text(encoding="utf-8"))
        self.assertIn("github", cat)


if __name__ == "__main__":
    unittest.main()
