# Per-session MCP pick for grok-acp

Board `1613bd82`.  Branch `grok/acp-mcp-pick`.  Worktree `~/apps/fleet-grok-acp-mcp-pick`.

## Why

GB-DEPLOYER called grok ACP wedged because a 2-day `--no-leader serve` on `:12419` still loaded every enabled MCP from config.  First `session/new` returned a one-liner.  `session/load` then hung ~180s and came back `{"ok": false, "error": ""}`.  That was the ACP client timeout, not a down seat-mcp.  grok-leader stayed parked on purpose (this TUI holds `leader.sock`).

The 15 servers were **not** Claude inheritance.  `[compat.claude] mcps = false` is already set.  They are Grok's own `~/.grok/config.toml` `[mcp_servers.*]` plus plugins.  ACP `mcpServers: []` means "use config," not "none."

## What changed

- grok-acp `start.sh` uses a stripped `GROK_HOME` (`acp-home/`).  No kitchen-sink MCPs.  No plugins.  Auth is a symlink to `~/.grok/auth.json`.  TUI / grok-leader still use `~/.grok`.
- `acp-client.py --mcp-server NAME` expands names from **user** `~/.grok/config.toml` into ACP objects and sends that list on `session/new`.
- `seat_launch` accepts `opts.mcpServers: ["github"]` for seat `grok` only.  Empty or omitted loads none.  grok-tui keeps the full TUI set.  No TUI picker.

## Deployer usage

```json
{"seat": "grok", "prompt": "…", "cwd": "/Users/jay/apps", "opts": {"mcpServers": ["github"]}}
```

## Verify

```bash
python3 scripts/test_mcp_catalog.py
python3 scripts/test_seat_mcp_grok_tui.py
/usr/bin/python3 ~/apps/grok-acp-runtime/acp-client.py handshake
```

Do not `pm2 restart grok-leader` while the TUI holds the socket.

## Follow-up 2026-08-29: Claude plugin MCP leak

github-only job `1bcfe9a7` still resolved chrome-devtools + Cloudflare + Vercel from `~/.claude/plugins`.  `[compat.claude] mcps = false` only skips `~/.claude.json`.  Grok auto-enables installed Claude plugins.  `github` from `session/new` never attached.  The turn ran `git status` then hung; acp-client could wait past its 180s prompt timeout because `pump()` was not cancelled.

Fix: `plugins.disabled` + `disabled_mcp_servers` on grok-acp home.  TUI unchanged.
