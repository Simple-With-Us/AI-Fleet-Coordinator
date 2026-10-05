## Always-on (supposed to stay up)

Live-checked Fri, Aug 21, 2026 ~2:25am CT.

| Name | Kind | What it is | Live now |
|---|---|---|---|
| **pm2 `seat-mcp`** | Always-on | `127.0.0.1:8793` only (`python3 -m seat_mcp` via `~/apps/seat-mcp/start.sh`).  Public `agents.jays.services`. | **Up** |
| **pm2 `botfleet-mcp`** | Always-on | BotFleet MCP over SSE on `127.0.0.1:8794` (`BOTFLEET_MCP_PORT`), talking to the local BotFleet server at `http://127.0.0.1:8799` (`BOTFLEET_URL`).  Start: `~/apps/botfleet-server/scripts/start-mcp-sse.sh` (bash) with cwd `~/apps/botfleet-server`.  Defined in the tracked `scripts/pm2-ecosystem.config.cjs` and watched by `mac-process-watch.sh` (`expect_pm2`).  Logs `~/.pm2/logs/botfleet-mcp-{out,error}.log` (the ecosystem's `logs` dir). | **Up** |
| `runtime copy` | Always-on | Pinned checkout `~/Code/AI-Fleet-Coordinator/scripts/pm2-ecosystem.config.cjs` for the Mac. | **Up** |
| `backup name` | Always-on | Not the live file: `scripts/pm2-ecosystem.config.cjs.bak` stays out of the note. | **Up** |
| `other` | Always-on | Unrelated helper `scripts/mac-status.sh`. | **Up** |
