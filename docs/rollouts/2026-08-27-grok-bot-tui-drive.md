# Grok Bot drive for live Grok TUI sessions

Branch `grok/tui-drive`.  Board `d854b8b4`.  Worktree `~/apps/fleet-grok-tui-drive`.

## Why

Grok Bot (Cursor cloud `[GB-<NAME>]` seats) could spawn **new** Grok ACP sessions on `127.0.0.1:12419` via seat-mcp `seat=grok`.  It could not attach to a **live Mac Grok TUI** chat.  Phone/Shellular already join `~/.grok/leader.sock`.  This lane is the same attach path for Grok Bot.

## What landed

- `leader-client.py` `peek` / `prompt` (no `yoloMode` on an existing TUI).
- `grok-drive.py` list (merges `~/.grok/active_sessions.json` as `live=true`), peek, prompt, new.
- seat-mcp v1.1 tools: `grok_sessions_list`, `grok_session_peek`, `grok_session_prompt`.  Seat `grok-tui`.
- Cloudflare Access-authenticated hops skip the browser Origin DNS-rebinding check (Bearer still required).
- Grok Bot / Cursor skill `drive-grok-tui`.  Cursor HTTP MCP fragment uses env placeholders only.
- Tracked copies: `scripts/grok-acp-runtime/`, `scripts/seat-mcp/`.

## How Grok Bot uses it

1. Call `grok_sessions_list` on `https://agents.jays.services/mcp` (Access + Bearer).
2. Pick a row with `live=true`.
3. `grok_session_prompt` `{sessionId, prompt}` → `{jobId}`.
4. Poll `seat_status` / `seat_result`.

On the Mac: `python3 ~/apps/grok-acp-runtime/grok-drive.py list`.

Do not start a second `grok-acp`.  Do not bind `:2419`.  Do not put tokens in `mcp.json` literals.

## Verification

```bash
python3 ~/apps/fleet-grok-tui-drive/scripts/test_seat_mcp_grok_tui.py
python3 ~/apps/fleet-grok-tui-drive/scripts/test_fleet_skill_identity.py
python3 ~/apps/grok-acp-runtime/leader-client.py handshake
python3 ~/apps/grok-acp-runtime/grok-drive.py list
curl -sS http://127.0.0.1:8793/health
```

Handshake + list only.  This lane does not inject a prompt into the live TUI as a test.

## Owner leftover

Add the HTTP MCP fragment in Cursor cloud (Grok Bot) with dashboard env vars `SEAT_MCP_TOKEN`, `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET`.  Local Mac Cursor may use `http://127.0.0.1:8793/mcp` and omit CF-Access headers.
