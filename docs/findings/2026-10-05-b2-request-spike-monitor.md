# B2 “request-spike” alert (PD #355) — where it lives and how to verify

Board: `1cf9a5987660413d9f907f09d20ce8bc` (fleet-infra).

## Summary

The PagerDuty incident is **not** emitted by a Mac crontab, LaunchAgent, or a script in **AI-Fleet-Coordinator**.  It is emitted by **Usage Monitor** (`Simple-With-Us/Usage-Monitor`) when the built-in **Backblaze B2** provider trips the generic **`request_anomaly`** statistical alert, which routes to PagerDuty when `ALERT_PAGERDUTY_ROUTING_KEY` is set.

The alert text says **“requests”**, but for Backblaze that metric is **inventoried storage in whole megabytes**, not Backblaze Class A/B/C API transaction counts.  Download and transaction caps remain Backblaze console Caps & Alerts (no fleet repo poller for those).

## Emitter (code paths)

| Layer | Path | Role |
| --- | --- | --- |
| Scheduler | `src/instrumentation.ts` → `startUsagePollingScheduler()` | In-process poller on the Usage Monitor host (not Mac). |
| Tick cadence | `src/lib/usage-recorder.ts` — `POLL_INTERVAL_MS` = 15 minutes | Decides which providers are due; Backblaze default poll interval is **360 minutes** (`provider-definitions.ts`). |
| Inventory | `src/lib/adapters/backblaze.ts` | `b2_authorize_account` + `b2_list_buckets` + `b2_list_file_versions`; sets `totalRequests` = **total storage MB** (see comment at ~line 394). |
| Anomaly | `src/lib/anomaly-loader.ts` + `src/lib/anomaly-detection.ts` | 14-day MAD window, default 3.5σ warning / 5σ critical; builds `request_anomaly` from daily changes in snapshot `totalRequests`. |
| Alert copy | `src/lib/anomaly-detection.ts` — `describeAnomaly()` | Produces strings like `Request spike: 22,662 requests on YYYY-MM-DD … (baseline 1,166 …, 14-day window, 12.4σ).` |
| Delivery | `src/lib/provider-alerts.ts` → `src/lib/alert-delivery.ts` | `request_anomaly` → PagerDuty Events API when configured. |
| Related (not the spike) | `src/lib/platform-status/probes/storage.ts`, `src/lib/fleet-backup-status.ts` | Read-only B2 health / backup inventory using the same monitor key env names. |

Manual/debug trigger (authenticated): `GET /api/cron/fetch-all` with `x-cron-secret` (`src/app/api/cron/fetch-all/route.ts`) — schedule moved in-process; not a Mac job.

## B2 `b2_authorize_account` — correct fields (fleet code)

All fleet callers use **B2 Native API v2** with **HTTP Basic authentication**, not a JSON body on v3/v4:

- **URL:** `https://api.backblazeb2.com/b2api/v2/b2_authorize_account`
- **Header:** `Authorization: Basic base64(applicationKeyId + ":" + applicationKey)`
- **Env (read-only monitor key):** `B2_MONITOR_KEY_ID` + `B2_MONITOR_APPLICATION_KEY`, or `BACKBLAZE_APPLICATION_KEY_ID` + `BACKBLAZE_APPLICATION_KEY` (see `.env.example` and `storage.ts`).

Backblaze’s public v4 examples use the same **Basic** pattern (`curl … -u "keyId:applicationKey"`).  POSTing JSON with `applicationKeyId` / `keyId` to v3/v4 and getting “unknown field” is expected; that is not what the monitor uses.

Secrets file on the owner Mac (`~/.secrets/backblaze-monitor.env`) is the **credential store** referenced in `docs/rollouts/2026-08-06-backblaze-b2-provider.md`; production values are synced via Infisical into Usage Monitor.

## What is counted (requests vs bytes)

- **Alert metric:** day-over-day change in **peak daily `UsageSnapshot.totalRequests`** for the `backblaze` provider row.
- **Semantic:** change in **total inventoried storage (MB)** across buckets (from `list_file_versions`), **not** B2 “requests per day” from billing.
- **Cost path:** separate `spend_anomaly` on estimated storage MTD (`totalCost`); PD #355 wording matches **`request_anomaly`** copy.

Large one-day “request” values (e.g. **22,662**) therefore mean **~22.6 GB net storage growth** in inventory that day (uploads, new versions, or inventory catching up after polls), not 22k API calls.

## How to verify the 2026-10-03 spike

1. **Usage Monitor DB (production):** For provider name `backblaze`, inspect `UsageSnapshot` rows around `2026-10-02` / `2026-10-03` UTC — compare peak `totalRequests` (MB) and `rawData.storage.totalGb` if present.
2. **Alert row:** Provider alert / delivery logs for `request_anomaly` on that date; message should match `describeAnomaly` format above.
3. **Workload correlation (10-03, not 10-04):** On the Coolify/Hetzner host, **`/etc/cron.d/fleet-backups`** runs **`/usr/local/sbin/fleet-sqlite-backup.sh`** (four times daily; full SQLite copies to B2).  Multi‑GB uploads drive large `list_file_versions` inventory and storage MB jumps.  **Do not** attribute the 10-03 signal to 2026-10-04 01:19Z/02:02Z replicator restarts unless snapshots show activity on 10-04 only.
4. **Cross-check:** Usage Monitor **fleet backup status** (`fleet-backup-status.ts`) and app `/api/health` storage fields on ST/CT/UM — optional; spike verification is snapshot + backup cron alignment.

## Repos searched from this investigation

| Repo | Result |
| --- | --- |
| `Simple-With-Us/AI-Fleet-Coordinator` (this repo) | B2 usage: Litestream/mac-collab, Hetzner Qdrant snapshot scripts only — **no** request-spike emitter. |
| `Simple-With-Us/Usage-Monitor` | **Emitter** (adapter + anomaly + PagerDuty delivery). |
| `jaywedgeworth22/BotFleet` | No Backblaze request-spike monitor. |
| `Simple-With-Us/Congress.Trade` | `fleet-sqlite-backup` consumer/host docs; not the PD emitter. |
| `Simple-With-Us/fleet-ops` | Not cloneable from this environment (private/missing). |
| `Simple-With-Us/Socratic.Trade` | B2/Litestream ops only (separate org path in search). |

## Follow-ups (out of scope for this note)

- **Labeling:** `describeAnomaly()` always prints “requests”; Backblaze should say “storage MB” (fix belongs in Usage-Monitor).
- **Gauge vs cumulative:** Anomaly loader treats `totalRequests` peaks with `dailyIncrementsFromCumulative` (designed for MTD counters); Backblaze stores an absolute storage gauge — worth a dedicated anomaly series in UM.
- **Transaction caps:** Use Backblaze console or a future adapter if Class A/B alerting in-app is required.
