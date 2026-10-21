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
python3 scripts/fleet-tools-doctor.py --json                      # the document Muster and fleet_config read
python3 scripts/fleet-tools-doctor.py --remote fleet-tools-ro --write -   # Mac and server together
```

One line per check, OK, WARN or FAIL, and exit 1 on a FAIL on this host.  It covers:
- whether the runtime is at the tip of AFC main on GitHub (the server's git checkout, which `current` must resolve to;  the Mac's managed runtime;  or, on a host still on release copies, whether the release is behind in any shipped file);
- the job that keeps it there:  the Mac's `com.jay.agent-sync-runtime-sync` LaunchAgent (loaded, not paused, exiting cleanly, its run counter moving), and the server's `fleet-cli-git-update.timer` and `fleet-tools-doctor.timer` (active and fired within 70 minutes);
- each fleet tool's currency (`tool:<name>`):  on the Mac, `agent-sync` and `board` run the runtime;  the `recall`, `fleet-mode` and `fleet-secrets` copies equal main blob for blob (a copy that differs names the newest commit it matches and how many later commits touch it, or says "local edit" when main never held it);  and `lane` is what `install_tools plan tools` says.  On the server, each wrapper execs `current/<script>`, `fleet-secrets` points at `linux-entrypoint.py`, and `fleet-share` runs a FleetLink checkout on main.  `fleet-share` on the Mac is presence only, so it reads `unknown`;
- whether the CLI checkouts are on `origin/main` (on the Mac these read `~/Code`, the human tree, so they are tagged `human-tree` and left out of Muster's verdict);
- that the five fleet tools and `fleet-share` are on PATH (or in `~/apps` on the Mac);
- that the files `recall`, `lane` and `fleet-mode` run from ship with the runtime;
- that the Mac's `~/apps/FLEET-MODE.md` equals `docs/FLEET-MODE.md` in the repo (a WARN when it differs);
- that the shared catalogs ship with the runtime;
- every tool in the registry (below), on each host;
- each named seat's credentials (key names in Infisical, through `fleet-secrets`);
- each client's MCP registrations.

On the server, `fleet-tools-doctor.timer` runs it every hour as CLAUDE with `--post`.  It posts to #agent-sync, topic `fleet tools doctor`, only when the set of failing checks changes, including the all-clear.  Since AFC-461 that set also holds a required registry tool the server lacks (`gitleaks` today) and a fleet tool that is behind past its grace window.

### States

Every row keeps `check`, `status` and `detail` and adds `state`, `category`, `tool`, `installed`, `expectedRev`, `behindCommits` and `reason`;  registry rows add `version`, `latest`, `latestSource`, `required`, `path` and `basis`.

| `state` | `status` | Means |
| --- | --- | --- |
| `current` | OK | Observed equal to the tip of main read from GitHub in this run, or to the latest version |
| `stale` | WARN within grace, else FAIL (a registry tool is always WARN) | Installed but behind or different;  `grace` is true while the updater is expected to catch up |
| `missing` | FAIL | A required command, file, wrapper or timer is absent |
| `unknown` | WARN | The doctor could not tell:  no network, a failed probe, an unrecognised answer, or an unreachable host |
| `absent` | OK | An optional tool that is not installed (informational) |

A fleet tool is never `current` from a local ref:  the expected revision is always `refs/heads/main` read with `git ls-remote` in the same run, and `--no-network` turns those rows `unknown`.  The Mac's runtime shares its object store with `~/Code` and every lane, so "the tip is already fetched" says nothing about the updater;  instead, behind is amber while the updater is healthy and its run counter moved since the previous report, and red once the runtime is still behind the same tip a report later.  A tip that failed the Mac's smoke test is amber (the updater waits for main to move);  on the server it is red.

### The tool registry

`TOOL_REGISTRY` at the top of `scripts/fleet-tools-doctor.py` is the list.  Each row names the command, its kind (`platform-cli` or `dev-tool`), and per host whether it is required (absent is `missing`, a FAIL) or optional (absent is `absent`).  A host that is not named does not check it.

- **Platform CLIs:**  `claude`, `codex`, `opencode`, `muse`, `cursor-agent`, `grok`, `agy`, `mcode`, `fx` and `clutch` are required on the Mac, and only `claude` and `codex` on the server (the two seats that work there).  `mmx` and `copilot` are optional everywhere, `kimi` is a retired seat and `droid` and `gemini` have no seat, so those are informational.
- **Dev tools:**  `gh`, `git`, `node`, `npm`, `python3`, `infisical`, `tailscale` and `gitleaks` are required on both hosts (`recall contribute` needs `gitleaks`;  `lane` needs `git` and `gh`), `jq` on the Mac and `docker` on the server.  `pnpm` and `uv` are optional on both, and `wrangler`, `cloudflared` and `xcodegen` on the Mac.

The version is the first version-looking token from `<tool> --version` (5 second cap;  an unrecognised answer is `unknown`, never a FAIL).  `muse` is read from its launcher's version file instead, because the launcher updates itself when it runs.  The latest version is read only with the network on:  `brew info --json=v2` when the command resolves into a Homebrew Cellar, else `npm view <package> version`, else the GitHub latest release, unauthenticated.  A tool with none of these is current on presence alone, unless the other host has a newer one:  in the combined report a tool older than the same tool on the other host is `stale` (`git` and `python3` follow their OS and are left out of that comparison).

What "current" means for a vendor CLI is one constant, `VENDOR_CURRENCY_POLICY`, pending the owner's decision.  The default, `latest`, holds a tool to its package manager's latest version and marks it `stale` with the gap.  `skew` would leave it current unless the hosts disagree, still showing the gap.

### The report Muster reads

`com.jay.fleet-tools-doctor` (template `scripts/launchd/com.jay.fleet-tools-doctor.plist`) runs every 900 seconds and at load:  `/usr/bin/python3 -I` on the managed runtime's copy of the doctor with `--remote fleet-tools-ro --write ~/.local/state/fleet-tools/report.json`.  The file is written atomically with mode 0600:

```json
{"schemaVersion": 1, "tool": "fleet-tools-doctor", "generatedAt": "<UTC>",
 "hosts": [{"host": "mac", "reachable": true, "...": "the --json document"},
           {"host": "server", "reachable": false, "error": "no read-only route to the server (...)", "checks": ["every server row, unknown"]}]}
```

Each host document carries `schemaVersion` 1, `host`, `generatedAt`, `doctorCommit`, `expected` (the tip and how it was read) and its rows.  A reader treats a report older than 45 minutes, a missing or malformed file, or any other `schemaVersion` as unknown, and leaves `harness` and `human-tree` rows out of its verdict.

### The server route (owner step)

The server half is read over a key that can do one thing:  run this doctor with `--json`.  Nothing is written on the server and Infisical is not touched.  The doctor treats a missing alias as "no read-only route to the server", so the Mac half works without it.  Installing the key needs the owner's OK, because it is a new way into root (limited by `restrict`, `from=` and a fixed command;  the hourly updater already runs main as root).

1. On the Mac, a dedicated key with no passphrase (launchd has no agent):
   ```bash
   ssh-keygen -t ed25519 -N "" -C "fleet-tools-ro (read-only doctor)" -f ~/.ssh/fleet-tools-ro
   ```
2. On the Mac, this block in `~/.ssh/config`, **above** the `Host *` block (ssh keeps the first value it finds).  `HostName` is the server's Tailscale name, the same one the existing `coolify` block uses, so its host key is already in `known_hosts`:
   ```
   Host fleet-tools-ro
     HostName <the server's Tailscale name>
     User root
     IdentityFile ~/.ssh/fleet-tools-ro
     IdentitiesOnly yes
     BatchMode yes
     StrictHostKeyChecking yes
     ForwardAgent no
     ControlMaster no
     ControlPath none
   ```
3. On the server, one line in `/root/.ssh/authorized_keys`, with the Mac's Tailscale IP and the contents of `~/.ssh/fleet-tools-ro.pub`:
   ```
   restrict,from="<Mac Tailscale IP>",command="/usr/bin/python3 -I /root/.local/share/fleet-cli/repo/scripts/fleet-tools-doctor.py --json" ssh-ed25519 AAAA... fleet-tools-ro (read-only doctor)
   ```
4. Check it:  `ssh fleet-tools-ro | head -c 200` and `ssh fleet-tools-ro whoami` both print the doctor's JSON (the forced command ignores what is asked), `ssh -N -L 9999:localhost:22 fleet-tools-ro` is refused, and `python3 scripts/fleet-tools-doctor.py --remote fleet-tools-ro` lists the server's rows.  Renaming the key turns them `unknown` again.

The doctor calls it with `-o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=yes` and no command.  An exit of 1 with a document is still an answer (the server has a FAIL row);  a timeout, any other exit, something that is not JSON, or a doctor too old to report `schemaVersion` 1 makes the server `reachable: false` and every server row `unknown`.  Removing the `authorized_keys` line revokes it.
