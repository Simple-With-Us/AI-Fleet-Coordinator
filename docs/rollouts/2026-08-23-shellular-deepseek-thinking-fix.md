# Shellular iOS DeepSeek "thinking" hang — 2026-08-23

## Symptom

Shellular iOS lists DeepSeek sessions but a new prompt stays on "DeepSeek is thinking…"
forever.  Session list works; response generation does not complete.

## Root cause (not the DeepSeek API)

Shellular spawns `~/apps/dsh-runtime/dsh-acp.sh`, which runs **DeepSeek Harness**
(`dsh --profile headless`), not a bare chat completion.

Harness headless profile defaults to `DSH_PERMISSION_MODE=workspace-write`, which sets
`approval: ask` on bash/fs tools.  When the model invokes a tool, Harness waits for an
interactive approval that Shellular iOS cannot supply.  The ACP bridge never receives
`stopReason: endTurn`, so the phone UI never leaves the thinking state.

Simple text-only prompts ("Say OK") can still work when the model skips tools.

## Fix

1. `dsh-acp.sh` exports `DSH_PERMISSION_MODE=danger-full-access` (approval: never).
2. `~/.shellular/agents.json` deepseek entry includes the same env var.
3. `scripts/dsh-acp.py` also sets the default on the child `dsh` process, adds
   `session/cancel`, stderr draining, and a 300s timeout (`DSH_ACP_TIMEOUT_SEC`).

Deploy tracked copies to the live Mac:

```bash
cp AI-Fleet-Coordinator/scripts/dsh-acp.py ~/apps/dsh-runtime/dsh-acp.py
cp AI-Fleet-Coordinator/scripts/dsh-acp.sh ~/apps/dsh-runtime/dsh-acp.sh
```

No pm2 restart required — Shellular spawns a fresh ACP child per agent session.

## Verify

```bash
# Simple (no tools) — should finish in seconds
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1}}' \
  '{"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"/Users/jay/Code/Socratic.Trade"}}' \
  '{"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{"sessionId":"SAME_ID","prompt":[{"type":"text","text":"Say exactly OK"}]}}' \
  | ~/apps/dsh-runtime/dsh-acp.sh

# Tool path — should complete with stopReason, not hang
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1}}' \
  '{"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"/Users/jay/Code/Socratic.Trade"}}' \
  '{"jsonrpc":"2.0","id":3,"method":"session/prompt","params":{"sessionId":"SAME_ID","prompt":[{"type":"text","text":"Run git status -sb and summarize in one line"}]}}' \
  | ~/apps/dsh-runtime/dsh-acp.sh
```

Use the **same** `sessionId` in `session/new` and `session/prompt` (Shellular does this).

## Not the issue

- Autorotate → Autorotate rename (unrelated to DeepSeek Harness).
- `fleet-ops` repo (attack map only; no DeepSeek integration).
- Streaming `[DONE]` on a raw API call (Shellular path is Harness + ACP, not OpenAI-style SSE).

## Related

- `docs/MAC-LOCAL-PROCESSES.md` — `~/apps/dsh-runtime/`
- `~/apps/dsh-runtime/README.md`
- Antigravity parallel: `agy-acp-turbo.sh` uses `--dangerously-skip-permissions`
