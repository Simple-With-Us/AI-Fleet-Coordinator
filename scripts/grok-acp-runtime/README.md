# Grok Leader & TUI Drive Helpers

Standalone `pm2 grok-acp` (port `12419`, `start.sh`, `acp-client.py`,
`acp-home-config.toml`) and `seat-mcp` (port `8793`) were retired in `AFC-926`.

This directory now holds only the `grok-leader` / `grok-drive.py` helpers used
by the shared Operator wake router and Mac TUI session sharing:

- `grok-drive.py` — drives a live Grok TUI session through the leader socket
- `leader-client.py` — client for `~/.grok/leader.sock`
- `session_disk.py` — session disk lookup helper
- `grok-idle-unload.py` — idle session cleanup helper (`com.jay.grok-idle-unload`)

Install/update the live copies under `~/apps/grok-acp-runtime` with:

```bash
bash scripts/install-grok-tui-drive.sh
```
