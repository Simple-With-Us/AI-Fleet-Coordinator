# B2 “request-spike” alert (PD #355) — where it lives and how to verify

Board: `1cf9a5987660413d9f907f09d20ce8bc` (fleet-infra).

## Fleet recall (before conclusion)

Queried shared memory with `recall "B2 request spike request_anomaly"` (app: fleet, limit 5) and opened the cited sources before recording the conclusion below.  Ranked hits (leads, not a substitute for the sources):

| Rank | Kind | What it established |
| --- | --- | --- |
| 1 | Board finding `1cf9a5987660413d9f907f09d20ce8bc` | BF-Director timing caveat: 2026-10-04 replicator restarts do not, by themselves, explain a 2026-10-03 spike. |
| 2 | Board / effort row (fleet-infra) | Same incident tracked as “emitting monitor not found” plus B2 v3/v4 auth confusion. |
| 3 | MONITOR lesson (2026-09-15) | **Different** incident (PD #147): Litestream L0/L1 churn, not this PD #355 page. |
| 4 | CODEX runbook (2026-10-05) | Emitter is Usage Monitor `request_anomaly`; the numeric “requests” value is storage MB; do not POST JSON to B2 v3/v4 authorize; verify on 2026-10-03 snapshots and backup cadence, not 10-04 restarts. |
| 5 | Congress.Trade Litestream rollout note | B2 destination policy history; not the PD emitter. |

This note corroborates hit 4, records hit 1’s timing caveat, and keeps credential locations, machine-specific absolute paths, and internal host paths out of public source (generic placeholders only).

## Summary

The PagerDuty incident is **not** emitted by a Mac crontab, LaunchAgent, or a script in **AI-Fleet-Coordinator**.  It is emitted by **Usage Monitor** (`Simple-With-Us/Usage-Monitor`) when the built-in **Backblaze B2** provider trips the generic **`request_anomaly`** statistical alert, which routes to PagerDuty when `ALERT_PAGERDUTY_ROUTING_KEY` is set.

The alert text says **“requests”**, but for Backblaze that metric is **inventoried storage in whole megabytes**, not Backblaze Class A/B/C API transaction counts.  Download and transaction caps remain Backblaze console Caps & Alerts (no fleet repo poller for those).

## Emitter (layers)

Paths below are **repo-relative placeholders** inside Usage Monitor, not host filesystem locations.

| Layer | Where (placeholder) | Role |
| --- | --- | --- |
| Scheduler | `<usage-monitor>/instrumentation` → `startUsagePollingScheduler()` | In-process poller on the Usage Monitor host (not Mac). |
| Tick cadence | `<usage-monitor>/usage-recorder` — `POLL_INTERVAL_MS` = 15 minutes | Decides which providers are due; Backblaze default poll interval is **360 minutes** (`provider-definitions`). |
| Inventory | `<usage-monitor>/adapters/backblaze` | `b2_authorize_account` + `b2_list_buckets` + `b2_list_file_versions`; sets `totalRequests` = **total storage MB**. |
| Anomaly | `<usage-monitor>/anomaly-loader` + `<usage-monitor>/anomaly-detection` | Rolling MAD window over recent daily changes; warning/critical sigma cutoffs from that detector; builds `request_anomaly` from snapshot `totalRequests`. |
| Alert copy | `<usage-monitor>/anomaly-detection` — `describeAnomaly()` | Produces strings like `Request spike: 22,662 requests on YYYY-MM-DD … (baseline 1,166 …, 14-day window, 12.4σ).` |
| Delivery | `<usage-monitor>/provider-alerts` → `<usage-monitor>/alert-delivery` | `request_anomaly` → PagerDuty Events API when configured. |
| Related (not the spike) | `<usage-monitor>/platform-status/probes/storage`, `<usage-monitor>/fleet-backup-status` | Read-only B2 health / backup inventory using the same monitor key env names. |

Manual/debug trigger (authenticated): `GET /api/cron/fetch-all` with `x-cron-secret` (`<usage-monitor>/api/cron/fetch-all`) — schedule moved in-process; not a Mac job.

## B2 `b2_authorize_account` — correct fields (fleet code)

All fleet callers use **B2 Native API v2** with **HTTP Basic authentication**, not a JSON body on v3/v4:

- **URL:** `https://api.backblazeb2.com/b2api/v2/b2_authorize_account`
- **Header:** `Authorization: Basic base64(applicationKeyId + ":" + applicationKey)`
- **Env (read-only monitor key names only):** `B2_MONITOR_KEY_ID` + `B2_MONITOR_APPLICATION_KEY`, or `BACKBLAZE_APPLICATION_KEY_ID` + `BACKBLAZE_APPLICATION_KEY` (see the Usage Monitor env example and the storage probe).

Backblaze’s public v4 examples use the same **Basic** pattern (`curl … -u "keyId:applicationKey"`).  POSTing JSON with `applicationKeyId` / `keyId` to v3/v4 and getting “unknown field” is expected; that is not what the monitor uses.

Credential values are supplied at runtime through the approved secret-management path (Infisical into Usage Monitor).  Do not document local credential-file locations or rollout-note absolute paths in this public tree.

## What is counted (requests vs bytes)

- **Alert metric:** day-over-day change in **peak daily `UsageSnapshot.totalRequests`** for the `backblaze` provider row.
- **Semantic:** change in **total inventoried storage (MB)** across buckets (from `list_file_versions`), **not** B2 “requests per day” from billing.
- **Cost path:** separate `spend_anomaly` on estimated storage MTD (`totalCost`); PD #355 wording matches **`request_anomaly`** copy.

Large one-day “request” values (e.g. **22,662**) therefore mean **~22.6 GB net storage growth** in inventory that day (uploads, new versions, or inventory catching up after polls), not 22k API calls.

## How to verify the 2026-10-03 spike

1. **Usage Monitor DB (production):** For provider name `backblaze`, inspect `UsageSnapshot` rows around `2026-10-02` / `2026-10-03` UTC — compare peak `totalRequests` (MB) and `rawData.storage.totalGb` if present.
2. **Alert row:** Provider alert / delivery logs for `request_anomaly` on that date; message should match `describeAnomaly` format above.
3. **Workload correlation (10-03, not 10-04):** On the Coolify/Hetzner host, the **fleet SQLite backup cron** (`<host-cron.d>/fleet-backups` → `<host-sbin>/fleet-sqlite-backup.sh`) runs four times daily and copies full SQLite files to B2.  Multi‑GB uploads drive large `list_file_versions` inventory and storage MB jumps.  **Do not** attribute the 10-03 signal to 2026-10-04 01:19Z/02:02Z replicator restarts unless snapshots show activity on 10-04 only.
4. **Cross-check:** Usage Monitor **fleet backup status** (`<usage-monitor>/fleet-backup-status`) and app `/api/health` storage fields on ST/CT/UM — optional; spike verification is snapshot + backup cron alignment.

This verification is the operator checklist for PD #355-class pages.  It does **not** claim the 2026-10-03 `UsageSnapshot` peaks were re-queried from production in this documentation-only change; confirm those rows live before treating a benign-backup story as closed.

## Repos searched from this investigation

| Repo | Result |
| --- | --- |
| `Simple-With-Us/AI-Fleet-Coordinator` (this repo) | B2 usage: Litestream/mac-collab, Hetzner Qdrant snapshot scripts only — **no** request-spike emitter. |
| `Simple-With-Us/Usage-Monitor` | **Emitter** (adapter + anomaly + PagerDuty delivery). |
| `jaywedgeworth22/BotFleet` | No Backblaze request-spike monitor. |
| `Simple-With-Us/Congress.Trade` | Fleet SQLite backup consumer/host docs; not the PD emitter. |
| `Simple-With-Us/fleet-ops` | Not cloneable from this environment (private/missing). |
| `Simple-With-Us/Socratic.Trade` | B2/Litestream ops only (separate org path in search). |

## Follow-ups (out of scope for this note)

- **Labeling:** `describeAnomaly()` always prints “requests”; Backblaze should say “storage MB” (fix belongs in Usage-Monitor).
- **Gauge vs cumulative:** Anomaly loader treats `totalRequests` peaks with `dailyIncrementsFromCumulative` (designed for MTD counters); Backblaze stores an absolute storage gauge — worth a dedicated anomaly series in UM.
- **Transaction caps:** Use Backblaze console or a future adapter if Class A/B alerting in-app is required.
