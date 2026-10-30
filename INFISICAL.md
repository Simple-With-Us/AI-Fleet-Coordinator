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
the existing limits.  The policy applies only to CURSOR, across all its grants, and only ever raises a default:  each window is the larger of the policy and the built-in limit (owner 2026-10-19), so Cursor's 100 Zulip writes an hour now count as 120.

Owner requested capacity for approximately 40 Cursor agents on 2026-10-15:
Linear writes 2,000/hour and 20,000/day, Linear reads 12,000/hour with separate
counters and pacing (100ms writes, 50ms reads).  Zulip posts/replies are
100/hour and 600/day; reads 1,500/hour; reactions 300/hour; recall contributions
50/hour and 200/day.  Zulip spacing stays 3 seconds for writes and 500ms for reads.
Other seats retain their existing limits.  Upstream rate limits still apply.

## Apple Notes through the Operator (AFC-486)

Apple Notes requests from the server and cloud seats go to the Operator's notes queue (`docs/protocols/operator-notes.md`), never through Zulip.  Four names, all in this project, `prod`:

| Name | Where | Who reads it |
|---|---|---|
| `OPERATOR_API_URL` (its row in "Fleet knobs" below is the generated one) | root `/`, a fleet knob (`scripts/fleet_knobs/registry.py`) | `simple-notes`, the stdio MCP and the Mac runner, through `fleet_knobs`, else (when the knob reads empty, as on the server once its last-known-good copy lapses) through `fleet-secrets get --project ai-fleet-coordinator`:  the Operator API's https origin.  Empty means not configured.  The Worker has the same origin as its committed `LISTENER_API_URL` var. |
| `OPERATOR_NOTES_WORKER_KEY` | `/operator`, a secret | the Operator (container start, `server/infisical_env.py`) and the Worker, as its secret `OPERATOR_NOTES_KEY` (DEPLOY.md, "Notes, recall and the guide") |
| `OPERATOR_NOTES_SERVER_KEY` | `/operator`, a secret | the Operator, and the server's CLIs and stdio MCP (`fleet-secrets get OPERATOR_NOTES_SERVER_KEY --path /operator --project ai-fleet-coordinator`, held in memory) |
| `OPERATOR_NOTES_RUNNER_KEY` | `/operator`, a secret | the Operator, and the Mac runner (`com.jay.operator-notes-runner`, the same `fleet-secrets get`) |

### Server Notes reader without environment credentials

The server's `simple-notes` and stdio MCP can use the existing read-only
`operator-notes-read` service token without a personal login or a CLI credential
argument. The SERVER consumer reads `~/.config/operator-notes/reader.json`.
Its parent directory must be owned by the current user and mode 0700; the file
must be a regular single-link current-user file, mode 0600, with no symlinks.
The schema has exactly `schema` (integer 1), `service_token` (the existing token),
`operator_api_url`, `project_id`, `token_id`, and `token_name`. The four non-secret
deployment pins are provisioned from the host's verified metadata and must match
the reviewed SHA-256 commitment in the reader, computed over canonical sorted JSON
with compact separators. They are not learned from the API response. Other
identities or destinations fail closed. No private inventory IDs or hostnames are
stored in source. Do not put a token in source, arguments, environment or logs.

When that file exists, it is authoritative for SERVER Notes. An unsafe file,
wrong token, expired token, failed request or unverified key fails closed; it does
not fall back to a personal login, environment credential or disk cache. An absent
file preserves existing behavior on hosts not using this reader. RUNNER and
WORKER are unchanged.

The reader checks the existing token's ID, name, AFC project, exact `prod:/operator`
scope, read-only permission and expiry, then makes a bounded, no-redirect point
read of `OPERATOR_NOTES_SERVER_KEY`. It verifies the returned key's project,
environment, folder, name, shared type and visible value. It never exports the
folder, writes a secret cache, creates/renews a login, or changes token scope.
Malformed HTTP status lines and incomplete responses are reduced to a short
redacted failure, never an exception containing response data.

A process may reuse the verified key for at most 600 monotonic elapsed seconds,
with a separate wall-clock check against the token's UTC expiry minus 30 seconds.
A backward wall-clock correction cannot extend the elapsed TTL. As with the existing KeySource cache, an Infisical
revocation or scope change can remain unobserved during this window if the Operator
still accepts the key. Every use rechecks the private config's custody and content;
changes invalidate the cache and require a new client. An Operator 401 drops it
without retrying the request. Expired cache is discarded before refresh, so a
failed refresh never returns a stale key. The cache is thread-safe and memory-only.

The non-secret URL comes from the committed private deployment pins; this does not
change fleet-knobs local-value or last-known-good freshness rules. Host provisioning,
private token custody and runtime activation remain separate release steps. Generic
`fleet-secrets` verified reads and writes are unchanged.

Each key is at least 32 random characters and is never printed, logged or written to a file.  The retired relay's knob `NOTES_RELAY_CONSUMER` (AFC-331) is no longer read by anything.

## Linear project linkifiers (AFC-565)

The Operator's Linear project webhook keeps Zulip and the repo linkifiers equal to Linear's projects (scripts/linear_project_linkifiers.py).  Its five secrets live in this project, `prod`, folder `/operator-linkifiers`, and are read only through its own scoped read-only service token, never from the environment.

| Name | Kind | Who reads it |
|---|---|---|
| `ZULIP_OPERATOR_EMAIL` | env config | the Operator webhook (the realm-admin bot's email) |
| `ZULIP_OPERATOR_API_KEY` | secret | the Operator webhook (upserts the realm linkifiers) |
| `LINEAR_API_KEY` | secret | the Operator webhook (reads Linear projects) |
| `LINEAR_WEBHOOK_SECRET` | secret | the Operator webhook (verifies the Linear-Signature) |
| `GITHUB_TOKEN` | secret | the Operator webhook (opens the sync PR) |
| `AFC_OPERATOR_LINKIFIERS_READ_TOKEN` | service token, read-only, scope prod:/operator-linkifiers | bound to the Operator container as `OPERATOR_LINKIFIERS_INFISICAL_TOKEN`; registered in the Fleet Reference sheet's Infisical Service Tokens tab |

Each value is never printed, logged or written to a file.

## Zulip Outgoing Wake Tokens

The existing server listener loads `ZULIP_OUTGOING_<CODE>_TOKEN` from prod `/zulip` at startup for each server seat.  `<CODE>` replaces hyphens in the seat tag with underscores.  These separate Zulip service tokens authenticate outgoing webhook requests; they are neither `ZULIP_<CODE>_API_KEY` nor the routine's bearer/HMAC key.  Missing tokens deny requests for that seat.

`scripts/zulip_migrate_outgoing.py` was retired 2026-10-19 because it needed the owner key;  bots are now provisioned through `scripts/zulip_provision_bots.py` with the realm-admin bot.  See [the outgoing-webhook runbook](docs/protocols/zulip-outgoing-migration.md).

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
| `ZULIP_SENTRY_EMAIL` / `ZULIP_SENTRY_API_KEY` | env config / secret | Sentry internal integrations for #alerts, #trading, #Work-In-Progress |
| `ZULIP_PAGERDUTY_EMAIL` / `ZULIP_PAGERDUTY_API_KEY` | env config / secret | PagerDuty v3 webhook to #incident-response |
| `ZULIP_LINEAR_EMAIL` / `ZULIP_LINEAR_API_KEY` | env config / secret | Linear webhook to #issues |
| `ZULIP_BETTERSTACK_EMAIL` / `ZULIP_BETTERSTACK_API_KEY` | env config / secret | BetterStack outgoing webhook to #alerts (`slack_incoming`) |
| `ZULIP_DATADOG_EMAIL` / `ZULIP_DATADOG_API_KEY` | env config / secret | Datadog `zulip-alerts` webhook to #alerts (`slack_incoming`) |

## pr-conflict-watch (Cloudflare Worker)

`workers/pr-conflict-watch` reads every secret from Infisical at runtime.  The Worker's only secret binding is one read-only Infisical service token; the other five secrets live in the Fleet-OPS project, `prod`, folder `/pr-conflict-watch`.  Names only, no values:

| Name | Kind | Who reads it |
|---|---|---|
| `GITHUB_WEBHOOK_SECRET` | secret | the Worker, to verify `X-Hub-Signature-256` |
| `GITHUB_TOKEN` | secret | the Worker, for read-only GitHub PR lookups |
| `ZULIP_SITE` | config (URL) | the Worker, the Zulip site base URL |
| `ZULIP_BOT_EMAIL` | config | the Worker, the fleet alerts bot (the MA bot) email |
| `ZULIP_BOT_API_KEY` | secret | the Worker, that bot's API key |

The Zulip pair is the fleet alerts bot, the MA bot: the same credential lives at Fleet-OPS prod `/zulip` as `MA_ZULIP_SITE` / `MA_ZULIP_EMAIL` / `MA_ZULIP_KEY`.  The folder holds plain values, not Infisical references, because expanding a reference would need the service token to read `/zulip` too.

One read-only service token, `pr-conflict-watch-read`, is scoped to exactly `prod` + `/pr-conflict-watch` and is bound to the Worker as the secret `INFISICAL_SERVICE_TOKEN`.  The Worker discovers the project id from the token itself (the repo is public, so the id is not committed) and refuses a token that may write or that is not scoped to that folder (`token_not_read_only`, `token_scope_mismatch`).  See [workers/pr-conflict-watch/README.md](workers/pr-conflict-watch/README.md).

## Runtime contract

1. **Load at startup.**  `main()` calls `infisical_settings.configure()` + `init_settings()` before binding: universal-auth login → access token → `GET /api/v3/secrets/raw`.  A missing required key fails fast with a `SettingsError` naming the key and pointing here.  Backend keys (`QDRANT_*`, `TEI_*`) are then mirrored into `os.environ` so the `fleet_rag` library (which reads env) keeps working unchanged.  Migration bridge: any managed key missing from Infisical is backfilled from the process environment, and the backfilled key names are logged, so the service keeps its old behavior while the admin fills Infisical in.
2. **Never fetch per-request.**  All runtime reads come from the in-memory cache (`infisical_settings.get()`).  There is no per-request, per-tick, or per-event Infisical call anywhere in the request path — verified by test (`test_runtime_reads_make_zero_network_calls_after_init`).
3. **Background refresh.**  A daemon thread re-fetches every `SETTINGS_REFRESH_SECONDS` (default 300), re-exports the backend keys, and on failure logs loudly while keeping the last-known-good cache.  On-demand refresh: `SIGHUP` to the process, `POST /admin/reload-settings`, or the admin settings save.
4. **Write-through on admin save.**  `POST /admin/settings` calls `infisical_settings.set()`, which writes to Infisical FIRST (`POST`/`PATCH /api/v3/secrets/raw`) and only then updates the local cache.  A failed write fails the save (502) and the cache is left untouched.

**Service token.**  A consumer can bind an Infisical service token as `INFISICAL_TOKEN` in the service environment, and it wins over the machine identity: the token is the bearer directly, so no universal-auth login happens at all, and a 401 raises a `SettingsError` naming the variable instead of a re-login retry, because a service token is not something the loader can refresh.  The token must carry the `st.` prefix, no whitespace and 20 to 2048 characters, or startup refuses it before any API call; its value never reaches a log, an error message, the admin listing or the environment export.  Bind it in the platform's own secret store for that service and let the platform inject it as an environment variable at start (Coolify, systemd, a container runtime); never write it into an image, a repository or a sheet.

## Admin gating

`GET /admin/settings` (key inventory; secret values masked as `set`/`empty`, never returned), `POST /admin/settings` (write-through save), and `POST /admin/reload-settings` require `Authorization: Bearer <RECALL_ADMIN_TOKEN>`.  The seat bearer (`RECALL_API_TOKEN`) gets 401 on these routes; with `RECALL_ADMIN_TOKEN` unset the routes answer 403.  "Admin" is whoever holds the admin token — the fleet operator, not the seats.

## Per-user boundary (out of scope by design)

This service has no per-user settings: seat identity is passed per-request by the caller (`recall_contribute` requires a `seat` argument) and is never stored.  Nothing per-user belongs in Infisical, and nothing here was migrated into it.

## Deliberately not migrated (this PR)

- THE BOARD server (`scripts/mac-collab/`, retired on 2026-10-19, AFC-363): Mac-local; read `~/.secrets/mac-collab.env` (canonical) or the environment.  Operator-managed on the Mac, not a hosted service surface.  Its tracked source was removed from this repo when the server was retired, and the env file it read went with it.
- The `/mac-collab` Fleet-OPS folder is gone (owner Jay, 2026-10-19, AFC-363):  its 15 `MAC_COLLAB_TOKEN*` secrets (root and per-seat), the 26 root `MAC_COLLAB_*` reference entries that pointed at them, and the `admin-jays-services` Worker secret `MAC_COLLAB_TOKEN` were deleted.  The stale `/global-api-keys` copy `MAC_COLLAB_TOKEN` and its root reference `GLOBAL_API_KEYS_MAC_COLLAB_TOKEN` were deleted the same day (owner Jay, AFC-363);  no `MAC_COLLAB` name is left in Fleet-OPS.
- Every Linear app's client credentials live in this project under `/linear`:  `LINEAR_<SEAT>_CLIENT_ID` / `_SECRET` (hyphens as underscores) and the shared app's `LINEAR_FLEET_CLIENT_ID` / `_SECRET`.  The `simple-board` CLI reads them from there through the `INFISICAL_AUTOMATION` identity, after the `LINEAR_CLIENT_ID` / `LINEAR_CLIENT_SECRET` environment pair, and it reads no credential file at all (AFC-348, AFC-359).  The same folder holds one shared minted token per app, `LINEAR_<SEAT>_CACHE_TOKEN` and `LINEAR_FLEET_CACHE_TOKEN`:  a JSON row (token, mint and expiry times, credential fingerprint, minting host) that `simple-board` writes after every mint and reads instead of minting again.  Never print one.
- `fleet-sentry-monitor/monitor.py`: operator cron script; env-configured on the box that runs it.
- `fleet_rag` CLI tools and MCP scripts: per-invocation env; no long-lived process to host a cache.
- `RECALL_FAKE=1`: local test/smoke switch only; stays an env var.  Bootstrap plumbing (`RECALL_REF`, `GITLEAKS_*`) likewise stays env-only.

## Fleet knobs

`scripts/fleet_knobs/` is how the open-loops jobs (LaunchAgents, hooks and CLIs that run for seconds) read their modes, caps and flags.  The values live in this project, `prod`, at the root path, and they are the only place a mode or a cap is set.  A refresh thread does not fit a job that lives a few seconds, so the contract differs from the service above:

1. **Fetch once per process.**  The first `knobs.get()` runs one `infisical export --env=prod --projectId=... --format=json` through the installed CLI, keeps only the names in the registry and drops the rest at once.  A failed fetch is not retried in the same process.  A non-prod `INFISICAL_ENVIRONMENT` or `INFISICAL_ENV` is refused before anything runs.  Auth follows `fleet-secrets` (below), first match wins:  `INFISICAL_TOKEN` already set, else (on Linux) the automation pair `INFISICAL_AUTOMATION_CLIENT_ID` and `INFISICAL_AUTOMATION_CLIENT_SECRET` in the environment, else the machine identity in the login Keychain, then the System keychain, each logged in through fleet-secrets' own helper (loaded from `FLEET_SECRETS_HOME`, the sibling `scripts/fleet_secrets`, or `~/.local/share/fleet-secrets`), else the CLI's own session.  A half-set automation pair is reason `automation-env-incomplete:missing-<var>` (the variable name in lowercase, as the failure marker keeps it) ``, not a keychain fault, and the pair never reaches the export child.  This is what makes a fetch work under pm2, which runs as the system LaunchDaemon `com.pm2.daemon` where the login Keychain and the CLI session are unreachable.  The token goes only into the CLI child's environment, never into a log, an argument list or a file.  The whole fetch (keychain lookups, the machine-identity login and the export) shares one 8 second budget, and running out anywhere is reason `timeout`.  **Fail fast across processes.**  A failed fetch writes `~/.local/state/fleet-knobs/fetch-failed.json` (0600, a timestamp and the reason token only).  For 60 seconds after it, other processes skip the fetch and use the last-known-good copy (reason `recent-<token>`), so a broken auth path costs one timeout across a burst of processes instead of one each.  A good fetch removes the marker, and `knobs.refresh()` ignores it.  A secret still fails closed inside the window.
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
| `OPERATOR_API_URL` | N1 | str | unset | no | nothing | the Operator API's https origin (no path):  where simple-notes, the stdio MCP and the Mac runner send Apple Notes requests off a Mac (docs/protocols/operator-notes.md);  empty means not configured;  the Worker has the same origin as its LISTENER_API_URL var |
| `APPLE_NOTES_PIN_RETRY` | N1 | bool | `true` | no | only set it to `false` | true lets apple-notes-coding.sh run the pin or unpin Shortcut a second time when the first run misses the note index;  false keeps the single attempt;  the APPLE_NOTES_PIN_RETRY environment variable is only a local narrowing override and can turn the retry off, never on |
| `CONSULT_ENABLED` | C7 | bool | `false` | no | only set it to `false` | false refuses every consult verb |
| `FLEET_SHEET_INGEST_ENABLED` | S1 | bool | `true` | no | only set it to `false` | false writes the registry mirror and skips the `recall ingest --source doc` that carries it;  the sheet itself is still written |
| `FLEET_SHEET_INGEST_TIMEOUT_MS` | S1 | int 60000 to 21600000 | `5400000` | no | only lower it | milliseconds the Fleet RAG ingest may run before fleet-apps-sheet-sync.mjs gives up on it;  a full corpus walk took about 43 minutes, so the default is 90;  a local file may only lower it |
| `COMPLIANCE_CERT_EXPIRY_DAYS` | C8 | int 1 to 365 | `30` | no | only lower it | days before an Apple signing certificate expires that the fleet-config compliance rule calls a finding;  the rule reads the value only once it is set in Infisical,  so the default is the value to set rather than one the rule may use |
| `COMPLIANCE_SECRETS_FINDINGS_MAX` | C8 | int 0 to 10 | `0` | no | only lower it | how many secrets-hygiene findings the fleet-config compliance rule tolerates;  the rule reads the value only once it is set in Infisical,  so the default is the value to set rather than one the rule may use |

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

`python3 scripts/check-infisical-env.py` (CI step "Infisical environment lint") fails on anything that would select a non-prod environment:  `INFISICAL_ENV` / `INFISICAL_ENVIRONMENT` set to `dev` or `staging` in an env file (`.cursor/infisical.env`, `.env.example`), a `:-dev` / `:=dev` fallback or `--env=dev` in a `cursor-cloud-start.sh`, and a `.infisical.json` whose `defaultEnvironment` is not `prod`.  `--fleet` lints every repo under `~/Code` as it is on `origin/main` (add `--fetch` to refresh first).  A line that truly needs the word carries `infisical-env: allow` with a reason.  This repo's CLI callers (`scripts/fleet_knobs`, `scripts/fleet_secrets`) always pass `--env=prod` and `--projectId` themselves, so it has no `.infisical.json`;  a repo that relies on one gets it with `"defaultEnvironment": "prod"`, because the CLI's own default is `dev`.

## Local dev

Copy `.env.example` (non-sensitive keys) and set the secrets in your shell, or point `INFISICAL_MACHINE_IDENTITY` at your handoff file and leave `INFISICAL_ENVIRONMENT` unset (it is `prod`).  Local runs read prod, so treat the secrets there as live.  Any other slug is refused:  `init()`, `set()` and every fetch raise `SettingsError` before the network, `refresh()` logs the refusal and keeps the last-known-good cache, and `configure()` leaves the previous settings in place.  Never commit real values.

## fleet-secrets

`fleet-secrets` (`scripts/fleet_secrets/`, Linear OPS-818) is the one way a Mac script, LaunchAgent or pm2 start script reads Fleet-OPS secrets now that nothing may read the flat file `~/.secrets/global-api-keys`.  Install it with `bash scripts/install-fleet-secrets.sh`;  that copies the package to `~/.local/share/fleet-secrets/` and links `~/.local/bin/fleet-secrets` to the copy (a symlink into a lane would break when the lane is retired), so re-run the installer after the package changes.  Python stdlib only.  Run `fleet-secrets doctor` to check it.

| Command | What it does |
|---|---|
| `fleet-secrets run [--path /] [--only A,B] -- CMD [ARG...]` | Fetches once, then `exec`s `CMD` with the secrets as environment variables (a variable of the same name is replaced).  `--only` injects just those keys and exits 3 if one is absent.  PATH, HOME, USER, SHELL, TMPDIR, `LD_*`, `DYLD_*` and a few interpreter variables are never injected by `run`, and `--only` naming one of them is a usage error (exit 2).  Every `INFISICAL_*` key is in that list too, so a stored `INFISICAL_TOKEN` or client secret never reaches the command.  The fleet-secrets auth variables (`INFISICAL_TOKEN` and the universal-auth client id and secret) are removed from the started command's environment. |
| `fleet-secrets get NAME [--path /] [--project fleet-ops\|ai-fleet-coordinator]` | Prints exactly one value to stdout with no trailing newline.  Exit 3 and a message naming the key if it is absent.  `--project` picks the Infisical project (read-only, default Fleet-OPS);  the FleetLink seat tokens are read with `--project ai-fleet-coordinator --path /fleetlink`. |
| `fleet-secrets names [--path /] [--project fleet-ops\|ai-fleet-coordinator]` | Key names only, one per line.  `--project ai-fleet-coordinator` is the one read-only second project (the FleetLink seat tokens live in its `/fleetlink`);  its id comes from `INFISICAL_AI_FLEET_COORDINATOR_PROJECT_ID` or `~/.agent-sync/ai-fleet-coordinator-project-id`. |
| `fleet-secrets set NAME --path /linear` | Creates or updates one Fleet-OPS prod secret, but ONLY names on the built-in allowlist `core.WRITE_ALLOWLIST` (now `^CACHE_[A-Z0-9_]+$` under `/linear`, the minted Linear tokens `simple-board` keeps);  anything else exits 2 before auth.  Widening the list takes a reviewed code change, never a flag or environment variable (a general write would let any agent overwrite any secret through the shared identity) (AFC-359).  The value comes from standard input (one trailing newline removed), never an argument.  It writes over REST with the machine identity's token (Keychain or `INFISICAL_TOKEN`);  the CLI's own login session is refused because it cannot hand a token to a REST call.  It never creates a folder:  a missing one fails with `folder-missing` and writes nothing.  Prints `created|updated NAME at PATH`, never the value.  Exit 4 on a failed write. |
| `fleet-secrets doctor [--json]` | Booleans, the CLI version and counts only:  CLI present, project id resolvable, auth mode, session valid, a test fetch, key count, unresolved-reference count.  Exit 1 when something is wrong. |

Exit codes:  0 ok, 2 usage or a refused setting, 3 key absent, 4 fetch or auth failure, 5 unresolved reference, 126 or 127 the command could not start.

**Where the values come from.**  Project Fleet-OPS, environment `prod` (the only one;  a non-prod `INFISICAL_ENVIRONMENT` or `INFISICAL_ENV` is refused).  Root `/` holds every key as an Infisical reference that expands to the real value, under the names the flat file used.  Folder `/global-api-keys` holds the same values as plain secrets.  Every key in the flat file is at root (checked Sun, Oct 18, 2026:  eleven names that were missing, the `AGENT_SYNC_*`, `B2_*_BUCKET_NAME` and `USAGE_*` tokens, `ANTIGRAVITY_INGEST_TOKEN` and `PAGERDUTY_API_KEY`, now have root references), so `--path` is only needed for keys that live in a folder alone.  The repo is public, so the project id is never committed:  it comes from `INFISICAL_FLEET_OPS_PROJECT_ID`, then the first line of `~/.agent-sync/fleet-ops-project-id` (the same two sources and error text as `scripts/agent_sync/zulip.py`), and the command fails closed when neither holds a UUID.

**Auth, first match wins.**  (1) `INFISICAL_TOKEN` already in the environment (mode `token`, for a cloud worker).  (2) On Linux only (the Hetzner server, which has no keychain), the server's universal-auth identity in `INFISICAL_AUTOMATION_CLIENT_ID` and `INFISICAL_AUTOMATION_CLIENT_SECRET` in the environment (mode `automation-env`):  an identity handed to the process in its environment always beats a keychain one, and `run` never passes the pair on.  Only one of the two is an error (`automation-env-incomplete`).  (3) A universal-auth machine identity in the login Keychain (mode `keychain`):  generic passwords with service `fleet-secrets` and accounts `infisical-client-id` and `infisical-client-secret`.  (4) The same two items in the System keychain, `/Library/Keychains/System.keychain` (mode `system-keychain`), which is the path for pm2 and any other boot-time job:  pm2 is started by `/Library/LaunchDaemons/com.pm2.daemon.plist`, so its jobs run as `jay` in the launchd System session, where the login Keychain is unreachable.  The command reads an item with `security find-generic-password -s fleet-secrets -a ACCOUNT -w` (plus the System keychain path for mode 4), captures it, runs `infisical login --method=universal-auth --plain` with the client id and secret in the child's environment (`INFISICAL_UNIVERSAL_AUTH_CLIENT_ID` and `_CLIENT_SECRET`), and hands the access token to the export call as `INFISICAL_TOKEN`.  Neither the secret nor the token is ever in an argument list, and neither reaches the command `run` starts.  Only one of the two items in a keychain is an error (`keychain-incomplete`), not a quiet fallback.  (5) The Infisical CLI's own login session (mode `session`), which is how a login shell works.  A LaunchAgent should use mode 3 or 4 once the machine identity exists, because a user session expires.  `doctor` reports `keychain_client_*_present` and `system_keychain_client_*_present` as booleans and the chosen `auth_mode`.  The System keychain items are written with `-A` (any application may read them without a prompt), so any account on this Mac can read the automation identity;  the owner accepted that on 2026-10-18 (OPS-818).  Rotate the machine identity in Infisical to revoke.

**Safety.**  One fetch per process, values only in memory, never written to disk, a log or stderr.  The CLI's stdout and stderr are captured, never echoed, so a failure is a reason token (`cli-missing`, `timeout`, `exit-1`, `bad-output`, `login-failed`).  A value that is still `${...}` after expansion stops the command and names the key (exit 5).  `get` and `run --only` check only the keys they use;  `names`, `doctor` and a plain `run` check all of them.

### Migration cookbook

**(a) A shell script that greps one key.**

```bash
# before
KEY="$(grep -m1 '^KEY=' ~/.secrets/global-api-keys | cut -d= -f2- | tr -d '"')"
# after
KEY="$(fleet-secrets get KEY)" || exit $?
```

`get` already strips nothing and adds nothing, so drop the `tr`.  Never `echo` the variable.

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

**One-time setup for a machine identity** (owner action, not done by the tool):  create a universal-auth machine identity in Infisical with read access to Fleet-OPS `prod`, then store its two values in the Keychain without printing them, for example `security add-generic-password -U -s fleet-secrets -a infisical-client-id -w` and the same for `infisical-client-secret`, answering the prompts.  `fleet-secrets doctor` then reports `auth_mode: keychain`.  **System keychain (owner action, once, with sudo):**  run `{ fleet-secrets get INFISICAL_AUTOMATION_CLIENT_ID; echo; fleet-secrets get INFISICAL_AUTOMATION_CLIENT_SECRET; echo; } | sudo bash scripts/fleet_secrets/seed-system-keychain.sh` from a checkout of this repo, in your GUI shell.  `get` returns `INFISICAL_*` names (only `run` withholds them), so no extra flag is needed.  The script writes the two items with `security add-generic-password -U -A` fed to `security -i` on stdin (so no value is ever on a command line), refuses a value with a character outside `[A-Za-z0-9+/=_.:@-]`, reads them back and prints only `match: true` or `match: false`.  After that, `bash scripts/fleet_secrets/switch-pm2-jobs.sh` proves `fleet-secrets doctor` passes from inside pm2 (`auth_mode: system-keychain`) and moves the pm2 job `vision-worker` onto `fleet-secrets`, restoring a job's backup if it does not come back online.

## Brokering credentials to agents

`fleet-secrets` injects values into a process it starts.  A **broker** is the other shape:  the agent never receives the value, only a placeholder, and a proxy attaches the real credential to the outbound HTTPS request.  Infisical ships two, and they are not interchangeable.

| | Agent Proxy | Agent Vault |
|---|---|---|
| Command | `infisical secrets agent-proxy run \| start \| connect` | `infisical agent-vault proxy \| run` |
| Sandbox | `run` sandboxes on macOS (Seatbelt) | no sandbox |
| Agent credential | universal-auth client id and secret | time-bound session token over a named access bundle |
| Plan | any plan | Pro add-on, 5,000 brokered requests a month included, then $2 per 1,000 |
| Infisical's guidance | local sandboxed proxy, or dynamic secrets | **recommended for new setups** |

Measured on this Mac, Oct 18 (OPS-818):  the `run` sandbox denies `~/.infisical`, `~/.ssh`, `~/.aws` and `~/.config/gh` but **not** `~/.secrets`, `~/Library/Keychains`, `~/.codex/config.toml` or `~/.claude.json`, and it has no deny flag;  `127.0.0.1` is unreachable inside it even with `--allow-host`, so local MCP servers and hooks break;  `--unmatched-host block` works;  and the first run writes `~/.infisical/agent-proxy/local-ca.{crt,key}`, which expires in about a week.  Agent Vault has no sandbox and `--no-proxy` always merges `localhost` and `127.0.0.1`, and its CA is fetched per run with optional `--ca-fingerprint` pinning.

Not everything can be brokered.  A broker attaches a header to an HTTPS request, so App Store Connect (a JWT signed by the p8), the Google service-account JSON, OAuth refresh tokens, IMAP/SMTP app passwords, certificates and private keys, and every loopback service are out of its reach and stay on `fleet-secrets run`.  `docs/plans/2026-10-18-fleet-ops-brokerable-host-map.md` classifies all 667 keys in Fleet-OPS `prod` by host, scheme and whether a broker can carry them, and names the two identities in `/agent-proxy` the pilot needs.  Adoption plan and phase gates:  `docs/plans/2026-10-18-infisical-agent-proxy-adoption.md`.

## Mac `global-api-keys` sync (retired, AFC-359)

The LaunchAgent `com.jay.infisical-secrets-sync` used to copy a few Infisical secrets into `~/.secrets/global-api-keys`.  Nothing reads that file (OPS-818), and the sync would refill the folder after it is archived, so `bash scripts/retire-secrets-dir.sh bootout` stops it and moves its plist, script and the one-time seeder `~/apps/infisical-sync` to `~/apps/_retired/` (the script, its plist template and its test are removed from this repo).  The keys it carried (the restic and B2 `fleet-work-backup` keys, the Cloudflare token) are read with `fleet-secrets` from Fleet-OPS root.

## Retiring `~/.secrets` (AFC-359)

Owner ruling (2026-10-18):  nothing on this Mac or in a fleet repo reads `~/.secrets`;  every value lives in Fleet-OPS (prod) and is read with `fleet-secrets` (environment first, then `fleet-secrets get NAME --path /folder`, then a loud failure that names the folder and key, never a fallback to a file).  Jay decides when the folder is deleted;  agents never delete it.

| Old file or folder | Infisical folder (Fleet-OPS, prod) |
|---|---|
| `Zulip/<Bot>-zuliprc` | `/zulip`, keys `<BOT>_ZULIP_EMAIL`, `_KEY`, `_SITE` |
| `ZulipAdmin/realm-admin.zuliprc` | `/zulip_operator`, `ZULIP_OPERATOR_*` (the realm-admin bot, role administrator) |
| `Linear/<SEAT>.env` | `/linear`, `<SEAT>_LINEAR_CLIENT_ID` and `_CLIENT_SECRET` |
| `appstore-connect.env`, `AuthKey_*.p8` | `/asc` (`ASC_KEY_ID`, `ASC_ISSUER_ID`, `ASC_KEY_P8`);  per-key `.p8` folders `/authkey-<id>-p8` |
| `ios-distribution.p12`, `ios-dist-p12-pass`, `apple-certs` | `/mac-certs` (`IOS_DISTRIBUTION_P12_BASE64`, `IOS_DIST_P12_PASSWORD`), root `IOS_DIST_P12_PASS` |
| `botfleet-developer-id-export-*/` | `/botfleet-developer-id-export` (`DEVELOPER_ID_P12_BASE64`, `P12_PASSWORD`) |
| `google-sheets-sa.json` | `/google-sheets-sa`, `GOOGLE_SHEETS_SA` |
| `seat-mcp.env`, `grok-acp.env`, `senate-relay.env`, `icloud-mail.env`, `provider-knob-sync.env` | `/seat-mcp`, `/grok-acp`, `/senate-relay`, `/icloud-mail`, `/provider-knob-sync` |
| `agent-sync.env`, `fleetlink-auth.env`, `fleetlink-handbook.env`, `fleet-share-hmac.env` | `/agent-sync`, `/fleetlink-auth`, `/fleetlink-handbook`, `/fleet-share-hmac` |
| `usage-monitor*.env`, `backblaze-*.env`, `ct-r2-weekly-archive.env` | `/usage-monitor`, `/usage-monitor-bills`, `/backblaze-monitor`, `/backblaze-app-keys`, `/ct-r2-weekly-archive` |
| `composio-session-id` | `/composio-session-id`, `COMPOSIO_SESSION_ID` (line 1 the session id, line 2 the MCP url) |
| `global-api-keys` | root `/` and `/global-api-keys` |
| (BotFleet webhook receivers, AFC-359) | `/botfleet-github-webhooks` (new);  `/botfleet-firefighter-webhooks` and `/botfleet-housekeeper-webhook` rotated after a path leaked into the seat-mcp log |

The names for every other file match its name (lower case, dashes), with one folder per file.

**State is not a secret.**  A file that a program rewrites while it runs (Echo's OAuth bundle, watermarks, health files, token caches) lives outside `~/.secrets`:  `~/.agent-sync/echo`, `~/.agent-sync/echo-linear`, `~/.cache/fleet-linear/` (directories 0700, files 0600).  `ECHO_OAUTH_JSON` in `/agent-sync` is only a bootstrap seed and goes stale on the first token refresh.

**Cutover** (`scripts/retire-secrets-dir.sh`, tested by `scripts/test-retire-secrets-dir.sh`, allowlist `scripts/retire-secrets-dir.allow`).  The script never deletes anything.

1. `bash scripts/retire-secrets-dir.sh check`  lists every reader left in the repos (on `origin/main`) and on the Mac, every symlink that points into the folder, and every process that holds a file open there.  Output is paths and line numbers only.
2. `bash scripts/retire-secrets-dir.sh bootout`  stops and disables `com.jay.infisical-secrets-sync`, the one writer.
3. `bash scripts/retire-secrets-dir.sh archive --rename`  copies the folder into an AES-256 encrypted disk image in `~/Documents` (you type the passphrase twice), verifies the image by mounting it read-only and comparing it with the folder, then renames the folder to `~/.secrets.retired-<stamp>`.  `rollback` puts it back and re-enables the writer.
4. After a quiet day, delete the renamed folder yourself.  The script prints the command and never runs it.

**fleet-work-backup is retired (owner ruling 2026-10-19).**  It was never installed on this Mac and its restic repository has zero snapshots, so OPS-907's backup never ran.  The B2 bucket named by `B2_FLEET_WORK_BUCKET_NAME` is left in place and empty, and the code stays in the repo (`scripts/fleet-work-backup/`) with nothing scheduling it.  Do not describe backups as working.

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

## Server fleet-tools update signal (2026-10-19)

AFC `prod` `/fleet-cli` holds the three values the GitHub workflow `fleet-cli-server-update.yml` uses to tell the Hetzner server to move its fleet-tools checkout (`docs/protocols/fleet-cli-from-git.md`), copied to repository secrets of the same names:

| Key | Kind | Notes |
|---|---|---|
| `FLEET_CLI_UPDATE_SSH_KEY` | secret | An ed25519 private key.  Its public half is in the server's `/root/.ssh/authorized_keys` with `restrict` and a forced command that only starts `fleet-cli-git-update.service`. |
| `FLEET_CLI_UPDATE_HOST` | private infrastructure | The server's address, kept out of this public repo. |
| `FLEET_CLI_UPDATE_KNOWN_HOSTS` | public | The server's host key line, so the workflow checks the host strictly. |

Rotate by minting a new key, replacing the `authorized_keys` line and both copies.  The repository variable `FLEET_CLI_SIGNAL_ENABLED=true` turns the workflow on.

## FleetLink agent server credential (AFC-328)

Canonical location: AI Fleet Coordinator, `prod`, `/fleetlink`.

| Key | Kind | Status |
| --- | --- | --- |
| `FLEETLINK_AGENT_SECRET` | secret | Created and read back 2026-10-15.  Installed as Worker secret `FLEETLINK_AGENT_SECRET` on `agent-sync-mcp` and as `AGENT_SYNC_SECRET` on FleetLink's `fleetlink-api`.  Never copy it to source, chat or a client. |
| `FLEETLINK_TOKEN_<SEAT>` | secret | One per fleet bot (hyphens as underscores, such as `FLEETLINK_TOKEN_GROK_WEB`):  that bot's FleetLink API token, minted to follow Jay's limits as they change.  Read by the Agent-Sync Worker (bundled as `FLEETLINK_SEAT_TOKENS`) and written to `~/.secrets/FleetLink/<SEAT>.env` on hosts where the seat runs `fleetlink`.  No expiry. |

The stdio Agent-Sync server (`simple-chat mcp`, every Mac and server seat) serves the `fleetlink_*` tools too, as each identity's own FleetLink bot and never with `FLEETLINK_AGENT_SECRET`.  It runs `fleetlink` with that seat's token from `FLEETLINK_TOKEN_<SEAT>` in its environment, else `FLEETLINK_TOKEN` (only when no launcher is set and `AGENT_SEAT` is unset or the same seat;  the server sets `AGENT_SEAT` to the seat it resolved from `--as` or `--default-seat` when the environment named none), else the file `~/.secrets/FleetLink/<SEAT>.env` (hyphens as underscores, `FLEETLINK_TOKEN=...`, a regular file the user owns with mode 0600 or 0400), which it hands to the child as `FLEETLINK_AUTH_FILE` without reading it.  With none of them, those tools answer `not_configured` naming the key to set (`docs/protocols/agent-sync-mcp.md` section 2.2).

The Agent-Sync MCP Worker sends it, with the acting seat in `X-Fleet-Seat`, to
FleetLink's `/mcp`, which maps each seat to its own FleetLink agent account.
Rotate both Workers from this one value, FleetLink first.  See
[the FleetLink runbook](scripts/agent-sync-mcp/DEPLOY.md#fleetlink-afc-328).


## WhatsApp Wake Bridge (AFC-477, Not Provisioned)

The owner selected dedicated read-only service tokens in AFC.  This supersedes the earlier machine-identity/Fleet-OPS proposal; no credentials have been provisioned.  Bridge and binder use exact AFC `prod` `/jet`.  Operator uses the separate sibling AFC `prod` `/operator-jet-wake`, containing only its distinct signing key and literal Worker URL.  No machine identity, project-wide grant, Operator read/write token or plan purchase is involved.

| Name | Source | Consumer |
|---|---|---|
| Existing `WHATSAPP_API_KEY`, unchanged | AFC prod `/jet` | Sole poller and supervised binder only |
| `WA_BRIDGE_HMAC_KEY_JET` | AFC prod `/jet` | Poller/binder and existing Worker |
| `WA_OWNER_BINDING_JET` | Verified private binding | Worker only |
| `WA_AGENT_HASH_JET`, `WA_SENDER_HASH_JET`, `WA_STATE_DIRECTORY` | AFC prod `/jet` | Sole poller |
| `WHATSAPP_BRIDGE_ENABLED`, `WHATSAPP_BRIDGE_WAKE_ENABLED` | Approved exact `on` settings | Bridge/Worker as applicable; default off |
| `WA_OPERATOR_HMAC_KEY_JET` | AFC prod `/operator-jet-wake` | Operator client and existing Worker |
| `WA_WORKER_URL` | Literal origin in both folders | Respective client |

Approved bootstrap tokens expire 90 days after provisioning and are server-scoped to `read` on one literal prod folder each, without wildcards, children or write grants.  Root-only systemd credentials deliver only the mode-specific token.  `launch.py` rejects ambient/shared auth; the reviewed `fleet_secrets run` strips bootstrap authentication before executing its exact child-key allowlist.  Operator receives no Meta key, owner-ingestion key, private owner binding or AFC `/jet` read grant.  Scope receipts and negative reads remain required before activation; mock tests are not proof of live provider permissions.  Expiry stops future loads, not keys already held by a running process.

No daily wake quota is introduced.  The [bridge runbook](scripts/whatsapp_bridge/README.md) gives exact token names, expiry, protected storage, recipients and action-time approval steps.  The parent relayed owner approval for the bounded 90-day token setup; provisioning and activation still await required CI/review readiness and coordination.  Neither client receives renewal or credential-management permission.

### Existing Linux root-private bootstrap launcher

For a Linux root host that already has its automation identity in
`/root/.config/fleet-secrets/bootstrap.json`, the reviewed opt-in installer is:

```sh
bash scripts/install-fleet-secrets.sh --linux-private-bootstrap
```

This installs code only. It neither creates nor copies that private file, changes
its contents, nor exports credentials into the shell or service configuration.
The file must be a root-owned, single-link regular file, mode 0600, in its
root-owned mode-0700 `fleet-secrets` directory. Ancestor directories must be
root-owned and not group/world writable. Symlinks are refused. The bounded JSON
must contain exactly two nonempty string fields:
`INFISICAL_AUTOMATION_CLIENT_ID` and `INFISICAL_AUTOMATION_CLIENT_SECRET`.

An inherited token still wins. A complete inherited automation pair wins over
the private file; a partial pair fails rather than mixing sources. Only Linux
UID 0 can use the fixed private-file fallback. Login is scoped to the child
process; the automation pair is stripped before delegation and never printed.
When neither an inherited token nor an automation pair nor a root-private
bootstrap is available, the wrapper delegates to the plain CLI's existing auth
policy rather than requiring a machine identity. Plain reads can use the CLI's
own valid session; `get-verified` and writes still refuse session-only auth.
A partial pair, invalid inherited token, unsafe bootstrap or failed machine
login remains a refusal, never a session downgrade. Delegation does not create
or renew a session and does not prove that the current host is authenticated.
Help reads no credential and performs no login. The normal installer, including
Mac installs, is unchanged. Re-running without the flag restores the plain
launcher. Applying this on a host and checking that host's tools is separate
from source review and tests.
