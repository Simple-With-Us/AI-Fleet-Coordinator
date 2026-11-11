# DSH web :3080 on Tailscale + Shellular Thinking hang — 2026-09-01

## Port

DeepSeek Harness web UI listens on **127.0.0.1:3080** (`dsh web`, default
`ctx.webStartup.port ?? 3080`).  Tailscale Serve publishes it as:

`https://macbook.boa-roygbiv.ts.net:3080`

Tailnet only (no Funnel).  IPv4: `100.113.106.39` is the Mac.  Browser-trust
fence also accepts that host.  Loopback stays bound; do not listen on `0.0.0.0`.

pm2 job `dsh-web` runs `~/apps/dsh-runtime/start-web.sh`.  Serve mapping is
re-asserted on each start via `serve-tailscale.sh`.

## Shellular hang

Symptom: iOS lists DeepSeek sessions, a prompt is sent, then **Thinking**
forever.  Live evidence 2026-09-01: `session-ff83ef86` under BotFleet ran 54
headless tool steps with `approval: never` and wrote ~1MB of session jsonl,
while `dsh-acp.py` had **no child** and Shellular showed no tokens.

Root cause (not the API, not `approval: ask` — that was the 2026-08-23 fix):

1. `dsh --profile headless` emits **nothing until the final answer**.  ACP
   `session/prompt` therefore never gets `session/update` chunks, so iOS stays
   on Thinking.
2. The child inherited ACP stdin (no `stdin=DEVNULL`).  Node can sit on the
   same fd Shellular is writing JSON-RPC to.
3. Timeout used `proc.kill()` only, not the process group, and blocked on
   `for line in proc.stdout`.  If a grandchild held the pipe, `endTurn` never
   arrived — forever, not 300s.

## Fix

`scripts/dsh-acp.py` 1.3.0:

- `stdin=DEVNULL` + `start_new_session=True`
- Immediate `DeepSeek started.` chunk, then `[working… Ns]` heartbeats
- Process-group SIGTERM/SIGKILL on timeout and `session/cancel`
- `--resume` when the ACP session id is a Harness `session-*` id
- Default timeout 900s (`DSH_ACP_TIMEOUT_SEC`)

Install: copy `scripts/dsh-acp.py` to `~/apps/dsh-runtime/dsh-acp.py` (Shellular
spawns a fresh child per agent session; no pm2 restart of `shellular` required
for the bridge).  `dsh-web` does need pm2 start.

## Verify

```bash
python3 scripts/test_acp_bridges.py
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3080/
tailscale serve status
```
