# 2026-08-30 — seat-mcp grok sessionId flush

Board `51965c1b`.  Branch `grok/seat-flush`.  Worktree `~/apps/fleet-grok-seat-flush`.

## Why

Grok Bot seats could not tell a hung `session/new` from a running turn.  Jobs `401c53c0` (ST #3077) and `15958b82` (ST #3120 follow-up) hit 900s, exit -15, `sessionId` none, `bytesOut` 0.  Successful jobs `017b7805` / `ac2f3ac6` only wrote `sessionId` after `session/prompt` finished because `acp-client.py` printed one JSON blob at the end.

`--timeout` was missing from the grok spawn argv (relied on acp-client default).  Each of initialize / session_new / session_prompt started a pump and cancelled it, so a permission or `terminal/*` request between calls could hang grok forever.

## What changed

- `acp-client.py` keeps one WebSocket pump for the connection.  Emits NDJSON `event=session` (flushed) as soon as `session/new` returns, then `event=tool` / `event=done`.
- `seats.py` passes `python3 -u`, `PYTHONUNBUFFERED=1`, and `--timeout` from `opts.timeoutSec`.
- `runner.py` ingests NDJSON lines live.  No `sessionId` within 50s fails with `session/new did not return`.
- `seat_status` includes `sessionId`, `bytesOut`, `lastTool`, `elapsedMs`, `gitMoved`.
- Second `seat_launch` grok is rejected while one grok ACP job is queued or running.  grok-tui jobs do not count.  `priorJobId` no longer auto `session/load`s a grok session.

## Grok Bot rule

```
seat_launch seat=grok
opts.timeoutSec: 900
opts.mcpServers: ["github"]
fresh session only — never opts.sessionId, never resume 01a050* / hung 01a051*
one grok ACP job at a time
never exec /Users/jay/.grok/bin/grok
never start grok-leader, never bind :2419
```

Treat grok as green only when `seat_status.gitMoved` is true (or the turn's git command completed).

## Live prove (2026-08-30)

- Handshake ok.  grok-leader stayed stopped.
- github-only `echo pong` twice: session `01a052e5-7cd2` 22.7s, `01a052e5-da5c` 31.1s.  NDJSON `event=session` first, then tool, then `event=done` containing pong.
- `git status -sb` in `~/apps/fleet-grok-seat-flush`: session `01a052e8-bc31`, 15.3s, `## grok/seat-flush...origin/main`.
- Overlapping two `acp-client.py` prompts on grok-acp: both got sessionIds; tool events leaked across clients; the sleep job exited 1.  **Do not run two grok ACP prompts.**  seat-mcp rejects the second.
- seat-mcp job `6e5f31c702d140bc91dedcc594bc15d7`: `seat_status` at t+1s had `sessionId=01a052e8-faec` and `bytesOut=161` while still running.  Second `seat_launch` grok rejected.  Result succeeded, exit 0, text contains `seat-pong`.
- grok-acp children: grok serve + stderr redactor.  No chrome-devtools/Cloudflare/Vercel MCP child of grok-acp.

## Out of scope

ST #3077 / #3120 product work stays Deployer's.  extra-ship no.  --force-ship no.  Lane is proved; Deployer launches a **fresh** github-only job for #3077 (do not resume 01a050* / 01a051*).
