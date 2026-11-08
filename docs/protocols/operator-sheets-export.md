# Operator Registry Sheets Export (OPS-1037)

The Operator's session registry (sqlite store in the `eavesdropper` container) mirrors to the **SWU Operator** Google Sheet (tabs `Sessions` and `Events`).  The registry stays the source of truth; the sheet is a read mirror.  Tab titles are exactly `Sessions` / `Events` and are never renamed by tooling.  The workbook id and the tabs' immutable sheet ids are install-time configuration (the drain's environment, below) — never source constants — so rotating the workbook is a config change, not a code change.

## How it works

- The registry store queues every session/event change in `export_outbox` (kinds `session_upsert`, `event_append`) with the row's cells in canonical registry layout.
- `scripts/simple_tools/operator_sheets_drain.mjs` (the drain runner) runs on the Coolify host under a systemd timer, 5 minutes after the previous run ends (`OnUnitInactiveSec`).  It reads the outbox through the registry's own CLI inside the container (`docker exec eavesdropper python3 -m simple_tools.operator_registry pending-outbox`), so the store is only ever touched through its own code paths.
- `operator_sheets_export.mjs` (`drainOutbox`) reconciles before writing: it scans each tab's complete physical grid once per drain into an ID index.  A short or empty page never ends the scan, and an ID found on more than one row is **ambiguous** — the drain refuses to write it, fences the attempt `ambiguous`, and stops for a human.  Session snapshots of one record inside a batch collapse to the latest; events already present are skipped by Event ID.
- The workbook's Sessions tab carries all 16 canonical columns and its Events tab all 9, so the drain takes its headers from `operator_sheets_export.mjs` (`SESSIONS_HEADERS`, `EVENTS_HEADERS`) and keeps no copy of its own.  The exporter still projects cells by header name (`layout`) and refuses a destination header the payload cannot fill.  Timestamps are display-formatted (America/Chicago, 12-hour, no zone label) from the UTC source cells; the store keeps UTC.
- `operator_sheets_google.mjs` is the Sheets transport.  It is constructed with the workbook's profile (`tables` option: real tab titles, headers, and immutable sheetIds), so identity or header drift fails closed before any write.
- The **export fence** lives in the store, per the Nov 4 ruling: `export_attempts` (schema v2) keyed by outbox_id + destination + tab + kind, outcomes `exported` / `already_present` / `failed` / `ambiguous`.  A row is marked exported only after its write confirms AND its fence row is recorded; a failure in either leaves the row pending and the next drain reconciles by ID.  Fence records and marks flush in batches (`record-export-attempts` takes a batch per call), and the fence CLI ops are deliberately store-local: the registry CLI answers them from the local file even when the registry-API cutover is configured, because the API carries no fence ops and the drain runs on the store's own host.
- Write pacing and quota back-off live in the drain runner (`pacedAdapter`), because the transport never retries.  Sheets writes go out at least 1.1 s apart, about 54 a minute, under the per-minute write quota.  An HTTP 429 is a certain refusal (nothing was written), so the runner sleeps (2 s doubling each time, or the Retry-After if longer, never more than 60 s) and resends it, at most 5 times; after that the error stops the drain and the row stays pending.  Any other failure, and every uncertain write, is never resent blind: the drain stops and the next run reconciles by ID.  A write that needed a 429 retry says so in its fence detail.
- Run limits.  A run repeats passes only while the last pass exported rows and failed none.  Any failed row ends the run, so a write whose outcome is unknown has the timer's 5 quiet minutes to settle before the next run reconciles it, instead of a new pass rescanning at once and appending it again.  A run also ends by itself after 25 minutes, between rows, and the timer resumes a long backfill.  SIGTERM (`systemctl stop`) likewise stops between rows and releases the lease.  Each registry `docker exec` is killed after 120 s.  The unit's `TimeoutStartSec=35min` is only a backstop above those limits.  A row refused for a credential-shaped cell counts as failed too, so while one is pending each run does a single pass (up to 1000 rows).  A registry or header failure outside the row loop also ends the run: its totals are still logged, the rows stay pending, and the unit exits non-zero.  A stop during a 429 back-off is honoured within about a second, with no retry sent.
- One drain at a time: the runner takes a writer lease (lock file, stale-lock steal only when the holder pid no longer exists; a pid owned by another user counts as alive), as the adapter contract requires.
- Credential-shaped cell text is refused per row (fenced `failed`, left pending) without wedging the drain.

## Install (Coolify host)

`scripts/simple_tools/server/install-operator-sheets-drain.sh` does the whole install, and is safe to run again.  Run it as root from a clean AI-Fleet-Coordinator checkout at `origin/main`, after the reviewed drain (PR 1456) has merged.  The script refuses a checkout with uncommitted changes, one not at `origin/main`, or one whose drain predates the review (no `pacedAdapter`, no run deadline, or a timer without `OnUnitInactiveSec`).  Run it with `--dry-run` first, which prints the plan, reports anything a real run would refuse, and changes nothing.

```
sudo OPERATOR_SHEETS_WORKBOOK_ID=<workbook id> \
  OPERATOR_SHEETS_TOKEN_FILE=<path to the token file> \
  INFISICAL_FLEET_OPS_PROJECT_ID=<Fleet-OPS project id> \
  bash scripts/simple_tools/server/install-operator-sheets-drain.sh --dry-run
```

Then run the same command without `--dry-run`.  The caller supplies:

- `OPERATOR_SHEETS_WORKBOOK_ID`: the SWU Operator workbook id.  It is not in this repository, so the caller sets it.
- `OPERATOR_SHEETS_TOKEN_FILE`: a root-owned file, mode 0600 or 0400, holding a scoped read-only Infisical service token for Fleet-OPS prod `/operator-thread-registry`.  Minting it needs the owner's approval, and the script never mints, prints or echoes it, or puts it on a command line.  The script refuses a token whose grant is wider than that folder or can write.
- `INFISICAL_FLEET_OPS_PROJECT_ID`, unless root's `~/.simple-tools/fleet-ops-project-id` already holds it.
- Optional: `OPERATOR_SHEETS_SESSIONS_SHEET_ID` (default 20964676), `OPERATOR_SHEETS_EVENTS_SHEET_ID` (default 1449318075), `OPERATOR_REGISTRY_CONTAINER` (default `eavesdropper`) and `OPERATOR_REGISTRY_DB` (default `/data/operator/registry.sqlite3`).  A swapped pair of sheet ids fails closed, because the transport checks each sheet id's tab title before any write.
- Optional drain knobs: `OPERATOR_SHEETS_PAGE_LIMIT` (1 to 5000) and `OPERATOR_SHEETS_LOCK`.  When one is not set, a value already in `drain.env` is kept, so a hand-set knob survives a rerun.

Before step 1 the script checks that `/etc/operator-sheets`, if it exists, is root-owned with no group or other access, so a loose directory stops the install before anything changes.  Then, in order, it:

1. stops `operator-sheets-drain.timer` if it is running, so no run starts on a half-copied set of modules, and installs the three drain modules (`operator_sheets_export.mjs`, `operator_sheets_google.mjs`, `operator_sheets_drain.mjs`) into `/usr/local/lib/operator-sheets-drain/` (0644, root; a file whose content, mode or owner differs is replaced);
2. reads `OPERATOR_GOOGLE_SHEETS_SA` from Infisical Fleet-OPS prod `/operator-thread-registry` inside its own process through `scripts/fleet_secrets/core.py`, checks it is a service-account key, and writes `/etc/operator-sheets/sa.json` (0600, root, in a 0700 directory);
3. writes `/etc/operator-sheets/drain.env` (0600) with `OPERATOR_SHEETS_SA_FILE`, the workbook id, both sheet ids, the registry container and store, and the knobs, and prints the names (never the values) of settings that changed, were kept or were dropped;
4. runs a preflight, with a 120 s limit on each read, to check that the registry's `pending-outbox` and `export-attempts` reads inside the container return JSON lists (it prints only the row counts).  The second read fails on an image without the export fence CLI or a store not yet at schema v2.  On a failure it prints one line and stops before the units, and leaves a timer it stopped in step 1 stopped;
5. installs `operator-sheets-drain.service` and `operator-sheets-drain.timer` into `/etc/systemd/system/` and runs `systemctl daemon-reload` when a unit changed.  It never enables the timer, and starts it again only if step 1 stopped it.

An unchanged file is left alone.  When the first live drain is cleared, enable the timer by hand with `systemctl enable --now operator-sheets-drain.timer`.  Tests: `python3 scripts/test_install_operator_sheets_drain.py` (offline, and run in CI).  They cover the inputs and the dry run, and the Python block with Infisical faked.  They also run steps 1, 4 and 5 for real in the script's test mode (`OPERATOR_SHEETS_INSTALL_TEST_ROOT`), which puts every destination under a temporary root and refuses to run unless `docker` and `systemctl` there are stubs.

The app redeploy (Eavesdropper image) must land before the first fenced drain: the fence CLI (`record-export-attempt`) ships in the image's copy of `operator_registry.py`, and the store migrates to schema v2 on the app's next write-open.

## Operations

- Run one drain now: `systemctl start operator-sheets-drain.service`, then `journalctl -u operator-sheets-drain -n 50`.  Each pass logs `{pass, exported, failed}` and a final `{done, exported, failed}`.
- Inspect the fence in the store: `docker exec -w /app/scripts eavesdropper python3 -m simple_tools.operator_registry export-attempts --db /data/operator/registry.sqlite3 --limit 20`.
- Pending backlog: same CLI, `pending-outbox --limit 5`.
- An `ambiguous` fence row names the duplicated ID and its sheet rows; resolve by hand in the sheet (delete the wrong duplicate), then let the next timer run reconcile.
- A `failed` row with a credential-shaped-cell detail is a registry data problem, not a transport problem: fix the source record; the row stays pending by design.
