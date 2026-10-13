# grok-acp-runtime

Always-on **localhost-only** Grok ACP.

- `pm2 grok-leader` — shared backend, `~/.grok/leader.sock`, `--always-approve --no-exit-on-disconnect`.  When a TUI already holds the socket, `leader.sh` exits 75 and pm2 parks the job `stopped` (`stop_exit_codes: [75]`).  Do not `pm2 restart grok-leader` while `/usr/sbin/lsof ~/.grok/leader.sock` shows a live grok.
- `pm2 grok-acp` — WebSocket `127.0.0.1:12419` (`--no-leader serve`).  `--leader serve` does **not** bind a port.  `start.sh` sets `GROK_HOME` to `acp-home/` (stripped config, no kitchen-sink MCPs, no plugins).  TUI / grok-leader keep `~/.grok`.  New Conductor sessions pass only the MCP names they asked for.
- Shellular Grok Build: `~/.grok/bin/grok agent --always-approve --leader stdio`
- New local `grok` TUI: `[cli] use_leader = true` in `~/.grok/config.toml`
- List/load chats: `python3 ~/apps/grok-acp-runtime/leader-client.py list`
- Drive a **live TUI** chat: `python3 ~/apps/grok-acp-runtime/grok-drive.py list` then `prompt --session-id ID --cwd DIR --prompt "…"`.  Prompt uses `session/resume` (not `session/load` — load hangs on a chat the TUI already has open) and returns once the TUI **queues** the message (`queued: true`).  Pass `--await-reply N` to wait for the **next** turn on disk (not a pre-existing idle).  Pass `--wait` only if you want the ACP turn's text.  Peek reads `summary.json` on disk.  `prompt` refuses `$GROK_SESSION_ID` unless `--self`.  `close` is `session/close`: unloads that chat's MCP tools and keeps `~/.grok/sessions/...` on disk (`/resume` still works).
- Hourly idle unload: `python3 ~/apps/grok-acp-runtime/grok-idle-unload.py` (launchd `com.jay.grok-idle-unload`).  A chat is a candidate only when it holds MCP processes under the shared leader (found with sysctl `KERN_PROCARGS2`, never logged).  Zero-turn stubs with no attached client get `session/close` after **30 minutes** idle (`GROK_IDLE_UNLOAD_STUB_MINUTES`, 0 = off); every other chat after **4 hours** (`GROK_IDLE_UNLOAD_HOURS` or `--max-age-hours`).  Idle counts from the newest of summary `updated_at`, the last turn, and the last load, so a chat resumed today is not unloaded before its first turn.  "Attached" means a non-leader `grok` process (Shellular, BotFleet, a TUI) has the chat's working directory as its own; the leader exposes no connection list.  Working / needs-input / pendingTool / this TUI / chats with background work (a shell or any process forked after the MCP load window, or any process outside the leader) are skipped.  Each close is re-checked on fresh data right before it runs.  Transcripts stay; `/resume` or `grok --resume ID` reloads tools.  `--dry-run --verbose` lists candidates and every skip without closing.  MCP children are signalled directly only after `closeOutcome: notResident`, never after a timeout or refusal.  The leader's `session/list` is not called unless `--use-list` (or `GROK_IDLE_UNLOAD_USE_LIST=1`) asks for it: candidates come from the process table and disk.  Each leader helper call gets `GROK_IDLE_UNLOAD_HELPER_TIMEOUT_SEC` (180s); a timeout logs one timestamped JSON line on stderr and exits 75.  Tests: `python3 scripts/test_grok_idle_unload.py`.
- After merging AFC copies, run `bash scripts/install-grok-tui-drive.sh` so `~/apps/` is not stale.
- Cloud agents do not run this CLI.  They call `https://agents.jays.services/mcp` (Access + Bearer).
- Bind is loopback only.  Never `:2419`.

Auth token lives in `~/.secrets/grok-acp.env` (`GROK_AGENT_SECRET`).
Never print it.  Clients send it as:

- `Authorization: Bearer <token>`, or
- query `?server-key=<token>`

## Conductor: start a session and send a follow-up

```bash
# 1) confirm the adapter is up
lsof -nP -iTCP:12419 -sTCP:LISTEN
pm2 show grok-acp

# 2) new session + first prompt (prints sessionId)
# no MCPs (stripped grok-acp home)
python3 /Users/jay/apps/grok-acp-runtime/acp-client.py new \
  --cwd /Users/jay \
  --prompt "Your first message"

# one server from ~/.grok/config.toml (name allow-list)
python3 /Users/jay/apps/grok-acp-runtime/acp-client.py new \
  --cwd /Users/jay \
  --mcp-server github \
  --prompt "Your first message"

# 3) follow-up on that session
python3 /Users/jay/apps/grok-acp-runtime/acp-client.py prompt \
  --session-id <SESSION_ID> \
  --prompt "Your follow-up"
```

Handshake-only check (no model turn):

```bash
python3 /Users/jay/apps/grok-acp-runtime/leader-client.py handshake
python3 /Users/jay/apps/grok-acp-runtime/leader-client.py list
python3 /Users/jay/apps/grok-acp-runtime/leader-client.py load \
  --session-id <SESSION_ID> --cwd /Users/jay/Code
python3 /Users/jay/apps/grok-acp-runtime/grok-drive.py list
python3 /Users/jay/apps/grok-acp-runtime/grok-drive.py peek \
  --session-id <SESSION_ID> --cwd /Users/jay/Code
```

`acp-client.py` is the Conductor WebSocket helper for **new** sessions on `:12419`.  It needs the `websockets` package (`/usr/bin/python3` on this Mac).  `--mcp-server NAME` expands names from **user** `~/.grok/config.toml` into ACP `mcpServers` objects.  Empty list means no MCPs on grok-acp (the serve process does not load the TUI kitchen sink).  The client auto-selects an offered allow option on `session/request_permission` and implements ACP `terminal/*` so `run_terminal_command` can finish.  Unknown server methods return JSON-RPC `-32601` (never an empty `{}`).  One WebSocket pump for the connection.  stdout is NDJSON: `event=session` (sessionId, flushed) as soon as `session/new` returns, then `event=tool` / `event=done`.  `--timeout` bounds `session/prompt`.  Local chat control uses `leader-client.py` / `grok-drive.py` (stdio, stdlib only).  TUI chats keep the full MCP set.  There is no TUI picker.

## What this can and cannot do

- Can list every Grok session on disk and `session/load` one (always-approve).
- New TUI / Shellular Grok join the shared leader.  A TUI already running before the leader started is listed, but is not live-attached until that TUI restarts.
- Cannot screenshot a terminal.  Do not bind `:2419`.  Do not `session/load` a chat that is mid-turn in another window unless you intend to share it.

## Restart

```bash
pm2 start /Users/jay/apps/pm2-ecosystem.config.cjs --only grok-acp
pm2 save
```

- Any local agent (not Grok-Bot-only): `grok-drive.py list|peek|tail|prompt|await|cancel`.  `prompt --from-name SEAT` prefixes `[from: SEAT]`.  Refuses working/needs-input unless `--queue`.  Refuses this TUI unless `--self`.  `--await-reply` waits for the next turn after inject.  `list`/`peek` include `pendingTool` when a permission prompt is waiting.  Do not auto-deny.
