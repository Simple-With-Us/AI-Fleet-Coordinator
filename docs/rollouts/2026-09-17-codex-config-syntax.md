# Codex config.toml MCP quoting (2026-09-17)

AG leftover chat `39d195c7`.  Board `080d51bd`.  Branch `grok/codex-config-syntax`.

## Error

```
Error loading configuration: ~/.codex/config.toml:172:82:
missing comma between array elements, expected `,`
args = [ "-c", "npx -y mcp-remote … --header \\"Authorization: Bearer $GITHUB_MCP_TOKEN\\"" ]
```

TOML basic strings treat `\\` as one backslash and the next `"` as end-of-string.  `Authorization` then sits outside the array element.

## Fix

Fleet form (token stays in the launch script, not on argv, not in TOML):

```toml
[mcp_servers.github]
command = "/Users/jay/apps/mcp-servers/github-mcp-launch.sh"
args = []
startup_timeout_sec = 120
enabled = false
```

Same pattern for `mcp_servers.render` → `render-mcp-launch.sh`.

`scripts/install-fleet-rag.sh` `toml_cfg` now `tomllib`-parses the destination file and prints `skipped-invalid-toml` instead of appending to a file Codex cannot load.

## Verify

```bash
python3 -m unittest scripts.fleet_rag.tests.test_codex_mcp_quoting -v
bash scripts/fleet_rag/tests/test_installer.sh
python3 -c "import tomllib; tomllib.load(open('$HOME/.codex/config.toml','rb'))"
```
