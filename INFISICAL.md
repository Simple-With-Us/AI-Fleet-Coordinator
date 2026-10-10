# INFISICAL.md — Sole Source of Truth for AI-Fleet-Coordinator

Owner directive (2026-10-03): **Infisical is the sole source of truth** for this app's
secrets, env variables, and tunable settings knobs.  Per-user settings stay in the app's
own store and never go in Infisical.

- Infisical project: **AI Fleet Coordinator** (`9bf7417a-fbbb-42ca-870c-2b45207233f5`), jays-services org.  **`prod` is the only environment** (owner decision 2026-10-10, new projects delete the default dev and staging at creation):  the service reads and writes prod, `INFISICAL_ENVIRONMENT` defaults to `prod`, and any other slug is refused.
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

- `scripts/mac-collab/mac-collab-server.py` (THE BOARD): Mac-local; reads `~/.secrets/mac-collab.env` (canonical) or the environment.  Operator-managed on the Mac, not a hosted service surface.
- `fleet-sentry-monitor/monitor.py`: operator cron script; env-configured on the box that runs it.
- `fleet_rag` CLI tools and MCP scripts: per-invocation env; no long-lived process to host a cache.
- `scripts/admin-panel` (Cloudflare Worker): serverless, no Infisical-reading backend in this repo.
- `workers/pr-conflict-watch`: Cloudflare Worker.
- `RECALL_FAKE=1`: local test/smoke switch only; stays an env var.  Bootstrap plumbing (`RECALL_REF`, `GITLEAKS_*`) likewise stays env-only.

## Rotation

Set a new value in the Infisical UI/API, or `POST /admin/settings` (write-through, immediate).  Background refresh picks UI/API changes up within `SETTINGS_REFRESH_SECONDS`; `POST /admin/reload-settings` or `SIGHUP` forces it now.  Rotating `RECALL_API_TOKEN` requires distributing the new bearer to the seats (coordinate via the board) — the old token stops working as soon as the refresh lands.  Never print, echo, or log a value; verify with key names and the `set`/`empty` flags from `GET /admin/settings` only.

## Prod-only lint

`python3 scripts/check-infisical-env.py` (CI step "Infisical environment lint") fails on anything that would select a non-prod environment:  `INFISICAL_ENV` / `INFISICAL_ENVIRONMENT` set to `dev` or `staging` in an env file (`.cursor/infisical.env`, `.env.example`), a `:-dev` / `:=dev` fallback or `--env=dev` in a `cursor-cloud-start.sh`, and a `.infisical.json` whose `defaultEnvironment` is not `prod`.  `--fleet` lints every repo under `~/Code` as it is on `origin/main` (add `--fetch` to refresh first).  A line that truly needs the word carries `infisical-env: allow` with a reason.  This repo does not call the Infisical CLI, so it has no `.infisical.json`;  a repo that does gets one with `"defaultEnvironment": "prod"`, because the CLI's own default is `dev`.

## Local dev

Copy `.env.example` (non-sensitive keys) and set the secrets in your shell, or point `INFISICAL_MACHINE_IDENTITY` at your handoff file and leave `INFISICAL_ENVIRONMENT` unset (it is `prod`).  Local runs read prod, so treat the secrets there as live.  Any other slug is refused:  `init()`, `set()` and every fetch raise `SettingsError` before the network, `refresh()` logs the refusal and keeps the last-known-good cache, and `configure()` leaves the previous settings in place.  Never commit real values.
