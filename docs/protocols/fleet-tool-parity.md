# Fleet Tool Parity

Every fleet capability exists on three surfaces:  the hosted Agent-Sync MCP server (`https://agent-sync.jays.services/mcp`, for cloud seats), the local tools (the stdio `agent-sync mcp` server and the `agent-sync`, `board` and `fleet-share` commands), and the Hetzner server, where Codex and Claude run the same local tools.  Five commands are the tool set on both hosts:  `agent-sync`, `board`, `recall`, `lane` and `fleet-mode` (`fleet-share` and `fleet-tools-doctor` come with the FleetLink checkout and this repo).  `apple-notes-coding.sh` is Mac-only by design.  This page is the rule for keeping them equal:  one contract per service, a CI test that fails when a surface drifts from it, and a doctor that checks each host.

## Contracts

| Service | The contract | Who reads it | The test that pins it |
| --- | --- | --- | --- |
| Zulip | `scripts/agent_sync/mcp/tools.json` (the stdio names; the hosted server serves them with a `zulip_` prefix) | stdio MCP (`tools.py`), hosted MCP (`contract.js` imports it), the CLI | `test_mcp_stdio.py` (tools/list equals the file), `hosted-tools.test.mjs`, the shared `fixtures.jsonl`, and the CLI mapping test that every contract tool has an `agent-sync` command (AFC-330) |
| Linear | The hosted catalogs in `scripts/agent-sync-mcp/src` (named commands, official Linear MCP tools, public inputs), exported to `scripts/linear-board/catalog/*.json` | hosted MCP (the JS), `board` (the JSON) | `linear-catalog-parity.test.mjs` (the committed JSON equals a fresh export) and `linear-parity.test.mjs` (constants) (AFC-329) |
| FleetLink | FleetLink's served MCP tool list, `docs/mcp-tools.json` in the FleetLink repo | FleetLink MCP, `fleet-share` (`MCP_TO_CLI`), hosted Agent-Sync (`scripts/agent-sync-mcp/fleetlink-tools.json`, a copy, mirrored in `src/fleetlink.js`), and stdio Agent-Sync, which mirrors hosted through `scripts/agent_sync/mcp/fleetlink-tools.json` (generated from `src/fleetlink.js`) and runs each tool as `fleet-share tool NAME` with the seat's own FleetLink token | FleetLink `test/cli-mcp-parity.test.mjs`;  here `test/fleetlink.test.mjs` (the hosted specs equal the copy, and the generated stdio JSON equals the hosted specs) and `test_mcp_fleetlink.py` (stdio serves that JSON) |
| Listener records | `scripts/agent_sync/live.py` (the layout) and the listener read API | `agent-sync listener …`, the stdio and hosted `listener_*` tools | the listener tests (AFC-330) |

`fleet-share tool NAME` and `board run NAME` reach any MCP tool or named command directly, so a capability is never missing from a CLI while its friendlier command is being written.

## When you change something

- **A Zulip tool.**  Change `tools.json` and both implementations (`tools.py`, `hosted-tools.js`) in one PR, add a golden case to `fixtures.jsonl`, and give the CLI the matching command.  CI fails otherwise.
- **A Linear tool or command.**  Change the JS catalog, run `node scripts/agent-sync-mcp/scripts/export-linear-catalog.mjs`, and commit the regenerated JSON with the change.  `board` picks it up from the JSON;  a new convenience flag still needs code in `board`.
- **A FleetLink tool.**  In FleetLink:  add the CLI command to `fleet-share` and to `MCP_TO_CLI`, then run `node scripts/export-mcp-tools.mjs`.  Here:  copy that file to `scripts/agent-sync-mcp/fleetlink-tools.json`, mirror the tool in `src/fleetlink.js`, and run `node scripts/export-fleetlink-tools.mjs` in `scripts/agent-sync-mcp` (the stdio contract), adding the tool to `EXTERNAL_CLI` in `scripts/agent_sync/mcp/tools.py`.  Both repos' CI fail until each side matches.
- **Anything a release ships** (`scripts/agent_sync/`, `scripts/linear-board/`, `scripts/agent-sync`, the catalogs, `scripts/recall` and its ten `fleet_rag` modules, `scripts/fleet_lanes/` and its five modules, `scripts/fleet-mode`, `docs/FLEET-MODE.md`):  merging is not enough, and a new module that one of these tools imports must be added to the receiver allowlist or the tool fails on the server (the release tests catch it for `recall`).  The Mac's `com.jay.agent-sync-runtime-sync` job publishes the runtime and pushes it to the server (`docs/protocols/coolify-fleet-cli-sync.md`), and the hosted Worker is deployed by hand (`scripts/agent-sync-mcp/DEPLOY.md`).  A change is done when both are live.

## Hosts

**The Hetzner server.**  `agent-sync`, `board`, `recall`, `lane` and `fleet-mode` run straight from a git checkout of AFC main at `~/.local/share/fleet-cli/repo` (since 2026-10-19;  `docs/protocols/fleet-cli-from-git.md`).  A merge to main signals the server to move it after a smoke test, and an hourly timer is the safety net.  The Mac no longer pushes release copies.  `fleet-share` (`~/.local/bin/fleet-share`) runs FleetLink's CLI from `/root/Code/FleetLink`, which `code-main-sync.timer` keeps on `origin/main` every 10 minutes along with the other clean checkouts.  Each seat that works here (CLAUDE, CODEX) reads its credentials from Infisical through `fleet-secrets`, the same as on the Mac:  its Zulip bot from Fleet-OPS `/zulip`, its Linear app from Fleet-OPS `/linear`, and its FleetLink bot token (`FLEETLINK_TOKEN_<SEAT>`) from the AI Fleet Coordinator project's `/fleetlink` (`fleet-secrets names --path /fleetlink --project ai-fleet-coordinator`).  No credential file is read for Zulip;  `~/.secrets/Zulip` is retired.  `fleet-secrets` (installed by `scripts/install-fleet-secrets.sh`) has no keychain on Linux, so it logs in with the server's universal-auth machine identity, `INFISICAL_AUTOMATION_CLIENT_ID` and `INFISICAL_AUTOMATION_CLIENT_SECRET` in the environment (auth mode `automation-env`;  `fleet-secrets doctor` shows it, booleans only).  Claude Code inherits the pair from its launcher, and Codex's `agent-sync` entry forwards both names with `env_vars`.  The project ids are read from `~/.agent-sync/fleet-ops-project-id` and `~/.agent-sync/ai-fleet-coordinator-project-id`.  Codex (`~/.codex/config.toml`) and Claude Code (`~/.claude.json`, user scope) register the `agent-sync` stdio server with their own default seat and no credential `env`.  They also register the `fleetlink` HTTP server with their bot token.

**What the server must provide for the tools a release cannot carry.**  `recall` needs its corpus credentials in the environment or a machine-identity file (`docs/protocols/coolify-fleet-cli-sync.md` lists the names), and `gitleaks` for `contribute`;  `lane` needs `git` and, for PR reviews, `gh`.  No credential ever ships in a release.

**The Mac.**  The same commands from the published runtime and the FleetLink checkout (`lane` and `fleet-mode` live in `~/apps`, `recall` in `~/.local/bin`);  seats' credentials from their own files as today.

## The doctor

```bash
python3 scripts/fleet-tools-doctor.py --seats CLAUDE,CODEX
```

One line per check, OK, WARN or FAIL, and exit 1 on a FAIL.  It covers:
- whether the server's git checkout is behind `origin/main` (or, on a host still on release copies, whether the release is behind in any shipped file);
- whether the CLI checkouts are on `origin/main`;
- that the five fleet tools and `fleet-share` are on PATH (or in `~/apps` on the Mac);
- that the files `recall`, `lane` and `fleet-mode` run from ship with the runtime;
- that the Mac's `~/apps/FLEET-MODE.md` equals `docs/FLEET-MODE.md` in the repo (a WARN when it differs);
- that the shared catalogs ship with the runtime;
- each named seat's credentials (key names in Infisical, through `fleet-secrets`);
- each client's MCP registrations.

On the server, `fleet-tools-doctor.timer` runs it every hour as CLAUDE with `--post`.  It posts to #agent-sync, topic `fleet tools doctor`, only when the set of failing checks changes, including the all-clear.
