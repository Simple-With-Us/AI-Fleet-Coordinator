# Coolify deploy: fleet-telemetry-relay

Runbook for Jay.  Nothing here runs on the Mac; the relay lives on the existing
Coolify host next to Usage-Monitor.  No secret values below, only names.

## 1. Create the service

New Coolify service from this repo, Dockerfile at `telemetry-relay/Dockerfile`
(build arg `OTELCOL_VERSION` defaults to the pinned stable; leave it unless a
newer stable tag is out).  Expose TCP ports `4318`, `4319`, `4320`, `4321`
(producer OTLP intake) and `13133` (health, `GET /` answers 200).  No public
domain is needed unless producers outside the tailnet post to it; keep it
private by default.

Recommended resources: 1 vCPU, 1 GB RAM (the memory_limiter caps the collector
at 512 MB).  Health check: HTTP on `13133`.

## 2. Set the environment (names only)

All values come from Infisical project "AI Fleet Coordinator", `prod`
environment.  Set each name in the Coolify service environment; verify with
names and `set`/`empty` only, never values.

| Name | Kind | Value source |
|---|---|---|
| `SENTRY_FLEET_OTLP_ENDPOINT` | config (URL, not secret) | Sentry > project `fleet-infra` > Settings > Client Keys (DSN) > OpenTelemetry (OTLP): the combined endpoint, shape `https://o<orgId>.ingest.sentry.io/api/<projectId>/integration/otlp` |
| `SENTRY_FLEET_OTLP_KEY` | secret | Same Sentry page: the OTLP public key (`sentry_key`). A DSN public key from the same page works |
| `USAGE_MONITOR_OTLP_METRICS_ENDPOINT` | config (URL, not secret) | Full Usage-Monitor metrics URL, e.g. `https://<usage-monitor-host>/api/otlp/v1/metrics` |
| `USAGE_MONITOR_OTLP_TRACES_ENDPOINT` | config (URL, not secret) | Full Usage-Monitor traces URL, e.g. `https://<usage-monitor-host>/api/otlp/v1/traces`.  The traces route is added by a parallel Usage-Monitor task; until it lands, set this to that URL anyway (4xx is not retried) or to a discard URL |
| `USAGE_MONITOR_INGEST_TOKEN` | secret | Usage-Monitor ingest bearer (`USAGE_INGEST_TOKEN` or a per-producer scoped token) |
| `RELAY_BASE_URL` | config (URL, not secret) | Public or tailnet base of this relay, no port, e.g. `https://<relay-host>`.  `configure-agents.sh` reads it (or `--relay-url`) when minting producer snippets |
| `RELAY_BEARER_CLAUDE_CODE` | secret | Generated: `openssl rand -hex 32`. One value per producer so a leak rotates alone |
| `RELAY_BEARER_CODEX` | secret | Generated, same method |
| `RELAY_BEARER_GROK` | secret | Generated, same method |
| `RELAY_BEARER_SHIM` | secret | Generated, same method |

An unset bearer fails closed: that producer's receiver rejects everything with
401, which is how a missing secret shows up.  Record the four bearer values in
Infisical under exactly these names; `scripts/telemetry/configure-agents.sh`
pulls them by name when minting producer configs.

## 3. Verify

1. `GET <relay>:13133/` answers 200.
2. Post with a wrong bearer, expect 401; post with the right bearer, expect 200.
   Example (Claude port, bearer from Infisical):
   `curl -sS -o /dev/null -w '%{http_code}\n' -H 'Authorization: Bearer <RELAY_BEARER_CLAUDE_CODE>' -H 'Content-Type: application/json' --data '{"resourceLogs":[]}' <relay>:4318/v1/logs`
   A 401 means the token mismatches the relay env; anything else is progress.
3. Point one producer at the relay (snippets from `configure-agents.sh`), run
   one turn, and confirm: the turn's span/log in Sentry `fleet-infra` tagged
   `signal=agent`, and the metrics in Usage-Monitor.
4. Until the parallel Usage-Monitor task ships `POST /api/otlp/v1/traces`, set
   `USAGE_MONITOR_OTLP_TRACES_ENDPOINT` to that URL anyway (4xx is not retried)
   or to a discard URL; metrics flow independently.

## 4. Guardrails before cutover

- In Sentry `fleet-infra`: set spike protection and per-key rate limits before
  pointing the fleet at the relay (span/log volume rises on day one).
- Confirm `fleet-infra` data-scrubbing does not strip `prompt`/`response`
  fields (Jay approved prompts; the relay scrubs secret shapes only).
- Leave the existing `agent-sessions` producers alone.  Cutover (audit D.1) is a
  later relay config change, not part of this deploy.

## 5. Rotate a producer token

New value in Infisical, same name; update the Coolify env; redeploy; re-run
`configure-agents.sh` for that producer and install the one snippet.  Other
producers are unaffected.
