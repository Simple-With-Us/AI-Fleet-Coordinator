# Infisical Agent Proxy adoption and `~/.secrets` retirement

Linear:  OPS-818.  Seat:  CLAUDE.  Started Sun, Oct 18.
Builds on Antigravity's audit, "~/.secrets & global-api-keys Deprecation & Migration Catalog" (Sun, Oct 18), and the Oct 8 research comment on OPS-818.

## Goal

Agents on this Mac use API keys without ever holding them.  Today every agent process can read `~/.secrets/global-api-keys` (about 230 keys) and `~/.secrets/Zulip/*-zuliprc` in plain text, so one prompt injection can leak any of them.  The end state:

- **LLM agent processes** get placeholders and an `HTTPS_PROXY`;  Infisical Agent Proxy swaps in the real credential only on requests to that credential's own host.
- **Daemons** (LaunchAgents, pm2 jobs) read what they need from Infisical at start through a machine identity (`infisical run`, or `scripts/fleet_knobs` for knobs), never from a flat file.
- **Human CLIs** (`secret-add`, `seat-app-creds`) write straight to Infisical.
- `~/.secrets/` is gone, and every key an agent could read before the cutover has been rotated.

## Corrections to the Antigravity catalog

1. **Agent Proxy does not need root references.**  The catalog proposes rewriting Fleet-OPS root `/` (693 raw values) to `${prod.<folder>.<KEY>}` so root becomes a "gateway" for the proxy.  The proxy does not work that way:  injection rules are "proxied services" defined on a project folder (dashboard or `POST /api/v1/proxied-services`), each with a host pattern and either a header rewrite (Bearer, API key, custom, Basic) or placeholder substitution.  One host can be mapped once per folder, so per-seat credentials for the same host need a folder per seat (`/zulip/claude`, `/zulip/codex`, …).  Rewriting root buys the proxy nothing.  **Status, measured Oct 18:  the rewrite has already happened.**  All 695 root values are bare references (checked with `expandSecretReferences=false`, names only).  687 resolve to the folder value.  Six point at keys that exist in no folder, so their root values are gone:  `ECHO_BOT_EMAIL`, `ECHO_RELAY_LISTENER_HMAC_KEY`, `LINEAR_APP_TOKEN_ECHO`, `LINEAR_APP_TOKEN_WISE`, `MAC_COLLAB_TOKEN_GROK-BUILD`, `MAC_COLLAB_TOKEN_WISE`.  Root readers that do not expand references now get the literal `${…}` string:  `scripts/infisical-fetch.mjs` (`expandSecretReferences: "false"`, used by the AFC, FleetLink and Usage-Monitor `worker-deploy.yml` for `CLOUDFLARE_FLEET_API_TOKEN`), and possibly `vercel-catch-up.yml` (`VERCEL_TOKEN`), Congress.Trade `ios-ship.yml` (`--expand=false`) and Fleet-OPS `scripts/cursor-cloud-start.sh` (exports all of root).
2. **Two kinds of consumer, two mechanisms.**  The proxy protects processes that can be prompt-injected.  Litestream, `grok-acp`, `seat-mcp` and the other daemons are not agents;  they move to `infisical run` with a machine identity, not to the proxy.
3. **Hiding a key does not limit what it can do.**  An agent behind the proxy cannot read the GitHub token but can still make any call that token allows.  Each wrapped agent needs its own machine identity, an allowlist of hosts, and `--unmatched-host=block`.
4. **The sandbox does not deny `~/.secrets`.**  Measured Oct 18:  `run` denies `~/.infisical`, `~/.ssh`, `~/.aws` and `~/.config/gh`, but `~/.secrets` (all 139 entries), `~/Library/Keychains`, `~/.codex/config.toml` and `~/.claude.json` stay readable, and there is no deny flag.  The wrapped process also inherits the parent environment.  The proxy delivers nothing until that directory is gone, which is why retirement and rotation are part of this plan rather than cleanup after it.
5. **Desktop apps cannot be wrapped.**  `run` wraps a process you launch.  The Claude desktop Code tab, Cursor, Antigravity and Codex desktop need a different answer (the proxy as a standalone server plus `HTTPS_PROXY` in the app's environment, if the app honours it).
6. **Plaintext cache in the audit folder.**  Antigravity left `infisical_secrets_cache.json`, `root_to_folder_mapping.json` and `root_references.env` world-readable in its brain folder.  CLAUDE set them to `0600` on Oct 18;  they should be deleted once this plan no longer needs them.

7. **`run` acts as whoever is logged in to the CLI, which is Jay.**  The CLI session on this Mac is Jay's own login, so `run` brokers as Jay, against the fleet rule.  Agents use `start` (a standalone proxy holding the proxy identity) plus `connect` (the agent identity, which can route but cannot read secrets).  Both identities already exist in Fleet-OPS `/agent-proxy`;  their scopes are not yet checked.
8. **Loopback is unreachable inside `run`.**  `127.0.0.1` failed even with `--allow-host`, so local MCP servers, hooks and the listener break under the sandbox.  `connect` does not sandbox and is the likelier fit.
9. **Infisical's own docs now point new setups at Agent Vault** and keep Agent Proxy for sandboxed local proxies and dynamic secrets.  Worth one look before Phase 2 commits to either.

## Phases

Each phase is additive until Phase 5.  Nothing is deleted, booted out or rotated without Jay.

### Phase 0:  Inventory (in progress, read-only)

- Names-only drift report:  root `/` against each mapped folder (equal, differ, unmapped, ambiguous).
- Who reads Fleet-OPS root today, and what the catalog missed (repos on `origin/main`, hooks, `~/.codex`, MCP configs, LaunchAgents).
- Agent Proxy smoke test, Oct 18:  `--unmatched-host block` works (403, logged), sandbox readability measured (above).  Injection not yet proven:  no proxied service exists in any Fleet-OPS folder.  The first run added a local CA to the login keychain and wrote `~/.infisical/agent-proxy/local-ca.{crt,key}`.
- Next:  create proxied service `zulip-claude` in a new folder `/zulip/claude` (host `simplewithus.zulipchat.com`, Basic auth from `CLAUDE_ZULIP_EMAIL` and `CLAUDE_ZULIP_KEY`), then `GET /api/v1/users/me` through `start` + `connect` should return 200 while the agent sees only placeholders.

### Phase 1:  Read from Infisical first (additive)

- Done in #634 (Oct 18):  `agent-sync` falls back to Fleet-OPS `/zulip` (`<PREFIX>_ZULIP_EMAIL`, `_KEY`, `_SITE`, prefix = zuliprc stem upper-cased, hyphens to underscores) when no zuliprc, `ZULIP_RC` or env triple is set.  It asks for the calling seat's three names only, and `AGENT_SYNC_INFISICAL=0` turns it off.  The project id is never committed:  each machine needs `INFISICAL_FLEET_OPS_PROJECT_ID` or `~/.agent-sync/fleet-ops-project-id` (set on the Mac).  Any Fleet-OPS login can still read every seat's keys, so per-seat identities remain the real fix.  `subscribe_all.py`, `heartbeat.py`, `daemon.py` and `launchd.py` follow the same resolver.
- Done Oct 18:  Jay chose to keep root as references.  The six dangling targets were recovered from each root key's version history (the version before the rewrite) into the folder its reference names, eleven keys that were in `/global-api-keys` only got root references, and all 705 root keys resolve.  Root readers that passed `expandSecretReferences=false` are being switched to expansion, with a guard that fails on an unresolved `${…}` (PRs titled "Expand Infisical secret references when reading Fleet-OPS root").
- Done in #635:  `fleet-secrets` (`get`, `run`, `names`, `doctor`), the one replacement for reading `~/.secrets/global-api-keys`.  See `INFISICAL.md` § fleet-secrets.  Auth is Jay's CLI session until a machine identity is in Keychain.
- In progress:  every reader of `~/.secrets/global-api-keys` moves to env first, then `fleet-secrets` (PRs titled "Stop reading ~/.secrets/global-api-keys").  Autorotate is archived on GitHub, so its commit waits in a local lane.
- `secret-add` and `seat-app-creds` write to Infisical (Fleet-OPS `/global-api-keys`, `/linear`) and, during the transition, still append to the local file.
- `recall`, `fleet-share`, `fleet-shares` resolve from the environment or Infisical before falling back to `~/.secrets`.

### Phase 2:  Agent Proxy pilot

- Run one standalone proxy (`infisical secrets agent-proxy start`, proxy identity from `/agent-proxy`, `--unmatched-host block`) as a LaunchAgent, and launch one terminal agent (the `claude` CLI or `opencode`) through `connect` with the agent identity.  Services for Zulip, Linear, GitHub and recall.
- Unknowns to settle:  Claude Code subscription login behind the proxy, the `board` CLI's token, whether desktop apps honour `HTTPS_PROXY` and trust the local CA, and the CA's one-week expiry.
- A wrapper per CLI seat (`~/apps/agent-proxy/<seat>`), registered in `MAC-LOCAL-PROCESSES.md`.

### Phase 3:  Daemons onto machine identities

`start_litestream.sh` (B2), `grok-acp-runtime/start.sh`, `seat-mcp/start.sh`, `provider-knob-sync-start.sh`, `fleet-apps-sheet-sync.mjs`, `rotate.py`, `gb_monitor_pull.py`, and anything Phase 0 adds.  `slack-sync.sh` is retired with Slack (Oct 7) and goes rather than migrates.  Each one switches to `infisical run --projectId … --env prod --path <folder> -- <cmd>` and gets its row updated in `MAC-LOCAL-PROCESSES.md`.

### Phase 4:  Instructions

`AGENT-SYNC.md` (Secret handoff, grep trap), the global agent files (`~/.claude/CLAUDE.md`, `~/AGENTS.md`, `~/.codex/AGENTS.md` and the other platforms), the `secret-handoff` and `secret-safety` skills, and `INFISICAL.md` § Mac `global-api-keys` sync:  replace "the handoff file" with "Infisical, through the proxy or `infisical run`".

### Phase 5:  Cutover (owner gate)

1. Boot out `com.jay.infisical-secrets-sync` and retire `scripts/infisical-secrets-sync.sh`.
2. Move `~/.secrets/` into an encrypted archive (not delete) and confirm every consumer still works for a day.
3. Rotate every key that sat in `~/.secrets` while agents could read it, most sensitive first.
4. Delete the archive after the rotation is confirmed.

## Decisions for Jay

- **Identities and plan.**  Infisical's Free plan covers five identities.  One machine identity per wrapped agent plus one per daemon group needs more than that.  Upgrade, or share identities by trust tier?
- **Fleet-OPS root.**  Decided Oct 18:  keep the references.
- **`fleet-secrets` identity.**  A universal-auth machine identity with read access to Fleet-OPS prod, its client id and secret in Keychain (service `fleet-secrets`).  Until then every migrated job depends on Jay's CLI login, which expires.
- **Autorotate.**  Unarchive it so the migration lands, or confirm it is dead.
- **Rotation scope at cutover.**  All keys, or only the keys with spend, write or admin power?
- **Desktop apps.**  Accept that the desktop Code tab, Cursor, Antigravity and Codex desktop keep reading keys until a standalone-proxy setup is proven, or restrict them to MCP connectors that hold their own credentials?
