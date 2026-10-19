# INFISICAL.md — Sole Source of Truth for AI-Fleet-Coordinator

Owner directive (2026-10-03): **Infisical is the sole source of truth** for this app's
secrets, env variables, and tunable settings knobs.  Per-user settings stay in the app's
own store and never go in Infisical.

- Infisical project: **AI Fleet Coordinator** (`9bf7417a-fbbb-42ca-870c-2b45207233f5`), jays-services org.  **`prod` is the only environment** (owner decision 2026-10-10, new projects delete the default dev and staging at creation):  the service reads and writes prod, `INFISICAL_ENVIRONMENT` defaults to `prod`, and any other slug is refused.
- Fleet knobs for the short-lived open-loops jobs (LaunchAgents, hooks, CLIs):  `scripts/fleet_knobs/`, documented in "Fleet knobs" below.
- Migrated surface (this PR): `scripts/fleet-recall-service/server.py` — the fleet's shared vector memory HTTP front (the deployable Python service with the admin surface).  Settings module: `scripts/fleet_rag/infisical_settings.py` (stdlib only).
- Machine identity: the shared automation identity (Admin on this project).  The service reads `INFISICAL_AUTOMATION_CLIENT_ID` / `INFISICAL_AUTOMATION_CLIENT_SECRET` (fallback `INFISICAL_SHARED_*`) from the environment, or the chmod-600 JSON handoff file (`INFISICAL_MACHINE_IDENTITY`, default `~/.secrets/infisical-machine-identity.json`).  With no identity configured, managed keys fall back to the process environment and the service keeps its old env-only behavior.

## Key inventory

| Key | Kind | Status |
|---|---|---|
| `QDRANT_URL` | env config | set in prod |
| `QDRANT_API_KEY` | secret | **to be filled by admin** |
| `QDRANT_FLEET_COLLECTION` | env config | set in prod |
| `TEI_URL` | env config | set in prod |
| `TEI_API_KEY` | secret | **to be filled by admin** |
| `QDRANT_READONLY_API_KEY` | secret (optional) | **to be filled by admin** |
| `TEI_EMBED_MODEL` | env config | set in prod |
| `RECALL_API_TOKEN` | secret | **to be filled by admin** (required; the service refuses to start without it) |
| `RECALL_ADMIN_TOKEN` | secret | **to be filled by admin** (the admin settings surface answers 403 until it is set) |
| `RECALL_SOCKET_TIMEOUT` | tunable knob | set in prod |
| `SETTINGS_REFRESH_SECONDS` | tunable knob | set in prod (controls the background refresh cadence) |
| `HOST`, `PORT` | env config | managed keys; currently supplied by the container env |

Secret values are NEVER invented, guessed, or copied from anywhere: the five secret keys above are not yet created in the project and are documented here as "to be filled by admin".  (The Infisical CLI rejects empty values, so they cannot exist as empty placeholders.)

## Hosted Agent-Sync: Cursor Cloud

The CURSOR seat uses the existing `cursor-bot@simplewithus.zulipchat.com` member
bot.  Its credential pair belongs in this project, `prod`, folder `/zulip`,
separately from the recall-service settings cache and the generated fleet-knobs
inventory below.  Store or update the source of truth in Infisical first, then
copy the credential to the Worker through the secure owner handoff in
[scripts/agent-sync-mcp/DEPLOY.md](scripts/agent-sync-mcp/DEPLOY.md#add-cursor-cloud-afc-294).

| Key | Kind | Worker binding / provisioning |
|---|---|---|
| `ZULIP_CURSOR_API_KEY` | secret | Owner-managed existing CURSOR bot key; encrypted Worker secret `ZULIP_KEY_CURSOR`.  Secure provisioning is required before live Cursor acceptance. |
| `ZULIP_CURSOR_EMAIL` | env config | `cursor-bot@simplewithus.zulipchat.com`; Worker var `ZULIP_EMAIL_CURSOR`. |

Provisioning and rotation use these same key names; no secret value belongs in
this inventory.  The installer verifies the email, bot identity and member role
before copying the key, and the hosted Worker repeats those checks at runtime.

### Cursor Shared-Agent Tool Budgets

`AGENT_SYNC_CURSOR_TOOL_BUDGETS` is a JSON policy in AFC `prod` `/zulip`,
copied to the same encrypted Worker binding.  Config validation rejects unknown
kinds, invalid limits and pacing changes outside Linear.  Missing policy retains
the existing limits.  The policy applies only to CURSOR, across all its grants.

Owner requested capacity for approximately 40 Cursor agents on 2026-10-15:
Linear writes 2,000/hour and 20,000/day, Linear reads 12,000/hour with separate
counters and pacing (100ms writes, 50ms reads).  Zulip posts/replies are
100/hour and 600/day; reads 1,500/hour; reactions 300/hour; recall contributions
50/hour and 200/day.  Zulip spacing stays 3 seconds for writes and 500ms for reads.
Other seats retain their existing limits.  Upstream rate limits still apply.

## Notes relay consumer (AFC-331)

`NOTES_RELAY_CONSUMER` is one fleet knob (registered in `scripts/fleet_knobs/registry.py`, listed under "Fleet knobs" below):  the seat whose bot is the Apple Notes relay consumer.  **Owner action:  create the key `NOTES_RELAY_CONSUMER` with the value `CLAUDE` in this project, `prod`, root path `/`.**  Nothing in the repo writes it.

| Reader | How it gets the value |
|---|---|
| Mac listener, stdio MCP, CLI | `fleet_knobs` (one fetch per process, last-known-good copy).  A `[notes_relay] seat` in `listener.toml` that differs is ignored and the listener then does not consume.  With Infisical unreachable and no usable copy the answer is the default `CLAUDE`, labelled `default CLAUDE (unreachable:...)` in the log line `notes relay consumer ...`. |
| Hosted Worker | An encrypted Worker binding of the same name, copied at deploy by `scripts/agent-sync-mcp/sync_notes_consumer.py` (DEPLOY.md, "Notes, recall and the guide").  Never in `wrangler.jsonc`.  Unset means `notes_status` answers `not_configured`. |

## Zulip Outgoing Wake Tokens

The existing server listener loads `ZULIP_OUTGOING_<CODE>_TOKEN` from prod `/zulip` at startup for each server seat.  `<CODE>` replaces hyphens in the seat tag with underscores.  These separate Zulip service tokens authenticate outgoing webhook requests; they are neither `ZULIP_<CODE>_API_KEY` nor the routine's bearer/HMAC key.  Missing tokens deny requests for that seat.

The on-demand `scripts/zulip_migrate_outgoing.py publish` helper readback-verifies the replacement email, API key, and service token in Infisical before atomically installing the local zuliprc.  Refresh every additional credential consumer before retiring the old identity.  See [the migration runbook](docs/protocols/zulip-outgoing-migration.md).

## Hosted Echo and Wise Zulip identities

The hosted bridge and server listener use the canonical seat names ECHO and WISE.  Their Zulip RC files are `Echo-zuliprc` and `Wise-zuliprc`; the latter replaces the former `Instinct-zuliprc` filename.  The bot email may retain its existing Zulip local part while the seat key is WISE.

| Key | Kind | Worker binding / provisioning |
|---|---|---|
| `ZULIP_ECHO_EMAIL` | env config | Echo bot email; Worker var `ZULIP_EMAIL_ECHO` |
| `ZULIP_ECHO_API_KEY` | secret | Echo bot key; Worker secret `ZULIP_KEY_ECHO` |
| `ZULIP_WISE_EMAIL` | env config | Wise bot email; Worker var `ZULIP_EMAIL_WISE` |
| `ZULIP_WISE_API_KEY` | secret | Wise bot key; Worker secret `ZULIP_KEY_WISE` |

Store current values in prod `/zulip`, and verify each bot identity before provisioning.  Never copy a key into source or logs.

## Zulip incoming-webhook bots

Jay owns these incoming-webhook bots.  Each key builds a vendor's webhook URL (`/api/v1/external/<integration>?api_key=…&stream=…`), so a leaked URL is a leaked key.  The local copies are `~/.secrets/Zulip/<Name>-zuliprc` (0600).  Routing is in [the signal routing plan](docs/plans/2026-10-15-fleet-signal-routing.md).

| Key | Kind | Used by |
|---|---|---|
| `ZULIP_SENTRY_EMAIL` / `ZULIP_SENTRY_API_KEY` | env config / secret | Sentry internal integrations for #alerts, #trading, #agent-sync |
| `ZULIP_PAGERDUTY_EMAIL` / `ZULIP_PAGERDUTY_API_KEY` | env config / secret | PagerDuty v3 webhook to #incident-response |
| `ZULIP_LINEAR_EMAIL` / `ZULIP_LINEAR_API_KEY` | env config / secret | Linear webhook to #issues |
| `ZULIP_BETTERSTACK_EMAIL` / `ZULIP_BETTERSTACK_API_KEY` | env config / secret | BetterStack outgoing webhook to #alerts (`slack_incoming`) |
| `ZULIP_DATADOG_EMAIL` / `ZULIP_DATADOG_API_KEY` | env config / secret | Datadog `zulip-alerts` webhook to #alerts (`slack_incoming`) |

## Runtime contract

1. **Load at startup.**  `main()` calls `infisical_settings.configure()` + `init_settings()` before binding: universal-auth login → access token → `GET /api/v3/secrets/raw`.  A missing required key fails fast with a `SettingsError` naming the key and pointing here.  Backend keys (`QDRANT_*`, `TEI_*`) are then mirrored into `os.environ` so the `fleet_rag` library (which reads env) keeps working unchanged.  Migration bridge: any managed key missing from Infisical is backfilled from the process environment, and the backfilled key names are logged, so the service keeps its old behavior while the admin fills Infisical in.
2. **Never fetch per-request.**  All runtime reads come from the in-memory cache (`infisical_settings.get()`).  There is no per-request, per-tick, or per-event Infisical call anywhere in the request path — verified by test (`test_runtime_reads_make_zero_network_calls_after_init`).
3. **Background refresh.**  A daemon thread re-fetches every `SETTINGS_REFRESH_SECONDS` (default 300), re-exports the backend keys, and on failure logs loudly while keeping the last-known-good cache.  On-demand refresh: `SIGHUP` to the process, `POST /admin/reload-settings`, or the admin settings save.
4. **Write-through on admin save.**  `POST /admin/settings` calls `infisical_settings.set()`, which writes to Infisical FIRST (`POST`/`PATCH /api/v3/secrets/raw`) and only then updates the local cache.  A failed write fails the save (502) and the cache is left untouched.

## Admin gating

`GET /admin/settings` (key inventory; secret values masked as `set`/`empty`, never returned), `POST /admin/settings` (write-through save), and `POST /admin/reload-settings` require `Authorization: Bearer <RECALL_ADMIN_TOKEN>`.  The seat bearer (`RECALL_API_TOKEN`) gets 401 on these routes; with `RECALL_ADMIN_TOKEN` unset the routes answer 403.  "Admin" is whoever holds the admin token — the fleet operator, not the seats.

## Per-user boundary (out of scope by design)

This service has no per-user settings: seat identity is passed per-request by the caller (`recall_contribute` requires a `seat` argument) and is never stored.  Nothing per-user belongs in Infisical, and nothing here was migrated into it.

## Deliberately not migrated (this PR)

- `scripts/mac-collab/mac-collab-server.py` (THE BOARD, a read-only archive after the Linear cutover and retired at about day 30): Mac-local; reads `~/.secrets/mac-collab.env` (canonical) or the environment.  Operator-managed on the Mac, not a hosted service surface.
- Every Linear app's client credentials live in this project under `/linear`:  `LINEAR_<SEAT>_CLIENT_ID` / `_SECRET` (hyphens as underscores) and the shared app's `LINEAR_FLEET_CLIENT_ID` / `_SECRET`.  The `board` CLI reads them from there first, through the `INFISICAL_AUTOMATION` identity, falling back to `~/.secrets/Linear/<SEAT>.env` only on a host without it (AFC-348).  The same folder holds one shared minted token per app, `LINEAR_<SEAT>_CACHE_TOKEN` and `LINEAR_FLEET_CACHE_TOKEN`:  a JSON row (token, mint and expiry times, credential fingerprint, minting host) that `board` writes after every mint and reads instead of minting again.  Never print one.
- `fleet-sentry-monitor/monitor.py`: operator cron script; env-configured on the box that runs it.
- `fleet_rag` CLI tools and MCP scripts: per-invocation env; no long-lived process to host a cache.
- `scripts/admin-panel` (Cloudflare Worker): serverless, no Infisical-reading backend in this repo.
- `workers/pr-conflict-watch`: Cloudflare Worker.
- `RECALL_FAKE=1`: local test/smoke switch only; stays an env var.  Bootstrap plumbing (`RECALL_REF`, `GITLEAKS_*`) likewise stays env-only.

## Fleet knobs

`scripts/fleet_knobs/` is how the open-loops jobs (LaunchAgents, hooks and CLIs that run for seconds) read their modes, caps and flags.  The values live in this project, `prod`, at the root path, and they are the only place a mode or a cap is set.  A refresh thread does not fit a job that lives a few seconds, so the contract differs from the service above:

1. **Fetch once per process.**  The first `knobs.get()` runs one `infisical export --env=prod --projectId=... --format=json` through the installed CLI, keeps only the names in the registry and drops the rest at once.  A failed fetch is not retried in the same process.  A non-prod `INFISICAL_ENVIRONMENT` or `INFISICAL_ENV` is refused before anything runs.
2. **Last-known-good.**  After a good fetch the non-secret knobs are copied to `~/.local/state/fleet-knobs/lkg.json` (directory 0700, file 0600, atomic rename).  When Infisical cannot be reached the job reads that copy and its report says `stale`.  A copy older than 48 hours, or one that is a symlink, belongs to someone else or is readable by group or other, is ignored, and the job runs on the defaults below.  A knob unset in Infisical is dropped from the copy, and so is a value that fails validation (a typo), so an outage right after a typo reads the default, the same answer a live read gives, and never an older and possibly wider value.  An export that holds none of the registered names (empty output, or only other keys) is treated as a failed fetch, reason `empty`, while the copy still has knobs:  the copy is kept and every answer is stale, because an empty answer is also what a wrong path or a broken login looks like.  The tradeoff:  deleting every knob on purpose reads as an outage until the copy ages out at 48 hours;  a project that has never had a knob has no such copy and is unaffected.  The 48 hour limit also applies to a pause:  `NEEDS_OWNER_NOTIFY_PAUSED=true` stops holding after 48 hours without a good fetch, and the fallback notifier runs again (owner decision open:  keep the limit, or let the last pause value hold).
3. **Secrets fail closed.**  `FLEET_FACTS_SIGNING_KEY` is never written to the copy, never defaulted and never exported.  A job that cannot fetch it stops (`SecretUnavailable`).  The value stays in that process's memory for the one fetch, as the service cache does.
4. **Local files only narrow.**  A job's own config file or a STOP file may lower a cap, remove a list entry, force a mode toward its first choice or set a flag to its safe value.  Anything that would widen the answer is ignored and the report says `local_ignored`.  The owner id and the launch time take no local override.
5. **Registered names only.**  A name that is not in `scripts/fleet_knobs/registry.py` cannot be read, so a job cannot ask Infisical for an arbitrary secret.
6. **Names only in logs.**  The CLI's output and stderr are never printed or logged;  a failure is a short reason (`cli-missing`, `timeout`, `exit-1`, `bad-output`).

Until a key is set in Infisical, a job runs on the Default column, which is the value for the component's first live run (modes off, dry-run or report, flags off, shadow on).  The first values are set at live step L5 (and L13, L20, L22, L25, L26, L29 for their own knobs).  To add a knob, add an entry to the registry, run `python3 -m fleet_knobs inventory --write ../INFISICAL.md` from `scripts/` and commit both;  CI fails when the block below and the registry differ.

<!-- fleet-knobs:begin  generated by `python3 -m fleet_knobs inventory --write ../INFISICAL.md`;  edit scripts/fleet_knobs/registry.py, not this block -->

| Key | Owner | Kind | Default | Secret | Local files may | What it does |
|---|---|---|---|---|---|---|
| `FLEET_AUTOPUSH_MODE` | C1 | enum: off < report < snapshot < full | `off` | no | only move toward `off` | what the autopush hooks may do;  full also needs FLEET_AUTOPUSH_IDLE_COMMIT |
| `FLEET_AUTOPUSH_REPOS` | C1 | list | empty | no | only remove entries | allowlist of owner/repo names the hooks may touch |
| `FLEET_AUTOPUSH_IDLE_COMMIT` | C1 | bool | `false` | no | only set it to `false` | true lets the idle backstop commit on its own (mode full only) |
| `FLEET_AUTOPUSH_IDLE_HOURS` | C1 | int 1 to 168 | `2` | no | only raise it | hours a lane sits unchanged before the backstop sweep acts |
| `FLEET_AUTOPUSH_PUBLIC_DRAFTS` | C1 | list | empty | no | only remove entries | public repos where a draft PR may open;  empty means none |
| `FLEET_FACTS_SIGNING_KEY` | F7 | str | none, fails closed | secret | nothing | HMAC key for the signed facts files;  never cached, a job without it publishes nothing |
| `FLEET_FACTS_PUBLISH_FULL_COPY` | F7 | bool | `false` | no | only set it to `false` | true also publishes the full mac-facts.json, which keeps lane paths and is readable by everything that can read fleet-loops-data;  false (the default) publishes only the path-free gb/ copy and the full list |
| `FLEET_FACTS_PUBLISH_CAP_MINUTES` | F7 | int 10 to 60 | `20` | no | only lower it | minutes a publish run may take before it ends itself and its child processes;  10 is the least that leaves room for the gatherers (5 minutes are reserved) and the writes |
| `FLEET_FACTS_PUBLISH_MAX_LOAD1` | F7 | int 1 to 1000 | `250` | no | only lower it | a publish run is skipped when the one-minute load average is over this |
| `FLEET_LOOPS_DATA_ALLOWED_APPS` | F7 | list | empty | no | nothing | app slugs the owner accepts as able to read the private fleet-loops-data repository;  empty means the publisher refuses while any installed app can read it |
| `NEEDS_OWNER_NOTIFY_PAUSED` | C6 | bool | `false` | no | only set it to `true` | true pauses the fallback notifier;  an Infisical outage past the 48 hour copy limit reads false and unpauses it |
| `NEEDS_OWNER_CAPS` | C6 | caps | `per_day=30,per_hour=6,urgent_per_hour=3` | no | only lower a number | notifier post caps |
| `OWNER_LINEAR_USER_ID` | C6 | str | unset | no | nothing | the owner's Linear user id;  empty means no Linear reply is accepted as the owner's |
| `CLAIM_EXPIRY_MODE` | C4 | enum: dry-run < ping-only < act | `dry-run` | no | only move toward `dry-run` | act also needs CLAIM_EXPIRY_LAUNCH_AT |
| `CLAIM_EXPIRY_CAPS` | C4 | caps | `pings_per_run=40,releases_per_day=40,releases_per_run=20` | no | only lower a number | code ceilings are the same numbers;  Infisical can lower them |
| `CLAIM_EXPIRY_LAUNCH_AT` | C4 | str | unset | no | nothing | ISO 8601 time the claim clock starts, set once;  empty means the clock has not started |
| `ASK_CAPTURE_ENABLED` | C3 | bool | `false` | no | only set it to `false` | false turns the capture hooks and the drain off |
| `ASK_CAPTURE_SHADOW` | C3 | bool | `true` | no | only set it to `true` | true appends would-file rows for review and never calls board |
| `ASK_CAPTURE_CAPS` | C3 | caps | `model_calls_day=80,per_seat_day=30,per_session=5` | no | only lower a number | extractor and filing caps |
| `LANE_SWEEP_MODE` | C5 | enum: report < act < retire | `report` | no | only move toward `report` | act arms merges and retires merged-PR lanes;  retire adds the Phase 3 deletions |
| `LANE_SWEEP_CAPS` | C5 | caps | `lane_retires_per_run=10,report_posts_per_run=12` | no | only lower a number | sweep action caps |
| `LANE_SWEEP_OPTIN_REPOS` | C5 | list | empty | no | only remove entries | money-path repos the sweep may act on;  empty means none |
| `NOTES_RELAY_CONSUMER` | N1 | str | `CLAUDE` | no | nothing | seat whose bot is the Apple Notes relay consumer (the Mac listener's [notes_relay] seat);  notes_status trusts a mark or a refusal reply only from it;  a local [notes_relay] seat that differs is ignored and the listener does not consume;  the Worker gets the same value as a binding at deploy |
| `CONSULT_ENABLED` | C7 | bool | `false` | no | only set it to `false` | false refuses every consult verb |

<!-- fleet-knobs:end -->

## Telemetry relay (Coolify)

`telemetry-relay/` is a Coolify-hosted otelcol-contrib.  Its secrets live in this
project, `prod`, and are copied into the Coolify service env by name (see
`telemetry-relay/COOLIFY.md`).  They are not fleet knobs and are not in the
generated block above.  Names only:

| Key | Kind | Who sets it |
|---|---|---|
| `SENTRY_FLEET_OTLP_ENDPOINT` | config (URL) | Jay, from Sentry `fleet-infra` Client Keys > OTLP |
| `SENTRY_FLEET_OTLP_KEY` | secret | Jay, same page (OTLP `sentry_key`) |
| `USAGE_MONITOR_OTLP_METRICS_ENDPOINT` | config (URL) | Jay, Usage-Monitor `/api/otlp/v1/metrics` |
| `USAGE_MONITOR_OTLP_TRACES_ENDPOINT` | config (URL) | Jay, Usage-Monitor `/api/otlp/v1/traces` (parallel task) |
| `USAGE_MONITOR_INGEST_TOKEN` | secret | Jay, Usage-Monitor ingest bearer |
| `RELAY_BEARER_CLAUDE_CODE` | secret | Jay, generated (`openssl rand -hex 32`) |
| `RELAY_BEARER_CODEX` | secret | Jay, generated |
| `RELAY_BEARER_GROK` | secret | Jay, generated |
| `RELAY_BEARER_SHIM` | secret | Jay, generated |
| `RELAY_BASE_URL` | config (URL) | Jay, the relay's public or tailnet base (no port) |

`scripts/telemetry/configure-agents.sh` pulls the bearer names and
`RELAY_BASE_URL` from Infisical (`--env=prod`) and writes 0600 snippets only; it
is shipped, never run against Jay's Mac, and prints no secret values.

## Rotation

Set a new value in the Infisical UI/API, or `POST /admin/settings` (write-through, immediate).  Background refresh picks UI/API changes up within `SETTINGS_REFRESH_SECONDS`; `POST /admin/reload-settings` or `SIGHUP` forces it now.  Rotating `RECALL_API_TOKEN` requires distributing the new bearer to the seats (coordinate via the board) — the old token stops working as soon as the refresh lands.  Never print, echo, or log a value; verify with key names and the `set`/`empty` flags from `GET /admin/settings` only.

## Prod-only lint

`python3 scripts/check-infisical-env.py` (CI step "Infisical environment lint") fails on anything that would select a non-prod environment:  `INFISICAL_ENV` / `INFISICAL_ENVIRONMENT` set to `dev` or `staging` in an env file (`.cursor/infisical.env`, `.env.example`), a `:-dev` / `:=dev` fallback or `--env=dev` in a `cursor-cloud-start.sh`, and a `.infisical.json` whose `defaultEnvironment` is not `prod`.  `--fleet` lints every repo under `~/Code` as it is on `origin/main` (add `--fetch` to refresh first).  A line that truly needs the word carries `infisical-env: allow` with a reason.  This repo's CLI callers (`scripts/fleet_knobs`, `scripts/infisical-secrets-sync.sh`) always pass `--env=prod` and `--projectId` themselves, so it has no `.infisical.json`;  a repo that relies on one gets it with `"defaultEnvironment": "prod"`, because the CLI's own default is `dev`.

## Local dev

Copy `.env.example` (non-sensitive keys) and set the secrets in your shell, or point `INFISICAL_MACHINE_IDENTITY` at your handoff file and leave `INFISICAL_ENVIRONMENT` unset (it is `prod`).  Local runs read prod, so treat the secrets there as live.  Any other slug is refused:  `init()`, `set()` and every fetch raise `SettingsError` before the network, `refresh()` logs the refusal and keeps the last-known-good cache, and `configure()` leaves the previous settings in place.  Never commit real values.

## fleet-secrets

`fleet-secrets` (`scripts/fleet_secrets/`, Linear OPS-818) is the one way a Mac script, LaunchAgent or pm2 start script reads Fleet-OPS secrets now that nothing may read the flat file `~/.secrets/global-api-keys`.  Install it with `bash scripts/install-fleet-secrets.sh`;  that copies the package to `~/.local/share/fleet-secrets/` and links `~/.local/bin/fleet-secrets` to the copy (a symlink into a lane would break when the lane is retired), so re-run the installer after the package changes.  Python stdlib only.  Run `fleet-secrets doctor` to check it.

| Command | What it does |
|---|---|
| `fleet-secrets run [--path /] [--only A,B] -- CMD [ARG...]` | Fetches once, then `exec`s `CMD` with the secrets as environment variables (a variable of the same name is replaced).  `--only` injects just those keys and exits 3 if one is absent.  PATH, HOME, USER, SHELL, TMPDIR, `LD_*`, `DYLD_*` and a few interpreter variables are never injected by `run`, and `--only` naming one of them is a usage error (exit 2).  Every `INFISICAL_*` key is in that list too, so a stored `INFISICAL_TOKEN` or client secret never reaches the command.  The fleet-secrets auth variables (`INFISICAL_TOKEN` and the universal-auth client id and secret) are removed from the started command's environment. |
| `fleet-secrets get NAME [--path /]` | Prints exactly one value to stdout with no trailing newline.  Exit 3 and a message naming the key if it is absent. |
| `fleet-secrets names [--path /]` | Key names only, one per line. |
| `fleet-secrets doctor [--json]` | Booleans, the CLI version and counts only:  CLI present, project id resolvable, auth mode, session valid, a test fetch, key count, unresolved-reference count.  Exit 1 when something is wrong. |

Exit codes:  0 ok, 2 usage or a refused setting, 3 key absent, 4 fetch or auth failure, 5 unresolved reference, 126 or 127 the command could not start.

**Where the values come from.**  Project Fleet-OPS, environment `prod` (the only one;  a non-prod `INFISICAL_ENVIRONMENT` or `INFISICAL_ENV` is refused).  Root `/` holds every key as an Infisical reference that expands to the real value, under the names the flat file used.  Folder `/global-api-keys` holds the same values as plain secrets, so `--path /global-api-keys` is the fallback for a key that is missing at root (checked Sun, Oct 18, 2026:  eleven names present in the flat file were not at root;  `AGENT_SYNC_TOKEN`, `AGENT_SYNC_POST_TOKEN`, `ANTIGRAVITY_INGEST_TOKEN`, `B2_CT_BUCKET_NAME`, `B2_FLEET_BUCKET_NAME`, `B2_ST_BUCKET_NAME`, `B2_UM_BUCKET_NAME`, `PAGERDUTY_API_KEY`, `USAGE_INGEST_TOKEN`, `USAGE_INGEST_PRODUCER_TOKENS` and `USAGE_READ_TOKEN`).  The repo is public, so the project id is never committed:  it comes from `INFISICAL_FLEET_OPS_PROJECT_ID`, then the first line of `~/.agent-sync/fleet-ops-project-id` (the same two sources and error text as `scripts/agent_sync/zulip.py`), and the command fails closed when neither holds a UUID.

**Auth, first match wins.**  (1) `INFISICAL_TOKEN` already in the environment (mode `token`, for a cloud worker).  (2) A universal-auth machine identity in the login Keychain (mode `keychain`):  generic passwords with service `fleet-secrets` and accounts `infisical-client-id` and `infisical-client-secret`.  (3) The same two items in the System keychain, `/Library/Keychains/System.keychain` (mode `system-keychain`), which is the path for pm2 and any other boot-time job:  pm2 is started by `/Library/LaunchDaemons/com.pm2.daemon.plist`, so its jobs run as `jay` in the launchd System session, where the login Keychain is unreachable.  The command reads an item with `security find-generic-password -s fleet-secrets -a ACCOUNT -w` (plus the System keychain path for mode 3), captures it, runs `infisical login --method=universal-auth --plain` with the client id and secret in the child's environment (`INFISICAL_UNIVERSAL_AUTH_CLIENT_ID` and `_CLIENT_SECRET`), and hands the access token to the export call as `INFISICAL_TOKEN`.  Neither the secret nor the token is ever in an argument list, and neither reaches the command `run` starts.  Only one of the two items in a keychain is an error (`keychain-incomplete`), not a quiet fallback.  (4) The Infisical CLI's own login session (mode `session`), which is how a login shell works.  A LaunchAgent should use mode 2 or 3 once the machine identity exists, because a user session expires.  `doctor` reports `keychain_client_*_present` and `system_keychain_client_*_present` as booleans and the chosen `auth_mode`.  The System keychain items are written with `-A` (any application may read them without a prompt), so any account on this Mac can read the automation identity;  the owner accepted that on 2026-10-18 (OPS-818).  Rotate the machine identity in Infisical to revoke.

**Safety.**  One fetch per process, values only in memory, never written to disk, a log or stderr.  The CLI's stdout and stderr are captured, never echoed, so a failure is a reason token (`cli-missing`, `timeout`, `exit-1`, `bad-output`, `login-failed`).  A value that is still `${...}` after expansion stops the command and names the key (exit 5).  `get` and `run --only` check only the keys they use;  `names`, `doctor` and a plain `run` check all of them.

### Migration cookbook

**(a) A shell script that greps one key.**

```bash
# before
KEY="$(grep -m1 '^KEY=' ~/.secrets/global-api-keys | cut -d= -f2- | tr -d '"')"
# after
KEY="$(fleet-secrets get KEY)" || exit $?
```

`get` already strips nothing and adds nothing, so drop the `tr`.  Add `--path /global-api-keys` for one of the eleven names above.  Never `echo` the variable.

**(b) A start script that sources the whole file.**

```bash
# before
set -a; . ~/.secrets/global-api-keys; set +a
exec ./server --port 8080
# after
exec fleet-secrets run -- ./server --port 8080
# or, when the process needs only a few keys
exec fleet-secrets run --only KEY_A,KEY_B -- ./server --port 8080
```

In a LaunchAgent plist or a pm2 ecosystem file, make `fleet-secrets` the program and put the old command after `run --`.  Use the absolute path `/Users/jay/.local/bin/fleet-secrets`, because a LaunchAgent's PATH is minimal.

**(c) Python or Node that opens and parses the file.**

```python
# before
for line in open(os.path.expanduser("~/.secrets/global-api-keys")):
    k, _, v = line.partition("="); env[k] = v.strip().strip('"')
# after
token = os.environ["KEY"]          # launched as:  fleet-secrets run --only KEY -- python3 job.py
```

Delete the file reader, read `os.environ` (`process.env` in Node) and launch the program under `fleet-secrets run`.  A job that must fetch inside the process can call `subprocess.run(["fleet-secrets", "get", "KEY"], capture_output=True, check=True).stdout.decode()`.  Do not put the value in argv or a log.

**One-time setup for a machine identity** (owner action, not done by the tool):  create a universal-auth machine identity in Infisical with read access to Fleet-OPS `prod`, then store its two values in the Keychain without printing them, for example `security add-generic-password -U -s fleet-secrets -a infisical-client-id -w` and the same for `infisical-client-secret`, answering the prompts.  `fleet-secrets doctor` then reports `auth_mode: keychain`.  **System keychain (owner action, once, with sudo):**  run `{ fleet-secrets get INFISICAL_AUTOMATION_CLIENT_ID; echo; fleet-secrets get INFISICAL_AUTOMATION_CLIENT_SECRET; echo; } | sudo bash scripts/fleet_secrets/seed-system-keychain.sh` from a checkout of this repo, in your GUI shell.  `get` returns `INFISICAL_*` names (only `run` withholds them), so no extra flag is needed.  The script writes the two items with `security add-generic-password -U -A` fed to `security -i` on stdin (so no value is ever on a command line), refuses a value with a character outside `[A-Za-z0-9+/=_.:@-]`, reads them back and prints only `match: true` or `match: false`.  After that, `bash scripts/fleet_secrets/switch-pm2-jobs.sh` proves `fleet-secrets doctor` passes from inside pm2 (`auth_mode: system-keychain`) and moves the pm2 jobs `mac-collab-litestream`, `vision-worker` and `mac-collab` onto `fleet-secrets`, restoring a job's backup if it does not come back online.

## Mac `global-api-keys` sync (`scripts/infisical-secrets-sync.sh`)

The LaunchAgent `com.jay.infisical-secrets-sync` copies selected secrets from Infisical
into `~/.secrets/global-api-keys`.  Nothing may read that file any more (OPS-818);  the readers in this repo now use `fleet-secrets` or the environment;  the Usage-Monitor jobs and the pm2 daemons are still moving (target, not yet shipped), and this sync retires at an owner-gated cutover.
Infisical remains the source of truth;  the file is derived and write-once rules apply where
noted in the sync map.

| Key | Kind | Status |
|---|---|---|
| `RESTIC_FLEET_WORK_PASSWORD` | secret | **to be filled by admin** (write-once in the sync map after the first good copy;  `fleet_work_backup.py new-password` sets it in Infisical without printing) |
| `B2_FLEET_WORK_BUCKET_NAME` | env config | **to be filled by admin** (dedicated bucket for fleet-work-backup;  not `B2_FLEET_BUCKET_NAME`) |
| `B2_FLEET_WORK_KEY_ID` | secret | **to be filled by admin** (scoped B2 application key, prefix `fleet-work-backup/`) |
| `B2_FLEET_WORK_APPLICATION_KEY` | secret | **to be filled by admin** |

Consumer:  `scripts/fleet-work-backup/` (`fleet_work_backup.py`, `install.py`).  Runbook:
`scripts/fleet-work-backup/README.md`.  Linear OPS-907.

## Hosted CLAUDE Zulip identity (AFC-296)

Canonical location: AI Fleet Coordinator, `prod`, `/zulip`.  These inventory
entries already exist, as confirmed by the owner.  Worker provisioning remains
unconfirmed.

| Key | Kind | Status |
| --- | --- | --- |
| `ZULIP_CLAUDE_EMAIL` | env config | Owner-confirmed existing entry; expected `claude-bot@simplewithus.zulipchat.com` |
| `ZULIP_CLAUDE_API_KEY` | secret | Owner-confirmed existing entry; Worker provisioning unconfirmed; never copy to source, chat or a client |

The Worker uses these same canonical names: var `ZULIP_CLAUDE_EMAIL` and secret
`ZULIP_CLAUDE_API_KEY`.  After authorization, the owner-operated installer
verifies the stored identity and provisions the Worker secret.  Its live
identity check requires the CLAUDE bot and member role 400 before writing.
See [the CLAUDE rollout](scripts/agent-sync-mcp/DEPLOY.md#add-hosted-claude-afc-296).

## FleetLink agent server credential (AFC-328)

Canonical location: AI Fleet Coordinator, `prod`, `/fleetlink`.

| Key | Kind | Status |
| --- | --- | --- |
| `FLEETLINK_AGENT_SECRET` | secret | Created and read back 2026-10-15.  Installed as Worker secret `FLEETLINK_AGENT_SECRET` on `agent-sync-mcp` and as `AGENT_SYNC_SECRET` on FleetLink's `fleetlink-api`.  Never copy it to source, chat or a client. |
| `FLEETLINK_TOKEN_<SEAT>` | secret | One per fleet bot (hyphens as underscores, such as `FLEETLINK_TOKEN_GROK_WEB`):  that bot's FleetLink API token, minted to follow Jay's limits as they change.  Read by the Agent-Sync Worker (bundled as `FLEETLINK_SEAT_TOKENS`) and written to `~/.secrets/FleetLink/<SEAT>.env` on hosts where the seat runs `fleet-share`.  No expiry. |

The stdio Agent-Sync server (`agent-sync mcp`, every Mac and server seat) serves the `fleetlink_*` tools too, as each identity's own FleetLink bot and never with `FLEETLINK_AGENT_SECRET`.  It runs `fleet-share` with that seat's token from `FLEETLINK_TOKEN_<SEAT>` in its environment, else `FLEETLINK_TOKEN` (only when no launcher is set and `AGENT_SEAT` is unset or the same seat), else the file `~/.secrets/FleetLink/<SEAT>.env` (hyphens as underscores, `FLEETLINK_TOKEN=...`, a regular file the user owns with mode 0600 or 0400), which it hands to the child as `FLEETLINK_AUTH_FILE` without reading it.  With none of them, those tools answer `not_configured` naming the key to set (`docs/protocols/agent-sync-mcp.md` section 2.2).

The Agent-Sync MCP Worker sends it, with the acting seat in `X-Fleet-Seat`, to
FleetLink's `/mcp`, which maps each seat to its own FleetLink agent account.
Rotate both Workers from this one value, FleetLink first.  See
[the FleetLink runbook](scripts/agent-sync-mcp/DEPLOY.md#fleetlink-afc-328).
