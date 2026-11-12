#!/bin/sh
# Local seat-mcp stdio launcher for Grok TUI / Cursor / any local agent.
# Token stays in Infisical Fleet-OPS /seat-mcp (SEAT_MCP_TOKEN, read by the proxy through fleet-secrets).
# Never in mcp.json or config.toml.
set -e
exec /usr/bin/python3 /Users/jay/apps/mcp-servers/seat-mcp-stdio-proxy.py
