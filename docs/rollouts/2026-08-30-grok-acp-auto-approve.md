# 2026-08-30 — grok-acp auto-approve + ACP terminals

Board `e1dc9024`.  Branch `grok/acp-auto-approve`.  Worktree `~/apps/fleet-grok-acp-auto-approve`.

## Why

github-only Conductor jobs (including the ST #3120 rebase attempt) hung after `git status` / `gh pr view`.  Isolation was already fine (`mcp_wait_ms: 0`).  Two client bugs remained:

1. `session/request_permission` was ignored in the tracked client, or answered with a hardcoded `allow-always` that is not in the offered options (`allow-once` / `reject-once`).
2. Grok's `run_terminal_command` uses ACP `terminal/create`.  The client advertised `terminal` and then replied `{}`, which Grok reports as `failed to deserialize response`, or waits until the 900s `session/prompt` timeout.

`--always-approve` / `_meta.yoloMode` still emit ACP permission requests.  The client has to answer them.

Did not rebase ST #3120.  Did not restart `grok-leader` (this TUI holds `~/.grok/leader.sock`).

## What changed

- `scripts/grok-acp-runtime/acp-client.py` — pick an offered allow option (`allow_always` then `allow_once`); implement `terminal/create|output|wait_for_exit|kill|release`; JSON-RPC `-32601` for unknown server methods; treat only id+result/error (no method) as client replies so server request ids cannot collide with pending prompts.
- `scripts/grok-acp-runtime/acp-home-config.toml` — `[ui] permission_mode = "always-approve"`.
- Unit tests in `scripts/test_acp_client.py` (no live serve).

## Verify

```bash
/usr/bin/python3 scripts/test_acp_client.py
/usr/bin/python3 ~/apps/grok-acp-runtime/acp-client.py handshake
# throwaway cwd — not ~/Code/Socratic.Trade
/usr/bin/python3 ~/apps/grok-acp-runtime/acp-client.py new \
  --cwd /tmp \
  --prompt 'Run the shell command: echo grok-acp-perm-ok. Reply with only that output.' \
  --timeout 90
```

Live 2026-08-30 after `pm2 restart grok-acp` (leader left stopped):

- Handshake ok on `127.0.0.1:12419`.
- Session `01a05117-778a-7110-bf5b-8285c7eef165` cwd `/tmp`: `echo grok-acp-perm-ok` returned in 12.7s.  `permission_requested` then `permission_resolved` allow `wait_ms: 0`, `tool_completed` 71ms success.  Follow-up `git --version && pwd` returned `git version 2.50.1` and `/private/tmp` in 3.6s, same permission/tool success path.  No `failed to deserialize response`.

Install after merge:

```bash
bash scripts/install-grok-tui-drive.sh
pm2 restart grok-acp
# do not pm2 restart grok-leader
```
