# Grok idle MCP unload 36h → 12h

Board `09102247`.  Branch `grok/idle-unload-12h`.  Follow-up to #171.

## Why

Owner: 12 hours is enough, as long as resume is easy.  Shorter than 12h may be better later.

`session/close` already keeps `~/.grok/sessions/...`.  `/resume` in the TUI, or `grok --resume <id>`, reloads MCP tools on the next turn.  Nothing is deleted.

## What

- Default `DEFAULT_IDLE_UNLOAD_SEC` and `--max-age-hours` are **12**.
- LaunchAgent sets `GROK_IDLE_UNLOAD_HOURS=12`.  Change that env (or pass `--max-age-hours`) to try 6h without another code change.
- Docs / skill / Mac process list updated.

## Verify

```bash
python3 scripts/test_session_disk.py
python3 ~/apps/grok-acp-runtime/grok-idle-unload.py --dry-run
```

Resume check: after a close, `python3 ~/apps/grok-acp-runtime/grok-drive.py peek --session-id ID` still finds the disk summary; `grok --resume ID` starts a new live attach.
