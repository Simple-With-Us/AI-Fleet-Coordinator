# TUI drive follow-ups + cloud hop

Branch `grok/tui-drive-cloudhop`.  Board `56cc91fd`.  Worktree `~/apps/fleet-grok-tui-cloudhop`.

## Why

#137 landed generic live-TUI drive.  Six leftovers plus the owner's ask that a **cloud** agent can talk to a **local** Grok TUI.

## What landed

1. **Install-on-merge** — `scripts/install-grok-tui-drive.sh` copies tracked `scripts/grok-acp-runtime`, `scripts/seat-mcp`, and `scripts/mcp-servers/seat-mcp-*` into `~/apps/`.  `--restart-seat-mcp` only if pm2 already owns the job.
2. **Await next turn** — `poll_after_inject` snapshots `turnStartedAt` before inject and waits for a later `turn_started` then `turn_ended`.  `--await-reply` uses this.  Bare `await` still returns immediately if the session is already idle.
3. **Cancel** — verified as a `session/cancel` notification after resume on an **idle other** session (not this TUI).  Idle chats ignore it.
4. **Needs-input** — `list` / `peek` include `pendingTool` from the last unresolved `permission_requested`.  Do not auto-deny.
5. **Launchers tracked** — `scripts/mcp-servers/seat-mcp-launch.sh` + `seat-mcp-stdio-proxy.py`.
6. **Self-guard** — `prompt` refuses `sessionId == $GROK_SESSION_ID` unless `--self`.
7. **Cloud hop** — documented and verified: `https://agents.jays.services/mcp` (Access + Bearer) → Mac seat-mcp → live TUI.  GET `/health` is public.  Env placeholders only.

Generic any-seat.  Not Grok-Bot-only.  Not ContactLogo.

## Verification

```bash
python3 scripts/test_session_disk.py
python3 scripts/test_seat_mcp_grok_tui.py
python3 scripts/test_fleet_skill_identity.py
bash scripts/install-grok-tui-drive.sh --dry-run
curl -sS http://127.0.0.1:8793/health
curl -sS https://agents.jays.services/health
python3 ~/apps/grok-acp-runtime/grok-drive.py list
```

Did not inject a follow-up into this TUI.  Self-guard is unit-tested with a fake `$GROK_SESSION_ID`.

## Owner leftover

Cursor cloud / Claude Code Cloud dashboards still need the HTTP MCP fragment with env vars `SEAT_MCP_TOKEN`, `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET` (names only).  Local Mac Cursor may keep `http://127.0.0.1:8793/mcp` and omit CF-Access headers.
