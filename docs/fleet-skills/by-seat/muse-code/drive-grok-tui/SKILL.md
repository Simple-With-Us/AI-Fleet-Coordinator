---
name: drive-grok-tui
description: Drive a live Mac Grok TUI session from any local or cloud agent (Claude, Cursor, Grok, Shellular, Grok Bot, this TUI, …). List chats with idle/working/needs-input and pendingTool, peek or tail the transcript, inject a prefixed follow-up, await the next turn via disk poll, or cancel. Cloud seats use https://agents.jays.services/mcp (Access + Bearer), not grok-drive on the VM. Use when you need to send work into an already-open grok terminal instead of spawning grok-acp :12419.
---

# Drive a live Grok TUI

> **Runtime (Muse Code).** Muse Code (`muse` CLI) is the interactive terminal coding agent (`[MC]`).  Notes name `Muse Code`.  Branches `muse-code/`.  New lanes are made with `~/apps/lane new <app> <slug>` at `~/apps/lanes/<Repo>/muse-code-<slug>`, and `AGENT_SEAT=MC` comes from the `muse-seat` wrapper, never from a guess.  Start `muse` inside a lane; `muse -w` appears to make a worktree inside `~/Code/<App>/.muse/worktrees`, so do not use it.  Distinct from **Muse Assist** (`[MA]`, former tag `[MUSE]`), which is the cloud VM batch compute / creative assistant dispatched via Mac/iOS apps.  Project `AGENTS.md` and `CLAUDE.md` load automatically when the workspace is trusted in `~/.config/muse/trust.json`, and the user-level `~/.claude/CLAUDE.md` loads as a fallback.  Skills installed here (`~/.config/muse/skills`) shadow foreign personal skills.  Setup checklist: `docs/MUSE-ONBOARDING.md`.


The Mac Grok TUI joins `~/.grok/leader.sock`.  Any agent can attach through
`grok-drive.py` (Mac) or seat-mcp (Mac or cloud).  Do **not** spawn a second
`grok-acp` on `:12419` to talk to those chats.

After a coordinator merge that touches these helpers, run
`bash scripts/install-grok-tui-drive.sh` from the AFC checkout so `~/apps/`
is not a stale copy.

## CLI (Mac)

```bash
python3 ~/apps/grok-acp-runtime/grok-drive.py list
python3 ~/apps/grok-acp-runtime/grok-drive.py peek --session-id ID --tail 8
python3 ~/apps/grok-acp-runtime/grok-drive.py tail --session-id ID --lines 12
python3 ~/apps/grok-acp-runtime/grok-drive.py prompt \
  --session-id ID --cwd DIR --prompt "…" --from-name CLAUDE
python3 ~/apps/grok-acp-runtime/grok-drive.py await --session-id ID --timeout 180
python3 ~/apps/grok-acp-runtime/grok-drive.py cancel --session-id ID --cwd DIR
python3 ~/apps/grok-acp-runtime/grok-drive.py close --session-id ID --cwd DIR
python3 ~/apps/grok-acp-runtime/grok-idle-unload.py --dry-run
```

- `list` rows include `live`, `turnState` (`idle` / `working` / `needs-input`), title, `lastTurnSummary`, and `pendingTool` when a permission prompt is waiting.  Do not auto-deny.
- `prompt` prefixes `[from: NAME]` (`--from-name`, else `$AGENT_TAG` / `$AGENT_SEAT` / `remote`).
- `prompt` refuses a working / needs-input session unless `--queue`.
- `prompt` refuses `sessionId == $GROK_SESSION_ID` unless `--self` (stops this TUI from injecting into itself).
- `prompt` returns `queued: true` as soon as the TUI accepts the message.  It does **not** wait for the turn.
- `--await-reply N` waits for a **new** `turn_started` after the pre-inject snapshot, then that turn to end.  It does not treat a pre-existing idle as the reply.
- `await` (no inject) returns immediately if the session is already idle; if working, it waits for **this** turn.
- Peek/tail/await never `session/load` a live chat (load hangs ~45s).
- `cancel` is a `session/cancel` **notification** after `session/resume`.  Best-effort.  Idle chats ignore it.
- `close` is `session/close`: unload that chat's MCP tools and keep the transcript on disk.  Refuses `$GROK_SESSION_ID` unless `--self`.  Refuses working / needs-input unless `--force`.
- Hourly `com.jay.grok-idle-unload` closes chats that hold MCP processes and sit idle: 30 minutes for a zero-turn stub with no attached client, 4 hours for the rest.  It does not `/delete`.  `/resume` or `grok --resume ID` reloads tools.  `GROK_IDLE_UNLOAD_STUB_MINUTES` and `GROK_IDLE_UNLOAD_HOURS` override the thresholds.

## MCP (local or cloud)

Local stdio launcher: `sh ~/apps/mcp-servers/seat-mcp-launch.sh`
(already in `~/.grok/config.toml`).  Loopback HTTP: `http://127.0.0.1:8793/mcp`.

Cloud / Grok Bot / Cursor cloud / Claude Code Cloud: HTTPS

`https://agents.jays.services/mcp`

Cloudflare Access **and** Bearer `SEAT_MCP_TOKEN`.  Env placeholders only — never
literals in git:

```json
{
  "mcpServers": {
    "seat-mcp": {
      "url": "https://agents.jays.services/mcp",
      "headers": {
        "Authorization": "Bearer ${SEAT_MCP_TOKEN}",
        "CF-Access-Client-Id": "${CF_ACCESS_CLIENT_ID}",
        "CF-Access-Client-Secret": "${CF_ACCESS_CLIENT_SECRET}"
      }
    }
  }
}
```

Tracked fragment: `scripts/seat-mcp/mcp.example.json`.  Secrets live in
`~/.secrets/seat-mcp.env` and
`~/.secrets/agents-jays-services-access-service-token.env` on the Mac, and in
the cloud dashboard env vars — not in this skill.

Tools: `grok_sessions_list`, `grok_session_peek`, `grok_session_tail`,
`grok_session_prompt`, `grok_session_await`, `grok_session_cancel`.

Flow: list → pick a `live=true` row → prompt `{sessionId, prompt, from}` →
await `{sessionId}` (or `awaitReply` on the prompt job).

A cloud agent cannot run `grok-drive.py` on the VM.  The MCP hop is the path
into the Mac TUI.  GET `https://agents.jays.services/health` is public (no
token).  POST `/mcp` without Access is 302.

## Do not

- Start a second `grok agent serve` or bind `:2419`.
- `session/load` a chat the TUI already has open.
- `--wait` on ACP for a live TUI reply (the wait *is* that TUI turn).
- Auto-deny a `needs-input` permission.  Surface `pendingTool` and let the TUI
  operator decide.
- Prompt this process's own `$GROK_SESSION_ID` without `--self`.
- Put tokens in `mcp.json` / `config.toml` literals.
