# Fleet Tool Parity

Every fleet capability exists on three surfaces:  the hosted Agent-Sync MCP server (`https://agent-sync.jays.services/mcp`, for cloud seats), the local tools (the stdio `agent-sync mcp` server and the `agent-sync`, `board` and `fleet-share` commands), and the Hetzner server, where Codex and Claude run the same local tools.  This page is the rule for keeping them equal:  one contract per service, a CI test that fails when a surface drifts from it, and a doctor that checks each host.

## Contracts

| Service | The contract | Who reads it | The test that pins it |
| --- | --- | --- | --- |
| Zulip | `scripts/agent_sync/mcp/tools.json` (the stdio names; the hosted server serves them with a `zulip_` prefix) | stdio MCP (`tools.py`), hosted MCP (`contract.js` imports it), the CLI | `test_mcp_stdio.py` (tools/list equals the file), `hosted-tools.test.mjs`, the shared `fixtures.jsonl`, and the CLI mapping test that every contract tool has an `agent-sync` command (AFC-330) |
| Linear | The hosted catalogs in `scripts/agent-sync-mcp/src` (named commands, official Linear MCP tools, public inputs), exported to `scripts/linear-board/catalog/*.json` | hosted MCP (the JS), `board` (the JSON) | `linear-catalog-parity.test.mjs` (the committed JSON equals a fresh export) and `linear-parity.test.mjs` (constants) (AFC-329) |
| FleetLink | FleetLink's served MCP tool list, `docs/mcp-tools.json` in the FleetLink repo | FleetLink MCP, `fleet-share` (`MCP_TO_CLI`), Agent-Sync (`scripts/agent-sync-mcp/fleetlink-tools.json`, a copy) | FleetLink `test/cli-mcp-parity.test.mjs`;  here `test/fleetlink.test.mjs` (the hosted specs equal the copy) |
| Listener records | `scripts/agent_sync/live.py` (the layout) and the listener read API | `agent-sync listener …`, the stdio and hosted `listener_*` tools | the listener tests (AFC-330) |

`fleet-share tool NAME` and `board run NAME` reach any MCP tool or named command directly, so a capability is never missing from a CLI while its friendlier command is being written.

## When you change something

- **A Zulip tool.**  Change `tools.json` and both implementations (`tools.py`, `hosted-tools.js`) in one PR, add a golden case to `fixtures.jsonl`, and give the CLI the matching command.  CI fails otherwise.
- **A Linear tool or command.**  Change the JS catalog, run `node scripts/agent-sync-mcp/scripts/export-linear-catalog.mjs`, and commit the regenerated JSON with the change.  `board` picks it up from the JSON;  a new convenience flag still needs code in `board`.
- **A FleetLink tool.**  In FleetLink:  add the CLI command to `fleet-share` and to `MCP_TO_CLI`, then run `node scripts/export-mcp-tools.mjs`.  Here:  copy that file to `scripts/agent-sync-mcp/fleetlink-tools.json` and mirror the tool in `src/fleetlink.js`.  Both repos' CI fail until each side matches.
- **Anything a release ships** (`scripts/agent_sync/`, `scripts/linear-board/`, `scripts/agent-sync`, the catalogs):  merging is not enough.  The Mac's `com.jay.agent-sync-runtime-sync` job publishes the runtime and pushes it to the server (`docs/protocols/coolify-fleet-cli-sync.md`), and the hosted Worker is deployed by hand (`scripts/agent-sync-mcp/DEPLOY.md`).  A change is done when both are live.

## Hosts

**The Hetzner server.**  `agent-sync` and `board` run from the fleet-cli release the Mac pushes;  the server never fetches code for them itself.  `fleet-share` (`~/.local/bin/fleet-share`) runs FleetLink's CLI from `/root/Code/FleetLink`, which `code-main-sync.timer` keeps on `origin/main` every 10 minutes along with the other clean checkouts.  Each seat that works here (CLAUDE, CODEX) holds its own credentials:  `~/.secrets/Zulip/<Seat>-zuliprc` (from Infisical `/zulip`), its Linear app (`~/.secrets/Linear/<SEAT>.env` or `LINEAR_<SEAT>_CLIENT_ID`/`_SECRET` in the environment), and its FleetLink bot token, `~/.secrets/FleetLink/<SEAT>.env` (from Infisical `/fleetlink`, `FLEETLINK_TOKEN_<SEAT>`).  Codex (`~/.codex/config.toml`) and Claude Code (`~/.claude.json`, user scope) register the `agent-sync` stdio server with their own default seat, and the `fleetlink` HTTP server with their bot token.

**The Mac.**  The same commands from the published runtime and the FleetLink checkout;  seats' credentials from their own files as today.

## The doctor

```bash
python3 scripts/fleet-tools-doctor.py --seats CLAUDE,CODEX
```

One line per check, OK, WARN or FAIL, and exit 1 on a FAIL.  It covers:
- whether the installed release is behind `origin/main` in any shipped file;
- whether the CLI checkouts are on `origin/main`;
- that the three commands are on PATH;
- that the shared catalogs ship with the runtime;
- each named seat's credentials;
- each client's MCP registrations.

On the server, `fleet-tools-doctor.timer` runs it every hour as CLAUDE with `--post`.  It posts to #agent-sync, topic `fleet tools doctor`, only when the set of failing checks changes, including the all-clear.
