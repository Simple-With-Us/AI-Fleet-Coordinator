#!/bin/sh
# Slack Collaboration MCP Server Launcher
# Sources Slack tokens from ~/.secrets/agent-sync.env without printing secrets
set -e
if [ -f /Users/jay/.secrets/agent-sync.env ]; then
  set -a; . /Users/jay/.secrets/agent-sync.env; set +a
fi
PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"; export PATH
exec node /Users/jay/apps/slack-collab-mcp/index.js
