# seat-mcp (v1.2)

Async Mac seat jobs over streamable HTTP MCP.  Not a synchronous `dsh_reply` tool.

Listen:  `127.0.0.1:8793` (`POST /mcp`).  Loopback only.  Public hop is Cloudflare Access on `https://agents.jays.services` (owner-approved 2026-08-26).  Bearer still required.  Cloud agents (Grok Bot, Cursor cloud, Claude Code Cloud, iOS) use that host — they cannot run `grok-drive.py` on the VM.

Fleet RAG (any device):

- MCP: `POST https://agents.jays.services/mcp` tools `recall_search` / `recall_stats` / `recall_contribute`
- REST: `GET /recall/stats`, `POST /recall/search`, `POST /recall/contribute` (same bearer + Access headers)

See `mcp.example.json` and `docs/RAG-FLEET-INFRA.md`.

Token:  Infisical Fleet-OPS `/seat-mcp` (`SEAT_MCP_TOKEN`), read with `fleet-secrets get SEAT_MCP_TOKEN --path /seat-mcp`.  Never print it.  Never put it in `~/.shellular/agents.json` or git.

Job records:  `~/.seat-mcp/jobs/<jobId>.json` (atomic write + ring-buffered tail).  Multi-seat:  do not use `~/.dsh/mcp-jobs`.

## Tools

- `seat_launch(seat, prompt, cwd, opts)` → `{jobId}`
- `seat_status(jobId)` → `{state, elapsedMs, sessionId, bytesOut, lastTool, gitMoved, heartbeat, partialTail}`
- `seat_reply(jobId)` → `{text}` (optional `prompt` starts an async follow-up and returns a new `{jobId}`)
- `seat_result(jobId)` → `{text, exitCode, sessionId, stats, artifacts}`
- `grok_sessions_list` → live TUI chats (`live`, `turnState`, `pendingTool`)
- `grok_session_peek(sessionId, cwd?)` → disk `summary.json`, no `session/load`, no prompt
- `grok_session_tail(sessionId, lines?)` → last N `updates.jsonl` chunks
- `grok_session_prompt(sessionId, prompt, cwd?, from?, queue?, self?)` → `{jobId}` (seat `grok-tui`)
- `grok_session_await(sessionId, timeoutSec?)` → disk poll until idle / needs-input
- `grok_session_cancel(sessionId)` → best-effort `session/cancel` notification

Heartbeat tells working vs wedged.  Timeout kills the process GROUP, not just the parent pid.

## Seats

v1.1 implements three production seats.  Shellular has no local HTTP API:  we may read `~/.shellular/agents.json` for names, but spawning is our job.

### deepseek

`/Users/jay/apps/dsh-runtime/dsh.sh --profile headless <prompt>`

Default sandbox:  `DSH_PERMISSION_MODE=read-only`.  There is no mode parameter.  Never default `danger-full-access`.  Do not use `npx @deepseek-ai/dsh`.  Do not edit `~/.dsh/settings-headless.yaml`.

Optional `opts.effort`:

- `quick`:  temp `--patch` YAML only (`deepseek-v4-flash` / `low`)
- `deep`:  temp `--patch` YAML only (`deepseek-v4-pro` / `high`)

DSH headless is one submitted task.  It cannot resume.  `seat_reply` with a follow-up prompt starts a new one-shot with prior text stuffed.  Passing `opts.sessionId` on deepseek is an error.

### grok

Grok Bot rule (binding):  `seat_launch` seat `grok`, **fresh** session (never `opts.sessionId`, never resume `01a050*` / hung `01a051*`), `opts.mcpServers: ["github"]`, `opts.timeoutSec: 900`.  Never exec `/Users/jay/.grok/bin/grok` from a Grok Bot seat.  One grok ACP job at a time — a second `seat_launch` grok is rejected while one is queued/running.  grok-tui pings do not count.

`acp-client.py` prints an NDJSON `{"event":"session","sessionId":…}` line as soon as `session/new` returns, then `event=tool` / `event=done`.  `seat_status` shows `sessionId` while the job is still running.  No sessionId within 50s fails the job (`session/new did not return`).  `opts.timeoutSec` is passed as `--timeout` so it bounds `session/prompt` (no hidden 180s cap).

Empty or omitted `mcpServers` loads none on grok-acp (stripped GROK_HOME), not the TUI kitchen sink.

`grok-acp` is `127.0.0.1:12419` only (never 2419).  Do not start a second serve.  Do not start `grok-leader` while a TUI holds `~/.grok/leader.sock`.

### grok-tui

Attach to a **live Mac Grok TUI** chat via the shared leader (`leader-client.py` / `grok-drive.py`).  Requires `opts.sessionId` from `grok_sessions_list`.  Does not create a new `:12419` session.  cwd may be anywhere under `/Users/jay` (the chat already exists).  Do not set a permission mode.

### cwd

New spawns (`deepseek`, `grok`) must exist under `/Users/jay/Code` or `/Users/jay/apps`.  `grok-tui` attach may use the live chat's cwd anywhere under `/Users/jay`.  Workspace-write allowlisting waits; v1 DeepSeek is read-only.

Test adapters (not for Conductor production):  `_echo`, `_sleep`.

## Start

```bash
/Users/jay/apps/seat-mcp/start.sh
```

`ecosystem.snippet.cjs` is a pm2 fragment.  Do not `pm2 start` / `pm2 save` from a one-off dsh or npx session.

## Conductor:  curl

Load the token into the environment without printing it:

```bash
SEAT_MCP_TOKEN="$(fleet-secrets get SEAT_MCP_TOKEN --path /seat-mcp)"
BASE=http://127.0.0.1:8793/mcp
AUTH="Authorization: Bearer $SEAT_MCP_TOKEN"
```

Health (no token):

```bash
curl -sS http://127.0.0.1:8793/health
```

Initialize:

```bash
curl -sS -X POST "$BASE" \
  -H "$AUTH" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"conductor","version":"1"}}}'
```

Launch (returns `jobId` immediately):

```bash
curl -sS -X POST "$BASE" \
  -H "$AUTH" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"seat_launch","arguments":{"seat":"deepseek","prompt":"reply with the single word pong","cwd":"/Users/jay/apps","opts":{"effort":"quick"}}}}'
```

Status / reply / result (replace JOBID):

```bash
curl -sS -X POST "$BASE" -H "$AUTH" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"seat_status","arguments":{"jobId":"JOBID"}}}'

curl -sS -X POST "$BASE" -H "$AUTH" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"seat_reply","arguments":{"jobId":"JOBID"}}}'

curl -sS -X POST "$BASE" -H "$AUTH" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"seat_result","arguments":{"jobId":"JOBID"}}}'
```

Grok launch:

```bash
curl -sS -X POST "$BASE" -H "$AUTH" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"seat_launch","arguments":{"seat":"grok","prompt":"reply with the single word pong","cwd":"/Users/jay/apps"}}}'
```

Grok TUI attach:

```bash
curl -sS -X POST "$BASE" -H "$AUTH" -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{"name":"grok_sessions_list","arguments":{}}}'
```

## What v1.2 does not do

- answering a TUI permission prompt from outside (surface `pendingTool`; the operator decides)
- cwd allowlist for DeepSeek workspace-write
- Shellular spawn / `npx @deepseek-ai/dsh` / TryCloudflare
- putting tokens in `mcp.json` literals (use env placeholders)

After merging AFC, refresh live copies:

```bash
bash scripts/install-grok-tui-drive.sh --restart-seat-mcp
```

## Fleet recall (v1.4)

Three read-mostly tools expose the fleet-agents knowledge corpus (Qdrant `fleet-agents`, self-hosted bge-m3) to every seat-mcp caller, including cloud seats on `https://agents.jays.services/mcp` that cannot run the local `recall` CLI or the `fleet-recall` stdio server.

- `recall_search(query, limit?, category?, app?, source?, seat?, since_days?)` → `{hits: [{score, text, source, app, category, seat, doc_id, chunk_index, heading, title, url, path, created_at}], mode}`
- `recall_stats()` → `{collection, status, points, embedder_healthy, by_source, by_app}`
- `recall_contribute(text, category, app?, seat, title?, url?)` → `{id, doc_id, scrubbed, status}`

Same contract as `scripts/recall` and `scripts/fleet-recall-mcp.py`.  The implementation is `fleet_rag.recall_api` in the installed package at `/Users/jay/apps/fleet-rag`; `seat_mcp/recall_bridge.py` only validates arguments, imports it by name (guarded `sys.path` insert, `FLEET_RAG_HOME` override), and maps `FleetRagError` / `ValueError` onto `SeatError`.  If the package is missing every recall tool returns a `SeatError` that says to run `bash scripts/install-fleet-rag.sh`.

Rules on this surface:

- `seat` is **required** for `recall_contribute`.  Cloud callers have no `AGENT_SEAT`, and the seat-mcp process's own environment is never attributed to them.  Pass your uppercase tag (`GROK`, `CURSOR`, `CLAUDE`, …).
- `category` for contributions is `lesson|preference|infrastructure|decision|runbook` only.  Text is 40..4000 chars, scrubbed, then gated by gitleaks when it is on PATH.
- Reads use the Qdrant read-only key when it is available; contributions need the write key, which `fleet_rag.core.load_config(need_write=True)` resolves from the environment or Infisical shared/prod.  Nothing about keys is ever returned to the caller.

Install / refresh (after merging AFC):

```bash
bash scripts/install-fleet-rag.sh --with-seat-mcp   # copies tools.py + recall_bridge.py into ~/apps/seat-mcp
pm2 restart seat-mcp
```

Tests:  `cd scripts && python3 -m unittest fleet_rag.tests.test_seat_mcp_recall -v`.

## Webhook proxy (`/hooks/`)

`POST` (and `GET`) `/hooks/<endpoint>/<secret>` is passed through, unauthenticated, to BotFleet's webhook-only ingress on `127.0.0.1:8800`, with the body and headers (minus `Host` and `Connection`) intact.  The ingress checks the capability secret and answers;  seat-mcp relays its status and body unchanged, or answers `502` when the ingress is down.  Because the route is unauthenticated, seat-mcp bounds each request first:  a POST needs a `Content-Length` (`411` without one, and for chunked bodies), a bad or negative length is `400`, a body over 2 MB (the same cap as the MCP and `/recall/*` paths) is `413` without being read, and a body that stalls for 30 seconds is `408`.  This is how GitHub, Sentry, PagerDuty and Congress.Trade reach BotFleet webhook triggers through the public seat-mcp route.

The path IS the credential, so the request log writes `/hooks/<redacted>` and never the endpoint id or the secret (AFC-359).  The capability URLs are recorded in Infisical Fleet-OPS (`/botfleet-github-webhooks`, `/botfleet-firefighter-webhooks`, `/global-api-keys`);  rotate one with BotFleet's `POST /api/webhooks/<id>/rotate` and then move every sender to the new URL.

Tests:  `cd scripts/seat-mcp && python3 -m unittest test_hooks_proxy test_recall_http test_main_token -v`.
