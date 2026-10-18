# CODEX fleet CLI sync on Coolify

The Mac's existing `com.jay.agent-sync-runtime-sync` job publishes a narrow set of fleet CLI code to the existing `root@coolify` SSH target.  Its existing two-minute schedule remains the only update trigger.  Coolify does not receive a new listener, timer, or service, and active Codex sessions are not restarted.

This is a code distribution path.  It does not provision authentication, create chats, route wakes, or change fleet seat ownership.  CODEX and JET remain separate app identities.

## Source and transport

The deployment target is Linux with `/usr/bin/python3` version 3.11 or newer.  The receiver and wrappers use isolated Python execution.

The source is the published revision selected by the Mac managed checkout at `~/apps/lanes/_managed/fleet/agent-sync-runtime`.  The pusher requires the expected AI-Fleet-Coordinator origin, a clean tracked checkout, and a commit reachable from `origin/main`.  It reads Git blobs by object ID rather than archiving a working directory.  Git attributes cannot substitute different release content.

The allowlist contains the board executable and its non-secret registry files, the Agent-Sync Python runtime and MCP schemas, the partition policy, the release receiver, and the sources of the other three fleet tools (`recall`, `lane` and `fleet-mode`, below).  Credentials, caches, user configuration, handbook mirrors, chat data, and untracked files are excluded.  Bundles have file-count and byte limits and a digest over every file's path, mode, and content hash.

The pusher uses the existing SSH authorization with batch mode and strict host-key checks.  It does not create keys, accept unknown host keys, or copy SSH configuration.  SSH failures are reported without relaying arbitrary remote output.  A failed server update does not block the Mac's existing runtime update.

## The five fleet tools (AFC-331)

The server carries the same five tools as the Mac, so a Claude or Codex thread there works the same way:  `agent-sync`, `board`, `recall`, `lane` and `fleet-mode`.  `apple-notes-coding.sh` is deliberately not mirrored;  a separate Notes relay handles the server.  What each shipped tool runs from, and what the server must provide:

- **`recall`.**  `scripts/recall` plus ten `scripts/fleet_rag/` modules (its static import closure:  `__init__`, `contribute_guard`, `core`, `doctor`, `health`, `public_fallback`, `recall_api`, `scrub`, `search_log`, `telemetry`).  `recall eval` and `recall ingest` import `eval` and `ingest` lazily and are Mac and box jobs, so they are not shipped and fail with an ImportError on the server.  No credentials ship.  The server provides them, either `QDRANT_URL`, `QDRANT_API_KEY`, `TEI_URL`, `TEI_API_KEY` and `QDRANT_FLEET_COLLECTION` (and optionally `TEI_RERANK_URL` and `TEI_RERANK_API_KEY`) in the environment, or a chmod-600 file named by `FLEET_RAG_HANDOFF_FILE` holding the Infisical machine identity (there is no default file any more:  `FLEET_RAG_HANDOFF_FILE` is explicit-only, because the flat `~/.secrets/global-api-keys` is retired under OPS-818;  supply the keys through the environment, or `fleet-secrets run` on the Mac).  `contribute` also wants `gitleaks` on PATH, or it marks the lesson `gitleaks-unavailable`.  Everything else is relative to `$HOME`.
- **`lane`.**  The checkout's own bash shim, `scripts/fleet_lanes/bin/lane`, which finds `scripts/` from its location, plus `fleet_lanes/{__init__,lane,doctor,guard,layout}.py` and the already-shipped `fleet-apps.json`.  The canonical source is the repo shim, not the Mac's `~/apps/lane`:  that file is a generated copy (`fleet_lanes.install_tools`) that points at the stable tree `~/apps/lane-tools` and is meant for the Mac only.  `lane` takes every path from `$HOME`, so repositories under `/root/Code/<Repo>` and lanes under `/root/apps/lanes/<Repo>` work.  It needs `git`, and `gh` for `--review --pr`.
- **`fleet-mode`.**  `scripts/fleet-mode` plus `docs/FLEET-MODE.md`, the canonical copy of the briefing.  The script reads the doc beside itself first and `~/apps/FLEET-MODE.md` second.  The Mac keeps a copy at `~/apps/FLEET-MODE.md`;  `fleet-tools-doctor.py` warns when it differs from the repo.

The wrappers for these three are written only by `--install`.  An ordinary update leaves wrappers alone, so a server that was installed before AFC-331 gets the new code on the next push but not the commands:  run `python3 scripts/fleet-cli-sync.py --install` once from the published managed runtime.  It is idempotent (it rewrites nothing that already matches) and still refuses an unmanaged file at any of the wrapper paths.  `fleet-tools-doctor.py` reports a missing command or missing shipped file until then.

## Server layout

| Path under `/root` | Purpose |
| --- | --- |
| `.local/share/fleet-cli/releases/<digest>` | Immutable validated code release |
| `.local/share/fleet-cli/current` | Atomic active release pointer |
| `.local/share/fleet-cli/previous` | Previous release available for rollback |
| `.local/share/fleet-cli/update.lock` | Prevent overlapping activation |
| `.local/share/fleet-cli/paused` | Operator pause marker |
| `.local/bin/board` | Board CLI wrapper |
| `.local/bin/agent-sync` | Agent-Sync CLI wrapper |
| `.local/bin/agent-sync-codex-mcp` | CODEX-scoped local MCP entry point |
| `.local/bin/fleet-cli-release` | Status, pause, resume, and rollback control |
| `.local/bin/recall` | Fleet-agents corpus search and contribute (`scripts/recall`) |
| `.local/bin/lane` | Lane and PR-review checkout creator (`scripts/fleet_lanes/bin/lane`) |
| `.local/bin/fleet-mode` | Prints the Fleet mode briefing (`scripts/fleet-mode`) |
| `.codex/config.toml` | Existing configuration with one Agent-Sync MCP entry |
| `.codex/backups/fleet-cli-*/config.toml` | Initial configuration backup |

Initial installation refuses conflicting unmanaged wrappers or a different existing Agent-Sync MCP entry.  Existing configuration settings and file modes are preserved.  Subsequent code updates leave configuration and wrappers unchanged.

Each release is checked for allowlisted paths, hashes, regular-file modes, and Python syntax, then exercised with an empty temporary home and no inherited credentials.  Board identity lookup must fail with the expected missing-credentials result, and `recall --help`, `lane --help` and the `fleet-mode` block must each succeed.  A release becomes current only after those checks pass.  Wrappers resolve the selected release once, so an already running process continues using its original code.

## Operation and rollback

From the published managed runtime, inspect the bounded bundle with `python3 scripts/fleet-cli-sync.py --dry-run`, then perform the initial wrapper and MCP registration with `python3 scripts/fleet-cli-sync.py --install`.  Ordinary scheduled runs omit `--install` and preserve configuration.  `--force` requests a fresh remote receipt when the bundle is already cached; it does not bypass source validation.

The private Mac state at `~/.agent-sync/coolify-cli-sync/state.json` records the last check time and outcome, including cached skips, separately from the last successful remote receipt.  This distinguishes an executing scheduled hook from an old installation record.

The Mac opt-in marker is `~/.agent-sync/coolify-cli-sync.enabled`.  Remove that marker to disable future pushes.  The existing Mac runtime pause mechanism also pauses this hook.  The hook runs before the Mac fetch, so a newly selected Mac revision normally reaches Coolify on the following scheduled run.

On Coolify, use:

```sh
/root/.local/bin/fleet-cli-release status
/root/.local/bin/fleet-cli-release pause
/root/.local/bin/fleet-cli-release resume
/root/.local/bin/fleet-cli-release rollback
```

Rollback validates and activates the previous release and pauses future activation.  It does not restart active sessions.  Resume only after the source revision is suitable; otherwise a later push can reselect the newer published code.  Releases are retained to protect running processes.  The receiver stops at its retention limit rather than deleting potentially active code.

For the first installation, no previous release exists.  Disable the Mac opt-in marker, restore the recorded initial configuration backup, and remove only the seven managed wrapper files to return to the prior integration state.  Retain release directories until no process is using them.  Do not remove unrelated configuration, credentials, or caches.

## Authentication and seat boundaries

On the assessed server, `/root/.secrets/Linear/CODEX.env` and `/root/.secrets/Zulip/Codex-zuliprc` were absent.  Installing code and registering the MCP server do not make board or Zulip operations authenticated.  No credentials are carried in a bundle or chat.

Use a separately approved secure provisioning route to populate the appropriate server-local files from the canonical secret manager.  CODEX's Linear file uses the names `LINEAR_CLIENT_ID` and `LINEAR_CLIENT_SECRET`.  JET requires its own app pair and seat-specific file.  Inspect variable presence without values before authentication: generic `LINEAR_CLIENT_ID` and `LINEAR_CLIENT_SECRET` process variables override seat files and can select the wrong app.

The MCP wrapper rejects a launcher without an explicit launch seat, rejects a non-CODEX launch seat, and rejects a non-CODEX `AGENT_SEAT`.  Its default seat is CODEX.  It does not assign CODEX to BotFleet Codex-engine tasks.  The server is not granted a listener partition, and the existing Mac CODEX wake owner remains unchanged.

## Router follow-up

The existing router selects seat leases; it does not yet select real Claude or Codex chat IDs across hosts.  A dedicated change should add a bounded chat inventory, deterministic ID matching, and a classifier that can choose only an enumerated candidate ID or abstain.  Side effects require independently verified owner transport and current inventory.  Chat creation requires a verified Jay request, an explicitly available model, and a definitive absence of a matching chat; its default host is Coolify.

Ambiguous or stale matches, incomplete inventory, unavailable models, malformed classifier output, and uncertain dispatch acknowledgements must hold rather than create another chat.  A durable request ledger should make dispatch and creation idempotent.  The existing restricted Sonnet classifier is the established baseline; subscription versus API billing and any Codex replacement's confinement remain unverified.  No cost saving or end-to-end wake behavior is established by this installer.

Offline router fixtures should cover duplicate names across hosts, stale and deleted IDs, malicious metadata, unknown candidate IDs, abstention, incomplete inventories, forged owner context, missing models, concurrent duplicate requests, and crashes around dispatch acknowledgements.  Routing and creation need separate live verification after the inventory and authority contracts are implemented.
