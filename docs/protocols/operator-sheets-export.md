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

## Zulip Threads Sync (AFC-823)

The same drain run also mirrors the listener's chat-thread aggregates to the **Operator Thread
Registry** workbook's `Zulip Threads` tab (design: `docs/operator-thread-registry.md`).  There is
no second unit, timer, lease, token or setting: `operator-sheets-drain.service` runs both exports
under the one writer lease, with the one service-account key file (`/etc/operator-sheets/sa.json`,
from the same scoped Infisical read).

- Read path.  The aggregates live in the listener container's volume
  (`/data/operator/thread-registry.json`), and no listener route serves them.  The drain reads them
  the way it reads the outbox: `docker exec -w /app/scripts eavesdropper python3 -m
  simple_tools.thread_registry export-rows`, the image's own read-only CLI, which prints the
  projected rows as one JSON object.  The call has the same 120 s limit, and an answer over 64 MiB
  is refused.
- Write path.  `runOnce` in `operator_sheets_drain.mjs` runs `syncThreadsOnce` first, then the
  Sessions and Events drain (`drainOnce`) with what is left of the 25-minute run deadline.  The sync
  checks that the payload names the workbook, tab and column order that `thread_registry_export.mjs`
  carries (`SPREADSHEET_ID`, `SHEET_TAB`, `THREAD_HEADERS`;  a test keeps them equal to
  `thread_registry.py`'s), skips and counts any row with a credential-shaped cell or a cell over
  Google's 50,000-character limit, and writes with that module's `drainThreadRows`: header check
  first, upsert by Thread Key, duplicate rows reported and never touched.  Writes go through
  the same `pacedAdapter`, 1.1 s apart with the same HTTP 429 back-off.  The sync never
  touches `export_outbox` or `export_attempts`.
- A human-edited tab.  The read pages to the tab's grid row count, so a blank row never hides
  the rows below it (Google leaves trailing blank rows out of a page, and a short page would
  otherwise end the read and get those rows appended again).  Just before each run of 10
  updates the drain re-reads those rows' Thread Keys, and skips and counts as `moved` any row a
  human sorted, inserted or deleted away;  the next run updates it at its new row.  An
  update writes columns B to M and O only, never the Thread Key or the human-owned Purpose, so
  a Purpose typed during a run survives.
- Idempotent.  The same aggregates at the same age give the same sheet:  a row whose cells
  already match (Registry Updated At aside) is not rewritten.  Status comes from the run's
  clock (`active` up to 24 hours after the last message, `idle` up to 14 days), so a thread
  crossing one of those ages is rewritten once with no new activity;  otherwise a run with no
  new thread activity writes nothing.
- Bounded.  The thread phase stops between writes, and between read pages, after 4 minutes (the
  registry read included) or 200 writes, whichever comes first.  Each Sheets call has a 30 s limit
  and the token request 15 s.  A first backfill of many threads is spread over runs, about 200 rows
  a run, and the Sessions and Events drain keeps at least 20 of its 25 minutes.
- Isolated.  The thread module is loaded only when the phase runs, every failure in the phase is
  caught, and it has its own paced adapter and token cache.  A thread failure therefore cannot stop
  or change the Sessions and Events drain, which runs after it either way, and a Sessions and Events
  failure cannot undo thread rows already written.  The unit's exit status reflects the Sessions
  and Events drain only.
- Counts only.  Each run logs one `{"threads": {...}}` line: `rows`, `refused` (rows with a
  credential-shaped or oversized cell), `updated`, `appended`, `unchanged`, `duplicates`,
  `moved`, `remaining`, `failed` and `ms`, plus a `stopped` reason
  (`signal`, `deadline` or `max_writes`) and one fixed `error` code when set, such as
  `registry_timeout`, `payload_destination_mismatch`, `sheet_header_mismatch` or `sheet_http_403`.
  It never logs topic, participant or header text, or an error message.
- The service account needs Editor on the Operator Thread Registry workbook as well as on the SWU
  Operator workbook.  Without it every run logs `sheet_http_403` and writes no thread rows.

## Install (Coolify host)

`scripts/simple_tools/server/install-operator-sheets-drain.sh` does the whole install, and is safe to run again.  Run it as root from a clean AI-Fleet-Coordinator checkout at `origin/main`, after the reviewed drain (PR 1456) has merged.  The script refuses a checkout with uncommitted changes, one not at `origin/main`, or one whose drain predates the review (no `pacedAdapter`, no run deadline, or a timer without `OnUnitInactiveSec`).  Run it with `--dry-run` first, which prints the plan, reports anything a real run would refuse, and changes nothing.

Use `--code-only` for a reviewed code-bundle update when the existing consumer configuration and systemd units must remain byte-for-byte unchanged.  It requires the exact clean `origin/main` checkout, an existing root-owned installation with `sa.json`, `drain.env`, and both units in their expected modes, plus all three existing core modules.  The thread exporter, shared registry reader, and its Python worker may be absent from an older three-module install and are installed together as one reviewed closure.  The worker is stored inside this consumer's private library directory; it does not overwrite a shared `/usr/local/lib/fleet_infisical.py`.  The reader uses the colocated worker, and only falls back to the repository-relative worker when the colocated path is absent.  It does not read the consumer files, contact Infisical or Docker, or change service/timer units.  It stages the helper, five JavaScript modules, and Python worker from the exact Git commit and syntax-checks the private staged bytes before stopping an active timer, then waits up to 26 minutes for the service to reach a confirmed inactive state.  It saves a root-only backup manifest and module snapshot under `/var/backups/operator-sheets-drain-code-only`, then replaces the modules one file at a time with an atomic rename per file.  Before restoring an active timer, it verifies that all installed modules match either the complete prior snapshot or the complete new bundle; mixed or unverifiable state leaves the timer stopped for inspection.  It never enables the timer.  This mode does not qualify the thread feature for activation: complete the separate review and runtime qualification gates first.

```
sudo OPERATOR_SHEETS_WORKBOOK_ID=<workbook id> \
  OPERATOR_SHEETS_TOKEN_FILE=<path to the token file> \
  INFISICAL_FLEET_OPS_PROJECT_ID=<Fleet-OPS project id> \
  bash scripts/simple_tools/server/install-operator-sheets-drain.sh --dry-run
```

Then run the same command without `--dry-run`.  The caller supplies:

- `OPERATOR_SHEETS_WORKBOOK_ID`: the SWU Operator workbook id.  It is not in this repository, so the caller sets it.
- `OPERATOR_SHEETS_TOKEN_FILE`: a root-owned file, mode 0600 or 0400, holding a scoped read-only Infisical service token for Fleet-OPS prod `/google-sheets-sa`.  Minting it needs the owner's approval, and the script never mints, prints or echoes it, or puts it on a command line.  The script refuses a token whose grant is wider than that folder or can write.
- `INFISICAL_FLEET_OPS_PROJECT_ID`, unless root's `~/.simple-tools/fleet-ops-project-id` already holds it.
- Optional: `OPERATOR_SHEETS_SESSIONS_SHEET_ID` (default 20964676), `OPERATOR_SHEETS_EVENTS_SHEET_ID` (default 1449318075), `OPERATOR_REGISTRY_CONTAINER` (default `eavesdropper`) and `OPERATOR_REGISTRY_DB` (default `/data/operator/registry.sqlite3`).  A swapped pair of sheet ids fails closed, because the transport checks each sheet id's tab title before any write.
- Optional drain knobs: `OPERATOR_SHEETS_PAGE_LIMIT` (1 to 5000) and `OPERATOR_SHEETS_LOCK`.  When one is not set, a value already in `drain.env` is kept, so a hand-set knob survives a rerun.

Before step 1 the script checks that `/etc/operator-sheets`, if it exists, is root-owned with no group or other access, so a loose directory stops the install before anything changes.  Then, in order, it:

1. stops `operator-sheets-drain.timer` if it is running, so no run starts on a half-copied set of
   modules, and installs the six-file code closure (`operator_sheets_export.mjs`,
   `operator_sheets_google.mjs`, `operator_sheets_drain.mjs`, `thread_registry_export.mjs`,
   `registry_export_reader.mjs` and the private `fleet_infisical.py` worker) into
   `/usr/local/lib/operator-sheets-drain/` (0644, root; a file whose content, mode or owner
   differs is replaced);
2. reads `GOOGLE_SHEETS_SA` from Infisical Fleet-OPS prod `/google-sheets-sa` inside its own process through `scripts/fleet_secrets/core.py`, checks it is a service-account key, and writes `/etc/operator-sheets/sa.json` (0600, root, in a 0700 directory);
3. writes `/etc/operator-sheets/drain.env` (0600) with `OPERATOR_SHEETS_SA_FILE`, the workbook id, both sheet ids, the registry container and store, and the knobs, and prints the names (never the values) of settings that changed, were kept or were dropped;
4. runs a preflight, with a 120 s limit on each read, to check that the registry's
   `pending-outbox` and `export-attempts` reads inside the container return JSON lists (it prints
   only the row counts).  The second read fails on an image without the
   export fence CLI or a store not yet at schema v2.  On a failure it
   prints one line and stops before the units, and leaves a timer it stopped in step 1 stopped.  It
   also checks that `thread_registry export-rows` returns the thread rows, and prints only their
   count.  A failure there is a warning and the install goes on, because the Sessions and Events
   drain does not depend on it;
5. installs `operator-sheets-drain.service` and `operator-sheets-drain.timer` into `/etc/systemd/system/` and runs `systemctl daemon-reload` when a unit changed.  It never enables the timer, and starts it again only if step 1 stopped it.

An unchanged file is left alone.  When the first live drain is cleared, enable the timer by hand with `systemctl enable --now operator-sheets-drain.timer`.  Tests: `python3 scripts/test_install_operator_sheets_drain.py` (offline, and run in CI).  They cover the inputs and the dry run, and the Python block with Infisical faked.  They also run steps 1, 4 and 5 for real in the script's test mode (`OPERATOR_SHEETS_INSTALL_TEST_ROOT`), which puts every destination under a temporary root and refuses to run unless `docker` and `systemctl` there are stubs.

The app redeploy (Eavesdropper image) must land before the first fenced drain: the fence CLI (`record-export-attempt`) ships in the image's copy of `operator_registry.py`, and the store migrates to schema v2 on the app's next write-open.

## Operations

- Run one drain now: `systemctl start operator-sheets-drain.service`, then
  `journalctl -u operator-sheets-drain -n 50`.  Each pass logs `{pass, exported, failed}` and a
  final `{done, exported, failed}`.  Before them, each run logs one `{"threads": {...}}` line for
  the Zulip Threads sync (see Zulip Threads Sync above).
- Thread store size, inside the listener: `docker exec -w /app/scripts eavesdropper python3 -m
  simple_tools.thread_registry stats` prints `{threads, messages, store}`.
- Check the Zulip Threads sync after an install, and whenever the tab looks stale.  The
  unit's exit status reflects the Sessions and Events drain only, so a thread failure shows
  only in the journal.  List the recent thread lines and look for `"failed":1` and its
  fixed `error` code;  a healthy run has `"failed":0`:

  ```
  journalctl -u operator-sheets-drain --since -2h -o cat --no-pager | grep '^{"threads"'
  ```

- Stop the thread sync alone (no setting turns it off):  remove the service account's
  Editor share on the Operator Thread Registry workbook, an owner Google action.  Each run
  then logs `sheet_http_403` and writes no thread rows, and the Sessions and Events drain
  carries on.  To take the sync out of the drain, revert the AFC-823 change and, once the
  revert is on `origin/main`, rerun the installer;  the installed
  `thread_registry_export.mjs` is then left in place, unused.
- A manual thread sync:  inside the listener,
  `python3 -m simple_tools.thread_registry export-rows --out rows.json`, then
  `node thread_registry_export.mjs rows.json` on a host with the key prints the plan and
  writes nothing.  `--write` writes, and only while no drain run holds the writer lease
  (`OPERATOR_SHEETS_LOCK`, default `/tmp/operator-sheets-drain.lock`);  stop
  `operator-sheets-drain.timer` and let a running drain finish first.  Rows with credential-shaped cells are refused and counted before anything is written, and only the refusal count is logged.
- Inspect the fence in the store: `docker exec -w /app/scripts eavesdropper python3 -m simple_tools.operator_registry export-attempts --db /data/operator/registry.sqlite3 --limit 20`.
- Pending backlog: same CLI, `pending-outbox --limit 5`.
- An `ambiguous` fence row names the duplicated ID and its sheet rows; resolve by hand in the sheet (delete the wrong duplicate), then let the next timer run reconcile.
- A `failed` row with a credential-shaped-cell detail is a registry data problem, not a transport problem: fix the source record; the row stays pending by design.
