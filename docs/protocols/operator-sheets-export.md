# Operator Registry Sheets Export (OPS-1037)

The Operator's session registry (sqlite store in the `eavesdropper` container) mirrors to the **SWU Operator** Google Sheet (tabs `Sessions` and `Events`).  The registry stays the source of truth; the sheet is a read mirror.  Tab titles are exactly `Sessions` / `Events` and are never renamed by tooling.  The workbook id and the tabs' immutable sheet ids are install-time configuration (the drain's environment, below) — never source constants — so rotating the workbook is a config change, not a code change.

## How it works

- The registry store queues every session/event change in `export_outbox` (kinds `session_upsert`, `event_append`) with the row's cells in canonical registry layout.
- `scripts/agent_sync/operator_sheets_drain.mjs` (the drain runner) runs on the Coolify host under a systemd timer every 5 minutes.  It reads the outbox through the registry's own CLI inside the container (`docker exec eavesdropper python3 -m agent_sync.operator_registry pending-outbox`), so the store is only ever touched through its own code paths.
- `operator_sheets_export.mjs` (`drainOutbox`) reconciles before writing: it scans each tab's complete physical grid once per drain into an ID index.  A short or empty page never ends the scan, and an ID found on more than one row is **ambiguous** — the drain refuses to write it, fences the attempt `ambiguous`, and stops for a human.  Session snapshots of one record inside a batch collapse to the latest; events already present are skipped by Event ID.
- The workbook's Sessions tab carries 10 of the 16 canonical columns; the exporter projects cells by header name (`layout`) and refuses a destination header the payload cannot fill.  Timestamps are display-formatted (America/Chicago, 12-hour, no zone label) from the UTC source cells; the store keeps UTC.
- `operator_sheets_google.mjs` is the Sheets transport.  It is constructed with the workbook's profile (`tables` option: real tab titles, headers, and immutable sheetIds), so identity or header drift fails closed before any write.
- The **export fence** lives in the store, per the Nov 4 ruling: `export_attempts` (schema v2) keyed by outbox_id + destination + tab + kind, outcomes `exported` / `already_present` / `failed` / `ambiguous`.  A row is marked exported only after its write confirms AND its fence row is recorded; a failure in either leaves the row pending and the next drain reconciles by ID.  Fence records and marks flush in batches (`record-export-attempts` takes a batch per call), and the fence CLI ops are deliberately store-local: the registry CLI answers them from the local file even when the registry-API cutover is configured, because the API carries no fence ops and the drain runs on the store's own host.
- One drain at a time: the runner takes a writer lease (lock file, stale-lock steal when the holder pid is dead), as the adapter contract requires.
- Credential-shaped cell text is refused per row (fenced `failed`, left pending) without wedging the drain.

## Install (Coolify host)

1. Copy `operator_sheets_export.mjs`, `operator_sheets_google.mjs`, and `operator_sheets_drain.mjs` from `scripts/agent_sync/` to `/usr/local/lib/operator-sheets-drain/` on the host (they import only each other and Node builtins).
2. Fetch the fleet Sheets service account (`OPERATOR_GOOGLE_SHEETS_SA`, Infisical Fleet-OPS prod, `/operator-thread-registry`) in-process and write it to `/etc/operator-sheets/sa.json`, mode 0600, root-owned.  Never print it or pass it as argv.
3. `/etc/operator-sheets/drain.env` (0600): `OPERATOR_SHEETS_SA_FILE=/etc/operator-sheets/sa.json` plus the target workbook's identity, read from the workbook itself at install time: `OPERATOR_SHEETS_WORKBOOK_ID`, `OPERATOR_SHEETS_SESSIONS_SHEET_ID`, `OPERATOR_SHEETS_EVENTS_SHEET_ID`.  The drain fails closed when any of them is missing.
4. Install `scripts/agent_sync/server/operator-sheets-drain.service` and `operator-sheets-drain.timer` into `/etc/systemd/system/`, `systemctl daemon-reload`, `systemctl enable --now operator-sheets-drain.timer`.

The app redeploy (Eavesdropper image) must land before the first fenced drain: the fence CLI (`record-export-attempt`) ships in the image's copy of `operator_registry.py`, and the store migrates to schema v2 on the app's next write-open.

## Operations

- Run one drain now: `systemctl start operator-sheets-drain.service`, then `journalctl -u operator-sheets-drain -n 50`.  Each pass logs `{pass, exported, failed}` and a final `{done, exported, failed}`.
- Inspect the fence in the store: `docker exec -w /app/scripts eavesdropper python3 -m agent_sync.operator_registry export-attempts --db /data/operator/registry.sqlite3 --limit 20`.
- Pending backlog: same CLI, `pending-outbox --limit 5`.
- An `ambiguous` fence row names the duplicated ID and its sheet rows; resolve by hand in the sheet (delete the wrong duplicate), then let the next timer run reconcile.
- A `failed` row with a credential-shaped-cell detail is a registry data problem, not a transport problem: fix the source record; the row stays pending by design.
